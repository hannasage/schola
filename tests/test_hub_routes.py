"""The hub route: GET /api/hub, its guards, its query, and its answer on real stores."""
import datetime
import json
import os
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
import re
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
import event_routes
import hub_data
import hub_routes
from event_log import EventLog, EventLogError
from lesson_store import LessonStore
from server import Handler

FIXTURE = ROOT / 'tests' / 'fixtures' / 'contracts' / 'valid' / 'lesson-module-minimal.json'
NOW = datetime.datetime(2026, 11, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
SESSION = '11111111-1111-4111-8111-111111111111'
ERROR_KEYS = {'error', 'code', 'retryable', 'field'}


def make_event(kind, when='2026-11-05T09:00:00Z', module='alpha', task='task-1', source=None, **data):
    app = ('attempt_submitted', 'check_result', 'task_finished', 'module_completed')
    defaults = {'check_result': {'checkId': 'check-1', 'attemptId': 'a1', 'status': 'failed', 'durationMs': 5, 'evidence': '', 'runnerType': None},
                'task_finished': {'result': 'passed'}, 'task_opened': {}, 'module_completed': {'checksPassed': 1, 'tasksTotal': 2},
                'hint_requested': {'level': 'nudge', 'attemptId': None, 'origin': 'student'}}[kind]
    return {'schemaVersion': '1.0', 'eventId': str(uuid.uuid4()), 'time': when, 'sessionId': SESSION, 'moduleId': module, 'moduleVersion': '1.0.0',
            'taskId': None if kind == 'module_completed' else task, 'source': source or ('app' if kind in app else 'student'),
            'skills': ['loops'] if kind == 'check_result' else [], 'type': kind, 'data': dict(defaults, **data)}


class HubCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.server.data_dir = self.data
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        patcher = mock.patch.object(hub_routes, '_utc_now', return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token = self.call('GET', '/api/checks/session', token=None)[1]['token']

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, method, path, body=None, headers=None, token=True, raw=False):
        connection = HTTPConnection('127.0.0.1', self.server.server_port)
        sent = {'Content-Type': 'application/json'}
        if token:
            sent['X-Schola-Check-Token'] = self.token if token is True else token
        sent.update(headers or {})
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=sent)
        response = connection.getresponse()
        content = response.read()
        result = (response.status, content if raw else json.loads(content), {name.lower(): value for name, value in response.getheaders()})
        connection.close()
        return result[:2] if not raw else result

    def get(self, query='', **kw):
        return self.call('GET', '/api/hub' + query, **kw)

    def import_module(self, module_id='alpha', tasks=2, checks=1, title='Alpha'):
        document = json.loads(FIXTURE.read_text())
        base = document['tasks'][0]
        document.update(id=module_id, title=title)
        document['tasks'] = [dict(base, id=f'task-{n}', prompt=f'Prompt {n}') for n in range(1, tasks + 1)]
        document['checks'] = [{'id': f'check-{n}', 'kind': 'self-reported', 'description': f'Check number {n}'} for n in range(1, checks + 1)]
        LessonStore(self.data).import_files([(f'{module_id}.json', (json.dumps(document) + '\n').encode())])

    def append(self, item):
        event_routes.log_for(self.server).append(item)

    def tree(self):
        return sorted(str(p.relative_to(self.data)) for p in self.data.rglob('*'))


