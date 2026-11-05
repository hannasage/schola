"""The check runner: one interface, and the built-in checks that run no creator code.

    run(check, workspace, limits) -> {status, evidence, log}

`check` is one entry of a lesson module's `checks` list. `workspace` is the
student's folder. `limits` is a `Limits` value, or None for the defaults.

The result always has these three parts:

* `status` is `passed`, `failed`, `error`, or `skipped`.
  `failed` means the student's work did not meet the check.
  `error` means the check itself could not run safely or at all.
  `skipped` means the app has no way to run this check. The student then
  reports the result by hand. A check that cannot be run is never `passed`.
* `evidence` is a small flat object of facts. It holds counts and names, never
  the content of the student's files.
* `log` is a short list of plain sentences.

Both are bounded and stripped of control characters, whatever an adapter
returns. An adapter that raises, or that returns something odd, gives an
`error` and never a pass.

An adapter is an object with a `types` tuple and a `run(check, workspace,
limits)` method that returns the same three parts. A `Registry` maps runner
types to adapters. The built-in adapter handles `file-exists`, `text-match`,
and `json-valid`. Other adapters join through `register`.

Only the Python standard library is used.
"""
import dataclasses
import json
import os
import re
import signal
import subprocess
import threading
import sys

import check_workspace
from check_workspace import WorkspaceError

STATUSES = ('passed', 'failed', 'error', 'skipped')

# Control characters, and the invisible or direction-changing characters that can make text look different from what it is.
_CONTROL = re.compile('[\\x00-\\x1f\\x7f-\\x9f\\u00ad\\u061c\\u200b-\\u200f\\u2028\\u2029\\u202a-\\u202e\\u2060\\u2066-\\u2069\\ufeff]')


@dataclasses.dataclass(frozen=True)
class Limits:
    """Every bound the runner applies. A check cannot raise one."""
    max_file_bytes: int = check_workspace.DEFAULT_MAX_FILE_BYTES
    max_path_bytes: int = check_workspace.DEFAULT_MAX_PATH_BYTES
    max_component_bytes: int = check_workspace.DEFAULT_MAX_COMPONENT_BYTES
    max_depth: int = check_workspace.DEFAULT_MAX_DEPTH
    max_pattern_chars: int = 200
    max_min_count: int = 1000
    regex_timeout: float = 2.0
    max_evidence_keys: int = 20
    max_evidence_chars: int = 500
    max_evidence_items: int = 20
    max_log_lines: int = 20
    max_log_chars: int = 300
    git_timeout: float = 10.0
    kill_wait_seconds: float = 5.0
    git_output_bytes: int = 1_000_000
    git_max_scan: int = 500
    script_output_bytes: int = 65536
    script_copy_bytes: int = 20_000_000
    script_copy_files: int = 2000
    script_fsize_bytes: int = 16 * 1024 * 1024
    script_nofile: int = 256
    script_nproc_headroom: int = 100

    def replace(self, **changes):
        return dataclasses.replace(self, **changes)

    def path_limits(self):
        return {'max_path_bytes': self.max_path_bytes, 'max_component_bytes': self.max_component_bytes, 'max_depth': self.max_depth}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def result(status, evidence=None, log=None):
    return {'status': status, 'evidence': dict(evidence or {}), 'log': list(log or [])}


def clean_text(value, limit):
    """Text with control, invisible, and direction characters removed, cut to `limit` characters."""
    return _CONTROL.sub('', value)[:limit]


_clean = clean_text


def _scalar(value, limits):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if -(2 ** 53) <= value <= 2 ** 53 else None
    if isinstance(value, float):
        return value if value == value and value not in (float('inf'), float('-inf')) else None
    if isinstance(value, str):
        return _clean(value, limits.max_evidence_chars)
    return None


