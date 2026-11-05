"""The learning event log store: append-only, one writer, caps on write, damage reported and never repaired."""
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import event_log
import learning_event
from event_log import EventLog, EventLogError

FIXTURES = ROOT / 'tests' / 'fixtures' / 'contracts' / 'learning-events'
BASE_EVENT = FIXTURES / 'valid' / 'task-opened.json'
MAX_BYTES = learning_event.MAX_BYTES
MAX_LINES = learning_event.MAX_LINES


def make_event(**changes):
    document = json.loads(BASE_EVENT.read_text(encoding='utf-8'))
    document['eventId'] = str(uuid.uuid4())
    document.update(changes)
    return document


def line_of(document):
    return json.dumps(document, separators=(',', ':'), ensure_ascii=False).encode('utf-8') + b'\n'


def tree(directory):
    """Every path under a folder, with a digest for files. Empty folders count."""
    directory = Path(directory)
    result = {}
    for path in sorted(directory.rglob('*')):
        key = str(path.relative_to(directory))
        result[key] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'dir'
    return result


class LogCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.folder = self.data / '_events'
        self.path = self.folder / 'events.jsonl'
        self.log = EventLog(self.data, lock_timeout=0.2)

    def put(self, data):
        """Write the log file directly, as an outside program or a crash would leave it."""
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)

    def raw(self):
        return self.path.read_bytes()

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(EventLogError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class AppendTests(LogCase):
    def test_round_trip_writes_one_compact_line_and_reads_it_back(self):
        event = make_event()
        result = self.log.append(event)
        self.assertEqual(result, {'appended': True, 'eventId': event['eventId'], 'line': 1, 'bytes': len(line_of(event)),
                                  'newlineAdded': False})
        self.assertEqual(self.raw(), line_of(event))
        report = self.log.read()
        self.assertTrue(report['ok'])
        self.assertTrue(report['exists'])
        self.assertEqual(report['events'], [{'line': 1, 'event': event}])
        self.assertEqual(report['lines'], [{'line': 1, 'ok': True}])
        self.assertFalse(report['truncatedTail'])

    def test_lines_come_back_in_order_and_line_numbers_count_up(self):
        events = [make_event() for _ in range(3)]
        numbers = [self.log.append(event)['line'] for event in events]
        self.assertEqual(numbers, [1, 2, 3])
        self.assertEqual([item['event'] for item in self.log.read()['events']], events)

    def test_every_valid_fixture_event_is_accepted_with_its_exact_content(self):
        names = sorted(path for path in (FIXTURES / 'valid').glob('*.json'))
        accepted = 0
        for index, path in enumerate(names):
            document = json.loads(path.read_text(encoding='utf-8'))
            document['eventId'] = str(uuid.uuid4())
            if not learning_event.validate_event(document)['ok']:
                continue
            self.log.append(document)
            accepted += 1
        self.assertGreater(accepted, 20)
        self.assertTrue(self.log.read()['ok'])

    def test_the_file_and_folder_are_private_to_the_user(self):
        self.log.append(make_event())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)

    def test_append_flushes_the_file_to_disk(self):
        self.log.append(make_event())
        with mock.patch('os.fsync', wraps=os.fsync) as fsync, mock.patch('event_log._fsync_dir'):
            self.log.append(make_event())
        self.assertEqual(fsync.call_count, 1)

    def test_creating_the_log_flushes_the_new_folder_entries(self):
        with mock.patch('event_log._fsync_dir') as flush:
            self.log.append(make_event())
        self.assertEqual([call.args[0] for call in flush.call_args_list], [self.data, self.folder])

    def test_a_second_append_does_not_flush_folders(self):
        self.log.append(make_event())
        with mock.patch('event_log._fsync_dir') as flush:
            self.log.append(make_event())
        flush.assert_not_called()

    def test_the_data_folder_is_the_only_location_input(self):
        elsewhere = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, elsewhere)
        EventLog(elsewhere).append(make_event())
        self.assertTrue((elsewhere / '_events' / 'events.jsonl').is_file())
        self.assertFalse(self.folder.exists())


