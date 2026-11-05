# The agent loop

This page describes step E6. It covers the agent loop. This part of Schola joins the policy evaluator, the model adapter, and the event log.
It says what the code does, what it refuses, and what it does not prove.
Read `docs/policy-evaluator.md` and `docs/model-adapters.md` first. Read `contracts/learning-events.md` for the events that this code reads and writes.

The code is `agent_loop.py`. The tests are `tests/test_agent_loop.py`.
The code uses only the Python standard library. It needs Python 3.10 or later.
Nothing calls the loop yet. No route, screen, or shell uses it in this step.

## What this step does and does not do

The app writes a learning event. Then the loop asks the policy evaluator a question: is the agent allowed to make an offer?
If the answer is yes, the loop asks the model for a short offer text. It writes an `agent_offer` event to the log. It hands the offer to the caller to show.
The student accepts the offer, dismisses it, or ignores it. The model is silent by default. It offers help quietly and never takes over.

The loop does not:

- show anything. It has no screen and no route. The shell shows the offer.
- save the mute. The mute lives in memory only. The shell saves it and calls `set_muted` at start.
- write `hint_requested`. The shell writes that event after the student accepts an offer. See "What the shell must do".
- store or read an API key. The adapter holds the key.
- choose the rate limit, the token cap, or the timeouts. Those are decisions for the owner. They have no default.
- repair a damaged log. It stops and says so.
- check that the model told the truth. It checks the shape of the text only.

## Terms

- A trigger event is the event that made a rule apply. The evaluator names it in its decision.
- An offer is one message of help from the agent. It has a text, an action, and a level.
- A pending offer is an offer that the student has not answered yet.
- An outcome is what the student did with an offer: `accepted`, `dismissed`, or `ignored`.
- The window is the last 200 events of the log. The evaluator reads only the window.
- If a check cannot pass, to fail closed is to do nothing. Here it means no model call and no event.
- Mute means the student switched the agent off. A self-mute is a mute that the loop sets after repeated failures.
- A pair is one policy id with one trigger event id. The log is append-only. So the outcome of an offer is a new `agent_offer` event for the same pair.
  One offer is one pair.

## Use

```python
loop = AgentLoop(
    policy=policy,                  # a TutorPolicy
    adapter=adapter,                # a ModelAdapter
    log=event_log,                  # an EventLog
    limits=OfferLimits(...),        # required, no default
    budget=TokenBudget(...),        # required: the token cap of the session
    session_id='...', module_id='m1', module_version='1.0.0',
    clock=time.time,                # returns seconds since the epoch
    new_id=lambda: str(uuid.uuid4()),
    max_output_tokens=...,          # required, no default
    offer_timeout_seconds=...,      # required, no default
    task_label=None,                # optional title of the module or task
)
loop.set_focus('task-1')            # the task in focus, or None
result = loop.on_event(event)       # after the app appended `event` to the log
if result.kind == 'offer':
    show_as_plain_text(result.offer.text)
    loop.resolve(result.offer.offer_id, 'accepted')   # or 'dismissed' or 'ignored'
loop.sweep()                        # call now and then: pending offers that timed out become 'ignored'
loop.set_muted(True)                # the student switches the agent off
```

Every argument has a keyword. The four owner decisions have no default. They are `limits`, `budget`, `max_output_tokens`, and `offer_timeout_seconds`.
Each argument has an exact type. A subclass of `TutorPolicy`, `OfferLimits`, or `TokenBudget` is refused. The `adapter` and the `log` accept a subclass.
The `clock` returns a number of seconds from 0 up to year 9999. The `new_id` function returns a new lowercase UUID each time.
The loop never reads the real clock. The caller injects both functions. So a test controls time and ids.

A wrong argument raises `AgentLoopError`. It has a stable `code` and a fixed message that holds no data. This is for programmer errors only.
A condition at run time never raises. It returns a result.

## What happens on one event

`on_event(event)` follows these steps in order. The whole path runs under one lock, so calls run one at a time.

