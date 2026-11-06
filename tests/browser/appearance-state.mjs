import assert from 'node:assert/strict';
import {openCurriculum} from './open.mjs';
import {chromium} from 'playwright';
import {isolateContext} from './network.mjs';

/** Runs only with the temporary server owned by run-appearance.mjs. */
export async function verifyAppearanceState(server) {
  const browser = await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined});
  try {
    const context = await browser.newContext({colorScheme:'dark',serviceWorkers:'block'});
    await isolateContext(context,new URL(server.url).origin);
    const page = await context.newPage();
    const original = await (await fetch(server.url+'/curriculum.json')).json();
    const imported = await fetch(server.url+'/api/curricula',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({...original,id:'appearance-second',title:'A separate learning project'})});
    assert.equal(imported.status,201);
    const library = await (await fetch(server.url+'/api/library')).json();
    const focus = library.records.find(record=>record.curriculumId===original.id).recordId;
    const selected = library.records.find(record=>record.curriculumId==='appearance-second').recordId;
    const focused = await fetch(server.url+'/api/library',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({focusRecordId:focus,expectedRevision:library.revision})});
    assert.equal(focused.status,200);
    await page.goto(server.url);await openCurriculum(page);
    await page.locator('#curriculum-select').selectOption(selected);
    await page.waitForFunction(id=>document.querySelector('#curriculum-select').value===id&&!document.querySelector('#curriculum-select').disabled,selected);
    await page.locator('[data-module="m02"]').click();
    await page.locator('#notes').fill('Keep the selected course notes.');
    await page.locator('[data-view=experiments]').click();
    await page.locator('#experiment-form [name=title]').fill('A recorded model comparison');
    await page.locator('#experiment-form [name=hypothesis]').fill('Recent form improves the held-out error.');
    await page.locator('#experiment-form [name=result]').fill('Keep the saved result unchanged.');
    await page.locator('#experiment-form button[type=submit]').click();
    await page.locator('#experiment-form [name=hypothesis]').fill('An unfinished next experiment.');
    await page.locator('[data-view=curriculum]').click();
    await page.waitForFunction(()=>document.querySelector('#save-state').textContent==='Saved on this computer');
    const address=server.url+'/api/records/'+selected;
    const before = await (await fetch(address)).json();
    const libraryBefore = await (await fetch(server.url+'/api/library')).json();
    assert.equal(before.state.experiments.length,1);
    assert.equal(before.draft.experiment.hypothesis,'An unfinished next experiment.');
    const writes=[];
    page.on('request',request=>{if(new URL(request.url()).pathname.startsWith('/api/')&&request.method()!=='GET')writes.push(request.url());});
    await page.getByRole('button',{name:'Change theme',exact:true}).click();
    for(const pair of ['bloom','coast']) for(const mode of ['dark','light','system']) {
      await page.locator(`input[name=theme-pair][value=${pair}]`).check();
      await page.locator(`input[name=theme-mode][value=${mode}]`).check();
      assert.equal(await page.locator('#notes').inputValue(),'Keep the selected course notes.');
      assert.equal(await page.locator('#curriculum-select').inputValue(),selected);
      assert.equal(await page.locator('.module.selected').getAttribute('data-module'),'m02');
    }
    await page.keyboard.press('Escape');
    assert.deepEqual(await (await fetch(address)).json(),before);
    assert.deepEqual(await (await fetch(server.url+'/api/library')).json(),libraryBefore);
    assert.deepEqual(writes,[],'Appearance changes must not save learning records or focus.');
    await page.locator('[data-view=experiments]').click();
    assert.equal(await page.locator('#experiment-form [name=hypothesis]').inputValue(),'An unfinished next experiment.');
    assert.equal(await page.locator('#experiment-list').getByText('A recorded model comparison',{exact:true}).textContent(),'A recorded model comparison');
    console.log('PASS: appearance preserves actual saved experiments, unfinished drafts, notes, separate focus, curriculum, and lesson without learning writes.');
  } finally { await browser.close(); }
}
