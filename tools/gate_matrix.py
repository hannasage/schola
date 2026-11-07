#!/usr/bin/env python3
"""Run the Python test suites of Schola on each supported Python version.

Safety class: local-write. The script writes log files to a new temporary
folder. It never changes the repository.

Why a script: the same suites run on the same versions in the same order
before every pull request. The result is pass or fail. An agent reads the
summary and decides what to do about a failure.

Exit code: 0 all suites pass, 1 a suite failed, ran no tests, skipped every
test, timed out, or has no matching interpreter, 2 wrong use of the script,
a refused lock file, or another gate running, 130 interrupted (SIGINT,
SIGTERM, or SIGHUP unless it is ignored).
"""

import argparse
import fcntl
import glob
import json
import os
import re
import shutil
import signal
import stat
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
LOCK_NAME = "schola-gate-matrix.lock"  # a file, not a folder
RAN = re.compile(r"^Ran (\d+) tests? in ([0-9.]+)s", re.M)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# The result line is the one right after "Ran N tests in Ns". A line a test prints cannot match.
RESULT_LINE = re.compile(r"^Ran \d+ tests? in [0-9.]+s[ \t]*\n[ \t]*\n(?:OK|FAILED)(?: \((.*?)\))?[ \t]*$", re.M)
SKIPPED = re.compile(r"skipped=(\d+)")
EXPECTED_FAILURES = re.compile(r"expected failures=(\d+)")
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


class Lock:
    """One gate at a time. Two full gates share the temporary folder and disturb each other.

    The lock is a kernel file lock. The system drops it when the process ends,
    so a crash leaves no stale lock and two gates cannot both take it.
    """

    def __init__(self, base):
        self.path = Path(base) / LOCK_NAME
        self.fd = None

    @property
    def held(self):
        return self.fd is not None

    def acquire(self):
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError as error:
            raise UsageError(f"cannot take the lock {self.path}: {error}")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise UsageError(f"{self.path} is not a regular file of this user")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                try:
                    owner = os.read(fd, 32).decode("ascii", "replace").strip() or "unknown"
                except OSError:
                    owner = "unknown"
                raise UsageError(f"another gate runs (pid {owner}, lock {self.path})")
            try:
                os.ftruncate(fd, 0)
                os.write(fd, str(os.getpid()).encode("ascii"))
            except OSError as error:
                raise UsageError(f"cannot write the lock {self.path}: {error}")
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def release(self):
        if self.fd is not None:
            os.close(self.fd)  # closing the file drops the lock; the file stays, so no unlink race
            self.fd = None


def signal_group(proc):
    """Kill the process group of proc. A group that is already gone is fine. A refusal is reported."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        print(f"gate_matrix: could not stop the process group of pid {proc.pid}: {error}", file=sys.stderr)


def kill_group(proc):
    signal_group(proc)
    proc.wait()


def interpreter_version(python):
    """Return the 'major.minor' the interpreter reports, or None."""
    try:
        done = subprocess.run([python, "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def run_suite(python, suite, root, timeout, log_path):
    """Run one suite. Return a result dict. The full output goes to log_path."""
    command = [python, *SUITES[suite]]
    # Python 3.14 colours the result line when FORCE_COLOR is set. The parser needs plain text.
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "CLICOLOR_FORCE")}
    env.update(LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8", PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1", PYTHON_COLORS="0")
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
        else:
            signal_group(proc)  # a test may leave a child behind
    seconds = round(time.monotonic() - started, 1)
    text = ANSI.sub("", Path(log_path).read_text(encoding="utf-8", errors="replace"))
    ran = RAN.findall(text)
    tests = int(ran[-1][0]) if ran else None
    results = RESULT_LINE.findall(text)
    detail = results[-1] if results else ""  # only the line after the last "Ran N tests" counts
    skipped = SKIPPED.search(detail)
    expected = EXPECTED_FAILURES.search(detail)
    if status != "timeout" and code != 0:
        status = "fail"
    first = None
    if status != "timeout" and (("NO TESTS RAN" in text) or (code == 0 and not tests)):
        status = "fail"
        first = "no tests ran"
    elif status != "timeout" and code == 0 and tests and not results:
        status = "fail"
        first = "result line not found"
    skipped_count = int(skipped.group(1)) if skipped else 0
    expected_count = int(expected.group(1)) if expected else 0
    if status == "pass" and tests and skipped_count + expected_count >= tests:
        status = "fail"
        first = "all tests skipped or expected to fail"
    if status == "fail" and first is None:
        match = FIRST_FAILURE.search(text)
        if match:
            first = match.group(0)
        else:
            lines = [line for line in text.splitlines() if line.strip()]
            first = lines[-1] if lines else f"exit code {code}"
    return {"suite": suite, "status": status, "tests": tests, "skipped": skipped_count,
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

    # Leave SIGHUP alone when it is ignored on purpose (nohup): closing a terminal must not stop the gate.
    hooked = [sig for sig in (signal.SIGTERM, signal.SIGHUP) if signal.getsignal(sig) != signal.SIG_IGN]
    previous = {sig: signal.signal(sig, on_term) for sig in hooked}
    try:
        log_dir = tempfile.mkdtemp(prefix="schola-gate-")
        results = run_matrix(root, versions, suites, args.timeout, overrides, args.allow_missing, log_dir, say)
    except KeyboardInterrupt:
        print("gate_matrix: interrupted; the running suite was stopped", file=sys.stderr)
        return 130
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
        lock.release()

    ok = all(r["status"] in ("pass", "skip") for r in results) and any(r["status"] == "pass" for r in results)
    if args.json:
        print(json.dumps({"ok": ok, "log_dir": log_dir, "results": results}), file=out)
    else:
        print(f"{'PASS' if ok else 'FAIL'}: logs in {log_dir}", file=out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
