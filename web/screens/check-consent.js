import {CheckServiceError} from '../services/checks.js';

const AGREE = 'Agree and allow this script';
const MAX_LINE = 300;

/** Server text is untrusted. Keep a one-line field plain and short. @param {unknown} value @param {number} [limit] */
const line = (value, limit = MAX_LINE) => (typeof value === 'string' || typeof value === 'number' ? String(value) : '').replace(/[\p{Cc}\p{Cf}]+/gu, ' ').trim().slice(0, limit);

/** @param {string} ch */
const tag = ch => `<U+${(ch.codePointAt(0) ?? 0).toString(16).toUpperCase().padStart(4, '0')}>`;

/**
 * Text as the student reads it. Every control, invisible, direction, line-separator, and lone-surrogate character becomes a
 * visible tag such as <U+202E>, so it cannot hide code. Only the line feed stays as a line break, and the tab stays as a tab,
 * because they hide nothing. The hash still covers the real bytes.
 * @param {unknown} value
 * @param {boolean} [keepBreaks] keep line feeds and tabs as they are
 */
export function visibleText(value, keepBreaks = true) {
  if (typeof value !== 'string') return '';
  return value.replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Cs}]/gu, ch => keepBreaks && (ch === '\n' || ch === '\t') ? ch : tag(ch));
}

/** The script as the student reads it. @param {unknown} source */
export const visibleSource = source => visibleText(source, true);

/** @param {unknown} list @returns {string[]} */
const strings = list => Array.isArray(list) ? list.filter(item => typeof item === 'string') : [];

/**
 * The consent screen for a script check. It shows exactly what would run and asks the student to agree.
 * Opening it reads one inspection from the server. Cancel and Escape make no further call. Only the Agree button
 * asks the server to record consent, and it never runs the script. A run is a separate step for the caller.
 * @param {HTMLDialogElement} dialog
 * @param {{service: CheckService, ref: CheckRef, onFinish?: (outcome: CheckConsentOutcome) => void}} options
 * @returns {() => void} a function that closes the dialog
 */
