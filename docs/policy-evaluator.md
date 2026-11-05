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
- store anything. The caller owns the counters (see "What the caller must do").
- read the policy from a file or from JSON text. A later step adds the policy contract file and the loader.
  That loader refuses an object that repeats a key. This step takes a Python dict.
- check that the events are valid. See "What the evaluator trusts".

## Terms

- A tutor policy is a list of rules written by the module creator. It is data, not code.
- A rule has an id, a trigger, and an action.
- A trigger says which events make a rule apply. It names an event type. Four parts are optional: a condition, a count, a repeat flag, and a scope.
- The trigger event is the newest event that made a rule apply.
- An action says what the agent is allowed to do after a rule applies. The list of actions is closed.
- An offer is one message of help from the agent. The student accepts it or dismisses it.
- The hint ladder is the ordered list of hint levels: `nudge`, `question`, `partial-example`, `full-answer`. A higher level reveals more.
- An attempt is work that the student submitted for a task. The app logs it as an `attempt_submitted` event.
- To clamp a level is to lower it to the highest level that the ladder allows. The decision records the clamp.
- Muted means the student switched the agent off.
- A window is the part of the event list that the evaluator reads.
- An epoch is the part of the window that a rule counts. It starts after the last event that restarts the count.

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
The policy, the state, and the limits must be exactly the classes of this module. A subclass gives `bad_policy`, `bad_state`, or `bad_limits`.

## The policy format

A policy is a JSON object. The reader takes a Python dict.

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
Every text value must be an exact Python `str`. A subclass of `str` is refused, because a subclass is able to lie about equality.

- `version` is the whole number 1.
- `rules` is a list of at most 50 rules. It is allowed to be empty. An empty policy never offers anything.
- `id` has 1 to 40 characters: lowercase ASCII letters, digits, and hyphens. It starts with a letter or a digit. Each id is unique in the policy.
  The id becomes the `policyId` of an `agent_offer` event. That field has this pattern.
- A rule has no label and no free text. Nothing in a policy holds a sentence.

### Triggers

- `event` is required. It is one of the nine event types that the agent does not write. The agent's own event, `agent_offer`, is not allowed.
  A rule that reacts to an offer is able to start a chain of offers. A later step decides on that.
  The evaluator still reads `agent_offer` events, for the repeat guard, the epoch, and the session count.
- `scope` is `task`, `session`, or `module`. The default is `task`. It says which events count:
  - `task`: events whose `taskId` is the task in focus. When no task is in focus, the rule never matches.
    An event with a null `taskId` never matches this scope. The type `module_completed` always has a null `taskId`, so it is not allowed with this scope.
  - `session`: events of the session in focus.
  - `module`: events of the whole module, across sessions and tasks.
  Events of any other module never count, in every scope.
- `count` is a whole number from 1 to 50. The default is 1. When this many events count, the rule matches.
  The cap is 50 and not 200. The window holds 200 events, and events that do not match use it up.
- `where` is an optional condition. The list is closed, and each condition fits only some events:
  - `status` fits `check_result`: `passed`, `failed`, `error`, `skipped`, or `self-reported`.
  - `level` fits `hint_requested`: a level of the hint ladder.
  - `outcome` fits `review_result`: `correct` or `incorrect`.
  A condition means "equals" and nothing else. There is no pattern, no expression, and no code.
- `repeat` is `true` or `false`. The default is `false`. With `true`, the `count` events share the same result.
  What "the same result" means depends on the event:
  - `check_result`: the same check id, the same status, and the same `evidence` text.
  - `hint_requested`: the same level.
  - `review_result`: the same skill and the same outcome.
  - `attempt_submitted`: the same work (the same digest).
  Other event types have no result, so `repeat: true` on them is an error.
  The events do not need to be next to each other.

### Where the count starts

A rule counts events from its epoch only. The epoch starts after the latest of these:

1. The last offer by this rule in the module. The rule needs new events for its next offer.
2. For a `check_result` trigger, the last `passed` check in the same scope and the same module.
   A rule that counts `passed` checks is not restarted by them. The passed check itself is not counted.
