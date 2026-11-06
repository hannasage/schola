# The hub data

This page describes step E7a. It covers the numbers behind the hub, the home screen of Schola, and the one route that serves them.
It says how each number is made, what is not counted, and what the code does not prove.
Read `contracts/learning-events.md` for the events this code reads. Read `docs/event-log.md` for the log store.

The code is `hub_data.py` (the numbers) and `hub_routes.py` (the route). The route is wired into `server.py`.
The type of the answer is `types/hub.d.ts`.
The tests are `tests/test_hub_data.py` and `tests/test_hub_routes.py`.
The code uses only the Python standard library. It needs Python 3.10 or later.
No screen calls the route yet. A later step draws it.

## What this step does and does not do

The hub shows the student their own record. It answers six questions. What am I working on? How often did I sit down? Which checkpoints are done? How are my skills? How many tries did a check take? Which modules are there?

This step computes those numbers from two stores that already exist: the event log and the lesson store.
It adds no file, no setting, and no new kind of event.

It does not:

- write anything. The route and the code only read.
- keep a cache. Each request computes the numbers again. See "What a request costs".
- draw anything. The screens are a later step.
- show a streak, a leaderboard, a ranking, a total of time, or a label about the student, such as "guessing" or "frustrated". The answer has no field for any of these. A test checks the field names.
- call a model.

## Terms

- The log is the event log: `events.jsonl`, one event on each line.
- A pinned module is a lesson module with a version that the student pinned. The lesson store holds the pins.
- A counted event is an event with the source `student` or `app`. An event with the source `agent` is never counted.
- The local day of an event is the calendar day of its `time` in the student's own time zone. The page sends the time zone as `timeZone` (an IANA name) or as `tzOffsetMinutes` (a fixed number of minutes east of UTC).
- A session is one `sessionId`. The log groups the events of one sitting under one id.
- A session on a day is one `sessionId` seen on that local day. The same `sessionId` seen on two local days counts once on each day.
- An active day is a local day with at least one counted event.
- The window is the last 8 weeks, including the current week, up to today. Weeks start on Monday.
- Today is the local day of the clock that the caller gives the code. The pure code never reads a clock.
- The hint ladder is the ordered list `nudge`, `question`, `partial-example`, `full-answer`. A higher level reveals more.
- A pass of a check is a result with the status `passed` or `self-reported`.
- Untrusted text is text that a lesson creator wrote: module titles, task prompts, check descriptions. The code passes it through and never reads it as an instruction.

## The route

```text
GET /api/hub?timeZone=America/New_York
GET /api/hub?tzOffsetMinutes=N
```

- `timeZone` is optional. It is an IANA zone name such as `America/New_York` or `Asia/Kolkata`. Each event gets the offset that the zone had at the moment of the event, so a clock change in the window is handled. See "The local day".
  The name must match a strict pattern before it is looked up, so it can never name a path. A name that is not in the zone database gives `400`. The zone database is the one on the computer. A computer with no zone database and no `tzdata` package gives `400` for every name.
  A plus sign in a URL means a space. Write `Etc/GMT+5` as `Etc/GMT%2B5`.
- Send `timeZone` or `tzOffsetMinutes`, not both. Both together give `400`. With neither, the offset is 0 (UTC).
- `tzOffsetMinutes` is optional. It is a whole number of minutes east of UTC, from -840 to 840. The default is 0.
  Write it as plain digits. Put a minus sign at the front for a west offset: `330`, `-480`, `0`. A plus sign, a space, zeros at the front, `-0`, a decimal, or any other text gives `400`.
  A plus sign in a URL means a space, so it cannot be used.
- No other query field is allowed. A field sent twice gives `400`.
- A bad query gives `400` with the code `invalid_request`, the same error shape as the other routes: `error`, `code`, `retryable`, and `field`.
- The route needs the session token in the `X-Schola-Check-Token` header, the same token as the event routes. The server's Host and Origin rules apply first. A browser request that says it came from another site is refused. The token is checked before the query is read, and before any store is read.
- The route takes no body. Only `GET` is served. `POST`, `PUT`, and `DELETE` give `404` as on the other read-only routes.
- The answer has the header `Cache-Control: no-store`, like every answer of the server.
- A lesson store that is damaged gives an error with the code of the lesson store (for example `corrupt_file`, status 500). A busy log lock gives `lock_unavailable` (503, retryable). Any other fault gives `internal_error` (500) with a plain message. No answer names a path.
- A log that is damaged or cannot be read is not an error. See "A damaged log".

