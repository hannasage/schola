"""The event log and reflection routes of the local server."""
import hashlib
import json
import os
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
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
import learning_event
from server import Handler

FIXTURES = ROOT / 'tests' / 'fixtures' / 'contracts' / 'learning-events' / 'valid'
SECRET = 'SECRET-REFLECTION-TEXT-9d1f'


def fixture_event(name='task-opened', **changes):
    document = json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))
    document['eventId'] = str(uuid.uuid4())
    document.update(changes)
    return document


def tree(directory):
    directory = Path(directory)
    return {str(p.relative_to(directory)): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'dir') for p in sorted(directory.rglob('*'))}


class RouteCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.log_path = self.data / '_events' / 'events.jsonl'
        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.server.data_dir = self.data
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.token = self.call('GET', '/api/checks/session', token=None)[1]['token']

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, method, path, body=None, headers=None, token=True, raw=None, binary=False):
        connection = HTTPConnection('127.0.0.1', self.server.server_port)
        sent = {'Content-Type': 'application/json'}
        if token:
            sent['X-Schola-Check-Token'] = self.token if token is True else token
        sent.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        content = response.read()
        kind = response.getheader('Content-Type')
        connection.close()
        if binary:
            return response.status, content, kind
        return response.status, json.loads(content)

    def call_full(self, method, path, body=None, headers=None):
        """Like call, and gives the raw bytes and the response headers."""
        connection = HTTPConnection('127.0.0.1', self.server.server_port)
        sent = {'Content-Type': 'application/json', 'X-Schola-Check-Token': self.token}
        sent.update(headers or {})
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=sent)
        response = connection.getresponse()
        content = response.read()
        result = (response.status, content, {name.lower(): value for name, value in response.getheaders()})
        connection.close()
        return result

    def reflection_event(self, saved):
        event = fixture_event('reflection-saved')
        event['data'] = {'reflectionId': saved['reflectionId'], 'charCount': saved['charCount']}
        return event

    def post_event(self, event, **kw):
        return self.call('POST', '/api/events', {'event': event}, **kw)

    def lines(self):
        return self.log_path.read_bytes().splitlines()


