"""The tutor policy evaluator: may the agent make an offer of help right now?

The engine reads learning events and a module's tutor policy. It decides whether the agent MAY make one offer.
It never writes words, never calls a model, never reads the clock, and never touches the disk or the network.
`docs/policy-evaluator.md` explains the terms, the limits, and what this step does not do.

    policy = TutorPolicy.from_dict(creator_data)          # strict check of untrusted data
    decision = evaluate(policy, events, state=state, limits=limits)

Limits that no policy and no model output can change
-----------------------------------------------------
1. An offer never reveals more than the hint ladder allows. `partial-example` and `full-answer` need a logged
   `attempt_submitted` event for the task in focus. The evaluator lowers (clamps) any higher level.
2. The agent cannot mark a task or a module done. The policy format has no action for it.
3. The agent cannot run a script or approve one. The policy format has no action for it.
4. The agent cannot widen its own permissions. The format is closed: a key or an action that is not listed is an error.
5. Offers are rate limited. `OfferLimits` has two required numbers and no defaults. The values are an open decision for the owner.
   The caller owns the counters (`offers_this_session`, `last_offer_at`) and must update them after each offer.
   The session cap also counts the offers of this session in the window (each policy and trigger pair once), so a counter that is too low does not lift it.
6. The student can mute the agent at any time. A muted state gives no offer, whatever the policy says.

Rules
-----
* Rules are checked in policy order. The first rule that matches and is not suppressed wins. One decision per call.
* A rule skips a trigger event that already has an `agent_offer` from any rule. One trigger event gets one offer.
* A rule counts events from its epoch only. The epoch starts after the latest of: the last offer by this rule, the last passed
  check (for a `check_result` trigger, in the same scope), and the last `task_finished` of the task (for scope `task`).
* The evaluator reads the last `MAX_EVENTS_READ` events of the list. It checks the fields it reads and nothing else.
  A malformed event inside that window raises `EvaluationError` (fail closed).
* The order of the event list is the order of events. Event times are not read.
* Text from events (such as `evidence`) is untrusted. It is compared for equality only. It is never copied into a decision.
  Every id that reaches a decision matches a fixed pattern first.

Only the Python standard library is used.
"""
import dataclasses
import re

import learning_event

EVENT_TYPES = learning_event.EVENT_TYPES
# The events a rule may name as a trigger. An `agent_offer` is the agent's own event, so it cannot trigger an offer.
TRIGGER_EVENT_TYPES = tuple(name for name in EVENT_TYPES if name != 'agent_offer')

POLICY_VERSION = 1

# The hint ladder, lowest level first. `tests/test_policy_evaluator.py` checks it against `contracts/lesson-common.v1.schema.json`.
HINT_LEVELS = ('nudge', 'question', 'partial-example', 'full-answer')
# The levels that need a logged attempt. The event contract states the same rule for `hint_requested`.
ATTEMPT_REQUIRED_LEVELS = ('partial-example', 'full-answer')
# The values of `agent_offer.offeredLevel` (the event contract).
RECALL_LEVEL = 'recall-question'
REVIEW_LEVEL = 'review-suggestion'
OFFER_LEVELS = HINT_LEVELS + (RECALL_LEVEL, REVIEW_LEVEL)
CHECK_STATUSES = ('passed', 'failed', 'error', 'skipped', 'self-reported')
REVIEW_OUTCOMES = ('correct', 'incorrect')
OFFER_OUTCOMES = ('pending', 'accepted', 'dismissed', 'ignored')
CEILING_REASONS = ('no_attempt',)

ACTION_TYPES = ('offer_hint', 'ask_recall', 'suggest_review', 'none')
DECISION_ACTIONS = ACTION_TYPES
SCOPES = ('task', 'session', 'module')
# The only conditions a trigger may state, and the events each one fits.
WHERE_KEYS = ('status', 'level', 'outcome')
_WHERE_CHOICES = (
    ('status', 'check_result', CHECK_STATUSES),
    ('level', 'hint_requested', HINT_LEVELS),
    ('outcome', 'review_result', REVIEW_OUTCOMES),
)
# The events where "the same result" has a meaning, so `repeat` is allowed.
REPEAT_EVENTS = ('check_result', 'hint_requested', 'review_result', 'attempt_submitted')
# The types that only the app may write (the event contract).
_APP_ONLY_TYPES = ('attempt_submitted', 'check_result', 'task_finished', 'module_completed')
# The types whose `taskId` must be a task id (the event contract).
_TASK_TYPES = ('attempt_submitted', 'check_result', 'hint_requested', 'task_opened', 'task_finished')
_SOURCES = ('student', 'app', 'agent')

