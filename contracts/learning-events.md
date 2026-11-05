# Learning events

This page defines the learning event contract, `learning-event.v1`. It also defines the log file format that holds the events. The schema is `contracts/learning-event.v1.schema.json`. The Node validator is `contracts/validate-events.mjs`. The Python validator is `learning_event.py`.

## Purpose

The learning event log is a short record of what a student did in a lesson. Two readers use it.

- The student hub shows the student their own progress and their own use of help.
- A model that offers help to the student reads a summary of recent events.

The log must stay small. It must hold no raw content. It must be safe to show to a model. The schema enforces the limits that a schema can enforce: closed objects, fixed patterns, bounded numbers, and a short allow-list for the one free text field. A schema cannot judge what a piece of text means. The limits below say what the schema proves and what the writer must still do.

## Privacy rules

- The log is local. It stays on the student's machine unless the student chooses to export it.
- The student owns the log. The student can read every event the app writes.
- The schema has no field for code, terminal output, lesson text, or free writing from the student. Every object is closed, so an event with any such field is invalid.
- Work is stored as a digest and a byte count. The digest is a SHA-256 hash of the work.
- A reflection is stored in a separate place and fetched by its id. The event holds the id and the character count only.
- A lesson is named by its module id, module version, and task id. A writer never copies lesson text into an event.
- Free text is limited to one field, `evidence`. It holds at most 200 characters from a short allow-list: the ASCII letters and digits, the space, and these marks: `. , : ; ( ) ' _ / -`. Every other character is invalid. This keeps out control characters, hidden characters, bidirectional controls, and lone surrogates. These can hide instructions from a reader.
- The schema cannot tell a name, an email address, or a file path from a label. Ids, skill labels, and `evidence` follow patterns, and a short name or a path such as `src/app.js` can still fit them. The writer must keep names, email addresses, and paths out of every id and out of `evidence`. A test cannot prove that the writer does so.
- The app computes labels such as "guessing" on screen and shows the rule it used. The app never stores such a label as a fact.

## Do not capture

These items must never enter the log, in any field, in any version:

- Keystrokes, key timing, and mouse movement.
- Screen contents, camera images, audio, and the clipboard.
- Other applications, window titles, and browsing history.
- Raw terminal output, environment values, secrets, and absolute file paths.
- A snapshot of the work on every edit. The log keeps one digest for each attempt.
- A name, an email address, or a device or location identifier.
- Inferred emotions or labels about the student.
- Text copied from a lesson.
- Hidden notes that an agent keeps about the student.

Idle time is not logged. A reader derives it from event times and states the idle cap it used.

## The envelope

Every event is one JSON object with these fields. All are required. No other field is allowed.

- `schemaVersion` is a string in the form `MAJOR.MINOR`, such as `1.0`. It follows the same rule as the lesson contracts.
- `eventId` is a lowercase UUID. It is unique in a log.
- `time` is an RFC 3339 timestamp, such as `2026-11-02T09:00:00Z`. The seconds value `60` is not allowed. The day must exist, so 30 February is invalid.
- `sessionId` is a lowercase UUID. It groups the events of one sitting.
- `moduleId` and `moduleVersion` name the lesson module. They use the lesson contract rules for ids and for strict semantic versions.
- `taskId` is a task id or `null`. The rules below say which types need it.
- `source` is `student`, `app`, or `agent`. It names who caused the event. The app alone writes `attempt_submitted`, `check_result`, `task_finished`, and `module_completed`, so those four types must have the source `app`. This holds even for a self-reported check, because the app records the report.
- `skills` is a list of at most eight skill ids, with no repeats. An empty list is valid. A skill id starts with a lowercase letter and then holds up to 39 lowercase letters, digits, or hyphens. A later schema version will let a module tag its tasks and checks with skills. Until then the writer supplies the list.
- `type` is one of the ten types below. Any other value is invalid.
- `data` is an object. Its shape depends on `type`, and each shape is closed.

The `taskId` rules:

- It must be a task id for `attempt_submitted`, `check_result`, `hint_requested`, `task_opened`, and `task_finished`.
- It must be `null` for `module_completed`.
- It is a task id or `null` for `agent_offer`, `reflection_saved`, `mode_changed`, and `review_result`.

