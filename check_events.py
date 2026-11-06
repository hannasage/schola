"""Check events: record a finished check run in the student's event log.

When a check runs, the app writes two learning events, one after the other:

1. `attempt_submitted`: the student's work was checked. It holds a new attempt id, a digest of the work,
   and the size of the work in bytes.
2. `check_result`: what the check found. It holds the same attempt id, the check id, the status, how
   long the run took, the runner type, and a short code in `evidence`.

The hub (`check_routes.py`) and the command line tool (`schola_cli.py`) both call `run_and_record`.
This module is the one place that holds the rules. It uses only the Python standard library.

What an event may hold
----------------------
Both events are checked by `learning_event.validate_event` before anything is written, and the log
checks them again. The source is always `app`, because the contract lets only the app write these two types.
The events never hold a path, a file name, file content, script output, or the text of a runner's evidence.

* The digest. It is the SHA-256 of a manifest of the files the check reads. The manifest has one line for each
  file, sorted by the UTF-8 bytes of the path: the relative path, a NUL byte, the SHA-256 of the file in
  lowercase hex, a NUL byte, the size in bytes in decimal, and a newline. The size of the work is the sum of the
  file sizes. A check that reads nothing, or whose file is missing, has the empty manifest: the SHA-256 of no
  bytes, and 0 bytes. The digest is computed after the run, from the folder as it stands then.
  - `file-exists`, `text-match`, and `json-valid` read the file at the runner path. A folder holds no file.
  - A script check reads the workspace copy the script runner makes. This module makes the same copy with the
    same function (`check_script.copy_workspace`) under the same limits, hashes it, and deletes it. The script
    file and anything the script writes are not part of the work.
  - Every other runner type, such as `git-commit-exists`, reads no work file, so it has the empty manifest.
* `evidence`. A runner's evidence is untrusted and holds more than the contract allows. The event keeps one
  short code only: the `reason` of the result, and only when it is on the fixed list `REASON_CODES`.
  Anything else becomes the empty string.

When no events are written
--------------------------
A run that never started writes nothing. The runner refuses before any adapter runs when the check has a bad
shape, when the student reports the check by hand, when no adapter handles the runner type, and when the
workspace is not a folder. Nothing was examined, so there is no work to point an attempt at. A `check_result`
needs an attempt id, so it is not written alone either. A started run writes both events whatever its status:
`passed`, `failed`, `error`, or `skipped`.

A started run also needs a task id, because the contract requires one for both types. A check has no link to
a task in the lesson module. So the task is the module's only task, or the task the caller names. A module
with several tasks and no named task writes nothing, and the report says `task_required`. A task id is never
made up.

Reporting
---------
Writing the events never changes the check result and never raises. The caller gets an `EventsReport`:
`written` is `True`, `False`, or `'partial'` (the attempt is in the log and the result is not), and `error` is
a stable code from `ERROR_CODES` when `written` is not `True`. The report never holds the text of an error.
"""
import dataclasses
import datetime
import hashlib
import os
import re
import secrets
import tempfile
import time
import uuid

import check_consent
import check_runner
import check_script
import check_workspace
import learning_event
from check_workspace import WorkspaceError
from event_log import EventLogError

SCHEMA_VERSION = '1.0'
SOURCE = 'app'
# The contract caps `artifactBytes` here.
MAX_ARTIFACT_BYTES = 104857600
# The contract caps `durationMs` here.
MAX_DURATION_MS = 3600000
_CHUNK = 65536
_FILE_RUNNERS = ('file-exists', 'text-match', 'json-valid')

# Codes for `EventsReport.error`.
NOT_RUN = 'not_run'
TASK_REQUIRED = 'task_required'
TASK_UNKNOWN = 'task_unknown'
LOG_FULL = 'log_full'
LOG_DAMAGED = 'log_damaged'
LOG_UNAVAILABLE = 'log_unavailable'
ARTIFACT_TOO_LARGE = 'artifact_too_large'
ARTIFACT_UNAVAILABLE = 'artifact_unavailable'
EVENT_INVALID = 'event_invalid'
EVENTS_FAILED = 'events_failed'
ERROR_CODES = frozenset({NOT_RUN, TASK_REQUIRED, TASK_UNKNOWN, LOG_FULL, LOG_DAMAGED, LOG_UNAVAILABLE, ARTIFACT_TOO_LARGE, ARTIFACT_UNAVAILABLE,
                         EVENT_INVALID, EVENTS_FAILED})

