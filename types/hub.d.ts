/** The hub numbers: the answer of GET /api/hub?tzOffsetMinutes=N. Read only. See docs/hub-data.md for how each number is made. */
type HubHintLevel = 'nudge' | 'question' | 'partial-example' | 'full-answer';

/** What the log reader found. A log that is damaged or cannot be read is `ok: false`; the rest of the answer is still given. */
interface HubLog {
  /** False when there is no event log yet. */
  exists: boolean;
  /** False when some line is damaged, the last line is cut, or the log cannot be read. */
  ok: boolean;
  /** The code of the log store when the log could not be read at all, else null. Then `ok` is false and the event numbers are empty. */
  fault: 'log_corrupt' | 'log_unreadable' | 'unsafe_path' | null;
  /** The number of valid events in the log, from every source. */
  events: number;
  /** The number of lines that are not valid events. A cut last line is counted in `truncatedTail` instead. */
  damagedLines: number;
  truncatedTail: boolean;
  /** Valid events the hub left out because a field it reads was wrong. A valid log has none. */
  skippedEvents: number;
  /** Student or app events dated after today in the student's day. They count in nothing. */
  futureEvents: number;
  /** Check results for a pinned module whose check id the pinned version does not have. They count in nothing. */
  unmatchedEvents: number;
}

interface HubNextTask {
  taskId: string;
  /** The prompt of the task, written by the lesson creator. Treat it as untrusted text and escape it. */
  text: string;
}

/** The module in focus. */
interface HubFocus {
  moduleId: string;
  version: string;
  /** Written by the lesson creator. Treat it as untrusted text and escape it. */
  title: string;
  tasksTotal: number;
  /** Tasks whose latest finish is `passed`. */
  tasksDone: number;
  /** Tasks whose latest finish is `abandoned` or `skipped`. */
  tasksSkipped: number;
  /** The first task without any finish, or null when every task has one. */
  nextTask: HubNextTask | null;
}

/** The sessions on one day of the student's local time, or null for a day after today. */
type HubDay = number | null;

interface HubWeek {
  /** The Monday of the week in the student's local time, as YYYY-MM-DD. */
  start: string;
  days: [HubDay, HubDay, HubDay, HubDay, HubDay, HubDay, HubDay];
  sessions: number;
  activeDays: number;
}

interface HubTotals {
  sessions: number;
  activeDays: number;
  /** The days from the start of the window up to and including today. */
  days: number;
}

interface HubParticipation {
  windowWeeks: 8;
  /** Oldest week first. The last week holds today. */
  weeks: HubWeek[];
  totals: HubTotals;
}

interface HubCheckpoints {
  done: number;
  total: number;
}

/** The latest results for one skill label. Empty until lesson modules tag their checks with skills. A `self-reported` or `skipped` result is not counted. */
interface HubSkill {
  skillId: string;
  passed: number;
  tried: number;
  window: 5;
}

interface HubCheck {
  moduleId: string;
  checkId: string;
  /** The description of the check from the lesson module, at most 2000 characters. Untrusted text. */
  title: string;
  /** Results that are not a pass before the first pass. A `skipped` result is not counted. */
  retriesBeforeFirstPass: number;
  /** True when the check has no pass yet. A `passed` or a `self-reported` result is a pass. */
  open: boolean;
  /** The highest hint on the task of the check before the first pass, or all hints so far if the check is open. */
  hintLevelReached: HubHintLevel | null;
}

interface HubLibraryEntry {
  moduleId: string;
  version: string;
  /** Written by the lesson creator. Treat it as untrusted text and escape it. */
  title: string;
  status: 'done' | 'focus' | 'next';
  tasksTotal: number;
  tasksDone: number;
  tasksSkipped: number;
}

interface HubResponse {
  schemaVersion: '1.0';
  /** RFC 3339 UTC, in whole seconds. */
  generatedAt: string;
  log: HubLog;
  /** Null when no module is pinned, or when every pinned module is complete. */
  focus: HubFocus | null;
  participation: HubParticipation;
  checkpoints: HubCheckpoints;
  /** The 100 skills tried most recently, sorted by id. */
  skills: HubSkill[];
  /** All skills with a result, so a reader can tell when the list is cut. */
  skillsTotal: number;
  /** Up to 20 checks: the open ones first, then the most recent. They are listed newest first. */
  checks: HubCheck[];
  /** All checks with a result, so a reader can tell when the list is cut. */
  checksTotal: number;
  library: HubLibraryEntry[];
}
