# Browser checks

Install the project dependencies and Playwright Chromium from the Schola directory:

```sh
npm ci
npx playwright install chromium
```

Run the isolated browser suite:

```sh
npm run test:browser
```

The runner starts its own Python server on an operating-system-assigned port.
It creates a temporary data directory and removes only that directory after the run.
It does not connect to the running hub on port 8765.

Learning requests use real local persistence.
The suite saves a note, restarts the server, imports another curriculum, and checks that their records stay separate.
It also rejects a stale write and confirms that the newer record remains unchanged.
The suite restores unfinished experiments, edit targets, and selected lessons after a server restart.
Failed saves block course changes, and conflicting recovery copies remain available for export.
Separate accounts can download the same curriculum without replacing each other's records.
The tests cover sign-out, canceled imports, version 1 restores, and version 2 restores.

The browser receives an empty `/api/cloud-config` response.
The runner blocks requests to every external origin and tests that restriction.
The suite does not read deployment credentials or send requests to a private backend.
Set `PLAYWRIGHT_EXECUTABLE_PATH` only when using an existing Chromium executable.

Run the isolated-server lifecycle check separately:

```sh
node --test tests/browser/server.test.mjs
```

## Appearance checks

Run the appearance suite on its own isolated server:

```sh
npm run test:appearance
```

This runner also creates temporary storage, suppresses cloud configuration, and blocks external requests and sockets.
It replaces all learning writes with test responses.
Screenshots stay in the temporary directory and leave with that directory after the run.

The suite covers both theme pairs, Dark, Light, and System modes, live system changes, reload, and browser-tab updates.
It also covers draft preservation, dismissal, focus return, narrow layouts, and unavailable storage.
It does not prove durable saving because it replaces write requests.
Use the isolated browser suite for that proof.

For a separately managed disposable server, `tests/appearance.mjs` also accepts `TOUCHLINE_URL` and `TOUCHLINE_ARTIFACT_DIR`.
Its default screenshot directory is `artifacts/`.
`PLAYWRIGHT_MODULE` selects Playwright's module, and `PLAYWRIGHT_EXECUTABLE_PATH` selects Chromium.

## Native checks

Build the native app for an iOS simulator before testing appearance.
Check pair selection, mode selection, device appearance changes, dismissal, and preference persistence after relaunch.
Appearance preferences remain local to each device.

## Data and account checks

`npm test` runs the Python storage tests and Node tests.
Both runtimes read the shared examples in `tests/fixtures/contracts/`.
The examples cover valid records, old backups, unknown versions, invalid references, and private fields in public data.

Account transfer tests replace the Supabase client with an in-memory stub.
They cover canceled downloads, interrupted publication, and account changes while a request waits.
They make no network requests and use no deployment credentials.

## Lesson module checks

The lesson checks run inside `npm test`, `npm run test:browser`, and `npm run verify`.
Run one group on its own while you work.

Python checks for the validator, the id map, the store, the migration, the backup, and the data that existed before lessons:

```sh
python3 -m unittest discover -s tests -p "test_lesson_*.py" -v
python3 -m unittest discover -s tests -p "test_storage_golden.py" -v
```

The lesson routes, including the 5 MB limit and the Host and Origin checks:

```sh
python3 -m unittest -v test_server.LessonRouteTests
```

Node checks for the lesson contracts, the match between the Python and Node validators, the backup schema, and the web service and review screens:

```sh
node --test tests/lesson*.test.mjs
```

The browser check is `tests/browser/lessons.mjs`. `npm run test:browser` runs it with a separate temporary data directory.
It blocks every external request. It imports a lesson packet and shows the review.
It checks that Cancel leaves nothing on disk and that Add stores the files.
It restarts the server and checks the modules are still there.
It checks that a damaged file and a changed copy of a stored version show a message and change nothing.
It checks that a newer version is only offered, and that the pin moves only after the second, explicit step.
It checks that a file that is not valid UTF-8 is refused, that a mix of lesson and other files is refused, and that a file near 4.5 MB whose request would pass 5,000,000 bytes is refused on the page with no call.
It checks that a curriculum import still uses the older review.

`tests/lessons-dialog.test.mjs` runs the import dialog against a fake document and a fake server.
It covers Cancel and Escape, a failed list after an import, a failed pin switch, and a reload that fails after a failed switch.

Check runner checks cover the built-in checks and the rules that keep them inside the student's workspace:

```sh
python3 -m unittest -v tests.test_check_runner
```

The tests build their own folders under a temporary directory. They cover `..` paths, absolute paths, NUL bytes, long paths, links that leave the folder, a link swapped in after the path check, named pipes, sockets, device files, files over the size cap, and a pattern that would run forever.

The git commit check, against repositories built at test time that try to run programs:

```sh
python3 -m unittest -v tests.test_check_git
```

Each hostile repository names a marker script in its config, in its hooks, and in an included file. A control step first runs a git command that is known to run the script, so an empty marker folder means something. The tests skip when git is not installed.

Script checks (tier 2), the schema version 1.1 fixtures, and the limits:

```sh
python3 -m unittest -v tests.test_check_script
node --test tests/lesson-contracts.test.mjs tests/lesson-python-parity.test.mjs
```

The script tests run real `python3` and `node` scripts. They cover a missing or wrong consent (no process starts, shown with a patched `subprocess`), a changed script, the scratch copy and its caps, a link that leaves the workspace, a clean environment that holds no secret, a timeout that kills a child, an output flood, a child left behind, the file, open-file, and process limits, a limit that cannot be set, and a second run that gets `busy`. Two tests record what a consented script can still do, so the documentation stays true. One test sends a lesson with a script check through the import route, the consent route, the run route, and the lesson backup. The process limit test forks at most 400 short-lived children. The Node tests check that the Python and Node validators agree on the script fixtures.

The check routes, the token, the workspace setting, and consent:

```sh
python3 -m unittest -v tests.test_check_routes
node --test tests/check-consent.test.mjs
```

The route tests start a server that handles requests at the same time, so they can check the busy answer. They cover a missing and a wrong token, a foreign Host and Origin, unsafe workspace folders, consent bound to a hash and a folder, and a run request that tries to grant consent. The Node test runs the check service and the consent screen against a fake server and a fake document. It checks that Cancel makes no call and that server text is shown as text only.

The golden data in `tests/fixtures/storage/pre-s03/` is the data folder as it was before lessons.
A test loads it and checks that no byte changes.
