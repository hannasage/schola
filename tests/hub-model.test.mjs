import assert from 'node:assert/strict';
import test from 'node:test';
import {
  readHub, heatLevel, dayDate, heatRows, percent, meterSegments, checkpointCells, skillCells, skillPercent,
  hintInfo, libraryLabel, libraryLine, logNotices, isEmptyLog, plural, HINT_LADDER, eventsUnavailable, UNAVAILABLE, updatedLabel,
} from '../web/hub/model.js';
import {sampleHub} from './hub-fixture.mjs';

const clone = value => structuredClone(value);

test('a valid answer is accepted and returned as it is', () => {
  const hub = sampleHub();
  const result = readHub(hub);
  assert.equal(result.ok, true);
  assert.deepEqual(result.hub, hub);
});

test('an answer that is not an object, or has another schema version, is refused', () => {
  for (const value of [null, undefined, 5, 'text', [], true]) assert.equal(readHub(value).ok, false);
  const result = readHub({...sampleHub(), schemaVersion: '2.0'});
  assert.equal(result.ok, false);
  assert.match(result.reason, /schemaVersion/);
  assert.equal(readHub({...sampleHub(), schemaVersion: 1}).ok, false);
});

test('every required key is checked, and the reason names the key', () => {
  for (const key of ['generatedAt', 'log', 'focus', 'participation', 'checkpoints', 'skills', 'skillsTotal', 'checks', 'checksTotal', 'library']) {
    const hub = sampleHub();
    delete hub[key];
    const result = readHub(hub);
    assert.equal(result.ok, false, key);
    assert.ok(result.reason.includes(key), `${key}: ${result.reason}`);
  }
});

test('nested shapes are checked', () => {
  const cases = {
    'log.ok': hub => { hub.log.ok = 'yes'; },
    'log.fault': hub => { hub.log.fault = 'boom'; },
    'log.events': hub => { hub.log.events = -1; },
    'log.damagedLines': hub => { hub.log.damagedLines = 1.5; },
    'focus.title': hub => { hub.focus.title = 5; },
    'focus.nextTask': hub => { hub.focus.nextTask = {taskId: 'a'}; },
    'participation.weeks': hub => { hub.participation.weeks.pop(); },
    'participation.weeks[2].days': hub => { hub.participation.weeks[2].days.pop(); },
    'participation.weeks[0].days': hub => { hub.participation.weeks[0].days[1] = -1; },
    'participation.weeks[1].start': hub => { hub.participation.weeks[1].start = '2026-13-45'; },
    'participation.totals': hub => { hub.participation.totals.sessions = 'many'; },
    'checkpoints.total': hub => { hub.checkpoints.total = null; },
    'skills': hub => { hub.skills = [{skillId: 'loops', passed: 2, tried: 1, window: 5}]; },
    'skills[0].window': hub => { hub.skills = [{skillId: 'loops', passed: 1, tried: 2, window: 6}]; },
    'checks[0].hintLevelReached': hub => { hub.checks[0].hintLevelReached = 'shout'; },
    'checks[0].open': hub => { hub.checks[0].open = 'no'; },
    'library[1].status': hub => { hub.library[1].status = 'weird'; },
    'library': hub => { hub.library = {}; },
  };
  for (const [name, change] of Object.entries(cases)) {
    const hub = clone(sampleHub());
    change(hub);
    const result = readHub(hub);
    assert.equal(result.ok, false, name);
    assert.ok(result.reason.includes(name.split('[')[0]), `${name}: ${result.reason}`);
  }
});

