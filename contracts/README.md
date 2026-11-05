# Schola contracts

These JSON Schemas use JSON Schema draft 2020-12. Their `urn:touchline:contract:*` identifiers identify schemas; they are not network addresses. Load all `*.schema.json` files before compiling a schema with Ajv2020. Validation must not coerce values, add defaults, or remove fields.

| Schema | Purpose |
| --- | --- |
| `curriculum.v1` | Existing curriculum, including supported legacy extensions |
| `progress.v1` | Existing progress, with a local revision |
| `legacy-backup.v1` | Existing curriculum and progress export, with or without its format marker |
| `local-record.v2` | One local record, its origin, status, selected module, and unfinished experiment |
| `library.v1` | One library revision and optional focus record |
| `library-response.v1` | Library preferences with record summaries |
| `publication-binding.v1` | A local record's publication destination and separate revision counters |
| `public-curriculum.v1`, `public-progress.v1` | Explicit public fields, with extensions rejected at every object level |
| `published-course.v1` | One published curriculum, progress, status, revision, and publication time |
| `published-library.v1` | A published library revision and optional focus curriculum |
| `hub-description.v1` | Public connection discovery and supported sign-in methods |
| `error.v1` | A structured error response |
| `lesson-module.v1` | One single-lesson module: outcome, content, tasks, hint ladders, checks, optional harness, license, optional price |
| `packet.v1` | An ordered set of lesson module references, each with an exact version |
| `help-record.v1` | One hint request, with the attempt it follows |
| `lesson-backup.v1` | A backup of stored lesson modules, packets, pins, and help records |
| `lesson-common.v1` | Shared definitions for the lesson schemas above |

## Local identity and revisions

A `recordId` is a lowercase UUID and identifies one saved local record. A curriculum ID identifies content; it does not identify a unique local record. Imports may share a curriculum ID while retaining separate record IDs and progress.

A local origin is `{ "kind": "local" }`. A cloud origin includes a hub UUID, canonical HTTPS backend origin, and account UUID. Cloud cache selection must match all three fields, not just the curriculum ID. Account changes must not expose another account's cache.

`state.revision` is the local compare-and-swap counter: a save supplies the revision it read, and a successful change increments it. It applies to the entire record, including draft and navigation updates. `cloudRevision` tracks the remote publication. `publishedLocalRevision` records which local revision was published. Publishing does not change a local origin into a cloud origin.

The library has one nullable `focusRecordId`. Focus is a preference, separate from a record's `active`, `paused`, `completed`, or `saved` status. Multiple records may be active. Changing focus must not pause or delete another record.

## Legacy compatibility

Curriculum version 1 may omit `version`; a supplied version must equal `1`. Progress must specify version `1`. Unknown versions are rejected before migration. Local curriculum and progress objects retain unknown extension fields, as the existing import boundary did. Original version 1 backups remain readable; importing them does not rewrite the original file. Their envelope accepts only `curriculum`, `state`, and the optional `format` marker. Imports reject unknown envelope metadata rather than silently discarding it. Nested legacy content remains preserved, including optional `totalHours` and module `number` metadata whose values the original validator did not constrain.

Public curriculum and progress use a closed field list. A publisher must build these projections explicitly, then validate them. It must not serialize a local record or copy arbitrary extension fields. Drafts, selected modules, local record IDs, origin scope, and publication bindings stay local. Public schemas describe data shape; publication still requires the owner's explicit action and the destination's access checks.

## Semantic validation

Schemas check types, required fields, versions, known field bounds, status values, and closed envelopes. The application must also check the following rules against the complete object and current store:

- Curriculum module IDs are unique. Each prerequisite and resource reference exists. Module weeks satisfy `weekStart <= weekEnd <= curriculum.weeks`.
- Progress `curriculumId` matches the curriculum. Each progress module and experiment references a module in that curriculum. Each task list has exactly the matching module's task count.
- Complete modules have all tasks checked and nonblank evidence. Submitted experiments have a nonblank title. Schemas also enforce these rules where no external context is needed.
- A selected module and draft experiment module reference existing modules. A draft may have an empty title and no experiment ID. A nonnull draft ID is nonempty; a nonempty draft date must be a real ISO calendar date. A submitted draft with an ID must resolve to the intended experiment; save and submit must not silently target a different record.
- Nonempty `startDate` values pass the existing calendar-date parser. `publishedAt` is a real UTC timestamp. A timestamp-shaped string can still contain an impossible date.
- Focus references resolve within the current library or publication scope. A binding resolves to the named record, account, backend, hub, and curriculum. A publication cannot report a local revision newer than the saved record.
- Revision values are nonnegative safe integers, at most `9007199254740991`, so JavaScript can compare them exactly. A save with an old revision returns an error without changing stored data.
- An HTTPS origin has no credentials, path, query, or fragment, uses its canonical host spelling, and omits the default port. Parse and normalize it with a URL implementation, then require an exact match. The schema rejects common malformed forms; runtime validation must also check IP literals and the port range.
- A publishable key starts with `sb_publishable_`, or has the three-part shape of a legacy JSON Web Token (JWT). For a legacy JWT, decode the header and payload safely and require the `anon` role; reject `service_role`, malformed payloads, and other roles. Passing this structural check does not prove that the credential works or belongs to the backend. The guided connection step performs that check.

