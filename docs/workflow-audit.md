# Workflow audit

This audit lists the steps in Schola work that repeat and can be scripts.
Schola has no package release, so there is no release script.

If all three of these are true, the step is a script:

- It runs in the same order every time.
- It needs no judgment. The result is pass or fail.
- Its inputs are known before it starts.

If the next step depends on what the last step found, or if a person must choose, it stays an agent task.
An agent can run a script, read its output, and try another approach. The script never decides for the agent.

## How the numbers were made

The "tokens saved" column is an estimate, not a measurement. It counts the tool calls and the output an agent reads in a step done by hand. Treat each figure as a size class: small is under 1,000 tokens, medium is 1,000 to 4,000, large is over 4,000.

How often a step runs comes from the history of this repository, which has 134 commits. Each pull request repeated the gates, the probes, and the body.

## Safety classes

- `read-only`: reads files, git, or the network. Changes nothing.
- `local-write`: changes files in the working tree, a separate working copy, or the temporary folder.
- `remote-write`: pushes, comments, labels, or opens a pull request.
- `human-only`: publish, deploy, spend, login, or secrets. An agent never runs it.

## Candidates

| Name | Steps | How often | Tokens saved per run (estimate) | Safety class | Inputs | Outputs | Decision |
|---|---|---|---|---|---|---|---|
| Python version matrix | Find an interpreter for each of Python 3.10 to 3.14. Run the unit suite and the server suite on each. Kill a suite that hangs. Read the counts and the first failure. | Every pull request, and again after each fix. About 3 to 5 times per pull request. | Large. Each run replaces a loop script, a wait, and a read of up to ten logs. | `local-write` | Repository folder, versions, timeout | One line per suite. Log files. Exit code 0 or 1. | **Script.** Built as `tools/gate_matrix.py`. |
| Smoke run of the command line and server | Make a new data folder. Run `version`, `modules`, `events list`, `events export`. Start the server on a free port. Read `/`, `/api/state`, `/api/library`. Check that `/api/hub` refuses a call with no token. Stop the server. Remove the folder. | After every change to a route, a command, or storage. About 2 to 4 times per pull request. | Medium. Each probe was a new one-off script before. | `local-write` | Repository folder, optional seed folder | One line per step. Exit code 0 or 1. | **Script.** Built as `tools/smoke.py`. |
| Pull request body | List the changed files. Fill in the gate result. Check 300 words, the required headings, no bare issue numbers, no tables, no dash characters, no session link, the attribution line last. | Every pull request. | Medium. The check catches rule breaks that cost a rewrite round. | `read-only` | Base branch, gate JSON, body file | A body skeleton. A list of problems. Exit code 0 or 1. | **Script.** Built as `tools/pr_body.py`. The sentences stay with the agent. |
| Full Node gate with a watchdog | Run `npm run verify` in the background. Kill it after a time limit. Write the exit code. Pull the pass and fail counts out of the log. | Every commit, because the pre-commit hook runs the gate. | Medium. Each pull request had its own copy of the wrapper. | `local-write` | Repository folder, time limit | Log file, summary lines, exit code | **Script, not built yet.** `gate_matrix.py` covers the Python half. Wrap `npm run verify` the same way next. |
| Flaky browser test loop | Run one browser test 20 times. Optionally add CPU load or a fixed delay on one route. Count the failures. | Rare. Once so far. | Medium per use. | `local-write` | Test file, run count, optional delay | Pass and fail count | **Keep as agent task.** The loop is easy. Finding the cause took a fixed 1.5 second delay on one route, and an agent chose that. Script the loop only after a second flaky test repeats it. |
| Mutation check of a new test | Change one line of the code under test. Run the test. Expect a failure. Restore the line. Repeat for each mutant. | Once per feature, for most features. | Large. Each feature wrote its own mutant script. | `local-write` | List of mutants | A list of caught and missed mutants | **Keep as agent task.** Choosing mutants that matter needs judgment. The restore-and-run harness can become a script later. |
| Record a review result | Append one JSON line with the pull request, the commit, the reviewer, and the verdict. Set the status and time of the work item. | Several times per pull request. | Small to medium. A text edit by regular expression broke once on a new Python version. | `local-write` | Work item id, commit, verdict | One appended line. An updated work item. | **Script, in a private repository.** The files hold private state, so the script does not belong in this public repository. |
| Remove a merged working copy | List working copies. Skip any copy with changes or an unmerged branch. Remove the rest. | After each merge. | Small. | `local-write` | Folder of copies | A list of copies removed and skipped | **Script, in a private repository.** It deletes, so it must default to a dry run. A person approves removal. |
| Leftover temporary folders | Count folders named `schola-script-*` in the temporary folder. Remove the stale ones. | When the check script test fails. When this audit ran, 147 folders existed. | Small. | `local-write` | None | A count. A list. | **Keep as agent task.** The leak is a test bug. Fix the test so it cleans up after itself. A cleanup script hides the bug. |
| Refresh a fixture or data file | None found. | Never. | None. | None | None | None | **No candidate.** `tests/fixtures/contracts` is edited by hand, and parity tests in two languages keep both sides in step. A generator needs a source of truth first. |
| Hub screenshots at three widths | Start the server with seeded data. Capture 375, 768, and 1280 pixels. | Per screen change. | Medium. | `local-write` | Screen route, seed folder | Three PNG files | **Keep as agent task.** `npm run test:hub` already captures them. Reading the pictures needs judgment. |
| Wait for CI | Poll the pull request checks until they finish. | Every pull request. | Small. | `read-only` | Pull request | Pass or fail | **Not a script here.** `gh pr checks --watch` already does it. |
| Three reviews of a diff | Run a code, security, and silent-failure review. Merge the findings into one list. | Every pull request. | Large. | `read-only` | Diff | One fix list | **Keep as agent task.** Each review is a judgment call by design. |

## What was built

Three scripts in `tools/`, each with tests in `tests/`:

- `tools/gate_matrix.py` and `tests/test_gate_matrix.py`
- `tools/smoke.py` and `tests/test_smoke.py`
- `tools/pr_body.py` and `tests/test_pr_body.py`

`tools/manifest.json` lists them with their arguments, output shape, and safety class.
If the manifest and the scripts disagree, `tests/test_tools_manifest.py` fails.

Each tool carries `mcp_exposure`: `expose` or `hidden`. A missing field means `hidden`. A later MCP server lists only the `expose` tools. The two `pr_body` tools are read-only and exposed. `gate_matrix` and `smoke` write files, so they are hidden. A human-only or destructive tool must be hidden, and `tests/test_tools_manifest.py` holds the shared check for this rule.

No script here pushes, comments, or opens a pull request, and none is `human-only`.

## What the scripts do not do

- `gate_matrix.py` runs the Python suites only. The Node and browser suites run through `npm run verify`.
- `smoke.py` checks status codes and JSON shape. It does not read the hub numbers.
- `pr_body.py` checks the rules that need no judgment. It cannot tell whether the sentences are true.
