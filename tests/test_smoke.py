import http.client
import io
import subprocess
import json
import os
import shutil
import sys
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import smoke  # noqa: E402

FAKE_CLI = '''
import sys
args = sys.argv[1:]
if args[:2] == ["--data-dir", args[1]]:
    args = args[2:]
if args == ["version"]:
    print("schola 0.0.0")
elif args[:1] == ["modules"] or args[:2] == ["events", "list"]:
    print("{}")
elif args[:2] == ["events", "export"]:
    print("schola: There is no event log. (code: log_missing)", file=sys.stderr)
    sys.exit(1)
else:
    sys.exit(3)
'''

FAKE_SERVER = '''
import argparse, http.server, json, sys
parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int)
parser.add_argument("--data-dir")
args = parser.parse_args()
HUB_STATUS = %(hub)d

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        status = {"/": 200, "/api/state": 200, "/api/library": 200, "/api/hub": HUB_STATUS}.get(path, 404)
        body = json.dumps({"ok": True}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
import os
if os.environ.get("SMOKE_CHILD_PID_FILE"):
    import subprocess
    child = subprocess.Popen(["sleep", "300"])
    open(os.environ["SMOKE_CHILD_PID_FILE"], "w").write(str(child.pid))
if os.environ.get("SMOKE_PID_FILE"):
    open(os.environ["SMOKE_PID_FILE"], "w").write(str(os.getpid()))
print("Schola is ready at http://127.0.0.1:%%d" %% server.server_port, flush=True)
server.serve_forever()
'''


def fake_root(hub_status=403, server_source=None):
    root = Path(tempfile.mkdtemp(prefix="smoke-fixture-"))
    (root / "schola_cli.py").write_text(FAKE_CLI, encoding="utf-8")
    (root / "server.py").write_text(server_source or FAKE_SERVER % {"hub": hub_status}, encoding="utf-8")
    return root


