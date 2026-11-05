// Validator for the lesson module, packet, and help record contracts.
//
// A call never changes its input and never writes a file. Files are opened for reading only.
import { readFileSync, readdirSync } from 'node:fs';
import { readFile, stat } from 'node:fs/promises';
import Ajv2020 from 'ajv/dist/2020.js';

/** The schema major versions this reader supports. */
export const SUPPORTED_SCHEMA_MAJORS = Object.freeze([1]);

/** The contract kinds this file validates. */
export const LESSON_CONTRACT_KINDS = Object.freeze(['lesson-module', 'packet', 'help-record']);

/** The largest document, in bytes, that a reader parses. Hosted ingest must enforce the same cap. */
export const MAX_BYTES = 5 * 1024 * 1024;
const HINT_LADDER = Object.freeze(['nudge', 'question', 'partial-example', 'full-answer']);
const MAX_ERRORS = 100;

const directory = new URL('./', import.meta.url);
const ajv = new Ajv2020({ strict: true, allErrors: true });
for (const name of readdirSync(directory).filter((file) => file.endsWith('.schema.json')).sort()) {
  ajv.addSchema(JSON.parse(readFileSync(new URL(name, directory), 'utf8')));
}
const validators = Object.fromEntries(LESSON_CONTRACT_KINDS.map((kind) => {
  const validate = ajv.getSchema(`urn:schola:contract:${kind}:v1`);
  if (!validate) throw new Error(`Missing schema for ${kind}.`);
  return [kind, validate];
}));

const invalid = (errors) => {
  if (errors.length <= MAX_ERRORS) return { ok: false, reason: 'invalid', errors };
  const omitted = errors.length - MAX_ERRORS;
  return { ok: false, reason: 'invalid', errors: [...errors.slice(0, MAX_ERRORS), { pointer: '', message: `${omitted} more errors omitted.` }] };
};
const tooLarge = () => ({ ok: false, reason: 'too_large', errors: [{ pointer: '', message: 'The document is larger than the 5 MB limit.' }] });
const unreadable = (error) => ({
  ok: false,
  reason: 'unreadable',
  errors: [{ pointer: '', message: `The file could not be read (${typeof error?.code === 'string' ? error.code : 'unknown'}).` }],
});
const escapePointer = (segment) => String(segment).replaceAll('~', '~0').replaceAll('/', '~1');
const pointerOf = (...segments) => segments.map((segment) => `/${escapePointer(segment)}`).join('');
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

function schemaErrors(validate) {
  return (validate.errors ?? []).map((error) => {
    const missing = error.keyword === 'required' ? `/${escapePointer(error.params.missingProperty)}` : '';
    return { pointer: `${error.instancePath}${missing}`, message: error.message ?? 'The value is not valid.' };
  });
}

function daysInMonth(year, month) {
  if (month === 2) return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28;
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

function duplicates(items, key, base, errors, label) {
  const seen = new Set();
  items.forEach((item, index) => {
    if (seen.has(item[key])) errors.push({ pointer: pointerOf(...base, index, key), message: `Each ${label} must be unique. This one is repeated.` });
    seen.add(item[key]);
  });
}

function moduleErrors(module) {
  const errors = [];
  duplicates(module.blocks, 'id', ['blocks'], errors, 'block id');
  duplicates(module.tasks, 'id', ['tasks'], errors, 'task id');
  duplicates(module.checks, 'id', ['checks'], errors, 'check id');
  const checkIds = new Set(module.checks.map((check) => check.id));
  module.blocks.forEach((block, index) => {
    if (block.type === 'check-reference' && !checkIds.has(block.checkId)) {
      errors.push({ pointer: pointerOf('blocks', index, 'checkId'), message: 'The block points at a check that this module does not declare.' });
    }
  });
  module.tasks.forEach((task, index) => {
    const ordered = task.hints.length === HINT_LADDER.length && task.hints.every((hint, level) => hint.level === HINT_LADDER[level]);
    if (!ordered) {
      errors.push({ pointer: pointerOf('tasks', index, 'hints'), message: `The hint ladder must hold exactly these levels in this order: ${HINT_LADDER.join(', ')}.` });
    }
  });
  module.prerequisites.forEach((id, index) => {
    if (id === module.id) {
      errors.push({ pointer: pointerOf('prerequisites', index), message: 'A module cannot list itself as a prerequisite.' });
    }
  });
  errors.push(...scriptErrors(module));
  return errors;
}

const SCRIPT_FIELDS = Object.freeze(['runtime', 'source', 'args', 'timeoutSeconds']);
/** The largest script, in UTF-8 bytes. The schema counts characters, so the byte limit is checked here. */
export const MAX_SCRIPT_BYTES = 65536;

/** Rules for script checks (schema version 1.1) that the schema cannot state. */
function scriptErrors(module) {
  const errors = [];
  const minor = Number(/^1\.(\d+)$/.exec(module.schemaVersion)[1]);
  if (minor < 1) {
    // Version 1.0 does not define the script fields. A 1.0 script runner with no fields stays valid.
    if (module.checks.some((check) => SCRIPT_FIELDS.some((field) => Object.hasOwn(check, field)))) {
      errors.push({ pointer: '/schemaVersion', message: 'Script check fields need schemaVersion 1.1 or later.' });
    }
    return errors;
  }
  module.checks.forEach((check, index) => {
    if (isObject(check.runner) && check.runner.type === 'script') {
      for (const field of ['runtime', 'source']) {
        if (!Object.hasOwn(check, field)) errors.push({ pointer: pointerOf('checks', index, field), message: `A script check needs the field ${field}.` });
      }
    }
    if (typeof check.source === 'string') {
      if (/\p{Cs}/u.test(check.source)) {
        errors.push({ pointer: pointerOf('checks', index, 'source'), message: 'The script holds a character that is not valid text.' });
      } else if (Buffer.byteLength(check.source, 'utf8') > MAX_SCRIPT_BYTES) {
        errors.push({ pointer: pointerOf('checks', index, 'source'), message: `The script is larger than ${MAX_SCRIPT_BYTES} bytes.` });
      }
    }
  });
  return errors;
}

function packetErrors(packet) {
  const errors = [];
  duplicates(packet.modules, 'id', ['modules'], errors, 'module id');
  return errors;
}

function helpRecordErrors(record) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T/.exec(record.timestamp);
  if (match && Number(match[3]) > daysInMonth(Number(match[1]), Number(match[2]))) {
    return [{ pointer: '/timestamp', message: 'The timestamp names a day that does not exist.' }];
  }
  return [];
}

