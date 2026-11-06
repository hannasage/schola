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
- save the mute. The mute lives in memory only. The shell saves `muted` and calls `set_muted` at start.
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
- Mute means the loop makes no offer. The student sets `muted`. The loop sets `self_muted` after repeated failures. Either one keeps the loop quiet.
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

A task label that holds a path or a key-shaped string raises `AgentLoopError` with the code `bad_config`. The loop checks it at construction and at each call.
So a bad label cannot cause model failures later.

## What happens on one event

`on_event(event)` follows these steps in order. The whole path runs under one lock, so calls run one at a time.

1. An event of the agent, of another session, or of another module is not a trigger. The loop computes once which event types the rules of the policy name.
   An event of any other type is not a trigger either, and the loop does not read the log for it. The result is `none` with the reason `not_a_trigger`.
2. A muted loop returns `none` with the reason `muted`. A pending offer that has not timed out gives `none` with the reason `offer_pending`.
3. The loop sweeps the pending offers that timed out. If the write of an `ignored` outcome fails, the old offer is still pending.
   The result is `none` with the reason `offer_pending` and the `error_code` of the log. There is no model call.
4. If the log was damaged at start, the loop rebuilds the counters now. If that fails, the result is `log_damaged`.
5. The loop reads the window from the log. A damaged line, a cut tail, or a read error gives `log_damaged`. There is no model call and no event.
6. The loop builds a `SessionState` and calls `evaluate`. It passes the trigger events that failed before as `exclude_triggers`.
   A malformed event in the window gives `evaluation_failed`.
7. If the decision is `none`, the result is `none` with the reasons of the decision. A matching `none` rule gives the reason `quiet_rule`.
   A trigger that failed before gives `trigger_failed`, after the evaluator tried the other rules. There is no model call and no event.
8. If the token budget is spent, the result is `none` with the reason `budget_exhausted`. There is no model call and no event.
9. The loop asks for a new event id, builds the prompt, and calls the model.
10. The loop cleans the text. A refused text gives `bad_offer_text`. There is no event.
11. The loop checks the mute and the mute generation, with the state lock held. A mute during the call drops the result.
    A mute followed by an unmute counts too. The reason is `muted_during_call`. There is no event and no counter change.
    The loop also checks that no other offer is pending.
12. The loop appends the `agent_offer` event with the outcome `pending`. It does this without the state lock.
    If the append fails, the offer is dropped and no counter changes. See "Writes that report an error".
13. The loop takes the state lock again and adds one to `offers_this_session`. It sets `last_offer_at` and keeps the offer as pending.
    A mute that landed since step 11 cancels the offer. The loop then marks the offer `ignored` in the log, and the result is `none` with `muted_during_call`.
    The loop never returns an offer that it already marked `ignored`.
14. Otherwise the result is `offer`.

The model call comes first and the event second. A model call that fails writes nothing. An event is never written for text that nobody saw.
This order has a cost. If the process stops between the call and the write, tokens were spent and no offer exists.

`on_event` takes as long as a model call. Call it from a background thread. Do not call it from the request that wrote the event.

## The result

`LoopResult` is frozen. It has these fields:

- `kind`: `offer`, `none`, or `error`.
- `offer`: a frozen `PendingOffer` for `offer`, else `None`.
- `reasons`: a tuple of stable codes. It is empty for an `offer`.
- `tokens_charged`: what the model call cost the budget. It is 0 for a result with no call.
- `error_code`: the stable code of an adapter error or an event log error, else `None`. An offer can carry one. See "Writes that report an error".
- `recovered`: how many pending offers of an earlier run the loop closed at start. Only the first result carries a count above 0.

A result never holds an exception message, model text, or event text. The offer text is only in `offer.text`.

`PendingOffer` has these fields: `offer_id` (the `eventId` of its `agent_offer` event), `text`, `action`, `level` (the `offeredLevel`), `rule_id`, `trigger_event_id`, and `created_at`.

The reasons, with the kind that goes with each. Four reasons come with the kind `error`. All others come with the kind `none`.