test('relations the server guarantees are checked, and the reason names the field', () => {
  const cases = {
    'checkpoints.done': hub => { hub.checkpoints.done = 4; },
    'checkpoints.total': hub => { hub.checkpoints.total = 2; },
    'library.status': hub => { hub.library[2].status = 'done'; },
    'library.focus': hub => { hub.library[0].status = 'next'; },
    'focus.tasksDone': hub => { hub.focus.tasksDone = 6; },
    'focus.tasksSkipped': hub => { hub.focus.tasksDone = 4; hub.focus.tasksSkipped = 3; },
    'library[1].tasksDone': hub => { hub.library[1].tasksDone = 3; },
    'library[2].tasksSkipped': hub => { hub.library[2].tasksDone = 2; hub.library[2].tasksSkipped = 2; },
    'skills[0].tried': hub => { hub.skills = [{skillId: 'a', passed: 1, tried: 6, window: 5}]; hub.skillsTotal = 1; },
    'skills[0].passed': hub => { hub.skills = [{skillId: 'a', passed: 3, tried: 2, window: 5}]; hub.skillsTotal = 1; },
    'skillsTotal': hub => { hub.skills = [{skillId: 'a', passed: 1, tried: 2, window: 5}]; hub.skillsTotal = 0; },
    'checksTotal': hub => { hub.checksTotal = 0; },
    'participation.weeks[0].sessions': hub => { hub.participation.weeks[0].sessions += 1; },
    'participation.weeks[1].activeDays': hub => { hub.participation.weeks[1].activeDays += 1; },
    'participation.weeks[7].days': hub => { hub.participation.weeks[7].days = [1, 2, 0, null, 1, null, null]; },
    'participation.weeks[2].start': hub => { hub.participation.weeks[2].start = hub.participation.weeks[1].start; },
    'participation.totals.sessions': hub => { hub.participation.totals.sessions += 1; },
    'participation.totals.activeDays': hub => { hub.participation.totals.activeDays += 1; },
  };
  for (const [name, change] of Object.entries(cases)) {
    const hub = clone(sampleHub());
    change(hub);
    const result = readHub(hub);
    assert.equal(result.ok, false, name);
    assert.ok(result.reason.includes(name.split('[')[0].split('.')[0]), `${name}: ${result.reason}`);
  }
});

test('answers at the edge of every relation are accepted', () => {
  // Every module done: no focus, done equals total.
  const finished = clone(sampleHub());
  finished.focus = null;
  finished.checkpoints = {done: 3, total: 3};
  finished.library = finished.library.map(entry => ({...entry, status: 'done'}));
  assert.equal(readHub(finished).ok, true);
  // All tasks done, all skills at the window, a week with every day filled.
  const full = clone(sampleHub());
  full.focus.tasksDone = 5;
  full.focus.tasksSkipped = 0;
  full.library[0].tasksDone = 5;
  full.library[0].tasksSkipped = 0;
  full.skills = [{skillId: 'a', passed: 5, tried: 5, window: 5}];
  full.skillsTotal = 1;
  full.checksTotal = full.checks.length;
  assert.equal(readHub(full).ok, true);
  // The last week can be complete when today is a Sunday.
  const sunday = clone(sampleHub());
  const week = sunday.participation.weeks[7];
  week.days = [1, 2, 0, 1, 0, 0, 3];
  week.sessions = 7;
  week.activeDays = 4;
  sunday.participation.totals = {sessions: sunday.participation.weeks.reduce((sum, item) => sum + item.sessions, 0), activeDays: sunday.participation.weeks.reduce((sum, item) => sum + item.activeDays, 0), days: 56};
  assert.equal(readHub(sunday).ok, true);
});

test('relations are rejected from both sides', () => {
  const cases = {
    'checkpoints.total (library is shorter)': hub => { hub.checkpoints.total = 4; },
    'library.status (two in focus)': hub => { hub.library[1].status = 'focus'; hub.checkpoints.done = 0; },
    'library.focus (another module)': hub => { hub.focus.moduleId = 'other'; },
    'focus.tasksDone (all done and one more)': hub => { hub.focus.tasksDone = 6; hub.focus.tasksSkipped = 0; hub.library[0].tasksDone = 6; hub.library[0].tasksSkipped = 0; },
    'skills[0].tried (six results)': hub => { hub.skills = [{skillId: 'a', passed: 6, tried: 6, window: 5}]; hub.skillsTotal = 1; },
    'participation.weeks[7].days (Monday not counted)': hub => { hub.participation.weeks[7].days = [null, null, null, null, null, null, null]; hub.participation.weeks[7].sessions = 0; hub.participation.weeks[7].activeDays = 0; hub.participation.totals = {sessions: 21, activeDays: 14, days: 49}; },
    'participation.weeks[3].days (an earlier week has a day to come)': hub => { hub.participation.weeks[3].days[6] = null; hub.participation.totals.days -= 1; },
    'participation.weeks[0].sessions (fewer than the days)': hub => { hub.participation.weeks[0].sessions -= 1; hub.participation.totals.sessions -= 1; },
    'participation.weeks[4].activeDays (fewer than the days)': hub => { hub.participation.weeks[4].activeDays -= 1; },
    'participation.totals.sessions (fewer)': hub => { hub.participation.totals.sessions -= 1; },
    'participation.totals.activeDays (fewer)': hub => { hub.participation.totals.activeDays -= 1; },
    'participation.totals.days (more)': hub => { hub.participation.totals.days += 1; },
    'participation.totals.days (fewer)': hub => { hub.participation.totals.days -= 1; },
  };
  for (const [name, change] of Object.entries(cases)) {
    const hub = clone(sampleHub());
    change(hub);
    assert.equal(readHub(hub).ok, false, name);
  }
});

