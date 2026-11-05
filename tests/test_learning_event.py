"""Tests for the stdlib-only learning event validator (learning_event.py).

The validator reads the real contracts/*.schema.json files. The shared
conformance manifest is the main guard: every case must give the result and
the stage that the manifest names.
"""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest

import learning_event as le

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'tests' / 'fixtures' / 'contracts'
MANIFEST = FIXTURES / 'learning-event-conformance.json'


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def read(name):
    return (FIXTURES / 'learning-events' / name).read_text(encoding='utf-8')


def good(name):
    return json.loads(read(f'valid/{name}.json'))


def pointers(result):
    return sorted(error['pointer'] for error in result.get('errors', []))


def expected_for(entry):
    if entry['expect'] == 'valid':
        return True, None
    return False, entry['expect'].split(':', 1)[1]


def event_mismatches(manifest, fixtures):
    """Return one line for every event entry the validator disagrees with."""
    found = []
    for entry in manifest:
        if entry['kind'] != 'learning-event':
            continue
        text = (fixtures / entry['file']).read_text(encoding='utf-8')
        result, stage = le.validate_event_text_with_stage(text)
        ok, reason = expected_for(entry)
        if result['ok'] != ok:
            found.append(f"{entry['name']}: ok {result['ok']} != {ok}")
        elif not ok and result['reason'] != reason:
            found.append(f"{entry['name']}: reason {result['reason']} != {reason}")
        if stage != entry['stage']:
            found.append(f"{entry['name']}: stage {stage} != {entry['stage']}")
    return found


def bad_lines(result):
    return {str(line['line']): line['reason'] for line in result['lines'] if not line['ok']}


class ConformanceTests(unittest.TestCase):
    def test_every_event_case_matches_result_reason_and_stage(self):
        entries = [entry for entry in load_manifest() if entry['kind'] == 'learning-event']
        self.assertGreaterEqual(len(entries), 50)
        for entry in entries:
            with self.subTest(case=entry['name']):
                text = (FIXTURES / entry['file']).read_text(encoding='utf-8')
                result, stage = le.validate_event_text_with_stage(text)
                ok, reason = expected_for(entry)
                self.assertEqual(result['ok'], ok, result)
                if ok:
                    self.assertEqual(list(result), ['ok'])
                else:
                    self.assertEqual(result['reason'], reason, result)
                    self.assertTrue(result['errors'])
                    for error in result['errors']:
                        self.assertEqual(sorted(error), ['message', 'pointer'])
                self.assertEqual(stage, entry['stage'])
                if stage not in ('parse', 'repeat'):
                    self.assertEqual(le.validate_event(json.loads(text)), result)

    def test_every_log_case_matches_lines_and_tail(self):
        entries = [entry for entry in load_manifest() if entry['kind'] == 'learning-event-log']
        self.assertGreaterEqual(len(entries), 10)
        for entry in entries:
            with self.subTest(case=entry['name']):
                path = FIXTURES / entry['file']
                text = path.read_text(encoding='utf-8')
                result = le.validate_log_text(text)
                self.assertEqual(sorted(result), ['lines', 'ok', 'truncatedTail'])
                self.assertEqual(result['ok'], entry['expect'] == 'valid')
                self.assertEqual(result['truncatedTail'], entry['truncatedTail'])
                self.assertEqual(bad_lines(result), entry['invalidLines'])
                for number, line in enumerate(result['lines'], start=1):
                    self.assertEqual(line['line'], number)
                    if line['ok']:
                        self.assertEqual(sorted(line), ['line', 'ok'])
                    else:
                        self.assertEqual(sorted(line), ['errors', 'line', 'ok', 'reason'])
                        self.assertTrue(line['errors'])
                self.assertEqual(le.validate_log_text(text.encode('utf-8')), result)
                self.assertEqual(le.validate_log_file(path), result)

    def test_the_manifest_matches_the_disk_in_both_directions(self):
        listed = {entry['file'] for entry in load_manifest()}
        self.assertEqual(len(listed), len(load_manifest()))
        on_disk = set()
        for folder in ('valid', 'invalid'):
            for item in (FIXTURES / 'learning-events' / folder).iterdir():
                on_disk.add(f'learning-events/{folder}/{item.name}')
        self.assertEqual(on_disk, listed)

    def test_every_event_type_has_a_valid_fixture(self):
        seen = set()
        for entry in load_manifest():
            if entry['kind'] == 'learning-event' and entry['expect'] == 'valid':
                seen.add(json.loads((FIXTURES / entry['file']).read_text(encoding='utf-8'))['type'])
        self.assertEqual(seen, set(le.EVENT_TYPES))
        self.assertEqual(len(le.EVENT_TYPES), 10)
        self.assertNotIn('confidence_rated', le.EVENT_TYPES)

    def test_fixture_files_keep_their_bytes_and_times(self):
        before = {}
        for entry in load_manifest():
            path = FIXTURES / entry['file']
            before[path] = (path.read_bytes(), path.stat().st_mtime_ns)
            if entry['kind'] == 'learning-event-log':
                le.validate_log_file(path)
            else:
                le.validate_event_text(path.read_text(encoding='utf-8'))
        for path, (data, mtime) in before.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mtime_ns, mtime)


