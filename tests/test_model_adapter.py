"""The model adapter interface: errors, the API key wrapper, the token budget, the shared call rules, and the context guard.

These tests use a fake adapter and no network. The transport has its own file, `test_api_adapter.py`.
"""
import copy
import inspect
import json
import logging
from pathlib import Path
import pickle
import sys
import threading
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import model_adapter as ma
from model_adapter import ApiKey, CompletionRequest, Message, ModelAdapterError, RawCompletion, TokenBudget, Usage

SECRET = 'sk-TESTSECRETVALUE0123456789abcdef'


def request(budget=None, text='Hello.', max_output_tokens=100):
    return CompletionRequest([Message('user', text)], max_output_tokens, budget or TokenBudget(1000))


def reply(text='Hi.', usage=(10, 5), stop='end'):
    return RawCompletion(text, None if usage is None else Usage(*usage), stop)


class FakeAdapter(ma.ModelAdapter):
    """Returns a canned reply, raises a canned error, or waits on an event. Counts its calls."""

    def __init__(self, result=reply, error=None, gate=None, **options):
        super().__init__(**options)
        self.result = reply() if result is reply else result
        self.error = error
        self.gate = gate
        self.calls = 0
        self.seen = []

    def _send(self, call):
        self.calls += 1
        self.seen.append(call)
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.result


def refused(test, code, function, *args, **kwargs):
    with test.assertRaises(ModelAdapterError) as context:
        function(*args, **kwargs)
    test.assertEqual(context.exception.code, code, str(context.exception))
    return context.exception


class ErrorTests(unittest.TestCase):
    def test_the_codes_are_closed(self):
        self.assertEqual(ma.ERROR_CODES, ('auth_failed', 'rate_limited', 'timeout', 'endpoint_unreachable', 'response_too_large',
                                          'bad_response', 'token_cap_reached', 'unsafe_endpoint', 'invalid_request'))

    def test_retryable_defaults_follow_the_code(self):
        retryable = {code for code in ma.ERROR_CODES if ModelAdapterError('x', code).retryable}
        self.assertEqual(retryable, {'rate_limited', 'timeout', 'endpoint_unreachable'})

    def test_retryable_can_be_set(self):
        self.assertTrue(ModelAdapterError('x', 'bad_response', retryable=True).retryable)
        self.assertFalse(ModelAdapterError('x', 'timeout', retryable=False).retryable)

    def test_an_unknown_code_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            ModelAdapterError('x', 'made_up')

    def test_payload_has_the_message_the_code_and_retryable(self):
        error = ModelAdapterError('Slow.', 'timeout')
        self.assertEqual(error.payload(), {'error': 'Slow.', 'code': 'timeout', 'retryable': True})
        self.assertEqual(str(error), 'Slow.')


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.key = ApiKey(SECRET)

    def assertHidden(self, text):
        self.assertNotIn(SECRET, text)
        self.assertNotIn(SECRET[:12], text)

    def test_str_repr_and_format_show_redacted(self):
        self.assertEqual(str(self.key), '[redacted]')
        self.assertEqual(repr(self.key), '[redacted]')
        self.assertEqual(format(self.key), '[redacted]')
        self.assertEqual(f'{self.key}', '[redacted]')
        self.assertEqual(f'{self.key!r}', '[redacted]')
        self.assertEqual('%s %r' % (self.key, self.key), '[redacted] [redacted]')
        self.assertEqual('{}'.format(self.key), '[redacted]')
        self.assertEqual(f'{self.key:>14}', '    [redacted]')

    def test_a_format_spec_cannot_leak_the_value(self):
        for spec in ('s', '>40', '.5', '<3', '^30'):
            self.assertHidden(format(self.key, spec))
        with self.assertRaises(ValueError) as context:
            format(self.key, 'd')
        self.assertHidden(str(context.exception))

    def test_containers_and_logging_show_redacted(self):
        self.assertHidden(repr([self.key]))
        self.assertHidden(repr({'key': self.key}))
        self.assertHidden(repr((self.key,)))
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger('test_model_adapter_key')
        logger.propagate = False
        logger.addHandler(Capture())
        logger.warning('key=%s', self.key)
        logger.warning('key=%r', self.key)
        self.assertEqual(records, ['key=[redacted]', 'key=[redacted]'])

    def test_pickle_is_refused_in_every_protocol(self):
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.assertRaises(TypeError) as context:
                pickle.dumps(self.key, protocol)
            self.assertHidden(str(context.exception))

    def test_json_never_writes_the_value(self):
        with self.assertRaises(TypeError) as context:
            json.dumps({'key': self.key})
        self.assertHidden(str(context.exception))
        self.assertEqual(json.dumps({'key': self.key}, default=str), '{"key": "[redacted]"}')
        self.assertEqual(json.dumps({'key': self.key}, default=repr), '{"key": "[redacted]"}')

    def test_copy_keeps_the_key_and_stays_hidden(self):
        for duplicate in (copy.copy(self.key), copy.deepcopy(self.key)):
            self.assertEqual(duplicate.reveal_for_transport(), SECRET)
            self.assertHidden(repr(duplicate))

    def test_the_value_is_reachable_by_one_method(self):
        self.assertEqual(self.key.reveal_for_transport(), SECRET)

    def test_the_wrapper_has_no_other_door(self):
        self.assertFalse(hasattr(self.key, '__dict__'))
        with self.assertRaises(TypeError):
            bytes(self.key)
        with self.assertRaises(TypeError):
            len(self.key)
        with self.assertRaises(TypeError):
            iter(self.key)
        with self.assertRaises(TypeError):
            hash(self.key)
        with self.assertRaises(AttributeError):
            self.key.other = 1
        with self.assertRaises(AttributeError):
            del self.key.reveal_for_transport

    def test_equality_compares_keys_and_nothing_else(self):
        self.assertEqual(self.key, ApiKey(SECRET))
        self.assertNotEqual(self.key, ApiKey(SECRET + 'x'))
        self.assertNotEqual(self.key, SECRET)

    def test_bad_values_are_refused_without_echo(self):
        bad = ['', ' ', 'has space', 'tab\there', 'line\nbreak', 'cr\rhere', 'nul\x00x', 'del\x7fx', 'caf\u00e9-key', 'emoji-\U0001F511',
               'x' * (ma.MAX_KEY_CHARS + 1), None, 12345, b'bytes-key', ['sk-list']]
        for value in bad:
            error = refused(self, 'invalid_request', ApiKey, value)
            if isinstance(value, str) and len(value) > 3:
                self.assertNotIn(value, str(error))
            self.assertIsNone(error.__cause__)

    def test_the_longest_key_is_accepted(self):
        self.assertEqual(ApiKey('k' * ma.MAX_KEY_CHARS).reveal_for_transport(), 'k' * ma.MAX_KEY_CHARS)