A hub link or QR code contains the hub address only. Public discovery never contains a password, email address, session token, management token, secret key, or service-role key. Unrecognized fields are rejected, including nested fields.

## Shared fixtures

`tests/fixtures/contracts/cases.json` lists each case's schema file, data file, and expected schema result (`valid`). A case marked `semanticValid: false` deliberately passes JSON Schema but must fail the relevant runtime check. Consumers must not treat schema success alone as permission to accept it. Fixture domains and credentials are synthetic; none grants access to a service.

`node --test tests/contracts.test.mjs` compiles all schemas with Ajv2020, checks every case, round-trips valid JSON without loss, checks the shipped curriculum, rejects unknown versions, and checks publication and discovery field boundaries. Python storage checks use the same fixture files for local runtime rules.

## Lesson modules and packets

The three lesson contracts are new and additive. They change no existing schema, fixture, or storage key. Their identifiers use the form `urn:schola:contract:<name>:v1`. The older contracts keep their `urn:touchline:contract:*` identifiers.

### Two meanings of module

In the curriculum contract, a module is a multi-week unit. It has `weekStart` and `weekEnd`. In the lesson contracts, the unit is a lesson module. A lesson module is one lesson with one outcome and one piece of verified work. It has no weeks. The schema and file names always say `lesson-module`. Never read one as the other.

### Files

- `lesson-module.v1` holds `schemaVersion`, `id`, `version`, `title`, `outcome`, `blocks`, `tasks`, `checks`, `prerequisites`, `license`, and the optional `harness`, `estimatedMinutes`, and `price`.
- `packet.v1` holds `schemaVersion`, `id`, `version`, `title`, and `modules`. Each entry in `modules` is an `id` and an exact `version`. A packet holds references, not module content. A packet with one entry is how a single module travels as a packet. `packetFromModule` in `validate.mjs` builds that packet from a module.
- `help-record.v1` holds `schemaVersion`, `moduleId`, `taskId`, `level`, `attemptId`, `timestamp`, and `source`.
- `lesson-common.v1` holds the shared patterns for ids, versions, hint levels, and timestamps.

Every object is closed. The `runner` descriptor of a check is closed too, but its keys are not fixed, because its shape belongs to the check runner. It must be a flat object with a `type` string. Other keys must match `^[a-z][A-Za-z0-9]{0,39}$` and must not be `__proto__`, `constructor`, or `prototype`. It holds at most 20 properties. Each value is a string of at most 500 characters, a 32-bit integer, a boolean, or an array of at most 20 strings of at most 200 characters. Nested objects are not allowed.

### Choices and their reasons