def _bound_evidence(evidence, limits):
    bounded = {}
    for key, value in evidence.items():
        if len(bounded) >= limits.max_evidence_keys:
            break
        if not isinstance(key, str):
            continue
        if isinstance(value, (list, tuple)):
            items = [_scalar(item, limits) for item in list(value)[:limits.max_evidence_items] if not isinstance(item, (dict, list, tuple))]
            bounded[_clean(key, 40)] = items
        elif isinstance(value, dict):
            continue
        else:
            bounded[_clean(key, 40)] = _scalar(value, limits)
    return bounded


def _bound_log(log, limits):
    return [_clean(line, limits.max_log_chars) for line in list(log)[:limits.max_log_lines] if isinstance(line, str)]


def normalize(raw, limits):
    """Check an adapter's answer and bound it. Anything off becomes an `error`."""
    if (not isinstance(raw, dict) or raw.get('status') not in STATUSES or not isinstance(raw.get('evidence'), dict)
            or not isinstance(raw.get('log'), (list, tuple))):
        return _failure('adapter_failed', 'The check runner returned an answer Schola cannot trust.', limits)
    return result(raw['status'], _bound_evidence(raw['evidence'], limits), _bound_log(raw['log'], limits))


def _failure(reason, message, limits, status='error'):
    return result(status, {'reason': reason}, [message])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class Registry:
    """Maps a runner `type` to the adapter that runs it."""

    def __init__(self):
        self._adapters = {}

    def register(self, adapter):
        types = tuple(adapter.types)
        taken = [name for name in types if name in self._adapters]
        if taken:
            raise ValueError(f'The runner type {taken[0]} already has an adapter.')
        for name in types:
            self._adapters[name] = adapter

    def adapter_for(self, runner_type):
        return self._adapters.get(runner_type)

    def types(self):
        return sorted(self._adapters)


# ---------------------------------------------------------------------------
# Descriptor fields
# ---------------------------------------------------------------------------

class DescriptorError(Exception):
    """The runner descriptor does not fit its type."""


def take_fields(runner, spec):
    """Read the fields a type allows. `spec` maps a name to (kind, required). Any other field is an error."""
    unknown = sorted(set(runner) - set(spec) - {'type'})
    if unknown:
        raise DescriptorError(f'This check has a field its type does not use: {unknown[0]}.')
    found = {}
    for name, (kind, required) in spec.items():
        if name not in runner:
            if required:
                raise DescriptorError(f'This check needs the field {name}.')
            continue
        value = runner[name]
        if kind == 'int' and isinstance(value, float) and value.is_integer():
            value = int(value)  # JSON Schema counts 2.0 as an integer, so the runtime does too
        ok = {'str': isinstance(value, str), 'bool': isinstance(value, bool),
              'int': isinstance(value, int) and not isinstance(value, bool),
              'strlist': isinstance(value, list) and all(isinstance(item, str) for item in value)}[kind]
        if not ok:
            raise DescriptorError(f'The field {name} has the wrong kind of value.')
        found[name] = value
    return found


def bad_descriptor(error):
    return result('error', {'reason': 'invalid_descriptor'}, [str(error)])


_WORKSPACE_FAILS = {
    check_workspace.NOT_FOUND: ('failed', 'not_found', 'No file or folder exists at that path.'),
    check_workspace.WRONG_KIND: ('failed', 'wrong_kind', 'The path is the wrong kind: a file where a folder is needed, or a folder where a file is needed.'),
}
_WORKSPACE_ERRORS = {
    check_workspace.INVALID_PATH: 'unsafe_path',
    check_workspace.OUTSIDE: 'unsafe_path',
    check_workspace.NOT_REGULAR: 'not_regular',
    check_workspace.TOO_LARGE: 'file_too_large',
    check_workspace.UNREADABLE: 'unreadable',
    check_workspace.NO_WORKSPACE: 'workspace_unavailable',
}


def workspace_result(error, path=None):
    """The result for a path that could not be used."""
    if error.code in _WORKSPACE_FAILS:
        status, reason, message = _WORKSPACE_FAILS[error.code]
    else:
        status, reason, message = 'error', _WORKSPACE_ERRORS.get(error.code, 'unreadable'), str(error)
    evidence = {'reason': reason}
    if path is not None and error.code in _WORKSPACE_FAILS:
        evidence['path'] = path
    return result(status, evidence, [message])


