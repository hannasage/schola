"""Validate learning events and learning event logs with the shared schemas.

This module uses only the Python standard library. It reads
`contracts/learning-event.v1.schema.json` and `contracts/lesson-common.v1.schema.json`
and gives the same result shape as `contracts/validate-events.mjs`.

It reuses the schema engine in `lesson_contract.py` (`SchemaRegistry`) and its
public constants. It does not change that file. `lesson_contract.load_contract_registry`
loads only the three lesson kinds, so this module builds its own registry from the
two schema files it needs.

Event results are `{'ok': True}` or `{'ok': False, 'reason': ..., 'errors': [...]}`.
Each error is `{'pointer': ..., 'message': ...}`. The reason is one of `invalid`,
`unsupported_version`, or `too_large`.

A log is JSON Lines text: one event per line, UTF-8, each line at most 4 KB.
`validate_log_text` returns `{'ok', 'lines', 'truncatedTail'}`. Each entry in
`lines` is `{'line': n, 'ok': True}` or `{'line': n, 'ok': False, 'reason': ..., 'errors': [...]}`.
A last line with no newline that is not complete JSON has the reason `truncated_tail`.
A log that cannot be split into lines at all (too large, not UTF-8, not text, or a file
that cannot be read) returns `ok` false, a `reason`, `errors`, an empty `lines` list,
and `truncatedTail` false.

A call never changes its input and never writes a file. Files are opened for
reading only. Both validators reject an object that repeats a key, at any depth.
"""
import errno
import functools
import json
import os
import re
import stat

import lesson_contract as lc
from lesson_contract import SchemaError, SchemaRegistry, UnsupportedKeywordError  # noqa: F401

SUPPORTED_SCHEMA_MAJORS = lc.SUPPORTED_SCHEMA_MAJORS
# The largest log, in bytes, that a reader parses. It matches the lesson contract cap.
MAX_BYTES = lc.MAX_BYTES
# The largest single line, in bytes, not counting the newline.
MAX_LINE_BYTES = 4096
# The most lines a log can hold. A valid event is over 300 bytes, so 5 MB holds far fewer.
MAX_LINES = 50000
MAX_ERRORS = lc.MAX_ERRORS
EVENT_SCHEMA_ID = 'urn:schola:contract:learning-event:v1'
EVENT_TYPES = (
    'attempt_submitted', 'check_result', 'hint_requested', 'agent_offer', 'reflection_saved',
    'task_opened', 'task_finished', 'module_completed', 'mode_changed', 'review_result',
)
_SCHEMA_FILES = ('lesson-common.v1.schema.json', 'learning-event.v1.schema.json')


@functools.lru_cache(maxsize=1)
def event_registry():
    """The registry for the two schema files that events need."""
    schemas = []
    for name in _SCHEMA_FILES:
        try:
            schemas.append(json.loads((lc.CONTRACTS_DIRECTORY / name).read_text(encoding='utf-8')))
        except (OSError, ValueError) as error:
            raise SchemaError(f'The schema file {name} cannot be read as JSON.') from error
    return SchemaRegistry(schemas)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def _error(pointer, message):
    return {'pointer': pointer, 'message': message}


def _invalid(errors):
    if len(errors) <= MAX_ERRORS:
        return {'ok': False, 'reason': 'invalid', 'errors': errors}
    omitted = len(errors) - MAX_ERRORS
    return {'ok': False, 'reason': 'invalid', 'errors': errors[:MAX_ERRORS] + [_error('', f'{omitted} more errors omitted.')]}


def _too_large_log():
    return _whole_log_failure('too_large', 'The log is larger than the 5 MB limit.')


def _too_large_line():
    return {'ok': False, 'reason': 'too_large', 'errors': [_error('', 'The line is larger than the 4 KB limit.')]}


def _whole_log_failure(reason, message):
    return {'ok': False, 'reason': reason, 'errors': [_error('', message)], 'lines': [], 'truncatedTail': False}


def _unreadable(error):
    name = errno.errorcode.get(error.errno, 'unknown') if isinstance(error, OSError) and error.errno else 'unknown'
    return _whole_log_failure('unreadable', f'The file could not be read ({name}).')


# ---------------------------------------------------------------------------
# Semantic checks: rules that a schema cannot state
# ---------------------------------------------------------------------------

_STAMP_DAY = re.compile(r'^([0-9]{4})-([0-9]{2})-([0-9]{2})T')