class InvalidEventTests(LogCase):
    def assert_nothing_written(self, code, document):
        before = tree(self.data)
        self.refused(code, self.log.append, document)
        self.assertEqual(tree(self.data), before)
        self.assertFalse(self.folder.exists())

    def test_non_objects_are_refused_and_write_nothing(self):
        for value in (None, [], 'text', 5, True, b'{}'):
            with self.subTest(value=value):
                self.assert_nothing_written('invalid_event', value)

    def test_an_event_with_an_unknown_field_is_refused(self):
        self.assert_nothing_written('invalid_event', make_event(extra='x'))

    def test_an_event_with_a_missing_field_is_refused(self):
        document = make_event()
        del document['sessionId']
        self.assert_nothing_written('invalid_event', document)

    def test_a_bad_event_type_is_refused(self):
        self.assert_nothing_written('invalid_event', make_event(type='confidence_rated'))

    def test_an_app_only_type_with_the_wrong_source_is_refused(self):
        document = json.loads((FIXTURES / 'valid' / 'check-result-passed.json').read_text(encoding='utf-8'))
        document['eventId'] = str(uuid.uuid4())
        document['source'] = 'student'
        self.assert_nothing_written('invalid_event', document)

    def test_a_value_that_is_not_json_is_refused(self):
        self.assert_nothing_written('invalid_event', make_event(skills={'a'}))
        self.assert_nothing_written('invalid_event', make_event(time=float('nan')))

    def test_a_key_that_is_not_text_is_refused(self):
        document = make_event()
        document[1] = 'x'
        self.assert_nothing_written('invalid_event', document)

    def test_an_unsupported_major_version_has_its_own_code(self):
        self.assert_nothing_written('unsupported_version', make_event(schemaVersion='2.0'))

    def test_a_huge_event_is_refused_as_too_large(self):
        self.assert_nothing_written('event_too_large', make_event(sessionId='a' * 9000))

    def test_the_error_lists_pointers_and_never_echoes_a_value(self):
        error = self.refused('invalid_event', self.log.append, make_event(taskId='SECRET-VALUE-123'))
        self.assertEqual(error.status, 422)
        self.assertIn('/taskId', [item['pointer'] for item in error.details])
        self.assertNotIn('SECRET-VALUE-123', json.dumps(error.payload()))

    def test_an_invalid_event_leaves_an_existing_log_unchanged(self):
        self.log.append(make_event())
        before = tree(self.data)
        self.refused('invalid_event', self.log.append, make_event(type='nope'))
        self.assertEqual(tree(self.data), before)

    def test_the_line_size_guard_holds_even_when_the_validator_says_ok(self):
        big = make_event(sessionId='a' * 5000)
        with mock.patch('event_log.le.validate_event_text', return_value={'ok': True}):
            self.refused('event_too_large', self.log.append, big)
        self.assertFalse(self.folder.exists())


class DuplicateTests(LogCase):
    def test_a_repeated_event_id_is_refused_and_writes_nothing(self):
        event = make_event()
        self.log.append(event)
        before = self.raw()
        error = self.refused('duplicate_event_id', self.log.append, dict(event, time='2026-11-02T10:00:00Z'))
        self.assertEqual(error.status, 409)
        self.assertEqual(self.raw(), before)

    def test_an_id_on_a_damaged_but_parseable_line_still_counts(self):
        taken = str(uuid.uuid4())
        self.put(json.dumps({'eventId': taken, 'type': 'nonsense'}).encode() + b'\n')
        self.refused('duplicate_event_id', self.log.append, make_event(eventId=taken))

    def test_the_id_index_is_rebuilt_when_the_file_changes_outside(self):
        self.log.append(make_event())
        outside = make_event()
        with open(self.path, 'ab') as handle:
            handle.write(line_of(outside))
        self.refused('duplicate_event_id', self.log.append, outside)
        self.assertEqual(self.log.read()['lines'][-1], {'line': 2, 'ok': True})

    def test_the_id_index_follows_a_file_that_is_replaced(self):
        first = make_event()
        self.log.append(first)
        self.path.unlink()
        self.log.append(first)
        self.assertEqual(self.raw(), line_of(first))

    def test_an_id_that_is_only_a_prefix_of_another_does_not_collide(self):
        self.log.append(make_event(eventId='a1b2c3d4-0000-4000-8000-000000000017'))
        self.log.append(make_event(eventId='a1b2c3d4-0000-4000-8000-000000000018'))
        self.assertEqual(len(self.log.read()['events']), 2)