# ---------------------------------------------------------------------------
# A regular expression runs in a child process that can be killed
# ---------------------------------------------------------------------------

# Python's `re` module has no timeout. A pattern such as (a+)+$ can run for years. So a pattern never runs in
# this process. It runs in a short-lived child, started with a clean environment in its own process group.
# After the timeout the whole group is killed.
_REGEX_WORKER = r'''
import json, re, sys
try:
    import resource
    cpu = int(sys.argv[1])
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
except Exception:
    pass
request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
try:
    flags = re.MULTILINE | (re.IGNORECASE if request["ignoreCase"] else 0)
    compiled = re.compile(request["pattern"], flags)
except Exception:
    sys.stdout.write(json.dumps({"invalid": True}))
    sys.exit(0)
count = 0
for _ in compiled.finditer(request["text"]):
    count += 1
    if count >= request["limit"]:
        break
sys.stdout.write(json.dumps({"count": count}))
'''
_CLEAN_PATH = '/usr/bin:/bin'
_live_children = set()


def live_regex_children():
    """How many pattern children are still running. It is 0 whenever no check is in progress."""
    return len(_live_children)


def kill_group(process):
    """Kill a child and every process in its group. Never raises.

    Returns True when the signal was sent or the process was already gone. Returns False when it could not be killed.
    """
    try:
        if hasattr(os, 'killpg'):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        return True
    except ProcessLookupError:
        return True
    except OSError:
        pass
    try:
        process.kill()
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return False


def count_matches(text, pattern, ignore_case, limit, timeout, kill_wait=5.0):
    """(count, problem). `problem` is None, `invalid_pattern`, `pattern_timeout`, `kill_failed`, or `pattern_failed`."""
    payload = json.dumps({'text': text, 'pattern': pattern, 'ignoreCase': ignore_case, 'limit': limit}).encode('utf-8')
    cpu = max(1, int(timeout) + 1)
    command = [sys.executable, '-I', '-S', '-c', _REGEX_WORKER, str(cpu)]
    env = {'PATH': _CLEAN_PATH, 'LANG': 'C.UTF-8'}
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env=env, cwd=os.path.abspath(os.sep), shell=False, start_new_session=True)
    except OSError:
        return 0, 'pattern_failed'
    _live_children.add(process)
    try:
        try:
            out, _ = process.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            if not kill_group(process):
                return 0, 'kill_failed'
            try:
                process.communicate(timeout=kill_wait)
            except subprocess.TimeoutExpired:
                return 0, 'kill_failed'
            return 0, 'pattern_timeout'
        if process.returncode != 0:
            return 0, 'pattern_failed'
        try:
            answer = json.loads(out.decode('utf-8'))
            count = answer['count'] if not answer.get('invalid') else None
        except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
            return 0, 'pattern_failed'
        if answer.get('invalid'):
            return 0, 'invalid_pattern'
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return 0, 'pattern_failed'
        return count, None
    finally:
        if process.poll() is None:
            kill_group(process)
            try:
                process.wait(timeout=kill_wait)
            except subprocess.TimeoutExpired:
                pass
        _live_children.discard(process)


# ---------------------------------------------------------------------------
# The built-in adapter
# ---------------------------------------------------------------------------

