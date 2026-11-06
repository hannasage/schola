import assert from 'node:assert/strict';
import {openCurriculum} from './open.mjs';

async function saved(page) {
  await page.waitForFunction(() => document.querySelector('#save-state').textContent === 'Saved on this computer');
}
async function choose(page,id) {
  await page.locator('#curriculum-select').selectOption(id);
  await page.waitForFunction(id => document.querySelector('#curriculum-select').value === id && !document.querySelector('#curriculum-select').disabled,id);
}
export async function verifyDrafts({page, server}) {
  await page.goto(server.url);
  await openCurriculum(page);
  const first = await page.locator('#curriculum-select').inputValue();
  const address=`${server.url}/api/records/${first}`;
  const firstRecord = await (await fetch(address)).json();
  assert.equal(firstRecord.format, 'touchline-record');
  await page.locator('[data-module="m02"]').click();
  await page.locator('[data-view="experiments"]').click();
  // A draft with no title still contains valuable work.
  await page.locator('#experiment-form [name=hypothesis]').fill('Keep this hypothesis before any title or result exists.');
  await saved(page);
  await server.restart();
  await page.reload();
  await openCurriculum(page);
  assert.equal(await page.locator('.module.selected').getAttribute('data-module'), 'm02');
  assert.equal(await page.locator('#experiment-form [name=title]').inputValue(), '');
  assert.equal(await page.locator('#experiment-form [name=hypothesis]').inputValue(), 'Keep this hypothesis before any title or result exists.');
  await page.locator('[data-view="experiments"]').click();
  await page.locator('#experiment-form [name=title]').fill('An unfinished idea');
  // Switching before the autosave timer fires must flush the entire draft.
  const second = await page.locator('#curriculum-select option').evaluateAll((options,first)=>options.find(option=>option.value!==first)?.value,first);
  assert.ok(second, 'Import a second curriculum before checking per-course drafts.');
  await choose(page,second);
  await page.waitForFunction(() => document.querySelector('#experiment-form [name=title]').value === '');
  await page.locator('#experiment-form [name=title]').fill('Other course draft');
  await choose(page,first);
  await page.waitForFunction(() => document.querySelector('#experiment-form [name=title]').value === 'An unfinished idea');
  await page.locator('#experiment-form button').click();
  await saved(page);
  const entry=(await (await fetch(address)).json()).state.experiments.at(-1);
  await page.locator(`[data-edit="${entry.id}"]`).click();
  await page.locator('#experiment-form [name=title]').fill('An edit still in progress');
  await saved(page);
  await page.reload();await openCurriculum(page);
  assert.equal(await page.locator('#experiment-form [name=title]').inputValue(),'An edit still in progress');
  const edited=(await (await fetch(address)).json()).draft.experiment;
  assert.equal(edited.id,entry.id);assert.equal(edited.date,entry.date);
  await page.locator('[data-view="curriculum"]').click();
  await page.route('**/api/records/*', async route => {
    if (route.request().method() === 'PUT') return route.fulfill({status:500,json:{error:'Test disk unavailable',code:'storage_error',retryable:true}});
    return route.continue();
  });
  await page.locator('#notes').fill('Recover this note after a failed save.');
  await page.waitForFunction(() => document.querySelector('#save-state').textContent === 'Not saved');
  await page.locator('#curriculum-select').selectOption(second);
  await page.waitForFunction(id => document.querySelector('#curriculum-select').value === id && !document.querySelector('#curriculum-select').disabled,first);
  await page.reload();await page.locator('#notes').waitFor({state:'attached'});
  assert.equal(await page.locator('#notes').inputValue(), 'Recover this note after a failed save.');
  await page.waitForFunction(() => document.querySelector('#save-state').textContent === 'Not saved');
  await page.unroute('**/api/records/*');
  await page.locator('#retry').click();await saved(page);
  await openCurriculum(page);
  assert.equal((await (await fetch(address)).json()).state.modules.m02.notes, 'Recover this note after a failed save.');
  // A device journal may not silently overwrite a different writer's newer revision.
  await page.route('**/api/records/*', async route => {
    if(route.request().method()==='PUT')return route.fulfill({status:500,json:{error:'Test write failure'}});
    return route.continue();
  });
  await page.locator('#notes').fill('Keep this conflicting device note for export.');
  await page.waitForFunction(() => document.querySelector('#save-state').textContent === 'Not saved');
  const otherWriter=await (await fetch(address)).json();
  otherWriter.state.modules.m02.notes='Newer saved record from another writer.';
  assert.equal((await fetch(address,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({expectedRevision:otherWriter.state.revision,record:otherWriter})})).status,200);
  await page.unroute('**/api/records/*');
  await page.reload();await page.locator('#notes').waitFor({state:'attached'});
  assert.equal(await page.locator('#notes').inputValue(),'Keep this conflicting device note for export.');
  assert.equal(await page.locator('#save-state').textContent(),'Recovery conflict');
  assert.match(await page.locator('#notice').textContent(),/older version/);
  const download=page.waitForEvent('download');await page.locator('#export').click();
  const stream=await (await download).createReadStream();let raw='';for await(const chunk of stream)raw+=chunk;
  const exported=JSON.parse(raw);
  assert.equal(exported.format,'touchline-record');assert.equal(exported.version,2);
  assert.equal(exported.state.modules.m02.notes,'Keep this conflicting device note for export.');
  assert.equal(exported.draft.experiment.title,'An edit still in progress');
  assert.equal((await (await fetch(address)).json()).state.modules.m02.notes,'Newer saved record from another writer.');
  await page.route('**/api/records/*', route => route.request().method()==='GET' ? route.fulfill({status:503,json:{error:'Cannot load the saved version.'}}) : route.continue());
  page.once('dialog',dialog=>dialog.accept());await page.locator('#retry').click();
  await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('Cannot load the saved version.'));
  assert.equal(await page.locator('#notes').inputValue(),'Keep this conflicting device note for export.');
  await page.unroute('**/api/records/*');
  page.once('dialog',dialog=>dialog.accept());await page.locator('#retry').click();
  await page.waitForFunction(()=>document.querySelector('#notes').value==='Newer saved record from another writer.');
  await saved(page);
  console.log('PASS: drafts, edit identity, and selected lessons survive restart; failed saves block switching and recover; stale recovery stays exportable without overwriting newer records.');
}