class GuardTests(HubCase):
    def test_a_request_without_the_token_is_refused(self):
        status, body = self.get(token=None)
        self.assertEqual((status, body['code']), (403, 'invalid_token'))
        self.assertEqual(set(body), {'error', 'code', 'retryable'})

    def test_a_wrong_token_is_refused(self):
        self.assertEqual(self.get(token='wrong')[0], 403)
        self.assertEqual(self.get(token=self.token + 'x')[0], 403)

    def test_a_request_from_another_site_is_refused(self):
        for site in ('cross-site', 'same-site'):
            status, body = self.get(headers={'Sec-Fetch-Site': site})
            self.assertEqual((status, body['code']), (403, 'forbidden_origin'), site)
        self.assertEqual(self.get(headers={'Sec-Fetch-Site': 'same-origin'})[0], 200)
        self.assertEqual(self.get(headers={'Sec-Fetch-Site': 'none'})[0], 200)

    def test_the_host_and_origin_rules_of_the_server_apply(self):
        self.assertEqual(self.get(headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.get(headers={'Host': f'127.0.0.1:{self.server.server_port + 1}'})[0], 403)
        self.assertEqual(self.get(headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.get(headers={'Origin': f'http://127.0.0.1:{self.server.server_port}'})[0], 200)

    def test_a_refused_request_reads_nothing(self):
        with mock.patch.object(hub_data, 'build_hub') as build:
            self.get(token=None)
            self.get(headers={'Sec-Fetch-Site': 'cross-site'})
            self.get('?tzOffsetMinutes=999')
        build.assert_not_called()

    def test_only_get_is_served(self):
        for method in ('POST', 'PUT', 'DELETE'):
            status, body = self.call(method, '/api/hub', {'confirm': True})
            self.assertEqual((status, body['code']), (404, 'not_found'), method)
        self.assertEqual(self.tree(), [])

    def test_a_path_that_only_looks_like_the_route_is_not_found(self):
        for path in ('/api/hub/', '/api/hubs', '/api/hub/extra', '/api/HUB'):
            self.assertEqual(self.call('GET', path)[0], 404, path)


class QueryTests(HubCase):
    def test_the_default_offset_is_zero(self):
        self.append(make_event('task_opened', '2026-11-04T23:59:30Z'))  # one minute east of UTC is already the next day
        default = self.get()[1]
        self.assertEqual(default, self.get('?tzOffsetMinutes=0')[1])
        self.assertEqual(default['participation']['weeks'][-1]['days'][:4], [0, 0, 1, 0])
        self.assertNotEqual(default, self.get('?tzOffsetMinutes=1')[1])

    def test_every_whole_offset_in_range_is_accepted_at_the_edges(self):
        for value in ('-840', '840', '0', '330', '-1', '1', '60', '-480'):
            self.assertEqual(self.get(f'?tzOffsetMinutes={value}')[0], 200, value)

    def test_the_offset_moves_today(self):
        self.assertEqual(self.get('?tzOffsetMinutes=840')[1]['participation']['weeks'][-1]['days'], [0, 0, 0, 0, 0, None, None])
        self.assertEqual(self.get('?tzOffsetMinutes=-840')[1]['participation']['weeks'][-1]['days'], [0, 0, 0, None, None, None, None])

    def test_a_bad_offset_is_a_400_with_the_stable_error_shape(self):
        bad = ('841', '-841', '1000', '99999999999999999999', '1.5', '1e2', 'abc', '', '+60', '%2B60', '%2060', '0x10', '--1', '-', '01', '-0',
               '%D9%A3', '%EF%BC%91%EF%BC%92', '60%0A', 'NaN', 'null', '60&tzOffsetMinutes=61')
        for value in bad:
            status, body = self.get(f'?tzOffsetMinutes={value}')
            self.assertEqual(status, 400, value)
            self.assertEqual((body['code'], body['field'], body['retryable']), ('invalid_request', 'tzOffsetMinutes', False), value)
            self.assertEqual(set(body), ERROR_KEYS)

    def test_an_unknown_query_field_is_a_400(self):
        for query in ('?limit=5', '?tzoffsetminutes=0', '?tzOffsetMinutes=0&limit=5', '?=1', '?x'):
            status, body = self.get(query)
            self.assertEqual((status, body['code'], body['field']), (400, 'invalid_request', 'query'), query)

    def test_a_repeated_offset_is_a_400(self):
        self.assertEqual(self.get('?tzOffsetMinutes=0&tzOffsetMinutes=0')[0], 400)

    def test_a_fragment_or_empty_query_is_fine(self):
        self.assertEqual(self.get('?')[0], 200)


class ResponseTests(HubCase):
    def test_an_empty_data_folder_gives_zeros_and_writes_nothing(self):
        status, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(body['log'], {'exists': False, 'ok': True, 'events': 0, 'damagedLines': 0, 'truncatedTail': False})
        self.assertEqual((body['focus'], body['library'], body['skills'], body['checks'], body['checkpoints']), (None, [], [], [], {'done': 0, 'total': 0}))
        self.assertEqual(body['generatedAt'], '2026-11-05T12:00:00Z')
        self.assertEqual(self.tree(), [])

    def test_the_headers_do_not_let_a_cache_keep_the_answer(self):
        status, content, headers = self.call('GET', '/api/hub', raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers['cache-control'], 'no-store')
        self.assertEqual(headers['content-type'], 'application/json')
        self.assertEqual(headers['x-content-type-options'], 'nosniff')
        self.assertNotIn('etag', headers)
        self.assertNotIn('last-modified', headers)
        self.assertNotIn('set-cookie', headers)
        self.assertIsInstance(json.loads(content), dict)

    def test_real_events_and_modules_roll_up_through_the_route(self):
        self.import_module('alpha', title='Alpha')
        self.import_module('beta', title='Beta')
        self.append(make_event('task_finished', module='alpha', task='task-1'))
        self.append(make_event('check_result', module='alpha', task='task-2'))
        self.append(make_event('module_completed', module='alpha'))
        self.append(make_event('task_opened', module='beta', task='task-1'))
        self.append(make_event('task_opened', module='beta', task='task-1', source='agent'))
        status, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(body['log'], {'exists': True, 'ok': True, 'events': 5, 'damagedLines': 0, 'truncatedTail': False})
        self.assertEqual((body['focus']['moduleId'], body['focus']['nextTask']), ('beta', {'taskId': 'task-1', 'text': 'Prompt 1'}))
        self.assertEqual(body['checkpoints'], {'done': 1, 'total': 2})
        self.assertEqual([(m['moduleId'], m['status']) for m in body['library']], [('alpha', 'done'), ('beta', 'focus')])
        self.assertEqual(body['skills'], [{'skillId': 'loops', 'passed': 0, 'tried': 1, 'window': 5}])
        self.assertEqual(body['participation']['totals'], {'sessions': 1, 'activeDays': 1, 'days': 53})

    def test_a_student_event_posted_by_the_page_shows_up(self):
        self.import_module('alpha')
        status, _ = self.call('POST', '/api/events', {'event': make_event('task_opened', module='alpha')})
        self.assertEqual(status, 201)
        self.assertEqual(self.get()[1]['participation']['totals']['sessions'], 1)

    def test_the_answer_changes_after_an_append(self):
        self.import_module('alpha')
        self.assertEqual(self.get()[1]['participation']['totals']['sessions'], 0)
        self.append(make_event('task_opened', module='alpha'))
        self.assertEqual(self.get()[1]['participation']['totals']['sessions'], 1)

    def test_the_offset_changes_the_day_an_event_lands_on(self):
        self.append(make_event('task_opened', '2026-11-04T23:30:00Z'))
        self.assertEqual(self.get('?tzOffsetMinutes=0')[1]['participation']['weeks'][-1]['days'][2], 1)
        self.assertEqual(self.get('?tzOffsetMinutes=60')[1]['participation']['weeks'][-1]['days'][3], 1)

    def test_a_damaged_log_is_a_200_with_the_finding(self):
        self.import_module('alpha')
        self.append(make_event('task_finished', module='alpha', task='task-1'))
        with open(self.data / '_events' / 'events.jsonl', 'ab') as handle:
            handle.write(b'not json\n{"half":')
        status, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(body['log'], {'exists': True, 'ok': False, 'events': 1, 'damagedLines': 1, 'truncatedTail': True})
        self.assertEqual(body['focus']['tasksDone'], 1)

    def test_a_log_over_its_limits_is_a_200_with_the_finding(self):
        self.import_module('alpha')
        folder = self.data / '_events'
        folder.mkdir(mode=0o700)
        (folder / 'events.jsonl').write_bytes(b'\n' * 100_000)
        os.chmod(folder / 'events.jsonl', 0o600)
        status, body = self.get()
        self.assertEqual(status, 200)
        self.assertFalse(body['log']['ok'])
        self.assertEqual(body['library'][0]['moduleId'], 'alpha')

    def test_a_busy_log_is_a_retryable_503_and_not_a_finding(self):
        busy = EventLogError('Another program is using the event log. Try again in a moment. Nothing was changed.', 'lock_unavailable', 503, retryable=True)
        with mock.patch.object(EventLog, 'read', side_effect=busy):
            status, body = self.get()
        self.assertEqual((status, body['code'], body['retryable']), (503, 'lock_unavailable', True))

    def test_a_damaged_lesson_store_is_an_error_that_names_no_path(self):
        self.import_module('alpha')
        (self.data / '_lessons' / 'pins.json').write_text('{ broken')
        status, body = self.get()
        self.assertEqual(status, 500)
        self.assertNotIn(str(self.data), json.dumps(body))
        self.assertIn('code', body)

    def test_an_unexpected_fault_is_a_plain_500_with_no_detail(self):
        with mock.patch.object(hub_data, 'build_hub', side_effect=RuntimeError('SECRET-DETAIL /private/path')):
            with mock.patch('traceback.print_exc'):
                status, body = self.get()
        self.assertEqual((status, body['code'], body['retryable']), (500, 'internal_error', False))
        self.assertNotIn('SECRET-DETAIL', json.dumps(body))

    def test_the_answer_holds_no_path_and_no_event_content(self):
        self.import_module('alpha')
        item = make_event('check_result', module='alpha', evidence='SECRET-EVIDENCE')
        self.append(item)
        _, content, _ = self.call('GET', '/api/hub', raw=True)
        text = content.decode()
        for forbidden in (str(self.data), 'SECRET-EVIDENCE', item['eventId'], SESSION, 'attemptId', 'eventId', '_events', '_lessons'):
            self.assertNotIn(forbidden, text)

    def test_the_route_reads_the_log_once_per_request(self):
        self.import_module('alpha')
        self.append(make_event('task_opened', module='alpha'))
        with mock.patch.object(EventLog, 'read', autospec=True, side_effect=EventLog.read) as spy:
            self.assertEqual(self.get()[0], 200)
        self.assertEqual(spy.call_count, 1)

    def test_the_route_does_not_change_either_store(self):
        self.import_module('alpha')
        self.append(make_event('task_opened', module='alpha'))

        def contents():
            return {str(p): p.read_bytes() for p in sorted(self.data.rglob('*')) if p.is_file() and p.name != '.events.lock'}
        before = contents()
        self.get()
        self.get('?tzOffsetMinutes=-300')
        self.assertEqual(contents(), before)


class TypeTests(HubCase):
    """types/hub.d.ts lists the same fields, at every level, as a real response."""

    @staticmethod
    def interfaces(text):
        found = {}
        for name, body in re.findall(r'^interface (\w+) \{(.*?)^\}', text, re.M | re.S):
            found[name] = re.findall(r'^\s+(\w+)\??: ([^;]+);', body, re.M)
        return found

    def test_the_typescript_type_matches_a_real_response(self):
        self.import_module('alpha')
        self.append(make_event('check_result', module='alpha', task='task-1'))
        self.append(make_event('hint_requested', module='alpha', task='task-1', level='question'))
        body = self.get()[1]
        declared = self.interfaces((ROOT / 'types' / 'hub.d.ts').read_text(encoding='utf-8'))

        def keys(name):
            return [field for field, _ in declared[name]]

        def kind(name, field):
            return dict(declared[name])[field]
        self.assertEqual(keys('HubResponse'), list(body))
        self.assertEqual(keys('HubLog'), list(body['log']))
        self.assertEqual(keys('HubFocus'), list(body['focus']))
        self.assertEqual(keys('HubNextTask'), list(body['focus']['nextTask']))
        self.assertEqual(keys('HubParticipation'), list(body['participation']))
        self.assertEqual(keys('HubWeek'), list(body['participation']['weeks'][0]))
        self.assertEqual(keys('HubTotals'), list(body['participation']['totals']))
        self.assertEqual(keys('HubCheckpoints'), list(body['checkpoints']))
        self.assertEqual(keys('HubSkill'), ['skillId', 'passed', 'tried', 'window'])
        self.assertEqual(keys('HubCheck'), list(body['checks'][0]))
        self.assertEqual(keys('HubLibraryEntry'), list(body['library'][0]))
        self.assertEqual(kind('HubResponse', 'schemaVersion'), "'1.0'")
        self.assertEqual(kind('HubResponse', 'focus'), 'HubFocus | null')
        self.assertEqual(kind('HubFocus', 'nextTask'), 'HubNextTask | null')
        self.assertEqual(kind('HubWeek', 'days'), '[HubDay, HubDay, HubDay, HubDay, HubDay, HubDay, HubDay]')
        self.assertEqual(kind('HubCheck', 'hintLevelReached'), 'HubHintLevel | null')
        self.assertEqual(kind('HubLibraryEntry', 'status'), "'done' | 'focus' | 'next'")
        self.assertEqual(kind('HubSkill', 'window'), '5')
        self.assertEqual(kind('HubParticipation', 'windowWeeks'), '8')
        self.assertEqual(body['checks'][0]['hintLevelReached'], 'question')

    def test_the_type_names_the_same_ladder_as_the_code(self):
        text = (ROOT / 'types' / 'hub.d.ts').read_text(encoding='utf-8')
        declared = re.search(r"^type HubHintLevel = ([^;]+);", text, re.M).group(1)
        self.assertEqual(re.findall(r"'([a-z-]+)'", declared), list(hub_data.HINT_LADDER))

    def test_the_type_file_has_no_streak_rank_or_total_time_field(self):
        text = (ROOT / 'types' / 'hub.d.ts').read_text(encoding='utf-8')
        fields = [field for fields in self.interfaces(text).values() for field, _ in fields]
        self.assertEqual([f for f in fields if re.search(r'streak|rank|leader|score|badge|minutes|duration|time', f, re.I)], [])


class ClockTests(unittest.TestCase):
    def test_the_route_clock_is_the_real_utc_time(self):
        before = datetime.datetime.now(datetime.timezone.utc)
        stamp = hub_routes._utc_now()
        after = datetime.datetime.now(datetime.timezone.utc)
        self.assertEqual(stamp.utcoffset(), datetime.timedelta(0))
        self.assertTrue(before <= stamp <= after)


class ModuleWiringTests(unittest.TestCase):
    def test_the_route_is_a_get_route_only(self):
        self.assertEqual(hub_routes.GET_ROUTES, ('/api/hub',))
        self.assertFalse(hasattr(hub_routes, 'POST_ROUTES'))

    def test_the_event_log_helper_is_the_one_the_server_keeps(self):
        server = mock.Mock(spec=['data_dir'])
        server.data_dir = Path('/nonexistent-data-dir')
        first = event_routes.log_for(server)
        self.assertIs(event_routes.log_for(server), first)
        self.assertIsInstance(first, event_log.EventLog)


if __name__ == '__main__':
    unittest.main()
