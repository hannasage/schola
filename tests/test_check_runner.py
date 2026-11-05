"""The check runner interface and the built-in checks: file-exists, text-match, json-valid.

The core of these tests is workspace confinement. A check path comes from lesson
content, so every way out of the workspace must fail without a read.
"""
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import check_runner
import check_workspace
from check_runner import Limits, run

SECRET = 'outside-secret-text'


def automated(runner, check_id='c1'):
    return {'id': check_id, 'kind': 'automated', 'description': 'A check.', 'runner': runner}


def tree(directory):
    """Every path under a folder with its type and bytes, so a test can prove nothing changed."""
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        relative = str(path.relative_to(directory))
        if path.is_symlink():
            result[relative] = ('link', os.readlink(path))
        elif path.is_file():
            result[relative] = ('file', path.read_bytes())
        else:
            result[relative] = ('other',)
    return result


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(os.path.realpath(self.temp.name))
        self.work = base / 'work'
        self.work.mkdir()
        self.outside = base / 'outside'
        self.outside.mkdir()
        (self.outside / 'secret.txt').write_text(SECRET)
        (self.work / 'notes.txt').write_text('hello world\nsecond line hello\n')
        (self.work / 'src').mkdir()
        (self.work / 'src' / 'app.py').write_text('print("hi")\n')
        (self.work / 'data.json').write_text('{"name": "a", "items": [1, 2]}')

    def check(self, runner, limits=None):
        return run(automated(runner), self.work, limits)


class InterfaceTests(Workspace):
    def test_a_result_has_a_status_evidence_and_log(self):
        result = self.check({'type': 'file-exists', 'path': 'notes.txt'})
        self.assertEqual(set(result), {'status', 'evidence', 'log'})
        self.assertIn(result['status'], check_runner.STATUSES)
        self.assertIsInstance(result['evidence'], dict)
        self.assertIsInstance(result['log'], list)
        json.dumps(result)

    def test_statuses_are_the_four_agreed_words(self):
        self.assertEqual(check_runner.STATUSES, ('passed', 'failed', 'error', 'skipped'))

    def test_an_unknown_runner_type_is_skipped_never_passed(self):
        result = self.check({'type': 'quantum-check', 'path': 'notes.txt'})
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['evidence']['reason'], 'unknown_runner_type')
        self.assertIn('self-report', ' '.join(result['log']).lower())

    def test_a_self_reported_check_is_skipped(self):
        result = run({'id': 'c1', 'kind': 'self-reported', 'description': 'Say so.'}, self.work)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['evidence']['reason'], 'self_reported')

    def test_bad_check_shapes_are_errors_not_exceptions(self):
        for check in (None, 5, [], {}, {'kind': 'automated'}, {'kind': 'automated', 'runner': None},
                      {'kind': 'automated', 'runner': {}}, {'kind': 'automated', 'runner': {'type': 5}},
                      {'kind': 'automated', 'runner': 'file-exists'}):
            with self.subTest(check=check):
                result = run(check, self.work)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['evidence']['reason'], 'invalid_check')

    def test_a_workspace_that_is_missing_is_an_error(self):
        for workspace in (None, '', self.work / 'nope', self.work / 'notes.txt', 5):
            with self.subTest(workspace=workspace):
                result = run(automated({'type': 'file-exists', 'path': 'notes.txt'}), workspace)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['evidence']['reason'], 'workspace_unavailable')

    def test_an_adapter_that_raises_gives_an_error_without_leaking_detail(self):
        class Broken:
            types = ('broken-check',)

            def run(self, check, workspace, limits):
                raise RuntimeError('/private/path/secret ' + SECRET)

        registry = check_runner.Registry()
        registry.register(Broken())
        result = run(automated({'type': 'broken-check'}), self.work, registry=registry)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['evidence']['reason'], 'adapter_failed')
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn('/private/path', json.dumps(result))

    def test_an_adapter_with_a_bad_status_or_shape_cannot_pass(self):
        for bad in ({'status': 'ok', 'evidence': {}, 'log': []}, {'status': 'passed'}, 'passed', None,
                    {'status': 'passed', 'evidence': 'x', 'log': []}, {'status': 'passed', 'evidence': {}, 'log': 'x'}):
            class Odd:
                types = ('odd-check',)

                def run(self, check, workspace, limits, bad=bad):
                    return bad

            registry = check_runner.Registry()
            registry.register(Odd())
            with self.subTest(bad=bad):
                result = run(automated({'type': 'odd-check'}), self.work, registry=registry)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['evidence']['reason'], 'adapter_failed')

    def test_evidence_and_log_are_bounded(self):
        class Loud:
            types = ('loud-check',)

            def run(self, check, workspace, limits):
                return {'status': 'passed',
                        'evidence': {f'k{i}': 'x' * 100000 for i in range(200)} | {'nested': {'a': 1}, 'list': ['y' * 5000] * 100, 'n': 3, 'b': True, 'z': None},
                        'log': ['line ' + 'x' * 100000] * 500}

        registry = check_runner.Registry()
        registry.register(Loud())
        limits = Limits()
        result = run(automated({'type': 'loud-check'}), self.work, limits, registry=registry)
        self.assertEqual(result['status'], 'passed')
        self.assertLessEqual(len(result['evidence']), limits.max_evidence_keys)
        for value in result['evidence'].values():
            if isinstance(value, str):
                self.assertLessEqual(len(value), limits.max_evidence_chars)
            if isinstance(value, list):
                self.assertLessEqual(len(value), limits.max_evidence_items)
        self.assertLessEqual(len(result['log']), limits.max_log_lines)
        self.assertTrue(all(len(line) <= limits.max_log_chars for line in result['log']))
        self.assertLess(len(json.dumps(result)), 20000)

    def test_control_characters_are_removed_from_text(self):
        class Rude:
            types = ('rude-check',)

            def run(self, check, workspace, limits):
                return {'status': 'failed', 'evidence': {'note': 'a\x00b\x1b[31mc\u202ed'}, 'log': ['x\x00y']}

        registry = check_runner.Registry()
        registry.register(Rude())
        result = run(automated({'type': 'rude-check'}), self.work, registry=registry)
        self.assertNotIn('\x00', json.dumps(result))
        self.assertNotIn('\x1b', result['evidence']['note'])
        self.assertNotIn('\u202e', result['evidence']['note'])

    def test_the_registry_refuses_a_duplicate_type_and_lists_types(self):
        registry = check_runner.default_registry()
        self.assertEqual({'file-exists', 'text-match', 'json-valid'} <= set(registry.types()), True)

        class Again:
            types = ('file-exists',)

            def run(self, check, workspace, limits):
                return {'status': 'passed', 'evidence': {}, 'log': []}

        with self.assertRaises(ValueError):
            registry.register(Again())

    def test_running_a_built_in_check_never_changes_the_workspace(self):
        before = tree(self.work)
        for runner in ({'type': 'file-exists', 'path': 'notes.txt'}, {'type': 'text-match', 'path': 'notes.txt', 'text': 'hello'},
                       {'type': 'json-valid', 'path': 'data.json'}, {'type': 'file-exists', 'path': '../outside/secret.txt'}):
            self.check(runner)
        self.assertEqual(tree(self.work), before)

    def test_unknown_limits_are_refused(self):
        with self.assertRaises(TypeError):
            Limits(not_a_limit=1)
        self.assertEqual(Limits().replace(regex_timeout=0.25).regex_timeout, 0.25)


