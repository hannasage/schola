# The tutor policy evaluator

This page describes step E5. It covers the tutor policy evaluator. This part of Schola gates the offers of help that the agent makes.
It says what the code does, what it refuses, and what it does not prove.
Read `docs/model-adapters.md` first for the other half of the engine. Read `contracts/learning-events.md` for the events that this code reads.

The code is `policy_evaluator.py`. The tests are `tests/test_policy_evaluator.py`.
The code uses only the Python standard library. It needs Python 3.10 or later.
Nothing calls the evaluator yet. No route, screen, or agent loop uses it in this step. The agent loop is step E6.

## What this step does and does not do

The evaluator decides. It answers one question: is the agent allowed to make an offer of help right now, and of what kind?

It does not:

- write any words. The agent writes the offer later, in step E6.
- call a model.
- use the network, the disk, or the clock. The caller passes in the time.
- store anything. It keeps no counters. The caller owns them (see "What the caller must do").
- read the policy from a file. A later step adds the policy contract file and the place where a module keeps its policy.
- check that the events are valid. It trusts that `learning_event.py` checked them. It checks only the fields it reads.

## Terms

- A tutor policy is a list of rules written by the module creator. It is data, not code.
- A rule has an id, a trigger, and an action.
- A trigger says which events make a rule apply. It names an event type. A trigger has four optional parts: a condition, a count, a repeat flag, and a scope.
- An action says what the agent is allowed to do after a rule applies. The list of actions is closed.
- An offer is one message of help from the agent. The student accepts it or dismisses it.
- The hint ladder is the ordered list of hint levels: `nudge`, `question`, `partial-example`, `full-answer`. A higher level reveals more.
- An attempt is work that the student submitted for a task. The app logs it as an `attempt_submitted` event.
- To clamp a level is to lower it to the highest level that the ladder allows. The decision records the clamp.
- Muted means the student switched the agent off.
- A window is the part of the event list that the evaluator reads.

## Use

```python
policy = TutorPolicy.from_dict(creator_data)             # raises PolicyError on any fault
decision = evaluate(
    policy, events,                                      # events: a list of validated event dicts, oldest first
    state=SessionState(muted=False, now=1000.0, offers_this_session=0, last_offer_at=None,
                       task_id='t1', session_id='...', module_id='m1'),
    limits=OfferLimits(max_offers_per_session=..., min_gap_seconds=...),   # both required
)
decision.action          # 'none', 'offer_hint', 'ask_recall', or 'suggest_review'
decision.level           # the highest hint level the agent is allowed to offer, for 'offer_hint' only
decision.to_dict()       # the same data as plain JSON values, with camelCase names
```

`evaluate` is a pure function. The same inputs give the same decision. It changes none of its inputs.

## The policy format

A policy is a JSON object. The reader takes a Python dict.
When a caller parses text, the caller refuses an object that repeats a key. `learning_event.py` does this for events.

```json
{
  "version": 1,
  "rules": [
    {
      "id": "two-same-fails",
      "trigger": {"event": "check_result", "where": {"status": "failed"}, "count": 2, "repeat": true, "scope": "task"},
      "action": {"type": "offer_hint", "maxLevel": "question"}
    }
  ]
}
```

Every object is closed. A key that is not listed here is an error.
The error names the object that holds the key. It never repeats the key, because the key is untrusted text.

- `version` is the whole number 1.
- `rules` is a list of at most 50 rules. It is allowed to be empty. An empty policy never offers anything.
- `id` has 1 to 40 characters: lowercase ASCII letters, digits, and hyphens. It starts with a letter or a digit. Each id is unique in the policy.
  The first idea also allowed capitals and `_`. The pattern is narrower now, because the id becomes the `policyId` of an `agent_offer` event. That field has this pattern.
- A rule has no label and no free text. Nothing in a policy holds a sentence.

### Triggers

- `event` is required. It is one of the ten event types of the event contract.
- `scope` is `task`, `session`, or `module`. The default is `task`. It says which events count:
  - `task`: events of the task in focus. When no task is in focus, the rule never matches.
  - `session`: events of the session in focus.
  - `module`: events of the whole module, across sessions and tasks.
  Events of any other module never count, in every scope.
- `count` is a whole number from 1 to 200. The default is 1. When this many events count, the rule matches.
  The cap is the size of the window. A larger count is never reached.
- `where` is an optional condition. The list is closed, and each condition fits only some events:
  - `status` fits `check_result`: `passed`, `failed`, `error`, `skipped`, or `self-reported`.
  - `level` fits `hint_requested`: a level of the hint ladder.
  - `outcome` fits `review_result` (`correct` or `incorrect`) and `agent_offer` (`pending`, `accepted`, `dismissed`, or `ignored`).
  A condition means "equals" and nothing else. There is no pattern, no expression, and no code.