1. An event of the agent, of another session, or of another module is not a trigger. The result is `none` with the reason `not_a_trigger`.
2. A muted loop returns `none` with the reason `muted`. A pending offer that has not timed out gives `none` with the reason `offer_pending`.
3. If the log was damaged at start, the loop rebuilds the counters now. If that fails, the result is `log_damaged`.
4. The loop reads the window from the log. A damaged line, a cut tail, or a read error gives `log_damaged`. There is no model call and no event.
5. The loop builds a `SessionState` and calls `evaluate`. A malformed event in the window gives `evaluation_failed`.
6. The loop sweeps the pending offers that timed out. This happens after the evaluator has read the log. See "Outcomes".
7. If the decision is `none`, the result is `none` with the reasons of the decision. A matching `none` rule gives the reason `quiet_rule`. There is no model call and no event.
8. If the trigger event failed before, the result is `none` with the reason `trigger_failed`.
9. If the token budget is spent, the result is `none` with the reason `budget_exhausted`. There is no model call and no event.
10. The loop asks for a new event id, builds the prompt, and calls the model.
11. The loop cleans the text. A refused text gives `bad_offer_text`. There is no event.
12. The loop checks the mute again, with the lock held. A mute during the call drops the result. The reason is `muted_during_call`. There is no event and no counter change.
13. The loop appends the `agent_offer` event with the outcome `pending`. If the append fails, the offer is dropped and no counter changes. The reason is `log_failed`.
14. The loop adds one to `offers_this_session`, sets `last_offer_at`, and keeps the offer as pending. The result is `offer`.

The model call comes first and the event second. A model call that fails writes nothing. An event is never written for text that nobody saw.
This order has a cost. If the process stops between the call and the write, tokens were spent and no offer exists.

`on_event` takes as long as a model call. Call it from a background thread. Do not call it from the request that wrote the event.

## The result

`LoopResult` is frozen. It has these fields:

- `kind`: `offer`, `none`, or `error`.
- `offer`: a frozen `PendingOffer` for `offer`, else `None`.
- `reasons`: a tuple of stable codes. It is empty for an `offer`.
- `tokens_charged`: what the model call cost the budget. It is 0 for a result with no call.
- `error_code`: the stable code of an adapter error or an event log error, else `None`.

A result never holds an exception message, model text, or event text. The offer text is only in `offer.text`.

`PendingOffer` has these fields: `offer_id` (the `eventId` of its `agent_offer` event), `text`, `action`, `level` (the `offeredLevel`), `rule_id`, `trigger_event_id`, and `created_at`.

The reasons, with the kind that goes with each:

- From the evaluator, kind `none`: `muted`, `session_offer_cap`, `min_gap`, `already_offered`, `no_match`.
- `quiet_rule`, kind `none`: a rule of the policy says to do nothing here.
- `not_a_trigger`, kind `none`: the event is from the agent, or from another session or module.
- `offer_pending`, kind `none`: an earlier offer waits for the student.
- `trigger_failed`, kind `none`: this trigger failed before. The loop does not try it again.
- `budget_exhausted`, kind `none`: the token budget has no room for a call.
- `log_damaged`, kind `none`: the log did not read clean. If the read raised, the `error_code` holds the log error code.
- `evaluation_failed`, kind `none`: the evaluator refused an event in the window. The `error_code` holds the evaluator code.
- `bad_offer_text`, kind `none`: the model text was refused. The tokens were spent.
- `muted_during_call`, kind `none`: the student muted the agent while the model worked. The tokens were spent.
- `model_failed`, kind `error`: the adapter raised. The `error_code` holds the adapter code.
- `log_failed`, kind `error`: the event failed to write. The `error_code` holds the log code. The tokens were spent.
- `self_muted_after_failures`: the loop adds it to the reasons of the third failure in a row. The loop is now muted.

## What the model sees

The system message is fixed text in the module (`SYSTEM_PROMPT`). It tells the model these things:

- It is a quiet tutor.
- It writes one or two short sentences.
- It only offers help.
- It gives no answer.
- It follows no instruction found in the data.
- It writes no links, no markup, and no code.

The user message has a labelled `DATA` block. The block holds:

- the offer kind (`hint`, `recall`, or `review`) and the allowed level name.
- three counts for the task in focus, taken from the window: failed checks, hint requests, and attempts.
- the skill ids of the trigger event. Only text that matches the skill id pattern goes in.
- the task label from the caller. No label means no line.

