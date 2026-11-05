"""The API adapter: endpoint rules, the request it sends, the replies it accepts, and every error path.

A fake provider runs on a loopback port in a thread. The tests never use the real network and never call a real provider.
"""
import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api_adapter
import model_adapter as ma
from api_adapter import ApiAdapter
from model_adapter import ApiKey, CompletionRequest, Message, ModelAdapterError, TokenBudget

SECRET = 'sk-TESTSECRETVALUE0123456789abcdef'
BODY_SECRET = 'PROVIDER-BODY-MARKER-77e1'
EXPECTED_FORMAT = 'The provider reply was not in the expected format.'
LOOPBACK_ONLY = ('127.0.0.1',)


def ok_payload(text='Hello from the model.', usage=None, finish='stop', **extra):
    payload = {'id': 'chatcmpl-1', 'object': 'chat.completion',
               'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': finish}]}
    payload['usage'] = {'prompt_tokens': 12, 'completion_tokens': 7, 'total_tokens': 19} if usage is None else usage
    if usage == 'omit':
        del payload['usage']
    payload.update(extra)
    return payload


def send(handler, status=200, body=b'', headers=None, content_type='application/json', length=True):
    handler.send_response(status)
    if content_type is not None:
        handler.send_header('Content-Type', content_type)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    if length:
        handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def reply_json(payload, status=200, **options):
    return lambda handler, body: send(handler, status, json.dumps(payload).encode('utf-8'), **options)


def reply_raw(body, status=200, **options):
    return lambda handler, request_body: send(handler, status, body, **options)


