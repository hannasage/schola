import assert from 'node:assert/strict';
import {openCurriculum} from './open.mjs';
import {createHash} from 'node:crypto';
import {readFile, readdir, stat} from 'node:fs/promises';
import {join} from 'node:path';

const fixtures = new URL('../fixtures/contracts/', import.meta.url);
const fixture = (folder, name) => readFile(new URL(`${folder}/${name}`, fixtures), 'utf8');
const upload = (page, name, text) => page.locator('#curriculum-file').setInputFiles({name, mimeType: 'application/json', buffer: Buffer.isBuffer(text) ? text : Buffer.from(text)});
const uploadMany = (page, files) => page.locator('#curriculum-file').setInputFiles(files.map(([name, text]) => ({name, mimeType: 'application/json', buffer: Buffer.from(text)})));
const notice = page => page.locator('#notice').textContent();
const blocked = page => page.waitForFunction(() => document.querySelector('#lesson-import')?.textContent.includes('Schola cannot add this file'));
const dialogText = page => page.locator('#import-review').innerText();
const get = async (server, path) => (await fetch(`${server.url}${path}`)).json();

/** A hash of every file under a folder, or null when the folder does not exist. */
async function treeHash(folder) {
  try { await stat(folder); } catch { return null; }
  const lines = [];
  const walk = async directory => {
    for (const entry of (await readdir(directory, {withFileTypes: true})).sort((a, b) => a.name.localeCompare(b.name))) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) { lines.push(`dir ${path.slice(folder.length)}`); await walk(path); }
      else lines.push(`${path.slice(folder.length)} ${createHash('sha256').update(await readFile(path)).digest('hex')}`);
    }
  };
  await walk(folder);
  return createHash('sha256').update(lines.join('\n')).digest('hex');
}

/**
 * Module import in a real browser. The page starts on an empty data folder and may only reach its own server.
 * Cancel leaves nothing on disk, Add stores the files, a restart keeps them, and bad files change nothing.
 */
