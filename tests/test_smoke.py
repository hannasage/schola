import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

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

    def test_a_server_that_never_says_ready_fails_the_start_step(self):
        root = self.fixture(server_source="import time\ntime.sleep(60)\n")
        code, data = self.run_main("--root", str(root), "--wait", "1")
        self.assertEqual(code, 1)
        self.assertEqual(data["steps"][-1]["step"], "server start")
        self.assertFalse(data["steps"][-1]["ok"])

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