# What each code of the event store becomes. A store code that is not here becomes `events_failed`.
_STORE_CODES = {
    'log_full': LOG_FULL, 'log_reserved': LOG_FULL,
    'log_corrupt': LOG_DAMAGED,
    'log_unreadable': LOG_UNAVAILABLE, 'lock_unavailable': LOG_UNAVAILABLE, 'write_failed': LOG_UNAVAILABLE,
    'fsync_failed': LOG_UNAVAILABLE, 'unsafe_path': LOG_UNAVAILABLE,
    'invalid_event': EVENT_INVALID, 'event_too_large': EVENT_INVALID, 'unsupported_version': EVENT_INVALID,
}

# Every reason code that a runner in this repository can give. An event keeps a reason only when it is on this list.
REASON_CODES = frozenset({
    # check_runner and check_workspace
    'invalid_check', 'self_reported', 'unknown_runner_type', 'adapter_failed', 'invalid_descriptor',
    'not_found', 'wrong_kind', 'unsafe_path', 'not_regular', 'file_too_large', 'unreadable', 'workspace_unavailable',
    'empty', 'invalid_json', 'wrong_top_level', 'missing_keys',
    'pattern_timeout', 'invalid_pattern', 'kill_failed', 'pattern_failed',
    # check_script
    'script_not_defined', 'consent_required', 'runtime_unavailable', 'busy', 'output_too_large', 'script_timeout', 'script_killed',
    'script_failed', 'script_not_started', 'workspace_too_large', 'unreadable_workspace', 'unsafe_workspace',
    # check_git
    'git_unavailable', 'git_failed', 'git_timeout', 'no_commits', 'no_repository', 'unsafe_git_dir', 'untrusted_repository',
})


def new_session_id():
    """A new lowercase UUID for one sitting. The hub makes one when it starts. Each command line run makes its own."""
    return str(uuid.uuid4())


def _new_event_id():
    return str(uuid.uuid4())


def _new_attempt_id():
    return 'att-' + secrets.token_hex(8)


@dataclasses.dataclass(frozen=True)
class Sources:
    """Where ids and times come from. A test replaces them with fixed values."""
    now: object = time.time
    monotonic: object = time.monotonic
    event_id: object = _new_event_id
    attempt_id: object = _new_attempt_id


@dataclasses.dataclass(frozen=True)
class EventsReport:
    """What happened to the events of one run. `written` is True, False, or 'partial'."""
    written: object
    error: object = None

    def fields(self):
        """The fields that the hub response and the command line output carry."""
        result = {'eventsWritten': self.written}
        if self.error is not None:
            result['eventsError'] = self.error
        return result


def error_code(error):
    """The stable code for an exception from the event log. The text of the error is never used."""
    if isinstance(error, EventLogError):
        code = error.code
        return _STORE_CODES.get(code, EVENTS_FAILED) if type(code) is str else EVENTS_FAILED
    return EVENTS_FAILED


# ---------------------------------------------------------------------------
# The digest of the work
# ---------------------------------------------------------------------------

