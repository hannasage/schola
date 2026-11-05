const ROUTES = {
  session: '/api/checks/session',
  workspace: '/api/checks/workspace',
  inspect: '/api/checks/inspect',
  consent: '/api/checks/consent',
  run: '/api/checks/run',
};
/** The header the local server wants on every check call except the one that hands out the token. */
export const TOKEN_HEADER = 'X-Schola-Check-Token';
const DETAIL_LIMIT = 300;
const GENERIC_TEXT = 'The local server could not finish this request.';
/** The server allows 65,536 bytes for these routes. This leaves a margin. */
export const MAX_REQUEST_BYTES = 60_000;
const SHA256 = /^[0-9a-f]{64}$/;
const BAD_ANSWER = 'The local server sent an answer Schola could not read. Try again.';

/**
 * Every code the check routes can answer with. The web side reads the code first.
 * A code that is not listed here gets a neutral message that shows the code.
 * @type {Record<string, {code: CheckErrorKind, text: string}>}
 */
const CODE_TEXT = {
  invalid_token: {code: 'invalid_token', text: 'Schola could not confirm this page with the local server. Reload Schola and try again.'},
  consent_required: {code: 'consent_required', text: 'Review this script and agree before it runs.'},
  busy: {code: 'busy', text: 'Another check is running. Wait for it to finish, then try again.'},
  workspace_required: {code: 'workspace_required', text: 'Choose the folder where you do your work before you run a check.'},
  invalid_workspace: {code: 'invalid_workspace', text: 'Schola cannot use that folder. Choose an existing folder inside your home folder.'},
  hash_mismatch: {code: 'hash_mismatch', text: 'The script changed since it was shown to you. Review it again.'},
  not_found: {code: 'not_found', text: 'Schola could not find that check in your lessons.'},
  payload_too_large: {code: 'too_large', text: 'This request is too large to send.'},
  confirmation_required: {code: 'confirmation_required', text: 'Read the script and confirm before you agree.'},
  consent_not_needed: {code: 'consent_not_needed', text: 'This check runs no script, so it needs no consent.'},
  forbidden_origin: {code: 'forbidden_origin', text: 'Open Schola from its own local address.'},
  corrupt_file: {code: 'request_failed', text: 'A saved lesson file is damaged. Schola kept it as it was.'},
  internal_error: {code: 'request_failed', text: 'Schola hit an unexpected problem and could not finish this check.'},
};
const PLAIN_REQUEST_CODES = new Set(['invalid_request', 'invalid_json', 'unsupported_media_type', 'request_timeout']);

/** Server text is untrusted. Strip control and invisible characters, then cap the length. @param {unknown} value */
function cleanDetail(value) {
  if (typeof value !== 'string') return '';
  return value.replace(/[\p{Cc}\p{Cf}]+/gu, ' ').trim().slice(0, DETAIL_LIMIT);
}

/** A failed check request. The message is fixed text. The server's words stay in `detail`. */
export class CheckServiceError extends Error {
  /** @param {number} status @param {CheckErrorKind} code @param {string} message @param {string} [detail] @param {string} [serverCode] */
  constructor(status, code, message, detail = '', serverCode = '') {
    super(message);
    this.name = 'CheckServiceError';
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.serverCode = serverCode;
  }
}

/** @param {unknown} value @returns {value is Record<string, any>} */
const isObject = value => Boolean(value) && typeof value === 'object' && !Array.isArray(value);

/**
 * The check routes of the local server. The transport is injected so tests can fake it.
 * The token is fetched at the first call and fetched again once if the server rejects it.
 * A run request has no field that grants consent. Only `grantConsent` can, and it needs `confirm: true`.
 * @param {typeof fetch} [transport]
 * @returns {CheckService}
 */
