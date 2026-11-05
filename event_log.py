"""Store learning events in one local append-only log.

Everything lives under one folder, `_events/`, inside the data folder. A curriculum
id cannot start with an underscore, so the folder never collides with a curriculum
folder, and older builds ignore it.

    _events/
      events.jsonl         the log, one event per line (the format is in contracts/learning-events.md)
      .events.lock         the lock file; it holds no data and is never deleted
      reflections/         one file for each reflection, apart from the log

Rules
-----
* Every event is checked by `learning_event`, the validator of the contract. This
  module holds no copy of the rules. An invalid event writes nothing.
* One writer at a time. A write (an event or a reflection) holds an exclusive `flock` on
  `.events.lock`. A reader holds a shared one, so it never sees a line that a writer has
  half written. The lock works between threads and between processes of one computer. It
  does not stop a program that ignores the lock and edits the file.
* A write is one `write` call in append mode, then `fsync`. A new log, a new folder, or a
  folder found empty also flushes the folders that hold it. A flush that failed is tried
  again before the next write. A failed step is an error, never a success.
* The caps of the contract are enforced on write: 4096 bytes per line, 5 MB per file,
  50000 lines, and a unique event id. A write that goes over a cap is refused with
  `log_full`. The store never rotates the log and never deletes a line.
* The page may not fill the log. `append(event, from_page=True)` refuses with `log_reserved`
  once the log holds 80% of the bytes or of the lines. The app keeps the rest.
* A `reflection_saved` event needs a stored reflection with the same id and `charCount`.
* A log that does not end in a newline (a cut line from a crash or a short write)
  gets one newline before the next event. Earlier bytes are never rewritten.
* A read reports damage and never repairs it. The report is checked once for each version
  of the file and kept while the file is unchanged. `delete` is the only way the store
  removes the log and the reflections, and only with `confirm=True`.
* Reflection text is kept in separate files, one for each reflection, apart from the log.
  There are at most 1000 reflections and 16 MB of them.

Only the Python standard library is used. macOS and Linux are supported. Windows has no
`fcntl` and every write there fails with `lock_unavailable`.
"""
import contextlib
import copy
import errno
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

import learning_event as le
from lesson_store import _fsync_dir
from local_store import StoreError

EVENTS_DIRECTORY = '_events'
LOG_FILE = 'events.jsonl'
LOCK_FILE = '.events.lock'
REFLECTIONS_DIRECTORY = 'reflections'
REFLECTION_VERSION = 1
# The longest reflection, in characters. It matches `charCount` in contracts/learning-event.v1.schema.json.
MAX_REFLECTION_CHARS = 10000
# The biggest reflection file. 10000 characters take at most 6 bytes each as JSON text, plus the fields around them.
MAX_REFLECTION_BYTES = 64 * 1024
# The most reflections, and the most bytes of them, that the store keeps.
MAX_REFLECTIONS = 1000
MAX_REFLECTION_STORE_BYTES = 16 * 1024 * 1024
# The page may not add an event once the log holds this much. The app may use the rest.
RESERVE_BYTES = le.MAX_BYTES * 4 // 5
RESERVE_LINES = le.MAX_LINES * 4 // 5
LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.005
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)  # a FIFO in place of a file must not hang an open

_REFLECTION_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.ASCII)
_REFLECTION_FILE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json', re.ASCII)
_REFLECTION_FIELDS = {'version', 'reflectionId', 'charCount', 'text'}
_REFUSALS = {'invalid': ('invalid_event', 422), 'unsupported_version': ('unsupported_version', 409), 'too_large': ('event_too_large', 413)}


class EventLogError(StoreError):
    """An event log problem with a stable code. `details` holds optional findings."""

    def __init__(self, message, code, status=500, field=None, details=None, retryable=False):
        super().__init__(message, code, status, field, retryable)
        self.details = details

    def payload(self):
        result = super().payload()
        if self.details is not None:
            result['details'] = self.details
        return result


def _errno_name(error):
    """The error code of an OSError, for a message. A path or a file name never goes in a message."""
    return errno.errorcode.get(error.errno, 'unknown') if error.errno else 'unknown'


class _State:
    """What one pass over the log file found, tied to the file it came from."""

    def __init__(self, signature, size, lines, ends_clean, ids):
        self.signature = signature  # (size, modified time, inode); None when the file does not exist
        self.size = size
        self.lines = lines
        self.ends_clean = ends_clean  # true when the file is empty or ends in a newline
        self.ids = ids  # every event id on a line that parses, valid or not

    @property
    def missing(self):
        return self.signature is None


