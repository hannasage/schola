"""The command line tool: one thin layer over the hub engine.

The tests use the real lesson store, the real event log, and the real check runner on temporary folders.
The tests that start a program use a free port. None of them touches the repository `data` folder.
"""
import ast
import collections
import hashlib
import io
import json
import os
from pathlib import Path
import random
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
import uuid
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import check_consent
import check_routes
import check_runner
import schola_cli
import server
from event_log import EventLog
from lesson_store import LessonStore

LAUNCHER = ROOT / 'schola'
MODULE_FIXTURE = ROOT / 'tests' / 'fixtures' / 'contracts' / 'valid' / 'lesson-module-minimal.json'
EVENT_FIXTURES = ROOT / 'tests' / 'fixtures' / 'contracts' / 'learning-events' / 'valid'
PYTHON = shutil.which('python3')
SOURCE = 'print("hello from a script")\n'
OTHER_SOURCE = 'print("another script")\n'
TOKEN_HEADER = 'X-Schola-Check-Token'
ESCAPES = '\x1b[2J\x1b[H\x1b]8;;http://evil.example\x07click\x1b]8;;\x07\rovertype\x00\x7f\x9b31m\u202eevil\u200b\u2066x\U000e0041'

Result = collections.namedtuple('Result', 'code out err')


class FakeIn(io.StringIO):
    def __init__(self, text='', tty=False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


class NoRead:
    """A standard input that fails the test when anything reads it."""

    def __init__(self):
        self.touched = False

    def isatty(self):
        return False

    def read(self, *args):
        self.touched = True
        return ''

    readline = readlines = read

    def __iter__(self):
        self.touched = True
        return iter(())


class FakeOut(io.StringIO):
    def __init__(self, tty=False):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def binary_out():
    return io.TextIOWrapper(io.BytesIO(), encoding='utf-8', newline='')


def module_document(version='1.0.0', source=SOURCE, module_id='hello-module'):
    document = json.loads(MODULE_FIXTURE.read_text())
    document['id'] = module_id
    document['version'] = version
    document['schemaVersion'] = '1.1'
    document['checks'] = [
        {'id': 'self', 'kind': 'self-reported', 'description': 'The student says it is done.'},
        {'id': 'has-readme', 'kind': 'automated', 'description': 'A README exists.', 'runner': {'type': 'file-exists', 'path': 'README.md'}},
        {'id': 'has-todo', 'kind': 'automated', 'description': 'A TODO exists.', 'runner': {'type': 'text-match', 'path': 'README.md', 'text': 'TODO'}},
        {'id': 'bad-pattern', 'kind': 'automated', 'description': 'A broken pattern.', 'runner': {'type': 'text-match', 'path': 'README.md', 'pattern': '('}},
        {'id': 'valid-json', 'kind': 'automated', 'description': 'JSON is valid.', 'runner': {'type': 'json-valid', 'path': 'data.json'}},
        {'id': 'script-a', 'kind': 'automated', 'description': 'A tier two check.', 'runner': {'type': 'script'}, 'runtime': 'python3', 'source': source},
        {'id': 'script-b', 'kind': 'automated', 'description': 'Another tier two check.', 'runner': {'type': 'script', 'name': 'other'},
         'runtime': 'python3', 'source': OTHER_SOURCE},
    ]
    return document


def tree(directory):
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        key = str(path.relative_to(directory))
        result[key] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else 'other'
    return result


def event(name='task-opened', **changes):
    document = json.loads((EVENT_FIXTURES / (name + '.json')).read_text())
    document['eventId'] = str(uuid.uuid4())
    document.update(changes)
    return document


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


FORBIDDEN_CATEGORIES = ('Cc', 'Cf', 'Cs', 'Co', 'Zl', 'Zp')


def unsafe_characters(text):
    """Characters that a terminal could act on, or that hide text. A newline is the only control character allowed."""
    return sorted({ch for ch in text if ch != '\n' and unicodedata.category(ch) in FORBIDDEN_CATEGORIES})


class CliCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(os.path.realpath(self.temp.name))
        self.base = base
        self.data = base / 'data'
        self.data.mkdir()
        self.home = base / 'home'
        self.home.mkdir()
        self.work = self.home / 'work'
        self.work.mkdir()
        (self.work / 'README.md').write_text('hello\n')
        (self.work / 'data.json').write_text('{"a": 1}')
        self.env = {'HOME': str(self.home)}
        self.lessons = LessonStore(self.data)

    def import_module(self, **kwargs):
        document = module_document(**kwargs)
        self.lessons.import_files([('m.json', json.dumps(document))])
        return document

    def go(self, *argv, stdin=None, env=None, out=None, data=True):
        """Run the tool in this process. Returns the exit code and what it wrote."""
        stdout, stderr = out if out is not None else FakeOut(), FakeOut()
        arguments = (['--data-dir', str(self.data)] if data else []) + list(argv)
        code = schola_cli.main(arguments, stdin=stdin if stdin is not None else NoRead(), stdout=stdout, stderr=stderr,
                               environ=dict(self.env if env is None else env))
        return Result(code, stdout.getvalue() if hasattr(stdout, 'getvalue') else '', stderr.getvalue())

    def check_hash(self, check_id='script-a', version='1.0.0'):
        document = self.lessons.get_module('hello-module', version)
        return check_consent.check_hash(next(check for check in document['checks'] if check['id'] == check_id))

    def assertSafe(self, text):
        self.assertEqual(unsafe_characters(text), [])

    def refuse_processes(self):
        return mock.patch('check_script.subprocess.Popen', side_effect=AssertionError('a process was started'))


# ---------------------------------------------------------------------------
# The shared text helper
# ---------------------------------------------------------------------------

class SafeTextTests(unittest.TestCase):
    def test_plain_text_and_printable_unicode_are_unchanged(self):
        for text in ('hello world', 'Zoe\u0301 caf\u00e9', '\u65e5\u672c\u8a9e', 'goal \U0001f600', '', 'a-b_c.d/e'):
            self.assertEqual(schola_cli.safe_text(text), text)

    def test_an_escape_sequence_cannot_clear_the_screen_or_move_the_cursor(self):
        shown = schola_cli.safe_text('before\x1b[2Jafter\x1b[H')
        self.assertNotIn('\x1b', shown)
        self.assertIn('<U+001B>', shown)
        self.assertIn('[2J', shown)  # the letters stay visible, so the reader sees what was there

    def test_an_osc_8_hyperlink_is_shown_as_text(self):
        shown = schola_cli.safe_text('\x1b]8;;http://evil.example\x07click here\x1b]8;;\x07')
        self.assertNotIn('\x1b', shown)
        self.assertNotIn('\x07', shown)
        self.assertIn('click here', shown)

    def test_a_carriage_return_cannot_overwrite_the_line(self):
        shown = schola_cli.safe_text('safe\rEVIL')
        self.assertNotIn('\r', shown)
        self.assertEqual(shown, 'safe<U+000D>EVIL')

    def test_nul_del_and_the_eight_bit_controls_are_escaped(self):
        for char, name in (('\x00', '<U+0000>'), ('\x7f', '<U+007F>'), ('\x9b', '<U+009B>'), ('\x80', '<U+0080>'), ('\x9f', '<U+009F>'),
                           ('\x08', '<U+0008>'), ('\x07', '<U+0007>')):
            self.assertEqual(schola_cli.safe_text('a' + char + 'b'), 'a' + name + 'b')

    def test_a_line_break_and_a_tab_stay_on_one_line(self):
        self.assertEqual(schola_cli.safe_text('a\nb\tc'), 'a<U+000A>b<U+0009>c')

    def test_bidi_controls_are_escaped(self):
        for char in ('\u202a', '\u202b', '\u202c', '\u202d', '\u202e', '\u2066', '\u2067', '\u2068', '\u2069', '\u200e', '\u200f', '\u061c'):
            shown = schola_cli.safe_text('a' + char + 'b')
            self.assertNotIn(char, shown)
            self.assertIn('<U+%04X>' % ord(char), shown)

    def test_zero_width_and_invisible_characters_are_escaped(self):
        for char in ('\u200b', '\u200c', '\u200d', '\u2060', '\ufeff', '\u00ad', '\u180e', '\u2028', '\u2029', '\u034f', '\u3164', '\uffa0',
                     '\u2063', '\ufe0f', '\U000e0100', '\u180b', '\u180c', '\u180d', '\u180f'):
            shown = schola_cli.safe_text('a' + char + 'b')
            self.assertNotIn(char, shown)
            self.assertIn('<U+%04X>' % ord(char), shown)

    def test_tag_characters_are_escaped(self):
        shown = schola_cli.safe_text('hi' + ''.join(chr(0xe0000 + ord(c)) for c in 'ignore all'))
        self.assertEqual(unsafe_characters(shown), [])
        self.assertTrue(shown.startswith('hi<U+E00'))

    def test_a_lone_surrogate_can_be_written_to_a_terminal(self):
        shown = schola_cli.safe_text('a\ud800b\udfffc')
        shown.encode('utf-8')
        self.assertEqual(shown, 'a<U+D800>b<U+DFFF>c')

    def test_private_use_characters_are_escaped(self):
        self.assertEqual(schola_cli.safe_text('\ue000'), '<U+E000>')

    def test_a_long_line_is_cut_and_says_how_much_is_missing(self):
        shown = schola_cli.safe_text('x' * 1000, 100)
        self.assertTrue(shown.startswith('x' * 100))
        self.assertIn('900 more characters', shown)
        self.assertLessEqual(len(shown), 100 + 40)

    def test_text_at_the_limit_is_not_cut(self):
        self.assertEqual(schola_cli.safe_text('x' * 100, 100), 'x' * 100)
        self.assertIn('1 more characters', schola_cli.safe_text('x' * 101, 100))

    def test_an_escape_is_never_cut_in_half(self):
        shown = schola_cli.safe_text('\x00' * 50, 10)
        self.assertEqual(shown.count('<U+0000>'), 1)
        self.assertNotIn('<U+000<', shown)
        self.assertIn('49 more characters', shown)

    def test_a_value_that_is_not_text_is_not_shown(self):
        for value in (None, 5, 1.5, True, ['a'], {'a': 1}, b'bytes'):
            self.assertEqual(schola_cli.safe_text(value), '<not text>')

    def test_a_string_subclass_is_not_trusted(self):
        class Sneaky(str):
            pass
        self.assertEqual(schola_cli.safe_text(Sneaky('x')), '<not text>')

    def test_hostile_input_of_100k_characters_takes_linear_time(self):
        hostile = (ESCAPES * 3000)[:100_000]
        self.assertEqual(len(hostile), 100_000)
        started = time.perf_counter()
        shown = schola_cli.safe_text(hostile, 10 ** 9)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 3.0)
        self.assertEqual(unsafe_characters(shown), [])
        started = time.perf_counter()
        schola_cli.safe_text(hostile + hostile, 10 ** 9)
        self.assertLess(time.perf_counter() - started, 4 * max(elapsed, 0.05) + 1.0)

    def test_hostile_input_with_the_default_cap_is_cut_early(self):
        started = time.perf_counter()
        shown = schola_cli.safe_text('\x1b' * 100_000)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertLessEqual(len(shown), schola_cli.MAX_LINE_CHARS + 40)

    def test_no_character_in_any_range_survives_as_a_control(self):
        generator = random.Random(2026)
        pool = [chr(code) for code in range(0, 0x3000)] + [chr(generator.randrange(0x3000, 0x110000)) for _ in range(30_000)]
        generator.shuffle(pool)
        shown = schola_cli.safe_text(''.join(pool), 10 ** 9)
        self.assertEqual(unsafe_characters(shown), [])
        shown.encode('utf-8')