class EventRouteTests(RouteCase):
    def test_a_student_event_round_trips_through_every_route(self):
        event = fixture_event()
        status, body = self.post_event(event)
        self.assertEqual((status, body['appended'], body['eventId'], body['line']), (201, True, event['eventId'], 1))
        status, report = self.call('GET', '/api/events')
        self.assertEqual(status, 200)
        self.assertEqual(report['events'], [{'line': 1, 'event': event}])
        self.assertTrue(report['ok'] and report['exists'] and not report['truncatedTail'])
        status, exported, kind = self.call('GET', '/api/events/export', binary=True)
        self.assertEqual(status, 200)
        self.assertEqual(kind, 'application/x-ndjson')
        self.assertEqual(exported, self.log_path.read_bytes())

    def test_reading_with_no_log_says_it_does_not_exist(self):
        self.assertEqual(self.call('GET', '/api/events'), (200, {'ok': True, 'exists': False, 'lines': [], 'events': [], 'truncatedTail': False,
                                                                 'total': 0, 'offset': 0, 'limit': 500, 'more': False}))
        status, body = self.call('GET', '/api/events/export')
        self.assertEqual((status, body['code']), (404, 'log_missing'))

    def test_report_shows_damage_and_a_truncated_tail(self):
        self.post_event(fixture_event())
        with open(self.log_path, 'ab') as handle:
            handle.write(b'{"cut')
        report = self.call('GET', '/api/events')[1]
        self.assertTrue(report['truncatedTail'])
        self.assertEqual(report['lines'][1]['reason'], 'truncated_tail')

    def test_delete_needs_an_explicit_confirm(self):
        self.post_event(fixture_event())
        before = self.log_path.read_bytes()
        for body in ({}, {'confirm': False}, {'confirm': 'true'}, {'confirm': 1}, {'confirm': None}):
            with self.subTest(body=body):
                status, answer = self.call('DELETE', '/api/events', body)
                self.assertEqual((status, answer['code']), (400, 'confirmation_required'))
        status, answer = self.call('DELETE', '/api/events')
        self.assertEqual((status, answer['code']), (400, 'confirmation_required'))
        self.assertEqual(self.log_path.read_bytes(), before)

    def test_delete_with_confirm_removes_the_log_and_a_second_delete_is_not_found(self):
        self.post_event(fixture_event())
        self.assertEqual(self.call('DELETE', '/api/events', {'confirm': True}), (200, {'deleted': True, 'logRemoved': True, 'reflectionsRemoved': 0}))
        self.assertFalse(self.log_path.exists())
        status, body = self.call('DELETE', '/api/events', {'confirm': True})
        self.assertEqual((status, body['code']), (404, 'log_missing'))

    def test_delete_body_must_hold_only_confirm(self):
        self.post_event(fixture_event())
        for body in ([], 'yes', {'confirm': True, 'path': '/'}):
            with self.subTest(body=body):
                status, answer = self.call('DELETE', '/api/events', body)
                self.assertEqual((status, answer['code']), (400, 'invalid_request'))
        self.assertTrue(self.log_path.exists())

    def test_request_shape_is_strict(self):
        for body in ([], 'text', {}, {'event': fixture_event(), 'extra': 1}, {'event': 'text'}, {'event': []}, {'event': None}, fixture_event()):
            with self.subTest(body=body):
                status, answer = self.call('POST', '/api/events', body)
                self.assertEqual((status, answer['code']), (400, 'invalid_request'))
        self.assertFalse(self.log_path.exists())

    def test_an_invalid_event_is_422_with_pointers_and_writes_nothing(self):
        before = tree(self.data)
        status, body = self.post_event(fixture_event(extra=1))
        self.assertEqual((status, body['code']), (422, 'invalid_event'))
        self.assertTrue(body['details'])
        self.assertEqual(tree(self.data), before)

    def test_a_missing_or_unknown_source_is_403_and_writes_nothing(self):
        missing = fixture_event()
        del missing['source']
        for event in (missing, fixture_event(source='teacher'), fixture_event(source=None), fixture_event(source=['app']), fixture_event(source='App'),
                      fixture_event(source='student '), fixture_event(source='')):
            with self.subTest(source=event.get('source')):
                status, body = self.post_event(event)
                self.assertEqual((status, body['code']), (403, 'source_forbidden'))
        self.assertFalse(self.log_path.exists())

    def test_a_repeated_event_id_is_409(self):
        event = fixture_event()
        self.post_event(event)
        status, body = self.post_event(event)
        self.assertEqual((status, body['code']), (409, 'duplicate_event_id'))
        self.assertEqual(len(self.lines()), 1)

    def test_a_full_log_is_409_log_reserved_for_the_page(self):
        self.log_path.parent.mkdir(mode=0o700)
        os.chmod(self.log_path.parent, 0o700)
        self.log_path.write_bytes(b'\n' * learning_event.MAX_LINES)
        status, body = self.post_event(fixture_event())
        self.assertEqual((status, body['code']), (409, 'log_reserved'))
        self.assertEqual(self.log_path.read_bytes(), b'\n' * learning_event.MAX_LINES)

    def test_a_held_lock_is_503_and_retryable(self):
        import fcntl
        self.log_path.parent.mkdir(mode=0o700)
        os.chmod(self.log_path.parent, 0o700)
        descriptor = os.open(self.log_path.parent / event_log.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with mock.patch.object(event_log, 'LOCK_TIMEOUT_SECONDS', 0.05):
            status, body = self.post_event(fixture_event())
        self.assertEqual((status, body['code'], body['retryable']), (503, 'lock_unavailable', True))
        self.assertFalse(self.log_path.exists())

    def test_a_failed_flush_is_a_500_with_a_typed_code_and_no_success(self):
        self.post_event(fixture_event())
        with mock.patch('os.fsync', side_effect=OSError(5, 'boom')):
            status, body = self.post_event(fixture_event())
        self.assertEqual((status, body['code']), (500, 'fsync_failed'))
        self.assertNotIn('appended', body)

    def test_an_unexpected_fault_is_a_500_without_detail(self):
        with mock.patch('event_log.EventLog.append', side_effect=RuntimeError('SECRET-DETAIL')), mock.patch('traceback.print_exc'):
            status, body = self.post_event(fixture_event())
        self.assertEqual((status, body['code']), (500, 'internal_error'))
        self.assertNotIn('SECRET-DETAIL', json.dumps(body))

    def test_body_limits_and_media_type(self):
        status, body = self.call('POST', '/api/events', raw=b'x' * (event_routes.BODY_LIMIT + 1))
        self.assertEqual((status, body['code']), (413, 'payload_too_large'))
        self.assertIn('256 KB', body['error'])
        self.assertEqual(self.call('POST', '/api/events', raw=b'{"event":', headers={})[1]['code'], 'invalid_json')
        self.assertEqual(self.call('POST', '/api/events', raw=b'{}', headers={'Content-Type': 'text/plain'})[0], 415)

    def test_other_methods_and_paths_are_not_found(self):
        self.assertEqual(self.call('PUT', '/api/events', {})[0], 404)
        self.assertEqual(self.call('DELETE', '/api/events/export', {'confirm': True})[0], 404)
        self.assertEqual(self.call('DELETE', '/api/reflections', {'confirm': True})[0], 404)
        self.assertEqual(self.call('POST', '/api/events/export', {})[0], 404)
        self.assertEqual(self.call('GET', '/api/events/other')[0], 404)
        self.assertEqual(self.call('GET', '/api/reflections')[0], 404)


class ServerLogTests(RouteCase):
    def test_one_event_log_serves_every_request(self):
        self.post_event(fixture_event())
        first = self.server.event_log
        self.post_event(fixture_event())
        self.call('GET', '/api/events')
        self.assertIs(self.server.event_log, first)

    def test_later_requests_do_not_read_the_file_again(self):
        with mock.patch.object(event_log.EventLog, '_read_log', autospec=True, side_effect=event_log.EventLog._read_log) as read:
            for _ in range(3):
                self.assertEqual(self.post_event(fixture_event())[0], 201)
            self.assertEqual(read.call_count, 0)
            for _ in range(3):
                self.assertEqual(self.call('GET', '/api/events')[0], 200)
            self.assertEqual(read.call_count, 1)
            self.call('GET', '/api/events/export', binary=True)
            self.assertEqual(read.call_count, 1)

    def test_a_change_made_outside_is_seen(self):
        event = fixture_event()
        self.post_event(event)
        self.assertEqual(len(self.call('GET', '/api/events')[1]['events']), 1)
        with open(self.log_path, 'ab') as handle:
            handle.write(json.dumps(fixture_event(), separators=(',', ':')).encode() + b'\n')
        self.assertEqual(len(self.call('GET', '/api/events')[1]['events']), 2)


class WindowRouteTests(RouteCase):
    def fill(self, count):
        for _ in range(count):
            self.assertEqual(self.post_event(fixture_event())[0], 201)

    def test_the_default_window_is_500_lines(self):
        self.fill(3)
        report = self.call('GET', '/api/events')[1]
        self.assertEqual((report['limit'], report['offset'], report['total'], report['more']), (500, 0, 3, False))

    def test_limit_and_offset_choose_a_window_and_the_report_says_whether_more_follows(self):
        self.fill(7)
        report = self.call('GET', '/api/events?limit=3&offset=2')[1]
        self.assertEqual([item['line'] for item in report['lines']], [3, 4, 5])
        self.assertEqual((report['total'], report['more'], report['limit'], report['offset']), (7, True, 3, 2))
        report = self.call('GET', '/api/events?limit=3&offset=6')[1]
        self.assertEqual(([item['line'] for item in report['lines']], report['more']), ([7], False))

    def test_the_limit_is_at_most_5000(self):
        self.fill(1)
        self.assertEqual(self.call('GET', '/api/events?limit=5000')[0], 200)
        status, body = self.call('GET', '/api/events?limit=5001')
        self.assertEqual((status, body['code']), (400, 'invalid_request'))

    def test_bad_windows_are_400(self):
        self.fill(1)
        for query in ('limit=0', 'limit=-1', 'limit=abc', 'limit=', 'limit=1.5', 'offset=-1', 'offset=x', 'limit=1&limit=2', 'offset=1&offset=2',
                      'extra=1', 'limit=99999999999', 'limit=%D9%A1'):
            with self.subTest(query=query):
                status, body = self.call('GET', f'/api/events?{query}')
                self.assertEqual((status, body['code']), (400, 'invalid_request'))


class ExportHeaderTests(RouteCase):
    def test_a_clean_log_says_ok_and_no_truncated_tail(self):
        self.post_event(fixture_event())
        status, content, headers = self.call_full('GET', '/api/events/export')
        self.assertEqual(status, 200)
        self.assertEqual(content, self.log_path.read_bytes())
        self.assertEqual((headers['x-schola-log-ok'], headers['x-schola-truncated-tail']), ('true', 'false'))

    def test_a_damaged_log_says_so_and_a_cut_tail_says_so(self):
        self.post_event(fixture_event())
        with open(self.log_path, 'ab') as handle:
            handle.write(b'junk\n')
        headers = self.call_full('GET', '/api/events/export')[2]
        self.assertEqual((headers['x-schola-log-ok'], headers['x-schola-truncated-tail']), ('false', 'false'))
        with open(self.log_path, 'ab') as handle:
            handle.write(b'{"cut')
        content, headers = self.call_full('GET', '/api/events/export')[1:]
        self.assertEqual((headers['x-schola-log-ok'], headers['x-schola-truncated-tail']), ('false', 'true'))
        self.assertEqual(content, self.log_path.read_bytes())

    def test_a_log_with_too_many_lines_still_exports_and_says_it_is_not_ok(self):
        self.log_path.parent.mkdir(mode=0o700)
        os.chmod(self.log_path.parent, 0o700)
        self.log_path.write_bytes(b'\n' * (learning_event.MAX_LINES + 1))
        status, content, headers = self.call_full('GET', '/api/events/export')
        self.assertEqual((status, headers['x-schola-log-ok'], headers['x-schola-truncated-tail']), (200, 'false', 'false'))
        self.assertEqual(content, self.log_path.read_bytes())


class ReserveRouteTests(RouteCase):
    def test_the_page_gets_log_reserved_at_80_percent_and_the_app_still_writes(self):
        self.log_path.parent.mkdir(mode=0o700)
        os.chmod(self.log_path.parent, 0o700)
        self.log_path.write_bytes(b'\n' * event_log.RESERVE_LINES)
        before = self.log_path.read_bytes()
        status, body = self.post_event(fixture_event())
        self.assertEqual((status, body['code']), (409, 'log_reserved'))
        self.assertEqual(self.log_path.read_bytes(), before)
        self.assertTrue(self.server.event_log.append(fixture_event('check-result-passed'))['appended'])


class DeleteRouteTests(RouteCase):
    def test_delete_removes_the_log_and_every_reflection_and_says_how_many(self):
        self.post_event(fixture_event())
        ids = [self.call('POST', '/api/reflections', {'text': f'text {number}'})[1]['reflectionId'] for number in range(2)]
        status, body = self.call('DELETE', '/api/events', {'confirm': True})
        self.assertEqual((status, body), (200, {'deleted': True, 'logRemoved': True, 'reflectionsRemoved': 2}))
        for identifier in ids:
            self.assertEqual(self.call('GET', f'/api/reflections/{identifier}')[0], 404)
        self.assertEqual(self.call('GET', '/api/events')[1]['exists'], False)

    def test_a_content_length_of_00_is_an_empty_body_not_a_413(self):
        self.post_event(fixture_event())
        connection = HTTPConnection('127.0.0.1', self.server.server_port)
        connection.putrequest('DELETE', '/api/events')
        connection.putheader('Content-Type', 'application/json')
        connection.putheader('Content-Length', '00')
        connection.putheader('X-Schola-Check-Token', self.token)
        connection.endheaders()
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual((response.status, body['code']), (400, 'confirmation_required'))

    def test_a_content_length_that_is_not_a_number_is_a_400(self):
        connection = HTTPConnection('127.0.0.1', self.server.server_port)
        connection.putrequest('DELETE', '/api/events')
        connection.putheader('Content-Length', 'abc')
        connection.putheader('X-Schola-Check-Token', self.token)
        connection.endheaders()
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual((response.status, body['code']), (400, 'invalid_request'))


class ReflectionTieRouteTests(RouteCase):
    def test_an_event_for_a_reflection_that_was_never_saved_is_422(self):
        status, body = self.post_event(self.reflection_event({'reflectionId': str(uuid.uuid4()), 'charCount': 4}))
        self.assertEqual((status, body['code']), (422, 'reflection_mismatch'))
        self.assertFalse(self.log_path.exists())

    def test_an_event_for_a_saved_reflection_is_accepted_and_a_wrong_count_is_not(self):
        saved = self.call('POST', '/api/reflections', {'text': 'four'})[1]
        self.assertEqual(self.post_event(self.reflection_event(dict(saved, charCount=5)))[1]['code'], 'reflection_mismatch')
        self.assertEqual(self.post_event(self.reflection_event(saved))[0], 201)


class FaultMessageTests(RouteCase):
    def test_an_unexpected_fault_on_an_append_says_the_event_may_or_may_not_be_saved(self):
        with mock.patch('event_log.EventLog.append', side_effect=RuntimeError('SECRET-DETAIL')), mock.patch('traceback.print_exc'):
            status, body = self.post_event(fixture_event())
        self.assertEqual((status, body['code']), (500, 'internal_error'))
        self.assertIn('The event may or may not be saved', body['error'])
        self.assertNotIn('reflection', body['error'].lower())
        self.assertNotIn('lesson', body['error'].lower())
        self.assertNotIn('SECRET-DETAIL', json.dumps(body))

    def test_an_unexpected_fault_on_every_other_event_route_never_mentions_lessons(self):
        cases = (('GET', '/api/events', None, 'read'), ('GET', '/api/events/export', None, 'export_checked'), ('DELETE', '/api/events', {'confirm': True}, 'delete'),
                 ('POST', '/api/reflections', {'text': 'x'}, 'save_reflection'), ('GET', f'/api/reflections/{uuid.uuid4()}', None, 'get_reflection'))
        for method, path, body, name in cases:
            with self.subTest(path=path, method=method):
                with mock.patch.object(event_log.EventLog, name, side_effect=RuntimeError('boom')), mock.patch('traceback.print_exc'):
                    status, answer = self.call(method, path, body)
                self.assertEqual((status, answer['code']), (500, 'internal_error'))
                self.assertNotIn('lesson', answer['error'].lower())

    def test_a_lesson_route_keeps_its_own_message(self):
        with mock.patch('lesson_routes.handle_get', side_effect=RuntimeError('boom')), mock.patch('traceback.print_exc'):
            status, answer = self.call('GET', '/api/lessons', token=None)
        self.assertEqual(status, 500)
        self.assertIn('lessons', answer['error'])


class TokenFirstTests(RouteCase):
    def declared(self, method, path, token):
        """A request that declares a body and never sends it. The answer must not wait for the body."""
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        connection.putrequest(method, path)
        connection.putheader('Content-Type', 'application/json')
        connection.putheader('Content-Length', '200')
        if token:
            connection.putheader('X-Schola-Check-Token', token)
        connection.endheaders()
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def test_a_wrong_token_is_answered_before_the_body_is_read(self):
        for method, path in (('POST', '/api/events'), ('POST', '/api/reflections'), ('DELETE', '/api/events')):
            with self.subTest(method=method, path=path):
                for token in (None, 'wrong'):
                    status, body = self.declared(method, path, token)
                    self.assertEqual((status, body['code']), (403, 'invalid_token'))

    def test_a_cross_site_request_is_answered_before_the_body_is_read(self):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        connection.putrequest('POST', '/api/events')
        connection.putheader('Content-Type', 'application/json')
        connection.putheader('Content-Length', '200')
        connection.putheader('X-Schola-Check-Token', self.token)
        connection.putheader('Sec-Fetch-Site', 'cross-site')
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual((response.status, json.loads(response.read())['code']), (403, 'forbidden_origin'))
        connection.close()


class SourceRuleTests(RouteCase):
    """A web client must not be able to forge an event that only the app or an agent may write."""

    def test_source_app_is_403_and_writes_nothing(self):
        before = tree(self.data)
        event = fixture_event('check-result-passed')
        self.assertEqual(event['source'], 'app')
        status, body = self.post_event(event)
        self.assertEqual((status, body['code']), (403, 'source_forbidden'))
        self.assertEqual(tree(self.data), before)
        self.assertFalse((self.data / '_events').exists())

    def test_source_agent_is_403_and_writes_nothing(self):
        status, body = self.post_event(fixture_event('agent-offer-pending', source='agent'))
        self.assertEqual((status, body['code']), (403, 'source_forbidden'))
        self.assertFalse((self.data / '_events').exists())

    def test_source_app_is_refused_even_on_an_otherwise_valid_event_of_a_student_type(self):
        status, body = self.post_event(fixture_event(source='app'))
        self.assertEqual((status, body['code']), (403, 'source_forbidden'))
        self.assertFalse((self.data / '_events').exists())

    def test_source_app_is_refused_on_a_log_that_already_has_events(self):
        self.post_event(fixture_event())
        before = self.log_path.read_bytes()
        self.assertEqual(self.post_event(fixture_event('task-finished-passed'))[0], 403)
        self.assertEqual(self.log_path.read_bytes(), before)

    def test_a_student_cannot_claim_an_app_only_type(self):
        event = fixture_event('check-result-passed', source='student')
        status, body = self.post_event(event)
        self.assertEqual((status, body['code']), (422, 'invalid_event'))
        self.assertFalse((self.data / '_events').exists())

    def test_the_in_process_api_still_accepts_source_app(self):
        event = fixture_event('check-result-passed')
        self.assertTrue(event_log.EventLog(self.data).append(event)['appended'])
        report = self.call('GET', '/api/events')[1]
        self.assertEqual(report['events'][0]['event']['source'], 'app')


class ReflectionRouteTests(RouteCase):
    def test_save_and_fetch(self):
        status, saved = self.call('POST', '/api/reflections', {'text': 'I traced the loop by hand.'})
        self.assertEqual(status, 201)
        self.assertEqual(set(saved), {'reflectionId', 'charCount'})
        self.assertEqual(saved['charCount'], 26)
        status, body = self.call('GET', f"/api/reflections/{saved['reflectionId']}")
        self.assertEqual(status, 200)
        self.assertEqual(body, {'reflectionId': saved['reflectionId'], 'charCount': 26, 'text': 'I traced the loop by hand.'})

    def test_the_size_limits(self):
        self.assertEqual(self.call('POST', '/api/reflections', {'text': 'a' * 10000})[0], 201)
        status, body = self.call('POST', '/api/reflections', {'text': 'a' * 10001})
        self.assertEqual((status, body['code']), (422, 'invalid_reflection'))
        self.assertEqual(self.call('POST', '/api/reflections', {'text': ''})[0], 422)
        wide = self.call('POST', '/api/reflections', {'text': '🙂' * 10000})
        self.assertEqual(wide[0], 201)

    def test_request_shape_is_strict(self):
        for body, expected in (([], 400), ('text', 400), ({}, 400), ({'text': 'a', 'extra': 1}, 400), ({'text': 5}, 422), ({'text': None}, 422)):
            with self.subTest(body=body):
                self.assertEqual(self.call('POST', '/api/reflections', body)[0], expected)
        self.assertFalse((self.data / '_events').exists())

    def test_unknown_and_bad_ids(self):
        status, body = self.call('GET', f'/api/reflections/{uuid.uuid4()}')
        self.assertEqual((status, body['code']), (404, 'reflection_not_found'))
        for tail in ('x', 'A1B2C3D4-0000-4000-8000-000000000017', '..', '..%2f..%2fetc%2fpasswd', '%2e%2e', f'{uuid.uuid4()}.json', f'{uuid.uuid4()}/x', 'a/b', '.', ''):
            with self.subTest(tail=tail):
                status, body = self.call('GET', f'/api/reflections/{tail}')
                self.assertEqual((status, body['code']), (400, 'invalid_reflection_id'))

    def test_the_text_is_in_no_error_and_not_in_the_log(self):
        self.call('POST', '/api/reflections', {'text': SECRET})
        saved = self.call('POST', '/api/reflections', {'text': SECRET})[1]
        self.post_event(self.reflection_event(saved))
        for status, body in (self.call('GET', f"/api/reflections/{uuid.uuid4()}"), self.call('POST', '/api/reflections', {'text': SECRET * 2000}),
                             self.call('POST', '/api/reflections', {'text': SECRET, 'extra': 1})):
            self.assertNotIn(SECRET, json.dumps(body))
        self.assertEqual(self.call('GET', f"/api/reflections/{saved['reflectionId']}")[1]['text'], SECRET)
        self.assertNotIn(SECRET.encode(), self.log_path.read_bytes())


class AuthTests(RouteCase):
    """Every route has the Host and Origin rules of the server and needs the session token."""

    def requests(self):
        saved = self.call('POST', '/api/reflections', {'text': 'kept'})[1]['reflectionId']
        self.post_event(fixture_event())
        return [('GET', '/api/events', None), ('GET', '/api/events/export', None), ('POST', '/api/events', {'event': fixture_event()}),
                ('DELETE', '/api/events', {'confirm': True}), ('POST', '/api/reflections', {'text': 'new'}),
                ('GET', f'/api/reflections/{saved}', None)]

    def test_no_token_and_a_wrong_token_are_403(self):
        calls = self.requests()
        before = tree(self.data)
        for method, path, body in calls:
            for token in (None, 'wrong', self.token + 'x', self.token[:-1]):
                with self.subTest(method=method, path=path, token=token):
                    status, answer = self.call(method, path, body, token=token)
                    self.assertEqual((status, answer['code']), (403, 'invalid_token'))
        self.assertEqual(tree(self.data), before)

    def test_a_cross_site_browser_request_is_403(self):
        calls = self.requests()
        before = tree(self.data)
        for method, path, body in calls:
            with self.subTest(method=method, path=path):
                status, answer = self.call(method, path, body, headers={'Sec-Fetch-Site': 'cross-site'})
                self.assertEqual((status, answer['code']), (403, 'forbidden_origin'))
        self.assertEqual(tree(self.data), before)

    def test_a_wrong_host_or_origin_is_403(self):
        calls = self.requests()
        before = tree(self.data)
        for method, path, body in calls:
            for headers in ({'Host': 'evil.example'}, {'Origin': 'http://evil.example'}, {'Origin': 'null'}):
                with self.subTest(method=method, path=path, headers=headers):
                    status, answer = self.call(method, path, body, headers=headers)
                    self.assertEqual((status, answer['code']), (403, 'forbidden_origin'))
        self.assertEqual(tree(self.data), before)

    def test_a_same_site_browser_request_with_the_token_works(self):
        port = self.server.server_port
        status, _ = self.call('GET', '/api/events', headers={'Origin': f'http://127.0.0.1:{port}', 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(status, 200)


if __name__ == '__main__':
    unittest.main()
