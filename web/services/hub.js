import {TOKEN_HEADER} from './checks.js';
import {readHub} from '../hub/model.js';

const SESSION_ROUTE = '/api/checks/session';
const HUB_ROUTE = '/api/hub';
const GENERIC_TEXT = 'The local server could not finish this request.';
const BAD_ANSWER = 'The local server sent an answer Schola could not read. Try again.';
const MAX_OFFSET = 840;
/** How long one hub load may take, in all, before the screen shows an error. */
export const DEFAULT_TIMEOUT_MS = 30_000;
const TIMEOUT_TEXT = 'The local server did not answer in time. Try again.';
const NO_CONNECTION_TEXT = 'Schola could not connect to the local server. Check that Schola is still running, then try again.';

/** @typedef {'timeout' | 'network_error' | 'invalid_token' | 'invalid_response' | 'busy' | 'lessons_damaged' | 'forbidden_origin' | 'request_failed'} HubErrorKind */

/**
 * Every code the hub route can answer with that needs its own words. The web side reads the code first.
 * A plain code that is not listed here gets a neutral message that shows the code.
 * @type {Record<string, {code: HubErrorKind, text: string}>}
 */
const CODE_TEXT = {
  invalid_token: {code: 'invalid_token', text: 'Schola could not confirm this page with the local server. Reload Schola and try again.'},
  forbidden_origin: {code: 'forbidden_origin', text: 'Open Schola from its own local address.'},
  lock_unavailable: {code: 'busy', text: 'Your event log is busy. Wait a moment, then try again.'},
  corrupt_file: {code: 'lessons_damaged', text: 'A saved lesson file is damaged. Schola kept it as it was.'},
  internal_error: {code: 'request_failed', text: 'Schola hit an unexpected problem and could not build the hub.'},
};

/** @param {unknown} value @returns {value is Record<string, any>} */
const isObject = value => Boolean(value) && typeof value === 'object' && !Array.isArray(value);

/** A failed hub request. The message is fixed text. The body of the server's answer is never kept: only the HTTP status and a plain error code. */
export class HubServiceError extends Error {
  /** @param {number} status @param {HubErrorKind} code @param {string} message @param {string} [detail] @param {string} [serverCode] */
  constructor(status, code, message, detail = '', serverCode = '') {
    super(message);
    this.name = 'HubServiceError';
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.serverCode = serverCode;
  }
}

/**
 * The query of the hub route. It carries the zone name, or else the offset, never both.
 * @param {{timeZone?: string, tzOffsetMinutes?: number}} choice
 */
export function hubQuery({timeZone, tzOffsetMinutes}) {
  const query = new URLSearchParams();
  if (timeZone) query.set('timeZone', timeZone);
  else if (typeof tzOffsetMinutes === 'number') query.set('tzOffsetMinutes', String(tzOffsetMinutes === 0 ? 0 : tzOffsetMinutes));
  const text = query.toString();
  return text ? '?' + text : '';
}

/**
 * Minutes east of UTC for a date, as the route wants them: a whole number from -840 to 840.
 * The browser counts west, so the sign flips.
 * @param {{getTimezoneOffset: () => number}} date
 */
export function offsetMinutes(date) {
  const east = Math.round(-date.getTimezoneOffset());
  return Math.min(MAX_OFFSET, Math.max(-MAX_OFFSET, east)) + 0;
}

/** @typedef {{getHub: (options?: {signal?: AbortSignal}) => Promise<{hub: HubResponse, offsetFallback: boolean}>}} HubService */

/** The time zone name of the browser, or an empty text when it has none. */
function browserZone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/**
 * The hub route of the local server. The transport, the zone, and the clock are injected so tests can fake them.
 * The token is fetched at the first call and fetched again once if the server rejects it.
 * The first request names the time zone. If the server refuses that field, one more request sends the offset.
 * @param {{transport?: typeof fetch, zone?: () => string | undefined, now?: () => {getTimezoneOffset: () => number}, timeoutMs?: number}} [options]
 * @returns {HubService}
 */
