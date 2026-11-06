import assert from 'node:assert/strict';
import {openCurriculum} from './open.mjs';

export async function verifyLearningScreens({page,server}) {
 await page.goto(server.url);
 await openCurriculum(page);
 const first=await page.locator('#curriculum-select').inputValue();
 const firstBefore=await (await fetch(`${server.url}/api/records/${first}`)).json();
 const original=(await (await fetch(server.url+'/curriculum.json')).json());
 const second={...original,id:'screens-second',title:'A second curriculum with a long title that stays readable on a narrow screen'};
 await fetch(server.url+'/api/curricula',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(second)});
 await page.locator('[data-view=library]').click();
 await page.getByRole('heading',{name:'Room for more than one curiosity.'}).waitFor();
 const library=await (await fetch(server.url+'/api/library')).json();
 const other=library.records.find(value=>value.curriculumId===second.id).recordId;
 const firstCard=page.locator(`[data-record-card="${first}"]`),otherCard=page.locator(`[data-record-card="${other}"]`);
 await firstCard.getByRole('button',{name:'Make this my focus'}).click();
 await firstCard.getByText('Current focus',{exact:true}).waitFor();
 await otherCard.getByRole('button',{name:'Continue learning'}).click();
 await page.locator('#notes').waitFor();
 assert.equal(await page.locator('#curriculum-select').inputValue(),other);
 await page.locator('#notes').fill('Independent notes for the second curriculum.');
 await page.locator('#study-hours').fill('2.5');await page.locator('#study-hours').blur();
 await page.locator('[data-task="0"]').check();
 await page.locator('#evidence').fill('A separate notebook and measured result.');
 await page.locator('[data-view=library]').click();
 await otherCard.getByRole('button',{name:'Make this my focus'}).click();
 await otherCard.getByText('Current focus',{exact:true}).waitFor();
 assert.equal((await (await fetch(server.url+'/api/library')).json()).focusRecordId,other);
 assert.deepEqual((await (await fetch(`${server.url}/api/records/${first}`)).json()).state,firstBefore.state);
 const otherSaved=await (await fetch(`${server.url}/api/records/${other}`)).json();
 assert.equal(otherSaved.state.modules[otherSaved.view.selectedModuleId].hours,2.5);
 await otherCard.getByLabel('Curriculum status').selectOption('paused');
 await otherCard.getByText('Paused',{exact:true}).first().waitFor();
 await page.waitForFunction(async()=> (await (await fetch('/api/library')).json()).focusRecordId===null);
 assert.equal((await (await fetch(`${server.url}/api/records/${other}`)).json()).state.modules[otherSaved.view.selectedModuleId].notes,'Independent notes for the second curriculum.');
 await page.locator('[data-view=home]').click();
 await page.getByRole('heading',{name:'Choose where to put your attention.'}).waitFor();
 await page.getByRole('button',{name:'Choose a focus',exact:true}).click();
 await firstCard.getByRole('button',{name:'Make this my focus'}).click();
 await firstCard.getByText('Current focus',{exact:true}).waitFor();
 await page.route('**/api/library',route=>route.request().method()==='PUT'?route.fulfill({status:409,json:{error:'The library changed in another tab.',code:'revision_conflict'}}):route.continue());
 await firstCard.getByRole('button',{name:'Clear focus'}).click();
 await page.getByRole('alert').getByText('The library changed in another tab.').waitFor();
 assert.equal((await (await fetch(server.url+'/api/library')).json()).focusRecordId,first);
 await page.unroute('**/api/library');
 await page.getByRole('button',{name:'Retry library'}).click();
 await firstCard.getByRole('button',{name:'Clear focus'}).waitFor();
 await page.locator('[data-view=home]').click();
 await page.getByRole('heading',{name:firstBefore.curriculum.title,exact:true}).waitFor();
 await page.setViewportSize({width:390,height:844});
 await page.locator('[data-view=library]').click();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.setViewportSize({width:1440,height:1000});
 await firstCard.getByRole('button',{name:'Continue learning'}).click();
 await page.locator('#notes').waitFor();
 assert.equal(await page.locator('#notes').inputValue(),firstBefore.state.modules[firstBefore.view.selectedModuleId]?.notes||'');
 await page.route('**/api/library',route=>route.fulfill({json:{version:1,revision:0,focusRecordId:null,records:[]}}));
 await page.reload();await page.locator('[data-view=library]').click();
 await page.getByRole('heading',{name:'Your next topic starts here.'}).waitFor();
 await page.unroute('**/api/library');
 await page.getByRole('button',{name:'All · 0'}).waitFor();
 await page.locator('[data-view=home]').click();
 await page.getByRole('button',{name:/Browse all curricula/}).click();
 await page.locator(`[data-record-card="${first}"]`).getByRole('button',{name:'Continue learning'}).click();
 await page.locator('#notes').waitFor();
 assert.equal(await page.locator('#notes').inputValue(),firstBefore.state.modules[firstBefore.view.selectedModuleId]?.notes||'');
 await page.route(`**/api/records/${first}`,route=>route.request().method()==='GET'?route.fulfill({status:503,json:{error:'Reading is temporarily unavailable.'}}):route.continue());
 await page.locator('[data-view=library]').click();
 await page.locator(`[data-record-card="${first}"]`).getByText('This curriculum is unavailable. Your saved files remain unchanged.').waitFor();
 await page.unroute(`**/api/records/${first}`);
 await page.getByRole('button',{name:'Retry library'}).click();
 await page.locator(`[data-record-card="${first}"]`).getByRole('button',{name:'Continue learning'}).waitFor();
 console.log('PASS: focus, lifecycle, long titles, and independent course histories survive switching and failed library writes.');
}

