"""Event log safety rules added after review: reserve, cache, windows, flush retries, odd files, odd folders."""
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
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

BASE_EVENT = ROOT / 'tests' / 'fixtures' / 'contracts' / 'learning-events' / 'valid' / 'task-opened.json'
MAX_BYTES = learning_event.MAX_BYTES
MAX_LINES = learning_event.MAX_LINES


def make_event(**changes):
    document = json.loads(BASE_EVENT.read_text(encoding='utf-8'))
    document['eventId'] = str(uuid.uuid4())
    document.update(changes)
    return document


def line_of(document):
    return json.dumps(document, separators=(',', ':'), ensure_ascii=False).encode('utf-8') + b'\n'


def make_private(folder):
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)


def filler(size):
    """Raw filler lines of at most 4000 bytes each, ending in a newline, `size` bytes in all."""
    lines, left = [], size
    while left > 0:
        take = min(left, 4000)
        if left - take == 1:
            take -= 1
        lines.append(b'a' * (take - 1) + b'\n')
        left -= take
    return b''.join(lines)


class SafetyCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.folder = self.data / '_events'
        self.path = self.folder / 'events.jsonl'
        self.reflections = self.folder / 'reflections'
        self.log = EventLog(self.data, lock_timeout=0.2)

    def put(self, data):
        make_private(self.folder)
        self.path.write_bytes(data)

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(EventLogError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class ReserveTests(SafetyCase):
    def test_the_reserve_is_80_percent_of_each_cap(self):
        self.assertEqual(event_log.RESERVE_BYTES, 4194304)
        self.assertEqual(event_log.RESERVE_LINES, 40000)

    def test_the_page_may_write_below_80_percent_of_the_bytes_and_not_at_it(self):
        self.put(filler(int(MAX_BYTES * 0.79)))
        self.assertTrue(self.log.append(make_event(), from_page=True)['appended'])
        self.put(filler(event_log.RESERVE_BYTES - 1))
        self.assertTrue(self.log.append(make_event(), from_page=True)['appended'])
        self.put(filler(event_log.RESERVE_BYTES))
        before = self.path.read_bytes()
        error = self.refused('log_reserved', self.log.append, make_event(), from_page=True)
        self.assertEqual(error.status, 409)
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_app_may_write_past_80_percent_up_to_the_cap(self):
        self.put(filler(event_log.RESERVE_BYTES))
        self.assertTrue(self.log.append(make_event())['appended'])
        self.put(filler(MAX_BYTES - 700))
        self.assertTrue(self.log.append(make_event(), from_page=False)['appended'])

    def test_the_page_may_write_below_80_percent_of_the_lines_and_not_at_it(self):
        self.put(b'\n' * (event_log.RESERVE_LINES - 1))
        self.assertEqual(self.log.append(make_event(), from_page=True)['line'], event_log.RESERVE_LINES)
        self.refused('log_reserved', self.log.append, make_event(), from_page=True)
        self.assertEqual(self.log.append(make_event())['line'], event_log.RESERVE_LINES + 1)

    def test_at_100_percent_the_page_gets_log_reserved_and_the_app_gets_log_full(self):
        self.put(b'\n' * MAX_LINES)
        self.refused('log_reserved', self.log.append, make_event(), from_page=True)
        self.refused('log_full', self.log.append, make_event())
        self.put(filler(MAX_BYTES))
        self.refused('log_reserved', self.log.append, make_event(), from_page=True)
        self.refused('log_full', self.log.append, make_event())

    def test_a_repeated_id_is_still_reported_as_the_repeat_on_a_reserved_log(self):
        event = make_event()
        self.put(b'\n' * event_log.RESERVE_LINES + line_of(event))
        self.refused('duplicate_event_id', self.log.append, event, from_page=True)


class CacheTests(SafetyCase):
    def test_a_second_append_on_the_same_instance_does_not_read_the_file(self):
        with mock.patch.object(EventLog, '_read_log', autospec=True, side_effect=EventLog._read_log) as read:
            for _ in range(3):
                self.log.append(make_event())
        self.assertEqual(read.call_count, 0)

    def test_a_new_instance_reads_the_file_once_and_then_reuses_the_scan(self):
        self.log.append(make_event())
        other = EventLog(self.data, lock_timeout=0.2)
        with mock.patch.object(EventLog, '_read_log', autospec=True, side_effect=EventLog._read_log) as read:
            other.append(make_event())
            other.append(make_event())
        self.assertEqual(read.call_count, 1)

    def test_read_checks_the_log_once_for_each_version_of_the_file(self):
        self.log.append(make_event())
        with mock.patch('event_log.le.validate_log_text', wraps=learning_event.validate_log_text) as check:
            self.log.read()
            self.log.read()
            self.log.export()
            self.assertEqual(check.call_count, 1)
            self.log.append(make_event())
            self.log.read()
            self.assertEqual(check.call_count, 2)

    def test_a_caller_cannot_change_the_cached_report(self):
        self.log.append(make_event())
        report = self.log.read()
        report['events'].clear()
        report['lines'].clear()
        again = self.log.read()
        self.assertEqual(len(again['events']), 1)
        self.assertEqual(len(again['lines']), 1)

    def replaced(self, how):
        """Cache the id of event A, then change the file so that it holds event B with exactly one of size, mtime, or inode different."""
        first, second = make_event(), make_event()
        self.log.append(first)
        self.log.read()
        before = os.stat(self.path)
        if how == 'size':
            self.path.write_bytes(line_of(second) + b'\n')
            os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif how == 'mtime':
            self.path.write_bytes(line_of(second))
            os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
        else:
            temp = self.folder / 'copy'
            temp.write_bytes(line_of(second))
            os.utime(temp, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(temp, self.path)
        after = os.stat(self.path)
        same = [after.st_size == before.st_size, after.st_mtime_ns == before.st_mtime_ns, after.st_ino == before.st_ino]
        self.assertEqual(same, {'size': [False, True, True], 'mtime': [True, False, True], 'inode': [True, True, False]}[how])
        return first, second

    def test_a_change_of_size_alone_rebuilds_the_id_index_and_the_report(self):
        first, second = self.replaced('size')
        self.assertEqual(self.log.read()['lines'][0]['ok'], True)
        self.assertEqual(self.log.read()['events'][0]['event']['eventId'], second['eventId'])
        self.refused('duplicate_event_id', self.log.append, second)
        self.assertTrue(self.log.append(first)['appended'])

    def test_a_change_of_modified_time_alone_rebuilds_the_id_index_and_the_report(self):
        first, second = self.replaced('mtime')
        self.assertEqual(self.log.read()['events'][0]['event']['eventId'], second['eventId'])
        self.refused('duplicate_event_id', self.log.append, second)
        self.assertTrue(self.log.append(first)['appended'])

    def test_a_change_of_inode_alone_rebuilds_the_id_index_and_the_report(self):
        first, second = self.replaced('inode')
        self.assertEqual(self.log.read()['events'][0]['event']['eventId'], second['eventId'])
        self.refused('duplicate_event_id', self.log.append, second)
        self.assertTrue(self.log.append(first)['appended'])

    def test_the_default_lock_timeout_is_two_seconds(self):
        self.assertEqual(event_log.LOCK_TIMEOUT_SECONDS, 2.0)
        self.assertEqual(EventLog(self.data).lock_timeout, 2.0)


class WindowTests(SafetyCase):
    def fill(self, count):
        events = [make_event() for _ in range(count)]
        for event in events:
            self.log.append(event)
        return events

    def test_a_window_holds_the_lines_after_the_offset(self):
        events = self.fill(10)
        report = self.log.read(limit=3, offset=2)
        self.assertEqual([item['line'] for item in report['lines']], [3, 4, 5])
        self.assertEqual([item['event'] for item in report['events']], events[2:5])
        self.assertEqual((report['total'], report['offset'], report['limit'], report['more']), (10, 2, 3, True))

    def test_the_last_window_says_there_is_no_more(self):
        self.fill(10)
        report = self.log.read(limit=5, offset=5)
        self.assertEqual((len(report['lines']), report['more']), (5, False))
        report = self.log.read(limit=5, offset=9)
        self.assertEqual((len(report['lines']), report['more']), (1, False))

    def test_a_window_past_the_end_is_empty_and_says_how_many_lines_there_are(self):
        self.fill(4)
        report = self.log.read(limit=5, offset=40)
        self.assertEqual((report['lines'], report['events'], report['total'], report['more']), ([], [], 4, False))

    def test_ok_and_truncated_tail_describe_the_whole_log_not_the_window(self):
        self.fill(5)
        with open(self.path, 'ab') as handle:
            handle.write(b'{"cut')
        report = self.log.read(limit=2, offset=0)
        self.assertFalse(report['ok'])
        self.assertTrue(report['truncatedTail'])
        self.assertEqual(len(report['lines']), 2)

    def test_a_damaged_line_keeps_its_own_number_in_a_window(self):
        self.fill(2)
        with open(self.path, 'ab') as handle:
            handle.write(b'not json\n')
        report = self.log.read(limit=1, offset=2)
        self.assertEqual([(item['line'], item['ok']) for item in report['lines']], [(3, False)])
        self.assertEqual(report['events'], [])

    def test_no_limit_gives_every_line(self):
        self.fill(7)
        report = self.log.read()
        self.assertEqual((len(report['lines']), len(report['events']), report['more'], report['limit']), (7, 7, False, None))

    def test_a_bad_limit_or_offset_is_refused(self):
        self.fill(1)
        for limit, offset in ((0, 0), (-1, 0), (True, 0), ('3', 0), (1.5, 0), (1, -1), (1, True), (1, '0'), (1, 0.5)):
            with self.subTest(limit=limit, offset=offset):
                error = self.refused('invalid_request', self.log.read, limit=limit, offset=offset)
                self.assertEqual(error.status, 400)


class FlushTests(SafetyCase):
    """A folder entry that did not reach the disk is flushed on the next write, even when the write itself is a repeat."""

    def failing_once(self, target):
        calls, state = [], {'failed': False}

        def flush(directory):
            calls.append(directory)
            if directory == target and not state['failed']:
                state['failed'] = True
                raise OSError(errno.EIO, 'boom')

        return flush, calls

    def test_a_failed_flush_of_the_data_folder_is_tried_again_by_the_next_append(self):
        flush, calls = self.failing_once(self.data)
        with mock.patch('event_log._fsync_dir', side_effect=flush):
            self.refused('fsync_failed', self.log.append, make_event())
            self.assertEqual(calls, [self.data])
            calls.clear()
            self.assertTrue(self.log.append(make_event())['appended'])
        self.assertEqual(calls[0], self.data)
        self.assertIn(self.folder, calls)

    def test_a_failed_flush_of_the_log_folder_is_tried_again_even_when_the_retry_is_a_repeat(self):
        event = make_event()
        flush, calls = self.failing_once(self.folder)
        with mock.patch('event_log._fsync_dir', side_effect=flush):
            error = self.refused('fsync_failed', self.log.append, event)
            self.assertEqual(error.details['maybeWritten'], True)
            calls.clear()
            self.refused('duplicate_event_id', self.log.append, event)
            self.assertEqual(calls, [self.folder])
            calls.clear()
            self.log.append(make_event())
        self.assertEqual(calls, [])

    def test_a_failed_flush_that_never_clears_keeps_blocking_writes(self):
        with mock.patch('event_log._fsync_dir', side_effect=OSError(errno.EIO, 'boom')):
            self.refused('fsync_failed', self.log.append, make_event())
            self.refused('fsync_failed', self.log.append, make_event())
        self.assertEqual(self.path.exists(), False)

    def test_a_new_instance_flushes_a_folder_that_a_crash_left_empty(self):
        make_private(self.folder)
        with mock.patch('event_log._fsync_dir') as flush:
            self.log.append(make_event())
        self.assertEqual([call.args[0] for call in flush.call_args_list], [self.data, self.folder, self.folder])

    def test_a_new_instance_flushes_the_folder_of_a_log_that_an_earlier_try_left_empty(self):
        self.put(b'')
        with mock.patch('event_log._fsync_dir') as flush:
            self.log.append(make_event())
        self.assertEqual([call.args[0] for call in flush.call_args_list], [self.folder])

    def test_a_log_with_content_and_a_healthy_folder_is_not_flushed(self):
        self.log.append(make_event())
        other = EventLog(self.data)
        with mock.patch('event_log._fsync_dir') as flush:
            other.append(make_event())
        flush.assert_not_called()

    def test_a_failed_log_write_that_left_an_empty_file_is_flushed_on_the_retry(self):
        with mock.patch('os.write', side_effect=OSError(errno.ENOSPC, 'full')):
            self.refused('write_failed', self.log.append, make_event())
        self.assertEqual(self.path.stat().st_size, 0)
        with mock.patch('event_log._fsync_dir') as flush:
            self.log.append(make_event())
        self.assertEqual([call.args[0] for call in flush.call_args_list], [self.folder])


class DataFolderTests(SafetyCase):
    def test_a_data_folder_that_the_store_makes_is_private(self):
        data = self.data / 'new' / 'data'
        EventLog(data).append(make_event())
        self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((data / '_events').stat().st_mode), 0o700)

    def test_a_data_folder_that_already_exists_keeps_its_mode(self):
        os.chmod(self.data, 0o755)
        self.addCleanup(os.chmod, self.data, 0o700)
        self.log.append(make_event())
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o755)

    def test_a_new_data_folder_flushes_its_parent(self):
        data = self.data / 'fresh'
        with mock.patch('event_log._fsync_dir') as flush:
            EventLog(data).append(make_event())
        self.assertEqual([call.args[0] for call in flush.call_args_list], [self.data, data, data / '_events'])

    def test_an_events_folder_open_to_others_is_refused_until_it_is_closed(self):
        self.folder.mkdir()
        os.chmod(self.folder, 0o755)
        for mode in (0o755, 0o750, 0o705, 0o770, 0o707):
            os.chmod(self.folder, mode)
            with self.subTest(mode=oct(mode)):
                error = self.refused('unsafe_path', self.log.append, make_event())
                self.assertEqual(error.status, 500)
        self.assertEqual([path.name for path in self.folder.iterdir()], [])
        os.chmod(self.folder, 0o700)
        self.assertTrue(self.log.append(make_event())['appended'])

    def test_a_reflection_folder_open_to_others_is_refused(self):
        make_private(self.folder)
        self.reflections.mkdir()
        os.chmod(self.reflections, 0o755)
        self.refused('unsafe_path', self.log.save_reflection, 'text')
        self.assertEqual(list(self.reflections.iterdir()), [])

    def test_a_folder_that_belongs_to_someone_else_is_refused(self):
        make_private(self.folder)
        with mock.patch('os.getuid', return_value=os.getuid() + 1):
            self.refused('unsafe_path', self.log.append, make_event())

    def test_a_regular_file_in_place_of_the_events_folder_is_log_corrupt_for_every_call(self):
        self.folder.write_text('not a folder')
        for name, call in (('read', self.log.read), ('export', self.log.export), ('append', lambda: self.log.append(make_event())),
                           ('delete', lambda: self.log.delete(confirm=True)), ('save', lambda: self.log.save_reflection('x')),
                           ('get', lambda: self.log.get_reflection(str(uuid.uuid4())))):
            with self.subTest(call=name):
                error = self.refused('log_corrupt', call)
                self.assertEqual(error.status, 500)
        self.assertEqual(self.folder.read_text(), 'not a folder')

    def test_a_regular_file_in_place_of_the_reflection_folder_is_log_corrupt_for_every_call(self):
        make_private(self.folder)
        self.reflections.write_text('not a folder')
        for name, call in (('read', self.log.read), ('export', self.log.export), ('append', lambda: self.log.append(make_event())),
                           ('delete', lambda: self.log.delete(confirm=True)), ('save', lambda: self.log.save_reflection('x')),
                           ('get', lambda: self.log.get_reflection(str(uuid.uuid4())))):
            with self.subTest(call=name):
                self.refused('log_corrupt', call)
        self.assertEqual(self.reflections.read_text(), 'not a folder')

    def test_lock_errors_other_than_a_timeout_are_not_retryable(self):
        make_private(self.folder)
        (self.folder / event_log.LOCK_FILE).mkdir()
        self.assertFalse(self.refused('lock_unavailable', self.log.append, make_event()).retryable)
        (self.folder / event_log.LOCK_FILE).rmdir()
        with mock.patch('fcntl.flock', side_effect=OSError(errno.ENOLCK, 'no locks')):
            self.assertFalse(self.refused('lock_unavailable', self.log.append, make_event()).retryable)

    def test_a_lock_timeout_is_retryable(self):
        make_private(self.folder)
        descriptor = os.open(self.folder / event_log.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self.assertTrue(self.refused('lock_unavailable', self.log.append, make_event()).retryable)


class ReaderLockTests(SafetyCase):
    def test_a_reader_makes_the_lock_file_and_takes_a_shared_lock(self):
        self.put(line_of(make_event()))
        self.assertFalse((self.folder / event_log.LOCK_FILE).exists())
        with mock.patch('fcntl.flock', wraps=fcntl.flock) as lock:
            self.assertTrue(self.log.read()['ok'])
        self.assertTrue((self.folder / event_log.LOCK_FILE).is_file())
        modes = [call.args[1] & ~fcntl.LOCK_NB for call in lock.call_args_list]
        self.assertEqual(modes, [fcntl.LOCK_SH])

    def test_export_takes_the_shared_lock_too(self):
        self.put(b'')
        with mock.patch('fcntl.flock', wraps=fcntl.flock) as lock:
            self.log.export()
        self.assertEqual([call.args[1] & ~fcntl.LOCK_NB for call in lock.call_args_list], [fcntl.LOCK_SH])

    def test_a_reader_never_makes_a_folder(self):
        self.log.read()
        self.assertEqual(list(self.data.iterdir()), [])
        self.refused('log_missing', self.log.export)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_a_reader_waits_for_the_writer_that_made_the_lock_file(self):
        self.put(line_of(make_event()))
        descriptor = os.open(self.folder / event_log.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self.refused('lock_unavailable', self.log.read)


class AfterTheWriteTests(SafetyCase):
    def test_a_failed_fstat_after_the_flush_still_reports_success_and_drops_the_cache(self):
        self.log.append(make_event())
        flushed = {'done': False}
        real_fsync, real_fstat = os.fsync, os.fstat

        def fsync(descriptor):
            real_fsync(descriptor)
            flushed['done'] = True

        def fstat(descriptor):
            if flushed['done']:
                raise OSError(errno.EIO, 'boom')
            return real_fstat(descriptor)

        event = make_event()
        with mock.patch('os.fsync', side_effect=fsync), mock.patch('os.fstat', side_effect=fstat):
            result = self.log.append(event)
        self.assertTrue(result['appended'])
        self.assertEqual(len(self.path.read_bytes().splitlines()), 2)
        with mock.patch.object(EventLog, '_read_log', autospec=True, side_effect=EventLog._read_log) as read:
            self.refused('duplicate_event_id', self.log.append, event)
        self.assertEqual(read.call_count, 1)

    def test_a_failed_close_of_the_lock_after_the_work_does_not_turn_success_into_an_error(self):
        self.log.append(make_event())
        lock_descriptors, failed = [], []
        real_open, real_close = os.open, os.close

        def opening(path, *rest, **named):
            descriptor = real_open(path, *rest, **named)
            if str(path).endswith(event_log.LOCK_FILE):
                lock_descriptors.append(descriptor)
            return descriptor

        def closing(descriptor):
            real_close(descriptor)
            if descriptor in lock_descriptors:
                lock_descriptors.remove(descriptor)
                failed.append(descriptor)
                raise OSError(errno.EIO, 'boom')

        with mock.patch('os.open', side_effect=opening), mock.patch('os.close', side_effect=closing):
            self.assertTrue(self.log.append(make_event())['appended'])
            self.assertTrue(self.log.read()['ok'])
        self.assertEqual(len(failed), 2)

    def test_a_failed_close_of_a_reflection_temp_file_closes_it_only_once(self):
        temp_descriptors, closed = [], []
        real_open, real_close = os.open, os.close

        def opening(path, *rest, **named):
            descriptor = real_open(path, *rest, **named)
            if str(path).endswith('.tmp'):
                temp_descriptors.append(descriptor)
            return descriptor

        def closing(descriptor):
            closed.append(descriptor)
            real_close(descriptor)
            if descriptor in temp_descriptors and closed.count(descriptor) == 1:
                raise OSError(errno.EIO, 'boom')

        with mock.patch('os.open', side_effect=opening), mock.patch('os.close', side_effect=closing):
            self.refused('write_failed', self.log.save_reflection, 'text')
        self.assertEqual(len(temp_descriptors), 1)
        self.assertEqual(closed.count(temp_descriptors[0]), 1)
        self.assertEqual(list(self.reflections.iterdir()), [])


class OddFileTests(SafetyCase):
    """A FIFO in place of a file must give an error, never a hang."""

    def fifo(self, path):
        make_private(path.parent)
        os.mkfifo(path)

        def release():
            # A blocked opener would wait forever. Open the other end so that it can finish.
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                os.close(descriptor)
            except OSError:
                pass

        self.addCleanup(release)

    def promptly(self, call):
        outcome = {}

        def run():
            try:
                outcome['value'] = call()
            except EventLogError as error:
                outcome['error'] = error

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), 'the call hung on a FIFO')
        return outcome

    def test_the_log_as_a_fifo_is_log_corrupt_for_every_call(self):
        self.fifo(self.path)
        for name, call in (('read', self.log.read), ('export', self.log.export), ('append', lambda: self.log.append(make_event())),
                           ('delete', lambda: self.log.delete(confirm=True))):
            with self.subTest(call=name):
                self.assertEqual(self.promptly(call)['error'].code, 'log_corrupt')

    def test_opening_a_fifo_for_reading_does_not_block(self):
        self.fifo(self.path)
        self.assertEqual(self.promptly(lambda: self.log._read_log('log_corrupt'))['error'].code, 'log_corrupt')

    def test_opening_a_fifo_for_writing_does_not_block(self):
        self.fifo(self.path)
        empty = event_log._State(None, 0, 0, True, set())
        with mock.patch.object(EventLog, '_load_state', return_value=empty):
            error = self.promptly(lambda: self.log._append_locked(str(uuid.uuid4()), b'{}\n', False))['error']
        self.assertEqual(error.code, 'write_failed')

    def test_the_lock_file_as_a_fifo_is_lock_unavailable(self):
        self.fifo(self.folder / event_log.LOCK_FILE)
        # Some systems refuse to lock a FIFO. Others allow it. The store must refuse the FIFO on either kind.
        with mock.patch('fcntl.flock'):
            outcome = self.promptly(lambda: self.log.append(make_event()))
        self.assertEqual(outcome['error'].code, 'lock_unavailable')
        self.assertFalse(self.path.exists())

    def test_a_reflection_file_as_a_fifo_is_reflection_corrupt(self):
        identifier = str(uuid.uuid4())
        self.fifo(self.reflections / f'{identifier}.json')
        self.assertEqual(self.promptly(lambda: self.log.get_reflection(identifier))['error'].code, 'reflection_corrupt')


if __name__ == '__main__':
    unittest.main()