class CapTests(LogCase):
    def filler(self, size):
        """Raw filler lines of at most 4000 bytes each, ending in a newline, `size` bytes in all."""
        lines, left = [], size
        while left > 0:
            take = min(left, 4000)
            if left - take == 1:
                take -= 1
            lines.append(b'a' * (take - 1) + b'\n')
            left -= take
        data = b''.join(lines)
        self.assertEqual(len(data), size)
        return data

    def test_the_file_may_end_exactly_at_the_size_limit(self):
        event = make_event()
        size = len(line_of(event))
        self.put(self.filler(MAX_BYTES - size))
        self.log.append(event)
        self.assertEqual(self.path.stat().st_size, MAX_BYTES)
        self.assertTrue(self.log.read()['events'][-1]['event'] == event)

    def test_one_byte_over_the_size_limit_is_log_full(self):
        event = make_event()
        size = len(line_of(event))
        self.put(self.filler(MAX_BYTES - size + 1))
        before = self.raw()
        error = self.refused('log_full', self.log.append, event)
        self.assertEqual(error.status, 409)
        self.assertEqual(self.raw(), before)

    def test_the_added_newline_of_a_cut_tail_counts_toward_the_size_limit(self):
        event = make_event()
        size = len(line_of(event))
        self.put(self.filler(MAX_BYTES - size + 1)[:-1])  # no final newline, so one byte is added
        self.refused('log_full', self.log.append, event)
        self.put(self.filler(MAX_BYTES - size)[:-1])
        self.assertTrue(self.log.append(event)['newlineAdded'])
        self.assertEqual(self.path.stat().st_size, MAX_BYTES)

    def test_a_file_already_over_the_size_limit_is_log_full_and_untouched(self):
        self.put(self.filler(MAX_BYTES + 1))
        before = self.raw()
        self.refused('log_full', self.log.append, make_event())
        self.assertEqual(self.raw(), before)

    def test_the_log_may_hold_exactly_the_line_limit(self):
        self.put(b'\n' * (MAX_LINES - 1))
        self.assertEqual(self.log.append(make_event())['line'], MAX_LINES)
        self.refused('log_full', self.log.append, make_event())
        self.assertEqual(self.raw().count(b'\n'), MAX_LINES)

    def test_a_cut_tail_counts_as_a_line(self):
        self.put(b'\n' * (MAX_LINES - 1) + b'{"cut')
        before = self.raw()
        self.refused('log_full', self.log.append, make_event())
        self.assertEqual(self.raw(), before)

    def test_log_full_never_rotates_or_deletes(self):
        self.put(b'\n' * MAX_LINES)
        self.refused('log_full', self.log.append, make_event())
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), [event_log.LOCK_FILE, event_log.LOG_FILE])
        self.assertEqual(self.raw(), b'\n' * MAX_LINES)

    def test_a_repeated_id_on_a_full_log_is_reported_as_the_repeat(self):
        event = make_event()
        self.put(b'\n' * (MAX_LINES - 1) + line_of(event)[:-1])
        self.refused('duplicate_event_id', self.log.append, event)


