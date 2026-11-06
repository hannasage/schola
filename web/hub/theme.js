/**
 * The Bench theme: a flat, sharp, dark Projection UI theme for the hub screen.
 * The hub is dark only. It does not follow the appearance setting of the rest of the app.
 */

/** Plain colours of the Bench palette, for the stylesheet and the tests. */
export const BENCH = {
  bg: '#0E1114',
  panel: '#14181C',
  raised: '#1A1F25',
  line: '#2B333B',
  text: '#DCE2E8',
  soft: '#B3BDC7',
  /** The readout grey. */
  muted: '#93A0AC',
  amber: '#F0B23E',
  onAmber: '#14120A',
  heatNone: '#1F252C',
  heatOne: '#6E5420',
  heatMany: '#F0B23E',
};

// The family names are the hub's own, so no other screen picks up these fonts by name.
const SANS = "'Schola Hub Plex Sans', 'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif";
const MONO = "'Schola Hub Plex Mono', 'IBM Plex Mono', ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace";

/** @type {import('@hannasage/projection-ui/core').UITheme} */
export const benchTheme = {
  bg: BENCH.bg,
  surface: BENCH.panel,
  border: BENCH.line,
  text: BENCH.text,
  muted: BENCH.muted,
  primary: BENCH.amber,
  primaryFg: BENCH.onAmber,
  danger: '#F28B82',
  font: SANS,
  fontBody: SANS,
  fontDisplay: SANS,
  fontMono: MONO,
  radius: 'sharp',
  appearance: 'flat',
  mode: 'dark',
  success: '#7FD6A0',
  warning: BENCH.amber,
  focus: BENCH.amber,
  backdrop: 'rgba(14, 17, 20, 0.72)',
};