test('optional states are accepted: no focus, a fault, a skill, an open check', () => {
  const hub = sampleHub({
    focus: null,
    skills: [{skillId: 'loops', passed: 3, tried: 4, window: 5}], skillsTotal: 1,
    checks: [{moduleId: 'a', checkId: 'b', title: 't', retriesBeforeFirstPass: 0, open: true, hintLevelReached: null}],
  });
  hub.library[0].status = 'next';
  hub.log = {...hub.log, ok: false, fault: 'log_corrupt'};
  assert.equal(readHub(hub).ok, true);
  const noTask = sampleHub();
  noTask.focus.nextTask = null;
  assert.equal(readHub(noTask).ok, true);
});

test('the reason never repeats text from the answer', () => {
  const hub = sampleHub();
  hub.focus.title = 5;
  hub.library[0].title = '\u{202E}EVIL';
  hub.library[0].status = '\u{202E}EVIL';
  const result = readHub(hub);
  assert.equal(result.ok, false);
  assert.doesNotMatch(result.reason, /EVIL/);
});

test('heat level: none, one, many, and future for a day after today', () => {
  assert.equal(heatLevel(null), 'future');
  assert.equal(heatLevel(0), 'none');
  assert.equal(heatLevel(1), 'one');
  assert.equal(heatLevel(2), 'many');
  assert.equal(heatLevel(40), 'many');
});

test('day dates run from the Monday of the week, across a month and a year', () => {
  assert.equal(dayDate('2026-11-02', 0), '2026-11-02');
  assert.equal(dayDate('2026-11-02', 6), '2026-11-08');
  assert.equal(dayDate('2026-10-26', 6), '2026-11-01');
  assert.equal(dayDate('2026-12-28', 6), '2027-01-03');
  assert.equal(dayDate('2028-02-28', 2), '2028-03-01');
});

test('heat rows give each cell a count, a level, a date and a plain sentence', () => {
  const rows = heatRows(sampleHub().participation);
  assert.equal(rows.length, 8);
  const last = rows[7];
  assert.equal(last.start, '2026-11-02');
  assert.deepEqual(last.cells.map(cell => cell.count), [1, 2, 0, null, null, null, null]);
  assert.deepEqual(last.cells.map(cell => cell.level), ['one', 'many', 'none', 'future', 'future', 'future', 'future']);
  assert.equal(last.cells[0].date, '2026-11-02');
  assert.equal(last.cells[0].text, 'Mon 2026-11-02: 1 session');
  assert.equal(last.cells[1].text, 'Tue 2026-11-03: 2 sessions');
  assert.equal(last.cells[2].text, 'Wed 2026-11-04: 0 sessions');
  assert.equal(last.cells[3].text, 'Thu 2026-11-05: not yet');
  assert.equal(last.sessions, 3);
  assert.equal(last.activeDays, 2);
});

test('plural uses the singular for one and the plural for everything else', () => {
  assert.equal(plural(1, 'session'), '1 session');
  assert.equal(plural(0, 'session'), '0 sessions');
  assert.equal(plural(2, 'day'), '2 days');
  assert.equal(plural(1, 'check', 'checks'), '1 check');
  assert.equal(plural(3, 'entry', 'entries'), '3 entries');
});

test('percent rounds, clamps, and gives 0 when the whole is 0', () => {
  assert.equal(percent(1, 3), 33);
  assert.equal(percent(2, 3), 67);
  assert.equal(percent(3, 3), 100);
  assert.equal(percent(0, 5), 0);
  assert.equal(percent(5, 0), 0);
  assert.equal(percent(7, 5), 100);
  assert.equal(percent(-1, 5), 0);
  assert.equal(percent(1, 200), 1);
  assert.equal(percent(1, 201), 0);
});

test('meter segments: one segment per task, done first, then skipped, then open', () => {
  assert.deepEqual(meterSegments({done: 2, skipped: 1, total: 5}), ['done', 'done', 'skipped', 'open', 'open']);
  assert.deepEqual(meterSegments({done: 0, skipped: 0, total: 3}), ['open', 'open', 'open']);
  assert.deepEqual(meterSegments({done: 3, skipped: 0, total: 3}), ['done', 'done', 'done']);
  assert.deepEqual(meterSegments({done: 0, skipped: 0, total: 0}), []);
});

