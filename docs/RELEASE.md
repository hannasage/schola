# Release and recovery

Use this guide with a checkout that contains all approved release changes.
Keep the repository private until publication is approved.
The public deployment template stays disabled during that review.
Native editing, automatic two-way sync, and App Store distribution are outside this release.

## Clean local installation

Install Python 3.10 or later.
The committed web bundles can run without Node.js or an account:

```sh
python3 server.py
```

Open http://127.0.0.1:8765 and keep the terminal open.
Use Control-C to stop the server.
The default learning directory is `data/` beside `server.py`.
For a separate acceptance run, start with a new temporary directory:

```sh
TOUCHLINE_CHECK_DATA=$(mktemp -d)
python3 server.py --port 8766 --data-dir "$TOUCHLINE_CHECK_DATA"
```

Open http://127.0.0.1:8766 for that run.
Keep real learning files and deployment credentials out of this directory.
Retain it until you review the results.

## Upgrade and backups

1. Export progress for each curriculum with work to retain. Each version 2 export includes its curriculum, progress, draft, and selected lesson.
2. If saving fails, export the working copy before closing the page. Browser recovery storage can be unavailable.
3. Stop the local server. Copy the complete data directory to a separate retained location, including `library.json`, record folders, backup files, and the `_lessons/` folder when it exists.
4. Keep deployment configuration and secrets in a separate private backup. Never add them to Git or a release attachment.
5. Preserve the earlier application checkout. Install the reviewed application revision in a separate checkout and retain its exact Git commit ID.
6. Make a separate working copy of the backed-up data directory. Keep the backup untouched.
7. Start the new application with that working copy using `--data-dir`. Confirm both curricula, focus, lesson selection, notes, and unfinished experiments before publishing anything.

Do not erase browser storage during an upgrade.
It can contain unsent drafts and pending publication requests.
Theme preferences are local to each browser origin or native device and are not part of progress exports.

Import curriculum accepts a full backup when its curriculum does not already exist.
The review defaults to Save for later and offers another active curriculum.
For an existing curriculum, select it and use Restore backup.
Export the current record before accepting replacement.
A version 1 progress-only backup requires its matching curriculum.

## Lesson modules and the `_lessons/` folder

A lesson module is one lesson with one outcome. A packet is a list of lesson modules, each with an exact version.
Schola stores imported lessons in a new folder, `_lessons/`, inside the data directory.
The folder appears with the first import. A data directory with no lessons has no `_lessons/` folder.

```text
<data-dir>/
  _lessons/
    modules/<module-id>/<version>/module.json
    modules/<module-id>/<version>/source.json
    packets/<packet-id>/<version>/packet.json
    packets/<packet-id>/<version>/source.json
    pins.json
    help/<module-id>.json
    map/<curriculum-id>.json
    progress/<record-id>/<lesson-id>.json
    snapshots/s03/<record-id>/
    migrated.json
```

- `module.json` and `packet.json` hold the exact bytes that were imported. `source.json` holds the digest, the file name, and the import time.
- `pins.json` says which version of each module the student uses. It has a revision number.
- `help/` holds help records. This release stores and exports them. It does not yet add new ones.
- `map/`, `progress/`, `snapshots/`, and `migrated.json` come from the migration described below.
- A curriculum ID cannot start with an underscore. The folder never clashes with a curriculum folder.
- Older builds do not read `_lessons/`. They read only `records/`, `curricula/`, and `<curriculum-id>/state.json`.

How an import works:

1. Choose one or more lesson files with Import curriculum, or one lesson backup with Import curriculum or Restore backup. Choose a packet and its modules together in one selection. A file with a `schemaVersion` goes to the lesson review. A curriculum or a progress backup goes to the older review, which has not changed. Several files work only when all of them are lesson files. A file that holds a list of lessons is not a lesson file, because Schola must keep the exact bytes of each file.
   Schola decodes each file as strict UTF-8. A file that is not valid UTF-8 is refused with a message, and Schola makes no call. It sends the text of each file as it is, so the server sees the same bytes and rejects a repeated key. Before it sends a request, Schola checks that the whole request is under 5,000,000 bytes. A larger request is refused on the page with a message that names both limits. Quotes and backslashes in a file count double in a request, so a file near 4.5 MB can still be too large to send.
