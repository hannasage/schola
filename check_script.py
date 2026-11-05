"""Tier 2: run a script that a lesson author wrote, after the student agreed.

THIS IS CONSENT, NOT ISOLATION. A script the student agrees to run can still
read the student's files and use the network. It runs with the student's own
permissions. What this module does is bound the damage a careless or greedy
script can do, with limits that work on macOS and Linux alike:

* It refuses to start a process unless a consent for the exact hash of the check
  is passed in. The hash covers the runner fields, the runtime, the arguments,
  the time limit, and the script bytes.
* It copies the workspace into a scratch folder and runs the script there. A
  relative path cannot touch the real workspace. The copy is at most 20 MB and
  2000 files. `.git` and `node_modules` are not copied. A link that leaves the
  workspace refuses the whole run.
* It runs `[runtime, script file, *args]` with no shell, no standard input, and a
  clean environment: `PATH`, `LANG`, and `HOME` (the scratch folder) only.
* The process starts in its own session. After the time limit, or when the
  output passes 64 KB, the whole process group gets SIGKILL. Children that stay
  in the group die with it. A child that starts a new session escapes.
* Resource limits (CPU seconds, file size, open files, process count, core
  dumps) are set by a small launcher that then replaces itself with the runtime.
  The launcher avoids `preexec_fn`, which is unsafe in a program with threads.
  A limit the system cannot set is listed in the result, never skipped quietly.
  No memory limit is set, because none is portable.
* One script runs at a time.
* The scratch folder is deleted afterward, and only if it is the folder this
  module made in the temp root.

Only the Python standard library is used.
"""
import hmac
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

try:
    import resource
except ImportError:  # Windows has no resource module.
    resource = None

import check_consent
import check_runner
import check_workspace
from check_runner import DescriptorError, result, take_fields
from check_workspace import WorkspaceError

PRIVATE_PREFIX = 'schola-script-'
SCRIPT_BASENAME = 'schola_check_script'
EXTENSIONS = {'python3': '.py', 'node': '.js'}
RUNTIMES = tuple(EXTENSIONS)
SKIP_FOLDERS = frozenset({'.git', 'node_modules'})
SAFE_PATH = '/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin'
MAX_SOURCE_BYTES = 65536
MAX_ARGS = 8
MAX_ARG_CHARS = 200
OUTPUT_TEXT_CHARS = 2000

_RUN_LOCK = threading.Lock()


def _cpu(context):
    return context['timeout'] + 1


def _nproc(context):
    return None if context['user_processes'] is None else context['user_processes'] + context['nproc_headroom']


# (label, resource name, function of the context that gives the value or None). The launcher sets each one.
LIMIT_PLAN = (
    ('cpu', 'RLIMIT_CPU', _cpu),
    ('fsize', 'RLIMIT_FSIZE', lambda context: context['fsize']),
    ('nofile', 'RLIMIT_NOFILE', lambda context: context['nofile']),
    ('nproc', 'RLIMIT_NPROC', _nproc),
    ('core', 'RLIMIT_CORE', lambda context: 0),
)

# Runs in a fresh Python, sets the limits, writes a report, and replaces itself with the runtime.
_LAUNCHER = r'''
import json, os, sys
spec = json.loads(sys.argv[1])
report = {"applied": [], "failed": []}
try:
    import resource
except ImportError:
    resource = None
for label, name, value in spec:
    try:
        number = getattr(resource, name)
        soft, hard = resource.getrlimit(number)
        target = value if hard == resource.RLIM_INFINITY else min(value, hard)
        if label == "nofile" and soft != resource.RLIM_INFINITY and soft < target:
            target = soft
        upper = target + 1 if label == "cpu" and (hard == resource.RLIM_INFINITY or target + 1 <= hard) else target
        resource.setrlimit(number, (target, upper))
        report["applied"].append([label, resource.getrlimit(number)[0]])
    except Exception as error:
        report["failed"].append([label, type(error).__name__ + ": " + str(error)[:80]])
try:
    with open(sys.argv[2], "w") as handle:
        json.dump(report, handle)
except Exception:
    pass
try:
    os.execv(sys.argv[3], sys.argv[3:])
except OSError as error:
    report["exec"] = type(error).__name__ + ": " + str(error)[:80]
    try:
        with open(sys.argv[2], "w") as handle:
            json.dump(report, handle)
    except Exception:
        pass
    sys.exit(127)
'''


