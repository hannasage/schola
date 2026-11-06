# The hub screen

This page describes step E7b. It covers the Hub view: the screen that draws the numbers of `GET /api/hub`.
Read `docs/hub-data.md` first. It says how each number is made. This page says how the screen shows them.

The code is in `web/hub/`. The call to the server is `web/services/hub.js`.
The tests are `tests/hub-*.test.mjs` and `tests/browser/hub.mjs`.

## Terms

- The hub is the home screen of Schola. The Hub view is the first item in the navigation.
- A session is one sitting. The event log groups the events of one sitting under one session id.
- An active day is a day with at least one session.
- A checkpoint is a module that the student has finished.
- A skill is a label on a check. A module author adds it.
- The event log is the file of learning events that the app writes. A pinned module is a lesson module with a version that the student chose.
- Untrusted text is text that a lesson author wrote: module titles, task prompts, check descriptions, and ids.

## What the screen shows

The screen has five panels and a library rail.

- **Continue.** The module in focus, its tasks done and skipped, and the next task. A strip of segments shows the tasks: done first, then skipped, then open. A module with more than 12 tasks is scaled to 12 segments. A part is never drawn as nothing or as all.
- **Participation.** Sessions and active days for each of the last 8 weeks, as plain counts. A table has one row for each week and one cell for each day. Each cell holds its count as a digit. A day after today is empty and dashed. There is no goal and no comparison.
- **Checkpoints.** The number of finished modules out of the number of pinned modules, and a grid with one cell for each checkpoint. A grid of more than 60 cells is scaled to 60. The number beside the grid is exact.
- **Skill estimate.** One row for each skill: its label, a window of 5 cells, passed over tried, and a percent. A button, "How is this computed?", opens the rules. The cells show counts. They do not show the order of the results.
- **Tries and hints.** One row for each check: the retries before the first pass and the highest hint level reached. A check with no pass yet says "Not passed yet", and its retries read "so far". The screen shows these figures and adds no judgment.
- **Library.** Every pinned module, in the order of the lesson store, with its status (Done, In focus, or Next) and its tasks done. On a screen under 961 pixels wide, the rail folds into a section below the panels.

The tutor offer is one line at the foot of the Continue panel. It shows only when an offer exists and offers are not muted. See "Limits".

## Where each number comes from

Every number comes from the answer of `GET /api/hub`. The screen computes nothing new except layout: a count of cells, a percent, a date of a day in a week.
`docs/hub-data.md` has the rules for each field:

- Continue: `focus`. See "Focus".
- Participation: `participation`. See "Participation".
- Checkpoints: `checkpoints`. See "Checkpoints".
- Skills: `skills` and `skillsTotal`. See "Skills".
- Tries and hints: `checks` and `checksTotal`. See "Checks".
- Library: `library`. See "Library".

The percent of a skill is passed over tried, rounded to a whole number. A skill with nothing tried shows `n/a`.

## What the screen never shows

The screen has no streak, no leaderboard, no ranking, no total of time, and no label about the student, such as "guessing" or "frustrated".
The answer has no field for these, and the browser test reads the whole screen for those words.

## The states

The screen has its own state for each case. No case shows a silent zero.

- **Loading.** A status line and grey blocks. It shows while the first request runs.
- **Request failed.** A red notice, the reason in plain words, and a "Try again" button. The screen shows no numbers beside the notice. The reasons are: the server did not answer, the page token was refused, the log is busy, a lesson file is damaged, the server sent an answer that Schola cannot read, and any other fault with its code.
- **Empty log.** The log is whole and has no events, or there is no log yet. The screen says "No activity yet" and still shows the modules and the library. Counts read 0.
- **Ready.** Everything else. The screen shows one notice for each finding below.

Each finding about the data has its own notice with its count:

