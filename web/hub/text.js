/**
 * Untrusted text from a lesson author, made safe to show.
 *
 * This is the same rule as `safe_text` in `schola_cli.py`. Control characters, bidirectional controls,
 * zero-width characters, tag characters, private use characters, look-alike blanks, and lone surrogates
 * show as `<U+XXXX>`. A run of more than two combining marks is cut. Long text ends in an ellipsis.
 *
 * The result is plain text. Draw it only as a React text node. It is never markup.
 */

export const MAX_MARK_RUN = 2;
const ELLIPSIS = '…';
const NOT_TEXT = '<not text>';
/** Characters that are not control characters but still hide text or break a line. */
const HIDDEN_CODES = new Set([0x00a0, 0x034f, 0x115f, 0x1160, 0x17b4, 0x17b5, 0x2800, 0x3164, 0xffa0]);
/** Controls, format characters, surrogates, private use, line and paragraph separators, unassigned code points, and default-ignorable code points. */
const HIDDEN_CATEGORY = /^[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}\p{Cn}\p{Default_Ignorable_Code_Point}]$/u;
/** Every space-like character except the plain space. */
const SPACE_LIKE = /^\p{Zs}$/u;
const OBJECT_REPLACEMENT = 0xfffc;
const TAB_SPACES = '    ';
const MARK = /^\p{M}$/u;

/** @param {string} char */
function isHidden(char) {
  const code = /** @type {number} */(char.codePointAt(0));
  if (code < 0x80) return code < 0x20 || code === 0x7f;
  if (code === OBJECT_REPLACEMENT || SPACE_LIKE.test(char)) return true;
  if (HIDDEN_CODES.has(code) || (code >= 0x180b && code <= 0x180f) || (code >= 0xfe00 && code <= 0xfe0f) || (code >= 0xe0100 && code <= 0xe01ef)) return true;
  return HIDDEN_CATEGORY.test(char);
}

/** @param {string} char */
function show(char) {
  return '<U+' + /** @type {number} */(char.codePointAt(0)).toString(16).toUpperCase().padStart(4, '0') + '>';
}

/**
 * The pieces of displayed text, in order. Each piece is one character or one marker.
 * @param {string} value
 * @param {boolean} multiline
 * @returns {string[]}
 */
function pieces(value, multiline) {
  /** @type {string[]} */
  const result = [];
  let run = 0;
  let cut = 0;
  let newlines = 0;
  const flush = () => { if (cut) { result.push(`<${cut} marks cut>`); cut = 0; } };
  const chars = [...value];
  for (let index = 0; index < chars.length; index += 1) {
    const char = chars[index];
    if (multiline && char === '\r' && chars[index + 1] === '\n') continue;
    if (multiline && char === '\t') {
      flush();
      run = 0;
      newlines = 0;
      result.push(TAB_SPACES);
      continue;
    }
    if (multiline && char === '\n') {
      flush();
      run = 0;
      newlines += 1;
      // At most one blank line in a row.
      if (newlines <= 2) result.push('\n');
      continue;
    }
    newlines = 0;
    if (MARK.test(char) && !isHidden(char)) {
      run += 1;
      if (run > MAX_MARK_RUN) { cut += 1; continue; }
      result.push(char);
      continue;
    }
    flush();
    run = 0;
    result.push(isHidden(char) ? show(char) : char);
  }
  flush();
  return result;
}

/**
 * @param {unknown} value
 * @param {{limit?: number, multiline?: boolean}} [options] `limit` counts shown characters, an escape counts for all of its characters.
 * @returns {string}
 */
export function safeText(value, {limit = 200, multiline = false} = {}) {
  if (typeof value !== 'string') return NOT_TEXT;
  const all = pieces(value, multiline);
  const sizes = all.map(piece => [...piece].length);
  const total = sizes.reduce((sum, size) => sum + size, 0);
  if (total <= limit) return all.join('');
  const room = Math.max(limit - 1, 0);
  let size = 0;
  let text = '';
  for (let index = 0; index < all.length; index += 1) {
    if (size + sizes[index] > room) break;
    size += sizes[index];
    text += all[index];
  }
  return text + ELLIPSIS;
}

/**
 * Like `safeText`, but a value with nothing visible in it shows `(blank)`, so no row is ever empty.
 * @param {unknown} value
 * @param {{limit?: number, multiline?: boolean}} [options]
 */
export function displayText(value, options) {
  const text = safeText(value, options);
  return text.trim() === '' ? '(blank)' : text;
}