class Provider:
    """A fake OpenAI-format endpoint. `responder(handler, request_body)` writes the reply. `requests` records each request."""

    def __init__(self, responder=None, tls=None):
        self.requests = []
        self.responder = responder or reply_json(ok_payload())
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length)
                provider.requests.append({'method': 'POST', 'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body})
                provider.responder(self, body)

            def log_message(self, *args):
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.server = Server(('127.0.0.1', 0), Handler)
        if tls is not None:
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def url(self, path='/v1', scheme='http'):
        return f'{scheme}://127.0.0.1:{self.port}{path}'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def call(budget=None, text='Hello.', max_output_tokens=100):
    return CompletionRequest([Message('user', text)], max_output_tokens, budget or TokenBudget(100_000))


class Case(unittest.TestCase):
    def provider(self, responder=None, **options):
        provider = Provider(responder, **options)
        self.addCleanup(provider.stop)
        return provider

    def adapter(self, provider, key=SECRET, path='/v1', **options):
        api_key = None if key is None else ApiKey(key)
        return ApiAdapter(provider.url(path), 'test-model', api_key, **options)

    def assertCode(self, code, function, *args, retryable=None, **kwargs):
        with self.assertRaises(ModelAdapterError) as context:
            function(*args, **kwargs)
        error = context.exception
        self.assertEqual(error.code, code, str(error))
        if retryable is not None:
            self.assertEqual(error.retryable, retryable)
        self.assertClean(error)
        return error

    def assertClean(self, error):
        """No key, no provider text, and no chained error in anything an error can show."""
        shown = ' '.join([str(error), repr(error), repr(error.args), json.dumps(error.payload()),
                          ''.join(traceback.format_exception(error)), ''.join(traceback.format_exception(error, capture_locals=False))])
        for secret in (SECRET, SECRET[:14], BODY_SECRET):
            self.assertNotIn(secret, shown)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)


class EndpointRuleTests(Case):
    def make(self, url, key=SECRET, **options):
        return ApiAdapter(url, 'model-x', None if key is None else ApiKey(key), **options)

    def test_good_endpoints_are_accepted(self):
        for url in ('https://api.openai.com/v1', 'https://api.example.com', 'https://api.example.com/', 'https://api.example.com/v1/',
                    'https://api.example.com:8443/openai/v1', 'HTTPS://API.EXAMPLE.COM/v1', 'http://127.0.0.1:11434/v1', 'http://localhost:8080/v1',
                    'http://[::1]:8000/v1', 'https://127.0.0.1:8443/v1', 'http://LOCALHOST/v1', 'https://[2606:4700::1111]/v1', 'https://8.8.8.8/v1'):
            with self.subTest(url=url):
                self.make(url)

    def test_http_is_refused_except_for_loopback(self):
        for url in ('http://api.example.com/v1', 'http://8.8.8.8/v1', 'http://127.0.0.2/v1', 'http://127.1/v1', 'http://0.0.0.0/v1',
                    'http://[::ffff:127.0.0.1]/v1', 'http://localhost.example.com/v1', 'http://127.0.0.1.example.com/v1',
                    'http://[::]/v1', 'http://10.0.0.1/v1', 'http://localhost./v1'):
            with self.subTest(url=url):
                self.assertCode('unsafe_endpoint', self.make, url)

    def test_other_schemes_and_shapes_are_refused(self):
        for url in ('ftp://example.com/v1', 'file:///etc/passwd', 'ws://example.com/v1', 'wss://example.com/v1', 'javascript:alert(1)',
                    'data:text/plain,x', '//example.com/v1', 'example.com/v1', '/v1', 'https://', 'https:///v1', 'https:example.com'):
            with self.subTest(url=url):
                self.assertCode('unsafe_endpoint', self.make, url)

    def test_credentials_are_refused_and_never_echoed(self):
        for url in ('https://user:hunter2pass@example.com/v1', 'https://user@example.com/v1', 'https://:hunter2pass@example.com/v1',
                    'https://example.com@evil.com/v1', 'https://example.com\\@evil.com/v1', 'http://user:hunter2pass@127.0.0.1/v1'):
            with self.subTest(url=url):
                error = self.assertCode('unsafe_endpoint', self.make, url)
                for piece in ('hunter2pass', 'user', 'example.com', 'evil'):
                    self.assertNotIn(piece, str(error))

    def test_fragments_and_queries_are_refused(self):
        for url in ('https://example.com/v1#frag', 'https://example.com/v1#', 'https://example.com/v1?x=1', 'https://example.com/v1?', 'https://example.com?k=v'):
            with self.subTest(url=url):
                self.assertCode('unsafe_endpoint', self.make, url)

    def test_malformed_urls_are_refused(self):
        for url in ('https://exa mple.com/v1', 'https://example.com/v1\n', 'https://example.com/v1\r\nX: y', 'https://example.com/v1\t', ' https://example.com/v1',
                    'https://example.com/v1 ', 'https://example.com/\x00', 'https://example.com:0/v1', 'https://example.com:65536/v1',
                    'https://example.com:abc/v1', 'https://example.com:-1/v1', 'https://[::1/v1', 'https://ex%61mple.com/v1', 'https://café.com/v1',
                    'https://example.com/café', 'https://-bad.example.com/', 'https://exa_mple.com/', 'https://example..com/', 'https://.example.com/',
                    'https://example.com/' + 'a' * 3000, 'https://[fe80::1%25eth0]/v1', 'https://example.com/%2e%2e/x'):
            with self.subTest(url=url):
                self.assertCode('unsafe_endpoint', self.make, url)

    def test_paths_with_dot_segments_or_double_slashes_are_refused(self):
        for path in ('/..', '/v1/..', '/a/../b', '/./v1', '/a//b', '//v1', '/v1/.'):
            with self.subTest(path=path):
                self.assertCode('unsafe_endpoint', self.make, 'https://example.com' + path)

    def test_ambiguous_numeric_hosts_are_refused(self):
        for host in ('2130706433', '0x7f.1', '127.1', '017700000001', '1.2.3', '300.1.1.1', '0x7f000001'):
            with self.subTest(host=host):
                self.assertCode('unsafe_endpoint', self.make, f'https://{host}/v1')

    def test_the_parts_of_a_good_endpoint(self):
        endpoint = api_adapter.parse_endpoint('https://api.example.com/openai/v1/')
        self.assertEqual((endpoint.scheme, endpoint.host, endpoint.port, endpoint.path, endpoint.loopback), ('https', 'api.example.com', 443, '/openai/v1', False))
        endpoint = api_adapter.parse_endpoint('http://LocalHost')
        self.assertEqual((endpoint.scheme, endpoint.host, endpoint.port, endpoint.path, endpoint.loopback), ('http', 'localhost', 80, '', True))
        self.assertEqual(api_adapter.parse_endpoint('https://[2606:4700::1111]:8443').port, 8443)

    def test_a_host_name_is_at_most_253_characters(self):
        label = 'a' * 63
        longest = '.'.join([label, label, label, 'a' * 61])
        self.assertEqual(len(longest), 253)
        self.make(f'https://{longest}/v1')
        self.assertCode('unsafe_endpoint', self.make, f'https://{longest}a/v1')

    def test_a_url_that_is_not_text_is_invalid(self):
        for url in (None, 5, b'https://example.com', ['https://example.com'], ''):
            with self.subTest(url=url):
                self.assertCode('invalid_request', self.make, url)

    def test_the_key_must_be_an_api_key(self):
        for key in (SECRET, b'bytes', 5):
            error = self.assertCode('invalid_request', ApiAdapter, 'https://example.com/v1', 'm', key)
            self.assertEqual(str(error), 'Send the API key as an ApiKey.')
        self.assertNotIn(SECRET, str(error))

    def test_a_missing_key_is_allowed_for_loopback_only(self):
        self.make('http://127.0.0.1:1/v1', key=None)
        self.make('https://localhost/v1', key=None)
        self.assertCode('invalid_request', self.make, 'https://example.com/v1', key=None)

    def test_model_names_are_checked(self):
        for model in ('gpt-4o', 'meta-llama/Llama-3.1-8B-Instruct', 'qwen2.5:7b', 'accounts/x/models/y', 'a' * 200):
            ApiAdapter('https://example.com/v1', model, ApiKey(SECRET))
        for model in ('', ' ', 'two words', 'new\nline', 'quote"', 'a' * 201, None, 5, 'café', '{x}'):
            with self.subTest(model=model):
                self.assertCode('invalid_request', ApiAdapter, 'https://example.com/v1', model, ApiKey(SECRET))

    def test_options_are_checked(self):
        bad = [{'max_tokens_field': 'max_output_tokens'}, {'max_tokens_field': None}, {'connect_timeout': 0}, {'connect_timeout': -1},
               {'read_timeout': True}, {'total_timeout': float('inf')}, {'total_timeout': float('nan')}, {'total_timeout': '5'},
               {'total_timeout': 601}, {'max_response_bytes': 0}, {'max_response_bytes': True}, {'max_response_bytes': 1.5},
               {'max_response_bytes': api_adapter.MAX_RESPONSE_BYTES_LIMIT + 1}, {'ssl_context': 'ctx'}, {'resolver': 'dns'}]
        for options in bad:
            with self.subTest(options=options):
                self.assertCode('invalid_request', ApiAdapter, 'https://example.com/v1', 'm', ApiKey(SECRET), **options)
        ApiAdapter('https://example.com/v1', 'm', ApiKey(SECRET), max_tokens_field='max_completion_tokens', connect_timeout=1, read_timeout=2.5,
                   total_timeout=600, max_response_bytes=api_adapter.MAX_RESPONSE_BYTES_LIMIT, ssl_context=ssl.create_default_context())

    def test_the_default_limits(self):
        self.assertEqual(api_adapter.MAX_RESPONSE_BYTES, 1_000_000)
        self.assertEqual(api_adapter.LOOPBACK_HOSTS, ('127.0.0.1', '::1', 'localhost'))

    def test_the_adapter_never_shows_its_key(self):
        adapter = self.make('https://example.com/v1')
        shown = repr(adapter) + str(vars(adapter))
        self.assertNotIn(SECRET, shown)
        self.assertNotIn(SECRET[:14], shown)


class AddressTests(Case):
    """Which addresses a host may resolve to. No test connects: a refused address stops the call before a socket opens."""

    def adapter_for(self, url, table=None, key=SECRET, **options):
        def resolver(host, port, *args, **kwargs):
            if table is None or host not in table:
                raise socket.gaierror(socket.EAI_NONAME, 'no such host')
            return [(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port)) for address in table[host]]
        return ApiAdapter(url, 'm', None if key is None else ApiKey(key), resolver=resolver, **options)

    def refused_before_connecting(self, adapter, code='unsafe_endpoint'):
        budget = TokenBudget(1000)
        with mock.patch('socket.create_connection', side_effect=AssertionError('a socket was opened')) as opened:
            self.assertCode(code, adapter.complete, call(budget))
        opened.assert_not_called()
        self.assertEqual(budget.spent, 0)
        self.assertEqual(budget.remaining, 1000)

    def test_literal_private_and_special_addresses_are_refused(self):
        hosts = ['10.0.0.1', '10.255.255.255', '172.16.0.1', '172.31.255.255', '192.168.1.1', '169.254.169.254', '169.254.0.1', '100.64.0.1',
                 '0.0.0.0', '224.0.0.1', '239.255.255.255', '240.0.0.1', '255.255.255.255', '198.18.0.1', '192.0.2.1', '198.51.100.1', '203.0.113.1',
                 '[fd00::1]', '[fe80::1]', '[fc00::1]', '[::]', '[ff02::1]', '[2001:db8::1]', '[::ffff:10.0.0.1]', '[::ffff:169.254.169.254]',
                 '[::ffff:127.0.0.1]', '[64:ff9b::a00:1]', '[64:ff9b::a9fe:a9fe]', '[2002:a00:1::1]', '[2002:a9fe:a9fe::1]', '[::10.0.0.1]', '[fd00:ec2::254]']
        for host in hosts:
            with self.subTest(host=host):
                self.refused_before_connecting(ApiAdapter(f'https://{host}/v1', 'm', ApiKey(SECRET)))

    def test_a_name_that_resolves_to_a_private_address_is_refused(self):
        for address in ('10.0.0.5', '192.168.0.10', '169.254.169.254', '127.0.0.1', '::1', 'fd00::5', '::ffff:10.0.0.5', '0.0.0.0'):
            with self.subTest(address=address):
                self.refused_before_connecting(self.adapter_for('https://api.example.com/v1', {'api.example.com': [address]}))

    def test_one_private_address_among_public_ones_refuses_the_name(self):
        adapter = self.adapter_for('https://api.example.com/v1', {'api.example.com': ['93.184.216.34', '10.0.0.5']})
        self.refused_before_connecting(adapter)
        adapter = self.adapter_for('https://api.example.com/v1', {'api.example.com': ['10.0.0.5', '93.184.216.34']})
        self.refused_before_connecting(adapter)

    def test_a_garbage_address_is_refused(self):
        for address in ('not-an-ip', '', '1.2.3', '999.1.1.1'):
            with self.subTest(address=address):
                self.refused_before_connecting(self.adapter_for('https://api.example.com/v1', {'api.example.com': [address]}))

    def test_public_addresses_pass(self):
        adapter = self.adapter_for('https://api.example.com/v1', {'api.example.com': ['93.184.216.34', '2606:2800:220:1:248:1893:25c8:1946', '8.8.8.8']})
        self.assertEqual(adapter._addresses(), ['93.184.216.34', '2606:2800:220:1:248:1893:25c8:1946', '8.8.8.8'])

    def test_repeated_addresses_are_listed_once(self):
        adapter = self.adapter_for('https://api.example.com/v1', {'api.example.com': ['8.8.8.8', '8.8.8.8', '1.1.1.1']})
        self.assertEqual(adapter._addresses(), ['8.8.8.8', '1.1.1.1'])

    def test_a_public_literal_address_needs_no_lookup(self):
        def resolver(*args, **kwargs):
            raise AssertionError('the resolver was used')
        self.assertEqual(ApiAdapter('https://8.8.8.8/v1', 'm', ApiKey(SECRET), resolver=resolver)._addresses(), ['8.8.8.8'])

    def test_localhost_must_resolve_to_loopback_only(self):
        good = self.adapter_for('http://localhost:1234/v1', {'localhost': ['::1', '127.0.0.1']})
        self.assertEqual(good._addresses(), ['::1', '127.0.0.1'])
        for address in ('93.184.216.34', '10.0.0.1', '192.168.1.5'):
            with self.subTest(address=address):
                self.refused_before_connecting(self.adapter_for('http://localhost:1234/v1', {'localhost': ['127.0.0.1', address]}))

    def test_loopback_literals_are_accepted_for_loopback_hosts(self):
        self.assertEqual(ApiAdapter('http://127.0.0.1:1/v1', 'm', None)._addresses(), ['127.0.0.1'])
        self.assertEqual(ApiAdapter('http://[::1]:1/v1', 'm', None)._addresses(), ['::1'])

    def test_a_lookup_answer_of_the_wrong_shape_is_unreachable(self):
        for answer in ([('bad',)], [()], [None], 5, None, [(1, 2, 3, 4)]):
            with self.subTest(answer=answer):
                adapter = ApiAdapter('https://api.example.com/v1', 'm', ApiKey(SECRET), resolver=lambda *a, answer=answer, **k: answer)
                self.assertCode('endpoint_unreachable', adapter.complete, call())

    def test_a_failed_lookup_is_unreachable_and_retryable(self):
        adapter = self.adapter_for('https://nowhere.example.com/v1', {})
        self.assertCode('endpoint_unreachable', adapter.complete, call(), retryable=True)

    def test_an_empty_lookup_is_unreachable(self):
        adapter = ApiAdapter('https://api.example.com/v1', 'm', ApiKey(SECRET), resolver=lambda *a, **k: [])
        self.assertCode('endpoint_unreachable', adapter.complete, call())

    def test_a_lookup_that_raises_something_odd_is_unreachable(self):
        def resolver(*args, **kwargs):
            raise OSError(f'resolver blew up {SECRET}')
        adapter = ApiAdapter('https://api.example.com/v1', 'm', ApiKey(SECRET), resolver=resolver)
        self.assertCode('endpoint_unreachable', adapter.complete, call())

    def test_a_lookup_that_hangs_times_out(self):
        release = threading.Event()
        self.addCleanup(release.set)
        adapter = ApiAdapter('https://api.example.com/v1', 'm', ApiKey(SECRET), connect_timeout=0.3, resolver=lambda *a, **k: release.wait(10) and [])
        started = time.monotonic()
        self.assertCode('timeout', adapter.complete, call(), retryable=True)
        self.assertLess(time.monotonic() - started, 3)

    def test_the_address_is_checked_again_on_every_call(self):
        answers = [['93.184.216.34'], ['10.0.0.5']]
        seen = []

        def resolver(host, port, *args, **kwargs):
            seen.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (answers[min(len(seen), 2) - 1][0], port))]
        adapter = ApiAdapter('https://api.example.com/v1', 'm', ApiKey(SECRET), resolver=resolver)
        self.assertEqual(adapter._addresses(), ['93.184.216.34'])
        self.refused_before_connecting(adapter)
        self.assertEqual(len(seen), 2)