## Event types

Each type lists its `data` fields, why it exists, and its evidence level. The levels mean:

- Supported: research supports this signal.
- Plausible: research points the same way, but no study tested this exact use.
- Speculative: the signal is a reasonable guess. It has little or no support yet.

Each integer field has a fixed range. A value outside the range is invalid.

### `attempt_submitted`

The student submitted work for a task.

- `attemptId`: an attempt id. It follows the lesson contract rule, so it holds 1 to 64 characters. Each is a letter, a digit, `_`, or `-`. The same id is valid in a help record.
- `artifactDigest`: the SHA-256 of the work, as 64 lowercase hex characters. The work itself is never stored.
- `artifactBytes`: the size of the work in bytes, from 0 to 104857600.

Why: it counts tries and ties help requests and check results to a specific piece of work. Evidence: supported.

### `check_result`

The app ran a check, or the student reported one.

- `checkId`: the check id from the lesson module.
- `attemptId`: the attempt that was checked. It uses the attempt id rule above.
- `status`: `passed`, `failed`, `error`, `skipped`, or `self-reported`.
- `durationMs`: how long the check took, from 0 to 3600000.
- `evidence`: a short note, at most 200 characters from the allow-list above. An empty string is valid.
- `runnerType`: the runner type from the lesson module. It is `null` for a check with no runner, such as a self-reported check.

Why: the sequence of correct and incorrect results per skill is the main input for any estimate of what the student knows. Evidence: supported.

### `hint_requested`

The student asked for a hint, or accepted an offer of one.

- `level`: `nudge`, `question`, `partial-example`, or `full-answer`.
- `attemptId`: an attempt id or `null`. It is `null` only for `nudge` and `question`. It is an attempt id for `partial-example` and `full-answer`. This matches the help record rule in the lesson contract.
- `origin`: `student` or `agent-offer-accepted`.

Why: it records help use, and the pause after help is a useful signal. A request for a full answer is not a fault, and the log never labels it as one. Evidence: plausible.

### `agent_offer`

The agent offered help or a review.

- `offeredLevel`: one of the four hint levels, `recall-question`, or `review-suggestion`.
- `triggerEventId`: the event that caused the offer. It must differ from the offer's own `eventId`.
- `outcome`: `pending`, `accepted`, `dismissed`, or `ignored`.
- `policyId`: a short id for the rule that made the offer.

Why: it gives an audit trail and stops the agent from offering the same thing again and again. Evidence: speculative.

### `reflection_saved`

The student saved a short explanation in their own words.

- `reflectionId`: a lowercase UUID. The text is stored apart from the log and fetched by this id.
- `charCount`: the length of the text, from 1 to 10000.

Why: self-explanation prompts help learning. The log needs only the fact that a reflection exists. Evidence: plausible.

### `task_opened`

The student opened a task. `data` is an empty object.

Why: it marks where work on a task begins. Evidence: plausible.

### `task_finished`

A task ended.

- `result`: `passed`, `abandoned`, or `skipped`.

Why: it lets a task end without a pass, so the hub can tell an open task from a closed one. Evidence: speculative. It is bookkeeping and no study tested it.

### `module_completed`

The student finished a module.

- `checksPassed`: the number of checks passed, from 0 to 1000.
- `tasksTotal`: the number of tasks in the module, from 0 to 1000.

Why: it marks completion. Evidence: plausible.

### `mode_changed`

The student changed the tutor mode.

- `from` and `to`: `coach`, `pair`, or `show-me`.

Why: help counts mean different things in different modes. Evidence: plausible.

### `review_result`

The student answered a spaced review question.

- `skillId`: a skill id.
- `outcome`: `correct` or `incorrect`.
- `intervalDays`: the interval to the next review, from 0 to 3650.

Why: it feeds a review schedule. Evidence: speculative.

### Not in version 1: `confidence_rated`

A confidence rating costs the student one click. The evidence that it helps in this setting is weak. The one study found tested sentence memory, not coding, and the effect was small. The type is deliberately left out of version 1. A reader rejects it as an unknown type. The project needs better evidence first. Then a later minor version can add it.