class ArtifactError(Exception):
    """The digest of the work cannot be made. `code` is `artifact_too_large` or `artifact_unavailable`."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class Artifact:
    """The digest of the work (64 lowercase hex characters) and its size in bytes."""
    digest: str
    size: int


def digest_entries(entries):
    """The artifact for `(relative path, file sha256 hex, size)` entries. The order of the entries does not matter."""
    rows = sorted((path.encode('utf-8', errors='surrogatepass'), sha, size) for path, sha, size in entries)
    total = sum(size for _path, _sha, size in rows)
    if total > MAX_ARTIFACT_BYTES:
        raise ArtifactError(ARTIFACT_TOO_LARGE)
    manifest = hashlib.sha256()
    for path, sha, size in rows:
        manifest.update(path + b'\0' + sha.encode('ascii') + b'\0' + str(size).encode('ascii') + b'\n')
    return Artifact(manifest.hexdigest(), total)


_EMPTY = Artifact(hashlib.sha256(b'').hexdigest(), 0)


def _hash_descriptor(read, limit):
    """(sha256 hex, size) of what `read(n)` gives. Returns None when the data is longer than `limit`."""
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = read(min(_CHUNK, limit + 1 - total))
        if not chunk:
            return digest.hexdigest(), total
        total += len(chunk)
        if total > limit:
            return None
        digest.update(chunk)


def _file_artifact(workspace, path, limit, limits, report_large):
    """The artifact for one file inside the workspace. A file that cannot be examined is the empty manifest."""
    try:
        parts = check_workspace.split_relative(path, **limits.path_limits())
        root = check_workspace.workspace_root(workspace)
        descriptor = check_workspace.open_inside(root, path, 'file', limit, **limits.path_limits())
    except WorkspaceError as error:
        if error.code == check_workspace.TOO_LARGE and report_large:
            raise ArtifactError(ARTIFACT_TOO_LARGE) from None
        return _EMPTY
    except OSError:
        return _EMPTY
    try:
        found = _hash_descriptor(lambda size: os.read(descriptor, size), limit)
    except OSError:
        raise ArtifactError(ARTIFACT_UNAVAILABLE) from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if found is None:
        if report_large:
            raise ArtifactError(ARTIFACT_TOO_LARGE)
        return _EMPTY
    return digest_entries([('/'.join(parts), found[0], found[1])])


def _tree_artifact(workspace, limits):
    """The artifact for the files the script runner copies. The copy is made again here and deleted at once."""
    try:
        root = check_workspace.workspace_root(workspace)
    except WorkspaceError:
        return _EMPTY
    try:
        scratch = tempfile.mkdtemp(prefix=check_script.PRIVATE_PREFIX)
    except OSError:
        raise ArtifactError(ARTIFACT_UNAVAILABLE) from None
    try:
        work = os.path.join(scratch, 'work')
        try:
            os.mkdir(work, 0o700)
            check_script.copy_workspace(root, work, limits.script_copy_bytes, limits.script_copy_files)
        except check_script.ScriptProblem:
            return _EMPTY
        except OSError:
            raise ArtifactError(ARTIFACT_UNAVAILABLE) from None
        entries = []
        try:
            for current, _folders, files in os.walk(work):
                for name in files:
                    full = os.path.join(current, name)
                    with open(full, 'rb') as handle:
                        found = _hash_descriptor(handle.read, MAX_ARTIFACT_BYTES)
                    if found is None:
                        raise ArtifactError(ARTIFACT_TOO_LARGE)
                    entries.append((os.path.relpath(full, work).replace(os.sep, '/'), found[0], found[1]))
        except OSError:
            raise ArtifactError(ARTIFACT_UNAVAILABLE) from None
        return digest_entries(entries)
    finally:
        check_script.remove_scratch(scratch)


def artifact_for(check, workspace, limits):
    """The `Artifact` for the files a check reads, as they stand now. See the module notes for the rule.

    Raises `ArtifactError` only when the work is over the size cap or the system cannot read it. A file that is
    missing, a path that is not safe, or a runner that reads no file gives the empty manifest.
    """
    runner = check.get('runner') if type(check) is dict else None
    runner_type = runner.get('type') if type(runner) is dict else None
    if check_consent.needs_consent(check):
        return _tree_artifact(workspace, limits)
    if runner_type in _FILE_RUNNERS:
        path = runner.get('path')
        if type(path) is not str:
            return _EMPTY
        if runner_type == 'file-exists':
            return _file_artifact(workspace, path, MAX_ARTIFACT_BYTES, limits, True)
        return _file_artifact(workspace, path, limits.max_file_bytes, limits, False)
    return _EMPTY


# ---------------------------------------------------------------------------
# The two events
# ---------------------------------------------------------------------------

def evidence_code(outcome):
    """The short code for the `evidence` field: the result's reason when it is on the fixed list, else the empty string."""
    evidence = outcome.get('evidence') if type(outcome) is dict else None
    reason = evidence.get('reason') if type(evidence) is dict else None
    if type(reason) is str and len(reason) <= 40 and reason in REASON_CODES:
        return reason
    return ''


