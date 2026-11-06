'use strict';
import {createLearningService} from './web/services/learning.js';
import {accounts} from './web/services/accounts.js';
import {projectPublication} from './web/publication.js';
import {journalList,evidenceMarkup} from './web/screens/journal.js';
import {prepareImport,reviewImport} from './web/screens/import-review.js';
import {createLessonService} from './web/services/lessons.js';
import {classifyLessonFile,classifyLessonText,looksLikeLessonText,openModuleImport} from './web/screens/module-import-dialog.js';
import {lessonMarkup} from './web/screens/lesson.js';
import {screens} from './web/screens/model.js';
import {shell} from './web/shell/bridge.js';
const learning = createLearningService();
const lessons = createLessonService();
/** @type {Curriculum} */ let course;
/** @type {LearningState} */ let state;
/** @type {LocalRecord} */ let record;
/** @type {LibrarySummary[]} */ let library = [];
/** @type {CloudOrigin | null} */ let accountScope = null;
/** @type {LibraryPreference} */ let libraryPreference={version:1,revision:0,focusRecordId:null};
/** @type {Map<string,LocalRecord>} */ const screenRecords=new Map();
let screenBusy=false;
let lastLocalRecordId = '';
let recoveryConflict = false;
let recoveryAvailable = true;
let viewEpoch = 0;
let accountInvalidated = false;
/** @type {Map<string, string>} */ const memoryRecovery = new Map();
/** @type {Set<string>} */ const volatileRecovery = new Set();
/** @type {string} */ let selected;
let dirty = false;
/** @type {Promise<boolean> | null} */ let saving = null;
/** @type {{recordId:string, epoch:number, promise:Promise<void>} | null} */ let statusWrite = null;
/** @type {number | undefined} */ let timer;
/** @type {string | null} */ let editing = null;
/**
 * @template {keyof TouchlineElements} K
 * @overload
 * @param {K} selector
 * @returns {TouchlineElements[K]}
 */
