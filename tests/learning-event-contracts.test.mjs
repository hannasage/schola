import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { chmod, copyFile, mkdtemp, readFile, readdir, rm, stat, truncate, utimes, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import Ajv2020 from 'ajv/dist/2020.js';
import { validateLessonContract } from '../contracts/validate.mjs';
import {
  EVENT_TYPES,
  MAX_BYTES,
  MAX_LINE_BYTES,
  MAX_LINES,
  validateEvent,
  validateEventText,
  validateLogFile,
  validateLogText,
} from '../contracts/validate-events.mjs';

const root = new URL('../', import.meta.url);
const fixtureRoot = new URL('tests/fixtures/contracts/', root);
const readText = (url) => readFile(url, 'utf8');
const readJSON = async (url) => JSON.parse(await readText(url));
const fixture = (relative) => new URL(relative, fixtureRoot);
const manifest = await readJSON(fixture('learning-event-conformance.json'));
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const deepFreeze = (value) => {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
};

const KINDS = ['learning-event', 'learning-event-log'];
const eventEntries = manifest.filter((entry) => entry.kind === 'learning-event');
const logEntries = manifest.filter((entry) => entry.kind === 'learning-event-log');

// Schema-only validation, to prove that each fixture fails at the stage the manifest names.
const ajv = new Ajv2020({ strict: true, allErrors: true });
for (const name of ['lesson-common.v1.schema.json', 'learning-event.v1.schema.json']) {
  ajv.addSchema(await readJSON(new URL(`contracts/${name}`, root)));
}
const schemaCheck = ajv.getSchema('urn:schola:contract:learning-event:v1');

const expectedFor = (entry) => (entry.expect === 'valid'
  ? { ok: true }
  : { ok: false, reason: entry.expect.slice('invalid:'.length) });

test('the manifest lists every fixture file under learning-events, and nothing else', async () => {
  const listed = new Set(manifest.map((entry) => entry.file));
  assert.equal(listed.size, manifest.length, 'a file is listed twice');
  assert.equal(new Set(manifest.map((entry) => entry.name)).size, manifest.length, 'a name is listed twice');
  const onDisk = new Set();
  for (const folder of ['valid', 'invalid']) {
    for (const name of await readdir(fixture(`learning-events/${folder}/`))) onDisk.add(`learning-events/${folder}/${name}`);
  }
  // Equality in both directions: no unlisted file on disk, and no listed file missing.
  assert.deepEqual([...onDisk].sort(), [...listed].sort());
  for (const entry of manifest) {
    assert.ok(KINDS.includes(entry.kind), entry.name);
    assert.match(entry.expect, /^(valid|invalid:(invalid|unsupported_version))$/, entry.name);
    assert.equal(entry.file.startsWith(entry.expect === 'valid' ? 'learning-events/valid/' : 'learning-events/invalid/'), true, entry.name);
    if (entry.kind === 'learning-event') {
      const stageReason = { none: 'valid', parse: 'invalid:invalid', repeat: 'invalid:invalid', schema: 'invalid:invalid', semantic: 'invalid:invalid', version: 'invalid:unsupported_version' };
      assert.equal(entry.expect, stageReason[entry.stage], `${entry.name}: stage and expectation disagree`);
      assert.match(entry.file, /\.json$/, entry.name);
    } else {
      assert.equal(entry.stage, entry.expect === 'valid' ? 'none' : 'lines', entry.name);
      assert.match(entry.file, /\.jsonl$/, entry.name);
      assert.equal(typeof entry.truncatedTail, 'boolean', entry.name);
      assert.equal(typeof entry.invalidLines, 'object', entry.name);
      assert.equal(Object.keys(entry.invalidLines).length === 0, entry.expect === 'valid', entry.name);
    }
  }
});

test('the manifest and the existing contract manifests do not overlap', async () => {
  const older = new Set((await readJSON(fixture('lesson-conformance.json'))).map((entry) => entry.file));
  for (const entry of manifest) assert.equal(older.has(entry.file), false, entry.name);
});

for (const entry of eventEntries) {
  test(`learning event conformance: ${entry.name}`, async () => {
    const text = await readText(fixture(entry.file));
    const result = validateEventText(text);
    assert.equal(result.ok, expectedFor(entry).ok, JSON.stringify(result));
    if (!result.ok) {
      assert.equal(result.reason, expectedFor(entry).reason, JSON.stringify(result));
      assert.ok(Array.isArray(result.errors) && result.errors.length > 0);
      for (const error of result.errors) {
        assert.equal(typeof error.pointer, 'string');
        assert.equal(typeof error.message, 'string');
        assert.deepEqual(Object.keys(error).sort(), ['message', 'pointer']);
      }
    } else {
      assert.deepEqual(Object.keys(result), ['ok']);
    }
    if (entry.stage === 'parse') {
      assert.throws(() => JSON.parse(text), SyntaxError, 'a parse case must not be valid JSON');
      return;
    }
    if (entry.stage === 'repeat') {
      assert.doesNotThrow(() => JSON.parse(text), 'a repeat case is valid JSON that repeats a key');
      assert.deepEqual(result.errors.map((error) => error.pointer), ['']);
      return;
    }
    const document = JSON.parse(text);
    if (entry.stage === 'none' || entry.stage === 'semantic') {
      assert.equal(schemaCheck(document), true, `a ${entry.stage} case must pass the schema`);
    }
    if (entry.stage === 'schema') assert.equal(schemaCheck(document), false, 'a schema case must fail the schema');
    if (entry.stage === 'version') {
      const marker = document.schemaVersion;
      assert.equal(typeof marker, 'string');
      assert.notEqual(Number(/^(\d+)\./.exec(marker)?.[1]), 1, 'a version case must name an unsupported major');
      assert.equal(result.reason, 'unsupported_version');
      assert.deepEqual(result.errors.map((error) => error.pointer), ['/schemaVersion']);
    }
    assert.deepEqual(validateEvent(document), result, 'validateEvent and validateEventText must agree');
  });
}

for (const entry of logEntries) {
  test(`learning event log conformance: ${entry.name}`, async () => {
    const text = await readText(fixture(entry.file));
    const result = validateLogText(text);
    assert.deepEqual(Object.keys(result).sort(), ['lines', 'ok', 'truncatedTail']);
    assert.equal(result.ok, entry.expect === 'valid', JSON.stringify(result));
    assert.equal(result.truncatedTail, entry.truncatedTail);
    const bad = Object.fromEntries(result.lines.filter((item) => !item.ok).map((item) => [String(item.line), item.reason]));
    assert.deepEqual(bad, entry.invalidLines);
    result.lines.forEach((item, index) => {
      assert.equal(item.line, index + 1, 'lines are numbered from 1 with no gap');
      if (item.ok) assert.deepEqual(Object.keys(item), ['line', 'ok']);
      else {
        assert.deepEqual(Object.keys(item).sort(), ['errors', 'line', 'ok', 'reason']);
        assert.ok(item.errors.length > 0);
        for (const error of item.errors) assert.deepEqual(Object.keys(error).sort(), ['message', 'pointer']);
      }
    });
    const expectedLineCount = text === '' ? 0 : text.split('\n').length - (text.endsWith('\n') ? 1 : 0);
    assert.equal(result.lines.length, expectedLineCount);
    // Reading the file gives the same result as reading its text.
    assert.deepEqual(await validateLogFile(fileURLToPath(new URL(entry.file, fixtureRoot))), result);
    // The bytes of the log give the same result as its text.
    assert.deepEqual(validateLogText(Buffer.from(text, 'utf8')), result);
  });
}

test('every event type has a valid fixture, and the type list is closed', async () => {
  assert.equal(EVENT_TYPES.length, 10);
  assert.equal(EVENT_TYPES.includes('confidence_rated'), false, 'confidence_rated is deliberately not in v1');
  const seen = new Set();
  for (const entry of eventEntries.filter((item) => item.expect === 'valid')) {
    seen.add((await readJSON(fixture(entry.file))).type);
  }
  assert.deepEqual([...seen].sort(), [...EVENT_TYPES].sort());
  const schema = await readJSON(new URL('contracts/learning-event.v1.schema.json', root));
  assert.deepEqual([...schema.properties.type.enum].sort(), [...EVENT_TYPES].sort());
});

test('the valid log fixture holds more than one event and every line is one event', async () => {
  const text = await readText(fixture('learning-events/valid/log-multi-line.jsonl'));
  const lines = text.split('\n').filter((item) => item !== '');
  assert.ok(lines.length >= 5);
  for (const item of lines) assert.equal(validateEventText(item).ok, true, item);
  assert.equal(text.endsWith('\n'), true);
});

test('a hint at the partial example or full answer level needs an attempt, as in the help record', async () => {
  const base = await readJSON(fixture('learning-events/valid/hint-requested-full-answer-accepted-offer.json'));
  const withHint = (level, attemptId) => ({ ...base, data: { ...base.data, level, attemptId } });
  for (const level of ['nudge', 'question']) {
    assert.equal(validateEvent(withHint(level, null)).ok, true, level);
    assert.equal(validateEvent(withHint(level, base.data.attemptId)).ok, true, level);
  }
  for (const level of ['partial-example', 'full-answer']) {
    const result = validateEvent(withHint(level, null));
    assert.equal(result.ok, false, level);
    assert.ok(result.errors.some((error) => error.pointer === '/data/attemptId'), JSON.stringify(result.errors));
    assert.equal(validateEvent(withHint(level, base.data.attemptId)).ok, true, level);
  }
});

test('an attempt id valid in a help record is valid in an event, and the reverse', async () => {
  const record = await readJSON(fixture('valid/help-record-partial-example-student.json'));
  const hint = await readJSON(fixture('learning-events/valid/hint-requested-partial-example.json'));
  const asRecord = (id) => validateLessonContract('help-record', { ...record, attemptId: id }).ok;
  const asEvent = (id) => validateEvent({ ...hint, data: { ...hint.data, attemptId: id } }).ok;
  const ids = [
    'a', 'A', '0', 'try_2-b', 'a'.repeat(64), 'a'.repeat(65), '', 'has space', 'a.b', 'a/b', 'a\n', '\u00e9', 'a1b2c3d4-0000-4000-8000-000000000001',
    'A1B2C3D4-0000-4000-8000-000000000001', '-', '_',
  ];
  for (const id of ids) assert.equal(asEvent(id), asRecord(id), JSON.stringify(id));
  // Ids that real fixtures use on each side are valid on the other side.
  const recordIds = [];
  for (const file of ['valid/help-record-full-answer-agent.json', 'valid/help-record-partial-example-student.json']) {
    recordIds.push((await readJSON(fixture(file))).attemptId);
  }
  const eventIds = [];
  for (const entry of eventEntries.filter((item) => item.expect === 'valid')) {
    const data = (await readJSON(fixture(entry.file))).data;
    if (typeof data.attemptId === 'string') eventIds.push(data.attemptId);
  }
  assert.ok(recordIds.length > 0 && eventIds.length > 5);
  for (const id of recordIds) assert.equal(asEvent(id), true, `help record id ${id} in an event`);
  for (const id of eventIds) assert.equal(asRecord(id), true, `event id ${id} in a help record`);
  assert.ok(eventIds.some((id) => !/^[0-9a-f]{8}-/.test(id)), 'some event fixtures use a non-UUID attempt id');
});

test('the app alone writes attempt, check, task finished, and module completed events', async () => {
  const appOnly = { 'attempt-submitted': 'attempt_submitted', 'check-result-passed': 'check_result', 'task-finished-passed': 'task_finished', 'module-completed': 'module_completed' };
  for (const [name] of Object.entries(appOnly)) {
    const base = await readJSON(fixture(`learning-events/valid/${name}.json`));
    assert.equal(base.source, 'app', name);
    for (const source of ['student', 'agent', 'teacher', null]) {
      const result = validateEvent({ ...base, source });
      assert.equal(result.ok, false, `${name} accepted source ${source}`);
      assert.ok(result.errors.some((found) => found.pointer === '/source'), name);
    }
  }
  // The other types take any of the three sources.
  for (const name of ['hint-requested-nudge-null-attempt', 'agent-offer-pending', 'reflection-saved', 'task-opened', 'mode-changed', 'review-result']) {
    const base = await readJSON(fixture(`learning-events/valid/${name}.json`));
    for (const source of ['student', 'app', 'agent']) assert.equal(validateEvent({ ...base, source }).ok, true, `${name} ${source}`);
  }
});

test('a task id is required as a string for five types, and null for module_completed', async () => {
  const mustHaveTask = ['attempt-submitted', 'check-result-passed', 'hint-requested-nudge-null-attempt', 'task-opened', 'task-finished-passed'];
  for (const name of mustHaveTask) {
    const base = await readJSON(fixture(`learning-events/valid/${name}.json`));
    assert.equal(validateEvent({ ...base, taskId: null }).ok, false, `${name} accepted a null task`);
    assert.equal(validateEvent({ ...base, taskId: 'other-task' }).ok, true, name);
  }
  const done = await readJSON(fixture('learning-events/valid/module-completed.json'));
  assert.equal(validateEvent({ ...done, taskId: 'a-task' }).ok, false);
});

test('data objects are closed for every event type, and a raw content field is refused', async () => {
  for (const entry of eventEntries.filter((item) => item.expect === 'valid' && item.name.indexOf('envelope-') !== 0)) {
    const document = await readJSON(fixture(entry.file));
    for (const field of ['code', 'text', 'output', 'stdout', 'stderr', 'body', 'content', 'prompt', 'path']) {
      const copy = structuredClone(document);
      copy.data[field] = 'x';
      const result = validateEvent(copy);
      assert.equal(result.ok, false, `${entry.name} accepted data.${field}`);
      assert.ok(result.errors.some((error) => error.pointer === '/data'), `${entry.name} data.${field}`);
      const top = structuredClone(document);
      top[field] = 'x';
      assert.equal(validateEvent(top).ok, false, `${entry.name} accepted ${field}`);
    }
  }
});

test('the evidence fixtures cover one character from every hidden or control range', async () => {
  const required = [
    ['tag character', 0xe0041], ['zero-width space', 0x200b], ['left-to-right mark', 0x200e], ['right-to-left mark', 0x200f],
    ['arabic letter mark', 0x61c], ['first isolate', 0x2066], ['last isolate', 0x2069], ['delete', 0x7f], ['next line', 0x85],
    ['last C1 control', 0x9f], ['right-to-left override', 0x202e], ['line separator', 0x2028], ['start of heading', 0x01], ['lone surrogate', 0xd800],
  ];
  const evidence = [];
  for (const entry of eventEntries.filter((item) => item.expect !== 'valid' && item.stage === 'schema')) {
    const text = (await readText(fixture(entry.file)));
    evidence.push(text);
  }
  const bag = evidence.join('');
  for (const [label, code] of required) {
    const found = code === 0xd800 ? bag.includes('\\ud800') : bag.includes(String.fromCodePoint(code)) || bag.includes(`\\u${code.toString(16).padStart(4, '0')}`);
    assert.equal(found, true, `no invalid fixture holds a ${label}`);
  }
  // Each of those characters alone makes an otherwise valid check_result invalid.
  const base = await readJSON(fixture('learning-events/valid/check-result-passed.json'));
  for (const [label, code] of required) {
    const char = code === 0xd800 ? '\ud800' : String.fromCodePoint(code);
    assert.equal(validateEvent({ ...base, data: { ...base.data, evidence: `a${char}b` } }).ok, false, label);
  }
});

test('integer bounds hold at the edge', async () => {
  const load = (name) => readJSON(fixture(`learning-events/valid/${name}.json`));
  const table = [
    ['attempt-submitted', 'artifactBytes', 0, 104857600],
    ['check-result-passed', 'durationMs', 0, 3600000],
    ['reflection-saved', 'charCount', 1, 10000],
    ['review-result', 'intervalDays', 0, 3650],
    ['module-completed', 'checksPassed', 0, 1000],
    ['module-completed', 'tasksTotal', 0, 1000],
  ];
  for (const [name, field, low, high] of table) {
    const base = await load(name);
    const set = (value) => ({ ...base, data: { ...base.data, [field]: value } });
    assert.equal(validateEvent(set(high)).ok, true, `${field} at the maximum`);
    assert.equal(validateEvent(set(high + 1)).ok, false, `${field} over the maximum`);
    assert.equal(validateEvent(set(low)).ok, true, `${field} at the minimum`);
    assert.equal(validateEvent(set(low - 1)).ok, false, `${field} under the minimum`);
    assert.equal(validateEvent(set(1.5)).ok, false, `${field} fraction`);
    assert.equal(validateEvent(set('5')).ok, false, `${field} string`);
    assert.equal(validateEvent(set(null)).ok, false, `${field} null`);
  }
});

test('the validator never changes a document, even a frozen one', async () => {
  for (const entry of eventEntries) {
    if (entry.stage === 'parse' || entry.stage === 'repeat') continue;
    const document = JSON.parse(await readText(fixture(entry.file)));
    const copy = structuredClone(document);
    const result = validateEvent(deepFreeze(document));
    assert.equal(result.ok, entry.expect === 'valid', entry.name);
    assert.deepEqual(document, copy, entry.name);
  }
});

test('only a major version of 1 is supported, and it is checked before any other field', async () => {
  const base = await readJSON(fixture('learning-events/valid/task-opened.json'));
  for (const marker of ['2.0', '0.9', '10.0', '2.5', '1000.0', '2.0.0', '99999999999999999999.0']) {
    const result = validateEvent({ ...base, schemaVersion: marker, type: 'not-a-type', extra: 1 });
    assert.equal(result.reason, 'unsupported_version', marker);
    assert.deepEqual(result.errors.map((error) => error.pointer), ['/schemaVersion']);
  }
  for (const marker of ['1', '1.', '1.x', 'v1.0', 1, null, '', '01.0', '1.0.0']) {
    const result = validateEvent({ ...base, schemaVersion: marker });
    assert.equal(result.ok, false, String(marker));
    assert.equal(result.reason, 'invalid', String(marker));
  }
  assert.equal(validateEvent({ ...base, schemaVersion: '1.0' }).ok, true);
  assert.equal(validateEvent({ ...base, schemaVersion: '1.999999' }).ok, true);
});

test('a non-object document is an invalid result', () => {
  for (const value of [null, undefined, 3, 'text', [], true]) {
    const result = validateEvent(value);
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'invalid');
  }
});