2. The review is read only. It shows each lesson, its tasks and checks, its license, its price, a tutor harness if it has one, and any missing prerequisite. Choosing the file makes one preview call, which writes nothing. Cancel makes no further call.
3. Add these lessons is the only step that writes. The server checks every file again, writes all new folders together, and writes `pins.json` last.
4. A damaged file, an unsupported version, a file over the size limit, a packet that names a module Schola does not have, or a conflict stops the whole import. Nothing is written.

Every write goes to a temporary file first. Schola flushes the file, renames it into place, and then flushes the folder that holds it.
For an import, Schola flushes every new folder and `_lessons/` itself before it writes `pins.json`, and flushes `_lessons/` again after.
A power cut leaves either the old file or the new one, never a mix.

A version is written once. If you import the same ID and version with the same bytes, nothing changes.
If you import the same ID and version with different bytes, Schola refuses the file and keeps the stored one.

### After a crash during an import

A crash can leave two kinds of leftovers: a `.staging-*` folder in `_lessons/`, and `.touchline-*.tmp` files beside the files being written.
The next import, restore, or migration removes only those older than one hour, so it never removes the files of a write that is running now.
A crash between the folder renames can also leave module folders that no pin names yet.
Those folders do no harm. The module list shows them with no version in use.
Import the same file again. Schola reports it as already stored, writes the missing pin, and finishes the job.

If a write fails and Schola cannot undo part of it, Schola does not hide that. It answers with the code `rollback_incomplete` and names the paths, relative to the data directory.
Check those paths before you try again.

## Version pins and updates

The first version of a module becomes its pin, because nothing is replaced.
A later version is stored but never moves the pin.
The module list says "a newer version is available" and offers the switch.
Schola never switches by itself.
A switch needs two steps in the app: Review the switch, then Switch to the new version.
Choosing Keep the current version, or closing the screen, changes nothing.
The server also refuses a pin change that does not carry `confirm: true`.
A switch must also carry `expectedRevision`, the revision of the lessons you last read. Without it the server answers 422 with the code `expected_revision_required`. If the lessons changed since you read them, the server answers 409 `revision_conflict` and changes nothing.

## Migration of existing curricula

Existing curricula keep working with no lesson files.
`POST /api/lessons/migrate` runs a one-time migration when you ask for it.
This release does not call it on its own.

The migration works in this order:

1. It checks every original file. If one is invalid, it writes nothing.
2. It copies each original into `_lessons/snapshots/s03/<record-id>/` and checks the copy against its digest. The folder also holds `manifest.json`, which lists the digests.
3. It writes `map/<curriculum-id>.json`, which gives each lesson an ID that fits the lesson rules. An ID in a map never changes. A refresh adds an ID for a module added to the curriculum later. The ID is the curriculum ID and the lesson ID joined by a hyphen, in lowercase, with each underscore changed to a hyphen. If that ID is not valid or is already taken, Schola uses the start of the curriculum ID and eight characters of a digest instead. The table is written once and never recomputed. Task number N becomes `task-N`.
4. It writes `progress/` files for each lesson. They are copies of the record. Schola refreshes them when the migrate route runs again after a change, or when you run the command line `refresh`. Saving a record does not refresh them, so a progress file can lag behind its record until then. The record is always the source of truth.
5. It writes `migrated.json` last. A second run finds it and changes nothing.

`records/<record-id>/record.json` stays the source of truth for an existing curriculum.
The migration builds IDs only. It builds no lesson module for the existing curricula.
If a step fails, Schola removes what that run created.

### Roll back the migration

The migration and the lesson import never change an original file.
The originals are `records/<record-id>/record.json`, `<curriculum-id>/state.json`, and `curricula/*.json`.
To go back to a build that knows nothing about lessons:

1. Stop the server with Control-C.
2. Move the whole `_lessons/` folder out of the data directory. Do not delete it. Keep it in a separate folder in case you want it later.
3. Start the earlier build with `--data-dir` pointing at the same data directory.

