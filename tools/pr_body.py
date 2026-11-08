#!/usr/bin/env python3
"""Write and check the body of a Schola pull request.

Safety class: read-only. The script reads git and the files you name, and it
prints to the screen. It never opens or edits a pull request.

Why a script: every pull request body has the same shape and the same rules.
The script checks the rules that need no judgment. An agent writes the
sentences, and the script says whether the body obeys the rules.

Commands:
  scaffold  Print a body with the changed files and the gate result filled in.
  check     Check a body file against the rules.

Exit code: 0 the body is good, 1 the body breaks a rule, 2 wrong use.
"""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

WORD_LIMIT = 300
FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
REQUIRED_HEADINGS = ["What changed", "How it was verified"]
CALLOUTS = {"NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION"}
BARE_NUMBER = re.compile(r"(?<![\w/&#])#\d+\b")
CALLOUT_LINE = re.compile(r"^>\s*\[!([A-Za-z]+)\]")
SESSION_LINK = re.compile(r"claude\.ai/(?:code/)?(?:session|chat)\S*", re.I)


class UsageError(Exception):
    pass


def strip_code(text):
    """Remove code and links. A number inside either is not a bare reference."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return re.sub(r"\[[^\]\n]{0,200}\]\([^)\n]{0,500}\)", "", text)  # a link; the bounds keep a long run of [ linear


def check_body(text):
    """Return a list of problems. An empty list means the body obeys every rule."""
    problems = []
    words = len(text.split())
    if words > WORD_LIMIT:
        problems.append(f"{words} words; the limit is {WORD_LIMIT}")
    headings = [m.group(1).strip().lower() for m in re.finditer(r"(?m)^#{1,6}[ \t]+(.+)$", strip_code(text))]
    for heading in REQUIRED_HEADINGS:
        if heading.lower() not in headings:
            problems.append(f"missing heading: {heading}")
    prose = strip_code(text)
    for number, line in enumerate(prose.splitlines(), 1):
        if BARE_NUMBER.search(line):
            # the line number, not the text: this tool can be pointed at any readable file
            problems.append(f"bare issue or pull request number on line {number} of the text without code and links (write the full link)")
            break
    if any(line.lstrip().startswith("|") for line in text.splitlines()):
        problems.append("a table; use a list")
    if "—" in text or "–" in text:
        problems.append("a dash character; write two sentences or a plain hyphen")
    if SESSION_LINK.search(text):
        problems.append("a session link; do not put one in a public pull request")
    for line in text.splitlines():
        match = CALLOUT_LINE.match(line)
        if match and match.group(1).upper() not in CALLOUTS:
            problems.append(f"unknown callout [!{match.group(1)}]")
    if re.search(r"\bTODO\b", strip_code(text)):
        problems.append("a TODO placeholder remains")
    if text.rstrip().splitlines()[-1:] != [FOOTER]:
        problems.append("the last line is not the attribution line")
    return problems


MAX_BYTES = 65_536  # a body has 300 words at most; a gate result is a few KB


def read_capped(path):
    """Read a regular file of at most MAX_BYTES as UTF-8. Anything else raises ValueError or OSError."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)  # a FIFO must not block the open
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"larger than {MAX_BYTES} bytes")
    return data.decode("utf-8")


def git_files(root, base):
    if base.startswith("-"):
        raise UsageError(f"--base {base!r} looks like an option")
    try:
        done = subprocess.run(["git", "-c", "core.quotepath=false", "diff", "--name-only", "--no-renames", f"{base}...HEAD", "--"],
                              cwd=root, capture_output=True, text=True)
    except OSError as error:
        raise UsageError(f"cannot run git in {root}: {error}")
    if done.returncode != 0:
        raise UsageError(f"git diff failed: {done.stderr.strip()[:200]}")
    files = [line for line in done.stdout.splitlines() if line]
    if not files:
        raise UsageError(f"no changed files between {base} and HEAD; check --base")
    return files


def gate_lines(path):
    """Turn a gate_matrix.py --json result into one line per Python version."""
    try:
        data = json.loads(read_capped(path))
        by_version = {}
        for item in data["results"]:
            skipped = item.get("skipped", 0)
            tests = item.get("tests")
            counted = tests - skipped if isinstance(tests, int) and isinstance(skipped, int) else None
            by_version.setdefault(item["version"], []).append((item["status"], counted, str(item.get("suite", "?"))))
        if not by_version:
            raise ValueError("the results list is empty")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        # str(), not repr(): the repr of a decode error holds the bytes of the file, and this tool reads any path
        raise UsageError(f"--gate-json is not a gate_matrix.py --json result: {type(error).__name__}: {error}")
    lines = []
    for version, items in by_version.items():
        statuses = [status for status, _tests, _suite in items]
        tests = sum(count for _status, count, _suite in items if isinstance(count, int))
        empty = any(status == "pass" and not (isinstance(count, int) and count > 0) for status, count, _suite in items)
        suites = ", ".join(dict.fromkeys(suite for _status, _count, suite in items))
        if any(status not in ("pass", "skip") for status in statuses):
            lines.append(f"- Python {version}: fail")
        elif all(status == "skip" for status in statuses):
            lines.append(f"- Python {version}: skip (not run)")
        elif empty or tests == 0:
            lines.append(f"- Python {version}: no tests counted")
        else:
            lines.append(f"- Python {version}: pass ({tests} tests; suites: {suites})")
    return lines


def scaffold(files, gate):
    out = ["## What changed", ""]
    out += [f"- `{name}`: TODO say what changed" for name in files] or ["- TODO say what changed"]
    out += ["", "## How it was verified", ""]
    out += gate or ["- TODO list the checks you ran and the result"]
    out += ["", "## Limits", "", "- TODO name what this does not cover, or delete this section", "", FOOTER]
    return "\n".join(out) + "\n"


def build_parser():
    parser = argparse.ArgumentParser(description="Write and check a Schola pull request body.")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scaffold", help="Print a body with the changed files and gate result filled in.")
    s.add_argument("--root", default=".", help="The repository folder.")
    s.add_argument("--base", default="origin/main", help="The base branch. The default is origin/main.")
    s.add_argument("--gate-json", default=None, help="A file with the output of gate_matrix.py --json.")
    c = sub.add_parser("check", help="Check a body file against the rules.")
    c.add_argument("file", help="The body file, or - for standard input.")
    c.add_argument("--json", action="store_true", help="Print one JSON object.")
    return parser


def main(argv=None, out=None, stdin=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scaffold":
            root = Path(args.root).resolve()
            gate = gate_lines(args.gate_json) if args.gate_json else []
            print(scaffold(git_files(root, args.base), gate), end="", file=out)
            return 0
        if args.file == "-":
            source = stdin or sys.stdin
            if source is None:
                raise UsageError("standard input is closed")
            try:
                text = source.read(MAX_BYTES + 1)
            except (OSError, ValueError) as error:
                raise UsageError(f"cannot read standard input: {error}")
            if len(text.encode("utf-8", "replace")) > MAX_BYTES:
                raise UsageError(f"standard input is larger than {MAX_BYTES} bytes")
        else:
            try:
                text = read_capped(args.file)
            except (OSError, ValueError) as error:
                raise UsageError(f"cannot read {args.file}: {error}")
        problems = check_body(text)
        if args.json:
            print(json.dumps({"ok": not problems, "words": len(text.split()), "problems": problems}), file=out)
        else:
            for problem in problems:
                print(f"problem: {problem}", file=out)
            print("OK" if not problems else "FAIL", file=out)
        return 1 if problems else 0
    except UsageError as error:
        print(f"pr_body: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