export async function verifyLessonImport({page, server}) {
  const lessonsFolder = join(server.directory, '_lessons');
  const module1 = await fixture('valid', 'lesson-module-minimal.json');
  const packet = await fixture('valid', 'packet-one-module.json');
  const writes = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET' && path.startsWith('/api/lessons')) writes.push(path);
  });
  const writesTo = path => writes.filter(entry => entry === path).length;

  await page.goto(server.url);
  await openCurriculum(page);
  const curriculaBefore = await get(server, '/api/library');
  assert.equal(await treeHash(lessonsFolder), null, 'a new data folder holds no lessons');

  // A packet and its module as two files: review, then Cancel. Nothing may reach the disk.
  const bundle = [['packet.json', packet], ['module.json', module1]];
  await uploadMany(page, bundle);
  await page.locator('#lesson-import').getByRole('button', {name: 'Add these lessons'}).waitFor();
  let review = await dialogText(page);
  assert.match(review, /Add the lesson set "A packet with one module"/);
  assert.match(review, /A minimal lesson module/);
  assert.match(review, /Schola would use this version/);
  assert.equal(await treeHash(lessonsFolder), null, 'the review writes nothing');
  assert.equal(writesTo('/api/lessons/import/preview'), 1);
  await page.locator('#lesson-import').getByRole('button', {name: 'Cancel'}).click();
  await page.waitForFunction(() => !document.querySelector('#import-review').open);
  assert.equal(await treeHash(lessonsFolder), null, 'Cancel leaves nothing on disk');
  assert.equal(writesTo('/api/lessons/import'), 0, 'Cancel makes no import call');
  assert.deepEqual((await get(server, '/api/lessons')).modules, []);

  // The same file again, this time confirmed.
  await uploadMany(page, bundle);
  await page.locator('#lesson-import').getByRole('button', {name: 'Add these lessons'}).click();
  await page.locator('#lesson-list').waitFor();
  assert.match(await dialogText(page), /Added 1 lesson/);
  assert.match(await dialogText(page), /You use version 1\.0\.0/);
  assert.equal(writesTo('/api/lessons/import'), 1);
  const stored = join(lessonsFolder, 'modules', 'hello-module', '1.0.0', 'module.json');
  assert.equal(JSON.parse(await readFile(stored, 'utf8')).id, 'hello-module');
  await page.locator('#lesson-import').getByRole('button', {name: 'Close'}).click();
  await page.waitForFunction(() => !document.querySelector('#import-review').open);

  // A restart keeps the modules.
  const afterImport = await treeHash(lessonsFolder);
  await server.restart();
  await page.reload();
  await openCurriculum(page);
  const listing = await get(server, '/api/lessons');
  assert.deepEqual(listing.modules.map(item => [item.id, item.pinnedVersion]), [['hello-module', '1.0.0']]);
  assert.equal(await treeHash(lessonsFolder), afterImport, 'a restart changes no lesson file');

  // A damaged file shows an error and changes nothing.
  for (const [folder, name] of [['invalid', 'lesson-module-damaged-truncated.json'], ['invalid', 'lesson-module-extra-property.json']]) {
    await upload(page, name, await fixture(folder, name));
    await blocked(page);
    review = await dialogText(page);
    assert.match(review, /Schola cannot add this file\. Nothing was changed\./, name);
    assert.equal(await page.locator('#lesson-import').getByRole('button', {name: 'Add these lessons'}).isDisabled(), true, 'a bad file cannot be confirmed');
    await page.locator('#lesson-import').getByRole('button', {name: 'Cancel'}).click();
    await page.waitForFunction(() => !document.querySelector('#import-review').open);
    assert.equal(await treeHash(lessonsFolder), afterImport, `${name} changed nothing`);
  }
  assert.equal(writesTo('/api/lessons/import'), 1, 'a bad file never reaches the import route');

  // The same id and version with different bytes is a conflict.
  const changed = JSON.parse(module1);
  changed.title = 'The same lesson with other words';
  await upload(page, 'changed.json', JSON.stringify(changed));
  await blocked(page);
  review = await dialogText(page);
  assert.match(review, /already exists with different content/);
  assert.match(review, /will not replace it/);
  assert.equal(await page.locator('#lesson-import').getByRole('button', {name: 'Add these lessons'}).isDisabled(), true, 'a conflict cannot be confirmed');
  await page.locator('#lesson-import').getByRole('button', {name: 'Cancel'}).click();
  await page.waitForFunction(() => !document.querySelector('#import-review').open);
  assert.equal(await treeHash(lessonsFolder), afterImport, 'a conflict changed nothing');

  // A file that is not valid UTF-8 is refused before any call. A mix of lesson and other files is refused too.
  const calls = writes.length;
  const brokenBytes = Buffer.concat([Buffer.from(module1.replace('A minimal lesson module', 'A minimal ')), Buffer.from([0xff, 0xfe]), Buffer.from(' lesson module')]);
  await upload(page, 'broken.json', brokenBytes);
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('not valid UTF-8'));
  assert.equal(await page.locator('#import-review').isVisible(), false);
  await upload(page, 'bytes.json', Buffer.from([0x7b, 0x22, 0xff, 0x22, 0x7d]));
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('bytes.json is not valid UTF-8'));
  await uploadMany(page, [['module.json', module1], ['course.json', JSON.stringify({id: 'x', title: 'x'})]]);
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('Several files work only when all of them are lesson files'));
  assert.equal(await page.locator('#import-review').isVisible(), false);
  assert.equal(writes.length, calls, 'none of these made a lesson call');
  assert.equal(await treeHash(lessonsFolder), afterImport);

  // A file under 5 MB whose request body passes 5,000,000 bytes (every quote doubles twice) is refused on the page, before any call.
  const huge = JSON.stringify({schemaVersion: '1.0', id: 'huge-module', version: '1.0.0', title: 'Huge', filler: '"'.repeat(300_000) + 'a'.repeat(3_900_000)});
  assert.ok(Buffer.byteLength(huge) > 4_400_000 && Buffer.byteLength(huge) < 5_000_000, 'the file itself is about 4.5 MB');
  await upload(page, 'huge.json', huge);
  await page.waitForFunction(() => document.querySelector('#lesson-import')?.textContent.includes('too large to send'));
  review = await dialogText(page);
  assert.match(review, /5 MB/);
  assert.match(review, /2 MB/);
  assert.equal(writes.length, calls, 'the page sent nothing');
  await page.locator('#lesson-import').getByRole('button', {name: 'Close'}).click();
  await page.waitForFunction(() => !document.querySelector('#import-review').open);
  assert.equal(await treeHash(lessonsFolder), afterImport);
  assert.equal(await notice(page).then(Boolean), true);

  // A newer version is stored and only offered. The pin moves after a second, explicit step.
  const newer = JSON.parse(module1);
  newer.version = '1.1.0';
  await upload(page, 'newer.json', JSON.stringify(newer));
  await page.locator('#lesson-import').getByRole('button', {name: 'Add these lessons'}).waitFor();
  assert.match(await dialogText(page), /Version 1\.1\.0 of .* is available\. You use version 1\.0\.0\. Nothing changes unless you choose to switch\./);
  await page.locator('#lesson-import').getByRole('button', {name: 'Add these lessons'}).click();
  await page.locator('#lesson-list').waitFor();
  assert.equal((await get(server, '/api/lessons')).modules[0].pinnedVersion, '1.0.0', 'adding a version does not move the pin');
  assert.equal((await get(server, '/api/lessons')).modules[0].newerVersionAvailable, '1.1.0');
  await page.locator('#lesson-import').getByRole('button', {name: 'Review the switch'}).click();
  assert.match(await dialogText(page), /Switch to version 1\.1\.0/);
  assert.equal(writesTo('/api/lessons/pins'), 0, 'the review step moves nothing');
  await page.locator('#lesson-import').getByRole('button', {name: 'Keep version 1.0.0'}).click();
  assert.equal(writesTo('/api/lessons/pins'), 0, 'declining moves nothing');
  assert.equal((await get(server, '/api/lessons')).modules[0].pinnedVersion, '1.0.0');
  await page.locator('#lesson-import').getByRole('button', {name: 'Review the switch'}).click();
  await page.locator('#lesson-import').getByRole('button', {name: 'Switch to version 1.1.0'}).click();
  await page.waitForFunction(() => document.querySelector('#lesson-import').textContent.includes('You now use version 1.1.0'));
  assert.equal(writesTo('/api/lessons/pins'), 1);
  assert.equal((await get(server, '/api/lessons')).modules[0].pinnedVersion, '1.1.0');
  await page.locator('#lesson-import').getByRole('button', {name: 'Close'}).click();
  await page.waitForFunction(() => !document.querySelector('#import-review').open);

  // Curricula and progress files keep the older review and the older routes.
  const before = await get(server, '/api/library');
  assert.deepEqual(before.records.map(item => item.recordId), curriculaBefore.records.map(item => item.recordId), 'lessons never touch the curriculum library');
  const record = await get(server, `/api/records/${before.records[0].recordId}`);
  const curriculum = {...structuredClone(record.curriculum), id: 'lesson-suite-course', title: 'Curriculum beside lessons'};
  const importsBefore = writes.length;
  await upload(page, 'course.json', JSON.stringify(curriculum));
  await page.locator('#confirm-import').waitFor();
  assert.equal(await page.locator('#lesson-import').count(), 0, 'a curriculum does not use the lesson review');
  await page.getByRole('button', {name: 'Cancel', exact: true}).click();
  assert.equal((await get(server, '/api/library')).records.length, before.records.length);
  await upload(page, 'course.json', JSON.stringify(curriculum));
  await page.locator('#confirm-import').click();
  await page.waitForFunction(() => !document.querySelector('#import-review').open);
  const after = await get(server, '/api/library');
  assert.ok(after.records.some(item => item.curriculumId === curriculum.id), 'the curriculum was imported');
  assert.equal(writes.length, importsBefore, 'a curriculum import makes no lesson call');
  console.log('PASS: lesson import is read-only until confirmed; Cancel, damaged files, and conflicts leave the disk unchanged; modules survive a restart; a newer version is only offered; curriculum import is unchanged.');
}
