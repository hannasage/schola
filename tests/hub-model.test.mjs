import assert from 'node:assert/strict';
import test from 'node:test';
import {
  readHub, heatLevel, dayDate, heatRows, percent, meterSegments, checkpointCells, skillCells, skillPercent,
  hintInfo, libraryLabel, logNotices, isEmptyLog, plural, HINT_LADDER,
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

test('optional states are accepted: no focus, a fault, a skill, an open check', () => {
  const hub = sampleHub({
    focus: null,
    skills: [{skillId: 'loops', passed: 3, tried: 4, window: 5}], skillsTotal: 1,
    checks: [{moduleId: 'a', checkId: 'b', title: 't', retriesBeforeFirstPass: 0, open: true, hintLevelReached: null}],
  });
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