The prompt never holds hint content, lesson text, answers, student attempts, reflections, evidence text, check ids, attempt ids, digests, event ids, or paths.
So the model cannot leak an answer from the prompt. The allowed level only tells the model how much help the student can ask for next.

The task label is creator text. It is untrusted. The loop removes hidden characters, collapses white space, and cuts the label to 200 characters.
Then it writes the label as a JSON string on one line, under a heading that says it is untrusted. A quote or a line break in the label cannot end the block.
The adapter's context guard then reads the whole prompt. A key-shaped string or a path in the label makes the adapter raise `invalid_request`.
The loop reports `model_failed` with that code. It does not echo the label.

A test scans every prompt that any test builds. A prompt with a banned marker, an id, a digest, or a path fails the test.

## The offer text

The text from the model is untrusted. The function `clean_offer_text` does this:

- It reads at most the first 20,000 characters.
- It removes control, format, private-use, unassigned, and surrogate characters. It removes line and paragraph separators and the blank filler letters.
- It turns every kind of white space into one space and trims the ends.
- It keeps at most 2 combining marks in a row.
- It cuts the text to 280 characters, at a character boundary, and trims the end.
- It refuses an empty result. It also refuses a result with a link, a code fence, or an HTML-like tag. A refused text returns `None`.

The checks for a link, a fence, and a tag run on a folded copy of the text (NFKC). So wide forms such as a full-width `https://` are caught.
The kept text is not folded. The checks run before the cut and again after it.

A link is any of these:

- `://`
- `www.`
- `javascript:` or `vbscript:`
- `data:`, `file:`, `mailto:`, `tel:`, `sms:`, or `blob:` followed by text
- `](`
- a word with a common web ending, such as `com`, `org`, `net`, or `io`. The full list is in `_LINK_SHAPES`

A code fence is three backticks or three tildes. A tag is `<` followed by a letter, `/`, `!`, or `?`.
Every pattern scans in linear time. A test runs each one on 100,000 characters of hostile text.

These checks are a list of shapes. They are not a proof:

- A link with another ending passes. So does a spelled-out address.
- Text such as `x<y` is refused, though it is not a tag.
- The text is still a model's words. It can be wrong, rude, or unhelpful. The loop does not judge meaning.
- The list of unassigned characters comes from the Python version. A very new character stays on one version and goes on another.

**The shell must show the text as plain text. It must never put the text in HTML or run it as markdown.** The cleaning is a second guard, not the first.

## Outcomes

`resolve(offer_id, outcome)` takes `accepted`, `dismissed`, or `ignored`. It appends a new `agent_offer` event with a new `eventId`.
The new event has the same `policyId`, `triggerEventId`, `offeredLevel`, `taskId`, and skills, and it carries the new outcome. Then `resolve` removes the pending offer.
The first event of the pair stays `pending` in the log. The log is append-only.

An unknown offer id, or an offer that is already resolved, gives a `ResolveResult` with `resolved` false and the reason `unknown_offer`. It does not raise.
A failed write gives the reason `log_failed`, and the offer stays pending.

`sweep()` marks every pending offer older than `offer_timeout_seconds` as `ignored`. An offer whose age equals the timeout is old enough.
`on_event` also sweeps. So a forgotten offer cannot block the loop for ever. An offer that timed out does not block a new offer.
The sweep runs after the evaluator has read the log. The outcome event of an offer restarts the count of its rule (see `docs/policy-evaluator.md`).
An earlier sweep hides the event that triggered the call.

While an offer is pending, the loop makes no new offer. The reason is `offer_pending`.

## Mute

`set_muted(True)` takes effect at once. It does not wait for a model call, because it uses a small lock that the call does not hold.
A mute also applies to an offer being built: the loop checks the mute again before it writes the event.
Muting does not delete pending offers. It marks each one `ignored` through `resolve` and returns the results.
`set_muted(False)` also resets the count of failures in a row.

The mute is in memory only. The shell saves it and restores it at start.

## Failures