Kind `error`:

- `model_failed`: the adapter raised, or it gave a result of the wrong type. The `error_code` holds the adapter code.
- `log_failed`: the event failed to write. The `error_code` holds the log code. The tokens were spent.
- `log_damaged`: the log did not read clean, or a write left a partial line. If the read or the write raised, the `error_code` holds the log error code.
- `evaluation_failed`: the evaluator refused an event in the window. The `error_code` holds the evaluator code.

Kind `none`:

- From the evaluator: `muted`, `session_offer_cap`, `min_gap`, `already_offered`, `trigger_failed`, `no_match`.
- `quiet_rule`: a rule of the policy says to do nothing here.
- `not_a_trigger`: the event is from the agent, from another session or module, or of a type that no rule names.
- `offer_pending`: an earlier offer waits for the student, or the loop failed to close an old one.
- `budget_exhausted`: the token budget has no room for a call.
- `bad_offer_text`: the model text was refused. The tokens were spent.
- `muted_during_call`: the student muted the agent while the model worked. The tokens were spent.

`self_muted_after_failures` is not a kind of its own. The loop adds it to the reasons of the third failure in a row. The loop is now self-muted.

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
- the skill ids of the trigger event, as a JSON list. Only text that matches the skill id pattern goes in.
- the task label from the caller, as a JSON string. No label means no line.

The prompt never holds hint content, lesson text, answers, student attempts, reflections, evidence text, check ids, attempt ids, digests, event ids, or paths.
So the model cannot leak an answer from the prompt. The allowed level only tells the model how much help the student can ask for next.

The task label is creator text. It is untrusted. The loop removes hidden characters, collapses white space, and cuts the label to 200 characters.
Then it writes the label as a JSON string on one line, under a heading that says it is untrusted. A quote or a line break in the label cannot end the block.
The context guard of the adapter reads the whole prompt too. A key that only the adapter holds can still reach that guard.
Then the adapter raises `invalid_request`, and the loop reports `model_failed` with that code. It does not echo the label.

A test scans every prompt that any test builds. A prompt with a banned marker, an id, a digest, or a path fails the test.

## The offer text

The text from the model is untrusted. The function `clean_offer_text` does this:

- It reads at most the first 20,000 characters.
- It removes control, format, private-use, unassigned, and surrogate characters. It removes line and paragraph separators and the blank filler letters.
- It turns every kind of white space into one space and trims the ends.
- It keeps at most 2 combining marks in a row.
- It builds a skeleton of the text. The skeleton drops all combining marks. It turns five dot look-alikes into dots. They are listed in `_DOT_LOOKALIKES`.
  Then it folds the text to plain forms (NFKC).
- It runs every refusal check on the cleaned text and on the skeleton. Each check also runs on a folded copy of the cleaned text.
- It cuts the text to 280 characters, at a character boundary, and trims the end. Then it runs the checks again on the cut text.
- A refused text returns `None`.

The loop refuses an empty text. It refuses a text with any of these:

- a scheme (`://`), `www.`, or `hxxp`
- a script or data scheme such as `javascript:`, `vbscript:`, `data:`, or `mailto:`. The full list is in `_SHAPES`
- `](` or a bracketed dot such as `[.]`
- a code mark: any backtick, or three tildes
- a bare domain: a dot with two or more letters on both sides, such as `example.com`, `example.co.uk`, `bit.ly`, `notes.ai`, or `main.py`
- a short domain with a path: a name, a dot, two letters, and a slash, such as `t.me/x`
- an IPv4 address, or `localhost` with a port
- `@` between two word characters
- an HTML entity such as `&#106;`, `&#x6a;`, or `&amp;`
- a tag: `</`, `<!`, `<?`, or `<name>`, `<name/`, `<name />`, or `<name attribute=`

Text such as `i<n correct` is not a tag. Every pattern scans in linear time. A test runs each one on 100,000 characters of hostile text.

These checks are a list of shapes. They are not a proof. Some good text is refused, and some bad text passes:

- Some good text is refused. The rule for a bare domain refuses `notes.ai`, `main.py`, `index.js`, and a sentence with no space after its full stop. The owner accepted these false positives.
  The text `a<b>c` is refused too.
- A domain with a one-letter label and no path passes, such as `t.co`.
- A link with another shape passes. An address spelled out in words passes.
- The text is still a model's words. It can be wrong, rude, or unhelpful. The loop does not judge meaning.
- The list of unassigned characters comes from the Python version. A very new character stays on one version and goes on another.
- The heuristics are tuned for English. Who checks them for other languages is an open decision.

The shell must show the text as plain text. It must never put the text in HTML or run it as markdown.
The shell must also turn auto-linking and data detectors off for the text. Then nothing makes a link out of a number or a name.
The cleaning is a second guard, not the first.

## Outcomes

`resolve(offer_id, outcome)` takes `accepted`, `dismissed`, or `ignored`. It appends a new `agent_offer` event with a new `eventId`.
The new event has the same `policyId`, `triggerEventId`, `offeredLevel`, `taskId`, and skills, and it carries the new outcome. Then `resolve` removes the pending offer.
The first event of the pair stays `pending` in the log. The log is append-only.

An unknown offer id, or an offer that is already resolved, gives a `ResolveResult` with `resolved` false and the reason `unknown_offer`. It does not raise.
While one thread writes the outcome of an offer, another call for the same offer gets `unknown_offer`. So the log never holds two outcomes from one `resolve`.
A failed write gives the reason `log_failed`, and the offer stays pending. A partial line gives the reason `log_damaged`.

`ResolveResult` has these fields: `resolved`, `offer_id`, `outcome`, `reason`, `event_id`, `error_code`, and `maybe_written`.
The `error_code` is the log code of a failed write. When the write gave an unclear result, `maybe_written` is true.

`sweep()` marks every pending offer older than `offer_timeout_seconds` as `ignored`. An offer whose age equals the timeout is old enough.
`on_event` also sweeps. So a forgotten offer cannot block the loop for ever. An offer that timed out does not block a new offer.
If the sweep cannot write, the old offer stays and blocks the new one. See step 3 above.

While an offer is pending, the loop makes no new offer. The reason is `offer_pending`.

The outcome event of an offer does not restart the count of its rule. Only the first event of a pair does. See `docs/policy-evaluator.md`.
So a failure that came while an offer was pending still counts after the student answers or the offer times out.

## Writes that report an error

A write of the log can raise an error with details. The loop reads two flags in the details: `maybeWritten` and `partialLine`.

- No flag: the write failed and wrote nothing. An offer is dropped (`log_failed`). A resolve keeps the offer pending.
- `maybeWritten` (for example `fsync_failed`): the event is possibly in the log. The loop reads the tail of the log and looks for the event id.
  If the id is there, the write counts as done. An offer is returned, with the log code in `error_code`. A resolve is `resolved`, with `maybe_written` true.
  If the id is not there, or the read cannot tell, the write counts as failed. There is no double outcome event.
- `partialLine`: the write left a partial line. The log is damaged. An offer gives `log_damaged` with the log code. A resolve gives `log_damaged`.
  The next event fails closed until the log reads clean.

A failed offer write and a partial line count as failures toward the self-mute. A failed resolve does not.

## Mute

The student can mute the loop with `set_muted(True)`. The loop can also mute itself. The two are separate:

- `muted` is the choice of the student. The shell saves it and restores it at start.
- `self_muted` is set after 3 failures in a row. The shell does not save it.
- If either one is true, the loop makes no offer.

`set_muted(True)` takes effect at once. It sets the flag before it writes anything, and it never waits for a model call or for the write of another thread.
Muting does not delete pending offers. It marks each one `ignored` through `resolve` and returns the results.
`set_muted(False)` clears `muted` and `self_muted`. If something was muted, it resets the count of failures. A call on a loop that is not muted keeps the count.