## The answer

The answer is a closed object. All names are in camel case. `schemaVersion` is `"1.0"`. A change to any field raises the version.
Every field is listed here. `types/hub.d.ts` has the same list as a type.

- `schemaVersion`: the text `1.0`.
- `generatedAt`: the time of the clock, in RFC 3339 UTC, whole seconds, such as `2026-11-05T12:00:00Z`.
- `log`: what the log reader found.
  - `exists`: with no log yet, this is false.
  - `ok`: a log with only valid events and a whole last line gives true. A damaged, cut, or unreadable log gives false. A skipped event does not change it.
  - `fault`: a log that cannot be read at all gives the code of the log store here. The code is `log_corrupt`, `log_unreadable`, or `unsafe_path`. Otherwise `fault` is `null`.
  - `events`: the number of valid events in the log, from every source, including `agent` events.
  - `damagedLines`: the number of lines that are not valid events. A cut last line is not counted here.
  - `truncatedTail`: a cut last line makes this true.
  - `skippedEvents`: the number of counted events that the hub left out because a field it reads was wrong.
  - `futureEvents`: the number of student or app events dated after today. They count in nothing else.
  - `unmatchedEvents`: the number of check results for a pinned module with a check id that the pinned version does not have. They count in nothing else.
- `focus`: the module in focus, or `null`.
  - `moduleId`, `version`, `title`: the pinned module. The title is untrusted text, at most 200 characters.
  - `tasksTotal`: the number of tasks in the module.
  - `tasksDone`: the number of tasks of the module whose latest `task_finished` result is `passed`.
  - `tasksSkipped`: the number of tasks of the module whose latest result is `abandoned` or `skipped`.
  - `nextTask`: `null`, or `{taskId, text}` for the first task in module order that has no `task_finished` event of any result. `text` is the prompt of the task, untrusted text, at most 5000 characters.
- `participation`: the sittings over time.
  - `windowWeeks`: 8.
  - `weeks`: 8 objects, oldest first. The last one holds today.
    - `start`: the Monday of the week in the student's local time, as `YYYY-MM-DD`.
    - `days`: 7 values, Monday first. Each is the number of sessions on that local day, or `null` for a day after today.
    - `sessions`: the sum of the day values.
    - `activeDays`: the number of days with at least one session.
  - `totals`: `sessions` and `activeDays` summed over the weeks, and `days`, the number of days from the start of the window up to and including today.
- `checkpoints`: `done`, the number of pinned modules with a `module_completed` event, and `total`, the number of pinned modules.
- `skills`: at most 100 items, sorted by `skillId`. Each has `skillId`, `passed`, `tried`, and `window` (always 5).
- `skillsTotal`: the number of skills with a result. A cut list is shorter than this number.
- `checks`: at most 20 items, newest first. Each has `moduleId`, `checkId`, `title`, `retriesBeforeFirstPass`, `open`, and `hintLevelReached`.
  - `title`: the description of the check from the module, at most 2000 characters. A check in a lesson module has a description and no title. The hub uses the description. Untrusted text.
- `checksTotal`: the number of checks with a result. A cut list is shorter than this number.
- `library`: every pinned module, in the order the lesson store lists them. Each has `moduleId`, `version`, `title` (at most 200 characters), `status`, `tasksTotal`, `tasksDone`, and `tasksSkipped`.
  - `status` is `done` (the module has a `module_completed` event), `focus` (it is the module in focus), or `next`.

## How each number is made

### Which events count

Only events with the source `student` or `app` count. Every number below leaves out `agent` events.
An event whose local day is after today counts in nothing. The clock of the writer was wrong, so the hub ignores such an event. `log.futureEvents` says how many there were.
A counted event with a wrong field is skipped alone. See "A damaged log".
An event from before the window still counts for the focus, the checkpoints, the skills, and the checks. It only stays out of `participation`.

### The local day