class TruncatedTailTests(LogCase):
    def test_a_cut_line_is_reported_as_the_truncated_tail(self):
        good = make_event()
        self.put(line_of(good) + b'{"schemaVersion":"1.0","even')
        report = self.log.read()
        self.assertTrue(report['truncatedTail'])
        self.assertFalse(report['ok'])
        self.assertEqual(report['lines'][1]['reason'], 'truncated_tail')
        self.assertEqual(report['events'], [{'line': 1, 'event': good}])

    def test_append_adds_one_newline_then_the_event_and_keeps_earlier_bytes(self):
        good = make_event()
        cut = b'{"schemaVersion":"1.0","even'
        self.put(line_of(good) + cut)
        before = self.raw()
        event = make_event()
        result = self.log.append(event)
        self.assertTrue(result['newlineAdded'])
        self.assertEqual(result['line'], 3)
        self.assertEqual(self.raw(), before + b'\n' + line_of(event))
        report = self.log.read()
        self.assertFalse(report['truncatedTail'])
        self.assertEqual([item['ok'] for item in report['lines']], [True, False, True])
        self.assertEqual(report['lines'][1]['reason'], 'invalid')

    def test_a_tail_cut_inside_a_multibyte_character_is_handled_the_same_way(self):
        good = make_event()
        self.put(line_of(good) + '{"evidence":"é'.encode('utf-8')[:-1])
        self.assertTrue(self.log.read()['truncatedTail'])
        self.log.append(make_event())
        report = self.log.read()
        self.assertEqual([item['ok'] for item in report['lines']], [True, False, True])

    def test_a_complete_last_line_with_no_newline_is_valid_and_still_gets_a_newline(self):
        first = make_event()
        self.put(line_of(first)[:-1])
        report = self.log.read()
        self.assertTrue(report['ok'])
        self.assertFalse(report['truncatedTail'])
        second = make_event()
        self.assertTrue(self.log.append(second)['newlineAdded'])
        self.assertEqual(self.raw(), line_of(first) + line_of(second))

    def test_a_cut_line_keeps_its_id_out_of_the_index_so_a_retry_can_succeed(self):
        event = make_event()
        self.put(line_of(event)[:40])
        self.assertTrue(self.log.append(event)['appended'])
        self.assertEqual(self.log.read()['events'], [{'line': 2, 'event': event}])

    def test_crash_in_the_middle_of_a_write_then_append_then_read(self):
        event = make_event()
        script = ('import os, sys\n'
                  'fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)\n'
                  'data = open(sys.argv[2], "rb").read()\n'
                  'os.write(fd, data[: len(data) // 2])\n'
                  'os._exit(9)\n')
        seed = self.data / 'event.json'
        seed.write_bytes(line_of(event))
        self.folder.mkdir()
        done = subprocess.run([sys.executable, '-c', script, str(self.path), str(seed)], capture_output=True, check=False)
        self.assertEqual(done.returncode, 9)
        half = self.raw()
        self.assertFalse(half.endswith(b'\n'))
        self.assertTrue(self.log.read()['truncatedTail'])
        later = make_event()
        self.log.append(later)
        self.assertEqual(self.raw(), half + b'\n' + line_of(later))
        report = self.log.read()
        self.assertEqual([item['ok'] for item in report['lines']], [False, True])
        self.assertEqual(report['events'], [{'line': 2, 'event': later}])


class DamageTests(LogCase):
    def test_non_utf8_bytes_damage_that_line_only(self):
        good = make_event()
        self.put(b'\xff\xfe\xfa not text\n' + line_of(good))
        report = self.log.read()
        self.assertEqual(report['lines'][0]['ok'], False)
        self.assertEqual(report['lines'][0]['reason'], 'invalid')
        self.assertEqual(report['events'], [{'line': 2, 'event': good}])

    def test_append_works_after_non_utf8_bytes(self):
        self.put(b'\xff\xfe\n')
        before = self.raw()
        self.log.append(make_event())
        self.assertTrue(self.raw().startswith(before))
        self.assertEqual([item['ok'] for item in self.log.read()['lines']], [False, True])

    def test_read_reports_every_damaged_line_with_number_reason_and_pointers(self):
        bad = make_event(taskId=5)
        self.put(b'not json\n' + line_of(bad) + b'\n')
        report = self.log.read()
        self.assertEqual([item['line'] for item in report['lines']], [1, 2, 3])
        self.assertEqual({item['reason'] for item in report['lines']}, {'invalid'})
        self.assertIn('/taskId', [error['pointer'] for error in report['lines'][1]['errors']])
        self.assertEqual(report['events'], [])

    def test_read_never_changes_the_file(self):
        self.put(b'\xff\n{"cut')
        before, mtime = self.raw(), self.path.stat().st_mtime_ns
        self.log.read()
        self.log.export()
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_a_blank_line_is_reported_as_damage(self):
        self.put(b'\n')
        self.assertFalse(self.log.read()['lines'][0]['ok'])

    def test_a_repeated_id_in_the_file_is_reported_on_the_later_line(self):
        event = make_event()
        self.put(line_of(event) * 2)
        report = self.log.read()
        self.assertEqual([item['ok'] for item in report['lines']], [True, False])
        self.assertIn('/eventId', [error['pointer'] for error in report['lines'][1]['errors']])


