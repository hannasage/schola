/**
 * The pure parts of the hub screen: checking the answer, and turning numbers into cells, segments, and notices.
 * Nothing here reads a clock, a network, or the page. See docs/hub-screen.md.
 * The shape of the answer is `HubResponse` in `types/hub.d.ts`.
 */

export const HUB_SCHEMA_VERSION = '1.0';
/** The hint ladder, lowest level first. */
export const HINT_LADDER = /** @type {const} */(['nudge', 'question', 'partial-example', 'full-answer']);
const HINT_WORDS = {nudge: 'nudge', question: 'question', 'partial-example': 'partial example', 'full-answer': 'full answer'};
const FAULTS = ['log_corrupt', 'log_unreadable', 'unsafe_path'];
const STATUSES = ['done', 'focus', 'next'];
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const WEEKS = 8;
const SKILL_WINDOW = 5;
const DAY = /^\d{4}-\d{2}-\d{2}$/;

// ---------------------------------------------------------------------------
// Checking the answer
// ---------------------------------------------------------------------------

/** @param {unknown} value @returns {value is Record<string, unknown>} */
const isObject = value => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
/** @param {unknown} value @returns {value is number} */
const isCount = value => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
/** @param {unknown} value @returns {value is string} */
const isText = value => typeof value === 'string';
/** @param {unknown} value */
function isDay(value) {
  if (!isText(value) || !DAY.test(value)) return false;
  const date = new Date(value + 'T00:00:00Z');
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

/**
 * Each check returns the name of the first field that is wrong, or '' when the value is fine.
 * The names are fixed strings written here. They never hold text from the answer.
 * @param {Record<string, unknown>} object @param {string} path @param {Record<string, (value: unknown) => boolean>} rules
 */
function fields(object, path, rules) {
  for (const [key, ok] of Object.entries(rules)) if (!ok(object[key])) return path + key;
  return '';
}

/** @param {unknown} list @param {string} path @param {(item: Record<string, unknown>, path: string) => string} check */
function listOf(list, path, check) {
  if (!Array.isArray(list)) return path;
  for (let index = 0; index < list.length; index += 1) {
    const item = list[index];
    const where = `${path}[${index}]`;
    if (!isObject(item)) return where;
    const bad = check(item, where);
    if (bad) return bad;
  }
  return '';
}

/** @param {Record<string, unknown>} hub */
function firstProblem(hub) {
  if (hub.schemaVersion !== HUB_SCHEMA_VERSION) return 'schemaVersion';
  if (!isText(hub.generatedAt)) return 'generatedAt';
  if (!isObject(hub.log)) return 'log';
  const logBad = fields(hub.log, 'log.', {
    exists: value => typeof value === 'boolean', ok: value => typeof value === 'boolean',
    fault: value => value === null || (isText(value) && FAULTS.includes(value)),
    events: isCount, damagedLines: isCount, truncatedTail: value => typeof value === 'boolean',
    skippedEvents: isCount, futureEvents: isCount, unmatchedEvents: isCount,
  });
  if (logBad) return logBad;

  if (hub.focus !== null) {
    if (!isObject(hub.focus)) return 'focus';
    const focusBad = fields(hub.focus, 'focus.', {
      moduleId: isText, version: isText, title: isText, tasksTotal: isCount, tasksDone: isCount, tasksSkipped: isCount,
      nextTask: value => value === null || (isObject(value) && isText(value.taskId) && isText(value.text)),
    });
    if (focusBad) return focusBad;
  }

  if (!isObject(hub.participation)) return 'participation';
  const part = hub.participation;
  if (part.windowWeeks !== WEEKS) return 'participation.windowWeeks';
  if (!Array.isArray(part.weeks) || part.weeks.length !== WEEKS) return 'participation.weeks';
  const weeksBad = listOf(part.weeks, 'participation.weeks', (week, where) => fields(week, where + '.', {
    start: isDay, sessions: isCount, activeDays: isCount,
    days: value => Array.isArray(value) && value.length === 7 && value.every(day => day === null || isCount(day)),
  }));
  if (weeksBad) return weeksBad;
  if (!isObject(part.totals)) return 'participation.totals';
  const totalsBad = fields(part.totals, 'participation.totals.', {sessions: isCount, activeDays: isCount, days: isCount});
  if (totalsBad) return totalsBad;

  if (!isObject(hub.checkpoints)) return 'checkpoints';
  const checkpointsBad = fields(hub.checkpoints, 'checkpoints.', {done: isCount, total: isCount});
  if (checkpointsBad) return checkpointsBad;

  const skillsBad = listOf(hub.skills, 'skills', (skill, where) => {
    const bad = fields(skill, where + '.', {skillId: isText, passed: isCount, tried: isCount, window: value => value === SKILL_WINDOW});
    if (bad) return bad;
    return /** @type {number} */(skill.passed) > /** @type {number} */(skill.tried) ? where + '.passed' : '';
  });
  if (skillsBad) return skillsBad;
  if (!isCount(hub.skillsTotal)) return 'skillsTotal';

  const checksBad = listOf(hub.checks, 'checks', (check, where) => fields(check, where + '.', {
    moduleId: isText, checkId: isText, title: isText, retriesBeforeFirstPass: isCount, open: value => typeof value === 'boolean',
    hintLevelReached: value => value === null || (isText(value) && /** @type {readonly string[]} */(HINT_LADDER).includes(value)),
  }));
  if (checksBad) return checksBad;
  if (!isCount(hub.checksTotal)) return 'checksTotal';

  const libraryBad = listOf(hub.library, 'library', (entry, where) => fields(entry, where + '.', {
    moduleId: isText, version: isText, title: isText, status: value => isText(value) && STATUSES.includes(value),
    tasksTotal: isCount, tasksDone: isCount, tasksSkipped: isCount,
  }));
  if (libraryBad) return libraryBad;
  return relationProblem(/** @type {HubResponse} */(/** @type {unknown} */(hub)));
}

/** The sum of a list of numbers. @param {number[]} values */
const sum = values => values.reduce((total, value) => total + value, 0);

/**
 * Relations that `hub_data.py` always keeps. An answer that breaks one did not come from a sound server.
 * Only guaranteed relations are checked here.
 * @param {HubResponse} hub
 */
function relationProblem(hub) {
  const {checkpoints, participation, library, focus} = hub;
  if (checkpoints.done > checkpoints.total) return 'checkpoints.done';
  // The library has one entry for each pinned module, and the checkpoint total counts the pinned modules.
  if (library.length !== checkpoints.total) return 'checkpoints.total';
  for (let index = 0; index < library.length; index += 1) {
    const entry = library[index];
    if (entry.tasksDone > entry.tasksTotal) return `library[${index}].tasksDone`;
    if (entry.tasksSkipped > entry.tasksTotal) return `library[${index}].tasksSkipped`;
    if (entry.tasksDone + entry.tasksSkipped > entry.tasksTotal) return `library[${index}].tasksSkipped`;
  }
  if (library.filter(entry => entry.status === 'done').length !== checkpoints.done) return 'library.status';
  const inFocus = library.filter(entry => entry.status === 'focus');
  if (focus === null ? inFocus.length !== 0 : inFocus.length !== 1 || inFocus[0].moduleId !== focus.moduleId || inFocus[0].version !== focus.version) return 'library.focus';
  if (focus) {
    if (focus.tasksDone > focus.tasksTotal) return 'focus.tasksDone';
    if (focus.tasksSkipped > focus.tasksTotal || focus.tasksDone + focus.tasksSkipped > focus.tasksTotal) return 'focus.tasksSkipped';
  }
  for (let index = 0; index < hub.skills.length; index += 1) {
    if (hub.skills[index].tried > hub.skills[index].window) return `skills[${index}].tried`;
  }
  if (hub.skills.length > hub.skillsTotal) return 'skillsTotal';
  if (hub.checks.length > hub.checksTotal) return 'checksTotal';
  const {weeks, totals} = participation;
  for (let index = 0; index < weeks.length; index += 1) {
    const week = weeks[index];
    const where = `participation.weeks[${index}]`;
    if (index > 0 && dayDate(weeks[index - 1].start, 7) !== week.start) return where + '.start';
    const counted = week.days.filter(day => day !== null);
    // A day after today is null. Only the last week can hold one, and the nulls close the week.
    const firstNull = week.days.indexOf(null);
    if (firstNull !== -1 && (index !== weeks.length - 1 || firstNull === 0 || week.days.slice(firstNull).some(day => day !== null))) return where + '.days';
    if (week.sessions !== sum(counted)) return where + '.sessions';
    if (week.activeDays !== counted.filter(day => day > 0).length) return where + '.activeDays';
  }
  if (totals.sessions !== sum(weeks.map(week => week.sessions))) return 'participation.totals.sessions';
  if (totals.activeDays !== sum(weeks.map(week => week.activeDays))) return 'participation.totals.activeDays';
  if (totals.days !== sum(weeks.map(week => week.days.filter(day => day !== null).length))) return 'participation.totals.days';
  return '';
}

/**
 * Check the answer of GET /api/hub before the screen reads it.
 * A wrong shape gives a reason that names a field. The reason holds no text from the answer.
 * @param {unknown} value
 * @returns {{ok: true, hub: HubResponse} | {ok: false, reason: string}}
 */
export function readHub(value) {
  if (!isObject(value)) return {ok: false, reason: 'The answer is not an object.'};
  for (const key of ['schemaVersion', 'generatedAt', 'log', 'focus', 'participation', 'checkpoints', 'skills', 'skillsTotal', 'checks', 'checksTotal', 'library']) {
    if (!(key in value)) return {ok: false, reason: `The field ${key} is missing.`};
  }
  const bad = firstProblem(value);
  if (bad) return {ok: false, reason: `The field ${bad} is wrong.`};
  return {ok: true, hub: /** @type {HubResponse} */(/** @type {unknown} */(value))};
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/** @param {number} count @param {string} singular @param {string} [pluralForm] */
export function plural(count, singular, pluralForm = singular + 's') {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

/** The share of `part` in `whole`, as a whole number from 0 to 100. Zero when the whole is zero. @param {number} part @param {number} whole */
export function percent(part, whole) {
  if (!(whole > 0)) return 0;
  return Math.min(100, Math.max(0, Math.round(part / whole * 100)));
}

/** @param {number} value @param {number} low @param {number} high */
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

// ---------------------------------------------------------------------------
// Participation
// ---------------------------------------------------------------------------

/** @param {number | null} count @returns {'future' | 'none' | 'one' | 'many'} */
export function heatLevel(count) {
  if (count === null) return 'future';
  if (count >= 2) return 'many';
  return count === 1 ? 'one' : 'none';
}

/** The date of one day of a week, as YYYY-MM-DD. `index` 0 is the Monday. @param {string} weekStart @param {number} index */
export function dayDate(weekStart, index) {
  const date = new Date(weekStart + 'T00:00:00Z');
  date.setUTCDate(date.getUTCDate() + index);
  return date.toISOString().slice(0, 10);
}

/**
 * One row for each week, one cell for each day. Every cell has a sentence, so no meaning rests on colour.
 * @param {HubParticipation} participation
 */
export function heatRows(participation) {
  return participation.weeks.map(week => ({
    start: week.start,
    sessions: week.sessions,
    activeDays: week.activeDays,
    cells: week.days.map((count, index) => {
      const date = dayDate(week.start, index);
      const weekday = WEEKDAYS[index];
      return {
        date, weekday, count, level: heatLevel(count),
        text: `${weekday} ${date}: ${count === null ? 'not yet' : plural(count, 'session')}`,
      };
    }),
  }));
}

/** The short names of the days, Monday first. */
export const WEEKDAY_NAMES = WEEKDAYS;

// ---------------------------------------------------------------------------
// Segments and cells
// ---------------------------------------------------------------------------

/**
 * Share `cells` among `parts`. A part that is not zero gets at least one cell. The largest part gives up or takes the rest.
 * @param {number[]} parts non-negative whole numbers
 * @param {number} cells at least as many as there are non-zero parts
 */
function scaleParts(parts, cells) {
  const whole = sum(parts);
  const out = parts.map(part => part === 0 ? 0 : Math.max(1, Math.round(part * cells / whole)));
  let extra = sum(out) - cells;
  while (extra !== 0) {
    const largest = out.indexOf(Math.max(...out));
    out[largest] += extra > 0 ? -1 : 1;
    extra += extra > 0 ? -1 : 1;
  }
  return out;
}

/** @template T @param {T} value @param {number} count @returns {T[]} */
const repeat = (value, count) => Array.from({length: count}, () => value);

/**
 * One segment for each task: done first, then skipped, then open.
 * A module with more tasks than `cap` is scaled. Every part that is not zero keeps at least one segment.
 * @param {{done: number, skipped: number, total: number}} counts @param {number} [cap]
 * @returns {('done' | 'skipped' | 'open')[]}
 */
export function meterSegments({done, skipped, total}, cap = 12) {
  total = Math.max(0, total);
  done = clamp(done, 0, total);
  skipped = clamp(skipped, 0, total - done);
  const open = total - done - skipped;
  const [doneCells, skippedCells, openCells] = total > cap ? scaleParts([done, skipped, open], cap) : [done, skipped, open];
  return [...repeat(/** @type {'done'} */('done'), doneCells), ...repeat(/** @type {'skipped'} */('skipped'), skippedCells), ...repeat(/** @type {'open'} */('open'), openCells)];
}

/**
 * The checkpoint grid: the done ones first, then the next one, then the rest.
 * A grid of more than `cap` checkpoints is scaled. The next cell stays one cell, and done and ahead each keep a cell when they exist.
 * @param {number} done @param {number} total @param {number} [cap]
 * @returns {('done' | 'next' | 'ahead')[]}
 */
export function checkpointCells(done, total, cap = 60) {
  total = Math.max(0, total);
  done = clamp(done, 0, total);
  const next = done < total ? 1 : 0;
  const ahead = Math.max(total - done - 1, 0);
  const [doneCells, aheadCells] = total > cap ? scaleParts([done, ahead], cap - next) : [done, ahead];
  return [...repeat(/** @type {'done'} */('done'), doneCells), ...repeat(/** @type {'next'} */('next'), next), ...repeat(/** @type {'ahead'} */('ahead'), aheadCells)];
}

/**
 * The window of a skill: the passes, then the results that were not passes, then the places not used yet.
 * The cells show counts. They do not show the order of the results.
 * @param {HubSkill} skill
 * @returns {('pass' | 'miss' | 'none')[]}
 */
export function skillCells(skill) {
  const size = skill.window;
  const pass = clamp(skill.passed, 0, size);
  const miss = clamp(skill.tried - skill.passed, 0, size - pass);
  return Array.from({length: size}, (_, index) => index < pass ? 'pass' : index < pass + miss ? 'miss' : 'none');
}

/** Passed over tried, or null when nothing was tried. @param {HubSkill} skill */
export function skillPercent(skill) {
  return skill.tried > 0 ? percent(skill.passed, skill.tried) : null;
}

/** @param {HubHintLevel | null} level */
export function hintInfo(level) {
  const index = level === null ? -1 : HINT_LADDER.indexOf(level);
  return {step: index + 1, of: HINT_LADDER.length, label: level === null || index < 0 ? 'none' : HINT_WORDS[level]};
}

/** @param {HubLibraryEntry['status']} status */
export function libraryLabel(status) {
  return status === 'done' ? 'Done' : status === 'focus' ? 'In focus' : 'Next';
}

/** The state line of a library entry: its status, its tasks done, and its skipped tasks when there are some. @param {HubLibraryEntry} entry */
export function libraryLine(entry) {
  const skipped = entry.tasksSkipped > 0 ? ` \u00b7 ${entry.tasksSkipped} skipped` : '';
  return `${libraryLabel(entry.status)} \u00b7 ${entry.tasksDone} of ${plural(entry.tasksTotal, 'task')}${skipped}`;
}

/** What a panel says in place of numbers that come from events when the log is not whole. */
export const UNAVAILABLE = 'Not available while the event log cannot be read.';

/**
 * True when the log is damaged or cannot be read. Every number that comes from events is then left out.
 * The lesson store still gives the module titles, the task totals, and the checkpoint total.
 * @param {HubResponse} hub
 */
export function eventsUnavailable(hub) {
  return !hub.log.ok;
}

/**
 * "Updated 14:05" in the student's own time, or with a short date when the answer is from another day.
 * Falls back to a short date if the time zone or the locale is not usable. Gives '' for a time that is not a date.
 * @param {string} iso @param {{timeZone?: string, locale?: string, now?: Date}} [options]
 */
export function updatedLabel(iso, {timeZone, locale, now = new Date()} = {}) {
  const time = new Date(typeof iso === 'string' ? iso : NaN);
  if (Number.isNaN(time.getTime())) return '';
  try {
    const dayOf = (/** @type {Date} */ date) => new Intl.DateTimeFormat('en-CA', {year: 'numeric', month: 'numeric', day: 'numeric', timeZone}).format(date);
    const clock = /** @type {Intl.DateTimeFormatOptions} */({hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone});
    const same = dayOf(time) === dayOf(now);
    return 'Updated ' + new Intl.DateTimeFormat(locale, same ? clock : {day: 'numeric', month: 'short', ...clock}).format(time);
  } catch {
    return 'Updated ' + time.toISOString().slice(0, 10);
  }
}

// ---------------------------------------------------------------------------
// Findings about the data
// ---------------------------------------------------------------------------

const FAULT_WORDS = {
  log_corrupt: 'The log file is over its limits or is not a regular file.',
  log_unreadable: 'The log file did not open or did not read.',
  unsafe_path: 'The log file, or a folder on its path, is a symbolic link, so Schola did not open it.',
};

/**
 * Every finding about the data becomes one visible notice. Nothing turns into a silent zero.
 * @param {HubResponse} hub
 * @returns {{id: string, tone: 'info' | 'warning', title: string, text: string}[]}
 */
export function logNotices(hub) {
  const {log} = hub;
  /** @type {{id: string, tone: 'info' | 'warning', title: string, text: string}[]} */
  const notices = [];
  if (log.fault) {
    notices.push({id: 'fault', tone: 'warning', title: 'Schola cannot read your event log',
      text: `${FAULT_WORDS[log.fault]} The parts that come from events are not available. Your module titles still show.`});
  }
  if (log.damagedLines > 0) {
    notices.push({id: 'damaged', tone: 'warning', title: 'Part of your event log is damaged',
      text: `${plural(log.damagedLines, 'line')} of the log ${log.damagedLines === 1 ? 'is' : 'are'} not valid. Schola left ${log.damagedLines === 1 ? 'it' : 'them'} out. The parts that come from events are not shown until the log is whole.`});
  }
  if (log.truncatedTail) {
    notices.push({id: 'truncated', tone: 'warning', title: 'The last line of your event log is cut off',
      text: 'Schola left that line out. The parts that come from events are not shown until the log is whole.'});
  }
  if (!log.ok && !log.fault && log.damagedLines === 0 && !log.truncatedTail) {
    notices.push({id: 'not-ok', tone: 'warning', title: 'Schola found a problem in your event log',
      text: 'Schola cannot say more. The parts that come from events are not shown.'});
  }
  if (log.skippedEvents > 0) {
    notices.push({id: 'skipped', tone: 'warning', title: 'Some events were left out',
      text: `${plural(log.skippedEvents, 'event')} had a field that Schola cannot read. Schola leaves ${log.skippedEvents === 1 ? 'it' : 'them'} out of every number.`});
  }
  if (log.futureEvents > 0) {
    notices.push({id: 'future', tone: 'warning', title: 'Some events are dated after today',
      text: `${plural(log.futureEvents, 'event')} ${log.futureEvents === 1 ? 'is' : 'are'} dated after today. Schola leaves ${log.futureEvents === 1 ? 'it' : 'them'} out of every number. The clock of the device that wrote ${log.futureEvents === 1 ? 'it' : 'them'} can be wrong.`});
  }
  if (log.unmatchedEvents > 0) {
    notices.push({id: 'unmatched', tone: 'warning', title: 'Some check results have no matching check',
      text: `${plural(log.unmatchedEvents, 'check result')} name${log.unmatchedEvents === 1 ? 's' : ''} a check that the pinned version of the module does not have. Schola leaves ${log.unmatchedEvents === 1 ? 'it' : 'them'} out of every number.`});
  }
  if (hub.checksTotal > hub.checks.length) {
    notices.push({id: 'checks-cut', tone: 'info', title: 'The list of checks is cut',
      text: `Showing ${hub.checks.length} of ${hub.checksTotal} checks. Open checks come first, then the most recent.`});
  }
  if (hub.skillsTotal > hub.skills.length) {
    notices.push({id: 'skills-cut', tone: 'info', title: 'The list of skills is cut',
      text: `Showing ${hub.skills.length} of ${hub.skillsTotal} skills. These are the ones tried most recently.`});
  }
  return notices;
}

/** True for a clean log with no events, or no log yet. A damaged or unreadable log is not empty. @param {HubResponse} hub */
export function isEmptyLog(hub) {
  return hub.log.ok && (!hub.log.exists || hub.log.events === 0);
}