const semanticChecks = { 'lesson-module': moduleErrors, packet: packetErrors, 'help-record': helpRecordErrors };

/**
 * Validate one parsed document.
 * @param {string} kind One of LESSON_CONTRACT_KINDS.
 * @param {unknown} document A parsed JSON value. It is read, never changed.
 * @returns {{ ok: true } | { ok: false, reason: 'invalid' | 'unsupported_version' | 'too_large' | 'unreadable', errors: { pointer: string, message: string }[] }}
 */
export function validateLessonContract(kind, document) {
  if (!LESSON_CONTRACT_KINDS.includes(kind)) {
    return invalid([{ pointer: '', message: `Unknown contract kind. Use one of: ${LESSON_CONTRACT_KINDS.join(', ')}.` }]);
  }
  if (!isObject(document)) return invalid([{ pointer: '', message: 'The document must be a JSON object.' }]);
  // Read the leading major number first. A value such as 2.0.0 is an unsupported version, not a malformed one.
  const marker = typeof document.schemaVersion === 'string' ? /^(\d+)\./.exec(document.schemaVersion) : null;
  if (marker && !SUPPORTED_SCHEMA_MAJORS.includes(Number(marker[1]))) {
    return {
      ok: false,
      reason: 'unsupported_version',
      errors: [{ pointer: '/schemaVersion', message: `This reader supports schema major version ${SUPPORTED_SCHEMA_MAJORS.join(', ')} only.` }],
    };
  }
  const validate = validators[kind];
  if (!validate(document)) return invalid(schemaErrors(validate));
  const errors = semanticChecks[kind](document);
  return errors.length > 0 ? invalid(errors) : { ok: true };
}

/**
 * Parse JSON text, then validate it. Text over MAX_BYTES is refused before parsing.
 * Text that is not JSON is an invalid result.
 * @param {string} kind
 * @param {string} text
 */
export function validateLessonText(kind, text) {
  if (typeof text !== 'string') return invalid([{ pointer: '', message: 'The text must be a string.' }]);
  if (Buffer.byteLength(text, 'utf8') > MAX_BYTES) return tooLarge();
  let document;
  try {
    document = JSON.parse(text);
  } catch {
    return invalid([{ pointer: '', message: 'The text is not complete, valid JSON.' }]);
  }
  return validateLessonContract(kind, document);
}

/**
 * Read a file and validate it. The file is opened for reading only, so it stays as it was.
 * A file over MAX_BYTES is refused after a size check and is never read.
 * @param {string} kind
 * @param {string | URL} file
 */
export async function validateLessonFile(kind, file) {
  let text;
  try {
    const info = await stat(file);
    if (info.size > MAX_BYTES) return tooLarge();
    const bytes = await readFile(file);
    try {
      text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
    } catch {
      return invalid([{ pointer: '', message: 'The text is not valid UTF-8.' }]);
    }
  } catch (error) {
    return unreadable(error);
  }
  return validateLessonText(kind, text);
}

/**
 * Build the one-entry packet that stands for a single lesson module.
 * The module is read, never changed. Validate the module first.
 * @param {{ schemaVersion: string, id: string, version: string, title: string }} module
 */
export function packetFromModule(module) {
  return {
    schemaVersion: module.schemaVersion,
    id: module.id,
    version: module.version,
    title: module.title,
    modules: [{ id: module.id, version: module.version }],
  };
}