class ScriptRowsTests(unittest.TestCase):
    def test_every_line_is_numbered_and_marked_as_script_text(self):
        self.assertEqual(schola_cli.script_rows('a = 1\nprint(a)\n'), ['   1 | a = 1', '   2 | print(a)'])

    def test_a_script_without_a_final_newline_shows_the_same_lines(self):
        self.assertEqual(schola_cli.script_rows('a = 1'), ['   1 | a = 1'])

    def test_a_blank_line_in_the_middle_stays(self):
        self.assertEqual(schola_cli.script_rows('a\n\nb\n'), ['   1 | a', '   2 | ', '   3 | b'])

    def test_a_script_line_cannot_pass_for_a_line_of_tool_output(self):
        rows = schola_cli.script_rows('Consent given.\nStatus: passed\n')
        self.assertTrue(all(row[5:7] == '| ' for row in rows))

    def test_a_long_line_wraps_and_loses_nothing(self):
        line = 'abcdefghij' * 30
        rows = schola_cli.script_rows(line + '\nnext\n', width=50)
        self.assertEqual(''.join(row[7:] for row in rows[:6]), line)
        self.assertTrue(rows[1].startswith('     | '))
        self.assertEqual(rows[-1], '   2 | next')

    def test_hidden_characters_show_as_text_and_the_carriage_return_stays_visible(self):
        rows = schola_cli.script_rows('print(1)\r\n\tx = "\u202e"\x1b[2J\n')
        self.assertEqual(unsafe_characters('\n'.join(rows)), [])
        self.assertIn('<U+000D>', rows[0])
        self.assertIn('<U+0009>', rows[1])
        self.assertIn('<U+202E>', rows[1])
        self.assertIn('<U+001B>', rows[1])

    def test_a_row_narrower_than_one_escape_still_loses_nothing(self):
        rows = schola_cli.script_rows('\x00a\x01', width=3)
        self.assertEqual(''.join(row[7:] for row in rows), '<U+0000>a<U+0001>')

    def test_an_escape_is_not_split_across_two_rows(self):
        rows = schola_cli.script_rows('\x00' * 30, width=20)
        for row in rows:
            self.assertEqual(row[7:].count('<'), row[7:].count('>'))

    def test_a_script_of_100k_hostile_characters_takes_linear_time(self):
        started = time.perf_counter()
        rows = schola_cli.script_rows(ESCAPES * 3000)
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertEqual(unsafe_characters('\n'.join(rows)), [])


# ---------------------------------------------------------------------------
# The data folder, the parser, and the shape of the program
# ---------------------------------------------------------------------------

class DataFolderTests(CliCase):
    def test_the_flag_wins_over_the_environment(self):
        self.assertEqual(schola_cli.resolve_data_dir('/flag', {'SCHOLA_DATA_DIR': '/env'}), Path('/flag'))

    def test_the_environment_wins_over_the_default(self):
        self.assertEqual(schola_cli.resolve_data_dir(None, {'SCHOLA_DATA_DIR': '/env'}), Path('/env'))

    def test_the_default_is_the_data_folder_of_the_repository(self):
        self.assertEqual(schola_cli.resolve_data_dir(None, {}), ROOT / 'data')

    def test_an_empty_environment_value_counts_as_not_set(self):
        self.assertEqual(schola_cli.resolve_data_dir(None, {'SCHOLA_DATA_DIR': ''}), ROOT / 'data')

    def test_an_empty_flag_is_a_usage_error(self):
        result = self.go('--data-dir', '', 'modules', data=False)
        self.assertEqual(result.code, 2)

    def test_the_command_reads_the_environment_it_is_given_and_not_the_process_environment(self):
        self.import_module()
        other = self.base / 'other'
        other.mkdir()
        with mock.patch.dict(os.environ, {'SCHOLA_DATA_DIR': str(other)}):
            result = self.go('modules', env={'HOME': str(self.home), 'SCHOLA_DATA_DIR': str(self.data)}, data=False)
        self.assertEqual(result.code, 0)
        self.assertIn('hello-module', result.out)

    def test_the_flag_works_before_and_after_the_command(self):
        self.import_module()
        for argv in (['--data-dir', str(self.data), 'modules'], ['modules', '--data-dir', str(self.data)]):
            result = self.go(*argv, data=False)
            self.assertEqual(result.code, 0, argv)
            self.assertIn('hello-module', result.out)

    def test_a_flag_after_the_command_does_not_lose_a_flag_before_it(self):
        self.import_module()
        result = self.go('--data-dir', str(self.data), 'modules', '--json', data=False)
        self.assertEqual(result.code, 0)
        self.assertEqual(json.loads(result.out)['modules'][0]['id'], 'hello-module')

    def test_the_data_folder_path_is_never_printed(self):
        self.import_module()
        for argv in (['modules'], ['module', 'show', 'hello-module'], ['events', 'list'], ['module', 'show', 'missing'], ['events', 'delete']):
            result = self.go(*argv)
            self.assertNotIn(str(self.data), result.out + result.err, argv)
            self.assertNotIn(self.data.name + os.sep, result.out + result.err, argv)


class ParserTests(CliCase):
    def test_no_command_is_a_usage_error(self):
        result = self.go(data=False)
        self.assertEqual(result.code, 2)
        self.assertIn('usage', result.err.lower())
        self.assertEqual(result.out, '')

    def test_an_unknown_command_is_a_usage_error(self):
        self.assertEqual(self.go('frobnicate', data=False).code, 2)

    def test_a_group_without_its_command_is_a_usage_error(self):
        for group in ('module', 'check', 'events'):
            self.assertEqual(self.go(group, data=False).code, 2, group)

    def test_help_exits_zero_and_goes_to_standard_output(self):
        for argv in (['--help'], ['hub', '--help'], ['modules', '--help'], ['module', 'show', '--help'], ['check', 'run', '--help'],
                     ['events', 'delete', '--help'], ['events', 'export', '--help'], ['version', '--help']):
            result = self.go(*argv, data=False)
            self.assertEqual(result.code, 0, argv)
            self.assertIn('usage', result.out.lower())
            self.assertEqual(result.err, '')

    def test_help_lists_every_command_in_simple_english(self):
        result = self.go('--help', data=False)
        for word in ('hub', 'modules', 'module show', 'check show', 'check run', 'events list', 'events export', 'events delete', 'version'):
            self.assertIn(word, result.out.replace('{', ' ').replace('}', ' ').replace(',', ' ') + schola_cli.help_text(), word)

    def test_help_text_avoids_the_banned_words(self):
        everything = schola_cli.help_text()
        for banned in (' may ', ' might ', ' should ', ' could '):
            self.assertNotIn(banned, everything.lower())

    def test_there_is_no_flag_that_skips_consent(self):
        for flag in ('--yes', '-y', '--force', '--assume-yes', '--no-consent', '--skip-consent'):
            self.import_module()
            result = self.go('check', 'run', 'hello-module', 'script-a', '--workspace', str(self.work), flag)
            self.assertEqual(result.code, 2, flag)

    def test_abbreviated_flags_are_not_accepted(self):
        self.import_module()
        digest = self.check_hash()
        result = self.go('check', 'run', 'hello-module', 'script-a', '--workspace', str(self.work), '--cons', digest)
        self.assertEqual(result.code, 2)

    def test_a_check_id_that_looks_like_an_option_is_a_usage_error(self):
        self.import_module()
        for argv in (['check', 'show', 'hello-module', '--weird'], ['check', 'show', '-x', 'script-a'], ['module', 'show', '--version']):
            self.assertEqual(self.go(*argv).code, 2, argv)

    def test_two_dashes_let_an_id_that_looks_like_an_option_through_and_it_is_not_found(self):
        self.import_module()
        for argv in (['check', 'show', '--', 'hello-module', '--weird'], ['module', 'show', '--', '--weird']):
            result = self.go(*argv)
            self.assertEqual(result.code, 1, argv)
            self.assertIn('code: not_found', result.err)
            self.assertNotIn('--weird', result.err)

    def test_two_dashes_still_find_a_real_check(self):
        self.import_module()
        result = self.go('check', 'show', '--', 'hello-module', 'has-readme')
        self.assertEqual(result.code, 0, result.err)
        self.assertIn('has-readme', result.out)

    def test_numbers_must_be_whole_and_in_range(self):
        for argv in (['events', 'list', '--limit', '0'], ['events', 'list', '--limit', '5001'], ['events', 'list', '--limit', 'x'],
                     ['events', 'list', '--limit', '1.5'], ['events', 'list', '--offset', '-1'], ['events', 'list', '--offset', '1000000000'],
                     ['hub', '--port', '65536'], ['hub', '--port', '-1'], ['hub', '--port', 'http']):
            self.assertEqual(self.go(*argv).code, 2, argv)

    def test_a_usage_error_does_not_print_the_input_it_rejected_as_a_command(self):
        result = self.go('events', 'list', '--limit', ESCAPES)
        self.assertEqual(result.code, 2)
        self.assertSafe(result.err)

    def test_an_unknown_argument_with_hostile_text_is_not_echoed_raw(self):
        result = self.go('modules', ESCAPES)
        self.assertEqual(result.code, 2)
        self.assertSafe(result.err)
        self.assertIn('<U+001B>', result.err)

    def test_an_engine_error_with_hostile_text_is_printed_safely(self):
        from local_store import StoreError
        with mock.patch.object(LessonStore, 'list_modules', side_effect=StoreError(ESCAPES, 'bad' + ESCAPES, 500)):
            result = self.go('modules')
        self.assertEqual(result.code, 1)
        self.assertSafe(result.err)
        self.assertIn('<U+001B>', result.err)

    def test_a_flag_that_takes_no_value_refuses_one(self):
        self.assertEqual(self.go('events', 'delete', '--confirm=yes').code, 2)

    def test_the_command_names_live_in_one_table(self):
        names = {' '.join(words) for _key, words, _help in schola_cli.COMMAND_TABLE}
        self.assertEqual(names, {'hub', 'modules', 'module show', 'check show', 'check run', 'events list', 'events export', 'events delete', 'version'})
        self.assertIsInstance(schola_cli.COMMAND_TABLE, tuple)


