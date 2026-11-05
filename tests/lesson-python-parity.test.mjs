// The Python validator (lesson_contract.py) and the Node validator (contracts/validate.mjs)
// must agree. This test runs the Python module in a child process, on local files only,
// and compares the verdict, the reason, and the error pointers with the Node result.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { mkdtemp, readFile, rm, truncate, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { MAX_BYTES, validateLessonFile, validateLessonText } from '../contracts/validate.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const fixtureRoot = path.join(root, 'tests', 'fixtures', 'contracts');
const manifest = JSON.parse(await readFile(path.join(fixtureRoot, 'lesson-conformance.json'), 'utf8'));

// The child process reads a JSON list of jobs from a file named in its arguments and writes a JSON list of results.
// Each job is { kind, text } or { kind, file }. The -I flag keeps the working folder and
// the environment out of the import path. No bytecode is written next to the sources.
const PYTHON_RUNNER = [
  'import sys, json',
  'sys.path.insert(0, sys.argv[1])',
  'import lesson_contract as lc',
  'with open(sys.argv[2], encoding="utf-8") as handle:',
  '    jobs = json.load(handle)',
  'out = []',
  'for job in jobs:',
  '    if "file" in job:',
  '        out.append(lc.validate_lesson_file(job["kind"], job["file"]))',
  '    else:',
  '        out.append(lc.validate_lesson_text(job["kind"], job["text"]))',
  'print(json.dumps(out))',
].join('\n');

// The child gets no stdin. A child that waits on a piped stdin can block forever when the machine is
// busy, so the jobs go in a private temp file and the child gets its path. A hung child is killed
// after PYTHON_TIMEOUT_MS, so a hang fails this test instead of freezing the whole run.
const PYTHON_TIMEOUT_MS = 60_000;

function runPython(args, jobs) {
  const folder = mkdtempSync(path.join(tmpdir(), 'schola-parity-'));
  try {
    const argv = [...args];
    if (jobs !== undefined) {
      const jobsFile = path.join(folder, 'jobs.json');
      writeFileSync(jobsFile, jobs, { mode: 0o600 });
      argv.push(jobsFile);
    }
    const child = spawnSync('python3', ['-I', '-c', ...argv], {
      stdio: ['ignore', 'pipe', 'pipe'],
      encoding: 'utf8',
      maxBuffer: 1 << 28,
      timeout: PYTHON_TIMEOUT_MS,
      killSignal: 'SIGKILL',
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
    });
    if (child.error) assert.fail(child.error.code === 'ETIMEDOUT' ? `the Python child did not finish in ${PYTHON_TIMEOUT_MS} ms and was killed` : child.error.message);
    assert.equal(child.status, 0, child.stderr);
    return child.stdout;
  } finally {
    rmSync(folder, { recursive: true, force: true });
  }
}

const runJobs = (jobs) => JSON.parse(runPython([PYTHON_RUNNER, root], JSON.stringify(jobs)));
const pointerSet = (result) => (result.ok ? [] : [...new Set(result.errors.map((error) => error.pointer))].sort());

function assertSame(python, node, label) {
  assert.equal(python.ok, node.ok, `${label}: ok`);
  assert.equal(python.reason, node.reason, `${label}: reason`);
  assert.deepEqual(pointerSet(python), pointerSet(node), `${label}: pointers`);
  if (!node.ok) assert.equal(python.errors.length, node.errors.length, `${label}: error count`);
  if (node.ok) assert.deepEqual(Object.keys(python), ['ok']);
  else {
    assert.deepEqual(Object.keys(python).sort(), ['errors', 'ok', 'reason']);
    for (const error of python.errors) assert.deepEqual(Object.keys(error).sort(), ['message', 'pointer']);
  }
}

const expectedFor = (entry) => (entry.expect === 'valid' ? true : entry.expect.slice('invalid:'.length));