class AddressFunctionTests(unittest.TestCase):
    def ip(self, text):
        import ipaddress
        return ipaddress.ip_address(text)

    def test_the_address_hidden_inside_an_ipv6_address_is_found(self):
        hidden = {'::ffff:10.0.0.1': '10.0.0.1', '2002:a00:1::1': '10.0.0.1', '64:ff9b::a00:1': '10.0.0.1', '64:ff9b::808:808': '8.8.8.8',
                  '::ffff:8.8.8.8': '8.8.8.8'}
        for text, expected in hidden.items():
            with self.subTest(text=text):
                self.assertEqual(str(api_adapter._embedded(self.ip(text))), expected)
        for text in ('8.8.8.8', '10.0.0.1', '2606:4700::1111', 'fd00::1', '::1', '64:ff9b:1::1'):
            with self.subTest(text=text):
                self.assertIsNone(api_adapter._embedded(self.ip(text)))

    def test_a_hidden_address_decides(self):
        self.assertTrue(api_adapter.address_allowed(self.ip('::ffff:8.8.8.8'), False))
        self.assertFalse(api_adapter.address_allowed(self.ip('::ffff:10.0.0.1'), False))
        self.assertFalse(api_adapter.address_allowed(self.ip('::ffff:127.0.0.1'), True))
        self.assertFalse(api_adapter.address_allowed(self.ip('::ffff:127.0.0.1'), False))
        self.assertFalse(api_adapter.address_allowed(self.ip('2002:7f00:1::'), True))
        self.assertFalse(api_adapter.address_allowed(self.ip('2002:7f00:1::'), False))
        self.assertFalse(api_adapter.address_allowed(self.ip('64:ff9b::7f00:1'), True))

    def test_an_address_with_a_scope_is_refused(self):
        self.assertTrue(api_adapter.address_allowed(self.ip('2606:4700::1111'), False))
        self.assertFalse(api_adapter.address_allowed(self.ip('2606:4700::1111%en0'), False))
        self.assertFalse(api_adapter.address_allowed(self.ip('::1%en0'), True))

    def test_loopback_hosts_accept_loopback_only(self):
        self.assertTrue(api_adapter.address_allowed(self.ip('127.0.0.1'), True))
        self.assertTrue(api_adapter.address_allowed(self.ip('127.8.8.8'), True))
        self.assertTrue(api_adapter.address_allowed(self.ip('::1'), True))
        self.assertFalse(api_adapter.address_allowed(self.ip('8.8.8.8'), True))
        self.assertFalse(api_adapter.address_allowed(self.ip('10.0.0.1'), True))

    def test_each_flag_alone_refuses_an_address(self):
        # The `ipaddress` tables differ between Python versions. Each flag must refuse on its own, even when `is_global` says yes.
        class Stub:
            version = 4
            is_global = True
            is_private = is_loopback = is_link_local = is_multicast = is_reserved = is_unspecified = False

        self.assertTrue(api_adapter.address_allowed(Stub(), False))
        for flag in ('is_private', 'is_loopback', 'is_link_local', 'is_multicast', 'is_reserved', 'is_unspecified'):
            with self.subTest(flag=flag):
                stub = Stub()
                setattr(stub, flag, True)
                self.assertFalse(api_adapter.address_allowed(stub, False))
        not_global = Stub()
        not_global.is_global = False
        self.assertFalse(api_adapter.address_allowed(not_global, False))