export async function verifyRecoveredStartup({page,server}) {
 const failures=[];
 await page.route('**/api/library',route=>route.fulfill({json:{version:1,revision:0,focusRecordId:null,records:[]}}));
 await page.reload();await page.locator('[data-view=library]').click();
 await page.getByRole('heading',{name:'Your next topic starts here.'}).waitFor();
 await page.unroute('**/api/library');
 await page.locator('[data-view=home]').click();
 await page.getByRole('button',{name:/Browse all curricula/}).click();
 const library=await (await fetch(server.url+'/api/library')).json();
 const id=library.records.find(value=>value.origin.kind==='local').recordId;
 const card=page.locator(`[data-record-card="${id}"]`);
 const write=page.waitForResponse(response=>response.url().includes('/api/records/'+id)&&response.request().method()==='PUT');
 await card.getByLabel('Curriculum status').selectOption('paused');await write;
 await page.waitForFunction(id=>!document.querySelector(`[data-record-card="${id}"] select`).disabled,id);
 if(await card.getByLabel('Curriculum status').inputValue()!=='paused'||await page.getByRole('alert').count())failures.push('Status before first Continue must succeed and refresh the library.');
 await card.getByRole('button',{name:'Continue learning'}).click();await page.locator('#notes').waitFor();
 const local=await (await fetch(`${server.url}/api/records/${id}`)).json();
 const backup={curriculum:{...local.curriculum,title:'Recovered startup private curriculum'},state:structuredClone(local.state)};
 backup.state.modules[local.view.selectedModuleId]={status:'active',tasks:local.curriculum.modules.find(value=>value.id===local.view.selectedModuleId).tasks.map(()=>false),notes:'Private notes after recovered startup.',evidence:'',hours:0};
 const origin={kind:'cloud',hubId:'11111111-1111-4111-8111-111111111111',backendOrigin:'https://startup-recovery.invalid',accountId:'22222222-2222-4222-8222-222222222222'};
 await page.evaluate(async({backup,origin})=>{await window.touchline.setAccountScope(origin);await window.touchline.importCloud(backup,origin);},{backup,origin});
 // The imported record starts at its first lesson. Put private text into that
 // visible lesson so sign-out must remove both its form and export route.
 await page.locator('#notes').fill('Private notes after recovered startup.');
 await page.waitForFunction(()=>document.querySelector('#save-state').textContent==='Saved on this computer');
 await page.route('**/api/library',route=>route.fulfill({status:503,json:{error:'Local reload unavailable after sign-out.'}}));
 await page.evaluate(()=>window.touchline.invalidateAccountScope());
 if(await page.locator('#notes').count()!==0)failures.push('Recovered-startup sign-out must remove the private lesson form.');
 if(!(await page.locator('#export').isDisabled()))failures.push('Recovered-startup sign-out must disable export while local reload fails.');
 if((await page.locator('body').innerText()).includes('Recovered startup private curriculum'))failures.push('Recovered-startup sign-out must hide every private curriculum title.');
 await page.unroute('**/api/library');await page.reload();await openCurriculum(page);
 assert.deepEqual(failures,[]);
 console.log('PASS: recovered startup supports status changes before selection and still hides private data and export on account invalidation.');
}

