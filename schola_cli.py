"""The Schola command line tool.

Schola ships as a desktop app and as this tool. Both use the same engine. This file is a thin layer over it.
It adds no engine behavior. It calls the lesson store, the check runner, the check consent code, and the event log.

    schola hub                                     start the hub
    schola modules                                 list the modules
    schola module show MODULE_ID                   show one module
    schola check show MODULE_ID CHECK_ID           show what a check would run
    schola check run MODULE_ID CHECK_ID            run a check in a workspace
    schola events list | export | delete           read, copy, or delete the event log
    schola version

The words of the commands are in COMMAND_TABLE, so the owner can rename them in one place.
`main` is the entry point that the tests call. Only `cli_entry` stops the program.

Text from modules, checks, events, and files is untrusted. `safe_text` is the one helper that makes it safe to print.
Read docs/cli.md for the exit codes, the limits, and what this tool does not do.

Only the Python standard library and the engine modules of this repository are used.
"""
import argparse
import dataclasses
import hmac
import json
import os
from pathlib import Path
import re
import sys
import types
import unicodedata
import webbrowser

import check_consent
import check_routes
import check_runner
from event_log import EventLog
from lesson_store import LessonStore
from local_store import StoreError

ROOT = Path(__file__).resolve().parent
CLI_VERSION = '0.1.0'

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3

DATA_DIR_VARIABLE = 'SCHOLA_DATA_DIR'
DEFAULT_PORT = 8765
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 5000
MAX_OFFSET = 999_999_999
# The longest line of ordinary text that the tool prints. Longer text is cut, and the cut says how much is missing.
MAX_LINE_CHARS = 200
# A line of a script is wrapped at this width. Nothing is cut.
SCRIPT_WIDTH = 100
# The most script rows that the consent question shows. A longer script cannot be agreed to by typing.
PROMPT_ROWS = 60
CONSENT_PREFIX_CHARS = 8
PROMPT_READ_CHARS = 200
EXPORT_LINE_CHARS = 20000
MAX_EVIDENCE_ROWS = 40

# (key, the words that call it, its help line). The words are the only place the command names are written.
COMMAND_TABLE = (
    ('hub', ('hub',), 'Start the hub on this computer and print its address.'),
    ('modules', ('modules',), 'List the modules you imported.'),
    ('module-show', ('module', 'show'), 'Show one module: its tasks and its checks.'),
    ('check-show', ('check', 'show'), 'Show what a check would run. This runs nothing.'),
    ('check-run', ('check', 'run'), 'Run a check in a workspace and print the result.'),
    ('events-list', ('events', 'list'), 'List the events in your log.'),
    ('events-export', ('events', 'export'), 'Copy your event log as JSON Lines.'),
    ('events-delete', ('events', 'delete'), 'Delete your event log and your reflections for good.'),
    ('version', ('version',), 'Print the version of this tool.'),
)
GROUP_HELP = (
    ('module', 'Read a module.'),
    ('check', 'Show or run a check.'),
    ('events', 'Read, copy, or delete your event log.'),
)

# The data fields that `events list` shows for each event type.
KEY_FIELDS = (
    ('attempt_submitted', ('attemptId', 'artifactBytes')),
    ('check_result', ('checkId', 'status')),
    ('hint_requested', ('level', 'origin')),
    ('agent_offer', ('offeredLevel', 'outcome')),
    ('reflection_saved', ('charCount',)),
    ('task_opened', ()),
    ('task_finished', ('result',)),
    ('module_completed', ('checksPassed', 'tasksTotal')),
    ('mode_changed', ('from', 'to')),
    ('review_result', ('skillId', 'outcome')),
)

NOT_TEXT = '<not text>'
_REASON = re.compile(r'[a-z_]{1,40}', re.ASCII)
# Characters that are not control characters but still hide text or break a line.
_HIDDEN_CODES = frozenset({0x034f, 0x115f, 0x1160, 0x3164, 0xffa0})
_HIDDEN_CATEGORIES = frozenset({'Cc', 'Cf', 'Cs', 'Co', 'Zl', 'Zp'})


# ---------------------------------------------------------------------------
# Making untrusted text safe to print
# ---------------------------------------------------------------------------

def _is_hidden(char):
    code = ord(char)
    if code < 0x80:
        return code < 0x20 or code == 0x7f
    if code in _HIDDEN_CODES or 0x180b <= code <= 0x180f or 0xfe00 <= code <= 0xfe0f or 0xe0100 <= code <= 0xe01ef:
        return True
    return unicodedata.category(char) in _HIDDEN_CATEGORIES