3. For scope `task`, the last `task_finished` event of the task in focus. A rule that counts `task_finished` events is not restarted by it.

Examples, with a rule that needs two failed checks:

- fail, fail, pass: no offer. The pass restarts the count.
- fail, pass, fail, fail: an offer, for the last failure.
- fail, fail, offer, fail: no offer. Two more failures give the next offer.

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
   The task in focus is `state.task_id`. It sets the ceiling. The task of the trigger events does not.
   When no task is in focus, there is no attempt. A `hint_requested` event never counts as an attempt.
   The evaluator sets `clamped` to true after it lowers a level. The clamp runs on every hint decision. The policy has no key to switch it off.
2. **The agent cannot mark a task or a module done.** The policy format has no such action and no such key. A policy that tries one is a `PolicyError`.
3. **The agent cannot run a script or approve one.** The same holds: the format has no way to say it.
4. **The agent cannot widen its own permissions.** Every object is closed. The limits, the mute, and the ladder are not in the policy.
   A model cannot edit a policy, because the evaluator takes a checked, frozen object.
5. **Offers are rate limited.** `OfferLimits` has two required numbers and no defaults: `max_offers_per_session` and `min_gap_seconds`.
   The values are an open decision for the owner.
   The session cap uses the larger of two numbers. One is `offers_this_session` from the state. The other is the number of offers in the window, for this session and this module.
   The evaluator counts each pair of `policyId` and `triggerEventId` once. The log is append-only, so a later outcome (`accepted`, `dismissed`, or `ignored`)
   is a new `agent_offer` event for the same pair. The offer and its outcome events count as one offer. Two triggers count as two offers.
   A counter that the caller forgot to update does not lift the cap.
   The gap uses `last_offer_at` from the state only. The evaluator never reads event times.
6. **Mute always works.** A muted state makes the evaluator return `none` with the reason `muted`. It reads no event.
   Any value of `muted` other than `False` counts as muted, with no error.
   The checks on the types of `events`, `policy`, `state`, and `limits` still come first. Even when muted, an `events` value that is not a list or tuple raises `bad_events`.

## The decision

`Decision` is a frozen object with these fields:

- `action`: `none`, `offer_hint`, `ask_recall`, or `suggest_review`.
- `rule_id`: the rule that made the offer. A `none` decision has one only for a matching `none` rule.
- `level`: for `offer_hint` only. It is the highest level that the agent is allowed to offer.
- `requested_level`: for `offer_hint` only. It is the `maxLevel` of the rule.
- `offered_level`: for an offer only. It is the exact value for `offeredLevel` in the `agent_offer` event. See "The offer event".
- `ceiling_reason`: `no_attempt` after a clamp, else `None`.
- `clamped`: true after the ladder lowered the level, false otherwise.
- `trigger_event_id`: for an offer only. It is the id of the trigger event.
- `suppressed_by`: a tuple of reasons for a `none` decision.
- `suppressed_rule_id` and `suppressed_trigger_event_id`: when the session cap or the gap stopped a rule that matched, the rule and its trigger event. Else `None`.
- `events_read`: how many events the evaluator read. It is 0 for a muted state.
- `truncated`: true for a list that is longer than the window.

Every id in a decision matches the pattern of the event contract. No free text from an event reaches a decision.
`to_dict()` gives the same fields with camelCase names.

The reasons in `suppressed_by` come from a closed list:

- `muted`: the student muted the agent. It wins over everything.
- `session_offer_cap`: the session already holds `max_offers_per_session` offers.
- `min_gap`: the last offer is less than `min_gap_seconds` old.
- `already_offered`: a rule matched, but its trigger event already has an `agent_offer` from some rule.
- `no_match`: no rule matched.

When both limits stop an offer, the reasons come in this order: `session_offer_cap`, then `min_gap`.

### How the evaluator picks a rule

