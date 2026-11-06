import assert from 'node:assert/strict';
import {mkdir} from 'node:fs/promises';
import {createIsolatedServer} from './server.mjs';
import {isolateContext} from './network.mjs';
import {seedHub, makeEvent, session, when} from './hub-seed.mjs';
import {sampleHub} from '../hub-fixture.mjs';

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const FOCUS_RING = 'rgb(240, 178, 62)';
const NEVER_SHOWN = /streak|leaderboard|ranking|\brank\b|struggl|guessing|frustrat|total time|time spent|hours logged/i;

/** The sentence a heat cell holds for a day that many days before today, in UTC. */
function cellSentence(daysAgo, count) {
  const day = new Date(when(daysAgo));
  return `${WEEKDAYS[day.getUTCDay()]} ${day.toISOString().slice(0, 10)}: ${count} ${count === 1 ? 'session' : 'sessions'}`;
}

/** Open the app in its own context and go to the Hub view. UTC makes the days the same on every machine. */
async function openHub(browser, server, {viewport = {width: 1280, height: 900}, reducedMotion = 'no-preference', prepare, wait = true} = {}) {
  const context = await browser.newContext({serviceWorkers: 'block', timezoneId: 'UTC', locale: 'en-US', viewport, reducedMotion});
  const external = await isolateContext(context, server.url);
  const errors = [];
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error' && /Content Security Policy|font/i.test(message.text())) errors.push('console: ' + message.text()); });
  if (prepare) await prepare(page);
  await page.goto(server.url);
  await page.locator('[data-view=hub]').click();
  if (wait) await page.locator('#hub-view [data-hub-state]:not([data-hub-state=loading])').waitFor();
  return {context, page, external, errors};
}

async function withServer(spec, run) {
  const server = await createIsolatedServer();
  try {
    if (spec) await seedHub(server.directory, spec);
    await run(server);
  } finally { await server.close(); }
}

async function assertCalm(page) {
  const text = await page.locator('#hub-view').textContent();
  assert.doesNotMatch(text, NEVER_SHOWN, 'the hub shows no streak, ranking, total time, or label about the student');
}

const FILES = [
  'ibm-plex-sans-latin-400-normal', 'ibm-plex-sans-latin-500-normal', 'ibm-plex-sans-latin-600-normal',
  'ibm-plex-mono-latin-400-normal', 'ibm-plex-mono-latin-500-normal',
];

async function verifyFontRoutes(server) {
  for (const name of FILES) {
    const response = await fetch(`${server.url}/assets/fonts/${name}.woff2`);
    assert.equal(response.status, 200, name);
    assert.equal(response.headers.get('content-type'), 'font/woff2');
    assert.equal(Buffer.from(await response.arrayBuffer()).subarray(0, 4).toString('latin1'), 'wOF2', name);
  }
  for (const path of ['/assets/fonts/IBM-Plex-Sans-OFL-1.1.txt', '/assets/fonts/', '/assets/fonts/..%2Fserver.py', '/assets/fonts/nothing.woff2']) {
    assert.equal((await fetch(server.url + path)).status, 404, path);
  }
}