def _show_char(char):
    """The character, or `<U+XXXX>` when a terminal could act on it or when it hides text."""
    return '<U+%04X>' % ord(char) if _is_hidden(char) else char


def safe_text(value, limit=MAX_LINE_CHARS):
    """One line of text that is safe to print on a terminal. Time grows in step with the text.

    Control characters (including ESC, CSI, OSC, CR, NUL, and the eight-bit controls), bidirectional controls,
    zero-width characters, tag characters, private use characters, and lone surrogates show as `<U+XXXX>`.
    The result is cut after `limit` characters, and the cut says how many characters are missing.
    A value that is not exactly a `str` gives `<not text>`.
    """
    if type(value) is not str:
        return NOT_TEXT
    pieces, size = [], 0
    for index, char in enumerate(value):
        piece = _show_char(char)
        if size + len(piece) > limit:
            pieces.append(f'...[{len(value) - index} more characters]')
            break
        pieces.append(piece)
        size += len(piece)
    return ''.join(pieces)


def _script_blocks(source, width):
    """For each line of a script, (the bytes it takes including its newline, its display rows)."""
    lines = source.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    blocks = []
    for number, line in enumerate(lines, 1):
        rows, chunk, size = [], [], 0
        for char in line:
            piece = _show_char(char)
            if chunk and size + len(piece) > width:
                rows.append(''.join(chunk))
                chunk, size = [], 0
            chunk.append(piece)
            size += len(piece)
        rows.append(''.join(chunk))
        shown = [(f'{number:>4} | ' if index == 0 else '     | ') + row for index, row in enumerate(rows)]
        blocks.append((len(line.encode('utf-8', errors='surrogatepass')) + 1, shown))
    return blocks


def script_rows(source, width=SCRIPT_WIDTH):
    """Every line of a script, numbered and marked, with hidden characters shown as `<U+XXXX>`. A long line wraps. Nothing is cut."""
    return [row for _size, rows in _script_blocks(source, width) for row in rows]


def _prompt_view(source):
    """(rows, hidden byte count) of the part of a script that fits the consent question."""
    shown, taken = [], 0
    for size, rows in _script_blocks(source, SCRIPT_WIDTH):
        if len(shown) + len(rows) > PROMPT_ROWS:
            return shown, len(source.encode('utf-8', errors='surrogatepass')) - taken
        shown.extend(rows)
        taken += size
    return shown, 0


def show_value(value):
    """A short, safe text for a small value from a check result or an event."""
    kind = type(value)
    if kind is str:
        return safe_text(value)
    if value is None:
        return 'none'
    if kind is bool:
        return 'true' if value else 'false'
    if kind is int:
        return str(value) if -(2 ** 53) <= value <= 2 ** 53 else '<big number>'
    if kind is float:
        return repr(value) if value == value and value not in (float('inf'), float('-inf')) else '<bad number>'
    if kind in (list, tuple):
        return ', '.join(show_value(item) for item in value[:20])
    return '<object>'


def to_json(value):
    """One JSON document. ASCII only (the encoder also escapes DEL), so a terminal cannot act on it. Keys are sorted, so the shape is stable."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)


def count_of(number, word):
    return f'{number} {word}' + ('' if number == 1 else 's')


# ---------------------------------------------------------------------------
# Context, errors, and output
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Streams:
    stdin: object
    stdout: object
    stderr: object


@dataclasses.dataclass(frozen=True)
class Context:
    streams: Streams
    data_dir: Path
    home: Path
    as_json: bool


class CliError(Exception):
    """A problem to report: a stable code, a fixed sentence, and an exit code."""

    def __init__(self, code, message, exit_code=EXIT_FAILED):
        super().__init__(code)
        self.code, self.message, self.exit_code = code, message, exit_code


class _Stop(Exception):
    """The parser wants the program to end with this status. `main` turns it into a return value."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def say(stream, text=''):
    stream.write(text + '\n')


def report(stream, failure):
    say(stream, f'schola: {safe_text(failure.message, 400)} (code: {safe_text(failure.code, 60)})')