/** @overload @param {string} selector @returns {HTMLElement} */
/** @param {string} selector @returns {HTMLElement} */
function $(selector) {
 const element = document.querySelector(selector);
 if (!(element instanceof HTMLElement)) throw new Error(`Missing control: ${selector}`);
 return element;
}
/** @param {unknown} error */
const message = error => error instanceof Error ? error.message : String(error);
/** @param {string} id @returns {CurriculumModule} */
function moduleById(id) {
 const module = course.modules.find(item => item.id === id);
 if (!module) throw new Error(`Unknown module: ${id}`);
 return module;
}
/** @param {unknown} v */
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c] || c));
/** @param {CurriculumModule} m @returns {ModuleProgress} */
const progress = m => state.modules[m.id] || {status:'not-started', tasks:m.tasks.map(()=>false), notes:'', evidence:'', hours:0};
/** @param {CurriculumModule} m @returns {ModuleProgress} */
const editable = m => state.modules[m.id] ||= progress(m);
/** @param {string} text */
const notice = (text, error=false) => { $('#notice').textContent=text; $('#notice').className=error?'error':''; };
/** @param {number} epoch */
function requireEpoch(epoch){if(epoch!==viewEpoch)throw Error('The account changed during this operation. Retry from the current account.');}
function recoveryKey() { return 'touchline-recovery:'+record.recordId; }
function journal() {
 const key=recoveryKey(),value=JSON.stringify({version:1,baseRevision:state.revision,record});memoryRecovery.set(key,value);
 try { localStorage.setItem(key,value); recoveryAvailable=true;volatileRecovery.delete(key); }
 catch { recoveryAvailable=false;volatileRecovery.add(key); notice('Device recovery is unavailable. Keep this page open until saving succeeds, or export your work.',true); }
}
function clearJournal() { memoryRecovery.delete(recoveryKey());volatileRecovery.delete(recoveryKey());try { localStorage.removeItem(recoveryKey()); } catch { /* The saved server record is still durable. */ } }
function changed() {
 dirty=true; journal(); $('#save-state').textContent=recoveryConflict?'Recovery conflict':'Unsaved changes';
 clearTimeout(timer); if(!recoveryConflict)timer=setTimeout(save,500);
}
/** @returns {Promise<boolean>} */
async function save() {
 clearTimeout(timer); if(recoveryConflict||accountInvalidated)return false; if(saving)return saving;
 const epoch=viewEpoch;
 saving=(async()=>{
  while(dirty) {
   $('#save-state').textContent='Saving…';
   try {
    // Keep one save promise while waiting, so timer and navigation callers
    // cannot both write the working copy with the old status revision.
    if(statusWrite?.recordId===record.recordId&&statusWrite.epoch===epoch)await statusWrite.promise;
    if(epoch!==viewEpoch||accountInvalidated)return false;
    dirty=false;
    const snapshot=structuredClone(record);
    const response=await learning.saveRecord(snapshot,snapshot.state.revision,accountScope);
    if(epoch!==viewEpoch)return false;
    state.revision=response.state.revision;screenRecords.set(record.recordId,record);updateScreens();
    if(dirty)journal();else clearJournal();
    $('#save-state').textContent='Saved on this computer'; $('#retry').hidden=true;
   } catch(error) {
    if(epoch!==viewEpoch)return false;
    dirty=true; journal(); notice(message(error)+(recoveryAvailable?' Your working copy is kept on this device.':' Export your work before closing this page.'),true);
    $('#save-state').textContent='Not saved'; $('#retry').hidden=false; return false;
   }
  }
  return true;
 })();
 const result=await saving; saving=null; return result;
}
function captureDraft() {
 if(accountInvalidated)return;
 const fields=$('#experiment-form').elements;
 record.draft.experiment={id:editing,date:record.draft.experiment?.date||new Date().toISOString().slice(0,10),module:fields.module.value,title:fields.title.value,hypothesis:fields.hypothesis.value,result:fields.result.value};
 $('#experiment-evidence').innerHTML=evidenceMarkup(fields.result.value);
 changed();
}
function restoreDraft() {
 const draft=record.draft.experiment,form=$('#experiment-form');
 editing=draft?.id||null;
 for(const key of /** @type {const} */(['title','hypothesis','result']))form.elements[key].value=draft?.[key]||'';
 form.elements.module.value=draft?.module||selected;
 $('#experiment-form button').textContent=editing?'Save experiment changes':'Record experiment';
 $('#experiment-heading').textContent=editing?'Edit experiment':'New experiment';
 $('#experiment-evidence').innerHTML=evidenceMarkup(draft?.result||'');
}
/** @param {LocalRecord} candidate @param {LocalRecord} current */
function usableRecovery(candidate,current) {
 if(candidate.format!=='touchline-record'||candidate.version!==2||candidate.recordId!==current.recordId||JSON.stringify(candidate.origin)!==JSON.stringify(current.origin)||JSON.stringify(candidate.curriculum)!==JSON.stringify(current.curriculum))return false;
 const ids=new Set(current.curriculum.modules.map(module=>module.id));
 if(!candidate.state||candidate.state.curriculumId!==current.curriculum.id||!candidate.state.modules||!Array.isArray(candidate.state.experiments)||!candidate.view||!ids.has(candidate.view.selectedModuleId)||!candidate.draft)return false;
 for(const [id,progress] of Object.entries(candidate.state.modules))if(!ids.has(id)||!progress||typeof progress.notes!=='string'||typeof progress.evidence!=='string'||!Array.isArray(progress.tasks)||typeof progress.hours!=='number')return false;
 const draft=candidate.draft.experiment;
 return draft===null||!!draft&&ids.has(draft.module)&&['date','title','hypothesis','result'].every(key=>typeof draft[/** @type {'date'|'title'|'hypothesis'|'result'} */(key)]==='string')&&(draft.id===null||typeof draft.id==='string');
}
function stats() {
 if(accountInvalidated)return;
 const p=course.modules.map(progress);
 shell.update({progress:{done:p.filter(x=>x.status==='complete').length,total:p.length}});
 $('#completed').textContent=`${p.filter(x=>x.status==='complete').length} / ${p.length}`;
 $('#hours').textContent=`${p.reduce((a,x)=>a+x.hours,0).toFixed(1).replace('.0','')} h`;
 $('#experiments-count').textContent=String(state.experiments.length);
 $('#duration').textContent=`${course.weeks} weeks`;
 $('#pace').textContent=`${state.weeklyHours} hours per week planned`;
}
function navigation() {
 if(accountInvalidated)return;
 let phase='';
 $('#modules').innerHTML=course.modules.map((m,i)=>{
  const heading=m.phase!==phase?`<p class="phase">${esc(m.phase)}</p>`:''; phase=m.phase;
  const p=progress(m);
  return `${heading}<button class="module ${selected===m.id?'selected':''} ${p.status==='complete'?'done':''}" data-module="${esc(m.id)}" ${selected===m.id?'aria-current="step"':''}><span class="module-number">${p.status==='complete'?'✓':String(i+1).padStart(2,'0')}</span><span>${esc(m.title)}<small>Weeks ${m.weekStart}–${m.weekEnd} · ${p.status==='complete'?'Complete':p.status==='active'?'In progress':m.hours+' h'}</small></span></button>`;
 }).join('');
 $('#modules').querySelectorAll('button').forEach(b=>b.onclick=()=>{selected=b.dataset.module || '';record.view.selectedModuleId=selected;if(!record.draft.experiment)restoreDraft();changed();navigation();detail();});
 stats();
}
/** @param {CurriculumModule} m */
function dates(m) {
 if(!state.startDate)return '';
 const start=new Date(state.startDate+'T12:00:00'); start.setDate(start.getDate()+(m.weekStart-1)*7);
 const end=new Date(state.startDate+'T12:00:00'); end.setDate(end.getDate()+m.weekEnd*7-1);
 return `${start.toLocaleDateString(undefined,{month:'short',day:'numeric'})} – ${end.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})}`;
}
function detail() {
 if(accountInvalidated)return;
 const m=moduleById(selected),p=progress(m);
 const missing=m.prerequisites.filter(id=>progress(moduleById(id)).status!=='complete');
 $('#module-detail').innerHTML=lessonMarkup(course,m,p,missing.length>0,dates(m));
 $('#switch-curriculum').onclick=()=>void shell.navigate('library');
 $('#log-experiment').onclick=()=>void shell.navigate('experiments');
 $('#status').value=p.status;
 $('#status').onchange=()=>{const targetControl=$('#status');const target=editable(m);if(targetControl.value==='complete'&&(!target.tasks.every(Boolean)||!target.evidence.trim())){notice('Complete the tasks and add evidence before marking this module complete.',true);targetControl.value=target.status;return;} target.status=/** @type {ModuleProgress['status']} */(targetControl.value);changed();navigation();};
 $('#study-hours').onchange=()=>{const targetControl=$('#study-hours');const n=Number(targetControl.value);if(!Number.isFinite(n)||n<0||n>10000){targetControl.value=String(progress(m).hours);notice('Enter study hours between 0 and 10000.',true);return;}editable(m).hours=n;changed();stats();};
 /** @type {NodeListOf<HTMLInputElement>} */(document.querySelectorAll('[data-task]')).forEach(el=>el.onchange=()=>{const p=editable(m);p.tasks[Number(el.dataset.task)]=el.checked;if(p.status==='not-started'||p.status==='complete'&&!el.checked)p.status='active';$('#status').value=p.status;changed();navigation();});
 for(const key of /** @type {const} */(['notes','evidence']))/** @type {HTMLTextAreaElement} */($('#'+key)).oninput=()=>{const field=/** @type {HTMLTextAreaElement} */($('#'+key));const p=editable(m);p[key]=field.value;if(p.status==='complete'&&!p.evidence.trim()){p.status='active';$('#status').value=p.status;navigation();}changed();};
 $('#save-module').onclick=async()=>{if(await save())notice('Your progress is saved on this computer.');};
 $('#tutor').onclick=async()=>{
  const p=progress(m);
  const prompt=`Act as my tutor for ${course.title}. Module: ${m.title}.\nGoal: ${m.outcome}\nConcepts: ${m.math}\nTask: ${m.tasks.find((_,i)=>!p.tasks[i])||m.challenge}\nMy notes: ${p.notes||'None yet.'}\nEvidence: ${p.evidence||'None yet.'}\nI am an experienced software engineer learning through applied projects. Ask what I predict before showing code. Give one small hint at a time. Ask me to calculate or implement a small example myself. Explain notation through the project. Help me test claims and inspect errors. Do not treat generated code as proof of my understanding. End with this independent challenge: ${m.challenge}`;
  try{await navigator.clipboard.writeText(prompt);notice('Tutor prompt copied. Paste it into your agent.');}catch{const field=$('#prompt-fallback');field.hidden=false;field.value=prompt;field.select();notice('Select and copy the tutor prompt below.');}
 };
}
function experiments(){
 if(accountInvalidated)return;
 $('#journal-curriculum').textContent=course.title;
 $('#journal-switch').onclick=()=>void shell.navigate('library');
 $('#experiment-module').innerHTML=course.modules.map(m=>`<option value="${esc(m.id)}">${esc(m.title)}</option>`).join('');
 restoreDraft();
 $('#experiment-list').innerHTML=journalList(course,state.experiments,editing);
 const canReplaceDraft=()=>!record.draft.experiment||(!record.draft.experiment.title&&!record.draft.experiment.hypothesis&&!record.draft.experiment.result)||confirm('Replace the unfinished experiment draft? Export progress first to keep it.');
 /** @type {NodeListOf<HTMLButtonElement>} */(document.querySelectorAll('[data-edit]')).forEach(b=>b.onclick=()=>{
  const entry=state.experiments.find(x=>x.id===b.dataset.edit);if(!entry||editing===entry.id||!canReplaceDraft())return;
  record.draft.experiment={...entry};changed();experiments();$('#experiment-form').scrollIntoView({behavior:'instant'});
 });
 $('#new-experiment').onclick=()=>{if(!canReplaceDraft())return;record.draft.experiment=null;editing=null;changed();experiments();$('#experiment-form').elements.title.focus();};
}
function guide(){
 if(accountInvalidated)return;
 $('#guide-view').innerHTML=`<p class="eyebrow">A REUSABLE HOME FOR YOUR LEARNING</p><h2>Your approach</h2><p>This plan is a working path, not a degree or an employment guarantee. Use independent work as the measure of progress.</p><div class="form-grid"><label>Start date<input id="start-date" type="date" value="${esc(state.startDate)}"></label><label>Planned hours per week<input id="weekly-hours" type="number" min="1" max="40" value="${state.weeklyHours}"></label></div><p>The course contains ${course.modules.reduce((a,m)=>a+m.hours,0)} estimated study hours. At your current pace, that is about ${Math.ceil(course.modules.reduce((a,m)=>a+m.hours,0)/state.weeklyHours)} weeks. Module week labels describe the original plan.</p><h3>Build → explain → test → reflect</h3><p>Use an agent to explore an idea, inspect an error, or critique your explanation. Predict outputs first. Complete the independent challenge yourself.</p><p>For the soccer course, reserve roughly 6 hours for building, 3 hours for mathematics, and 2 hours for review each week. Use extra time for weak foundations before adding more model types.</p><h3>New topics belong here too</h3><p>Download the curriculum template. Give it to your agent with a topic, starting skills, and time budget. Import the completed JSON file with a unique curriculum ID. Each curriculum keeps its own notes, hours, experiments, and progress.</p><button id="export-course">Export this curriculum</button><h3>Keep a portable copy</h3><p>Export progress regularly. A progress backup includes this curriculum and its learning records. Import a curriculum or a full backup into a new library. To replace an existing record, select it and use Restore backup. The server also keeps the previous saved state on this computer.</p><h3>Next six months</h3>${(course.nextPhase||['Choose your next topic after reviewing the evidence from this course.']).map(x=>`<p>${esc(x)}</p>`).join('')}<p class="muted">Sources reviewed: ${esc(course.researchDate||'Not specified')}. The hub works locally. External readings require internet access. Tutor prompts use your existing agent; this application does not make AI API calls.</p>`;
 $('#start-date').onchange=()=>{state.startDate=$('#start-date').value;changed();};
 $('#weekly-hours').onchange=()=>{const field=$('#weekly-hours');const n=Number(field.value);if(n<1||n>40||!Number.isFinite(n)){field.value=String(state.weeklyHours);return;}state.weeklyHours=n;changed();stats();guide();};
 $('#export-course').onclick=()=>download(course,course.id+'-curriculum.json');
}
/** @param {unknown} value @param {string} name */
function download(value,name){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function render(){
 if(accountInvalidated)return;
 $('#experiment-form').hidden=false;
 $('#plan-title').textContent=course.title;$('#plan-subtitle').textContent=course.subtitle;$('#plan-description').textContent=course.description;$('#module-count').textContent=course.modules.length+' modules';
 $('#curriculum-select').value=record.recordId;screenRecords.set(record.recordId,record);updateScreens(); navigation();detail();experiments();guide();
}
/** @param {string} id @param {boolean} [ignoreRecovery] */
async function loadCourse(id,ignoreRecovery=false){
 const next=library.find(item=>item.recordId===id||item.origin.kind==='local'&&item.curriculumId===id);
 if(!next)throw Error('Unknown learning record.');
 const epoch=viewEpoch;
 const serverRecord=await learning.getRecord(next.recordId,accountScope);
 if(epoch!==viewEpoch)throw Error('The account changed while this record was loading.');
 if(!ignoreRecovery&&!accountInvalidated&&(dirty||saving))throw Error('Your work changed while the record was loading. Save it before switching.');
 record=serverRecord;accountInvalidated=false; dirty=false; recoveryConflict=false; clearTimeout(timer);
 let recovered=false;
 if(!ignoreRecovery)try {
  const raw=memoryRecovery.get(recoveryKey())||localStorage.getItem(recoveryKey());
  if(raw){const pending=JSON.parse(raw);if(pending.version===1&&usableRecovery(pending.record,serverRecord)){
   record=pending.record; dirty=true; recovered=true; recoveryConflict=pending.baseRevision!==serverRecord.state.revision;
  }else notice('A device recovery copy could not be read. The saved record is shown.',true);}
 }catch { recoveryAvailable=false; }
 course=record.curriculum; state=record.state; selected=record.view.selectedModuleId;
 if(record.origin.kind==='local')lastLocalRecordId=record.recordId;
 render();shell.update({ready:true});for(const id of ['import','curriculum-select','add-curriculum','template'])/** @type {HTMLInputElement | HTMLSelectElement | HTMLButtonElement} */($('#'+id)).disabled=false;
 $('#retry').hidden=!recoveryConflict; $('#retry').textContent=recoveryConflict?'Use saved version':'Retry save';
 $('#save-state').textContent=recoveryConflict?'Recovery conflict':recovered?'Unsaved changes':'Saved on this computer';
 if(recoveryConflict)notice('This device has unsaved work based on an older version. Export progress to keep this working copy, then choose Use saved version. Nothing will overwrite the newer saved record.',true);
 else if(recovered){notice('Recovered your unsaved work from this device. Saving locally…');timer=setTimeout(save,500);}
 else notice('Ready. Your progress saves locally as you work.');
 try{localStorage.setItem('touchline-course',record.recordId);}catch{ /* The server record does not depend on browser storage. */ }
}
$('#retry').onclick=async()=>{
 if(!recoveryConflict){await save();return;}
 if(!confirm('Discard the recovered working copy and load the newer saved version? Export progress first if you want to keep this copy.'))return;
 try{clearJournal();await loadCourse(record.recordId,true);}catch(error){notice(message(error)+' Your current working copy is still available for export. Retry loading the saved version.',true);}
};
let importAttempt=0;
/** Lesson modules, lesson packets, and lesson backups get their own read-only review. Curriculum and progress files never come here. The source is the exact text of each file. @param {LessonSource} source @param {'lesson'|'backup'|null} kind @param {string} filename */
function reviewLessons(source,kind,filename){
 openModuleImport(/** @type {HTMLDialogElement} */($('#import-review')),{service:lessons,source,filename,restore:kind==='backup',onFinish:imported=>{if(imported)notice('Lessons added. Your curricula and progress did not change.');}});
}
/** @param {HTMLInputElement} field @param {boolean} restore */
async function previewFile(field,restore){
 const chosen=[...(field.files??[])];const file=chosen[0];if(!file)return;
 const attempt=++importAttempt,epoch=viewEpoch,targetId=record?.recordId;
 const guard=()=>{requireEpoch(epoch);if(attempt!==importAttempt)throw Error('A newer file was selected. Review that file instead.');if(accountInvalidated||record?.recordId!==targetId)throw Error('The selected record changed. Choose the file again.');};
 try{
  /** @type {string[]} */ const texts=[];
  for(const item of chosen){
   if(item.size>5000000)throw Error('The file is too large. Curriculum and backup files must be under 2 MB. Lesson files must be under 5 MB.');
   // Strict decoding: a file that is not valid UTF-8 is refused. Nothing is repaired or guessed.
   try{texts.push(new TextDecoder('utf-8',{fatal:true}).decode(await item.arrayBuffer()));}
   catch{throw Error(`${item.name} is not valid UTF-8 text. Nothing was changed.`);}
   guard();
  }
  if(chosen.length>1){
   chosen.forEach((item,index)=>{
    if(classifyLessonText(texts[index])!=='lesson')throw Error(`${item.name} is not a lesson module or packet. Choose one curriculum or backup file at a time. Several files work only when all of them are lesson files.`);
   });
   reviewLessons(texts,'lesson',file.name);return;
  }
  const fileText=texts[0];
  /** @type {any} */ let input;
  try{input=JSON.parse(fileText);}catch(parseError){
   if(looksLikeLessonText(fileText)){reviewLessons(fileText,null,file.name);return;}
   throw file.size>2000000?Error('The file must be smaller than 2 MB.'):parseError;
  }
  const lessonKind=classifyLessonFile(input);
  if(lessonKind){reviewLessons(fileText,lessonKind,file.name);return;}
  if(file.size>2000000)throw Error('The file must be smaller than 2 MB.');
  let source=prepareImport(input,restore?record:undefined);
  if(restore&&source.format!=='touchline-record')source={...structuredClone(record),curriculum:source.curriculum,state:source.state,draft:{experiment:null}};
  if(restore&&source.curriculum.id!==course.id)throw Error('Switch to the matching curriculum before restoring this backup.');
  if(!(await save()))throw Error('Save or export your current changes before reviewing this file.');guard();
  const expected=state.revision,origin=restore?record.origin:/** @type {LocalRecordOrigin} */({kind:'local'});
  const replacement=restore?{targetRecordId:targetId,expectedRevision:expected}:undefined;
  const preview=await learning.previewImport(source,origin,restore?('status' in source?source.status:record.status):'saved',replacement);guard();
  const dialog=/** @type {HTMLDialogElement} */($('#import-review'));
  let committed=false;
  reviewImport(dialog,{source:preview,restore,filename:file.name,onCommit:async status=>{
   guard();if(state.revision!==expected||dirty||saving)throw Error('Your current work changed. Close this review and choose the file again.');
   if(!committed){await learning.importRecord(source,origin,status,replacement);committed=true;}guard();
   await refreshLibrary();guard();populateCourses();
   if(restore){await loadCourse(targetId);guard();notice('Backup restored.');}
   else notice('Curriculum imported. Select it from Your curricula.');
  }});
  if(restore)$('#export-before-restore').onclick=()=>download(record,course.id+'-progress.json');
 }catch(error){notice(error instanceof Error&&'field' in error&&error.field?String(error.field)+': '+message(error):message(error),true);}finally{field.value='';}
}
$('#import').onchange=()=>previewFile($('#import'),true);
$('#add-curriculum').onclick=()=>$('#curriculum-file').click();
$('#curriculum-file').onchange=()=>previewFile($('#curriculum-file'),false);
$('#template').onclick=()=>download({id:'my-new-topic',version:1,title:'My new topic',subtitle:'MY LEARNING PLAN',description:'Replace this with your learning goal.',weeks:2,weeklyHours:5,resources:{reference:{title:'Example resource: replace this',url:'https://example.com',use:'Explain which section to use.'}},modules:[{id:'intro',title:'First project',phase:'Foundations',question:'What will you investigate?',outcome:'Describe the artifact you will build.',math:'Describe the concepts you will learn. This field can cover any subject.',tasks:['Build a small example.','Explain your result independently.','Record evidence and reflect.'],weeks:['Week 1: Explore and build.','Week 2: Test and explain.'],evidence:'Describe the evidence required for completion.',challenge:'Describe an independent task.',resources:['reference'],reading:'Name the relevant sections.',dataset:'Describe data or materials. Use Not applicable if none are needed.',hours:10,weekStart:1,weekEnd:2,prerequisites:[]}],nextPhase:['Choose a follow-on topic.']},'curriculum-template.json');
async function refreshLibrary() {
 const epoch=viewEpoch;
 const value=await learning.listLibrary(accountScope);requireEpoch(epoch);
 library=value.records;libraryPreference={version:value.version,revision:value.revision,focusRecordId:value.focusRecordId};
 screenRecords.clear();
 const values=await Promise.allSettled(library.map(item=>learning.getRecord(item.recordId,accountScope)));requireEpoch(epoch);
 values.forEach((value,index)=>{if(value.status==='fulfilled')screenRecords.set(library[index].recordId,value.value);});
 updateScreens();populateCourses();
 screens.update({loading:false,error:values.some(value=>value.status==='rejected')?'Some curricula could not load. Retry to read their saved progress.':''});
}
function updateScreens(){
 screens.update({rows:library.map(summary=>({summary:{...summary,status:screenRecords.get(summary.recordId)?.status||summary.status},record:screenRecords.get(summary.recordId)||null})),focusRecordId:libraryPreference.focusRecordId});
}
/** @param {()=>Promise<void>} work */
async function screenAction(work){
 if(screenBusy){screens.update({error:'A request is in progress. Wait for it to finish and try again.'});return;}
 screenBusy=true;screens.update({busy:true,error:''});
 $('#curriculum-select').disabled=true;
 try{if(record&&!accountInvalidated&&!(await save()))throw Error('Save or export your current work before changing curricula.');await work();}
 catch(error){screens.update({error:message(error)});notice(message(error),true);}
 finally{screenBusy=false;screens.update({busy:false});$('#curriculum-select').disabled=accountInvalidated;}
}
screens.bind({
 open:id=>screenAction(async()=>{await loadCourse(id);await shell.navigate('curriculum');}),
 focus:id=>screenAction(async()=>{const epoch=viewEpoch;await learning.saveFocus(id,libraryPreference.revision,accountScope);requireEpoch(epoch);await refreshLibrary();notice(id?'Your focus changed. Other curricula keep their status.':'Focus cleared. Every curriculum keeps its progress.');}),
 status:(id,status)=>screenAction(async()=>{
  const epoch=viewEpoch;
  const operation=(async()=>{
   // The open record must keep its working-copy revision. Fetching a newer
   // revision here could let later edits overwrite another writer's changes.
   const target=record?.recordId===id?structuredClone(record):await learning.getRecord(id,accountScope);requireEpoch(epoch);
   const next=await learning.saveRecord({...target,status},target.state.revision,accountScope);requireEpoch(epoch);
   if(record?.recordId===id){record.status=next.status;record.state.revision=next.state.revision;if(dirty)journal();}
   await refreshLibrary();notice('Curriculum status saved. Its learning history is kept.');
  })();
  statusWrite={recordId:id,epoch,promise:operation};
  try{await operation;}finally{if(statusWrite?.promise===operation)statusWrite=null;}
 }),
 refresh:()=>screenAction(async()=>{screens.update({loading:true});try{await refreshLibrary();populateCourses();}finally{screens.update({loading:false});}}),
 import:()=>$('#curriculum-file').click(),
});
function populateCourses(){
 $('#curriculum-select').innerHTML=library.map(item=>`<option value="${esc(item.recordId)}">${esc(item.title)}${item.origin.kind==='cloud'?' · Cloud copy':''}</option>`).join('');
 if(record)$('#curriculum-select').value=record.recordId;
}
$('#curriculum-select').onchange=async()=>{const field=$('#curriculum-select');const id=field.value;field.disabled=true;try{if(!(await save())){field.value=record.recordId;return;}await loadCourse(id);}catch(error){notice(message(error),true);field.value=record.recordId;}finally{field.disabled=false;}};
$('#experiment-form').oninput=captureDraft;
$('#experiment-module').onchange=captureDraft;
$('#experiment-form').onsubmit=e=>{
 e.preventDefault();if(accountInvalidated)return;const f=$('#experiment-form');
 const entry={id:editing||crypto.randomUUID(),date:record.draft.experiment?.date||new Date().toISOString().slice(0,10),module:f.elements.module.value,title:f.elements.title.value.trim(),hypothesis:f.elements.hypothesis.value.trim(),result:f.elements.result.value.trim()};
 if(!entry.title)return;
 if(editing){const index=state.experiments.findIndex(x=>x.id===editing);if(index<0){notice('The experiment being edited no longer exists. Export your draft before restoring the saved record.',true);return;}entry.date=state.experiments[index].date;state.experiments[index]=entry;}else state.experiments.push(entry);
 record.draft.experiment=null;editing=null;f.reset();changed();experiments();stats();notice('Experiment recorded. Saving locally…');
};
shell.bind({
 navigate:async view=>{
  const epoch=viewEpoch;
  if(accountInvalidated){if(view!=='sync')return false;}
  else {const loaded=(await ready)||!!record;requireEpoch(epoch);if(!loaded&&!['sync','home','library','hub'].includes(view))return false;if(loaded&&!(await save()))return false;requireEpoch(epoch);}
  if(view==='home'||view==='library'){try{await refreshLibrary();requireEpoch(epoch);}catch(error){screens.update({loading:false,error:message(error)});}}
  for(const id of ['hub','home','library','curriculum','experiments','guide','sync'])$('#'+id+'-view').hidden=id!==view;
  if(view==='guide')guide();if(view==='curriculum')detail();
  return true;
 },
 exportProgress:()=>download(record,course.id+'-progress.json'),
 restoreBackup:()=>$('#import').click(),
 reportError:error=>notice(message(error),true),
});
window.addEventListener('beforeunload',e=>{if(dirty||saving||volatileRecovery.size){e.preventDefault();e.returnValue='';}});
async function offerRecovery(){
 const panel=$('#record-recovery');
 try{
  const options=await learning.recoveryOptions();if(!options.length)return;
  panel.hidden=false;
  panel.innerHTML=`<h2>Recover a local learning record</h2><p>A saved record is damaged. Choose a retained local backup to restore. The damaged file stays on this computer.</p><label>Available local backup<select id="recovery-record">${options.map(option=>`<option value="${esc(option.recordId)}">${esc(option.title)} · saved revision ${option.revision}</option>`).join('')}</select></label><button id="recover-record" type="button">Restore saved local copy</button><p id="recovery-error" role="status"></p>`;
  $('#recover-record').onclick=async()=>{
   const option=options.find(item=>item.recordId===/** @type {HTMLSelectElement} */($('#recovery-record')).value);if(!option)return;
   if(!confirm(`Restore the retained backup for ${option.title}? Newer work in the damaged file will remain in a separate recovery file. Reload any other open Schola tabs after recovery.`))return;
   /** @type {HTMLButtonElement} */($('#recover-record')).disabled=true;
   try{await learning.recoverRecord(option.recordId,option.damagedDigest);location.reload();}
   catch(error){$('#recovery-error').textContent=message(error)+' Reload the page to inspect recovery again.';/** @type {HTMLButtonElement} */($('#recover-record')).disabled=false;}
  };
 }catch(error){notice('Recovery could not be inspected. '+message(error)+' Your files remain unchanged.',true);}
}
const ready=(async()=>{try{
 await learning.listCurricula();await refreshLibrary();populateCourses();let id;
 try{id=localStorage.getItem('touchline-course');}catch{ /* Local selection is optional. */ }
 if(!library.length){screens.update({loading:false});return false;}
 await loadCourse(id&&library.some(item=>item.recordId===id||item.origin.kind==='local'&&item.curriculumId===id)?id:library[0].recordId);
 for(const id of ['import','curriculum-select','add-curriculum','template'])/** @type {HTMLInputElement | HTMLSelectElement | HTMLButtonElement} */($('#'+id)).disabled=false;
 return true;
}catch(error){screens.update({loading:false,error:message(error)+' Reload the page to retry.'});notice(message(error)+' Reload the page to retry.',true);await offerRecovery();return false;}})();

/** @type {{token:string,epoch:number,origin:CloudOrigin,source:LocalRecord|LearningBackup,status:LibraryStatus,target:LocalRecord|null}|null} */
let cloudReview=null;
accounts.bind({
 publicationRecords: async()=>{
  const epoch=viewEpoch;if(!(await ready)&&!record)throw Error('The local library is not ready.');requireEpoch(epoch);
  if(statusWrite)await statusWrite.promise;requireEpoch(epoch);
  if(!(await save()))throw Error('Save or export local changes before publishing.');requireEpoch(epoch);
  const value=await learning.listLibrary(accountScope);requireEpoch(epoch);
  const records=await Promise.all(value.records.map(item=>learning.getRecord(item.recordId,accountScope)));requireEpoch(epoch);
  return {records,focusRecordId:value.focusRecordId};
 },
 prepareCloudImport: async(backup,origin,status,targetRecordId)=>{
  const epoch=viewEpoch;cloudReview=null;
  if(accountInvalidated||JSON.stringify(accountScope)!==JSON.stringify(origin))throw Error('Sign in to the matching hub before downloading.');
  const valid=projectPublication(backup);
  const value=await learning.listLibrary(accountScope);requireEpoch(epoch);
  const summary=targetRecordId?value.records.find(item=>item.recordId===targetRecordId):value.records.find(item=>item.origin.kind==='cloud'&&item.curriculumId===valid.curriculum.id);
  if(targetRecordId&&!summary)throw Error('The selected local record is no longer available.');
  const target=summary?(record?.recordId===summary.recordId?structuredClone(record):await learning.getRecord(summary.recordId,accountScope)):null;requireEpoch(epoch);
  if(target&&target.curriculum.id!==valid.curriculum.id)throw Error('The cloud curriculum does not match the local record.');
  if(target&&( !valid.curriculum.modules.some(module=>module.id===target.view.selectedModuleId)||target.draft.experiment&&!valid.curriculum.modules.some(module=>module.id===target.draft.experiment?.module)))throw Error('This cloud curriculum removed the selected lesson or draft module. Export your local backup and finish or move the draft before downloading.');
  const source=target?{...structuredClone(target),curriculum:valid.curriculum,state:valid.state}:valid;
  const token=crypto.randomUUID();cloudReview={token,epoch,origin,source,status:target?.status??status,target};
  return {token,local:target};
 },
 commitCloudImport: async token=>{
  const review=cloudReview;if(!review||review.token!==token)throw Error('Review the cloud snapshot again.');
  const guard=()=>{requireEpoch(review.epoch);if(accountInvalidated||JSON.stringify(accountScope)!==JSON.stringify(review.origin))throw Error('The account changed. Review again.');};guard();
  if(review.target?.recordId===record?.recordId&&(dirty||saving||state.revision!==review.target.state.revision))throw Error('Local work changed after review. Save it and review again.');
  const imported=await learning.importRecord(review.source,review.target?.origin??review.origin,review.status,review.target?{targetRecordId:review.target.recordId,expectedRevision:review.target.state.revision}:undefined);guard();
  cloudReview=null;await refreshLibrary();guard();populateCourses();
  if(review.target?.recordId===record?.recordId){await loadCourse(imported.recordId);guard();}
  return {recordId:imported.recordId,revision:imported.state.revision};
 },
 snapshot: async () => { const epoch=viewEpoch;if(!(await ready)&&!record)throw Error('The local library is not ready.');requireEpoch(epoch); if(!(await save())) throw Error('Save local changes before publishing.');requireEpoch(epoch); return JSON.parse(JSON.stringify({curriculum:course,state})); },
 invalidateAccountScope: async () => {
  cloudReview=null;
  // Session loss must hide private data even when startup failed or is pending.
  if(dirty||saving)journal();
  const oldSave=saving;
  viewEpoch++;const epoch=viewEpoch;accountInvalidated=true;accountScope=null;clearTimeout(timer);dirty=false;recoveryConflict=false;
  shell.update({ready:false,progress:null});
  library=[];screenRecords.clear();libraryPreference={version:1,revision:0,focusRecordId:null};updateScreens();screens.update({loading:true,error:''});populateCourses();
  for(const selector of ['#modules','#module-detail','#experiment-list','#experiment-module','#guide-view'])$(selector).replaceChildren();
  $('#experiment-form').reset();$('#experiment-form').hidden=true;editing=null;$('#experiment-evidence').replaceChildren();$('#journal-curriculum').textContent='';
  /** @type {HTMLDialogElement} */($('#import-review')).close();$('#import-review').replaceChildren();
  $('#plan-title').textContent='Your local learning library';$('#plan-subtitle').textContent='';$('#plan-description').textContent='';$('#module-count').textContent='';
  for(const selector of ['#completed','#hours','#experiments-count','#duration','#pace'])$(selector).textContent='';
  $('#retry').hidden=true;
  for(const id of ['import','curriculum-select','add-curriculum','template'])/** @type {HTMLInputElement | HTMLSelectElement | HTMLButtonElement} */($('#'+id)).disabled=true;
  notice('The account session ended. Cloud work is hidden while your local library loads.');
  try{
   // An already-started write may finish, but cannot update this new view.
   if(oldSave)await oldSave;requireEpoch(epoch);
   await refreshLibrary();requireEpoch(epoch);populateCourses();
   const next=library.find(item=>item.recordId===lastLocalRecordId)||library[0];
   if(!next)throw Error('No local learning record is available.');
   await loadCourse(next.recordId);requireEpoch(epoch);
   if(volatileRecovery.size)notice('Cloud work is hidden and held in this page only. Keep the page open and sign in again to recover or export it.',true);
  }catch(error){if(epoch!==viewEpoch)return;notice('Cloud work remains hidden. '+message(error)+' Reload to retry your local library.',true);}
 },
 setAccountScope: async origin => {
  const epoch=viewEpoch;
  if(!(await ready)&&!record)throw Error('The local library is not ready.');
  requireEpoch(epoch);
  if(!accountInvalidated&&!(await save()))throw Error('Save or export local changes before changing accounts.');
  requireEpoch(epoch);
  const previousScope=accountScope,previousLibrary=library;accountScope=origin;
  try{await refreshLibrary();requireEpoch(epoch);
  populateCourses();
  const next=library.find(item=>item.recordId===record.recordId)||library.find(item=>item.recordId===lastLocalRecordId&&item.origin.kind==='local')||library.find(item=>item.origin.kind==='local');
  if(!next)throw Error('No local learning record is available.');
  await loadCourse(next.recordId);requireEpoch(epoch);
  }catch(error){if(epoch===viewEpoch){accountScope=previousScope;library=previousLibrary;populateCourses();}throw error;}
 },
 importCloud: async (backup,origin) => {
  const epoch=viewEpoch;
  if(!(await ready)&&!record)throw Error('The local library is not ready.');
  requireEpoch(epoch);
  if(!origin)throw Error('Sign in before importing a cloud record.');
  if(!(await save()))throw Error('Save or export local changes first.');
  requireEpoch(epoch);
  if(JSON.stringify(accountScope)!==JSON.stringify(origin))await accounts.service.setAccountScope(origin);
  requireEpoch(epoch);await refreshLibrary();requireEpoch(epoch);
  const existing=library.find(item=>item.origin.kind==='cloud'&&item.curriculumId===backup.curriculum.id);
  if(existing&&!confirm('Replace this account’s downloaded cloud copy with the latest published version? Export progress first to keep local changes.'))return false;
  const imported=await learning.importRecord(backup,origin,'active',existing?{targetRecordId:existing.recordId,expectedRevision:existing.revision}:undefined);
  requireEpoch(epoch);await refreshLibrary();requireEpoch(epoch);populateCourses();await loadCourse(imported.recordId);requireEpoch(epoch);return true;
 },
 download
});
// Compatibility facade for the current connection view. New clients use accounts.service.
window.touchline=accounts.service;
