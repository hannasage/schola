# The event log store

This page describes step E2: the local store for learning events.
It builds on the learning event contract from step E1 (`contracts/learning-events.md`).
The contract says what an event is and what the log file looks like.
This page says how Schola writes, reads, exports, and deletes that file, and what that work proves.

The code is `event_log.py` (the store) and `event_routes.py` (the local server routes).
The tests are `tests/test_event_log.py`, `tests/test_event_log_safety.py`, `tests/test_event_reflections.py`, and `tests/test_event_routes.py`.
The code uses only the Python standard library.

## Terms

- The log is the file `events.jsonl`. It holds one event on each line.
- An append adds one event to the end of the log.
- A cut line is a last line that has no newline because a write stopped early. The contract calls it a truncated tail.
- A lock is a promise between programs that only one of them writes at a time. Schola uses the operating system call `flock`.
- `fsync` is the call that asks the operating system to write a file to the disk now.
- A reflection is a short text the student writes in their own words. The contract keeps it out of the log.
- The page is the web page that the local server shows. The app is the code inside the server process.

## Where the files are

The store uses the data folder, the same folder that holds records and lessons.
The server takes it from `--data-dir`.
The store adds one folder, `_events/`. No curriculum id starts with the character `_`, so the name never collides with a curriculum.
Older builds ignore the folder.

```text
<data-dir>/
  _events/
    events.jsonl          the log
    .events.lock          the lock file; it holds no data and is never deleted
    reflections/
      <reflection-id>.json   one reflection each
```

The store creates `_events/` and `reflections/` with mode `0700` and the files with mode `0600`.
When the store has to create the data folder itself, it uses mode `0700` too.
A data folder that already exists keeps its mode.
When `_events/` or `reflections/` already exists, the store checks three things before it writes.
The path must be a real folder, not a link. The current user must own it. Its mode must allow no access to anyone else.
If one of them fails, the store raises `unsafe_path` (or `log_corrupt` for a path that is not a folder) and writes nothing.
This keeps other user accounts out. It does not keep out a program that runs as the student.

If `_events/` or `reflections/` is a regular file, every call raises `log_corrupt` and leaves the file as it is.

## How an append works

1. The store turns the event into compact JSON text and gives the text to `learning_event.validate_event_text`. That is the one set of rules. The store has no copy of them.
2. If the event is not valid, the store raises an error and writes nothing. It does not even create the folder.
3. If the event is a `reflection_saved` event, the store looks for the stored reflection. It needs a reflection with the same id and the same `charCount`. Otherwise it raises `reflection_mismatch`.
4. The store flushes any folder whose last flush failed. If that fails again, the append fails.
5. The store takes the exclusive lock. If it does not get the lock, it raises `lock_unavailable`.
6. If the file changed since the last read, the store reads the log again. It builds the list of event ids and counts the lines.
7. If the id of the event is already in the log, the store refuses the event (`duplicate_event_id`).
8. If the log holds 80% of the bytes or of the lines, the store refuses a page event (`log_reserved`).
9. If the write puts the log over a cap, the store refuses the event (`log_full`).
10. If the log is not empty and does not end in a newline, the store puts one newline before the event. It never changes earlier bytes.
11. The store writes the line in one `write` call with the append flag, then calls `fsync`.
12. If it created the log, or found it empty, it also calls `fsync` on the folder, so the file name survives a crash.
13. The store gives back the line number of the event.

The server keeps one store object for its whole run. The object keeps two results in memory:
the list of event ids and line counts that appends need, and the checked report that reads need.
Before it trusts either one, the store compares the size, the modified time, and the inode of the file with the values it saw.
If any of them changed, it reads the file again.
A program that rewrites the file with the same size, the same modified time, and the same inode fools this check. The check is not a promise against such a program.
The list holds the id of every line that parses as a JSON object with a text `eventId`. This is slightly stricter than the contract, which skips lines of an unsupported major version. A stricter check only ever refuses more.

## The caps

The store enforces these rules from the contract on every write:

- A line is at most 4096 bytes, not counting the newline.
- The file is at most 5 MB (5 * 1024 * 1024 bytes).
- The file has at most 50000 lines. A cut last line counts as a line.
- An event id appears once.

A write that goes over a cap fails with `log_full`. The store does not rotate the log and does not delete lines.
When a log is full, what to do next is an open product decision. Until that decision is made, a full log stays full.
The closed schema keeps every field short, so a valid event stays far below 4096 bytes. The store still checks the line cap.

### The reserve

The page must not be able to fill the log and leave no room for the app.
An event that comes through the routes is a page event.
The byte cap is 4194304 bytes at 80%. The line cap is 40000 lines at 80%. Once the log holds either amount, the store refuses page events with `log_reserved` (409).
At exactly 80% the page is refused. One byte or one line below it, the page is accepted.
The app writes through `EventLog.append`, which has no reserve, so it can use the rest up to 100%.
A full log gives the page `log_reserved` and the app `log_full`.

