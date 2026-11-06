# The command line tool

This page describes step E9. It covers the `schola` command line tool.
Schola ships as a desktop app and as this tool. Both use one engine.
The tool lets you open the hub, read a module, run a check, and read your event log from a terminal.

The tool adds no engine behavior. It calls the same Python modules as the hub.
The code is `schola_cli.py`. The launcher is the file `schola` at the top of the repository.
The tests are `tests/test_cli.py` and `tests/test_run_server.py`.
The code uses only the Python standard library. It needs Python 3.10 or later. It runs on macOS and Linux.

## Terms

- The hub is the local web server. It listens on `127.0.0.1` only.
- A module is a lesson that you imported. It has tasks and checks.
- A check tests your work. Some checks run no code from the lesson author. A script check runs a script that the lesson author wrote.
- A workspace is the folder that holds your work. A check reads it.
- The data folder is the folder where Schola keeps your modules and your event log.
- The event log is the file of events about your learning. Only you own it. The format is in `contracts/learning-events.md`.
- A reflection is a short text that you wrote about your learning. It lives apart from the log.
- Consent is your agreement to run one exact script.
- The check hash is a SHA-256 hash of everything that a script check runs. It covers the runner, the runtime, the arguments, the time limit, and the script. Change any of them and the hash changes.
- An exit code is the number that a command returns to the shell. 0 means success.
- JSON is a text format that programs read. JSON Lines is one JSON object on each line.
- A terminal is a window where you type commands.

## Start

From the repository folder, run the launcher:

```sh
./schola --help
```

You can also run it as a Python module:

```sh
python3 -m schola_cli --help
```

The old command `python3 server.py` still works. It starts the hub the same way as before.

## Options for every command

- `--data-dir DIR` sets the data folder.
  - The flag wins.
  - If you give no flag, the tool uses the environment variable `SCHOLA_DATA_DIR`. An empty value counts as not set.
  - If neither is set, the tool uses the `data` folder of the repository. This is the folder that `python3 server.py` uses.
  - Write the flag before or after the command name. Both work.
- `--json` makes a command that lists or shows print one JSON document. Its keys are stable and sorted. The output is ASCII only. It works on `modules`, `module show`, `check show`, `check run`, `events list`, and `version`.
- `--help` prints the help for the command.

The tool has no color and no emoji. It does not print the path of the data folder.
The flags `--yes`, `--force`, and similar do not exist. Write `--` before a value that starts with a dash, such as `schola module show -- --odd-id`.

## Commands

All the command words live in one table, `COMMAND_TABLE`, in `schola_cli.py`. The owner can rename them there.

### `schola hub [--port 8765] [--data-dir DIR] [--open]`

Starts the hub on `127.0.0.1` and prints its address. Stop it with Control-C. Control-C ends the hub with exit code 0.

- `--port` is a whole number from 0 to 65535. The default is 8765. Port 0 lets the system pick a free port. The tool prints the port it got.
- `--open` opens the address in your browser after the hub listens. If no browser opens, the tool says so and keeps running.

If the port is busy, the tool exits with 1 and the code `hub_unavailable`.
The hub in this tool is the same as the one that `python3 server.py` starts. The page, the routes, and the check token are unchanged.

### `schola modules`

Lists each module with its id, version, title, and the number of tasks and checks.
The version is the pinned version. A pin is the version the hub uses. If a newer version is stored, the tool says so. The tool never moves a pin.
If no module is imported, it says so and exits with 0.
The tool cannot import a module. Use the hub.

### `schola module show MODULE_ID [--version V]`

Shows the title, the outcome, the tasks, and the checks of one module.
For each check it shows the id, the kind, the runner type, and whether it needs consent.
If you give no `--version`, the tool uses the pinned version. If no pin exists, it uses the newest stored version.

### `schola check show MODULE_ID CHECK_ID [--version V] [--workspace PATH]`

Shows exactly what a check runs, before you run it. This command runs nothing.
For a check that runs no script, it shows the runner and its fields.
For a script check, it shows:

- the runtime, the arguments, and the time limit,
- the limits that Schola sets and the limits it cannot set,
- the whole script, with line numbers, and
- the check hash, in full.

It also says whether the check needs consent.
With `--workspace`, the tool checks the folder by the rules below and prints the path as you gave it.

### `schola check run MODULE_ID CHECK_ID [--version V] --workspace PATH [--consent HASH] [--json]`

Runs one check in a workspace. It prints the status, the evidence, and the log.
The check reads your workspace. It never changes it.

The statuses are `passed`, `failed`, `error`, and `skipped`.
A check that ran and reported `failed` is a result. The exit code is 0. The status is in the output.
Only a check that cannot run (`error`) gives exit code 1. A `skipped` check gives 0. It means Schola cannot run that check, so you report the result yourself.