class FileExistsTests(Workspace):
    def test_a_present_file_passes(self):
        result = self.check({'type': 'file-exists', 'path': 'src/app.py'})
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['evidence']['path'], 'src/app.py')

    def test_a_missing_file_fails(self):
        result = self.check({'type': 'file-exists', 'path': 'src/missing.py'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['evidence']['reason'], 'not_found')

    def test_a_missing_parent_fails(self):
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'nope/deeper/file.txt'})['status'], 'failed')
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'notes.txt/child'})['status'], 'failed')

    def test_kind_decides_what_counts(self):
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'src'})['status'], 'failed')
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'src', 'kind': 'directory'})['status'], 'passed')
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'src', 'kind': 'any'})['status'], 'passed')
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'notes.txt', 'kind': 'directory'})['status'], 'failed')
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'notes.txt', 'kind': 'any'})['status'], 'passed')
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'notes.txt', 'kind': 'socket'})['evidence']['reason'], 'invalid_descriptor')

    def test_non_empty(self):
        (self.work / 'empty.txt').write_text('')
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'empty.txt'})['status'], 'passed')
        result = self.check({'type': 'file-exists', 'path': 'empty.txt', 'nonEmpty': True})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['evidence']['reason'], 'empty')
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'notes.txt', 'nonEmpty': True})['status'], 'passed')

    def test_a_large_file_still_exists(self):
        with open(self.work / 'big.bin', 'wb') as handle:
            handle.truncate(50_000_000)
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'big.bin'})['status'], 'passed')

    def test_descriptor_mistakes_are_errors(self):
        for runner in ({'type': 'file-exists'}, {'type': 'file-exists', 'path': 5}, {'type': 'file-exists', 'path': ['a']},
                       {'type': 'file-exists', 'path': 'a', 'extra': 'x'}, {'type': 'file-exists', 'path': 'a', 'nonEmpty': 'yes'},
                       {'type': 'file-exists', 'path': 'a', 'nonEmpty': 1}):
            with self.subTest(runner=runner):
                result = self.check(runner)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['evidence']['reason'], 'invalid_descriptor')

    def test_unicode_and_odd_names(self):
        for name in ('caf\u00e9.txt', 'emoji-\U0001f600.txt', 'with space.txt', "quote'name.txt", 'sql;--.txt', 'percent%20.txt'):
            (self.work / name).write_text('x')
            with self.subTest(name=name):
                self.assertEqual(self.check({'type': 'file-exists', 'path': name})['status'], 'passed')


