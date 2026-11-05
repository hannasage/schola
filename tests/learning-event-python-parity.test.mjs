// The Python validator (learning_event.py) and the Node validator (contracts/validate-events.mjs)
// must agree. This test runs the Python module in a child process, on local files only,
// and compares the verdict, the reason, and the error pointers with the Node result.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm, truncate, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { MAX_BYTES, MAX_LINE_BYTES, MAX_LINES, validateEventText, validateLogFile, validateLogText } from '../contracts/validate-events.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const fixtureRoot = path.join(root, 'tests', 'fixtures', 'contracts');
const manifest = JSON.parse(await readFile(path.join(fixtureRoot, 'learning-event-conformance.json'), 'utf8'));

// The child process reads a JSON list of jobs on stdin and writes a JSON list of results.
// A job is { op: 'event' | 'log' | 'file', text | file }. The -I flag keeps the working folder
// and the environment out of the import path. No bytecode is written next to the sources.
const PYTHON_RUNNER = [
  'import sys, json, base64',
  'sys.path.insert(0, sys.argv[1])',
  'import learning_event as le',
  'jobs = json.load(sys.stdin)',
  'out = []',
  'for job in jobs:',
  '    if job["op"] == "file":',
  '        out.append(le.validate_log_file(job["file"]))',
  '    elif job["op"] == "bytes":',
  '        out.append(le.validate_log_text(base64.b64decode(job["b64"])))',
  '    elif job["op"] == "log":',
  '        out.append(le.validate_log_text(job["text"]))',
  '    else:',
  '        out.append(le.validate_event_text(job["text"]))',
  'print(json.dumps(out))',
].join('\n');

function runPython(args, input) {
  const child = spawnSync('python3', ['-I', '-c', ...args], {
    input,
    encoding: 'utf8',
    maxBuffer: 1 << 28,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  });
  assert.equal(child.status, 0, child.stderr);
  return child.stdout;
}

const runJobs = (jobs) => JSON.parse(runPython([PYTHON_RUNNER, root], JSON.stringify(jobs)));
const pointerSet = (result) => (result.ok ? [] : [...new Set((result.errors ?? []).map((error) => error.pointer))].sort());

function assertSameShape(python, node, label) {
  assert.equal(python.ok, node.ok, `${label}: ok`);
  assert.equal(python.reason, node.reason, `${label}: reason`);
  assert.deepEqual(pointerSet(python), pointerSet(node), `${label}: pointers`);
  if (!node.ok) assert.equal(python.errors.length, node.errors.length, `${label}: error count`);
}

function assertSameEvent(python, node, label) {
  assertSameShape(python, node, label);
  if (node.ok) assert.deepEqual(Object.keys(python), ['ok']);
  else {
    assert.deepEqual(Object.keys(python).sort(), ['errors', 'ok', 'reason']);
    for (const error of python.errors) assert.deepEqual(Object.keys(error).sort(), ['message', 'pointer']);
  }
}

function assertSameLog(python, node, label) {
  assert.deepEqual(Object.keys(python).sort(), Object.keys(node).sort(), `${label}: keys`);
  assert.equal(python.ok, node.ok, `${label}: ok`);
  assert.equal(python.truncatedTail, node.truncatedTail, `${label}: truncatedTail`);
  assert.equal(python.reason, node.reason, `${label}: whole-log reason`);
  assert.equal(python.lines.length, node.lines.length, `${label}: line count`);
  node.lines.forEach((line, index) => {
    assert.equal(python.lines[index].line, line.line, `${label}: line number`);
    assertSameShape(python.lines[index], line, `${label}: line ${line.line}`);
  });
  if (node.errors) assert.deepEqual(pointerSet(python), pointerSet(node), `${label}: pointers`);
}

const expectedFor = (entry) => (entry.expect === 'valid' ? true : entry.expect.slice('invalid:'.length));
const textOf = (entry) => readFile(path.join(fixtureRoot, entry.file), 'utf8');
const eventEntries = manifest.filter((entry) => entry.kind === 'learning-event');
const logEntries = manifest.filter((entry) => entry.kind === 'learning-event-log');

test('the Python validator agrees with the Node validator on every event fixture', async () => {
  const texts = await Promise.all(eventEntries.map(textOf));
  const results = runJobs(texts.map((text) => ({ op: 'event', text })));
  eventEntries.forEach((entry, index) => {
    assertSameEvent(results[index], validateEventText(texts[index]), entry.name);
    // Both must also match the manifest, so two wrong validators cannot agree by accident.
    assert.equal(results[index].ok ? true : results[index].reason, expectedFor(entry), entry.name);
  });
});

