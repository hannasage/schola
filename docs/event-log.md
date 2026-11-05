# The event log store

This page describes step E2: the local store for learning events.
It builds on the learning event contract from step E1 (`contracts/learning-events.md`).
The contract says what an event is and what the log file looks like.
This page says how Schola writes, reads, exports, and deletes that file, and what that work proves.

The code is `event_log.py` (the store) and `event_routes.py` (the local server routes).
The tests are `tests/test_event_log.py`, `tests/test_event_reflections.py`, and `tests/test_event_routes.py`.
The code uses the Python standard library only.

## Terms

- The log is the file `events.jsonl`. It holds one event on each line.
- An append adds one event to the end of the log.
- A cut line is a last line that has no newline because a write stopped early. The contract calls it a truncated tail.
- A lock is a promise between programs that only one of them writes at a time. Schola uses the operating system call `flock`.
- `fsync` is the call that asks the operating system to write a file to the disk now.
- A reflection is a short text the student writes in their own words. The contract keeps it out of the log.

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

The store creates the folders with mode `0700` and the files with mode `0600`.
This keeps other user accounts out. It does not keep out a program that runs as the student.

## How an append works

1. The store turns the event into compact JSON text and gives the text to `learning_event.validate_event_text`. That is the one set of rules. The store has no copy of them.
2. If the event is not valid, the store raises an error and writes nothing. It does not even create the folder.
3. The store takes the exclusive lock. If it does not get the lock, it raises `lock_unavailable`.
4. If the file changed since the last read, the store reads the log again. It builds the list of event ids and counts the lines.
5. If the id of the event is already in the log, the store refuses the event (`duplicate_event_id`).
6. If the write puts the log over a cap, the store refuses the event (`log_full`).
7. If the log is not empty and does not end in a newline, the store puts one newline before the event. It never changes earlier bytes.
8. The store writes the line in one `write` call with the append flag, then calls `fsync`.
9. If it created the log, it also calls `fsync` on the folder, so the new file name survives a crash.
10. The store gives back the line number of the event.

The id list is held in memory. The store checks the size, the modified time, and the inode of the file before it trusts the list. If any of them changed, it reads the file again.
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

## Reading, exporting, and deleting

- `read()` gives the report from the validator: a list of lines, each valid or damaged, with its number, its reason, and its error pointers. It adds the valid events and the flag `truncatedTail`. It never repairs or drops anything. A missing log reads as empty with `exists` set to false.
- `export()` gives the exact bytes of the file, including damaged lines. A missing log raises `log_missing`.
- `delete(confirm=True)` removes the log file and calls `fsync` on the folder. Without `confirm=True` it raises `confirmation_required` and does nothing. A missing log raises `log_missing`. It never ends as a silent no-op.

`delete` removes the log file only. It does not remove the lock file or any reflection.
It does not overwrite the disk blocks of the file, so it is not a secure erase.
A backup tool that already copied the file keeps its copy.

## Reflections

A reflection is 1 to 10000 characters. The store keeps it in its own file, named by a random lowercase UUID, apart from the log.
The log holds only the id and the character count, as the contract says.

- `save_reflection(text)` gives back `{reflectionId, charCount}`. It writes a temporary file, calls `fsync`, renames the file into place, and calls `fsync` on the folder. A crash leaves the whole file or no file.
- `get_reflection(id)` gives back `{reflectionId, charCount, text}`.
- The store checks the id against the pattern for a lowercase UUID before it touches the disk. An id with a slash, a dot, or any other character is refused with `invalid_reflection_id`.
- A file that is damaged raises `reflection_corrupt` and stays on disk.
- The text never appears in the log or in an error message. The tests check this.

The store does not limit how many reflections it keeps.

## Errors

Every failure has a code. A failed step never reports success.