test('meter segments never claim more than the data says', () => {
  assert.deepEqual(meterSegments({done: 9, skipped: 9, total: 3}), ['done', 'done', 'done']);
  assert.deepEqual(meterSegments({done: -1, skipped: 0, total: 2}), ['open', 'open']);
});

test('a long module is scaled to the cap, and a part is never shown as all or as nothing', () => {
  const some = meterSegments({done: 1, skipped: 0, total: 100}, 12);
  assert.equal(some.length, 12);
  assert.equal(some.filter(value => value === 'done').length, 1);
  const almost = meterSegments({done: 99, skipped: 0, total: 100}, 12);
  assert.equal(almost.length, 12);
  assert.equal(almost.filter(value => value === 'open').length, 1);
  const whole = meterSegments({done: 100, skipped: 0, total: 100}, 12);
  assert.ok(whole.every(value => value === 'done'));
  const none = meterSegments({done: 0, skipped: 0, total: 100}, 12);
  assert.ok(none.every(value => value === 'open'));
});

test('a scaled meter gives every non-zero part at least one cell, for every total from 13 to 130', () => {
  let combos = 0;
  for (let total = 13; total <= 130; total += 1) {
    for (let done = 0; done <= total; done += 1) {
      for (let skipped = 0; done + skipped <= total; skipped += 1) {
        const open = total - done - skipped;
        const cells = meterSegments({done, skipped, total}, 12);
        const count = state => cells.filter(value => value === state).length;
        const label = `${done}/${skipped}/${open} of ${total}`;
        assert.equal(cells.length, 12, label);
        if (done > 0) assert.ok(count('done') >= 1, 'done is shown: ' + label);
        if (skipped > 0) assert.ok(count('skipped') >= 1, 'skipped is shown: ' + label);
        if (open > 0) assert.ok(count('open') >= 1, 'open is shown: ' + label);
        if (done === 0) assert.equal(count('done'), 0, label);
        if (skipped === 0) assert.equal(count('skipped'), 0, label);
        if (open === 0) assert.equal(count('open'), 0, label);
        assert.deepEqual(cells, [...cells].sort((a, b) => ['done', 'skipped', 'open'].indexOf(a) - ['done', 'skipped', 'open'].indexOf(b)), 'order: ' + label);
        if (skipped > 0 && open > 0) combos += 1;
      }
    }
  }
  assert.ok(combos > 4000, String(combos));
});

test('the part that gives up a cell is the largest part', () => {
  // 1 done, 1 skipped, 98 open of 100: the open part keeps the rest.
  assert.deepEqual(meterSegments({done: 1, skipped: 1, total: 100}, 12), ['done', 'skipped', ...Array(10).fill('open')]);
});

test('checkpoint cells: done, then the next one, then the rest', () => {
  assert.deepEqual(checkpointCells(2, 5), ['done', 'done', 'next', 'ahead', 'ahead']);
  assert.deepEqual(checkpointCells(0, 3), ['next', 'ahead', 'ahead']);
  assert.deepEqual(checkpointCells(3, 3), ['done', 'done', 'done']);
  assert.deepEqual(checkpointCells(0, 0), []);
  assert.deepEqual(checkpointCells(5, 3), ['done', 'done', 'done']);
});

test('checkpoint cells are capped, and scaling keeps a part as a part', () => {
  const cells = checkpointCells(1, 500, 60);
  assert.equal(cells.length, 60);
  assert.equal(cells.filter(value => value === 'done').length, 1);
  const nearly = checkpointCells(499, 500, 60);
  assert.equal(nearly.filter(value => value === 'done').length, 59);
  assert.equal(nearly[59], 'next');
});

test('a scaled checkpoint grid shows done, next and ahead whenever each exists, for 61 to 400 checkpoints', () => {
  for (let total = 61; total <= 400; total += 1) {
    for (let done = 0; done <= total; done += 1) {
      const cells = checkpointCells(done, total, 60);
      const count = state => cells.filter(value => value === state).length;
      const ahead = Math.max(total - done - 1, 0);
      const label = `${done} of ${total}`;
      assert.equal(cells.length, 60, label);
      assert.equal(count('next'), done < total ? 1 : 0, label);
      if (done > 0) assert.ok(count('done') >= 1, 'done: ' + label);
      if (ahead > 0) assert.ok(count('ahead') >= 1, 'ahead: ' + label);
      if (done === 0) assert.equal(count('done'), 0, label);
      if (ahead === 0) assert.equal(count('ahead'), 0, label);
    }
  }
});