test('the Python validator agrees with the Node validator on every log fixture, line by line', async () => {
  const texts = await Promise.all(logEntries.map(textOf));
  const results = runJobs(texts.map((text) => ({ op: 'log', text })));
  logEntries.forEach((entry, index) => {
    assertSameLog(results[index], validateLogText(texts[index]), entry.name);
    assert.equal(results[index].ok, entry.expect === 'valid', entry.name);
    assert.equal(results[index].truncatedTail, entry.truncatedTail, entry.name);
  });
});

test('the two validators agree when they read log files', async () => {
  const results = runJobs(logEntries.map((entry) => ({ op: 'file', file: path.join(fixtureRoot, entry.file) })));
  for (const [index, entry] of logEntries.entries()) {
    assertSameLog(results[index], await validateLogFile(path.join(fixtureRoot, entry.file)), entry.name);
  }
});

test('the two validators agree on unreadable, oversized, and non-UTF-8 files', async () => {
  const folder = await mkdtemp(path.join(tmpdir(), 'schola-event-parity-'));
  try {
    const big = path.join(folder, 'big.jsonl');
    await writeFile(big, '');
    await truncate(big, MAX_BYTES + 1);
    const exact = path.join(folder, 'exact.jsonl');
    await writeFile(exact, '');
    await truncate(exact, MAX_BYTES);
    const broken = path.join(folder, 'broken.jsonl');
    await writeFile(broken, Buffer.from([0x7b, 0xff, 0x0a]));
    const bom = path.join(folder, 'bom.jsonl');
    await writeFile(bom, Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), await readFile(path.join(fixtureRoot, 'learning-events/valid/log-multi-line.jsonl'))]));
    const cases = [
      ['missing', path.join(folder, 'missing.jsonl')],
      ['directory', folder],
      ['too large', big],
      ['exactly the cap', exact],
      ['bad byte', broken],
      ['byte order mark', bom],
    ];
    const results = runJobs(cases.map(([, file]) => ({ op: 'file', file })));
    for (const [index, [label, file]] of cases.entries()) {
      const node = await validateLogFile(file);
      assertSameLog(results[index], node, label);
      if (node.reason === 'unreadable') assert.deepEqual(results[index].errors, node.errors, label);
    }
    assert.deepEqual(results.map((result) => result.reason), ['unreadable', 'unreadable', 'too_large', undefined, undefined, undefined]);
    assert.equal(results[4].lines[0].reason, 'invalid', 'a bad byte damages one line only');
    assert.equal(results[5].lines[0].ok, false, 'a byte order mark damages the first line');
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});

const fixtureDocument = async (file) => JSON.parse(await readFile(path.join(fixtureRoot, 'learning-events', file), 'utf8'));