test('an object that repeats a key is invalid, at any depth and however the key is written', () => {
  const repeats = [
    '{"a":1,"a":2}', '{"a":{"b":1,"b":2}}', '{"a":[{"b":1,"b":2}]}', '{"a":1,"\\u0061":2}', '{"__proto__":1,"__proto__":2}',
    '{"":1,"":2}', '{"a":1,"b":2,"a":3}', '[{"a":1,"a":1}]', '{"a\\n":1,"a\\u000a":2}', '{"\\ud800":1,"\\ud800":2}',
  ];
  for (const text of repeats) {
    const result = validateEventText(text);
    assert.equal(result.ok, false, text);
    assert.equal(result.reason, 'invalid', text);
    assert.deepEqual(result.errors.map((error) => error.pointer), [''], text);
  }
  // The same key in different objects, or a key text inside a string value, is not a repeat.
  const fine = [
    '{"a":{"a":1},"b":{"a":2}}', '{"a":[{"a":1},{"a":2}]}', '{"a":"a","b":"a"}', '{"a":"x\\",\\"a\\":1"}', '{"a":"{\\"a\\":1,\\"a\\":2}"}', '{"a":1,"A":2}',
  ];
  for (const text of fine) {
    const result = validateEventText(text);
    assert.equal(result.reason, 'invalid', text);
    assert.notDeepEqual(result.errors.map((error) => error.pointer), [''], `${text} is damaged by the schema, not by a repeat`);
  }
});