class MutationTests(unittest.TestCase):
    """Prove the conformance check can fail, using temporary copies only."""

    def make_copy(self):
        folder = tempfile.mkdtemp(prefix='schola-event-mutation-')
        self.addCleanup(shutil.rmtree, folder, True)
        target = Path(folder) / 'contracts-fixtures'
        shutil.copytree(FIXTURES, target)
        return target

    def test_the_real_manifest_has_no_mismatch(self):
        self.assertEqual(event_mismatches(load_manifest(), FIXTURES), [])

    def test_a_flipped_expectation_is_reported_for_every_event_case(self):
        manifest = [entry for entry in load_manifest() if entry['kind'] == 'learning-event']
        for entry in manifest:
            entry['expect'] = 'invalid:invalid' if entry['expect'] == 'valid' else 'valid'
        report = event_mismatches(manifest, FIXTURES)
        for entry in manifest:
            self.assertTrue(any(line.startswith(entry['name'] + ':') for line in report), entry['name'])

    def test_a_flipped_stage_is_reported(self):
        manifest = [entry for entry in load_manifest() if entry['kind'] == 'learning-event']
        for entry in manifest:
            entry['stage'] = 'schema' if entry['stage'] != 'schema' else 'semantic'
        self.assertEqual(len(event_mismatches(manifest, FIXTURES)), len(manifest))

    def test_a_repaired_parse_fixture_is_accepted(self):
        copy_root = self.make_copy()
        repaired = (copy_root / 'learning-events' / 'valid' / 'task-opened.json').read_text(encoding='utf-8')
        path = copy_root / 'learning-events' / 'invalid' / 'damaged-json.json'
        self.assertEqual(le.validate_event_text_with_stage(path.read_text(encoding='utf-8'))[1], 'parse')
        path.write_text(repaired, encoding='utf-8')
        self.assertEqual(le.validate_event_text_with_stage(path.read_text(encoding='utf-8')), ({'ok': True}, 'none'))

    def test_a_version_fixture_with_major_one_reaches_the_schema(self):
        document = json.loads(read('invalid/unsupported-major-version.json'))
        self.assertEqual(le.validate_event(document)['reason'], 'unsupported_version')
        document['schemaVersion'] = '1.0'
        self.assertEqual(le.validate_event(document), {'ok': True})

    def test_a_broken_valid_fixture_is_rejected(self):
        document = good('attempt-submitted')
        del document['data']['artifactDigest']
        result = le.validate_event(document)
        self.assertFalse(result['ok'])
        self.assertIn('/data/artifactDigest', pointers(result))


