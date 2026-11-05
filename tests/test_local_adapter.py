"""The local adapter: loopback only, no key, presets, the model list, and the limits it adds to the API adapter.

A fake model server runs on a loopback port in a thread. The tests never start a real runtime, never call the default ports of one,
and never use the real network. Where a test builds a preset, it sends the call to the fake server by replacing `socket.create_connection`.
"""
import dataclasses
import errno
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / 'tests'):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import local_adapter
from local_adapter import LocalAdapter
import model_adapter as ma
from model_adapter import ApiKey, CompletionRequest, Message, ModelAdapterError, TokenBudget
import api_adapter
from test_api_adapter import BODY_SECRET, SECRET, Case, Provider, call, free_port, ok_payload, reply_json, reply_raw, send

NO_USABLE_ID = 'The server listed models, but Schola can use none of the names. Type the model name instead.'
GENERAL = 'Schola could not reach the endpoint.'
NOT_LISTENING = 'No local model server answered at that address. Start one, then try again.'


def models_payload(*ids):
    return {'object': 'list', 'data': [{'id': name, 'object': 'model', 'owned_by': 'test'} for name in ids]}


class LocalCase(Case):
    def adapter(self, provider, model='test-model', path='/v1', **options):
        return LocalAdapter(provider.url(path), model, **options)

    def lister(self, payload, **options):
        return self.adapter(self.provider(reply_json(payload)), **options)


class EndpointTests(LocalCase):
    def test_loopback_endpoints_are_accepted(self):
        for url in ('http://127.0.0.1:11434/v1', 'http://localhost:1234/v1', 'http://[::1]:8080/v1', 'http://LOCALHOST/v1', 'http://127.0.0.1', 'https://127.0.0.1:8443/v1',
                    'https://localhost/v1'):
            with self.subTest(url=url):
                LocalAdapter(url, 'm')

    def test_every_other_host_is_refused_with_unsafe_endpoint_and_the_url_is_never_echoed(self):
        for url in ('https://api.openai.com/v1', 'http://api.openai.com/v1', 'https://8.8.8.8/v1', 'https://[2606:4700::1111]/v1', 'http://10.0.0.5:11434/v1',
                    'http://192.168.1.20:11434/v1', 'http://127.0.0.2:11434/v1', 'http://127.1/v1', 'http://0.0.0.0:11434/v1', 'http://[::]:11434/v1',
                    'http://[::ffff:127.0.0.1]/v1', 'http://localhost.example.com/v1', 'http://127.0.0.1.example.com/v1', 'http://localhost./v1',
                    'https://127.0.0.2/v1', 'https://example.com/v1', 'http://169.254.169.254/v1', 'http://host.docker.internal:11434/v1'):
            with self.subTest(url=url):
                error = self.assertCode('unsafe_endpoint', LocalAdapter, url, 'm', retryable=False)
                self.assertNotIn(url.split('/')[2], str(error))

    def test_a_remote_host_is_refused_for_the_host_and_not_for_the_missing_key(self):
        self.assertCode('unsafe_endpoint', LocalAdapter, 'https://api.openai.com/v1', 'm')
        self.assertCode('unsafe_endpoint', LocalAdapter, 'https://api.openai.com/v1', 'm', None)

    def test_the_api_adapter_rules_still_apply(self):
        for url in ('ftp://127.0.0.1/v1', 'http://user:pass@127.0.0.1/v1', 'http://127.0.0.1/v1?x=1', 'http://127.0.0.1/v1#a', 'http://127.0.0.1:99999/v1',
                    'http://127.0.0.1/v1/../x', 'http://127.0.0.1//v1'):
            with self.subTest(url=url):
                self.assertCode('unsafe_endpoint', LocalAdapter, url, 'm')
        for bad in (None, '', 42, b'http://127.0.0.1/v1', ['http://127.0.0.1/v1']):
            with self.subTest(bad=bad):
                self.assertCode('invalid_request', LocalAdapter, bad, 'm')

    def test_the_model_name_rule_is_the_api_adapters(self):
        for bad in ('', 'has space', 'x' * 201, 'é', 42, 'a\n', 'a\x00b'):
            with self.subTest(bad=bad):
                self.assertCode('invalid_request', LocalAdapter, 'http://127.0.0.1:1/v1', bad)
        for good in ('llama3.2:latest', 'hf.co/user/model:Q4_K_M', 'qwen2.5-7b-instruct', 'x' * 200):
            LocalAdapter('http://127.0.0.1:1/v1', good)

    def test_only_these_three_spellings_are_accepted_so_other_loopback_addresses_are_refused_on_purpose(self):
        for url in ('http://127.0.0.2/v1', 'http://127.255.255.254/v1', 'http://[::ffff:127.0.0.1]/v1', 'http://0:11434/v1', 'http://[0:0:0:0:0:0:0:1]/v1',
                    'http://localhost./v1', 'http://LOCALHOST.:1/v1', 'http://ip6-localhost/v1', 'http://localhost.localdomain/v1', 'http://foo.localhost/v1'):
            with self.subTest(url=url):
                self.assertCode('unsafe_endpoint', LocalAdapter, url, 'm')

    def test_any_port_and_path_prefix_on_a_loopback_host_is_accepted(self):
        for url in ('http://127.0.0.1:1/', 'http://127.0.0.1:65535/a/b/c', 'http://localhost:9999/openai/v1/', 'http://[::1]:3000/v1'):
            with self.subTest(url=url):
                LocalAdapter(url, 'm')

    def test_localhost_is_not_looked_up_and_means_two_fixed_addresses(self):
        self.assertEqual(local_adapter.LOCALHOST_ADDRESSES, ('127.0.0.1', '::1'))
        adapter = LocalAdapter('http://localhost:11434/v1', 'm')
        with mock.patch('socket.getaddrinfo', side_effect=AssertionError('no lookup')), mock.patch('threading.Thread', side_effect=AssertionError('no lookup thread')):
            self.assertEqual(adapter._addresses(), ['127.0.0.1', '::1'])
        self.assertEqual(LocalAdapter('http://127.0.0.1:1/v1', 'm')._addresses(), ['127.0.0.1'])
        self.assertEqual(LocalAdapter('http://[::1]:1/v1', 'm')._addresses(), ['::1'])
        with self.assertRaises(TypeError):
            LocalAdapter('http://localhost:1/v1', 'm', resolver=lambda *args, **kwargs: [])

    def test_a_bad_address_for_localhost_is_still_refused_on_every_call_before_any_connection(self):
        for answer in (('10.0.0.5',), ('8.8.8.8',), ('127.0.0.1', '8.8.8.8'), ('::1', '192.168.0.9'), ('169.254.169.254',), ('::ffff:127.0.0.1',), ('0.0.0.0',), ('not an address',)):
            with self.subTest(answer=answer), mock.patch.object(local_adapter, 'LOCALHOST_ADDRESSES', answer):
                adapter = LocalAdapter('http://localhost:11434/v1', 'm')
                with mock.patch('socket.create_connection') as connect:
                    for function in (lambda: adapter.complete(call()), adapter.list_models):
                        error = self.assertCode('unsafe_endpoint', function, retryable=False)
                        self.assertEqual(str(error), 'localhost resolved to an address that is not this computer.')
                    connect.assert_not_called()

    def test_the_connection_goes_to_the_fixed_address_and_the_name_stays_in_the_host_header(self):
        provider = self.provider()
        adapter = LocalAdapter(f'http://localhost:{provider.port}/v1', 'm')
        self.assertEqual(adapter.complete(call()).text, 'Hello from the model.')
        self.assertEqual(provider.requests[0]['headers']['host'], f'localhost:{provider.port}')

    def test_a_completion_and_a_list_work_through_the_name_localhost(self):
        provider = self.provider(lambda handler, body: reply_json(models_payload('a') if handler.command == 'GET' else ok_payload())(handler, body))
        adapter = LocalAdapter(f'http://localhost:{provider.port}/v1', 'm')
        self.assertEqual(adapter.complete(call()).stop_reason, 'end')
        self.assertEqual(adapter.list_models().ids, ('a',))

    def test_a_completion_and_a_list_work_through_the_ipv6_loopback_address(self):
        try:
            provider = Provider(host='::1')
        except OSError:
            self.skipTest('this system has no IPv6 loopback address')
        self.addCleanup(provider.stop)
        provider.responder = lambda handler, body: reply_json(models_payload('a') if handler.command == 'GET' else ok_payload())(handler, body)
        adapter = LocalAdapter(f'http://[::1]:{provider.port}/v1', 'm')
        self.assertEqual(adapter.complete(call()).stop_reason, 'end')
        self.assertEqual(adapter.list_models().ids, ('a',))
        self.assertEqual(provider.requests[0]['headers']['host'], f'[::1]:{provider.port}')

    def test_the_url_messages_are_about_a_local_server_and_do_not_say_use_https(self):
        for url in ('http://example.com/v1', 'https://example.com/v1', 'ftp://127.0.0.1/v1', 'http://127.0.0.1/v1?x', 'http://127.0.0.2/v1', 'http://user:p@127.0.0.1/v1'):
            with self.subTest(url=url):
                error = self.assertCode('unsafe_endpoint', LocalAdapter, url, 'm')
                self.assertEqual(str(error), local_adapter.UNSAFE_URL)
                self.assertNotIn('Use https', str(error))
                self.assertIn('API adapter', str(error))