def resolve_task(module, requested):
    """`(task id, None)` or `(None, error code)`. A task id is the one that was named, or the module's only task."""
    tasks = module.get('tasks') if type(module) is dict else None
    ids = [task['id'] for task in tasks if type(task) is dict and type(task.get('id')) is str] if type(tasks) is list else []
    if requested is not None:
        if type(requested) is str and requested in ids:
            return requested, None
        return None, TASK_UNKNOWN
    if len(ids) == 1:
        return ids[0], None
    return None, TASK_REQUIRED


def _stamp(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _duration_ms(began, ended):
    return max(0, min(MAX_DURATION_MS, int((ended - began) * 1000)))


def _runner_type(check):
    runner = check.get('runner') if type(check) is dict else None
    value = runner.get('type') if type(runner) is dict else None
    return value if type(value) is str else None


def build_events(*, session_id, module, task_id, check, outcome, duration_ms, artifact, started_at, finished_at, sources):
    """The two events of one started run, in the order they are written: the attempt, then the result."""
    attempt_id = sources.attempt_id()

    def envelope(event_type, stamp, data):
        return {'schemaVersion': SCHEMA_VERSION, 'eventId': sources.event_id(), 'time': _stamp(stamp), 'sessionId': session_id,
                'moduleId': module['id'], 'moduleVersion': module['version'], 'taskId': task_id, 'source': SOURCE, 'skills': [],
                'type': event_type, 'data': data}

    attempt = envelope('attempt_submitted', started_at, {'attemptId': attempt_id, 'artifactDigest': artifact.digest, 'artifactBytes': artifact.size})
    result = envelope('check_result', finished_at, {'checkId': check['id'], 'attemptId': attempt_id, 'status': outcome['status'], 'durationMs': duration_ms,
                                                    'evidence': evidence_code(outcome), 'runnerType': _runner_type(check)})
    return [attempt, result]


def _append(log, events):
    written = 0
    for event in events:
        try:
            log.append(event)
        except Exception as error:  # noqa: BLE001 - any fault becomes a code; a KeyboardInterrupt still stops the program
            return EventsReport('partial' if written else False, error_code(error))
        written += 1
    return EventsReport(True)


def _read_clock(clock):
    try:
        return clock()
    except Exception:  # noqa: BLE001 - a broken clock must not stop the check
        return None


def run_and_record(check, workspace, *, log, session_id, module, task_id=None, consent=None, limits=None, registry=None, sources=None):
    """Run one check and write its events. Returns `(result, EventsReport)`.

    The result is exactly what `check_runner.run` gives. It never changes because of the log. `module` is the stored
    module document the check belongs to. `task_id` is optional; see the module notes. `log` is an `EventLog`.
    """
    sources = sources or Sources()
    started_at = _read_clock(sources.now)
    began = _read_clock(sources.monotonic)
    trace = check_runner.run_traced(check, workspace, limits, registry, consent=consent)
    ended = _read_clock(sources.monotonic)
    finished_at = _read_clock(sources.now)
    if not trace.started:
        return trace.result, EventsReport(False, NOT_RUN)
    try:
        chosen, problem = resolve_task(module, task_id)
        if problem is not None:
            return trace.result, EventsReport(False, problem)
        artifact = artifact_for(check, workspace, limits or check_runner.Limits())
        events = build_events(session_id=session_id, module=module, task_id=chosen, check=check, outcome=trace.result,
                              duration_ms=_duration_ms(began, ended), artifact=artifact, started_at=started_at, finished_at=finished_at,
                              sources=sources)
        if any(not learning_event.validate_event(event)['ok'] for event in events):
            return trace.result, EventsReport(False, EVENT_INVALID)
    except ArtifactError as error:
        return trace.result, EventsReport(False, error.code)
    except Exception:  # noqa: BLE001 - the check result must come back whatever goes wrong here
        return trace.result, EventsReport(False, EVENTS_FAILED)
    return trace.result, _append(log, events)
