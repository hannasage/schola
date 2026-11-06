# Check events

This page describes the step that writes the learning events of a check run.
It builds on the event contract (`contracts/learning-events.md`) and the event log store (`docs/event-log.md`).

The code is `check_events.py`. The hub (`check_routes.py`) and the command line tool (`schola_cli.py`) both call it.
The tests are `tests/test_check_events.py`, the check event tests in `tests/test_check_routes.py` and `tests/test_cli.py`, and `tests/check-events-parity.test.mjs`.
The code uses only the Python standard library.

## Terms

- A check is a test of the student's work. A lesson module lists its checks.
- A run is one use of one check.
- A started run is a run where the runner began to look at the work. The check runner says so with the `started` flag of `check_runner.run_traced`.
- An attempt is one run of a check, seen as one piece of submitted work. Each run is a new attempt.
- A digest is a short fingerprint of data. Schola uses SHA-256. The same data always gives the same digest.
- The manifest is a list of the files that a check reads. The digest of the work is the digest of that list.
- A session is one sitting. Its id is a lowercase UUID.
- The app is the code inside the Schola process. The contract lets only the app write `attempt_submitted` and `check_result`. So these events have the source `app`.

## What a run writes

A started run writes two events, one after the other.

1. `attempt_submitted` holds a new attempt id, the digest of the work, and the size of the work in bytes.
2. `check_result` holds the same attempt id, the check id, and the status. It also holds the run time in milliseconds, the runner type, and a short code in `evidence`.

Both events hold the same envelope. It has the schema version `1.0`, a new event id, and the time in whole seconds (UTC). It also has the session id, the module id and version, a task id, the source `app`, and an empty skills list.
The attempt event takes the time at the start of the run. The result event takes the time at the end of the run.

The app checks both events with `learning_event.validate_event` before it writes either one.
If one is not valid, it writes neither, and the code is `event_invalid`.
The store checks each event again at its append.

A started run writes both events for every status: `passed`, `failed`, `error`, and `skipped`.
An `error` run or a `skipped` run still looked at the work, so it has an attempt.
A repeated run is a new attempt with new ids. The log keeps every attempt.

## When a run writes nothing

A run that did not start writes no event. The runner refuses before it starts in these cases:

- the check is not in a shape Schola understands,
- the check is self-reported (the student reports it by hand),
- no adapter handles the runner type,
- the workspace is not a folder, or
- the script runner refuses to start: it has no consent, the runtime is missing, the runner is busy, the launcher stopped, or the check holds no script.

Nothing was examined in these cases, so there is no work to point an attempt at. The app also makes no copy of the workspace and no digest.
A `check_result` needs an attempt id, so the app does not write it alone.
The response says `eventsWritten: false` with the code `not_run`.

A request that the hub or the tool refuses before a run also writes nothing. Examples are a missing token, a missing consent, a missing workspace, an unknown module, an unknown check, and an unknown task.
Those requests fail with their own error. They have no `eventsWritten` field.

## The task id

Both events need a task id. A check has no link to a task in the lesson module, so the app cannot read the task from the check.
The app never makes up a task id. It uses one of these:

1. The task that the caller names. This is `taskId` in the run request or `--task` on the command line. The module must have that task.
2. The only task of the module. This applies to a module with one task.

A module with several tasks and no named task writes nothing. The response says `eventsWritten: false` with the code `task_required`.
A named task that the module does not have is refused before the check runs.

## The digest of the work

The digest is the SHA-256 of a manifest. The manifest has one line for each file that the check reads.
A line holds the relative path, a NUL byte, and the SHA-256 of the file in lowercase hex. It ends with a NUL byte, the size in bytes in decimal, and a newline.
The lines are sorted by the UTF-8 bytes of the path. The size of the work is the sum of the file sizes. The cap is 100 MB.

The files that a check reads depend on its runner:

- `file-exists`, `text-match`, and `json-valid` read the file at the runner path. A folder holds no file.
- A script check reads the copy of the workspace that the script runner makes. The app makes the same copy again with the same function and the same limits. The limits are 20 MB and 2000 files, and the copy skips `.git` and `node_modules`. The app hashes the copy and deletes it. The script and the files it writes are not part of the work.
- Any other runner, such as `git-commit-exists`, reads no work file.

The empty manifest is the SHA-256 of no bytes, and 0 bytes. These checks have it:

- a check that reads no file by design, such as `git-commit-exists` or a runner type that nobody handles,
- a check whose path names no file, such as an unsafe path or a folder, and
- a check whose file was missing for the run too.

A check that tried to read and failed does not get the empty manifest. The digest would not describe the work. The app writes no event, and the response says `eventsWritten: false` with the code `artifact_unavailable`. The check result is still returned. These cases fail:

- the script runner refuses the copy (a link that leaves the folder, too many files, too many bytes),
- a file is over the read limit of its runner, or the system refuses to open it,
- a file was there for the run and is gone or swapped at the digest, and
- the system gives a read error.

