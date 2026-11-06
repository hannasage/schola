import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';

const project = fileURLToPath(new URL('../../', import.meta.url));

// The real stores write the data: LessonStore pins the modules and EventLog checks and appends each event.
// Nothing here writes a file by hand, except the damage that a test asks for on purpose.
const seeder = `
import json, sys, os
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from event_log import EventLog
from lesson_store import LessonStore
data = Path(sys.argv[1])
spec = json.load(sys.stdin)
base = json.loads((Path(sys.argv[2]) / 'tests/fixtures/contracts/valid/lesson-module-minimal.json').read_text())
files = []
for module in spec.get('modules', []):
    document = dict(base, id=module['id'], title=module['title'])
    task = base['tasks'][0]
    document['tasks'] = [dict(task, id='task-%d' % n, prompt=text) for n, text in enumerate(module['tasks'], 1)]
    document['checks'] = [{'id': name, 'kind': 'self-reported', 'description': text} for name, text in module['checks']]
    files.append((module['id'] + '.json', (json.dumps(document) + '\\n').encode()))
if files:
    LessonStore(data).import_files(files)
log = EventLog(data)
for event in spec.get('events', []):
    log.append(event)
tail = spec.get('rawAppend')
if tail:
    with open(data / '_events' / 'events.jsonl', 'ab') as handle:
        handle.write(tail.encode('utf-8'))
if spec.get('logAsSymlink'):
    target = data / '_events' / 'elsewhere.jsonl'
    os.replace(data / '_events' / 'events.jsonl', target)
    os.symlink(target, data / '_events' / 'events.jsonl')
print('SEEDED')
`;

/** Seed an isolated server's data directory through the real stores. @param {string} directory @param {Record<string, any>} spec */
export async function seedHub(directory, spec) {
  const child = spawn('python3', ['-u', '-c', seeder, directory, project], {stdio: ['pipe', 'pipe', 'pipe']});
  let out = '';
  let err = '';
  child.stdout.setEncoding('utf8').on('data', value => { out += value; });
  child.stderr.setEncoding('utf8').on('data', value => { err += value; });
  const done = new Promise(resolve => child.once('exit', resolve));
  child.stdin.end(JSON.stringify(spec));
  const code = await done;
  if (code !== 0 || !out.includes('SEEDED')) throw new Error(`The hub seeder failed (${code}): ${err.slice(-1500)}`);
}

const SESSIONS = new Map();
/** One stable session id for each name. @param {string} name */
export const session = name => { if (!SESSIONS.has(name)) SESSIONS.set(name, randomUUID()); return SESSIONS.get(name); };

/** The RFC 3339 time, in whole seconds, for a number of whole days before today at the given UTC hour. @param {number} daysAgo @param {number} [hour] */
export function when(daysAgo, hour = 9, now = new Date()) {
  const day = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - daysAgo, hour, 0, 0);
  return new Date(day).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

const APP = new Set(['attempt_submitted', 'check_result', 'task_finished', 'module_completed']);
const DEFAULTS = {
  check_result: {checkId: 'check-1', attemptId: 'a1', status: 'failed', durationMs: 5, evidence: '', runnerType: null},
  task_finished: {result: 'passed'},
  task_opened: {},
  module_completed: {checksPassed: 1, tasksTotal: 2},
  hint_requested: {level: 'nudge', attemptId: null, origin: 'student'},
};

/** One event of the learning-event contract. @param {string} kind @param {{time: string, session: string, module?: string, task?: string, data?: Record<string, unknown>}} options */
export function makeEvent(kind, {time, session: sessionId, module = 'alpha', task = 'task-1', data = {}}) {
  return {
    schemaVersion: '1.0', eventId: randomUUID(), time, sessionId, moduleId: module, moduleVersion: '1.0.0',
    taskId: kind === 'module_completed' ? null : task, source: APP.has(kind) ? 'app' : 'student',
    skills: [], type: kind, data: {...DEFAULTS[kind], ...data},
  };
}
