import assert from 'node:assert/strict';
import {openCurriculum} from './open.mjs';

async function saveNotes(page, notes) {
  const saved = page.waitForResponse(response =>
    response.request().method() === 'PUT' && new URL(response.url()).pathname.startsWith('/api/records/'));
  await page.locator('#notes').fill(notes);
  await page.locator('#save-module').click();
  assert.equal((await saved).status(), 200);
  await page.waitForFunction(() => document.querySelector('#save-state').textContent === 'Saved on this computer');
}

export async function verifyPersistence({page, server}) {
  await page.goto(server.url);
  await openCurriculum(page);
  assert.deepEqual(await page.evaluate(async () => (await fetch('/api/cloud-config')).json()), {});
  const moduleId = await page.locator('.module.selected').getAttribute('data-module');
  const firstNotes = 'First course: a real saved note, with π and <angle>.';
  await saveNotes(page, firstNotes);
  const firstRecordId=await page.locator('#curriculum-select').inputValue();
  const disk=await (await fetch(`${server.url}/api/records/${firstRecordId}`)).json();
  assert.equal(disk.state.modules[moduleId].notes,firstNotes);
  await server.restart();
  await page.reload();
  await openCurriculum(page);
  assert.equal(await page.locator('#notes').inputValue(), firstNotes);

  const curriculum = await (await fetch(`${server.url}/curriculum.json`)).json();
  const second = {...curriculum, id: 'browser-second-course', title: 'Second isolated course'};
  const imported = page.waitForResponse(response =>
    response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/import');
  await page.locator('#curriculum-file').setInputFiles({
    name: 'second-course.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(second)),
  });
  await page.locator('#confirm-import').click();
  assert.equal((await imported).status(), 201);
  await page.waitForFunction(()=>document.querySelectorAll('#curriculum-select option').length===2);
  const secondRecordId=await page.locator('#curriculum-select option').filter({hasText:'Second isolated course'}).getAttribute('value');
  await page.locator('#curriculum-select').selectOption(secondRecordId);
  await page.waitForFunction(() => document.querySelector('#plan-title').textContent === 'Second isolated course');
  assert.equal(await page.locator('#notes').inputValue(), '');
  await saveNotes(page, 'Second course: a different saved note.');
  await page.locator('#curriculum-select').selectOption(firstRecordId);
  await page.waitForFunction(expected => document.querySelector('#notes').value === expected, firstNotes);
  assert.equal(await page.locator('#notes').inputValue(), firstNotes);
  const otherDisk=await (await fetch(`${server.url}/api/records/${secondRecordId}`)).json();
  assert.equal(otherDisk.state.modules[moduleId].notes, 'Second course: a different saved note.');
  console.log('PASS: notes persist on disk after restart; imported curricula keep separate learning records.');
}

export async function verifyStaleWrite(server) {
  const address = `${server.url}/api/state?plan=soccer-ml`;
  const previous = await (await fetch(address)).json();
  const write = state => fetch(address, {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(state),
  });
  const accepted = await write({...previous, weeklyHours: 8});
  assert.equal(accepted.status, 200);
  const latest = await accepted.json();
  const rejected = await write({...previous, weeklyHours: 9});
  assert.equal(rejected.status, 409);
  assert.match((await rejected.json()).error, /newer (?:progress|changes)/i);
  assert.deepEqual(await (await fetch(address)).json(), latest);
  console.log('PASS: a stale writer receives a conflict and cannot overwrite newer progress.');
}