test('skill cells show the passes, then the misses, then the unused places of the window of 5', () => {
  assert.deepEqual(skillCells({skillId: 'a', passed: 3, tried: 4, window: 5}), ['pass', 'pass', 'pass', 'miss', 'none']);
  assert.deepEqual(skillCells({skillId: 'a', passed: 0, tried: 0, window: 5}), ['none', 'none', 'none', 'none', 'none']);
  assert.deepEqual(skillCells({skillId: 'a', passed: 5, tried: 5, window: 5}), ['pass', 'pass', 'pass', 'pass', 'pass']);
});

test('skill percent is passed over tried, and absent when nothing was tried', () => {
  assert.equal(skillPercent({skillId: 'a', passed: 3, tried: 4, window: 5}), 75);
  assert.equal(skillPercent({skillId: 'a', passed: 1, tried: 3, window: 5}), 33);
  assert.equal(skillPercent({skillId: 'a', passed: 0, tried: 0, window: 5}), null);
});

test('hint info numbers the ladder and uses plain words, with none for no hint', () => {
  assert.deepEqual(HINT_LADDER, ['nudge', 'question', 'partial-example', 'full-answer']);
  assert.deepEqual(hintInfo(null), {step: 0, of: 4, label: 'none'});
  assert.deepEqual(hintInfo('nudge'), {step: 1, of: 4, label: 'nudge'});
  assert.deepEqual(hintInfo('question'), {step: 2, of: 4, label: 'question'});
  assert.deepEqual(hintInfo('partial-example'), {step: 3, of: 4, label: 'partial example'});
  assert.deepEqual(hintInfo('full-answer'), {step: 4, of: 4, label: 'full answer'});
});

test('library labels are plain words', () => {
  assert.equal(libraryLabel('done'), 'Done');
  assert.equal(libraryLabel('focus'), 'In focus');
  assert.equal(libraryLabel('next'), 'Next');
});

test('a clean answer has no notice', () => {
  assert.deepEqual(logNotices(sampleHub()), []);
});

test('each finding produces its own plain notice with the count', () => {
  const ids = changes => logNotices(sampleHub(changes)).map(notice => notice.id);
  const withLog = changes => ({log: {...sampleHub().log, ...changes}});
  assert.deepEqual(ids(withLog({ok: false, fault: 'log_corrupt', events: 0})), ['fault']);
  assert.deepEqual(ids(withLog({ok: false, damagedLines: 3})), ['damaged']);
  assert.deepEqual(ids(withLog({ok: false, truncatedTail: true})), ['truncated']);
  assert.deepEqual(ids(withLog({skippedEvents: 2})), ['skipped']);
  assert.deepEqual(ids(withLog({futureEvents: 4})), ['future']);
  assert.deepEqual(ids(withLog({unmatchedEvents: 1})), ['unmatched']);
  assert.deepEqual(ids({checksTotal: 31}), ['checks-cut']);
  assert.deepEqual(ids({skillsTotal: 101, skills: Array.from({length: 100}, (_, n) => ({skillId: 's' + n, passed: 1, tried: 1, window: 5}))}), ['skills-cut']);
  assert.deepEqual(ids(withLog({ok: false, damagedLines: 2, truncatedTail: true, skippedEvents: 1})), ['damaged', 'truncated', 'skipped']);
  assert.equal(logNotices(sampleHub(withLog({damagedLines: 3, ok: false})))[0].text.includes('3'), true);
  assert.match(logNotices(sampleHub(withLog({futureEvents: 4})))[0].text, /4 events/);
  assert.match(logNotices(sampleHub(withLog({futureEvents: 1})))[0].text, /1 event /);
  assert.match(logNotices(sampleHub({checksTotal: 31}))[0].text, /1 of 31/);
});

test('notices use plain words and name no file path', () => {
  const log = {exists: true, ok: false, fault: 'unsafe_path', events: 5, damagedLines: 2, truncatedTail: true, skippedEvents: 1, futureEvents: 1, unmatchedEvents: 1};
  for (const notice of [...logNotices(sampleHub({log, checksTotal: 40, skillsTotal: 200})), ...logNotices(sampleHub({log: {...log, fault: null, futureEvents: 3, skippedEvents: 3, unmatchedEvents: 3}}))]) {
    const words = notice.title + ' ' + notice.text;
    assert.doesNotMatch(words, /nowhere|docs\/|\.md|hub_data|\.py|\.js/, notice.id);
  }
  assert.match(logNotices(sampleHub({log: {...sampleHub().log, futureEvents: 2}}))[0].text, /leaves them out of every number/);
});