- The log cannot be read at all (`log.fault`). The notice names the cause. The numbers that come from events show as empty. A fault is not an empty log.
- Lines of the log are damaged (`log.damagedLines`).
- The last line of the log is cut off (`log.truncatedTail`).
- The log is not whole and none of the above says why (`log.ok` is false). The notice says that Schola cannot say more.
- Events were left out because a field was wrong (`log.skippedEvents`).
- Events are dated after today (`log.futureEvents`).
- Check results name a check that the pinned version does not have (`log.unmatchedEvents`).
- The list of checks is cut (`checksTotal` is more than the list).
- The list of skills is cut (`skillsTotal` is more than the list).

The skills list is empty until modules tag their checks with skills. The panel then says "Skill estimates need modules that tag their checks with skills. None of your modules do yet." It draws no meter.

## Text from a lesson author

Every string from the answer is untrusted text. The screen draws it only as a React text node.
It uses no `dangerouslySetInnerHTML`, no markdown, no automatic links, and no `href` built from data.

`web/hub/text.js` makes the text safe before it is drawn. It follows the rule of `safe_text` in `schola_cli.py`:

- A control character, a bidirectional control, a zero-width character, a tag character, a private use character, a look-alike blank, or a lone surrogate shows as `<U+XXXX>`.
- A run of more than two combining marks is cut, and the cut says how many marks.
- Long text ends in an ellipsis. The limit counts shown characters. An escape counts for all of its characters.
- A task prompt keeps its line breaks, with at most one blank line in a row.

The limits are 120 characters for a title, 600 for a task prompt, 160 for a check description, and 60 for an id. A cut never hides information that a person must hover to read.

## How the screen gets its data

`web/services/hub.js` calls `GET /api/hub` with the session token in the `X-Schola-Check-Token` header, the same way as the check routes.

- The first request sends `timeZone` with the name that the browser reports.
- If the server answers 400 for the field `timeZone`, one more request sends `tzOffsetMinutes`, the minutes east of UTC. The page sends only one of the two fields in a request.
- If the server rejects the token, the page fetches a new token once and sends the same request again.
- `web/hub/model.js` checks the answer before the screen reads it: the schema version `1.0` and every field the screen uses. A wrong shape is the "request failed" state, not a crash.
- The screen asks again each time the Hub view opens. A newer request wins over an older one.

## Look and feel

The look is called Bench Instrument. It is dark only. It does not follow the appearance setting of the rest of the app.
`web/hub/theme.js` holds the Bench theme as a Projection UI theme: flat, sharp, and dark. The panels, notices, buttons, table, and the "how is this computed" view are Projection UI components.
Four parts are custom, because the library has none: the heat table, the checkpoint grid, the skill and task meters, and the mono readout.

- Type is IBM Plex Sans for reading and IBM Plex Mono for numbers and labels. The fonts are files in `assets/fonts/`, and the local server serves them from a fixed list. See `THIRD_PARTY.md`.
- Contrast is at least 4.5 to 1 for text. The readout grey `#93A0AC` on the panel colour `#14181C` is checked by a test.
- Focus shows an amber ring of 2 pixels on every control.
- Buttons are at least 44 pixels high and wide.
- Motion is a fade of 120 milliseconds when content appears. A person who asks for reduced motion gets none.
- The screen fits 375, 768, and 1280 pixels wide with no sideways scroll of the page.

The heat table and the checkpoint grid do not rely on colour. The heat table is a real table, and each cell has a sentence for a screen reader. The checkpoint grid is a list with a word for each cell, and the number beside it is exact.

## Limits

- No server route sends tutor offers yet. `TutorOffer` is built and tested with props, and the screen does not mount it with data.
- No lesson screen exists yet, so the Continue panel has no button. The screen takes an optional `onContinue`.
- The view is not the landing view. The owner has not decided that.
- The numbers show what the log says. They do not prove that an event is true, and they are not a measure of learning. See `docs/hub-data.md`, "What this does not prove".
- A student who has many modules scrolls the library rail. The rail is a focusable region so the keyboard reaches it.
- The browser test cannot produce a skipped event or a list that is cut with the real stores. It tests those two cases with a stubbed answer. The unit tests cover them too.