class KeyTests(LocalCase):
    def test_a_key_is_refused_with_invalid_request_and_never_echoed(self):
        for key in (ApiKey(SECRET), SECRET, 'x', b'x', 0, ''):
            with self.subTest(key=type(key).__name__):
                third = self.assertCode('invalid_request', LocalAdapter, 'http://127.0.0.1:1/v1', 'm', key)
                named = self.assertCode('invalid_request', LocalAdapter, 'http://127.0.0.1:1/v1', 'm', api_key=key)
                self.assertNotIn(SECRET, str(third) + str(named))
        self.assertCode('invalid_request', LocalAdapter.preset, 'ollama', 'm', api_key=ApiKey(SECRET))

    def test_no_authorization_header_is_ever_sent(self):
        provider = self.provider(lambda handler, body: reply_json(models_payload('a') if handler.command == 'GET' else ok_payload())(handler, body))
        adapter = self.adapter(provider)
        adapter.complete(call())
        adapter.list_models()
        self.assertEqual([request['method'] for request in provider.requests], ['POST', 'GET'])
        for request in provider.requests:
            self.assertNotIn('authorization', request['headers'])
            self.assertNotIn('proxy-authorization', request['headers'])
            self.assertNotIn('cookie', request['headers'])

    def test_the_adapter_holds_no_key(self):
        adapter = LocalAdapter('http://127.0.0.1:1/v1', 'm')
        self.assertIsNone(adapter._api_key)
        self.assertEqual(adapter._secrets, ())

    def test_a_key_shaped_message_is_still_refused_before_anything_is_sent(self):
        provider = self.provider()
        adapter = self.adapter(provider)
        budget = TokenBudget(100_000)
        shaped = 'sk-' + 'A1b2C3d4' * 4
        self.assertCode('invalid_request', adapter.complete, call(budget, f'my key is {shaped}'))
        self.assertEqual((provider.requests, budget.spent), ([], 0))

    def test_a_path_outside_the_allowed_folders_is_still_refused_before_anything_is_sent(self):
        provider = self.provider()
        adapter = self.adapter(provider, allowed_paths=['/allowed/project'])
        self.assertCode('invalid_request', adapter.complete, call(text='read /etc/passwd'))
        adapter.complete(call(text='read /allowed/project/notes.txt'))
        self.assertEqual(len(provider.requests), 1)


