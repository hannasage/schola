#!/usr/bin/env python3
"""Run the Python test suites of Schola on each supported Python version.

Safety class: local-write. The script writes log files to a new temporary
folder. It never changes the repository.

Why a script: the same suites run on the same versions in the same order
before every pull request. The result is pass or fail. An agent reads the
summary and decides what to do about a failure.

Exit code: 0 all suites pass, 1 a suite failed, ran no tests, timed out, or
has no matching interpreter, 2 wrong use of the script or another gate is
running, 130 interrupted.
"""

import argparse
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_VERSIONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]
SUITES = {
    "unit": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
    "server": ["-m", "unittest", "test_server.py"],
}
DEFAULT_SUITES = ["unit", "server"]
LOCK_NAME = "schola-gate-matrix.lock"
RAN = re.compile(r"^Ran (\d+) tests? in ([0-9.]+)s", re.M)
SKIPPED = re.compile(r"skipped=(\d+)")
FIRST_FAILURE = re.compile(r"^(?:FAIL|ERROR): .+$", re.M)


class UsageError(Exception):
    pass


def find_python(version, overrides):
    """Return a path to the interpreter for a version, or None."""
    if version in overrides:
        path = overrides[version]
        return path if os.access(path, os.X_OK) else None
    found = shutil.which(f"python{version}")
    if found:
        return found
    pattern = os.path.expanduser(f"~/.local/share/uv/python/cpython-{version}*/bin/python{version}")
    for candidate in sorted(glob.glob(pattern)):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def parse_overrides(items):
    overrides = {}
    for item in items:
        version, sep, path = item.partition("=")
        if not sep or not version or not path:
            raise UsageError(f"--python expects VERSION=PATH, got {item!r}")
        overrides[version] = path
    return overrides


def pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Lock:
    """One gate at a time. Two full gates share the temporary folder and disturb each other."""

    GRACE_SECONDS = 5  # a new lock folder may not hold its pid file yet

    def __init__(self, base):
        self.path = Path(base) / LOCK_NAME
        self.held = False

    def acquire(self):
        for _ in range(2):
            try:
                self.path.mkdir()
            except FileExistsError:
                if self._is_live():
                    raise UsageError(f"another gate runs (pid {self._owner()}, lock {self.path})")
                shutil.rmtree(self.path, ignore_errors=True)  # the owner is gone: stale lock
                continue
            except OSError as error:
                raise UsageError(f"cannot take the lock {self.path}: {error}")
            (self.path / "pid").write_text(str(os.getpid()), encoding="utf-8")
            self.held = True
            return
        raise UsageError(f"cannot take the lock {self.path}")

    def _owner(self):
        try:
            return int((self.path / "pid").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _is_live(self):
        owner = self._owner()
        if owner is None:
            try:
                return time.time() - self.path.stat().st_mtime < self.GRACE_SECONDS
            except OSError:
                return False
        return pid_alive(owner)

    def release(self):
        if self.held and self._owner() == os.getpid():
            shutil.rmtree(self.path, ignore_errors=True)
        self.held = False


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def interpreter_version(python):
    """Return the 'major.minor' the interpreter reports, or None."""
    try:
        done = subprocess.run([python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def run_suite(python, suite, root, timeout, log_path):
    """Run one suite. Return a result dict. The full output goes to log_path."""
    command = [python, *SUITES[suite]]
    env = dict(os.environ, LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8", PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    status = "pass"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            code = None
            status = "timeout"
        except BaseException:
            kill_group(proc)  # the suite has its own session: a signal to this script never reaches it
            raise
    seconds = round(time.monotonic() - started, 1)
    text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    ran = RAN.findall(text)
    tests = int(ran[-1][0]) if ran else None
    skipped = SKIPPED.search(text)
    if status != "timeout" and code != 0:
        status = "fail"
    first = None
    if status != "timeout" and (("NO TESTS RAN" in text) or (code == 0 and not tests)):
        status = "fail"
        first = "no tests ran"
    if status == "fail" and first is None:
        match = FIRST_FAILURE.search(text)
        if match:
            first = match.group(0)
        else:
            lines = [line for line in text.splitlines() if line.strip()]
            first = lines[-1] if lines else f"exit code {code}"
    return {"suite": suite, "status": status, "tests": tests, "skipped": int(skipped.group(1)) if skipped else 0,
            "seconds": seconds, "first_failure": first, "command": command}


def run_matrix(root, versions, suites, timeout, overrides, allow_missing, log_dir, say):
    results = []
    for version in versions:
        python = find_python(version, overrides)
        if python is None:
            say(f"python{version}: no interpreter found")
            results.append({"version": version, "suite": "*", "status": "skip" if allow_missing else "missing",
                            "tests": None, "skipped": 0, "seconds": 0, "first_failure": None, "log": None, "python": None})
            continue
        actual = interpreter_version(python)
        if actual != version:
            say(f"python{version}: {python} reports {actual}, not {version}")
            results.append({"version": version, "suite": "*", "status": "wrong_version", "tests": None, "skipped": 0,
                            "seconds": 0, "first_failure": f"{python} reports {actual}", "log": None, "python": python})
            continue
        for suite in suites:
            log_path = Path(log_dir) / f"py{version}-{suite}.log"
            say("run: " + " ".join([python, *SUITES[suite]]))
            result = run_suite(python, suite, root, timeout, log_path)
            result.update(version=version, log=str(log_path), python=python)
            result.pop("command")
            say(f"python{version} {suite}: {result['status']} ({result['tests']} tests, {result['skipped']} skipped, {result['seconds']}s)")
            if result["first_failure"]:
                say(f"  first failure: {result['first_failure']}")
            results.append(result)
    return results


def build_parser():
    parser = argparse.ArgumentParser(description="Run the Schola Python suites on each Python version.")
    parser.add_argument("--root", default=".", help="The repository folder. The default is the current folder.")
    parser.add_argument("--versions", default=",".join(DEFAULT_VERSIONS), help="Comma-separated Python versions.")
    parser.add_argument("--suites", default=",".join(DEFAULT_SUITES), help=f"Comma-separated suites from: {', '.join(SUITES)}.")
    parser.add_argument("--timeout", type=int, default=900, help="Seconds a suite may run. The default is 900.")
    parser.add_argument("--python", action="append", default=[], metavar="VERSION=PATH", help="Use this interpreter for a version.")
    parser.add_argument("--allow-missing", action="store_true", help="Skip a version with no interpreter instead of failing.")
    parser.add_argument("--list", action="store_true", help="Print the plan and run nothing.")
    parser.add_argument("--json", action="store_true", help="Print one JSON object.")
    parser.add_argument("--lock-dir", default=None, help="The folder that holds the lock. The default is the temporary folder.")
    return parser


def main(argv=None, out=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.root).resolve()
        if not (root / "tests").is_dir() or not (root / "test_server.py").is_file():
            raise UsageError(f"{root} is not a Schola checkout (needs tests/ and test_server.py)")
        versions = [v for v in args.versions.split(",") if v]
        suites = [s for s in args.suites.split(",") if s]
        unknown = [s for s in suites if s not in SUITES]
        if not versions or not suites or unknown:
            raise UsageError(f"bad --versions or --suites (unknown suites: {unknown})")
        if args.timeout < 1:
            raise UsageError("--timeout must be 1 or more")
        overrides = parse_overrides(args.python)
    except UsageError as error:
        print(f"gate_matrix: {error}", file=sys.stderr)
        return 2

    say = (lambda _text: None) if args.json else (lambda text: print(text, file=out))
    if args.list:
        plan = [{"version": v, "suite": s, "python": find_python(v, overrides)} for v in versions for s in suites]
        if args.json:
            print(json.dumps({"ok": True, "plan": plan}), file=out)
        else:
            for item in plan:
                print(f"python{item['version']} {item['suite']}: {item['python'] or 'NOT FOUND'}", file=out)
        return 0

    lock = Lock(args.lock_dir or tempfile.gettempdir())
    try:
        lock.acquire()
    except UsageError as error:
        print(f"gate_matrix: {error}", file=sys.stderr)
        return 2
    def on_term(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, on_term)
    try:
        log_dir = tempfile.mkdtemp(prefix="schola-gate-")
        results = run_matrix(root, versions, suites, args.timeout, overrides, args.allow_missing, log_dir, say)
    except KeyboardInterrupt:
        print("gate_matrix: interrupted; the running suite was stopped", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
        lock.release()

    ok = all(r["status"] in ("pass", "skip") for r in results) and any(r["status"] == "pass" for r in results)
    if args.json:
        print(json.dumps({"ok": ok, "log_dir": log_dir, "results": results}), file=out)
    else:
        print(f"{'PASS' if ok else 'FAIL'}: logs in {log_dir}", file=out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