class _Checked:
    """The bytes of the log, the validator report for them, and the valid events, tied to the file they came from."""

    def __init__(self, signature, data, report, events):
        self.signature, self.data, self.report, self.events = signature, data, report, events


def _signature(info):
    return (info.st_size, info.st_mtime_ns, info.st_ino)


def _scan(data, signature):
    """Count lines and collect event ids from the bytes of a log. Lines that do not parse add no id.

    A line that does not parse is damage. `read` reports it, so skipping it here hides nothing.
    """
    pieces = data.split(b'\n')
    ends_clean = not data or data.endswith(b'\n')
    lines = len(pieces) - 1 if ends_clean else len(pieces)
    ids = set()
    for piece in pieces:
        # Like the validators, do not parse a line over the limit. An id there counts for nothing.
        if not piece or len(piece) > le.MAX_LINE_BYTES:
            continue
        try:
            document = json.loads(piece.decode('utf-8'))
        except (UnicodeError, ValueError, RecursionError):
            continue
        if isinstance(document, dict) and isinstance(document.get('eventId'), str):
            ids.add(document['eventId'])
    return _State(signature, len(data), lines, ends_clean, ids)


def _failure(message, code, partial=False, maybe=False, **more):
    """A write failure with the facts a caller needs: did part of a line land, and may the data be there."""
    return EventLogError(message, code, details=dict({'partialLine': partial, 'maybeWritten': maybe}, **more))


