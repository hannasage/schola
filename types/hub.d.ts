/** The hub numbers: the answer of GET /api/hub?tzOffsetMinutes=N. Read only. See docs/hub-data.md for how each number is made. */
type HubHintLevel = 'nudge' | 'question' | 'partial-example' | 'full-answer';

/** What the log reader found. A log that is damaged or cannot be read is `ok: false`; the rest of the answer is still given. */
interface HubLog {
  /** False when there is no event log yet. */
  exists: boolean;
  ok: boolean;
  /** The number of valid events in the log, from every source. */
  events: number;
  /** The number of lines that are not valid events. A cut last line is counted in `truncatedTail` instead. */
  damagedLines: number;
  truncatedTail: boolean;
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
  tasksDone: number;
  /** The first task without a finish, or null when every task has one. */
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

/** The latest results for one skill label. Empty until lesson modules tag their checks with skills. */
interface HubSkill {
  skillId: string;
  passed: number;
  tried: number;
  window: 5;
}

interface HubCheck {
  moduleId: string;
  checkId: string;
  /** The description of the check from the lesson module, cut to 200 characters. Untrusted text. */
  title: string;
  retriesBeforeFirstPass: number;
  /** True when the check has not passed yet. */
  open: boolean;
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
  skills: HubSkill[];
  /** The 20 most recently active checks, newest first. */
  checks: HubCheck[];
  library: HubLibraryEntry[];
}
