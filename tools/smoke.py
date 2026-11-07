#!/usr/bin/env python3
"""Smoke-run the Schola command line and the local server on a new data folder.

Safety class: local-write. The script makes one temporary data folder, starts
the server on a free port, and removes both when it ends. It never reads or
writes the real data folder.

Why a script: after every change to a route or a command, an agent needs the
same short list of calls and the same expected answers. This script runs
that list. An agent reads the failures and decides what they mean.

Exit code: 0 every step passes, 1 a step failed, 2 wrong use of the script,
130 interrupted.
"""

import argparse
import http.client
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

READY = re.compile(r"http://127\.0\.0\.1:(\d+)")


class UsageError(Exception):
    pass


def cli_step(name, root, data_dir, args, want_code=0, want_json=False, want_text=None, want_error=None):
    """Run one command line call and compare the result with what the step expects."""
    command = [sys.executable, str(root / "schola_cli.py"), "--data-dir", str(data_dir), *args]
    env = dict(os.environ, LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8", PYTHONDONTWRITEBYTECODE="1")
    try:
        done = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"step": name, "ok": False, "detail": "timed out after 60 seconds"}
    problem = None
    if done.returncode != want_code:
        problem = f"exit code {done.returncode}, expected {want_code}: {(done.stderr or done.stdout).strip()[:200]}"
    elif want_json:
        try:
            json.loads(done.stdout)
        except ValueError:
            problem = "output is not one JSON document"
    elif want_text is not None and want_text not in done.stdout:
        problem = f"output does not contain {want_text!r}"
    elif want_error is not None and want_error not in done.stderr:
        problem = f"error output does not contain {want_error!r}"
    return {"step": name, "ok": problem is None, "detail": problem or "ok"}


def http_step(name, base, path, want_status, want_json=False, timeout=10):
    request = urllib.request.Request(base + path)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: never use a proxy
    try:
        with opener.open(request, timeout=timeout) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:
        try:
            status, body = error.code, error.read()
        except (http.client.HTTPException, OSError) as read_error:
            return {"step": name, "ok": False, "detail": f"request failed: {read_error!r}"}
        finally:
            error.close()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        return {"step": name, "ok": False, "detail": f"request failed: {error!r}"}
    problem = None
    if status != want_status:
        problem = f"status {status}, expected {want_status}"
    elif want_json:
        try:
            json.loads(body)
        except ValueError:
            problem = "body is not JSON"
    return {"step": name, "ok": problem is None, "detail": problem or "ok"}


def start_server(root, data_dir, wait):
    """Start server.py on a free port. Return (process, base_url). Raise RuntimeError when it is not ready."""
    env = dict(os.environ, LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, str(root / "server.py"), "--port", "0", "--data-dir", str(data_dir)],
                            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True).start()
    deadline = time.monotonic() + wait
    seen = []
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            if proc.poll() is not None and lines.empty():
                break
            continue
        seen.append(line.strip())
        match = READY.search(line)
        if match:
            return proc, f"http://127.0.0.1:{match.group(1)}"
    stop_server(proc)
    raise RuntimeError("server not ready: " + (seen[-1] if seen else "no output"))


def stop_server(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    if proc.stdout:
        proc.stdout.close()


def run_smoke(root, seed, wait, say):
    steps = []
    data_dir = Path(tempfile.mkdtemp(prefix="schola-smoke-"))
    try:
        if seed:
            shutil.copytree(seed, data_dir, dirs_exist_ok=True, symlinks=True)
        calls = [
            ("cli version", ["version"], {"want_text": "schola "}),
            ("cli modules", ["modules", "--json"], {"want_json": True}),
            ("cli events list", ["events", "list", "--json"], {"want_json": True}),
            ("cli events export, no log", ["events", "export"], {"want_code": 1, "want_error": "log_missing"}),
        ]
        for name, args, expect in calls:
            say("run: schola " + " ".join(args))
            steps.append(cli_step(name, root, data_dir, args, **expect))
        try:
            proc, base = start_server(root, data_dir, wait)
        except RuntimeError as error:
            steps.append({"step": "server start", "ok": False, "detail": str(error)})
            return steps
        try:
            steps.append({"step": "server start", "ok": True, "detail": base})
            for name, path, status, as_json in [
                ("GET /", "/", 200, False),
                ("GET /api/state", "/api/state", 200, True),
                ("GET /api/library", "/api/library", 200, True),
                ("GET /api/hub without token", "/api/hub?timeZone=UTC", 403, False),
                ("GET unknown path", "/no-such-path", 404, False),
            ]:
                say(f"run: GET {path}")
                steps.append(http_step(name, base, path, status, as_json))
        finally:
            stop_server(proc)
        return steps
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)  # the folder this call made


def build_parser():
    parser = argparse.ArgumentParser(description="Smoke-run the Schola command line and server on a new data folder.")
    parser.add_argument("--root", default=".", help="The repository folder. The default is the current folder.")
    parser.add_argument("--seed", default=None, help="A folder to copy into the new data folder first.")
    parser.add_argument("--wait", type=int, default=20, help="Seconds to wait for the server. The default is 20.")
    parser.add_argument("--json", action="store_true", help="Print one JSON object.")
    return parser


def main(argv=None, out=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.root).resolve()
        for needed in ("schola_cli.py", "server.py"):
            if not (root / needed).is_file():
                raise UsageError(f"{root} is not a Schola checkout (no {needed})")
        seed = Path(args.seed).resolve() if args.seed else None
        if seed and not seed.is_dir():
            raise UsageError(f"--seed {seed} is not a folder")
        if args.wait < 1:
            raise UsageError("--wait must be 1 or more")
    except UsageError as error:
        print(f"smoke: {error}", file=sys.stderr)
        return 2

    say = (lambda _text: None) if args.json else (lambda text: print(text, file=out))

    def on_term(_signum, _frame):
        raise KeyboardInterrupt  # runs the cleanup in run_smoke

    previous = signal.signal(signal.SIGTERM, on_term)
    try:
        steps = run_smoke(root, seed, args.wait, say)
    except KeyboardInterrupt:
        print("smoke: interrupted; the server was stopped", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
    ok = all(step["ok"] for step in steps)
    if args.json:
        print(json.dumps({"ok": ok, "steps": steps}), file=out)
    else:
        for step in steps:
            print(f"{'pass' if step['ok'] else 'FAIL'}: {step['step']} ({step['detail']})", file=out)
        print("PASS" if ok else "FAIL", file=out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
