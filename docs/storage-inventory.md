# Storage inventory before the module storage work

This page records how the app stores curriculum, progress, records, backups, the library, and recovery data today.
It was written before any change to storage.
Later steps compare their results with this page and with the golden data described at the end.

The page cites code as `file:line`.
The line numbers match the commit this work started from.
The code runs on the Python standard library only.

## The data folder

The server takes the folder from `--data-dir`.
The default is `data/` beside `server.py` (`server.py:282`).
The server hands the folder to the store on every request (`server.py:141-143`).

The folder holds these files.
Names in angle brackets stand for a value.

```text
<data-dir>/
  library.json
  records/
    <record-id>/
      record.json
      record.backup.json
      recovery-state.json
      record.damaged-<digest>.json
  <curriculum-id>/
    state.json
  curricula/
    <curriculum-id>.json
```

The first four entries are the current layout.
The last two entries are the original layout from the first builds.
The current code reads the original layout and never writes to it.

## Record: `records/<record-id>/record.json`

A record holds one curriculum and the progress on it.
This is the file that the app treats as the truth.

- The folder name is a canonical lowercase UUID (`local_store.py:54-60`, `:206-208`).
  It must equal the `recordId` inside the file (`local_store.py:228-229`).
- The file has exactly nine fields (`local_store.py:170`).
  - `format` is the text `touchline-record`.
  - `version` is the number `2`.
  - `recordId` is the folder name.
  - `origin` is `{"kind": "local"}` or a cloud origin with `hubId`, `backendOrigin`, and `accountId` (`local_store.py:63-87`).
  - `curriculum` is a full copy of the curriculum, checked by `validate_curriculum` (`server.py:19-71`).
  - `state` is the progress, described below.
  - `status` is one of `active`, `paused`, `completed`, or `saved` (`local_store.py:14`).
  - `view` is `{"selectedModuleId": <lesson id>}`. The lesson must exist in the curriculum (`local_store.py:178-180`).
  - `draft` is `{"experiment": null}` or one unfinished experiment with six text fields (`local_store.py:181-198`).
- The store writes the file as JSON with two-space indentation and one final newline (`local_store.py:138`).
- A local record created from a legacy original gets this ID: `uuid5(MIGRATION_NAMESPACE, "local:" + <curriculum-id>)` (`local_store.py:13`, `:265`).
  The ID of an imported or cloud record is a random UUID (`local_store.py:202`, `:462`).
- A record is new at revision 0 and starts with the first lesson selected (`local_store.py:201-204`).

### Progress: the `state` field

The `state` field is also the format of the original `state.json` file.
`validate` checks it (`server.py:76-114`).

- `version` is `1`.
- `revision` is a whole number from 0 to 9007199254740991 (`local_store.py:15`, `:157`).
- `curriculumId` equals the curriculum ID.
- `weeklyHours` is a number from 1 to 40.
- `startDate` is empty or an ISO date.
- `modules` maps a lesson ID to `status`, `tasks`, `notes`, `evidence`, and `hours` (`server.py:93-106`).
  - `status` is `not-started`, `active`, or `complete`.
  - `tasks` is a list of booleans, one for each task in the lesson.
  - `complete` needs every task true and some evidence text.
- `experiments` is a list of up to 2000 entries. Each has `id`, `date`, `module`, `title`, `hypothesis`, and `result` (`server.py:107-113`).
- Unknown extra fields survive a save. The store checks only the fields above.

The initial state has no lesson entries and an empty experiment list (`server.py:73-74`).
A lesson has an entry only after the student changes it.

### Lesson identifiers today

A lesson ID matches `[a-zA-Z0-9_-]{1,60}` and is unique inside its curriculum (`server.py:50`).
A curriculum ID matches `[a-z0-9][a-z0-9-]{0,59}` (`server.py:20`).
The built-in soccer curriculum uses `soccer-ml` and the lesson IDs `m01` to `m13`.
A task has no ID. Progress refers to a task by its position in the `tasks` list (`server.py:98`).

## Backup: `records/<record-id>/record.backup.json`

Every save of an existing record copies the old `record.json` bytes to `record.backup.json` first (`local_store.py:139-141`, `:374`).
The backup is one step deep.
Saving again replaces it.
The store never writes a backup for a first write or for `library.json`.
Backup and record are written in this order: backup, then record.
A failed record write leaves both files with the old content (tested in `tests/test_records.py:111`).