SUPPRESSION_CODES = ('muted', 'session_offer_cap', 'min_gap', 'already_offered', 'no_match')
_RATE_CODES = ('session_offer_cap', 'min_gap')

# Sanity limits of the format.
MAX_RULES = 50
MAX_ID_CHARS = 40
MAX_EVENTS_READ = 200
# The window holds 200 events and events that do not match use it up, so a high count is rarely reachable. The format stops at 50.
MAX_COUNT = 50
MAX_NUMBER = 10 ** 15

POLICY_ERROR_CODES = ('not_an_object', 'unknown_key', 'missing_key', 'wrong_type', 'bad_id', 'bad_value', 'out_of_range', 'too_many_rules',
                      'duplicate_rule_id', 'unknown_action', 'unknown_event', 'bad_condition', 'unsupported_version')
EVALUATION_ERROR_CODES = ('bad_policy', 'bad_events', 'bad_event', 'bad_state', 'bad_limits', 'bad_decision')

_FREE_LEVELS = tuple(level for level in HINT_LEVELS if level not in ATTEMPT_REQUIRED_LEVELS)
if HINT_LEVELS[:len(_FREE_LEVELS)] != _FREE_LEVELS or not _FREE_LEVELS:
    raise RuntimeError('The levels that need an attempt must be the top of the hint ladder.')

# The id patterns of the event contract (`lesson-common` and `learning-event`). Each is anchored with `fullmatch` and has a bounded size,
# so a match takes linear time. `tests/test_policy_evaluator.py` checks them against the schema files.
_RULE_ID = re.compile(r'[a-z0-9][a-z0-9-]*', re.ASCII)
_LOCAL_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,59}', re.ASCII)
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.ASCII)
_ATTEMPT_ID = re.compile(r'[A-Za-z0-9_-]{1,64}', re.ASCII)
_SKILL_ID = re.compile(r'[a-z][a-z0-9-]{0,39}', re.ASCII)
_DIGEST = re.compile(r'[0-9a-f]{64}', re.ASCII)
_EVIDENCE = re.compile(r"[A-Za-z0-9 .,:;()'_/-]{0,200}", re.ASCII)
_LOCAL_ID_CHARS, _UUID_CHARS, _ATTEMPT_ID_CHARS, _SKILL_ID_CHARS, _DIGEST_CHARS, _EVIDENCE_CHARS = 60, 36, 64, 40, 64, 200


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------

class PolicyEvaluatorError(Exception):
    """An evaluator problem with a stable code and a JSON-pointer-like path. The message is a fixed sentence.

    The message and the path never hold text from the data. A path holds only fixed field names and list positions.
    """
    CODES = ()

    def __init__(self, message, code, path=''):
        if code not in self.CODES:
            raise ValueError('Unknown policy evaluator error code.')
        super().__init__(message)
        self.code = code
        self.path = path

    def payload(self):
        return {'error': str(self), 'code': self.code, 'path': self.path}

    def __reduce__(self):
        return (self.__class__, (str(self), self.code, self.path))


class PolicyError(PolicyEvaluatorError):
    """A tutor policy that does not follow the format."""
    CODES = POLICY_ERROR_CODES


class EvaluationError(PolicyEvaluatorError):
    """An input of `evaluate` that is wrong: a policy, an event, a state, or a limit."""
    CODES = EVALUATION_ERROR_CODES


def _policy_error(code, message, path=''):
    return PolicyError(message, code, path)


def _at(prefix, build, *args):
    """Call `build`. A PolicyError from it gets `prefix` in front of its path."""
    try:
        return build(*args)
    except PolicyError as error:
        raise PolicyError(str(error), error.code, prefix + error.path) from None


def _is_int(value):
    return type(value) is int


def _is_number(value):
    """A plain int or float of sane size. A bool is not a number here. NaN and infinity fail the range test."""
    return type(value) in (int, float) and -MAX_NUMBER <= value <= MAX_NUMBER


def _is_shaped(value, pattern, longest):
    """True for an exact `str` of 1 to `longest` characters that fully matches `pattern`."""
    return type(value) is str and 1 <= len(value) <= longest and pattern.fullmatch(value) is not None


def _canonical(value, choices):
    """The member of `choices` that equals `value`, or None. Only an exact `str` can match, so a rigged `==` is never used."""
    if type(value) is not str:
        return None
    for choice in choices:
        if value == choice:
            return choice
    return None


def _store(instance, name, value):
    """Replace a field of a frozen dataclass during `__post_init__`."""
    object.__setattr__(instance, name, value)