class BuiltinAdapter:
    """`file-exists`, `text-match`, and `json-valid`. None of them runs creator code."""
    types = ('file-exists', 'text-match', 'json-valid')

    def run(self, check, workspace, limits):
        runner = check['runner']
        handler = {'file-exists': self.file_exists, 'text-match': self.text_match, 'json-valid': self.json_valid}[runner['type']]
        try:
            return handler(runner, workspace, limits)
        except DescriptorError as error:
            return bad_descriptor(error)

    # file-exists: path, kind (file, directory, any), nonEmpty
    def file_exists(self, runner, workspace, limits):
        fields = take_fields(runner, {'path': ('str', True), 'kind': ('str', False), 'nonEmpty': ('bool', False)})
        kind = fields.get('kind', 'file')
        if kind not in ('file', 'directory', 'any'):
            raise DescriptorError('The field kind must be file, directory, or any.')
        path = fields['path']
        try:
            found, size = check_workspace.exists_inside(workspace, path, kind, 2 ** 62, **limits.path_limits())
        except WorkspaceError as error:
            return workspace_result(error, path)
        if not found:
            return workspace_result(WorkspaceError(check_workspace.NOT_FOUND), path)
        if fields.get('nonEmpty') and size == 0 and kind != 'directory':
            return result('failed', {'reason': 'empty', 'path': path}, ['The file exists, but it is empty.'])
        return result('passed', {'path': path, 'kind': kind}, ['Found the path.'])

    # text-match: path, text or pattern, ignoreCase, minCount
    def text_match(self, runner, workspace, limits):
        fields = take_fields(runner, {'path': ('str', True), 'text': ('str', False), 'pattern': ('str', False),
                                      'ignoreCase': ('bool', False), 'minCount': ('int', False)})
        if ('text' in fields) == ('pattern' in fields):
            raise DescriptorError('Give exactly one of text or pattern.')
        needle = fields.get('text', fields.get('pattern'))
        if not needle:
            raise DescriptorError('The text or pattern cannot be empty.')
        if 'pattern' in fields and len(needle) > limits.max_pattern_chars:
            raise DescriptorError(f'A pattern may hold at most {limits.max_pattern_chars} characters.')
        minimum = fields.get('minCount', 1)
        if not 1 <= minimum <= limits.max_min_count:
            raise DescriptorError(f'The field minCount must be between 1 and {limits.max_min_count}.')
        ignore_case = fields.get('ignoreCase', False)
        path = fields['path']
        try:
            data = check_workspace.read_file(workspace, path, limits.max_file_bytes, **limits.path_limits())
        except WorkspaceError as error:
            return workspace_result(error, path)
        content = data.decode('utf-8', errors='replace')
        if 'text' in fields:
            matches = (content.casefold() if ignore_case else content).count(needle.casefold() if ignore_case else needle)
        else:
            matches, problem = count_matches(content, needle, ignore_case, minimum, limits.regex_timeout, limits.kill_wait_seconds)
            if problem == 'pattern_timeout':
                return result('error', {'reason': problem, 'path': path}, ['The pattern took too long, so Schola stopped it. Ask the lesson author to simplify it.'])
            if problem == 'invalid_pattern':
                return result('error', {'reason': problem}, ['The pattern in this check is not valid.'])
            if problem == 'kill_failed':
                return result('error', {'reason': problem}, ['The pattern took too long and Schola could not stop it. Close Schola and open it again.'])
            if problem:
                return result('error', {'reason': problem}, ['The pattern could not be run.'])
        evidence = {'path': path, 'matches': min(matches, limits.max_min_count), 'required': minimum}
        if matches >= minimum:
            return result('passed', evidence, ['Found the text.'])
        return result('failed', evidence, ['The text was not found often enough.'])

    # json-valid: path, topLevel (any, object, array), requiredKeys
    def json_valid(self, runner, workspace, limits):
        fields = take_fields(runner, {'path': ('str', True), 'topLevel': ('str', False), 'requiredKeys': ('strlist', False)})
        top = fields.get('topLevel', 'any')
        if top not in ('any', 'object', 'array'):
            raise DescriptorError('The field topLevel must be any, object, or array.')
        keys = fields.get('requiredKeys', [])
        path = fields['path']
        try:
            data = check_workspace.read_file(workspace, path, limits.max_file_bytes, **limits.path_limits())
        except WorkspaceError as error:
            return workspace_result(error, path)
        try:
            document = json.loads(data.decode('utf-8-sig'), parse_constant=_no_constant)
        except (ValueError, UnicodeError, RecursionError):
            return result('failed', {'reason': 'invalid_json', 'path': path}, ['The file is not valid JSON.'])
        kind = _json_kind(document)
        evidence = {'path': path, 'topLevel': kind}
        if top != 'any' and kind != top:
            return result('failed', {**evidence, 'reason': 'wrong_top_level', 'expected': top}, [f'The top level should be {top}.'])
        if keys:
            missing = [key for key in keys if not isinstance(document, dict) or key not in document]
            if missing:
                return result('failed', {**evidence, 'reason': 'missing_keys', 'missingKeys': missing}, ['Some required keys are missing.'])
        return result('passed', evidence, ['The file is valid JSON.'])