export async function verifyStatusWriterConflict({page,server}) {
 await page.reload();await openCurriculum(page);
 const id=await page.locator('#curriculum-select').inputValue();
 const original=await (await fetch(`${server.url}/api/records/${id}`)).json();
 const selected=original.view.selectedModuleId;
 const current=original.state.modules[selected]||{status:'not-started',tasks:original.curriculum.modules.find(value=>value.id===selected).tasks.map(()=>false),notes:'',evidence:'',hours:0};
 const changed=structuredClone(original);changed.state.modules[selected]={...current,notes:'A newer note from the other writer.'};
 assert.equal((await fetch(`${server.url}/api/records/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({record:changed,expectedRevision:original.state.revision})})).status,200);
 await page.locator('[data-view=library]').click();
 const response=page.waitForResponse(value=>value.url().includes('/api/records/'+id)&&value.request().method()==='PUT');
 await page.locator(`[data-record-card="${id}"]`).getByLabel('Curriculum status').selectOption(original.status==='active'?'paused':'active');
 const statusResult=await response;
 await page.waitForFunction(id=>!document.querySelector(`[data-record-card="${id}"] select`).disabled,id);
 await page.locator('[data-view=curriculum]').click();await page.locator('#notes').waitFor();
 await page.locator('#study-hours').fill(String(current.hours+0.25));await page.locator('#study-hours').blur();
 await page.locator('#save-module').click();
 await page.waitForFunction(()=>['Saved on this computer','Not saved'].includes(document.querySelector('#save-state').textContent));
 const latest=await (await fetch(`${server.url}/api/records/${id}`)).json();
 const failures=[];
 if(statusResult.status()!==409)failures.push('A status change on a stale open record must return a conflict.');
 if(latest.state.modules[selected].notes!=='A newer note from the other writer.')failures.push('Editing hours after a status conflict must not overwrite the newer note.');
 await page.evaluate(id=>localStorage.removeItem('touchline-recovery:'+id),id);
 await page.reload();await openCurriculum(page);
 assert.deepEqual(failures,[]);
 console.log('PASS: a second writer cannot lose notes through a status change followed by an hours edit.');
}

export async function verifyEditsDuringStatusSave({page,server}) {
 await page.clock.install();
 await page.addInitScript(()=>{
  const transport=window.fetch;window.statusTestRecordId='';window.statusTestWrites=[];
  window.fetch=(input,options)=>{if(window.statusTestRecordId&&String(input).startsWith('/api/records/'+window.statusTestRecordId)&&options?.method==='PUT')window.statusTestWrites.push(JSON.parse(options.body));return transport(input,options);};
 });
 await page.reload();await openCurriculum(page);
 const id=await page.locator('#curriculum-select').inputValue();
 await page.locator('[data-module="m02"]').click();
 await page.locator('[data-view=experiments]').click();
 await page.locator('#experiment-form [name=title]').fill('A draft preserved during status changes.');
 await page.locator('[data-view=library]').click();
 const before=await (await fetch(`${server.url}/api/records/${id}`)).json();
 const status=before.status==='active'?'paused':'active';
 let release,observed;
 const pending=new Promise(resolve=>{observed=resolve;});
 const gate=new Promise(resolve=>{release=resolve;});
 let firstStatus=true;
 await page.evaluate(id=>{window.statusTestRecordId=id;window.statusTestWrites=[];},id);
 await page.route(`**/api/records/${id}`,async route=>{
  if(route.request().method()==='PUT'&&route.request().postDataJSON().record.status===status&&firstStatus){
   firstStatus=false;const response=await route.fetch();observed();await gate;return route.fulfill({response});
  }
  await route.continue();
 });
 await page.locator(`[data-record-card="${id}"]`).getByLabel('Curriculum status').selectOption(status);
 await pending;
 await page.locator('[data-view=curriculum]').click();await page.locator('#notes').waitFor();
 assert.equal(await page.locator('.module.selected').getAttribute('data-module'),'m02');
 await page.locator('#notes').fill('Keep this edit made while the status write is pending.');
 // Advance past autosave while the server already saved status but its response
 // is still blocked. No second write can use the now-stale working revision.
 await page.clock.runFor(600);
 await page.locator('#save-module').click();
 const notesWrite=page.waitForResponse(response=>response.url().includes('/api/records/'+id)&&response.request().method()==='PUT'&&response.request().postDataJSON().record.state.modules.m02?.notes==='Keep this edit made while the status write is pending.');
 try{assert.equal(await page.evaluate(()=>window.statusTestWrites.length),1,'Autosave must wait for the status response before choosing a revision.');}
 finally{release();}
 assert.equal((await notesWrite).status(),200);
 assert.equal(await page.evaluate(()=>window.statusTestWrites.length),2,'The queued edit must produce one successful notes write.');
 await page.waitForFunction(()=>document.querySelector('#save-state').textContent==='Saved on this computer');
 await page.unroute(`**/api/records/${id}`);
 await page.evaluate(()=>{window.statusTestRecordId='';});
 const after=await (await fetch(`${server.url}/api/records/${id}`)).json();
 assert.equal(after.status,status);
 assert.equal(after.state.modules.m02.notes,'Keep this edit made while the status write is pending.');
 assert.equal(after.view.selectedModuleId,'m02');
 assert.deepEqual(after.draft,before.draft);
 await page.reload();await openCurriculum(page);
 assert.equal(await page.locator('#notes').inputValue(),'Keep this edit made while the status write is pending.');
 assert.equal(await page.locator('#experiment-form [name=title]').inputValue(),'A draft preserved during status changes.');
 console.log('PASS: edits during a delayed status save keep the selected lesson and unfinished experiment.');
 await verifyQueuedStatusInvalidation({page,server});
}

async function verifyQueuedStatusInvalidation({page,server}) {
 const localId=await page.locator('#curriculum-select').inputValue();
 const local=await (await fetch(`${server.url}/api/records/${localId}`)).json();
 const origin={kind:'cloud',hubId:crypto.randomUUID(),backendOrigin:'https://status-invalidation.invalid',accountId:crypto.randomUUID()};
 await page.evaluate(async({backup,origin})=>{await window.touchline.setAccountScope(origin);await window.touchline.importCloud(backup,origin);},{backup:{curriculum:local.curriculum,state:local.state},origin});
 const id=await page.locator('#curriculum-select').inputValue();
 const query=new URLSearchParams({hubId:origin.hubId,backendOrigin:origin.backendOrigin,accountId:origin.accountId});
 const address=`${server.url}/api/records/${id}?${query}`;
 await page.locator('[data-view=library]').click();
 const before=await (await fetch(address)).json();
 const status=before.status==='active'?'paused':'active';
 let release,observed;
 const gate=new Promise(resolve=>{release=resolve;});const pending=new Promise(resolve=>{observed=resolve;});
 await page.evaluate(id=>{window.statusTestRecordId=id;window.statusTestWrites=[];},id);
 await page.route(`**/api/records/${id}*`,async route=>{
  if(route.request().method()!=='PUT')return route.continue();
  const response=await route.fetch();observed();await gate;return route.fulfill({response});
 });
 await page.locator(`[data-record-card="${id}"]`).getByLabel('Curriculum status').selectOption(status);await pending;
 await page.locator('[data-view=curriculum]').click();await page.locator('#notes').fill('Keep the queued private edit in recovery only.');
 await page.clock.runFor(600);
 const invalidating=page.evaluate(()=>window.touchline.invalidateAccountScope());
 try{await page.waitForFunction(()=>document.querySelector('#experiment-form').hidden);}
 finally{release();}
 await invalidating;
 assert.equal(await page.evaluate(()=>window.statusTestWrites.length),1,'Account invalidation must cancel the queued notes write.');
 const after=await (await fetch(address)).json();
 assert.deepEqual(after.state.modules,before.state.modules);
 assert.equal(after.state.revision,before.state.revision+1);
 assert.equal(await page.locator(`#curriculum-select option[value="${id}"]`).count(),0);
 assert.equal(await page.evaluate(({id,selected})=>JSON.parse(localStorage.getItem('touchline-recovery:'+id)).record.state.modules[selected].notes,{id,selected:before.view.selectedModuleId}),'Keep the queued private edit in recovery only.');
 await page.unroute(`**/api/records/${id}*`);await page.evaluate(()=>{window.statusTestRecordId='';});
 console.log('PASS: account invalidation cancels queued autosave, hides the cloud record, and keeps the private edit in recovery.');
}