class RequestTests(Case):
    def test_the_request_and_the_reply(self):
        provider = self.provider()
        budget = TokenBudget(1000)
        result = self.adapter(provider).complete(CompletionRequest([Message('system', 'Be brief.'), Message('user', 'Hi')], 100, budget))
        self.assertEqual(result.text, 'Hello from the model.')
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), (12, 7))
        self.assertEqual(result.stop_reason, 'end')
        self.assertTrue(result.usage_reported)
        self.assertEqual(budget.spent, 19)
        self.assertEqual(len(provider.requests), 1)
        seen = provider.requests[0]
        self.assertEqual((seen['method'], seen['path']), ('POST', '/v1/chat/completions'))
        self.assertEqual(seen['headers']['authorization'], 'Bearer ' + SECRET)
        self.assertEqual(seen['headers']['content-type'], 'application/json')
        self.assertEqual(seen['headers']['accept'], 'application/json')
        self.assertEqual(seen['headers']['accept-encoding'], 'identity')
        self.assertEqual(seen['headers']['connection'], 'close')
        self.assertEqual(seen['headers']['host'], f'127.0.0.1:{provider.port}')
        self.assertTrue(seen['headers']['user-agent'].startswith('schola-api-adapter'))
        self.assertEqual(json.loads(seen['body']), {'model': 'test-model', 'stream': False, 'max_tokens': 100,
                                                    'messages': [{'role': 'system', 'content': 'Be brief.'}, {'role': 'user', 'content': 'Hi'}]})

    def test_the_path_is_built_from_the_base_url(self):
        for base, expected in (('/v1', '/v1/chat/completions'), ('/v1/', '/v1/chat/completions'), ('', '/chat/completions'), ('/', '/chat/completions'),
                               ('/openai/v1', '/openai/v1/chat/completions')):
            provider = self.provider()
            self.adapter(provider, path=base).complete(call())
            self.assertEqual(provider.requests[0]['path'], expected)

    def test_the_token_limit_field_can_be_chosen(self):
        provider = self.provider()
        self.adapter(provider, max_tokens_field='max_completion_tokens').complete(call(max_output_tokens=321))
        body = json.loads(provider.requests[0]['body'])
        self.assertEqual(body['max_completion_tokens'], 321)
        self.assertNotIn('max_tokens', body)

    def test_a_loopback_endpoint_can_run_without_a_key(self):
        provider = self.provider()
        self.adapter(provider, key=None).complete(call())
        self.assertNotIn('authorization', provider.requests[0]['headers'])

    def test_unicode_and_odd_characters_survive(self):
        text = "café 你好 \U0001F600 O'Brien \"quoted\" \\ back; DROP TABLE x;--   </script>"
        provider = self.provider(reply_json(ok_payload(text='Reply: ' + text)))
        result = self.adapter(provider).complete(call(text=text))
        self.assertEqual(json.loads(provider.requests[0]['body'])['messages'][0]['content'], text)
        self.assertEqual(result.text, 'Reply: ' + text)

    def test_a_large_request_and_a_large_reply(self):
        provider = self.provider(reply_json(ok_payload(text='r' * 800_000)))
        messages = [Message('user', 'm' * 8000) for _ in range(50)]
        result = self.adapter(provider).complete(CompletionRequest(messages, 100, TokenBudget(10 ** 6)))
        self.assertEqual(len(result.text), 800_000)
        self.assertEqual(len(json.loads(provider.requests[0]['body'])['messages']), 50)

    def test_localhost_by_name(self):
        provider = self.provider()
        try:
            names = {item[4][0] for item in socket.getaddrinfo('localhost', provider.port, type=socket.SOCK_STREAM)}
        except OSError:
            names = set()
        if '127.0.0.1' not in names:
            self.skipTest('localhost does not resolve to 127.0.0.1 here')
        ApiAdapter(f'http://localhost:{provider.port}/v1', 'm', ApiKey(SECRET)).complete(call())
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(provider.requests[0]['headers']['host'], f'localhost:{provider.port}')

    def test_the_second_address_is_tried_when_the_first_refuses(self):
        provider = self.provider()
        dead = free_port()
        calls = []
        real = socket.create_connection

        def connector(address, timeout=None, *args, **kwargs):
            calls.append(address)
            return real(('127.0.0.1', dead if len(calls) == 1 else provider.port), timeout)
        adapter = ApiAdapter(f'http://localhost:{provider.port}/v1', 'm', ApiKey(SECRET), resolver=lambda host, port, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port)), (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', port))])
        with mock.patch('socket.create_connection', side_effect=connector):
            adapter.complete(call())
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(provider.requests), 1)

    def test_the_connection_goes_to_the_checked_address_not_the_name(self):
        provider = self.provider()
        adapter = ApiAdapter(f'http://localhost:{provider.port}/v1', 'm', ApiKey(SECRET), resolver=lambda host, port, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))])
        with mock.patch('socket.create_connection', wraps=socket.create_connection) as opened:
            adapter.complete(call())
        self.assertEqual([item.args[0] for item in opened.call_args_list], [('127.0.0.1', provider.port)])

    def test_the_connection_is_closed_after_every_call(self):
        provider = self.provider()
        failing = self.provider(reply_raw(b'', status=500))
        closed = []
        original = api_adapter._PinnedConnection.close

        def spy(connection):
            closed.append(connection)
            return original(connection)
        with mock.patch.object(api_adapter._PinnedConnection, 'close', spy):
            self.adapter(provider).complete(call())
            self.assertCode('endpoint_unreachable', self.adapter(failing).complete, call())
        self.assertEqual(len(closed), 2)
        self.assertTrue(all(connection.sock is None for connection in closed))

    def test_proxy_settings_in_the_environment_are_ignored(self):
        provider = self.provider()
        dead = f'http://127.0.0.1:{free_port()}'
        with mock.patch.dict(os.environ, {'HTTP_PROXY': dead, 'http_proxy': dead, 'HTTPS_PROXY': dead, 'https_proxy': dead, 'ALL_PROXY': dead}):
            self.adapter(provider).complete(call())
        self.assertEqual(len(provider.requests), 1)

    def test_each_call_uses_its_own_connection_and_the_budget_adds_up(self):
        provider = self.provider()
        budget = TokenBudget(1000)
        adapter = self.adapter(provider)
        for _ in range(3):
            adapter.complete(call(budget))
        self.assertEqual(budget.spent, 57)
        self.assertEqual(len(provider.requests), 3)