class ReadTests(LogCase):
    def test_a_missing_log_reads_as_empty_and_says_it_does_not_exist(self):
        before = tree(self.data)
        report = self.log.read()
        self.assertEqual(report, {'ok': True, 'exists': False, 'lines': [], 'events': [], 'truncatedTail': False})
        self.assertEqual(tree(self.data), before)

    def test_a_missing_log_beside_an_existing_folder_reads_as_empty(self):
        self.folder.mkdir()
        self.assertFalse(self.log.read()['exists'])

    def test_an_empty_file_reads_as_an_empty_log(self):
        self.put(b'')
        report = self.log.read()
        self.assertEqual(report, {'ok': True, 'exists': True, 'lines': [], 'events': [], 'truncatedTail': False})

    def test_a_log_over_the_size_limit_is_log_corrupt_and_kept(self):
        self.put(b'a' * (MAX_BYTES + 1))
        self.refused('log_corrupt', self.log.read)
        self.assertEqual(self.path.stat().st_size, MAX_BYTES + 1)

    def test_a_log_with_too_many_lines_is_log_corrupt(self):
        self.put(b'\n' * (MAX_LINES + 1))
        self.refused('log_corrupt', self.log.read)

    def test_a_log_that_is_a_folder_is_log_corrupt(self):
        self.path.mkdir(parents=True)
        for call in (self.log.read, self.log.export, lambda: self.log.append(make_event())):
            with self.subTest(call=call):
                self.refused('log_corrupt', call)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can read every file')
    def test_an_unreadable_file_is_log_unreadable_and_names_no_path(self):
        self.put(b'')
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o600)
        for call in (self.log.read, self.log.export, lambda: self.log.append(make_event())):
            with self.subTest(call=call):
                error = self.refused('log_unreadable', call)
                self.assertNotIn(str(self.data), json.dumps(error.payload()))

    def test_symbolic_links_are_refused(self):
        outside = self.data / 'outside.jsonl'
        outside.write_bytes(line_of(make_event()))
        kept = outside.read_bytes()
        self.folder.mkdir()
        self.path.symlink_to(outside)
        for call in (self.log.read, self.log.export, lambda: self.log.append(make_event()), lambda: self.log.delete(confirm=True)):
            with self.subTest(call=call):
                self.refused('unsafe_path', call)
        self.assertEqual(outside.read_bytes(), kept)

    def test_a_linked_folder_is_refused(self):
        real = self.data / 'real'
        real.mkdir()
        self.folder.symlink_to(real, target_is_directory=True)
        self.refused('unsafe_path', self.log.append, make_event())
        self.assertEqual(list(real.iterdir()), [])


class ExportTests(LogCase):
    def test_export_returns_the_exact_bytes_including_damage(self):
        data = line_of(make_event()) + b'\xff\xfe\n\n{"cut'
        self.put(data)
        self.assertEqual(self.log.export(), data)

    def test_export_of_a_clean_log_equals_the_file(self):
        for _ in range(3):
            self.log.append(make_event())
        self.assertEqual(self.log.export(), self.raw())

    def test_export_of_a_missing_log_is_log_missing(self):
        error = self.refused('log_missing', self.log.export)
        self.assertEqual(error.status, 404)

    def test_export_of_an_empty_file_is_empty_bytes(self):
        self.put(b'')
        self.assertEqual(self.log.export(), b'')

    def test_export_of_a_log_over_the_size_limit_is_log_corrupt(self):
        self.put(b'a' * (MAX_BYTES + 1))
        self.refused('log_corrupt', self.log.export)