class TokenBudgetTests(unittest.TestCase):
    def test_the_cap_is_required_and_has_no_default(self):
        with self.assertRaises(TypeError):
            TokenBudget()
        self.assertIs(inspect.signature(TokenBudget).parameters['cap'].default, inspect.Parameter.empty)

    def test_bad_caps_are_refused(self):
        for cap in (0, -1, True, False, 1.5, '100', None, [100], float('inf')):
            refused(self, 'invalid_request', TokenBudget, cap)

    def test_a_cap_of_one_is_accepted(self):
        budget = TokenBudget(1)
        self.assertEqual((budget.cap, budget.spent, budget.remaining, budget.exhausted), (1, 0, 1, False))

    def test_a_large_cap_is_accepted(self):
        self.assertEqual(TokenBudget(10 ** 12).remaining, 10 ** 12)

    def test_repr_shows_the_numbers(self):
        self.assertEqual(repr(TokenBudget(50)), 'TokenBudget(cap=50, spent=0)')

    def test_a_call_adds_its_reported_usage(self):
        budget = TokenBudget(100)
        FakeAdapter(reply(usage=(30, 20))).complete(request(budget))
        self.assertEqual((budget.spent, budget.remaining, budget.exhausted), (50, 50, False))

    def test_spending_exactly_the_cap_refuses_the_next_call_before_any_io(self):
        budget = TokenBudget(50)
        adapter = FakeAdapter(reply(usage=(30, 20)))
        adapter.complete(request(budget))
        self.assertTrue(budget.exhausted)
        self.assertEqual(budget.remaining, 0)
        error = refused(self, 'token_cap_reached', adapter.complete, request(budget))
        self.assertFalse(error.retryable)
        self.assertEqual(adapter.calls, 1)

    def test_one_token_below_the_cap_still_allows_a_call(self):
        budget = TokenBudget(51)
        adapter = FakeAdapter(reply(usage=(30, 20)))
        adapter.complete(request(budget))
        adapter.complete(request(budget))
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(budget.spent, 100)
        self.assertEqual(budget.remaining, 0)

    def test_a_call_may_overshoot_the_cap_and_the_next_is_refused(self):
        budget = TokenBudget(10)
        adapter = FakeAdapter(reply(usage=(500, 500)))
        adapter.complete(request(budget))
        self.assertEqual((budget.spent, budget.remaining), (1000, 0))
        refused(self, 'token_cap_reached', adapter.complete, request(budget))

    def test_a_failed_call_charges_nothing_and_frees_its_reservation(self):
        budget = TokenBudget(100)
        failing = FakeAdapter(error=ModelAdapterError('Slow.', 'timeout'))
        refused(self, 'timeout', failing.complete, request(budget, max_output_tokens=90))
        self.assertEqual(budget.spent, 0)
        FakeAdapter().complete(request(budget, max_output_tokens=90))
        self.assertEqual(budget.spent, 15)

    def test_calls_in_flight_count_against_the_cap(self):
        budget = TokenBudget(100)
        gate = threading.Event()
        adapter = FakeAdapter(gate=gate)
        outcomes = []

        def run():
            try:
                outcomes.append(adapter.complete(request(budget, max_output_tokens=60)))
            except ModelAdapterError as error:
                outcomes.append(error)

        first, second = threading.Thread(target=run), threading.Thread(target=run)
        first.start()
        second.start()
        started = time.monotonic()
        while adapter.calls < 2 and time.monotonic() - started < 5:
            time.sleep(0.01)
        self.assertEqual(adapter.calls, 2)
        # Two calls hold 120 reserved tokens against a cap of 100, so a third is refused before it sends anything.
        refused(self, 'token_cap_reached', adapter.complete, request(budget, max_output_tokens=60))
        self.assertEqual(adapter.calls, 2)
        gate.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(budget.spent, 30)

    def test_many_threads_never_lose_a_charge(self):
        budget = TokenBudget(10 ** 9)
        adapter = FakeAdapter(reply(usage=(1, 1)))
        threads = [threading.Thread(target=lambda: [adapter.complete(request(budget)) for _ in range(50)]) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(budget.spent, 8 * 50 * 2)


class RequestTests(unittest.TestCase):
    def test_roles_are_closed(self):
        self.assertEqual(ma.ROLES, ('system', 'user', 'assistant'))
        for role in ('tool', 'developer', '', None, 'User'):
            refused(self, 'invalid_request', Message, role, 'x')

    def test_text_must_be_text(self):
        for text in (None, 5, b'x', ['x']):
            refused(self, 'invalid_request', Message, 'user', text)

    def test_text_must_be_encodable_and_bounded(self):
        refused(self, 'invalid_request', Message, 'user', 'lone \ud800 surrogate')
        refused(self, 'invalid_request', Message, 'user', 'x' * (ma.MAX_MESSAGE_CHARS + 1))
        self.assertEqual(Message('user', 'x' * ma.MAX_MESSAGE_CHARS).text, 'x' * ma.MAX_MESSAGE_CHARS)

    def test_empty_text_is_refused(self):
        refused(self, 'invalid_request', Message, 'user', '')

    def test_unicode_text_is_accepted(self):
        for text in ('caf\u00e9', '\u4f60\u597d', '\U0001F600 emoji', "O'Brien; DROP TABLE x;--", 'a\u0000b'):
            self.assertEqual(Message('user', text).text, text)

    def test_messages_must_be_a_bounded_non_empty_sequence_of_messages(self):
        budget = TokenBudget(10)
        for messages in ([], (), None, 'text', [{'role': 'user', 'text': 'x'}], [Message('user', 'x'), 'y']):
            refused(self, 'invalid_request', CompletionRequest, messages, 10, budget)
        refused(self, 'invalid_request', CompletionRequest, [Message('user', 'x')] * (ma.MAX_MESSAGES + 1), 10, budget)
        self.assertEqual(len(CompletionRequest([Message('user', 'x')] * ma.MAX_MESSAGES, 10, budget).messages), ma.MAX_MESSAGES)

    def test_the_total_text_is_bounded(self):
        budget = TokenBudget(10)
        one = Message('user', 'x' * ma.MAX_MESSAGE_CHARS)
        refused(self, 'invalid_request', CompletionRequest, [one] * 5, 10, budget)
        CompletionRequest([one] * 4, 10, budget)

    def test_max_output_tokens_is_a_bounded_whole_number(self):
        budget = TokenBudget(10)
        for value in (0, -1, True, 1.5, '10', None, ma.MAX_OUTPUT_TOKENS + 1):
            refused(self, 'invalid_request', CompletionRequest, [Message('user', 'x')], value, budget)
        for value in (1, ma.MAX_OUTPUT_TOKENS):
            self.assertEqual(CompletionRequest([Message('user', 'x')], value, budget).max_output_tokens, value)

    def test_the_budget_must_be_a_token_budget(self):
        for budget in (None, 100, {'cap': 100}):
            refused(self, 'invalid_request', CompletionRequest, [Message('user', 'x')], 10, budget)

    def test_a_list_of_messages_becomes_a_tuple(self):
        self.assertIsInstance(request().messages, tuple)

    def test_the_adapter_refuses_something_that_is_not_a_request(self):
        adapter = FakeAdapter()
        for value in (None, {'messages': []}, 'hi'):
            refused(self, 'invalid_request', adapter.complete, value)
        self.assertEqual(adapter.calls, 0)

    def test_usage_must_be_whole_non_negative_numbers(self):
        for pair in ((-1, 0), (0, -1), (True, 1), (1, False), (1.5, 1), ('1', 1), (None, 1), (10 ** 12 + 1, 0)):
            refused(self, 'bad_response', Usage, *pair)
        self.assertEqual(Usage(0, 0).total, 0)
        self.assertEqual(Usage(3, 4).total, 7)


class CompleteTests(unittest.TestCase):
    def test_the_happy_path(self):
        budget = TokenBudget(1000)
        result = FakeAdapter(reply('Hello there.', usage=(12, 7), stop='end')).complete(request(budget))
        self.assertIsInstance(result, ma.CompletionResult)
        self.assertEqual(result.text, 'Hello there.')
        self.assertEqual(result.usage, Usage(12, 7))
        self.assertEqual(result.stop_reason, 'end')
        self.assertTrue(result.usage_reported)
        self.assertEqual(result.tokens_charged, 19)
        self.assertIsNone(result.notice)

    def test_the_adapter_sees_the_request_it_was_given(self):
        adapter = FakeAdapter()
        call = request(text='Question?')
        adapter.complete(call)
        self.assertIs(adapter.seen[0], call)

    def test_missing_usage_charges_the_request_maximum_and_says_so(self):
        budget = TokenBudget(1000)
        result = FakeAdapter(reply(usage=None)).complete(request(budget, max_output_tokens=250))
        self.assertIsNone(result.usage)
        self.assertFalse(result.usage_reported)
        self.assertEqual(result.tokens_charged, 250)
        self.assertEqual(budget.spent, 250)
        self.assertIn('no usage', result.notice)
        self.assertIn('250', result.notice)

    def test_missing_usage_can_spend_the_budget(self):
        budget = TokenBudget(100)
        adapter = FakeAdapter(reply(usage=None))
        adapter.complete(request(budget, max_output_tokens=100))
        refused(self, 'token_cap_reached', adapter.complete, request(budget))

    def test_stop_reasons_are_closed(self):
        self.assertEqual(ma.STOP_REASONS, ('end', 'length', 'content_filter', 'other'))
        for stop in ma.STOP_REASONS:
            self.assertEqual(FakeAdapter(reply(stop=stop)).complete(request()).stop_reason, stop)
        refused(self, 'bad_response', FakeAdapter(reply(stop='tool_calls')).complete, request())

    def test_a_reply_of_the_wrong_kind_is_a_bad_response_and_costs_nothing(self):
        budget = TokenBudget(1000)
        for bad in ('text', None, {'text': 'x'}, RawCompletion(5, Usage(1, 1), 'end'), RawCompletion('x', (1, 1), 'end')):
            refused(self, 'bad_response', FakeAdapter(bad).complete, request(budget))
        self.assertEqual(budget.spent, 0)

    def test_a_typed_error_from_the_transport_passes_through(self):
        error = ModelAdapterError('Down.', 'endpoint_unreachable')
        raised = refused(self, 'endpoint_unreachable', FakeAdapter(error=error).complete, request())
        self.assertIs(raised, error)

    def test_an_unexpected_exception_becomes_a_clean_typed_error(self):
        key = ApiKey(SECRET)
        adapter = FakeAdapter(error=RuntimeError(f'boom {key.reveal_for_transport()}'), secrets=[key])
        error = refused(self, 'bad_response', adapter.complete, request())
        self.assertNotIn(SECRET, str(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        text = ''.join(traceback.format_exception(error))
        self.assertNotIn(SECRET, text)

    def test_the_key_value_is_removed_from_the_reply_text(self):
        key = ApiKey(SECRET)
        adapter = FakeAdapter(reply(f'Your key is {SECRET}, ok? {SECRET}'), secrets=[key])
        result = adapter.complete(request())
        self.assertNotIn(SECRET, result.text)
        self.assertEqual(result.text, 'Your key is [redacted], ok? [redacted]')

    def test_secrets_must_be_api_keys(self):
        for secrets in (['plain-string'], [None], 'abc'):
            refused(self, 'invalid_request', FakeAdapter, secrets=secrets)

    def test_a_message_with_a_key_shaped_string_is_refused_before_any_io(self):
        adapter = FakeAdapter()
        error = refused(self, 'invalid_request', adapter.complete, request(text='use sk-abcdefghijklmnopqrstuvwxyz0123 please'))
        self.assertEqual(adapter.calls, 0)
        self.assertNotIn('abcdefghijkl', str(error))

    def test_a_message_with_the_adapters_own_key_is_refused(self):
        key = ApiKey('plain-short-key')
        adapter = FakeAdapter(secrets=[key])
        error = refused(self, 'invalid_request', adapter.complete, request(text='the key is plain-short-key'))
        self.assertNotIn('plain-short-key', str(error))
        self.assertEqual(adapter.calls, 0)

    def test_a_message_with_a_path_is_refused_unless_allowed(self):
        refused(self, 'invalid_request', FakeAdapter().complete, request(text='read /etc/passwd'))
        adapter = FakeAdapter(allowed_paths=['/work/module'])
        adapter.complete(request(text='read /work/module/step1.py'))
        refused(self, 'invalid_request', adapter.complete, request(text='read /work/other/step1.py'))
        self.assertEqual(adapter.calls, 1)

    def test_a_refused_message_does_not_reserve_budget(self):
        budget = TokenBudget(100)
        refused(self, 'invalid_request', FakeAdapter().complete, request(budget, text='read /etc/passwd', max_output_tokens=100))
        FakeAdapter().complete(request(budget, max_output_tokens=100))

    def test_the_base_class_has_no_transport(self):
        budget = TokenBudget(100)
        with self.assertRaises(NotImplementedError):
            ma.ModelAdapter().complete(request(budget))
        self.assertEqual(budget.spent, 0)
        FakeAdapter().complete(request(budget, max_output_tokens=99))  # the reservation was freed

    def test_a_result_hides_nothing_it_does_not_hold(self):
        result = FakeAdapter().complete(request())
        self.assertNotIn(SECRET, repr(result))


class ContextGuardTests(unittest.TestCase):
    def check(self, text, **options):
        ma.context_guard([Message('user', text)], **options)

    def assertRefused(self, text, **options):
        error = refused(self, 'invalid_request', self.check, text, **options)
        self.assertNotIn(text.strip()[:30], str(error))
        return error

    def test_plain_teaching_text_passes(self):
        for text in ('Explain what a for loop does.', 'Try the risk-assessment-framework-for-teams example.', 'task-based-learning-objectives-v2',
                     'Use 1/2 and km/h and and/or.', 'See https://example.com/docs/page for details.', 'sha256 digest 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08',
                     'The ratio is 3 / 4.', 'A bare slash / is not a path.', 'Visit mailto:a@b.co or https://example.com/a/b/c.', 'Note:/ is odd but fine here.'):
            with self.subTest(text=text):
                self.check(text)

    def test_key_shapes_are_refused(self):
        shapes = [
            'sk-abcdefghijklmnopqrstuvwxyz012345',
            'sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123',
            'sk-or-v1-abcdefghijklmnopqrstuvwxyz0123',
            'sk_' + 'live_abcdefghijklmnopqrstuvwx',
            'AKIAABCDEFGHIJKLMNOP',
            'ghp_abcdefghijklmnopqrstuvwxyz0123456789',
            'github_pat_abcdefghijklmnopqrstuvwxyz0123',
            'xoxb-123456789012-abcdefghij',
            'AIzaSyA-abcdefghijklmnopqrstuvwxyz01234',
            'hf_abcdefghijklmnopqrstuvwxyz0123456789',
            'gsk_abcdefghijklmnopqrstuvwxyz0123',
            '-----BEGIN RSA PRIVATE KEY-----',
            '-----BEGIN PRIVATE KEY-----',
            'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop',
            'Authorization: Bearer abcdefghijklmnopqrstuvwxyz',
            'api_key = "abcdefghijklmnop1234"',
            'API-KEY: abcdefghijklmnop1234',
            'password=hunter2hunter2hunter2',
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                self.assertRefused(f'Here you go: {shape} thanks')

    def test_a_key_shape_in_any_message_is_refused_and_the_index_is_named(self):
        messages = [Message('system', 'Be kind.'), Message('user', 'Hi'), Message('assistant', 'AKIAABCDEFGHIJKLMNOP')]
        error = refused(self, 'invalid_request', ma.context_guard, messages)
        self.assertIn('3', str(error))

    def test_a_key_shape_split_by_a_newline_is_not_a_match(self):
        self.check('sk-abcdefghijkl\nmnopqrstuvwxyz')  # the guard is a heuristic; the docs say so

    def test_held_keys_are_matched_exactly(self):
        key = ApiKey('short-key-1')
        self.assertRefused('my key: short-key-1', secrets=[key])
        self.check('my key: short-key-2', secrets=[key])

    def test_absolute_paths_are_refused_by_default(self):
        for text in ('/etc/passwd', 'open /Users/student/notes.txt now', '(see /var/log/system.log)', '"/tmp/x"', 'at /usr', 'a:/etc/hosts',
                     'C:\\Users\\student\\notes.txt', 'D:/data/file.txt', '\\\\server\\share\\file', '~/secrets.txt', '~root/.ssh/id_rsa',
                     'file:///etc/passwd', 'file://localhost/etc/passwd'):
            with self.subTest(text=text):
                self.assertRefused(text)

    def test_traversal_is_refused_even_when_a_root_is_allowed(self):
        for text in ('../../etc/passwd', 'go to ../secrets', 'open /work/module/../../etc/passwd', 'a/b/../c', 'x\\..\\y'):
            with self.subTest(text=text):
                self.assertRefused(text, allowed_paths=['/work/module'])

    def test_allowed_paths_pass(self):
        roots = ['/work/module', Path('/data/student')]
        for text in ('/work/module', '/work/module/', '/work/module/a/b.py', 'x /work/module/a.py, y', '/data/student/notes.md'):
            with self.subTest(text=text):
                self.check(text, allowed_paths=roots)

    def test_a_sibling_prefix_is_not_inside_the_root(self):
        self.assertRefused('/work/module-secret/key', allowed_paths=['/work/module'])
        self.assertRefused('/work/modul', allowed_paths=['/work/module'])
        self.assertRefused('/work', allowed_paths=['/work/module'])

    def test_a_windows_path_is_checked_against_a_windows_root(self):
        self.check('open C:\\Work\\Module\\a.py', allowed_paths=['C:\\Work\\Module'])
        self.check('open c:/work/module/a.py', allowed_paths=['C:\\Work\\Module'])
        self.assertRefused('open C:\\Work\\Other\\a.py', allowed_paths=['C:\\Work\\Module'])
        self.assertRefused('open /work/module/a.py', allowed_paths=['C:\\Work\\Module'])

    def test_root_that_is_not_absolute_is_refused(self):
        for root in ('relative/dir', '', '~/x', 5, None):
            refused(self, 'invalid_request', ma.context_guard, [Message('user', 'x')], allowed_paths=[root])

    def test_a_string_in_place_of_a_list_of_roots_is_refused(self):
        refused(self, 'invalid_request', ma.context_guard, [Message('user', 'x')], allowed_paths='/work/module')

    def test_urls_are_not_paths(self):
        self.check('see https://example.com/etc/passwd and http://127.0.0.1:8080/a/b')

    def test_the_guard_checks_every_message(self):
        messages = [Message('user', 'fine'), Message('user', 'also fine'), Message('user', 'read /root/.ssh')]
        refused(self, 'invalid_request', ma.context_guard, messages)

    def test_the_guard_refuses_something_that_is_not_a_message(self):
        refused(self, 'invalid_request', ma.context_guard, ['text'])

    def test_a_huge_message_is_checked_in_bounded_time(self):
        started = time.monotonic()
        self.check('a/' * 20000 + ' ' + '/x' * 20000 + 'z' * 10000, allowed_paths=['/x'])  # no catastrophic backtracking
        self.assertLess(time.monotonic() - started, 5)


if __name__ == '__main__':
    unittest.main()