Two other things are also called backups.

- The browser export is the record itself (`server.py:221-223`). The `/export` route returns the same record as the plain route.
- An older export file has the form `{"format": "touchline-backup", "curriculum": ..., "state": ...}`. The `format` field is optional there. The store accepts this form on import only (`local_store.py:463-472`).

## Library: `library.json`

The library file holds the focus.
It has exactly three fields (`local_store.py:311`).

- `version` is `1`.
- `revision` is a whole number.
- `focusRecordId` is a record ID or `null`. Only an `active` record can be the focus (`local_store.py:318`, `:355`).

A missing file counts as `{"version": 1, "revision": 0, "focusRecordId": null}` (`local_store.py:309`).
When the student saves a new focus, the store writes the file (`local_store.py:357-358`).
It also writes the file to clear a focus that points to a missing or inactive record (`local_store.py:318-320`).

The list of records that the app shows is not stored.
`library()` builds it from the record files on each read (`local_store.py:323-332`).

## Writing and reading rules

- Every write goes to a temporary file in the same folder, then flushes, calls `fsync`, and renames over the target (`local_store.py:121-135`).
  The temporary file name starts with `.touchline-` and ends with `.tmp` (`local_store.py:126`).
- The store refuses symbolic links and `..` in any data path (`local_store.py:98-112`).
- A file that cannot be parsed raises `corrupt_file` and stays on disk (`local_store.py:114-119`).
  The same holds for an invalid record, library, or original (`local_store.py:231-236`, `:316-317`, `:269-272`).
- A record with another `format` or `version` is never a recovery candidate (`local_store.py:168-169`, `:391-392`).
- Two records cannot share an origin and curriculum ID (`local_store.py:237-240`).
- The body of an HTTP request has a limit of 2,000,000 bytes (`server.py:193`).

## Revisions and conflicts

A save carries the revision that the student last read, called `expectedRevision`.
If it differs from the stored revision, the store raises `revision_conflict` with status 409 (`local_store.py:340-346`).
A successful save adds 1 to the revision (`local_store.py:373`).
The record keeps its revision in `state.revision`.
The library keeps its own in `library.json` (`local_store.py:348-359`).

## Original files from the first builds

The first builds wrote these files.
The current code reads them and does not write them.

- `<curriculum-id>/state.json` holds the progress of one curriculum.
  The built-in curriculum has this file in older data folders.
- `curricula/<curriculum-id>.json` holds an imported curriculum.
- `<curriculum-id>/state.backup.json` held the previous progress.
  The current code does not read it.

The first builds wrote these files with `json.dump(value, indent=2)` and no final newline.
This is visible in the repository history.

Migration runs at the start of nearly every read and write (`local_store.py:275-280`).

1. It reads every record and finds which curriculum IDs already have a local record (`local_store.py:247-248`).
2. It lists the built-in curriculum and every `curricula/*.json` file (`local_store.py:249-252`).
3. It checks every original before writing anything (`local_store.py:254-264`).
4. It creates a record for each curriculum that has none. If `<curriculum-id>/state.json` exists, the state comes from that file. If not, the state is the initial state (`local_store.py:262-268`).
5. It writes only the new `record.json` files (`local_store.py:278-279`).

A single invalid original stops all migration and writes nothing (`local_store.py:269-272`).
Migration never edits or deletes an original.
Once a record exists, the store ignores the original for that curriculum.
Edits to a migrated curriculum happen in the record only.
`save_legacy_state` writes to the record, not to `state.json` (`local_store.py:379-383`).
This means a `state.json` file can fall behind its record.
A test checks that the original stays unchanged (`tests/test_records.py:36`).

## Recovery files

Recovery restores a damaged `record.json` from its backup.

- A record is recoverable if `record.json` is unreadable or invalid.
  Its `record.backup.json` must also be a valid local record with the same ID (`local_store.py:385-419`).
- The review digest is `sha256(damaged bytes + 0x00 + sorted JSON of the backup and the reserved revision)` (`local_store.py:431-434`).
- `recover_record` runs these steps in order (`local_store.py:436-451`):
  1. If the digest differs from the one the student reviewed, it stops.
  2. It copies the damaged bytes to `record.damaged-<digest>.json` (`local_store.py:443`).
  3. It writes `recovery-state.json` as `{"version": 1, "revision": <n>}` (`local_store.py:448`).
  4. It writes the backup as the new `record.json` with revision `n` (`local_store.py:449-450`).