class ShapeTests(unittest.TestCase):
    def source(self):
        return (ROOT / 'schola_cli.py').read_text(encoding='utf-8')

    def test_only_the_entry_point_may_stop_the_program(self):
        tree_ = ast.parse(self.source())
        for function in [node for node in ast.walk(tree_) if isinstance(node, ast.FunctionDef) and node.name != 'cli_entry']:
            for call in [node for node in ast.walk(function) if isinstance(node, ast.Call)]:
                name = ast.unparse(call.func)
                self.assertNotIn(name, ('sys.exit', 'exit', 'quit', 'os._exit', 'raise SystemExit'), function.name)
            for raised in [node for node in ast.walk(function) if isinstance(node, ast.Raise)]:
                self.assertNotIn('SystemExit', ast.unparse(raised), function.name)

    def test_the_agent_modules_are_not_imported(self):
        source = self.source()
        for name in ('agent_loop', 'model_adapter', 'api_adapter', 'local_adapter', 'policy_evaluator'):
            self.assertNotIn(name, source)
        probe = 'import sys, schola_cli, server\nbanned = {"agent_loop", "model_adapter", "api_adapter", "local_adapter", "policy_evaluator"}\nprint(sorted(banned & set(sys.modules)))'
        done = subprocess.run([sys.executable, '-c', probe], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.stdout.strip(), '[]', done.stderr)

    def test_only_the_standard_library_and_the_engine_are_imported(self):
        local = {path.stem for path in ROOT.glob('*.py')}
        allowed = {'check_consent', 'check_routes', 'check_runner', 'event_log', 'lesson_store', 'local_store', 'server'}
        for node in ast.walk(ast.parse(self.source())):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                root = name.split('.')[0]
                if root in local:
                    self.assertIn(root, allowed, name)
                else:
                    self.assertIn(root, sys.stdlib_module_names if hasattr(sys, 'stdlib_module_names') else {root}, name)

    def test_the_module_keeps_no_mutable_global_state(self):
        for node in ast.parse(self.source()).body:
            if isinstance(node, ast.Assign):
                self.assertNotIsInstance(node.value, (ast.List, ast.Dict, ast.Set), ast.unparse(node)[:60])

    def test_results_are_frozen_dataclasses(self):
        self.assertTrue(schola_cli.Context.__dataclass_params__.frozen)
        self.assertTrue(schola_cli.Streams.__dataclass_params__.frozen)


class OutputFaultTests(CliCase):
    def test_an_output_that_closes_early_is_exit_one_and_not_a_traceback(self):
        self.import_module()

        class Closed(FakeOut):
            def write(self, text):
                raise BrokenPipeError()

        result = self.go('modules', out=Closed())
        self.assertEqual(result.code, 1)
        self.assertNotIn('Traceback', result.err)

    def test_control_c_during_a_command_is_exit_one(self):
        with mock.patch.object(LessonStore, 'list_modules', side_effect=KeyboardInterrupt()):
            result = self.go('modules')
        self.assertEqual(result.code, 1)
        self.assertIn('Stopped', result.err)
        self.assertNotIn('Traceback', result.err)


class JsonShapeTests(CliCase):
    def test_every_json_document_has_sorted_keys_at_every_level(self):
        self.import_module()
        EventLog(self.data).append(event())

        def ordered(pairs):
            keys = [key for key, _value in pairs]
            self.assertEqual(keys, sorted(keys))
            return dict(pairs)

        for argv in (['modules', '--json'], ['module', 'show', 'hello-module', '--json'], ['check', 'show', 'hello-module', 'script-a', '--json'],
                     ['check', 'run', 'hello-module', 'has-readme', '--workspace', str(self.work), '--json'], ['events', 'list', '--json'], ['version', '--json']):
            result = self.go(*argv)
            self.assertEqual(result.code, 0, (argv, result.err))
            json.loads(result.out, object_pairs_hook=ordered)


class NoReadTests(CliCase):
    def test_no_command_reads_standard_input_unless_it_asks_for_consent(self):
        self.import_module()
        EventLog(self.data).append(event())
        output = self.base / 'out.jsonl'
        for argv in (['modules'], ['module', 'show', 'hello-module'], ['check', 'show', 'hello-module', 'script-a'],
                     ['check', 'run', 'hello-module', 'has-readme', '--workspace', str(self.work)],
                     ['check', 'run', 'hello-module', 'script-a', '--workspace', str(self.work)],
                     ['events', 'list'], ['events', 'export', '--output', str(output)], ['events', 'delete'], ['version']):
            reader = NoRead()
            with self.refuse_processes():
                self.go(*argv, stdin=reader)
            self.assertFalse(reader.touched, argv)
        self.assertTrue(output.exists())


# ---------------------------------------------------------------------------
# The server refactor and the hub command
# ---------------------------------------------------------------------------

class HubTests(CliCase):
    def test_the_hub_command_starts_the_server_on_the_given_port_and_folder(self):
        calls = []
        with mock.patch.object(server, 'run_server', lambda port, data_dir, announce: calls.append((port, data_dir, announce))):
            result = self.go('hub', '--port', '8123')
        self.assertEqual(result.code, 0)
        self.assertEqual(calls[0][:2], (8123, self.data))
        self.assertIsInstance(calls[0][1], Path)

    def test_the_default_port_is_8765(self):
        calls = []
        with mock.patch.object(server, 'run_server', lambda port, data_dir, announce: calls.append(port)):
            self.go('hub')
        self.assertEqual(calls, [8765])

    def test_the_hub_prints_its_address_and_how_to_stop(self):
        def fake(port, data_dir, announce):
            announce(f'http://127.0.0.1:{port}')

        with mock.patch.object(server, 'run_server', fake):
            result = self.go('hub', '--port', '8124')
        self.assertEqual(result.out.splitlines()[0], 'Schola is ready at http://127.0.0.1:8124')
        self.assertIn('Control-C', result.out)
        self.assertEqual(result.err, '')

    def test_the_hub_opens_a_browser_only_when_asked_and_only_after_it_is_ready(self):
        order = []

        def fake(port, data_dir, announce):
            order.append('start')
            announce(f'http://127.0.0.1:{port}')

        with mock.patch.object(server, 'run_server', fake), mock.patch('webbrowser.open', side_effect=lambda url: order.append(url) or True) as opener:
            self.go('hub', '--port', '8125')
            opener.assert_not_called()
            self.go('hub', '--port', '8125', '--open')
        self.assertEqual(order, ['start', 'start', 'http://127.0.0.1:8125'])

    def test_a_browser_that_will_not_open_is_not_an_error(self):
        def fake(port, data_dir, announce):
            announce(f'http://127.0.0.1:{port}')

        for failure in (False, RuntimeError('no browser')):
            with mock.patch.object(server, 'run_server', fake), mock.patch('webbrowser.open', side_effect=[failure] if failure is False else failure):
                result = self.go('hub', '--open')
            self.assertEqual(result.code, 0)
            self.assertIn('Open the address yourself', result.out)

    def test_a_closed_output_is_not_reported_as_a_port_problem(self):
        with mock.patch.object(server, 'run_server', side_effect=BrokenPipeError()):
            result = self.go('hub')
        self.assertEqual(result.code, 1)
        self.assertNotIn('hub_unavailable', result.err)

    def test_a_busy_port_is_a_failure_with_a_fixed_message(self):
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            busy.listen(1)
            result = self.go('hub', '--port', str(busy.getsockname()[1]))
        self.assertEqual(result.code, 1)
        self.assertIn('code: hub_unavailable', result.err)
        self.assertNotIn('Errno', result.err)
        self.assertNotIn('Traceback', result.err)

    def test_control_c_stops_the_hub_with_exit_zero(self):
        process = subprocess.Popen([sys.executable, str(LAUNCHER), 'hub', '--port', '0', '--data-dir', str(self.data)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.base)
        timer = threading.Timer(30, process.kill)
        timer.start()
        self.addCleanup(timer.cancel)
        try:
            line = process.stdout.readline()
            self.assertRegex(line, r'^Schola is ready at http://127\.0\.0\.1:[0-9]+\n$')
            port = int(line.rsplit(':', 1)[1])
            self.assertNotEqual(port, 0)
            connection = HTTPConnection('127.0.0.1', port, timeout=10)
            connection.request('GET', '/api/events')
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 403)  # the hub is up and applies its own token rule
            process.send_signal(signal.SIGINT)
            out, err = process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(err, '')


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------