If the work is over the cap of 100 MB, the code is `artifact_too_large`.
If the app cannot delete its temporary copy, it prints a warning on the error output and keeps going.
The app computes the digest after the run, from the folder as it stands then. A file that changes between the run and the digest gives the digest of the new content.

The path is part of the digest, but it is not in the event. Nobody can read a path or a file name from the log.

## The evidence code

A runner returns evidence with paths, counts, and other facts. That evidence can echo a line of the student's work, so it is untrusted.
The event never holds it. The `evidence` field of `check_result` holds one code or the empty string.

The code is the `reason` of the result. The app keeps a reason only if it is on a fixed list, `check_events.REASON_CODES`. Examples are `not_found`, `invalid_json`, and `script_timeout`.
A reason that is not on the list becomes the empty string.
A test reads the runner source files. If a runner gains a reason that the list does not have, the test fails.

## When the events cannot be written

A fault in the log never fails the check and never hides its result.
The hub response and the command line output carry the result as before. They add these fields:

- `eventsWritten` is `true`, `false`, or `"partial"`.
  - `true`: both events are in the log.
  - `false`: the store says that nothing was written (or the app wrote nothing on purpose).
  - `"partial"`: the attempt is in the log and the result is not, or a write may have reached the log (`log_uncertain`). The reader cannot tell from the value alone, so the code matters.
- `eventsError` is a stable code. If `eventsWritten` is `true`, the field is missing.

The codes are:

- `not_run`: the run did not start.
- `task_required` and `task_unknown`: the app has no task id for the events.
- `log_full`: the log is at a cap (5 MB or 50000 lines). The store does not rotate or delete the log.
- `log_reserved`: the log holds 80% of its bytes or lines, and a hub run may not add more. See "The reserve".
- `log_uncertain`: a write may have reached the log. See below.
- `log_damaged`: part of the event store is not what it must be. An example is a log that is a folder.
- `log_unavailable`: the log cannot be read or written. The cause is a permission, a lock, the disk, or a link in the path.
- `duplicate_event_id`: the log already holds an event with this id.
- `clock_unavailable`: the clock gave no usable time, so no event has a time.
- `artifact_too_large` and `artifact_unavailable`: the app cannot make the digest of the work.
- `event_invalid`: an event did not pass the contract. This is a bug or a bad module id. It is not a fault of the student.
- `events_failed`: any other fault before a write. The app prints the class name of the fault on the error output, and nothing else.

### An uncertain write

The store raises some faults after the bytes are on the disk or may be there. Examples are a failed `fsync`, a failed flush of the folder, a failed close, and a write that stopped early.
The store marks these faults with `maybeWritten`. The app does not call such a write "not saved".
If the store says `maybeWritten`, the report is `eventsWritten: "partial"` with the code `log_uncertain`.
If the first write is uncertain, the app does not try the second event.
If the second write is uncertain, the first event is in the log, and the report is the same.
A fault that the app does not know, such as an exception that is not from the store, also counts as uncertain.
The app reports `eventsWritten: false` only when the store says that nothing was written.
The reader must read the event log before a retry. A retry makes new ids, so a duplicate attempt is possible.

A code never holds the text of an error, a path, or a file name.
If `eventsWritten` is not `true`, the command line tool also prints one line on the error output. The one exception is `not_run`, which is not a fault.
For `log_uncertain` the line says that the events may or may not be saved, and to read the event log before a retry.
The exit code stays the exit code of the check result, unless the caller passes `--strict-events`. Then the exit code is 4.
The hub answers 200 with the result. It also prints one line with the code on its own error output. That line holds no path.

The two events are two appends. The store takes its lock once for each append, so another writer can add an event between them.
If the second append fails, the attempt stays in the log and the report says `"partial"`. The store never removes a line.

## The reserve

The hub run route is open to the page, so its events obey the same 80% reserve as other page events (`from_page=True`).
A loop of runs cannot fill the log. The last 20% stays free for other events of the app.
When the log reaches 80%, a hub run returns its result and the code `log_reserved`.
The command line tool writes with no reserve. It can use the log up to 100%.

## The task id is not proof

The `taskId` in a run request is untrusted. The server checks that the module has that task. It does not check that the check belongs to that task.
A caller that holds the session token can name any task of the module. The events then attribute a result to that task.
The token does not separate the page from another script on the same origin. So the events show what the app ran. They do not prove which task the student worked on.

## The session id

`check_events.new_session_id()` is the one place that makes a session id.
The hub makes one id at the start of its check session. It uses that id for every event it writes until it stops.
Each run of the command line tool makes its own id.

## What the events do not record

The events never hold:

- a path, a folder name, or a file name,
- the content of a file or a line of the student's work,
- script text, script output, or the log lines of a run,
- the text of a runner's evidence (only a code from the fixed list),
- the check hash or a consent,
- the name of the student, an email address, or a device.

The other items in "Do not capture" in the contract stay out too.
The digest is the only trace of the work, and it cannot be turned back into the work.
A person who has the same files can compute the same digest. So the digest tells only whether two attempts hold the same work.