class EventRuleTests(unittest.TestCase):
    def test_the_input_is_never_changed(self):
        for name in ('attempt-submitted', 'hint-requested-nudge-null-attempt', 'review-result'):
            document = good(name)
            snapshot = copy.deepcopy(document)
            le.validate_event(document)
            self.assertEqual(document, snapshot)

    def test_non_objects_are_invalid(self):
        for value in (None, 3, 'text', [], True):
            with self.subTest(value=value):
                self.assertEqual(le.validate_event(value)['reason'], 'invalid')

    def test_only_major_one_is_supported_and_it_is_read_first(self):
        base = good('task-opened')
        for marker in ('2.0', '0.9', '10.0', '2.0.0', '9' * 400 + '.0'):
            with self.subTest(marker=marker):
                result = le.validate_event({**base, 'schemaVersion': marker, 'type': 'not-a-type', 'extra': 1})
                self.assertEqual(result['reason'], 'unsupported_version')
                self.assertEqual(pointers(result), ['/schemaVersion'])
        for marker in ('1', '1.', '1.x', 'v1.0', 1, None, '', '01.0', '1.0.0'):
            with self.subTest(marker=marker):
                self.assertEqual(le.validate_event({**base, 'schemaVersion': marker})['reason'], 'invalid')
        self.assertTrue(le.validate_event({**base, 'schemaVersion': '1.999999'})['ok'])

    def test_a_hint_needs_an_attempt_at_the_top_two_levels(self):
        base = good('hint-requested-full-answer-accepted-offer')
        for level in ('nudge', 'question'):
            self.assertTrue(le.validate_event({**base, 'data': {**base['data'], 'level': level, 'attemptId': None}})['ok'])
        for level in ('partial-example', 'full-answer'):
            result = le.validate_event({**base, 'data': {**base['data'], 'level': level, 'attemptId': None}})
            self.assertFalse(result['ok'])
            self.assertIn('/data/attemptId', pointers(result))
            self.assertTrue(le.validate_event({**base, 'data': {**base['data'], 'level': level}})['ok'])

    def test_raw_content_fields_are_refused_in_every_valid_event(self):
        for entry in load_manifest():
            if entry['kind'] != 'learning-event' or entry['expect'] != 'valid' or entry['name'].startswith('envelope-'):
                continue
            document = json.loads((FIXTURES / entry['file']).read_text(encoding='utf-8'))
            for field in ('code', 'text', 'output', 'stdout', 'stderr', 'body', 'content', 'prompt', 'path'):
                with self.subTest(case=entry['name'], field=field):
                    inside = copy.deepcopy(document)
                    inside['data'][field] = 'x'
                    self.assertFalse(le.validate_event(inside)['ok'])
                    outside = copy.deepcopy(document)
                    outside[field] = 'x'
                    self.assertFalse(le.validate_event(outside)['ok'])

    def test_one_character_from_every_hidden_or_control_range_is_refused_in_evidence(self):
        base = good('check-result-passed')
        for label, code in (('tag character', 0xE0041), ('zero-width space', 0x200B), ('left-to-right mark', 0x200E),
                            ('right-to-left mark', 0x200F), ('arabic letter mark', 0x61C), ('first isolate', 0x2066),
                            ('last isolate', 0x2069), ('delete', 0x7F), ('next line', 0x85), ('last C1 control', 0x9F),
                            ('right-to-left override', 0x202E), ('line separator', 0x2028), ('start of heading', 0x01),
                            ('lone surrogate', 0xD800)):
            with self.subTest(label=label):
                document = copy.deepcopy(base)
                document['data']['evidence'] = f'a{chr(code)}b'
                self.assertFalse(le.validate_event(document)['ok'])
        document = copy.deepcopy(base)
        document['data']['evidence'] = "Line 4: expected (a, b) got 'c'; see x_y/z-1."
        self.assertTrue(le.validate_event(document)['ok'])

    def test_integer_bounds_hold_at_the_edge(self):
        table = [
            ('attempt-submitted', 'artifactBytes', 0, 104857600),
            ('check-result-passed', 'durationMs', 0, 3600000),
            ('reflection-saved', 'charCount', 1, 10000),
            ('review-result', 'intervalDays', 0, 3650),
            ('module-completed', 'checksPassed', 0, 1000),
            ('module-completed', 'tasksTotal', 0, 1000),
        ]
        for name, field, low, high in table:
            base = good(name)
            for value, ok in ((high, True), (high + 1, False), (low, True), (low - 1, False), (1.5, False), ('5', False), (None, False), (True, False)):
                with self.subTest(field=field, value=value):
                    self.assertEqual(le.validate_event({**base, 'data': {**base['data'], field: value}})['ok'], ok)

    def test_an_attempt_id_valid_in_a_help_record_is_valid_in_an_event_and_the_reverse(self):
        import lesson_contract as lc
        record = json.loads((FIXTURES / 'valid' / 'help-record-partial-example-student.json').read_text(encoding='utf-8'))
        hint = good('hint-requested-partial-example')
        ids = ['a', 'A', '0', 'try_2-b', 'a' * 64, 'a' * 65, '', 'has space', 'a.b', 'a/b', 'a\n', '\u00e9',
               'a1b2c3d4-0000-4000-8000-000000000001', 'A1B2C3D4-0000-4000-8000-000000000001', '-', '_']
        for attempt_id in ids:
            with self.subTest(attempt_id=attempt_id):
                as_record = lc.validate_lesson_contract('help-record', {**record, 'attemptId': attempt_id})['ok']
                as_event = le.validate_event({**hint, 'data': {**hint['data'], 'attemptId': attempt_id}})['ok']
                self.assertEqual(as_event, as_record)
        self.assertTrue(le.validate_event({**hint, 'data': {**hint['data'], 'attemptId': 'attempt-3'}})['ok'])

    def test_the_app_alone_writes_four_event_types(self):
        for name in ('attempt-submitted', 'check-result-passed', 'task-finished-passed', 'module-completed'):
            base = good(name)
            self.assertEqual(base['source'], 'app', name)
            for source in ('student', 'agent', 'teacher', None):
                with self.subTest(name=name, source=source):
                    result = le.validate_event({**base, 'source': source})
                    self.assertFalse(result['ok'])
                    self.assertIn('/source', pointers(result))
        for name in ('hint-requested-nudge-null-attempt', 'agent-offer-pending', 'reflection-saved', 'task-opened', 'mode-changed', 'review-result'):
            for source in ('student', 'app', 'agent'):
                with self.subTest(name=name, source=source):
                    self.assertTrue(le.validate_event({**good(name), 'source': source})['ok'])

    def test_a_task_id_is_required_for_five_types_and_null_for_module_completed(self):
        for name in ('attempt-submitted', 'check-result-passed', 'hint-requested-nudge-null-attempt', 'task-opened', 'task-finished-passed'):
            with self.subTest(name=name):
                self.assertFalse(le.validate_event({**good(name), 'taskId': None})['ok'])
                self.assertTrue(le.validate_event({**good(name), 'taskId': 'other-task'})['ok'])
        self.assertFalse(le.validate_event({**good('module-completed'), 'taskId': 'a-task'})['ok'])

    def test_the_day_must_exist(self):
        base = good('task-opened')
        self.assertTrue(le.validate_event({**base, 'time': '2028-02-29T10:00:00Z'})['ok'])
        for stamp in ('2026-02-29T10:00:00Z', '2100-02-29T10:00:00Z', '2026-04-31T10:00:00Z', '2026-06-31T10:00:00Z'):
            with self.subTest(stamp=stamp):
                self.assertEqual(pointers(le.validate_event({**base, 'time': stamp})), ['/time'])

    def test_an_offer_cannot_be_triggered_by_itself(self):
        offer = good('agent-offer-pending')
        offer['data']['triggerEventId'] = offer['eventId']
        result, stage = le.validate_event_text_with_stage(json.dumps(offer))
        self.assertEqual(pointers(result), ['/data/triggerEventId'])
        self.assertEqual(stage, 'semantic')

    def test_more_than_100_errors_are_cut_with_a_marker(self):
        result = le.validate_event({**good('task-opened'), 'skills': ['Bad'] * 150})
        self.assertEqual(result['reason'], 'invalid')
        self.assertEqual(len(result['errors']), 101)
        self.assertRegex(result['errors'][-1]['message'], r'^\d+ more errors omitted\.$')
        self.assertEqual(result['errors'][-1]['pointer'], '')


