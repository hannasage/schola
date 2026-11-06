import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {createIsolatedServer} from './browser/server.mjs';
import {isolateContext} from './browser/network.mjs';
import {verifyPersistence, verifyStaleWrite} from './browser/persistence.mjs';

import {verifyDrafts, verifyRecordScopes, verifyAccountInvalidation} from './browser/drafts.mjs';

import {verifyConnection} from './browser/connection.mjs';
import {verifyPublications} from './browser/publications.mjs';

import {verifyLearningScreens,verifyRecoveredStartup,verifyStatusWriterConflict,verifyEditsDuringStatusSave} from './browser/learning-screens.mjs';
import {verifyJournalImport} from './browser/journal-import.mjs';
import {verifyLessonImport} from './browser/lessons.mjs';
import {verifyShell,verifyStartupRecovery} from './browser/shell.mjs';
import {verifyHub} from './browser/hub.mjs';

const server = await createIsolatedServer();
let browser;
try {
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
  });
  const context = await browser.newContext({serviceWorkers: 'block'});
  const externalRequests = await isolateContext(context, server.url);
  const errors = [];
  context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
  const page = await context.newPage();
  await verifyPersistence({page, server});
  await verifyStaleWrite(server);
  await verifyDrafts({page, server});
  await verifyRecordScopes({page, server});
  await verifyAccountInvalidation({page, server});
  await verifyShell({page,server});
  await verifyStartupRecovery({page,server});
  await verifyConnection({page,server});
  const publicationsServer=await createIsolatedServer();
  const publicationsContext=await browser.newContext({serviceWorkers:'block'});
  try {
    await isolateContext(publicationsContext,publicationsServer.url);
    const publicationsPage=await publicationsContext.newPage();
    publicationsPage.on('pageerror',error=>errors.push(error.message));
    await verifyPublications({page:publicationsPage,server:publicationsServer});
  } finally {await publicationsContext.close();await publicationsServer.close();}
  const lessonsServer=await createIsolatedServer();
  const lessonsContext=await browser.newContext({serviceWorkers:'block'});
  try {
    const lessonRequests=await isolateContext(lessonsContext,lessonsServer.url);
    const lessonsPage=await lessonsContext.newPage();
    lessonsPage.on('pageerror',error=>errors.push(error.message));
    await verifyLessonImport({page:lessonsPage,server:lessonsServer});
    assert.deepEqual(lessonRequests,[],'lesson import reaches no external address');
  } finally {await lessonsContext.close();await lessonsServer.close();}
  await verifyLearningScreens({page,server});
  await verifyRecoveredStartup({page,server});
  await verifyStatusWriterConflict({page,server});
  await verifyEditsDuringStatusSave({page,server});
  await verifyJournalImport({page,server});
  await verifyHub({browser});
  const blocked = await page.evaluate(async () => {
    try { await fetch('https://touchline-test-egress.invalid/probe'); return false; }
    catch { return true; }
  });
  assert.equal(blocked, true);
  assert.ok(externalRequests.includes('https://touchline-test-egress.invalid'));
  assert.deepEqual(errors, []);
  console.log('PASS: browser requests cannot reach an external backend; private cloud configuration stays suppressed.');
} finally {
  try { await browser?.close(); }
  finally { await server.close(); }
}