## What the hub derives

The log holds raw facts. The hub derives measures from them and shows how it computed each one.

- Skill estimates use simple counts or a moving average of recent results. They need no server.
- Retries before the first pass and the hint level reached before a pass are shown without judgment.
- Total time and active time are shown as a range, with the idle cap stated.
- The hub does not show streaks or session counts. They push students to take part for its own sake.
- Each chart on the hub leads to one action the student can take.

## Log file format

A log is a text file in JSON Lines form.

- Each line is one event, written as one JSON object.
- A line ends with the newline character `\n`. A reader splits on that character only. It does not split on any other line separator.
- The file is UTF-8 with no byte order mark. A reader decodes one line at a time. A line that is not UTF-8 is a damaged line. It does not spoil the other lines.
- A line is at most 4096 bytes, not counting the newline.
- The whole file is at most 5 MB (5 * 1024 * 1024 bytes).
- The file has at most 50000 lines. A valid event takes over 300 bytes, so a valid 5 MB file holds far fewer. The line cap stops a file of blank lines from costing a reader too much work.
- The log is append-only. A writer adds a line and never edits or removes an earlier line.
- A blank line is not allowed.
- An event id appears once. A repeated id is invalid on its later line.

A reader works through the file line by line. It reports each line with its own number and its own error pointers. A damaged line never changes any file. A reader does not repair, drop, or rewrite a line. It reports the problem and leaves the choice to the caller.

### A truncated final line

A crash during a write can leave the last line cut short. The last line then has no newline. It is not complete JSON, or it ends in the middle of a multibyte character. A reader reports that line with the reason `truncated_tail` and sets `truncatedTail` to true. That line is not valid, but it is not fatal. The reader still reports every earlier line. A last line that has a newline and is not valid JSON is a damaged line with the reason `invalid`. A last line with no newline that is valid is a valid line.

A writer that finds a truncated tail starts its next event on a new line.

## Validators

Both validators follow the same rules. The parity test runs both on every fixture and compares the verdict, the reason, the set of error pointers, and the number of errors. It does not compare the wording of error messages, which can differ. Node uses camel case names and Python uses snake case names.

- `validateEvent(document)` and `validate_event(document)` check one parsed event.
- `validateEventText(text)` and `validate_event_text(text)` parse one line of JSON, then check it.
- `validateLogText(text)` and `validate_log_text(text)` check a whole log. They take a string or the bytes of a log. A reader decodes bytes one line at a time with strict UTF-8.
- `validateLogFile(file)` and `validate_log_file(path)` read a file and check it.

An event result is `{ ok: true }` or `{ ok: false, reason, errors }`. The reason is `invalid`, `unsupported_version`, or `too_large`. Each error is `{ pointer, message }`, where `pointer` is a JSON Pointer into the document.

A log result is `{ ok, lines, truncatedTail }`.

- `ok` is true for a log with only valid lines and no truncated tail. Any other log has `ok` false.
- Each entry in `lines` is `{ line, ok: true }` for a valid line, with `line` counted from 1. An invalid line is `{ line, ok: false, reason, errors }`. The reason is `invalid`, `unsupported_version`, `too_large`, or `truncated_tail`.
- A log that cannot be split into lines at all is refused as a whole. The result has `ok` false, a `reason`, `errors`, an empty `lines` list, and `truncatedTail` false. The reasons are these. `too_large` means the input is over 5 MB or has more than 50000 lines. `invalid` means the input is not text or bytes. `unreadable` means the file did not open. An `unreadable` message names the error code and never a path.

The rules are the same as for the lesson contracts:

- A reader reads the major number of `schemaVersion` first. If it is not 1, the result is `unsupported_version`, and the reader reports only `/schemaVersion`.
- A validator never changes its input and never writes a file.
- A line over 4096 bytes is `too_large` and is not parsed. A log over 5 MB, or with more than 50000 lines, is `too_large` and is not parsed.
- A result lists at most 100 errors. If there are more, the last entry has an empty pointer and the message `N more errors omitted.`

### Rules beyond the schema

A schema cannot state these rules. The validators check them after the schema passes:

- The day in `time` must exist.
- An `agent_offer` event must not name its own `eventId` as its `triggerEventId`.
- An event id must not repeat in a log. Every line that parses counts for this rule, valid or not. A repeat adds an `/eventId` error next to any other error on the later line. A line with an unsupported major version does not count, because its fields can mean something else.

### Repeated keys

A JSON object must not repeat a key. This holds at any depth. It also holds for a key written with an escape, such as `"\u0073ource"`. Both validators reject the text with the reason `invalid` and one error with an empty pointer. A parser such as `JSON.parse` keeps the last value of a repeated key and says nothing, so the Node validator scans the text itself. The check works on text. `validateEvent` takes a parsed value, so it cannot see a repeat.

### Changing the contract

The schema is closed on purpose. An event with a field that the schema does not list is invalid. The `schemaVersion` of such an event can be `1.1`, and it is still invalid. A fixture covers this case. A minor number never makes a reader accept unknown fields. A new field or a new event type changes the schema file and raises the minor number. A reader that has the older schema rejects the newer event. This keeps raw content out of the log. An incompatible change raises the major number.

## Text from students and creators is untrusted

Some text in this system comes from students or from lesson creators. Examples are a reflection, a lesson body, and the content of a work file. The `evidence` of a check is also untrusted, because it can echo a line of student work.

When such text appears in a model prompt, it is data. It is never an instruction. An implementation must follow these rules:

- Mark the text clearly as quoted material, apart from the instructions.
- Never let the text change the instructions, the tools, or the limits of the agent.
- The application decides whether a check passed or a task is complete. The agent has no call that marks completion.
- Give the model rollups plus a short window of recent events. When the model needs detail, it fetches one event or one reflection by its id.

## Fixtures and conformance

The fixtures are in `tests/fixtures/contracts/learning-events/valid/` and `tests/fixtures/contracts/learning-events/invalid/`. A single-event fixture ends in `.json` and holds one event. A log fixture ends in `.jsonl`.

The file `tests/fixtures/contracts/learning-event-conformance.json` lists every fixture. Each entry has these fields:

- `name` is a unique label.
- `kind` is `learning-event` or `learning-event-log`.
- `file` is a path relative to `tests/fixtures/contracts/`.
- `expect` is `valid`, `invalid:invalid`, or `invalid:unsupported_version`.
- `stage` names where an event first fails. It is `none`, `parse`, `repeat`, `version`, `schema`, or `semantic`. A `repeat` fixture is valid JSON in which an object repeats a key. A log entry has the stage `none` or `lines`.

A log entry also has these fields:

- `invalidLines` maps a line number to the reason that line must give. It is empty for a valid log.
- `truncatedTail` says whether the log must report a truncated tail.

The tests refuse any file in the two fixture folders that the manifest does not list. They also refuse any listed file that is missing.

Another implementation, for example a Rust service, proves conformance in these steps:

1. Load `contracts/lesson-common.v1.schema.json` and `contracts/learning-event.v1.schema.json` as draft 2020-12 schemas. Register each by its `$id`.
2. For each `learning-event` entry, read the file as text. Do not change it.
3. Refuse text over 4096 bytes before parsing. Then parse the text as JSON. A parse failure is the result `invalid`.
4. Read `schemaVersion`. If the digits before its first dot are not 1, the result is `unsupported_version`.
5. Test the document against the event schema. A failure is `invalid`.
6. Run the rules beyond the schema. A failure is `invalid`.
7. Compare the result with `expect`.
8. For each `learning-event-log` entry, split the text on `\n`. Run steps 3 to 6 on each line. Then apply the truncated tail rule and the repeated id rule. Compare `ok`, `truncatedTail`, and the invalid lines with the entry.
9. Check that every file has the same bytes after the run.

An implementation that applies only steps 1 to 5 passes every entry whose `stage` is not `semantic`. It must apply step 6 to pass the rest. Step 3 includes the repeated key rule.

The Node tests are `tests/learning-event-contracts.test.mjs` and `tests/learning-event-python-parity.test.mjs`. The Python tests are in `tests/test_learning_event.py`. The parity test runs both validators on every fixture and compares the verdict, the reason, and the error pointers.