test('the two validators agree on hand-made event edge cases', async () => {
  const attempt = await fixtureDocument('valid/attempt-submitted.json');
  const check = await fixtureDocument('valid/check-result-passed.json');
  const hint = await fixtureDocument('valid/hint-requested-full-answer-accepted-offer.json');
  const offer = await fixtureDocument('valid/agent-offer-pending.json');
  const opened = await fixtureDocument('valid/task-opened.json');
  const withField = (base, change) => {
    const copy = structuredClone(base);
    change(copy);
    return copy;
  };
  const cases = [
    ['a few errors', withField(opened, (d) => { d.skills = Array.from({ length: 7 }, () => 'Bad'); })],
    ['major 2', withField(opened, (d) => { d.schemaVersion = '2.0.0'; })],
    ['major 0', withField(opened, (d) => { d.schemaVersion = '0.9'; })],
    ['leading zero major', withField(opened, (d) => { d.schemaVersion = '01.0'; })],
    ['huge major', withField(opened, (d) => { d.schemaVersion = `${'9'.repeat(400)}.0`; })],
    ['three part version', withField(opened, (d) => { d.schemaVersion = '1.0.0'; })],
    ['version not a string', withField(opened, (d) => { d.schemaVersion = 1; })],
    ['array document', []],
    ['null document', null],
    ['id with a trailing newline', withField(opened, (d) => { d.eventId = `${d.eventId}\n`; })],
    ['uppercase id', withField(opened, (d) => { d.eventId = d.eventId.toUpperCase(); })],
    ['non-ASCII digits in a timestamp', withField(opened, (d) => { d.time = '٢٠٢٤-01-01T00:00:00Z'; })],
    ['leap day', withField(opened, (d) => { d.time = '2024-02-29T10:00:00Z'; })],
    ['not a leap day', withField(opened, (d) => { d.time = '2023-02-29T10:00:00Z'; })],
    ['century not a leap year', withField(opened, (d) => { d.time = '2100-02-29T10:00:00Z'; })],
    ['31 days in a 30 day month', withField(opened, (d) => { d.time = '2024-04-31T10:00:00Z'; })],
    ['second 60', withField(opened, (d) => { d.time = '2024-04-30T10:00:60Z'; })],
    ['evidence of 200 emoji', withField(check, (d) => { d.data.evidence = '\u{1F600}'.repeat(200); })],
    ['evidence of 201 emoji', withField(check, (d) => { d.data.evidence = '\u{1F600}'.repeat(201); })],
    ['evidence with a right-to-left override', withField(check, (d) => { d.data.evidence = 'a\u202Eb'; })],
    ['evidence with an isolate', withField(check, (d) => { d.data.evidence = 'a\u2066b'; })],
    ['evidence with a line separator', withField(check, (d) => { d.data.evidence = 'a\u2028b'; })],
    ['evidence with a next-line character', withField(check, (d) => { d.data.evidence = 'a\u0085b'; })],
    ['evidence with a tab', withField(check, (d) => { d.data.evidence = 'a\tb'; })],
    ['evidence with a delete character', withField(check, (d) => { d.data.evidence = 'a\u007Fb'; })],
    ['evidence with a trailing newline', withField(check, (d) => { d.data.evidence = 'ab\n'; })],
    ['evidence that is not a string', withField(check, (d) => { d.data.evidence = 5; })],
    ['bytes as a float with no fraction', withField(attempt, (d) => { d.data.artifactBytes = 5.0; })],
    ['bytes as a fraction', withField(attempt, (d) => { d.data.artifactBytes = 5.5; })],
    ['bytes as a boolean', withField(attempt, (d) => { d.data.artifactBytes = true; })],
    ['huge bytes', withField(attempt, (d) => { d.data.artifactBytes = 1e30; })],
    ['digest with a trailing newline', withField(attempt, (d) => { d.data.artifactDigest = `${d.data.artifactDigest}\n`; })],
    ['null attempt on a full answer', withField(hint, (d) => { d.data.attemptId = null; })],
    ['missing attempt on a full answer', withField(hint, (d) => { delete d.data.attemptId; })],
    ['null attempt on a nudge', withField(hint, (d) => { d.data.level = 'nudge'; d.data.attemptId = null; })],
    ['number attempt on a nudge', withField(hint, (d) => { d.data.level = 'nudge'; d.data.attemptId = 5; })],
    ['unknown level', withField(hint, (d) => { d.data.level = 'answer'; })],
    ['offer triggered by itself', withField(offer, (d) => { d.data.triggerEventId = d.eventId; })],
    ['unknown type', withField(opened, (d) => { d.type = 'confidence_rated'; })],
    ['type not a string', withField(opened, (d) => { d.type = 5; })],
    ['type of Object.prototype name', withField(opened, (d) => { d.type = 'constructor'; })],
    ['data null', withField(opened, (d) => { d.data = null; })],
    ['data array', withField(opened, (d) => { d.data = []; })],
    ['data with __proto__ key', JSON.parse('{"schemaVersion":"1.0","eventId":"a1b2c3d4-0000-4000-8000-000000000001","time":"2026-11-02T09:00:00Z","sessionId":"7c9e6679-7425-40de-944b-e07fc1f90ae7","moduleId":"m","moduleVersion":"1.0.0","taskId":"t","source":"app","skills":[],"type":"task_opened","data":{"__proto__":1}}')],
    ['skills not an array', withField(opened, (d) => { d.skills = 'a'; })],
    ['skills with a non-string', withField(opened, (d) => { d.skills = [5]; })],
    ['skill id with a trailing newline', withField(opened, (d) => { d.skills = ['a\n']; })],
    ['skill id of 40 characters', withField(opened, (d) => { d.skills = ['a'.repeat(40)]; })],
    ['skill id of 41 characters', withField(opened, (d) => { d.skills = ['a'.repeat(41)]; })],
    ['task id of 61 characters', withField(opened, (d) => { d.taskId = 'a'.repeat(61); })],
    ['task id a number', withField(opened, (d) => { d.taskId = 5; })],
    ['source unknown', withField(opened, (d) => { d.source = 'teacher'; })],
    ['module version with a leading zero', withField(opened, (d) => { d.moduleVersion = '01.0.0'; })],
  ];
  const texts = cases.map(([, value]) => JSON.stringify(value));
  const results = runJobs(texts.map((text) => ({ op: 'event', text })));
  cases.forEach(([label], index) => assertSameEvent(results[index], validateEventText(texts[index]), label));
});