# ----------------------------------------------------------------------
# The policy
# ----------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Action:
    """What a rule lets the agent do: offer a hint up to `max_level`, ask a recall question, suggest a review, or do nothing."""
    type: str
    max_level: object = None

    def __post_init__(self):
        if type(self.type) is not str:
            raise _policy_error('wrong_type', 'The action type must be text.', '/type')
        kind = _canonical(self.type, ACTION_TYPES)
        if kind is None:
            raise _policy_error('unknown_action', 'This action type is not allowed.', '/type')
        _store(self, 'type', kind)
        if kind == 'offer_hint':
            if self.max_level is None:
                raise _policy_error('missing_key', 'An offer_hint action needs a maxLevel.')
            if type(self.max_level) is not str:
                raise _policy_error('wrong_type', 'The maxLevel must be text.', '/maxLevel')
            level = _canonical(self.max_level, HINT_LEVELS)
            if level is None:
                raise _policy_error('bad_value', 'The maxLevel must be a level of the hint ladder.', '/maxLevel')
            _store(self, 'max_level', level)
        elif self.max_level is not None:
            raise _policy_error('unknown_key', 'Only an offer_hint action has a maxLevel.')


@dataclasses.dataclass(frozen=True)
class Trigger:
    """When a rule matches: `count` events of type `event` in `scope`, filtered by `where`, with the same result if `repeat`."""
    event: str
    where: tuple = ()
    count: int = 1
    repeat: bool = False
    scope: str = 'task'

    def __post_init__(self):
        if type(self.event) is not str:
            raise _policy_error('wrong_type', 'The event must be text.', '/event')
        event = _canonical(self.event, TRIGGER_EVENT_TYPES)
        if event is None:
            raise _policy_error('unknown_event', 'This event type is not known or cannot trigger a rule.', '/event')
        _store(self, 'event', event)
        if type(self.scope) is not str:
            raise _policy_error('wrong_type', 'The scope must be text.', '/scope')
        scope = _canonical(self.scope, SCOPES)
        if scope is None:
            raise _policy_error('bad_value', 'The scope must be task, session, or module.', '/scope')
        _store(self, 'scope', scope)
        if event == 'module_completed' and scope == 'task':
            raise _policy_error('bad_condition', 'A module_completed event has no task, so the scope cannot be task.', '/scope')
        if not _is_int(self.count):
            raise _policy_error('wrong_type', 'The count must be a whole number.', '/count')
        if not 1 <= self.count <= MAX_COUNT:
            raise _policy_error('out_of_range', f'The count must be from 1 to {MAX_COUNT}.', '/count')
        if not isinstance(self.repeat, bool):
            raise _policy_error('wrong_type', 'The repeat flag must be true or false.', '/repeat')
        if self.repeat and event not in REPEAT_EVENTS:
            raise _policy_error('bad_condition', 'Repeat does not fit this event type.', '/repeat')
        self._check_where(event)

    def _check_where(self, event):
        if not isinstance(self.where, tuple):
            raise _policy_error('wrong_type', 'The conditions must be a tuple.', '/where')
        seen = []
        stored = []
        for pair in self.where:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise _policy_error('wrong_type', 'Each condition must be a name and a value.', '/where')
            raw_key, value = pair
            key = _canonical(raw_key, WHERE_KEYS)
            if key is None or key in seen:
                raise _policy_error('unknown_key', 'This condition is not allowed.', '/where')
            choices = next((allowed for name, kind, allowed in _WHERE_CHOICES if name == key and kind == event), None)
            if choices is None:
                raise _policy_error('bad_condition', 'This condition does not fit this event type.', '/where')
            if type(value) is not str:
                raise _policy_error('wrong_type', 'A condition value must be text.', f'/where/{key}')
            member = _canonical(value, choices)
            if member is None:
                raise _policy_error('bad_value', 'This condition value is not allowed.', f'/where/{key}')
            seen.append(key)
            stored.append((key, member))
        _store(self, 'where', tuple(stored))


def _check_rule_id(value, path):
    if type(value) is not str:
        raise _policy_error('wrong_type', 'A rule id must be text.', path)
    if not 1 <= len(value) <= MAX_ID_CHARS or not _RULE_ID.fullmatch(value):
        raise _policy_error('bad_id', f'A rule id has 1 to {MAX_ID_CHARS} lowercase letters, digits, or hyphens, and starts with a letter or digit.', path)