class ConfinementTests(Workspace):
    """Every way out of the workspace fails, for every built-in check type."""

    def kinds(self, path):
        return [{'type': 'file-exists', 'path': path, 'kind': 'any'}, {'type': 'text-match', 'path': path, 'text': SECRET},
                {'type': 'json-valid', 'path': path}]

    def assertRefused(self, path, reasons=('unsafe_path',)):
        for runner in self.kinds(path):
            result = self.check(runner)
            self.assertEqual(result['status'], 'error', (runner, result))
            self.assertIn(result['evidence']['reason'], reasons, (runner, result))
            self.assertNotIn(SECRET, json.dumps(result))

    def test_parent_parts_are_refused(self):
        for path in ('../outside/secret.txt', 'src/../../outside/secret.txt', '..', 'src/..', './../outside/secret.txt', 'a/../..'):
            with self.subTest(path=path):
                self.assertRefused(path)

    def test_absolute_paths_are_refused(self):
        for path in (str(self.outside / 'secret.txt'), '/etc/passwd', '//etc/passwd', '/'):
            with self.subTest(path=path):
                self.assertRefused(path)

    def test_nul_and_control_bytes_are_refused(self):
        for path in ('notes.txt\x00.png', '\x00', 'a\x00/b', 'a\nb', 'a\x7fb', 'a\x1bb'):
            with self.subTest(path=repr(path)):
                self.assertRefused(path)

    def test_a_path_that_is_too_long_is_refused(self):
        for path in ('x' * 5000, 'a/' * 100 + 'f', 'y' * 300, 'z' * 200 + '/' + 'z' * 200 + '/' + 'z' * 200 + '/' + 'z' * 200 + '/' + 'z' * 200 + '/' + 'z' * 200):
            with self.subTest(length=len(path)):
                self.assertRefused(path)

    def test_odd_forms_are_refused(self):
        for path in ('', '.', './', '/.', 'C:/Windows/win.ini', 'c:secret', 'a\\b', '..\\outside\\secret.txt', None, 5, ['a'], b'notes.txt'):
            with self.subTest(path=repr(path)):
                for runner in self.kinds(path):
                    result = self.check(runner)
                    self.assertIn(result['status'], ('error',), (runner, result))

    def test_a_link_to_a_file_outside_is_refused_and_never_read(self):
        os.symlink(self.outside / 'secret.txt', self.work / 'link.txt')
        self.assertRefused('link.txt')

    def test_a_link_to_a_folder_outside_is_refused(self):
        os.symlink(self.outside, self.work / 'dirlink')
        self.assertRefused('dirlink/secret.txt')
        self.assertRefused('dirlink', ('unsafe_path',))

    def test_a_relative_link_that_climbs_out_is_refused(self):
        os.symlink('../outside/secret.txt', self.work / 'climb.txt')
        self.assertRefused('climb.txt')

    def test_a_chain_of_links_that_ends_outside_is_refused(self):
        os.symlink(self.outside / 'secret.txt', self.work / 'hop2')
        os.symlink('hop2', self.work / 'hop1')
        self.assertRefused('hop1')

    def test_a_dangling_link_that_points_outside_is_refused(self):
        os.symlink(self.outside / 'nothing-here', self.work / 'dangling')
        self.assertRefused('dangling')

    def test_a_link_to_a_device_is_refused(self):
        os.symlink('/dev/null', self.work / 'null')
        self.assertRefused('null')

    def test_a_link_loop_is_an_error_and_not_a_crash(self):
        os.symlink('b', self.work / 'a')
        os.symlink('a', self.work / 'b')
        for runner in self.kinds('a'):
            self.assertEqual(self.check(runner)['status'], 'error')

    def test_a_link_that_stays_inside_is_allowed(self):
        os.symlink('notes.txt', self.work / 'alias.txt')
        os.symlink('src', self.work / 'srclink')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'alias.txt', 'text': 'hello world'})['status'], 'passed')
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'srclink/app.py'})['status'], 'passed')

    def test_a_dangling_link_inside_is_just_missing(self):
        os.symlink('gone.txt', self.work / 'dangling-inside')
        self.assertEqual(self.check({'type': 'file-exists', 'path': 'dangling-inside'})['status'], 'failed')

    def test_a_workspace_that_is_itself_a_link_works(self):
        os.symlink(self.work, Path(self.temp.name) / 'workspace-link')
        result = run(automated({'type': 'file-exists', 'path': 'notes.txt'}), Path(self.temp.name) / 'workspace-link')
        self.assertEqual(result['status'], 'passed')

    def test_a_link_swapped_in_after_the_path_check_is_still_refused(self):
        """The window between realpath and open. O_NOFOLLOW must close it."""
        target = self.work / 'swap.txt'
        target.write_text('plain')
        real_resolve = check_workspace._resolve

        def resolve_then_swap(root, parts):
            found = real_resolve(root, parts)
            target.unlink()
            os.symlink(self.outside / 'secret.txt', target)
            return found

        with mock.patch.object(check_workspace, '_resolve', resolve_then_swap):
            result = self.check({'type': 'text-match', 'path': 'swap.txt', 'text': SECRET})
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['evidence']['reason'], 'unsafe_path')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_a_folder_swapped_for_a_link_after_the_path_check_is_still_refused(self):
        (self.work / 'dir').mkdir()
        (self.work / 'dir' / 'f.txt').write_text('plain')
        real_resolve = check_workspace._resolve

        def resolve_then_swap(root, parts):
            found = real_resolve(root, parts)
            shutil.rmtree(self.work / 'dir')
            os.symlink(self.outside, self.work / 'dir')
            return found

        (self.outside / 'f.txt').write_text(SECRET)
        with mock.patch.object(check_workspace, '_resolve', resolve_then_swap):
            result = self.check({'type': 'text-match', 'path': 'dir/f.txt', 'text': SECRET})
        self.assertEqual(result['status'], 'error')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_the_descriptor_check_catches_a_file_that_lives_outside(self):
        """If the operating system says the open file is outside, the read is refused."""
        outside_fd_path = str(self.outside / 'secret.txt')
        with mock.patch.object(check_workspace, '_fd_path', return_value=outside_fd_path):
            result = self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'hello'})
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['evidence']['reason'], 'unsafe_path')

    def test_a_named_pipe_is_refused_and_does_not_hang(self):
        os.mkfifo(self.work / 'pipe')
        signal.alarm(10)
        try:
            for runner in self.kinds('pipe'):
                started = time.monotonic()
                result = self.check(runner)
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(result['status'], 'error', runner)
                self.assertEqual(result['evidence']['reason'], 'not_regular')
        finally:
            signal.alarm(0)

    def test_a_link_to_a_named_pipe_inside_is_refused_and_does_not_hang(self):
        os.mkfifo(self.work / 'pipe')
        os.symlink('pipe', self.work / 'pipe-link')
        signal.alarm(10)
        try:
            self.assertEqual(self.check({'type': 'text-match', 'path': 'pipe-link', 'text': 'x'})['evidence']['reason'], 'not_regular')
        finally:
            signal.alarm(0)

    def test_a_socket_file_is_refused(self):
        path = self.work / 's.sock'
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(path))
        self.assertRefused('s.sock', ('not_regular',))

    def test_a_device_file_is_refused_without_being_opened(self):
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == 'notes.txt' and kwargs.get('dir_fd') is not None:
                values = list(info)
                values[0] = stat.S_IFCHR | 0o600
                return os.stat_result(values)
            return info

        opened = []
        real_open = os.open

        def spy_open(path, *args, **kwargs):
            opened.append(path)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(check_workspace.os, 'stat', fake_stat), mock.patch.object(check_workspace.os, 'open', spy_open):
            result = self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'hello'})
        self.assertEqual(result['evidence']['reason'], 'not_regular')
        self.assertNotIn('notes.txt', opened)

    def test_a_file_over_the_size_cap_is_refused_and_not_read(self):
        with open(self.work / 'huge.txt', 'wb') as handle:
            handle.truncate(check_workspace.DEFAULT_MAX_FILE_BYTES + 1)
        for runner in ({'type': 'text-match', 'path': 'huge.txt', 'text': 'x'}, {'type': 'json-valid', 'path': 'huge.txt'}):
            result = self.check(runner)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(result['evidence']['reason'], 'file_too_large')

    def test_a_file_over_the_size_cap_is_refused_before_any_byte_is_read(self):
        with open(self.work / 'huge.txt', 'wb') as handle:
            handle.truncate(check_workspace.DEFAULT_MAX_FILE_BYTES + 1)
        reads = []
        with mock.patch.object(check_workspace.os, 'read', lambda *args: reads.append(args) or b''):
            result = self.check({'type': 'text-match', 'path': 'huge.txt', 'text': 'x'})
        self.assertEqual(result['evidence']['reason'], 'file_too_large')
        self.assertEqual(reads, [])

    def test_a_file_exactly_at_the_cap_is_read(self):
        (self.work / 'edge.txt').write_text('a' * (check_workspace.DEFAULT_MAX_FILE_BYTES - 1) + 'Z')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'edge.txt', 'text': 'aZ'})['status'], 'passed')

    def test_a_smaller_limit_applies(self):
        result = self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'hello'}, Limits(max_file_bytes=10))
        self.assertEqual(result['evidence']['reason'], 'file_too_large')

    def test_a_file_that_grows_while_it_is_read_is_still_capped(self):
        """The size seen by fstat can be stale. The read loop stops at the cap on its own."""
        (self.work / 'grow.txt').write_text('abc')
        chunks = []

        def endless(fd, count):
            chunks.append(count)
            return b'x' * count

        with mock.patch.object(check_workspace.os, 'read', endless):
            with self.assertRaises(check_workspace.WorkspaceError) as caught:
                check_workspace.read_file(self.work, 'grow.txt', max_file_bytes=100)
        self.assertEqual(caught.exception.code, 'file_too_large')
        self.assertLessEqual(sum(chunks), 101)

    def test_an_unreadable_file_is_an_error_without_a_path(self):
        if os.geteuid() == 0:
            self.skipTest('root can read everything')
        (self.work / 'locked.txt').write_text('x')
        os.chmod(self.work / 'locked.txt', 0)
        self.addCleanup(os.chmod, self.work / 'locked.txt', 0o600)
        result = self.check({'type': 'text-match', 'path': 'locked.txt', 'text': 'x'})
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['evidence']['reason'], 'unreadable')
        self.assertNotIn(str(self.work), json.dumps(result))

    def test_no_message_holds_a_local_path(self):
        os.symlink(self.outside / 'secret.txt', self.work / 'link.txt')
        for path in ('link.txt', '../outside/secret.txt', 'missing.txt'):
            for runner in self.kinds(path):
                text = json.dumps(self.check(runner))
                self.assertNotIn(self.temp.name, text)
                self.assertNotIn(str(self.outside), text)