The code reads the `time` of the event with its own offset. The offset is `Z` or `+hh:mm`. It turns the time into minutes since 1970 in UTC.
With `timeZone`, the code asks the zone for the local date of that moment. The zone knows its own clock changes, so an event before and an event after a change both land on the right day.
With `tzOffsetMinutes`, the code adds the one fixed offset to every event. This is right for a zone with no clock change. Where the clock changes inside the window, the events on one side of the change land on the wrong day. The error is the size of the change, one hour in most zones.
An instant outside the years 1 to 9999 cannot be placed in a zone. The code uses its UTC day. Such an event is far from the window, so no number changes.
A zone is as old as the zone database on the computer. The hub does not model a rule that the database lacks.

### Participation

For each local day in the window, the code collects the distinct session ids of the counted events. The day value is the size of that set.
The window starts on the Monday 7 weeks before the Monday of the current week, in local time. It ends today. Days after today are `null`.
Events out of time order are fine. Each event goes to its own day.

### Checkpoints

`done` counts distinct pinned modules with a `module_completed` event. A completion for a module that is not pinned does not count, so `done` is never more than `total`.

### Skills

A skill is a label on a `check_result` event (`skills`). The code takes the results of a skill in log order and keeps the latest 5.
Only the status `passed` counts as passed. The statuses `failed` and `error` count as tried and not passed.
The statuses `skipped` and `self-reported` are not skill evidence. Such a result is not tried, and it does not take a place in the window of 5.
`tried` is the number of results in the window and `passed` is how many of them passed.
The list keeps the 100 skills with the most recent result in the log. `skillsTotal` counts all of them.
The log carries `skills: []` today, so the list is usually empty. This is correct. A later schema version lets a module tag its checks with skills. The code never makes up a skill.
Skills do not depend on the pins. A skill from a module that is no longer pinned still counts.

### Checks

The code looks at the checks of the pinned modules. A check with at least one counted `check_result` event shows.

- A `passed` result is a pass. A `self-reported` result is also a pass for the check, because the student can only report some checks by hand.
- `retriesBeforeFirstPass` is the number of `failed` and `error` results before the first pass. Results after the first pass do not count.
- A `skipped` result is neither a retry nor a pass. It does not make the check recent. A check with only `skipped` results does not show.
- If the check has no pass yet, `open` is true. The count is then the number of retries so far.
- `hintLevelReached` is the highest ladder level among the counted `hint_requested` events on the task of the check, before the first pass of that check. If the check has no pass yet, every hint so far counts. If there is no hint, it is `null`.
  A hint names a task and not a check. Two checks on one task share the hints that came before the pass of each. A check run for more than one task takes the highest level of those tasks.
- The list shows at most 20 checks. If there are more, it keeps the open checks first and then the most recent ones. It lists them newest first. `checksTotal` counts all of them.
- A check result for a pinned module with a check id that the pinned version lacks is not shown. `log.unmatchedEvents` counts such results. A pin that moves to a version with a renamed check causes this.

### Focus

The focus is a pinned module that has no `module_completed` event.
Among those, it is the one with the latest counted event in the log. "Latest" means last in the log, not the biggest `time`. Any counted event of the module counts, so a hint or a mode change moves the focus too.
If no open module has an event, the focus is the first open module in the list of the lesson store. With no events at all, that is the first pinned module.
If every pinned module is complete, or nothing is pinned, `focus` is `null`.

The task counts come from `task_finished` events. Its `result` is `passed`, `abandoned`, or `skipped`. The latest result of a task wins.
`tasksDone` counts tasks with `passed`. `tasksSkipped` counts tasks with `abandoned` or `skipped`. A result for a task that the pinned version lacks counts nowhere.
`nextTask` moves past a task with any result, so a student is not sent back to a task they gave up.

### Library

The library lists every pinned module in the order of the lesson store, with its status and its task counts. Modules that are stored but not pinned do not show. Events for modules that are not pinned do not change the focus, the library, the checkpoints, or the checks.
They still count as activity in `participation`.

### Events are tied to the module id, not the version

An event names a module version, but the hub counts by module id and the pinned task list. When the student moves a pin to a new version, the events from the old version still count for that module. A task id that the new version lacks is ignored by the task counts. A check id that it lacks is counted in `log.unmatchedEvents`.

