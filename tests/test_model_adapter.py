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
from unittest import mock

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


def wait_for(condition, seconds=5):
    started = time.monotonic()
    while not condition():
        if time.monotonic() - started > seconds:
            raise AssertionError('condition not reached')
        time.sleep(0.005)


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

    def test_an_error_survives_pickling(self):
        error = ModelAdapterError('Slow.', 'timeout', retryable=False)
        restored = pickle.loads(pickle.dumps(error))
        self.assertEqual((str(restored), restored.code, restored.retryable), ('Slow.', 'timeout', False))

    def test_the_typescript_type_matches_the_payload(self):
        import re
        text = (ROOT / 'types' / 'model-adapter.d.ts').read_text(encoding='utf-8')
        codes = re.findall(r"^\s+\| '([a-z_]+)'", text, re.M)
        self.assertEqual(tuple(codes), ma.ERROR_CODES)
        body = text.split('interface ModelAdapterErrorPayload {')[1].split('}')[0]
        keys = re.findall(r'^\s+([a-z_]+): ', body, re.M)
        self.assertEqual(keys, list(ModelAdapterError('x', 'timeout').payload()))

    def test_payload_has_the_message_the_code_retryable_and_the_tokens_charged(self):
        error = ModelAdapterError('Slow.', 'timeout')
        self.assertEqual(error.payload(), {'error': 'Slow.', 'code': 'timeout', 'retryable': True, 'tokens_charged': 0})
        self.assertEqual(str(error), 'Slow.')
        self.assertEqual(ModelAdapterError('x', 'bad_response', tokens_charged=7).payload()['tokens_charged'], 7)

    def test_tokens_charged_is_a_whole_non_negative_number(self):
        self.assertEqual(ModelAdapterError('x', 'timeout').tokens_charged, 0)
        for value in (-1, True, 1.5, '3', None):
            with self.assertRaises(ValueError):
                ModelAdapterError('x', 'timeout', tokens_charged=value)

    def test_tokens_charged_survives_pickling(self):
        restored = pickle.loads(pickle.dumps(ModelAdapterError('x', 'bad_response', retryable=True, tokens_charged=42)))
        self.assertEqual((restored.tokens_charged, restored.retryable), (42, True))


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

    def test_init_cannot_run_a_second_time(self):
        refused(self, 'invalid_request', self.key.__init__, 'another-key-value')
        self.assertEqual(self.key.reveal_for_transport(), SECRET)

    def test_the_reduce_methods_refuse_when_called_directly(self):
        with self.assertRaises(TypeError):
            self.key.__reduce__()
        with self.assertRaises(TypeError):
            self.key.__reduce_ex__(2)

    def test_the_stored_value_cannot_be_replaced_or_deleted(self):
        with self.assertRaises(AttributeError):
            self.key._ApiKey__value = 'other-value'
        with self.assertRaises(AttributeError):
            del self.key._ApiKey__value
        self.assertEqual(self.key.reveal_for_transport(), SECRET)

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
        FakeAdapter(reply(usage=(30, 20))).complete(request(budget, max_output_tokens=40))
        self.assertEqual((budget.spent, budget.remaining, budget.exhausted), (50, 50, False))

    def test_the_input_estimate_is_the_characters_over_three_rounded_up(self):
        for chars, estimate in ((1, 1), (3, 1), (4, 2), (300, 100), (301, 101)):
            with self.subTest(chars=chars):
                budget = TokenBudget(1000)
                gate = threading.Event()
                adapter = FakeAdapter(gate=gate)
                thread = threading.Thread(target=adapter.complete, args=(request(budget, text='x' * chars, max_output_tokens=50),))
                thread.start()
                wait_for(lambda: adapter.calls == 1)
                self.assertEqual(budget.remaining, 1000 - 50 - estimate)
                gate.set()
                thread.join(5)

    def test_the_estimate_counts_every_message(self):
        budget = TokenBudget(1000)
        gate = threading.Event()
        adapter = FakeAdapter(gate=gate)
        call = CompletionRequest([Message('system', 'x' * 30), Message('user', 'y' * 31)], 50, budget)
        thread = threading.Thread(target=adapter.complete, args=(call,))
        thread.start()
        wait_for(lambda: adapter.calls == 1)
        self.assertEqual(budget.remaining, 1000 - 50 - 21)  # 61 characters: 21 tokens
        gate.set()
        thread.join(5)

    def test_remaining_and_exhausted_count_calls_in_flight(self):
        budget = TokenBudget(100)
        gate = threading.Event()
        adapter = FakeAdapter(gate=gate)
        thread = threading.Thread(target=adapter.complete, args=(request(budget, text='abc', max_output_tokens=99),))
        thread.start()
        wait_for(lambda: adapter.calls == 1)
        self.assertEqual((budget.spent, budget.remaining, budget.exhausted), (0, 0, True))
        gate.set()
        thread.join(5)
        self.assertEqual((budget.spent, budget.remaining, budget.exhausted), (15, 85, False))

    def test_spending_exactly_the_cap_refuses_the_next_call_before_any_io(self):
        budget = TokenBudget(50)
        adapter = FakeAdapter(reply(usage=(30, 20)))
        adapter.complete(request(budget, max_output_tokens=40))
        self.assertTrue(budget.exhausted)
        self.assertEqual(budget.remaining, 0)
        error = refused(self, 'token_cap_reached', adapter.complete, request(budget, max_output_tokens=1))
        self.assertFalse(error.retryable)
        self.assertEqual(adapter.calls, 1)

    def test_a_call_that_cannot_fit_is_refused_before_any_io(self):
        budget = TokenBudget(1000)
        adapter = FakeAdapter()
        refused(self, 'token_cap_reached', adapter.complete, request(budget, max_output_tokens=200000))
        self.assertEqual((adapter.calls, budget.spent, budget.remaining), (0, 0, 1000))

    def test_a_call_that_just_fits_is_allowed_and_one_more_token_is_not(self):
        budget = TokenBudget(100)
        adapter = FakeAdapter()
        refused(self, 'token_cap_reached', adapter.complete, request(budget, text='abc', max_output_tokens=100))
        adapter.complete(request(budget, text='abc', max_output_tokens=99))  # 1 input + 99 output = 100
        self.assertEqual(adapter.calls, 1)
        budget = TokenBudget(100)
        refused(self, 'token_cap_reached', adapter.complete, request(budget, text='abcd', max_output_tokens=99))  # 2 + 99 = 101
        self.assertEqual(adapter.calls, 1)

    def test_a_call_may_overshoot_the_cap_when_the_provider_counts_more_and_the_next_is_refused(self):
        budget = TokenBudget(100)
        adapter = FakeAdapter(reply(usage=(500, 500)))
        adapter.complete(request(budget, max_output_tokens=50))
        self.assertEqual((budget.spent, budget.remaining), (1000, 0))
        refused(self, 'token_cap_reached', adapter.complete, request(budget, max_output_tokens=1))

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
                outcomes.append(adapter.complete(request(budget, max_output_tokens=40)))
            except ModelAdapterError as error:
                outcomes.append(error)

        first, second = threading.Thread(target=run), threading.Thread(target=run)
        first.start()
        second.start()
        wait_for(lambda: adapter.calls == 2)
        # Two calls hold 84 tokens of a cap of 100, so a third (42 tokens) is refused before it sends anything.
        refused(self, 'token_cap_reached', adapter.complete, request(budget, max_output_tokens=40))
        self.assertEqual(adapter.calls, 2)
        gate.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(budget.spent, 30)

    def test_twenty_concurrent_calls_cannot_pass_the_cap(self):
        budget = TokenBudget(1000)
        gate = threading.Event()
        adapter = FakeAdapter(reply(usage=(2, 100)), gate=gate)  # the provider reports what was reserved: 2 + 100
        outcomes = []

        def run():
            try:
                outcomes.append(adapter.complete(request(budget, max_output_tokens=100)))
            except ModelAdapterError as error:
                outcomes.append(error)

        threads = [threading.Thread(target=run) for _ in range(20)]
        for thread in threads:
            thread.start()
        wait_for(lambda: len(outcomes) == 11)  # 9 calls fit (9 x 102 = 918); the other 11 are refused at once
        self.assertEqual(adapter.calls, 9)
        self.assertTrue(all(isinstance(outcome, ModelAdapterError) and outcome.code == 'token_cap_reached' for outcome in outcomes))
        gate.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(outcomes), 20)
        self.assertEqual(budget.spent, 918)
        self.assertLessEqual(budget.spent, budget.cap)

    def test_every_change_to_the_counters_holds_the_lock(self):
        class Checked(TokenBudget):
            def __setattr__(self, name, value):
                if name in ('_spent', '_reserved') and '_lock' in self.__dict__:
                    assert self._lock.locked(), f'{name} changed without the lock'
                super().__setattr__(name, value)

        budget = Checked(1000)
        FakeAdapter().complete(request(budget))  # begin, then finish
        refused(self, 'timeout', FakeAdapter(error=ModelAdapterError('x', 'timeout')).complete, request(budget))  # begin, then abort
        self.assertEqual(budget.spent, 15)

    def test_many_threads_never_lose_a_charge(self):
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, previous)
        budget = TokenBudget(10 ** 9)
        adapter = FakeAdapter(reply(usage=(1, 1)))
        barrier = threading.Barrier(8)
        problems = []

        def work():
            try:
                barrier.wait(10)
                for _ in range(200):
                    adapter.complete(request(budget))
            except BaseException as error:
                problems.append(error)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(problems, [])
        self.assertEqual(budget.spent, 8 * 200 * 2)
        self.assertEqual(budget._reserved, 0)


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
        adapter.complete(request(budget, max_output_tokens=98))
        self.assertEqual(budget.spent, 98)
        refused(self, 'token_cap_reached', adapter.complete, request(budget, max_output_tokens=1))

    def test_zero_usage_next_to_text_is_not_free(self):
        for usage in ((0, 0),):
            budget = TokenBudget(1000)
            result = FakeAdapter(reply('Some text.', usage=usage)).complete(request(budget, max_output_tokens=250))
            self.assertFalse(result.usage_reported)
            self.assertIsNone(result.usage)
            self.assertEqual((result.tokens_charged, budget.spent), (250, 250))
            self.assertIn('no usage', result.notice)

    def test_zero_usage_next_to_no_text_is_reported_as_it_is(self):
        budget = TokenBudget(1000)
        result = FakeAdapter(reply('', usage=(0, 0), stop='length')).complete(request(budget, max_output_tokens=250))
        self.assertTrue(result.usage_reported)
        self.assertEqual((result.tokens_charged, budget.spent), (0, 0))

    def test_an_error_charges_the_tokens_it_carries(self):
        budget = TokenBudget(1000)
        error = ModelAdapterError('Odd.', 'bad_response', tokens_charged=30)
        raised = refused(self, 'bad_response', FakeAdapter(error=error).complete, request(budget))
        self.assertIs(raised, error)
        self.assertEqual((budget.spent, budget._reserved), (30, 0))

    def test_a_retry_loop_on_billed_errors_ends_at_the_cap(self):
        budget = TokenBudget(1000)
        adapter = FakeAdapter(error=ModelAdapterError('Odd.', 'bad_response', retryable=True, tokens_charged=100))
        outcome = None
        for _ in range(50):
            try:
                adapter.complete(request(budget))
            except ModelAdapterError as error:
                outcome = error
                if error.code == 'token_cap_reached':
                    break
        self.assertEqual(outcome.code, 'token_cap_reached')
        self.assertEqual((adapter.calls, budget.spent), (9, 900))

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
        text = ''.join(traceback.TracebackException(type(error), error, error.__traceback__, capture_locals=True).format())
        self.assertNotIn(SECRET, text)

    def test_the_key_value_is_removed_from_the_reply_text(self):
        key = ApiKey(SECRET)
        adapter = FakeAdapter(reply(f'Your key is {SECRET}, ok? {SECRET}'), secrets=[key])
        result = adapter.complete(request())
        self.assertNotIn(SECRET, result.text)
        self.assertEqual(result.text, 'Your key is [redacted], ok? [redacted]')
        self.assertTrue(result.redacted)
        self.assertIn('key', result.notice)
        self.assertIn('redacted', result.notice)

    def test_a_clean_reply_is_not_marked_redacted(self):
        result = FakeAdapter(secrets=[ApiKey(SECRET)]).complete(request())
        self.assertFalse(result.redacted)
        self.assertIsNone(result.notice)

    def test_both_notices_are_kept(self):
        result = FakeAdapter(reply(f'key {SECRET}', usage=None), secrets=[ApiKey(SECRET)]).complete(request())
        self.assertIn('no usage', result.notice)
        self.assertIn('redacted', result.notice)

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
        refused(self, 'invalid_request', FakeAdapter().complete, request(budget, text='read /etc/passwd', max_output_tokens=90))
        FakeAdapter().complete(request(budget, max_output_tokens=90))

    def test_an_interrupt_frees_the_reservation_and_passes_through(self):
        budget = TokenBudget(100)
        with self.assertRaises(KeyboardInterrupt):
            FakeAdapter(error=KeyboardInterrupt()).complete(request(budget, max_output_tokens=90))
        self.assertEqual((budget.spent, budget._reserved), (0, 0))
        FakeAdapter().complete(request(budget, max_output_tokens=90))

    def test_an_interrupt_between_the_reservation_and_the_send_frees_it(self):
        budget = TokenBudget(1000)
        original = TokenBudget._begin

        def begin_then_interrupt(self, reserve, hold):
            original(self, reserve, hold)
            raise KeyboardInterrupt()
        with mock.patch.object(TokenBudget, '_begin', begin_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                FakeAdapter().complete(request(budget))
        self.assertEqual(budget._reserved, 0)

    def test_memory_errors_pass_through_and_free_the_reservation(self):
        budget = TokenBudget(1000)
        with self.assertRaises(MemoryError):
            FakeAdapter(error=MemoryError()).complete(request(budget))
        self.assertEqual((budget.spent, budget._reserved), (0, 0))

    def test_only_the_missing_transport_raises_not_implemented_through(self):
        class Odd(ma.ModelAdapter):
            def _send(self, call):
                raise NotImplementedError('from a subclass')
        budget = TokenBudget(1000)
        error = refused(self, 'bad_response', Odd().complete, request(budget))
        self.assertEqual(budget._reserved, 0)
        self.assertNotIn('subclass', str(error))

    def test_an_unexpected_exception_is_logged_without_the_key(self):
        key = ApiKey(SECRET)
        adapter = FakeAdapter(error=RuntimeError(f'boom {SECRET} and more'), secrets=[key])
        with self.assertLogs('schola.model_adapter', 'ERROR') as logs:
            refused(self, 'bad_response', adapter.complete, request())
        text = '\n'.join(logs.output)
        self.assertIn('RuntimeError', text)
        self.assertIn('Traceback', text)
        self.assertNotIn(SECRET, text)
        self.assertIn('[redacted]', text)

    def test_the_retryable_flag_of_an_unexpected_exception_is_kept(self):
        class Flaky(Exception):
            retryable = True
        with self.assertLogs('schola.model_adapter', 'ERROR'):
            self.assertTrue(refused(self, 'bad_response', FakeAdapter(error=Flaky()).complete, request()).retryable)
            self.assertFalse(refused(self, 'bad_response', FakeAdapter(error=RuntimeError()).complete, request()).retryable)

    def test_a_reply_that_only_looks_like_a_raw_completion_is_refused(self):
        import types
        look_alike = types.SimpleNamespace(text='x', usage=None, stop_reason='end')
        refused(self, 'bad_response', FakeAdapter(look_alike).complete, request())
        usage_like = RawCompletion('x', types.SimpleNamespace(total=5), 'end')
        refused(self, 'bad_response', FakeAdapter(usage_like).complete, request())

    def test_bad_allowed_paths_are_refused_when_the_adapter_is_made(self):
        for paths in ('/', ['relative'], [''], ('/ok', 'bad'), {'/ok': 1}.keys(), (path for path in ['/ok']), None):
            with self.subTest(paths=paths):
                refused(self, 'invalid_request', FakeAdapter, allowed_paths=paths)

    def test_the_base_class_has_no_transport(self):
        budget = TokenBudget(100)
        with self.assertRaises(NotImplementedError):
            ma.ModelAdapter().complete(request(budget, max_output_tokens=50))
        self.assertEqual(budget.spent, 0)
        FakeAdapter().complete(request(budget, max_output_tokens=90))  # the reservation was freed

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
                     'The ratio is 3 / 4.', 'A bare slash / is not a path.', 'Visit mailto:a@b.co or https://example.com/a/b/c.', 'Note:/ is odd but fine here.',
                     '\u0438/\u0438\u043b\u0438 and \u65e5\u672c/\u4e2d\u56fd', 'Close it with </div> and <br/> and <p>text</p>.', 'Wait.. what? 0..10 and x...',
                     'A basic internationalization guide.', 'secret: the quick brown fox', 'token = next_token_value', 'My password is long.',
                     'Use the tokens: one, two', 'Try [1..5] and a[0:1].', 'See https://example.com/etc/passwd.'):
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
            'AWS_SECRET_ACCESS_KEY=abcdefghij0123456789klmnop',
            'MISTRAL_API_KEY=abcdefghij0123456789klmnop',
            'DB_PASSWORD = "abcdefghij0123456789"',
            'client_secret: abcdefghij0123456789',
            'access_token=abcdefghij0123456789',
            'GITHUB_TOKEN=abcdefghij0123456789',
            '{"apiKey": "abcdefghij0123456789"}',
            "{'password': 'abcdefghij0123456789'}",
            'postgres://user:pa55word@db.example.com/app',
            '-----BEGIN PGP PRIVATE KEY BLOCK-----',
            'npm_' + 'abcdefghijklmnopqrstuvwxyz0123456789',
            'glpat-abcdefghijklmnopqrst',
            'SG.' + 'abcdefghijklmnop.abcdefghijklmnop',
            'ya29.' + 'abcdefghijklmnopqrstuvwx',
            'whsec_' + 'abcdefghijklmnopqrstuv',
            'sb_secret_abcdefghijklmnop',
            'Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ=',
            'SK-abcdefghijklmnopqrstuvwxyz012345',
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                self.assertRefused(f'Here you go: {shape} thanks')

    def test_a_key_shape_in_any_message_is_refused_and_the_index_is_named(self):
        messages = [Message('system', 'Be kind.'), Message('user', 'Hi'), Message('assistant', 'AKIAABCDEFGHIJKLMNOP')]
        error = refused(self, 'invalid_request', ma.context_guard, messages)
        self.assertIn('3', str(error))

    def test_a_key_shape_hidden_by_spaces_new_lines_or_invisible_characters_is_still_found(self):
        for text in ('sk-abcdefghijkl\nmnopqrstuvwxyz', 'sk- abcdefghij klmnopqrstuvwxyz', 'sk-abcdefghij\u200bklmnopqrstuvwxyz', 's\u200bk-abcdefghijklmnopqrstuvwxyz',
                     '\uff53\uff4b-abcdefghijklmnopqrstuvwxyz', 'AKIA ABCD EFGH IJKL MNOP', 'gh\u2060p_abcdefghijklmnopqrstuvwxyz0123456789',
                     'ghp_abcdefghijklmn\n opqrstuvwxyz0123456789', 'pass\u200bword=abcdefghij0123456789'):
            with self.subTest(text=text):
                self.assertRefused(text)

    def test_a_keyword_value_without_a_digit_is_not_a_secret(self):
        for text in ('password=abcdefghijklmnopqrstuvwx', 'api_key: abcdefghijklmnopqrstuvwx', 'token = abcdefghij_klmnopqrst'):
            with self.subTest(text=text):
                self.check(text)

    def test_a_short_keyword_value_is_not_a_secret(self):
        self.check('password=abc123')

    def test_held_keys_are_matched_exactly(self):
        key = ApiKey('short-key-1')
        self.assertRefused('my key: short-key-1', secrets=[key])
        self.check('my key: short-key-2', secrets=[key])

    def test_a_held_key_is_found_when_it_is_split_or_hidden(self):
        key = ApiKey('short-key-12345')
        for text in ('short-\u200bkey-12345', 'short-key- 12345', 'short-key-\n12345', '\uff53hort-key-12345'):
            with self.subTest(text=text):
                self.assertRefused(text, secrets=[key])

    def test_a_held_key_is_found_across_messages(self):
        key = ApiKey('short-key-12345')
        messages = [Message('user', 'my key is short-key-'), Message('user', '12345 ok')]
        error = refused(self, 'invalid_request', ma.context_guard, messages, secrets=[key])
        self.assertNotIn('short-key', str(error))
        messages = [Message('user', 'short-'), Message('user', '\n'), Message('user', 'key-'), Message('user', '12345')]
        refused(self, 'invalid_request', ma.context_guard, messages, secrets=[key])
        ma.context_guard([Message('user', 'short-'), Message('user', 'other')], secrets=[key])

    def test_absolute_paths_are_refused_by_default(self):
        for text in ('/etc/passwd', 'open /Users/student/notes.txt now', '(see /var/log/system.log)', '"/tmp/x"', 'at /usr', 'a:/etc/hosts',
                     'C:\\Users\\student\\notes.txt', 'D:/data/file.txt', '\\\\server\\share\\file', '~/secrets.txt', '~root/.ssh/id_rsa',
                     'file:///etc/passwd', 'file://localhost/etc/passwd'):
            with self.subTest(text=text):
                self.assertRefused(text)

    def test_path_forms_that_slipped_past_the_first_guard_are_refused(self):
        for text in ('//etc/passwd', '///etc/passwd', 'file:/etc/passwd', 'FILE:/etc/passwd', 'File:/etc/passwd', 'see:/etc/passwd', 'path:/etc/shadow',
                     'cat </etc/passwd', 'x>/etc/passwd', '<code>/etc/passwd</code>', 'x -/etc/passwd', 'cd ..', 'go .. now', 'cd ..\n', '$HOME/.ssh/id_rsa',
                     '${HOME}/.ssh/id_rsa', '$PWD/x', '//server/share/x', 'a=/etc/passwd', 'x,/etc/passwd', 'x;/etc/passwd', '[/etc/passwd]',
                     '{"p":"/etc/passwd"}', 'x|/etc/passwd', 'x&/etc/passwd', '%USERPROFILE%\\notes', '%HOME%/notes', 'cp a /etc/x', '(/etc/passwd)',
                     'a:\\b', 'x\u2028/etc/passwd', '\uff0fetc\uff0fpasswd', "'/etc/passwd'", '`/etc/passwd`', '~/a', 'x ~root/y',
                     '\u041f\u0440\u0438\u0432\u0435\u0442 /etc/passwd'):
            with self.subTest(text=text):
                self.assertRefused(text)

    def test_roots_of_everything_are_refused(self):
        for root in ('/', '//', '/.', '/a/..', 'C:\\', 'c:/', 'D:\\.'):
            with self.subTest(root=root):
                refused(self, 'invalid_request', ma.context_guard, [Message('user', 'x')], allowed_paths=[root])
        ma.context_guard([Message('user', 'x')], allowed_paths=['C:\\Work', '/a'])

    def test_a_root_with_a_space_lets_its_paths_through(self):
        roots = ['/Users/x/Sage Advice/y']
        for text in ('read /Users/x/Sage Advice/y/file.txt now', 'read /Users/x/Sage Advice/y', 'in "/Users/x/Sage Advice/y/a b/c.py".',
                     'see /Users/x/Sage Advice/y/sub/file, then stop', '/Users/x/Sage Advice/y.'):
            with self.subTest(text=text):
                self.check(text, allowed_paths=roots)
        for text in ('read /Users/x/Sage Advice/other/file', 'read /Users/x/Sage Advice/yy/file', 'read /Users/x/Sage Advice/y/../z',
                     'read /Users/x/Sage Advice/y/file and /etc/passwd', 'read /Users/x/Sage', 'read /Users/x/Sage Advice/y-1/file',
                     'read /Users/x/sage advice/y/file', 'read /Users/x/Sage Advice'):
            with self.subTest(text=text):
                self.assertRefused(text, allowed_paths=roots)

    def test_a_windows_root_with_a_space_lets_its_paths_through(self):
        roots = ['C:\\Program Files\\Schola']
        self.check('open C:\\Program Files\\Schola\\a.txt now', allowed_paths=roots)
        self.check('open c:/program files/schola/a.txt now', allowed_paths=roots)
        self.assertRefused('open C:\\Program Files\\Other\\a.txt', allowed_paths=roots)

    def test_text_in_other_scripts_is_not_a_path(self):
        for text in ('\u0438/\u0438\u043b\u0438', '\u65e5\u672c/\u4e2d\u56fd', '\u0627/\u0628', 'caf\u00e9/th\u00e9', 'x\u0438/y'):
            with self.subTest(text=text):
                self.check(text)

    def test_the_scan_has_a_length_cap(self):
        refused(self, 'invalid_request', ma.context_guard, [Message('user', 'a' * ma.MAX_MESSAGE_CHARS)] * 5)
        ma.context_guard([Message('user', 'a' * ma.MAX_MESSAGE_CHARS)] * 4)
        refused(self, 'invalid_request', ma.context_guard, [Message('user', 'a')] * (ma.MAX_MESSAGES + 1))

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
        for text in ('/work/module', '/', b'/work'):
            refused(self, 'invalid_request', ma.context_guard, [Message('user', 'x')], allowed_paths=text)

    def test_roots_must_be_a_list_a_tuple_or_a_set(self):
        for roots in ((root for root in ['/work']), {'/work': 1}, None, 5):
            refused(self, 'invalid_request', ma.context_guard, [Message('user', 'x')], allowed_paths=roots)
        for roots in (['/work'], ('/work',), {'/work'}, frozenset({'/work'})):
            self.check('see /work/a', allowed_paths=roots)

    def test_secrets_must_be_api_keys_in_the_guard_too(self):
        refused(self, 'invalid_request', ma.context_guard, [Message('user', 'x')], secrets=['plain'])

    def test_the_path_error_names_the_message_number(self):
        messages = [Message('user', 'fine'), Message('user', 'also fine'), Message('user', 'read /root/.ssh')]
        error = refused(self, 'invalid_request', ma.context_guard, messages)
        self.assertIn('Message 3 ', str(error))
        messages = [Message('user', 'fine'), Message('user', 'AKIAABCDEFGHIJKLMNOP')]
        self.assertIn('Message 2 ', str(refused(self, 'invalid_request', ma.context_guard, messages)))

    def test_unc_paths_ignore_case(self):
        self.check('open \\\\server\\share\\a.txt', allowed_paths=['\\\\Server\\Share'])
        self.assertRefused('open \\\\server\\other\\a.txt', allowed_paths=['\\\\Server\\Share'])

    def test_a_file_url_drops_its_host(self):
        self.check('see file://localhost/work/module/a.py and file:///work/module/b.py', allowed_paths=['/work/module'])
        self.assertRefused('see file://localhost/etc/passwd', allowed_paths=['/work/module'])
        self.assertRefused('see file://', allowed_paths=['/work/module'])

    def test_a_trailing_full_stop_is_not_part_of_the_path(self):
        self.check('The folder is /work/module.', allowed_paths=['/work/module'])
        self.check('Is it /work/module? Yes: /work/module!', allowed_paths=['/work/module'])

    def test_double_slash_comments_are_not_paths(self):
        for text in ('x = 1 //comment', '//comment', 'a //b //c', 'see ///three'):
            with self.subTest(text=text):
                self.check(text)

    def test_a_url_with_dot_dot_is_not_a_traversal(self):
        self.check('see https://example.com/a/../b for details')
        self.assertRefused('see file:///work/module/../../etc/passwd', allowed_paths=['/work/module'])

    def test_urls_are_not_paths(self):
        self.check('see https://example.com/etc/passwd and http://127.0.0.1:8080/a/b')

    def test_the_guard_checks_every_message(self):
        messages = [Message('user', 'fine'), Message('user', 'also fine'), Message('user', 'read /root/.ssh')]
        refused(self, 'invalid_request', ma.context_guard, messages)

    def test_the_guard_refuses_something_that_is_not_a_message(self):
        refused(self, 'invalid_request', ma.context_guard, ['text'])

    def test_a_huge_message_is_checked_in_bounded_time(self):
        started = time.monotonic()
        self.check('a/' * 20000 + ' ' + 'z' * 10000, allowed_paths=['/x'])  # no catastrophic backtracking
        self.assertRefused('/x' * 3000, allowed_paths=['/x'])  # a path of 6000 characters is longer than any real path
        self.check('/x' * 2000, allowed_paths=['/x'])
        self.assertLess(time.monotonic() - started, 5)

    def test_hostile_text_is_scanned_in_linear_time(self):
        size = 100_000
        texts = {
            'letters': 'a' * size, 'keyword': 'token=' * 16666, 'jwt': 'eyJaaaaaaaa-' * 8333, 'underscores': '_' * size, 'prefix': 'sk-' * 33333,
            'slashes': '/' * size, 'segments': 'a/' * 50000, 'spaces': ' ' * size, 'bearer': 'bearer ' * 14285, 'dots': '.' * size,
            'file urls': 'file://' * 14285, 'schemes': 'a://' * 25000, 'tildes': '~' * size, 'backslashes': '\\' * size, 'secret words': 'secret_' * 14285,
            'aws': 'AKIA' * 25000, 'sendgrid': 'SG.' * 33333, 'dotdot': '../' * 33333, 'drives': 'x:/' * 33333, 'colons': ':/' * 50000,
            'keyword value': 'token=' + 'a' * (size - 6), 'password mix': 'a_password=' * 9000, 'unc': '\\\\a' * 25000, 'unicode': '\u0438/' * 50000,
            'zero width': 's\u200b' * 50000, 'credentials': 'x://a:' * 16666, 'pem': '-----BEGIN ' * 9000, 'basic': 'authorization: basic ' * 4700,
            'angle': '<' * size, 'html': '</div>' * 16666, 'long run': 'a-' * 50000, 'newlines': '\n' * size, 'mixed': 'sk-a/ token= ~ \\\\x:/' * 4000,
        }
        for name, text in texts.items():
            with self.subTest(name=name):
                started = time.process_time()  # CPU time, so a busy machine does not fail the test
                try:
                    ma.context_guard([Message('user', text[:ma.MAX_MESSAGE_CHARS])])
                except ModelAdapterError:
                    pass
                self.assertLess(time.process_time() - started, 1.0, name)


if __name__ == '__main__':
    unittest.main()