test('an unknown event type is invalid and names the type pointer', async () => {
  const base = await readJSON(fixture('learning-events/valid/task-opened.json'));
  const result = validateEvent({ ...base, type: 'confidence_rated', data: { rating: 3 } });
  assert.equal(result.reason, 'invalid');
  assert.ok(result.errors.some((error) => error.pointer === '/type'));
});

test('a result lists at most 100 errors and says how many were left out', async () => {
  const base = await readJSON(fixture('learning-events/valid/task-opened.json'));
  const many = { ...base, skills: Array.from({ length: 150 }, () => 'Bad') };
  const result = validateEvent(many);
  assert.equal(result.reason, 'invalid');
  assert.equal(result.errors.length, 101);
  assert.match(result.errors.at(-1).message, /^\d+ more errors omitted\.$/);
  assert.equal(result.errors.at(-1).pointer, '');
});

test('text and lines over their caps are refused before parsing', () => {
  const overLine = ' '.repeat(MAX_LINE_BYTES + 1);
  assert.equal(validateEventText(overLine).reason, 'too_large');
  assert.equal(validateEventText(' '.repeat(MAX_LINE_BYTES)).reason, 'invalid', 'at the cap the text is parsed');
  assert.equal(validateEventText('é'.repeat(MAX_LINE_BYTES / 2 + 1)).reason, 'too_large', 'the cap counts bytes');
  assert.equal(validateEventText(5).reason, 'invalid');
  const big = validateLogText(`{${' '.repeat(MAX_BYTES)}`);
  assert.equal(big.ok, false);
  assert.equal(big.reason, 'too_large');
  assert.deepEqual(big.lines, []);
  assert.equal(big.truncatedTail, false);
  const edge = validateLogText(' '.repeat(MAX_BYTES));
  assert.notEqual(edge.reason, 'too_large', 'a log at the cap is read');
  assert.equal(validateLogText('é'.repeat(MAX_BYTES / 2 + 1)).reason, 'too_large', 'the log cap counts bytes');
});