## A damaged log

A damaged log is a finding. It is not an error. Every case below sends a line to stderr, so a fault is not silent.

- A log with a damaged line or a cut last line has `log.ok` false. The numbers come from the valid events only. `damagedLines` and `truncatedTail` say how much was left out.
- A log over its limits, or a log file that is not a regular file, gives the code `log_corrupt`.
- A file that fails to open or to read gives the code `log_unreadable`.
- A log file or a folder on its path that is a symbolic link gives the code `unsafe_path`.
  In each of these three cases, `log.ok` is false, `log.fault` holds the code, and the events are 0. The answer still has `focus`, `library`, and `checkpoints.total` from the lesson store.
  The hub writes the error class and the code to stderr. It does not write a path.
- A valid log never holds an event with missing or wrong fields. If one appears, the hub skips that event alone. All the other events count. `log.skippedEvents` counts the skipped events, and `log.ok` stays true.
  The hub writes the line number and the name of the wrong field for the first 5 skipped events, and then a total. It never writes event content.
- If the log is missing, the answer has `log.exists: false` and zeros.
- A busy lock is not damage. It is the error `lock_unavailable`.

## What a request costs

The route reads the log once for each request, with one call to `EventLog.read`. It does not add a cache.
The log store already keeps the checked log in memory while the file is unchanged (`docs/event-log.md`, "What a read costs").
That cache does not make a request free. Each read copies the checked lines and events before it gives them back, and the code then goes through every event once. The work is linear in the number of events.

The figures below are for a log within 0.1% of the 5 MB cap. That is about 13,900 events of the common kind `check_result`, or about 19,200 of the smallest kind.
They are medians of 5 runs on an Apple laptop with one process. The laptop was doing other work, so read them as a guide.

- Python 3.10, log of `check_result` events: the first request after a change takes about 1.4 s (at most 1.5 s). The next requests take about 0.24 s.
- Python 3.10, log of the smallest events: the first request takes about 1.6 s. The next requests take about 0.30 s.
- Python 3.14: the first request takes about 1.1 s. The next requests take about 0.10 to 0.13 s.
- The rollup alone, with the events already read, takes 0.04 to 0.07 s. With `timeZone` it takes about 0.02 s more for 19,000 events.
- The call to `EventLog.read` is the rest. Its first call checks every line. Its later calls copy the checked lines and events, which costs about 0.07 to 0.2 s.

Under heavy load from other programs, the same first request took 3 s to 20 s.
The server answers one request at a time. A hub request blocks the other requests while it runs.
The first request after any append pays the full check of the log. This is the known cost from the log store (E2).
A log of 50,000 lines cannot fit within 5 MB, because a valid event takes over 300 bytes. So the cap on bytes is the cap that matters. The log store refuses a log over a cap. The hub then shows `log.ok: false`.

Suppose this cost is too high for the screen. Then there are two next steps. One is to cache the answer by the signature of the log file and the pins. The other is to let the log store give out its checked events without a copy. This step does neither, because both change the log store.

## What this does not prove

- It does not prove that an event is true. A student event can be made up by the page. The hub shows what the log says.
- The numbers are not a measure of learning. A session count and an active day count show that the student sat down. They say nothing about how well. The hub does not rank, score, or compare the student with anyone.
- With `tzOffsetMinutes`, a clock change is not modelled. Use `timeZone`. See "The local day".
- A `self-reported` result is the student's own word. The hub treats it as a pass for a check, and it does not use it as evidence for a skill.
- Skills stay empty until modules tag their checks.
- The text caps are the maxima of the lesson contract: 200 characters for a title, 5000 for a task prompt, and 2000 for a check description. A valid module is never cut. A module that the contract refuses is cut at the cap. The response does not flag such a cut, because the module is not valid.
  The text is not changed in any other way. Control characters and bidirectional marks pass through as data, because the JSON text escapes them. The screen must escape the text it draws.
- A list that the hub cuts, which are `skills` and `checks`, has a total beside it. The reader can see the cut.
- The numbers are computed on each request. There is no history of past answers.
- The log has at most 80% of its room for the page. The hub does not show how full the log is.
- The tests run on Python 3.10, 3.12, 3.13, and 3.14. Linux CI is the final check.