test('the Python validator agrees with the Node validator on every conformance fixture', async () => {
  const texts = await Promise.all(manifest.map((entry) => readFile(path.join(fixtureRoot, entry.file), 'utf8')));
  const results = runJobs(manifest.map((entry, index) => ({ kind: entry.kind, text: texts[index] })));
  assert.equal(results.length, manifest.length);
  manifest.forEach((entry, index) => {
    const node = validateLessonText(entry.kind, texts[index]);
    assertSame(results[index], node, entry.name);
    // Both must also match the manifest, so two wrong validators cannot agree by accident.
    assert.equal(results[index].ok ? true : results[index].reason, expectedFor(entry), entry.name);
  });
});

test('the Python validator agrees with the Node validator when it reads fixture files', async () => {
  const results = runJobs(manifest.map((entry) => ({ kind: entry.kind, file: path.join(fixtureRoot, entry.file) })));
  for (const [index, entry] of manifest.entries()) {
    assertSame(results[index], await validateLessonFile(entry.kind, path.join(fixtureRoot, entry.file)), entry.name);
  }
});

test('the two validators agree on unreadable and oversized files', async () => {
  const folder = await mkdtemp(path.join(tmpdir(), 'schola-lesson-parity-'));
  try {
    const big = path.join(folder, 'big.json');
    await writeFile(big, '');
    await truncate(big, MAX_BYTES + 1);
    const exact = path.join(folder, 'exact.json');
    await writeFile(exact, '');
    await truncate(exact, MAX_BYTES);
    const cases = [
      ['missing', path.join(folder, 'missing.json')],
      ['directory', folder],
      ['too large', big],
      ['exactly the cap', exact],
    ];
    const results = runJobs(cases.map(([, file]) => ({ kind: 'packet', file })));
    for (const [index, [label, file]] of cases.entries()) {
      const node = await validateLessonFile('packet', file);
      assertSame(results[index], node, label);
      if (node.reason === 'unreadable') assert.deepEqual(results[index].errors, node.errors, label);
    }
    assert.deepEqual(results.map((result) => result.reason), ['unreadable', 'unreadable', 'too_large', 'invalid']);
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});

test('the two validators both refuse a file that is not valid UTF-8', async () => {
  const folder = await mkdtemp(path.join(tmpdir(), 'schola-lesson-utf8-'));
  try {
    const good = await readFile(path.join(fixtureRoot, 'valid', 'lesson-module-minimal.json'));
    const broken = Buffer.from(good.toString('latin1').replace('A minimal lesson module', 'A minimal \xff lesson module'), 'latin1');
    const withBom = Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), good]);
    const cases = [['good', good], ['bad byte in a valid document', broken], ['byte order mark', withBom]];
    const files = [];
    for (const [label, bytes] of cases) {
      const file = path.join(folder, `${label.replaceAll(' ', '-')}.json`);
      await writeFile(file, bytes);
      files.push([label, file]);
    }
    const results = runJobs(files.map(([, file]) => ({ kind: 'lesson-module', file })));
    for (const [index, [label, file]] of files.entries()) assertSame(results[index], await validateLessonFile('lesson-module', file), label);
    assert.deepEqual(results.map((result) => result.ok), [true, false, false]);
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});

const fixtureDocument = async (file) => JSON.parse(await readFile(path.join(fixtureRoot, file), 'utf8'));