class TextMatchTests(Workspace):
    def test_a_literal_match_passes_and_a_miss_fails(self):
        self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'hello world'})['status'], 'passed')
        result = self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'absent'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['evidence']['matches'], 0)

    def test_text_is_literal_and_never_a_pattern(self):
        (self.work / 'dots.txt').write_text('a.c and (x+) and [y]')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'dots.txt', 'text': 'a.c'})['status'], 'passed')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'dots.txt', 'text': 'a+c'})['status'], 'failed')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'dots.txt', 'text': '(x+)'})['status'], 'passed')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'dots.txt', 'text': '.*'})['status'], 'failed')

    def test_ignore_case_and_min_count(self):
        self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'HELLO'})['status'], 'failed')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'HELLO', 'ignoreCase': True})['status'], 'passed')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'hello', 'minCount': 2})['status'], 'passed')
        result = self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'hello', 'minCount': 3})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['evidence']['matches'], 2)
        self.assertEqual(result['evidence']['required'], 3)

    def test_a_missing_file_fails(self):
        result = self.check({'type': 'text-match', 'path': 'nope.txt', 'text': 'x'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['evidence']['reason'], 'not_found')

    def test_unicode_emoji_and_odd_bytes(self):
        (self.work / 'u.txt').write_text('caf\u00e9 \U0001f600 \u202e done', encoding='utf-8')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'u.txt', 'text': '\U0001f600'})['status'], 'passed')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'u.txt', 'text': 'caf\u00e9'})['status'], 'passed')
        (self.work / 'bad.bin').write_bytes(b'ok \xff\xfe more\x00 tail')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'bad.bin', 'text': 'tail'})['status'], 'passed')

    def test_special_characters_in_the_search_text_are_safe(self):
        (self.work / 's.txt').write_text("'; DROP TABLE x; -- $(touch no) `touch no` \\n %s {0}")
        for text in ("'; DROP TABLE x; --", '$(touch no)', '`touch no`', '%s', '{0}'):
            self.assertEqual(self.check({'type': 'text-match', 'path': 's.txt', 'text': text})['status'], 'passed', text)
        self.assertFalse((self.work / 'no').exists())

    def test_the_file_content_is_not_echoed(self):
        result = self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'zzz'})
        self.assertNotIn('second line', json.dumps(result))

    def test_a_pattern_matches(self):
        self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'pattern': r'^hello\s+world$'})['status'], 'passed')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'pattern': r'^HELLO', 'ignoreCase': True})['status'], 'passed')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'pattern': r'hello', 'minCount': 2})['status'], 'passed')
        self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'pattern': r'zzz+'})['status'], 'failed')

    def test_a_pattern_that_would_hang_is_killed_and_the_caller_goes_on(self):
        (self.work / 'evil.txt').write_text('a' * 64 + '!')
        limits = Limits(regex_timeout=0.5)
        started = time.monotonic()
        result = self.check({'type': 'text-match', 'path': 'evil.txt', 'pattern': r'^(a+)+$'}, limits)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['evidence']['reason'], 'pattern_timeout')
        # The runner is still usable, and no child is left behind.
        self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'hello'})['status'], 'passed')
        self.assertEqual(check_runner.live_regex_children(), 0)

    def test_the_child_process_is_killed_with_its_group(self):
        (self.work / 'evil.txt').write_text('a' * 64 + '!')
        seen = []
        real_popen = check_runner.subprocess.Popen

        def spy(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            seen.append(process)
            return process

        with mock.patch.object(check_runner.subprocess, 'Popen', spy):
            self.check({'type': 'text-match', 'path': 'evil.txt', 'pattern': r'^(a+)+$'}, Limits(regex_timeout=0.4))
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0].poll())

    def test_the_child_gets_a_clean_environment(self):
        seen = {}
        real_popen = check_runner.subprocess.Popen

        def spy(*args, **kwargs):
            seen.update(kwargs)
            seen['args'] = args[0]
            return real_popen(*args, **kwargs)

        with mock.patch.dict(os.environ, {'SCHOLA_TEST_SECRET': 'hunter2'}), mock.patch.object(check_runner.subprocess, 'Popen', spy):
            self.check({'type': 'text-match', 'path': 'notes.txt', 'pattern': 'hello'})
        self.assertNotIn('SCHOLA_TEST_SECRET', seen['env'])
        self.assertIs(seen.get('shell', False), False)
        self.assertIsInstance(seen['args'], list)

    def test_pattern_mistakes_are_errors(self):
        cases = [
            ({'pattern': 'x' * 201}, 'invalid_descriptor'),
            ({'pattern': ''}, 'invalid_descriptor'),
            ({'pattern': 'a', 'text': 'a'}, 'invalid_descriptor'),
            ({}, 'invalid_descriptor'),
            ({'text': ''}, 'invalid_descriptor'),
            ({'text': 'a', 'minCount': 0}, 'invalid_descriptor'),
            ({'text': 'a', 'minCount': 100000}, 'invalid_descriptor'),
            ({'text': 'a', 'minCount': True}, 'invalid_descriptor'),
            ({'text': 'a', 'ignoreCase': 'yes'}, 'invalid_descriptor'),
            ({'pattern': '(unclosed'}, 'invalid_pattern'),
            ({'pattern': '[z-a]'}, 'invalid_pattern'),
            ({'pattern': '(?P<n>a)(?P<n>b)'}, 'invalid_pattern'),
        ]
        for extra, reason in cases:
            with self.subTest(extra=extra):
                result = self.check({'type': 'text-match', 'path': 'notes.txt', **extra})
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['evidence']['reason'], reason)

    def test_a_pattern_with_inline_flags_that_change_cost_is_still_bounded(self):
        (self.work / 'big.txt').write_text('a' * 20000 + '!')
        started = time.monotonic()
        result = self.check({'type': 'text-match', 'path': 'big.txt', 'pattern': r'(.*)*x'}, Limits(regex_timeout=0.4))
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn(result['status'], ('failed', 'error'))