class PresetTests(LocalCase):
    def test_the_presets_are_these_three_default_ports_and_nothing_else(self):
        self.assertEqual(dict(local_adapter.PRESETS), {'ollama': 'http://127.0.0.1:11434/v1', 'lmstudio': 'http://127.0.0.1:1234/v1',
                                                       'llamacpp': 'http://127.0.0.1:8080/v1'})

    def test_the_presets_cannot_be_changed(self):
        with self.assertRaises(TypeError):
            local_adapter.PRESETS['evil'] = 'http://127.0.0.1:1/v1'
        with self.assertRaises(TypeError):
            del local_adapter.PRESETS['ollama']

    def test_every_preset_is_a_loopback_url_the_adapter_accepts(self):
        for name, url in local_adapter.PRESETS.items():
            with self.subTest(name=name):
                adapter = LocalAdapter.preset(name, 'm')
                self.assertIsInstance(adapter, LocalAdapter)
                self.assertEqual((adapter._endpoint.scheme, adapter._endpoint.host), ('http', '127.0.0.1'))
                self.assertEqual(adapter._endpoint.port, int(url.rsplit(':', 1)[1].split('/')[0]))
                self.assertEqual(adapter._endpoint.path, '/v1')

    def test_a_preset_calls_its_port_and_passes_options_on(self):
        provider = self.provider()
        wanted = []
        real = socket.create_connection

        def connector(address, *args, **kwargs):
            wanted.append(address)  # the default port of a real runtime is never opened
            return real(('127.0.0.1', provider.port), *args, **kwargs)
        adapter = LocalAdapter.preset('ollama', 'llama3.2:latest', read_timeout=7, total_timeout=9, allowed_paths=['/allowed'])
        with mock.patch('socket.create_connection', side_effect=connector):
            self.assertEqual(adapter.complete(call()).text, 'Hello from the model.')
        self.assertEqual(wanted, [('127.0.0.1', 11434)])
        self.assertEqual(json.loads(provider.requests[0]['body'])['model'], 'llama3.2:latest')
        self.assertEqual((adapter._read_timeout, adapter._total_timeout, adapter._allowed_paths), (7, 9, ('/allowed',)))

    def test_an_unknown_name_is_invalid_request_and_is_not_echoed(self):
        for name in ('Ollama', 'OLLAMA', 'lm-studio', 'llama.cpp', 'vllm', '', None, 42, ['ollama'], ('ollama',), b'ollama', 'ollama ', 'http://127.0.0.1:1/v1', 'x' * 5000):
            with self.subTest(name=repr(name)[:20]):
                error = self.assertCode('invalid_request', LocalAdapter.preset, name, 'm')
                self.assertNotIn('vllm', str(error))
                self.assertLess(len(str(error)), 300)

    def test_a_preset_cannot_move_the_base_url(self):
        with self.assertRaises(TypeError):
            LocalAdapter.preset('ollama', 'm', base_url='https://api.openai.com/v1')

    def test_a_preset_checks_the_model_name(self):
        self.assertCode('invalid_request', LocalAdapter.preset, 'ollama', 'has space')

    def test_a_subclass_gets_its_own_class_from_preset(self):
        class Mine(LocalAdapter):
            pass
        self.assertIsInstance(Mine.preset('lmstudio', 'm'), Mine)


class NoModelTests(LocalCase):
    def test_an_adapter_with_no_model_can_list_but_not_complete(self):
        provider = self.provider(reply_json(models_payload('a', 'b')))
        adapter = LocalAdapter(provider.url())
        self.assertEqual(adapter.list_models().ids, ('a', 'b'))
        budget = TokenBudget(1000)
        self.assertCode('invalid_request', adapter.complete, call(budget), retryable=False)
        self.assertEqual((budget.spent, budget.remaining, len(provider.requests)), (0, 1000, 1))

    def test_the_guard_also_sits_where_the_body_is_built(self):
        adapter = LocalAdapter('http://127.0.0.1:1/v1', None)
        self.assertCode('invalid_request', adapter._body, call())
        provider = self.provider()
        adapter = LocalAdapter(provider.url())
        error, reply = adapter._exchange(call())
        self.assertEqual(error.code, "invalid_request")
        self.assertEqual(provider.requests, [])

    def test_a_request_of_the_wrong_kind_gets_the_request_sentence_first(self):
        for adapter in (LocalAdapter('http://127.0.0.1:1/v1', None), LocalAdapter('http://127.0.0.1:1/v1', 'm')):
            for bad in (None, 'text', {'messages': []}, [Message('user', 'x')]):
                error = self.assertCode('invalid_request', adapter.complete, bad)
                self.assertEqual(str(error), 'Send a CompletionRequest.')

    def test_the_model_sentence_wins_over_the_context_guard_so_nothing_else_runs_first(self):
        adapter = LocalAdapter('http://127.0.0.1:1/v1', None)
        shaped = 'sk-' + 'A1b2C3d4' * 4
        error = self.assertCode('invalid_request', adapter.complete, call(text=f'my key is {shaped}'))
        self.assertEqual(str(error), 'Choose a model before you ask for a completion.')

    def test_a_preset_can_be_made_without_a_model(self):
        self.assertIsInstance(LocalAdapter.preset('ollama'), LocalAdapter)

    def test_a_model_that_is_not_text_is_still_refused(self):
        self.assertCode('invalid_request', LocalAdapter, 'http://127.0.0.1:1/v1', 5)


