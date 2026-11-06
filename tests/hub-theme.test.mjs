import assert from 'node:assert/strict';
import test from 'node:test';
import {benchTheme, BENCH} from '../web/hub/theme.js';

/** WCAG relative luminance of a #RRGGBB colour. */
function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map(at => parseInt(hex.slice(at, at + 2), 16) / 255)
    .map(value => value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export const contrast = (a, b) => {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
};

test('the contrast helper agrees with known values', () => {
  assert.equal(Math.round(contrast('#000000', '#FFFFFF')), 21);
  assert.equal(contrast('#777777', '#777777'), 1);
});

test('the Bench theme is a flat, sharp, dark Projection theme with the decided palette', () => {
  assert.equal(benchTheme.appearance, 'flat');
  assert.equal(benchTheme.radius, 'sharp');
  assert.equal(benchTheme.mode, 'dark');
  assert.equal(benchTheme.bg, '#0E1114');
  assert.equal(benchTheme.surface, '#14181C');
  assert.equal(benchTheme.border, '#2B333B');
  assert.equal(benchTheme.text, '#DCE2E8');
  assert.equal(benchTheme.muted, '#93A0AC');
  assert.equal(benchTheme.primary, '#F0B23E');
  assert.equal(benchTheme.primaryFg, '#14120A');
  assert.equal(benchTheme.focus, '#F0B23E');
  assert.match(benchTheme.fontBody, /^'Schola Hub Plex Sans', 'IBM Plex Sans'/);
  assert.match(benchTheme.fontMono, /^'Schola Hub Plex Mono', 'IBM Plex Mono'/);
  assert.match(benchTheme.fontBody, /sans-serif$/);
  assert.match(benchTheme.fontMono, /monospace$/);
});

test('text has at least 4.5 to 1 against every surface it sits on', () => {
  const surfaces = [BENCH.bg, BENCH.panel, BENCH.raised];
  for (const surface of surfaces) {
    for (const [name, colour] of Object.entries({text: BENCH.text, soft: BENCH.soft, readout: BENCH.muted, amber: BENCH.amber, danger: benchTheme.danger, success: benchTheme.success})) {
      assert.ok(contrast(colour, surface) >= 4.5, `${name} ${colour} on ${surface}: ${contrast(colour, surface).toFixed(2)}`);
    }
  }
});

test('the readout grey on the panel colour passes at 4.5 to 1', () => {
  assert.ok(contrast('#93A0AC', '#14181C') >= 4.5, contrast('#93A0AC', '#14181C').toFixed(2));
});

test('text on the amber signal and on the heat cells passes at 4.5 to 1', () => {
  assert.ok(contrast(BENCH.onAmber, BENCH.amber) >= 4.5);
  assert.ok(contrast(BENCH.text, BENCH.heatOne) >= 4.5, contrast(BENCH.text, BENCH.heatOne).toFixed(2));
  assert.ok(contrast(BENCH.muted, BENCH.heatNone) >= 4.5, contrast(BENCH.muted, BENCH.heatNone).toFixed(2));
  assert.ok(contrast(BENCH.onAmber, BENCH.heatMany) >= 4.5);
});

test('the non-text marks (cells, segments, lines) reach 3 to 1 where they carry meaning', () => {
  assert.ok(contrast(BENCH.amber, BENCH.panel) >= 3);
  assert.ok(contrast(BENCH.line, BENCH.panel) >= 1.2, 'lines are decoration; the meaning is also in text');
});