Every call of `set_muted` adds one to the mute generation. A self-mute adds one too. The loop reads the generation before the model call and again before it writes.
If the number changed, a mute and an unmute happened during the call, and the loop drops the offer.

The shell must check `muted` again just before it shows an offer. A mute can land after `on_event` returned.

## Failures

After an adapter error or a failed write, the loop remembers the trigger event id and passes it to the evaluator as an excluded trigger.
The evaluator skips that trigger and tries the next rule. So a failed trigger does not hide the rules after it.
The budget keeps the tokens that were spent. This stops a retry storm.

- An error that can pass (`timeout`, `rate_limited`, `endpoint_unreachable`, the adapter flag `retryable`) is remembered until `offer_timeout_seconds` after the failure.
  Then the loop can try the trigger again.
- Any other error, a failed write, and a refused text are remembered for good, up to 64 triggers. The oldest are dropped first.

After 3 failures in a row the loop mutes itself. The result of the third failure has two reasons: the reason of the failure and `self_muted_after_failures`.
A success resets the count. The student ends the self-mute with `set_muted(False)`.
The count rises for an adapter error, a failed write, and a partial line. It does not rise for a refused text.
A refused text is a fault of the model's words, not of the system. The trigger is still remembered.

The loop reports the adapter error `token_cap_reached` as `budget_exhausted`, not as a failure. The adapter refused before it sent anything.
So nothing was spent, and a full budget is not the fault of the model. It does not count toward the self-mute.

A damaged log does not count as a failure. It is a fault of the log, not of the model.

The loop catches an error from the adapter call only. An error from the adapter other than `ModelAdapterError` is logged and becomes `model_failed` with the code `bad_response`.
The log line holds the type and the places in the code (the logger `schola.agent_loop`, level warning). It never holds the message.
A result of the wrong type also becomes `model_failed`. An error in the loop's own code is not caught. It reaches the caller, and it does not count as a model failure.

## Counters and restart

The loop keeps `offers_this_session` and `last_offer_at`. At start it rebuilds them from the log:

- It reads the log in pages of 500 lines.
- It looks at the `agent_offer` events with the source `agent` of this session and this module.
- `offers_this_session` is the number of pairs. An offer and its outcome events are one pair.
- `last_offer_at` is the latest of the pair times. The time of a pair is the earliest time among its events that the loop can read.
  A pair whose time cannot be read (such as the year 0000) is still counted. A time below 0 (such as the year 1) counts as 0.
  Before each evaluation the loop also caps `last_offer_at` at the current time.

The log keeps times in whole seconds. So the rebuilt `last_offer_at` is rounded down to the second.
The loop reads zone offsets such as `+05:30` and fractions of a second, because another writer can write them.
A restart does not reset the cap. If the log is damaged at start, the counters stay at zero. Then `on_event` fails closed until the log reads clean, and it rebuilds the counters.

A pending offer is in memory only. At start the loop finds each pair with no outcome event, in this session and module. It tries to append an `ignored` outcome for each pair.
The first result of `on_event` reports the number of closed offers in `recovered`.
A failed write does not block the loop. The pair stays counted, and the evaluator does not offer its trigger again.
After a restart, `resolve` for the old offer id gives `unknown_offer`.

## Concurrency

The server is threaded. `on_event` runs under one lock (`_lock`) for its whole path, including the model call.
So two threads with a limit of one make exactly one offer. The second thread waits, then reads the log that holds the first offer.

A second lock (`_state_lock`) guards the mute flags, the mute generation, the focus, the pending offers, and the counters.
The loop holds it for short work only. It never holds it during a log read or write, a model call, `clock()`, or `new_id()`.
So these calls never wait for a model call: `set_muted`, `muted`, `self_muted`, `pending_offers`, and `resolve`.
They never wait for the log write of another thread either.
A test holds a write open for 1.5 seconds and shows that `set_muted` returns at once.
The order is always the first lock, then the second.

Calls to `on_event` queue behind a slow model call. The timeout of the adapter bounds the wait.

## Reading the log: cost