@dataclasses.dataclass(frozen=True)
class Rule:
    """One rule: an id, a trigger, and an allowed action. The id is also the `policyId` of an `agent_offer` event."""
    id: str
    trigger: Trigger
    action: Action

    def __post_init__(self):
        _check_rule_id(self.id, '/id')
        if type(self.trigger) is not Trigger:
            raise _policy_error('wrong_type', 'The trigger must be a Trigger.', '/trigger')
        if type(self.action) is not Action:
            raise _policy_error('wrong_type', 'The action must be an Action.', '/action')


@dataclasses.dataclass(frozen=True)
class TutorPolicy:
    """A checked tutor policy: a tuple of rules in the order the creator wrote them."""
    rules: tuple

    def __post_init__(self):
        if not isinstance(self.rules, tuple):
            raise _policy_error('wrong_type', 'The rules must be a tuple.', '/rules')
        if len(self.rules) > MAX_RULES:
            raise _policy_error('too_many_rules', f'A policy may hold at most {MAX_RULES} rules.', '/rules')
        ids = set()
        for index, item in enumerate(self.rules):
            if type(item) is not Rule:
                raise _policy_error('wrong_type', 'Each rule must be a Rule.', f'/rules/{index}')
            if item.id in ids:
                raise _policy_error('duplicate_rule_id', 'Each rule id must be unique in a policy.', f'/rules/{index}/id')
            ids.add(item.id)

    @classmethod
    def from_dict(cls, data):
        """Check untrusted creator data and return a frozen policy. A dict is read and never changed.

        Raises `PolicyError` with a stable code and a path. The data is a dict with `version` (1) and `rules` (a list).
        """
        body = _closed(data, ('version', 'rules'), ('version', 'rules'), '')
        version = body['version']
        if not _is_int(version):
            raise _policy_error('wrong_type', 'The version must be a whole number.', '/version')
        if version != POLICY_VERSION:
            raise _policy_error('unsupported_version', f'This reader supports policy version {POLICY_VERSION} only.', '/version')
        rules = body['rules']
        if not isinstance(rules, list):
            raise _policy_error('wrong_type', 'The rules must be a list.', '/rules')
        if len(rules) > MAX_RULES:
            raise _policy_error('too_many_rules', f'A policy may hold at most {MAX_RULES} rules.', '/rules')
        built = tuple(_rule_from_dict(item, f'/rules/{index}') for index, item in enumerate(rules))
        return _at('', cls, built)


def _closed(data, allowed, required, path):
    """A dict with only allowed text keys and all required keys. Errors point at the object, never at a key."""
    if not isinstance(data, dict):
        raise _policy_error('not_an_object', 'This must be an object.', path)
    for key in data:
        if type(key) is not str or key not in allowed:
            raise _policy_error('unknown_key', 'This object has a key that is not allowed.', path)
    for key in required:
        if key not in data:
            raise _policy_error('missing_key', 'This object lacks a required key.', path)
    return data


def _rule_from_dict(data, path):
    body = _closed(data, ('id', 'trigger', 'action'), ('id', 'trigger', 'action'), path)
    _check_rule_id(body['id'], f'{path}/id')
    trigger = _trigger_from_dict(body['trigger'], f'{path}/trigger')
    action = _action_from_dict(body['action'], f'{path}/action')
    return _at(path, Rule, body['id'], trigger, action)


def _trigger_from_dict(data, path):
    body = _closed(data, ('event', 'where', 'count', 'repeat', 'scope'), ('event',), path)
    where = ()
    if 'where' in body:
        conditions = _closed(body['where'], WHERE_KEYS, (), f'{path}/where')
        where = tuple((key, conditions[key]) for key in WHERE_KEYS if key in conditions)
    options = {name: body[name] for name in ('count', 'repeat', 'scope') if name in body}
    return _at(path, lambda: Trigger(body['event'], where=where, **options))


def _action_from_dict(data, path):
    body = _closed(data, ('type', 'maxLevel'), ('type',), path)
    return _at(path, Action, body['type'], body.get('maxLevel'))


# ----------------------------------------------------------------------
# Limits, state, and the decision
# ----------------------------------------------------------------------

def _check_limits(limits):
    """Check the numbers of an `OfferLimits`. Also run by `evaluate`, so a value forced in later is caught."""
    cap, gap = limits.max_offers_per_session, limits.min_gap_seconds
    if not _is_int(cap) or not 0 <= cap <= MAX_NUMBER:
        raise EvaluationError('The offer cap must be a whole number of 0 or more.', 'bad_limits')
    if not _is_number(gap) or gap < 0:
        raise EvaluationError('The minimum gap must be a number of seconds, 0 or more.', 'bad_limits')