class JsonValidTests(Workspace):
    def test_valid_json_passes(self):
        result = self.check({'type': 'json-valid', 'path': 'data.json'})
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['evidence']['topLevel'], 'object')

    def test_broken_json_fails(self):
        (self.work / 'bad.json').write_text('{"a": ')
        result = self.check({'type': 'json-valid', 'path': 'bad.json'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['evidence']['reason'], 'invalid_json')

    def test_json_edge_cases_fail(self):
        for text in ('', '   ', '{"a": NaN}', '{"a": Infinity}', "{'a': 1}", '{"a": 1,}', '{"a": 1} extra', '\ufeff{"a": 1}x'):
            (self.work / 'e.json').write_text(text, encoding='utf-8')
            with self.subTest(text=text):
                self.assertEqual(self.check({'type': 'json-valid', 'path': 'e.json'})['status'], 'failed')

    def test_deep_nesting_fails_without_a_crash(self):
        (self.work / 'deep.json').write_text('[' * 200000 + ']' * 200000)
        result = self.check({'type': 'json-valid', 'path': 'deep.json'})
        self.assertEqual(result['status'], 'failed')

    def test_a_file_that_is_not_utf8_fails(self):
        (self.work / 'latin.json').write_bytes(b'{"a": "\xe9"}')
        self.assertEqual(self.check({'type': 'json-valid', 'path': 'latin.json'})['status'], 'failed')

    def test_top_level_type(self):
        (self.work / 'list.json').write_text('[1, 2]')
        self.assertEqual(self.check({'type': 'json-valid', 'path': 'list.json', 'topLevel': 'array'})['status'], 'passed')
        self.assertEqual(self.check({'type': 'json-valid', 'path': 'list.json', 'topLevel': 'object'})['status'], 'failed')
        self.assertEqual(self.check({'type': 'json-valid', 'path': 'data.json', 'topLevel': 'object'})['status'], 'passed')
        self.assertEqual(self.check({'type': 'json-valid', 'path': 'data.json', 'topLevel': 'string'})['evidence']['reason'], 'invalid_descriptor')

    def test_required_keys(self):
        self.assertEqual(self.check({'type': 'json-valid', 'path': 'data.json', 'requiredKeys': ['name', 'items']})['status'], 'passed')
        result = self.check({'type': 'json-valid', 'path': 'data.json', 'requiredKeys': ['name', 'missing']})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['evidence']['missingKeys'], ['missing'])
        (self.work / 'list.json').write_text('[1]')
        self.assertEqual(self.check({'type': 'json-valid', 'path': 'list.json', 'requiredKeys': ['a']})['status'], 'failed')
        self.assertEqual(self.check({'type': 'json-valid', 'path': 'data.json', 'requiredKeys': 'name'})['evidence']['reason'], 'invalid_descriptor')

    def test_a_missing_file_fails(self):
        self.assertEqual(self.check({'type': 'json-valid', 'path': 'nope.json'})['status'], 'failed')