// A long error list is cut at 100. Which errors come first depends on the order each engine checks
// keywords, so this test compares the count, the reason, and the marker, not the pointers.
test('the two validators cut a long error list the same way', async () => {
  const opened = await fixtureDocument('valid/task-opened.json');
  const text = JSON.stringify({ ...opened, skills: Array.from({ length: 150 }, (_, index) => `Bad${index}`) });
  const [python] = runJobs([{ op: 'event', text }]);
  const node = validateEventText(text);
  for (const result of [python, node]) {
    assert.equal(result.reason, 'invalid');
    assert.equal(result.errors.length, 101);
    assert.equal(result.errors.at(-1).pointer, '');
    assert.match(result.errors.at(-1).message, /^\d+ more errors omitted\.$/);
  }
  assert.equal(python.errors.at(-1).message, node.errors.at(-1).message);
});

test('the two validators agree on damaged, oversized, and odd event and log text', () => {
  const one = '{"schemaVersion":"1.0"}';
  const cases = [
    ['empty', 'event', ''],
    ['truncated', 'event', '{"schemaVersion": "1.0"'],
    ['trailing comma', 'event', '[1,]'],
    ['single quotes', 'event', "{'a': 1}"],
    ['NaN', 'event', '{"a": NaN}'],
    ['byte order mark', 'event', '﻿{}'],
    ['bare number', 'event', '5'],
    ['over the line cap', 'event', ' '.repeat(MAX_LINE_BYTES + 1)],
    ['at the line cap', 'event', ' '.repeat(MAX_LINE_BYTES)],
    ['two bytes per character, over the line cap', 'event', 'é'.repeat(MAX_LINE_BYTES / 2 + 1)],
    ['log: empty', 'log', ''],
    ['log: one empty line', 'log', '\n'],
    ['log: two empty lines', 'log', '\n\n'],
    ['log: whitespace tail', 'log', `${one}\n   `],
    ['log: carriage return endings', 'log', `${one}\r\n${one}\r\n`],
    ['log: no final newline', 'log', `${one}\n${one}`],
    ['log: bare number lines', 'log', '5\n"x"\nnull\n'],
    ['log: tail that is a valid non-object', 'log', `${one}\n5`],
    ['log: tail longer than a line', 'log', `${one}\n{${' '.repeat(MAX_LINE_BYTES + 1)}`],
    ['log: line separators inside a line', 'log', `${one}\u2028${one}\n`],
    ['log: NaN line', 'log', '{"a": NaN}\n'],
    ['log: over the cap', 'log', `{${' '.repeat(MAX_BYTES)}`],
    ['log: at the cap', 'log', ' '.repeat(MAX_BYTES)],
    ['log: two bytes per character, over the cap', 'log', 'é'.repeat(MAX_BYTES / 2 + 1)],
  ];
  const results = runJobs(cases.map(([, op, text]) => ({ op, text })));
  cases.forEach(([label, op, text], index) => {
    if (op === 'event') assertSameEvent(results[index], validateEventText(text), label);
    else assertSameLog(results[index], validateLogText(text), label);
  });
});

