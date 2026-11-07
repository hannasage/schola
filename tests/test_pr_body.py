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

import pr_body  # noqa: E402

GOOD = """## What changed

- `tools/gate_matrix.py`: runs the suites on each Python version.

## How it was verified

- Python 3.10: pass (2490 tests)

> [!NOTE]
> Open for you: pick the default timeout.

""" + pr_body.FOOTER + "\n"


def problems_for(text):
    return pr_body.check_body(text)


class CheckBodyTest(unittest.TestCase):
    def test_a_good_body_has_no_problems(self):
        self.assertEqual(problems_for(GOOD), [])

    def test_over_300_words_is_a_problem(self):
        text = GOOD.replace("pick the default timeout.", "word " * 300)
        self.assertTrue(any("words; the limit is 300" in p for p in problems_for(text)))

    def test_exactly_300_words_is_allowed(self):
        base = len(GOOD.split())
        text = GOOD.replace("pick the default timeout.", "word " * (300 - base + 4))  # the phrase is 4 words
        self.assertEqual(len(text.split()), 300)
        self.assertEqual(problems_for(text), [])

    def test_missing_headings_are_named(self):
        text = GOOD.replace("## How it was verified", "## Checks")
        self.assertEqual(problems_for(text), ["missing heading: How it was verified"])

    def test_a_bare_number_is_a_problem_but_a_link_and_code_are_not(self):
        bad = GOOD.replace("pick the default timeout.", "see #33 for the fix.")
        self.assertTrue(any("bare issue" in p for p in problems_for(bad)))
        link = GOOD.replace("pick the default timeout.", "see [the fix](https://github.com/hannasage/schola/pull/33) and `#33`.")
        self.assertEqual(problems_for(link), [])
        fenced = GOOD.replace("> [!NOTE]", "```\nissue #33\n```\n\n> [!NOTE]")
        self.assertEqual(problems_for(fenced), [])

    def test_a_markdown_heading_is_not_a_bare_number(self):
        self.assertEqual(problems_for(GOOD.replace("## How it was verified", "## How it was verified\n\n### 1 Detail")), [])

    def test_a_table_is_a_problem(self):
        text = GOOD.replace("> [!NOTE]", "| a | b |\n|---|---|\n\n> [!NOTE]")
        self.assertTrue(any(p.startswith("a table") for p in problems_for(text)))

    def test_dash_characters_are_a_problem(self):
        self.assertTrue(any("dash" in p for p in problems_for(GOOD.replace("runs", "runs — and"))))
        self.assertTrue(any("dash" in p for p in problems_for(GOOD.replace("runs", "runs – and"))))

    def test_a_session_link_is_a_problem(self):
        text = GOOD.replace("pick the default timeout.", "see https://claude.ai/code/session_abc123 now.")
        self.assertTrue(any("session link" in p for p in problems_for(text)))

    def test_an_unknown_callout_is_a_problem(self):
        self.assertTrue(any("unknown callout" in p for p in problems_for(GOOD.replace("[!NOTE]", "[!DANGER]"))))

    def test_a_missing_or_misplaced_footer_is_a_problem(self):
        self.assertTrue(any("attribution" in p for p in problems_for(GOOD.replace(pr_body.FOOTER, ""))))
        self.assertTrue(any("attribution" in p for p in problems_for(GOOD + "\nAfter the footer.\n")))

    def test_headings_inside_a_code_fence_do_not_count(self):
        fenced = "```\n## What changed\n\n## How it was verified\n```\n\n" + pr_body.FOOTER + "\n"
        self.assertEqual(sorted(p for p in problems_for(fenced) if p.startswith("missing")),
                         ["missing heading: How it was verified", "missing heading: What changed"])
        self.assertTrue(any("missing heading" in p for p in problems_for(GOOD.replace("## What changed", "#What changed"))))

    def test_a_link_whose_text_is_a_number_is_not_a_bare_number(self):
        text = GOOD.replace("pick the default timeout.", "see [#33](https://github.com/hannasage/schola/pull/33).")
        self.assertEqual(problems_for(text), [])

    def test_a_todo_placeholder_is_a_problem_outside_code(self):
        self.assertEqual(problems_for(GOOD.replace("pick the default timeout.", "TODO pick one.")), ["a TODO placeholder remains"])
        self.assertEqual(problems_for(GOOD.replace("pick the default timeout.", "use `TODO` markers.")), [])

    def test_an_empty_body_does_not_crash(self):
        self.assertTrue(problems_for(""))