class ReviewFixTests(Workspace):
    def test_every_hiding_character_is_removed_from_text(self):
        for ch in ['\u200e', '\u200f', '\u061c', '\u00ad', '\u2028', '\u2029', '\u0085', '\u202a', '\u202e', '\u2066', '\u2069', '\u200b',
                   '\u200d', '\u2060', '\ufeff', '\x00', '\x1b', '\x7f', '\x9f']:
            with self.subTest(char=hex(ord(ch))):
                self.assertEqual(check_runner.clean_text(f'a{ch}b', 10), 'ab')
        self.assertEqual(check_runner.clean_text('caf\u00e9 \U0001f600', 20), 'caf\u00e9 \U0001f600')

    def test_the_control_character_pattern_is_written_with_escapes_only(self):
        source = (ROOT / 'check_runner.py').read_text(encoding='utf-8')
        line = next(item for item in source.splitlines() if item.startswith('_CONTROL'))
        self.assertTrue(all(ord(ch) < 128 for ch in line), 'the pattern holds a raw invisible character')
        self.assertTrue(all(ord(ch) < 128 or ch in '\u00e9' for ch in line))

    def test_a_read_that_fails_is_an_error_and_never_an_empty_file(self):
        (self.work / 'f.txt').write_text('data')
        for failure in (OSError(errno.EIO, 'io'), BlockingIOError(errno.EAGAIN, 'again')):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(check_workspace.os, 'read', side_effect=failure):
                with self.assertRaises(check_workspace.WorkspaceError) as caught:
                    check_workspace.read_file(self.work, 'f.txt')
                self.assertEqual(caught.exception.code, 'unreadable')
                result = self.check({'type': 'text-match', 'path': 'f.txt', 'text': 'data'})
                self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unreadable'))

    def test_whole_number_floats_are_accepted_as_integers_and_others_are_not(self):
        self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'hello', 'minCount': 2.0})['status'], 'passed')
        for value in (2.5, float('nan'), float('inf'), True, '2'):
            self.assertEqual(self.check({'type': 'text-match', 'path': 'notes.txt', 'text': 'hello', 'minCount': value})['evidence']['reason'], 'invalid_descriptor')

    def test_kill_group_says_whether_the_process_is_gone(self):
        process = subprocess.Popen(['sleep', '30'], start_new_session=True)
        self.assertIs(check_runner.kill_group(process), True)
        process.wait(5)
        self.assertIs(check_runner.kill_group(process), True, 'a process that is already gone counts as gone')
        broken = mock.Mock(pid=2 ** 22)
        broken.kill.side_effect = PermissionError('no')
        with mock.patch.object(check_runner.os, 'killpg', side_effect=PermissionError('no')):
            self.assertIs(check_runner.kill_group(broken), False)

    def test_a_pattern_child_that_cannot_be_killed_is_reported_and_does_not_hang(self):
        (self.work / 'evil.txt').write_text('a' * 64 + '!')
        spawned = []
        real = check_runner.subprocess.Popen

        def spy(*args, **kwargs):
            process = real(*args, **kwargs)
            spawned.append(process)
            return process

        started = time.monotonic()
        with mock.patch.object(check_runner, 'kill_group', return_value=False), mock.patch.object(check_runner.subprocess, 'Popen', spy):
            result = self.check({'type': 'text-match', 'path': 'evil.txt', 'pattern': r'^(a+)+$'}, Limits(regex_timeout=0.3, kill_wait_seconds=0.5))
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'kill_failed'))
        for process in spawned:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(5)

    def test_the_regex_child_is_killed_when_the_wait_itself_breaks(self):
        class Broken:
            pid = 1
            returncode = None

            def communicate(self, *args, **kwargs):
                raise RuntimeError('boom')

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        killed = []
        with mock.patch.object(check_runner.subprocess, 'Popen', return_value=Broken()), \
                mock.patch.object(check_runner, 'kill_group', lambda process: killed.append(process) or True):
            with self.assertRaises(RuntimeError):
                check_runner.count_matches('text', 'a', False, 1, 1.0)
        self.assertEqual(len(killed), 1)
        self.assertEqual(check_runner.live_regex_children(), 0)

    def test_a_pattern_child_that_fails_never_gives_a_count(self):
        class Fake:
            pid = 1

            def __init__(self, code, out):
                self.returncode, self.out = code, out

            def communicate(self, payload=None, timeout=None):
                return self.out, b''

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        cases = {'non-zero exit with a big count': (1, b'{"count": 99999}'), 'not JSON': (0, b'not json'), 'no count': (0, b'{}'),
                 'a count that is not a number': (0, b'{"count": "many"}'), 'a list': (0, b'[1]'), 'empty': (0, b''), 'not utf-8': (0, b'\xff\xfe')}
        for label, (code, out) in cases.items():
            with self.subTest(label=label), mock.patch.object(check_runner.subprocess, 'Popen', return_value=Fake(code, out)):
                self.assertEqual(check_runner.count_matches('text', 'a', False, 5, 1.0), (0, 'pattern_failed'))
        with mock.patch.object(check_runner.subprocess, 'Popen', side_effect=OSError('no')):
            self.assertEqual(check_runner.count_matches('text', 'a', False, 5, 1.0), (0, 'pattern_failed'))
        with mock.patch.object(check_runner, 'count_matches', return_value=(0, 'pattern_failed')):
            result = self.check({'type': 'text-match', 'path': 'notes.txt', 'pattern': 'hello'})
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'pattern_failed'))

    def test_the_pattern_worker_survives_a_bad_cpu_limit_argument(self):
        done = subprocess.run([sys.executable, '-I', '-S', '-c', check_runner._REGEX_WORKER, 'not-a-number'], input=json.dumps(
            {'text': 'aaa', 'pattern': 'a', 'ignoreCase': False, 'limit': 5}).encode(), capture_output=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {'count': 3})
        done = subprocess.run([sys.executable, '-I', '-S', '-c', check_runner._REGEX_WORKER], input=json.dumps(
            {'text': 'aaa', 'pattern': 'a', 'ignoreCase': False, 'limit': 5}).encode(), capture_output=True, timeout=20)
        self.assertEqual(json.loads(done.stdout), {'count': 3})


if __name__ == '__main__':
    unittest.main()