- `schemaVersion` is a string in the form `MAJOR.MINOR`, such as `1.0`. Minor version 1 added the script check fields. The schemas accept major version 1 and any minor number. Objects are closed, so a document with new fields needs a schema that knows them. A reader that sees an unknown field in a `1.x` document reports it as invalid.
- The content `version` of a module and a packet is a three-part string such as `1.2.0`. A plain integer cannot tell a fix from a new feature. A pre-release suffix is not allowed. A packet entry must name one exact version, never a range.
- Ids use lowercase letters, digits, and hyphens, start with a letter or digit, and hold at most 60 characters. This pattern is stricter than the curriculum module id pattern, so ids are safe in file names and links.
- Block, task, and check ids use the same pattern. They are unique inside one module.
- A block has one of four types. `text` holds a body. `source-link` holds a title and an `http` or `https` link. `interactive-code` holds a language and starter text. `check-reference` names a check declared in the same module.
- A task holds a prompt and a hint ladder. The ladder has exactly four entries. The levels are `nudge`, `question`, `partial-example`, and `full-answer`, in that order. The schema enforces the order. The validator also checks it as a backstop.
- Text fields (`title`, `outcome`, task `prompt`, check `description`, and block `body`) need at least one visible character. White space, the zero-width characters U+200B to U+200D, U+2060, and U+FEFF do not count.
- A link in a `source-link` block or in `license` uses `http` or `https`. It has no user information before the host. It holds no control character (U+0000 to U+001F and U+007F) and no bidirectional override (U+202A to U+202E and U+2066 to U+2069). An `@` in the path or query is allowed.
- A check has a `kind`. The kind `automated` needs a `runner` descriptor. The kind `self-reported` must not have one. A `script` runner also needs `runtime` and `source` from schema version 1.1.
- A harness reference holds an id and `requiredPermissions`. The list is empty or holds names from a closed set. The set is the tutor call surface: `get_lesson`, `get_attempt`, `run_check`, `request_hint`, and `log_reflection`. A harness may add context. It never widens what an agent may do, so no other permission name is valid.
- `prerequisites` is a list of module ids. It is advice only. A reader warns about a missing prerequisite and never blocks on it.
- `license` is an object with an `id`, such as an SPDX identifier, and an optional link.
- `price` is optional. An absent price means no price is set. The `amount` is an integer in minor units, such as cents, from 0 to 99999999. The `currency` is a three-letter code in capitals. The schema checks only that shape. The hosted backend must check the code against an allowlist of the currencies it supports. These fields describe a price. They carry no payment logic.

### Checks hold descriptors, not code

In schema version 1.0, a check holds a `description` and, for an automated check, a `runner` descriptor. A runner descriptor is data only. The validator never executes it. Nothing may use it to grant permissions. A runner reads the descriptor and decides what to do. The `interactive-code` block holds starter text for the student to edit. It is data. No reader may run it or any runner field unless the reader first shows the student what will run and the student agrees.

Schema version 1.1 adds one case. A check whose runner type is `script` also holds the text of a script. See "Script checks" below. A script is still data to the validator, and a reader still runs nothing until the student sees the script and agrees.

### Built-in check runners

The local app runs a small set of check types itself. They run no creator code. A runner descriptor names one of them in `type` and adds the fields below. The rules for all of them:

- Every path is relative to the student's workspace folder. It uses `/` between parts. It may not be absolute, hold `..`, a backslash, a drive letter, a NUL byte, or any control character, or be longer than 1,024 bytes. A part may hold at most 255 bytes, and a path at most 32 parts.
- A symbolic link that leaves the workspace is never followed. A link that stays inside is allowed.
- A runner never opens a device, a named pipe, or a socket. It reads at most 1,000,000 bytes of a file. A larger file gives an `error` result.
- A field that the type does not list is an `error`, not a hint to ignore.
- An unknown `type` gives the status `skipped`, never `passed`. The student then reports the result by hand.

The types:

- `file-exists`: `path` (required), `kind` (`file`, `directory`, or `any`, default `file`), `nonEmpty` (boolean, files only).
- `text-match`: `path` (required), and exactly one of `text` or `pattern`. `text` is a literal substring. `pattern` is a regular expression of at most 200 characters, run in a separate process that is stopped after two seconds. `ignoreCase` (boolean) and `minCount` (integer from 1 to 1,000, default 1) are optional.
- `json-valid`: `path` (required), `topLevel` (`any`, `object`, or `array`, default `any`), and `requiredKeys` (a list of top-level key names).

- `git-commit-exists`: `minCommits` (integer from 1 to 1,000, default 1), `messageContains` (a literal text of 1 to 500 characters), and `ignoreCase` (boolean). Without `messageContains`, the check counts the commits reachable from `HEAD`. With it, the check counts the commits among the newest 500 whose message holds the text. The workspace must be a git repository that the current user owns. Its `.git` entry must be a real folder, or a plain file (a gitfile) that names a folder inside the workspace. The repository config may hold only plain-data settings. A partial clone, a setting that names a program, an `include`, and a promisor pack are refused.

A run returns a `status` of `passed`, `failed`, `error`, or `skipped`, a small flat `evidence` object, and a short `log`. The evidence of a built-in check holds counts and names, not the content of a student file. A script check is different: its evidence holds the first 500 characters of the script's output, and a script can print the content of a file it reads. Treat the evidence of a script check as readable by the student and by the student's agent.

### Script checks (schemaVersion 1.1)

A check of kind `automated` whose runner is `{ "type": "script" }` holds a script that the local app can run after the student agrees. The script fields sit on the check, next to the `runner`, because the flat `runner` cannot hold a script.

