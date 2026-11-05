"""The check routes: session token, workspace setting, consent, and the run route.

The design limit these tests respect: the token and the consent cannot tell a
student's click from another program on the same computer. They guard against
lesson content and against a request that carries no token. They do not guard
against a hostile local program, which already has the student's permissions.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import check_consent
import check_routes
import check_runner
import check_script
from server import Handler

FIXTURE = ROOT / 'tests' / 'fixtures' / 'contracts' / 'valid' / 'lesson-module-minimal.json'
TOKEN_HEADER = 'X-Schola-Check-Token'


SCRIPT = {'runtime': 'python3', 'source': 'print("hello")\n'}


def module_document(version='1.0.0', checks=None, schema='1.1'):
    document = json.loads(FIXTURE.read_text())
    document['version'] = version
    document['schemaVersion'] = schema
    document['checks'] = checks or [
        {'id': 'self', 'kind': 'self-reported', 'description': 'The student says it is done.'},
        {'id': 'has-readme', 'kind': 'automated', 'description': 'A README exists.', 'runner': {'type': 'file-exists', 'path': 'README.md'}},
        {'id': 'script-a', 'kind': 'automated', 'description': 'A tier two check.', 'runner': {'type': 'script'}, **SCRIPT},
        {'id': 'script-b', 'kind': 'automated', 'description': 'Another tier two check.', 'runner': {'type': 'script', 'name': 'other'}, **SCRIPT},
    ]
    return document


class FakeScriptAdapter:
    """Stands in for the tier two adapter. It records every call, so a test can prove nothing ran."""
    types = ('script',)

    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.explode = False

    def run(self, check, workspace, limits):
        self.calls.append((check['id'], workspace))
        self.started.set()
        self.release.wait(10)
        if self.explode:
            raise RuntimeError('boom')
        return {'status': 'passed', 'evidence': {'ran': True}, 'log': ['fake']}


class RouteCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(os.path.realpath(self.temp.name))
        self.data = base / 'data'
        self.data.mkdir()
        self.home = base / 'home'
        self.home.mkdir()
        self.work = self.home / 'work'
        self.work.mkdir()
        (self.work / 'README.md').write_text('hi')
        self.outside_home = base / 'elsewhere'
        self.outside_home.mkdir()
        self.base = base
        self.adapter = FakeScriptAdapter()
        self.start_server()
        self.import_module(module_document())

    def start_server(self, session=None):
        registry = check_runner.Registry()
        registry.register(check_runner.BuiltinAdapter())
        registry.register(self.adapter)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.data_dir = self.data
        self.server.check_session = session or check_routes.CheckSession(registry=registry, home=self.home)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server, self.server, self.thread)
        self.token = None

    @staticmethod
    def stop_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join()

    def call(self, method, path, body=None, headers=None, raw=None, token=True, origin=True, host=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port)
        port = self.server.server_port
        sent = {'Content-Type': 'application/json'}
        if host is not False:
            sent['Host'] = host or f'127.0.0.1:{port}'
        if origin is True:
            sent['Origin'] = f'http://127.0.0.1:{port}'
        elif origin:
            sent['Origin'] = origin
        if token is True:
            token = self.fetch_token()
        if token:
            sent[TOKEN_HEADER] = token
        sent.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        payload = json.loads(response.read() or b'null')
        connection.close()
        return response.status, payload

    def fetch_token(self):
        if self.token is None:
            connection = HTTPConnection('127.0.0.1', self.server.server_port)
            connection.request('GET', '/api/checks/session')
            self.token = json.loads(connection.getresponse().read())['token']
            connection.close()
        return self.token

    def import_module(self, document):
        status, payload = self.call('POST', '/api/lessons/import', {'source': json.dumps(document), 'filename': 'm.json'}, token=False)
        self.assertEqual(status, 200, payload)

    def set_workspace(self, path=None):
        status, payload = self.call('POST', '/api/checks/workspace', {'path': str(path or self.work)})
        self.assertEqual(status, 200, payload)
        return payload

    def ref(self, check_id, **extra):
        return {'moduleId': 'hello-module', 'version': '1.0.0', 'checkId': check_id, **extra}

    def consent(self, check_id='script-a'):
        status, shown = self.call('POST', '/api/checks/inspect', self.ref(check_id))
        self.assertEqual(status, 200, shown)
        status, payload = self.call('POST', '/api/checks/consent', self.ref(check_id, confirm=True, shownHash=shown['checkHash']))
        self.assertEqual(status, 200, payload)
        return payload


class SessionTests(RouteCase):
    def test_the_session_route_gives_one_random_token_for_this_launch(self):
        status, first = self.call('GET', '/api/checks/session', token=False)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(first['token']), 32)
        self.assertEqual(self.call('GET', '/api/checks/session', token=False)[1], first)
        self.assertEqual(set(first), {'token'})

    def test_two_servers_have_different_tokens(self):
        first = self.fetch_token()
        self.token = None
        self.start_server(session=check_routes.CheckSession())
        self.assertNotEqual(first, self.fetch_token())

    def test_the_token_comes_only_with_the_local_host_and_origin(self):
        for kwargs in ({'host': 'evil.example'}, {'host': 'evil.example:80'}, {'origin': 'http://evil.example'}, {'origin': 'null'},
                       {'origin': 'http://localhost:1'}, {'host': '127.0.0.1:1'}):
            with self.subTest(kwargs=kwargs):
                status, payload = self.call('GET', '/api/checks/session', token=False, **kwargs)
                self.assertEqual(status, 403)
                self.assertEqual(payload['code'], 'forbidden_origin')
                self.assertNotIn(self.fetch_token(), json.dumps(payload))

    def test_a_request_the_browser_marks_cross_site_gets_no_token(self):
        for site in ('cross-site', 'same-site'):
            status, payload = self.call('GET', '/api/checks/session', token=False, headers={'Sec-Fetch-Site': site})
            self.assertEqual(status, 403, site)
            self.assertNotIn(self.fetch_token(), json.dumps(payload))
        for site in ('same-origin', 'none'):
            self.assertEqual(self.call('GET', '/api/checks/session', token=False, headers={'Sec-Fetch-Site': site})[0], 200, site)

    def test_every_other_check_route_needs_the_token(self):
        routes = [('GET', '/api/checks/workspace', None), ('POST', '/api/checks/workspace', {'path': str(self.work)}),
                  ('POST', '/api/checks/inspect', self.ref('has-readme')), ('POST', '/api/checks/run', self.ref('has-readme')),
                  ('POST', '/api/checks/consent', self.ref('script-a', confirm=True, shownHash='0' * 64))]
        for method, path, body in routes:
            for label, token in (('missing', False), ('wrong', 'x' * 43), ('short', 'abc'), ('empty', ''), ('long', 'z' * 5000)):
                with self.subTest(path=path, token=label):
                    status, payload = self.call(method, path, body, token=token)
                    self.assertEqual(status, 403)
                    self.assertEqual(payload['code'], 'invalid_token')

    def test_the_token_is_compared_in_constant_time(self):
        with mock.patch.object(check_routes.hmac, 'compare_digest', wraps=check_routes.hmac.compare_digest) as compare:
            self.call('GET', '/api/checks/workspace')
            self.call('GET', '/api/checks/workspace', token='x' * 43)
        self.assertGreaterEqual(compare.call_count, 2)

    def test_a_token_with_odd_characters_is_just_wrong(self):
        status, payload = self.call('GET', '/api/checks/workspace', token='café'.encode('utf-8').decode('latin-1'))
        self.assertEqual((status, payload['code']), (403, 'invalid_token'))

    def test_a_token_in_the_url_or_the_body_does_not_count(self):
        token = self.fetch_token()
        self.assertEqual(self.call('GET', f'/api/checks/workspace?token={token}', token=False)[0], 403)
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('has-readme', token=token), token=False)[0], 403)

    def test_a_foreign_origin_with_a_good_token_is_still_refused(self):
        status, payload = self.call('POST', '/api/checks/run', self.ref('has-readme'), origin='http://evil.example')
        self.assertEqual((status, payload['code']), (403, 'forbidden_origin'))


class WorkspaceTests(RouteCase):
    def stored(self):
        return self.data / '_lessons' / 'check-workspace.json'

    def test_it_starts_unset(self):
        status, payload = self.call('GET', '/api/checks/workspace')
        self.assertEqual((status, payload['configured'], payload['path']), (200, False, None))

    def test_a_folder_is_saved_as_its_real_path_and_survives_a_restart(self):
        link = self.base / 'link-to-work'
        os.symlink(self.work, link)
        payload = self.set_workspace(link)
        self.assertEqual(payload['path'], str(self.work))
        self.assertTrue(payload['configured'])
        self.assertEqual(json.loads(self.stored().read_text())['path'], str(self.work))
        self.server.shutdown()
        self.token = None
        self.start_server()
        self.assertEqual(self.call('GET', '/api/checks/workspace')[1]['path'], str(self.work))

    def test_the_write_leaves_no_temporary_file(self):
        self.set_workspace()
        self.assertEqual(sorted(p.name for p in self.stored().parent.iterdir() if p.is_file()), ['check-workspace.json', 'pins.json'])

    def test_the_other_lesson_data_is_untouched(self):
        before = {str(p): p.read_bytes() for p in (self.data / '_lessons' / 'modules').rglob('*') if p.is_file()}
        self.set_workspace()
        after = {str(p): p.read_bytes() for p in (self.data / '_lessons' / 'modules').rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.call('GET', '/api/lessons', token=False)[0], 200)
        self.assertEqual(self.call('GET', '/api/lessons/export', token=False)[0], 200)

    def test_unsafe_choices_are_refused_and_change_nothing(self):
        self.set_workspace()
        saved = self.stored().read_bytes()
        (self.work / 'file.txt').write_text('x')
        inside_data = self.data / 'sub'
        inside_data.mkdir()
        inside_lessons = self.data / '_lessons' / 'modules'
        deep_home = self.home / 'projects'
        deep_home.mkdir()
        (self.home / '.ssh').mkdir()
        (deep_home / '.venv').mkdir()
        os.symlink(self.home / '.ssh', self.base / 'hidden-link')
        os.symlink(self.home, self.base / 'home-link')
        os.symlink(self.data, self.base / 'data-link')
        cases = {
            'root': '/', 'home': str(self.home), 'parent of home': str(self.home.parent), 'data dir': str(self.data),
            'inside data dir': str(inside_data), 'inside lessons': str(inside_lessons), 'contains data dir': str(self.data.parent),
            'link to home': str(self.base / 'home-link'), 'link to data': str(self.base / 'data-link'),
            'outside home': str(self.outside_home), 'a system folder': '/etc', 'a hidden folder in home': str(self.home / '.ssh'),
            'a hidden folder below': str(self.home / 'projects' / '.venv'), 'a link to a hidden folder': str(self.base / 'hidden-link'),
            'missing': str(self.work / 'nope'), 'a file': str(self.work / 'file.txt'), 'relative': 'work', 'dot': '.', 'empty': '',
            'tilde': '~', 'nul': str(self.work) + '\x00', 'long': '/' + 'a' * 5000, 'parent parts': str(self.work) + '/../work/../../..',
        }
        for label, path in cases.items():
            with self.subTest(label=label):
                status, payload = self.call('POST', '/api/checks/workspace', {'path': path})
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload['code'], 'invalid_workspace')
                self.assertEqual(self.stored().read_bytes(), saved)
        for label, value in {'number': 5, 'list': ['/tmp'], 'null': None}.items():
            with self.subTest(label=label):
                self.assertEqual(self.call('POST', '/api/checks/workspace', {'path': value})[0], 400)
        self.assertEqual(self.stored().read_bytes(), saved)

    def test_only_a_path_field_is_allowed(self):
        self.assertEqual(self.call('POST', '/api/checks/workspace', {'path': str(self.work), 'extra': 1})[0], 400)
        self.assertEqual(self.call('POST', '/api/checks/workspace', {})[0], 400)
        self.assertEqual(self.call('POST', '/api/checks/workspace', [str(self.work)])[0], 400)

    def test_a_saved_folder_that_later_becomes_unsafe_is_not_used(self):
        self.set_workspace()
        self.stored().write_text(json.dumps({'version': 1, 'path': str(self.home)}))
        status, payload = self.call('GET', '/api/checks/workspace')
        self.assertEqual(status, 200)
        self.assertFalse(payload['usable'])
        status, payload = self.call('POST', '/api/checks/run', self.ref('has-readme'))
        self.assertEqual((status, payload['code']), (409, 'workspace_required'))

    def test_a_damaged_setting_file_is_reported_and_kept(self):
        self.set_workspace()
        self.stored().write_text('{not json')
        status, payload = self.call('GET', '/api/checks/workspace')
        self.assertEqual((status, payload['code']), (500, 'corrupt_file'))
        self.assertEqual(self.stored().read_text(), '{not json')


class BodyTests(RouteCase):
    def test_a_body_over_64_kb_is_refused(self):
        big = {'path': 'a' * 70000}
        for path in ('/api/checks/workspace', '/api/checks/run', '/api/checks/consent', '/api/checks/inspect'):
            with self.subTest(path=path):
                status, payload = self.call('POST', path, big)
                self.assertEqual((status, payload['code']), (413, 'payload_too_large'))

    def test_lesson_routes_keep_their_larger_limit(self):
        document = module_document(version='2.0.0')
        document['blocks'] = [{'type': 'text', 'id': f'b{i}', 'body': 'x' * 40000} for i in range(3)]
        status, payload = self.call('POST', '/api/lessons/import', {'source': json.dumps(document), 'filename': 'big.json'}, token=False)
        self.assertEqual(status, 200, payload)

    def test_other_body_problems(self):
        self.assertEqual(self.call('POST', '/api/checks/run', raw=b'{nope')[1]['code'], 'invalid_json')
        self.assertEqual(self.call('POST', '/api/checks/run', raw=b'[]')[0], 400)
        self.assertEqual(self.call('POST', '/api/checks/run', raw=b'{"a": NaN}')[1]['code'], 'invalid_json')
        status, payload = self.call('POST', '/api/checks/run', self.ref('has-readme'), headers={'Content-Type': 'text/plain'})
        self.assertEqual((status, payload['code']), (415, 'unsupported_media_type'))
        self.assertEqual(self.call('GET', '/api/checks/nothing')[0], 404)
        self.assertEqual(self.call('PUT', '/api/checks/run', self.ref('has-readme'))[0], 404)


class RunTests(RouteCase):
    def test_a_built_in_check_runs_without_consent(self):
        self.set_workspace()
        status, payload = self.call('POST', '/api/checks/run', self.ref('has-readme'))
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload['result']['status'], 'passed')
        self.assertEqual(payload['checkId'], 'has-readme')
        self.assertEqual(payload['tier'], 1)
        self.assertRegex(payload['checkHash'], r'^[0-9a-f]{64}$')
        (self.work / 'README.md').unlink()
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('has-readme'))[1]['result']['status'], 'failed')

    def test_a_self_reported_check_is_skipped(self):
        self.set_workspace()
        status, payload = self.call('POST', '/api/checks/run', self.ref('self'))
        self.assertEqual((status, payload['result']['status']), (200, 'skipped'))

    def test_a_workspace_is_needed(self):
        status, payload = self.call('POST', '/api/checks/run', self.ref('has-readme'))
        self.assertEqual((status, payload['code']), (409, 'workspace_required'))

    def test_unknown_module_version_or_check(self):
        self.set_workspace()
        for body in ({'moduleId': 'nope', 'version': '1.0.0', 'checkId': 'has-readme'}, {'moduleId': 'hello-module', 'version': '9.9.9', 'checkId': 'has-readme'},
                     {'moduleId': 'hello-module', 'version': '1.0.0', 'checkId': 'nope'}, {'moduleId': '../x', 'version': '1.0.0', 'checkId': 'has-readme'},
                     {'moduleId': 'hello-module', 'version': '../../1.0.0', 'checkId': 'has-readme'}):
            with self.subTest(body=body):
                status, payload = self.call('POST', '/api/checks/run', body)
                self.assertEqual((status, payload['code']), (404, 'not_found'))

    def test_the_request_shape_is_strict(self):
        self.set_workspace()
        for body in ({}, {'moduleId': 'hello-module'}, self.ref('has-readme', extra=1), {**self.ref('has-readme'), 'version': 1},
                     {**self.ref('has-readme'), 'checkId': ['a']}, {**self.ref('has-readme'), 'moduleId': None}, self.ref('has-readme', consentId=5)):
            with self.subTest(body=body):
                status, payload = self.call('POST', '/api/checks/run', body)
                self.assertEqual((status, payload['code']), (400, 'invalid_request'))

    def test_the_run_does_not_change_the_workspace_or_the_lessons(self):
        self.set_workspace()
        lessons_before = {str(p): p.read_bytes() for p in (self.data / '_lessons' / 'modules').rglob('*') if p.is_file()}
        before = sorted(p.name for p in self.work.iterdir())
        self.call('POST', '/api/checks/run', self.ref('has-readme'))
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), before)
        self.assertEqual({str(p): p.read_bytes() for p in (self.data / '_lessons' / 'modules').rglob('*') if p.is_file()}, lessons_before)

    def test_a_check_in_a_damaged_stored_module_is_a_server_fault_without_detail(self):
        self.set_workspace()
        path = self.data / '_lessons' / 'modules' / 'hello-module' / '1.0.0' / 'module.json'
        path.write_bytes(path.read_bytes() + b' ')
        status, payload = self.call('POST', '/api/checks/run', self.ref('has-readme'))
        self.assertEqual((status, payload['code']), (500, 'corrupt_file'))
        self.assertNotIn(str(self.data), json.dumps(payload))


class ConsentTests(RouteCase):
    def test_a_tier_two_check_without_consent_is_refused_and_nothing_runs(self):
        self.set_workspace()
        status, payload = self.call('POST', '/api/checks/run', self.ref('script-a'))
        self.assertEqual((status, payload['code']), (403, 'consent_required'))
        self.assertEqual(self.adapter.calls, [])

    def test_inspect_shows_what_would_run(self):
        self.set_workspace()
        status, shown = self.call('POST', '/api/checks/inspect', self.ref('script-a'))
        self.assertEqual(status, 200)
        self.assertTrue(shown['needsConsent'])
        self.assertEqual(shown['workspace'], str(self.work))
        self.assertEqual(shown['runnerType'], 'script')
        self.assertRegex(shown['checkHash'], r'^[0-9a-f]{64}$')
        _, builtin = self.call('POST', '/api/checks/inspect', self.ref('has-readme'))
        self.assertFalse(builtin['needsConsent'])

    def test_consent_then_run(self):
        self.set_workspace()
        granted = self.consent()
        self.assertRegex(granted['consentId'], r'^[A-Za-z0-9_-]{16,}$')
        self.assertEqual(granted['workspace'], str(self.work))
        status, payload = self.call('POST', '/api/checks/run', self.ref('script-a', consentId=granted['consentId']))
        self.assertEqual((status, payload['result']['status'], payload['tier']), (200, 'passed', 2))
        self.assertEqual(self.adapter.calls, [('script-a', str(self.work))])
        # The id is required. A matching binding without the id is not enough.
        status, payload = self.call('POST', '/api/checks/run', self.ref('script-a'))
        self.assertEqual((status, payload['code']), (403, 'consent_required'))
        self.assertEqual(len(self.adapter.calls), 1)

    def test_consent_needs_the_workspace(self):
        status, shown = self.call('POST', '/api/checks/inspect', self.ref('script-a'))
        self.assertEqual((status, shown['code']), (409, 'workspace_required'))
        status, payload = self.call('POST', '/api/checks/consent', self.ref('script-a', confirm=True, shownHash='0' * 64))
        self.assertEqual((status, payload['code']), (409, 'workspace_required'))

    def test_a_wrong_hash_grants_nothing(self):
        self.set_workspace()
        for shown in ('0' * 64, 'f' * 64, '', 'abc', 5, None):
            with self.subTest(shown=shown):
                status, payload = self.call('POST', '/api/checks/consent', self.ref('script-a', confirm=True, shownHash=shown))
                self.assertIn(status, (400, 409))
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('script-a'))[1]['code'], 'consent_required')
        self.assertEqual(self.adapter.calls, [])

    def test_confirm_must_be_exactly_true(self):
        self.set_workspace()
        shown = self.call('POST', '/api/checks/inspect', self.ref('script-a'))[1]['checkHash']
        for confirm in (False, 'true', 1, None, [], {}):
            with self.subTest(confirm=confirm):
                status, payload = self.call('POST', '/api/checks/consent', self.ref('script-a', confirm=confirm, shownHash=shown))
                self.assertEqual((status, payload['code']), (400, 'confirmation_required'))
        body = self.ref('script-a', shownHash=shown)
        self.assertEqual(self.call('POST', '/api/checks/consent', body)[0], 400)
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('script-a'))[1]['code'], 'consent_required')

    def test_consent_is_not_needed_for_a_built_in_check(self):
        self.set_workspace()
        shown = self.call('POST', '/api/checks/inspect', self.ref('has-readme'))[1]['checkHash']
        status, payload = self.call('POST', '/api/checks/consent', self.ref('has-readme', confirm=True, shownHash=shown))
        self.assertEqual((status, payload['code']), (400, 'consent_not_needed'))

    def test_consent_for_one_check_does_not_cover_another(self):
        self.set_workspace()
        granted = self.consent('script-a')
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('script-b'))[1]['code'], 'consent_required')
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('script-b', consentId=granted['consentId']))[1]['code'], 'consent_required')
        self.assertEqual(self.adapter.calls, [])

    def test_consent_is_bound_to_the_workspace(self):
        self.set_workspace()
        granted = self.consent()
        other = self.home / 'other'
        other.mkdir()
        self.set_workspace(other)
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('script-a', consentId=granted['consentId']))[1]['code'], 'consent_required')
        self.set_workspace(self.work)
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('script-a', consentId=granted['consentId']))[0], 200)

    def test_consent_ends_with_the_server_session(self):
        self.set_workspace()
        granted = self.consent()
        self.server.shutdown()
        self.token = None
        self.start_server()
        status, payload = self.call('POST', '/api/checks/run', self.ref('script-a', consentId=granted['consentId']))
        self.assertEqual((status, payload['code']), (403, 'consent_required'))
        self.assertEqual(self.adapter.calls, [])

    def test_consent_is_bound_to_the_check_hash(self):
        """A changed descriptor is a different hash, so the old consent does not apply."""
        self.set_workspace()
        granted = self.consent('script-a')
        changed = module_document(version='1.1.0', checks=[
            {'id': 'script-a', 'kind': 'automated', 'description': 'A tier two check.', 'runner': {'type': 'script', 'changed': True}, **SCRIPT}])
        self.import_module(changed)
        body = {'moduleId': 'hello-module', 'version': '1.1.0', 'checkId': 'script-a', 'consentId': granted['consentId']}
        self.assertEqual(self.call('POST', '/api/checks/run', body)[1]['code'], 'consent_required')
        same_hash = {**body, 'version': '1.0.0'}
        self.assertEqual(self.call('POST', '/api/checks/run', same_hash)[0], 200)

    def test_a_run_request_can_never_grant_consent(self):
        self.set_workspace()
        shown = self.call('POST', '/api/checks/inspect', self.ref('script-a'))[1]['checkHash']
        attempts = [
            self.ref('script-a', confirm=True, shownHash=shown),
            self.ref('script-a', confirm=True),
            self.ref('script-a', consent=True),
            self.ref('script-a', consentId='forged-consent-id-0000'),
            self.ref('script-a', consentId=True),
            self.ref('script-a', grant=True, shownHash=shown),
        ]
        for body in attempts:
            with self.subTest(body=sorted(body)):
                status, payload = self.call('POST', '/api/checks/run', body)
                self.assertIn(status, (400, 403))
        for headers in ({'X-Schola-Consent': 'yes'}, {'X-Consent': shown}, {'X-Schola-Agent': 'tutor', 'X-Schola-Confirm': 'true'}):
            status, payload = self.call('POST', '/api/checks/run', self.ref('script-a'), headers=headers)
            self.assertEqual((status, payload['code']), (403, 'consent_required'))
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(len(self.server.check_session.consents.records()), 0)

    def test_consent_can_only_be_created_by_the_consent_route(self):
        self.set_workspace()
        for path in ('/api/checks/inspect', '/api/checks/workspace', '/api/checks/run'):
            shown = self.call('POST', '/api/checks/inspect', self.ref('script-a'))[1]['checkHash']
            self.call('POST', path, self.ref('script-a', confirm=True, shownHash=shown))
        self.assertEqual(len(self.server.check_session.consents.records()), 0)

    def test_the_consent_route_wants_a_browser_origin(self):
        """A speed bump only. A program on this computer can send any Origin it likes."""
        self.set_workspace()
        shown = self.call('POST', '/api/checks/inspect', self.ref('script-a'))[1]['checkHash']
        status, payload = self.call('POST', '/api/checks/consent', self.ref('script-a', confirm=True, shownHash=shown), origin=False)
        self.assertEqual((status, payload['code']), (403, 'forbidden_origin'))
        self.assertEqual(len(self.server.check_session.consents.records()), 0)

    def test_a_consent_matches_only_its_exact_binding(self):
        registry = check_consent.ConsentRegistry()
        record = registry.grant('m', '1.0.0', 'c', 'a' * 64, '/w')
        self.assertEqual(registry.find('m', '1.0.0', 'c', 'a' * 64, '/w', consent_id=record['consentId'])['consentId'], record['consentId'])
        self.assertIsNone(registry.find('m', '1.0.0', 'c', 'a' * 64, '/w'), 'the id is required')
        self.assertIsNone(registry.find('m', '1.0.0', 'c', 'a' * 64, '/w', consent_id=None))
        self.assertIsNone(registry.find('m', '1.0.0', 'c', 'a' * 64, '/w', consent_id=''))
        for binding in (('n', '1.0.0', 'c', 'a' * 64, '/w'), ('m', '1.0.1', 'c', 'a' * 64, '/w'), ('m', '1.0.0', 'd', 'a' * 64, '/w'),
                        ('m', '1.0.0', 'c', 'b' * 64, '/w'), ('m', '1.0.0', 'c', 'a' * 64, '/x')):
            self.assertIsNone(registry.find(*binding, consent_id=record['consentId']), binding)
        self.assertIsNone(registry.find('m', '1.0.0', 'c', 'a' * 64, '/w', consent_id='wrong'))
        registry.clear()
        self.assertIsNone(registry.find('m', '1.0.0', 'c', 'a' * 64, '/w', consent_id=record['consentId']))

    def test_the_consent_list_is_bounded(self):
        registry = check_consent.ConsentRegistry(limit=3)
        for index in range(10):
            registry.grant('m', '1.0.0', f'c{index}', 'h' * 64, '/w')
        self.assertEqual(len(registry.records()), 3)
        first = registry.records()
        self.assertEqual([r['checkId'] for r in first], ['c7', 'c8', 'c9'])


class ReviewFixTests(RouteCase):
    def test_a_run_for_a_script_needs_the_consent_id_and_checks_it(self):
        self.set_workspace()
        granted = self.consent('script-a')
        other = self.consent('script-b')
        for body in (self.ref('script-a'), self.ref('script-a', consentId=''), self.ref('script-a', consentId='x' * 24),
                     self.ref('script-a', consentId=other['consentId'])):
            with self.subTest(body=body):
                status, payload = self.call('POST', '/api/checks/run', body)
                self.assertEqual((status, payload['code']), (403, 'consent_required'))
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('script-a', consentId=granted['consentId']))[0], 200)

    def test_every_check_route_refuses_a_foreign_host_even_with_a_good_token(self):
        routes = [('GET', '/api/checks/workspace', None), ('POST', '/api/checks/workspace', {'path': str(self.work)}),
                  ('POST', '/api/checks/inspect', self.ref('has-readme')), ('POST', '/api/checks/run', self.ref('has-readme')),
                  ('POST', '/api/checks/consent', self.ref('script-a', confirm=True, shownHash='0' * 64))]
        for method, path, body in routes:
            for host in ('evil.example', 'evil.example:80', '127.0.0.1:1', f'127.0.0.1.evil.example:{self.server.server_port}'):
                with self.subTest(path=path, host=host):
                    status, payload = self.call(method, path, body, host=host, origin=False)
                    self.assertEqual((status, payload['code']), (403, 'forbidden_origin'))
        self.assertFalse((self.data / '_lessons' / 'check-workspace.json').exists())

    def test_a_saved_setting_with_the_wrong_shape_is_damage_and_never_unconfigured(self):
        self.set_workspace()
        stored = self.data / '_lessons' / 'check-workspace.json'
        for text in ('', '[]', '{}', '{"version": 1}', '{"version": 1, "path": 5}', '{"version": 2, "path": "/x"}', 'null', '"text"'):
            stored.write_text(text)
            with self.subTest(text=text):
                status, payload = self.call('GET', '/api/checks/workspace')
                self.assertEqual((status, payload['code']), (500, 'corrupt_file'))
                status, payload = self.call('POST', '/api/checks/run', self.ref('has-readme'))
                self.assertEqual((status, payload['code']), (500, 'corrupt_file'))
                self.assertEqual(stored.read_text(), text)

    def test_a_version_1_0_script_runner_with_no_fields_needs_no_consent_and_is_skipped(self):
        legacy = module_document(version='2.0.0', schema='1.0', checks=[
            {'id': 'old-script', 'kind': 'automated', 'description': 'A 1.0 script runner.', 'runner': {'type': 'script'}}])
        self.import_module(legacy)
        self.set_workspace()
        real = check_runner.Registry()
        real.register(check_runner.BuiltinAdapter())
        real.register(check_script.ScriptAdapter())
        self.server.check_session.registry = real
        ref = {'moduleId': 'hello-module', 'version': '2.0.0', 'checkId': 'old-script'}
        status, shown = self.call('POST', '/api/checks/inspect', ref)
        self.assertEqual((status, shown['needsConsent']), (200, False))
        status, payload = self.call('POST', '/api/checks/run', ref)
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload['tier'], payload['result']['status']), (1, 'skipped'))
        self.assertEqual(self.adapter.calls, [])

    def test_inspect_lists_the_limits_that_do_not_apply(self):
        self.set_workspace()
        status, shown = self.call('POST', '/api/checks/inspect', self.ref('script-a'))
        self.assertTrue(any(item.lower().startswith('memory') for item in shown['limitsNotApplied']), shown)
        self.assertTrue(any(item.startswith('timeout=') for item in shown['limits']))
        status, builtin = self.call('POST', '/api/checks/inspect', self.ref('has-readme'))
        self.assertEqual(builtin['limitsNotApplied'], [])


class BusyTests(RouteCase):
    def test_a_second_run_while_one_is_going_gets_429(self):
        self.set_workspace()
        granted = self.consent()
        self.adapter.release.clear()
        first = {}
        thread = threading.Thread(target=lambda: first.update(zip(('status', 'payload'), self.call('POST', '/api/checks/run', self.ref('script-a', consentId=granted['consentId'])))))
        thread.start()
        self.assertTrue(self.adapter.started.wait(5))
        status, payload = self.call('POST', '/api/checks/run', self.ref('has-readme'))
        self.assertEqual((status, payload['code']), (429, 'busy'))
        self.assertTrue(payload['retryable'])
        self.adapter.release.set()
        thread.join(10)
        self.assertEqual(first['status'], 200)
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('has-readme'))[0], 200)

    def test_the_lock_is_released_after_a_fault(self):
        self.set_workspace()
        self.consent()
        with mock.patch.object(check_routes.check_runner, 'run', side_effect=RuntimeError('/secret/path')), mock.patch('server.traceback.print_exc'):
            status, payload = self.call('POST', '/api/checks/run', self.ref('has-readme'))
        self.assertEqual((status, payload['code']), (500, 'internal_error'))
        self.assertNotIn('secret', json.dumps(payload))
        self.assertEqual(self.call('POST', '/api/checks/run', self.ref('has-readme'))[0], 200)


class HashTests(unittest.TestCase):
    CHECK = {'id': 'c', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'script', 'a': 1, 'b': ['x']},
             'runtime': 'python3', 'source': 'print(1)\n', 'args': ['--x'], 'timeoutSeconds': 30}

    def test_the_hash_is_stable_and_ignores_key_order_and_the_description(self):
        first = check_consent.check_hash(self.CHECK)
        shuffled = {'timeoutSeconds': 30, 'args': ['--x'], 'source': 'print(1)\n', 'runtime': 'python3',
                    'runner': {'b': ['x'], 'a': 1, 'type': 'script'}, 'description': 'a different description', 'kind': 'automated', 'id': 'other'}
        self.assertEqual(first, check_consent.check_hash(shuffled))
        self.assertRegex(first, r'^[0-9a-f]{64}$')

    def test_every_part_that_runs_changes_the_hash(self):
        base = check_consent.check_hash(self.CHECK)
        for field, value in (('source', 'print(2)\n'), ('source', 'print(1)\n '), ('runtime', 'node'), ('args', ['--y']), ('args', []),
                             ('timeoutSeconds', 31), ('runner', {'type': 'script', 'a': 2, 'b': ['x']}), ('runner', {'type': 'script-2', 'a': 1, 'b': ['x']})):
            changed = copy.deepcopy(self.CHECK)
            changed[field] = value
            with self.subTest(field=field, value=value):
                self.assertNotEqual(base, check_consent.check_hash(changed))

    def test_the_script_bytes_are_hashed_as_utf8(self):
        check = copy.deepcopy(self.CHECK)
        check['source'] = 'print("café \U0001f600")\n'
        self.assertRegex(check_consent.check_hash(check), r'^[0-9a-f]{64}$')
        self.assertEqual(check_consent.source_bytes(check), check['source'].encode('utf-8'))
        self.assertEqual(check_consent.source_bytes({'id': 'x'}), b'')

    def test_which_checks_need_consent(self):
        self.assertTrue(check_consent.needs_consent(self.CHECK))
        self.assertFalse(check_consent.needs_consent({'kind': 'automated', 'runner': {'type': 'file-exists'}}))
        self.assertFalse(check_consent.needs_consent({'kind': 'self-reported'}))
        self.assertFalse(check_consent.needs_consent(None))

    def test_the_digest_matches_a_hand_computed_value(self):
        canonical = json.dumps({'args': ['--x'], 'runner': {'a': 1, 'b': ['x'], 'type': 'script'}, 'runtime': 'python3', 'timeoutSeconds': 30},
                               sort_keys=True, separators=(',', ':')).encode()
        expected = hashlib.sha256(canonical + b'\n' + b'print(1)\n').hexdigest()
        self.assertEqual(check_consent.check_hash(self.CHECK), expected)


if __name__ == '__main__':
    unittest.main()