1. A muted state returns `none` at once.
2. The evaluator checks the rules in the order the creator wrote them. The first rule that matches and is not already answered wins. One decision per call.
3. When the winning rule is a `none` rule, that is the answer.
4. Otherwise the evaluator applies the rate limit. When a limit stops the offer, the answer is `none` with the reasons.
5. A hint offer is clamped last.

Look at the window for `agent_offer` events of the same module. An offer by any rule uses up its trigger event.
The evaluator skips a rule whose trigger event is used up, and goes on to the next rule.
So one trigger event gets one offer. A narrow rule that answered a trigger leaves no broad rule free to answer the same trigger.
A new event is a new trigger event, so a new offer follows.

### The offer event

When the agent makes an offer, the caller writes an `agent_offer` event with these values:

- `policyId`: `decision.rule_id`.
- `triggerEventId`: `decision.trigger_event_id`.
- `offeredLevel`: `decision.offered_level`. For a hint it is the clamped level. For `ask_recall` it is `recall-question`. For `suggest_review` it is `review-suggestion`.
- `outcome`: `pending`.
- `source`: `agent`. The task id is the task in focus, or null.

A test builds this event from each kind of decision and runs it through `learning_event.validate_event`.

## What the caller must do

The agent loop (step E6) owns these duties. The evaluator cannot do them.

- Call `evaluate` after each new event, with the events oldest first.
- Keep `offers_this_session` and `last_offer_at`. After the agent makes an offer, add one to the count and set the time. The evaluator never changes them.
  The count is a floor check only. The evaluator also counts the offers it finds in the window.
- Pass the right `now`. The evaluator never reads a clock. `last_offer_at` is not later than `now`, and neither is negative.
- Write an `agent_offer` event for each offer, as above. Without it, the same trigger gets the offer again.
- Pass the real mute state. A caller that forgets the mute defeats limit 6.
- Treat `level` as a ceiling, not as a command. The agent is free to offer less.
- Pick the limit values. They have no default.

## What the evaluator trusts

The evaluator trusts that `learning_event.py` or `contracts/validate-events.mjs` approved each event. It does not check the whole log.
It re-checks only what it reads, and it fails closed:

- The envelope: `eventId` and `sessionId` (lowercase UUID), `moduleId` and `taskId` (the id pattern), `source`, `type`, and `data` (an object).
- The source rules: only `agent_offer` has the source `agent`, and `agent_offer` has no other source. The four app-only types need the source `app`.
- The task rules: the five task types need a task id, and `module_completed` needs a null one.
- The `data` fields that it reads, with the patterns of the event contract:
  - `check_result`: the check id, the status, and `evidence`.
  - `attempt_submitted`: the attempt id and the digest.
  - `hint_requested`: the level.
  - `review_result`: the skill and the outcome.
  - `agent_offer`: the policy id, the trigger event id, and the outcome.
- No event id appears twice in the window.

Every text value must be an exact `str`. The evaluator compares event text for equality. It never copies the text into a decision.
It does not read event times, the module version, or any other field.

## The window and the events

- The evaluator reads the last 200 events of the list (`MAX_EVENTS_READ`). It does not read older ones.
  An attempt or a failure older than the window does not count. This errs on the safe side for the ladder: a missed attempt lowers the level.
  `truncated` tells the caller that older events exist. `events_read` tells how many the evaluator read.
- The order of the list is the order of events. The evaluator does not read event times, so a list in the wrong order gives wrong counts.
- One event that fails a check raises `EvaluationError` with the code `bad_event`. The path holds the position in the list you passed.
  The evaluator does not skip the event. A silent skip hides an attempt or a failure.
  So one malformed event in the window blocks all offers until newer events push it out of the window. The mute still works.
- Cost: one pass over the window for each rule. With 50 rules and 200 events that is 10,000 steps. The evaluator uses no pattern matching on text of unbounded length.

## Errors

`PolicyError` is for the policy. `EvaluationError` is for the other inputs. Both have a stable `code`, a fixed message, and a `path`.
The path is JSON-pointer-like, such as `/rules/0/trigger/count`. It holds only fixed field names and list positions, never text from the data.

