import assert from 'node:assert/strict';
import test from 'node:test';
import {safeText} from '../web/hub/text.js';

test('plain text passes through unchanged', () => {
  assert.equal(safeText('Expected goals: a first model'), 'Expected goals: a first model');
  assert.equal(safeText('Café, naïve, 日本語, 😀'), 'Café, naïve, 日本語, 😀');
  assert.equal(safeText(''), '');
});

test('control characters show as their code, in the same form as the command line', () => {
  assert.equal(safeText('a\u0007b'), 'a<U+0007>b');
  assert.equal(safeText('a\u001b[31mred'), 'a<U+001B>[31mred');
  assert.equal(safeText('a\u0000b\u007fc\u0085d'), 'a<U+0000>b<U+007F>c<U+0085>d');
  assert.equal(safeText('line1\nline2'), 'line1<U+000A>line2');
  assert.equal(safeText('tab\there'), 'tab<U+0009>here');
});

test('the whole control range is shown, and the space is not', () => {
  assert.equal(safeText('a\u{1f}b c'), 'a<U+001F>b c');
  assert.equal(safeText('\u{7e}\u{7f}'), '~<U+007F>');
});

test('bidirectional controls, zero-width, tag, private use and look-alike blanks show as their code', () => {
  assert.equal(safeText('evil\u{202E}txt.exe'), 'evil<U+202E>txt.exe');
  assert.equal(safeText('a\u{2066}b\u{2069}c'), 'a<U+2066>b<U+2069>c');
  assert.equal(safeText('a\u{200B}b\u{200D}c\u{FEFF}d'), 'a<U+200B>b<U+200D>c<U+FEFF>d');
  assert.equal(safeText('a\u{E0041}b'), 'a<U+E0041>b');
  assert.equal(safeText('a\u{E000}b'), 'a<U+E000>b');
  assert.equal(safeText('a\u{A0}b\u{3164}c\u{2800}d'), 'a<U+00A0>b<U+3164>c<U+2800>d');
  assert.equal(safeText('a\u{2028}b\u{2029}c'), 'a<U+2028>b<U+2029>c');
  assert.equal(safeText('a\u{FE0F}b\u{180E}c'), 'a<U+FE0F>b<U+180E>c');
});

test('a lone surrogate shows as its code', () => {
  assert.equal(safeText('a\uD800b'), 'a<U+D800>b');
  assert.equal(safeText('a\uDC00b'), 'a<U+DC00>b');
});

test('a run of more than two combining marks is cut and the cut says so', () => {
  const marks = '\u{301}\u{302}\u{303}\u{304}\u{305}';
  assert.equal(safeText('e' + marks), 'e\u{301}\u{302}<3 marks cut>');
  assert.equal(safeText('e\u{301}\u{302}x'), 'e\u{301}\u{302}x');
});

test('a value that is not text is named, not shown', () => {
  for (const value of [undefined, null, 5, {}, [], true]) assert.equal(safeText(value), '<not text>');
});

test('long text is cut with an ellipsis, never in the middle of a code, never over the limit', () => {
  assert.equal(safeText('abcdefghij', {limit: 10}), 'abcdefghij');
  assert.equal(safeText('abcdefghijk', {limit: 10}), 'abcdefghi…');
  assert.equal(safeText('abc\u{202E}def', {limit: 8}), 'abc…');
  assert.equal(safeText('abc\u{202E}def', {limit: 12}), 'abc<U+202E>…');
  assert.ok(safeText('😀'.repeat(50), {limit: 10}).length <= 20);
  assert.equal([...safeText('😀'.repeat(50), {limit: 10})].length, 10);
  assert.equal(safeText('x'.repeat(5000)).length, 200);
  assert.ok(safeText('x'.repeat(5000)).endsWith('…'));
});

test('a limit of zero or less shows an ellipsis only', () => {
  assert.equal(safeText('abc', {limit: 0}), '…');
  assert.equal(safeText('', {limit: 0}), '');
});

test('multiline text keeps line breaks and nothing else that is hidden', () => {
  assert.equal(safeText('one\ntwo\r\nthree', {multiline: true, limit: 100}), 'one\ntwo\nthree');
  assert.equal(safeText('one\rtwo', {multiline: true, limit: 100}), 'one<U+000D>two');
  assert.equal(safeText('one\u{2028}two\u0007', {multiline: true, limit: 100}), 'one<U+2028>two<U+0007>');
  assert.equal(safeText('a\n\n\n\n\nb', {multiline: true, limit: 100}), 'a\n\nb');
});

test('markup stays text: the helper never adds or removes angle brackets', () => {
  assert.equal(safeText('<img src=x onerror=alert(1)>'), '<img src=x onerror=alert(1)>');
});