def _days_in_month(year, month):
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def semantic_errors(event):
    """The semantic errors for an event that already passed its schema."""
    errors = []
    match = _STAMP_DAY.match(event['time'])
    if match and int(match.group(3)) > _days_in_month(int(match.group(1)), int(match.group(2))):
        errors.append(_error('/time', 'The time names a day that does not exist.'))
    if event['type'] == 'agent_offer' and event['data']['triggerEventId'] == event['eventId']:
        errors.append(_error('/data/triggerEventId', 'An offer cannot be triggered by its own event.'))
    return errors


# ---------------------------------------------------------------------------
# One event
# ---------------------------------------------------------------------------

_MAJOR_MARKER = re.compile(r'^([0-9]+)\.')


def _is_supported_major(digits):
    stripped = digits.lstrip('0')
    # A longer number cannot be a supported major, and this avoids converting a huge digit string.
    return len(stripped) <= 9 and int(stripped or '0') in SUPPORTED_SCHEMA_MAJORS


def _run_document(document):
    """Validate a parsed event. Returns (result, stage)."""
    if not isinstance(document, dict):
        return _invalid([_error('', 'The document must be a JSON object.')]), 'document'
    # Read the leading major number first. A value such as 2.0 is an unsupported version, not a malformed one.
    marker = _MAJOR_MARKER.match(document['schemaVersion']) if isinstance(document.get('schemaVersion'), str) else None
    if marker and not _is_supported_major(marker.group(1)):
        message = f"This reader supports schema major version {', '.join(str(m) for m in SUPPORTED_SCHEMA_MAJORS)} only."
        return {'ok': False, 'reason': 'unsupported_version', 'errors': [_error('/schemaVersion', message)]}, 'version'
    found = event_registry().validate(EVENT_SCHEMA_ID, document)
    if found:
        return _invalid([_error(pointer, message) for pointer, message in found]), 'schema'
    errors = semantic_errors(document)
    if errors:
        return _invalid(errors), 'semantic'
    return {'ok': True}, 'none'


def validate_event(document):
    """Validate one parsed event. The document is read and never changed."""
    return _run_document(document)[0]


def _reject_constant(name):
    raise ValueError(f'{name} is not allowed in JSON.')


class _RepeatedKey(ValueError):
    """An object repeats a key."""


def _no_repeats(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _RepeatedKey('An object repeats a key.')
    return dict(pairs)


def _byte_length(text):
    return len(text.encode('utf-8', errors='surrogatepass'))


def _run_text(text):
    """Validate one line of text. Returns (result, stage, document), where document is None unless the text parsed."""
    if isinstance(text, (bytes, bytearray)):
        if len(text) > MAX_LINE_BYTES:
            return _too_large_line(), 'size', None
        try:
            text = bytes(text).decode('utf-8')
        except UnicodeError:
            return _invalid([_error('', 'The text is not valid UTF-8.')]), 'parse', None
    if not isinstance(text, str):
        return _invalid([_error('', 'The text must be a string.')]), 'parse', None
    if _byte_length(text) > MAX_LINE_BYTES:
        return _too_large_line(), 'size', None
    try:
        document = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_repeats)
    except _RepeatedKey:
        return _invalid([_error('', 'The text is not complete, valid JSON.')]), 'repeat', None
    except (ValueError, RecursionError):
        return _invalid([_error('', 'The text is not complete, valid JSON.')]), 'parse', None
    try:
        result, stage = _run_document(document)
    except RecursionError:
        return _invalid([_error('', 'The document is nested too deeply.')]), 'parse', None
    return result, stage, document


def validate_event_text_with_stage(text):
    """Like validate_event_text, and also names the stage that gave the result.

    The stage is `none`, `size`, `parse`, `repeat`, `document`, `version`, `schema`, or
    `semantic`. The stage `repeat` means valid JSON in which an object repeats a key. Returns (result, stage).
    """
    result, stage, _ = _run_text(text)
    return result, stage


def validate_event_text(text):
    """Parse one line of JSON, then validate it. Text over MAX_LINE_BYTES is refused before parsing."""
    return validate_event_text_with_stage(text)[0]


# ---------------------------------------------------------------------------
# A whole log
# ---------------------------------------------------------------------------

def _line_result(number, result):
    if result['ok']:
        return {'line': number, 'ok': True}
    return {'line': number, 'ok': False, 'reason': result['reason'], 'errors': result['errors']}


def _decode_line(chunk):
    """Decode the bytes of one line. Returns (text, status). The status is `ok`, `cut`, or `bad`.

    `cut` means the only fault is a character left incomplete at the very end.
    """
    try:
        return chunk.decode('utf-8'), 'ok'
    except UnicodeDecodeError as error:
        if error.reason == 'unexpected end of data' and error.end == len(chunk):
            return None, 'cut'
        return None, 'bad'