test('log input that is not a string or bytes is refused as a whole', () => {
  for (const value of [null, 5, {}, undefined]) {
    const result = validateLogText(value);
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'invalid');
    assert.deepEqual(result.lines, []);
  }
});

test('bytes are decoded one line at a time, so one bad line does not fail the whole log', async () => {
  const log = await readText(fixture('learning-events/valid/log-multi-line.jsonl'));
  const [first, second] = log.split('\n');
  const bytes = Buffer.concat([Buffer.from(`${first}\n`), Buffer.from([0x7b, 0xff, 0x7d, 0x0a]), Buffer.from(`${second}\n`)]);
  const result = validateLogText(bytes);
  assert.equal(result.reason, undefined, 'no whole-log refusal');
  assert.equal(result.ok, false);
  assert.deepEqual(result.lines.map((line) => line.ok), [true, false, true]);
  assert.equal(result.lines[1].reason, 'invalid');
  assert.match(result.lines[1].errors[0].message, /UTF-8/);
  assert.equal(result.truncatedTail, false);
});

test('a multibyte character cut at the end of the log is a truncated tail, not a failed log', async () => {
  const log = await readText(fixture('learning-events/valid/log-multi-line.jsonl'));
  const first = log.split('\n')[0];
  const cutBytes = Buffer.from('{"note":"é').subarray(0, -1);
  assert.equal(cutBytes.at(-1), 0xc3, 'the cut leaves a lead byte');
  for (const tail of [cutBytes, Buffer.from('{"note":"€').subarray(0, -1), Buffer.from('{"note":"\u{1F600}').subarray(0, -2)]) {
    const result = validateLogText(Buffer.concat([Buffer.from(`${first}\n`), tail]));
    assert.equal(result.reason, undefined);
    assert.equal(result.truncatedTail, true);
    assert.deepEqual(result.lines.map((line) => line.ok), [true, false]);
    assert.equal(result.lines[1].reason, 'truncated_tail');
  }
  // A bad byte that is not an incomplete ending is a damaged line, not a truncated tail.
  for (const tail of [Buffer.from([0x7b, 0xff, 0x7b]), Buffer.from([0x7b, 0xff]), Buffer.from([0x7b, 0xc3, 0x28])]) {
    const result = validateLogText(Buffer.concat([Buffer.from(`${first}\n`), tail]));
    assert.equal(result.truncatedTail, false);
    assert.equal(result.lines[1].reason, 'invalid');
  }
});