class DeleteTests(LogCase):
    def test_delete_without_confirm_is_refused_and_keeps_the_log(self):
        self.log.append(make_event())
        before = self.raw()
        for value in (None, False, 'true', 1, 'yes', [True]):
            with self.subTest(value=value):
                error = self.refused('confirmation_required', self.log.delete, confirm=value)
                self.assertEqual(error.status, 400)
        error = self.refused('confirmation_required', self.log.delete)
        self.assertEqual(error.field, 'confirm')
        self.assertEqual(self.raw(), before)

    def test_unconfirmed_delete_of_a_missing_log_still_asks_for_confirmation(self):
        self.refused('confirmation_required', self.log.delete)

    def test_confirmed_delete_removes_only_the_log_and_flushes_the_folder(self):
        self.log.append(make_event())
        other = self.folder / 'reflections'
        other.mkdir()
        (other / 'keep.json').write_text('{}')
        with mock.patch('event_log._fsync_dir') as flush:
            result = self.log.delete(confirm=True)
        self.assertEqual(result, {'deleted': True})
        flush.assert_called_once_with(self.folder)
        self.assertFalse(self.path.exists())
        self.assertTrue((other / 'keep.json').is_file())

    def test_delete_of_a_missing_log_is_log_missing_not_a_silent_success(self):
        error = self.refused('log_missing', self.log.delete, confirm=True)
        self.assertEqual(error.status, 404)

    def test_after_delete_the_same_event_id_can_be_written_again(self):
        event = make_event()
        self.log.append(event)
        self.log.delete(confirm=True)
        self.assertEqual(self.log.read()['exists'], False)
        self.log.append(event)
        self.assertEqual(self.raw(), line_of(event))

    def test_a_failed_folder_flush_after_delete_is_reported(self):
        self.log.append(make_event())
        with mock.patch('event_log._fsync_dir', side_effect=OSError(errno.EIO, 'boom')):
            error = self.refused('fsync_failed', self.log.delete, confirm=True)
        self.assertEqual(error.details, {'removed': True})
        self.assertFalse(self.path.exists())

    def test_a_failed_unlink_is_delete_failed(self):
        self.log.append(make_event())
        with mock.patch('os.unlink', side_effect=OSError(errno.EPERM, 'no')):
            self.refused('delete_failed', self.log.delete, confirm=True)
        self.assertTrue(self.path.exists())