class ReplyShapeTests(Case):
    def run_reply(self, payload):
        provider = self.provider(reply_json(payload))
        return self.adapter(provider).complete(call())

    def test_finish_reasons_map_to_stop_reasons(self):
        for finish, expected in (('stop', 'end'), ('length', 'length'), ('content_filter', 'content_filter'), ('tool_calls', 'other'),
                                 ('function_call', 'other'), (None, 'other'), ('something_new', 'other')):
            with self.subTest(finish=finish):
                self.assertEqual(self.run_reply(ok_payload(finish=finish)).stop_reason, expected)

    def test_a_missing_finish_reason_is_other(self):
        payload = ok_payload()
        del payload['choices'][0]['finish_reason']
        self.assertEqual(self.run_reply(payload).stop_reason, 'other')

    def test_a_finish_reason_that_is_not_text_is_other(self):
        self.assertEqual(self.run_reply(ok_payload(finish=['stop'])).stop_reason, 'other')

    def test_extra_fields_are_ignored(self):
        payload = ok_payload(system_fingerprint='fp', created=1)
        payload['usage']['prompt_tokens_details'] = {'cached_tokens': 3}
        payload['choices'].append({'index': 1, 'message': {'role': 'assistant', 'content': 'second'}, 'finish_reason': 'stop'})
        result = self.run_reply(payload)
        self.assertEqual(result.text, 'Hello from the model.')
        self.assertEqual(result.usage.total, 19)

    def test_an_empty_reply_text_is_allowed(self):
        self.assertEqual(self.run_reply(ok_payload(text='', finish='length')).text, '')

    def test_zero_usage_is_reported_usage(self):
        result = self.run_reply(ok_payload(usage={'prompt_tokens': 0, 'completion_tokens': 0}))
        self.assertTrue(result.usage_reported)
        self.assertEqual(result.tokens_charged, 0)

    def test_missing_or_unusable_usage_charges_the_maximum_and_says_so(self):
        cases = ['omit', None, {}, [], 'lots', {'prompt_tokens': 5}, {'completion_tokens': 5}, {'total_tokens': 19},
                 {'prompt_tokens': -1, 'completion_tokens': 5}, {'prompt_tokens': 1, 'completion_tokens': -5}, {'prompt_tokens': '1', 'completion_tokens': 2},
                 {'prompt_tokens': True, 'completion_tokens': 2}, {'prompt_tokens': 1.5, 'completion_tokens': 2}, {'prompt_tokens': None, 'completion_tokens': 2},
                 {'prompt_tokens': 10 ** 13, 'completion_tokens': 1}]
        for usage in cases:
            with self.subTest(usage=usage):
                payload = ok_payload(usage=usage if usage is not None else {})
                if usage is None:
                    payload['usage'] = None
                provider = self.provider(reply_json(payload))
                budget = TokenBudget(5000)
                result = self.adapter(provider).complete(call(budget, max_output_tokens=400))
                self.assertFalse(result.usage_reported)
                self.assertIsNone(result.usage)
                self.assertEqual(result.tokens_charged, 400)
                self.assertEqual(budget.spent, 400)
                self.assertIn('no usage', result.notice)

    def test_bad_shapes_are_bad_responses(self):
        good = ok_payload()
        shapes = {
            'array': [good], 'null': None, 'string': 'ok', 'number': 5, 'empty object': {},
            'no choices': {'usage': good['usage']}, 'choices null': {'choices': None}, 'choices empty': {'choices': []},
            'choices object': {'choices': {'message': {'content': 'x'}}}, 'choice string': {'choices': ['x']},
            'choice no message': {'choices': [{'finish_reason': 'stop'}]}, 'message string': {'choices': [{'message': 'x'}]},
            'content missing': {'choices': [{'message': {'role': 'assistant'}}]}, 'content null': {'choices': [{'message': {'content': None}}]},
            'content number': {'choices': [{'message': {'content': 5}}]}, 'content parts': {'choices': [{'message': {'content': [{'type': 'text', 'text': 'x'}]}}]},
            'content object': {'choices': [{'message': {'content': {'a': 1}}}]},
        }
        for name, shape in shapes.items():
            with self.subTest(shape=name):
                provider = self.provider(reply_json(shape))
                budget = TokenBudget(1000)
                error = self.assertCode('bad_response', self.adapter(provider).complete, call(budget), retryable=False)
                self.assertEqual(str(error), EXPECTED_FORMAT)
                self.assertEqual(budget.spent, 0)

    def test_bad_bodies_are_bad_responses(self):
        bodies = {
            'html': b'<html>Bad gateway</html>', 'empty': b'', 'text': b'OK', 'truncated json': b'{"choices": [',
            'nan': b'{"choices":[{"message":{"content":"x"}}],"usage":{"prompt_tokens":NaN,"completion_tokens":1}}',
            'invalid utf8': b'{"choices":[{"message":{"content":"\xff\xfe"}}]}', 'bom': b'\xef\xbb\xbf' + json.dumps(ok_payload()).encode(),
            'deep nesting': b'[' * 500000 + b']' * 500000, 'two documents': json.dumps(ok_payload()).encode() * 2,
            'huge integer': b'{"choices":[{"message":{"content":"x"}}],"usage":{"prompt_tokens":' + b'9' * 5000 + b',"completion_tokens":1}}',
            'long integer': b'{"choices":[{"message":{"content":"x"}}],"usage":{"prompt_tokens":' + b'9' * 100 + b',"completion_tokens":1}}',
            'lone surrogate escape': b'{"choices":[{"message":{"content":"a\\ud800b"}}]}',
        }
        for name, body in bodies.items():
            with self.subTest(body=name):
                provider = self.provider(reply_raw(body))
                error = self.assertCode('bad_response', self.adapter(provider).complete, call(), retryable=False)
                self.assertEqual(str(error), EXPECTED_FORMAT)

    def test_an_integer_of_18_digits_is_read_and_19_is_refused(self):
        body = lambda digits: b'{"choices":[{"message":{"content":"x"}}],"usage":{"prompt_tokens":' + b'9' * digits + b',"completion_tokens":1}}'
        provider = self.provider(reply_raw(body(18)))
        self.assertFalse(self.adapter(provider).complete(call()).usage_reported)
        provider = self.provider(reply_raw(body(19)))
        self.assertCode('bad_response', self.adapter(provider).complete, call())

    def test_the_content_type_must_be_json(self):
        body = json.dumps(ok_payload()).encode()
        for content_type in ('text/html', 'text/plain', 'text/event-stream', 'application/xml', None, 'application/jsonp', 'application/json-seq-x', 'text/json', 'application/x-json',
                             'application/json; charset=latin-1', 'application/json; charset=utf-16'):
            with self.subTest(content_type=content_type):
                provider = self.provider(reply_raw(body, content_type=content_type))
                self.assertCode('bad_response', self.adapter(provider).complete, call())
        for content_type in ('application/json', 'application/json; charset=utf-8', 'APPLICATION/JSON;charset=UTF-8', 'application/vnd.api+json'):
            with self.subTest(content_type=content_type):
                provider = self.provider(reply_raw(body, content_type=content_type))
                self.assertEqual(self.adapter(provider).complete(call()).text, 'Hello from the model.')

    def test_a_compressed_reply_is_refused(self):
        body = json.dumps(ok_payload()).encode()
        for encoding in ('gzip', 'deflate', 'br'):
            with self.subTest(encoding=encoding):
                provider = self.provider(reply_raw(body, headers={'Content-Encoding': encoding}))
                self.assertCode('bad_response', self.adapter(provider).complete, call())
        provider = self.provider(reply_raw(body, headers={'Content-Encoding': 'identity'}))
        self.assertEqual(self.adapter(provider).complete(call()).stop_reason, 'end')

    def test_the_key_is_removed_from_reply_text(self):
        provider = self.provider(reply_json(ok_payload(text=f'The key is {SECRET}.')))
        result = self.adapter(provider).complete(call())
        self.assertEqual(result.text, 'The key is [redacted].')


class StatusTests(Case):
    def test_each_status_maps_to_a_code(self):
        table = {401: ('auth_failed', False), 403: ('auth_failed', False), 429: ('rate_limited', True), 408: ('timeout', True), 504: ('timeout', True),
                 500: ('endpoint_unreachable', True), 502: ('endpoint_unreachable', True), 503: ('endpoint_unreachable', True), 599: ('endpoint_unreachable', True),
                 400: ('invalid_request', False), 404: ('invalid_request', False), 413: ('invalid_request', False), 422: ('invalid_request', False),
                 402: ('invalid_request', False), 499: ('invalid_request', False), 202: ('bad_response', False), 204: ('bad_response', False),
                 206: ('bad_response', False), 201: ('bad_response', False), 203: ('bad_response', False)}
        for status, (code, retryable) in table.items():
            with self.subTest(status=status):
                body = json.dumps(ok_payload(text=f'{BODY_SECRET} echo {SECRET}') if status < 300 else {'error': {'message': f'{BODY_SECRET} echo {SECRET}'}}).encode()
                provider = self.provider(reply_raw(body if status != 204 else b'', status=status))
                budget = TokenBudget(1000)
                self.assertCode(code, self.adapter(provider).complete, call(budget), retryable=retryable)
                self.assertEqual(budget.spent, 0)
                self.assertEqual(budget.remaining, 1000)

    def test_the_status_number_is_in_the_message_and_the_body_is_not(self):
        provider = self.provider(reply_raw(b'{"error":"' + BODY_SECRET.encode() + b'"}', status=404))
        error = self.assertCode('invalid_request', self.adapter(provider).complete, call())
        self.assertIn('404', str(error))

    def test_a_success_status_with_a_key_echo_in_an_error_body_is_clean(self):
        provider = self.provider(reply_raw(SECRET.encode(), status=401, content_type='text/plain'))
        self.assertCode('auth_failed', self.adapter(provider).complete, call())

    def test_an_error_body_is_never_read(self):
        provider = self.provider(lambda handler, body: send(handler, 500, b'x' * 5_000_000))
        started = time.monotonic()
        self.assertCode('endpoint_unreachable', self.adapter(provider).complete, call())
        self.assertLess(time.monotonic() - started, 3)


