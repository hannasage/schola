"""The event log and reflection routes of the local server."""
import hashlib
import json
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
        self.assertEqual(self.call('GET', '/api/events'), (200, {'ok': True, 'exists': False, 'lines': [], 'events': [], 'truncatedTail': False}))
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
        self.assertEqual(self.call('DELETE', '/api/events', {'confirm': True}), (200, {'deleted': True}))
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

    def test_a_missing_or_unknown_source_is_422_and_writes_nothing(self):
        missing = fixture_event()
        del missing['source']
        for event in (missing, fixture_event(source='teacher'), fixture_event(source=None), fixture_event(source=['app']), fixture_event(source='App')):
            with self.subTest(source=event.get('source')):
                status, body = self.post_event(event)
                self.assertEqual((status, body['code']), (422, 'invalid_event'))
        self.assertFalse(self.log_path.exists())

    def test_a_repeated_event_id_is_409(self):
        event = fixture_event()
        self.post_event(event)
        status, body = self.post_event(event)
        self.assertEqual((status, body['code']), (409, 'duplicate_event_id'))
        self.assertEqual(len(self.lines()), 1)

    def test_a_full_log_is_409_log_full(self):
        self.log_path.parent.mkdir()
        self.log_path.write_bytes(b'\n' * learning_event.MAX_LINES)
        status, body = self.post_event(fixture_event())
        self.assertEqual((status, body['code']), (409, 'log_full'))
        self.assertEqual(self.log_path.read_bytes(), b'\n' * learning_event.MAX_LINES)

    def test_a_held_lock_is_503_and_retryable(self):
        import fcntl
        import os
        self.log_path.parent.mkdir()
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
        self.post_event(fixture_event('reflection-saved'))
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