class FailureTests(LogCase):
    def test_a_failed_fsync_is_reported_and_never_success(self):
        self.log.append(make_event())
        event = make_event()
        with mock.patch('os.fsync', side_effect=OSError(errno.EIO, 'boom')):
            error = self.refused('fsync_failed', self.log.append, event)
        self.assertEqual(error.status, 500)
        self.assertEqual(error.details['maybeWritten'], True)
        self.assertEqual(error.details['partialLine'], False)

    def test_after_a_failed_fsync_a_retry_learns_the_line_is_there(self):
        self.log.append(make_event())
        event = make_event()
        with mock.patch('os.fsync', side_effect=OSError(errno.EIO, 'boom')):
            self.refused('fsync_failed', self.log.append, event)
        self.refused('duplicate_event_id', self.log.append, event)
        self.assertTrue(self.raw().endswith(line_of(event)))

    def test_a_failed_folder_flush_on_create_is_fsync_failed(self):
        with mock.patch('event_log._fsync_dir', side_effect=OSError(errno.EIO, 'boom')):
            self.refused('fsync_failed', self.log.append, make_event())

    def test_a_short_write_is_write_failed_and_says_a_partial_line_may_remain(self):
        event = make_event()
        real = os.write

        def short(descriptor, data):
            return real(descriptor, data[:10])

        with mock.patch('os.write', side_effect=short):
            error = self.refused('write_failed', self.log.append, event)
        self.assertTrue(error.details['partialLine'])
        self.assertIn('partial', error.payload()['error'].lower())
        self.assertEqual(self.raw(), line_of(event)[:10])

    def test_after_a_short_write_the_next_append_treats_it_as_a_truncated_tail(self):
        real = os.write
        with mock.patch('os.write', side_effect=lambda descriptor, data: real(descriptor, data[:10])):
            self.refused('write_failed', self.log.append, make_event())
        self.assertTrue(self.log.read()['truncatedTail'])
        later = make_event()
        self.assertTrue(self.log.append(later)['newlineAdded'])
        self.assertEqual([item['ok'] for item in self.log.read()['lines']], [False, True])

    def test_a_write_error_writes_nothing_and_is_write_failed(self):
        self.log.append(make_event())
        before = self.raw()
        with mock.patch('os.write', side_effect=OSError(errno.ENOSPC, 'full')):
            error = self.refused('write_failed', self.log.append, make_event())
        self.assertFalse(error.details['partialLine'])
        self.assertEqual(self.raw(), before)

    def test_a_log_that_cannot_be_opened_for_writing_is_write_failed(self):
        self.log.append(make_event())
        real = os.open

        def refuse(path, flags, *rest, **named):
            if Path(path) == self.path and flags & os.O_APPEND:
                raise OSError(errno.EACCES, 'denied')
            return real(path, flags, *rest, **named)

        with mock.patch('os.open', side_effect=refuse):
            error = self.refused('write_failed', self.log.append, make_event())
        self.assertFalse(error.details['partialLine'])

    def test_a_failed_append_does_not_poison_the_instance(self):
        self.log.append(make_event())
        with mock.patch('os.fsync', side_effect=OSError(errno.EIO, 'boom')):
            self.refused('fsync_failed', self.log.append, make_event())
        self.assertTrue(self.log.append(make_event())['appended'])
        self.assertEqual(len(self.log.read()['events']), 3)


class LockTests(LogCase):
    def hold(self, exclusive=True):
        self.folder.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.folder / event_log.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        self.addCleanup(os.close, descriptor)
        return descriptor

    def test_a_held_lock_makes_append_fail_with_lock_unavailable_and_write_nothing(self):
        self.log.append(make_event())
        self.hold()
        before = self.raw()
        error = self.refused('lock_unavailable', self.log.append, make_event())
        self.assertEqual(error.status, 503)
        self.assertTrue(error.retryable)
        self.assertEqual(self.raw(), before)

    def test_a_held_lock_blocks_delete_and_read(self):
        self.log.append(make_event())
        self.hold()
        self.refused('lock_unavailable', self.log.delete, confirm=True)
        self.refused('lock_unavailable', self.log.read)
        self.refused('lock_unavailable', self.log.export)
        self.assertTrue(self.path.exists())

    def test_readers_share_the_lock(self):
        self.log.append(make_event())
        self.hold(exclusive=False)
        self.assertTrue(self.log.read()['ok'])

    def test_an_append_waits_for_a_short_lock(self):
        self.log.append(make_event())
        descriptor = self.hold()
        slow = EventLog(self.data, lock_timeout=5)
        timer = threading.Timer(0.15, lambda: fcntl.flock(descriptor, fcntl.LOCK_UN))
        timer.start()
        self.addCleanup(timer.join)
        self.assertTrue(slow.append(make_event())['appended'])

    def test_a_lock_file_that_cannot_be_opened_is_lock_unavailable(self):
        self.folder.mkdir()
        (self.folder / event_log.LOCK_FILE).mkdir()
        self.refused('lock_unavailable', self.log.append, make_event())
        self.assertFalse(self.path.exists())

    def test_a_system_lock_error_is_lock_unavailable(self):
        with mock.patch('fcntl.flock', side_effect=OSError(errno.ENOLCK, 'no locks')):
            self.refused('lock_unavailable', self.log.append, make_event())
        self.assertFalse(self.path.exists())

    def test_the_lock_is_released_after_a_failed_append(self):
        self.log.append(make_event())
        with mock.patch('os.fsync', side_effect=OSError(errno.EIO, 'boom')):
            self.refused('fsync_failed', self.log.append, make_event())
        other = EventLog(self.data, lock_timeout=0.2)
        self.assertTrue(other.append(make_event())['appended'])