After an adapter error, a refused text, or a failed write, the loop remembers the trigger event id. It does not try that trigger again.
The budget keeps the tokens that were spent. This stops a retry storm. The loop remembers the last 64 failed triggers.
The loop can try a trigger again after the memory drops it.

After 3 failures in a row the loop mutes itself. The result of the third failure has two reasons: the reason of the failure and `self_muted_after_failures`.
A success resets the count. The student unmutes with `set_muted(False)`.

The loop reports the adapter error `token_cap_reached` as `budget_exhausted`, not as a failure. The adapter refused before it sent anything.
So nothing was spent, and a full budget is not the fault of the model. It does not count toward the self-mute.

A damaged log does not count as a failure. It is a fault of the log, not of the model.

## Counters

The loop keeps `offers_this_session` and `last_offer_at`. At start it rebuilds them from the log:

- It reads the log in pages of 500 lines.
- It looks at the `agent_offer` events of this session and this module.
- `offers_this_session` is the number of pairs. An offer and its outcome events are one pair.
- `last_offer_at` is the latest of the pair times. The time of a pair is the time of its first event, not of its outcome.

The log keeps times in whole seconds. So the rebuilt `last_offer_at` is rounded down to the second.
A restart does not reset the cap. If the log is damaged at start, the counters stay at zero. Then `on_event` fails closed until the log reads clean, and it rebuilds the counters.
Before each evaluation the loop caps `last_offer_at` at the current time. So a clock that moved back does not make the evaluator refuse the state.

A pending offer is in memory only. After a restart, the offer that was pending in the log is not restored. The shell can no longer resolve it.
It stays `pending` in the log. The evaluator still counts it and does not offer its trigger again.

## Concurrency

The server is threaded. `on_event` runs under one lock for its whole path, including the model call.
So two threads with a limit of one make exactly one offer. The second thread waits, then reads the log that holds the first offer.
A second lock guards the mute, the focus, the pending offers, and the counters. The loop holds it for short work only.
`set_muted`, `resolve`, and `sweep` use only the second lock. So they never wait for a model call.
The order is always the first lock, then the second.

Calls to `on_event` queue behind a slow model call. The timeout of the adapter bounds the wait.

## Reading the log: cost

The loop reads the last 200 events with `EventLog.read`. It asks for the line count first. Then it asks for the lines from `total - 200` on.
It does not put the log in a prompt.

`EventLog` checks the whole file once for each version of the file. It caches the check while the file is unchanged.
Every append makes a new version. So each `on_event` that follows a write pays for one full check of the file. When an offer appends, it pays for a second check.
The cost grows with the size of the log. It is the known first-read cost of step E2. The loop does not hide it and does not add a cache of its own.

These figures come from one Apple silicon Mac, Python 3.14. The events were 622 bytes each: a check result with 200 characters of evidence and four skills.
The figure is the time of one `on_event` that makes an offer:

- 84 events (52 KB): about 15 ms.
- 842 events (0.5 MB): about 115 ms.
- 4,214 events (2.6 MB): about 0.9 s.
- 7,586 events (4.7 MB, 90 percent of the 5 MB cap): about 1.5 s. Five runs gave 1.2 s to 2.6 s.
- The first read of the 4.7 MB log took about 3.6 s. Rebuilding the counters at start then took 63 ms.

The cap is 5 MB. So a log near the cap costs seconds for each event. A real log has smaller events than these, so it holds more of them.
Each figure is from one machine and one run. They are not a bound. Linux was not measured.
A later step can lift this cost by reading only the tail of the file.

## What the shell must do

- Show `offer.text` as plain text.
- Call `on_event` after each student or app event, from a background thread.
- When the student opens a task, call `set_focus`.
- Call `sweep` now and then. The page can do this on page show and page hide.
- Save the mute. Call `set_muted` at start.
- After the student accepts a hint offer, write a `hint_requested` event with the origin `agent-offer-accepted`. The loop does not write it. Then call `resolve(offer_id, 'accepted')`.
- Treat `offer.level` as the most help the student can ask for. The evaluator already clamped it.

## Known limits

- The outcome event of an offer restarts the count of its rule. So does every `agent_offer` event of that rule.
  Events between the offer and its outcome do not count toward the next offer of that rule. This errs on the side of fewer offers. See "Open decisions".