class ModulesTests(CliCase):
    def test_an_empty_data_folder_says_so_and_succeeds(self):
        result = self.go('modules')
        self.assertEqual((result.code, result.err), (0, ''))
        self.assertIn('No modules are imported yet', result.out)

    def test_a_data_folder_that_does_not_exist_is_empty_and_is_not_created(self):
        missing = self.base / 'nothing-here'
        result = self.go('modules', '--data-dir', str(missing), data=False)
        self.assertEqual(result.code, 0)
        self.assertIn('No modules are imported yet', result.out)
        self.assertFalse(missing.exists())

    def test_the_list_command_writes_nothing(self):
        self.import_module()
        before = tree(self.data)
        self.go('modules')
        self.go('module', 'show', 'hello-module')
        self.assertEqual(tree(self.data), before)

    def test_it_lists_id_version_title_and_the_counts(self):
        self.import_module()
        result = self.go('modules')
        self.assertEqual(result.code, 0)
        self.assertIn('hello-module 1.0.0', result.out)
        self.assertIn('A minimal lesson module', result.out)
        self.assertIn('1 task', result.out)
        self.assertIn('7 checks', result.out)

    def test_json_has_stable_keys_and_one_document(self):
        self.import_module()
        result = self.go('modules', '--json')
        document = json.loads(result.out)
        self.assertEqual(set(document), {'modules'})
        self.assertEqual(set(document['modules'][0]), {'id', 'version', 'title', 'tasks', 'checks', 'versions', 'newerVersionAvailable'})
        self.assertEqual(document['modules'][0]['tasks'], 1)
        self.assertEqual(document['modules'][0]['checks'], 7)
        self.assertEqual(result.out.count('\n'), result.out.rstrip('\n').count('\n') + 1)

    def test_json_on_an_empty_folder_is_an_empty_list(self):
        self.assertEqual(json.loads(self.go('modules', '--json').out), {'modules': []})

    def test_the_pinned_version_is_the_one_listed_and_a_newer_one_is_noted(self):
        self.import_module()
        self.import_module(version='1.1.0')
        document = json.loads(self.go('modules', '--json').out)['modules'][0]
        self.assertEqual(document['version'], '1.0.0')
        self.assertEqual(document['newerVersionAvailable'], '1.1.0')
        self.assertEqual(document['versions'], ['1.0.0', '1.1.0'])
        self.assertIn('1.1.0 is newer', self.go('modules').out)

    def test_an_unexpected_fault_is_exit_one_with_a_fixed_message_and_no_detail(self):
        with mock.patch.object(LessonStore, 'list_modules', side_effect=RuntimeError('secret detail /private/path')):
            result = self.go('modules')
        self.assertEqual(result.code, 1)
        self.assertIn('code: internal_error', result.err)
        self.assertNotIn('secret detail', result.err + result.out)
        self.assertNotIn('Traceback', result.err)

    def test_a_file_system_fault_is_exit_one_with_a_fixed_message_and_no_detail(self):
        with mock.patch.object(LessonStore, 'list_modules', side_effect=PermissionError(13, 'Permission denied', '/secret/place')):
            result = self.go('modules')
        self.assertEqual(result.code, 1)
        self.assertIn('code: storage_unavailable', result.err)
        self.assertNotIn('/secret/place', result.err + result.out)
        self.assertNotIn('Permission denied', result.err)

    def test_a_damaged_store_is_a_failure_with_a_fixed_message(self):
        self.import_module()
        (self.data / '_lessons' / 'modules' / 'hello-module' / '1.0.0' / 'module.json').write_text('{"changed": true}')
        result = self.go('modules')
        self.assertEqual(result.code, 1)
        self.assertIn('code: corrupt_file', result.err)
        self.assertNotIn('Traceback', result.err)

    def test_show_gives_title_tasks_and_checks_with_runner_and_consent(self):
        self.import_module()
        result = self.go('module', 'show', 'hello-module')
        self.assertEqual(result.code, 0, result.err)
        lines = result.out.splitlines()
        for expected in ('Module: hello-module', 'Version: 1.0.0', 'Title: A minimal lesson module', '  task-1: State the fact in one sentence.',
                         '  has-readme: automated, file-exists, no consent needed', '  script-a: automated, script, needs consent',
                         '  self: self-reported, no runner, no consent needed', 'Tasks (1):', 'Checks (7):'):
            self.assertIn(expected, lines)

    def test_show_json_has_stable_keys(self):
        self.import_module()
        document = json.loads(self.go('module', 'show', 'hello-module', '--json').out)
        self.assertEqual(set(document), {'id', 'version', 'title', 'outcome', 'tasks', 'checks'})
        self.assertEqual(set(document['tasks'][0]), {'id', 'prompt'})
        self.assertEqual(set(document['checks'][0]), {'id', 'kind', 'runnerType', 'needsConsent', 'description'})
        by_id = {check['id']: check for check in document['checks']}
        self.assertIs(by_id['script-a']['needsConsent'], True)
        self.assertIs(by_id['has-readme']['needsConsent'], False)
        self.assertEqual(by_id['has-readme']['runnerType'], 'file-exists')
        self.assertIsNone(by_id['self']['runnerType'])

    def test_show_takes_a_version(self):
        self.import_module()
        self.import_module(version='1.1.0')
        self.assertIn('Version: 1.1.0', self.go('module', 'show', 'hello-module', '--version', '1.1.0').out.splitlines())
        self.assertIn('Version: 1.0.0', self.go('module', 'show', 'hello-module').out.splitlines())

    def test_an_unknown_module_or_version_is_not_found(self):
        self.import_module()
        for argv in (['module', 'show', 'nope'], ['module', 'show', 'hello-module', '--version', '9.9.9'], ['module', 'show', 'hello-module', '--version', 'x'],
                     ['module', 'show', '../etc']):
            result = self.go(*argv)
            self.assertEqual(result.code, 1, argv)
            self.assertIn('code: not_found', result.err)

    def hostile_module(self):
        text = ESCAPES
        document = {'schemaVersion': '1.1', 'id': 'evil-module', 'version': '1.0.0', 'title': text, 'outcome': text,
                    'blocks': [], 'prerequisites': [], 'license': {'id': 'CC-BY-4.0'},
                    'tasks': [{'id': 'task-1' + text, 'prompt': text, 'hints': []}],
                    'checks': [{'id': 'check' + text, 'kind': 'automated' + text, 'description': text, 'runner': {'type': 'script' + text, 'path': text},
                                'runtime': 'python3' + text, 'source': text + '\nprint(1)\n', 'args': [text]},
                               {'id': 'plain', 'kind': 'automated', 'description': text, 'runner': {'type': 'file-exists', 'path': text, 'k' + text: text}},
                               {'id': 'hostile-script', 'kind': 'automated', 'description': text, 'runner': {'type': 'script', 'name': text},
                                'runtime': 'python3', 'source': text + '\nprint(1)\n', 'args': [text, 'two' + text]}]}
        raw = json.dumps(document).encode()
        folder = self.data / '_lessons' / 'modules' / 'evil-module' / '1.0.0'
        folder.mkdir(parents=True)
        (folder / 'module.json').write_bytes(raw)
        (folder / 'source.json').write_text(json.dumps({'sha256': hashlib.sha256(raw).hexdigest()}))
        return document

    def test_hostile_text_in_every_field_cannot_reach_the_terminal(self):
        document = self.hostile_module()
        hostile_check = document['checks'][0]['id']
        for argv in (['modules'], ['module', 'show', 'evil-module'], ['check', 'show', 'evil-module', hostile_check], ['check', 'show', 'evil-module', 'plain'],
                     ['check', 'show', 'evil-module', 'hostile-script'], ['check', 'show', 'evil-module', hostile_check, '--workspace', str(self.work)]):
            result = self.go(*argv)
            self.assertEqual(result.code, 0, (argv, result.err))
            self.assertSafe(result.out)
            self.assertSafe(result.err)
            self.assertNotIn('\x1b', result.out)

    def test_hostile_text_in_json_output_is_escaped_to_ascii(self):
        document = self.hostile_module()
        hostile_check = document['checks'][0]['id']
        for argv in (['modules', '--json'], ['module', 'show', 'evil-module', '--json'], ['check', 'show', 'evil-module', hostile_check, '--json'],
                     ['check', 'show', 'evil-module', 'hostile-script', '--json']):
            result = self.go(*argv)
            self.assertEqual(result.code, 0, (argv, result.err))
            result.out.encode('ascii')
            for char in '\x1b\x00\x7f\r':
                self.assertNotIn(char, result.out)
            parsed = json.loads(result.out)
            if argv[0] == 'modules':
                self.assertEqual(parsed['modules'][0]['title'], ESCAPES)  # the data is intact; only its form is safe

    def test_a_hostile_script_check_is_escaped_in_the_consent_text_and_arguments(self):
        self.hostile_module()
        with self.refuse_processes():
            result = self.go('check', 'run', 'evil-module', 'hostile-script', '--workspace', str(self.work), stdin=FakeIn('0' * 8 + '\n', tty=True))
        self.assertEqual(result.code, 3)
        self.assertSafe(result.out)
        self.assertSafe(result.err)
        self.assertIn('  1: <U+001B>[2J', result.out)

    def test_a_unicode_module_title_is_shown_as_it_is(self):
        document = module_document()
        document['title'] = 'F\u00fatbol \u65e5\u672c\u8a9e'
        self.lessons.import_files([('m.json', json.dumps(document))])
        self.assertIn('F\u00fatbol \u65e5\u672c\u8a9e', self.go('modules').out)

    def test_a_unicode_data_folder_path_works(self):
        folder = self.base / 'd\u00e1ta-\u65e5\u672c'
        folder.mkdir()
        LessonStore(folder).import_files([('m.json', json.dumps(module_document()))])
        result = self.go('modules', '--data-dir', str(folder), data=False)
        self.assertEqual(result.code, 0, result.err)
        self.assertIn('hello-module', result.out)


# ---------------------------------------------------------------------------
# Checks: show
# ---------------------------------------------------------------------------

class CheckShowTests(CliCase):
    def setUp(self):
        super().setUp()
        self.import_module()

    def test_a_script_check_shows_everything_that_would_run_and_the_hash(self):
        result = self.go('check', 'show', 'hello-module', 'script-a', '--workspace', str(self.work))
        self.assertEqual(result.code, 0, result.err)
        digest = self.check_hash()
        for expected in ('Check: script-a', 'Kind: automated', 'Runner: script', 'Needs consent: yes', f'Check hash: {digest}', 'Runtime: python3',
                         'Arguments: none', 'Time limit: 30 seconds', '   1 | print("hello from a script")', 'Nothing was run.', 'timeout=30s'):
            self.assertIn(expected, result.out)
        self.assertIn('network: the script can use the network', result.out)
        self.assertIn(str(self.work), result.out)

    def test_it_runs_nothing(self):
        before = tree(self.work)
        with self.refuse_processes():
            for check_id in ('script-a', 'has-readme', 'self', 'bad-pattern'):
                result = self.go('check', 'show', 'hello-module', check_id, '--workspace', str(self.work))
                self.assertEqual(result.code, 0, result.err)
        self.assertEqual(tree(self.work), before)

    def test_it_never_calls_the_runner(self):
        with mock.patch.object(schola_cli.check_runner, 'run', side_effect=AssertionError('the runner was called')) as runner:
            for check_id in ('script-a', 'has-readme', 'self'):
                for extra in ((), ('--json',), ('--workspace', str(self.work))):
                    result = self.go('check', 'show', 'hello-module', check_id, *extra)
                    self.assertEqual(result.code, 0, (check_id, extra, result.err))
        runner.assert_not_called()

    def test_a_check_that_runs_no_script_needs_no_consent(self):
        result = self.go('check', 'show', 'hello-module', 'has-readme')
        self.assertIn('Needs consent: no', result.out)
        self.assertIn('Runner: file-exists', result.out)
        self.assertIn('path: README.md', result.out)
        self.assertNotIn('Script', result.out)

    def test_a_self_reported_check_has_no_runner(self):
        result = self.go('check', 'show', 'hello-module', 'self')
        self.assertIn('Kind: self-reported', result.out)
        self.assertIn('Runner: none', result.out)
        self.assertIn('Needs consent: no', result.out)

    def test_a_workspace_name_with_hidden_characters_is_shown_escaped(self):
        folder = self.home / 'wo\u202erk\u200b'
        folder.mkdir()
        result = self.go('check', 'show', 'hello-module', 'has-readme', '--workspace', str(folder))
        self.assertEqual(result.code, 0, result.err)
        self.assertSafe(result.out)
        self.assertIn('wo<U+202E>rk<U+200B>', result.out)

    def test_the_workspace_is_optional_for_show_and_is_not_invented(self):
        result = self.go('check', 'show', 'hello-module', 'script-a')
        self.assertEqual(result.code, 0)
        self.assertNotIn('Workspace:', result.out)

    def test_json_has_stable_keys(self):
        document = json.loads(self.go('check', 'show', 'hello-module', 'script-a', '--json', '--workspace', str(self.work)).out)
        self.assertEqual(set(document), {'moduleId', 'version', 'checkId', 'kind', 'runnerType', 'description', 'needsConsent', 'checkHash', 'runtime',
                                         'source', 'args', 'timeoutSeconds', 'workspace', 'limits', 'limitsNotApplied', 'runner'})
        self.assertEqual(document['checkHash'], self.check_hash())
        self.assertEqual(document['source'], SOURCE)
        self.assertIs(document['needsConsent'], True)
        self.assertEqual(document['workspace'], str(self.work))
        self.assertEqual(document['timeoutSeconds'], 30)
        plain = json.loads(self.go('check', 'show', 'hello-module', 'has-readme', '--json').out)
        self.assertIsNone(plain['workspace'])
        self.assertIsNone(plain['source'])
        self.assertIs(plain['needsConsent'], False)

    def test_the_hash_is_the_hash_the_hub_shows(self):
        check = next(c for c in self.lessons.get_module('hello-module', '1.0.0')['checks'] if c['id'] == 'script-a')
        shown = check_routes._shown(check, None, check_consent.check_hash(check))
        document = json.loads(self.go('check', 'show', 'hello-module', 'script-a', '--json').out)
        for key in ('checkHash', 'needsConsent', 'runnerType', 'runtime', 'source', 'args', 'timeoutSeconds', 'limits', 'limitsNotApplied'):
            self.assertEqual(document[key], shown[key], key)

    def test_a_longer_script_is_shown_in_full(self):
        source = ''.join(f'line_{n} = {n}\n' for n in range(500))
        self.import_module(version='1.1.0', source=source)
        result = self.go('check', 'show', 'hello-module', 'script-a', '--version', '1.1.0')
        self.assertIn(' 500 | line_499 = 499', result.out)
        self.assertNotIn('hidden', result.out)

    def test_unknown_module_version_or_check_is_not_found_and_the_message_does_not_echo_the_input(self):
        for argv in (['check', 'show', 'nope', 'script-a'], ['check', 'show', 'hello-module', 'nope'], ['check', 'show', 'hello-module', 'script-a', '--version', '2.0.0'],
                     ['check', 'show', 'hello-module', ESCAPES]):
            result = self.go(*argv)
            self.assertEqual(result.code, 1, argv)
            self.assertIn('code: not_found', result.err)
            self.assertNotIn('nope', result.err)
            self.assertSafe(result.err)

    def test_a_bad_workspace_is_refused_with_exit_three(self):
        result = self.go('check', 'show', 'hello-module', 'script-a', '--workspace', str(self.base / 'missing'))
        self.assertEqual(result.code, 3)
        self.assertIn('code: invalid_workspace', result.err)


