// Run against the local hub with Playwright installed. API writes are mocked.
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
import assert from 'node:assert/strict';
import {mkdir} from 'node:fs/promises';
import {join} from 'node:path';
import {isolateContext} from './browser/network.mjs';
const appUrl = process.env.TOUCHLINE_URL || 'http://127.0.0.1:8765/';
/** The app opens on the Hub. These checks work in the learning path. */
const openCurriculum=async target=>{await target.locator('[data-view=curriculum]').click();await target.locator('#notes').waitFor();};
const artifactDirectory = process.env.TOUCHLINE_ARTIFACT_DIR || 'artifacts';
await mkdir(artifactDirectory, {recursive: true});
const browser = await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined});
const errors=[];
const collectErrors=context=>context.on('page',page=>page.on('pageerror',error=>errors.push(error.message)));
try {
const context = await browser.newContext({viewport:{width:1440,height:1000},colorScheme:'dark',serviceWorkers:'block'});
await isolateContext(context, new URL(appUrl).origin, {mockLearningWrites:true});
collectErrors(context);
const page = await context.newPage();
// Learning edits below are test-only: never write to the user's data or backend.
await page.goto(appUrl);await openCurriculum(page);
const theme=()=>page.locator('html').getAttribute('data-theme');
assert.equal(await theme(),'noir-bloom');
const course=await page.locator('#curriculum-select').inputValue();
const module=await page.locator('.module.selected').getAttribute('data-module');
await page.locator('#notes').fill('Theme test: preserve this in-progress draft.');
await page.getByRole('button',{name:'Change theme',exact:true}).click();
await page.evaluate(()=>document.fonts.ready);
assert.deepEqual(await page.evaluate(()=>[...document.fonts].filter(font=>font.family.startsWith('Touchline Appearance')).map(font=>font.status)),['loaded','loaded']);
assert.match(await page.locator('.appearance-current').textContent(), /Following your device · Dark/);
assert.equal(await page.locator('#appearance-panel').evaluate(el => Math.round(el.getBoundingClientRect().width)),360);
for(const [pair,dark,light] of [['bloom','noir-bloom','confetti-studio'],['coast','dusk-protocol','coastal-day']]){
 await page.locator(`input[name=theme-pair][value=${pair}]`).check();
 for(const mode of ['dark','light','system']){
  await page.locator(`input[name=theme-mode][value=${mode}]`).check();
  assert.equal(await theme(),mode==='light'?light:dark);
  assert.match(await page.locator('.appearance-current').textContent(),mode==='system'?/Following your device · Dark/:new RegExp('Always '+mode));
  assert.equal(await page.locator('#notes').inputValue(),'Theme test: preserve this in-progress draft.');
  assert.equal(await page.locator('#curriculum-select').inputValue(),course);
  assert.equal(await page.locator('.module.selected').getAttribute('data-module'),module);
 }
 await page.emulateMedia({colorScheme:'light'});await page.waitForFunction(id=>document.documentElement.dataset.theme===id,light);assert.equal(await theme(),light);
 await page.emulateMedia({colorScheme:'dark'});await page.waitForFunction(id=>document.documentElement.dataset.theme===id,dark);assert.equal(await theme(),dark);
}
await page.screenshot({path:join(artifactDirectory,'appearance-desktop-dark.png')});
await page.keyboard.press('Escape');assert.equal(await page.getByRole('button',{name:'Change theme',exact:true}).getAttribute('aria-expanded'),'false');
assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('aria-label')),'Change theme');
await page.reload();await openCurriculum(page);assert.equal(await theme(),'dusk-protocol');
await page.getByRole('button',{name:'Change theme',exact:true}).click();
await page.locator('input[name=theme-mode][value=light]').check();
await page.emulateMedia({colorScheme:'dark'});assert.equal(await theme(),'coastal-day');
await page.screenshot({path:join(artifactDirectory,'appearance-desktop-light.png')});
const second=await context.newPage();await second.goto(appUrl);await openCurriculum(second);assert.equal(await second.locator('html').getAttribute('data-theme'),'coastal-day');
await page.locator('input[name=theme-pair][value=bloom]').check();await second.waitForFunction(()=>document.documentElement.dataset.theme==='confetti-studio');await second.close();
for(const width of [390,768]){
 await page.setViewportSize({width,height:844});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'viewport overflow');
 const bounds=await page.locator('#appearance-panel').boundingBox();assert.ok(bounds.x>=0&&bounds.x+bounds.width<=width);
 assert.equal(await page.locator('.appearance-done').evaluate(el=>getComputedStyle(el).fontSize),'14px');
 await page.screenshot({path:join(artifactDirectory,`appearance-${width}-light.png`)});
}
await page.getByRole('button',{name:'Done',exact:true}).click();
await page.getByRole('button',{name:'Change theme',exact:true}).click();await page.mouse.click(5,5);assert.equal(await page.locator('#appearance-panel').count(),0);
await page.evaluate(()=>localStorage.setItem('touchline-appearance','invalid json'));
await page.reload();await openCurriculum(page);assert.equal(await theme(),'noir-bloom');
const blocked=await browser.newContext({colorScheme:'light',serviceWorkers:'block'});await isolateContext(blocked,new URL(appUrl).origin,{mockLearningWrites:true});await blocked.addInitScript(()=>{
 const getItem=Storage.prototype.getItem,setItem=Storage.prototype.setItem;
 window.allowAppearanceReads=()=>{Storage.prototype.getItem=getItem;};
 window.allowAppearanceWrites=()=>{Storage.prototype.setItem=setItem;};
 Storage.prototype.getItem=()=>{throw new DOMException('Blocked','SecurityError')};
 Storage.prototype.setItem=()=>{throw new DOMException('Blocked','SecurityError')};
});
collectErrors(blocked);
const p=await blocked.newPage();await p.goto(appUrl);await openCurriculum(p);assert.equal(await p.locator('html').getAttribute('data-theme'),'confetti-studio');await p.getByRole('button',{name:'Change theme',exact:true}).click();
assert.equal(await p.locator('.appearance-note').textContent(),'Only for this tab. Preferences could not be saved.');
for (const key of ['unrelated-preference','touchline-appearance',null]) {
 await p.evaluate(key=>window.dispatchEvent(new StorageEvent('storage',{key})),key);
 assert.equal(await p.locator('.appearance-note').textContent(),'Only for this tab. Preferences could not be saved.');
}
await p.locator('input[name=theme-mode][value=dark]').check();assert.equal(await p.locator('html').getAttribute('data-theme'),'noir-bloom');
assert.equal(await p.locator('.appearance-note').textContent(),'Only for this tab. Preferences could not be saved.');
await p.evaluate(()=>window.allowAppearanceWrites());
await p.locator('input[name=theme-mode][value=light]').check();
assert.equal(await p.locator('.appearance-note').textContent(),'Saved on this device.');
await p.evaluate(()=>window.dispatchEvent(new StorageEvent('storage',{key:'touchline-appearance'})));
assert.equal(await p.locator('.appearance-note').textContent(),'Only for this tab. Preferences could not be saved.');
await p.evaluate(()=>{window.allowAppearanceReads();window.dispatchEvent(new StorageEvent('storage',{key:'touchline-appearance'}));});
assert.equal(await p.locator('.appearance-note').textContent(),'Saved on this device.');
assert.equal(await p.locator('html').getAttribute('data-theme'),'confetti-studio');
assert.deepEqual(errors,[]);
console.log('PASS: all six pair/mode choices, live system changes, manual override, reload and cross-tab persistence, draft/course/module preservation, Escape/focus and outside dismissal, phone/tablet sizing, corrupt and unavailable storage. No page errors.');
} finally {
  await browser.close();
}