- `runtime` is required. It is `python3` or `node`.
- `source` is required. It is the script text. It is UTF-8, at most 65,536 bytes, and holds no NUL byte and no lone surrogate. The schema limits the length to 65,536 characters. The validators also count bytes, so 22,000 characters of three bytes each are too large.
- `args` is optional. It is a list of at most 8 texts of at most 200 characters, with no NUL byte. The app passes each one as a separate argument. No shell reads them.
- `timeoutSeconds` is optional. It is a whole number from 1 to 120. The default is 30.

The `runner` of a script check holds only `type`. The script fields are allowed only on a check whose runner type is `script`. A check of kind `self-reported` or a check with another runner type must not hold them.

The version rule: these fields exist from schema version 1.1. A document with `schemaVersion` `1.0` that holds any of them is invalid, and the error points at `/schemaVersion`. A document with `schemaVersion` `1.1` or later that has a `script` runner must hold `runtime` and `source`. A `1.0` document with `{ "type": "script" }` and no fields stays valid, as it was. A reader that cannot run it skips the check, and the student reports the result by hand.

The change is additive. The schema file keeps the identifier `urn:schola:contract:lesson-module:v1`. Every earlier fixture and test passes unchanged. The Python and Node validators apply the same rules and give the same answers on every fixture.

`timeoutSeconds` may be written as a whole-number float such as `30.0`. JSON Schema counts it as an integer, both validators accept it, and the runtime treats it as `30`.

A reader must treat the script as untrusted. It shows the runtime, the arguments, the time limit, and the script to the student. It records the student's agreement for the SHA-256 hash of everything that would run. See [Release and recovery](../docs/RELEASE.md#local-checks) for how Schola runs one and what it does not protect.

### Help records and the full answer

Every hint request makes one help record. The `attemptId` field is always present. It may be `null` only for the levels `nudge` and `question`. The levels `partial-example` and `full-answer` need the id of a logged attempt. This is how the contract keeps the full answer behind a logged attempt. The `source` is `student` or `agent`. The `timestamp` follows RFC 3339.

### Validator

`contracts/validate.mjs` exports these functions:

- `validateLessonContract(kind, document)` checks a parsed document. The kind is `lesson-module`, `packet`, or `help-record`.
- `validateLessonText(kind, text)` parses JSON text first. Text that is not complete JSON gives an invalid result.
- `validateLessonFile(kind, file)` reads a file first. A file that cannot be read gives an invalid result.
- `packetFromModule(module)` builds the one-entry packet for a module.

A call returns `{ ok: true }` or `{ ok: false, reason, errors }`. Each error is `{ pointer, message }`, where `pointer` is a JSON Pointer into the document. The `reason` is one of these:

- `invalid`: the document broke the schema or a semantic rule, or it is not JSON.
- `unsupported_version`: the major version is not supported.
- `too_large`: the document is larger than `MAX_BYTES`, which is 5 MB. The check runs before parsing, and a file is measured with a size check before it is read. The hosted backend must enforce the same cap at upload.
- `unreadable`: a file could not be opened or measured. The message names the error code, such as `ENOENT`, `EISDIR`, or `EACCES`. It never holds a path.

A result lists at most 100 errors. If there are more, the last entry has an empty pointer and the message `N more errors omitted.`

The validator never changes its input. It never writes a file. It opens files for reading only. A damaged file or a file with an unsupported version stays exactly as it was.

### Unknown major versions

A reader supports a list of major versions. Today the list is `1`. A reader reads `schemaVersion` first. If the major number is not in its list, it stops. It returns `unsupported_version`, it reports only `/schemaVersion`, and it does not look at any other field. A reader never tries to repair, migrate, or rewrite such a file. The reader takes the digits before the first dot as the major number, so `2.0.0` and `1000000.0` are `unsupported_version`. A marker with major number 1 that is not in the `MAJOR.MINOR` form, such as `1.0.0`, is `invalid`. A marker with no dot is `invalid`.

### Semantic checks

A schema alone cannot check these rules. `validate.mjs` checks them after the schema passes:

- Module ids are unique in a packet.
- Block ids, task ids, and check ids are unique in a module.
- Every `check-reference` block names a check that the module declares.
- A prerequisite is not the id of the module itself. The schema already checks that each prerequisite is a valid module id. A prerequisite does not need to be in the same packet.
- Every hint ladder holds the four levels in the required order. The schema already enforces this, so the check is a backstop.
- A help record timestamp names a day that exists. The schema alone accepts a date such as 30 February.