class RedirectTests(Case):
    def test_a_redirect_is_never_followed_and_the_key_stays_home(self):
        elsewhere = self.provider()
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                first = self.provider(lambda handler, body, s=status: send(handler, s, b'', headers={'Location': f'http://127.0.0.1:{elsewhere.port}/steal'}))
                budget = TokenBudget(1000)
                error = self.assertCode('unsafe_endpoint', self.adapter(first).complete, call(budget), retryable=False)
                self.assertNotIn('steal', str(error))
                self.assertEqual(len(first.requests), 1)
                self.assertEqual(elsewhere.requests, [])
                self.assertEqual(budget.spent, 0)
        self.assertEqual(elsewhere.requests, [])

    def test_a_redirect_to_the_same_host_is_refused_too(self):
        provider = self.provider(lambda handler, body: send(handler, 307, b'', headers={'Location': '/v1/other'}))
        self.assertCode('unsafe_endpoint', self.adapter(provider).complete, call())
        self.assertEqual(len(provider.requests), 1)

    def test_a_redirect_without_a_location_is_refused(self):
        provider = self.provider(lambda handler, body: send(handler, 302, b''))
        self.assertCode('unsafe_endpoint', self.adapter(provider).complete, call())

    def test_a_redirect_to_a_metadata_address_never_connects(self):
        provider = self.provider(lambda handler, body: send(handler, 302, b'', headers={'Location': 'http://169.254.169.254/latest/meta-data/'}))
        with mock.patch('socket.create_connection', wraps=socket.create_connection) as opened:
            self.assertCode('unsafe_endpoint', self.adapter(provider).complete, call())
        self.assertEqual([call_args.args[0] for call_args in opened.call_args_list], [('127.0.0.1', provider.port)])


class SizeAndTimeTests(Case):
    def padded(self, size):
        body = json.dumps(ok_payload()).encode()
        return body + b' ' * (size - len(body))

    def test_a_body_exactly_at_the_cap_is_accepted_and_one_byte_over_is_refused(self):
        for chunked in (False, True):
            with self.subTest(chunked=chunked):
                def responder(handler, request_body, size):
                    body = self.padded(size)
                    if chunked:
                        handler.send_response(200)
                        handler.send_header('Content-Type', 'application/json')
                        handler.send_header('Transfer-Encoding', 'chunked')
                        handler.end_headers()
                        for start in range(0, len(body), 700):
                            piece = body[start:start + 700]
                            handler.wfile.write(f'{len(piece):x}\r\n'.encode() + piece + b'\r\n')
                        handler.wfile.write(b'0\r\n\r\n')
                    else:
                        send(handler, 200, body)
                at_cap = self.provider(lambda h, b: responder(h, b, 2000))
                self.assertEqual(self.adapter(at_cap, max_response_bytes=2000).complete(call()).text, 'Hello from the model.')
                over = self.provider(lambda h, b: responder(h, b, 2001))
                self.assertCode('response_too_large', self.adapter(over, max_response_bytes=2000).complete, call(), retryable=False)

    def test_the_default_cap_is_one_megabyte(self):
        provider = self.provider(lambda h, b: send(h, 200, self.padded(1_000_001)))
        self.assertCode('response_too_large', self.adapter(provider).complete, call())
        provider = self.provider(lambda h, b: send(h, 200, self.padded(1_000_000)))
        self.assertEqual(self.adapter(provider).complete(call()).stop_reason, 'end')

    def test_a_declared_length_over_the_cap_is_refused_without_reading(self):
        def responder(handler, body):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', '999999999')
            handler.end_headers()
            handler.wfile.write(b'{')
            handler.wfile.flush()
            time.sleep(3)
        provider = self.provider(responder)
        started = time.monotonic()
        self.assertCode('response_too_large', self.adapter(provider).complete, call())
        self.assertLess(time.monotonic() - started, 2)

    def test_a_body_with_no_length_that_never_ends_is_cut_at_the_cap(self):
        def responder(handler, body):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Connection', 'close')
            handler.end_headers()
            try:
                for _ in range(5000):
                    handler.wfile.write(b' ' * 4096)
            except OSError:
                pass
            handler.close_connection = True
        provider = self.provider(responder)
        self.assertCode('response_too_large', self.adapter(provider, max_response_bytes=100_000).complete, call())

    def test_a_slow_body_is_cut_by_the_total_time_limit(self):
        def responder(handler, body):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', '1000')
            handler.end_headers()
            try:
                for _ in range(100):
                    handler.wfile.write(b' ')
                    handler.wfile.flush()
                    time.sleep(0.15)
            except OSError:
                pass
        provider = self.provider(responder)
        started = time.monotonic()
        self.assertCode('timeout', self.adapter(provider, read_timeout=1.0, total_timeout=0.6).complete, call(), retryable=True)
        self.assertLess(time.monotonic() - started, 3)

    def test_slow_headers_are_cut_by_the_total_time_limit(self):
        def responder(handler, body):
            handler.wfile.write(b'HTTP/1.1 200 OK\r\n')
            try:
                for index in range(100):
                    handler.wfile.write(f'X-Slow-{index}: y\r\n'.encode())
                    handler.wfile.flush()
                    time.sleep(0.15)
            except OSError:
                pass
        provider = self.provider(responder)
        started = time.monotonic()
        self.assertCode('timeout', self.adapter(provider, read_timeout=1.0, total_timeout=0.6).complete, call())
        self.assertLess(time.monotonic() - started, 3)

    def test_a_silent_server_hits_the_read_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)
        provider = self.provider(lambda handler, body: release.wait(5))
        started = time.monotonic()
        self.assertCode('timeout', self.adapter(provider, read_timeout=0.3, total_timeout=30).complete, call(), retryable=True)
        self.assertLess(time.monotonic() - started, 3)

    def test_a_server_that_stalls_in_the_body_hits_the_read_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def responder(handler, body):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', '500')
            handler.end_headers()
            handler.wfile.write(b'{')
            handler.wfile.flush()
            release.wait(5)
        provider = self.provider(responder)
        self.assertCode('timeout', self.adapter(provider, read_timeout=0.3, total_timeout=30).complete, call())

    def test_a_connection_that_never_completes_hits_the_connect_timeout(self):
        def connector(address, timeout=None, *args, **kwargs):
            self.assertEqual(timeout, 0.25)
            raise socket.timeout('timed out')
        adapter = ApiAdapter('http://127.0.0.1:9/v1', 'm', ApiKey(SECRET), connect_timeout=0.25)
        with mock.patch('socket.create_connection', side_effect=connector):
            self.assertCode('timeout', adapter.complete, call(), retryable=True)