The loop reads the last 200 events with `EventLog.read`. It asks for the line count first. Then it asks for the lines from `total - 200` on.
It does not put the log in a prompt. An event of a type that no rule names does not read the log at all.

`EventLog` checks the whole file once for each version of the file. It caches the check while the file is unchanged.
Every append makes a new version. So each `on_event` that follows a write pays for one full check of the file. When an offer appends, it pays for a second check.
The cost grows with the size of the log. It is the known first-read cost of step E2. The loop does not hide it and does not add a cache of its own.

These figures come from one Apple silicon Mac, Python 3.14. The events were 622 bytes each: a check result with 200 characters of evidence and four skills.
The figure is the time of one `on_event` that makes an offer:

- 84 events (52 KB): about 10 ms.
- 842 events (0.5 MB): about 80 ms.
- 4,214 events (2.6 MB): about 0.4 s.
- 7,586 events (4.7 MB, 90 percent of the 5 MB cap): about 0.7 s. The first read of that log took about 0.9 s, and the rebuild of the counters at start took 51 ms.

An earlier run of the same script on the same Mac, with other jobs running, gave 15 ms, 115 ms, 0.9 s, and 1.5 s. So the figures move with the load of the machine.

The cap is 5 MB. So a log near the cap costs seconds for each event. A real log has smaller events than these, so it holds more of them.
Each figure is from one machine and one run. They are not a bound. Linux was not measured.
A tail-only read of the file lifts this cost. That change is open. It needs a new reader in step E2.

## What the shell must do

- Show `offer.text` as plain text, with auto-linking and data detectors off.
- Check `muted` again just before it shows an offer.
- Call `on_event` after each student or app event, from a background thread.
- When the student opens a task, call `set_focus`.
- Call `sweep` now and then. The page can do this on page show and page hide.
- Save `muted` and nothing else. Call `set_muted` at start.
- After the student accepts a hint offer, write a `hint_requested` event with the origin `agent-offer-accepted`. The loop does not write it. Then call `resolve(offer_id, 'accepted')`.
- Treat `offer.level` as the most help the student can ask for. The evaluator already clamped it.

## Known limits

- A trigger that failed for good gets no offer. The student sees nothing for that event.
- Nothing is stored for the mute, the focus, or the pending offers. The pending offers of an earlier run are closed at start, on a best effort basis.
- The model call holds the main lock. One slow call delays the next event. The timeout of the adapter is the bound.
- If the process stops between the model call and the write, tokens were spent and no offer exists.
- If a mute lands after the write and the `ignored` write then fails, the offer stays pending in memory and in the log. The next sweep tries again.
  The shell must check `muted` before it shows an offer.
- If the clock moves backwards, a pending offer stays until the clock catches up with its timeout. The loop caps `last_offer_at` at the current time, but it does not move `created_at`.
- The sanitizer is a heuristic. See "The offer text".
- The tests use a stub model. No real model, no real network, and no real shell ran. The behavior with real replies is untested.
- The cost of reading the log grows with the log. A tail-only read is open. See "Reading the log: cost".
- An event of a type that no rule names does not run the evaluator. Say a limit held back an offer, such as the gap. Then the next event of a trigger type brings the offer back.
  An event of another type does not.
- Python cannot hide an object from code in the same process. These limits stop mistakes and bad data. They do not stop a hostile program in the same process.
- The tests ran on macOS only. Linux continuous integration is the final check. It has not run for this step.

## Open decisions for the owner

- The values of `max_offers_per_session` and `min_gap_seconds` (`OfferLimits`), the token cap, `max_output_tokens`, and `offer_timeout_seconds`. None has a default.
- Whether the log read needs a tail-only path before the shell calls `on_event` on a long log.
- Who checks the heuristics of the offer text for languages other than English.
- Whether the bare-domain rule keeps refusing file names such as `main.py`, or lists the web endings again.

## Tests

`tests/test_agent_loop.py` uses no network and no real clock. The model is a stub that overrides `_send`. The log is a real `EventLog` in a temporary folder.
The clock and the ids are injected.