export function openCheckConsent(dialog, {service, ref, onFinish}) {
  const previous = document.activeElement;
  /** @type {'loading' | 'ready' | 'saving' | 'granted' | 'failed'} */
  let phase = 'loading';
  /** @type {CheckInspection | null} */ let shown = null;
  /** @type {string} */ let notice = '';
  /** @type {string} */ let consentId = '';
  let closed = false;

  /** @param {string} tag @param {string} [content] @param {string} [className] */
  const element = (tag, content = '', className = '') => {
    const node = document.createElement(tag);
    if (content) node.textContent = content;
    if (className) node.className = className;
    return node;
  };
  /** @param {string} label @param {string} value */
  const row = (label, value) => {
    const paragraph = element('p');
    paragraph.append(element('strong', `${label}: `), element('span', value));
    return paragraph;
  };
  /** @param {string} label @param {() => void} handler @param {boolean} [disabled] */
  const button = (label, handler, disabled = false) => {
    const node = element('button', label);
    node.setAttribute('type', 'button');
    if (disabled) node.setAttribute('disabled', 'disabled');
    else node.addEventListener('click', handler);
    return node;
  };

  const close = (/** @type {CheckConsentOutcome} */ outcome) => {
    if (closed) return;
    closed = true;
    dialog.close();
    dialog.replaceChildren();
    if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    onFinish?.(outcome);
  };
  const cancel = () => { if (phase !== 'saving') close({consented: false}); };

  /** @returns {Promise<boolean>} whether the check was read */
  async function inspect() {
    try {
      shown = await service.inspect(ref);
      phase = 'ready';
      return true;
    } catch (problem) {
      shown = null;
      phase = 'failed';
      notice = problem instanceof Error ? problem.message : 'Schola could not load this check.';
      return false;
    }
  }

  async function agree() {
    if (phase !== 'ready' || !shown) return;
    const hash = shown.checkHash;
    phase = 'saving';
    draw();
    try {
      const granted = await service.grantConsent(ref, {confirm: true, shownHash: hash});
      consentId = granted.consentId;
      phase = 'granted';
      notice = '';
    } catch (problem) {
      notice = problem instanceof Error ? problem.message : 'Schola could not save your consent.';
      // A changed script must be read again. Fetch it, show it, and keep the old consent out of it.
      if (problem instanceof CheckServiceError && problem.code === 'hash_mismatch') {
        const changedNotice = notice;
        if (await inspect()) notice = changedNotice;
      } else phase = 'ready';
    }
    if (!closed) draw();
  }

  function draw() {
    const heading = element('div', '', 'import-heading');
    const title = element('h1', 'Run this script?');
    title.setAttribute('id', 'check-consent-title');
    heading.append(title);
    const body = element('div');
    body.setAttribute('id', 'check-consent');
    if (phase === 'loading') body.append(element('p', 'Loading the check.'));
    if (phase === 'failed') {
      body.append(alert(notice), button('Close', () => close({consented: false})));
    }
    if (phase === 'ready' || phase === 'saving') {
      if (shown && !shown.needsConsent) {
        body.append(element('p', 'This check runs no script. It needs no consent.'), button('Close', () => close({consented: false})));
      } else if (shown) {
        const busy = phase === 'saving';
        const args = strings(shown.args);
        const source = element('pre', visibleSource(shown.source), 'consent-source');
        source.setAttribute('tabindex', '0');
        const nonAscii = [...`${shown.source ?? ''}${args.join('')}`].some(ch => (ch.codePointAt(0) ?? 0) > 127);
        const argumentList = element('ul', '', 'consent-args');
        for (const item of args) argumentList.append(element('li', visibleText(item, false), 'consent-arg'));
        const applied = strings(shown.limits).map(item => element('li', visibleText(item, false)));
        const notApplied = [...strings(shown.limitsNotApplied)];
        if (!notApplied.some(item => item.toLowerCase().startsWith('memory'))) notApplied.unshift('memory: no memory limit is set');
        const limitList = element('ul');
        limitList.append(...applied);
        const missingList = element('ul');
        missingList.append(...notApplied.map(item => element('li', visibleText(item, false))));
        body.append(
          element('p', 'A lesson author wrote this script. If you agree, Schola runs it on this computer when you run the check.'),
          element('p', 'This is consent, not a sandbox. A script you allow can still read your files and use the network.', 'import-warning'),
          ...(nonAscii ? [element('p', 'This script or its arguments hold characters outside plain ASCII. Look for letters that only look like other letters.', 'import-conflict')] : []),
          row('Runtime', line(shown.runtime, 40) || 'Not named'),
          element('p', 'Arguments, one to a line:'),
          args.length ? argumentList : element('p', 'None'),
          row('Time limit', `${Number.isSafeInteger(shown.timeoutSeconds) ? shown.timeoutSeconds : 30} seconds`),
          row('Your folder', line(shown.workspace, 1000)),
          row('SHA-256 of what runs', line(shown.checkHash, 64)),
          element('h2', 'Limits that apply'),
          limitList,
          element('h2', 'Limits that do not apply'),
          missingList,
          element('h2', 'Script'),
          source,
        );
        if (notice) body.append(alert(notice));
        const actions = element('div', '', 'learning-actions');
        actions.append(button(AGREE, () => { void agree(); }, busy), button('Cancel', cancel, busy));
        body.append(actions);
      }
    }
    if (phase === 'granted') {
      body.append(element('p', 'Your consent is saved until you close Schola. The script has not run yet.'), button('Close', () => close({consented: true, consentId})));
    }
    dialog.replaceChildren(heading, body);
  }

  /** @param {string} text */
  function alert(text) {
    const node = element('p', text, 'import-conflict');
    node.setAttribute('role', 'alert');
    return node;
  }

  dialog.oncancel = event => {
    event.preventDefault();
    if (phase === 'granted' || phase === 'failed') close({consented: phase === 'granted', ...(phase === 'granted' ? {consentId} : {})});
    else cancel();
  };
  dialog.setAttribute('aria-labelledby', 'check-consent-title');
  dialog.showModal();
  draw();
  void inspect().then(() => { if (!closed) draw(); });
  return () => close({consented: false});
}
