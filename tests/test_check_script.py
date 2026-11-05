"""Tier 2 script checks: consent, scratch copy, portable limits, and the routes around them.

This is consent, not isolation. These tests prove the limits the runner does set
(time, output, a clean environment, a scratch copy, and resource limits where the
system has them). They also record what a consented script can still do.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import check_consent
import check_routes
import check_runner
import check_script
from check_runner import Limits, run
from server import Handler

PYTHON = shutil.which('python3')
NODE = shutil.which('node')
SECRET = 'schola-test-secret-value'
MODULE_FIXTURE = ROOT / 'tests' / 'fixtures' / 'contracts' / 'valid' / 'lesson-module-script-check.json'


def script_check(source, runtime='python3', args=None, timeout=None, check_id='script-a'):
    check = {'id': check_id, 'kind': 'automated', 'description': 'A script check.', 'runner': {'type': 'script'},
             'runtime': runtime, 'source': textwrap.dedent(source)}
    if args is not None:
        check['args'] = args
    if timeout is not None:
        check['timeoutSeconds'] = timeout
    return check


def consent_for(check):
    return {'checkHash': check_consent.check_hash(check)}


def tree(directory):
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        relative = str(path.relative_to(directory))
        if path.is_symlink():
            result[relative] = ('link', os.readlink(path))
        elif path.is_file():
            result[relative] = ('file', path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        else:
            result[relative] = ('dir',)
    return result


def scratch_leftovers():
    return sorted(name for name in os.listdir(tempfile.gettempdir()) if name.startswith(check_script.PRIVATE_PREFIX))


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


@unittest.skipUnless(PYTHON, 'python3 is not on the PATH')
class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(os.path.realpath(self.temp.name))
        self.work = self.base / 'work'
        self.work.mkdir()
        (self.work / 'notes.txt').write_text('hello notes\n')
        (self.work / 'src').mkdir()
        (self.work / 'src' / 'app.py').write_text('VALUE = 42\n')
        self.before = tree(self.work)
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        self.registry = check_runner.Registry()
        self.registry.register(check_script.ScriptAdapter())
        self.leftovers = scratch_leftovers()

    def tearDown(self):
        self.assertEqual(scratch_leftovers(), self.leftovers, 'a scratch folder was left behind')

    def go(self, check, consent=True, limits=None, workspace=None):
        return run(check, workspace or self.work, limits, self.registry, consent=consent_for(check) if consent is True else consent)

    def assertWorkspaceUnchanged(self):
        self.assertEqual(tree(self.work), self.before)


class ConsentGateTests(ScriptCase):
    def popen_spy(self):
        return mock.patch.object(check_script.subprocess, 'Popen', side_effect=AssertionError('a process was started'))

    def test_no_consent_means_no_process(self):
        check = script_check('print(1)')
        for consent in (None, {}, {'checkHash': ''}, {'checkHash': 'f' * 64}, {'checkHash': 5}, 'yes', 1, [], {'consentId': 'x'}):
            with self.subTest(consent=consent), self.popen_spy() as popen:
                result = self.go(check, consent=consent)
                self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'consent_required'))
                popen.assert_not_called()

    def test_consent_for_other_script_bytes_is_not_consent(self):
        approved = script_check('print("approved")')
        changed = dict(approved, source=approved['source'] + ' ')
        with self.popen_spy() as popen:
            result = run(changed, self.work, None, self.registry, consent=consent_for(approved))
        self.assertEqual(result['evidence']['reason'], 'consent_required')
        popen.assert_not_called()
        for field, value in (('runtime', 'node'), ('args', ['x']), ('timeoutSeconds', 5), ('runner', {'type': 'script', 'extra': 'x'})):
            with self.popen_spy() as popen:
                result = run(dict(approved, **{field: value}), self.work, None, self.registry, consent=consent_for(approved))
            self.assertIn(result['evidence']['reason'], ('consent_required', 'invalid_descriptor'), field)
            popen.assert_not_called()

    def test_the_default_registry_has_the_script_adapter_and_it_still_asks_for_consent(self):
        self.assertIn('script', check_runner.default_registry().types())
        with self.popen_spy() as popen:
            result = run(script_check('print(1)'), self.work)
        self.assertEqual(result['evidence']['reason'], 'consent_required')
        popen.assert_not_called()

    def test_a_bad_descriptor_never_reaches_a_process(self):
        good = script_check('print(1)')
        bad = [
            dict(good, runtime='ruby'), dict(good, runtime=None), {k: v for k, v in good.items() if k != 'runtime'},
            {k: v for k, v in good.items() if k != 'source'}, dict(good, source=''), dict(good, source=5), dict(good, source='a\x00b'),
            dict(good, source='x' * 65537), dict(good, source='€' * 22000), dict(good, source='\ud800'),
            dict(good, args=['a'] * 9), dict(good, args=[1]), dict(good, args='x'), dict(good, args=['a\x00']), dict(good, args=['x' * 201]), dict(good, args=['a\ud800']),
            dict(good, timeoutSeconds=0), dict(good, timeoutSeconds=121), dict(good, timeoutSeconds=True), dict(good, timeoutSeconds='5'), dict(good, timeoutSeconds=1.5),
            dict(good, runner={'type': 'script', 'extra': 'x'}),
        ]
        for check in bad:
            with self.subTest(check={k: (v if k != 'source' else str(v)[:20]) for k, v in check.items()}), self.popen_spy() as popen:
                result = self.go(check)
                self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'invalid_descriptor'))
                popen.assert_not_called()

    def test_a_missing_runtime_is_skipped_for_self_report(self):
        with mock.patch.object(check_script.shutil, 'which', return_value=None), self.popen_spy() as popen:
            result = self.go(script_check('print(1)'))
        self.assertEqual((result['status'], result['evidence']['reason']), ('skipped', 'runtime_unavailable'))
        self.assertIn('self-report', ' '.join(result['log']).lower())
        popen.assert_not_called()


class RunTests(ScriptCase):
    def test_exit_zero_passes_and_the_evidence_is_complete(self):
        result = self.go(script_check('print("all good")'))
        self.assertEqual(result['status'], 'passed')
        evidence = result['evidence']
        self.assertEqual(evidence['exitCode'], 0)
        self.assertEqual(evidence['output'], 'all good')
        self.assertEqual(evidence['runtime'], 'python3')
        self.assertIsInstance(evidence['durationMs'], int)
        self.assertIs(evidence['timedOut'], False)
        self.assertIs(evidence['outputTruncated'], False)
        self.assertIsInstance(evidence['limits'], list)
        self.assertTrue(evidence['limits'])
        self.assertIn('Memory', ' '.join(result['log']))
        self.assertWorkspaceUnchanged()

    def test_a_nonzero_exit_is_a_failed_check(self):
        result = self.go(script_check('import sys\nprint("nope", file=sys.stderr)\nsys.exit(3)'))
        self.assertEqual((result['status'], result['evidence']['exitCode']), ('failed', 3))
        self.assertIn('nope', result['evidence']['output'])

    def test_a_script_that_crashes_is_a_failed_check(self):
        result = self.go(script_check('raise SystemExit("bad")'))
        self.assertEqual(result['status'], 'failed')
        result = self.go(script_check('import os\nos.kill(os.getpid(), 9)'))
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['evidence']['reason'], 'script_killed')

    def test_arguments_reach_the_script_as_separate_arguments(self):
        result = self.go(script_check('import sys\nprint(sys.argv[1:])', args=['--flag', 'a b', '$(touch pwned)', ';echo hi', 'é\U0001f600']))
        self.assertEqual(result['status'], 'passed')
        self.assertIn("['--flag', 'a b', '$(touch pwned)', ';echo hi'", result['evidence']['output'])
        self.assertNotIn('pwned', os.listdir(self.work))
        self.assertWorkspaceUnchanged()

    def test_the_command_is_the_runtime_then_the_script_then_the_arguments_with_no_shell(self):
        seen = []
        real = check_script.subprocess.Popen

        def spy(command, *args, **kwargs):
            seen.append((command, kwargs))
            return real(command, *args, **kwargs)

        with mock.patch.object(check_script.subprocess, 'Popen', spy):
            self.go(script_check('print(1)', args=['x', 'y']))
        command, kwargs = [item for item in seen if item[0][0] != '/bin/ps'][-1]
        self.assertIsInstance(command, list)
        self.assertIs(kwargs.get('shell', False), False)
        self.assertEqual(command[-3:][1:], ['x', 'y'])
        self.assertTrue(command[-3].endswith('.py'))
        self.assertEqual(os.path.basename(command[-4]), os.path.basename(PYTHON))
        self.assertIs(kwargs['stdin'], check_script.subprocess.DEVNULL)
        self.assertIs(kwargs['stderr'], check_script.subprocess.STDOUT)
        self.assertTrue(kwargs['start_new_session'])

    def test_the_script_runs_in_a_scratch_copy_and_cannot_change_the_real_workspace_by_a_relative_path(self):
        source = '''
            import os
            print(sorted(os.listdir(".")))
            open("notes.txt", "w").write("changed")
            os.remove("src/app.py")
            open("created.txt", "w").write("new")
            os.makedirs("deep/er", exist_ok=True)
            open("deep/er/x", "w").write("x")
            print(open("notes.txt").read())
        '''
        result = self.go(script_check(source))
        self.assertEqual(result['status'], 'passed')
        self.assertIn("['notes.txt', 'schola_check_script.py', 'src']", result['evidence']['output'])
        self.assertWorkspaceUnchanged()
        self.assertFalse((self.work / 'created.txt').exists())

    def test_the_script_sees_a_copy_of_the_work_and_its_modules_import(self):
        source = '''
            import sys
            sys.path.insert(0, "src")
            import app
            print(app.VALUE, open("notes.txt").read().strip())
        '''
        result = self.go(script_check(source))
        self.assertEqual((result['status'], result['evidence']['output']), ('passed', '42 hello notes'))

    def test_known_limit_a_script_can_still_write_by_an_absolute_path(self):
        """Consent is not isolation. This test records the limit. If a sandbox adapter ever lands, change it."""
        target = self.outside / 'written-by-script.txt'
        result = self.go(script_check(f'open({str(target)!r}, "w").write("still possible")'))
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(target.exists())
        self.assertIn('not a sandbox', ' '.join(result['log']).lower())

    def test_known_limit_a_script_can_read_files_outside_the_workspace(self):
        (self.outside / 'private.txt').write_text('private data')
        result = self.go(script_check(f'print(open({str(self.outside / "private.txt")!r}).read())'))
        self.assertEqual(result['evidence']['output'], 'private data')

    def test_the_environment_is_clean_and_holds_no_secret(self):
        source = '''
            import json, os
            print(json.dumps(dict(os.environ)))
            print("cwd=" + os.getcwd())
            print("stdin=" + repr(__import__("sys").stdin.read()))
        '''
        with mock.patch.dict(os.environ, {'SCHOLA_SECRET': SECRET, 'AWS_SECRET_ACCESS_KEY': SECRET, 'GITHUB_TOKEN': SECRET, 'PYTHONPATH': '/elsewhere',
                                            'PYTHONSTARTUP': '/elsewhere/x.py', 'NODE_OPTIONS': '--require /elsewhere/x.js', 'LD_PRELOAD': '/elsewhere/x.so', 'DYLD_INSERT_LIBRARIES': '/x'}):
            result = self.go(script_check(source), limits=Limits(max_evidence_chars=5000))
        output = result['evidence']['output']
        self.assertNotIn(SECRET, json.dumps(result))
        environment = json.loads(output.split(' | ')[0])
        self.assertLessEqual(set(environment), {'PATH', 'LANG', 'HOME', '__CF_USER_TEXT_ENCODING', 'LC_CTYPE'})
        self.assertEqual(environment['LANG'], 'C.UTF-8')
        self.assertTrue(environment['PATH'].startswith('/'))
        self.assertTrue(environment['HOME'].startswith(tempfile.gettempdir()) or os.path.realpath(environment['HOME']).startswith(os.path.realpath(tempfile.gettempdir())))
        self.assertNotEqual(os.path.realpath(environment['HOME']), str(self.work))
        self.assertIn("stdin=''", output)
        self.assertIn('cwd=', output)
        self.assertNotIn(str(self.work), output)

    def test_unicode_and_odd_output_is_cleaned(self):
        source = '''
            import sys
            sys.stdout.buffer.write("caf\\u00e9 \\U0001f600 \\x1b[31mred\\x1b[0m \\u202e".encode("utf-8") + b" \\xff\\xfe \\x00 end")
        '''
        result = self.go(script_check(source))
        text = result['evidence']['output']
        for bad in ('\x00', '\x1b', '‮'):
            self.assertNotIn(bad, text)
        self.assertIn('café', text)
        self.assertIn('end', text)

    def test_source_with_unicode_and_windows_line_endings_runs(self):
        check = script_check('x = "café \U0001f600"\r\nprint(x)\r\n')
        check['source'] = 'x = "café \U0001f600"\r\nprint(x)\r\n'
        result = self.go(check)
        self.assertEqual((result['status'], result['evidence']['output']), ('passed', 'café \U0001f600'))

    @unittest.skipUnless(NODE, 'node is not on the PATH')
    def test_a_node_script_runs(self):
        # The runtime is looked up only in the standard program folders. A node from a version manager lives elsewhere,
        # so this test adds its folder to the lookup path.
        safe = os.path.dirname(NODE) + ':' + check_script.SAFE_PATH
        check = script_check('console.log("from node", process.argv.slice(2).join(","), require("fs").readFileSync("notes.txt", "utf8").trim())', runtime='node', args=['a', 'b'])
        with mock.patch.object(check_script, 'SAFE_PATH', safe):
            result = self.go(check)
        self.assertEqual((result['status'], result['evidence']['output']), ('passed', 'from node a,b hello notes'))
        self.assertWorkspaceUnchanged()

    @unittest.skipUnless(NODE, 'node is not on the PATH')
    def test_a_runtime_outside_the_standard_folders_is_skipped_for_self_report(self):
        if shutil.which('node', path=check_script.SAFE_PATH):
            self.skipTest('node is in a standard folder here')
        result = self.go(script_check('console.log(1)', runtime='node'))
        self.assertEqual((result['status'], result['evidence']['reason']), ('skipped', 'runtime_unavailable'))

    def test_the_scratch_folder_is_removed_after_success_failure_and_timeout(self):
        self.go(script_check('print(1)'))
        self.go(script_check('import sys\nsys.exit(1)'))
        self.go(script_check('import time\ntime.sleep(30)', timeout=1))
        self.go(script_check('import os, stat\nos.makedirs("locked/inner")\nopen("locked/inner/f", "w").write("x")\nos.chmod("locked/inner", 0o500)\nos.chmod("locked", 0o500)'))

    def test_the_scratch_folder_is_never_removed_outside_the_temp_root_or_without_its_prefix(self):
        victim = self.base / 'schola-script-victim'
        victim.mkdir()
        (victim / 'keep.txt').write_text('keep')
        with mock.patch.object(check_script.tempfile, 'mkdtemp', return_value=str(victim)):
            self.go(script_check('print(1)'))
        self.assertTrue((victim / 'keep.txt').exists())
        near = Path(tempfile.gettempdir()) / 'not-ours-victim'
        near.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, near, True)
        (near / 'keep.txt').write_text('keep')
        with mock.patch.object(check_script.tempfile, 'mkdtemp', return_value=str(near)):
            self.go(script_check('print(1)'))
        self.assertTrue((near / 'keep.txt').exists())


class CopyTests(ScriptCase):
    def popen_spy(self):
        return mock.patch.object(check_script.subprocess, 'Popen', side_effect=AssertionError('a process was started'))

    def test_git_and_node_modules_are_not_copied(self):
        for folder in ('.git', 'node_modules', 'src/node_modules'):
            (self.work / folder).mkdir(parents=True, exist_ok=True)
            (self.work / folder / 'file.txt').write_text('x')
        self.before = tree(self.work)
        result = self.go(script_check('import os\nprint(sorted(os.listdir(".")), os.path.exists("src/node_modules"))'))
        self.assertEqual(result['status'], 'passed')
        self.assertNotIn('.git', result['evidence']['output'])
        self.assertNotIn('node_modules', result['evidence']['output'].split('|')[0].replace('src', ''))
        self.assertTrue(result['evidence']['output'].endswith('False'))
        self.assertWorkspaceUnchanged()

    def test_a_workspace_over_the_size_cap_is_refused_before_any_process(self):
        (self.work / 'big.bin').write_bytes(b'x' * 1_000_000)
        with self.popen_spy() as popen:
            result = self.go(script_check('print(1)'), limits=Limits(script_copy_bytes=500_000))
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'workspace_too_large'))
        popen.assert_not_called()

    def test_the_default_cap_is_20_megabytes_and_2000_files(self):
        self.assertEqual(Limits().script_copy_bytes, 20_000_000)
        self.assertEqual(Limits().script_copy_files, 2000)

    def test_a_workspace_with_too_many_files_is_refused(self):
        for index in range(30):
            (self.work / f'f{index}.txt').write_text('x')
        with self.popen_spy() as popen:
            result = self.go(script_check('print(1)'), limits=Limits(script_copy_files=20))
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'workspace_too_large'))
        popen.assert_not_called()

    def test_files_in_the_skipped_folders_do_not_count(self):
        (self.work / 'node_modules').mkdir()
        for index in range(50):
            (self.work / 'node_modules' / f'f{index}.txt').write_text('x' * 100000)
        self.assertEqual(self.go(script_check('print(1)'), limits=Limits(script_copy_files=20, script_copy_bytes=100_000)).get('status'), 'passed')

    def test_a_link_that_leaves_the_workspace_refuses_the_run(self):
        (self.outside / 'secret.txt').write_text('secret')
        for name, make in (('file-link', lambda: os.symlink(self.outside / 'secret.txt', self.work / 'link.txt')),
                           ('dir-link', lambda: os.symlink(self.outside, self.work / 'linkdir')),
                           ('relative-climb', lambda: os.symlink('../outside/secret.txt', self.work / 'climb.txt')),
                           ('dangling-outside', lambda: os.symlink(self.outside / 'nothing', self.work / 'dangling'))):
            make()
            self.before = tree(self.work)
            with self.subTest(name=name), self.popen_spy() as popen:
                result = self.go(script_check('print(1)'))
                self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unsafe_workspace'))
                popen.assert_not_called()
            for entry in list(self.work.iterdir()):
                if entry.is_symlink():
                    entry.unlink()

    def test_a_link_inside_the_workspace_is_copied_as_a_file_and_a_linked_folder_is_skipped(self):
        os.symlink('notes.txt', self.work / 'alias.txt')
        os.symlink('src', self.work / 'srclink')
        self.before = tree(self.work)
        result = self.go(script_check('import os\nprint(sorted(os.listdir(".")), os.path.islink("alias.txt"), open("alias.txt").read().strip())'))
        self.assertEqual(result['status'], 'passed')
        self.assertIn('alias.txt', result['evidence']['output'])
        self.assertNotIn('srclink', result['evidence']['output'])
        self.assertTrue(result['evidence']['output'].endswith('False hello notes'))
        self.assertWorkspaceUnchanged()

    def test_special_files_are_skipped_and_never_opened(self):
        os.mkfifo(self.work / 'pipe')
        self.before = tree(self.work)
        signal.alarm(20)
        try:
            result = self.go(script_check('import os\nprint(sorted(os.listdir(".")))'))
        finally:
            signal.alarm(0)
        self.assertEqual(result['status'], 'passed')
        self.assertNotIn("'pipe'", result['evidence']['output'])

    def test_empty_folders_unusual_names_and_executable_bits_are_kept_where_safe(self):
        (self.work / 'empty').mkdir()
        (self.work / 'run.sh').write_text('#!/bin/sh\necho hi\n')
        (self.work / 'run.sh').chmod(0o755)
        (self.work / 'café \U0001f600.txt').write_text('x')
        (self.work / 'new\nline.txt').write_text('x')
        self.before = tree(self.work)
        source = 'import os\nprint(os.path.isdir("empty"), os.access("run.sh", os.X_OK), os.path.exists("caf\\u00e9 \\U0001f600.txt"), [n for n in os.listdir(".") if "line" in n])'
        result = self.go(script_check(source))
        self.assertEqual((result['status'], result['evidence']['output']), ('passed', 'True True True []'))
        self.assertWorkspaceUnchanged()


class LimitTests(ScriptCase):
    def test_a_script_that_runs_too_long_is_killed_with_its_children(self):
        pid_file = self.outside / 'child.pid'
        source = f'''
            import subprocess, time
            child = subprocess.Popen(["sleep", "60"])
            open({str(pid_file)!r}, "w").write(str(child.pid))
            time.sleep(60)
        '''
        started = time.monotonic()
        result = self.go(script_check(source, timeout=1))
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'script_timeout'))
        self.assertIs(result['evidence']['timedOut'], True)
        self.assertTrue(wait_dead(int(pid_file.read_text())), 'the child of the script is still running')

    def test_a_background_child_that_outlives_a_finished_script_is_killed(self):
        pid_file = self.outside / 'child.pid'
        source = f'''
            import subprocess
            child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            open({str(pid_file)!r}, "w").write(str(child.pid))
            print("parent done")
        '''
        result = self.go(script_check(source))
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(wait_dead(int(pid_file.read_text())), 'a background child survived the run')

    def test_a_flood_of_output_is_cut_off_and_the_script_is_killed(self):
        pid_file = self.outside / 'flood.pid'
        source = f'''
            import os, sys
            open({str(pid_file)!r}, "w").write(str(os.getpid()))
            while True:
                sys.stdout.write("x" * 1000)
                sys.stdout.flush()
        '''
        started = time.monotonic()
        result = self.go(script_check(source))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'output_too_large'))
        self.assertIs(result['evidence']['outputTruncated'], True)
        self.assertLessEqual(result['evidence']['outputBytes'], 65536 + 65536)
        self.assertTrue(wait_dead(int(pid_file.read_text())))

    def test_a_flood_on_the_error_stream_is_cut_off_too(self):
        source = 'import sys\nwhile True:\n    sys.stderr.write("e" * 1000)\n    sys.stderr.flush()'
        result = self.go(script_check(source))
        self.assertEqual(result['evidence']['reason'], 'output_too_large')

    def test_output_just_under_the_cap_is_kept(self):
        result = self.go(script_check('import sys\nsys.stdout.write("y" * 65536)'))
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['evidence']['outputBytes'], 65536)
        self.assertNotIn('reason', result['evidence'])

    def test_the_limits_that_were_set_are_listed_and_the_ones_that_were_not_are_never_silent(self):
        result = self.go(script_check('print(1)', timeout=7))
        evidence, log = result['evidence'], ' '.join(result['log'])
        self.assertTrue(any(item.startswith('timeout=7') for item in evidence['limits']))
        self.assertTrue(any(item.startswith('output=') for item in evidence['limits']))
        if check_script.resource is not None:
            for name in ('cpu', 'fsize', 'nofile', 'core'):
                self.assertTrue(any(item.startswith(name + '=') for item in evidence['limits']), (name, evidence['limits']))
        self.assertIn('Memory', log)
        self.assertTrue(evidence['limitsNotApplied'])
        self.assertTrue(any(item.startswith('memory') for item in evidence['limitsNotApplied']))

    def test_a_limit_the_system_cannot_set_is_reported_in_the_result(self):
        plan = check_script.LIMIT_PLAN + (('bogus', 'RLIMIT_DOES_NOT_EXIST', lambda ctx: 5),)
        with mock.patch.object(check_script, 'LIMIT_PLAN', plan):
            result = self.go(script_check('print(1)'))
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(any(item.startswith('bogus') for item in result['evidence']['limitsNotApplied']), result['evidence'])
        self.assertIn('bogus', ' '.join(result['log']))

    def test_a_system_with_no_resource_module_runs_without_limits_and_says_so(self):
        with mock.patch.object(check_script, 'resource', None):
            result = self.go(script_check('print("no limits here")'))
        self.assertEqual(result['status'], 'passed')
        self.assertIn('resource', ' '.join(result['log']).lower())
        self.assertTrue(any(item.startswith('cpu') for item in result['evidence']['limitsNotApplied']))

    @unittest.skipIf(check_script.resource is None, 'this system has no resource module')
    def test_the_open_file_limit_applies(self):
        source = '''
            import os
            opened = 0
            try:
                while opened < 2000:
                    os.open("/dev/null", os.O_RDONLY)
                    opened += 1
            except OSError:
                pass
            print(opened)
        '''
        result = self.go(script_check(source))
        self.assertEqual(result['status'], 'passed')
        self.assertLess(int(result['evidence']['output']), 300)

    @unittest.skipIf(check_script.resource is None, 'this system has no resource module')
    def test_the_file_size_limit_applies(self):
        source = '''
            try:
                with open("big.bin", "wb") as handle:
                    handle.write(b"x" * (64 * 1024 * 1024))
                    handle.flush()
                print("written")
            except OSError as error:
                print("blocked", error.errno)
        '''
        result = self.go(script_check(source))
        self.assertEqual(result['evidence']['output'][:7], 'blocked')

    @unittest.skipIf(check_script.resource is None, 'this system has no resource module')
    def test_a_script_that_burns_cpu_is_stopped(self):
        started = time.monotonic()
        result = self.go(script_check('while True:\n    pass', timeout=1))
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(result['status'], 'error')
        self.assertIn(result['evidence']['reason'], ('script_timeout', 'script_killed'))

    @unittest.skipIf(check_script.resource is None, 'this system has no resource module')
    def test_the_process_limit_stops_a_runaway_fork_loop(self):
        """A bounded loop, not a real fork bomb: at most 400 children that each sleep a few seconds."""
        source = '''
            import os, time
            pids = []
            try:
                while len(pids) < 400:
                    pid = os.fork()
                    if pid == 0:
                        time.sleep(4)
                        os._exit(0)
                    pids.append(pid)
            except OSError:
                pass
            print(len(pids))
            for pid in pids:
                os.waitpid(pid, 0)
        '''
        result = self.go(script_check(source, timeout=20))
        self.assertTrue(any(item.startswith('nproc=') for item in result['evidence']['limits']), result['evidence'])
        self.assertEqual(result['status'], 'passed', result)
        self.assertLess(int(result['evidence']['output']), 400)

    def test_a_second_run_while_one_is_going_is_busy_and_starts_no_process(self):
        started = threading.Event()
        real = check_script.subprocess.Popen
        calls = []

        def spy(command, *args, **kwargs):
            if command[0] != '/bin/ps':
                calls.append(1)
                started.set()
            return real(command, *args, **kwargs)

        slow = script_check('import time\ntime.sleep(2)\nprint("slow done")')
        first = {}
        with mock.patch.object(check_script.subprocess, 'Popen', spy):
            thread = threading.Thread(target=lambda: first.update(self.go(slow)))
            thread.start()
            self.assertTrue(started.wait(5))
            second = self.go(script_check('print("second")', check_id='script-b'))
            thread.join(15)
        self.assertEqual((second['status'], second['evidence']['reason']), ('error', 'busy'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(first['status'], 'passed')
        self.assertEqual(self.go(script_check('print("third")'))['status'], 'passed')

    def test_the_run_lock_is_released_after_a_fault(self):
        with mock.patch.object(check_script.subprocess, 'Popen', side_effect=OSError('no')):
            result = self.go(script_check('print(1)'))
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.go(script_check('print("again")'))['status'], 'passed')


class ReviewFixTests(ScriptCase):
    def popen_spy(self):
        return mock.patch.object(check_script.subprocess, 'Popen', side_effect=AssertionError('a process was started'))

    def test_a_version_1_0_script_runner_with_no_fields_is_skipped_and_needs_no_consent(self):
        legacy = {'id': 'old', 'kind': 'automated', 'description': 'A 1.0 script runner.', 'runner': {'type': 'script'}}
        self.assertFalse(check_consent.needs_consent(legacy))
        with self.popen_spy() as popen:
            result = run(legacy, self.work, None, self.registry, consent=None)
        self.assertEqual((result['status'], result['evidence']['reason']), ('skipped', 'script_not_defined'))
        self.assertIn('self-report', ' '.join(result['log']).lower())
        popen.assert_not_called()
        self.assertTrue(check_consent.needs_consent(dict(legacy, runtime='python3')))
        self.assertTrue(check_consent.needs_consent(dict(legacy, source='print(1)')))

    def test_a_whole_number_float_for_the_time_limit_is_the_same_as_the_integer(self):
        as_float, as_int = script_check('print("ok")', timeout=30.0), script_check('print("ok")', timeout=30)
        self.assertEqual(check_consent.check_hash(as_float), check_consent.check_hash(as_int))
        self.assertEqual(self.go(as_float)['status'], 'passed')
        for bad in (30.5, float('nan'), float('inf'), 0.0, 121.0, True):
            with self.subTest(bad=bad), self.popen_spy():
                self.assertEqual(self.go(script_check('print(1)', timeout=bad))['evidence']['reason'], 'invalid_descriptor')

    def test_a_child_in_its_own_session_that_holds_the_output_is_flagged(self):
        pid_file = self.outside / 'escaped.pid'
        source = f"""
            import subprocess
            child = subprocess.Popen(["sleep", "30"], start_new_session=True)
            open({str(pid_file)!r}, "w").write(str(child.pid))
            print("done")
        """
        try:
            result = self.go(script_check(source, timeout=10))
            self.assertEqual(result['evidence']['outputIncomplete'], True)
            self.assertTrue(any(item.startswith('process group') for item in result['evidence']['limitsNotApplied']), result['evidence']['limitsNotApplied'])
            self.assertIn('own session', ' '.join(result['log']))
        finally:
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if pid_file.exists() and alive(int(pid_file.read_text())):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

    def test_a_normal_run_is_not_flagged_incomplete(self):
        result = self.go(script_check('print(1)'))
        self.assertIs(result['evidence']['outputIncomplete'], False)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can read everything')
    def test_a_folder_or_file_that_cannot_be_read_refuses_the_run(self):
        (self.work / 'locked').mkdir()
        (self.work / 'locked' / 'secret.txt').write_text('x')
        os.chmod(self.work / 'locked', 0)
        self.addCleanup(os.chmod, self.work / 'locked', 0o700)
        with self.popen_spy() as popen:
            result = self.go(script_check('print(1)'))
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unreadable_workspace'))
        popen.assert_not_called()
        os.chmod(self.work / 'locked', 0o700)
        (self.work / 'locked' / 'secret.txt').chmod(0)
        self.addCleanup(os.chmod, self.work / 'locked' / 'secret.txt', 0o600)
        with self.popen_spy() as popen:
            result = self.go(script_check('print(1)'))
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unreadable_workspace'))
        popen.assert_not_called()

    def test_a_runtime_that_cannot_start_is_an_error_and_not_a_failed_check(self):
        with mock.patch.object(check_script.shutil, 'which', return_value='/nonexistent/python3'):
            result = self.go(script_check('print(1)'))
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'script_not_started'))
        self.assertNotEqual(result['status'], 'failed')

    def test_a_launcher_that_dies_before_it_reports_is_an_error(self):
        with mock.patch.object(check_script, '_LAUNCHER', 'import sys\nsys.exit(3)'):
            result = self.go(script_check('print(1)'))
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'script_not_started'))

    def test_exit_code_127_gets_a_hint(self):
        result = self.go(script_check('import sys\nsys.exit(127)'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('runtime could not start', ' '.join(result['log']))

    def test_the_runtime_is_looked_up_on_the_safe_path_only(self):
        seen = []
        real = check_script.shutil.which

        def spy(name, *args, **kwargs):
            seen.append(kwargs.get('path'))
            return real(name, *args, **kwargs)

        with mock.patch.object(check_script.shutil, 'which', spy):
            self.go(script_check('print(1)'))
        self.assertEqual(seen, [check_script.SAFE_PATH])

    def test_cleanup_never_changes_the_mode_of_a_target_outside_the_scratch_folder(self):
        folder, file = self.outside / 'target-folder', self.outside / 'target-file.txt'
        folder.mkdir(mode=0o755)
        os.chmod(folder, 0o755)
        file.write_text('x')
        os.chmod(file, 0o644)
        source = f"import os\nos.symlink({str(folder)!r}, 'link-to-folder')\nos.symlink({str(file)!r}, 'link-to-file')\nos.makedirs('own/inner')\nos.chmod('own/inner', 0o500)"
        result = self.go(script_check(source))
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o644)

    def test_a_scratch_folder_that_cannot_be_removed_is_noted(self):
        with mock.patch.object(check_script.shutil, 'rmtree', lambda *a, **k: None):
            result = self.go(script_check('print(1)'))
        self.assertEqual(result['status'], 'passed')
        self.assertIn('could not remove', ' '.join(result['log']).lower())
        for name in scratch_leftovers():
            if name not in self.leftovers:
                shutil.rmtree(Path(tempfile.gettempdir()) / name, ignore_errors=True)

    def test_a_kill_that_fails_is_an_error_that_does_not_hang_and_frees_the_lock(self):
        pid_file = self.outside / 'stubborn.pid'
        source = f'import os, time\nopen({str(pid_file)!r}, "w").write(str(os.getpid()))\ntime.sleep(60)'
        started = time.monotonic()
        try:
            with mock.patch.object(check_runner, 'kill_group', return_value=False):
                result = self.go(script_check(source, timeout=1), limits=Limits(kill_wait_seconds=0.5))
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'kill_failed'))
        finally:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if pid_file.exists():
                try:
                    os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.assertEqual(self.go(script_check('print("again")'))['status'], 'passed')

    def test_output_cut_for_the_result_is_flagged(self):
        result = self.go(script_check('print("z" * 2000)'))
        self.assertEqual(len(result['evidence']['output']), 500)
        self.assertIs(result['evidence']['outputTruncated'], True)
        result = self.go(script_check('print("z" * 2000)'), limits=Limits(max_evidence_chars=100))
        self.assertEqual(len(result['evidence']['output']), 100)
        self.assertIs(result['evidence']['outputTruncated'], True)
        self.assertIs(self.go(script_check('print("short")'))['evidence']['outputTruncated'], False)

    def test_the_collector_snapshot_is_never_torn_while_the_reader_is_still_writing(self):
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, write_end)
        process = mock.Mock(stdout=os.fdopen(read_end, 'rb', buffering=0))
        collector = check_script.Collector(process, 1000)
        thread = threading.Thread(target=collector.run, daemon=True)
        thread.start()
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                try:
                    os.write(write_end, b'x' * 37)
                except OSError:
                    return

        pump = threading.Thread(target=writer, daemon=True)
        pump.start()
        for _ in range(3000):
            data, total, overflow = collector.snapshot()
            self.assertEqual(len(data), min(total, 1000))
            self.assertEqual(overflow, total > 1000)
        stop.set()
        collector.stop()
        pump.join(2)

    def test_a_collector_that_is_stopped_adds_nothing_afterward(self):
        read_end, write_end = os.pipe()
        process = mock.Mock(stdout=os.fdopen(read_end, 'rb', buffering=0))
        collector = check_script.Collector(process, 100)
        thread = threading.Thread(target=collector.run, daemon=True)
        thread.start()
        os.write(write_end, b'abc')
        deadline = time.monotonic() + 3
        while collector.snapshot()[1] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        collector.stop()
        before = collector.snapshot()
        os.write(write_end, b'more')
        os.close(write_end)
        thread.join(3)
        self.assertEqual(collector.snapshot(), before)

    def test_the_lock_is_released_after_an_unhandled_exception_in_the_adapter(self):
        with mock.patch.object(check_script.ScriptAdapter, '_run', side_effect=RuntimeError('/secret/path')):
            result = self.go(script_check('print(1)'))
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'adapter_failed'))
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(self.go(script_check('print("again")'))['status'], 'passed')

    def test_skipped_content_is_noted_in_the_log(self):
        os.mkfifo(self.work / 'pipe')
        (self.work / '.git').mkdir()
        os.symlink('src', self.work / 'srclink')
        result = self.go(script_check('print(1)'))
        log = ' '.join(result['log'])
        self.assertIn('.git or node_modules', log)
        self.assertIn('special files, linked folders', log)

    def test_the_file_count_cap_stops_the_copy_inside_the_loop(self):
        for index in range(60):
            (self.work / f'f{index}.txt').write_text('x')
        reads = []
        real = check_script.check_workspace.read_file

        def spy(*args, **kwargs):
            reads.append(args[1])
            return real(*args, **kwargs)

        with mock.patch.object(check_script.check_workspace, 'read_file', spy), self.popen_spy() as popen:
            result = self.go(script_check('print(1)'), limits=Limits(script_copy_files=10))
        self.assertEqual(result['evidence']['reason'], 'workspace_too_large')
        self.assertLessEqual(len(reads), 10)
        popen.assert_not_called()

    def test_a_missing_launcher_report_is_noted(self):
        with mock.patch.object(check_script, '_read_report', return_value=None):
            result = self.go(script_check('print(1)'))
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(any(item.startswith('resource limits') for item in result['evidence']['limitsNotApplied']))
        self.assertIn('resource limits', ' '.join(result['log']))

    def test_the_limits_that_do_not_apply_can_be_listed_before_a_run(self):
        planned, missing = check_script.describe_limits(Limits(), 20)
        self.assertTrue(any(item.startswith('timeout=20s') for item in planned))
        self.assertTrue(any(item.lower().startswith('memory') for item in missing))
        with mock.patch.object(check_script, 'resource', None):
            _, missing = check_script.describe_limits(Limits(), 20)
        self.assertTrue(any(item.startswith('cpu') for item in missing))
        with mock.patch.object(check_script.sys, 'platform', 'darwin'):
            _, missing = check_script.describe_limits(Limits(), 20)
        self.assertTrue(any('macOS' in item for item in missing))


class RouteTests(unittest.TestCase):
    """The whole path: import a version 1.1 lesson, inspect it, agree, and run it, over HTTP."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(os.path.realpath(self.temp.name))
        self.data, self.home = base / 'data', base / 'home'
        self.work = self.home / 'work'
        for folder in (self.data, self.home, self.work):
            folder.mkdir()
        (self.work / 'notes.txt').write_text('hello notes\n')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.data_dir = self.data
        self.server.check_session = check_routes.CheckSession(home=self.home)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join()))
        self.token = self.call('GET', '/api/checks/session', token=False)[1]['token']
        self.document = json.loads(MODULE_FIXTURE.read_text())
        self.document['checks'] = [self.document['checks'][2], dict(self.document['checks'][2], id='slow-script', source='import time\ntime.sleep(3)\nprint("slow")\n')]
        self.document['checks'][0]['id'] = 'script-a'
        self.document['checks'][0]['source'] = 'import sys\nprint("route ok", open("notes.txt").read().strip(), sys.argv[1:])\n'
        self.document['checks'][0]['args'] = ['--strict']
        self.document['checks'][0].pop('timeoutSeconds', None)
        status, payload = self.call('POST', '/api/lessons/import', {'source': json.dumps(self.document), 'filename': 'm.json'}, token=False)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.call('POST', '/api/checks/workspace', {'path': str(self.work)})[0], 200)

    def call(self, method, path, body=None, token=True):
        connection = HTTPConnection('127.0.0.1', self.server.server_port)
        port = self.server.server_port
        headers = {'Content-Type': 'application/json', 'Host': f'127.0.0.1:{port}', 'Origin': f'http://127.0.0.1:{port}'}
        if token:
            headers[check_routes.TOKEN_HEADER] = self.token
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read() or b'null')
        connection.close()
        return response.status, payload

    def ref(self, check_id='script-a', **extra):
        return {'moduleId': self.document['id'], 'version': self.document['version'], 'checkId': check_id, **extra}

    def agree(self, check_id='script-a'):
        status, shown = self.call('POST', '/api/checks/inspect', self.ref(check_id))
        self.assertEqual(status, 200, shown)
        status, granted = self.call('POST', '/api/checks/consent', self.ref(check_id, confirm=True, shownHash=shown['checkHash']))
        self.assertEqual(status, 200, granted)
        return shown, granted

    def test_inspect_agree_run(self):
        status, shown = self.call('POST', '/api/checks/inspect', self.ref())
        self.assertEqual((status, shown['needsConsent'], shown['runtime'], shown['args']), (200, True, 'python3', ['--strict']))
        self.assertIn('route ok', shown['source'])
        self.assertEqual(shown['timeoutSeconds'], 30)
        self.assertEqual(shown['workspace'], str(self.work))
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref())[1]['code'], 'consent_required')
        shown, granted = self.agree()
        status, payload = self.call('POST', '/api/checks/run', self.ref(consentId=granted['consentId']))
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload['tier'], payload['result']['status']), (2, 'passed'))
        self.assertEqual(payload['result']['evidence']['output'], "route ok hello notes ['--strict']")
        self.assertEqual(payload['checkHash'], shown['checkHash'])

    def test_a_new_version_with_changed_script_bytes_needs_its_own_consent(self):
        _, granted = self.agree()
        changed = json.loads(json.dumps(self.document))
        changed['version'] = '1.0.1'
        changed['checks'][0]['source'] += '# one more line\n'
        status, payload = self.call('POST', '/api/lessons/import', {'source': json.dumps(changed), 'filename': 'm2.json'}, token=False)
        self.assertEqual(status, 200, payload)
        body = {**self.ref(), 'version': '1.0.1', 'consentId': granted['consentId']}
        self.assertEqual(self.call('POST', '/api/checks/run', body)[1]['code'], 'consent_required')
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref(consentId=granted['consentId']))[0], 200)

    def test_a_damaged_stored_script_is_not_run(self):
        _, granted = self.agree()
        path = self.data / '_lessons' / 'modules' / self.document['id'] / self.document['version'] / 'module.json'
        data = path.read_bytes()
        path.write_bytes(data.replace(b'route ok', b'evil ok'))
        status, payload = self.call('POST', '/api/checks/run', self.ref(consentId=granted['consentId']))
        self.assertEqual((status, payload['code']), (500, 'corrupt_file'))

    def test_a_second_run_over_http_is_busy(self):
        _, granted = self.agree('slow-script')
        body = self.ref('slow-script', consentId=granted['consentId'])
        first = {}
        thread = threading.Thread(target=lambda: first.update(zip(('status', 'payload'), self.call('POST', '/api/checks/run', body))))
        thread.start()
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            time.sleep(0.2)
            status, payload = self.call('POST', '/api/checks/run', body)
            if status == 429:
                break
        thread.join(20)
        self.assertEqual((status, payload['code']), (429, 'busy'))
        self.assertEqual(first['status'], 200)

    def test_the_lesson_backup_carries_a_script_module_and_restores_it(self):
        status, backup = self.call('GET', '/api/lessons/export', token=False)
        self.assertEqual(status, 200)
        self.assertIn('route ok', json.dumps(backup))
        with tempfile.TemporaryDirectory() as other:
            from lesson_store import LessonStore
            import lesson_backup
            store = LessonStore(Path(other))
            lesson_backup.restore_backup(store, backup)
            restored = store.get_module(self.document['id'], self.document['version'])
        self.assertEqual(restored['checks'][0]['source'], self.document['checks'][0]['source'])
        self.assertEqual(restored['schemaVersion'], '1.1')


if __name__ == '__main__':
    unittest.main()