Checks that run no script need no consent.

#### Consent for a script check

A script check runs code on your computer with your permissions. This is consent, not a sandbox. The script can read your files and use the network.
You agree in one of two ways.

1. On a terminal, the tool prints what will run and asks a question. It shows the workspace, the runtime, the arguments, the time limit, the script, and the full check hash. You agree by typing the first 8 characters of the hash and pressing Enter. Any other answer, or the end of input, means no. The tool reads one line of at most 200 characters.
2. Without a terminal, run `schola check show`, read the script, and pass the full hash with `--consent HASH`. The hash must match exactly. A short hash, a hash in capital letters, or a hash of another check is refused.

These rules have no exception:

- There is no `--yes` flag. There is no environment variable that skips consent.
- A wrong `--consent` value is refused. The tool does not fall back to the terminal question.
- Consent lasts for this one command. The tool keeps it in memory and writes it nowhere. The next command asks again.
- A consent covers one module, one version, one check, one hash, and one workspace. The tool builds it with the same `ConsentRegistry` that the hub uses.
- If the script is too long for the screen, the tool shows the first 60 rows. It says how many bytes it did not show. You cannot agree by typing then. You must read the whole script with `schola check show` and pass `--consent`.
- A script line that is longer than 100 characters wraps onto more rows. Nothing is cut.
- If you pass `--consent` for a check that runs no script, the tool ignores it and says so on the error output.

#### The workspace rules

The tool uses the same function as the hub, `check_routes.validate_workspace`. It refuses the folder with exit code 3 and the code `invalid_workspace` if:

- the path is not an existing folder that Schola can read,
- the folder is the top of the disk, your home folder, or a folder that holds your home folder,
- the folder is outside your home folder, or inside a hidden folder (a name that starts with a dot),
- the folder holds the data folder or is inside it, or
- the path has a control character.

Schola follows a symbolic link and checks the real folder. A link that leads out of your home folder is refused.
A relative path starts at the current folder. The tool turns it into a full path first.
The home folder comes from the `HOME` variable.

#### Does `check run` write events?

No. Today nothing writes a `check_result` or an `attempt_submitted` event. The page does not write them. The hub routes do not write them. The check route returns the result and writes no event.
The contract says only the app writes those events, with the source `app`. The part of the app that writes them does not exist yet.
So `check run` leaves the log alone. It does not invent events. The owner has to decide who writes a `check_result` event, and when. After that decision, the tool writes the same event as the page.
A test checks that `check run` writes nothing to the data folder.

### `schola events list [--limit N] [--offset N] [--json]`

Shows the events of the log, one line each: the line number, the event id, the time, the type, the module, the task, the source, and the main fields of the event.
`--limit` is a whole number from 1 to 5000. The default is 100. `--offset` is the number of lines to skip. The most is 999999999.
If more lines follow, the tool prints the offset to use next.

If a line of the log is damaged, the tool prints `line N is damaged (reason)` and still prints the good events. It does not print the damaged text. It exits with 1 and the code `log_damaged`. Any damaged line in the whole log causes this, even when the lines you asked for are fine. A cut last line counts as damage (`truncated_tail`).
The tool never repairs a log.
If there is no log, the tool says so and exits with 0.
The JSON document has the keys `ok`, `exists`, `total`, `offset`, `limit`, `more`, `truncatedTail`, `events`, and `damaged`.

### `schola events export [--output FILE]`

Writes the checked log as JSON Lines, byte for byte.

- Without `--output` the tool writes to standard output. If standard output is a terminal, the tool escapes control characters, so a damaged line cannot act on your terminal, and it says so. Redirect the output to a file for the exact bytes.
- With `--output FILE` the tool creates a new file with mode 0600. It refuses to overwrite a file or follow a link. It exits with 3 and the code `output_exists`. If the folder does not exist, it exits with 1 and the code `output_unwritable`.
- If there is no log, the tool exits with 1 and the code `log_missing`. It creates no file.
- If the log has damage, the tool writes everything and then exits with 1 and the code `log_damaged`.

### `schola events delete --confirm`

Deletes the event log and the reflections for good. You own the log, so you can delete it. This cannot be undone. The disk blocks are not overwritten.
Without `--confirm` the tool refuses with exit code 3 and the code `confirmation_required`. It does not open the log.
It prints what it removed. If there is nothing to delete, it says so and exits with 0.

### `schola version`

Prints `schola` and the version. With `--json` it prints `{"name": "schola", "version": "..."}`.

## Exit codes