- Policy codes for the shape: `not_an_object`, `unknown_key`, `missing_key`, `wrong_type`.
- Policy codes for the values: `bad_id`, `bad_value`, `out_of_range`, `too_many_rules`, `duplicate_rule_id`.
- Policy codes for the meaning: `unknown_action`, `unknown_event`, `bad_condition`, `unsupported_version`.
- Evaluation codes: `bad_policy`, `bad_events`, `bad_event`, `bad_state`, `bad_limits`, `bad_decision`.

The code checks the state and the limits when it builds them. It checks them again at each call.
So a value forced into a frozen object with `object.__setattr__` is still caught, except `muted`, which counts as muted.

## Known limits

- A count includes every matching event in the epoch, not only the latest run.
- It is a choice that "the same result" includes `evidence`. The rule is strict: two failures with different notes are two different results.
- An attempt that left the window no longer unlocks the upper levels. A student with a long log sees lower levels than the ladder allows. That is the safe direction.
- The ladder checks that an attempt exists for the task in focus. It does not check that the attempt is recent, and it does not check the module version.
- A passed check restarts the count of a `check_result` rule, and a finished task restarts a task-scope rule. These are the only restarts. Other events never restart a count.
- The policy has no way to say "at most one offer per rule", "only in a certain mode", or "only after a hint". These are not in the spec for this step.
- The evaluator does not read the mode of the student (`coach`, `pair`, `show-me`).
- The session cap counts offers found in the window. An offer older than the window is not found, so only the caller's counter remembers it.
- An outcome event also counts as an offer by its rule for the epoch (see "Where the count starts"). So the outcome of an offer restarts the count of that rule
  a second time. Events between the offer and its outcome no longer count toward that rule. This errs on the side of fewer offers.
- Python cannot hide an object from code in the same process. A program in the same process is able to change a frozen policy.
  These limits stop mistakes and bad data. They do not stop a hostile program in the same process.
- The tests ran on macOS only. Linux continuous integration is the final check. It has not run for this step.

## Open decisions for the owner

- The values of `max_offers_per_session` and `min_gap_seconds`. They have no default on purpose.
- Whether a creator sets a lower rate limit for one module. Today only the caller sets the limits.
- Whether a later step lets a rule react to the agent's own offers.
- The shape of the policy contract file, the version number it uses, and the JSON loader. This step ships its own strict check and uses `version` 1.

## Tests

`tests/test_policy_evaluator.py` uses no network, no disk, and no clock. It builds events that the real event validator accepts.
The 197 tests passed on Python 3.10, 3.12, 3.13, and 3.14, on macOS.

- It checks the constants kept in code against the schema files. A drift fails a test.
- It tests each policy fault and the closed objects.
- It tests the three limits that the format must make impossible to state: done-marking, scripts, and permissions.
- It tests every ladder level with and without an attempt. A random search looks for an offer above the ladder.
- It tests mute, the caps, the gap, rule order, scope, count, repeat, the epoch, the window, and the repeat guard.
- It tests every event type and every field that is read, with wrong types, wrong patterns, and strings that lie about equality.
- It tests that inputs stay unchanged, also with inputs that raise on any change.
- It tests that the module imports only a few standard modules and holds no changing global state.
- It tests that 50 rules over 200 events finish within a loose bound, and that hostile text of 100,000 characters is refused.
  The bounds are generous, so a slow runner does not fail them.

A mutation pass changed the key rules one at a time and ran the tests. There were 176 changes. The tests caught 170.
Six survive. Each leaves the code with the same behavior for every input:

- Dropping the guard for a missing task id on attempts. An attempt always has a task id, or the event is refused.
- Giving a single non-repeat trigger a different position number. It has no other trigger to compare with.
- Building repeat groups for a non-repeat rule. The groups are not read.
- Dropping the length check, or making it one longer. The patterns also bound the length. The check only refuses huge text sooner.
- Raising the rule cap in `from_dict` by one. The `TutorPolicy` constructor holds the same cap and refuses the same lists.