@dataclasses.dataclass(frozen=True)
class OfferLimits:
    """The rate limit of offers. Both numbers are required. Schola has no default (an open decision for the owner).

    `max_offers_per_session` is how many offers one session may hold. `min_gap_seconds` is the least time between two offers.
    """
    max_offers_per_session: int
    min_gap_seconds: float

    def __post_init__(self):
        _check_limits(self)


def _check_state(state):
    """Check the values of a `SessionState`. Also run by `evaluate`, so a value forced in later is caught."""
    def bad(message):
        return EvaluationError(message, 'bad_state')

    if state.muted is not True and state.muted is not False:
        raise bad('muted must be true or false.')
    if not _is_number(state.now) or state.now < 0:
        raise bad('now must be a number of seconds, 0 or more.')
    if not _is_int(state.offers_this_session) or not 0 <= state.offers_this_session <= MAX_NUMBER:
        raise bad('offers_this_session must be a whole number of 0 or more.')
    last = state.last_offer_at
    if last is not None and (not _is_number(last) or last < 0 or last > state.now):
        raise bad('last_offer_at must be None or a number of seconds from 0 up to now.')
    if state.task_id is not None and not _is_shaped(state.task_id, _LOCAL_ID, _LOCAL_ID_CHARS):
        raise bad('task_id must be None or a task id.')
    if not _is_shaped(state.session_id, _UUID, _UUID_CHARS):
        raise bad('session_id must be a lowercase UUID.')
    if not _is_shaped(state.module_id, _LOCAL_ID, _LOCAL_ID_CHARS):
        raise bad('module_id must be a module id.')


@dataclasses.dataclass(frozen=True)
class SessionState:
    """What the caller knows about the session. The caller owns the counters and must update them after each offer.

    `muted` is true when the student muted the agent. `now` is the time in seconds, given by the caller. The evaluator never reads a clock.
    `offers_this_session` counts the offers made in this session. `last_offer_at` is the time of the last offer, or None.
    It cannot be later than `now`. `task_id`, `session_id`, and `module_id` name the task, session, and module in focus.
    `task_id` is None when no task is open.
    """
    muted: bool
    now: float
    offers_this_session: int
    last_offer_at: object
    task_id: object
    session_id: str
    module_id: str

    def __post_init__(self):
        _check_state(self)


def _decision_error(message):
    return EvaluationError(message, 'bad_decision')


def _rule_id_or_none(value):
    if value is not None:
        try:
            _check_rule_id(value, '')
        except PolicyError:
            raise _decision_error('The rule id is not valid.') from None


def _event_id_or_none(value):
    if value is not None and not _is_shaped(value, _UUID, _UUID_CHARS):
        raise _decision_error('An event id must be a lowercase UUID.')