test('a damaged log is never reported as a clean log, and a fault says it cannot be read', () => {
  const fault = logNotices(sampleHub({log: {...sampleHub().log, ok: false, fault: 'unsafe_path', events: 0}}));
  assert.equal(fault.length, 1);
  assert.match(fault[0].title, /cannot read/i);
  assert.equal(fault[0].tone, 'warning');
});

test('notice text never uses a word the owner banned for docs and stays calm', () => {
  const log = {exists: true, ok: false, fault: null, events: 5, damagedLines: 2, truncatedTail: true, skippedEvents: 1, futureEvents: 1, unmatchedEvents: 1};
  const all = [
    ...logNotices(sampleHub({log, checksTotal: 40, skillsTotal: 200})),
    ...logNotices(sampleHub({log: {...log, fault: 'log_unreadable'}})),
  ];
  for (const notice of all) assert.doesNotMatch(notice.title + ' ' + notice.text, /\b(may|might|should|could|would)\b/i, notice.id);
});

test('an empty log: no log yet, or a clean log with no events; not a damaged or unreadable one', () => {
  assert.equal(isEmptyLog(sampleHub({log: {...sampleHub().log, exists: false, events: 0}})), true);
  assert.equal(isEmptyLog(sampleHub({log: {...sampleHub().log, events: 0}})), true);
  assert.equal(isEmptyLog(sampleHub()), false);
  assert.equal(isEmptyLog(sampleHub({log: {...sampleHub().log, ok: false, fault: 'log_unreadable', events: 0}})), false);
  assert.equal(isEmptyLog(sampleHub({log: {...sampleHub().log, ok: false, damagedLines: 2, events: 0}})), false);
});

test('events are unavailable whenever the log is not ok: a fault or a damaged log', () => {
  assert.equal(eventsUnavailable(sampleHub()), false);
  assert.equal(eventsUnavailable(sampleHub({log: {...sampleHub().log, ok: false, fault: 'log_corrupt'}})), true);
  assert.equal(eventsUnavailable(sampleHub({log: {...sampleHub().log, ok: false, damagedLines: 1}})), true);
  assert.equal(eventsUnavailable(sampleHub({log: {...sampleHub().log, exists: false, events: 0}})), false);
  assert.equal(UNAVAILABLE, 'Not available while the event log cannot be read.');
});

test('the library line shows done tasks, and skipped tasks when there are some', () => {
  const entry = {moduleId: 'a', version: '1', title: 't', status: 'done', tasksTotal: 2, tasksDone: 1, tasksSkipped: 1};
  assert.equal(libraryLine(entry), 'Done \u00b7 1 of 2 tasks \u00b7 1 skipped');
  assert.equal(libraryLine({...entry, status: 'focus', tasksSkipped: 0}), 'In focus \u00b7 1 of 2 tasks');
  assert.equal(libraryLine({...entry, status: 'next', tasksTotal: 1, tasksDone: 0, tasksSkipped: 0}), 'Next \u00b7 0 of 1 task');
  assert.equal(libraryLine({...entry, tasksSkipped: 3}), 'Done \u00b7 1 of 2 tasks \u00b7 3 skipped');
});

test('the updated label shows the local time, falls back to a short date, and never shows the raw text', () => {
  assert.equal(updatedLabel('2026-11-04T14:05:09Z', {timeZone: 'UTC', locale: 'en-GB', now: new Date('2026-11-04T15:00:00Z')}), 'Updated 14:05');
  assert.equal(updatedLabel('2026-11-04T14:05:09Z', {timeZone: 'Asia/Kolkata', locale: 'en-GB', now: new Date('2026-11-04T15:00:00Z')}), 'Updated 19:35');
  assert.equal(updatedLabel('2026-11-04T23:30:00Z', {timeZone: 'UTC', locale: 'en-GB', now: new Date('2026-11-06T00:00:00Z')}), 'Updated 4 Nov, 23:30');
  assert.equal(updatedLabel('2026-11-04T14:05:09Z', {timeZone: 'No/Such_Zone', locale: 'en-GB'}), 'Updated 2026-11-04');
  assert.equal(updatedLabel('not a date'), '');
  assert.equal(updatedLabel(''), '');
});