class CommandTest(unittest.TestCase):
    def make_repo(self):
        root = Path(tempfile.mkdtemp(prefix="prbody-repo-"))
        self.addCleanup(shutil.rmtree, root, True)

        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                           cwd=root, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (root / "a.txt").write_text("a", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "base")
        git("checkout", "-q", "-b", "work")
        (root / "b.txt").write_text("b", encoding="utf-8")
        (root / "c.txt").write_text("c", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "change")
        return root

    def test_scaffold_lists_the_changed_files_and_the_gate_result(self):
        root = self.make_repo()
        gate = root.parent / (root.name + "-gate.json")
        self.addCleanup(gate.unlink)
        gate.write_text(json.dumps({"ok": True, "results": [
            {"version": "3.10", "suite": "unit", "status": "pass", "tests": 5},
            {"version": "3.10", "suite": "server", "status": "pass", "tests": 3},
            {"version": "3.11", "suite": "unit", "status": "fail", "tests": 5}]}), encoding="utf-8")
        out = io.StringIO()
        code = pr_body.main(["scaffold", "--root", str(root), "--base", "main", "--gate-json", str(gate)], out=out)
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("- `b.txt`: TODO", text)
        self.assertIn("- `c.txt`: TODO", text)
        self.assertNotIn("a.txt", text)
        self.assertIn("- Python 3.10: pass (8 tests; suites: unit, server)", text)
        self.assertIn("- Python 3.11: fail", text)
        self.assertTrue(text.rstrip().endswith(pr_body.FOOTER))

    def test_a_scaffold_has_the_right_shape_and_keeps_todo_text_for_the_agent(self):
        root = self.make_repo()
        out = io.StringIO()
        pr_body.main(["scaffold", "--root", str(root), "--base", "main"], out=out)
        self.assertEqual(problems_for(out.getvalue()), ["a TODO placeholder remains"])
        self.assertIn("TODO", out.getvalue())

    def test_scaffold_with_a_bad_base_exits_two(self):
        root = self.make_repo()
        err = io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            code = pr_body.main(["scaffold", "--root", str(root), "--base", "no-such-branch"], out=io.StringIO())
        finally:
            sys.stderr = real
        self.assertEqual(code, 2)
        self.assertIn("git diff failed", err.getvalue())

    def test_a_base_that_looks_like_an_option_is_refused_and_writes_nothing(self):
        root = self.make_repo()
        target = root / "x"
        err = io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            code = pr_body.main(["scaffold", "--root", str(root), f"--base=--output={target}"], out=io.StringIO())
        finally:
            sys.stderr = real
        self.assertEqual(code, 2)
        self.assertIn("looks like an option", err.getvalue())
        self.assertFalse(target.exists())

    def run_tool(self, *args, stdin=None):
        """Run the script in its own process with a time limit, so a blocking open fails the test."""
        return subprocess.run([sys.executable, str(ROOT / "tools" / "pr_body.py"), *args], input=stdin,
                              capture_output=True, text=True, timeout=30)

    def test_a_file_that_is_not_regular_or_is_too_large_is_refused_by_check_and_by_gate_json(self):
        root = self.make_repo()
        folder = Path(tempfile.mkdtemp(prefix="prbody-read-"))
        self.addCleanup(shutil.rmtree, folder, True)
        fifo = folder / "fifo"
        os.mkfifo(fifo)
        big = folder / "big.md"
        big.write_bytes(b"a " * (pr_body.MAX_BYTES // 2 + 10))
        for path, word in ((fifo, "not a regular file"), (big, "larger than")):
            for label, args in (("check", ["check", str(path)]),
                                ("gate-json", ["scaffold", "--root", str(root), "--base", "main", "--gate-json", str(path)])):
                with self.subTest(path=path.name, tool=label):
                    done = self.run_tool(*args)
                    self.assertEqual(done.returncode, 2, done.stderr)
                    self.assertIn(word, done.stderr)

    def test_standard_input_is_capped_like_a_file(self):
        done = self.run_tool("check", "-", stdin="a " * (pr_body.MAX_BYTES // 2 + 10))
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("larger than", done.stderr)

    def test_a_long_run_of_brackets_does_not_stall_the_check(self):
        started = time.monotonic()
        problems = pr_body.check_body("[" * (pr_body.MAX_BYTES - 10))
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(problems)

    def test_a_bare_number_problem_names_the_line_and_never_repeats_the_text(self):
        text = "## What changed\n\nSecret words here #12 more secret words\n"
        problems = pr_body.check_body(text)
        bare = [p for p in problems if "bare issue" in p]
        self.assertEqual(len(bare), 1)
        self.assertIn("line 3", bare[0])
        self.assertNotIn("Secret", bare[0])

    def test_scaffold_with_a_bad_gate_file_exits_two(self):
        root = self.make_repo()
        bad = root / "gate.json"
        bad.write_text("not json", encoding="utf-8")
        err = io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            code = pr_body.main(["scaffold", "--root", str(root), "--base", "main", "--gate-json", str(bad)], out=io.StringIO())
        finally:
            sys.stderr = real
        self.assertEqual(code, 2)

    def test_a_skipped_or_empty_version_is_never_reported_as_pass(self):
        root = self.make_repo()
        gate = root / "gate.json"
        gate.write_text(json.dumps({"ok": True, "results": [
            {"version": "3.10", "suite": "*", "status": "skip", "tests": None},
            {"version": "3.11", "suite": "unit", "status": "pass", "tests": 0},
            {"version": "3.12", "suite": "unit", "status": "pass", "tests": None},
            {"version": "3.13", "suite": "unit", "status": "timeout", "tests": None}]}), encoding="utf-8")
        out = io.StringIO()
        pr_body.main(["scaffold", "--root", str(root), "--base", "main", "--gate-json", str(gate)], out=out)
        text = out.getvalue()
        self.assertIn("- Python 3.10: skip (not run)", text)
        self.assertIn("- Python 3.11: no tests counted", text)
        self.assertIn("- Python 3.12: no tests counted", text)
        self.assertIn("- Python 3.13: fail", text)
        self.assertNotIn(": pass", text)

    def test_one_empty_suite_in_a_version_is_not_reported_as_pass(self):
        root = self.make_repo()
        gate = root / "gate.json"
        gate.write_text(json.dumps({"results": [
            {"version": "3.10", "suite": "unit", "status": "pass", "tests": 100},
            {"version": "3.10", "suite": "server", "status": "pass", "tests": None}]}), encoding="utf-8")
        out = io.StringIO()
        pr_body.main(["scaffold", "--root", str(root), "--base", "main", "--gate-json", str(gate)], out=out)
        self.assertIn("- Python 3.10: no tests counted", out.getvalue())

    def test_non_utf8_standard_input_exits_two(self):
        class Broken:
            def read(self, size=-1):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        err = io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            code = pr_body.main(["check", "-"], out=io.StringIO(), stdin=Broken())
        finally:
            sys.stderr = real
        self.assertEqual(code, 2)

    def test_an_empty_diff_and_an_empty_results_list_exit_two(self):
        root = self.make_repo()
        gate = root / "gate.json"
        gate.write_text('{"results": []}', encoding="utf-8")
        err = io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            self.assertEqual(pr_body.main(["scaffold", "--root", str(root), "--base", "HEAD"], out=io.StringIO()), 2)
            self.assertIn("no changed files", err.getvalue())
            self.assertEqual(pr_body.main(["scaffold", "--root", str(root), "--base", "main", "--gate-json", str(gate)], out=io.StringIO()), 2)
        finally:
            sys.stderr = real

    def test_a_version_where_every_test_was_skipped_has_no_tests_counted(self):
        root = self.make_repo()
        gate = root / "gate.json"
        gate.write_text(json.dumps({"results": [
            {"version": "3.10", "suite": "unit", "status": "pass", "tests": 5, "skipped": 5}]}), encoding="utf-8")
        out = io.StringIO()
        pr_body.main(["scaffold", "--root", str(root), "--base", "main", "--gate-json", str(gate)], out=out)
        self.assertIn("- Python 3.10: no tests counted", out.getvalue())

    def test_closed_standard_input_exits_two(self):
        real_stdin, real_stderr = sys.stdin, sys.stderr
        sys.stdin, sys.stderr = None, io.StringIO()
        try:
            code = pr_body.main(["check", "-"], out=io.StringIO())
        finally:
            sys.stdin, sys.stderr = real_stdin, real_stderr
        self.assertEqual(code, 2)

    def test_wrong_input_exits_two_not_one(self):
        root = self.make_repo()
        cases = {"list.json": "[]", "nokey.json": '{"results":[{"status":"pass"}]}', "dict.json": '{"results":{"a":1}}'}
        for name, content in cases.items():
            path = root / name
            path.write_text(content, encoding="utf-8")
            err = io.StringIO()
            real = sys.stderr
            sys.stderr = err
            try:
                code = pr_body.main(["scaffold", "--root", str(root), "--base", "main", "--gate-json", str(path)], out=io.StringIO())
                self.assertEqual(code, 2, name)
                binary = root / "body.md"
                binary.write_bytes(b"\xff\xfe not utf-8")
                self.assertEqual(pr_body.main(["check", str(binary)], out=io.StringIO()), 2)
                self.assertEqual(pr_body.main(["scaffold", "--root", str(root / "missing"), "--base", "main"], out=io.StringIO()), 2)
                self.assertEqual(pr_body.main(["scaffold", "--root", str(root), "--base=--output=x"], out=io.StringIO()), 2)
            finally:
                sys.stderr = real

    def test_file_names_with_accents_are_not_quoted(self):
        root = self.make_repo()
        (root / "caf\u00e9.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x"], cwd=root, check=True)
        out = io.StringIO()
        pr_body.main(["scaffold", "--root", str(root), "--base", "main"], out=out)
        self.assertIn("`caf\u00e9.txt`", out.getvalue())

    def test_check_exit_codes_and_json(self):
        directory = Path(tempfile.mkdtemp(prefix="prbody-files-"))
        self.addCleanup(shutil.rmtree, directory, True)
        good = directory / "good.md"
        good.write_text(GOOD, encoding="utf-8")
        bad = directory / "bad.md"
        bad.write_text("no headings\n", encoding="utf-8")
        out = io.StringIO()
        self.assertEqual(pr_body.main(["check", str(good)], out=out), 0)
        self.assertEqual(out.getvalue().strip(), "OK")
        out = io.StringIO()
        self.assertEqual(pr_body.main(["check", str(bad), "--json"], out=out), 1)
        data = json.loads(out.getvalue())
        self.assertFalse(data["ok"])
        self.assertIn("missing heading: What changed", data["problems"])

    def test_check_reads_standard_input_and_reports_a_missing_file(self):
        out = io.StringIO()
        self.assertEqual(pr_body.main(["check", "-"], out=out, stdin=io.StringIO(GOOD)), 0)
        err = io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            code = pr_body.main(["check", "/no/such/file.md"], out=io.StringIO())
        finally:
            sys.stderr = real
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err.getvalue())


if __name__ == "__main__":
    unittest.main()