class NetworkFailureTests(Case):
    @staticmethod
    def read_request(connection):
        """Read the whole request, so closing the connection later sends no reset."""
        data = b''
        while b'\r\n\r\n' not in data:
            data += connection.recv(65536)
        head, _, rest = data.partition(b'\r\n\r\n')
        length = 0
        for line in head.split(b'\r\n')[1:]:
            name, _, value = line.partition(b':')
            if name.strip().lower() == b'content-length':
                length = int(value)
        while len(rest) < length:
            rest += connection.recv(65536)

    def raw_server(self, handler_function):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(5)
        self.addCleanup(listener.close)

        def serve():
            while True:
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                with contextlib.suppress(OSError):
                    connection.settimeout(2)
                    self.read_request(connection)
                    handler_function(connection)
                with contextlib.suppress(OSError):
                    connection.close()
        threading.Thread(target=serve, daemon=True).start()
        return f'http://127.0.0.1:{listener.getsockname()[1]}/v1'

    def adapter_at(self, url, **options):
        return ApiAdapter(url, 'm', ApiKey(SECRET), **options)

    def test_a_refused_connection_is_unreachable_and_retryable(self):
        self.assertCode('endpoint_unreachable', self.adapter_at(f'http://127.0.0.1:{free_port()}/v1').complete, call(), retryable=True)

    def test_a_server_that_hangs_up_is_unreachable(self):
        self.assertCode('endpoint_unreachable', self.adapter_at(self.raw_server(lambda connection: None)).complete, call(), retryable=True)

    def test_a_connection_reset_is_unreachable(self):
        def reset(connection):
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b'\x01\x00\x00\x00\x00\x00\x00\x00')
        self.assertCode('endpoint_unreachable', self.adapter_at(self.raw_server(reset)).complete, call())

    def test_a_garbage_reply_is_a_bad_response(self):
        for garbage in (b'not http at all\r\n\r\n', b'HTTP/9.9 ???\r\n\r\n', b'\x00\x01\x02\x03\r\n\r\n', b'HTTP/1.1 abc OK\r\n\r\n'):
            with self.subTest(garbage=garbage):
                error = self.assertCode('bad_response', self.adapter_at(self.raw_server(lambda connection, g=garbage: connection.sendall(g))).complete, call())
                self.assertEqual(str(error), EXPECTED_FORMAT)

    def test_a_body_cut_short_is_a_bad_response(self):
        def short(connection):
            connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"choices"')
        self.assertCode('bad_response', self.adapter_at(self.raw_server(short)).complete, call())

    def test_a_complete_json_body_shorter_than_its_declared_length_is_a_bad_response(self):
        body = json.dumps(ok_payload()).encode()

        def short(connection):
            connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n' % (len(body) + 500) + body)
        error = self.assertCode('bad_response', self.adapter_at(self.raw_server(short)).complete, call())
        self.assertIn('ended early', str(error))

    def test_a_chunked_body_cut_short_is_a_bad_response(self):
        def short(connection):
            connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n20\r\n{"choices"')
        self.assertCode('bad_response', self.adapter_at(self.raw_server(short)).complete, call())

    def test_a_giant_header_block_is_refused(self):
        def flood(connection):
            connection.sendall(b'HTTP/1.1 200 OK\r\n' + b'X-Pad: ' + b'a' * 70000 + b'\r\n\r\n')
        self.assertCode('bad_response', self.adapter_at(self.raw_server(flood)).complete, call())

    def test_too_many_headers_are_refused(self):
        def flood(connection):
            connection.sendall(b'HTTP/1.1 200 OK\r\n' + b''.join(b'X-%d: a\r\n' % index for index in range(500)) + b'\r\n')
        self.assertCode('bad_response', self.adapter_at(self.raw_server(flood)).complete, call())

    def test_a_network_error_message_never_holds_the_os_text(self):
        error = self.assertCode('endpoint_unreachable', self.adapter_at(f'http://127.0.0.1:{free_port()}/v1').complete, call())
        for piece in ('Errno', 'refused', '127.0.0.1', 'Connection'):
            self.assertNotIn(piece, str(error))

    def test_an_unexpected_failure_inside_the_transport_is_clean(self):
        adapter = self.adapter_at('http://127.0.0.1:9/v1')
        with mock.patch('api_adapter._PinnedConnection.request', side_effect=RuntimeError(f'boom {SECRET}')):
            error = self.assertCode('bad_response', adapter.complete, call())
        self.assertIn('unexpected', str(error))

    def test_a_value_error_inside_the_transport_is_a_bad_reply(self):
        adapter = self.adapter_at('http://127.0.0.1:9/v1')
        with mock.patch('api_adapter._PinnedConnection.request', side_effect=ValueError(f'bad {SECRET}')):
            error = self.assertCode('bad_response', adapter.complete, call())
        self.assertEqual(str(error), EXPECTED_FORMAT)

    def test_an_http_protocol_error_is_a_bad_reply(self):
        import http.client
        adapter = self.adapter_at('http://127.0.0.1:9/v1')
        with mock.patch('api_adapter._PinnedConnection.request', side_effect=http.client.HTTPException(f'bad {SECRET}')):
            error = self.assertCode('bad_response', adapter.complete, call())
        self.assertEqual(str(error), EXPECTED_FORMAT)

    def test_a_dropped_connection_is_unreachable_not_a_bad_reply(self):
        import http.client
        adapter = self.adapter_at('http://127.0.0.1:9/v1')
        with mock.patch('api_adapter._PinnedConnection.request', side_effect=http.client.RemoteDisconnected('gone')):
            error = self.assertCode('endpoint_unreachable', adapter.complete, call(), retryable=True)
        self.assertNotIn('format', str(error))


class BudgetBeforeIoTests(Case):
    def test_a_spent_budget_refuses_before_any_network_work(self):
        provider = self.provider()
        budget = TokenBudget(19)
        adapter = self.adapter(provider)
        adapter.complete(call(budget))
        self.assertTrue(budget.exhausted)
        looked_up = []
        adapter = ApiAdapter(provider.url(), 'm', ApiKey(SECRET), resolver=lambda *a, **k: looked_up.append(1))
        with mock.patch('socket.create_connection', side_effect=AssertionError('a socket was opened')) as opened:
            error = self.assertCode('token_cap_reached', adapter.complete, call(budget), retryable=False)
        opened.assert_not_called()
        self.assertEqual(looked_up, [])
        self.assertEqual(len(provider.requests), 1)
        self.assertNotIn(SECRET, str(error))

    def test_missing_usage_spends_the_maximum_and_closes_the_budget(self):
        provider = self.provider(reply_json(ok_payload(usage='omit')))
        budget = TokenBudget(100)
        adapter = self.adapter(provider)
        result = adapter.complete(call(budget, max_output_tokens=100))
        self.assertFalse(result.usage_reported)
        self.assertEqual(budget.spent, 100)
        self.assertCode('token_cap_reached', adapter.complete, call(budget))
        self.assertEqual(len(provider.requests), 1)

    def test_a_failed_call_does_not_use_up_the_budget(self):
        provider = self.provider(reply_raw(b'', status=503))
        budget = TokenBudget(100)
        adapter = self.adapter(provider)
        for _ in range(5):
            self.assertCode('endpoint_unreachable', adapter.complete, call(budget, max_output_tokens=100))
        self.assertEqual(budget.spent, 0)
        self.assertEqual(len(provider.requests), 5)

    def test_a_guarded_message_is_refused_before_any_network_work(self):
        provider = self.provider()
        adapter = self.adapter(provider)
        self.assertCode('invalid_request', adapter.complete, call(text='my key is ' + SECRET))
        self.assertCode('invalid_request', adapter.complete, call(text='read /etc/passwd'))
        self.assertEqual(provider.requests, [])

    def test_allowed_paths_reach_the_adapter(self):
        provider = self.provider()
        adapter = self.adapter(provider, allowed_paths=['/work/module'])
        adapter.complete(call(text='see /work/module/a.py'))
        self.assertCode('invalid_request', adapter.complete, call(text='see /work/other/a.py'))
        self.assertEqual(len(provider.requests), 1)


class KeyHandlingTests(Case):
    """The key reaches the provider and nothing else. Every error path is checked for it."""

    def scenarios(self):
        echo = json.dumps({'error': f'{SECRET} {BODY_SECRET}'}).encode()
        other = self.provider()
        yield 'bad json', self.provider(reply_raw(echo + b'{')), {}
        yield 'wrong shape', self.provider(reply_json({'echo': SECRET, 'x': BODY_SECRET})), {}
        for status in (400, 401, 403, 404, 429, 500, 503, 504):
            yield f'status {status}', self.provider(reply_raw(echo, status=status)), {}
        yield 'redirect', self.provider(lambda h, b: send(h, 302, echo, headers={'Location': f'http://127.0.0.1:{other.port}/{SECRET}'})), {}
        yield 'oversize', self.provider(lambda h, b: send(h, 200, echo + b' ' * 5000)), {'max_response_bytes': 1000}
        yield 'refused', None, {'url': f'http://127.0.0.1:{free_port()}/v1'}
        yield 'private', None, {'url': 'https://10.0.0.1/v1'}
        yield 'credentials', None, {'url': f'https://user:{SECRET}@example.com/v1'}

    def test_no_error_path_shows_the_key(self):
        count = 0
        for name, provider, options in self.scenarios():
            with self.subTest(name=name):
                url = options.pop('url', None) or provider.url()
                try:
                    adapter = ApiAdapter(url, 'm', ApiKey(SECRET), **options)
                    adapter.complete(call())
                except ModelAdapterError as error:
                    self.assertClean(error)
                    count += 1
                else:
                    self.fail('no error')
        self.assertGreaterEqual(count, 15)

    def test_the_key_goes_only_to_the_configured_host(self):
        other = self.provider()
        provider = self.provider(lambda h, b: send(h, 302, b'', headers={'Location': f'http://127.0.0.1:{other.port}/'}))
        self.assertCode('unsafe_endpoint', self.adapter(provider).complete, call())
        self.assertEqual(other.requests, [])
        self.assertTrue(all(request['headers']['authorization'] == 'Bearer ' + SECRET for request in provider.requests))

    def test_the_adapter_makes_no_logging_calls_with_the_key(self):
        provider = self.provider(reply_raw(b'bad', status=401))
        with self.assertNoLogs(level='DEBUG'):
            self.assertCode('auth_failed', self.adapter(provider).complete, call())

    def test_the_key_never_appears_on_the_wire_of_a_refused_endpoint(self):
        # A refused endpoint opens no socket at all.
        with mock.patch('socket.create_connection', side_effect=AssertionError('opened')):
            self.assertCode('unsafe_endpoint', ApiAdapter('https://10.0.0.1/v1', 'm', ApiKey(SECRET)).complete, call())

    def test_errors_survive_pickling_without_the_key(self):
        import pickle
        provider = self.provider(reply_raw(b'', status=401))
        error = self.assertCode('auth_failed', self.adapter(provider).complete, call())
        restored = pickle.loads(pickle.dumps(error))
        self.assertEqual(str(restored), str(error))


@unittest.skipUnless(shutil.which('openssl'), 'openssl is not installed')
class TlsTests(Case):
    """https to a loopback server with certificates made for the test. Only the contexts the tests build trust them."""

    @classmethod
    def make_certificate(cls, name, san):
        directory = Path(cls.temp.name)
        cert, key = directory / f'{name}.cert.pem', directory / f'{name}.key.pem'
        result = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=test',
                                 '-addext', f'subjectAltName={san}', '-keyout', str(key), '-out', str(cert)], capture_output=True, text=True)
        if result.returncode != 0:
            raise unittest.SkipTest('openssl could not make a test certificate')
        return cert, key

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.both = cls.make_certificate('both', 'DNS:localhost,IP:127.0.0.1')
        cls.name_only = cls.make_certificate('name', 'DNS:localhost')
        cls.other_name = cls.make_certificate('other', 'DNS:other.example')

    def server_context(self, pair):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*pair)
        return context

    def client_context(self, pair):
        context = ssl.create_default_context()
        context.load_verify_locations(pair[0])
        return context

    def local(self, host):
        return lambda name, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]

    def test_https_with_a_trusted_certificate_works(self):
        provider = self.provider(tls=self.server_context(self.both))
        adapter = ApiAdapter(provider.url(scheme='https'), 'm', ApiKey(SECRET), ssl_context=self.client_context(self.both))
        self.assertEqual(adapter.complete(call()).text, 'Hello from the model.')
        self.assertEqual(provider.requests[0]['headers']['authorization'], 'Bearer ' + SECRET)

    def test_the_certificate_is_checked_against_the_name_in_the_url_not_the_address(self):
        provider = self.provider(tls=self.server_context(self.name_only))
        context = self.client_context(self.name_only)
        by_name = ApiAdapter(f'https://localhost:{provider.port}/v1', 'm', ApiKey(SECRET), ssl_context=context, resolver=self.local('localhost'))
        self.assertEqual(by_name.complete(call()).stop_reason, 'end')
        by_address = ApiAdapter(f'https://127.0.0.1:{provider.port}/v1', 'm', ApiKey(SECRET), ssl_context=context)
        self.assertCode('endpoint_unreachable', by_address.complete, call(), retryable=False)
        self.assertEqual(len(provider.requests), 1)

    def test_a_certificate_for_another_name_is_refused(self):
        provider = self.provider(tls=self.server_context(self.other_name))
        adapter = ApiAdapter(provider.url(scheme='https'), 'm', ApiKey(SECRET), ssl_context=self.client_context(self.other_name))
        error = self.assertCode('endpoint_unreachable', adapter.complete, call(), retryable=False)
        self.assertIn('certificate', str(error))
        self.assertEqual(provider.requests, [])

    def test_an_untrusted_certificate_stops_the_call_before_the_request_is_sent(self):
        provider = self.provider(tls=self.server_context(self.both))
        adapter = ApiAdapter(provider.url(scheme='https'), 'm', ApiKey(SECRET))
        error = self.assertCode('endpoint_unreachable', adapter.complete, call(), retryable=False)
        self.assertEqual(provider.requests, [])
        self.assertIn('certificate', str(error))

    def test_a_plain_http_server_on_an_https_url_is_an_error_and_clean(self):
        provider = self.provider()
        adapter = ApiAdapter(provider.url(scheme='https'), 'm', ApiKey(SECRET), ssl_context=self.client_context(self.both))
        error = self.assertCode('endpoint_unreachable', adapter.complete, call(), retryable=False)
        self.assertIn('TLS', str(error))
        self.assertEqual(provider.requests, [])

    def test_the_socket_is_closed_when_the_handshake_fails(self):
        provider = self.provider()
        opened = []
        real = socket.create_connection

        def connector(*args, **kwargs):
            opened.append(real(*args, **kwargs))
            return opened[-1]

        class Failing(ssl.SSLContext):
            def wrap_socket(self, *args, **kwargs):
                raise ssl.SSLError('handshake failed')
        context = Failing(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        adapter = ApiAdapter(provider.url(scheme='https'), 'm', ApiKey(SECRET), ssl_context=context)
        with mock.patch('socket.create_connection', side_effect=connector):
            self.assertCode('endpoint_unreachable', adapter.complete, call())
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].fileno(), -1)

    def test_the_default_context_checks_certificates_and_hostnames(self):
        adapter = ApiAdapter('https://127.0.0.1:1/v1', 'm', ApiKey(SECRET))
        self.assertEqual(adapter._ssl_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(adapter._ssl_context.check_hostname)

    def test_a_context_that_skips_verification_is_refused(self):
        weak = []
        context = ssl.create_default_context()
        context.check_hostname = False
        weak.append(context)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        weak.append(context)
        context = ssl.create_default_context()
        context.verify_mode = ssl.CERT_OPTIONAL
        weak.append(context)
        for context in weak:
            self.assertCode('invalid_request', ApiAdapter, 'https://example.com/v1', 'm', ApiKey(SECRET), ssl_context=context)


if __name__ == '__main__':
    unittest.main()