- The new revision is two above the backup revision. If the reserved revision is larger, the new revision is one above it (`local_store.py:447`).
  A writer that holds an older copy can no longer save.
- The store keeps the backup and every damaged file.

## HTTP routes that touch storage

- `GET /api/recovery`, `GET /api/curricula`, `GET /api/state`, `GET /api/record`, `GET /api/library`, and `GET /api/records/<id>[/export]` read (`server.py:202-223`).
  Every read except the recovery list runs migration first, so a read can create record files.
- `PUT /api/state`, `PUT /api/curricula`, `PUT /api/library`, and `PUT /api/records/<id>` write (`server.py:232-254`).
- `POST /api/import/preview` writes nothing. `POST /api/import` and `POST /api/recovery` write (`server.py:256-277`, `local_store.py:453-501`).

## Added by the event log step (E2)

This page was written before any change to storage. The event log step adds one folder to the data folder.
`docs/event-log.md` describes it in full. The older sections above stay as they were.

```text
<data-dir>/
  _events/
    events.jsonl
    .events.lock
    reflections/
      <reflection-id>.json
```

- `events.jsonl` is the learning event log. It follows `contracts/learning-events.md`. The store appends to it and never edits a line (`event_log.py`). The one way the store removes it is `DELETE /api/events` with `confirm`, which also removes every reflection.
- `.events.lock` is the lock file. It holds no data and the store never deletes it.
- `reflections/<reflection-id>.json` holds one reflection. The name is a random lowercase UUID. The file holds `version`, `reflectionId`, `charCount`, and `text`. The store keeps at most 1000 of them and 16 MB in all.
- The folder name starts with the character `_`, as `_lessons/` does. No curriculum id starts with that character.
- The store creates `_events/` and `reflections/` with mode `0700` and files with mode `0600`. It creates the data folder with mode `0700` when it has to make it. A store folder that already exists must belong to the current user and allow no access to others, or the store refuses to write.
- Older builds ignore the folder. The record, library, and recovery code never reads or writes it.
- The routes are `POST /api/events`, `GET /api/events`, `GET /api/events/export`, `DELETE /api/events`, `POST /api/reflections`, and `GET /api/reflections/<id>`. All need the session token (`event_routes.py`).
- A log over the 5 MB cap gives `log_full` on an append and `log_corrupt` on a read. A store folder that is a regular file gives `log_corrupt`.
- The log is local and is not encrypted. Any program that runs as the student can read it. The privacy limits are those of the contract and of `docs/event-log.md`.
- A request body for these routes has a limit of 262,144 bytes.

## Not covered here

- The storage of the native iOS app. This page did not read it.
- Any real learner data folder. None was available, so the original-file formats come from the code and the repository history.

## Golden data

The folder `tests/fixtures/storage/pre-s03/` freezes three data folders.
Synthetic content only.
No file holds real learner data.

- `current-layout/` has the soccer record at revision 2, one finished lesson, one active lesson, one experiment, one draft, a selected lesson, the one-step backup, and `library.json` with a focus.
  The real server wrote it through its HTTP routes.
- `legacy-layout/` has the original files: `soccer-ml/state.json`, `soccer-ml/state.backup.json`, an imported `curricula/legacy-sample.json`, and its `legacy-sample/state.json`.
  The test code writes them in the format of the first builds, because the current code cannot.
- `recovery-layout/` has a damaged record that the real server restored from its backup.
  It holds the restored `record.json`, the backup, `recovery-state.json`, and the damaged copy.
- `manifest.json` lists the SHA-256 digest of every file above.

`tests/test_storage_golden.py` checks four things:

1. Every file matches the manifest, and the manifest lists every file.
2. The current code reads each layout and leaves every byte unchanged on reads.
3. The current code writes the same bytes again when a save carries unchanged content.
4. The current code builds the same bytes from scratch.

Later steps must keep this test green.
Do not rebuild the fixture to make a test pass.
To create it again on purpose, run `python3 tests/test_storage_golden.py --write` from the repository root and review the diff.