def convert(error):
    """The CliError that stands for any exception. The text is a fixed sentence. A refusal (exit 3) is raised as a CliError by the command itself."""
    if isinstance(error, CliError):
        return error
    if isinstance(error, StoreError):
        payload = error.payload()
        code = payload.get('code') if type(payload.get('code')) is str else 'failed'
        message = payload.get('error') if type(payload.get('error')) is str else 'The operation failed.'
        return CliError(code, message, EXIT_FAILED)
    if isinstance(error, OSError):
        return CliError('storage_unavailable', 'Schola cannot read or write the data folder.')
    return CliError('internal_error', 'Schola hit an unexpected problem and stopped. Check your files before you try again.')


def is_terminal(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def resolve_data_dir(flag, environ):
    """The data folder: the flag, else the environment variable, else the `data` folder of the repository."""
    if flag:
        return Path(flag)
    value = environ.get(DATA_DIR_VARIABLE)
    if type(value) is str and value:
        return Path(value)
    return ROOT / 'data'


def resolve_home(environ):
    value = environ.get('HOME')
    if type(value) is str and os.path.isabs(value):
        return Path(value)
    return Path.home()


# ---------------------------------------------------------------------------
# Finding things
# ---------------------------------------------------------------------------

def resolve_version(store, module_id, version):
    """The version asked for, else the pinned one, else the newest stored one."""
    if version is not None:
        return version
    pinned = store.pins()['pins'].get(module_id)
    if pinned:
        return pinned
    stored = store.versions(module_id)
    return stored[-1] if stored else None


def load_module(ctx, module_id, version):
    store = LessonStore(ctx.data_dir)
    version = resolve_version(store, module_id, version)
    return version, store.get_module(module_id, version), store


def load_check(ctx, args):
    store = LessonStore(ctx.data_dir)
    version = resolve_version(store, args.module_id, args.version)
    check = check_routes._find_check(store, args.module_id, version, args.check_id)
    return version, check


def passed_workspace(ctx, path):
    """The real path of a workspace the hub would accept. Anything else is refused. A relative path starts at the current folder."""
    if type(path) is str and path and not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    try:
        return check_routes.validate_workspace(path, ctx.data_dir, ctx.home)
    except check_routes.CheckError as error:
        raise CliError('invalid_workspace', str(error), EXIT_REFUSED) from None


def dict_of(value):
    return value if type(value) is dict else {}


def list_of(value):
    return value if type(value) is list else []


def needs_consent(check):
    return check_consent.needs_consent(check)


def runner_type(check):
    value = dict_of(check.get('runner')).get('type') if type(check) is dict else None
    return value


# ---------------------------------------------------------------------------
# hub
# ---------------------------------------------------------------------------

def cmd_hub(args, ctx):
    import server  # imported here, so the other commands do not load the web handler
    out = ctx.streams.stdout

    def announce(address):
        say(out, f'Schola is ready at {address}')
        say(out, 'Press Control-C to stop.')
        out.flush()
        if args.open:
            try:
                opened = webbrowser.open(address)
            except Exception:  # noqa: BLE001 - a browser that fails to open must not stop the hub
                opened = False
            if not opened:
                say(out, 'Schola could not open a browser. Open the address yourself.')
                out.flush()

    try:
        server.run_server(args.port, ctx.data_dir, announce)
    except BrokenPipeError:
        raise  # the output closed; this is not a port problem
    except OSError:
        raise CliError('hub_unavailable', 'Schola cannot start the hub on that port. Use --port to choose another.') from None
    return EXIT_OK


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------

def cmd_modules(args, ctx):
    store = LessonStore(ctx.data_dir)
    rows = []
    for entry in store.list_modules()['modules']:
        version = entry['pinnedVersion'] or entry['versions'][-1]
        document = store.get_module(entry['id'], version)
        rows.append({'id': entry['id'], 'version': version, 'title': entry['title'], 'tasks': len(list_of(document.get('tasks'))),
                     'checks': len(list_of(document.get('checks'))), 'versions': entry['versions'],
                     'newerVersionAvailable': entry['newerVersionAvailable']})
    out = ctx.streams.stdout
    if ctx.as_json:
        say(out, to_json({'modules': rows}))
        return EXIT_OK
    if not rows:
        say(out, 'No modules are imported yet. Import a module in the Schola hub.')
        return EXIT_OK
    say(out, f'Modules: {len(rows)}')
    for row in rows:
        say(out, f"{safe_text(row['id'])} {safe_text(row['version'])}  {safe_text(row['title'])}  "
                 f"({count_of(row['tasks'], 'task')}, {count_of(row['checks'], 'check')})")
        if row['newerVersionAvailable']:
            say(out, f"  {safe_text(row['newerVersionAvailable'])} is newer and stored. The pin stays on {safe_text(row['version'])}.")
    return EXIT_OK


def consent_label(check):
    return 'needs consent' if needs_consent(check) else 'no consent needed'


def cmd_module_show(args, ctx):
    version, document, _store = load_module(ctx, args.module_id, args.version)
    tasks = [{'id': dict_of(task).get('id'), 'prompt': dict_of(task).get('prompt')} for task in list_of(document.get('tasks'))]
    checks = [{'id': dict_of(check).get('id'), 'kind': dict_of(check).get('kind'), 'runnerType': runner_type(check),
               'needsConsent': needs_consent(check), 'description': dict_of(check).get('description')} for check in list_of(document.get('checks'))]
    out = ctx.streams.stdout
    if ctx.as_json:
        say(out, to_json({'id': args.module_id, 'version': version, 'title': document.get('title'), 'outcome': document.get('outcome'),
                          'tasks': tasks, 'checks': checks}))
        return EXIT_OK
    say(out, f'Module: {safe_text(args.module_id)}')
    say(out, f'Version: {safe_text(version)}')
    say(out, f"Title: {safe_text(document.get('title'))}")
    say(out, f"Outcome: {safe_text(document.get('outcome'))}")
    say(out, f'Tasks ({len(tasks)}):')
    for task in tasks:
        say(out, f"  {safe_text(task['id'])}: {safe_text(task['prompt'])}")
    say(out, f'Checks ({len(checks)}):')
    for check in checks:
        runner = check['runnerType']
        say(out, f"  {safe_text(check['id'])}: {safe_text(check['kind'])}, {safe_text(runner) if runner is not None else 'no runner'}, "
                 f"{'needs consent' if check['needsConsent'] else 'no consent needed'}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# check show
# ---------------------------------------------------------------------------

def shown_check(ctx, args, version, check, workspace):
    """The facts about a check. The engine's own function builds them, so the hub and this tool agree."""
    digest = check_consent.check_hash(check)
    facts = check_routes._shown(check, None, digest)
    runner = dict_of(check.get('runner'))
    return {
        'moduleId': args.module_id, 'version': version, 'checkId': check.get('id'), 'kind': check.get('kind'),
        'runnerType': facts['runnerType'], 'description': facts['description'], 'needsConsent': facts['needsConsent'],
        'checkHash': digest, 'runtime': facts['runtime'], 'source': facts['source'], 'args': facts['args'],
        'timeoutSeconds': facts['timeoutSeconds'], 'workspace': workspace, 'limits': facts['limits'],
        'limitsNotApplied': facts['limitsNotApplied'], 'runner': runner if runner else None,
    }


def print_script(out, facts, rows):
    source = facts['source']
    say(out, f"Script ({len(source.encode('utf-8', errors='surrogatepass'))} bytes):")
    for row in rows:
        say(out, row)


def print_what_runs(out, facts):
    """The lines that say what a script check runs. `check show` and the consent question both use them."""
    say(out, f"Runtime: {safe_text(facts['runtime'])}")
    arguments = list_of(facts['args'])
    if not arguments:
        say(out, 'Arguments: none')
    else:
        say(out, 'Arguments:')
        for number, value in enumerate(arguments, 1):
            say(out, f'  {number}: {safe_text(value)}')
    say(out, f"Time limit: {show_value(facts['timeoutSeconds'])} seconds")


def cmd_check_show(args, ctx):
    version, check = load_check(ctx, args)
    passed = args.workspace
    if passed is not None:
        passed_workspace(ctx, passed)  # the same rules as the hub; the path as passed is what is shown
    facts = shown_check(ctx, args, version, check, passed)
    out = ctx.streams.stdout
    if ctx.as_json:
        say(out, to_json(facts))
        return EXIT_OK
    say(out, f"Module: {safe_text(args.module_id)} {safe_text(version)}")
    say(out, f"Check: {safe_text(facts['checkId'])}")
    say(out, f"Kind: {safe_text(facts['kind'])}")
    say(out, f"Runner: {safe_text(facts['runnerType']) if facts['runnerType'] is not None else 'none'}")
    if facts['description'] is not None:
        say(out, f"Description: {safe_text(facts['description'])}")
    say(out, f"Needs consent: {'yes' if facts['needsConsent'] else 'no'}")
    say(out, f"Check hash: {facts['checkHash']}")
    if passed is not None:
        say(out, f'Workspace: {safe_text(passed)}')
    fields = {key: value for key, value in dict_of(facts['runner']).items() if key != 'type'}
    if fields:
        say(out, 'Runner fields:')
        for key in sorted(fields, key=str):
            say(out, f'  {safe_text(key)}: {show_value(fields[key])}')
    if facts['needsConsent']:
        print_what_runs(out, facts)
        say(out, 'Limits Schola sets:')
        for item in facts['limits']:
            say(out, f'  {safe_text(item)}')
        say(out, 'Limits Schola cannot set:')
        for item in facts['limitsNotApplied']:
            say(out, f'  {safe_text(item)}')
    if type(facts['source']) is str:
        print_script(out, facts, script_rows(facts['source']))
    say(out, 'Nothing was run.')
    return EXIT_OK


# ---------------------------------------------------------------------------
# check run
# ---------------------------------------------------------------------------

def ask_for_consent(args, ctx, version, check, digest, shown_path):
    """Return when the student agreed to this exact script. Raise a refusal otherwise. Consent lasts for this command only."""
    streams, out = ctx.streams, ctx.streams.stdout
    if args.consent is not None:
        # An exact match of the full hash. A wrong value is refused, and it never falls back to the question.
        if not hmac.compare_digest(args.consent.encode('utf-8', errors='replace'), digest.encode('ascii')):
            raise CliError('hash_mismatch', 'That hash does not match this check. Run "schola check show" and read the script again.', EXIT_REFUSED)
        return
    if not is_terminal(streams.stdin):
        raise CliError('consent_required', 'This check runs a script. Read it with "schola check show", '
                                           'then run this command again with --consent and the check hash.', EXIT_REFUSED)
    facts = check_routes._shown(check, None, digest)
    rows, hidden = _prompt_view(facts['source'] if type(facts['source']) is str else '')
    say(out, 'This check runs a script on your computer.')
    say(out, 'Read the script before you agree.')
    say(out, f'Module: {safe_text(args.module_id)} {safe_text(version)}')
    say(out, f'Check: {safe_text(check.get("id"))}')
    say(out, f'Workspace: {safe_text(shown_path)}')
    print_what_runs(out, facts)
    print_script(out, facts, rows)
    if hidden:
        say(out, f'{hidden} bytes of the script are not shown.')
    say(out, f'Check hash: {digest}')
    say(out, 'This is consent, not a sandbox. The script can read your files and use the network.')
    if hidden:
        out.flush()
        raise CliError('consent_required', 'The script is too long to agree to by typing. Read it with "schola check show", '
                                           'then run this command again with --consent and the check hash.', EXIT_REFUSED)
    out.write(f'To agree, type the first {CONSENT_PREFIX_CHARS} characters of the check hash and press Enter: ')
    out.flush()
    try:
        answer = streams.stdin.readline(PROMPT_READ_CHARS)
    except (OSError, ValueError):
        answer = ''
    if type(answer) is not str or not hmac.compare_digest(answer.strip().encode('utf-8', errors='replace'), digest[:CONSENT_PREFIX_CHARS].encode('ascii')):
        raise CliError('consent_declined', 'Consent was not given. Nothing was run.', EXIT_REFUSED)


def print_result(out, args, version, outcome):
    say(out, f'Check: {safe_text(args.check_id)}')
    say(out, f'Module: {safe_text(args.module_id)} {safe_text(version)}')
    say(out, f"Status: {safe_text(outcome.get('status'))}")
    evidence = dict_of(outcome.get('evidence'))
    say(out, 'Evidence:' if evidence else 'Evidence: none')
    for key in list(evidence)[:MAX_EVIDENCE_ROWS]:
        say(out, f'  {safe_text(key)}: {show_value(evidence[key])}')
    log = list_of(outcome.get('log'))
    say(out, 'Log:' if log else 'Log: none')
    for line in log[:MAX_EVIDENCE_ROWS]:
        say(out, f'  {safe_text(line)}')


def cmd_check_run(args, ctx):
    version, check = load_check(ctx, args)
    workspace = passed_workspace(ctx, args.workspace)
    digest = check_consent.check_hash(check)
    consent, tier = None, 1
    if needs_consent(check):
        tier = 2
        ask_for_consent(args, ctx, version, check, digest, args.workspace)
        registry = check_consent.ConsentRegistry()  # in memory, for this command only
        granted = registry.grant(args.module_id, version, args.check_id, digest, workspace)
        consent = registry.find(args.module_id, version, args.check_id, digest, workspace, granted['consentId'])
    elif args.consent is not None:
        say(ctx.streams.stderr, 'schola: This check needs no consent. The --consent value was ignored.')
    outcome = check_runner.run(check, workspace, None, None, consent=consent)
    out = ctx.streams.stdout
    if ctx.as_json:
        say(out, to_json({'moduleId': args.module_id, 'version': version, 'checkId': args.check_id, 'checkHash': digest, 'tier': tier, 'result': outcome}))
    else:
        print_result(out, args, version, outcome)
    # A check that ran and failed is a result, not a fault. Only a check that could not run is exit 1.
    return EXIT_FAILED if dict_of(outcome).get('status') not in ('passed', 'failed', 'skipped') else EXIT_OK


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

def key_fields(event_type, data):
    data = dict_of(data)
    names = dict(KEY_FIELDS).get(event_type) if type(event_type) is str else None
    if names is None:
        scalar = (str, int, float, bool)
        names = sorted(key for key, value in data.items() if type(key) is str and (value is None or type(value) in scalar))[:3]
    return ' '.join(f'{name}={show_value(data[name])}' for name in names if name in data)


def event_row(item):
    event = item.get('event') if type(item) is dict else None
    number = item.get('line') if type(item) is dict else None
    if type(event) is not dict:
        return f'{show_value(number)}: <unreadable event>'
    head = (f"{show_value(number)}: {show_value(event.get('eventId'))} {show_value(event.get('time'))} {show_value(event.get('type'))} "
            f"module={show_value(event.get('moduleId'))} task={show_value(event.get('taskId'))} source={show_value(event.get('source'))}")
    fields = key_fields(event.get('type'), event.get('data'))
    return head + (' ' + fields if fields else '')


def damaged_lines(report):
    found = []
    for line in list_of(report.get('lines')):
        if type(line) is dict and line.get('ok') is False:
            reason = line.get('reason')
            found.append({'line': line.get('line'), 'reason': reason if type(reason) is str and _REASON.fullmatch(reason) else 'unknown'})
    return found


def cmd_events_list(args, ctx):
    report = EventLog(ctx.data_dir).read(limit=args.limit, offset=args.offset)
    damaged = damaged_lines(report)
    out = ctx.streams.stdout
    if ctx.as_json:
        say(out, to_json({'ok': report['ok'], 'exists': report['exists'], 'total': report['total'], 'offset': report['offset'], 'limit': report['limit'],
                          'more': report['more'], 'truncatedTail': report['truncatedTail'], 'events': report['events'], 'damaged': damaged}))
    elif not report['exists']:
        say(out, 'There is no event log yet.')
    else:
        total = report['total']
        say(out, f'Event log: {count_of(total, "line")} in total.')
        first, last = args.offset + 1, args.offset + len(list_of(report.get('lines')))
        say(out, f'Showing lines {first} to {last}.' if last >= first else 'No lines in this window.')
        rows = {item['line']: event_row(item) for item in list_of(report.get('events')) if type(item) is dict and type(item.get('line')) is int}
        bad = {item['line']: item['reason'] for item in damaged}
        for line in sorted(set(rows) | set(bad), key=lambda number: (type(number) is not int, number)):
            if line in rows:
                say(out, rows[line])
            else:
                say(out, f'line {show_value(line)} is damaged ({safe_text(bad[line], 40)}).')
        if report['more']:
            say(out, f'More lines follow. Use --offset {last} to read them.')
    if not report['ok']:
        raise CliError('log_damaged', 'The event log has damaged lines. Schola left the file as it is.')
    return EXIT_OK


def write_new_file(path, data):
    """Create a new private file with these bytes. An existing file or link is never touched."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise CliError('output_exists', 'That file already exists. Schola does not overwrite files. Choose a new name.', EXIT_REFUSED) from None
    except (OSError, ValueError):
        raise CliError('output_unwritable', 'Schola cannot create that file. Check that its folder exists and that you can write there.') from None
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        try:
            os.unlink(path)  # only the file this command just created
        except OSError:
            pass
        raise CliError('output_unwritable', 'Schola cannot write that file. The file was removed.') from None


def write_export(streams, data):
    out = streams.stdout
    if is_terminal(out):
        say(streams.stderr, 'schola: The output is a terminal, so control characters are escaped. Use --output FILE for the exact bytes.')
        for line in data.decode('utf-8', errors='replace').split('\n'):
            say(out, safe_text(line, EXPORT_LINE_CHARS))
        return
    buffer = getattr(out, 'buffer', None)
    if buffer is not None:
        out.flush()
        buffer.write(data)
        buffer.flush()
    else:
        out.write(data.decode('utf-8', errors='replace'))


def cmd_events_export(args, ctx):
    exported = EventLog(ctx.data_dir).export_checked()
    data = exported['data']
    if args.output is not None:
        write_new_file(args.output, data)
        say(ctx.streams.stdout, f'Wrote {count_of(len(data), "byte")} to {safe_text(args.output)}.')
    else:
        write_export(ctx.streams, data)
    if not exported['ok']:
        raise CliError('log_damaged', 'The event log has damaged lines. The export holds the log exactly as it is.')
    return EXIT_OK


def cmd_events_delete(args, ctx):
    if not args.confirm:
        raise CliError('confirmation_required', 'This deletes your event log and your reflections for good. '
                                                'Run the command again with --confirm to do it.', EXIT_REFUSED)
    out = ctx.streams.stdout
    try:
        done = EventLog(ctx.data_dir).delete(True)
    except StoreError as error:
        if error.payload().get('code') != 'log_missing':
            raise
        say(out, 'There is no event log. Nothing was removed.')
        return EXIT_OK
    say(out, 'Deleted the event log.' if done.get('logRemoved') else 'There was no event log file.')
    say(out, f'Deleted {count_of(done.get("reflectionsRemoved", 0), "reflection")}.')
    say(out, 'This cannot be undone.')
    return EXIT_OK


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

def cmd_version(args, ctx):
    if ctx.as_json:
        say(ctx.streams.stdout, to_json({'name': 'schola', 'version': CLI_VERSION}))
    else:
        say(ctx.streams.stdout, f'schola {CLI_VERSION}')
    return EXIT_OK


HANDLERS = types.MappingProxyType({
    'hub': cmd_hub, 'modules': cmd_modules, 'module-show': cmd_module_show, 'check-show': cmd_check_show, 'check-run': cmd_check_run,
    'events-list': cmd_events_list, 'events-export': cmd_events_export, 'events-delete': cmd_events_delete, 'version': cmd_version,
})


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

def _number(low, high, name):
    def convert_number(text):
        if not re.fullmatch(r'[0-9]{1,10}', text, re.ASCII) or not low <= int(text) <= high:
            raise argparse.ArgumentTypeError(f'Use a whole number from {low} to {high}.')
        return int(text)
    convert_number.__name__ = name
    return convert_number


def _folder_name(text):
    if not text:
        raise argparse.ArgumentTypeError('Give a folder name.')
    return text


def _file_name(text):
    if not text:
        raise argparse.ArgumentTypeError('Give a file name.')
    return text


def _add_arguments(key, parser):
    if key == 'hub':
        parser.add_argument('--port', type=_number(0, 65535, 'port'), default=DEFAULT_PORT, help=f'The port to listen on. The default is {DEFAULT_PORT}.')
        parser.add_argument('--open', action='store_true', help='Open the hub in your browser.')
    if key in ('module-show', 'check-show', 'check-run'):
        parser.add_argument('module_id', metavar='MODULE_ID', help='The id of the module.')
    if key in ('check-show', 'check-run'):
        parser.add_argument('check_id', metavar='CHECK_ID', help='The id of the check.')
    if key in ('module-show', 'check-show', 'check-run'):
        parser.add_argument('--version', help='The module version. The default is the pinned version.')
    if key == 'check-show':
        parser.add_argument('--workspace', metavar='PATH', help='Your work folder. Schola checks that the hub would accept it.')
    if key == 'check-run':
        parser.add_argument('--workspace', metavar='PATH', required=True, help='Your work folder. The check reads it.')
        parser.add_argument('--consent', metavar='HASH', help='For a script check without a terminal: the full check hash from "check show".')
    if key == 'events-list':
        parser.add_argument('--limit', type=_number(1, MAX_LIST_LIMIT, 'limit'), default=DEFAULT_LIST_LIMIT,
                            help=f'How many lines to read. The most is {MAX_LIST_LIMIT}. The default is {DEFAULT_LIST_LIMIT}.')
        parser.add_argument('--offset', type=_number(0, MAX_OFFSET, 'offset'), default=0, help='How many lines to skip. The default is 0.')
    if key == 'events-export':
        parser.add_argument('--output', metavar='FILE', type=_file_name, help='Write to a new file. Schola does not overwrite a file.')
    if key == 'events-delete':
        parser.add_argument('--confirm', action='store_true', help='Say that you want to delete the log and the reflections for good.')
    if key in ('modules', 'module-show', 'check-show', 'check-run', 'events-list', 'version'):
        parser.add_argument('--json', action='store_true', help='Print one JSON document.')
    parser.add_argument('--data-dir', metavar='DIR', type=_folder_name, default=argparse.SUPPRESS,
                        help=f'The data folder. The default is the data folder of the repository, or ${DATA_DIR_VARIABLE}.')


def build_parser(streams):
    class Parser(argparse.ArgumentParser):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('allow_abbrev', False)
            try:
                super().__init__(*args, color=False, **kwargs)
            except TypeError:  # Python before 3.14 has no color option
                super().__init__(*args, **kwargs)

        def print_help(self, file=None):
            super().print_help(streams.stdout if file is None else file)

        def print_usage(self, file=None):
            super().print_usage(streams.stderr if file is None else file)

        def error(self, message):
            self.print_usage(streams.stderr)
            say(streams.stderr, f'{self.prog}: error: {safe_text(message, 400)}')
            raise _Stop(EXIT_USAGE)

        def exit(self, status=0, message=None):
            if message:
                streams.stderr.write(safe_text(message, 400) + '\n')
            raise _Stop(status)

    parser = Parser(prog='schola', description='Open the Schola hub, read modules, run checks, and read your event log.')
    parser.add_argument('--data-dir', metavar='DIR', type=_folder_name, default=None,
                        help=f'The data folder. The default is the data folder of the repository, or ${DATA_DIR_VARIABLE}.')
    top = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)
    groups = {}
    for key, words, help_line in COMMAND_TABLE:
        if len(words) == 1:
            leaf = top.add_parser(words[0], help=help_line, description=help_line)
        else:
            if words[0] not in groups:
                group_help = dict(GROUP_HELP).get(words[0], '')
                group = top.add_parser(words[0], help=group_help, description=group_help)
                groups[words[0]] = group.add_subparsers(dest=words[0] + '_command', metavar='COMMAND', required=True)
            leaf = groups[words[0]].add_parser(words[1], help=help_line, description=help_line)
        _add_arguments(key, leaf)
        leaf.set_defaults(key=key)
    return parser


def _all_help(parser):
    yield parser.format_help()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                yield from _all_help(child)


def help_text():
    """All the help text of the tool, for the checks on its wording."""
    import io
    empty = Streams(io.StringIO(), io.StringIO(), io.StringIO())
    return '\n'.join(_all_help(build_parser(empty)))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def main(argv=None, *, stdin=None, stdout=None, stderr=None, environ=None):
    """Run one command and return its exit code. Nothing here stops the program or keeps state between calls."""
    streams = Streams(sys.stdin if stdin is None else stdin, sys.stdout if stdout is None else stdout, sys.stderr if stderr is None else stderr)
    environ = os.environ if environ is None else environ
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser(streams).parse_args(arguments)
    except _Stop as stop:
        return stop.status
    context = Context(streams, resolve_data_dir(args.data_dir, environ), resolve_home(environ), bool(getattr(args, 'json', False)))
    try:
        return HANDLERS[args.key](args, context)
    except BrokenPipeError:
        return EXIT_FAILED
    except KeyboardInterrupt:
        say(streams.stderr, 'schola: Stopped.')
        return EXIT_FAILED
    except Exception as error:  # noqa: BLE001 - every fault becomes one fixed sentence and a code
        failure = convert(error)
        report(streams.stderr, failure)
        return failure.exit_code


def cli_entry():
    """What the `schola` launcher runs. This is the only place that stops the program."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='backslashreplace')  # a terminal that cannot show a character gets an escape, not a crash
        except (AttributeError, ValueError):
            pass
    code = main()
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = EXIT_FAILED
    sys.exit(code)


if __name__ == '__main__':
    cli_entry()
