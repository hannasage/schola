# Schola

Schola is a local learning hub with a 26-week curriculum about machine learning through soccer projects.
The plan assumes 10–12 study hours per week and prior software engineering experience.

## Start the application

Install Python 3.10 or later on your computer.
Open a terminal in this project folder.
Run this command:

```sh
python3 server.py
```

Open http://127.0.0.1:8765 in your browser.
Keep the terminal open while you use the application.
Press Control-C in that terminal to stop the server.

On macOS, you can also open `Start Touchline.command` to start the application.

If port 8765 is busy, use another local port:

```sh
python3 server.py --port 8766
```

Then open http://127.0.0.1:8766 in your browser.

## Command line tool

Schola also has a command line tool. It uses the same engine as the hub.
From this folder, run `./schola --help` to see the commands.

```sh
./schola hub                                   # start the hub
./schola modules                               # list your modules
./schola check show MODULE_ID CHECK_ID         # show what a check runs
./schola check run MODULE_ID CHECK_ID --workspace PATH
./schola events list                           # read your event log
./schola events export --output log.jsonl      # copy your event log
./schola events delete --confirm               # delete your event log for good
```

A script check asks for your consent before it runs. There is no flag that skips the question.
The tool has no agent commands yet. It does not store an API key.
Read [The command line tool](docs/cli.md) for every command, the exit codes, and the limits.

## Learning records

The application saves notes, tasks, evidence, study hours, and experiment records automatically.
A module requires completed tasks and written evidence before you can mark it complete.
Completion is a self-assessment. The application does not assess the quality of your evidence.
The experiment journal supports planned experiments and later edits to record results.

The application saves each record in `data/records/<record-id>/record.json`.
A record contains its curriculum, progress, unfinished experiment, and selected lesson.
The adjacent `record.backup.json` contains the previous saved record.
Each save replaces one complete file.
An old browser tab cannot overwrite a newer save.

On first use, the application copies older curriculum and progress files into this record format.
It keeps the original files unchanged and does not repeat a completed migration.
It rejects damaged files and unknown versions instead of replacing them with empty progress.
If a local record is damaged, the recovery screen can restore its validated retained backup.
It preserves the damaged file and reserves a new revision. Reload other open tabs after recovery.
Read [Release and recovery](docs/RELEASE.md) before restoring files by hand.

Unfinished experiment forms save without requiring a title.
If a save fails, the browser keeps a recovery copy on that device when browser storage is available.
A recovery copy based on an older save stays visible for export but cannot overwrite the newer record.
Use Export progress before choosing Use saved version to retain that conflicting work.
If browser storage is unavailable, keep the page open until saving succeeds or export your work.

Use Export progress to download a version 2 record, including its unfinished experiment and selected lesson.
Restore backup also accepts the original version 1 progress exports.
To transfer a course and its progress, use Import curriculum once with the full backup on the new installation.
Review the file, then choose Save for later or Start now as another active curriculum.
If that curriculum already exists, select it and use Restore backup instead.
The built-in soccer curriculum already exists on every installation, so use Restore backup for that course.
Export the current record before confirming a replacement.

## New curricula

Download the template from the application.
Give the template to your agent with your topic, background, time budget, and preferred projects.
Ask the agent to preserve the structure and assign a unique curriculum ID.
Import the completed JSON file with Import curriculum.
Select the new course from Your curricula.
Keep one curriculum as your focus, or work with several active curricula.
Opening another curriculum does not change your focus.

Each course has separate progress, experiments, and unfinished work.
Local imports reject duplicate curriculum IDs to protect existing progress.
Downloaded copies use the hub, backend, and account as part of their identity.
They can share a curriculum ID with a local course or another account's download.
Signing out hides downloaded copies and retains local courses.
These are account filters on a trusted local computer, not encryption or access control for its files.
To revise a course, import the revision under a new ID.
Moving progress between revisions requires manual reconciliation of module IDs and tasks.
The interface does not include a curriculum editor.

## Local operation

The server listens only on 127.0.0.1.
The prebuilt local tracker needs Python and no account.
Optional sharing across devices uses your own Supabase project.
The application does not require an AI API key.
The local tracker works without internet access after startup.
Cloud publishing and sign-in require internet access.
External learning resources require internet access.
Tutor prompts are copied into your existing agent. The application does not call an AI model.
The tracker does not download datasets or train models itself.

## Curriculum

Read `CURRICULUM.md` for the written learning plan.
The application reads the built-in lesson content from `curriculum.json`.
The curriculum includes foundational mathematics, several model families, research practice, and an engineering portfolio.
It does not replace every computer science subject or guarantee hiring readiness.

## Shared progress and native companion

Open Devices, enter your hub address, and review its identity before signing in.
Email codes are the default. Google and Apple appear only when configured by the owner.
Supabase setup permission is separate from your learning account.

Each owner runs a connector on their own Vercel project with their own PostgreSQL database.
Published learning snapshots stay in their own Supabase project. There is no shared Sage setup service.
Read [Owner-hosted connector](hosted/README.md) for guided setup and [Private publications](backend/README.md) for backend requirements.
The repository remains private during review, and its public deployment template is disabled.

Publish selected curricula to make their lessons and progress available on other devices.
Each publication has its own result. Publishing library focus does not publish curriculum content.
Local edits do not sync automatically.

Open `ios/Touchline.xcodeproj` in Xcode for the native iPhone and iPad companion.
Enter the same hub address or scan its QR code, review the destination, then sign in.
Select the curricula to download for offline reading. Native learning records remain read-only.
Read [Native companion](ios/README.md) for build, installation, and recovery steps.

## Rebuild the web interface

The web interface uses Projection UI 0.2.0-next.2.
Appearance offers Noir Bloom / Confetti Studio and Dusk Protocol / Coastal Day.
Each pair supports Dark, Light, and System modes.
Preferences stay in the current browser or native device, separate from learning records.
The local package archive makes this alpha dependency available to other developers.
Use Node.js 24 to rebuild the web assets.
From the project folder, run:

```sh
npm ci
npm run build
```

The generated `web.bundle.js` and `web.bundle.css` are served by Python.
The server never serves `.env.local` or backend source files.

## Development gates

Use Node.js 24 and Python 3.10 or later.
Install PostgreSQL tools (`initdb`, `pg_ctl`, and `psql`) for the disposable database tests.
If they are not found, set `PG_BIN` to their directory. Tests create their own local cluster.
Install the pinned development packages and test browser:

```sh
npm ci
npx playwright install chromium
git config core.hooksPath .githooks
```

Run the required checks before each commit:

```sh
npm run verify
npm run test:browser
npm run test:appearance
npm run test:backend
node tests/run-setup-browser.mjs
```

The first command checks types, lint, shared data formats, persistence, account transfer behavior, and the web build.
Read `contracts/README.md` for record formats, version rules, and shared test examples.
Browser tests start their own server with temporary learning records and block external requests.
They do not use the running hub or its saved progress.
Read `tests/README.md` for individual checks and browser configuration.

Read [Release and recovery](docs/RELEASE.md) for clean-install checks, backups, native test commands, and pending owner checks.
