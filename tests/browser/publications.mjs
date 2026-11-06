import assert from 'node:assert/strict';
import {openCurriculum} from './open.mjs';
import {readFile} from 'node:fs/promises';
const hub=JSON.parse(await readFile(new URL('../fixtures/contracts/valid/hub-description.json',import.meta.url),'utf8'));
const fixture=JSON.parse(await readFile(new URL('../fixtures/contracts/valid/legacy-backup.json',import.meta.url),'utf8'));
export async function verifyPublications({page,server}){
 const account={id:'0bdb7cc4-f92d-4ac7-84b7-49928d888f2c',email:'learner@example.com',email_confirmed_at:'2026-10-28T12:00:00Z',is_anonymous:false,aud:'authenticated',role:'authenticated',app_metadata:{provider:'email'},user_metadata:{}};
 const sessionId='fbbbbccb-8f23-4e5f-b13c-c7ff2a814079',claims={sub:account.id,session_id:sessionId,role:'authenticated',exp:Math.floor(Date.now()/1000)+3600};
 const token=Buffer.from(JSON.stringify({alg:'HS256',typ:'JWT'})).toString('base64url')+'.'+Buffer.from(JSON.stringify(claims)).toString('base64url')+'.test-signature';
 const remote=new Map(),requests=new Map(),writes=[];
 const local=[];
 for(let i=0;i<3;i++){
  const source=structuredClone(fixture);source.curriculum.id=source.state.curriculumId='publication-browser-'+i;source.curriculum.title=['Soccer prediction','Creative coding','Photography practice'][i];
  const response=await fetch(server.url+'/api/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source,origin:{kind:'local'},status:'active'})});assert.equal(response.status,201);local.push(await response.json());
 }
 const third=structuredClone(local[2]);third.draft.experiment={id:null,date:'2026-10-29',module:'baseline',title:'Unfinished private experiment',hypothesis:'Keep the draft',result:''};
 const thirdSaved=await fetch(`${server.url}/api/records/${third.recordId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({record:third,expectedRevision:third.state.revision})});assert.equal(thirdSaved.status,200);local[2]=await thirdSaved.json();
 remote.set(third.curriculum.id,{id:third.curriculum.id,curriculum:structuredClone(third.curriculum),progress:structuredClone(third.state),revision:4,status:'active',format_version:1,updated_at:'2026-10-29T12:00:00+00:00'});
 remote.get(third.curriculum.id).progress.modules.baseline.notes='Reviewed cloud notes';
 await page.route(hub.hubOrigin+'/**',route=>route.fulfill({json:hub,headers:{'access-control-allow-origin':'*'}}));
 await page.route(hub.backend.url+'/**',route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname;
  const headers={'access-control-allow-origin':'*','access-control-allow-headers':'authorization,apikey,content-type,x-client-info,x-supabase-api-version','access-control-allow-methods':'GET,POST,OPTIONS'};
  if(request.method()==='OPTIONS')return route.fulfill({status:200,headers});
  if(path==='/auth/v1/otp')return route.fulfill({json:{},headers});
  if(path==='/auth/v1/verify')return route.fulfill({json:{access_token:token,refresh_token:'test-refresh',token_type:'bearer',expires_in:3600,expires_at:claims.exp,user:account},headers});
  if(path==='/auth/v1/user')return route.fulfill({json:account,headers});
  if(path==='/auth/v1/logout')return route.fulfill({status:204,headers});
  if(path==='/rest/v1/rpc/register_touchline_session')return route.fulfill({json:{sessionId,accountId:account.id},headers});
  if(path==='/rest/v1/rpc/list_touchline_sessions')return route.fulfill({json:[],headers});
  if(path==='/rest/v1/learning_courses'){const id=url.searchParams.get('id')?.replace(/^eq\./,'');return route.fulfill({json:id?remote.get(id):[...remote.values()],headers});}
  if(path==='/rest/v1/rpc/publish_learning_course'){
   const args=request.postDataJSON();writes.push(args);if(requests.has(args.request_id))return route.fulfill({json:requests.get(args.request_id),headers});const old=remote.get(args.course_id);
   if((old?.revision??-1)!==args.expected_revision)return route.fulfill({status:409,json:{code:'40001',message:'Newer snapshot'},headers});
   const row={id:args.course_id,curriculum:args.course_content,progress:args.course_progress,status:args.course_status,revision:(old?.revision??-1)+1,format_version:1,updated_at:'2026-10-29T12:30:00+00:00'};remote.set(row.id,row);requests.set(args.request_id,row);return route.fulfill({json:row,headers});
  }
  return route.abort('blockedbyclient');
 });
 await page.emulateMedia({colorScheme:'dark'});await page.goto(server.url);await openCurriculum(page);
 // A clean working copy must still wait for its status write before publication capture.
 await page.locator('#curriculum-select').selectOption(local[0].recordId);await page.waitForFunction(()=>document.querySelector('#plan-title').textContent==='Soccer prediction');
 const id=local[0].recordId;let release,observed;const gate=new Promise(r=>release=r),pending=new Promise(r=>observed=r);
 await page.route(`**/api/records/${id}`,async route=>{if(route.request().method()==='PUT'){observed();await gate;}return route.continue();});
 await page.getByRole('button',{name:'Your curricula',exact:true}).click();await page.locator(`[data-record-card="${id}"]`).getByLabel('Curriculum status').selectOption('paused');await pending;
 const captured=page.evaluate(()=>window.touchline.publicationRecords());let finished=false;void captured.then(()=>finished=true);await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));assert.equal(finished,false);release();const capturedRecords=await captured;assert.equal(capturedRecords.records.find(r=>r.recordId===id).status,'paused');await page.unroute(`**/api/records/${id}`);local[0]=await(await fetch(`${server.url}/api/records/${id}`)).json();
 await page.getByRole('button',{name:'Devices',exact:true}).click();await page.getByLabel('Hub address',{exact:true}).fill(hub.hubOrigin);await page.getByRole('button',{name:'Find my hub'}).click();await page.getByRole('button',{name:'Confirm this hub'}).click();await page.getByLabel('Email',{exact:true}).fill(account.email);await page.getByRole('button',{name:'Email me a sign-in code'}).click();await page.getByLabel('Sign-in code',{exact:true}).fill('123456');await page.getByRole('button',{name:'Verify and open my hub'}).click();
 await page.getByRole('heading',{name:'Choose what to publish'}).waitFor();
 for(const record of local)await page.locator(`[data-publication-record="${record.recordId}"] input`).check();
 await page.getByRole('button',{name:'Publish selected · 3'}).click();await page.getByRole('button',{name:'Review Photography practice',exact:true}).waitFor();
 assert.equal(await page.locator('.publication-result strong').filter({hasText:/^Published$/}).count(),2);assert.equal(writes.length,3);assert.equal(remote.get(third.curriculum.id).revision,4);
 await page.waitForFunction(()=>document.querySelectorAll('#cloud-courses .connection-course').length===3);
 assert.equal(await page.locator('#cloud-courses .connection-course').count(),3);
 for(const record of local)assert.deepEqual(await(await fetch(`${server.url}/api/records/${record.recordId}`)).json(),record);
 await page.setViewportSize({width:1440,height:1000});await page.waitForFunction(()=>!document.querySelector('#cloud-upload').disabled);await page.locator('.publication-selection').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/touchline-p10-publications-desktop.png'});
 await page.getByRole('button',{name:'Review Photography practice',exact:true}).click();await page.getByRole('dialog').waitFor();
 await page.setViewportSize({width:390,height:844});await page.locator('.publication-review').evaluate(dialog=>{dialog.scrollTop=0;});await page.screenshot({path:'/tmp/touchline-p10-conflict-mobile.png'});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.equal(await page.getByRole('heading',{name:'A newer snapshot is online.'}).isVisible(),true);
 await page.setViewportSize({width:1440,height:900});await page.keyboard.press('Escape');await page.getByRole('dialog').waitFor({state:'hidden'});assert.equal(await page.getByRole('button',{name:'Review Photography practice',exact:true}).evaluate(button=>button===document.activeElement),true);await page.getByRole('button',{name:'Review Photography practice',exact:true}).click();await page.getByRole('dialog').waitFor();
 const downloaded=page.waitForEvent('download');await page.getByRole('button',{name:'Export my local backup'}).click();assert.equal((await downloaded).suggestedFilename(),third.curriculum.id+'-local-backup.json');
 remote.get(third.curriculum.id).revision++;
 await page.getByRole('button',{name:'Download cloud version'}).click();await page.getByText('The cloud snapshot changed. Review the new version before downloading.',{exact:true}).waitFor();assert.deepEqual(await(await fetch(`${server.url}/api/records/${third.recordId}`)).json(),local[2]);
 await page.getByRole('button',{name:'Review Photography practice',exact:true}).click();await page.getByRole('dialog').waitFor();
 await page.route('**/api/import*',route=>route.fulfill({status:500,json:{error:'Test interrupted local import'}}));await page.getByRole('button',{name:'Download cloud version'}).click();await page.getByRole('dialog').getByRole('alert').filter({hasText:/interrupted local import/}).waitFor();assert.deepEqual(await(await fetch(`${server.url}/api/records/${third.recordId}`)).json(),local[2]);await page.unroute('**/api/import*');
 await page.getByRole('button',{name:'Download cloud version'}).click();await page.getByRole('dialog').waitFor({state:'hidden'});
 const imported=await(await fetch(`${server.url}/api/records/${third.recordId}`)).json();assert.equal(imported.state.modules.baseline.notes,'Reviewed cloud notes');assert.deepEqual(imported.draft,local[2].draft);assert.deepEqual(imported.view,local[2].view);assert.deepEqual(imported.origin,{kind:'local'});
 for(const record of local.slice(0,2))assert.deepEqual(await(await fetch(`${server.url}/api/records/${record.recordId}`)).json(),record);
 const origin={kind:'cloud',hubId:hub.hubId,backendOrigin:hub.backend.url,accountId:account.id};const incompatible=structuredClone(fixture);incompatible.curriculum.id=incompatible.state.curriculumId=third.curriculum.id;incompatible.curriculum.modules[0].id='replacement';incompatible.state.modules={};
 const error=await page.evaluate(async({backup,origin,id})=>{try{await window.touchline.prepareCloudImport(backup,origin,'active',id);return null;}catch(error){return error.message;}},{backup:incompatible,origin,id:third.recordId});assert.match(error,/selected lesson or draft module/);
 await page.setViewportSize({width:1440,height:900});await page.getByRole('button',{name:'Sign out this browser'}).click();await page.getByRole('button',{name:'Use a different hub'}).click();await page.getByLabel('Hub address',{exact:true}).waitFor();
 await page.unroute(hub.hubOrigin+'/**');await page.unroute(hub.backend.url+'/**');
 console.log('PASS: independent publication, clean status serialization, cloud conflict review/export, atomic import failure, draft preservation, and changed-cloud checks.');
}