class ScriptProblem(Exception):
    def __init__(self, status, reason, message):
        super().__init__(message)
        self.status, self.reason, self.message = status, reason, message

    def as_result(self, **evidence):
        return result(self.status, {'reason': self.reason, **evidence}, [self.message])


# ---------------------------------------------------------------------------
# The descriptor
# ---------------------------------------------------------------------------

def read_script(check, limits):
    """The checked fields of a script check: (runtime, source, args, timeout seconds). Raises DescriptorError."""
    take_fields(check['runner'], {})  # a script runner holds only its type
    runtime, source = check.get('runtime'), check.get('source')
    if runtime not in RUNTIMES:
        raise DescriptorError('The runtime must be python3 or node.')
    if not isinstance(source, str) or not source:
        raise DescriptorError('A script check needs the script text.')
    if '\x00' in source:
        raise DescriptorError('The script holds a NUL character.')
    try:
        encoded = source.encode('utf-8')
    except UnicodeError:
        raise DescriptorError('The script holds a character that is not valid text.') from None
    if len(encoded) > MAX_SOURCE_BYTES:
        raise DescriptorError(f'The script is larger than {MAX_SOURCE_BYTES} bytes.')
    args = check.get('args', [])
    if (not isinstance(args, list) or len(args) > MAX_ARGS
            or any(not isinstance(item, str) or len(item) > MAX_ARG_CHARS or '\x00' in item for item in args)):
        raise DescriptorError(f'The arguments must be at most {MAX_ARGS} short texts.')
    try:
        for item in args:
            item.encode('utf-8')
    except UnicodeError:
        raise DescriptorError('An argument holds a character that is not valid text.') from None
    timeout = check.get('timeoutSeconds', check_consent.DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, float) and timeout.is_integer():
        timeout = int(timeout)  # JSON Schema counts 30.0 as an integer, so the runtime does too
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise DescriptorError('The time limit must be a whole number from 1 to 120 seconds.')
    return runtime, source, list(args), timeout


def consent_allows(check, consent):
    """True when `consent` names the exact hash of this check."""
    if not isinstance(consent, dict) or not isinstance(consent.get('checkHash'), str):
        return False
    return hmac.compare_digest(consent['checkHash'].encode('utf-8', errors='replace'), check_consent.check_hash(check).encode('ascii'))


# ---------------------------------------------------------------------------
# The scratch copy
# ---------------------------------------------------------------------------

def copy_workspace(root, destination, max_bytes, max_files):
    """Copy the workspace into `destination`. Returns a list of notes. Raises ScriptProblem if it cannot be done safely."""
    total = entries = 0
    skipped = {'folders': 0, 'special': 0, 'names': 0, 'links': 0}
    too_big = ScriptProblem('error', 'workspace_too_large',
                            'Your folder is larger than 20 MB or holds more than 2000 files. Schola does not copy .git or node_modules. Use a smaller folder for this check.')
    def unreadable(error):
        raise _unreadable() from error

    for current, folders, files in os.walk(root, followlinks=False, onerror=unreadable):
        relative = os.path.relpath(current, root)
        parts = [] if relative == '.' else relative.split(os.sep)
        kept = []
        for name in sorted(folders):
            path = os.path.join(current, name)
            if name in SKIP_FOLDERS:
                skipped['folders'] += 1
            elif os.path.islink(path):
                if not check_workspace.is_inside(root, os.path.realpath(path)):
                    raise _outside()
                skipped['links'] += 1
            else:
                try:
                    check_workspace.split_relative('/'.join(parts + [name]))
                except WorkspaceError:
                    skipped['names'] += 1
                    continue
                kept.append(name)
        folders[:] = kept
        os.makedirs(os.path.join(destination, *parts), mode=0o700, exist_ok=True)
        for name in kept:
            entries += 1
            os.makedirs(os.path.join(destination, *parts, name), mode=0o700, exist_ok=True)
        for name in sorted(files):
            path = os.path.join(current, name)
            try:
                info = os.lstat(path)
            except OSError:
                skipped['special'] += 1
                continue
            if stat.S_ISLNK(info.st_mode):
                real = os.path.realpath(path)
                if not check_workspace.is_inside(root, real):
                    raise _outside()
                try:
                    if not stat.S_ISREG(os.stat(real).st_mode):
                        skipped['links'] += 1
                        continue
                    info = os.stat(real)
                except OSError:
                    skipped['links'] += 1
                    continue
            elif not stat.S_ISREG(info.st_mode):
                skipped['special'] += 1
                continue
            try:
                check_workspace.split_relative('/'.join(parts + [name]))
            except WorkspaceError:
                skipped['names'] += 1
                continue
            entries += 1
            if entries > max_files:
                raise too_big
            try:
                data = check_workspace.read_file(root, '/'.join(parts + [name]), max_bytes - total)
            except WorkspaceError as error:
                if error.code == check_workspace.TOO_LARGE:
                    raise too_big from None
                if error.code == check_workspace.OUTSIDE:
                    raise _outside() from None
                if error.code == check_workspace.UNREADABLE:
                    raise _unreadable() from None
                skipped['special'] += 1
                continue
            total += len(data)
            target = os.path.join(destination, *parts, name)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700 if info.st_mode & 0o111 else 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
        if entries > max_files:
            raise too_big
    notes = []
    if skipped['folders']:
        notes.append(f'Did not copy {skipped["folders"]} .git or node_modules folders.')
    if skipped['special'] or skipped['names'] or skipped['links']:
        notes.append(f'Did not copy {skipped["special"] + skipped["names"] + skipped["links"]} special files, linked folders, or files with unusual names.')
    return notes