# ---------------------------------------------------------------------------
# Checks: run, and consent
# ---------------------------------------------------------------------------

class CheckRunTests(CliCase):
    def setUp(self):
        super().setUp()
        self.import_module()

    def run_check(self, check_id, *extra, **kwargs):
        return self.go('check', 'run', 'hello-module', check_id, '--workspace', str(self.work), *extra, **kwargs)

    def test_a_passing_built_in_check_prints_status_and_evidence(self):
        result = self.run_check('has-readme')
        self.assertEqual((result.code, result.err), (0, ''))
        self.assertIn('Status: passed', result.out)
        self.assertIn('path: README.md', result.out)
        self.assertIn('Evidence:', result.out)

    def test_a_check_that_ran_and_failed_is_exit_zero(self):
        result = self.run_check('has-todo')
        self.assertEqual(result.code, 0)
        self.assertIn('Status: failed', result.out)

    def test_a_check_that_could_not_run_is_exit_one_and_still_shows_its_result(self):
        result = self.run_check('bad-pattern')
        self.assertEqual(result.code, 1)
        self.assertIn('Status: error', result.out)

    def test_a_self_reported_check_is_skipped_and_exit_zero(self):
        result = self.run_check('self')
        self.assertEqual(result.code, 0)
        self.assertIn('Status: skipped', result.out)

    def test_json_output_is_one_document_with_stable_keys(self):
        result = self.run_check('has-readme', '--json')
        document = json.loads(result.out)
        self.assertEqual(set(document), {'moduleId', 'version', 'checkId', 'checkHash', 'tier', 'result'})
        self.assertEqual(document['tier'], 1)
        self.assertEqual(set(document['result']), {'status', 'evidence', 'log'})
        self.assertEqual(document['result']['status'], 'passed')
        result.out.encode('ascii')

    def test_the_run_reads_the_real_workspace_and_changes_nothing(self):
        before = tree(self.work)
        (self.work / 'README.md').write_text('a TODO here\n')
        before = tree(self.work)
        self.assertIn('Status: passed', self.run_check('has-todo').out)
        self.assertEqual(tree(self.work), before)

    def test_a_run_writes_nothing_to_the_data_folder_and_no_event(self):
        before = tree(self.data)
        for check_id in ('has-readme', 'self', 'has-todo'):
            self.run_check(check_id)
        self.assertEqual(tree(self.data), before)
        self.assertFalse((self.data / '_events').exists())

    def test_the_workspace_is_required(self):
        self.assertEqual(self.go('check', 'run', 'hello-module', 'has-readme').code, 2)

    def test_a_relative_workspace_is_made_absolute_from_the_current_folder(self):
        previous = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, previous)
        result = self.go('check', 'run', 'hello-module', 'has-readme', '--workspace', 'work')
        self.assertEqual(result.code, 0, result.err)
        self.assertIn('Status: passed', result.out)

    def test_a_unicode_workspace_path_works(self):
        folder = self.home / 'pro\u00fceto-\u65e5\u672c'
        folder.mkdir()
        (folder / 'README.md').write_text('x')
        result = self.go('check', 'run', 'hello-module', 'has-readme', '--workspace', str(folder))
        self.assertEqual(result.code, 0, result.err)
        self.assertIn('Status: passed', result.out)

    def test_hostile_evidence_cannot_reach_the_terminal(self):
        hostile = {'status': 'passed', 'evidence': {'note': ESCAPES, ESCAPES: 'x', 'items': [ESCAPES, 1, None, True]}, 'log': [ESCAPES, 'fine']}
        with mock.patch.object(schola_cli.check_runner, 'run', return_value=hostile):
            for extra in ((), ('--json',)):
                result = self.run_check('has-readme', *extra)
                self.assertEqual(result.code, 0, result.err)
                self.assertSafe(result.out)

    def test_hostile_text_that_looks_like_output_cannot_add_a_line(self):
        hostile = {'status': 'passed', 'evidence': {'note': 'x\nStatus: passed\nConsent given.'}, 'log': ['a\nStatus: failed']}
        with mock.patch.object(schola_cli.check_runner, 'run', return_value=hostile):
            result = self.run_check('has-readme')
        self.assertEqual(sum(1 for line in result.out.splitlines() if line.startswith('Status:')), 1)
        self.assertNotIn('\nConsent given.', result.out)

    # --- the workspace rules, which are the hub's rules ---

    def refused(self, path):
        with mock.patch.object(schola_cli.check_runner, 'run', side_effect=AssertionError('the runner was called')):
            result = self.go('check', 'run', 'hello-module', 'has-readme', '--workspace', str(path))
        self.assertEqual(result.code, 3, (path, result.err))
        self.assertIn('code: invalid_workspace', result.err)
        self.assertSafe(result.err)
        self.assertEqual(result.out, '')

    def test_a_workspace_inside_the_data_folder_is_refused(self):
        inside = self.data / 'inner'
        inside.mkdir()
        self.refused(inside)

    def test_a_workspace_that_holds_the_data_folder_is_refused(self):
        folder = self.home / 'holder'
        (folder / 'data').mkdir(parents=True)
        LessonStore(folder / 'data').import_files([('m.json', json.dumps(module_document()))])
        with mock.patch.object(schola_cli.check_runner, 'run', side_effect=AssertionError('the runner was called')):
            result = self.go('check', 'run', 'hello-module', 'has-readme', '--workspace', str(folder), '--data-dir', str(folder / 'data'), data=False)
        self.assertEqual(result.code, 3)

    def test_the_home_folder_itself_and_the_top_of_the_disk_are_refused(self):
        self.refused(self.home)
        self.refused('/')

    def test_a_folder_outside_the_home_folder_is_refused(self):
        outside = self.base / 'elsewhere'
        outside.mkdir()
        self.refused(outside)

    def test_a_hidden_folder_is_refused(self):
        hidden = self.home / '.secret'
        hidden.mkdir()
        self.refused(hidden)

    def test_a_missing_folder_and_a_file_are_refused(self):
        self.refused(self.home / 'missing')
        self.refused(self.work / 'README.md')

    def test_a_symlink_that_leaves_the_home_folder_is_refused(self):
        outside = self.base / 'elsewhere'
        outside.mkdir()
        (outside / 'README.md').write_text('x')
        link = self.home / 'link'
        link.symlink_to(outside)
        self.refused(link)

    def test_a_symlink_to_a_folder_inside_home_runs_in_the_real_folder(self):
        link = self.home / 'link'
        link.symlink_to(self.work)
        result = self.go('check', 'run', 'hello-module', 'has-readme', '--workspace', str(link))
        self.assertEqual(result.code, 0, result.err)
        self.assertIn('Status: passed', result.out)

    def test_a_workspace_name_with_a_control_character_is_refused_and_not_echoed(self):
        self.refused(str(self.home) + '/work\x1b[2J')

    # --- consent ---

    def test_a_script_check_without_a_terminal_and_without_a_hash_is_refused_and_nothing_starts(self):
        with self.refuse_processes():
            result = self.run_check('script-a')
        self.assertEqual(result.code, 3)
        self.assertIn('code: consent_required', result.err)
        self.assertIn('check show', result.err)
        self.assertNotIn(self.check_hash(), result.out + result.err)
        self.assertNotIn('Status:', result.out)

    def test_a_wrong_consent_value_is_refused_and_nothing_starts(self):
        digest = self.check_hash()
        wrong = ['', ' ', digest[:8], digest[:63], digest.upper(), digest + '0', digest + '\n', ' ' + digest, '0' * 64, self.check_hash('script-b'),
                 digest[:-1] + ('0' if digest[-1] != '0' else '1'), 'yes', 'true', '1']
        for value in wrong:
            with self.refuse_processes():
                result = self.run_check('script-a', '--consent', value)
            self.assertEqual(result.code, 3, repr(value))
            self.assertIn('code: hash_mismatch', result.err)
            self.assertNotIn(digest, result.out + result.err)

    def test_the_hash_of_another_check_does_not_apply(self):
        with self.refuse_processes():
            result = self.run_check('script-b', '--consent', self.check_hash('script-a'))
        self.assertEqual(result.code, 3)

    def test_a_changed_script_needs_a_new_hash(self):
        old = self.check_hash()
        self.import_module(version='1.1.0', source='print("changed")\n')
        with self.refuse_processes():
            result = self.go('check', 'run', 'hello-module', 'script-a', '--version', '1.1.0', '--workspace', str(self.work), '--consent', old)
        self.assertEqual(result.code, 3)
        self.assertNotEqual(old, self.check_hash(version='1.1.0'))

    @unittest.skipUnless(PYTHON, 'python3 is not on the PATH')
    def test_the_full_hash_runs_the_script_and_prints_the_result(self):
        result = self.run_check('script-a', '--consent', self.check_hash())
        self.assertEqual((result.code, result.err), (0, ''))
        self.assertIn('Status: passed', result.out)
        self.assertIn('hello from a script', result.out)

    @unittest.skipUnless(PYTHON, 'python3 is not on the PATH')
    def test_a_consent_is_for_one_command_only(self):
        digest = self.check_hash()
        self.assertEqual(self.run_check('script-a', '--consent', digest).code, 0)
        with self.refuse_processes():
            second = self.run_check('script-a')
        self.assertEqual(second.code, 3)
        self.assertEqual(tree(self.data).keys() & {'consent.json'}, set())

    @unittest.skipUnless(PYTHON, 'python3 is not on the PATH')
    def test_the_script_runs_in_the_workspace_that_was_named(self):
        other = self.home / 'other'
        other.mkdir()
        digest = self.check_hash()
        seen = []
        real = schola_cli.check_runner.run

        def spy(check, workspace, *args, **kwargs):
            seen.append(workspace)
            return real(check, workspace, *args, **kwargs)

        with mock.patch.object(schola_cli.check_runner, 'run', spy):
            self.go('check', 'run', 'hello-module', 'script-a', '--workspace', str(other), '--consent', digest)
        self.assertEqual(seen, [str(other)])

    def test_the_consent_is_bound_to_check_module_version_hash_and_workspace_through_the_registry(self):
        grants = []
        original = check_consent.ConsentRegistry.grant

        def spy(registry, *args):
            grants.append(args)
            return original(registry, *args)

        with mock.patch.object(check_consent.ConsentRegistry, 'grant', spy), mock.patch.object(schola_cli.check_runner, 'run', return_value={'status': 'passed', 'evidence': {}, 'log': []}) as runner:
            self.run_check('script-a', '--consent', self.check_hash())
        self.assertEqual(grants, [('hello-module', '1.0.0', 'script-a', self.check_hash(), str(self.work))])
        self.assertEqual(runner.call_args.kwargs['consent']['checkHash'], self.check_hash())

    def test_no_environment_value_and_no_flag_skips_consent(self):
        digest = self.check_hash()
        env = dict(self.env, SCHOLA_CONSENT=digest, SCHOLA_YES='1', YES='1', CI='1', SCHOLA_ASSUME_YES='1', SCHOLA_CONSENT_HASH=digest)
        with self.refuse_processes():
            result = self.run_check('script-a', env=env)
        self.assertEqual(result.code, 3)

    def test_the_process_environment_cannot_skip_consent_either(self):
        digest = self.check_hash()
        names = ('SCHOLA_CONSENT', 'SCHOLA_YES', 'YES', 'CI', 'SCHOLA_ASSUME_YES', 'SCHOLA_CONSENT_HASH', 'SCHOLA_CONSENT_ALL')
        with mock.patch.dict(os.environ, {name: digest if 'CONSENT' in name else '1' for name in names}):
            with self.refuse_processes():
                result = self.run_check('script-a')
        self.assertEqual(result.code, 3)

    def test_the_consent_is_bound_to_the_real_workspace_path(self):
        link = self.home / 'link'
        link.symlink_to(self.work)
        grants = []
        original = check_consent.ConsentRegistry.grant

        def spy(registry, *args):
            grants.append(args)
            return original(registry, *args)

        with mock.patch.object(check_consent.ConsentRegistry, 'grant', spy), \
                mock.patch.object(schola_cli.check_runner, 'run', return_value={'status': 'passed', 'evidence': {}, 'log': []}) as runner:
            self.go('check', 'run', 'hello-module', 'script-a', '--workspace', str(link), '--consent', self.check_hash())
        self.assertEqual(grants[0][-1], str(self.work))
        self.assertEqual(runner.call_args.args[1], str(self.work))

    def test_a_consent_value_for_a_check_that_runs_no_script_is_ignored_with_a_note(self):
        result = self.run_check('has-readme', '--consent', 'whatever')
        self.assertEqual(result.code, 0)
        self.assertIn('needs no consent', result.err)

    # --- the terminal prompt ---

    @unittest.skipUnless(PYTHON, 'python3 is not on the PATH')
    def test_on_a_terminal_the_student_types_the_first_eight_characters_to_agree(self):
        digest = self.check_hash()
        result = self.run_check('script-a', stdin=FakeIn(digest[:8] + '\n', tty=True))
        self.assertEqual(result.code, 0, result.err)
        self.assertIn('Status: passed', result.out)
        self.assertIn(digest, result.out)  # the full hash is shown, not the short one
        self.assertIn('   1 | print("hello from a script")', result.out)
        self.assertIn('first 8 characters', result.out)
        self.assertIn('This is consent, not a sandbox.', result.out.split('Status: passed')[0])
        self.assertIn(f'Workspace: {self.work}', result.out.splitlines())
        self.assertIn('Check: script-a', result.out.splitlines())
        self.assertLess(result.out.index('1 | print'), result.out.index('Status: passed'))

    def test_on_a_terminal_a_wrong_answer_is_refused_and_nothing_starts(self):
        digest = self.check_hash()
        for answer in ('\n', '', 'yes\n', 'y\n', digest[:7] + '\n', digest[:9] + '\n', digest + '\n', digest[:8].upper() + '\n', '0' * 8 + '\n', self.check_hash('script-b')[:8] + '\n'):
            with self.refuse_processes():
                result = self.run_check('script-a', stdin=FakeIn(answer, tty=True))
            self.assertEqual(result.code, 3, repr(answer))
            self.assertIn('code: consent_declined', result.err)
            self.assertNotIn('Status:', result.out)

    def test_the_prompt_reads_one_bounded_line_only(self):
        class Endless(FakeIn):
            def readline(self, size=-1):
                assert size is not None and 0 < size <= 1000, size
                return 'x' * size

        with self.refuse_processes():
            result = self.run_check('script-a', stdin=Endless('', tty=True))
        self.assertEqual(result.code, 3)

    def test_the_prompt_is_not_shown_without_a_terminal(self):
        with self.refuse_processes():
            result = self.run_check('script-a', stdin=FakeIn(self.check_hash()[:8] + '\n', tty=False))
        self.assertEqual(result.code, 3)
        self.assertIn('code: consent_required', result.err)

    def test_a_consent_value_wins_over_the_prompt_and_a_wrong_one_does_not_fall_back_to_it(self):
        digest = self.check_hash()
        with self.refuse_processes():
            result = self.run_check('script-a', '--consent', '0' * 64, stdin=FakeIn(digest[:8] + '\n', tty=True))
        self.assertEqual(result.code, 3)
        self.assertIn('code: hash_mismatch', result.err)

    @unittest.skipUnless(PYTHON, 'python3 is not on the PATH')
    def test_a_terminal_session_with_a_consent_value_does_not_ask(self):
        result = self.run_check('script-a', '--consent', self.check_hash(), stdin=NoRead())
        self.assertEqual(result.code, 0)

    def test_hostile_script_text_is_escaped_in_the_consent_text(self):
        source = 'print(1)\r\n# \x1b[2J\x1b]8;;http://evil.example\x07x\x1b]8;;\x07\n\u202e# hidden\n\ufeffprint(2)\n'
        self.import_module(version='1.2.0', source=source)
        digest = self.check_hash(version='1.2.0')
        with self.refuse_processes():
            result = self.go('check', 'run', 'hello-module', 'script-a', '--version', '1.2.0', '--workspace', str(self.work), stdin=FakeIn('0' * 8, tty=True))
        self.assertEqual(result.code, 3)
        self.assertSafe(result.out)
        self.assertSafe(result.err)
        self.assertIn('<U+001B>', result.out)
        self.assertIn('<U+202E>', result.out)
        self.assertIn(digest, result.out)

    def test_a_script_too_long_for_the_screen_cannot_be_agreed_to_by_typing(self):
        source = ''.join(f'value_{n} = {n}\n' for n in range(400))
        self.import_module(version='1.3.0', source=source)
        digest = self.check_hash(version='1.3.0')
        with self.refuse_processes():
            result = self.go('check', 'run', 'hello-module', 'script-a', '--version', '1.3.0', '--workspace', str(self.work), stdin=FakeIn(digest[:8] + '\n', tty=True))
        self.assertEqual(result.code, 3)
        self.assertIn('code: consent_required', result.err)
        self.assertIn('value_0 = 0', result.out)
        self.assertNotIn('value_399', result.out)
        shown = ''.join(f'value_{n} = {n}\n' for n in range(400))
        hidden = len(source.encode()) - sum(len(line.encode()) + 1 for line in shown.split('\n')[:self.shown_lines(result.out)])
        self.assertIn(f'{hidden} bytes of the script are not shown', result.out + result.err)
        self.assertIn('check show', result.err)

    def shown_lines(self, text):
        return sum(1 for line in text.splitlines() if line[:4].strip().isdigit() and line[4:7] == ' | ')

    @unittest.skipUnless(PYTHON, 'python3 is not on the PATH')
    def test_a_script_too_long_for_the_screen_runs_with_the_full_hash(self):
        source = ''.join(f'value_{n} = {n}\n' for n in range(400)) + 'print("long done")\n'
        self.import_module(version='1.3.0', source=source)
        result = self.go('check', 'run', 'hello-module', 'script-a', '--version', '1.3.0', '--workspace', str(self.work), '--consent', self.check_hash(version='1.3.0'))
        self.assertEqual(result.code, 0, result.err)
        self.assertIn('long done', result.out)

    def test_a_script_exactly_at_the_screen_cap_is_shown_whole(self):
        lines = schola_cli.PROMPT_ROWS
        source = ''.join(f'v{n} = {n}\n' for n in range(lines))
        self.import_module(version='1.4.0', source=source)
        with self.refuse_processes():
            result = self.go('check', 'run', 'hello-module', 'script-a', '--version', '1.4.0', '--workspace', str(self.work), stdin=FakeIn('0' * 8 + '\n', tty=True))
        self.assertIn('code: consent_declined', result.err)
        self.assertNotIn('not shown', result.out + result.err)

    def test_one_line_over_the_screen_cap_hides_it_and_counts_it(self):
        lines = schola_cli.PROMPT_ROWS + 1
        source = ''.join(f'v{n} = {n}\n' for n in range(lines))
        self.import_module(version='1.4.0', source=source)
        with self.refuse_processes():
            result = self.go('check', 'run', 'hello-module', 'script-a', '--version', '1.4.0', '--workspace', str(self.work), stdin=FakeIn('0' * 8 + '\n', tty=True))
        self.assertIn('code: consent_required', result.err)
        self.assertIn(f'{len(f"v{lines - 1} = {lines - 1}") + 1} bytes of the script are not shown', result.out + result.err)

    def test_a_single_line_longer_than_the_screen_is_hidden_and_counted(self):
        source = 'x = "' + 'a' * 20000 + '"\n'
        self.import_module(version='1.5.0', source=source)
        with self.refuse_processes():
            result = self.go('check', 'run', 'hello-module', 'script-a', '--version', '1.5.0', '--workspace', str(self.work), stdin=FakeIn('0' * 8 + '\n', tty=True))
        self.assertIn('code: consent_required', result.err)
        self.assertIn(f'{len(source.encode())} bytes of the script are not shown', result.out + result.err)

    def test_the_runner_is_called_without_a_consent_for_a_check_that_needs_none(self):
        with mock.patch.object(schola_cli.check_runner, 'run', return_value={'status': 'passed', 'evidence': {}, 'log': []}) as runner:
            self.run_check('has-readme')
        self.assertIsNone(runner.call_args.kwargs['consent'])

    def test_no_consent_text_is_printed_for_a_check_that_runs_no_script(self):
        result = self.run_check('has-readme', stdin=FakeIn('', tty=True))
        self.assertNotIn('first 8 characters', result.out)