test('a log with too many lines is refused as a whole, and blank lines are cheap', () => {
  assert.equal(MAX_LINES, 50000);
  const over = validateLogText('\n'.repeat(MAX_LINES + 1));
  assert.equal(over.ok, false);
  assert.equal(over.reason, 'too_large');
  assert.deepEqual(over.lines, []);
  assert.equal(validateLogText(Buffer.from('\n'.repeat(MAX_LINES + 1))).reason, 'too_large');
  const edge = validateLogText('\n'.repeat(MAX_LINES));
  assert.equal(edge.reason, undefined);
  assert.equal(edge.lines.length, MAX_LINES);
  assert.ok(edge.lines.every((line) => line.ok === false && line.reason === 'invalid'));
  // A final line without a newline counts as a line.
  assert.equal(validateLogText(`${'\n'.repeat(MAX_LINES)}{`).reason, 'too_large');
  const started = performance.now();
  validateLogText('\n'.repeat(MAX_LINES));
  assert.ok(performance.now() - started < 2000, 'blank lines take the fast path');
});

test('a log splits on the newline character only', async () => {
  const one = (await readText(fixture('learning-events/valid/task-opened.json'))).trim();
  const withSeparator = `${one}\u2028${one}`;
  const result = validateLogText(`${withSeparator}\n`);
  assert.equal(result.lines.length, 1, 'U+2028 is not a line break');
  assert.equal(result.lines[0].ok, false);
});

test('a truncated final line is reported as truncated_tail and the earlier lines still count', async () => {
  const good = await readText(fixture('learning-events/valid/log-multi-line.jsonl'));
  const cut = good.slice(0, good.length - 25);
  const result = validateLogText(cut);
  assert.equal(result.truncatedTail, true);
  assert.equal(result.ok, false);
  const last = result.lines.at(-1);
  assert.equal(last.reason, 'truncated_tail');
  assert.equal(last.ok, false);
  assert.ok(result.lines.slice(0, -1).every((item) => item.ok), 'earlier lines are still valid');
  // The same cut text ending in a newline is a damaged line, not a truncated tail.
  const damaged = validateLogText(`${cut}\n`);
  assert.equal(damaged.truncatedTail, false);
  assert.equal(damaged.lines.at(-1).reason, 'invalid');
});