class SmokeTest(unittest.TestCase):
    def run_main(self, *argv):
        out = io.StringIO()
        code = smoke.main([*argv, "--json"], out=out)
        return code, json.loads(out.getvalue())

    def fixture(self, **kwargs):
        root = fake_root(**kwargs)
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def test_a_good_checkout_passes_every_step(self):
        code, data = self.run_main("--root", str(self.fixture()))
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        names = [s["step"] for s in data["steps"]]
        self.assertEqual(len(names), 10)
        self.assertIn("GET /api/hub without token", names)

    def test_an_open_hub_route_is_a_failure(self):
        code, data = self.run_main("--root", str(self.fixture(hub_status=200)))
        self.assertEqual(code, 1)
        failed = [s for s in data["steps"] if not s["ok"]]
        self.assertEqual([s["step"] for s in failed], ["GET /api/hub without token"])
        self.assertIn("status 200, expected 403", failed[0]["detail"])

    def test_a_server_that_never_says_ready_fails_the_start_step_and_is_stopped(self):
        pid_file = Path(tempfile.mkdtemp(prefix="smoke-pid-")) / "pid"
        self.addCleanup(shutil.rmtree, pid_file.parent, True)
        os.environ["SMOKE_PID_FILE"] = str(pid_file)
        self.addCleanup(os.environ.pop, "SMOKE_PID_FILE", None)
        root = self.fixture(server_source="import os, time\nopen(os.environ['SMOKE_PID_FILE'], 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
        code, data = self.run_main("--root", str(root), "--wait", "1")
        self.assertEqual(code, 1)
        self.assertEqual(data["steps"][-1]["step"], "server start")
        self.assertFalse(data["steps"][-1]["ok"])
        self.assertTrue(self.pid_gone(int(pid_file.read_text())))

    def test_a_server_that_ignores_sigterm_is_killed(self):
        pid_file = Path(tempfile.mkdtemp(prefix="smoke-pid-")) / "pid"
        self.addCleanup(shutil.rmtree, pid_file.parent, True)
        os.environ["SMOKE_PID_FILE"] = str(pid_file)
        self.addCleanup(os.environ.pop, "SMOKE_PID_FILE", None)
        source = FAKE_SERVER % {"hub": 403}
        source = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + source
        real_grace = smoke.TERM_GRACE
        smoke.TERM_GRACE = 1
        self.addCleanup(setattr, smoke, "TERM_GRACE", real_grace)
        code, _data = self.run_main("--root", str(self.fixture(server_source=source)))
        self.assertEqual(code, 0)
        self.assertTrue(self.pid_gone(int(pid_file.read_text())), "SIGKILL must follow an ignored SIGTERM")

    def test_a_body_that_is_not_json_fails_its_step(self):
        source = (FAKE_SERVER % {"hub": 403}).replace("body = json.dumps({\"ok\": True}).encode()", "body = b'not json'")
        code, data = self.run_main("--root", str(self.fixture(server_source=source)))
        self.assertEqual(code, 1)
        failed = [s["step"] for s in data["steps"] if not s["ok"]]
        self.assertEqual(failed, ["GET /api/state", "GET /api/library"])

    def test_cli_output_that_is_not_json_or_the_wrong_error_fails_its_step(self):
        root = self.fixture()
        cli = (root / "schola_cli.py").read_text()
        (root / "schola_cli.py").write_text(cli.replace('print("{}")', 'print("nope")'), encoding="utf-8")
        code, data = self.run_main("--root", str(root))
        failed = [s["step"] for s in data["steps"] if not s["ok"]]
        self.assertEqual(failed, ["cli modules", "cli events list"])
        root2 = self.fixture()
        cli = (root2 / "schola_cli.py").read_text()
        (root2 / "schola_cli.py").write_text(cli.replace("log_missing", "something_else"), encoding="utf-8")
        code, data = self.run_main("--root", str(root2))
        self.assertEqual([s["step"] for s in data["steps"] if not s["ok"]], ["cli events export, no log"])

    def test_a_server_that_exits_at_once_fails_the_start_step(self):
        root = self.fixture(server_source="import sys\nprint('boom')\nsys.exit(4)\n")
        code, data = self.run_main("--root", str(root))
        self.assertEqual(code, 1)
        self.assertIn("boom", data["steps"][-1]["detail"])

    def test_a_cli_with_the_wrong_exit_code_fails_its_step(self):
        root = self.fixture()
        (root / "schola_cli.py").write_text("import sys\nsys.exit(9)\n", encoding="utf-8")
        code, data = self.run_main("--root", str(root))
        self.assertEqual(code, 1)
        first = data["steps"][0]
        self.assertFalse(first["ok"])
        self.assertIn("exit code 9, expected 0", first["detail"])

    def pid_gone(self, pid):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        return False

    def test_the_data_folder_is_removed_and_the_server_is_stopped(self):
        pid_file = Path(tempfile.mkdtemp(prefix="smoke-pid-")) / "pid"
        self.addCleanup(shutil.rmtree, pid_file.parent, True)
        os.environ["SMOKE_PID_FILE"] = str(pid_file)
        self.addCleanup(os.environ.pop, "SMOKE_PID_FILE", None)
        before = set(Path(tempfile.gettempdir()).glob("schola-smoke-*"))
        code, _data = self.run_main("--root", str(self.fixture()))
        self.assertEqual(code, 0)
        after = set(Path(tempfile.gettempdir()).glob("schola-smoke-*"))
        self.assertEqual(after - before, set())
        pid = int(pid_file.read_text())
        self.assertTrue(self.pid_gone(pid), "the server process must be gone")

    def test_the_whole_process_group_of_the_server_is_stopped(self):
        child_file = Path(tempfile.mkdtemp(prefix="smoke-child-")) / "pid"
        self.addCleanup(shutil.rmtree, child_file.parent, True)
        os.environ["SMOKE_CHILD_PID_FILE"] = str(child_file)
        self.addCleanup(os.environ.pop, "SMOKE_CHILD_PID_FILE", None)
        code, _data = self.run_main("--root", str(self.fixture()))
        self.assertEqual(code, 0)
        child = int(child_file.read_text())
        if not self.pid_gone(child):
            os.kill(child, 9)
            self.fail("a child of the server kept running")

    def test_a_signal_while_waiting_for_the_server_stops_it(self):
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum.name):
                self.wait_signal_case(signum)

    def wait_signal_case(self, signum):
        pid_file = Path(tempfile.mkdtemp(prefix="smoke-pid-")) / "pid"
        self.addCleanup(shutil.rmtree, pid_file.parent, True)
        root = self.fixture(server_source="import os, time\nopen(os.environ['SMOKE_PID_FILE'], 'w').write(str(os.getpid()))\ntime.sleep(120)\n")
        env = dict(os.environ, SMOKE_PID_FILE=str(pid_file))
        proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "smoke.py"), "--root", str(root), "--wait", "60"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.1)
        server_pid = int(pid_file.read_text())
        proc.send_signal(signum)
        _out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, err)
        if not self.pid_gone(server_pid):
            os.kill(server_pid, 9)
            self.fail("the server kept running after a signal")

    def test_an_ignored_hangup_stays_ignored(self):
        pid_file = Path(tempfile.mkdtemp(prefix="smoke-pid-")) / "pid"
        self.addCleanup(shutil.rmtree, pid_file.parent, True)
        root = self.fixture(server_source="import os, time\nopen(os.environ['SMOKE_PID_FILE'], 'w').write(str(os.getpid()))\ntime.sleep(120)\n")
        env = dict(os.environ, SMOKE_PID_FILE=str(pid_file))
        proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "smoke.py"), "--root", str(root), "--wait", "60"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN))
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.1)
        server_pid = int(pid_file.read_text())
        proc.send_signal(signal.SIGHUP)
        time.sleep(1)
        self.assertIsNone(proc.poll(), "an ignored SIGHUP must not stop smoke")
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, err)
        self.assertTrue(self.pid_gone(server_pid))

    def test_the_last_lines_of_a_server_that_exits_are_kept(self):
        # The server writes its last line and exits in the gap between two reads. A fake process makes that
        # gap certain: the line appears when poll() is first called. Poll first, then read, keeps the line.
        class Fake:
            pid = 999999999

            def __init__(self, command, stdout=None, **_kwargs):
                self.out = stdout
                self.written = False

            def poll(self):
                if not self.written:
                    self.written = True
                    self.out.write(b"boom at the end\n")
                    self.out.flush()
                return 4

            def wait(self, timeout=None):
                return 4

        with mock.patch.object(smoke.subprocess, "Popen", Fake), mock.patch.object(smoke, "signal_group"):
            with self.assertRaises(RuntimeError) as caught:
                smoke.start_server(ROOT, Path(tempfile.gettempdir()), 5)
        self.assertIn("boom at the end", str(caught.exception))

    def test_a_refused_group_signal_is_reported(self):
        proc = mock.Mock(pid=424242)
        err = io.StringIO()
        with mock.patch.object(smoke.os, "killpg", side_effect=PermissionError("denied")), \
                mock.patch.object(smoke.sys, "stderr", err):
            smoke.signal_group(proc, signal.SIGKILL)
        self.assertIn("could not signal the process group of pid 424242", err.getvalue())
        with mock.patch.object(smoke.os, "killpg", side_effect=ProcessLookupError), \
                mock.patch.object(smoke.sys, "stderr", err):
            smoke.signal_group(proc, signal.SIGKILL)  # a group that is gone is silent

    def test_a_failed_step_still_stops_the_server(self):
        pid_file = Path(tempfile.mkdtemp(prefix="smoke-pid-")) / "pid"
        self.addCleanup(shutil.rmtree, pid_file.parent, True)
        os.environ["SMOKE_PID_FILE"] = str(pid_file)
        self.addCleanup(os.environ.pop, "SMOKE_PID_FILE", None)
        code, _data = self.run_main("--root", str(self.fixture(hub_status=200)))
        self.assertEqual(code, 1)
        self.assertTrue(self.pid_gone(int(pid_file.read_text())))

    def test_a_short_body_is_a_failed_step_not_a_crash(self):
        server = FAKE_SERVER % {"hub": 403}
        server = server.replace("self.send_header(\"Content-Length\", str(len(body)))",
                                "self.send_header(\"Content-Length\", str(len(body) + 500))")
        code, data = self.run_main("--root", str(self.fixture(server_source=server)))
        self.assertEqual(code, 1)
        self.assertTrue(any("request failed" in s["detail"] for s in data["steps"]), data["steps"])

    def test_a_symlink_in_the_seed_is_copied_as_a_link(self):
        root = self.fixture()
        seed = Path(tempfile.mkdtemp(prefix="smoke-seed-"))
        self.addCleanup(shutil.rmtree, seed, True)
        outside = Path(tempfile.mkdtemp(prefix="smoke-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.txt").write_text("private", encoding="utf-8")
        (seed / "link.txt").symlink_to(outside / "secret.txt")
        (root / "schola_cli.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "link = Path(sys.argv[2]) / 'link.txt'\n"
            "if not link.is_symlink():\n    sys.exit(7)\n"
            "args = sys.argv[3:]\n"
            "if args == ['version']:\n    print('schola 0.0.0')\n"
            "elif args[:1] == ['modules'] or args[:2] == ['events', 'list']:\n    print('{}')\n"
            "else:\n    print('schola: log_missing', file=sys.stderr)\n    sys.exit(1)\n",
            encoding="utf-8")
        code, data = self.run_main("--root", str(root), "--seed", str(seed))
        self.assertEqual(code, 0, data)

    def test_the_seed_folder_is_copied_in(self):
        root = self.fixture()
        seed = Path(tempfile.mkdtemp(prefix="smoke-seed-"))
        self.addCleanup(shutil.rmtree, seed, True)
        (seed / "library.json").write_text("{}", encoding="utf-8")
        (root / "schola_cli.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "d = Path(sys.argv[2])\n"
            "if not (d / 'library.json').is_file():\n    sys.exit(7)\n"
            "args = sys.argv[3:]\n"
            "if args == ['version']:\n    print('schola 0.0.0')\n"
            "elif args[:1] == ['modules'] or args[:2] == ['events', 'list']:\n    print('{}')\n"
            "else:\n    print('schola: log_missing', file=sys.stderr)\n    sys.exit(1)\n",
            encoding="utf-8")
        code, data = self.run_main("--root", str(root), "--seed", str(seed))
        self.assertEqual(code, 0, data)

    def test_wrong_use_exits_two(self):
        err = io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            empty = Path(tempfile.mkdtemp(prefix="smoke-empty-"))
            self.addCleanup(shutil.rmtree, empty, True)
            self.assertEqual(smoke.main(["--root", str(empty)], out=io.StringIO()), 2)
            self.assertEqual(smoke.main(["--root", str(self.fixture()), "--seed", "/no/such/folder"], out=io.StringIO()), 2)
            self.assertEqual(smoke.main(["--root", str(self.fixture()), "--wait", "0"], out=io.StringIO()), 2)
        finally:
            sys.stderr = real
        self.assertIn("smoke:", err.getvalue())

    def test_the_real_checkout_passes(self):
        code, data = self.run_main("--root", str(ROOT))
        self.assertEqual(code, 0, [s for s in data["steps"] if not s["ok"]])


if __name__ == "__main__":
    unittest.main()