test('the two validators agree on hand-made edge cases', async () => {
  const module = await fixtureDocument('valid/lesson-module-full.json');
  const packet = await fixtureDocument('valid/packet-one-module.json');
  const record = await fixtureDocument('valid/help-record-nudge-null-attempt.json');
  const withField = (base, change) => {
    const copy = structuredClone(base);
    change(copy);
    return copy;
  };
  const cases = [
    ['more than 100 schema errors', 'packet', withField(packet, (d) => { d.modules = Array.from({ length: 150 }, () => ({ id: 'm' })); })],
    ['more than 100 semantic errors', 'lesson-module', withField(module, (d) => { d.blocks = Array.from({ length: 200 }, () => ({ type: 'text', id: 'same', body: 'x' })); })],
    ['exactly 100 semantic errors', 'lesson-module', withField(module, (d) => { d.blocks = Array.from({ length: 101 }, () => ({ type: 'text', id: 'same', body: 'x' })); })],
    ['major 2', 'packet', withField(packet, (d) => { d.schemaVersion = '2.0.0'; })],
    ['major 0', 'packet', withField(packet, (d) => { d.schemaVersion = '0.9'; })],
    ['leading zero major', 'packet', withField(packet, (d) => { d.schemaVersion = '01.0'; })],
    ['huge major', 'packet', withField(packet, (d) => { d.schemaVersion = `${'9'.repeat(400)}.0`; })],
    ['three part version', 'packet', withField(packet, (d) => { d.schemaVersion = '1.0.0'; })],
    ['version not a string', 'packet', withField(packet, (d) => { d.schemaVersion = 1; })],
    ['array document', 'packet', []],
    ['null document', 'help-record', null],
    ['unknown kind', 'curriculum', {}],
    ['space-only title', 'packet', withField(packet, (d) => { d.title = ' 　'; })],
    ['title with a zero-width space only', 'packet', withField(packet, (d) => { d.title = '​⁠'; })],
    ['id with a trailing newline', 'packet', withField(packet, (d) => { d.id = `${d.id}\n`; })],
    ['non-ASCII digits in a timestamp', 'help-record', withField(record, (d) => { d.timestamp = '٢٠٢٤-01-01T00:00:00Z'; })],
    ['leap day', 'help-record', withField(record, (d) => { d.timestamp = '2024-02-29T10:00:00Z'; })],
    ['not a leap day', 'help-record', withField(record, (d) => { d.timestamp = '2023-02-29T10:00:00Z'; })],
    ['century not a leap year', 'help-record', withField(record, (d) => { d.timestamp = '2100-02-29T10:00:00Z'; })],
    ['31 days in a 30 day month', 'help-record', withField(record, (d) => { d.timestamp = '2024-04-31T10:00:00Z'; })],
    ['title of 200 emoji', 'packet', withField(packet, (d) => { d.title = '\u{1F600}'.repeat(200); })],
    ['title of 201 emoji', 'packet', withField(packet, (d) => { d.title = '\u{1F600}'.repeat(201); })],
    ['minutes as a float with no fraction', 'lesson-module', withField(module, (d) => { d.estimatedMinutes = 5.0; })],
    ['minutes as a fraction', 'lesson-module', withField(module, (d) => { d.estimatedMinutes = 5.5; })],
    ['minutes as a boolean', 'lesson-module', withField(module, (d) => { d.estimatedMinutes = true; })],
    ['duplicate prerequisites', 'lesson-module', withField(module, (d) => { d.prerequisites = ['a-b', 'a-b']; })],
    ['self prerequisite', 'lesson-module', withField(module, (d) => { d.prerequisites = [d.id]; })],
    ['reordered hints', 'lesson-module', withField(module, (d) => { d.tasks[0].hints.reverse(); })],
    ['automated check without a runner', 'lesson-module', withField(module, (d) => { d.checks = [{ id: 'c', kind: 'automated', description: 'x' }]; })],
    ['self-reported check with a runner', 'lesson-module', withField(module, (d) => { d.checks = [{ id: 'c', kind: 'self-reported', description: 'x', runner: { type: 't' } }]; })],
    ['runner with a bad key', 'lesson-module', withField(module, (d) => { d.checks = [{ id: 'c', kind: 'automated', description: 'x', runner: { type: 't', Bad: 'x', constructor: 'y', n: 2147483648 } }]; })],
    ['runner with a nested object', 'lesson-module', withField(module, (d) => { d.checks = [{ id: 'c', kind: 'automated', description: 'x', runner: { type: 't', nested: { a: 1 } } }]; })],
    ['url with a user name', 'lesson-module', withField(module, (d) => { d.blocks = [{ type: 'source-link', id: 's', title: 't', url: 'https://user@example.com/x' }]; })],
    ['null attempt on a full answer', 'help-record', withField(record, (d) => { d.level = 'full-answer'; })],
  ];
  const texts = cases.map(([, , value]) => JSON.stringify(value));
  const results = runJobs(cases.map(([, kind], index) => ({ kind, text: texts[index] })));
  cases.forEach(([label, kind], index) => assertSame(results[index], validateLessonText(kind, texts[index]), label));
});

