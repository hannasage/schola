import io
import json
import os
import signal
import stat
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import gate_matrix  # noqa: E402

VERSION = "%d.%d" % sys.version_info[:2]  # the tests label the running interpreter with its real version

PASSING = "import unittest\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertTrue(True)\n    def test_two(self):\n        self.assertTrue(True)\n"
FAILING = "import unittest\n\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.assertEqual(1, 2)\n"
SLOW_WITH_PID = "import os, time, unittest\n\nclass T(unittest.TestCase):\n    def test_slow(self):\n        open(os.environ[\"GATE_PID_FILE\"], \"w\").write(str(os.getpid()))\n        time.sleep(60)\n"
EMPTY = "import unittest\n"
SKIPPING = "import unittest\n\nclass T(unittest.TestCase):\n    @unittest.skip(\"no\")\n    def test_a(self):\n        pass\n    def test_b(self):\n        pass\n"
SLOW = "import time, unittest\n\nclass T(unittest.TestCase):\n    def test_slow(self):\n        time.sleep(60)\n"


def make_project(tests_source):
    """A fake Schola checkout: a tests folder, one test, and a test_server.py."""
    root = Path(tempfile.mkdtemp(prefix="gate-fixture-"))
    (root / "tests").mkdir()
    (root / "tests" / "test_one.py").write_text(tests_source, encoding="utf-8")
    (root / "test_server.py").write_text(PASSING, encoding="utf-8")
    return root


class GateMatrixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gate-test-")  # logs, fixtures, and locks all live here
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(tempfile, "tempdir", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lock_dir = Path(tempfile.mkdtemp(prefix="gate-lock-"))
        self.addCleanup(shutil.rmtree, self.lock_dir, True)

    def project(self, source):
        root = make_project(source)
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def run_main(self, root, *extra):
        out = io.StringIO()
        argv = ["--root", str(root), "--lock-dir", str(self.lock_dir), "--json", *extra]
        code = gate_matrix.main(argv, out=out)
        data = json.loads(out.getvalue()) if out.getvalue().strip() else None
        if data and data.get("log_dir"):
            self.addCleanup(shutil.rmtree, data["log_dir"], True)
        return code, data

    def test_passing_suites_exit_zero_with_counts(self):
        root = self.project(PASSING)
        code, data = self.run_main(root, "--versions", VERSION, "--python", f"{VERSION}={sys.executable}")
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual([r["suite"] for r in data["results"]], ["unit", "server"])
        for result in data["results"]:
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["tests"], 2)
            self.assertTrue(Path(result["log"]).is_file())

    def test_failing_suite_exits_one_and_names_the_first_failure_only(self):
        root = self.project(FAILING)
        code, data = self.run_main(root, "--versions", VERSION, "--python", f"{VERSION}={sys.executable}")
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        unit = data["results"][0]
        self.assertEqual(unit["status"], "fail")
        self.assertTrue(unit["first_failure"].startswith("FAIL: test_bad"), unit["first_failure"])
        self.assertEqual(data["results"][1]["status"], "pass")  # the server suite still runs

    def test_timeout_kills_the_suite_and_fails(self):
        root = self.project(SLOW)
        started = time.monotonic()
        code, data = self.run_main(root, "--versions", VERSION, "--suites", "unit", "--timeout", "2",
                                   "--python", f"{VERSION}={sys.executable}")
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(code, 1)
        self.assertEqual(data["results"][0]["status"], "timeout")

    def test_missing_interpreter_fails_unless_allowed(self):
        root = self.project(PASSING)
        code, data = self.run_main(root, "--versions", "9.99")
        self.assertEqual(code, 1)
        self.assertEqual(data["results"][0]["status"], "missing")
        code, data = self.run_main(root, "--versions", f"9.99,{VERSION}", "--allow-missing",
                                   "--python", f"{VERSION}={sys.executable}")
        self.assertEqual(code, 0)
        self.assertEqual(data["results"][0]["status"], "skip")

    def test_wrong_use_exits_two(self):
        empty = Path(tempfile.mkdtemp(prefix="gate-empty-"))
        self.addCleanup(shutil.rmtree, empty, True)
        root = self.project(PASSING)
        for argv in (["--root", str(empty)],
                     ["--root", str(root), "--suites", "nope"],
                     ["--root", str(root), "--python", "no-equals-sign"],
                     ["--root", str(root), "--timeout", "0"]):
            err = io.StringIO()
            real = sys.stderr
            sys.stderr = err
            try:
                code = gate_matrix.main(argv + ["--lock-dir", str(self.lock_dir)], out=io.StringIO())
            finally:
                sys.stderr = real
            self.assertEqual(code, 2, argv)
            self.assertIn("gate_matrix:", err.getvalue())

    def test_list_runs_nothing_and_takes_no_lock(self):
        root = self.project(FAILING)
        code, data = self.run_main(root, "--list", "--versions", f"{VERSION},9.99", "--python", f"{VERSION}={sys.executable}")
        self.assertEqual(code, 0)
        self.assertEqual([p["python"] is None for p in data["plan"]], [False, False, True, True])
        self.assertNotIn("log_dir", data)
        self.assertFalse((self.lock_dir / gate_matrix.LOCK_NAME).exists())

    def test_text_output_prints_each_command_before_it_runs(self):
        root = self.project(PASSING)
        out = io.StringIO()
        code = gate_matrix.main(["--root", str(root), "--lock-dir", str(self.lock_dir), "--versions", VERSION,
                                 "--suites", "server", "--python", f"{VERSION}={sys.executable}"], out=out)
        self.addCleanup(shutil.rmtree, out.getvalue().strip().rsplit("logs in ", 1)[-1], True)
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("run: "), lines[0])
        self.assertIn("unittest test_server.py", lines[0])
        self.assertTrue(lines[-1].startswith("PASS: logs in "), lines[-1])

    def test_a_suite_that_runs_no_tests_fails_on_every_version(self):
        root = self.project(EMPTY)
        code, data = self.run_main(root, "--suites", "unit", "--versions", VERSION, "--python", f"{VERSION}={sys.executable}")
        self.assertEqual(code, 1)
        self.assertEqual(data["results"][0]["status"], "fail")
        self.assertEqual(data["results"][0]["first_failure"], "no tests ran")

    def test_skipped_tests_are_counted(self):
        root = self.project(SKIPPING)
        code, data = self.run_main(root, "--suites", "unit", "--versions", VERSION, "--python", f"{VERSION}={sys.executable}")
        self.assertEqual(code, 0)
        self.assertEqual(data["results"][0]["skipped"], 1)

    def test_an_interpreter_of_the_wrong_version_fails(self):
        root = self.project(PASSING)
        code, data = self.run_main(root, "--versions", "2.7", "--python", f"2.7={sys.executable}")
        self.assertEqual(code, 1)
        self.assertEqual(data["results"][0]["status"], "wrong_version")

    def test_a_run_where_everything_was_skipped_is_not_ok(self):
        root = self.project(PASSING)
        code, data = self.run_main(root, "--versions", "9.98,9.99", "--allow-missing")
        self.assertEqual(code, 0 if data["ok"] else 1)
        self.assertFalse(data["ok"])
        self.assertEqual(code, 1)

    def test_a_bad_lock_dir_exits_two(self):
        root = self.project(PASSING)
        err = io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            code = gate_matrix.main(["--root", str(root), "--lock-dir", "/no/such/folder", "--versions", VERSION,
                                     "--python", f"{VERSION}={sys.executable}"], out=io.StringIO())
        finally:
            sys.stderr = real
        self.assertEqual(code, 2)
        self.assertIn("cannot take the lock", err.getvalue())

    def test_a_held_lock_refuses_a_second_gate_and_names_the_owner(self):
        root = self.project(PASSING)
        first = gate_matrix.Lock(self.lock_dir)
        first.acquire()
        self.addCleanup(first.release)
        err = io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            code = gate_matrix.main(["--root", str(root), "--lock-dir", str(self.lock_dir), "--versions", VERSION,
                                     "--python", f"{VERSION}={sys.executable}"], out=io.StringIO())
        finally:
            sys.stderr = real
        self.assertEqual(code, 2)
        self.assertIn(f"another gate runs (pid {os.getpid()}", err.getvalue())

    def test_two_locks_cannot_both_be_held_and_release_frees_it(self):
        first, second = gate_matrix.Lock(self.lock_dir), gate_matrix.Lock(self.lock_dir)
        first.acquire()
        with self.assertRaises(gate_matrix.UsageError):
            second.acquire()
        self.assertFalse(second.held)
        first.release()
        second.acquire()
        self.assertTrue(second.held)
        second.release()

    def test_a_lock_file_left_by_a_dead_process_does_not_block(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (self.lock_dir / gate_matrix.LOCK_NAME).write_text(str(dead.pid), encoding="utf-8")
        root = self.project(PASSING)
        code, _data = self.run_main(root, "--versions", VERSION, "--python", f"{VERSION}={sys.executable}")
        self.assertEqual(code, 0)

    def test_the_lock_is_free_again_after_a_run(self):
        root = self.project(PASSING)
        self.run_main(root, "--versions", VERSION, "--python", f"{VERSION}={sys.executable}")
        lock = gate_matrix.Lock(self.lock_dir)
        lock.acquire()
        lock.release()

    def test_a_symlink_in_place_of_the_lock_file_is_refused(self):
        target = self.lock_dir / "elsewhere"
        target.write_text("keep", encoding="utf-8")
        (self.lock_dir / gate_matrix.LOCK_NAME).symlink_to(target)
        with self.assertRaises(gate_matrix.UsageError):
            gate_matrix.Lock(self.lock_dir).acquire()
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def stub_python(self, output, code=0):
        """An interpreter that reports VERSION and prints fixed test output. It stands in for Python 3.10 or 3.11."""
        path = self.lock_dir / "stub-python"
        path.write_text(f"#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then echo {VERSION}; exit 0; fi\ncat <<'EOF'\n{output}\nEOF\nexit {code}\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def test_zero_tests_with_exit_zero_fails_as_python_3_10_and_3_11_report_it(self):
        root = self.project(PASSING)
        stub = self.stub_python("\n----------------------------------------------------------------------\nRan 0 tests in 0.000s\n\nOK")
        code, data = self.run_main(root, "--suites", "unit", "--versions", VERSION, "--python", f"{VERSION}={stub}")
        self.assertEqual(code, 1)
        self.assertEqual(data["results"][0]["first_failure"], "no tests ran")

    def test_a_suite_where_every_test_is_skipped_fails(self):
        root = self.project(PASSING)
        stub = self.stub_python("Ran 3 tests in 0.001s\n\nOK (skipped=3)")
        code, data = self.run_main(root, "--suites", "unit", "--versions", VERSION, "--python", f"{VERSION}={stub}")
        self.assertEqual(code, 1)
        self.assertEqual(data["results"][0]["first_failure"], "all tests skipped or expected to fail")

    def test_a_child_a_test_leaves_behind_is_killed_when_the_suite_ends(self):
        pid_file = self.lock_dir / "child.pid"
        source = ("import os, subprocess, unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
                  "        child = subprocess.Popen(['sleep', '300'])\n"
                  "        open(os.environ['GATE_CHILD_PID'], 'w').write(str(child.pid))\n")
        root = self.project(source)
        os.environ["GATE_CHILD_PID"] = str(pid_file)
        self.addCleanup(os.environ.pop, "GATE_CHILD_PID", None)
        code, _data = self.run_main(root, "--suites", "unit", "--versions", VERSION, "--python", f"{VERSION}={sys.executable}")
        self.assertEqual(code, 0)
        child = int(pid_file.read_text())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(child, 9)
            self.fail("a child of the suite kept running")

    def test_a_fifo_in_place_of_the_lock_file_is_refused_as_not_regular(self):
        os.mkfifo(self.lock_dir / gate_matrix.LOCK_NAME)
        with self.assertRaises(gate_matrix.UsageError) as caught:
            gate_matrix.Lock(self.lock_dir).acquire()
        self.assertIn("not a regular file of this user", str(caught.exception))

    def test_a_lock_file_owned_by_another_user_is_refused(self):
        from types import SimpleNamespace
        fake = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.geteuid() + 1)
        with mock.patch.object(gate_matrix.os, "fstat", return_value=fake):
            with self.assertRaises(gate_matrix.UsageError) as caught:
                gate_matrix.Lock(self.lock_dir).acquire()
        self.assertIn("not a regular file of this user", str(caught.exception))

    def test_a_refused_acquire_closes_its_descriptor(self):
        first = gate_matrix.Lock(self.lock_dir)
        first.acquire()
        self.addCleanup(first.release)
        before = len(os.listdir("/dev/fd"))
        with self.assertRaises(gate_matrix.UsageError):
            gate_matrix.Lock(self.lock_dir).acquire()
        self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_main_restores_the_signal_handlers_it_replaced(self):
        root = self.project(PASSING)
        before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
        self.run_main(root, "--versions", VERSION, "--python", f"{VERSION}={sys.executable}")
        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)), before)

    def test_skip_counts_come_from_the_result_line_only(self):
        root = self.project(PASSING)
        cases = {
            "Ran 3 tests in 0.1s\n\nOK (skipped=3)": "fail",
            "a test said skipped=0 here\nRan 3 tests in 0.1s\n\nOK (skipped=3)": "fail",
            "Ran 3 tests in 0.1s\n\nOK (skipped=1, expected failures=2)": "fail",
            "a test said skipped=3 here\nRan 3 tests in 0.1s\n\nOK": "pass",
            "Ran 3 tests in 0.1s\n\nOK (skipped=1)": "pass",
        }
        for output, want in cases.items():
            with self.subTest(output=output):
                stub = self.stub_python(output)
                _code, data = self.run_main(root, "--suites", "unit", "--versions", VERSION, "--python", f"{VERSION}={stub}")
                self.assertEqual(data["results"][0]["status"], want)

    def test_a_longer_old_pid_in_the_lock_file_is_cut_off(self):
        (self.lock_dir / gate_matrix.LOCK_NAME).write_text("9" * 20, encoding="utf-8")
        lock = gate_matrix.Lock(self.lock_dir)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual((self.lock_dir / gate_matrix.LOCK_NAME).read_text(), str(os.getpid()))

    def test_an_interrupt_in_process_exits_130_and_frees_the_lock(self):
        root = self.project(PASSING)
        with mock.patch.object(gate_matrix, "run_matrix", side_effect=KeyboardInterrupt):
            err = io.StringIO()
            real = sys.stderr
            sys.stderr = err
            try:
                code = gate_matrix.main(["--root", str(root), "--lock-dir", str(self.lock_dir), "--versions", VERSION,
                                         "--python", f"{VERSION}={sys.executable}"], out=io.StringIO())
            finally:
                sys.stderr = real
        self.assertEqual(code, 130)
        lock = gate_matrix.Lock(self.lock_dir)
        lock.acquire()  # a lock that was not released would refuse
        lock.release()

    def test_a_signal_stops_the_suite_and_exits_130(self):
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum.name):
                self.signal_case(signum)

    def signal_case(self, signum):
        root = self.project(SLOW_WITH_PID)
        pid_file = self.lock_dir / "suite.pid"
        pid_file.unlink(missing_ok=True)  # a second case must wait for its own suite
        env = dict(os.environ, GATE_PID_FILE=str(pid_file), TMPDIR=self.tmp)
        proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "gate_matrix.py"), "--root", str(root),
                                 "--lock-dir", str(self.lock_dir), "--versions", VERSION, "--suites", "unit",
                                 "--python", f"{VERSION}={sys.executable}"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.1)
        suite_pid = int(pid_file.read_text())
        proc.send_signal(signum)
        _out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, err)
        for _ in range(50):
            try:
                os.kill(suite_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(suite_pid, 9)
            self.fail("the suite process kept running")

    def test_an_ignored_hangup_stays_ignored(self):
        root = self.project(SLOW_WITH_PID)
        pid_file = self.lock_dir / "hup.pid"
        env = dict(os.environ, GATE_PID_FILE=str(pid_file), TMPDIR=self.tmp)
        proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "gate_matrix.py"), "--root", str(root),
                                 "--lock-dir", str(self.lock_dir), "--versions", VERSION, "--suites", "unit",
                                 "--python", f"{VERSION}={sys.executable}"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN))
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.1)
        suite_pid = int(pid_file.read_text())
        proc.send_signal(signal.SIGHUP)
        time.sleep(1)
        self.assertIsNone(proc.poll(), "an ignored SIGHUP must not stop the gate")
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, err)
        try:
            os.kill(suite_pid, 9)
        except ProcessLookupError:
            pass

    def test_find_python_ignores_a_file_that_cannot_run(self):
        not_executable = self.lock_dir / "python3.10"
        not_executable.write_text("", encoding="utf-8")
        self.assertIsNone(gate_matrix.find_python(VERSION, {VERSION: str(not_executable)}))


if __name__ == "__main__":
    unittest.main()