class EventLog:
    def __init__(self, data_dir, lock_timeout=None):
        self.data = Path(data_dir).absolute()
        self.folder = self.data / EVENTS_DIRECTORY
        self.path = self.folder / LOG_FILE
        self.reflections = self.folder / REFLECTIONS_DIRECTORY
        self.lock_timeout = LOCK_TIMEOUT_SECONDS if lock_timeout is None else lock_timeout
        self._state = None  # the last scan of the file for writes, reused while the file is unchanged
        self._checked = None  # the last validated read, reused while the file is unchanged
        self._unflushed = set()  # folders whose last flush failed; the next write flushes them first

    # ------------------------------------------------------------------
    # Paths, folders, and low level file work
    # ------------------------------------------------------------------

    def _safe(self, path):
        path = Path(path)
        current = self.data
        for part in path.relative_to(self.data).parts:
            current = current / part
            if current.is_symlink():
                raise EventLogError('Data paths cannot be symbolic links.', 'unsafe_path')
        return path

    def _check_folders(self):
        """Refuse a store whose folders are not folders. A missing folder is fine: nothing was written yet."""
        for path in (self.folder, self.reflections):
            self._safe(path)
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                return
            except OSError as error:
                raise EventLogError(f'The event folder cannot be read ({_errno_name(error)}).', 'log_unreadable') from error
            if not stat.S_ISDIR(info.st_mode):
                raise EventLogError('Part of the event store is not a folder. Schola left it as it is.', 'log_corrupt')

    def _folder_exists(self):
        self._check_folders()
        return self.folder.is_dir()

    def _require_private(self, path):
        """A folder that already exists must be a real folder that belongs to this user and that no one else can use."""
        try:
            info = os.lstat(path)
        except OSError as error:
            raise _failure(f'The event folder cannot be read ({_errno_name(error)}). Nothing was written.', 'log_unreadable') from error
        if not stat.S_ISDIR(info.st_mode):
            raise EventLogError('Part of the event store is not a folder. Schola left it as it is.', 'log_corrupt')
        if (hasattr(os, 'getuid') and info.st_uid != os.getuid()) or info.st_mode & 0o077:
            raise EventLogError('An event folder must belong to you and be closed to other users (mode 0700). Schola left it as it is.', 'unsafe_path')

    def _is_empty(self, path):
        try:
            with os.scandir(path) as entries:
                return next(entries, None) is None
        except OSError as error:
            raise _failure(f'The event folder cannot be read ({_errno_name(error)}). Nothing was written.', 'write_failed') from error

    def _flush(self, directories, what, maybe=False, note='', **more):
        """Flush folders to disk. A folder that fails stays on the unflushed list, so the next write tries it again."""
        self._unflushed.update(directories)
        for directory in directories:
            try:
                _fsync_dir(directory)
            except OSError as error:
                raise _failure(f'Schola could not flush {what} to disk ({_errno_name(error)}).{note}', 'fsync_failed', maybe=maybe, **more) from error
            self._unflushed.discard(directory)

    def _flush_pending(self):
        if self._unflushed:
            self._flush(sorted(self._unflushed, key=str), 'an event folder')

    def _make_folder(self):
        """Create `_events/` (and the data folder, private, when it is missing). Flush every folder that gained an entry."""
        self._flush_pending()
        self._safe(self.folder)
        to_flush = []
        try:
            self.data.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(self.data, 0o700)
                to_flush.append(self.data.parent)
            except FileExistsError:
                pass
            try:
                os.mkdir(self.folder, 0o700)
                to_flush.append(self.data)
            except FileExistsError:
                self._require_private(self.folder)
                if self._is_empty(self.folder):  # a crash can leave a folder whose entry never reached the disk
                    to_flush.extend([self.data, self.folder])
        except OSError as error:
            raise _failure(f'Schola could not create the event folder ({_errno_name(error)}). Nothing was written.', 'write_failed') from error
        self._flush(to_flush, 'the event folder')

    @contextlib.contextmanager
    def _locked(self, exclusive):
        """Hold the log lock. The folder must exist. The lock file is made when it is missing, for a reader too."""
        if fcntl is None:
            raise EventLogError('This system cannot lock the event log. Nothing was changed.', 'lock_unavailable', 503)
        try:
            descriptor = os.open(self.folder / LOCK_FILE, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _NONBLOCK, 0o600)
        except OSError as error:
            raise EventLogError(f'Schola could not open the event log lock ({_errno_name(error)}). Nothing was changed.', 'lock_unavailable', 503) from error
        try:
            try:
                regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
            except OSError as error:
                raise EventLogError(f'Schola could not check the event log lock ({_errno_name(error)}). Nothing was changed.', 'lock_unavailable', 503) from error
            if not regular:
                raise EventLogError('The event log lock is not a regular file. Nothing was changed.', 'lock_unavailable', 503)
            self._take(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            # Closing releases the lock even when the system reports an error, and the work is already done. The error that is on its way out stays the one to report.
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def _take(self, descriptor, mode):
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise EventLogError('Another program is using the event log. Try again in a moment. Nothing was changed.',
                                        'lock_unavailable', 503, retryable=True) from None
                time.sleep(_LOCK_POLL_SECONDS)
            except OSError as error:
                raise EventLogError(f'Schola could not lock the event log ({_errno_name(error)}). Nothing was changed.', 'lock_unavailable', 503) from error

    def _stat_log(self):
        """The stat of the log file, or None when there is none. Anything but a regular file is `log_corrupt`."""
        self._safe(self.path)
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise EventLogError(f'The event log cannot be read ({_errno_name(error)}).', 'log_unreadable') from error
        if not stat.S_ISREG(info.st_mode):
            raise EventLogError('The event log is not a regular file. Schola left it as it is.', 'log_corrupt')
        return info

    def _read_log(self, oversize_code):
        """Read the whole log. Returns (bytes, stat), or None when there is no log. A file over the size limit raises `oversize_code`."""
        self._safe(self.path)
        try:
            descriptor = os.open(self.path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise EventLogError('Data paths cannot be symbolic links.', 'unsafe_path') from error
            raise EventLogError(f'The event log cannot be read ({_errno_name(error)}).', 'log_unreadable') from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise EventLogError('The event log is not a regular file. Schola left it as it is.', 'log_corrupt')
            with os.fdopen(descriptor, 'rb') as handle:
                descriptor = None  # the file object owns it now
                data = handle.read(le.MAX_BYTES + 1)
        except OSError as error:
            raise EventLogError(f'The event log cannot be read ({_errno_name(error)}).', 'log_unreadable') from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(data) > le.MAX_BYTES:
            raise EventLogError('The event log is over its 5 MB limit. Schola left it as it is.', oversize_code, 409 if oversize_code == 'log_full' else 500)
        return data, info

    def _load_state(self):
        """The scan of the log as it is now. The cached scan is used only when the file is unchanged. Call with the lock held."""
        info = self._stat_log()
        if info is None:
            self._state = None
            return _State(None, 0, 0, True, set())
        if self._state is not None and self._state.signature == _signature(info):
            return self._state
        self._state = None
        found = self._read_log('log_full')
        if found is None:
            return _State(None, 0, 0, True, set())
        data, read_info = found
        self._state = _scan(data, _signature(read_info))
        return self._state

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    @staticmethod
    def _line_for(event):
        """The bytes of one line for an event, with the newline, after the validator has accepted it. Returns (bytes, parsed event)."""
        try:
            text = json.dumps(event, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise EventLogError('An event must be a JSON object. Nothing was written.', 'invalid_event', 422) from None
        result = le.validate_event_text(text)
        if not result['ok']:
            code, status = _REFUSALS[result['reason']]
            raise EventLogError('The event does not follow the learning event contract. Nothing was written.', code, status, details=result['errors'])
        try:
            line = text.encode('utf-8')
        except UnicodeError:
            raise EventLogError('An event must be UTF-8 text. Nothing was written.', 'invalid_event', 422) from None
        # The validator already refuses a longer line. This keeps the file rule true if the validator ever changes.
        if len(line) > le.MAX_LINE_BYTES:
            raise EventLogError('The event is over the 4096 byte line limit. Nothing was written.', 'event_too_large', 413)
        return line + b'\n', json.loads(text)

    def _require_reflection(self, data):
        """A `reflection_saved` event must name a stored reflection with the same character count."""
        mismatch = EventLogError('No saved reflection has this id and character count. Save the reflection first. Nothing was written.',
                                 'reflection_mismatch', 422, field='data.reflectionId')
        try:
            stored = self.get_reflection(data['reflectionId'])
        except EventLogError as error:
            if error.code == 'reflection_not_found':
                raise mismatch from None
            raise
        if stored['charCount'] != data['charCount']:
            raise mismatch

    def append(self, event, from_page=False):
        """Check one event and add it to the log as one line. Returns `{appended, eventId, line, bytes, newlineAdded}`.

        `from_page` is true for an event that came from the web page. Such an event is refused with `log_reserved`
        once the log is 80% full, so the page cannot leave no room for the app.

        Raises `EventLogError`. After `write_failed` or `fsync_failed` the error `details` say whether a
        partial last line may be in the file. The next append treats such a line as a truncated tail.
        """
        line, document = self._line_for(event)
        if document['type'] == 'reflection_saved':
            self._require_reflection(document['data'])
        self._safe(self.path)
        self._check_folders()
        self._make_folder()
        with self._locked(exclusive=True):
            return self._append_locked(document['eventId'], line, from_page)

    def _append_locked(self, event_id, line, from_page):
        state = self._load_state()
        if event_id in state.ids:
            raise EventLogError('An event with this id is already in the log. Nothing was written.', 'duplicate_event_id', 409, field='eventId')
        newline = b'' if state.ends_clean else b'\n'
        data = newline + line
        if from_page and (state.size >= RESERVE_BYTES or state.lines >= RESERVE_LINES):
            raise EventLogError('The event log is nearly full. The page cannot add more events. Nothing was written.', 'log_reserved', 409)
        if state.lines + 1 > le.MAX_LINES or state.size + len(data) > le.MAX_BYTES:
            raise EventLogError('The event log is full. Schola does not rotate or delete it. Nothing was written.', 'log_full', 409)
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _NONBLOCK, 0o600)
        except OSError as error:
            raise _failure(f'Schola could not open the event log for writing ({_errno_name(error)}). Nothing was written.', 'write_failed') from error
        self._state = None  # from here the file may change, so the next call scans it again
        try:
            try:
                regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
            except OSError as error:
                raise _failure(f'Schola could not check the event log ({_errno_name(error)}). Nothing was written.', 'write_failed') from error
            if not regular:
                raise EventLogError('The event log is not a regular file. Schola left it as it is.', 'log_corrupt')
            try:
                written = os.write(descriptor, data)
            except OSError as error:
                raise _failure(f'Schola could not write the event ({_errno_name(error)}). Nothing was written.', 'write_failed') from error
            if written != len(data):
                partial = written > 0
                raise _failure('The write stopped early. The log may end in a partial line. The next event starts on a new line.' if partial
                               else 'The write stopped early. Nothing was written.', 'write_failed', partial=partial, maybe=partial)
            try:
                os.fsync(descriptor)
            except OSError as error:
                raise _failure(f'Schola could not flush the event to disk ({_errno_name(error)}). The event may be in the log.'
                               ' A read does not prove it is on disk. Check the log before you retry.', 'fsync_failed', maybe=True) from error
            try:
                info = os.fstat(descriptor)
            except OSError:
                info = None  # the event is on disk; only the cache is lost, and the next call scans the file again
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)  # the first error is the one to report
            raise
        try:
            os.close(descriptor)
        except OSError as error:
            raise _failure(f'Schola could not close the event log ({_errno_name(error)}). The event may be in the log.'
                           ' A read does not prove it is on disk. Check the log before you retry.', 'fsync_failed', maybe=True) from error
        if state.missing or state.size == 0:
            # A new file, or one that an earlier try left empty: its entry in the folder must reach the disk too.
            self._flush([self.folder], 'the new event log', maybe=True)
        state.ids.add(event_id)
        if info is not None:
            self._state = _State(_signature(info), info.st_size, state.lines + 1, True, state.ids)
        return {'appended': True, 'eventId': event_id, 'line': state.lines + 1, 'bytes': len(line), 'newlineAdded': bool(newline)}

    # ------------------------------------------------------------------
    # Read, export, delete
    # ------------------------------------------------------------------

    def _validated(self):
        """The validated log as it is now, or None when there is no log. Checked once for each version of the file."""
        if not self._folder_exists():
            return None
        with self._locked(exclusive=False):
            info = self._stat_log()
            if info is None:
                return None
            if self._checked is not None and self._checked.signature == _signature(info):
                return self._checked
            self._checked = None
            found = self._read_log('log_corrupt')
            if found is None:
                return None
            data, read_info = found
            report = le.validate_log_text(data)
            events = []
            if 'reason' not in report:
                pieces = data.split(b'\n')
                events = [{'line': item['line'], 'event': json.loads(pieces[item['line'] - 1])} for item in report['lines'] if item['ok']]
            self._checked = _Checked(_signature(read_info), data, report, events)
            return self._checked

    def read(self, limit=None, offset=0):
        """The per-line report of the log. Reads only; repairs nothing.

        The report is `{ok, exists, lines, events, truncatedTail, total, offset, limit, more}`. `lines` is the validator
        report, with a reason and error pointers for each damaged line. `events` holds the valid events with their line
        numbers. `ok` and `truncatedTail` describe the whole log. `limit` and `offset` choose a window of `limit` lines
        after `offset` lines (all lines when `limit` is None). `total` is the number of lines and `more` says whether
        lines follow the window. A missing log reads as empty with `exists` false.
        """
        if (limit is not None and (type(limit) is not int or limit < 1)) or type(offset) is not int or offset < 0:
            raise EventLogError('Send a limit of 1 or more and an offset of 0 or more.', 'invalid_request', 400, field='limit')
        checked = self._validated()
        if checked is None:
            return {'ok': True, 'exists': False, 'lines': [], 'events': [], 'truncatedTail': False, 'total': 0, 'offset': offset, 'limit': limit, 'more': False}
        report = checked.report
        if 'reason' in report:
            # The validator refuses a log it cannot split into lines. The store only writes within the limits, so another program made this one.
            raise EventLogError('The event log is over its limits. Schola left it as it is.', 'log_corrupt')
        end = len(report['lines']) if limit is None else offset + limit
        lines = report['lines'][offset:end]
        events = [item for item in checked.events if offset < item['line'] <= end]
        return {'ok': report['ok'], 'exists': True, 'lines': copy.deepcopy(lines), 'events': copy.deepcopy(events),
                'truncatedTail': report['truncatedTail'], 'total': len(report['lines']), 'offset': offset, 'limit': limit,
                'more': end < len(report['lines'])}

    def export_checked(self):
        """The exact bytes of the log and what the validator found: `{data, ok, truncatedTail}`. Raises `log_missing` when there is no log."""
        checked = self._validated()
        if checked is None:
            raise EventLogError('There is no event log.', 'log_missing', 404)
        return {'data': checked.data, 'ok': checked.report['ok'], 'truncatedTail': checked.report.get('truncatedTail', False)}

    def export(self):
        """The exact bytes of the log, damage and all. Raises `log_missing` when there is no log."""
        return self.export_checked()['data']

    def _reflection_entries(self):
        """The reflection files and the leftover temporary files in the reflection folder, as (name, size, is_temp)."""
        self._safe(self.reflections)
        found = []
        try:
            with os.scandir(self.reflections) as entries:
                for entry in entries:
                    if _REFLECTION_FILE.fullmatch(entry.name):
                        found.append((entry.name, entry.stat(follow_symlinks=False).st_size, False))
                    elif entry.name.startswith('.touchline-') and entry.name.endswith('.tmp'):
                        found.append((entry.name, entry.stat(follow_symlinks=False).st_size, True))
        except FileNotFoundError:
            return []
        except OSError as error:
            raise EventLogError(f'The reflection folder cannot be read ({_errno_name(error)}).', 'reflection_unreadable') from error
        return found

    def delete(self, confirm=False):
        """Delete the log, every reflection, and leftover temporary files. Only `confirm=True` does it.

        Returns `{deleted: True, logRemoved, reflectionsRemoved}`, or raises. Nothing to delete is `log_missing`. The lock
        file stays. This is not a secure erase: the disk blocks are not overwritten.
        """
        if confirm is not True:
            raise EventLogError('Confirm before the event log and the reflections are deleted. This cannot be undone.', 'confirmation_required', 400, field='confirm')
        if not self._folder_exists():
            raise EventLogError('There is no event log.', 'log_missing', 404)
        with self._locked(exclusive=True):
            log_present = self._stat_log() is not None
            leftovers = self._reflection_entries()
            if not log_present and not leftovers:
                raise EventLogError('There is no event log.', 'log_missing', 404)
            self._state = None
            self._checked = None
            done = {'logRemoved': False, 'reflectionsRemoved': 0}
            if log_present:
                try:
                    os.unlink(self.path)
                except OSError as error:
                    raise EventLogError(f'Schola could not delete the event log ({_errno_name(error)}). The log is unchanged.', 'delete_failed', details=dict(done)) from error
                done['logRemoved'] = True
                self._flush_after_delete(self.folder, done)
            removed_files = 0
            for name, _, is_temp in leftovers:
                try:
                    os.unlink(self.reflections / name)
                except OSError as error:
                    raise EventLogError(f'Schola could not delete every reflection ({_errno_name(error)}). Some were removed.',
                                        'delete_failed', details=dict(done)) from error
                if not is_temp:
                    done['reflectionsRemoved'] += 1
                removed_files += 1
            if removed_files:
                self._flush_after_delete(self.reflections, done)
        return dict({'deleted': True}, **done)

    def _flush_after_delete(self, directory, done):
        try:
            _fsync_dir(directory)
        except OSError as error:
            raise EventLogError(f'Files were removed, but Schola could not flush the folder to disk ({_errno_name(error)}).',
                                'fsync_failed', details=dict(done, removed=True)) from error

    # ------------------------------------------------------------------
    # Reflections
    # ------------------------------------------------------------------

    def _make_reflection_folder(self):
        self._make_folder()
        self._safe(self.reflections)
        to_flush = []
        try:
            os.mkdir(self.reflections, 0o700)
            to_flush.append(self.folder)
        except FileExistsError:
            self._require_private(self.reflections)
            if self._is_empty(self.reflections):
                to_flush.extend([self.folder, self.reflections])
        except OSError as error:
            raise _failure(f'Schola could not create the reflection folder ({_errno_name(error)}). Nothing was saved.', 'write_failed') from error
        self._flush(to_flush, 'the reflection folder')

    def save_reflection(self, text):
        """Store one reflection in its own file. Returns `{reflectionId, charCount}`.

        The text is 1 to 10000 characters. It goes to a temporary file, is flushed, and is renamed into place, so a
        crash leaves no half-written reflection. The text is never part of an error message. The store holds at most
        1000 reflections and 16 MB of them (`reflections_full`). Leftover temporary files are removed first.
        """
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_REFLECTION_CHARS:
            raise EventLogError(f'A reflection is text of 1 to {MAX_REFLECTION_CHARS} characters. Nothing was saved.', 'invalid_reflection', 422, field='text')
        identifier = str(uuid.uuid4())
        record = {'version': REFLECTION_VERSION, 'reflectionId': identifier, 'charCount': len(text), 'text': text}
        try:
            data = (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        except UnicodeError:
            raise EventLogError('A reflection must be UTF-8 text. Nothing was saved.', 'invalid_reflection', 422, field='text') from None
        self._make_reflection_folder()
        with self._locked(exclusive=True):
            # Every save holds the lock, so a temporary file found here belongs to a save that crashed.
            entries = self._reflection_entries()
            for name, _, is_temp in entries:
                if is_temp:
                    try:
                        os.unlink(self.reflections / name)
                    except OSError as error:
                        raise _failure(f'Schola could not remove a leftover file ({_errno_name(error)}). Nothing was saved.', 'write_failed') from error
            kept = [size for _, size, is_temp in entries if not is_temp]
            if len(kept) + 1 > MAX_REFLECTIONS or sum(kept) + len(data) > MAX_REFLECTION_STORE_BYTES:
                raise EventLogError('The reflection store is full. Delete the log and reflections to start again. Nothing was saved.', 'reflections_full', 409)
            self._write_once(self.reflections / f'.touchline-{identifier}.tmp', self.reflections / f'{identifier}.json', data, identifier, len(text))
        return {'reflectionId': identifier, 'charCount': len(text)}

    def _write_once(self, temp, final, data, identifier, count):
        """Write a file with a temporary file, a flush, a rename, and a flush of its folder. A crash leaves the file whole or absent."""
        descriptor, renamed = None, False
        try:
            try:
                descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
                written = os.write(descriptor, data)
            except OSError as error:
                raise _failure(f'Schola could not write the reflection ({_errno_name(error)}). Nothing was saved.', 'write_failed') from error
            if written != len(data):
                raise _failure('The write stopped early. Nothing was saved.', 'write_failed')
            try:
                os.fsync(descriptor)
            except OSError as error:
                raise _failure(f'Schola could not flush the reflection to disk ({_errno_name(error)}). Nothing was saved.', 'fsync_failed') from error
            try:
                closing, descriptor = descriptor, None  # one close, whatever it reports
                os.close(closing)
                os.replace(temp, final)
            except OSError as error:
                raise _failure(f'Schola could not finish the reflection ({_errno_name(error)}). Nothing was saved.', 'write_failed') from error
            renamed = True
        finally:
            # Cleanup must not hide the error that is on its way out. A temporary file left behind holds a whole copy and no id anyone can fetch.
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            if not renamed:
                with contextlib.suppress(OSError):
                    os.unlink(temp)
        self._flush([final.parent], 'the reflection folder', maybe=True, reflectionId=identifier, charCount=count,
                    note=' The reflection was saved under the id in the details. Fetch it with that id before you rely on it.')

    def get_reflection(self, reflection_id):
        """The stored reflection `{reflectionId, charCount, text}`. An id that is not a lowercase UUID is refused before any file is touched."""
        if not isinstance(reflection_id, str) or _REFLECTION_ID.fullmatch(reflection_id) is None:
            raise EventLogError('A reflection id is a lowercase UUID.', 'invalid_reflection_id', 400, field='reflectionId')
        self._check_folders()
        path = self._safe(self.reflections / f'{reflection_id}.json')
        try:
            descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
        except (FileNotFoundError, NotADirectoryError):
            raise EventLogError('There is no reflection with this id.', 'reflection_not_found', 404) from None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise EventLogError('Data paths cannot be symbolic links.', 'unsafe_path') from error
            raise EventLogError(f'The reflection cannot be read ({_errno_name(error)}).', 'reflection_unreadable') from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise EventLogError('The saved reflection is damaged. Schola left it as it is.', 'reflection_corrupt')
            with os.fdopen(descriptor, 'rb') as handle:
                descriptor = None  # the file object owns it now
                data = handle.read(MAX_REFLECTION_BYTES + 1)
        except OSError as error:
            raise EventLogError(f'The reflection cannot be read ({_errno_name(error)}).', 'reflection_unreadable') from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        # Every message below leaves out the stored text on purpose.
        damaged = EventLogError('The saved reflection is damaged. Schola left it as it is.', 'reflection_corrupt')
        if len(data) > MAX_REFLECTION_BYTES:
            raise damaged
        try:
            record = json.loads(data.decode('utf-8'))
        except (ValueError, RecursionError):
            raise damaged from None
        if (not isinstance(record, dict) or set(record) != _REFLECTION_FIELDS or record['version'] != REFLECTION_VERSION
                or type(record['version']) is not int or record['reflectionId'] != reflection_id or type(record['charCount']) is not int
                or not isinstance(record['text'], str) or not 1 <= len(record['text']) <= MAX_REFLECTION_CHARS
                or record['charCount'] != len(record['text'])):
            raise damaged
        return {'reflectionId': reflection_id, 'charCount': record['charCount'], 'text': record['text']}
