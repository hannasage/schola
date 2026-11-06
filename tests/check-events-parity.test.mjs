// The check events that the app writes must pass the Node validator too, not only the Python one.
// This test runs a real check in a child process (real lesson store, real check runner, real event log, temporary
// folders), then validates the log file with contracts/validate-events.mjs and compares the verdict with Python.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { validateLogFile } from '../contracts/validate-events.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const PYTHON_TIMEOUT_MS = 120_000;

// Argument 1 is the repository root. Argument 2 is a new empty folder for the data and the work.
// The program prints the path of the log and the Python verdict for it, as JSON.
const PYTHON_RUNNER = [
  'import json, os, sys',
  'sys.path.insert(0, sys.argv[1])',
  'import check_events, learning_event',
  'from event_log import EventLog',
  'from lesson_store import LessonStore',
  'base = sys.argv[2]',
  'data, work = os.path.join(base, "data"), os.path.join(base, "work")',
  'os.makedirs(data); os.makedirs(work)',
  'open(os.path.join(work, "README.md"), "w").write("hello TODO\\n")',
  'document = json.load(open(os.path.join(sys.argv[1], "tests", "fixtures", "contracts", "valid", "lesson-module-minimal.json")))',
  'document["schemaVersion"] = "1.1"',
  'document["checks"] = [',
  '    {"id": "has-readme", "kind": "automated", "description": "A README exists.", "runner": {"type": "file-exists", "path": "README.md"}},',
  '    {"id": "has-nope", "kind": "automated", "description": "Text is there.", "runner": {"type": "text-match", "path": "README.md", "text": "NOPE"}},',
  '    {"id": "bad-pattern", "kind": "automated", "description": "A broken pattern.", "runner": {"type": "text-match", "path": "README.md", "pattern": "("}},',
  '    {"id": "missing", "kind": "automated", "description": "A missing file.", "runner": {"type": "json-valid", "path": "nope.json"}},',
  ']',
  'LessonStore(data).import_files([("m.json", json.dumps(document))])',
  'module = LessonStore(data).get_module(document["id"], document["version"])',
  'log = EventLog(data)',
  'session = check_events.new_session_id()',
  'for check in module["checks"]:',
  '    outcome, report = check_events.run_and_record(check, work, log=log, session_id=session, module=module)',
  '    assert report.written is True, report',
  'path = os.path.join(data, "_events", "events.jsonl")',
  'print(json.dumps({"path": path, "python": learning_event.validate_log_file(path)}))',
].join('\n');

function runPython(folder) {
  const child = spawnSync('python3', ['-I', '-c', PYTHON_RUNNER, root, folder], {
    stdio: ['ignore', 'pipe', 'pipe'],
    encoding: 'utf8',
    timeout: PYTHON_TIMEOUT_MS,
    killSignal: 'SIGKILL',
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  });
  if (child.error) assert.fail(child.error.code === 'ETIMEDOUT' ? `the Python child did not finish in ${PYTHON_TIMEOUT_MS} ms and was killed` : child.error.message);
  assert.equal(child.status, 0, child.stderr);
  return JSON.parse(child.stdout);
}

test('the events of real check runs pass the Node validator and the Python validator alike', async () => {
  const folder = mkdtempSync(path.join(tmpdir(), 'schola-check-events-'));
  try {
    const { path: logPath, python } = runPython(folder);
    const node = await validateLogFile(logPath);
    assert.equal(node.ok, true, JSON.stringify(node));
    assert.equal(python.ok, true, JSON.stringify(python));
    assert.equal(node.lines.length, 8);
    assert.deepEqual(node.lines.map((line) => line.ok), python.lines.map((line) => line.ok));
    assert.equal(node.truncatedTail, python.truncatedTail);

    const events = readFileSync(logPath, 'utf8').trimEnd().split('\n').map((line) => JSON.parse(line));
    assert.deepEqual(events.map((event) => event.type), Array(4).fill(['attempt_submitted', 'check_result']).flat());
    assert.deepEqual([...new Set(events.map((event) => event.source))], ['app']);
    assert.equal(new Set(events.map((event) => event.sessionId)).size, 1);
    for (let index = 0; index < events.length; index += 2) {
      assert.equal(events[index].data.attemptId, events[index + 1].data.attemptId);
      assert.match(events[index].data.artifactDigest, /^[0-9a-f]{64}$/);
    }
    assert.deepEqual(events.filter((event) => event.type === 'check_result').map((event) => event.data.status), ['passed', 'failed', 'error', 'failed']);
    assert.deepEqual(events.filter((event) => event.type === 'check_result').map((event) => event.data.evidence), ['', '', 'invalid_pattern', 'not_found']);
    // The log holds no path, no file name, and no file content.
    const raw = readFileSync(logPath, 'utf8');
    for (const needle of [folder, 'README', 'hello TODO', 'nope.json']) assert.ok(!raw.includes(needle), `the log holds ${needle}`);
  } finally {
    rmSync(folder, { recursive: true, force: true });
  }
});