class TextAndLogTests(unittest.TestCase):
    ONE = (FIXTURES / 'learning-events' / 'valid' / 'task-opened.json').read_text(encoding='utf-8').strip()

    def test_text_over_the_line_cap_is_too_large_before_parsing(self):
        self.assertEqual(le.validate_event_text(' ' * (le.MAX_LINE_BYTES + 1))['reason'], 'too_large')
        self.assertEqual(le.validate_event_text(' ' * le.MAX_LINE_BYTES)['reason'], 'invalid')
        self.assertEqual(le.validate_event_text('é' * (le.MAX_LINE_BYTES // 2 + 1))['reason'], 'too_large')

    def test_damaged_event_text_is_invalid(self):
        for text in ('', '{', '{"a":', '[1,]', "{'a': 1}", '{"a": NaN}', '﻿{}'):
            with self.subTest(text=text):
                result, stage = le.validate_event_text_with_stage(text)
                self.assertEqual((result['reason'], stage), ('invalid', 'parse'))
        for value in (None, 5, ['x'], {}):
            self.assertEqual(le.validate_event_text(value)['reason'], 'invalid')

    def test_a_repeated_object_key_is_rejected_by_this_validator(self):
        result, stage = le.validate_event_text_with_stage('{"schemaVersion": "1.0", "schemaVersion": "1.1"}')
        self.assertEqual((result['reason'], stage), ('invalid', 'repeat'))
        self.assertEqual(pointers(result), [''])

    def test_a_log_over_the_cap_is_refused_as_a_whole(self):
        result = le.validate_log_text('{' + ' ' * le.MAX_BYTES)
        self.assertEqual(result['reason'], 'too_large')
        self.assertEqual((result['ok'], result['lines'], result['truncatedTail']), (False, [], False))
        self.assertNotEqual(le.validate_log_text(' ' * le.MAX_BYTES).get('reason'), 'too_large')
        self.assertEqual(le.validate_log_text('é' * (le.MAX_BYTES // 2 + 1))['reason'], 'too_large')

    def test_a_log_that_is_not_text_or_bytes_is_refused_as_a_whole(self):
        for value in (None, 5, {}):
            result = le.validate_log_text(value)
            self.assertEqual((result['ok'], result['reason'], result['lines']), (False, 'invalid', []))

    def test_bytes_are_decoded_one_line_at_a_time(self):
        first, second = read('valid/log-multi-line.jsonl').split('\n')[:2]
        data = first.encode() + b'\n{\xff}\n' + second.encode() + b'\n'
        result = le.validate_log_text(data)
        self.assertNotIn('reason', result)
        self.assertEqual([line['ok'] for line in result['lines']], [True, False, True])
        self.assertEqual(result['lines'][1]['reason'], 'invalid')
        self.assertIn('UTF-8', result['lines'][1]['errors'][0]['message'])
        self.assertFalse(result['truncatedTail'])

    def test_a_multibyte_character_cut_at_the_end_is_a_truncated_tail(self):
        first = read('valid/log-multi-line.jsonl').split('\n')[0].encode()
        for tail in ('{"note":"é'.encode()[:-1], '{"note":"€'.encode()[:-1], '{"note":"\U0001F600'.encode()[:-2]):
            with self.subTest(tail=tail):
                result = le.validate_log_text(first + b'\n' + tail)
                self.assertNotIn('reason', result)
                self.assertTrue(result['truncatedTail'])
                self.assertEqual([line['ok'] for line in result['lines']], [True, False])
                self.assertEqual(result['lines'][1]['reason'], 'truncated_tail')
        for tail in (b'{\xff{', b'{\xff', b'{\xc3('):
            with self.subTest(tail=tail):
                result = le.validate_log_text(first + b'\n' + tail)
                self.assertFalse(result['truncatedTail'])
                self.assertEqual(result['lines'][1]['reason'], 'invalid')

    def test_a_log_with_too_many_lines_is_refused_as_a_whole(self):
        self.assertEqual(le.MAX_LINES, 50000)
        for over in ('\n' * (le.MAX_LINES + 1), b'\n' * (le.MAX_LINES + 1), '\n' * le.MAX_LINES + '{'):
            result = le.validate_log_text(over)
            self.assertEqual((result['ok'], result['reason'], result['lines']), (False, 'too_large', []))
        edge = le.validate_log_text('\n' * le.MAX_LINES)
        self.assertNotIn('reason', edge)
        self.assertEqual(len(edge['lines']), le.MAX_LINES)
        self.assertTrue(all(not line['ok'] and line['reason'] == 'invalid' for line in edge['lines']))

    def test_a_log_splits_on_the_newline_character_only(self):
        result = le.validate_log_text(f'{self.ONE} {self.ONE}\n')
        self.assertEqual(len(result['lines']), 1)
        self.assertFalse(result['lines'][0]['ok'])

    def test_an_empty_log_is_valid(self):
        self.assertEqual(le.validate_log_text(''), {'ok': True, 'lines': [], 'truncatedTail': False})

    def test_a_truncated_tail_is_not_valid_and_not_fatal(self):
        text = read('valid/log-multi-line.jsonl')
        cut = text[:-25]
        result = le.validate_log_text(cut)
        self.assertTrue(result['truncatedTail'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['lines'][-1]['reason'], 'truncated_tail')
        self.assertTrue(all(line['ok'] for line in result['lines'][:-1]))
        damaged = le.validate_log_text(cut + '\n')
        self.assertFalse(damaged['truncatedTail'])
        self.assertEqual(damaged['lines'][-1]['reason'], 'invalid')

    def test_a_valid_last_line_without_a_newline_is_valid(self):
        self.assertTrue(le.validate_log_text(read('valid/log-multi-line.jsonl')[:-1])['ok'])

    def test_a_line_of_exactly_4_kb_is_read_and_one_byte_more_is_not(self):
        at_cap = self.ONE + ' ' * (le.MAX_LINE_BYTES - len(self.ONE.encode('utf-8')))
        self.assertTrue(le.validate_log_text(at_cap + '\n')['ok'])
        over = le.validate_log_text(at_cap + ' \n')
        self.assertEqual(over['lines'][0]['reason'], 'too_large')

    def test_a_repeated_event_id_is_reported_on_the_later_line(self):
        result = le.validate_log_text(f'{self.ONE}\n{self.ONE}\n')
        self.assertTrue(result['lines'][0]['ok'])
        self.assertEqual(pointers(result['lines'][1]), ['/eventId'])

    def test_unreadable_and_oversized_files_are_refused_as_a_whole(self):
        folder = Path(tempfile.mkdtemp(prefix='schola-event-files-'))
        self.addCleanup(shutil.rmtree, folder, True)
        missing = le.validate_log_file(folder / 'missing.jsonl')
        self.assertEqual(missing['reason'], 'unreadable')
        self.assertNotIn(str(folder), json.dumps(missing))
        broken = folder / 'broken.jsonl'
        broken.write_bytes(b'{\xff\n')
        damaged = le.validate_log_file(broken)
        self.assertNotIn('reason', damaged)
        self.assertEqual(damaged['lines'][0]['reason'], 'invalid')
        big = folder / 'big.jsonl'
        big.write_bytes(b' ' * (le.MAX_BYTES + 1))
        self.assertEqual(le.validate_log_file(big)['reason'], 'too_large')
        self.assertEqual(sorted(os.listdir(folder)), ['big.jsonl', 'broken.jsonl'])


def schema():
    return json.loads((ROOT / 'contracts' / 'learning-event.v1.schema.json').read_text(encoding='utf-8'))


def data_defs_by_type(document):
    found = {}
    for rule in document['allOf']:
        name = rule['then']['properties']['data']['$ref'].split('/')[-1]
        found[rule['if']['properties']['type']['const']] = (name, document['$defs'][name])
    return found


class PinnedRuleTests(unittest.TestCase):
    def test_deleting_any_required_field_makes_an_event_invalid(self):
        document = schema()
        by_type = data_defs_by_type(document)
        lists = []

        def walk(node, where):
            if isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f'{where}/{index}')
            elif isinstance(node, dict):
                if isinstance(node.get('required'), list) and '/if' not in where:
                    lists.append(where)
                for key, child in node.items():
                    if key not in ('enum', 'const'):
                        walk(child, f'{where}/{key}')

        walk(document, '')
        expected = [''] + [f'/$defs/{name}' for name, definition in by_type.values() if 'required' in definition]
        self.assertEqual(sorted(lists), sorted(expected))
        self.assertEqual(sorted(by_type), sorted(le.EVENT_TYPES))
        self.assertEqual(sorted(document['required']), sorted(document['properties']))
        covered = set()
        for entry in load_manifest():
            if entry['kind'] != 'learning-event' or entry['expect'] != 'valid':
                continue
            event = json.loads((FIXTURES / entry['file']).read_text(encoding='utf-8'))
            for key in document['required']:
                with self.subTest(case=entry['name'], missing=key):
                    broken = copy.deepcopy(event)
                    del broken[key]
                    self.assertFalse(le.validate_event(broken)['ok'])
            for key in by_type[event['type']][1].get('required', []):
                with self.subTest(case=entry['name'], missing=f'data.{key}'):
                    broken = copy.deepcopy(event)
                    del broken['data'][key]
                    self.assertFalse(le.validate_event(broken)['ok'])
                    covered.add((event['type'], key))
        for event_type, (name, definition) in by_type.items():
            for key in definition.get('required', []):
                self.assertIn((event_type, key), covered)
            self.assertEqual(sorted(definition.get('required', [])), sorted(definition.get('properties', {})), event_type)

    def test_the_enum_sets_are_pinned_and_match_the_guide(self):
        document = schema()
        guide = (ROOT / 'contracts' / 'learning-events.md').read_text(encoding='utf-8').split('\n')
        defs = document['$defs']
        levels = ['nudge', 'question', 'partial-example', 'full-answer']
        pins = [
            ('source', document['properties']['source']['enum'], ['student', 'app', 'agent'], '- `source` is ', None),
            ('check status', defs['dataCheckResult']['properties']['status']['enum'], ['passed', 'failed', 'error', 'skipped', 'self-reported'], '- `status`: ', None),
            ('offeredLevel', defs['dataAgentOffer']['properties']['offeredLevel']['enum'], levels + ['recall-question', 'review-suggestion'], '- `offeredLevel`: ', ['recall-question', 'review-suggestion']),
            ('offer outcome', defs['dataAgentOffer']['properties']['outcome']['enum'], ['pending', 'accepted', 'dismissed', 'ignored'], '- `outcome`: `pending`', None),
            ('review outcome', defs['dataReviewResult']['properties']['outcome']['enum'], ['correct', 'incorrect'], '- `outcome`: `correct`', None),
            ('mode from', defs['dataModeChanged']['properties']['from']['enum'], ['coach', 'pair', 'show-me'], '- `from` and `to`: ', None),
            ('mode to', defs['dataModeChanged']['properties']['to']['enum'], ['coach', 'pair', 'show-me'], '- `from` and `to`: ', None),
            ('task result', defs['dataTaskFinished']['properties']['result']['enum'], ['passed', 'abandoned', 'skipped'], '- `result`: ', None),
            ('hint origin', defs['dataHintRequested']['properties']['origin']['enum'], ['student', 'agent-offer-accepted'], '- `origin`: ', None),
        ]
        for label, actual, expected, prefix, doc_tokens in pins:
            with self.subTest(label=label):
                self.assertEqual(sorted(actual), sorted(expected))
                line = next((item for item in guide if item.startswith(prefix)), None)
                self.assertIsNotNone(line, prefix)
                body = re.sub(r'^- `[^`]+`( and `[^`]+`)?(:| is) ', '', line).split('. ')[0]
                self.assertEqual(sorted(re.findall(r'`([^`]+)`', body)), sorted(doc_tokens or expected))

    def test_a_repeated_event_id_is_reported_next_to_the_other_errors(self):
        one = good('task-opened')
        other = good('task-finished-passed')

        def text(*documents):
            return ''.join(json.dumps(item) + '\n' for item in documents)

        second = le.validate_log_text(text(one, {**one, 'extra': 1}))['lines'][1]
        self.assertEqual((second['ok'], second['reason']), (False, 'invalid'))
        self.assertEqual(sorted(error['pointer'] for error in second['errors']), ['', '/eventId'])
        result = le.validate_log_text(text({**one, 'extra': 1}, one))
        self.assertEqual([line['ok'] for line in result['lines']], [False, False])
        self.assertEqual(pointers(result['lines'][1]), ['/eventId'])
        newer = le.validate_log_text(text({**one, 'schemaVersion': '2.0'}, one, {**one, 'schemaVersion': '2.0'}))
        self.assertEqual([line.get('reason', 'ok') for line in newer['lines']], ['unsupported_version', 'ok', 'unsupported_version'])
        self.assertEqual(pointers(newer['lines'][2]), ['/schemaVersion'])
        odd = le.validate_log_text(text({**one, 'eventId': 'x'}, {**one, 'eventId': 'x'}, {**other, 'eventId': 'y'}))
        self.assertEqual([','.join(error['pointer'] for error in line['errors']) for line in odd['lines']], ['/eventId', '/eventId,/eventId', '/eventId'])
        self.assertTrue(le.validate_log_text(text(one, other))['ok'])

    @unittest.skipIf(hasattr(os, 'getuid') and os.getuid() == 0, 'a root user can read any file')
    def test_a_file_over_the_size_cap_is_never_read(self):
        folder = Path(tempfile.mkdtemp(prefix='schola-event-noread-'))
        self.addCleanup(shutil.rmtree, folder, True)
        big = folder / 'big.jsonl'
        big.write_bytes(b'')
        os.truncate(big, le.MAX_BYTES + 1)
        big.chmod(0)
        self.assertEqual(le.validate_log_file(big)['reason'], 'too_large')
        exact = folder / 'exact.jsonl'
        exact.write_bytes(b'')
        os.truncate(exact, le.MAX_BYTES)
        exact.chmod(0)
        self.assertEqual(le.validate_log_file(exact)['reason'], 'unreadable')


class SchemaTests(unittest.TestCase):
    def test_every_object_in_the_schema_is_closed(self):
        schema = json.loads((ROOT / 'contracts' / 'learning-event.v1.schema.json').read_text(encoding='utf-8'))
        self.assertEqual(schema['$id'], 'urn:schola:contract:learning-event:v1')
        open_objects = []

        def walk(node, where):
            if isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f'{where}/{index}')
            elif isinstance(node, dict):
                if node.get('type') == 'object' and node.get('additionalProperties') is not False:
                    open_objects.append(where)
                for key, child in node.items():
                    if key not in ('enum', 'const'):
                        walk(child, f'{where}/{key}')

        walk(schema, '')
        self.assertEqual(open_objects, [])

    def test_the_event_schema_never_changes_the_lesson_validator(self):
        import lesson_contract as lc
        self.assertEqual(lc.LESSON_CONTRACT_KINDS, ('lesson-module', 'packet', 'help-record'))
        self.assertIsNotNone(lc.load_contract_registry())


if __name__ == '__main__':
    unittest.main()