def _no_constant(name):
    raise ValueError(f'{name} is not allowed in JSON.')


def _json_kind(value):
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, bool):
        return 'boolean'
    if value is None:
        return 'null'
    return 'string' if isinstance(value, str) else 'number'


# ---------------------------------------------------------------------------
# Reading a child's output
# ---------------------------------------------------------------------------

class Collector:
    """Reads the merged output of a process on its own thread. The reader and the caller share it through a lock.

    `snapshot()` is always whole: the bytes kept, the number of bytes seen, and whether the cap was passed.
    After `stop()` the reader adds nothing more, even if a child still holds the pipe open.
    """

    def __init__(self, process, cap, on_overflow=None):
        self._process, self._cap, self._on_overflow = process, cap, on_overflow
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._data, self._total, self._overflow = bytearray(), 0, False

    def run(self):
        stream = self._process.stdout
        try:
            fd = stream.fileno()
            while not self._stopped.is_set():
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                with self._lock:
                    if self._stopped.is_set():
                        break
                    self._total += len(chunk)
                    if len(self._data) < self._cap:
                        self._data += chunk[:self._cap - len(self._data)]
                    over = self._total > self._cap
                    self._overflow = over
                if over:
                    if self._on_overflow is not None:
                        self._on_overflow()
                    break
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def stop(self):
        self._stopped.set()

    def snapshot(self):
        with self._lock:
            return bytes(self._data), self._total, self._overflow


# ---------------------------------------------------------------------------
# Running a check
# ---------------------------------------------------------------------------

_DEFAULT = None


def default_registry():
    """The registry with every adapter this app ships."""
    global _DEFAULT
    if _DEFAULT is None:
        registry = Registry()
        registry.register(BuiltinAdapter())
        import check_git  # imported here because check_git imports this module
        registry.register(check_git.GitAdapter())
        import check_script
        registry.register(check_script.ScriptAdapter())
        _DEFAULT = registry
    return _DEFAULT


def run(check, workspace, limits=None, registry=None, consent=None):
    """Run one check. Always returns a result. Never raises, and never changes the workspace.

    `consent` is the student's agreement to a script check, as a dict with the `checkHash` that was agreed to.
    Only an adapter that sets `needs_consent` receives it, and it checks the hash itself.
    """
    limits = limits or Limits()
    registry = registry or default_registry()
    if not isinstance(check, dict):
        return _failure('invalid_check', 'This check is not in a shape Schola understands.', limits)
    if check.get('kind') == 'self-reported':
        return result('skipped', {'reason': 'self_reported'}, ['This check is reported by you. Schola does not run it.'])
    runner = check.get('runner')
    if check.get('kind') != 'automated' or not isinstance(runner, dict) or not isinstance(runner.get('type'), str):
        return _failure('invalid_check', 'This check has no runner Schola can read.', limits)
    adapter = registry.adapter_for(runner['type'])
    if adapter is None:
        return result('skipped', {'reason': 'unknown_runner_type', 'runnerType': runner['type']},
                      ['Schola cannot run this kind of check. Report the result yourself (self-report).'])
    try:
        root = check_workspace.workspace_root(workspace)
    except WorkspaceError as error:
        return workspace_result(error)
    try:
        raw = adapter.run(check, root, limits, consent=consent) if getattr(adapter, 'needs_consent', False) else adapter.run(check, root, limits)
        return normalize(raw, limits)
    except Exception as error:  # noqa: BLE001 - an adapter fault must become an error result, with no detail
        return result('error', {'reason': 'adapter_failed'}, [f'The check stopped with an unexpected problem ({type(error).__name__}).'])