- 0: the command worked. This includes a check that ran and reported `failed`, and a list command on an empty data folder.
- 1: the operation failed. A check cannot run (`error`). The log is damaged or missing. A module or check was not found. The data folder cannot be read. The hub cannot start. An unexpected problem stopped the tool.
- 2: usage error. The words or flags are wrong. The tool prints the usage to the error output.
- 3: refused. The consent is missing or wrong. The workspace is not allowed. The output file exists. A delete has no `--confirm`.

## Limits

The limits of this tool:

- `--port`: 0 to 65535. The default is 8765.
- `--limit`: 1 to 5000. The default is 100.
- `--offset`: 0 to 999999999.
- A line of ordinary text: 200 characters, then cut. The text says how many characters are missing.
- A script row: 100 characters, then wrapped. Nothing is cut.
- The consent question: 60 script rows. A longer script cannot be agreed to by typing.
- The typed consent answer: the first 8 characters of the check hash. The tool reads at most 200 characters of the answer.
- A check result: the tool prints at most 40 evidence entries and 40 log lines.
- A line of the log that goes to a terminal with `events export`: 20000 characters, then cut.
- An exported file: mode 0600, never overwritten.

The limits of the engine apply as they do in the hub. In short: a script holds at most 64 KB and takes at most 8 arguments. The default time limit is 30 seconds. A log holds at most 5 MB, 50000 lines, and 4096 bytes in a line. `docs/event-log.md` and `check_script.py` list the rest.

## Error messages

Every error is one fixed sentence and a stable code, on the error output:

```text
schola: <sentence> (code: <code>)
```

The tool never prints the text of an exception, a stack trace, or the input that caused the error.
The sentences come from the engine error classes (`payload()`) or from this tool. The codes are the engine codes, plus these: `consent_required`, `hash_mismatch`, `consent_declined`, `output_exists`, `output_unwritable`, `log_damaged`, `hub_unavailable`, `storage_unavailable`, and `internal_error`.

## Text from modules, events, and files

Titles, task text, check ids, evidence, scripts, and event fields come from lesson authors, from you, or from the app. They are untrusted.
The shared helper `safe_text` makes them safe before the tool prints them:

- Control characters show as `<U+XXXX>`. This covers escape sequences (`ESC [ 2 J`), OSC hyperlinks, carriage returns, NUL, DEL, and the eight-bit controls.
- Bidirectional controls, zero-width characters, tag characters, variation selectors, private use characters, and lone surrogates show the same way.
- A line of ordinary text is cut after 200 characters. The cut says how many characters are missing.
- A newline or a tab inside a value shows as `<U+000A>` or `<U+0009>`. A value cannot add a line to the output.

A script is printed with a line number and a `|` mark at the start of each row. A script line cannot pass for a line of tool output.
In `--json` output the tool uses `json.dumps` with ASCII escapes, so no raw control character reaches the terminal.

The helper cannot find look-alike letters (homoglyphs). A title can still use a Cyrillic letter that looks Latin. The tool does not try to detect that.
The tool does not check the text of your own workspace path beyond the workspace rules.

## Sharing the data folder with a running hub

The tool and a running hub can use the same data folder at the same time.
The event log takes a lock for each write and each read. A reader never sees a half-written line.
The tests read while another writer appends. They also run a real hub thread on the same folder. The hub adds an event. The tool reads it and deletes the log. The hub then reads the empty log.
The hub and this tool are two processes. Each process allows one script at a time. Nothing shares that rule between them. So one script from the hub and one from this tool can run together.

## What this step does not do

- It has no agent commands. It does not import `agent_loop`, `model_adapter`, `api_adapter`, or `local_adapter`. The agent commands come later.
- It does not read, store, or ask for an API key. Key storage for each platform and the token cap are open owner decisions.
- It has no packaging for PyPI, no installer, and no shell completion.
- It adds no routes and changes no part of the web page.
- It changes no engine file. The one change in `server.py` moves the start-up code into the function `run_server`.
- It writes no event (see "Does `check run` write events?").
- It cannot import modules, move a pin, or write a reflection.
- It does not support Windows. The event log needs `fcntl`.

## What is untested

- The tests ran on macOS with Python 3.10, 3.12, 3.13, 3.14, and 3.15. Linux continuous integration is the judge. A pass on macOS alone is not proof.
- The tests use fake terminals and one real pseudo terminal. They do not test a real terminal program, so they do not test how it draws escaped text.
- `--open` is tested with a replacement for the browser call. No test opens a real browser.
- The escape list comes from Unicode categories. A new Unicode version can add a hidden character that the list does not know. The list covers `Cc`, `Cf`, `Cs`, `Co`, `Zl`, `Zp`, the variation selectors, and a few filler characters.
- The delete failure test skips for an account that can write to a read-only folder, such as the root account.
- Two scripts that run at once from two processes are not tested.
