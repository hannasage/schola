/** A clean, valid answer of GET /api/hub for unit tests. `changes` is merged at the top level. */
export function sampleHub(changes = {}) {
  const week = (start, days) => ({
    start, days,
    sessions: days.reduce((sum, value) => sum + (value ?? 0), 0),
    activeDays: days.filter(value => value).length,
  });
  const starts = ['2026-09-14', '2026-09-21', '2026-09-28', '2026-10-05', '2026-10-12', '2026-10-19', '2026-10-26', '2026-11-02'];
  const weeks = starts.map((start, index) => week(start, index === 7 ? [1, 2, 0, null, null, null, null] : [0, 1, 0, 0, 2, 0, 0]));
  return {
    schemaVersion: '1.0',
    generatedAt: '2026-11-04T12:00:00Z',
    log: {exists: true, ok: true, fault: null, events: 12, damagedLines: 0, truncatedTail: false, skippedEvents: 0, futureEvents: 0, unmatchedEvents: 0},
    focus: {moduleId: 'alpha', version: '1.0.0', title: 'Alpha module', tasksTotal: 5, tasksDone: 2, tasksSkipped: 1,
      nextTask: {taskId: 'task-4', text: 'Write the second function.'}},
    participation: {windowWeeks: 8, weeks, totals: {sessions: weeks.reduce((s, w) => s + w.sessions, 0), activeDays: weeks.reduce((s, w) => s + w.activeDays, 0), days: 52}},
    checkpoints: {done: 1, total: 3},
    skills: [],
    skillsTotal: 0,
    checks: [{moduleId: 'alpha', checkId: 'check-1', title: 'First check', retriesBeforeFirstPass: 2, open: false, hintLevelReached: 'question'}],
    checksTotal: 1,
    library: [
      {moduleId: 'alpha', version: '1.0.0', title: 'Alpha module', status: 'focus', tasksTotal: 5, tasksDone: 2, tasksSkipped: 1},
      {moduleId: 'beta', version: '1.0.0', title: 'Beta module', status: 'done', tasksTotal: 2, tasksDone: 2, tasksSkipped: 0},
      {moduleId: 'gamma', version: '1.0.0', title: 'Gamma module', status: 'next', tasksTotal: 3, tasksDone: 0, tasksSkipped: 0},
    ],
    ...changes,
  };
}