test('the two validators agree on byte input, cut multibyte characters, and line counts', async () => {
  const log = await readFile(path.join(fixtureRoot, 'learning-events/valid/log-multi-line.jsonl'));
  const first = log.subarray(0, log.indexOf(10) + 1);
  const cases = [
    ['cut two-byte character', Buffer.concat([first, Buffer.from('{"n":"\u00e9').subarray(0, -1)])],
    ['cut three-byte character', Buffer.concat([first, Buffer.from('{"n":"\u20ac').subarray(0, -1)])],
    ['cut three-byte character, two bytes gone', Buffer.concat([first, Buffer.from('{"n":"\u20ac').subarray(0, -2)])],
    ['cut four-byte character', Buffer.concat([first, Buffer.from('{"n":"\u{1F600}').subarray(0, -1)])],
    ['bad byte in the tail', Buffer.concat([first, Buffer.from([0x7b, 0xff, 0x7b])])],
    ['bad byte last in the tail', Buffer.concat([first, Buffer.from([0x7b, 0xff])])],
    ['bad continuation in the tail', Buffer.concat([first, Buffer.from([0x7b, 0xc3, 0x28])])],
    ['overlong encoding at the end', Buffer.concat([first, Buffer.from([0x7b, 0xc0])])],
    ['surrogate bytes at the end', Buffer.concat([first, Buffer.from([0x7b, 0xed, 0xa0])])],
    ['bad byte on a middle line', Buffer.concat([first, Buffer.from([0x7b, 0xff, 0x7d, 0x0a]), first])],
    ['complete multibyte character in the tail', Buffer.concat([first, Buffer.from('{"n":"\u00e9')])],
    ['byte order mark first', Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), log])],
    ['MAX_LINES blank lines', Buffer.alloc(MAX_LINES, 0x0a)],
    ['over MAX_LINES blank lines', Buffer.alloc(MAX_LINES + 1, 0x0a)],
    ['MAX_LINES lines and a tail', Buffer.concat([Buffer.alloc(MAX_LINES, 0x0a), Buffer.from('{')])],
  ];
  const results = runJobs(cases.map(([, bytes]) => ({ op: 'bytes', b64: bytes.toString('base64') })));
  cases.forEach(([label, bytes], index) => assertSameLog(results[index], validateLogText(new Uint8Array(bytes)), label));
  const textCases = [
    ['text: MAX_LINES blank lines', '\n'.repeat(MAX_LINES)],
    ['text: over MAX_LINES blank lines', '\n'.repeat(MAX_LINES + 1)],
  ];
  const textResults = runJobs(textCases.map(([, text]) => ({ op: 'log', text })));
  textCases.forEach(([label, text], index) => assertSameLog(textResults[index], validateLogText(text), label));
});

test('the two validators agree on a log of valid events with repeated ids and mixed damage', async () => {
  const log = await readFile(path.join(fixtureRoot, 'learning-events/valid/log-multi-line.jsonl'), 'utf8');
  const lines = log.split('\n').filter(Boolean);
  const text = [lines[0], lines[1], lines[0], 'oops', lines[2].replace('"1.0"', '"3.0"'), lines[3], lines[1]].join('\n') + '\n' + lines[4].slice(0, 90);
  const [python] = runJobs([{ op: 'log', text }]);
  assertSameLog(python, validateLogText(text), 'mixed log');
  assert.equal(python.truncatedTail, true);
});

// Both validators refuse an object that repeats a key, at any depth and however the key is written.
// JSON.parse alone keeps the last value, so the Node validator scans the text for repeats first.
test('the two validators agree on repeated event ids across valid, invalid, and newer-version lines', async () => {
  const one = await fixtureDocument('valid/task-opened.json');
  const other = await fixtureDocument('valid/task-finished-passed.json');
  const text = (...documents) => `${documents.map((item) => JSON.stringify(item)).join('\n')}\n`;
  const cases = [
    ['repeat on an invalid line', text(one, { ...one, extra: 1 })],
    ['invalid first line', text({ ...one, extra: 1 }, one)],
    ['newer version lines', text({ ...one, schemaVersion: '2.0' }, one, { ...one, schemaVersion: '2.0' })],
    ['ids that are not UUIDs', text({ ...one, eventId: 'x' }, { ...one, eventId: 'x' }, { ...other, eventId: 'y' })],
    ['id that is not a string', text({ ...one, eventId: 5 }, { ...one, eventId: 5 })],
    ['three repeats', text(one, one, one)],
  ];
  const results = runJobs(cases.map(([, value]) => ({ op: 'log', text: value })));
  cases.forEach(([label, value], index) => assertSameLog(results[index], validateLogText(value), label));
});