To put an original file back from its snapshot, use the command line with the server stopped:

```sh
python3 lesson_migrate.py restore --data-dir <data-dir> --confirm
```

This restore is the one place in the lesson code that may overwrite an original record file.
It exists only to undo the migration, and it does nothing without `--confirm`.
It checks every snapshot against its digest first. A snapshot that is unfinished or damaged stops it before any write.
Work saved after the migration lives in the live file, so the restore refuses to replace a record whose revision is newer than its snapshot.
It lists those files and writes nothing. Add `--force` only when you want to give that work up.
Before it replaces any file, it keeps the old bytes under `_lessons/snapshots/s03-replaced/`.
It never deletes a file, and it is all or nothing: if a write fails, it puts back what it had replaced.
If that also fails, it stops with the code `partial_restore` and lists the files that stay replaced.

Run the command line `migrate`, `refresh`, and `restore` commands only while the server is stopped.
The pin file is read, changed, and written without a lock, so a command and a running server could overwrite each other.

## Lesson backup: export and restore

A lesson backup is a separate file with the format `schola-lesson-backup`.
The older progress backup format has not changed.
A lesson backup holds every stored module and packet as exact text with its digest, the pins, and the help records.
It holds no progress. Export progress for each curriculum as before.

To export, open `http://127.0.0.1:8765/api/lessons/export` in the browser and save the answer as a `.json` file.
Schola refuses an export that would pass 5 MB.

To restore, choose the file with Import curriculum. The review is titled Restore these lessons.
Schola checks the whole file before it writes anything. It checks the format, every digest, every module and packet, and every reference.
A restore is all or nothing.
Restoring into an empty data directory rebuilds the same files.
Restoring into a used data directory only adds what is missing:

- A version that is already stored with the same bytes is skipped.
- A version stored with different bytes stops the restore.
- A pin is set only for a module that has no pin. A restore never moves a pin.
- Help records are added after the stored ones, with no repeats.

## Size limits

A lesson request may be up to 5 MB. A single lesson file may be up to 5 MB. A request may hold up to 2000 files.
A larger request gets status 413 and writes nothing.
The other routes keep their 2 MB limit.
A request that declares a body and never sends it is dropped after 10 seconds with status 408, so it cannot hold the server.

The lesson library has a limit too, so a full library can always be exported and restored inside the 5 MB file size.
These are the defaults:

- The lessons may add up to 4,000,000 bytes to a backup. Schola counts each stored file as it is written into a backup, with its escaped text and about 2 KB for the fields around it, plus the help records.
- The library may hold up to 500 module and packet versions.

An import or restore that would pass either limit gets status 413 with the code `store_full` and changes nothing. The preview says so too.
Because of the first limit, a single lesson file of more than about 4 MB cannot be stored, even though a request may carry 5 MB.

## Lesson routes

All lesson routes use the local address and the same Host and Origin checks as the other routes.

| Route | Use |
|---|---|
| `GET /api/lessons` | List modules, their pins, their versions, and any newer version on offer |
| `GET /api/lessons/export` | Return the lesson backup |
| `POST /api/lessons/import/preview` | Check a file and say what an import would do. Writes nothing |
| `POST /api/lessons/import` | Import a module or packet, or restore a lesson backup |
| `POST /api/lessons/pins` | Move one pin. Needs `confirm: true` |
| `POST /api/lessons/migrate` | Run the one-time migration. Send an empty object |

Import and preview take `{source, filename}`. The `source` is the file text, a list of file texts, or one parsed document. The app always sends text. Send text whenever the exact bytes matter, because a parsed document is stored as the server writes it back out.
The error codes are: 422 `invalid_lesson`, `missing_module`, and `expected_revision_required`, 409 `version_conflict`, `unsupported_version`, `revision_conflict`, and `revision_limit`, 413 `payload_too_large` and `store_full`, 408 `request_timeout`, 400 for a malformed request, and 500 `internal_error` for a fault in stored data.
A 500 `internal_error` answer carries no path and no trace. The details go to the server console only.