- `repeat` is `true` or `false`. The default is `false`. With `true`, the `count` events share the same result.
  What "the same result" means depends on the event:
  - `check_result`: the same check id, the same status, and the same `evidence` text.
  - `hint_requested`: the same level.
  - `review_result`: the same skill and the same outcome.
  - `attempt_submitted`: the same work (the same digest).
  Other event types have no result, so `repeat: true` on them is an error.
  The events do not need to be next to each other.

### Actions

The list is closed. Any other `type` is an error.

- `{"type": "offer_hint", "maxLevel": <level>}`: the agent is allowed to offer a hint up to this level.
- `{"type": "ask_recall"}`: the agent is allowed to ask a recall question.
- `{"type": "suggest_review"}`: the agent is allowed to suggest a review.
- `{"type": "none"}`: do nothing. When this rule matches, it wins and stops the search. A creator uses it to keep the agent quiet in one situation.

## The six limits

The spec says the engine enforces six limits. No policy and no model output changes them. This is how each one holds.

1. **An offer never reveals more than the ladder allows.** The evaluator lowers `maxLevel` to the highest level whose condition holds for the task in focus.
   `nudge` and `question` need nothing. `partial-example` and `full-answer` need a logged `attempt_submitted` event for that task, in the same module.
   When no task is in focus, there is no attempt. A `hint_requested` event never counts as an attempt.
   The evaluator sets the `clamped` field to true after it lowers a level.
   The clamp runs on every hint decision. The policy has no key to switch it off.
2. **The agent cannot mark a task or a module done.** The policy format has no such action and no such key. A policy that tries one is a `PolicyError`.
3. **The agent cannot run a script or approve one.** The same holds: the format has no way to say it.
4. **The agent cannot widen its own permissions.** Every object is closed. The limits, the mute, and the ladder are not in the policy.
   A model cannot edit a policy, because the evaluator takes a checked, frozen object.
5. **Offers are rate limited.** `OfferLimits` has two required numbers and no defaults: `max_offers_per_session` and `min_gap_seconds`.
   The values are an open decision for the owner. The evaluator counts nothing itself. It reads `offers_this_session` and `last_offer_at` from the state.
6. **Mute always works.** A muted state makes the evaluator return `none` with the reason `muted`. It reads nothing else.
   This happens before the events are read. No event, policy, or limit changes it.

## The decision

`Decision` is a frozen object with these fields:

- `action`: `none`, `offer_hint`, `ask_recall`, or `suggest_review`.
- `rule_id`: the rule that made the offer. Only a matching `none` rule sets it in a `none` decision.
- `level`: for `offer_hint` only. It is the highest level that the agent is allowed to offer.
- `suppressed_by`: a tuple of reasons for a `none` decision.
- `clamped`: true after the ladder lowered the level, false otherwise.
- `trigger_event_id`: for an offer only. It is the id of the event that made the rule match.
  The first design had no such field. The caller needs it to write the `agent_offer` event, and the repeat guard below needs that event.

The reasons in `suppressed_by` come from a closed list:

- `muted`: the student muted the agent. It wins over everything.
- `session_offer_cap`: the session already holds `max_offers_per_session` offers.
- `min_gap`: the last offer is less than `min_gap_seconds` old. A last-offer time in the future also counts as too recent.
- `already_offered`: a rule matched, but the agent already made an offer for that rule and that trigger event.
- `no_match`: no rule matched.

When both limits stop an offer, the reasons come in this order: `session_offer_cap`, then `min_gap`.

### How the evaluator picks a rule

1. A muted state returns `none` at once.
2. The evaluator checks the rules in the order the creator wrote them. The first rule that matches and is not already answered wins. One decision per call.
3. When the winning rule is a `none` rule, that is the answer.
4. Otherwise the evaluator applies the rate limit. When a limit stops the offer, the answer is `none` with the reasons.
5. A hint offer is clamped last.

The evaluator skips a rule that is already answered, and goes on to the next rule.
Look at the window for an `agent_offer` event of the same module. That event has the rule's id as `policyId` and the trigger event as `triggerEventId`. Then the rule is "already answered".
This keeps the agent from making the same offer again for the same two failures. A new failure is a new trigger event, so a new offer follows.
This check reads offers from the events. The rate limit does not. The rate limit reads the counters in the state.

## What the caller must do

The agent loop (step E6) owns these duties. The evaluator cannot do them.

- Call `evaluate` after each new event, with the events oldest first.
- Keep `offers_this_session` and `last_offer_at`. After the agent makes an offer, add one to the count and set the time. The evaluator never changes them.
- Pass the right `now`. The evaluator never reads a clock.
- Write an `agent_offer` event for each offer. Use `rule_id` as `policyId` and `trigger_event_id` as `triggerEventId`. Without it, the same offer repeats.
- Pass the real mute state. A caller that forgets the mute defeats limit 6.
- Treat `level` as a ceiling, not as a command. The agent is free to offer less.
- Pick the limit values. They have no default.