class TimeoutDefaultTests(LocalCase):
    def test_the_defaults_are_longer_than_the_api_adapters_and_inside_its_bound(self):
        adapter = LocalAdapter('http://127.0.0.1:1/v1', 'm')
        self.assertEqual((adapter._connect_timeout, adapter._read_timeout, adapter._total_timeout), (5.0, 300.0, 600.0))
        self.assertGreater(local_adapter.DEFAULT_READ_TIMEOUT, api_adapter.DEFAULT_READ_TIMEOUT)
        self.assertGreater(local_adapter.DEFAULT_TOTAL_TIMEOUT, api_adapter.DEFAULT_TOTAL_TIMEOUT)
        self.assertLessEqual(local_adapter.DEFAULT_TOTAL_TIMEOUT, api_adapter.MAX_TIMEOUT)

    def test_the_timeouts_are_checked_like_the_api_adapters(self):
        url = 'http://127.0.0.1:1/v1'
        for options in ({'connect_timeout': 0}, {'read_timeout': -1}, {'total_timeout': 0}, {'total_timeout': 601}, {'read_timeout': 601}, {'connect_timeout': True},
                        {'read_timeout': '5'}, {'total_timeout': float('nan')}, {'total_timeout': float('inf')}, {'read_timeout': 100, 'total_timeout': 50},
                        {'connect_timeout': 20, 'read_timeout': 10, 'total_timeout': 10}, {'total_timeout': '30'}, {'total_timeout': True}, {'total_timeout': float('nan')}):
            with self.subTest(options=options):
                self.assertCode('invalid_request', LocalAdapter, url, 'm', **options)
        LocalAdapter(url, 'm', connect_timeout=600, read_timeout=600, total_timeout=600)

    def test_setting_only_the_total_time_works_and_lowers_the_defaults_that_would_not_fit(self):
        url = 'http://127.0.0.1:1/v1'
        adapter = LocalAdapter(url, 'm', total_timeout=30)
        self.assertEqual((adapter._connect_timeout, adapter._read_timeout, adapter._total_timeout), (5.0, 30, 30))
        adapter = LocalAdapter(url, 'm', total_timeout=2)
        self.assertEqual((adapter._connect_timeout, adapter._read_timeout, adapter._total_timeout), (2, 2, 2))
        adapter = LocalAdapter(url, 'm', total_timeout=450)
        self.assertEqual((adapter._connect_timeout, adapter._read_timeout, adapter._total_timeout), (5.0, 300.0, 450))
        adapter = LocalAdapter(url, 'm', read_timeout=40)
        self.assertEqual((adapter._connect_timeout, adapter._read_timeout, adapter._total_timeout), (5.0, 40, 600.0))
        adapter = LocalAdapter(url, 'm', connect_timeout=1, total_timeout=30)
        self.assertEqual((adapter._connect_timeout, adapter._read_timeout, adapter._total_timeout), (1, 30, 30))
        self.assertEqual(LocalAdapter.preset('ollama', 'm', total_timeout=30)._read_timeout, 30)

    def test_a_value_the_caller_sets_is_never_lowered_and_is_still_checked(self):
        url = 'http://127.0.0.1:1/v1'
        self.assertCode('invalid_request', LocalAdapter, url, 'm', read_timeout=100, total_timeout=50)
        self.assertCode('invalid_request', LocalAdapter, url, 'm', connect_timeout=100, total_timeout=50)
        self.assertCode('invalid_request', LocalAdapter, url, 'm', total_timeout=601)
        self.assertCode('invalid_request', LocalAdapter, url, 'm', total_timeout=0)

    def test_the_response_cap_is_checked_like_the_api_adapters(self):
        for cap in (0, -1, True, 10_000_001, 1.5, None):
            with self.subTest(cap=cap):
                self.assertCode('invalid_request', LocalAdapter, 'http://127.0.0.1:1/v1', 'm', max_response_bytes=cap)

    def test_a_slow_model_that_answers_inside_the_read_time_is_accepted(self):
        def slow(handler, body):
            time.sleep(1.2)
            reply_json(ok_payload())(handler, body)
        adapter = self.adapter(self.provider(slow), read_timeout=5, total_timeout=10)
        self.assertEqual(adapter.complete(call()).text, 'Hello from the model.')

    def test_a_model_that_never_answers_ends_at_the_total_time_and_holds_the_budget_for_the_call(self):
        provider = self.provider(lambda handler, body: time.sleep(30))
        adapter = self.adapter(provider, connect_timeout=1, read_timeout=1, total_timeout=1.5)
        for attempt in range(3):
            budget = TokenBudget(100_000)
            request = call(budget)
            started = time.monotonic()
            error = self.assertCode('timeout', adapter.complete, request, retryable=True)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 3.0, f'attempt {attempt}: {elapsed:.2f} s')
            self.assertEqual(error.tokens_charged, request.hold_tokens)
            self.assertEqual(budget.spent, request.hold_tokens)


class CompleteTests(LocalCase):
    def test_the_request_is_the_openai_chat_request_with_streaming_off(self):
        provider = self.provider()
        self.adapter(provider, model='llama3.2:latest').complete(call(text='Hi there.', max_output_tokens=321))
        sent = provider.requests[0]
        self.assertEqual((sent['method'], sent['path']), ('POST', '/v1/chat/completions'))
        self.assertEqual(json.loads(sent['body']), {'model': 'llama3.2:latest', 'messages': [{'role': 'user', 'content': 'Hi there.'}], 'max_tokens': 321, 'stream': False})

    def test_the_output_limit_field_can_be_renamed(self):
        provider = self.provider()
        self.adapter(provider, max_tokens_field='max_completion_tokens').complete(call(max_output_tokens=55))
        self.assertEqual(json.loads(provider.requests[0]['body'])['max_completion_tokens'], 55)

    def test_usage_is_charged_as_reported_with_the_shared_floor(self):
        provider = self.provider(reply_json(ok_payload(usage={'prompt_tokens': 40, 'completion_tokens': 10})))
        budget = TokenBudget(100_000)
        result = self.adapter(provider).complete(call(budget))
        self.assertEqual((result.usage_reported, result.tokens_charged, budget.spent), (True, 50, 50))

    def test_missing_usage_charges_the_whole_hold(self):
        for usage in ('omit', {}, {'prompt_tokens': 0, 'completion_tokens': 5}, {'prompt_tokens': 'a', 'completion_tokens': 5}, {'prompt_tokens': 5}, None):
            with self.subTest(usage=usage):
                payload = ok_payload(usage='omit' if usage is None else usage)
                if usage is None:
                    payload['usage'] = None
                provider = self.provider(reply_json(payload))
                budget = TokenBudget(100_000)
                request = call(budget, max_output_tokens=400)
                result = self.adapter(provider).complete(request)
                self.assertFalse(result.usage_reported)
                self.assertIsNone(result.usage)
                self.assertEqual((result.tokens_charged, budget.spent), (request.hold_tokens, request.hold_tokens))
                self.assertIn('no usage', result.notice)

    def test_a_cap_that_cannot_hold_the_call_sends_nothing(self):
        provider = self.provider()
        budget = TokenBudget(50)
        self.assertCode('token_cap_reached', self.adapter(provider).complete, call(budget, max_output_tokens=100), retryable=False)
        self.assertEqual((provider.requests, budget.spent), ([], 0))

    def test_the_reply_text_is_not_trusted_and_a_provider_error_text_never_leaks(self):
        provider = self.provider(reply_json({'error': {'message': BODY_SECRET, 'code': 500}}))
        self.assertCode('endpoint_unreachable', self.adapter(provider).complete, call())

    def test_a_status_error_is_not_read_and_a_redirect_is_never_followed(self):
        target = self.provider()
        redirecting = self.provider(lambda handler, body: send(handler, 302, b'', headers={'Location': target.url('/v1/chat/completions')}))
        self.assertCode('unsafe_endpoint', self.adapter(redirecting).complete, call())
        self.assertCode('unsafe_endpoint', self.adapter(redirecting).list_models)
        self.assertEqual(target.requests, [])


