import assert from 'node:assert/strict';
import test from 'node:test';
import {build} from 'esbuild';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {mkdir} from 'node:fs/promises';
import {createElement, isValidElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

const root = fileURLToPath(new URL('../', import.meta.url));
// React stays outside the bundle, so the component and this test share one copy of it.
const directory = fileURLToPath(new URL('../node_modules/.cache/schola-hub-tests/', import.meta.url));
await mkdir(directory, {recursive: true});
const outfile = directory + 'tutor-offer.mjs';
await build({
  entryPoints: [root + 'web/hub/TutorOffer.jsx'], outfile, bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react-dom', 'react/jsx-runtime'], logLevel: 'silent',
});
const {TutorOffer} = await import(pathToFileURL(outfile).href);

const offer = {id: 'offer-1', text: 'Want a nudge on task 3?'};
const markup = props => renderToStaticMarkup(createElement(TutorOffer, props));

/** Every element in a rendered tree, expanding plain function components that take no hooks. */
function elements(node, found = []) {
  if (Array.isArray(node)) { node.forEach(child => elements(child, found)); return found; }
  if (!isValidElement(node)) return found;
  found.push(node);
  elements(node.props.children, found);
  return found;
}
const text = node => (Array.isArray(node) ? node.map(text).join('') : typeof node === 'string' ? node : isValidElement(node) ? text(node.props.children) : '');

test('nothing shows when there is no offer', () => {
  assert.equal(markup({offer: null}), '');
  assert.equal(markup({}), '');
  assert.equal(markup({offer: undefined, muted: false}), '');
});

test('nothing shows when offers are muted, even with an offer', () => {
  assert.equal(markup({offer, muted: true}), '');
});

test('an offer shows as one line of text with Accept, Dismiss and Mute', () => {
  const html = markup({offer, muted: false});
  assert.match(html, /Want a nudge on task 3\?/);
  for (const name of ['Accept', 'Dismiss', 'Mute']) assert.equal((html.match(new RegExp('>' + name + '<', 'g')) ?? []).length, 1, name);
  assert.equal((html.match(/<button/g) ?? []).length, 3);
  assert.doesNotMatch(html, /<br|<p[ >]|<li/);
  assert.match(html, /data-tutor-offer/);
});

test('the offer text is plain text: markup is escaped and hidden characters are shown', () => {
  const html = markup({offer: {id: 'x', text: '<img src=x onerror=alert(1)> \u{202E}evil\u{7}'}});
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /&lt;U\+202E&gt;evil&lt;U\+0007&gt;/);
  assert.ok(![...html].some(char => char.codePointAt(0) < 9 || char === '\u{202e}'), 'no raw control or bidi character in the markup');
});

test('the offer text is not a link and not markdown', () => {
  const html = markup({offer: {id: 'x', text: '[click](https://example.test) https://example.test **bold**'}});
  assert.doesNotMatch(html, /<a[ >]|<strong|<b>|href=/);
  assert.match(html, /\[click\]\(https:\/\/example.test\) https:\/\/example.test \*\*bold\*\*/);
});

test('a long offer is cut with an ellipsis', () => {
  const html = markup({offer: {id: 'x', text: 'a'.repeat(1000)}});
  const shown = /data-tutor-offer-text[^>]*>([^<]*)</.exec(html)[1];
  assert.equal([...shown].length, 160);
  assert.ok(shown.endsWith('a…'));
});

test('each button calls its own handler with the id of the offer, and no other', () => {
  const calls = [];
  const tree = TutorOffer({offer, muted: false, onAccept: id => calls.push(['accept', id]), onDismiss: id => calls.push(['dismiss', id]), onMute: id => calls.push(['mute', id])});
  const buttons = elements(tree).filter(node => typeof node.props.onClick === 'function');
  assert.deepEqual(buttons.map(node => text(node)), ['Accept', 'Dismiss', 'Mute']);
  for (const button of buttons) button.props.onClick();
  assert.deepEqual(calls, [['accept', 'offer-1'], ['dismiss', 'offer-1'], ['mute', 'offer-1']]);
});

test('a button with no handler does nothing and does not throw', () => {
  const tree = TutorOffer({offer});
  for (const button of elements(tree).filter(node => typeof node.props.onClick === 'function')) assert.doesNotThrow(() => button.props.onClick());
});

test('every button is a real button with a name, so a keyboard reaches it', () => {
  const html = markup({offer});
  assert.equal((html.match(/<button[^>]*type="button"/g) ?? []).length, 3);
  assert.match(html, /aria-label="Tutor offer"/);
});