### Safe use of ids and links

An id is a label. A tool must never use an id directly as a file name or path. Prefix it or hash it first. The hosted backend must also apply a server-side request filter, which blocks private and local addresses, before it fetches any `source-link` URL.

A help record does not name a module in the same document. A tool that holds both must also check that `moduleId` and `taskId` exist in the module. That check belongs to the tool.

### Fixtures and conformance

The fixtures are in `tests/fixtures/contracts/valid/` and `tests/fixtures/contracts/invalid/`. Their names start with `lesson-module-`, `packet-`, or `help-record-`. The file `tests/fixtures/contracts/lesson-conformance.json` lists every one of them. Each entry has these fields:

- `name` is a unique label.
- `kind` is `lesson-module`, `packet`, or `help-record`.
- `file` is a path relative to `tests/fixtures/contracts/`.
- `expect` is `valid`, `invalid:invalid`, or `invalid:unsupported_version`.
- `stage` names where a document first fails. It is `none`, `parse`, `version`, `schema`, or `semantic`. A `parse` fixture is not valid JSON. A `version` fixture has a `schemaVersion` string with a major number other than 1. A `schema` fixture fails the schema. A `semantic` fixture passes the schema and fails a semantic rule.

The test refuses any file in `valid/` or `invalid/` that is neither a case in `cases.json` nor an entry in the manifest.

Another implementation, for example a Rust service, proves conformance in these steps:

1. Load every `contracts/*.schema.json` file as a draft 2020-12 schema. Register each by its `$id`.
2. For each manifest entry, read the file as text. Do not change it.
3. Refuse text over 5 MB before parsing. Then parse the text as JSON. A parse failure is the result `invalid`.
4. Read `schemaVersion`. If the digits before its first dot are not a supported major number, the result is `unsupported_version`.
5. Validate the document against the schema for its `kind`. A failure is `invalid`.
6. Run the semantic checks above. A failure is `invalid`.
7. Compare the result with `expect`. Every entry must match.
8. Check that every file has the same bytes after the run.

An implementation that applies only steps 1 to 5 passes every entry whose `stage` is not `semantic`. Entries with a `schema` stage include a hint ladder out of order. It must apply step 6 to pass the rest. The Node test is `tests/lesson-contracts.test.mjs`. Run it with `node --test tests/lesson-contracts.test.mjs`.

## Lesson storage and backup

The server checks every lesson file itself. It does not trust the browser.
`lesson_contract.py` is a Python reader for these same schema files. It uses only the Python standard library.
It must give the same verdict, reason, and error pointers as `contracts/validate.mjs`.
`tests/lesson-python-parity.test.mjs` runs both on every case in `lesson-conformance.json` and compares them.
Change a schema, a semantic rule, or a fixture in both places, or that test fails.

### `lesson-backup.v1`

A lesson backup is a new file format. It does not change `local-record.v2` or any older schema.
Its `format` is `schola-lesson-backup`. Its fields are `schemaVersion`, `createdAt`, `modules`, `packets`, `pins`, and `help`.

- Each entry in `modules` and `packets` holds the `id`, the `version`, the `sha256` digest of its text, the exact `text` that was imported, and a small `source` record.
- `pins` holds a `revision` and a map from module ID to the pinned version.
- `help` holds one group of help records for each module.
- The backup holds no progress. A curriculum record backup already covers progress.

Restore checks a backup in this order, and writes nothing until every step passes:

1. Read `schemaVersion`. An unknown major version is `unsupported_version`.
2. Validate the schema.
3. Check every digest against its text.
4. Validate every module and packet text with the lesson rules above.
5. Check the references. A packet needs its modules, a pin needs its module, and a help record needs its module and task.

The fixtures are in `tests/fixtures/contracts/lesson-backup/`. The manifest is `lesson-backup-conformance.json`. Each entry has a `stage` of `none`, `version`, `schema`, or `semantic`.
`tests/lesson-backup.test.mjs` checks the schema stage. `tests/test_lesson_backup.py` checks the others, and the restore itself.

### Where lessons live on disk

Imported lessons live under `_lessons/` in the data directory. They are stored as the exact bytes that were imported.
A version is written once. The same ID and version with different bytes is a conflict, and Schola refuses it.
The layout, the pin rules, the migration, and the rollback steps are in [Release and recovery](../docs/RELEASE.md#lesson-modules-and-the-_lessons-folder).
The routes and their size limit are in the same guide.