export function createHubService({transport, zone = browserZone, now = () => new Date(), timeoutMs = DEFAULT_TIMEOUT_MS} = {}) {
  /** @type {string | null} */
  let token = null;

  /** @param {string} path @param {RequestInit} init @returns {Promise<{status: number, ok: boolean, value: unknown}>} */
  async function send(path, init) {
    let response;
    try { response = await (transport ?? globalThis.fetch.bind(globalThis))(path, init); }
    catch { throw new HubServiceError(0, 'network_error', NO_CONNECTION_TEXT); }
    let value;
    try { value = await response.json(); } catch { value = undefined; }
    return {status: response.status, ok: response.ok, value};
  }

  /** @param {{status: number, value: unknown}} answer */
  function failure(answer) {
    const {status, value} = answer;
    const serverCode = isObject(value) && typeof value.code === 'string' && /^[a-z][a-z_]{0,39}$/.test(value.code) ? value.code : '';
    /** @type {{code: HubErrorKind, text: string}} */
    let known;
    if (serverCode && Object.hasOwn(CODE_TEXT, serverCode)) known = CODE_TEXT[serverCode];
    else if (serverCode) known = {code: 'request_failed', text: `The local server could not finish this request (code: ${serverCode}).`};
    else known = {code: 'request_failed', text: GENERIC_TEXT};
    return new HubServiceError(status, known.code, known.text, '', serverCode);
  }

  /** @param {AbortSignal} signal */
  async function getToken(signal) {
    if (token) return token;
    const answer = await send(SESSION_ROUTE, {method: 'GET', credentials: 'omit', signal});
    if (!answer.ok || !isObject(answer.value) || typeof answer.value.token !== 'string' || !answer.value.token) throw failure(answer);
    token = answer.value.token;
    return token;
  }

  /** One GET with the token. A restarted server has a new token, so a rejected token is fetched again once. @param {string} query @param {AbortSignal} signal */
  async function ask(query, signal) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      /** @type {RequestInit} */
      const init = {method: 'GET', credentials: 'omit', headers: {[TOKEN_HEADER]: await getToken(signal)}, signal};
      const answer = await send(HUB_ROUTE + query, init);
      if (answer.ok) return answer;
      if (isObject(answer.value) && answer.value.code === 'invalid_token' && attempt === 0) { token = null; continue; }
      return answer;
    }
    throw new HubServiceError(0, 'invalid_token', CODE_TEXT.invalid_token.text);
  }

  /** @returns {string} */
  function zoneName() {
    try { const name = zone(); return typeof name === 'string' ? name : ''; }
    catch { return ''; }
  }

  /** @param {AbortSignal} signal @returns {Promise<{hub: HubResponse, offsetFallback: boolean}>} */
  async function load(signal) {
    const name = zoneName();
    let offsetFallback = !name;
    let answer = await ask(hubQuery({timeZone: name, tzOffsetMinutes: offsetMinutes(now())}), signal);
    // The server may not know the zone name. It then answers 400 for that field. Send the offset once.
    if (name && answer.status === 400 && isObject(answer.value) && answer.value.field === 'timeZone') {
      offsetFallback = true;
      answer = await ask(hubQuery({tzOffsetMinutes: offsetMinutes(now())}), signal);
    }
    if (!answer.ok) throw failure(answer);
    const checked = readHub(answer.value);
    if (!checked.ok) throw new HubServiceError(answer.status, 'invalid_response', BAD_ANSWER, checked.reason);
    return {hub: checked.hub, offsetFallback};
  }

  return {
    /**
     * One load, with one clock for all of it: the token, the first request, and the retry.
     * The race makes the deadline hold even for a transport that ignores its signal.
     */
    async getHub({signal} = {}) {
      if (signal?.aborted) throw new DOMException('The hub request was cancelled.', 'AbortError');
      const controller = new AbortController();
      /** @type {ReturnType<typeof setTimeout> | undefined} */
      let timer;
      /** @type {(() => void) | undefined} */
      let onAbort;
      const deadline = new Promise((_, reject) => {
        timer = setTimeout(() => { controller.abort(); reject(new HubServiceError(0, 'timeout', TIMEOUT_TEXT)); }, timeoutMs);
      });
      const cancelled = new Promise((_, reject) => {
        onAbort = () => { reject(new DOMException('The hub request was cancelled.', 'AbortError')); controller.abort(); };
        signal?.addEventListener('abort', onAbort, {once: true});
      });
      try { return await Promise.race([load(controller.signal), deadline, cancelled]); }
      finally {
        clearTimeout(timer);
        if (onAbort) signal?.removeEventListener('abort', onAbort);
      }
    },
  };
}

/**
 * Small facts about a failure, for a line of small text. They hold the HTTP status and the plain error code, and nothing from the body.
 * @param {{code?: string, status?: number, serverCode?: string}} error
 */
export function errorFacts({code, status, serverCode}) {
  if (code === 'timeout') return 'Timed out waiting for the server.';
  if (code === 'network_error') return 'Could not connect to the server.';
  const parts = [];
  if (typeof status === 'number' && status > 0) parts.push(`HTTP ${status}`);
  if (serverCode) parts.push(`code ${serverCode}`);
  if (code === 'invalid_response') parts.push('answer not readable');
  return parts.join(' \u00b7 ');
}
