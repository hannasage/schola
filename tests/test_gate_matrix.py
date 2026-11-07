import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

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

    def test_a_live_lock_refuses_a_second_gate(self):
        root = self.project(PASSING)
        lock = self.lock_dir / gate_matrix.LOCK_NAME
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
        err = io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            code = gate_matrix.main(["--root", str(root), "--lock-dir", str(self.lock_dir), "--versions", VERSION,
                                     "--python", f"{VERSION}={sys.executable}"], out=io.StringIO())
        finally:
            sys.stderr = real
        self.assertEqual(code, 2)
        self.assertIn("another gate runs", err.getvalue())
        self.assertTrue(lock.is_dir(), "the live lock must stay")

    def test_a_stale_lock_is_replaced_and_the_lock_is_released(self):
        root = self.project(PASSING)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        lock = self.lock_dir / gate_matrix.LOCK_NAME
        lock.mkdir()
        (lock / "pid").write_text(str(dead.pid), encoding="utf-8")
        code, _data = self.run_main(root, "--versions", VERSION, "--python", f"{VERSION}={sys.executable}")
        self.assertEqual(code, 0)
        self.assertFalse(lock.exists(), "the lock must be released after the run")

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

    def test_a_new_lock_folder_without_a_pid_file_counts_as_live(self):
        lock = gate_matrix.Lock(self.lock_dir)
        (self.lock_dir / gate_matrix.LOCK_NAME).mkdir()
        with self.assertRaises(gate_matrix.UsageError):
            lock.acquire()

    def test_a_pid_of_zero_or_less_is_not_alive(self):
        self.assertFalse(gate_matrix.pid_alive(0))
        self.assertFalse(gate_matrix.pid_alive(-1))

    def test_release_does_not_remove_a_lock_another_process_owns(self):
        lock = gate_matrix.Lock(self.lock_dir)
        lock.acquire()
        (lock.path / "pid").write_text("1", encoding="utf-8")  # another owner took it
        lock.release()
        self.assertTrue(lock.path.is_dir())

    def test_a_signal_stops_the_suite_and_releases_the_lock(self):
        root = self.project(SLOW_WITH_PID)
        pid_file = self.lock_dir / "suite.pid"
        env = dict(os.environ, GATE_PID_FILE=str(pid_file))
        proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "gate_matrix.py"), "--root", str(root),
                                 "--lock-dir", str(self.lock_dir), "--versions", VERSION, "--suites", "unit",
                                 "--python", f"{VERSION}={sys.executable}"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.1)
        suite_pid = int(pid_file.read_text())
        proc.terminate()
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
        self.assertFalse((self.lock_dir / gate_matrix.LOCK_NAME).exists())

    def test_find_python_ignores_a_file_that_cannot_run(self):
        not_executable = self.lock_dir / "python3.10"
        not_executable.write_text("", encoding="utf-8")
        self.assertIsNone(gate_matrix.find_python(VERSION, {VERSION: str(not_executable)}))


if __name__ == "__main__":
    unittest.main()
