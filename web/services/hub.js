import {TOKEN_HEADER} from './checks.js';
import {readHub} from '../hub/model.js';

const SESSION_ROUTE = '/api/checks/session';
const HUB_ROUTE = '/api/hub';
const DETAIL_LIMIT = 300;
const GENERIC_TEXT = 'The local server could not finish this request.';
const BAD_ANSWER = 'The local server sent an answer Schola could not read. Try again.';
const MAX_OFFSET = 840;

/** @typedef {'network_error' | 'invalid_token' | 'invalid_response' | 'busy' | 'lessons_damaged' | 'forbidden_origin' | 'request_failed'} HubErrorKind */

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

/** Server text is untrusted. Strip control and invisible characters, then cap the length. @param {unknown} value */
function cleanDetail(value) {
  if (typeof value !== 'string') return '';
  return value.replace(/[\p{Cc}\p{Cf}]+/gu, ' ').trim().slice(0, DETAIL_LIMIT);
}

/** @param {unknown} value @returns {value is Record<string, any>} */
const isObject = value => Boolean(value) && typeof value === 'object' && !Array.isArray(value);

/** A failed hub request. The message is fixed text. The server's words stay in `detail`. */
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

/** The time zone name of the browser, or an empty text when it has none. */
function browserZone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/**
 * The hub route of the local server. The transport, the zone, and the clock are injected so tests can fake them.
 * The token is fetched at the first call and fetched again once if the server rejects it.
 * The first request names the time zone. If the server refuses that field, one more request sends the offset.
 * @param {{transport?: typeof fetch, zone?: () => string | undefined, now?: () => {getTimezoneOffset: () => number}}} [options]
 * @returns {{getHub: (options?: {signal?: AbortSignal}) => Promise<HubResponse>}}
 */
export function createHubService({transport, zone = browserZone, now = () => new Date()} = {}) {
  /** @type {string | null} */
  let token = null;

  /** @param {string} path @param {RequestInit} init @returns {Promise<{status: number, ok: boolean, value: unknown}>} */
  async function send(path, init) {
    let response;
    try { response = await (transport ?? globalThis.fetch.bind(globalThis))(path, init); }
    catch { throw new HubServiceError(0, 'network_error', 'The local server did not answer. Try again.'); }
    let value;
    try { value = await response.json(); } catch { value = undefined; }
    return {status: response.status, ok: response.ok, value};
  }

  /** @param {{status: number, value: unknown}} answer */
  function failure(answer) {
    const {status, value} = answer;
    const detail = cleanDetail(isObject(value) ? value.error : '');
    const serverCode = isObject(value) && typeof value.code === 'string' && /^[a-z][a-z_]{0,39}$/.test(value.code) ? value.code : '';
    /** @type {{code: HubErrorKind, text: string}} */
    let known;
    if (serverCode && Object.hasOwn(CODE_TEXT, serverCode)) known = CODE_TEXT[serverCode];
    else if (serverCode) known = {code: 'request_failed', text: `The local server could not finish this request (code: ${serverCode}).`};
    else known = {code: 'request_failed', text: GENERIC_TEXT};
    return new HubServiceError(status, known.code, known.text, detail, serverCode);
  }

  async function getToken() {
    if (token) return token;
    const answer = await send(SESSION_ROUTE, {method: 'GET', credentials: 'omit'});
    if (!answer.ok || !isObject(answer.value) || typeof answer.value.token !== 'string' || !answer.value.token) throw failure(answer);
    token = answer.value.token;
    return token;
  }

  /** One GET with the token. A restarted server has a new token, so a rejected token is fetched again once. @param {string} query @param {AbortSignal | undefined} signal */
  async function ask(query, signal) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      /** @type {RequestInit} */
      const init = {method: 'GET', credentials: 'omit', headers: {[TOKEN_HEADER]: await getToken()}};
      if (signal) init.signal = signal;
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

  return {
    async getHub({signal} = {}) {
      const name = zoneName();
      let answer = await ask(hubQuery({timeZone: name, tzOffsetMinutes: offsetMinutes(now())}), signal);
      // The server may not know the zone name. It then answers 400 for that field. Send the offset once.
      if (name && answer.status === 400 && isObject(answer.value) && answer.value.field === 'timeZone') {
        answer = await ask(hubQuery({tzOffsetMinutes: offsetMinutes(now())}), signal);
      }
      if (!answer.ok) throw failure(answer);
      const checked = readHub(answer.value);
      if (!checked.ok) throw new HubServiceError(answer.status, 'invalid_response', BAD_ANSWER, checked.reason);
      return checked.hub;
    },
  };
}