export function createCheckService(transport = globalThis.fetch.bind(globalThis)) {
  /** @type {string | null} */
  let token = null;

  /** @param {string} path @param {RequestInit} init @returns {Promise<{status: number, ok: boolean, value: unknown}>} */
  async function send(path, init) {
    let response;
    try { response = await transport(path, init); }
    catch { throw new CheckServiceError(0, 'network_error', 'The local server did not answer. Try again.'); }
    let value;
    try { value = await response.json(); } catch { value = undefined; }
    return {status: response.status, ok: response.ok, value};
  }

  async function getToken() {
    if (token) return token;
    const answer = await send(ROUTES.session, {method: 'GET', credentials: 'omit'});
    if (!answer.ok || !isObject(answer.value) || typeof answer.value.token !== 'string' || !answer.value.token) throw failure(answer);
    token = answer.value.token;
    return token;
  }

  /** @param {{status: number, value: unknown}} answer */
  function failure(answer) {
    const {status, value} = answer;
    const detail = cleanDetail(isObject(value) ? value.error : '');
    const serverCode = isObject(value) && typeof value.code === 'string' && /^[a-z][a-z_]{0,39}$/.test(value.code) ? value.code : '';
    /** @type {{code: CheckErrorKind, text: string}} */
    let known;
    if (serverCode && Object.hasOwn(CODE_TEXT, serverCode)) known = CODE_TEXT[serverCode];
    else if (serverCode && PLAIN_REQUEST_CODES.has(serverCode)) known = {code: 'request_failed', text: GENERIC_TEXT};
    else if (serverCode) known = {code: 'request_failed', text: `The local server could not finish this request (code: ${serverCode}).`};
    else known = {code: 'request_failed', text: GENERIC_TEXT};
    return new CheckServiceError(status, known.code, known.text, detail, serverCode);
  }

  /** @param {string} path @param {{method?: string, body?: unknown}} [options] @returns {Promise<Record<string, any>>} */
  async function request(path, {method = 'GET', body} = {}) {
    /** @type {string | undefined} */
    let text;
    if (body !== undefined) {
      text = JSON.stringify(body);
      if (new TextEncoder().encode(text).length > MAX_REQUEST_BYTES) throw new CheckServiceError(0, 'too_large', CODE_TEXT.payload_too_large.text);
    }
    for (let attempt = 0; attempt < 2; attempt++) {
      /** @type {Record<string, string>} */
      const headers = {[TOKEN_HEADER]: await getToken()};
      /** @type {RequestInit} */
      const init = {method, credentials: 'omit', headers};
      if (text !== undefined) { headers['Content-Type'] = 'application/json'; init.body = text; }
      const answer = await send(path, init);
      if (answer.ok) {
        if (!isObject(answer.value)) throw new CheckServiceError(answer.status, 'invalid_response', BAD_ANSWER);
        return answer.value;
      }
      const error = failure(answer);
      // A restarted server has a new token. Fetch it again and send the same request once more.
      if (error.serverCode === 'invalid_token' && attempt === 0) { token = null; continue; }
      throw error;
    }
    throw new CheckServiceError(0, 'invalid_token', CODE_TEXT.invalid_token.text);
  }

  return {
    getWorkspace: async () => /** @type {CheckWorkspace} */(await request(ROUTES.workspace)),
    setWorkspace: async path => /** @type {CheckWorkspace} */(await request(ROUTES.workspace, {method: 'POST', body: {path}})),
    inspect: async ref => /** @type {CheckInspection} */(await request(ROUTES.inspect, {method: 'POST', body: {...ref}})),
    grantConsent: async (ref, options) => {
      if (!options || options.confirm !== true || typeof options.shownHash !== 'string' || !SHA256.test(options.shownHash)) {
        throw new CheckServiceError(0, 'confirmation_required', CODE_TEXT.confirmation_required.text);
      }
      return /** @type {CheckConsentGrant} */(await request(ROUTES.consent, {method: 'POST', body: {...ref, confirm: true, shownHash: options.shownHash}}));
    },
    run: async (ref, consentId) => /** @type {CheckRunResponse} */(await request(ROUTES.run, {method: 'POST', body: consentId === undefined ? {...ref} : {...ref, consentId}})),
  };
}