- A trigger that failed gets no offer. The student sees nothing for that event.
- Nothing is stored for the mute, the focus, or the pending offers.
- The model call holds the main lock. One slow call delays the next event. The timeout of the adapter is the bound.
- If the process stops between the model call and the write, tokens were spent and no offer exists.
- The sanitizer is a heuristic. See "The offer text".
- The tests use a stub model. No real model, no real network, and no real shell ran. The behavior with real replies is untested.
- The cost of reading the log grows with the log. See "Reading the log: cost".
- Python cannot hide an object from code in the same process. These limits stop mistakes and bad data. They do not stop a hostile program in the same process.
- The tests ran on macOS only. Linux continuous integration is the final check. It has not run for this step.

## Open decisions for the owner

- The values of `max_offers_per_session` and `min_gap_seconds` (`OfferLimits`), the token cap, `max_output_tokens`, and `offer_timeout_seconds`. None has a default.
- Whether the outcome of an offer restarts the count of its rule. Today it does. A change in the evaluator can restart the count at the first event of a pair only.
- Whether the loop closes a pending offer from an earlier run with an `ignored` event at start.
- Whether the log read needs a faster path before the shell calls `on_event` on a long log.
- Who checks the heuristics of the offer text for languages other than English.

## Tests

`tests/test_agent_loop.py` uses no network and no real clock. The model is a stub that overrides `_send`. The log is a real `EventLog` in a temporary folder.
The clock and the ids are injected.

The 155 tests passed on Python 3.10, 3.12, 3.13, and 3.14, on macOS. The 197 tests of the evaluator also passed on the same four versions.

- It builds a schema-valid `agent_offer` event for each kind of offer (hint, recall, review) and runs it through `learning_event.validate_event`.
- It checks every reason code and the kind that goes with it.
- It checks that the model is called before the event is written, and that a failed write drops the offer and keeps the counters.
- It checks that the cap holds over 100 events, and that a restart rebuilds the counters from the log. It also checks a cap whose offers left the window.
- It checks that a spent budget makes no model call and no event.
- It checks the mute at start, during the log read, during the model call, and in the gap between the last check and the write.
- It runs two threads with a limit of one, with a barrier and a stub that waits. It makes exactly one offer. It also runs 12 threads against a cap of two.
- It checks the failure rules: no retry of a failed trigger, a self-mute at 3 failures, a reset after a success, and a reset after an unmute.
- It checks a damaged log, a cut tail, an unreadable log, and a read report that is not `ok` on either read. All fail closed.
- It scans every prompt that any test builds for banned markers, ids, digests, and paths. It also checks that the scan itself catches them.
- It puts a key-shaped string or a path in the task label. The context guard stops it. The result does not echo it.
- It checks the offer text cleaner on each kind of hidden character, link, fence, and tag. It also checks text that only looks a little like one.
  It runs 3,000 random texts and 35 kinds of hostile text of 100,000 characters.
- It checks that the module imports only the standard library and the three local modules, and that it holds no changing global state.

A stub model replaces the real one. No test uses a real model, a real network, or a real shell.

A mutation pass changed the key rules one at a time and ran the tests. The changes cover these rules:

- the cap and the counters
- the mute checks before and after the call
- the order of the model call and the write
- the reads that fail closed
- the text cleaner
- the self-mute after 3 failures
- the single pending offer
- the locks and the outcome events
- the session cap floor in the evaluator

- The first pass made 156 changes. The tests caught 130. One change had a typo in its own pattern and ran nothing. The other 25 got new tests or a simpler rule in the code.
- The second pass made 165 changes: the 156, with the typo fixed, and 9 more. The tests caught 162. Three survived.
- A third run of those three changes, against the final tests, caught all three.

One of the three needs a note. The change `offset += REBUILD_PAGE - 1` is equivalent at the real page size of 500.
A line read twice changes nothing, because the loop counts pairs. A test with a page size of one catches it as an endless loop, and the run stops it at a time limit.
So that kill is real but degenerate.

The mutation script is a throwaway tool. It is not part of the repository.