## Reading, exporting, and deleting

- `read(limit, offset)` gives the report from the validator for a window of lines. The window starts after `offset` lines and holds at most `limit` lines. It adds the valid events of the window and the flag `truncatedTail`. It never repairs or drops anything. A missing log reads as empty with `exists` set to false.
- The report has the fields `total`, `offset`, `limit`, and `more`. `total` counts the lines in the whole log. If lines follow the window, `more` is true.
- `ok` and `truncatedTail` describe the whole log, not the window. When `ok` is false, some line is damaged. The list `events` holds the valid lines of the window only, so it is partial: a damaged line has no entry in it.
- `export()` gives the exact bytes of the file, including damaged lines. A missing log raises `log_missing`. The route adds the headers `X-Schola-Log-Ok` and `X-Schola-Truncated-Tail` so that a caller learns, with the bytes, whether the log is clean.
- `delete(confirm=True)` removes the log file, every reflection file, and every leftover temporary file. It flushes the folders. It returns `{deleted, logRemoved, reflectionsRemoved}`. Without `confirm=True` it raises `confirmation_required` and does nothing. When there is nothing to delete, it raises `log_missing`. It never ends as a silent no-op.

`delete` keeps the lock file.
It does not overwrite the disk blocks of the files, so it is not a secure erase.
A backup tool that already copied the files keeps its copies.
The routes do not check who clicked. The session token does not separate the page script from the app. Any script on the app origin can read the token. That script can call `DELETE`.
Whether a delete moves the files to a backup folder first is a decision for the maintainer. This step does not do it.

### What a read costs

The server handles one request at a time. A read blocks the other requests while it runs.
The first read after the file changes checks every line with the validator. At the 5 MB cap, this takes several seconds on a laptop.
The store keeps the result while the file is unchanged, so the next reads are fast.
The window limit makes the answer small. It does not make the first check cheaper.
An export also needs that check, because the headers say whether the log is clean.
An append changes the file, so the next read pays the check again.

## Reflections

A reflection is 1 to 10000 characters. The store keeps it in its own file, named by a random lowercase UUID, apart from the log.
The log holds only the id and the character count, as the contract says.

- `save_reflection(text)` gives back `{reflectionId, charCount}`. It writes a temporary file, calls `fsync`, renames the file into place, and calls `fsync` on the folder. A crash leaves the whole file or no file.
- A save holds the exclusive lock. It first removes every leftover temporary file, because a temporary file at that point belongs to a save that crashed.
- The store keeps at most 1000 reflections and 16 MB of them. A save past either limit raises `reflections_full` (409). Delete frees the room.
- `get_reflection(id)` gives back `{reflectionId, charCount, text}`.
- The store checks the id against the pattern for a lowercase UUID before it touches the disk. An id with a slash, a dot, or any other character is refused with `invalid_reflection_id`.
- A file that is damaged raises `reflection_corrupt` and stays on disk.
- The text never appears in the log or in an error message. The tests check this.
- If no stored reflection has the same id and `charCount`, the store refuses a `reflection_saved` event. A damaged reflection raises `reflection_corrupt`, not `reflection_mismatch`.

If the flush of the folder fails after the rename, the reflection exists. The error is `fsync_failed`, and its `details` hold `reflectionId` and `charCount`. The caller can fetch the reflection with that id.

## Errors

Every failure has a code. A failed step never reports success.

- `invalid_event` (422): the event does not follow the contract. `details` lists the pointers.
- `unsupported_version` (409): the major version is not 1.
- `event_too_large` (413): the line is over 4096 bytes.
- `duplicate_event_id` (409): the id is already in the log.
- `reflection_mismatch` (422): no stored reflection has the id and the count of a `reflection_saved` event.
- `log_full` (409): the write goes over a cap. A log that is already over the size cap gives `log_full` on an append and `log_corrupt` on a read.
- `log_reserved` (409): the log holds 80% of a cap and the event came from the page.
- `lock_unavailable` (503): the store did not get the lock in two seconds, or the system refused it. Only the timeout is marked retryable.
- `write_failed` (500): the store failed to open or write the file, or the write stopped early. When part of a line reached the file, `details.partialLine` is true. The next append treats that line as a cut line.
- `fsync_failed` (500): the store wrote the data, but the flush failed. `details.maybeWritten` is true. A read after this error does not prove that the line is on disk, because the operating system can serve the line from memory. A retry of the same event gets `duplicate_event_id` once the line is in the file. The store tries the failed folder flush again before the retry.
- `delete_failed` (500): the store failed to remove a file. `details` say what was removed.
- `log_missing` (404): there is no log to export, or nothing to delete.
- `log_unreadable` (500): the store failed to open or read the file.
- `log_corrupt` (500): the store leaves the files as they are. Four cases give it: a log that is not a regular file, a log of more than 50000 lines, a log over 5 MB, and an `_events/` or `reflections/` that is not a folder.
- `unsafe_path` (500): a data path is a symbolic link, or a store folder is not private and owned by the current user.
- `confirmation_required` (400): a delete came without `confirm: true`.
- `reflections_full` (409), `invalid_reflection` (422), `invalid_reflection_id` (400), `reflection_not_found` (404), `reflection_unreadable` (500), `reflection_corrupt` (500): the reflection cases.

