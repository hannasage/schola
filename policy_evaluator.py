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
6. The student can mute the agent at any time. A muted state gives no offer, whatever the policy says.

Rules
-----
* Rules are checked in policy order. The first rule that matches and is not suppressed wins. One decision per call.
* A rule that matches but was already answered (an `agent_offer` event with the same rule id and the same trigger event)
  is skipped, so the same offer is not made again for the same trigger.
* The evaluator reads the last `MAX_EVENTS_READ` events of the list. It checks the fields it reads and nothing else.
  A malformed event inside that window raises `EvaluationError` (fail closed).
* The order of the event list is the order of events. Event times are not read.
* Text from events (such as `evidence`) is untrusted. It is compared for equality only. It is never copied into a decision.

Only the Python standard library is used.
"""
import dataclasses
import math
import re

import learning_event

EVENT_TYPES = learning_event.EVENT_TYPES

POLICY_VERSION = 1

# The hint ladder, lowest level first. `tests/test_policy_evaluator.py` checks it against `contracts/lesson-common.v1.schema.json`.
HINT_LEVELS = ('nudge', 'question', 'partial-example', 'full-answer')
# The levels that need a logged attempt. The event contract states the same rule for `hint_requested`.
ATTEMPT_REQUIRED_LEVELS = ('partial-example', 'full-answer')
CHECK_STATUSES = ('passed', 'failed', 'error', 'skipped', 'self-reported')
REVIEW_OUTCOMES = ('correct', 'incorrect')
OFFER_OUTCOMES = ('pending', 'accepted', 'dismissed', 'ignored')

ACTION_TYPES = ('offer_hint', 'ask_recall', 'suggest_review', 'none')
DECISION_ACTIONS = ACTION_TYPES
SCOPES = ('task', 'session', 'module')
# The only conditions a trigger may state, and the events each one fits.
WHERE_KEYS = ('status', 'level', 'outcome')
_WHERE_CHOICES = (
    ('status', 'check_result', CHECK_STATUSES),
    ('level', 'hint_requested', HINT_LEVELS),
    ('outcome', 'review_result', REVIEW_OUTCOMES),
    ('outcome', 'agent_offer', OFFER_OUTCOMES),
)
# The events where "the same result" has a meaning, so `repeat` is allowed.
REPEAT_EVENTS = ('check_result', 'hint_requested', 'review_result', 'attempt_submitted')
# The types that only the app may write (the event contract).
_APP_ONLY_TYPES = ('attempt_submitted', 'check_result', 'task_finished', 'module_completed')
# The types whose `taskId` must be a task id (the event contract).
_TASK_TYPES = ('attempt_submitted', 'check_result', 'hint_requested', 'task_opened', 'task_finished')
_SOURCES = ('student', 'app', 'agent')

SUPPRESSION_CODES = ('muted', 'session_offer_cap', 'min_gap', 'already_offered', 'no_match')

# Sanity limits of the format.
MAX_RULES = 50
MAX_ID_CHARS = 40
MAX_EVENTS_READ = 200
# A count above the window can never be reached, so the format refuses it.
MAX_COUNT = MAX_EVENTS_READ
MAX_FIELD_CHARS = 256
MAX_STATE_ID_CHARS = 64
MAX_NUMBER = 10 ** 15

POLICY_ERROR_CODES = ('not_an_object', 'unknown_key', 'missing_key', 'wrong_type', 'bad_id', 'bad_value', 'out_of_range', 'too_many_rules',
                      'duplicate_rule_id', 'unknown_action', 'unknown_event', 'bad_condition', 'unsupported_version')
EVALUATION_ERROR_CODES = ('bad_policy', 'bad_events', 'bad_event', 'bad_state', 'bad_limits', 'bad_decision')

_FREE_LEVELS = tuple(level for level in HINT_LEVELS if level not in ATTEMPT_REQUIRED_LEVELS)
if HINT_LEVELS[:len(_FREE_LEVELS)] != _FREE_LEVELS or not _FREE_LEVELS:
    raise RuntimeError('The levels that need an attempt must be the top of the hint ladder.')
_RULE_ID = re.compile(r'[a-z0-9][a-z0-9-]*', re.ASCII)


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
    """A plain int or a finite float of sane size. A bool is not a number here."""
    if type(value) is int:
        return -MAX_NUMBER <= value <= MAX_NUMBER
    return type(value) is float and math.isfinite(value) and -MAX_NUMBER <= value <= MAX_NUMBER


# ----------------------------------------------------------------------
# The policy
# ----------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Action:
    """What a rule lets the agent do: offer a hint up to `max_level`, ask a recall question, suggest a review, or do nothing."""
    type: str
    max_level: object = None

    def __post_init__(self):
        if not isinstance(self.type, str):
            raise _policy_error('wrong_type', 'The action type must be text.', '/type')
        if self.type not in ACTION_TYPES:
            raise _policy_error('unknown_action', 'This action type is not allowed.', '/type')
        if self.type == 'offer_hint':
            if self.max_level is None:
                raise _policy_error('missing_key', 'An offer_hint action needs a maxLevel.')
            if not isinstance(self.max_level, str):
                raise _policy_error('wrong_type', 'The maxLevel must be text.', '/maxLevel')
            if self.max_level not in HINT_LEVELS:
                raise _policy_error('bad_value', 'The maxLevel must be a level of the hint ladder.', '/maxLevel')
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
        if not isinstance(self.event, str):
            raise _policy_error('wrong_type', 'The event must be text.', '/event')
        if self.event not in EVENT_TYPES:
            raise _policy_error('unknown_event', 'This event type is not known.', '/event')
        if not isinstance(self.scope, str):
            raise _policy_error('wrong_type', 'The scope must be text.', '/scope')
        if self.scope not in SCOPES:
            raise _policy_error('bad_value', 'The scope must be task, session, or module.', '/scope')
        if not _is_int(self.count):
            raise _policy_error('wrong_type', 'The count must be a whole number.', '/count')
        if not 1 <= self.count <= MAX_COUNT:
            raise _policy_error('out_of_range', f'The count must be from 1 to {MAX_COUNT}.', '/count')
        if not isinstance(self.repeat, bool):
            raise _policy_error('wrong_type', 'The repeat flag must be true or false.', '/repeat')
        if self.repeat and self.event not in REPEAT_EVENTS:
            raise _policy_error('bad_condition', 'Repeat does not fit this event type.', '/repeat')
        self._check_where()

    def _check_where(self):
        if not isinstance(self.where, tuple):
            raise _policy_error('wrong_type', 'The conditions must be a tuple.', '/where')
        seen = []
        for pair in self.where:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise _policy_error('wrong_type', 'Each condition must be a name and a value.', '/where')
            key, value = pair
            if not isinstance(key, str) or key not in WHERE_KEYS or key in seen:
                raise _policy_error('unknown_key', 'This condition is not allowed.', '/where')
            choices = next((allowed for name, event, allowed in _WHERE_CHOICES if name == key and event == self.event), None)
            if choices is None:
                raise _policy_error('bad_condition', 'This condition does not fit this event type.', '/where')
            if not isinstance(value, str):
                raise _policy_error('wrong_type', 'A condition value must be text.', f'/where/{key}')
            if value not in choices:
                raise _policy_error('bad_value', 'This condition value is not allowed.', f'/where/{key}')
            seen.append(key)


def _check_rule_id(value, path):
    if not isinstance(value, str):
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
        if not isinstance(self.trigger, Trigger):
            raise _policy_error('wrong_type', 'The trigger must be a Trigger.', '/trigger')
        if not isinstance(self.action, Action):
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
            if not isinstance(item, Rule):
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
        if not isinstance(key, str) or key not in allowed:
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

def _limit_error(message):
    return EvaluationError(message, 'bad_limits')


@dataclasses.dataclass(frozen=True)
class OfferLimits:
    """The rate limit of offers. Both numbers are required. Schola has no default (an open decision for the owner).

    `max_offers_per_session` is how many offers one session may hold. `min_gap_seconds` is the least time between two offers.
    """
    max_offers_per_session: int
    min_gap_seconds: float

    def __post_init__(self):
        if not _is_int(self.max_offers_per_session) or not 0 <= self.max_offers_per_session <= MAX_NUMBER:
            raise _limit_error('The offer cap must be a whole number of 0 or more.')
        if not _is_number(self.min_gap_seconds) or self.min_gap_seconds < 0:
            raise _limit_error('The minimum gap must be a number of seconds, 0 or more.')


def _state_error(message):
    return EvaluationError(message, 'bad_state')


def _state_id(value, allow_none):
    if value is None:
        return allow_none
    return isinstance(value, str) and 1 <= len(value) <= MAX_STATE_ID_CHARS


@dataclasses.dataclass(frozen=True)
class SessionState:
    """What the caller knows about the session. The caller owns the counters and must update them after each offer.

    `muted` is true when the student muted the agent. `now` is the time in seconds, given by the caller. The evaluator never reads a clock.
    `offers_this_session` counts the offers made in this session. `last_offer_at` is the time of the last offer, or None.
    `task_id`, `session_id`, and `module_id` name the task, session, and module in focus. `task_id` is None when no task is open.
    """
    muted: bool
    now: float
    offers_this_session: int
    last_offer_at: object
    task_id: object
    session_id: str
    module_id: str

    def __post_init__(self):
        if self.muted is not True and self.muted is not False:
            raise _state_error('muted must be true or false.')
        if not _is_number(self.now):
            raise _state_error('now must be a number of seconds.')
        if not _is_int(self.offers_this_session) or not 0 <= self.offers_this_session <= MAX_NUMBER:
            raise _state_error('offers_this_session must be a whole number of 0 or more.')
        if self.last_offer_at is not None and not _is_number(self.last_offer_at):
            raise _state_error('last_offer_at must be a number of seconds or None.')
        if not _state_id(self.task_id, True) or not _state_id(self.session_id, False) or not _state_id(self.module_id, False):
            raise _state_error(f'Each id must be text of 1 to {MAX_STATE_ID_CHARS} characters. Only task_id may be None.')


def _decision_error(message):
    return EvaluationError(message, 'bad_decision')


@dataclasses.dataclass(frozen=True)
class Decision:
    """The answer of `evaluate`: may the agent make an offer, and of what kind?

    `action` is `none`, `offer_hint`, `ask_recall`, or `suggest_review`. `rule_id` is the rule that made the offer. A `none` decision
    names a rule only when that rule says "do nothing". `level` is the highest hint level the agent may offer, and only for `offer_hint`.
    `suppressed_by` holds the reasons a `none` decision came from, from `SUPPRESSION_CODES`. `clamped` is true when the ladder lowered the level.
    `trigger_event_id` is the id of the event that made the rule match. The caller writes it into the `agent_offer` event as `triggerEventId`.
    That is how the next call knows the offer was made. Only an offer has one.
    """
    action: str
    rule_id: object
    level: object
    suppressed_by: tuple
    clamped: bool
    trigger_event_id: object = None

    def __post_init__(self):
        if self.action not in DECISION_ACTIONS or not isinstance(self.action, str):
            raise _decision_error('The decision action is not allowed.')
        if not isinstance(self.suppressed_by, tuple) or any(not isinstance(code, str) or code not in SUPPRESSION_CODES for code in self.suppressed_by):
            raise _decision_error('suppressed_by must be a tuple of known codes.')
        if not isinstance(self.clamped, bool):
            raise _decision_error('clamped must be true or false.')
        if self.rule_id is not None:
            try:
                _check_rule_id(self.rule_id, '')
            except PolicyError:
                raise _decision_error('The rule id is not valid.') from None
        if self.action == 'none':
            if self.level is not None or self.clamped:
                raise _decision_error('A decision to do nothing has no level and is not clamped.')
            if (self.rule_id is None) == (not self.suppressed_by):
                raise _decision_error('A decision to do nothing names either a rule or a reason, not both and not neither.')
            if self.trigger_event_id is not None:
                raise _decision_error('A decision to do nothing has no trigger event.')
            return
        if self.rule_id is None or self.suppressed_by:
            raise _decision_error('An offer names its rule and has no suppression.')
        if not isinstance(self.trigger_event_id, str) or not 1 <= len(self.trigger_event_id) <= MAX_FIELD_CHARS:
            raise _decision_error('An offer names the event that triggered it.')
        if self.action == 'offer_hint':
            if self.level not in HINT_LEVELS:
                raise _decision_error('A hint offer needs a level of the hint ladder.')
        elif self.level is not None or self.clamped:
            raise _decision_error('Only a hint offer has a level or a clamp.')

    def to_dict(self):
        return {'action': self.action, 'ruleId': self.rule_id, 'level': self.level, 'suppressedBy': list(self.suppressed_by), 'clamped': self.clamped,
                'triggerEventId': self.trigger_event_id}


def _no(codes):
    return Decision('none', None, None, codes, False)


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


def _text(value, position, empty_ok=False):
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS or (not value and not empty_ok):
        raise _bad_event(position)
    return value


def _member(value, choices, position):
    if not isinstance(value, str) or value not in choices:
        raise _bad_event(position)
    return value


def _read(event, position):
    """Check the fields that the evaluator reads and return a `_Fact`. Anything else in the event is not looked at."""
    if not isinstance(event, dict):
        raise _bad_event(position)
    for name in ('eventId', 'sessionId', 'moduleId', 'taskId', 'source', 'type', 'data'):
        if name not in event:
            raise _bad_event(position)
    type_ = _member(event['type'], EVENT_TYPES, position)
    source = _member(event['source'], _SOURCES, position)
    if type_ in _APP_ONLY_TYPES and source != 'app':
        raise _bad_event(position)
    task = event['taskId']
    if task is not None or type_ in _TASK_TYPES:
        _text(task, position)
    data = event['data']
    if not isinstance(data, dict):
        raise _bad_event(position)
    fact = _Fact(_text(event['eventId'], position), type_, _text(event['sessionId'], position), _text(event['moduleId'], position), task)
    if type_ == 'check_result':
        check_id = _text(data.get('checkId'), position)
        status = _member(data.get('status'), CHECK_STATUSES, position)
        evidence = _text(data.get('evidence'), position, empty_ok=True)
        fact.filters['status'] = status
        fact.key = (check_id, status, evidence)
    elif type_ == 'attempt_submitted':
        _text(data.get('attemptId'), position)
        fact.key = _text(data.get('artifactDigest'), position)
    elif type_ == 'hint_requested':
        fact.filters['level'] = fact.key = _member(data.get('level'), HINT_LEVELS, position)
    elif type_ == 'review_result':
        skill = _text(data.get('skillId'), position)
        outcome = _member(data.get('outcome'), REVIEW_OUTCOMES, position)
        fact.filters['outcome'] = outcome
        fact.key = (skill, outcome)
    elif type_ == 'agent_offer':
        fact.filters['outcome'] = _member(data.get('outcome'), OFFER_OUTCOMES, position)
        fact.offer = (_text(data.get('policyId'), position), _text(data.get('triggerEventId'), position))
    return fact


def _in_scope(fact, scope, state):
    if scope == 'task':
        return fact.task == state.task_id
    if scope == 'session':
        return fact.session == state.session_id
    return True


def _trigger_events(trigger, facts, state):
    """The events that make `trigger` true, as (event id, position) pairs. An empty tuple means no match.

    One pass over the window. The scope and the module are checked first, then `where`, then the count.
    Without `repeat` the answer is the newest matching event. With `repeat`, events are grouped by their result,
    and the answer holds the newest event of each group that reached the count.
    """
    if trigger.scope == 'task' and state.task_id is None:
        return ()
    total = 0
    newest = None
    groups = {}
    for position, fact in enumerate(facts):
        if fact.type != trigger.event or fact.module != state.module_id or not _in_scope(fact, trigger.scope, state):
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


def _ceiling(facts, state):
    """The highest hint level the ladder allows for the task in focus."""
    if state.task_id is not None:
        for fact in facts:
            if fact.type == 'attempt_submitted' and fact.module == state.module_id and fact.task == state.task_id:
                return HINT_LEVELS[-1]
    return _FREE_LEVELS[-1]


# ----------------------------------------------------------------------
# The decision
# ----------------------------------------------------------------------

def evaluate(policy, events, *, state, limits):
    """Decide whether the agent may make an offer. A pure function: the same inputs give the same decision.

    `policy` is a `TutorPolicy`. `events` is a list or tuple of already validated event dicts, oldest first. `state` is a `SessionState`.
    `limits` is an `OfferLimits`. Nothing passed in is changed. Raises `EvaluationError` for a wrong input or a malformed event in the window.
    """
    if not isinstance(policy, TutorPolicy):
        raise EvaluationError('The policy must be a TutorPolicy.', 'bad_policy')
    if not isinstance(state, SessionState):
        raise EvaluationError('The state must be a SessionState.', 'bad_state')
    if not isinstance(limits, OfferLimits):
        raise EvaluationError('The limits must be OfferLimits.', 'bad_limits')
    if not isinstance(events, (list, tuple)):
        raise EvaluationError('The events must be a list or a tuple.', 'bad_events')
    # Mute wins over everything, so nothing else is read.
    if state.muted:
        return _no(('muted',))
    window = events[-MAX_EVENTS_READ:]
    first = len(events) - len(window)
    facts = [_read(event, first + index) for index, event in enumerate(window)]
    offered = {fact.offer for fact in facts if fact.offer is not None and fact.module == state.module_id}

    skipped = False
    winner = None
    trigger_id = None
    for rule in policy.rules:
        triggers = _trigger_events(rule.trigger, facts, state)
        if not triggers:
            continue
        if rule.action.type == 'none':
            winner = rule
            break
        fresh = [pair for pair in triggers if (rule.id, pair[0]) not in offered]
        if not fresh:
            skipped = True
            continue
        winner, trigger_id = rule, max(fresh, key=lambda pair: pair[1])[0]
        break
    if winner is None:
        return _no(('already_offered',) if skipped else ('no_match',))
    if winner.action.type == 'none':
        return Decision('none', winner.id, None, (), False)

    suppressed = []
    if state.offers_this_session >= limits.max_offers_per_session:
        suppressed.append('session_offer_cap')
    if state.last_offer_at is not None and state.now - state.last_offer_at < limits.min_gap_seconds:
        suppressed.append('min_gap')
    if suppressed:
        return _no(tuple(suppressed))

    if winner.action.type != 'offer_hint':
        return Decision(winner.action.type, winner.id, None, (), False, trigger_id)
    ceiling = _ceiling(facts, state)
    level = min(winner.action.max_level, ceiling, key=HINT_LEVELS.index)
    return Decision('offer_hint', winner.id, level, (), level != winner.action.max_level, trigger_id)