## The window and the events

- The evaluator reads the last 200 events of the list (`MAX_EVENTS_READ`). It does not read older ones.
  An attempt or a failure older than the window does not count. This errs on the safe side for the ladder: a missed attempt lowers the level.
- The order of the list is the order of events. The evaluator does not read event times, so a list in the wrong order gives wrong counts.
- For each event in the window, the evaluator checks the fields it reads: the ids, the type, the source, the task id, and a few `data` fields.
  An event that fails this check raises `EvaluationError` with the code `bad_event`.
  The evaluator does not skip the event. A silent skip hides an attempt or a failure.
- An event of the four app-only types needs the source `app`. The types are `attempt_submitted`, `check_result`, `task_finished`, and `module_completed`.
  Any other source gives `bad_event`. The ladder reads attempts, so this stops an event from the student or the agent from unlocking a higher level.
- The types tied to a task need a task id. They are `attempt_submitted`, `check_result`, `hint_requested`, `task_opened`, and `task_finished`.
- Event text such as `evidence` is untrusted. The evaluator compares it for equality and never copies it into a decision.
- Cost: one pass over the window for each rule. With 50 rules and 200 events that is 10,000 steps. The evaluator uses no pattern matching on event text.

## Errors

`PolicyError` is for the policy. `EvaluationError` is for the other inputs. Both have a stable `code`, a fixed message, and a `path`.
The path is JSON-pointer-like, such as `/rules/0/trigger/count`. It holds only fixed field names and list positions, never text from the data.

- Policy codes for the shape: `not_an_object`, `unknown_key`, `missing_key`, `wrong_type`.
- Policy codes for the values: `bad_id`, `bad_value`, `out_of_range`, `too_many_rules`, `duplicate_rule_id`.
- Policy codes for the meaning: `unknown_action`, `unknown_event`, `bad_condition`, `unsupported_version`.
- Evaluation codes: `bad_policy`, `bad_events`, `bad_event`, `bad_state`, `bad_limits`, `bad_decision`.

## Known limits

- A pass by the student does not reset a count. Say a student fails twice, then passes. The "two failures" still hold in the window.
  The repeat guard stops a second offer for the same trigger. But a first offer that a limit delayed arrives after the pass.
  The caller avoids this by calling `evaluate` after each event.
- A count includes every matching event in the window and in the scope, not only the latest run.
- It is a choice that "the same result" includes `evidence`. The rule is strict: two failures with different notes are two different results.
- An attempt that left the window no longer unlocks the upper levels. A student with a long log sees lower levels than the ladder allows. That is the safe direction.
- The ladder checks that an attempt exists for the task. It does not check that the attempt is the one the student works on now. It does not check the module version.
- The policy has no way to say "at most one offer per rule", "only in a certain mode", or "only after a hint". These are not in the spec for this step.
- The evaluator does not read the mode of the student (`coach`, `pair`, `show-me`).
- Python cannot hide an object from code in the same process. A program in the same process is able to change a frozen object.
  These limits stop mistakes and bad data. They do not stop a hostile program in the same process.
- The tests ran on macOS only. Linux continuous integration is the final check. It has not run for this step.

## Open decisions for the owner

- The values of `max_offers_per_session` and `min_gap_seconds`. They have no default on purpose.
- Whether a pass resets a count (see "Known limits").
- Whether a creator sets a lower rate limit for one module. Today only the caller sets the limits.
- The shape of the policy contract file, and the version number it uses. This step ships its own strict check and uses `version` 1.

## Tests

`tests/test_policy_evaluator.py` uses no network, no disk, and no clock. It builds events that the real event validator accepts.
The 135 tests passed on Python 3.10, 3.12, 3.13, and 3.14, on macOS.

- It checks the constants kept in code against the schema files. A drift fails a test.
- It tests each policy fault and the closed objects.
- It tests the three limits that the format must make impossible to state: done-marking, scripts, and permissions.
- It tests every ladder level with and without an attempt. A random search looks for an offer above the ladder.
- It tests mute, the caps, the gap, rule order, scope, count, repeat, the window, and the repeat guard.
- It tests that inputs stay unchanged, also with inputs that raise on any change.
- It tests that the clock is never read, and that the module imports only a few standard modules.
- It tests that 50 rules over 200 events finish quickly, and that hostile text of 100,000 characters is refused fast.

A mutation pass changed the key rules one at a time and ran the tests. The rules were the clamp, mute, caps, rule order, scope, and the closed objects.
Some changes survived. Each one leaves the code with the same behavior for every input.