The messages name the error code of the system, such as `EIO`. They never name a path and never show reflection text.

An unexpected fault in an event route gives `internal_error` (500) with a plain message.
For an append, the message says that the event is either saved or not saved. The caller reads the log before a retry.

## The routes

All routes need the Host and Origin rules of the server. They also need the session token in the `X-Schola-Check-Token` header, the same token that the check routes use.
A browser request that says it came from another site is refused.
The server checks the token before it reads the body of a POST or a DELETE.
A request body is at most 256 KB.

- `POST /api/events` with `{event}` adds one event and answers 201.
- `GET /api/events?limit=&offset=` answers a window of the report. The limit is 1 to 5000 and the default is 500. The offset is a count of lines and the default is 0. Any other query field, a repeated field, or a value that is not a whole number in range gives 400.
- `GET /api/events/export` answers the file bytes as `application/x-ndjson`, with the two headers.
- `DELETE /api/events` with `{confirm: true}` deletes the log and the reflections. A DELETE with no body gets `confirmation_required`.
- `POST /api/reflections` with `{text}` saves a reflection and answers 201.
- `GET /api/reflections/<id>` answers one reflection.

The page adds events with `source: "student"` only.
Any other source, or no source, gets `403 source_forbidden` and nothing is written.
An event with the source `app` or `agent` comes only through `EventLog.append` inside the server process.
An `agent_offer` event gets `403 type_forbidden` from the page, even with the source `student`. Only the agent writes offers.
This keeps a page from forging a check result.
The contract does not tell a real student event from a made-up one. A page can still send a student event of any type that the contract allows. A hint request is one example.

Types for these routes are in `types/events.d.ts`.

## What E2 proves

- Every event in the log passed the contract validator at write time.
- An invalid event, a repeated id, or a write over a cap leaves the file unchanged.
- Two threads or two processes on one computer never interleave a line and never lose one. Tests run both.
- Of several writers that race with the same event id, exactly one wins.
- A cut last line is reported, kept, and followed by a new line on the next append. Earlier bytes never change.
- A read changes nothing in the log, even on a damaged file.
- A failed write, flush, lock, or delete is a distinct code and never a success.
- A route that is called without the token, from the wrong Host or Origin, or from another site does nothing.
- The page cannot add an event with the source `app` or `agent`, and it cannot fill the last 20% of the log.
- A `reflection_saved` event always names a reflection that was stored at the time of the append.
- A FIFO or a link in place of a store file gives an error and never a hang.

## What E2 does not prove

- It does not rotate the log. A full log stays full until the product decision is made.
- The lock works on one computer. It does not work across a network folder or two computers.
- The lock is advisory. A program that ignores it, or that edits the file by hand, is not stopped. The store finds such a change at the next read and reports damage, but it cannot prevent it.
- On macOS, `fsync` hands the data to the drive. The drive can keep it in its own cache. Schola uses the same call as its other stores. It does not use the stronger `F_FULLFSYNC`.
- A crash between the write and the flush can lose the last event. The caller learns this from the error. A later read does not settle it.
- Windows is not supported. Every call that takes the lock fails with `lock_unavailable`, and that includes read, export, and delete.
- It does not prove that an event is true. The contract cannot tell a made-up student event from a real one, and neither can the store.
- It does not prove that an id, a skill label, or `evidence` holds no name, email address, or path. The contract says the writer must keep them out.
- It does not encrypt anything. The log and the reflections are plain files. Any program that runs as the student can read, change, or delete them.
- The session token stops other web pages. It does not stop another program on the same computer, which can ask for the token. It does not stop a script that runs on the app origin either.
- `delete` does not erase disk blocks and does not keep a backup.
- A read creates a missing lock file. A read on a read-only folder fails with `lock_unavailable`.
- A read blocks the server for the time of the first check after each change. See "What a read costs".
- The cache check cannot see a rewrite that keeps the size, the modified time, and the inode.
- Rate limits do not exist. The reserve keeps room for the app. It does not stop the page from filling the first 80%.