@dataclasses.dataclass(frozen=True)
class Decision:
    """The answer of `evaluate`: may the agent make an offer, and of what kind?

    `action` is `none`, `offer_hint`, `ask_recall`, or `suggest_review`. `rule_id` is the rule that made the offer. A `none` decision names a
    rule only when that rule says "do nothing". `level` is the highest hint level the agent may offer, and only for `offer_hint`.
    `suppressed_by` holds the reasons a `none` decision came from, from `SUPPRESSION_CODES`. `clamped` is true when the ladder lowered the level.

    For an offer, the caller writes an `agent_offer` event from these fields: `rule_id` is the `policyId`, `trigger_event_id` is the `triggerEventId`,
    `offered_level` is the `offeredLevel`, and the outcome is `pending`. `requested_level` is what the rule asked for. `ceiling_reason` is
    `no_attempt` when the ladder lowered the level because the task has no logged attempt.

    When the session cap or the minimum gap stopped a rule that matched, `suppressed_rule_id` and `suppressed_trigger_event_id` name that rule
    and its trigger event. `events_read` is the number of events the evaluator read. `truncated` is true when the list was longer than the window.
    """
    action: str
    rule_id: object
    level: object
    suppressed_by: tuple
    clamped: bool
    trigger_event_id: object = None
    events_read: int = 0
    truncated: bool = False
    suppressed_rule_id: object = None
    suppressed_trigger_event_id: object = None
    requested_level: object = None
    offered_level: object = None
    ceiling_reason: object = None

    def __post_init__(self):
        if _canonical(self.action, DECISION_ACTIONS) is None:
            raise _decision_error('The decision action is not allowed.')
        if not isinstance(self.suppressed_by, tuple) or any(_canonical(code, SUPPRESSION_CODES) is None for code in self.suppressed_by):
            raise _decision_error('suppressed_by must be a tuple of known codes.')
        if not isinstance(self.clamped, bool) or not isinstance(self.truncated, bool):
            raise _decision_error('clamped and truncated must be true or false.')
        if not _is_int(self.events_read) or not 0 <= self.events_read <= MAX_EVENTS_READ:
            raise _decision_error('events_read must be a whole number from 0 to the window size.')
        _rule_id_or_none(self.rule_id)
        _rule_id_or_none(self.suppressed_rule_id)
        _event_id_or_none(self.trigger_event_id)
        _event_id_or_none(self.suppressed_trigger_event_id)
        if self.action == 'none':
            self._check_none()
        else:
            self._check_offer()

    def _check_none(self):
        if self.clamped or any(value is not None for value in (self.level, self.trigger_event_id, self.requested_level, self.offered_level,
                                                                 self.ceiling_reason)):
            raise _decision_error('A decision to do nothing has no level, no trigger event, and no clamp.')
        if (self.rule_id is None) == (not self.suppressed_by):
            raise _decision_error('A decision to do nothing names either a rule or a reason, not both and not neither.')
        if (self.suppressed_rule_id is None) != (self.suppressed_trigger_event_id is None):
            raise _decision_error('A suppressed rule and its trigger event come together.')
        if self.suppressed_rule_id is not None and (self.rule_id is not None or not self.suppressed_by
                                                    or any(code not in _RATE_CODES for code in self.suppressed_by)):
            raise _decision_error('Only the rate limit names the rule it stopped.')

    def _check_offer(self):
        if self.rule_id is None or self.suppressed_by or self.suppressed_rule_id is not None or self.suppressed_trigger_event_id is not None:
            raise _decision_error('An offer names its rule and has no suppression.')
        if self.trigger_event_id is None:
            raise _decision_error('An offer names the event that triggered it.')
        if self.action == 'offer_hint':
            level, asked = _canonical(self.level, HINT_LEVELS), _canonical(self.requested_level, HINT_LEVELS)
            if level is None or asked is None or HINT_LEVELS.index(level) > HINT_LEVELS.index(asked):
                raise _decision_error('A hint offer needs a level of the hint ladder, not above the level asked for.')
            if self.clamped != (level != asked) or self.offered_level != level:
                raise _decision_error('The clamp flag and the offered level must follow from the levels.')
            if self.ceiling_reason != ('no_attempt' if self.clamped else None):
                raise _decision_error('The ceiling reason must name the clamp.')
            return
        expected = RECALL_LEVEL if self.action == 'ask_recall' else REVIEW_LEVEL
        if self.level is not None or self.requested_level is not None or self.clamped or self.ceiling_reason is not None or self.offered_level != expected:
            raise _decision_error('Only a hint offer has a level or a clamp.')

    def to_dict(self):
        return {'action': self.action, 'ruleId': self.rule_id, 'level': self.level, 'suppressedBy': list(self.suppressed_by), 'clamped': self.clamped,
                'triggerEventId': self.trigger_event_id, 'eventsRead': self.events_read, 'truncated': self.truncated,
                'suppressedRuleId': self.suppressed_rule_id, 'suppressedTriggerEventId': self.suppressed_trigger_event_id,
                'requestedLevel': self.requested_level, 'offeredLevel': self.offered_level, 'ceilingReason': self.ceiling_reason}


# ----------------------------------------------------------------------
# Reading events
# ----------------------------------------------------------------------

class _Fact:
    """The few fields of one event that the evaluator reads."""
    __slots__ = ('id', 'type', 'session', 'module', 'task', 'filters', 'key', 'offer')

    def __init__(self, event_id, type_, session, module, task):
        self.id, self.type, self.session, self.module, self.task = event_id, type_, session, module, task
        self.filters = {}
        self.key = None
        self.offer = None


def _bad_event(position):
    return EvaluationError('An event in the window is malformed.', 'bad_event', f'/events/{position}')


def _shaped(value, pattern, longest, position):
    # The length check is a fast refusal of huge text. Each pattern also bounds its own length.
    if type(value) is not str or len(value) > longest or pattern.fullmatch(value) is None:
        raise _bad_event(position)
    return value


def _member(value, choices, position):
    member = _canonical(value, choices)
    if member is None:
        raise _bad_event(position)
    return member


