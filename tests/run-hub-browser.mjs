// Runs only the Hub screen checks. `npm run test:browser` runs these too, after the older checks.
// Set SCHOLA_HUB_ARTIFACTS to a folder to also save a screenshot at each width.
import {chromium} from 'playwright';
import {verifyHub} from './browser/hub.mjs';

const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined});
try {
  await verifyHub({browser, artifacts: process.env.SCHOLA_HUB_ARTIFACTS || undefined});
  console.log('PASS: the Hub screen shows each state, escapes lesson text, and fits 375, 768 and 1280 pixels.');
} finally { await browser.close(); }