COUNTING_WORKER = '''
import json, sys, uuid
sys.path.insert(0, sys.argv[1])
from event_log import EventLog, EventLogError
base = json.loads(open(sys.argv[2], encoding="utf-8").read())
count, shared = int(sys.argv[4]), sys.argv[5]
log = EventLog(sys.argv[3], lock_timeout=60)
appended = duplicates = 0
for _ in range(count):
    event = dict(base, eventId=shared if shared != "-" else str(uuid.uuid4()))
    try:
        log.append(event)
        appended += 1
    except EventLogError as error:
        if error.code != "duplicate_event_id":
            raise
        duplicates += 1
print(json.dumps({"appended": appended, "duplicates": duplicates}))
'''


class ConcurrencyTests(LogCase):
    def run_workers(self, processes, count, shared='-'):
        script = self.data / 'worker.py'
        script.write_text(COUNTING_WORKER, encoding='utf-8')
        workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, workdir)
        log_dir = workdir / 'data'
        log_dir.mkdir()
        children = [subprocess.Popen([sys.executable, str(script), str(ROOT), str(BASE_EVENT), str(log_dir), str(count), shared],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(processes)]
        results = []
        for child in children:
            out, err = child.communicate(timeout=120)
            self.assertEqual(child.returncode, 0, err.decode())
            results.append(json.loads(out))
        return EventLog(log_dir), results

    def test_two_processes_never_interleave_or_lose_a_line(self):
        log, results = self.run_workers(2, 40)
        self.assertEqual(sum(item['appended'] for item in results), 80)
        report = log.read()
        self.assertTrue(report['ok'], report['lines'][:3])
        self.assertEqual(len(report['events']), 80)
        self.assertEqual(len({item['event']['eventId'] for item in report['events']}), 80)

    def test_four_processes_racing_for_one_event_id_give_exactly_one_winner(self):
        log, results = self.run_workers(4, 1, shared=str(uuid.uuid4()))
        self.assertEqual(sum(item['appended'] for item in results), 1)
        self.assertEqual(sum(item['duplicates'] for item in results), 3)
        self.assertEqual(len(log.read()['events']), 1)

    def test_threads_never_interleave_or_lose_a_line(self):
        errors = []

        def work():
            log = EventLog(self.data, lock_timeout=30)
            try:
                for _ in range(30):
                    log.append(make_event())
            except Exception as error:  # noqa: BLE001 - the test reports any failure of a worker
                errors.append(error)

        threads = [threading.Thread(target=work) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        report = self.log.read()
        self.assertTrue(report['ok'])
        self.assertEqual(len(report['events']), 180)
        self.assertEqual(len({item['event']['eventId'] for item in report['events']}), 180)

    def test_threads_racing_for_one_event_id_give_exactly_one_winner(self):
        event = make_event()
        outcomes = []

        def work():
            try:
                EventLog(self.data, lock_timeout=30).append(event)
                outcomes.append('ok')
            except EventLogError as error:
                outcomes.append(error.code)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ['duplicate_event_id'] * 7 + ['ok'])
        self.assertEqual(len(self.log.read()['events']), 1)

    def test_one_instance_shared_by_threads_stays_correct(self):
        shared = EventLog(self.data, lock_timeout=30)
        errors = []

        def work():
            try:
                for _ in range(20):
                    shared.append(make_event())
            except Exception as error:  # noqa: BLE001 - the test reports any failure of a worker
                errors.append(error)

        threads = [threading.Thread(target=work) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.log.read()['events']), 100)


if __name__ == '__main__':
    unittest.main()