export async function verifyRecordScopes({page,server}) {
  await openCurriculum(page);
  const localId=await page.locator('#curriculum-select').inputValue();
  const local=await (await fetch(`${server.url}/api/records/${localId}`)).json();
  assert.equal(local.origin.kind,'local');
  const firstScope={kind:'cloud',hubId:crypto.randomUUID(),backendOrigin:'https://isolated-backend.invalid',accountId:crypto.randomUUID()};
  const secondScope={...firstScope,accountId:crypto.randomUUID()};
  const cloudBackup={curriculum:local.curriculum,state:structuredClone(local.state)};
  cloudBackup.state.modules.m02.notes='First account download, same curriculum ID.';
  await page.evaluate(async({backup,origin})=>{await window.touchline.setAccountScope(origin);await window.touchline.importCloud(backup,origin);},{backup:cloudBackup,origin:firstScope});
  const firstCloudId=await page.locator('#curriculum-select').inputValue();
  assert.notEqual(firstCloudId,localId);
  await page.locator('[data-module="m02"]').click();await saved(page);
  assert.equal(await page.locator('#notes').inputValue(),'First account download, same curriculum ID.');
  assert.equal((await (await fetch(`${server.url}/api/records/${localId}`)).json()).state.modules.m02.notes,local.state.modules.m02.notes);
  await page.evaluate(origin=>window.touchline.setAccountScope(origin),secondScope);
  assert.equal(await page.locator(`#curriculum-select option[value="${firstCloudId}"]`).count(),0);
  assert.equal(await page.locator('#curriculum-select').inputValue(),localId);
  cloudBackup.state.modules.m02.notes='Second account download, same curriculum ID.';
  await page.evaluate(({backup,origin})=>window.touchline.importCloud(backup,origin),{backup:cloudBackup,origin:secondScope});
  const secondCloudId=await page.locator('#curriculum-select').inputValue();
  assert.notEqual(secondCloudId,firstCloudId);assert.notEqual(secondCloudId,localId);
  await page.evaluate(()=>window.touchline.setAccountScope(null));
  assert.equal(await page.locator(`#curriculum-select option[value="${firstCloudId}"], #curriculum-select option[value="${secondCloudId}"]`).count(),0);
  assert.equal(await page.locator('#curriculum-select').inputValue(),localId);
  // Portable v2 restores keep this installation's local identity.
  const portable={...structuredClone(local),recordId:crypto.randomUUID(),origin:firstScope,draft:{experiment:{id:null,date:'2026-01-01',module:'m02',title:'Portable draft',hypothesis:'Restore every field.',result:''}}};
  const beforeUnsupported=await (await fetch(`${server.url}/api/records/${localId}`)).json();
  for(const invalid of [{...portable,version:3},{...portable,format:'unknown-future-format'}]){
    await page.locator('#import').setInputFiles({name:'unsupported.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(invalid))});
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.startsWith('Unsupported'));
    assert.deepEqual(await (await fetch(`${server.url}/api/records/${localId}`)).json(),beforeUnsupported);
  }
  await page.locator('#import').setInputFiles({name:'portable-v2.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(portable))});
  await page.locator('#confirm-import').click();
  await page.waitForFunction(()=>document.querySelector('#notice').textContent==='Backup restored.');
  const restored=await (await fetch(`${server.url}/api/records/${localId}`)).json();
  assert.deepEqual(restored.origin,{kind:'local'});assert.equal(restored.recordId,localId);
  assert.equal(restored.draft.experiment.title,'Portable draft');
  const legacy={format:'touchline-backup',curriculum:local.curriculum,state:structuredClone(local.state)};
  legacy.state.modules.m02.notes='Restored legacy progress.';
  await page.locator('#import').setInputFiles({name:'legacy-v1.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(legacy))});
  await page.locator('#confirm-import').click();
  await page.waitForFunction(()=>document.querySelector('#notes').value==='Restored legacy progress.');
  const legacyRestored=await (await fetch(`${server.url}/api/records/${localId}`)).json();
  assert.equal(legacyRestored.draft.experiment,null);assert.deepEqual(legacyRestored.origin,{kind:'local'});
  // If browser storage is unavailable, never promise a recovery copy.
  await page.evaluate(()=>{window.testStorageSetter=Storage.prototype.setItem;Storage.prototype.setItem=()=>{throw new Error('Test storage unavailable');};});
  await page.route('**/api/records/*',async route=>route.request().method()==='PUT'?route.fulfill({status:500,json:{error:'Test disk unavailable'}}):route.continue());
  await page.locator('#notes').fill('Keep the page open when recovery storage is unavailable.');
  await page.waitForFunction(()=>document.querySelector('#save-state').textContent==='Not saved');
  assert.match(await page.locator('#notice').textContent(),/Export your work before closing/);
  assert.equal(await page.evaluate(()=>!window.dispatchEvent(new Event('beforeunload',{cancelable:true}))),true);
  await page.evaluate(()=>{Storage.prototype.setItem=window.testStorageSetter;delete window.testStorageSetter;});
  await page.unroute('**/api/records/*');await page.locator('#retry').click();await saved(page);
  console.log('PASS: identical curriculum IDs remain separate across local and two cloud accounts; sign-out hides cloud copies; v1/v2 restores preserve local identity; unavailable recovery storage warns and guards closing.');
}

export async function verifyAccountInvalidation({page,server}) {
  await openCurriculum(page);
  const localId=await page.locator('#curriculum-select').inputValue();
  const local=await (await fetch(`${server.url}/api/records/${localId}`)).json();
  const origin={kind:'cloud',hubId:crypto.randomUUID(),backendOrigin:'https://expired-account.invalid',accountId:crypto.randomUUID()};
  await page.evaluate(async({backup,origin})=>{await window.touchline.setAccountScope(origin);await window.touchline.importCloud(backup,origin);},{backup:{curriculum:local.curriculum,state:local.state},origin});
  const cloudId=await page.locator('#curriculum-select').inputValue();
  const beforeRestoreLocal=await (await fetch(`${server.url}/api/records/${localId}`)).json();
  const query=new URLSearchParams({hubId:origin.hubId,backendOrigin:origin.backendOrigin,accountId:origin.accountId});
  const beforeRestoreCloud=await (await fetch(`${server.url}/api/records/${cloudId}?${query}`)).json();
  await page.evaluate(()=>{window.originalFileText=File.prototype.arrayBuffer;File.prototype.arrayBuffer=async function(){window.readingBackup=true;await new Promise(resolve=>{window.releaseBackup=resolve;});return window.originalFileText.call(this);};});
  await page.locator('#import').setInputFiles({name:'pending-cloud-backup.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(beforeRestoreCloud))});
  await page.waitForFunction(()=>window.readingBackup);
  await page.evaluate(()=>window.touchline.invalidateAccountScope());
  await page.evaluate(()=>{window.releaseBackup();File.prototype.arrayBuffer=window.originalFileText;delete window.readingBackup;delete window.releaseBackup;});
  await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('account changed'));
  assert.deepEqual(await (await fetch(`${server.url}/api/records/${localId}`)).json(),beforeRestoreLocal);
  assert.deepEqual(await (await fetch(`${server.url}/api/records/${cloudId}?${query}`)).json(),beforeRestoreCloud);
  // Changing a local course while reading a backup must also reject the old target.
  const otherLocal=await page.locator('#curriculum-select option').evaluateAll((options,id)=>options.find(option=>option.value!==id)?.value,localId);
  await page.evaluate(()=>{window.originalFileText=File.prototype.arrayBuffer;File.prototype.arrayBuffer=async function(){window.readingBackup=true;await new Promise(resolve=>{window.releaseBackup=resolve;});return window.originalFileText.call(this);};});
  await page.locator('#import').setInputFiles({name:'pending-local-backup.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(beforeRestoreLocal))});
  await page.waitForFunction(()=>window.readingBackup);await choose(page,otherLocal);
  await page.evaluate(()=>{window.releaseBackup();File.prototype.arrayBuffer=window.originalFileText;delete window.readingBackup;delete window.releaseBackup;});
  await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('selected record changed'));
  assert.deepEqual(await (await fetch(`${server.url}/api/records/${localId}`)).json(),beforeRestoreLocal);
  await choose(page,localId);await page.evaluate(origin=>window.touchline.setAccountScope(origin),origin);await choose(page,cloudId);
  await page.locator('[data-view="experiments"]').click();
  await page.route('**/api/records/*',async route=>route.request().method()==='PUT'?route.fulfill({status:500,json:{error:'Test disk failure'}}):route.continue());
  await page.locator('#experiment-form [name=title]').fill('An account draft that must survive sign-out.');
  await page.waitForFunction(()=>document.querySelector('#save-state').textContent==='Not saved');
  // Force local loading to fail too: the old cloud form must remain hidden.
  await page.route(`**/api/records/${localId}`,route=>route.fulfill({status:500,json:{error:'Test local read failure'}}));
  await page.evaluate(()=>window.touchline.invalidateAccountScope());
  assert.equal(await page.locator('#experiment-form').isVisible(),false);
  assert.equal(await page.locator('#experiment-form [name=title]').inputValue(),'');
  assert.equal(await page.locator('#export').isDisabled(),true);
  assert.match(await page.locator('#notice').textContent(),/Cloud work remains hidden/);
  const journalBefore=await page.evaluate(id=>localStorage.getItem('touchline-recovery:'+id),cloudId);
  // Dispatch without waiting for enabled controls: the shell disables private views during invalidation.
  await page.locator('[data-view="curriculum"]').dispatchEvent('click');await page.locator('[data-view="guide"]').dispatchEvent('click');
  await page.evaluate(()=>{const field=document.querySelector('#experiment-form [name=title]');field.value='Injected input after sign-out';field.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#experiment-form').dispatchEvent(new Event('submit',{cancelable:true}));});
  assert.equal(await page.locator('#module-detail').textContent(),'');assert.equal(await page.locator('#guide-view').textContent(),'');
  assert.equal(await page.evaluate(id=>localStorage.getItem('touchline-recovery:'+id),cloudId),journalBefore);
  const recovery=await page.evaluate(id=>JSON.parse(localStorage.getItem('touchline-recovery:'+id)),cloudId);
  assert.equal(recovery.record.draft.experiment.title,'An account draft that must survive sign-out.');
  await page.unroute(`**/api/records/${localId}`);await page.unroute('**/api/records/*');
  await page.evaluate(origin=>window.touchline.setAccountScope(origin),origin);
  await page.waitForFunction(()=>document.querySelector('#experiment-form [name=title]').value==='An account draft that must survive sign-out.');
  await saved(page);
  await page.evaluate(()=>window.touchline.setAccountScope(null));
  assert.equal(await page.locator(`#curriculum-select option[value="${cloudId}"]`).count(),0);
  // A delayed old-account library fetch cannot reinstall cloud entries after invalidation.
  let releaseLibrary;let libraryStarted;
  const libraryGate=new Promise(resolve=>{releaseLibrary=resolve;});
  const libraryObserved=new Promise(resolve=>{libraryStarted=resolve;});
  await page.route('**/api/library?*',async route=>{libraryStarted();await libraryGate;await route.continue();});
  const pendingScope=page.evaluate(origin=>window.touchline.setAccountScope(origin).then(()=>null,error=>error.message),origin);
  await libraryObserved;await page.evaluate(()=>window.touchline.invalidateAccountScope());
  releaseLibrary();assert.match(await pendingScope,/account changed/);
  assert.equal(await page.locator(`#curriculum-select option[value="${cloudId}"]`).count(),0);
  await page.unroute('**/api/library?*');
  // A scope change waiting on a slow save cannot revive its old account either.
  await page.evaluate(origin=>window.touchline.setAccountScope(origin),origin);
  await choose(page,cloudId);await page.locator('[data-view="experiments"]').click();
  let releaseSave;let saveStarted;
  const saveGate=new Promise(resolve=>{releaseSave=resolve;});const saveObserved=new Promise(resolve=>{saveStarted=resolve;});
  await page.route('**/api/records/*',async route=>{if(route.request().method()!=='PUT')return route.continue();saveStarted();await saveGate;await route.fulfill({status:500,json:{error:'Delayed save failed'}});});
  await page.locator('#experiment-form [name=title]').fill('Keep a slow-save draft.');
  const pendingSwitch=page.evaluate(origin=>window.touchline.setAccountScope(origin).then(()=>null,error=>error.message),origin);
  await saveObserved;const invalidating=page.evaluate(()=>window.touchline.invalidateAccountScope());
  await page.waitForFunction(()=>document.querySelector('#experiment-form').hidden);
  releaseSave();await invalidating;assert.match(await pendingSwitch,/Save or export|account changed/);
  assert.equal(await page.locator(`#curriculum-select option[value="${cloudId}"]`).count(),0);
  assert.equal(await page.evaluate(id=>JSON.parse(localStorage.getItem('touchline-recovery:'+id)).record.draft.experiment.title,cloudId),'Keep a slow-save draft.');
  await page.unroute('**/api/records/*');
  console.log('PASS: expired accounts hide cloud fields through failed loads, navigation, and delayed responses; account drafts remain recoverable.');
}