def _log_parts(source):
    """Split a log into lines at the newline character only.

    Returns (parts, tail_has_newline). Each part is (text, byte_length, status), where text is None
    when the line is not valid UTF-8. Bytes are decoded one line at a time.
    """
    if isinstance(source, str):
        pieces = source.split('\n')
        tail_has_newline = pieces[-1] == ''
        if tail_has_newline:
            pieces.pop()
        return [(piece, None, 'ok') for piece in pieces], tail_has_newline
    pieces = source.split(b'\n')
    tail_has_newline = pieces[-1] == b''
    if tail_has_newline:
        pieces.pop()
    parts = []
    for chunk in pieces:
        if len(chunk) > MAX_LINE_BYTES:
            parts.append(('', len(chunk), 'ok'))
            continue
        text, status = _decode_line(chunk)
        parts.append((text, len(chunk), status))
    return parts, tail_has_newline


def _line_count(source):
    newlines = source.count('\n' if isinstance(source, str) else b'\n')
    if not source or source[-1:] in ('\n', b'\n'):
        return newlines
    return newlines + 1


def validate_log_text(text):
    """Validate JSON Lines text, one event per line. Text over MAX_BYTES is refused before parsing.

    Lines end at the newline character only. Bytes are decoded one line at a time, so a
    line that is not UTF-8 damages that line only. A final line with no newline that is not
    complete JSON, or that ends inside a character, is reported as `truncated_tail`. A repeated
    event id is reported on the later line. A log of more than MAX_LINES lines is refused as a whole.
    """
    if isinstance(text, bytearray):
        text = bytes(text)
    if isinstance(text, bytes):
        if len(text) > MAX_BYTES:
            return _too_large_log()
    elif not isinstance(text, str):
        return _whole_log_failure('invalid', 'The text must be a string.')
    elif _byte_length(text) > MAX_BYTES:
        return _too_large_log()
    if _line_count(text) > MAX_LINES:
        return _whole_log_failure('too_large', 'The log has more than 50000 lines.')
    parts, tail_has_newline = _log_parts(text)
    lines = []
    truncated_tail = False
    seen = set()
    for index, (part, size, status) in enumerate(parts):
        number = index + 1
        is_tail = index == len(parts) - 1 and not tail_has_newline
        if size is None:
            size = _byte_length(part)
        if size > MAX_LINE_BYTES:
            lines.append(_line_result(number, _too_large_line()))
            continue
        if status == 'cut' and is_tail:
            truncated_tail = True
            lines.append(_line_result(number, _truncated()))
            continue
        if status != 'ok':
            lines.append(_line_result(number, _invalid([_error('', 'The line is not valid UTF-8.')])))
            continue
        if part == '':
            # A blank line is a fast path: it needs no parse.
            lines.append(_line_result(number, _invalid([_error('', 'The text is not complete, valid JSON.')])))
            continue
        result, stage, document = _run_text(part)
        if is_tail and stage == 'parse':
            truncated_tail = True
            result = _truncated()
        elif isinstance(document, dict) and isinstance(document.get('eventId'), str) and result.get('reason') != 'unsupported_version':
            # Every parsed line counts, valid or not. A line of an unsupported version does not, because
            # its fields mean something else to this reader.
            event_id = document['eventId']
            if event_id in seen:
                errors = [] if result['ok'] else result['errors']
                if len(errors) <= MAX_ERRORS:
                    result = _invalid(errors + [_error('/eventId', 'Each event id must be unique in a log. This one is repeated.')])
            seen.add(event_id)
        lines.append(_line_result(number, result))
    return {'ok': all(line['ok'] for line in lines), 'lines': lines, 'truncatedTail': truncated_tail}


def _truncated():
    return {'ok': False, 'reason': 'truncated_tail', 'errors': [_error('', 'The last line is cut short. It has no newline and is not complete JSON.')]}


def validate_log_file(path):
    """Read a log file and validate it. The file is opened for reading only.

    A file over MAX_BYTES is refused after a size check, before it is read.
    """
    try:
        info = os.stat(path)
        if info.st_size > MAX_BYTES:
            return _too_large_log()
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise OSError(errno.EINVAL, 'not a regular file')
        with open(path, 'rb') as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as error:
        return _unreadable(error)
    if len(data) > MAX_BYTES:
        return _too_large_log()
    return validate_log_text(data)
