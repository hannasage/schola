"""Reflections: separate local files fetched by id. The text never enters the log or an error message."""
import errno
import json
import os
from pathlib import Path
import re
import sys
import tempfile
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
SECRET = 'SECRET-REFLECTION-TEXT-9d1f'
LOWER_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def tree(directory):
    return sorted(str(path.relative_to(directory)) for path in Path(directory).rglob('*'))


class ReflectionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.folder = self.data / '_events' / 'reflections'
        self.log = EventLog(self.data, lock_timeout=0.2)

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(EventLogError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def assert_no_text(self, error):
        for shown in (str(error), repr(error), json.dumps(error.payload())):
            self.assertNotIn(SECRET, shown)


class SaveAndGetTests(ReflectionCase):
    def test_round_trip(self):
        text = 'I used a loop because the list had a known size.'
        saved = self.log.save_reflection(text)
        self.assertEqual(set(saved), {'reflectionId', 'charCount'})
        self.assertEqual(saved['charCount'], 48)
        self.assertRegex(saved['reflectionId'], LOWER_UUID)
        self.assertEqual(self.log.get_reflection(saved['reflectionId']),
                         {'reflectionId': saved['reflectionId'], 'charCount': 48, 'text': text})

    def test_text_comes_back_exactly(self):
        for text in ('line one\nline two\r\n\ttabbed', 'quote " backslash \\ slash /', 'é ü 日本語 🙂 ZWJ‍', '<script>alert(1)</script>',
                     "'; DROP TABLE x; --", '\u0000 nul and \u001f control', ' leading and trailing '):
            with self.subTest(text=text):
                saved = self.log.save_reflection(text)
                self.assertEqual(saved['charCount'], len(text))
                self.assertEqual(self.log.get_reflection(saved['reflectionId'])['text'], text)

    def test_the_count_is_in_characters_not_bytes(self):
        saved = self.log.save_reflection('🙂' * 10)
        self.assertEqual(saved['charCount'], 10)

    def test_each_save_gets_its_own_id_and_file(self):
        ids = {self.log.save_reflection('same text')['reflectionId'] for _ in range(5)}
        self.assertEqual(len(ids), 5)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), sorted(f'{item}.json' for item in ids))

    def test_the_text_is_stored_apart_from_the_log(self):
        saved = self.log.save_reflection(SECRET)
        self.assertFalse((self.data / '_events' / 'events.jsonl').exists())
        event = json.loads((FIXTURES / 'valid' / 'reflection-saved.json').read_text(encoding='utf-8'))
        event.update(eventId=str(uuid.uuid4()))
        event['data'].update(reflectionId=saved['reflectionId'], charCount=saved['charCount'])
        self.log.append(event)
        self.assertNotIn(SECRET.encode(), self.log.export())

    def test_a_save_leaves_no_temporary_file_and_a_private_file(self):
        saved = self.log.save_reflection('text')
        names = [path.name for path in self.folder.iterdir()]
        self.assertEqual(names, [f"{saved['reflectionId']}.json"])
        self.assertEqual((self.folder / names[0]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)

    def test_a_save_flushes_the_file_and_the_folders(self):
        with mock.patch('os.fsync', wraps=os.fsync) as fsync, mock.patch('event_log._fsync_dir') as flush:
            self.log.save_reflection('text')
        self.assertEqual(fsync.call_count, 1)
        self.assertEqual([call.args[0] for call in flush.call_args_list], [self.data, self.data / '_events', self.folder])

    def test_the_limits_match_the_contract(self):
        schema = json.loads((ROOT / 'contracts' / 'learning-event.v1.schema.json').read_text(encoding='utf-8'))
        text = json.dumps(schema)
        self.assertIn('"maximum": 10000', text)
        self.assertEqual(event_log.MAX_REFLECTION_CHARS, 10000)
        event = json.loads((FIXTURES / 'valid' / 'reflection-saved.json').read_text(encoding='utf-8'))
        saved = self.log.save_reflection('x')
        event['data'] = {'reflectionId': saved['reflectionId'], 'charCount': event_log.MAX_REFLECTION_CHARS}
        self.assertTrue(learning_event.validate_event(event)['ok'])
        event['data']['charCount'] = event_log.MAX_REFLECTION_CHARS + 1
        self.assertFalse(learning_event.validate_event(event)['ok'])


class SizeTests(ReflectionCase):
    def test_10000_characters_are_accepted(self):
        saved = self.log.save_reflection('a' * 10000)
        self.assertEqual(saved['charCount'], 10000)
        self.assertEqual(len(self.log.get_reflection(saved['reflectionId'])['text']), 10000)

    def test_10000_wide_and_escaped_characters_are_accepted_and_read_back(self):
        for text in ('🙂' * 10000, '\u0001' * 10000, '"' * 10000):
            with self.subTest(text=text[:1]):
                saved = self.log.save_reflection(text)
                self.assertEqual(self.log.get_reflection(saved['reflectionId'])['text'], text)

    def test_10001_characters_are_refused_and_write_nothing(self):
        error = self.refused('invalid_reflection', self.log.save_reflection, 'a' * 10001)
        self.assertEqual(error.status, 422)
        self.assertEqual(tree(self.data), [])

    def test_one_character_is_accepted_and_empty_is_refused(self):
        self.assertEqual(self.log.save_reflection('a')['charCount'], 1)
        self.refused('invalid_reflection', self.log.save_reflection, '')

    def test_text_that_is_not_text_is_refused_and_writes_nothing(self):
        for value in (None, 5, b'bytes', ['a'], {'text': 'a'}, True):
            with self.subTest(value=value):
                self.refused('invalid_reflection', self.log.save_reflection, value)
        self.assertEqual(tree(self.data), [])

    def test_text_that_cannot_be_utf8_is_refused(self):
        self.refused('invalid_reflection', self.log.save_reflection, 'a\ud800b')
        self.assertEqual(tree(self.data), [])


class IdTests(ReflectionCase):
    def test_an_unknown_id_is_not_found(self):
        self.log.save_reflection('text')
        error = self.refused('reflection_not_found', self.log.get_reflection, str(uuid.uuid4()))
        self.assertEqual(error.status, 404)

    def test_an_unknown_id_with_no_store_at_all_is_not_found(self):
        self.refused('reflection_not_found', self.log.get_reflection, str(uuid.uuid4()))
        self.assertEqual(tree(self.data), [])

    def test_ids_that_are_not_lowercase_uuids_are_refused_before_the_disk_is_touched(self):
        good = str(uuid.uuid4())
        bad = ['', '..', '../../etc/passwd', '../secret', good + '\n', good + '/..', good + '.json', good.upper(), good.replace('-', ''),
               f'..%2f{good}', ' ' + good, good[:-1], good + '0', 'g' + good[1:], '٠' * 8 + good[8:], 'a' * 5000, None, 5, b'x', ['a'], {}]
        for value in bad:
            with self.subTest(value=value):
                with mock.patch('os.open', side_effect=AssertionError('touched the disk')), mock.patch('os.lstat', side_effect=AssertionError('touched the disk')):
                    error = self.refused('invalid_reflection_id', self.log.get_reflection, value)
                self.assertEqual(error.status, 400)

    def test_one_uppercase_letter_in_any_group_is_refused(self):
        base = 'abcdefab-abcd-abcd-abcd-abcdefabcdef'
        for index, character in enumerate(base):
            if character.isalpha():
                with self.subTest(index=index):
                    self.refused('invalid_reflection_id', self.log.get_reflection, base[:index] + character.upper() + base[index + 1:])

    def test_a_path_that_leaves_the_store_is_never_read(self):
        (self.data / 'secret.json').write_text(json.dumps({'text': SECRET}))
        self.log.save_reflection('text')
        for value in ('../../secret', '../../secret.json', str(self.data / 'secret')):
            with self.subTest(value=value):
                error = self.refused('invalid_reflection_id', self.log.get_reflection, value)
                self.assert_no_text(error)


class FailureTests(ReflectionCase):
    def leftovers(self):
        return [path for path in self.folder.rglob('*')] if self.folder.exists() else []

    def test_a_failed_file_flush_is_reported_and_leaves_no_file(self):
        self.log.save_reflection('first')  # builds the folders
        before = tree(self.data)
        with mock.patch('os.fsync', side_effect=OSError(errno.EIO, 'boom')):
            error = self.refused('fsync_failed', self.log.save_reflection, SECRET)
        self.assertEqual(error.details['maybeWritten'], False)
        self.assertEqual(tree(self.data), before)
        self.assert_no_text(error)

    def test_a_failed_folder_flush_after_the_rename_says_the_reflection_may_be_saved(self):
        self.log.save_reflection('first')
        with mock.patch('event_log._fsync_dir', side_effect=OSError(errno.EIO, 'boom')):
            error = self.refused('fsync_failed', self.log.save_reflection, SECRET)
        self.assertEqual(error.details['maybeWritten'], True)
        self.assertEqual(len(list(self.folder.glob('*.json'))), 2)
        self.assert_no_text(error)

    def test_a_write_error_is_write_failed_and_leaves_no_file(self):
        self.log.save_reflection('first')
        before = tree(self.data)
        with mock.patch('os.write', side_effect=OSError(errno.ENOSPC, 'full')):
            error = self.refused('write_failed', self.log.save_reflection, SECRET)
        self.assertEqual(tree(self.data), before)
        self.assert_no_text(error)

    def test_a_short_write_is_write_failed_and_leaves_no_file(self):
        self.log.save_reflection('first')
        before = tree(self.data)
        real = os.write
        with mock.patch('os.write', side_effect=lambda descriptor, data: real(descriptor, data[:5])):
            error = self.refused('write_failed', self.log.save_reflection, SECRET)
        self.assertEqual(tree(self.data), before)
        self.assert_no_text(error)

    def test_a_failed_rename_is_write_failed_and_leaves_no_file(self):
        self.log.save_reflection('first')
        before = tree(self.data)
        with mock.patch('os.replace', side_effect=OSError(errno.EXDEV, 'no')):
            self.refused('write_failed', self.log.save_reflection, SECRET)
        self.assertEqual(tree(self.data), before)

    def test_a_folder_that_cannot_be_made_is_write_failed(self):
        (self.data / '_events').write_text('a file, not a folder')
        error = self.refused('write_failed', self.log.save_reflection, SECRET)
        self.assert_no_text(error)

    def test_a_symbolic_link_is_refused_for_the_folder_and_for_a_file(self):
        saved = self.log.save_reflection('text')
        outside = self.data / 'outside.json'
        outside.write_text('{}')
        (self.folder / f'{uuid.uuid4()}.json').symlink_to(outside)
        link = next(path for path in self.folder.iterdir() if path.is_symlink())
        self.refused('unsafe_path', self.log.get_reflection, link.stem)
        self.assertEqual(self.log.get_reflection(saved['reflectionId'])['text'], 'text')
        real = self.data / '_events' / 'real'
        self.folder.rename(real)
        self.folder.symlink_to(real, target_is_directory=True)
        self.refused('unsafe_path', self.log.save_reflection, 'more')
        self.refused('unsafe_path', self.log.get_reflection, saved['reflectionId'])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can read every file')
    def test_an_unreadable_file_is_reflection_unreadable(self):
        saved = self.log.save_reflection(SECRET)
        path = self.folder / f"{saved['reflectionId']}.json"
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        error = self.refused('reflection_unreadable', self.log.get_reflection, saved['reflectionId'])
        self.assertNotIn(str(self.data), json.dumps(error.payload()))


class CorruptTests(ReflectionCase):
    def stored(self, content):
        saved = self.log.save_reflection('ok')
        path = self.folder / f"{saved['reflectionId']}.json"
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
        return saved['reflectionId'], path

    def test_damaged_files_are_reflection_corrupt_kept_and_never_echoed(self):
        identifier = str(uuid.uuid4())
        good = {'version': 1, 'reflectionId': None, 'charCount': len(SECRET), 'text': SECRET}
        cases = {
            'not json': b'{"text": "' + SECRET.encode() + b'"',
            'not utf-8': b'\xff\xfe' + SECRET.encode(),
            'array': [SECRET],
            'missing field': {k: v for k, v in good.items() if k != 'charCount'},
            'extra field': dict(good, extra=1),
            'wrong version': dict(good, version=2),
            'wrong id': dict(good, reflectionId=identifier),
            'wrong count': dict(good, charCount=1),
            'count not int': dict(good, charCount=float(len(SECRET))),
            'empty text': dict(good, text='', charCount=0),
            'text not text': dict(good, text=[SECRET]),
            'too long': dict(good, text='a' * 10001, charCount=10001),
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                saved = self.log.save_reflection('ok')
                if isinstance(content, dict) and name != 'wrong id':
                    content = dict(content, reflectionId=saved['reflectionId'])
                path = self.folder / f"{saved['reflectionId']}.json"
                data = content if isinstance(content, bytes) else json.dumps(content).encode()
                path.write_bytes(data)
                error = self.refused('reflection_corrupt', self.log.get_reflection, saved['reflectionId'])
                self.assertEqual(error.status, 500)
                self.assert_no_text(error)
                self.assertEqual(path.read_bytes(), data)

    def test_a_valid_file_padded_over_the_size_limit_is_reflection_corrupt(self):
        saved = self.log.save_reflection('ok')
        path = self.folder / f"{saved['reflectionId']}.json"
        path.write_bytes(path.read_bytes() + b' ' * event_log.MAX_REFLECTION_BYTES)
        self.refused('reflection_corrupt', self.log.get_reflection, saved['reflectionId'])
        padded = self.log.save_reflection('ok')
        path = self.folder / f"{padded['reflectionId']}.json"
        path.write_bytes(path.read_bytes().rstrip() + b' ' * (event_log.MAX_REFLECTION_BYTES - len(path.read_bytes().rstrip())))
        self.assertEqual(self.log.get_reflection(padded['reflectionId'])['text'], 'ok')

    def test_a_folder_in_place_of_a_file_is_reflection_corrupt(self):
        identifier = str(uuid.uuid4())
        (self.folder / f'{identifier}.json').mkdir(parents=True)
        self.refused('reflection_corrupt', self.log.get_reflection, identifier)


if __name__ == '__main__':
    unittest.main()