## Local checks

A lesson check can run on the student's computer. The check runner has two tiers.

- Tier 1 checks are built in: `file-exists`, `text-match`, and `json-valid`. They run no creator code. The field names are in [Contracts](../contracts/README.md#built-in-check-runners). A check type the app cannot run is skipped, and the student reports the result by hand.
- Tier 2 checks run a script written by the lesson author. They run only after the student sees the script and agrees.

The check routes use the local address and the same Host and Origin checks as the other routes. A request body may be up to 64 KB.

| Route | Use |
|---|---|
| `GET /api/checks/session` | Return the token for this launch of the server |
| `GET /api/checks/workspace` | Say which folder checks read, and whether it is still usable |
| `POST /api/checks/workspace` | Set that folder. Send `{path}` |
| `POST /api/checks/inspect` | Say what a check would run, with its hash. Send `{moduleId, version, checkId}` |
| `POST /api/checks/consent` | Record the student's agreement to one script. Send `{moduleId, version, checkId, confirm: true, shownHash}` |
| `POST /api/checks/run` | Run one check. Send `{moduleId, version, checkId}`. A script check also needs the `consentId` that the consent route returned |

Every route after the first needs the token in the `X-Schola-Check-Token` header. The server makes the token when it starts and compares it in constant time. The first route gives the token only to a request that passes the Host and Origin checks and that no browser marks as cross-site.

### The workspace folder

The workspace is the one folder that checks read. It is saved in `_lessons/check-workspace.json` in the data folder. The path must exist, be a folder, and be resolved with its real path. The folder must be inside the home folder. Schola refuses the home folder itself, any folder that holds it, the top of the disk, any folder outside the home folder (so `/etc` and an external drive are refused), any folder with a name that starts with a dot anywhere below the home folder (so `~/.ssh` is refused), the data folder, any folder inside the data folder, and any folder that holds the data folder. It checks again each time it runs a check.

### Consent

- A consent record is tied to the module, the version, the check, the SHA-256 hash of everything that would run, and the real path of the workspace. The hash covers the runner fields, the runtime, the arguments, the time limit, and the script bytes. If any of these change, the old consent no longer applies.
- Only `POST /api/checks/consent` creates a consent. It needs `confirm: true` and a `shownHash` equal to the hash the server computes now. The run route has no field and no header that can grant consent. A run request with an unknown field is refused.
- Consent lives in memory. It ends when the server stops.
- A run for a script needs the `consentId` that the consent route gave back. Schola looks the id up and checks that it was given for this module, version, check, hash, and folder. A matching binding without the id is not enough.
- A version 1.0 `script` runner holds no script. It needs no consent. Schola skips it, and the student reports the result by hand.
- A built-in check needs no consent.
- A run for a script without a valid consent answers 403 `consent_required`, and no process starts.
- One check runs at a time. A second run request while one is going answers 429 `busy`.

The consent screen is `web/screens/check-consent.js`. It shows the runtime, each argument on its own line and whole, the time limit, the folder, the hash, the limits that apply and the limits that do not apply, and the script. Every control, invisible, direction, and line-separator character in the script and the arguments is shown as a tag such as `<U+202E>`. Only line feeds and tabs stay plain. A banner warns when the script holds characters outside plain ASCII. It says that this is consent, not a sandbox. Cancel and Escape make no call. No other screen opens it yet.

Error codes (the `code` of an HTTP answer): 403 `invalid_token`, `consent_required`, and `forbidden_origin`, 400 `invalid_workspace`, `confirmation_required`, `consent_not_needed`, `invalid_request`, and `invalid_json`, 415 `unsupported_media_type`, 408 `request_timeout`, 409 `workspace_required` and `hash_mismatch`, 404 `not_found`, 413 `payload_too_large`, 429 `busy`, and 500 `internal_error` or `corrupt_file`.

A check that runs answers 200 even when it cannot finish. The `result.evidence.reason` then says why. The reasons are:

- Any check: `invalid_check`, `invalid_descriptor`, `unknown_runner_type` (status `skipped`), `self_reported` (status `skipped`), `workspace_unavailable`, and `adapter_failed`.
- File checks: `not_found` and `wrong_kind` (status `failed`), `unsafe_path`, `not_regular`, `file_too_large`, and `unreadable`. Also `empty`, `invalid_json`, `wrong_top_level`, and `missing_keys` (status `failed`).
- Pattern checks: `invalid_pattern`, `pattern_timeout`, `pattern_failed`, and `kill_failed`.
- Git checks: `no_repository` and `no_commits` (status `failed`), `unsafe_git_dir`, `untrusted_repository`, `git_failed`, `git_timeout`, `output_too_large`, `kill_failed`, and `git_unavailable` (status `skipped`).
- Script checks: `consent_required`, `busy`, `script_not_defined` and `runtime_unavailable` (status `skipped`), `workspace_too_large`, `unsafe_workspace`, `unreadable_workspace`, `script_not_started`, `script_failed`, `script_timeout`, `script_killed`, `output_too_large`, and `kill_failed`.

### The git commit check

The `git-commit-exists` check runs git on the student's repository. A repository can name programs in its own config, so Schola treats it as untrusted.

- Git runs from an argument list, never through a shell, with the workspace as its working folder and a clean environment. No `GIT_*` variable of the server reaches it. System and user config are switched off.
- Schola checks the `.git` entry first. It must be a real folder, or a plain file (a gitfile) that names a folder inside the workspace. A link is refused. A gitfile that names a folder outside the workspace is refused. A borrowed object store is refused. Any git entry that is a link or a pipe is refused. Schola then names the folder with `--git-dir`, so git never walks up to a parent repository.
- A fixed list of `-c` overrides switches off the settings that name a program: file system monitor, hooks path, SSH command, pager, editor, ask-pass program, alternate refs command, attributes and ignore files, external diff, credential helper, signature program, pack hook, and automatic housekeeping.
- Schola reads the repository config as text before git runs. It accepts only a short list of plain-data settings, such as `user.name`, `remote.<name>.url`, and `branch.<name>.merge`. It refuses a setting that names a program, an `include`, an `extensions` key other than `objectformat`, a partial clone or promisor setting, a `ext::` remote address, and a `.promisor` pack file. A partial clone fetches a missing object by running a transport, and a hostile repository can name a program for it. Git LFS repositories hold a `filter` setting and are refused too.
- The environment also sets `GIT_NO_LAZY_FETCH=1` and `GIT_ALLOW_PROTOCOL=none`, and `-c protocol.allow=never` closes every transport. On git 2.39.5 (Apple build) the first one is honored: git printed that lazy fetching was disabled. A test turns the config check off. It showed that removing either the lazy-fetch variable or the protocol settings still stops the transport, and that removing both lets git run the program.
- Only `rev-parse`, `show-ref`, `rev-list`, and `cat-file --batch` run. `show-ref` runs only to tell an empty repository from a damaged one: no commits counts as "no commits yet" only when `HEAD` names a branch and the repository holds no refs. Any other case is an error. Each run has a time limit of 10 seconds and an output limit of 1 MB. Past either limit, Schola kills git and every process it started.
- Schola checks that the current user owns the repository. Git skips its own ownership check when it is given `--git-dir`, so Schola repeats it. Schola never passes `safe.directory`.

What the tests proved, on git 2.39.5 (Apple build) on macOS. Each hostile repository was built at test time. A control step first showed that a dangerous command runs the marker program.

- With a repository whose config names a file system monitor, hooks, an SSH command, a pager, an editor, an external diff, a text converter, a credential helper, a signature program, a pack hook, and an included file that sets more of them, the read commands run none of them. This holds with the overrides and also without them. These tests turn the config check off, so they prove the second layer. With the config check on, such a repository is refused before git starts.
- A partial-clone repository with a missing commit makes plain git run the program named in `remote.<name>.uploadpack`. Schola refuses the repository, and with the config check off the environment layers stop it.
- The overrides do stop `status`, `diff`, `commit`, `stash`, and `ls-remote` from running the file system monitor, the hooks, the external diff, and the SSH command. The overrides stay as a second layer, in case a later git reads more.
- A pager starts only on a terminal. Schola gives git a pipe, `--no-pager`, and `core.pager=cat`.
- Hooks in the repository's own `hooks` folder and in a configured hooks path did not run.
- The `.git` link, the gitfile that points outside, the borrowed object store, and a git entry that is a pipe are refused before git starts.
- Git and a child of git that hang are killed with their process group. Output past the limit is cut off.

What the tests did not prove:

- A text converter or a clean or smudge filter is named by a `.gitattributes` rule, so no fixed override can switch it off. `log -p` still ran a text converter with every override set. Schola stays safe only because it never runs a command that makes a patch or checks out files. A test guards the list of commands and flags.
- Other git versions. A newer git may read more settings. Other operating systems. Windows was not tested.
- A repository on a network or a special file system.
- Objects that a repository reads through `GIT_ALTERNATE_OBJECT_DIRECTORIES` or a replace ref. Schola clears the variable and passes `--no-replace-objects`, but did not build a test for every route.
- Whether git's own ownership check and Schola's agree on every system. Schola's check compares the owner of the workspace and of the git folder with the current user.

### Script checks

A script check (tier 2) runs `python3` or `node` on a script from the lesson. It needs schema version 1.1. The student must see the script and agree first. Without a valid consent for the exact hash, no process starts. Consent is not isolation. The script can still read the student's files and use the network. It runs with the student's own permissions.

What Schola does to limit a script:

- It copies the workspace into a fresh scratch folder under the temp folder. The copy holds at most 20 MB and 2,000 files. It leaves out `.git` and `node_modules`, linked folders, pipes, sockets, and files with unusual names. A link that points outside the workspace stops the whole run. The script runs in the copy, so a relative path cannot change the real workspace. The scratch folder is deleted afterward, and only if it is the folder Schola made.
- It writes the script into the copy as `schola_check_script.py` or `schola_check_script.js` and runs the runtime on it, with the arguments, from the copy. There is no shell and no standard input.
- It gives the script a clean environment: `PATH` (a fixed list of standard folders), `LANG`, and `HOME` (the scratch folder). No other variable of the server's reaches the script.
- It starts the script in its own process group. After the time limit, or when the combined output passes 64 KB, it kills the whole group. It kills the group again when the script ends, so a child left behind dies too. A child that starts a new session is outside the group and escapes.
- It sets resource limits through a small launcher that replaces itself with the runtime: CPU time (the time limit plus one second), the largest file (16 MB), open files (256), core dumps (none), and the number of processes. The process limit counts all processes of the user, so Schola sets it to the user's current count plus 100. If a limit cannot be set, the result names it. It is never skipped quietly.
- It sets no memory limit, because no portable one exists.
- One script runs at a time. A second request gets `busy`.

The result holds the exit code, the first 500 characters of the output, the output size, the time taken, whether the script timed out, and the limits that were and were not applied. `outputTruncated` is true when the output passed 64 KB or was cut to 500 characters for the result. `outputIncomplete` is true when a child that started its own session still held the output after the run, so the output may be incomplete and the child may still run. The log says so. The script's output is evidence. The student and the student's agent can read it, and a script can print the content of a file it reads.

Other rules:

- Schola looks up `python3` and `node` only in `/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin`, and `/bin`. A runtime from a version manager, such as nvm or pyenv, is not found there. The check is then skipped, and the student reports the result by hand.
- A folder or file in the workspace that Schola cannot read stops the run, because a partial copy could give a wrong answer.
- If the runtime or the launcher cannot start, the result is an error (`script_not_started`), not a failed check. An exit code of 126 or 127 from the script itself stays a failed check, with a hint in the log.
- If Schola cannot kill a script that ran too long, the result is the error `kill_failed`, and the run lock is released.

What was checked, on macOS 15 (Darwin 24) with Python 3.14:

- The limits on open files, file size, and processes were enforced. A script stopped at the file limit, a write past the size limit failed, and a loop that tried to start 400 child processes was stopped before it reached 400. The CPU limit ended a busy loop.
- The memory limits `RLIMIT_AS` and `RLIMIT_DATA` could not be set. `setrlimit` raised an error. So no memory limit applies on macOS.

What was not checked: Linux (no Linux machine was available, so the limits are untested there), and Windows (the `resource` module does not exist there, so scripts would run with the timeout and the output cap only, and Windows is untested).

### What the token and the consent protect

They protect the student from hostile lesson content, and from a page on another site. They do not protect the student from a hostile program on the same computer. That program can read the token from the session route and call the consent route with any headers it likes. It already has the student's permissions. Nothing in this design can tell a click from a program.

The tutor surface, when it exists, must not offer the consent route to an agent. An agent can run a check, but an agent cannot agree for the student.

## Known limits

These are deliberate choices for this release, not oversights.

- The server supports macOS and Linux. Windows is untested. Schola does not reject Windows reserved names such as `con` or `nul` as lesson IDs.
- The lesson routes check the Host header, and the Origin header when a browser sends one. A request with no Origin header is accepted. The check routes add a per-launch token.
- Consent is not isolation. A script that a student agrees to run can still read the student's files and use the network. It can write to any path the student can write to, if it knows the path. The token and the consent protect against lesson content, not against another program on the same computer.
- Hidden tests cannot stay hidden. A script check stores its script as text in the lesson file on the student's disk, and the student sees it before they agree. A lesson that needs a secret answer key must check the work elsewhere.
- Script limits on macOS and Linux only. Windows is untested. On macOS no memory limit applies, because the system refuses one. On Linux the limits are untested. A runtime that lives outside the standard program folders, such as a node from nvm, is skipped.
- A script that starts a new session escapes the process group kill. The process limit helps, but it counts all processes of the user, so Schola sets it to the user's count plus 100.
- The server handles one request at a time. While a check runs, other requests wait. The busy answer protects a future server that handles requests at the same time.
- The pin file is read, changed, and written without a lock file. Do not run two writers at once. Stop the server before you run the command line.
- The module list reads each stored module on every request. It does not cache.

## Recovery

A failed save leaves the working copy visible.
If a stale tab conflicts with a newer record, export its work before choosing Use saved version.
Do not overwrite the newer record to clear the warning.

For a damaged local record, use the recovery screen's validated retained backup.
Recovery preserves the damaged bytes and assigns a new revision.
Reload other open Schola tabs afterward.
If that screen cannot offer a valid backup, stop the server and retain the damaged files before restoring a known complete data backup.
Do not edit revision numbers or replace unknown versions with empty records.

A publication retry uses the saved request identity and original payload.
Keep its recovery storage intact and use the visible retry or conflict review.
Downloading a cloud copy requires review before replacing the selected local record.
Export first when the current local work must survive replacement.
Other curricula retain their own results and records.

Native refresh keeps the previous valid copy after a temporary request or write failure.
Confirmed loss of account access hides and removes private downloads.
An offline device can retain previously verified copies until it can check its session again.
If cleanup fails, use Retry sign-out and cleanup before connecting another account.
Local imports remain separate from private downloads.
See [Native data and recovery](../ios/README.md#data-and-recovery).

To roll back clients, use the retained compatible checkout and a copy of the saved data.
Do not remove server access restrictions, drop tables, or erase publications as a client rollback.
Unknown data versions require a compatible client or a reviewed repair.
See [Backend recovery](../backend/README.md) and [Connector setup](../hosted/README.md).

## Automated acceptance commands

Run commands from the repository root with Node.js 24, Python 3.10 or later, and PostgreSQL tools installed.
The database runners create disposable clusters with local sockets and no TCP listener.
Set `PG_BIN` to the directory containing `initdb`, `pg_ctl`, and `psql` if automatic discovery fails.

```sh
npm ci
npx playwright install chromium
git config core.hooksPath .githooks
npm run verify
npm run test:backend
npm run test:browser
npm run test:appearance
node tests/run-setup-browser.mjs
```

`verify` checks repository permissions, types, lint, Python and Node tests, and the web build.
Browser checks use temporary records and block external requests.
The lesson checks run inside `verify` and `test:browser`. `tests/README.md` lists them.
The setup suite uses fixture responses and does not grant provider permission or deploy a backend.
The packaged connector test runs within `verify` and checks its public file boundary without deployment credentials.
Read [Test runners](../tests/README.md) for individual checks.

List installed native destinations before running both device classes:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun simctl list devices available
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project ios/Touchline.xcodeproj -scheme Touchline -destination 'generic/platform=iOS Simulator' -derivedDataPath /tmp/touchline-release-build CODE_SIGNING_ALLOWED=NO build
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project ios/Touchline.xcodeproj -scheme Touchline -destination 'platform=iOS Simulator,name=iPhone 16 Pro,OS=18.5' -derivedDataPath /tmp/touchline-release-tests CODE_SIGNING_ALLOWED=NO test
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project ios/Touchline.xcodeproj -scheme Touchline -destination 'platform=iOS Simulator,name=iPad Pro 11-inch (M4),OS=18.5' -derivedDataPath /tmp/touchline-release-tests CODE_SIGNING_ALLOWED=NO test
```

If either named simulator is absent, choose an installed iPhone and iPad from the list.
Run the suites serially. These commands do not sign or install a physical-device build.
Native tests use isolated storage and mocked services.
Simulator results do not prove live email delivery, provider configuration, camera access, or device file protection.

## Acceptance journey

Record the tested Git commit, command results, device destinations, and any unresolved finding outside this guide.
Complete the journey on disposable data before using an owner's deployment.

| Area | Required check |
|---|---|
| Learning | Import two curricula, keep one focus, edit both, save experiments, restart, and restore an exported backup. |
| Lessons | Import a lesson file and cancel the review. Import it again and confirm. Restart and check the module is still there. Import a damaged file and a changed copy of a stored version. Import a newer version and check that the pin stays until you switch. Export the lesson backup and restore it into an empty data directory. |
| Draft recovery | Keep an unfinished experiment through navigation and relaunch. Test failed saving and stale-tab recovery without losing the newer record. |
| Setup | Review the actual destination, cancel permission, recover partial preparation, and verify discovery and backend health before accepting readiness. |
| Publications | Publish two curricula independently. Resolve one conflict while preserving the other course and local backups. Publish focus separately. |
| Accounts | Reject anonymous, unconfirmed, non-owner, wrong-hub, and wrong-account access. Test sign-out, revocation, and a failed backend switch. |
| Native reading | Select downloads on iPhone and iPad, launch offline, open sources, then refresh after a new publication. Preserve valid copies after a temporary failure. |
| Appearance | Test both theme pairs in Dark, Light, and System. Change system appearance, dismiss the panel, and relaunch without losing drafts or navigation. |
| Accessibility | Check keyboard access, visible focus, labels, larger text, contrast, reduced motion, and screen-reader reading order. |
| Review | Complete independent code, security, and failure-handling reviews plus a bounded visual and motion review. |

## Owner checks still required

Automated fixtures do not complete these live checks:

- Deploy the connector to the owner's Vercel project with the owner's PostgreSQL database. Verify the fixed HTTPS address and public discovery boundary.
- Configure both owner-controlled Supabase OAuth registrations. Check real permission grant, cancellation, cleanup, and partial setup recovery.
- Check real email-code delivery and return behavior. Check Google and Apple only if enabled, including the same pinned owner identity.
- Confirm native provider returns on the installed app and web returns on the original local browser. A hosted return page does not finish local sign-in.
- Sign and install the native app with the owner's Apple team. Check camera permission, QR scanning, source opening, and account cleanup on physical iPhone and iPad.
- Check file protection across device restart and first unlock. Check VoiceOver manually on both device classes, including larger text and appearance controls.

Follow [Owner-hosted deployment](../hosted/README.md) and [Native installation](../ios/README.md#install-on-your-device).
Keep encryption keys, database credentials, and provider secrets on the owner's server.
The one-time setup code is entered only at the verified owner HTTPS setup page.
Schedule the documented operator cleanup command for expired setup grants. No automatic scheduler is included.
Complete live checks only with the required owner approval, and retain their results before release.