class NotListeningTests(LocalCase):
    def test_nothing_listening_gives_the_friendly_retryable_error_for_a_list(self):
        adapter = LocalAdapter(f'http://127.0.0.1:{free_port()}/v1', 'm')
        error = self.assertCode('endpoint_unreachable', adapter.list_models, retryable=True)
        self.assertEqual(str(error), NOT_LISTENING)
        self.assertEqual(error.tokens_charged, 0)
        self.assertEqual(error.payload(), {'error': NOT_LISTENING, 'code': 'endpoint_unreachable', 'retryable': True, 'tokens_charged': 0})

    def test_nothing_listening_gives_the_same_error_for_a_completion_and_charges_nothing(self):
        port = free_port()
        adapter = LocalAdapter(f'http://127.0.0.1:{port}/v1', 'm')
        budget = TokenBudget(1000)
        error = self.assertCode('endpoint_unreachable', adapter.complete, call(budget), retryable=True)
        self.assertEqual(str(error), NOT_LISTENING)
        self.assertEqual((budget.spent, budget.remaining), (0, 1000))
        self.assertNotIn(str(port), str(error))
        self.assertNotIn('127.0.0.1', str(error))

    def test_the_sentence_is_fixed_for_every_loopback_spelling_and_every_address_is_tried(self):
        tried = []

        def refuse(address, *args, **kwargs):
            tried.append(address)
            raise ConnectionRefusedError(errno.ECONNREFUSED, 'refused')
        for host, expected in (('127.0.0.1', ['127.0.0.1']), ('[::1]', ['::1']), ('localhost', ['127.0.0.1', '::1'])):
            del tried[:]
            adapter = LocalAdapter(f'http://{host}:1234/secret-path/v1', 'm')
            with mock.patch('socket.create_connection', side_effect=refuse):
                self.assertEqual(str(self.assertCode('endpoint_unreachable', adapter.list_models, retryable=True)), NOT_LISTENING)
            self.assertEqual([address for address, port in tried], expected)

    def test_only_a_refusal_gets_the_friendly_sentence(self):
        cases = ((errno.ECONNREFUSED, ConnectionRefusedError, NOT_LISTENING, True), (errno.ECONNABORTED, ConnectionAbortedError, NOT_LISTENING, True),
                 (errno.EMFILE, OSError, GENERAL, True), (errno.ENETUNREACH, OSError, GENERAL, True), (errno.EADDRNOTAVAIL, OSError, GENERAL, True),
                 (errno.EACCES, PermissionError, GENERAL, False), (errno.EPERM, PermissionError, GENERAL, False), (errno.EHOSTUNREACH, OSError, GENERAL, True))
        adapter = LocalAdapter('http://127.0.0.1:1234/v1', 'm')
        for number, kind, sentence, retryable in cases:
            for call_it in (adapter.list_models, lambda: adapter.complete(call())):
                with self.subTest(errno=number), mock.patch('socket.create_connection', side_effect=kind(number, 'x')):
                    error = self.assertCode('endpoint_unreachable', call_it, retryable=retryable)
                    self.assertEqual(str(error), sentence)
                    self.assertEqual(error.tokens_charged, 0)

    def test_a_refusal_on_one_address_and_trouble_on_the_other_still_says_nothing_is_listening(self):
        for order in ((OSError(errno.EADDRNOTAVAIL, 'x'), ConnectionRefusedError(errno.ECONNREFUSED, 'x')), (ConnectionRefusedError(errno.ECONNREFUSED, 'x'), OSError(errno.ENETUNREACH, 'x'))):
            with self.subTest(order=[type(e).__name__ for e in order]), mock.patch('socket.create_connection', side_effect=list(order)):
                error = self.assertCode('endpoint_unreachable', LocalAdapter('http://localhost:1234/v1', 'm').list_models, retryable=True)
                self.assertEqual(str(error), NOT_LISTENING)

    def test_a_timeout_on_any_address_is_a_timeout_and_not_the_friendly_sentence(self):
        for order in ((socket.timeout('t'), ConnectionRefusedError(errno.ECONNREFUSED, 'x')), (ConnectionRefusedError(errno.ECONNREFUSED, 'x'), socket.timeout('t'))):
            for call_it in (lambda adapter: adapter.list_models(), lambda adapter: adapter.complete(call())):
                with self.subTest(first=type(order[0]).__name__), mock.patch('socket.create_connection', side_effect=list(order)):
                    adapter = LocalAdapter('http://localhost:1234/v1', 'm')
                    error = self.assertCode('timeout', call_it, adapter, retryable=True)
                    self.assertNotEqual(str(error), NOT_LISTENING)

    def test_other_connection_trouble_keeps_the_general_sentence(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(5)
        self.addCleanup(listener.close)

        def serve():
            while True:
                try:
                    listener.accept()[0].close()
                except OSError:
                    return
        threading.Thread(target=serve, daemon=True).start()
        adapter = LocalAdapter(f'http://127.0.0.1:{listener.getsockname()[1]}/v1', 'm')
        error = self.assertCode('endpoint_unreachable', adapter.list_models, retryable=True)
        self.assertEqual(str(error), GENERAL)

    def test_a_server_error_status_keeps_its_own_sentence(self):
        provider = self.provider(reply_raw(b'', 503))
        error = self.assertCode('endpoint_unreachable', self.adapter(provider).list_models, retryable=True)
        self.assertEqual(str(error), 'The provider had a server error (HTTP 503).')


class ListModelsTests(LocalCase):
    def test_it_asks_with_a_get_to_models_and_returns_the_ids_in_order_as_a_tuple(self):
        provider = self.provider(reply_json(models_payload('llama3.2:latest', 'qwen2.5:7b', 'hf.co/user/model:Q4_K_M')))
        result = self.adapter(provider).list_models()
        self.assertEqual(result.ids, ('llama3.2:latest', 'qwen2.5:7b', 'hf.co/user/model:Q4_K_M'))
        self.assertEqual((result.skipped, result.truncated), (0, False))
        self.assertIsInstance(result.ids, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.ids = ()
        sent = provider.requests[0]
        self.assertEqual((sent['method'], sent['path'], sent['body']), ('GET', '/v1/models', b''))
        self.assertEqual(sent['headers']['accept'], 'application/json')
        self.assertNotIn('content-type', sent['headers'])
        self.assertEqual(sent['headers']['connection'], 'close')
        self.assertEqual(len(provider.requests), 1)

    def test_it_works_with_a_base_url_that_has_no_path_or_a_trailing_slash(self):
        provider = self.provider(reply_json(models_payload('a')))
        self.assertEqual(LocalAdapter(provider.url(''), 'm').list_models().ids, ('a',))
        self.assertEqual(LocalAdapter(provider.url('/v1/'), 'm').list_models().ids, ('a',))
        self.assertEqual([request['path'] for request in provider.requests], ['/models', '/v1/models'])

    def test_it_takes_no_argument_and_so_uses_no_budget(self):
        adapter = self.adapter(self.provider(reply_json(models_payload('a'))))
        with self.assertRaises(TypeError):
            adapter.list_models(TokenBudget(10))
        self.assertEqual(adapter.list_models().ids, ('a',))

    def test_an_empty_list_is_an_empty_result(self):
        result = self.lister({'object': 'list', 'data': []}).list_models()
        self.assertEqual((result.ids, result.skipped, result.truncated), ((), 0, False))

    def test_ids_that_the_model_name_rule_refuses_are_left_out(self):
        long_id = 'x' * 201
        ids = ['ok-1', 'has space', '', long_id, 'é', 'a\nb', '/Users/me/My Models/x.gguf', 'ok-2', 'a\x00b', 'x' * 200]
        result = self.lister(models_payload(*ids)).list_models()
        self.assertEqual(result.ids, ('ok-1', 'ok-2', 'x' * 200))
        self.assertEqual((result.skipped, result.truncated), (7, False))

    def test_every_returned_id_can_start_an_adapter(self):
        names = ('llama3.2:latest', 'hf.co/user/model:Q4_K_M', 'qwen2.5-7b-instruct-1m', 'a@b+c#d~e', 'models/x.gguf', 'llama3:8b', 'a..b')
        for name in self.lister(models_payload(*names)).list_models().ids:
            LocalAdapter('http://127.0.0.1:1/v1', name)

    def test_entries_of_the_wrong_kind_are_left_out(self):
        data = [{'id': 'good'}, 'text', 5, None, [], {'id': 5}, {'id': None}, {'id': ['x']}, {'name': 'no-id'}, {'id': True}, {}, {'id': 'good-too'}]
        result = self.lister({'data': data}).list_models()
        self.assertEqual((result.ids, result.skipped), (('good', 'good-too'), 10))

    def test_a_repeated_id_is_listed_once(self):
        result = self.lister(models_payload('a', 'b', 'a', 'b', 'c')).list_models()
        self.assertEqual((result.ids, result.skipped), (('a', 'b', 'c'), 2))

    def test_the_count_is_capped_and_the_result_says_so(self):
        cap = local_adapter.MAX_LISTED_MODELS
        self.assertEqual(cap, 500)
        names = [f'model-{number}' for number in range(cap + 100)]
        result = self.lister(models_payload(*names)).list_models()
        self.assertEqual((result.ids, result.truncated, result.skipped), (tuple(names[:cap]), True, 0))
        exact = self.lister(models_payload(*names[:cap])).list_models()
        self.assertEqual((len(exact.ids), exact.truncated), (cap, False))
        one_more = self.lister(models_payload(*names[:cap + 1])).list_models()
        self.assertEqual((len(one_more.ids), one_more.truncated), (cap, True))

    def test_unusable_entries_after_the_cap_do_not_make_the_list_truncated(self):
        cap = local_adapter.MAX_LISTED_MODELS
        data = [{'id': f'model-{number}'} for number in range(cap)] + [{'id': 'bad id'}, {'id': 'model-3'}, 'text']
        result = self.lister({'data': data}).list_models()
        self.assertEqual((len(result.ids), result.truncated, result.skipped), (cap, False, 3))

    def test_the_cap_counts_listed_ids_and_not_skipped_entries(self):
        junk = [{'id': 'bad id'}] * 1000
        result = self.lister({'data': junk + [{'id': 'fine'}]}).list_models()
        self.assertEqual((result.ids, result.skipped), (('fine',), 1000))

    def test_a_list_with_entries_and_no_usable_id_is_an_error_not_an_empty_list(self):
        for data in ([{'id': '/Users/me/My Models/x.gguf'}], [{'id': 'has space'}, {'id': ''}], ['text', 5, None], [{'name': 'a'}], [{'id': 'a'}] * 0 + [{'id': 'bad id'}] * 600):
            with self.subTest(data=str(data)[:40]):
                error = self.assertCode('bad_response', self.lister({'data': data}).list_models, retryable=False)
                self.assertEqual(str(error), NO_USABLE_ID)
                self.assertEqual(error.tokens_charged, 0)

    def test_a_reply_that_is_not_the_list_envelope_is_bad_response_and_charges_nothing(self):
        for body in (b'', b'not json', b'[]', b'[{"id":"a"}]', b'null', b'"text"', b'5', b'{}', b'{"data":null}', b'{"data":{}}', b'{"data":"a"}', b'{"models":[{"id":"a"}]}',
                     b'{"data":[}', b'{"data": NaN}', b'{"data":[],"n":' + b'9' * 40 + b'}', b'\xff\xfe', b'{"data":[{"id":"a"}]} trailing'):
            with self.subTest(body=body[:30]):
                error = self.assertCode('bad_response', self.adapter(self.provider(reply_raw(body))).list_models, retryable=False)
                self.assertEqual(error.tokens_charged, 0)

    def test_a_deeply_nested_reply_is_a_typed_error_and_never_a_crash(self):
        # Some Python versions refuse this depth and some read it. Both are fine: an entry that is a list is not a model.
        body = b'{"data":' + b'[' * 100_000 + b']' * 100_000 + b'}'
        adapter = self.adapter(self.provider(reply_raw(body)), max_response_bytes=1_000_000)
        try:
            self.fail(adapter.list_models())
        except ModelAdapterError as error:
            self.assertEqual(error.code, 'bad_response')
            self.assertClean(error)
        top = b'[' * 100_000 + b']' * 100_000
        self.assertCode('bad_response', self.adapter(self.provider(reply_raw(top))).list_models)

    def test_ids_are_untrusted_labels_so_path_like_and_option_like_ids_are_left_out(self):
        bad = ('../x', 'a/../b', '..', 'a/..', '/abs/model.gguf', '/', '~', '~root/x', '~/m', '-flag', '--model', '-', 'C:/models/x', 'c:\\models\\x', 'Z:/x', 'C:\\x', '//server/share')
        good = ('llama3:8b', 'hf.co/user/model', 'a..b', 'x/y/z', 'a-b', 'model.v2', 'q4_k_m', 'a~b', 'c:x', 'ab:/x', 'ab/x', 'c:', '1:/x', '.hidden', 'a/.b', '...')
        for name in bad:
            with self.subTest(bad=name):
                result = self.lister(models_payload(name, 'fine')).list_models()
                self.assertEqual((result.ids, result.skipped), (('fine',), 1))
        result = self.lister(models_payload(*good)).list_models()
        self.assertEqual(result.ids, good)
        self.assertEqual(result.skipped, 0)

    def test_a_huge_id_is_skipped_quickly(self):
        big = 'a' * 900_000
        adapter = self.adapter(self.provider(reply_json(models_payload(big, 'fine'))), max_response_bytes=1_000_000)
        started = time.monotonic()
        self.assertEqual(adapter.list_models().ids, ('fine',))
        self.assertLess(time.monotonic() - started, 5.0)

    def test_an_error_object_in_a_200_reply_is_an_error_and_never_echoed(self):
        for body, code in (({'error': {'message': BODY_SECRET, 'code': 429}}, 'rate_limited'), ({'error': {'message': BODY_SECRET}}, 'bad_response'),
                           ({'error': BODY_SECRET, 'data': [{'id': 'a'}]}, 'bad_response'), ({'error': {'code': '404', 'message': BODY_SECRET}}, 'invalid_request')):
            with self.subTest(body=code):
                error = self.assertCode(code, self.lister(body).list_models)
                self.assertEqual(error.tokens_charged, 0)

    def test_status_errors_map_like_the_api_adapter_and_charge_nothing(self):
        cases = {401: ('auth_failed', False), 403: ('auth_failed', False), 404: ('invalid_request', False), 429: ('rate_limited', True), 500: ('endpoint_unreachable', True),
                 503: ('endpoint_unreachable', True), 504: ('timeout', True), 501: ('bad_response', False), 302: ('unsafe_endpoint', False)}
        for status, (code, retryable) in cases.items():
            with self.subTest(status=status):
                provider = self.provider(lambda handler, body, status=status: send(handler, status, BODY_SECRET.encode()))
                error = self.assertCode(code, self.adapter(provider).list_models, retryable=retryable)
                self.assertEqual(error.tokens_charged, 0)

    def test_a_local_server_gets_local_sentences_for_the_statuses_that_read_wrong_for_it(self):
        no_list = 'This server does not offer a model list. Type the model name instead.'
        wants_key = 'The local server asked for a key. This adapter does not send one.'
        for status, sentence, code in ((404, no_list, 'invalid_request'), (401, wants_key, 'auth_failed'), (403, wants_key, 'auth_failed')):
            with self.subTest(status=status):
                provider = self.provider(lambda handler, body, status=status: send(handler, status, BODY_SECRET.encode()))
                error = self.assertCode(code, self.adapter(provider).list_models, retryable=False)
                self.assertEqual(str(error), sentence)
                self.assertNotIn('API key', str(error).replace('asked for a key', ''))
                inside = self.assertCode(code, self.lister({'error': {'code': status, 'message': BODY_SECRET}}).list_models, retryable=False)
                self.assertTrue(str(inside).endswith(sentence), str(inside))
        for status in (401, 403):
            provider = self.provider(lambda handler, body, status=status: send(handler, status, b'x'))
            self.assertEqual(str(self.assertCode('auth_failed', self.adapter(provider).complete, call())), wants_key)
            inside = self.assertCode('auth_failed', self.lister({'error': {'code': status}}).complete, call())
            self.assertTrue(str(inside).endswith(wants_key), str(inside))
        inside = self.assertCode('invalid_request', self.lister({'error': {'code': 404}}).complete, call())
        self.assertTrue(str(inside).endswith('Check the base URL and the model name.'), str(inside))
        self.assertNotIn('model list', str(inside))

    def test_a_404_on_the_chat_path_keeps_the_general_sentence_and_the_other_statuses_do_not_change(self):
        provider = self.provider(lambda handler, body: send(handler, 404, b'x'))
        error = self.assertCode('invalid_request', self.adapter(provider).complete, call(), retryable=False)
        self.assertEqual(str(error), 'The provider refused the request (HTTP 404). Check the base URL and the model name.')
        for status, code, sentence in ((429, 'rate_limited', 'The provider says there were too many requests or the quota is used. Try again later.'),
                                       (504, 'timeout', 'The provider timed out (HTTP 504).')):
            provider = self.provider(lambda handler, body, status=status: send(handler, status, b'x'))
            self.assertEqual(str(self.assertCode(code, self.adapter(provider).list_models)), sentence)

    def test_the_api_adapter_keeps_its_own_wording(self):
        for status, sentence in ((401, 'The provider refused the API key. Check the key and what it may do.'),
                                 (404, 'The provider refused the request (HTTP 404). Check the base URL and the model name.')):
            provider = self.provider(lambda handler, body, status=status: send(handler, status, b'x'))
            adapter = api_adapter.ApiAdapter(provider.url(), 'm', ApiKey(SECRET))
            self.assertEqual(str(self.assertCode('auth_failed' if status == 401 else 'invalid_request', adapter.complete, call())), sentence)

    def test_the_body_cap_holds(self):
        padded = json.dumps(models_payload('a')).encode()
        for size, outcome in ((2000, ('a',)), (2001, None)):
            with self.subTest(size=size):
                provider = self.provider(reply_raw(padded + b' ' * (size - len(padded))))
                adapter = self.adapter(provider, max_response_bytes=2000)
                if outcome is None:
                    error = self.assertCode('response_too_large', adapter.list_models, retryable=False)
                    self.assertEqual(error.tokens_charged, 0)
                else:
                    self.assertEqual(adapter.list_models().ids, outcome)

    def test_a_chunked_body_over_the_cap_is_refused_too(self):
        def chunked(handler, body):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Transfer-Encoding', 'chunked')
            handler.end_headers()
            for _ in range(10):
                handler.wfile.write(b'400\r\n' + b' ' * 1024 + b'\r\n')
            handler.wfile.write(b'0\r\n\r\n')
        self.assertCode('response_too_large', self.adapter(self.provider(chunked), max_response_bytes=2000).list_models)

    def test_the_media_type_encoding_and_length_rules_hold(self):
        good = json.dumps(models_payload('a')).encode()
        cases = (dict(content_type='text/html'), dict(content_type='application/json; charset=latin-1'), dict(headers={'Content-Encoding': 'gzip'}),
                 dict(content_type=None), dict(headers={'Content-Length': 'x'}, length=False))
        for options in cases:
            with self.subTest(options=options):
                self.assertCode('bad_response', self.adapter(self.provider(reply_raw(good, **options))).list_models)
        self.assertEqual(self.adapter(self.provider(reply_raw(good, content_type='application/vnd.api+json; charset=utf-8'))).list_models().ids, ('a',))

    def test_a_body_that_ends_early_is_bad_response(self):
        def short(handler, body):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', '500')
            handler.end_headers()
            handler.wfile.write(b'{"data":')
            handler.wfile.flush()
            handler.close_connection = True
        error = self.assertCode('bad_response', self.adapter(self.provider(short)).list_models)
        self.assertEqual(error.tokens_charged, 0)

    def test_a_server_that_never_answers_ends_at_the_list_limit_with_a_retryable_timeout(self):
        provider = self.provider(lambda handler, body: time.sleep(30))
        adapter = self.adapter(provider, connect_timeout=1, read_timeout=1, total_timeout=1.5)
        for attempt in range(3):
            started = time.monotonic()
            error = self.assertCode('timeout', adapter.list_models, retryable=True)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 3.0, f'attempt {attempt}: {elapsed:.2f} s')
            self.assertEqual(error.tokens_charged, 0)

    def test_the_list_never_waits_longer_than_its_own_limit_even_when_the_call_limits_are_long(self):
        self.assertEqual(local_adapter.LIST_SECONDS, 30.0)
        provider = self.provider(lambda handler, body: time.sleep(30))
        adapter = self.adapter(provider)  # the defaults: read 300 seconds, total 600 seconds
        with mock.patch.object(local_adapter, 'LIST_SECONDS', 1.0):
            started = time.monotonic()
            self.assertCode('timeout', adapter.list_models, retryable=True)
            self.assertLess(time.monotonic() - started, 3.0)

    def test_a_short_call_limit_also_shortens_the_list(self):
        provider = self.provider(lambda handler, body: time.sleep(30))
        adapter = self.adapter(provider, connect_timeout=0.5, read_timeout=0.5, total_timeout=0.5)
        started = time.monotonic()
        self.assertCode('timeout', adapter.list_models)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_server_that_drips_the_body_ends_at_the_list_limit(self):
        def drip(handler, body):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            try:
                for _ in range(100):
                    handler.wfile.write(b' ')
                    handler.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
        adapter = self.adapter(self.provider(drip), connect_timeout=1, read_timeout=1, total_timeout=1)
        started = time.monotonic()
        self.assertCode('timeout', adapter.list_models)
        self.assertLess(time.monotonic() - started, 2.5)

    def test_the_list_keeps_the_callers_read_and_total_times_apart(self):
        data = json.dumps(models_payload('a')).encode()

        def slow_body(handler, body):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            try:
                for index in range(0, len(data), 3):  # short waits; the whole reply takes a few seconds
                    handler.wfile.write(data[index:index + 3])
                    handler.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
        provider = self.provider(slow_body)
        self.assertEqual(self.adapter(provider, connect_timeout=1, read_timeout=1, total_timeout=20).list_models().ids, ('a',))
        self.assertCode('timeout', self.adapter(provider, connect_timeout=1, read_timeout=1, total_timeout=1.5).list_models)

    def test_a_body_that_drips_ends_at_the_list_limit_when_the_call_limits_are_long(self):
        def drip(handler, body):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            try:
                for _ in range(100):
                    handler.wfile.write(b' ')
                    handler.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
        adapter = self.adapter(self.provider(drip))  # the defaults: read 300 seconds, total 600 seconds
        with mock.patch.object(local_adapter, 'LIST_SECONDS', 1.0):
            started = time.monotonic()
            self.assertCode('timeout', adapter.list_models, retryable=True)
            self.assertLess(time.monotonic() - started, 4.0)

    def test_a_list_is_read_whole_when_the_server_says_connection_close(self):
        body = json.dumps(models_payload('a', 'b')).encode()

        def chunked(handler, request_body):
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Transfer-Encoding', 'chunked')
            handler.send_header('Connection', 'close')
            handler.end_headers()
            for start in range(0, len(body), 9):
                piece = body[start:start + 9]
                handler.wfile.write(f'{len(piece):x}\r\n'.encode() + piece + b'\r\n')
            handler.wfile.write(b'0\r\n\r\n')
        responders = {'length': lambda handler, b: send(handler, 200, body, headers={'Connection': 'close'}), 'chunked': chunked,
                      'neither': lambda handler, b: send(handler, 200, body, headers={'Connection': 'close'}, length=False)}
        for name, responder in responders.items():
            with self.subTest(framing=name):
                self.assertEqual(self.adapter(self.provider(responder)).list_models().ids, ('a', 'b'))

    def test_a_failure_leaks_no_url_address_or_provider_text(self):
        provider = self.provider(lambda handler, body: send(handler, 200, json.dumps({'data': [{'id': 'a'}], 'error': BODY_SECRET}).encode()))
        error = self.assertCode('bad_response', self.adapter(provider).list_models)
        for text in (str(provider.port), '127.0.0.1', '/v1/models'):
            self.assertNotIn(text, str(error))

    def test_an_unexpected_failure_inside_the_list_is_a_typed_error(self):
        adapter = self.adapter(self.provider(reply_json(models_payload('a'))))
        with mock.patch.object(local_adapter, '_models', side_effect=RuntimeError(BODY_SECRET)):
            error = self.assertCode('bad_response', adapter.list_models)
        self.assertNotIn(BODY_SECRET, str(error))

    def test_the_list_works_while_a_completion_is_running(self):
        gate = threading.Event()

        def responder(handler, body):
            if handler.command == 'GET':
                reply_json(models_payload('a'))(handler, body)
            else:
                gate.wait(10)
                reply_json(ok_payload())(handler, body)
        adapter = self.adapter(self.provider(responder))
        results = []
        worker = threading.Thread(target=lambda: results.append(adapter.complete(call())))
        worker.start()
        try:
            self.assertEqual(adapter.list_models().ids, ('a',))
        finally:
            gate.set()
            worker.join(10)
        self.assertEqual(results[0].text, 'Hello from the model.')


if __name__ == '__main__':
    unittest.main()