def _read(event, position):
    """Check the fields that the evaluator reads and return a `_Fact`. Anything else in the event is not looked at."""
    if not isinstance(event, dict):
        raise _bad_event(position)
    for name in ('eventId', 'sessionId', 'moduleId', 'taskId', 'source', 'type', 'data'):
        if name not in event:
            raise _bad_event(position)
    type_ = _member(event['type'], EVENT_TYPES, position)
    source = _member(event['source'], _SOURCES, position)
    # Only the agent writes an `agent_offer`, and the agent writes nothing else.
    if (type_ == 'agent_offer') != (source == 'agent') or (type_ in _APP_ONLY_TYPES and source != 'app'):
        raise _bad_event(position)
    task = event['taskId']
    if task is None:
        if type_ in _TASK_TYPES:
            raise _bad_event(position)
    elif type_ == 'module_completed':
        raise _bad_event(position)
    else:
        _shaped(task, _LOCAL_ID, _LOCAL_ID_CHARS, position)
    data = event['data']
    if not isinstance(data, dict):
        raise _bad_event(position)
    fact = _Fact(_shaped(event['eventId'], _UUID, _UUID_CHARS, position), type_, _shaped(event['sessionId'], _UUID, _UUID_CHARS, position),
                 _shaped(event['moduleId'], _LOCAL_ID, _LOCAL_ID_CHARS, position), task)
    if type_ == 'check_result':
        check_id = _shaped(data.get('checkId'), _LOCAL_ID, _LOCAL_ID_CHARS, position)
        status = _member(data.get('status'), CHECK_STATUSES, position)
        evidence = _shaped(data.get('evidence'), _EVIDENCE, _EVIDENCE_CHARS, position)
        fact.filters['status'] = status
        fact.key = (check_id, status, evidence)
    elif type_ == 'attempt_submitted':
        _shaped(data.get('attemptId'), _ATTEMPT_ID, _ATTEMPT_ID_CHARS, position)
        fact.key = _shaped(data.get('artifactDigest'), _DIGEST, _DIGEST_CHARS, position)
    elif type_ == 'hint_requested':
        fact.filters['level'] = fact.key = _member(data.get('level'), HINT_LEVELS, position)
    elif type_ == 'review_result':
        skill = _shaped(data.get('skillId'), _SKILL_ID, _SKILL_ID_CHARS, position)
        outcome = _member(data.get('outcome'), REVIEW_OUTCOMES, position)
        fact.filters['outcome'] = outcome
        fact.key = (skill, outcome)
    elif type_ == 'agent_offer':
        _member(data.get('outcome'), OFFER_OUTCOMES, position)
        fact.offer = (_shaped(data.get('policyId'), _LOCAL_ID, _LOCAL_ID_CHARS, position),
                      _shaped(data.get('triggerEventId'), _UUID, _UUID_CHARS, position))
    return fact


def _in_scope(fact, scope, state):
    if scope == 'task':
        return fact.task == state.task_id
    if scope == 'session':
        return fact.session == state.session_id
    return True


def _trigger_events(rule, facts, state):
    """The events that make the rule's trigger true, as (event id, position) pairs. An empty tuple means no match.

    One pass over the window. Only events of the module in focus count. The rule counts from its epoch: the count restarts
    after an offer by this rule, after a passed check (for a `check_result` trigger that does not count passed checks, in the same scope),
    and after the end of the task in focus (for scope `task`, unless the trigger counts `task_finished` events).
    Without `repeat` the answer is the newest matching event. With `repeat`, events are grouped by their result,
    and the answer holds the newest event of each group that reached the count.
    """
    trigger = rule.trigger
    if trigger.scope == 'task' and state.task_id is None:
        return ()
    ends_on_pass = trigger.event == 'check_result' and ('status', 'passed') not in trigger.where
    ends_on_finish = trigger.scope == 'task' and trigger.event != 'task_finished'
    total = 0
    newest = None
    groups = {}
    for position, fact in enumerate(facts):
        if fact.module != state.module_id:
            continue
        if fact.type == 'agent_offer':
            if fact.offer[0] == rule.id:
                total, newest, groups = 0, None, {}
            continue
        if (ends_on_pass and fact.type == 'check_result' and fact.filters['status'] == 'passed' and _in_scope(fact, trigger.scope, state)) or \
                (ends_on_finish and fact.type == 'task_finished' and fact.task == state.task_id):
            total, newest, groups = 0, None, {}
            continue
        if fact.type != trigger.event or not _in_scope(fact, trigger.scope, state):
            continue
        if any(fact.filters.get(name) != value for name, value in trigger.where):
            continue
        total += 1
        newest = (fact.id, position)
        if trigger.repeat:
            seen = groups.get(fact.key)
            groups[fact.key] = (1 if seen is None else seen[0] + 1, fact.id, position)
    if not trigger.repeat:
        return (newest,) if total >= trigger.count else ()
    return tuple((group[1], group[2]) for group in groups.values() if group[0] >= trigger.count)