@unittest.skipUnless(PYTHON and hasattr(os, 'openpty'), 'needs python3 and a pseudo terminal')
class RealTerminalTests(CliCase):
    """The consent question on a real pseudo terminal, started through the launcher."""

    def setUp(self):
        super().setUp()
        self.import_module()

    def ask(self, answer):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(self.home), 'LC_ALL': 'en_US.UTF-8', 'LANG': 'en_US.UTF-8'}
        process = subprocess.Popen([str(LAUNCHER), 'check', 'run', 'hello-module', 'script-a', '--workspace', str(self.work), '--data-dir', str(self.data)],
                                   stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(self.base))
        os.close(slave)
        os.write(master, answer.encode())
        timer = threading.Timer(60, process.kill)
        timer.start()
        try:
            out, err = process.communicate(timeout=90)
        finally:
            timer.cancel()
        return process.returncode, out, err

    def test_the_right_eight_characters_on_a_real_terminal_run_the_script(self):
        code, out, err = self.ask(self.check_hash()[:8] + '\n')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('hello from a script', out)

    def test_a_wrong_answer_on_a_real_terminal_runs_nothing(self):
        code, out, err = self.ask('00000000\n')
        self.assertEqual(code, 3)
        self.assertIn('code: consent_declined', err)
        self.assertNotIn('Status:', out)

    def test_end_of_input_on_a_real_terminal_runs_nothing(self):
        code, out, err = self.ask('\x04')
        self.assertEqual(code, 3)
        self.assertNotIn('Status:', out)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class EventsTests(CliCase):
    def log(self):
        return EventLog(self.data)

    def damage(self, extra=b'this is not json\n'):
        with open(self.data / '_events' / 'events.jsonl', 'ab') as handle:
            handle.write(extra)

    def test_list_on_an_empty_data_folder_says_so_and_succeeds(self):
        result = self.go('events', 'list')
        self.assertEqual((result.code, result.err), (0, ''))
        self.assertIn('There is no event log yet', result.out)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_list_on_a_missing_data_folder_creates_nothing(self):
        missing = self.base / 'nothing-here'
        result = self.go('events', 'list', '--data-dir', str(missing), data=False)
        self.assertEqual(result.code, 0)
        self.assertFalse(missing.exists())

    def test_list_json_on_an_empty_folder(self):
        document = json.loads(self.go('events', 'list', '--json').out)
        self.assertEqual(document['exists'], False)
        self.assertEqual(document['events'], [])
        self.assertEqual(document['total'], 0)

    def test_list_shows_id_time_type_task_source_and_key_fields(self):
        first = event('task-opened')
        second = event('check-result-passed')
        self.log().append(first)
        self.log().append(second)
        result = self.go('events', 'list')
        self.assertEqual((result.code, result.err), (0, ''))
        self.assertIn(first['eventId'], result.out)
        self.assertIn('2026-11-02T09:17:00Z', result.out)
        lines = [line for line in result.out.splitlines() if second['eventId'] in line]
        self.assertEqual(len(lines), 1)
        for expected in ('check_result', 'task=write-brief', 'source=app', 'checkId=brief-has-title', 'status=passed', 'module=intro-chat'):
            self.assertIn(expected, lines[0])
        self.assertIn('2 lines', result.out)

    def test_list_json_has_stable_keys(self):
        self.log().append(event('task-opened'))
        document = json.loads(self.go('events', 'list', '--json').out)
        self.assertEqual(set(document), {'ok', 'exists', 'total', 'offset', 'limit', 'more', 'truncatedTail', 'events', 'damaged'})
        self.assertEqual(set(document['events'][0]), {'line', 'event'})
        self.assertEqual(document['damaged'], [])
        self.assertIs(document['ok'], True)
        self.assertEqual(document['limit'], schola_cli.DEFAULT_LIST_LIMIT)

    def test_limit_and_offset_choose_a_window_and_say_where_to_go_on(self):
        ids = []
        for _ in range(5):
            item = event('task-opened')
            ids.append(item['eventId'])
            self.log().append(item)
        result = self.go('events', 'list', '--limit', '2', '--offset', '1')
        self.assertEqual(result.code, 0)
        self.assertNotIn(ids[0], result.out)
        self.assertIn(ids[1], result.out)
        self.assertIn(ids[2], result.out)
        self.assertNotIn(ids[3], result.out)
        self.assertIn('--offset 3', result.out)
        last = self.go('events', 'list', '--limit', '2', '--offset', '4')
        self.assertNotIn('--offset', last.out)
        document = json.loads(self.go('events', 'list', '--limit', '2', '--offset', '1', '--json').out)
        self.assertEqual([item['line'] for item in document['events']], [2, 3])
        self.assertIs(document['more'], True)

    def test_an_offset_past_the_end_shows_nothing_and_succeeds(self):
        self.log().append(event())
        result = self.go('events', 'list', '--offset', '50')
        self.assertEqual(result.code, 0)

    def test_a_damaged_line_is_reported_with_exit_one_and_the_good_events_still_show(self):
        good = event('task-opened')
        self.log().append(good)
        self.damage()
        after = event('check-result-passed')
        self.damage(json.dumps(after).encode() + b'\n')
        result = self.go('events', 'list')
        self.assertEqual(result.code, 1)
        self.assertIn(good['eventId'], result.out)
        self.assertIn(after['eventId'], result.out)
        self.assertIn('line 2 is damaged (invalid)', result.out)
        self.assertIn('code: log_damaged', result.err)
        self.assertNotIn('Traceback', result.err)
        self.assertNotIn('this is not json', result.out + result.err)

    def test_a_damaged_line_in_json_output_lists_the_line_and_reason(self):
        self.log().append(event())
        self.damage()
        result = self.go('events', 'list', '--json')
        self.assertEqual(result.code, 1)
        document = json.loads(result.out)
        self.assertEqual(document['damaged'], [{'line': 2, 'reason': 'invalid'}])
        self.assertIs(document['ok'], False)

    def test_a_cut_last_line_is_reported(self):
        self.log().append(event())
        self.damage(b'{"schemaVersion":"1.0","eve')
        result = self.go('events', 'list')
        self.assertEqual(result.code, 1)
        self.assertIn('line 2 is damaged (truncated_tail)', result.out)

    def test_hostile_bytes_in_a_damaged_line_never_reach_the_terminal(self):
        self.log().append(event())
        self.damage(b'\x1b[2J\x1b]8;;http://evil.example\x07\r\xff\xfe not json \xe2\x80\xae\n')
        for extra in ((), ('--json',)):
            result = self.go('events', 'list', *extra)
            self.assertEqual(result.code, 1)
            self.assertSafe(result.out)
            self.assertSafe(result.err)

    def test_hostile_event_fields_are_escaped(self):
        hostile = {'ok': True, 'exists': True, 'total': 1, 'offset': 0, 'limit': 100, 'more': False, 'truncatedTail': False,
                   'lines': [{'line': 1, 'ok': True}],
                   'events': [{'line': 1, 'event': {'eventId': ESCAPES, 'time': ESCAPES, 'type': ESCAPES, 'taskId': ESCAPES, 'source': ESCAPES,
                                                    'moduleId': ESCAPES, 'data': {'checkId': ESCAPES, 'status': ESCAPES, 'other': [ESCAPES]}}}]}
        with mock.patch.object(EventLog, 'read', return_value=hostile):
            for extra in ((), ('--json',)):
                result = self.go('events', 'list', *extra)
                self.assertEqual(result.code, 0, result.err)
                self.assertSafe(result.out)
                if extra:
                    result.out.encode('ascii')

    def test_a_damage_reason_from_outside_is_not_trusted(self):
        report = {'ok': False, 'exists': True, 'total': 2, 'offset': 0, 'limit': 100, 'more': False, 'truncatedTail': False,
                  'lines': [{'line': 1, 'ok': False, 'reason': ESCAPES}, {'line': 2, 'ok': False, 'reason': 'invalid'}], 'events': []}
        with mock.patch.object(EventLog, 'read', return_value=report):
            for extra in ((), ('--json',)):
                result = self.go('events', 'list', *extra)
                self.assertEqual(result.code, 1)
                self.assertSafe(result.out)
            self.assertIn('line 1 is damaged (unknown).', self.go('events', 'list').out)
            self.assertEqual(json.loads(self.go('events', 'list', '--json').out)['damaged'], [{'line': 1, 'reason': 'unknown'}, {'line': 2, 'reason': 'invalid'}])

    def test_an_event_of_an_unknown_type_still_lists_with_its_first_fields(self):
        odd = {'ok': True, 'exists': True, 'total': 1, 'offset': 0, 'limit': 100, 'more': False, 'truncatedTail': False, 'lines': [{'line': 1, 'ok': True}],
               'events': [{'line': 1, 'event': {'eventId': 'e1', 'time': 't', 'type': 'new_type', 'taskId': None, 'source': 'app', 'moduleId': 'm',
                                                'data': {'b': 2, 'a': 1, 'c': {'x': 1}}}}]}
        with mock.patch.object(EventLog, 'read', return_value=odd):
            out = self.go('events', 'list').out
        self.assertIn('new_type', out)
        self.assertIn('a=1 b=2', out)
        self.assertIn('task=none', out)

    def test_an_event_with_a_wrong_shape_does_not_crash_the_list(self):
        odd = {'ok': True, 'exists': True, 'total': 1, 'offset': 0, 'limit': 100, 'more': False, 'truncatedTail': False, 'lines': [{'line': 1, 'ok': True}],
               'events': [{'line': 1, 'event': 'not an object'}, {'line': 2, 'event': {'data': 5, 'type': 7}}]}
        with mock.patch.object(EventLog, 'read', return_value=odd):
            result = self.go('events', 'list')
        self.assertEqual(result.code, 0, result.err)

    def test_a_log_over_its_limits_is_a_failure_with_a_fixed_message(self):
        folder = self.data / '_events'
        folder.mkdir(mode=0o700)
        (folder / 'events.jsonl').write_bytes(b'\n' * 50001)
        result = self.go('events', 'list')
        self.assertEqual(result.code, 1)
        self.assertIn('code: log_corrupt', result.err)

    # --- export ---

    def test_export_writes_the_exact_bytes_to_standard_output(self):
        self.log().append(event('task-opened'))
        self.log().append(event('check-result-passed'))
        out = binary_out()
        result = self.go('events', 'export', out=out)
        self.assertEqual(result.code, 0)
        out.flush()
        self.assertEqual(out.buffer.getvalue(), (self.data / '_events' / 'events.jsonl').read_bytes())

    def test_export_of_a_damaged_log_to_standard_output_keeps_every_byte(self):
        self.log().append(event())
        self.damage(b'\xff\xfe not utf-8 \x00\n')
        out = binary_out()
        result = self.go('events', 'export', out=out)
        self.assertEqual(result.code, 1)
        out.flush()
        self.assertEqual(out.buffer.getvalue(), (self.data / '_events' / 'events.jsonl').read_bytes())

    def test_export_to_a_text_stream_without_a_buffer_still_gives_the_lines(self):
        item = event()
        self.log().append(item)
        result = self.go('events', 'export')
        self.assertEqual(result.code, 0)
        self.assertEqual(json.loads(result.out.strip())['eventId'], item['eventId'])

    def test_export_to_a_terminal_is_escaped_and_says_so(self):
        self.log().append(event())
        self.damage(b'\x1b[2J\x1b]8;;http://evil.example\x07 bad \xe2\x80\xae\n')
        result = self.go('events', 'export', out=FakeOut(tty=True))
        self.assertEqual(result.code, 1)
        self.assertSafe(result.out)
        self.assertIn('escaped', result.err)

    def test_export_to_a_file_is_private_and_exact(self):
        self.log().append(event())
        target = self.base / 'export.jsonl'
        result = self.go('events', 'export', '--output', str(target))
        self.assertEqual((result.code, result.err), (0, ''))
        self.assertEqual(target.read_bytes(), (self.data / '_events' / 'events.jsonl').read_bytes())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(result.out, f'Wrote {len(target.read_bytes())} bytes to {target}.\n')

    def test_export_refuses_to_overwrite_an_existing_file(self):
        self.log().append(event())
        target = self.base / 'export.jsonl'
        target.write_text('precious')
        result = self.go('events', 'export', '--output', str(target))
        self.assertEqual(result.code, 3)
        self.assertIn('code: output_exists', result.err)
        self.assertEqual(target.read_text(), 'precious')

    def test_export_refuses_a_symlink_even_a_dangling_one(self):
        self.log().append(event())
        keep = self.base / 'keep.txt'
        keep.write_text('precious')
        for name, destination in (('link-to-file', keep), ('dangling', self.base / 'nowhere')):
            link = self.base / name
            link.symlink_to(destination)
            result = self.go('events', 'export', '--output', str(link))
            self.assertEqual(result.code, 3, name)
        self.assertEqual(keep.read_text(), 'precious')
        self.assertFalse((self.base / 'nowhere').exists())

    def test_a_write_that_fails_removes_the_file_it_created(self):
        self.log().append(event())
        target = self.base / 'export.jsonl'
        with mock.patch('os.fsync', side_effect=OSError('disk full')):
            result = self.go('events', 'export', '--output', str(target))
        self.assertEqual(result.code, 1)
        self.assertIn('code: output_unwritable', result.err)
        self.assertNotIn('disk full', result.err)
        self.assertFalse(target.exists())

    def test_export_to_a_folder_that_does_not_exist_is_a_failure(self):
        self.log().append(event())
        result = self.go('events', 'export', '--output', str(self.base / 'no-folder' / 'x.jsonl'))
        self.assertEqual(result.code, 1)
        self.assertIn('code: output_unwritable', result.err)
        self.assertNotIn('Errno', result.err)

    def test_export_with_no_log_fails_and_creates_no_file(self):
        target = self.base / 'export.jsonl'
        result = self.go('events', 'export', '--output', str(target))
        self.assertEqual(result.code, 1)
        self.assertIn('code: log_missing', result.err)
        self.assertFalse(target.exists())

    def test_export_of_a_damaged_log_writes_everything_and_exits_one(self):
        self.log().append(event())
        self.damage()
        target = self.base / 'export.jsonl'
        result = self.go('events', 'export', '--output', str(target))
        self.assertEqual(result.code, 1)
        self.assertIn('code: log_damaged', result.err)
        self.assertEqual(target.read_bytes(), (self.data / '_events' / 'events.jsonl').read_bytes())

    def test_an_empty_output_name_is_a_usage_error(self):
        self.assertEqual(self.go('events', 'export', '--output', '').code, 2)

    def test_a_file_name_with_hidden_characters_is_shown_escaped(self):
        self.log().append(event())
        target = self.base / 'ex\u202eport.jsonl'
        result = self.go('events', 'export', '--output', str(target))
        self.assertEqual(result.code, 0, result.err)
        self.assertSafe(result.out)
        self.assertIn('ex<U+202E>port.jsonl', result.out)

    def test_export_to_a_unicode_file_name_works(self):
        self.log().append(event())
        target = self.base / 'ex\u00f3rt-\u65e5\u672c.jsonl'
        self.assertEqual(self.go('events', 'export', '--output', str(target)).code, 0)
        self.assertTrue(target.exists())

    # --- delete ---

    def test_delete_without_confirm_is_refused_and_removes_nothing(self):
        self.log().append(event())
        self.log().save_reflection('I learned a thing.')
        before = tree(self.data)
        result = self.go('events', 'delete')
        self.assertEqual(result.code, 3)
        self.assertIn('code: confirmation_required', result.err)
        self.assertIn('--confirm', result.err)
        self.assertEqual(tree(self.data), before)

    def test_delete_without_confirm_does_not_even_open_the_log(self):
        with mock.patch.object(EventLog, 'delete', side_effect=AssertionError('delete was called')), \
                mock.patch.object(EventLog, '__init__', side_effect=AssertionError('the log was opened')):
            self.assertEqual(self.go('events', 'delete').code, 3)

    def test_delete_with_confirm_removes_the_log_and_the_reflections_and_says_what_it_removed(self):
        self.log().append(event())
        self.log().save_reflection('One.')
        self.log().save_reflection('Two.')
        result = self.go('events', 'delete', '--confirm')
        self.assertEqual((result.code, result.err), (0, ''))
        self.assertIn('Deleted the event log.', result.out)
        self.assertIn('Deleted 2 reflections.', result.out)
        self.assertFalse((self.data / '_events' / 'events.jsonl').exists())
        self.assertEqual(list((self.data / '_events' / 'reflections').iterdir()), [])
        self.assertIn('cannot be undone', result.out)

    def test_delete_passes_exactly_true_to_the_log(self):
        with mock.patch.object(EventLog, 'delete', return_value={'deleted': True, 'logRemoved': True, 'reflectionsRemoved': 0}) as call:
            self.go('events', 'delete', '--confirm')
        call.assert_called_once_with(True)

    def test_delete_leaves_the_lessons_and_records_alone(self):
        self.import_module()
        self.log().append(event())
        lessons = tree(self.data / '_lessons')
        self.go('events', 'delete', '--confirm')
        self.assertEqual(tree(self.data / '_lessons'), lessons)

    def test_delete_when_there_is_nothing_says_so_and_succeeds(self):
        result = self.go('events', 'delete', '--confirm')
        self.assertEqual(result.code, 0)
        self.assertIn('There is no event log. Nothing was removed.', result.out)

    def test_a_second_delete_is_harmless(self):
        self.log().append(event())
        self.assertEqual(self.go('events', 'delete', '--confirm').code, 0)
        second = self.go('events', 'delete', '--confirm')
        self.assertEqual(second.code, 0)
        self.assertIn('Nothing was removed', second.out)

    def test_a_delete_that_fails_is_exit_one_with_a_fixed_message(self):
        self.log().append(event())
        (self.data / '_events').chmod(0o500)
        self.addCleanup((self.data / '_events').chmod, 0o700)
        if os.access(self.data / '_events', os.W_OK):
            self.skipTest('this account can write to a read-only folder')
        result = self.go('events', 'delete', '--confirm')
        self.assertEqual(result.code, 1)
        self.assertIn('code: delete_failed', result.err)
        self.assertNotIn('Errno', result.err)