// 1. A new install: no modules, no events.
async function verifyEmptyInstall(browser) {
  await withServer(null, async server => {
    await verifyFontRoutes(server);
    const {page, context, external, errors} = await openHub(browser, server);
    try {
      assert.equal(await page.locator('#hub-view [data-hub-state]').getAttribute('data-hub-state'), 'empty');
      await page.getByText('No activity yet').waitFor();
      assert.match(await page.locator('[data-empty=focus]').innerText(), /No lesson modules yet/);
      assert.match(await page.locator('[data-empty=skills]').innerText(), /need modules that tag their checks with skills/);
      assert.match(await page.locator('[data-empty=checks]').innerText(), /No check has a result yet/);
      assert.match(await page.locator('[data-empty=library]').innerText(), /No lesson modules yet/);
      assert.match(await page.locator('[data-empty=checkpoints]').innerText(), /No checkpoints yet/);
      assert.equal((await page.locator('[data-checkpoint-count]').innerText()).replace(/\s+/g, ' '), '0 of 0 done');
      assert.equal(await page.locator('[data-heat] tbody tr').count(), 8);
      assert.equal(await page.locator('[data-skill]').count(), 0, 'no fake skill meter');
      assert.deepEqual(await page.locator('[data-notice]').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-notice'))), ['empty-log']);
      assert.equal(await page.locator('#hub-view a').count(), 0);
      await assertCalm(page);
      // The fonts load from the local server under the page's own content policy.
      const loaded = await page.evaluate(async () => (await Promise.all([
        document.fonts.load("500 14px 'Schola Hub Plex Mono'"), document.fonts.load("400 14px 'Schola Hub Plex Sans'"),
      ])).map(list => list.length));
      assert.deepEqual(loaded, [1, 1]);
      assert.deepEqual(external, []);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}

const alpha = {
  id: 'alpha', title: 'Reading a shot map',
  tasks: ['Open the shot map.', 'Mark each shot by outcome.', 'Count the shots by zone.', 'Write what the map shows.'],
  checks: [['check-1', 'Count the shots in the penalty area.'], ['check-2', 'Name the zone with the most shots.']],
};
const beta = {id: 'beta', title: 'A second module', tasks: ['Read the notes.', 'Answer the question.'], checks: [['check-1', 'Answer in one sentence.']]};
const gamma = {id: 'gamma', title: 'A finished module', tasks: ['Read.', 'Write.'], checks: [['check-1', 'Confirm the write-up.']]};

/** Events written for a student who finished one module and is half way through another. */
function studyEvents() {
  const old = session('old'), mid = session('mid'), recent = session('recent'), now = session('now');
  const at = (days, hour = 9) => when(days, hour);
  return [
    makeEvent('task_opened', {time: at(15), session: old, module: 'gamma', task: 'task-1'}),
    makeEvent('task_finished', {time: at(15, 10), session: old, module: 'gamma', task: 'task-1'}),
    makeEvent('task_finished', {time: at(15, 11), session: old, module: 'gamma', task: 'task-2'}),
    makeEvent('check_result', {time: at(15, 12), session: old, module: 'gamma', task: 'task-2', data: {status: 'passed'}}),
    makeEvent('module_completed', {time: at(15, 13), session: old, module: 'gamma', data: {checksPassed: 1, tasksTotal: 2}}),
    makeEvent('task_opened', {time: at(3), session: mid, task: 'task-1'}),
    makeEvent('hint_requested', {time: at(3, 10), session: mid, task: 'task-1', data: {level: 'nudge'}}),
    makeEvent('hint_requested', {time: at(3, 11), session: mid, task: 'task-1', data: {level: 'question'}}),
    makeEvent('check_result', {time: at(3, 12), session: mid, task: 'task-1', data: {status: 'failed'}}),
    makeEvent('check_result', {time: at(3, 13), session: mid, task: 'task-1', data: {status: 'failed'}}),
    makeEvent('check_result', {time: at(3, 14), session: mid, task: 'task-1', data: {status: 'passed'}}),
    makeEvent('task_finished', {time: at(3, 15), session: mid, task: 'task-1'}),
    makeEvent('task_opened', {time: at(1), session: recent, task: 'task-2'}),
    makeEvent('task_finished', {time: at(1, 10), session: recent, task: 'task-2'}),
    makeEvent('task_opened', {time: at(0, 0), session: now, task: 'task-3'}),
  ];
}

// 2. A seeded module with events written through the real event log.
async function verifySeeded(browser) {
  await withServer({modules: [alpha, beta, gamma], events: studyEvents()}, async server => {
    const {page, context, external, errors} = await openHub(browser, server);
    try {
      assert.equal(await page.locator('#hub-view [data-hub-state]').getAttribute('data-hub-state'), 'ready');
      assert.equal(await page.locator('[data-focus-title]').innerText(), 'Reading a shot map');
      assert.equal(await page.locator('[data-next-task]').innerText(), 'Count the shots by zone.');
      assert.match(await page.locator('.hub-meta').innerText(), /tasks done\s*2\/4/);
      assert.equal(await page.locator('[data-task-meter]').getAttribute('aria-label'), '2 of 4 tasks done');
      assert.equal(await page.locator('[data-task-meter] i[data-state=done]').count(), 2);
      assert.equal((await page.locator('[data-checkpoint-count]').innerText()).replace(/\s+/g, ' '), '1 of 3 done');
      assert.equal(await page.locator('[data-checkpoint-grid] li[data-state=done]').count(), 1);
      assert.equal(await page.locator('[data-checkpoint-grid] li[data-state=next]').count(), 1);
      // Participation: four sessions on four days, plain counts.
      const readout = (await page.locator('[data-panel=hub-participation] .hub-readout').innerText()).replace(/\s+/g, ' ');
      assert.match(readout, /^4 sessions 4 active days \d+ days in 8 weeks$/);
      for (const days of [15, 3, 1]) assert.equal(await page.getByRole('cell', {name: cellSentence(days, 1)}).count(), 1, cellSentence(days, 1));
      assert.equal(await page.getByRole('cell', {name: cellSentence(0, 1)}).count(), 1);
      // The check: two retries before the first pass, hint level question.
      const row = page.locator('[data-checks] tbody tr', {hasText: 'Count the shots in the penalty area.'});
      assert.equal(await row.count(), 1);
      const cells = (await row.locator('td').allInnerTexts()).map(text => text.replace(/\s+/g, ' ').trim());
      assert.equal(cells[1], '2');
      assert.match(cells[2], /question$/);
      assert.doesNotMatch(cells[0], /Not passed yet/);
      // The library: the order of the store, one status each.
      const library = await page.locator('[data-library] li').evaluateAll(nodes => nodes.map(node => [node.getAttribute('data-status'), node.querySelector('.hub-lib-title').textContent, node.querySelector('.hub-lib-state').textContent]));
      assert.deepEqual(library, [
        ['focus', 'Reading a shot map', 'In focus · 2 of 4 tasks'],
        ['next', 'A second module', 'Next · 0 of 2 tasks'],
        ['done', 'A finished module', 'Done · 2 of 2 tasks'],
      ]);
      // Skills stay empty until modules tag skills: an honest empty state, never a meter.
      assert.match(await page.locator('[data-empty=skills]').innerText(), /need modules that tag their checks with skills/);
      assert.equal(await page.locator('[data-skill]').count(), 0);
      assert.equal(await page.locator('[data-notice]').count(), 0);
      // The "how is this computed" view opens and closes.
      const how = page.getByRole('button', {name: 'How is this computed?'});
      assert.equal(await how.getAttribute('aria-expanded'), 'false');
      await how.click();
      assert.equal(await how.getAttribute('aria-expanded'), 'true');
      await page.getByText('Only a result of passed counts as a pass.').waitFor();
      await how.click();
      assert.equal(await how.getAttribute('aria-expanded'), 'false');
      assert.equal(await page.locator('#hub-view a').count(), 0);
      await assertCalm(page);
      // Leaving and coming back loads the numbers again.
      await page.locator('[data-view=library]').click();
      await page.locator('#hub-view').waitFor({state: 'hidden'});
      await page.locator('[data-view=hub]').click();
      await page.locator('[data-focus-title]').waitFor();
      assert.deepEqual(external, []);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}

// 3. A damaged log, a cut last line, a future event, and a check result with no matching check each show a notice.
async function verifyFindings(browser) {
  const events = [
    makeEvent('task_opened', {time: when(0, 0), session: session('f1'), task: 'task-1'}),
    makeEvent('check_result', {time: when(0, 1), session: session('f1'), task: 'task-1', data: {checkId: 'check-zzz', status: 'failed'}}),
    makeEvent('task_opened', {time: when(-3), session: session('f2'), task: 'task-1'}),
  ];
  await withServer({modules: [alpha], events, rawAppend: '{"not":"an event"}\n{"schemaVersion":"1.0","eventId":"cut'}, async server => {
    const {page, context, errors} = await openHub(browser, server);
    try {
      assert.equal(await page.locator('#hub-view [data-hub-state]').getAttribute('data-hub-state'), 'ready');
      const ids = await page.locator('[data-notice]').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-notice')));
      assert.deepEqual(ids, ['damaged', 'truncated', 'future', 'unmatched']);
      const notices = (await page.locator('[data-notices]').innerText()).replace(/\s+/g, ' ');
      assert.match(notices, /1 line of the log is not valid/);
      assert.match(notices, /The last line of your event log is cut off/);
      assert.match(notices, /1 event is dated after today/);
      assert.match(notices, /1 check result names a check/);
      // The valid events still count. Nothing turns into a silent zero.
      assert.equal(await page.locator('[data-focus-title]').innerText(), 'Reading a shot map');
      assert.match((await page.locator('[data-panel=hub-participation] .hub-readout').innerText()).replace(/\s+/g, ' '), /^1 session 1 active day/);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
  // A log that cannot be read at all: the log file is a symbolic link.
  await withServer({modules: [alpha], events: events.slice(0, 1), logAsSymlink: true}, async server => {
    const {page, context, errors} = await openHub(browser, server);
    try {
      assert.equal(await page.locator('#hub-view [data-hub-state]').getAttribute('data-hub-state'), 'ready', 'a fault is not an empty log');
      assert.deepEqual(await page.locator('[data-notice]').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-notice'))), ['fault']);
      assert.match(await page.locator('[data-notice=fault]').innerText(), /Schola cannot read your event log/);
      assert.match(await page.locator('[data-notice=fault]').innerText(), /symbolic link/);
      assert.equal(await page.locator('[data-focus-title]').innerText(), 'Reading a shot map', 'the lessons still show');
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}

// 4. Text from a lesson author is plain text, whatever it holds.
async function verifyHostileText(browser) {
  const evil = {
    id: 'evil',
    title: 'Evil \u{202E}title \u{200B}<b>bold</b> [link](https://example.test) \u{2066}',
    tasks: ['Run <img src=x onerror="window.__pwned=1"> now \u{202E}reversed'],
    checks: [['check-1', 'Check <script>window.__pwned=2</script> \u{7}bell **bold** https://example.test/auto']],
  };
  const events = [
    makeEvent('task_opened', {time: when(0, 0), session: session('h1'), module: 'evil', task: 'task-1'}),
    makeEvent('check_result', {time: when(0, 1), session: session('h1'), module: 'evil', task: 'task-1', data: {status: 'failed'}}),
  ];
  await withServer({modules: [evil], events}, async server => {
    const {page, context, errors} = await openHub(browser, server);
    try {
      const hub = page.locator('#hub-view');
      assert.equal(await page.locator('[data-focus-title]').innerText(), 'Evil <U+202E>title <U+200B><b>bold</b> [link](https://example.test) <U+2066>');
      assert.equal(await page.locator('[data-next-task]').innerText(), 'Run <img src=x onerror="window.__pwned=1"> now <U+202E>reversed');
      assert.match(await page.locator('[data-checks]').innerText(), /Check <script>window\.__pwned=2<\/script> <U\+0007>bell \*\*bold\*\* https:\/\/example\.test\/auto/);
      assert.equal(await hub.locator('img, script, strong, a, iframe, b:not(.hub-readout b, .hub-cpn b)').count(), 0, 'no element came from the text');
      assert.equal(await page.evaluate(() => window.__pwned), undefined);
      const raw = await hub.evaluate(node => node.textContent);
      assert.equal([...raw].some(char => ['\u{202E}', '\u{200B}', '\u{2066}', '\u{7}'].includes(char)), false, 'no raw control or bidi character is on the page');
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}

// 5. A failed request, an unreadable answer, and a server that does not answer each have a state with a retry.
async function verifyFailures(browser) {
  await withServer({modules: [alpha], events: studyEvents().slice(-1)}, async server => {
    const {page, context, errors} = await openHub(browser, server, {
      wait: false,
      prepare: page => page.route('**/api/hub*', route => route.fulfill({status: 500, json: {error: 'broke <b>', code: 'internal_error', retryable: false}})),
    });
    try {
      const failed = page.locator('[data-hub-state=error]');
      await failed.waitFor();
      assert.match(await failed.innerText(), /Schola cannot load your hub/);
      assert.match(await failed.innerText(), /Schola hit an unexpected problem and could not build the hub\./);
      assert.equal(await page.locator('[data-focus-title]').count(), 0, 'old or invented numbers are not shown beside an error');
      await page.unroute('**/api/hub*');
      await page.getByRole('button', {name: 'Try again'}).click();
      await page.locator('[data-hub-state=ready]').waitFor();
      assert.equal(await page.locator('[data-focus-title]').innerText(), 'Reading a shot map');
      // An answer with the wrong shape is an error, not a crash.
      await page.route('**/api/hub*', route => route.fulfill({status: 200, json: {schemaVersion: '9.9'}}));
      await page.locator('[data-view=library]').click();
      await page.locator('[data-view=hub]').click();
      await page.locator('[data-hub-state=error]').waitFor();
      assert.match(await page.locator('[data-hub-state=error]').innerText(), /answer Schola could not read/);
      // A server that does not answer.
      await page.unroute('**/api/hub*');
      await page.route('**/api/hub*', route => route.abort('failed'));
      await page.getByRole('button', {name: 'Try again'}).click();
      await page.getByText('The local server did not answer.').waitFor();
      await page.unroute('**/api/hub*');
      await page.getByRole('button', {name: 'Try again'}).click();
      await page.locator('[data-hub-state=ready]').waitFor();
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
  // The time zone name is sent first. If the server refuses the field, the offset is sent once.
  await withServer(null, async server => {
    const urls = [];
    const {context, errors} = await openHub(browser, server, {
      prepare: async page => {
        await page.route('**/api/hub*', route => {
          urls.push(new URL(route.request().url()).search);
          return urls.length === 1 ? route.fulfill({status: 400, json: {error: 'bad zone', code: 'invalid_request', retryable: false, field: 'timeZone'}}) : route.continue();
        });
      },
    });
    try {
      assert.deepEqual(urls, ['?timeZone=UTC', '?tzOffsetMinutes=0']);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}

// 6. Keyboard: a visible amber ring, a sensible order, and the how-view works from the keyboard.
async function verifyKeyboard(browser) {
  await withServer({modules: [alpha, beta, gamma], events: studyEvents()}, async server => {
    const {page, context, errors} = await openHub(browser, server);
    try {
      await page.locator('[data-view=hub]').focus();
      const stops = [];
      for (let step = 0; step < 12; step += 1) {
        await page.keyboard.press('Tab');
        const stop = await page.evaluate(() => {
          const node = document.activeElement;
          if (!node || !node.closest('#hub-view')) return null;
          const style = getComputedStyle(node);
          return {name: node.getAttribute('aria-label') || node.textContent.trim().slice(0, 40), tag: node.tagName, ring: style.outlineStyle + ' ' + style.outlineWidth + ' ' + style.outlineColor};
        });
        if (stop) stops.push(stop);
        else if (stops.length) break;
      }
      assert.equal(stops.length, 2, 'the Hub view has two tab stops: the how-view and the scrollable library');
      assert.ok(stops[0].name.startsWith('How is this computed?'), stops[0].name);
      assert.equal(stops[0].tag, 'BUTTON');
      assert.ok(stops[1].name.startsWith('Library'), stops[1].name);
      for (const stop of stops) assert.equal(stop.ring, 'solid 2px ' + FOCUS_RING, stop.name);
      const back = page.getByRole('button', {name: 'How is this computed?'});
      await back.focus();
      const ring = await back.evaluate(node => { const style = getComputedStyle(node); return {style: style.outlineStyle, width: style.outlineWidth, color: style.outlineColor}; });
      assert.deepEqual(ring, {style: 'solid', width: '2px', color: FOCUS_RING});
      await page.keyboard.press('Enter');
      assert.equal(await back.getAttribute('aria-expanded'), 'true');
      await page.keyboard.press('Space');
      assert.equal(await back.getAttribute('aria-expanded'), 'false');
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
  // Reduced motion: no animation. Otherwise a short fade on the content.
  await withServer(null, async server => {
    const calm = await openHub(browser, server, {reducedMotion: 'reduce'});
    try {
      assert.equal(await calm.page.locator('.hub-fade').first().evaluate(node => getComputedStyle(node).animationName), 'none');
    } finally { await calm.context.close(); }
    const normal = await openHub(browser, server, {reducedMotion: 'no-preference'});
    try {
      assert.equal(await normal.page.locator('.hub-fade').first().evaluate(node => getComputedStyle(node).animationName), 'hub-fade');
      assert.equal(await normal.page.locator('.hub-fade').first().evaluate(node => getComputedStyle(node).animationDuration), '0.12s');
    } finally { await normal.context.close(); }
  });
}

// 7. Layout at three widths: no page scroll sideways, a 44 px target on small screens, the rail folds below.
async function verifyLayout(browser, artifacts) {
  const modules = Array.from({length: 24}, (_, index) => ({
    id: 'module-' + String(index + 1).padStart(2, '0'),
    title: index === 0 ? 'W'.repeat(150) : `Module number ${index + 1} with a fairly long title that wraps`,
    tasks: ['A task.', 'Another task.'],
    checks: [['check-1', index === 0 ? 'C'.repeat(190) : 'A check that has a longer description than the table column holds at a small width.']],
  }));
  const events = [
    makeEvent('task_opened', {time: when(0, 0), session: session('l1'), module: 'module-01'}),
    makeEvent('check_result', {time: when(0, 1), session: session('l1'), module: 'module-01', data: {status: 'failed'}}),
    makeEvent('check_result', {time: when(1, 1), session: session('l2'), module: 'module-02', data: {status: 'passed'}}),
  ];
  await withServer({modules, events}, async server => {
    const {page, context, errors} = await openHub(browser, server, {viewport: {width: 1280, height: 900}});
    try {
      for (const [width, height] of [[375, 812], [768, 1024], [1280, 900]]) {
        await page.setViewportSize({width, height});
        await page.locator('[data-focus-title]').waitFor();
        const report = await page.evaluate(() => {
          const hub = document.querySelector('#hub-view');
          const limit = innerWidth + 1;
          const wide = [...hub.querySelectorAll('*')].filter(node => !node.closest('.hub-sr') && !node.classList.contains('hub-sr') && node.getBoundingClientRect().right > limit)
            .map(node => node.tagName + '.' + node.className).slice(0, 5);
          const scrollers = [...hub.querySelectorAll('*')].filter(node => ['auto', 'scroll'].includes(getComputedStyle(node).overflowX) && node.scrollWidth > node.clientWidth + 1)
            .map(node => node.tagName + '.' + node.className).slice(0, 5);
          const small = [...hub.querySelectorAll('button')].filter(node => node.getBoundingClientRect().height < 43.5 || node.getBoundingClientRect().width < 43.5)
            .map(node => node.textContent.trim().slice(0, 30));
          const rail = document.querySelector('.hub-rail').getBoundingClientRect();
          const main = document.querySelector('.hub-main').getBoundingClientRect();
          return {page: document.documentElement.scrollWidth <= innerWidth && document.body.scrollWidth <= innerWidth, wide, scrollers, small,
            railBelow: rail.top >= main.bottom - 1, railLeft: rail.right <= main.left + 1};
        });
        assert.equal(report.page, true, `no sideways page scroll at ${width}`);
        assert.deepEqual(report.wide, [], `nothing spills past the right edge at ${width}`);
        assert.deepEqual(report.scrollers, [], `no inner sideways scroll at ${width}`);
        assert.deepEqual(report.small, [], `every button is at least 44 px at ${width}`);
        if (width <= 960) assert.equal(report.railBelow, true, `the rail folds below at ${width}`);
        else assert.equal(report.railLeft, true, `the rail sits left at ${width}`);
        if (artifacts) await page.screenshot({path: `${artifacts}/hub-${width}.png`, fullPage: true});
      }
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}

// 8. Parts the real stores cannot produce yet: skills, a cut list, a skipped event. The answer is stubbed.
async function verifyStubbedFindings(browser) {
  const skills = Array.from({length: 3}, (_, index) => ({skillId: `skill-${index + 1}`, passed: index + 1, tried: 4, window: 5}));
  const hub = sampleHub({
    log: {exists: true, ok: true, fault: null, events: 40, damagedLines: 0, truncatedTail: false, skippedEvents: 2, futureEvents: 0, unmatchedEvents: 0},
    skills, skillsTotal: 130, checks: sampleHub().checks, checksTotal: 45,
  });
  await withServer(null, async server => {
    const {page, context, errors} = await openHub(browser, server, {
      prepare: page => page.route('**/api/hub*', route => route.fulfill({json: hub})),
    });
    try {
      assert.deepEqual(await page.locator('[data-notice]').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-notice'))), ['skipped', 'checks-cut', 'skills-cut']);
      assert.match(await page.locator('[data-notice=skipped]').innerText(), /2 events had a field that Schola cannot read/);
      assert.match(await page.locator('[data-notice=skills-cut]').innerText(), /Showing 3 of 130 skills/);
      assert.match(await page.locator('[data-notice=checks-cut]').innerText(), /Showing 1 of 45 checks/);
      const rows = (await page.locator('[data-skill]').allInnerTexts()).map(text => text.replace(/\s+/g, ' ').trim());
      assert.deepEqual(rows, ['skill-1 1/4 25%', 'skill-2 2/4 50%', 'skill-3 3/4 75%']);
      assert.equal(await page.locator('[data-skill] .hub-cells i[data-state=pass]').count(), 6);
      assert.equal(await page.locator('[data-empty=skills]').count(), 0);
      await assertCalm(page);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}

export async function verifyHub({browser, artifacts}) {
  if (artifacts) await mkdir(artifacts, {recursive: true});
  await verifyEmptyInstall(browser);
  await verifySeeded(browser);
  await verifyFindings(browser);
  await verifyHostileText(browser);
  await verifyFailures(browser);
  await verifyKeyboard(browser);
  await verifyLayout(browser, artifacts);
  await verifyStubbedFindings(browser);
}
