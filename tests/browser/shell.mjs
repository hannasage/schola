import assert from 'node:assert/strict';
import {openCurriculum} from './open.mjs';

export async function verifyShell({page,server}) {
  await page.goto(server.url);
  await openCurriculum(page);
  await page.route('**/api/records/*', route => route.request().method()==='PUT'
    ? route.fulfill({status:503,json:{error:'The save is unavailable.',code:'storage_unavailable',retryable:true}})
    : route.continue());
  await page.locator('#notes').fill('A navigation failure must retain this note.');
  await page.locator('[data-view="experiments"]').click();
  await page.waitForFunction(()=>document.querySelector('#save-state').textContent==='Not saved');
  assert.equal(await page.locator('[data-view="curriculum"]').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#experiments-view').isVisible(),false);
  assert.equal(await page.locator('#notes').inputValue(),'A navigation failure must retain this note.');
  await page.unroute('**/api/records/*');
  await page.locator('[data-view="experiments"]').click();
  await page.locator('#experiment-form input[name="title"]').waitFor();
  assert.equal(await page.locator('[data-view="experiments"]').getAttribute('aria-pressed'),'true');
  await page.locator('#experiment-form input[name="title"]').fill('Keep this unfinished experiment.');
  await page.locator('[data-view="guide"]').click();
  await page.locator('#weekly-hours').waitFor();
  await page.locator('[data-view="experiments"]').click();
  assert.equal(await page.locator('#experiment-form input[name="title"]').inputValue(),'Keep this unfinished experiment.');
  await page.locator('[data-view="curriculum"]').click();
  await page.locator('#notes').waitFor();
  console.log('PASS: shell navigation waits for successful saves; a failed transition retains its view and unfinished work.');
}

export async function verifyStartupRecovery({page,server}) {
  const diagnostics=[];
  page.on('console',message=>{if(message.type()==='error')diagnostics.push(message.text());});
  for (const missing of ['resume-slot','retry']) {
    const writes=[];
    const recordWrite=request=>{
      if(new URL(request.url()).pathname.startsWith('/api/') && !['GET','HEAD'].includes(request.method())) writes.push(request.url());
    };
    page.on('request',recordWrite);
    await page.route(server.url+'/',async route=>{
      const response=await route.fetch();
      const body=(await response.text()).replace(`id="${missing}"`,`id="missing-${missing}"`);
      await route.fulfill({response,body});
    },{times:1});
    await page.goto(server.url);
    const fallback=page.getByRole('alert');
    await fallback.getByRole('heading',{name:'Schola could not start'}).waitFor({timeout:3000});
    assert.match(await fallback.textContent(),new RegExp(`Missing control: #${missing}`));
    assert.equal(await page.locator('#hub-host').count(),0);
    assert.deepEqual(writes,[],'Startup failure must not mutate learning records.');
    page.off('request',recordWrite);
    await fallback.getByRole('button',{name:'Reload'}).click();
    await openCurriculum(page);
    assert.equal(await page.locator('#notes').inputValue(),'A navigation failure must retain this note.');
    assert.equal(await page.getByRole('heading',{name:'Schola could not start'}).count(),0);
  }
  assert.ok(diagnostics.some(message=>message.includes('resume-slot')),'Keep render diagnostics available.');
  assert.ok(diagnostics.some(message=>message.includes('retry')),'Keep controller diagnostics available.');
  console.log('PASS: missing shell controls and controller startup failures show diagnostics and reload without losing saved work.');
}