class SharedFolderTests(CliCase):
    def test_reading_while_another_writer_appends_never_shows_a_half_written_line(self):
        log = EventLog(self.data)
        log.append(event())
        stop = threading.Event()
        written = []

        def writer():
            while not stop.is_set() and len(written) < 150:
                item = event('check-result-passed')
                log.append(item)
                written.append(item['eventId'])

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            seen = 0
            for _ in range(25):
                result = self.go('events', 'list', '--json', '--limit', '5000')
                self.assertEqual(result.code, 0, result.err)
                document = json.loads(result.out)
                self.assertIs(document['ok'], True)
                self.assertGreaterEqual(document['total'], seen)
                seen = document['total']
        finally:
            stop.set()
            thread.join()
        self.assertEqual(self.go('events', 'list', '--json', '--limit', '5000').code, 0)

    def test_the_tool_works_while_a_hub_runs_on_the_same_folder_and_the_hub_sees_a_delete(self):
        hub = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        hub.daemon_threads = True
        hub.data_dir = self.data
        thread = threading.Thread(target=hub.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (hub.shutdown(), hub.server_close(), thread.join()))
        port = hub.server_port

        def call(method, path, body=None):
            connection = HTTPConnection('127.0.0.1', port, timeout=10)
            connection.request('GET', '/api/checks/session')
            token = json.loads(connection.getresponse().read())['token']
            connection.close()
            headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{port}', TOKEN_HEADER: token}
            connection = HTTPConnection('127.0.0.1', port, timeout=10)
            connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read() or b'null')
            connection.close()
            return response.status, payload

        self.import_module()
        item = event('task-opened', source='student')
        self.assertEqual(call('POST', '/api/events', {'event': item})[0], 201)
        listed = self.go('events', 'list', '--json')
        self.assertEqual(json.loads(listed.out)['events'][0]['event']['eventId'], item['eventId'])
        self.assertEqual(self.go('modules').code, 0)
        self.assertEqual(self.go('events', 'delete', '--confirm').code, 0)
        status, report = call('GET', '/api/events')
        self.assertEqual(status, 200)
        self.assertEqual((report['exists'], report['total']), (False, 0))
        again = event('task-opened', source='student')
        self.assertEqual(call('POST', '/api/events', {'event': again})[0], 201)
        self.assertEqual(json.loads(self.go('events', 'list', '--json').out)['total'], 1)

    def test_exports_run_while_events_are_being_appended(self):
        log = EventLog(self.data)
        log.append(event())
        results = []

        def read_many():
            for _ in range(10):
                results.append(self.go('events', 'export', '--output', str(self.base / f'x-{len(results)}-{threading.get_ident()}.jsonl')).code)

        threads = [threading.Thread(target=read_many) for _ in range(3)]
        for thread in threads:
            thread.start()
        for _ in range(30):
            log.append(event())
        for thread in threads:
            thread.join()
        self.assertEqual(set(results), {0})