- `invalid_event` (422): the event does not follow the contract. `details` lists the pointers.
- `unsupported_version` (409): the major version is not 1.
- `event_too_large` (413): the line is over 4096 bytes.
- `duplicate_event_id` (409): the id is already in the log.
- `log_full` (409): the write goes over a cap.
- `lock_unavailable` (503, retryable): the store did not get the lock in two seconds, or the system refused it.
- `write_failed` (500): the store failed to open or write the file, or the write stopped early. When part of a line reached the file, `details.partialLine` is true. The next append treats that line as a cut line.
- `fsync_failed` (500): the store wrote the data, but the flush failed. `details.maybeWritten` is true. Before a retry, read the log. If the line is there, a retry of the same event gets `duplicate_event_id`.
- `delete_failed` (500): the store failed to remove the file.
- `log_missing` (404): there is no log to export or delete.
- `log_unreadable` (500): the store failed to open or read the file.
- `log_corrupt` (500): the log is not a regular file, or it is over the caps because another program wrote it. The store leaves it as it is.
- `unsafe_path` (500): a data path is a symbolic link.
- `confirmation_required` (400): a delete came without `confirm: true`.
- `invalid_reflection` (422), `invalid_reflection_id` (400), `reflection_not_found` (404), `reflection_unreadable` (500), `reflection_corrupt` (500): the reflection cases.

The messages name the error code of the system, such as `EIO`. They never name a path and never show reflection text.

## The routes

All routes need the Host and Origin rules of the server. They also need the session token in the `X-Schola-Check-Token` header, the same token that the check routes use.
A browser request that says it came from another site is refused.
A request body is at most 256 KB.

- `POST /api/events` with `{event}` adds one event and answers 201.
- `GET /api/events` answers the report.
- `GET /api/events/export` answers the file bytes as `application/x-ndjson`.
- `DELETE /api/events` with `{confirm: true}` deletes the log.
- `POST /api/reflections` with `{text}` saves a reflection and answers 201.
- `GET /api/reflections/<id>` answers one reflection.

The page adds events with `source: "student"` only.
An event with the source `app` or `agent` gets `403 source_forbidden` and nothing is written.
Those events come only through `EventLog.append` inside the server process.
This keeps a page from forging a check result.
The contract does not tell a real student event from a made-up one. A page can still send a student event of any type that the contract allows. A hint request is one example.

Types for these routes are in `types/events.d.ts`.

## What E2 proves

- Every event in the log passed the contract validator at write time.
- An invalid event, a repeated id, or a write over a cap leaves the file unchanged.
- Two threads or two processes on one computer never interleave a line and never lose one. Tests run both.
- Of several writers that race with the same event id, exactly one wins.
- A cut last line is reported, kept, and followed by a new line on the next append. Earlier bytes never change.
- A read changes nothing, even on a damaged file.
- A failed write, flush, lock, or delete is a distinct code and never a success.
- A route that is called without the token, from the wrong Host or Origin, or from another site does nothing.
- The page cannot add an event with the source `app` or `agent`.

## What E2 does not prove

- It does not rotate the log. A full log stays full until the product decision is made.
- The lock works on one computer. It does not work across a network folder or two computers.
- The lock is advisory. A program that ignores it, or that edits the file by hand, is not stopped. The store finds such a change at the next read and reports damage, but it cannot prevent it.
- On macOS, `fsync` hands the data to the drive. The drive can keep it in its own cache. Schola uses the same call as its other stores. It does not use the stronger `F_FULLFSYNC`.
- A crash between the write and the flush can lose the last event. The caller learns this from the error, or from the missing line at the next read.
- Windows is not supported. Writes there fail with `lock_unavailable`.
- It does not prove that an event is true. The contract cannot tell a made-up student event from a real one, and neither can the store.
- It does not prove that an id, a skill label, or `evidence` holds no name, email address, or path. The contract says the writer must keep them out.
- It does not encrypt anything. The log and the reflections are plain files. Any program that runs as the student can read, change, or delete them.
- The session token stops other web pages. It does not stop another program on the same computer, which can ask for the token.
- `delete` does not remove reflections and does not erase disk blocks.
- Nothing limits how many reflections are saved.
- A stale temporary file from a crash during a reflection save is not cleaned up. It holds a whole reflection under a name that no id points to.