def _has_attempt(facts, state):
    """True when the window holds an attempt for the task in focus. The focus task sets the ladder, not the trigger's task."""
    if state.task_id is None:
        return False
    return any(fact.type == 'attempt_submitted' and fact.module == state.module_id and fact.task == state.task_id for fact in facts)


# ----------------------------------------------------------------------
# The decision
# ----------------------------------------------------------------------

def evaluate(policy, events, *, state, limits):
    """Decide whether the agent may make an offer. A pure function: the same inputs give the same decision.

    `policy` is a `TutorPolicy`. `events` is a list or tuple of already validated event dicts, oldest first. `state` is a `SessionState`.
    `limits` is an `OfferLimits`. Nothing passed in is changed. Raises `EvaluationError` for a wrong input or a malformed event in the window.
    The types of `policy`, `state`, and `limits` must be exactly the classes of this module.
    """
    if type(policy) is not TutorPolicy:
        raise EvaluationError('The policy must be a TutorPolicy.', 'bad_policy')
    if type(state) is not SessionState:
        raise EvaluationError('The state must be a SessionState.', 'bad_state')
    if type(limits) is not OfferLimits:
        raise EvaluationError('The limits must be OfferLimits.', 'bad_limits')
    if not isinstance(events, (list, tuple)):
        raise EvaluationError('The events must be a list or a tuple.', 'bad_events')
    truncated = len(events) > MAX_EVENTS_READ
    # Mute wins over everything, so nothing else is read. Any value but False counts as muted.
    if state.muted is not False:
        return Decision('none', None, None, ('muted',), False, truncated=truncated)
    _check_state(state)
    _check_limits(limits)
    window = events[-MAX_EVENTS_READ:]
    first = len(events) - len(window)
    facts = [_read(event, first + index) for index, event in enumerate(window)]
    ids = set()
    for index, fact in enumerate(facts):
        if fact.id in ids:
            raise _bad_event(first + index)
        ids.add(fact.id)
    meta = {'events_read': len(facts), 'truncated': truncated}
    # One offer per trigger event, from any rule. The session count is the larger of the caller's counter and the offers in the window.
    offered_triggers = {fact.offer[1] for fact in facts if fact.offer is not None and fact.module == state.module_id}
    # An outcome (accepted, dismissed, ignored) is a new `agent_offer` event for the same policy and trigger, so count each pair once.
    session_offers = len({fact.offer for fact in facts if fact.offer is not None and fact.module == state.module_id and fact.session == state.session_id})

    skipped = False
    winner = None
    trigger_id = None
    for rule in policy.rules:
        triggers = _trigger_events(rule, facts, state)
        if not triggers:
            continue
        if rule.action.type == 'none':
            winner = rule
            break
        fresh = [pair for pair in triggers if pair[0] not in offered_triggers]
        if not fresh:
            skipped = True
            continue
        winner, trigger_id = rule, max(fresh, key=lambda pair: pair[1])[0]
        break
    if winner is None:
        return Decision('none', None, None, ('already_offered',) if skipped else ('no_match',), False, **meta)
    if winner.action.type == 'none':
        return Decision('none', winner.id, None, (), False, **meta)

    suppressed = []
    if max(state.offers_this_session, session_offers) >= limits.max_offers_per_session:
        suppressed.append('session_offer_cap')
    if state.last_offer_at is not None and state.now - state.last_offer_at < limits.min_gap_seconds:
        suppressed.append('min_gap')
    if suppressed:
        return Decision('none', None, None, tuple(suppressed), False, suppressed_rule_id=winner.id, suppressed_trigger_event_id=trigger_id, **meta)

    if winner.action.type == 'ask_recall':
        return Decision('ask_recall', winner.id, None, (), False, trigger_id, offered_level=RECALL_LEVEL, **meta)
    if winner.action.type == 'suggest_review':
        return Decision('suggest_review', winner.id, None, (), False, trigger_id, offered_level=REVIEW_LEVEL, **meta)
    asked = winner.action.max_level
    ceiling = HINT_LEVELS[-1] if _has_attempt(facts, state) else _FREE_LEVELS[-1]
    level = min(asked, ceiling, key=HINT_LEVELS.index)
    clamped = level != asked
    return Decision('offer_hint', winner.id, level, (), clamped, trigger_id, requested_level=asked, offered_level=level,
                    ceiling_reason='no_attempt' if clamped else None, **meta)