test('the two validators agree on damaged, oversized, and odd text', () => {
  const cases = [
    ['empty', 'packet', ''],
    ['truncated', 'packet', '{"schemaVersion": "1.0"'],
    ['trailing comma', 'packet', '[1,]'],
    ['single quotes', 'packet', "{'a': 1}"],
    ['NaN', 'packet', '{"a": NaN}'],
    ['byte order mark', 'packet', '﻿{}'],
    ['bare number', 'packet', '5'],
    ['over the cap', 'packet', `{${' '.repeat(MAX_BYTES)}`],
    ['at the cap', 'packet', `{${' '.repeat(MAX_BYTES - 1)}`],
    ['two bytes per character, over the cap', 'packet', `"${'é'.repeat(MAX_BYTES / 2)}"`],
  ];
  const results = runJobs(cases.map(([, kind, text]) => ({ kind, text })));
  cases.forEach(([label, kind, text], index) => assertSame(results[index], validateLessonText(kind, text), label));
});

// A small seeded mutation run. It keeps the two validators in step on documents the
// fixtures do not cover. The seed is fixed, so a failure can be repeated.
test('the two validators agree on seeded mutations of every fixture', async () => {
  let seed = 20260101;
  const random = () => {
    seed = (seed * 1664525 + 1013904223) % 4294967296;
    return seed / 4294967296;
  };
  const pick = (list) => structuredClone(list[Math.floor(random() * list.length)]);
  const pool = [null, true, false, 0, 1, -1, 1.5, 2147483648, '', ' ', 'x', 'nudge', 'full-answer', 'text', 'automated', 'a-b', 'A',
    ' ', '1.0', '2.0', '2024-02-30T10:00:00Z', 'https://user@example.com', 'student', '__proto__', 'USD', [], {}, [1], ['a', 'a'], { a: 1 }];
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
        else parent[pick(['extra', 'runner', 'id', 'level'])] = pick(pool);
      } else if (typeof parent[key] === 'string') parent[key] = parent[key].slice(0, Math.floor(random() * parent[key].length)) + pick(['', '\n', ' ']);
      else parent[key] = pick(pool);
    }
    return copy;
  };
  const jobs = [];
  for (const entry of manifest) {
    if (entry.stage === 'parse') continue;
    const document = await fixtureDocument(entry.file);
    for (let count = 0; count < 12; count += 1) jobs.push({ kind: entry.kind, text: JSON.stringify(mutate(document)) });
  }
  const results = runJobs(jobs);
  jobs.forEach((job, index) => assertSame(results[index], validateLessonText(job.kind, job.text), `mutation ${index}: ${job.text.slice(0, 200)}`));
});

test('an unsupported schema keyword makes the Python validator raise', () => {
  const script = [
    'import sys',
    'sys.path.insert(0, sys.argv[1])',
    'import lesson_contract as lc',
    'try:',
    '    lc.SchemaRegistry([{"$id": "urn:test:a", "format": "email"}])',
    'except lc.UnsupportedKeywordError as error:',
    '    print("raised", "format" in str(error))',
    'else:',
    '    print("passed")',
  ].join('\n');
  assert.equal(runPython([script, root]).trim(), 'raised True');
});