test('a repeated object key is refused by both validators in the same way', async () => {
  const opened = JSON.stringify(await fixtureDocument('valid/task-opened.json'));
  const texts = [
    opened.replace(/"source":"(\w+)"/, '"source":"$1","source":"student"'),
    opened.replace('"data":{}', '"data":{"a":1,"a":2}'),
    opened.replace(/"source":"(\w+)"/, '"source":"$1","\\u0073ource":"student"'),
    opened.replace('"skills":["brief-writing"]', '"skills":[{"a":1,"a":2}]'),
    opened.replace('"data":{}', '"data":{"__proto__":1,"__proto__":2}'),
    '{"a":1,"a":2}',
    '{"":1,"":2}',
    '{"\\ud800":1,"\\ud800":2}',
    '{"a":{"a":1},"b":{"a":2}}',
    '{"a":"x\\",\\"a\\":1"}',
  ];
  for (const text of texts.slice(0, 5)) assert.notEqual(text, opened);
  const results = runJobs(texts.map((text) => ({ op: 'event', text })));
  texts.forEach((text, index) => assertSameEvent(results[index], validateEventText(text), text.slice(0, 80)));
  for (const index of [0, 1, 2, 3, 4, 5, 6, 7]) {
    assert.equal(results[index].ok, false);
    assert.deepEqual(results[index].errors.map((error) => error.pointer), ['']);
    assert.equal(validateEventText(texts[index]).ok, false);
  }
});

// A small seeded mutation run. It keeps the two validators in step on documents the
// fixtures do not cover. The seed is fixed, so a failure can be repeated.
test('the two validators agree on seeded mutations of every event fixture', async () => {
  let seed = 20261102;
  const random = () => {
    seed = (seed * 1664525 + 1013904223) % 4294967296;
    return seed / 4294967296;
  };
  const pick = (list) => structuredClone(list[Math.floor(random() * list.length)]);
  const pool = [null, true, false, 0, 1, -1, 1.5, 2147483648, '', ' ', 'x', 'nudge', 'full-answer', 'passed', 'a-b', 'A',
    '1.0', '2.0', '2024-02-30T10:00:00Z', 'student', '__proto__', 'a\u202Eb', 'a\nb', '\u{1F600}'.repeat(201), [], {}, [1], ['a', 'a'], { a: 1 },
    'a1b2c3d4-0000-4000-8000-000000000001', 'A1B2C3D4-0000-4000-8000-000000000001', 'task_opened', 'review_result'];
  const paths = (value, base = []) => {
    const found = [base];
    if (Array.isArray(value)) value.forEach((item, index) => found.push(...paths(item, [...base, index])));
    else if (value && typeof value === 'object') for (const key of Object.keys(value)) found.push(...paths(value[key], [...base, key]));
    return found;
  };
  const at = (document, route) => route.reduce((node, key) => node[key], document);
  const mutate = (document) => {
    const copy = structuredClone(document);
    for (let step = 0; step < 1 + Math.floor(random() * 3); step += 1) {
      const routes = paths(copy).filter((route) => route.length > 0);
      if (routes.length === 0) break;
      const route = pick(routes);
      const parent = at(copy, route.slice(0, -1));
      const key = route.at(-1);
      const kind = Math.floor(random() * 4);
      if (kind === 0) {
        if (Array.isArray(parent)) parent.splice(key, 1);
        else delete parent[key];
      } else if (kind === 1) parent[key] = pick(pool);
      else if (kind === 2) {
        if (Array.isArray(parent)) parent.push(structuredClone(parent[key]));
        else parent[pick(['extra', 'code', 'level', 'attemptId'])] = pick(pool);
      } else if (typeof parent[key] === 'string') parent[key] = parent[key].slice(0, Math.floor(random() * parent[key].length)) + pick(['', '\n', ' ']);
      else parent[key] = pick(pool);
    }
    return copy;
  };
  const jobs = [];
  for (const entry of eventEntries) {
    if (entry.stage === 'parse' || entry.stage === 'repeat') continue;
    const document = JSON.parse(await textOf(entry));
    for (let count = 0; count < 12; count += 1) jobs.push({ op: 'event', text: JSON.stringify(mutate(document)) });
  }
  const results = runJobs(jobs);
  jobs.forEach((job, index) => assertSameEvent(results[index], validateEventText(job.text), `mutation ${index}: ${job.text.slice(0, 200)}`));
});

test('an unsupported schema keyword makes the Python validator raise', () => {
  const script = [
    'import sys',
    'sys.path.insert(0, sys.argv[1])',
    'import learning_event as le',
    'try:',
    '    le.SchemaRegistry([{"$id": "urn:test:a", "format": "email"}])',
    'except le.UnsupportedKeywordError as error:',
    '    print("raised", "format" in str(error))',
    'else:',
    '    print("passed")',
  ].join('\n');
  assert.equal(runPython([script, root], '').trim(), 'raised True');
});