# ---------------------------------------------------------------------------
# The launcher and the version
# ---------------------------------------------------------------------------

class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(os.path.realpath(self.temp.name)) / 'data'
        self.data.mkdir()

    def launch(self, *argv, command=None, env=None, cwd=None):
        base = {'PATH': os.environ.get('PATH', ''), 'LC_ALL': 'en_US.UTF-8', 'LANG': 'en_US.UTF-8', 'HOME': self.temp.name}
        return subprocess.run(command or [str(LAUNCHER), *argv], capture_output=True, text=True, timeout=60, cwd=cwd or self.temp.name, env=dict(base, **(env or {})))

    def test_the_launcher_is_an_executable_with_the_right_first_line(self):
        self.assertTrue(os.access(LAUNCHER, os.X_OK))
        self.assertEqual(LAUNCHER.read_text().splitlines()[0], '#!/usr/bin/env python3')
        self.assertEqual(LAUNCHER.stat().st_mode & stat.S_IXUSR, stat.S_IXUSR)

    def test_the_launcher_runs_from_any_folder(self):
        done = self.launch('version', cwd='/')
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertRegex(done.stdout, r'^schola 0\.1\.0\n$')

    def test_the_launcher_works_through_a_symbolic_link(self):
        link = Path(self.temp.name) / 'schola-link'
        link.symlink_to(LAUNCHER)
        done = self.launch(command=[str(link), 'version'])
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_module_form_gives_the_same_answer(self):
        launched = self.launch('modules', '--data-dir', str(self.data))
        module = self.launch(command=[sys.executable, '-m', 'schola_cli', 'modules', '--data-dir', str(self.data)], cwd=str(ROOT))
        self.assertEqual((launched.returncode, launched.stdout), (module.returncode, module.stdout))
        self.assertIn('No modules are imported yet', module.stdout)

    def test_the_exit_codes_reach_the_shell(self):
        self.assertEqual(self.launch('nonsense').returncode, 2)
        self.assertEqual(self.launch('events', 'delete', '--data-dir', str(self.data)).returncode, 3)
        self.assertEqual(self.launch('module', 'show', 'nope', '--data-dir', str(self.data)).returncode, 1)
        self.assertEqual(self.launch('events', 'list', '--data-dir', str(self.data)).returncode, 0)

    def test_the_environment_variable_sets_the_data_folder(self):
        LessonStore(self.data).import_files([('m.json', json.dumps(module_document()))])
        done = self.launch('modules', env={'SCHOLA_DATA_DIR': str(self.data)})
        self.assertIn('hello-module', done.stdout)
        flagged = self.launch('modules', '--data-dir', str(self.data / 'empty'), env={'SCHOLA_DATA_DIR': str(self.data)})
        self.assertIn('No modules', flagged.stdout)

    def test_a_terminal_that_cannot_show_unicode_gets_escapes_and_not_a_crash(self):
        document = module_document()
        document['title'] = 'F\u00fatbol \u65e5\u672c\u8a9e'
        LessonStore(self.data).import_files([('m.json', json.dumps(document))])
        done = self.launch('modules', '--data-dir', str(self.data), env={'LC_ALL': 'C', 'LANG': 'C', 'PYTHONIOENCODING': 'ascii'})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('Traceback', done.stderr)
        self.assertIn('hello-module', done.stdout)

    def test_a_closed_pipe_ends_the_tool_quietly(self):
        LessonStore(self.data).import_files([('m.json', json.dumps(module_document()))])
        process = subprocess.Popen([str(LAUNCHER), 'modules', '--data-dir', str(self.data)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdout.close()
        _out, err = process.communicate(timeout=30)
        self.assertNotIn(b'Traceback', err)

    def test_the_version_command(self):
        result = Result(*(lambda r: (r.returncode, r.stdout, r.stderr))(self.launch('version')))
        self.assertEqual(result.code, 0)
        package = json.loads((ROOT / 'package.json').read_text())
        self.assertEqual(result.out, f'schola {package["version"]}\n')
        self.assertEqual(json.loads(self.launch('version', '--json').stdout), {'name': 'schola', 'version': package['version']})


if __name__ == '__main__':
    unittest.main()