The 241 tests passed on Python 3.10, 3.12, 3.13, and 3.14, on macOS. The 214 tests of the evaluator and the 54 tests of the event routes passed on the same four versions.

- It builds a schema-valid `agent_offer` event for each kind of offer (hint, recall, review) and runs it through `learning_event.validate_event`.
- It checks every reason code and the kind that goes with it.
- It checks that the model is called before the event is written, and that a failed write drops the offer and keeps the counters.
- It makes the log report `fsync_failed` with the event present, with the event absent, and with a partial line. It does this for an offer and for a resolve.
  A resolve writes one outcome event, never two.
- It makes the sweep write fail, and checks that no model call follows. It also checks that two offers are never pending.
- It checks the cap over 100 events, and that a restart rebuilds the counters from the log. It also checks a cap whose offers left the window.
- It checks a restart with a pending offer: the loop closes it, reports `recovered`, and answers a late resolve with `unknown_offer`.
- It reads times with zone offsets and fractions, and with the years 0000 and 0001, and checks the clamps.
- It checks that a spent budget makes no model call and no event.
- It checks the mute at start, during the log read, during the model call, and between the write and the commit.
  It also checks the mute during a one and a half second append. A mute and an unmute during the call drop the offer.
  The mute and the readers return at once while a model call or a write is slow.
- It checks that `muted` and `self_muted` are separate. It also checks the cases where `set_muted(False)` resets the count of failures.
- It runs two threads with a limit of one, with a barrier and a stub that waits. It makes exactly one offer. It also runs 12 threads against a cap of two.
- It checks the failure rules. A failed trigger gets no retry. An error that can pass gets a retry after the timeout. Three failures give a self-mute. A success resets the count.
  A refused text does not count, and a failed trigger does not hide another rule.
- It checks a damaged log, a cut tail, an unreadable log, and a read report that is not `ok` on either read. All fail closed.
- It checks an adapter that raises, an adapter that returns the wrong type, a memory error, and a bug in the loop's own code.
- It scans every prompt that any test builds for banned markers, ids, digests, and paths. It also checks that the scan itself catches them.
- It puts a key-shaped string or a path in the task label. The loop refuses it with `bad_config`. A key that only the adapter knows reaches the context guard.
- It checks the offer text cleaner on each kind of hidden character, link, domain, address, entity, code mark, and tag. It also checks text that only looks a little like one.
  It checks every bypass of the security report. It runs 3,000 random texts, 71 kinds of hostile text of 100,000 characters, and 18 long runs.
- It checks that the module imports only the standard library and the three local modules, and that it holds no changing global state.

A stub model replaces the real one. No test uses a real model, a real network, or a real shell.

A mutation pass changed the key rules one at a time and ran the tests. The mutation script is a throwaway tool. It is not part of the repository.

The first review round made 172 changes to the new and changed code:

- the trigger types, the recovery at start, and the counters
- the time reader (zone sign, fraction, minutes, hours)
- the mute generation, the cancel after the write, and the two kinds of mute
- the ambiguous writes, the read that checks a write, and the partial line
- the resolve claim, the sweep, and the failed trigger memory with its expiry
- the label check, the prompt, and the result kinds
- the text cleaner: the skeleton, the bare domain, the address shapes, the entity shapes, the tag, and the code marks
- the evaluator: the first-event rule for the count and the excluded triggers

The tests caught 156 and missed 16. New tests caught 13 of the 16. Three survive. Each is equivalent:

- `state ignores self mute` and `precommit ignores self mute`. Only the failure path of `on_event` sets the self-mute, and it runs under the main lock.
  No thread can set it between the first mute check and the commit. So the second and third checks of it never see a change.
- `partial after confirm`. A partial line makes the log unreadable, so the read that looks for the event always says no. The order of the two tests changes nothing.

One change to `event_routes.py` (the check for the type `agent_offer`) was run by hand. The tests caught it.

An earlier round made 165 changes to the first version of the loop. The tests caught 162.
New tests killed the three that survived. One of them died as an endless loop, which the run stopped at a time limit.