def _unreadable():
    return ScriptProblem('error', 'unreadable_workspace',
                         'Schola could not read a file or folder in your folder, so a copy would be incomplete. Fix its permissions and try again.')


def _outside():
    return ScriptProblem('error', 'unsafe_workspace', 'A link in your folder points outside it, so Schola will not copy the folder. Remove the link and try again.')


def _make_writable(path):
    """Give the owner full access to a folder or file inside the scratch folder. A link is never followed."""
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            return
        os.chmod(path, 0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
    except OSError:
        pass


def remove_scratch(path):
    """Delete the scratch folder, but only a folder this module made directly in the temp root.

    Returns True when nothing is left. Returns False when the folder is still there.
    """
    try:
        real = os.path.realpath(path)
        if (os.path.dirname(real) != os.path.realpath(tempfile.gettempdir()) or not os.path.basename(real).startswith(PRIVATE_PREFIX)
                or os.path.islink(path)):
            return True

        def fix(function, target, _error):
            _make_writable(os.path.dirname(target))
            _make_writable(target)
            try:
                function(target)
            except OSError:
                pass

        _make_writable(real)
        for current, folders, _files in os.walk(real):
            for name in folders:
                _make_writable(os.path.join(current, name))
        if sys.version_info >= (3, 12):
            shutil.rmtree(real, onexc=fix)
        else:
            shutil.rmtree(real, onerror=fix)
        return not os.path.lexists(real)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def count_user_processes():
    """How many processes this user has now, or None. The process limit counts the whole user, not one tree."""
    if not hasattr(os, 'getuid'):
        return None
    try:
        done = subprocess.run(['/bin/ps', '-U', str(os.getuid()), '-o', 'pid='], capture_output=True, timeout=5, env={'PATH': '/usr/bin:/bin'}, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return len(done.stdout.split())


def plan_limits(timeout, limits):
    """(spec for the launcher, notes for limits that cannot be set before the launch)."""
    context = {'timeout': timeout, 'fsize': limits.script_fsize_bytes, 'nofile': limits.script_nofile,
               'nproc_headroom': limits.script_nproc_headroom, 'user_processes': None}
    if any(entry[0] == 'nproc' for entry in LIMIT_PLAN) and resource is not None:
        context['user_processes'] = count_user_processes()
    spec, missing = [], []
    for label, name, value in LIMIT_PLAN:
        if resource is None:
            missing.append(f'{label}: the resource module is not available on this system')
            continue
        number = value(context)
        if number is None:
            missing.append(f'{label}: Schola could not work out a safe value')
        else:
            spec.append([label, name, number])
    return spec, missing


def describe_limits(limits, timeout):
    """(limits Schola intends to set, limits that do not apply). For the consent screen, before a run."""
    planned = [f'timeout={timeout}s', f'output={limits.script_output_bytes}B', f'copy={limits.script_copy_bytes}B/{limits.script_copy_files}files']
    if resource is not None:
        planned += [f'cpu={timeout + 1}s', f'fsize={limits.script_fsize_bytes}B', f'nofile={limits.script_nofile}', 'nproc=your process count plus '
                    f'{limits.script_nproc_headroom}', 'core=0']
    if sys.platform == 'darwin':
        missing = ['memory: macOS does not allow a memory limit, so none is set']
    else:
        missing = ['memory: no portable memory limit exists, so none is set']
    if resource is None:
        missing += [f'{label}: the resource module is not available on this system' for label, _name, _fn in LIMIT_PLAN]
    missing.append('network: the script can use the network')
    missing.append('files: the script can read any file you can read')
    return planned, missing


_UNITS = {'cpu': 's', 'fsize': 'B'}


def _describe(label, value):
    return f'{label}={value}{_UNITS.get(label, "")}'


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class ScriptAdapter:
    """The `script` runner type: `runtime`, `source`, `args`, and `timeoutSeconds` live on the check."""
    types = ('script',)
    needs_consent = True

    def run(self, check, workspace, limits, consent=None):
        if not check_consent.needs_consent(check):
            # A version 1.0 script runner holds no script. There is nothing to run and nothing to agree to.
            return result('skipped', {'reason': 'script_not_defined'},
                          ['This check names a script runner but holds no script, so Schola cannot run it. Report the result yourself (self-report).'])
        try:
            runtime, source, args, timeout = read_script(check, limits)
        except DescriptorError as error:
            return check_runner.bad_descriptor(error)
        if not consent_allows(check, consent):
            return result('error', {'reason': 'consent_required'}, ['The student has not agreed to run this exact script. Nothing was started.'])
        executable = shutil.which(runtime, path=SAFE_PATH)
        if not executable:
            return result('skipped', {'reason': 'runtime_unavailable', 'runtime': runtime},
                          [f'{runtime} was not found in the standard program folders, so Schola cannot run this check. Report the result yourself (self-report).'])
        if not _RUN_LOCK.acquire(blocking=False):
            return result('error', {'reason': 'busy'}, ['Another script is running. Wait for it to finish.'])
        scratch, clean = None, True
        try:
            scratch = tempfile.mkdtemp(prefix=PRIVATE_PREFIX)
            outcome = self._run(os.path.abspath(executable), runtime, source, args, timeout, workspace, scratch, limits)
        except ScriptProblem as problem:
            outcome = problem.as_result()
        except OSError:
            outcome = result('error', {'reason': 'script_failed'}, ['Schola could not set up or start the script.'])
        finally:
            _RUN_LOCK.release()
            if scratch is not None:
                clean = remove_scratch(scratch)
        if not clean:
            outcome['log'].append('Schola could not remove its temporary folder. It is safe to delete a folder named schola-script-* in your temp folder.')
        return outcome

    def _run(self, executable, runtime, source, args, timeout, workspace, scratch, limits):
        work = os.path.join(scratch, 'work')
        os.mkdir(work, 0o700)
        notes = copy_workspace(workspace, work, limits.script_copy_bytes, limits.script_copy_files)
        script_path = os.path.join(work, SCRIPT_BASENAME + EXTENSIONS[runtime])
        fd = os.open(script_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(source.encode('utf-8'))
        spec, missing = plan_limits(timeout, limits)
        report_path = os.path.join(scratch, 'limits.json')
        target = [executable, script_path, *args]
        if spec:
            command = [sys.executable, '-I', '-S', '-c', _LAUNCHER, json.dumps(spec), report_path, *target]
        else:
            command = target
        env = {'PATH': SAFE_PATH, 'LANG': 'C.UTF-8', 'HOME': scratch}
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, cwd=work, env=env, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        except OSError:
            raise ScriptProblem('error', 'script_failed', 'Schola could not start the script.') from None
        cap = limits.script_output_bytes
        collector = Collector(process, cap, on_overflow=lambda: check_runner.kill_group(process))
        reader = threading.Thread(target=collector.run, daemon=True)
        reader.start()
        timed_out = False
        try:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                killed = check_runner.kill_group(process)
                try:
                    process.wait(timeout=limits.kill_wait_seconds)
                except subprocess.TimeoutExpired:
                    killed = False
                if not killed:
                    raise ScriptProblem('error', 'kill_failed', 'The script ran too long and Schola could not stop it. Close Schola and open it again.') from None
            # Anything the script left running in its group goes now.
            check_runner.kill_group(process)
            reader.join(2)
            incomplete = reader.is_alive()
        finally:
            collector.stop()
        duration = int((time.monotonic() - started) * 1000)
        return self._result(process.returncode, runtime, collector.snapshot(), cap, incomplete, timed_out, duration, timeout, spec, missing, report_path, notes, limits)

    @staticmethod
    def _result(code, runtime, snapshot, cap, incomplete, timed_out, duration, timeout, spec, missing, report_path, notes, limits):
        data, total, overflow = snapshot
        applied = [f'timeout={timeout}s', f'output={cap}B', f'copy={limits.script_copy_bytes}B/{limits.script_copy_files}files']
        not_applied = ['memory: No portable memory limit exists, so Schola sets none'] + list(missing)
        reported = _read_report(report_path) if spec else None
        if spec:
            if reported is None:
                not_applied.append('resource limits: the launcher did not report, so they are unknown')
                if code not in (0, None) and not timed_out and not overflow:
                    raise ScriptProblem('error', 'script_not_started', 'Schola could not start the script. The launcher stopped before it ran the runtime.')
            else:
                if reported.get('exec'):
                    raise ScriptProblem('error', 'script_not_started',
                                        'Schola could not start the script. The runtime could not be run, so this is not a result for your work.')
                applied += [_describe(label, value) for label, value in reported['applied']]
                not_applied += [f'{label}: could not be set ({reason})' for label, reason in reported['failed']]
        if incomplete:
            not_applied.append('process group: a child of the script started its own session and still holds the output, so Schola could not stop it')
        cleaned = check_runner.clean_text(' | '.join(bytes(data[:cap]).decode('utf-8', errors='replace').splitlines()), 10 ** 9)
        shown = cleaned[:limits.max_evidence_chars]
        evidence = {
            'runtime': runtime, 'exitCode': code if code is not None and code >= 0 else None, 'timedOut': timed_out,
            'outputTruncated': overflow or len(cleaned) > len(shown), 'outputIncomplete': incomplete, 'outputBytes': min(total, cap),
            'durationMs': duration, 'output': shown, 'limits': applied, 'limitsNotApplied': not_applied,
        }
        log = ['This is consent, not a sandbox. The script can still read your files and use the network.']
        log += notes
        log.append('Limits applied: ' + ', '.join(applied) + '.')
        log += [('Memory limit: ' + item.split(': ', 1)[1] + '.') if item.startswith('memory') else ('Not applied: ' + item + '.') for item in not_applied]
        if incomplete:
            log.append('A child of the script started its own session and still holds the output. The output may be incomplete, and the child may still be running.')
        if overflow:
            return result('error', {**evidence, 'reason': 'output_too_large'}, ['The script printed more than 64 KB, so Schola stopped it.'] + log)
        if timed_out:
            return result('error', {**evidence, 'reason': 'script_timeout'}, [f'The script ran longer than {timeout} seconds, so Schola stopped it.'] + log)
        if code == 0:
            return result('passed', evidence, ['The script finished with exit code 0.'] + log)
        if code is not None and code > 0:
            hint = ['Exit code 126 or 127 can mean the runtime could not start. Check that it runs from a terminal.'] if code in (126, 127) else []
            return result('failed', evidence, [f'The script finished with exit code {code}.'] + hint + log)
        name = _signal_name(-code) if code is not None else 'unknown'
        return result('error', {**evidence, 'reason': 'script_killed', 'signal': name}, [f'The system stopped the script ({name}).'] + log)


def _read_report(path):
    try:
        with open(path, encoding='utf-8') as handle:
            report = json.load(handle)
        if isinstance(report, dict) and isinstance(report.get('applied'), list) and isinstance(report.get('failed'), list):
            return report
    except (OSError, ValueError):
        pass
    return None


def _signal_name(number):
    try:
        return signal.Signals(number).name
    except ValueError:
        return f'signal {number}'


Collector = check_runner.Collector