test('a valid final line without a newline is valid', async () => {
  const good = await readText(fixture('learning-events/valid/log-multi-line.jsonl'));
  const result = validateLogText(good.slice(0, -1));
  assert.equal(result.ok, true);
  assert.equal(result.truncatedTail, false);
});

test('a log line cannot be exactly 4 KB plus one byte, and exactly 4 KB is read', async () => {
  const one = (await readText(fixture('learning-events/valid/task-opened.json'))).trim();
  const atCap = one + ' '.repeat(MAX_LINE_BYTES - Buffer.byteLength(one));
  assert.equal(Buffer.byteLength(atCap), MAX_LINE_BYTES);
  assert.equal(validateLogText(`${atCap}\n`).ok, true);
  const over = validateLogText(`${atCap} \n`);
  assert.equal(over.ok, false);
  assert.equal(over.lines[0].reason, 'too_large');
});

test('mutation check: a parse fixture made valid JSON would fail the parse-stage assertion', async () => {
  const folder = await mkdtemp(path.join(tmpdir(), 'schola-event-mutation-'));
  try {
    const entry = eventEntries.find((item) => item.stage === 'parse');
    assert.ok(entry, 'the manifest has a parse case');
    const copy = path.join(folder, 'damaged.json');
    await copyFile(new URL(entry.file, fixtureRoot), copy);
    assert.throws(() => JSON.parse(readTextSync(copy)), SyntaxError);
    await writeFile(copy, await readText(fixture('learning-events/valid/task-opened.json')));
    assert.doesNotThrow(() => JSON.parse(readTextSync(copy)));
    assert.equal(validateEventText(readTextSync(copy)).ok, true, 'a repaired parse fixture is accepted, so the stage check can fail');
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});

test('mutation check: a version fixture given major 1 would fail the version-stage assertion', async () => {
  const entry = eventEntries.find((item) => item.stage === 'version');
  assert.ok(entry, 'the manifest has a version case');
  const document = await readJSON(fixture(entry.file));
  assert.equal(validateEvent(document).reason, 'unsupported_version');
  const repaired = { ...document, schemaVersion: '1.0' };
  assert.equal(validateEvent(repaired).ok, true, 'with major 1 the same document is valid');
  assert.equal(validateEvent({ ...repaired, schemaVersion: '3.0' }).reason, 'unsupported_version');
});

test('mutation check: a schema fixture made valid, and a valid fixture made invalid, change the verdict', async () => {
  const bad = await readJSON(fixture('learning-events/invalid/hint-full-answer-null-attempt.json'));
  assert.equal(validateEvent(bad).ok, false);
  assert.equal(validateEvent({ ...bad, data: { ...bad.data, attemptId: 'a1b2c3d4-0000-4000-8000-000000000999' } }).ok, true);
  const good = await readJSON(fixture('learning-events/valid/attempt-submitted.json'));
  assert.equal(validateEvent({ ...good, extra: true }).ok, false);
  const semantic = await readJSON(fixture('learning-events/invalid/impossible-date.json'));
  assert.equal(validateEvent(semantic).ok, false);
  assert.equal(validateEvent({ ...semantic, time: '2026-02-28T09:00:00Z' }).ok, true);
});

test('semantic rules: the day must exist and an offer cannot be triggered by itself', async () => {
  const base = await readJSON(fixture('learning-events/valid/task-opened.json'));
  const day = (time) => validateEvent({ ...base, time });
  assert.equal(day('2028-02-29T10:00:00Z').ok, true);
  for (const time of ['2026-02-29T10:00:00Z', '2100-02-29T10:00:00Z', '2026-04-31T10:00:00Z', '2026-06-31T10:00:00Z']) {
    const result = day(time);
    assert.equal(result.ok, false, time);
    assert.deepEqual(result.errors.map((error) => error.pointer), ['/time'], time);
  }
  const offer = await readJSON(fixture('learning-events/valid/agent-offer-pending.json'));
  const self = { ...offer, data: { ...offer.data, triggerEventId: offer.eventId } };
  const result = validateEvent(self);
  assert.equal(result.ok, false);
  assert.deepEqual(result.errors.map((error) => error.pointer), ['/data/triggerEventId']);
});

test('a log reports a repeated event id on the later line', async () => {
  const one = (await readText(fixture('learning-events/valid/task-opened.json'))).trim();
  const result = validateLogText(`${one}\n${one}\n`);
  assert.equal(result.ok, false);
  assert.equal(result.lines[0].ok, true);
  assert.equal(result.lines[1].ok, false);
  assert.deepEqual(result.lines[1].errors.map((error) => error.pointer), ['/eventId']);
});

test('a repeated event id is reported next to the other errors, and an invalid first line still counts', async () => {
  const one = await readJSON(fixture('learning-events/valid/task-opened.json'));
  const other = await readJSON(fixture('learning-events/valid/task-finished-passed.json'));
  const text = (...documents) => `${documents.map((item) => JSON.stringify(item)).join('\n')}\n`;
  // The repeat is on an invalid line: the repeat error joins the schema error.
  const second = validateLogText(text(one, { ...one, extra: 1 })).lines[1];
  assert.equal(second.ok, false);
  assert.equal(second.reason, 'invalid');
  assert.deepEqual(second.errors.map((found) => found.pointer).sort(), ['', '/eventId']);
  // The first line is invalid, and a later valid line that repeats its id is still reported.
  const result = validateLogText(text({ ...one, extra: 1 }, one));
  assert.deepEqual(result.lines.map((line) => line.ok), [false, false]);
  assert.deepEqual(result.lines[1].errors.map((found) => found.pointer), ['/eventId']);
  // A line with an unsupported version reports only the version, and does not take part.
  const newer = validateLogText(text({ ...one, schemaVersion: '2.0' }, one, { ...one, schemaVersion: '2.0' }));
  assert.deepEqual(newer.lines.map((line) => line.reason ?? 'ok'), ['unsupported_version', 'ok', 'unsupported_version']);
  assert.deepEqual(newer.lines[2].errors.map((found) => found.pointer), ['/schemaVersion']);
  // An id that is not a UUID still counts as a repeat, and distinct ids do not.
  const odd = validateLogText(text({ ...one, eventId: 'x' }, { ...one, eventId: 'x' }, { ...other, eventId: 'y' }));
  assert.deepEqual(odd.lines.map((line) => line.errors.map((found) => found.pointer).join()), ['/eventId', '/eventId,/eventId', '/eventId']);
  assert.equal(validateLogText(text(one, other)).ok, true);
});

test('a file over the size cap is refused after a size check and is never read', async (context) => {
  if (process.getuid?.() === 0) {
    context.skip('a root user can read any file');
    return;
  }
  const folder = await mkdtemp(path.join(tmpdir(), 'schola-event-noread-'));
  try {
    const big = path.join(folder, 'big.jsonl');
    await writeFile(big, '');
    await truncate(big, MAX_BYTES + 1);
    await chmod(big, 0o000);
    assert.equal((await validateLogFile(big)).reason, 'too_large', 'a read would have given unreadable');
    const exact = path.join(folder, 'exact.jsonl');
    await writeFile(exact, '');
    await truncate(exact, MAX_BYTES);
    await chmod(exact, 0o000);
    assert.equal((await validateLogFile(exact)).reason, 'unreadable', 'a file at the cap is read, so the denied read shows');
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});

test('the validators never change a fixture file', async () => {
  const stamp = async () => Promise.all(manifest.map(async (entry) => {
    const url = fixture(entry.file);
    const info = await stat(url);
    return [entry.file, info.mtimeMs, info.size, sha(await readFile(url))];
  }));
  const folder = await mkdtemp(path.join(tmpdir(), 'schola-event-files-'));
  try {
    const old = new Date('2020-01-02T03:04:05Z');
    const copies = [];
    for (const entry of logEntries) {
      const target = path.join(folder, path.basename(entry.file));
      await copyFile(new URL(entry.file, fixtureRoot), target);
      await utimes(target, old, old);
      copies.push(target);
    }
    const before = await Promise.all(copies.map(async (file) => [(await stat(file)).mtimeMs, sha(await readFile(file))]));
    for (const file of copies) await validateLogFile(file);
    const after = await Promise.all(copies.map(async (file) => [(await stat(file)).mtimeMs, sha(await readFile(file))]));
    assert.deepEqual(after, before);
    assert.deepEqual((await readdir(folder)).sort(), copies.map((file) => path.basename(file)).sort(), 'no file was created or removed');
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
  const before = await stamp();
  for (const entry of logEntries) await validateLogFile(fileURLToPath(new URL(entry.file, fixtureRoot)));
  assert.deepEqual(await stamp(), before);
});

test('an unreadable, oversized, or non-UTF-8 log file is refused as a whole', async () => {
  const folder = await mkdtemp(path.join(tmpdir(), 'schola-event-bad-'));
  try {
    const missing = await validateLogFile(path.join(folder, 'missing.jsonl'));
    assert.equal(missing.ok, false);
    assert.equal(missing.reason, 'unreadable');
    assert.doesNotMatch(JSON.stringify(missing), new RegExp(folder.replaceAll('/', '\\/')), 'the message holds no path');
    const broken = path.join(folder, 'broken.jsonl');
    await writeFile(broken, Buffer.from([0x7b, 0xff, 0x0a]));
    const damaged = await validateLogFile(broken);
    assert.equal(damaged.reason, undefined, 'a bad byte damages one line, not the whole file');
    assert.equal(damaged.lines[0].reason, 'invalid');
    const big = path.join(folder, 'big.jsonl');
    await writeFile(big, Buffer.alloc(MAX_BYTES + 1, 0x20));
    assert.equal((await validateLogFile(big)).reason, 'too_large');
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});

const SCHEMA_URL = new URL('contracts/learning-event.v1.schema.json', root);
const dataDefOf = (schema) => {
  const byType = {};
  for (const rule of schema.allOf) {
    const name = rule.then.properties.data.$ref.split('/').pop();
    byType[rule.if.properties.type.const] = { name, def: schema.$defs[name] };
  }
  return byType;
};

test('deleting any required field, in the envelope or in any data shape, makes an event invalid', async () => {
  const schema = await readJSON(SCHEMA_URL);
  // Find every required list outside an if clause, so a new shape cannot slip past this test.
  const lists = [];
  const walk = (node, where) => {
    if (Array.isArray(node)) node.forEach((item, index) => walk(item, `${where}/${index}`));
    else if (node && typeof node === 'object') {
      if (Array.isArray(node.required) && !where.includes('/if')) lists.push(where);
      for (const [key, child] of Object.entries(node)) if (key !== 'enum' && key !== 'const') walk(child, `${where}/${key}`);
    }
  };
  walk(schema, '');
  const byType = dataDefOf(schema);
  const expected = ['', ...Object.values(byType).filter(({ def }) => def.required).map(({ name }) => `/$defs/${name}`)];
  assert.deepEqual([...lists].sort(), expected.sort());
  assert.deepEqual(Object.keys(byType).sort(), [...EVENT_TYPES].sort());
  assert.deepEqual([...schema.required].sort(), Object.keys(schema.properties).sort(), 'every envelope field is required');
  const covered = new Set();
  for (const entry of eventEntries.filter((item) => item.expect === 'valid')) {
    const document = await readJSON(fixture(entry.file));
    for (const key of schema.required) {
      const copy = structuredClone(document);
      delete copy[key];
      assert.equal(validateEvent(copy).ok, false, `${entry.name} stayed valid without ${key}`);
    }
    for (const key of byType[document.type].def.required ?? []) {
      const copy = structuredClone(document);
      delete copy.data[key];
      assert.equal(validateEvent(copy).ok, false, `${entry.name} stayed valid without data.${key}`);
      covered.add(`${document.type}.${key}`);
    }
  }
  for (const [type, { def }] of Object.entries(byType)) {
    for (const key of def.required ?? []) assert.ok(covered.has(`${type}.${key}`), `no valid fixture covers ${type}.${key}`);
    assert.deepEqual([...(def.required ?? [])].sort(), Object.keys(def.properties ?? {}).sort(), `${type}: every data field is required`);
  }
});

test('the enum sets are pinned and match the guide', async () => {
  const schema = await readJSON(SCHEMA_URL);
  const guide = await readText(new URL('contracts/learning-events.md', root));
  const levels = ['nudge', 'question', 'partial-example', 'full-answer'];
  const pins = [
    ['source', schema.properties.source.enum, ['student', 'app', 'agent'], '- `source` is '],
    ['check status', schema.$defs.dataCheckResult.properties.status.enum, ['passed', 'failed', 'error', 'skipped', 'self-reported'], '- `status`: '],
    ['offeredLevel', schema.$defs.dataAgentOffer.properties.offeredLevel.enum, [...levels, 'recall-question', 'review-suggestion'], '- `offeredLevel`: ', ['recall-question', 'review-suggestion']],
    ['offer outcome', schema.$defs.dataAgentOffer.properties.outcome.enum, ['pending', 'accepted', 'dismissed', 'ignored'], '- `outcome`: `pending`'],
    ['review outcome', schema.$defs.dataReviewResult.properties.outcome.enum, ['correct', 'incorrect'], '- `outcome`: `correct`'],
    ['mode from', schema.$defs.dataModeChanged.properties.from.enum, ['coach', 'pair', 'show-me'], '- `from` and `to`: '],
    ['mode to', schema.$defs.dataModeChanged.properties.to.enum, ['coach', 'pair', 'show-me'], '- `from` and `to`: '],
    ['task result', schema.$defs.dataTaskFinished.properties.result.enum, ['passed', 'abandoned', 'skipped'], '- `result`: '],
    ['hint origin', schema.$defs.dataHintRequested.properties.origin.enum, ['student', 'agent-offer-accepted'], '- `origin`: '],
  ];
  for (const [label, actual, expected, prefix, docTokens] of pins) {
    assert.deepEqual([...actual].sort(), [...expected].sort(), `${label} enum changed`);
    const line = guide.split('\n').find((item) => item.startsWith(prefix));
    assert.ok(line, `${label}: the guide has no line starting ${prefix}`);
    const listed = [...line.replace(/^- `[^`]+`( and `[^`]+`)?(:| is) /, '').split('. ')[0].matchAll(/`([^`]+)`/g)].map((match) => match[1]);
    assert.deepEqual(listed.sort(), [...(docTokens ?? expected)].sort(), `${label}: the guide and the schema disagree`);
  }
});

test('the schema file is closed everywhere an object appears', async () => {
  const schema = await readJSON(new URL('contracts/learning-event.v1.schema.json', root));
  assert.equal(schema.$id, 'urn:schola:contract:learning-event:v1');
  assert.equal(schema.$schema, 'https://json-schema.org/draft/2020-12/schema');
  const open = [];
  const walk = (node, where) => {
    if (Array.isArray(node)) node.forEach((item, index) => walk(item, `${where}/${index}`));
    else if (node && typeof node === 'object') {
      if (node.type === 'object' && node.additionalProperties !== false) open.push(where);
      for (const [key, child] of Object.entries(node)) if (key !== 'enum' && key !== 'const') walk(child, `${where}/${key}`);
    }
  };
  walk(schema, '');
  assert.deepEqual(open, []);
});

function readTextSync(file) {
  return readFileSync(file, 'utf8');
}
