import assert from 'node:assert/strict';
import test from 'node:test';
import {createHubService, HubServiceError, hubQuery, offsetMinutes, errorFacts} from '../web/services/hub.js';
import {sampleHub} from './hub-fixture.mjs';

const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers: {'content-type': 'application/json'}});
const bad = (field, code = 'invalid_request') => json({error: 'x', code, retryable: false, field}, 400);

/** A fake server: records every request and answers by path. */
function fake(handlers) {
  const calls = [];
  const transport = async (path, init = {}) => {
    calls.push({path, init});
    const base = path.split('?')[0];
    if (base === '/api/checks/session') return handlers.session ? handlers.session(calls) : json({token: 'tok-' + calls.filter(call => call.path === base).length});
    return handlers.hub(path, init, calls);
  };
  return {calls, transport};
}

test('the query carries the zone name, encoded', () => {
  assert.equal(hubQuery({timeZone: 'America/New_York'}), '?timeZone=America%2FNew_York');
  assert.equal(hubQuery({timeZone: 'Etc/GMT+5'}), '?timeZone=Etc%2FGMT%2B5');
});

test('the query carries a whole offset in plain digits, with a minus for west', () => {
  assert.equal(hubQuery({tzOffsetMinutes: 330}), '?tzOffsetMinutes=330');
  assert.equal(hubQuery({tzOffsetMinutes: -480}), '?tzOffsetMinutes=-480');
  assert.equal(hubQuery({tzOffsetMinutes: 0}), '?tzOffsetMinutes=0');
  assert.equal(hubQuery({tzOffsetMinutes: -0}), '?tzOffsetMinutes=0');
});

test('the query never sends both fields, and sends neither for an empty request', () => {
  assert.equal(hubQuery({timeZone: 'UTC', tzOffsetMinutes: 60}), '?timeZone=UTC');
  assert.equal(hubQuery({}), '');
  assert.equal(hubQuery({timeZone: ''}), '');
});

test('the offset is east of UTC: the sign of the browser value is flipped, and -0 becomes 0', () => {
  assert.equal(offsetMinutes({getTimezoneOffset: () => 300}), -300);
  assert.equal(offsetMinutes({getTimezoneOffset: () => -330}), 330);
  assert.equal(Object.is(offsetMinutes({getTimezoneOffset: () => 0}), 0), true);
  assert.equal(offsetMinutes({getTimezoneOffset: () => 0.4}), 0);
  assert.equal(offsetMinutes({getTimezoneOffset: () => 100000}), -840);
  assert.equal(offsetMinutes({getTimezoneOffset: () => -100000}), 840);
});

test('the first request sends the token and the zone name, and returns the validated answer', async () => {
  const {calls, transport} = fake({hub: () => json(sampleHub())});
  const service = createHubService({transport, zone: () => 'Europe/Paris', now: () => new Date(0)});
  const answer = await service.getHub();
  assert.deepEqual(answer, {hub: sampleHub(), offsetFallback: false});
  assert.equal(calls.length, 2);
  assert.equal(calls[1].path, '/api/hub?timeZone=Europe%2FParis');
  assert.equal(calls[1].init.method, 'GET');
  assert.equal(calls[1].init.credentials, 'omit');
  assert.equal(calls[1].init.headers['X-Schola-Check-Token'], 'tok-1');
});

test('a 400 for the zone field is retried once with the offset', async () => {
  const {calls, transport} = fake({hub: path => path.includes('timeZone=') ? bad('timeZone') : json(sampleHub())});
  const service = createHubService({transport, zone: () => 'Nowhere/Land', now: () => ({getTimezoneOffset: () => -120})});
  const answer = await service.getHub();
  assert.equal(answer.hub.schemaVersion, '1.0');
  assert.equal(answer.offsetFallback, true, 'the screen is told that the offset stood in for the zone');
  const paths = calls.map(call => call.path).filter(path => path.startsWith('/api/hub'));
  assert.deepEqual(paths, ['/api/hub?timeZone=Nowhere%2FLand', '/api/hub?tzOffsetMinutes=120']);
});

test('a 400 for another field is not retried and is an error', async () => {
  const {calls, transport} = fake({hub: () => bad('tzOffsetMinutes')});
  const service = createHubService({transport, zone: () => 'Europe/Paris', now: () => new Date(0)});
  await assert.rejects(service.getHub(), error => error instanceof HubServiceError && error.status === 400);
  assert.equal(calls.filter(call => call.path.startsWith('/api/hub')).length, 1);
});

test('the offset retry happens once: a second 400 is an error', async () => {
  const {calls, transport} = fake({hub: () => bad('timeZone')});
  const service = createHubService({transport, zone: () => 'Nowhere/Land', now: () => new Date(0)});
  await assert.rejects(service.getHub(), HubServiceError);
  assert.equal(calls.filter(call => call.path.startsWith('/api/hub')).length, 2);
});

test('with no zone name from the browser, the offset is sent at once', async () => {
  const {calls, transport} = fake({hub: () => json(sampleHub())});
  for (const zone of [() => undefined, () => '', () => { throw new Error('no Intl'); }]) {
    calls.length = 0;
    const answer = await createHubService({transport, zone, now: () => ({getTimezoneOffset: () => 240})}).getHub();
    assert.equal(answer.offsetFallback, true);
    assert.deepEqual(calls.map(call => call.path).filter(path => path.startsWith('/api/hub')), ['/api/hub?tzOffsetMinutes=-240']);
  }
});

test('a rejected token is fetched again and the same request is sent once more', async () => {
  let hubCalls = 0;
  const {calls, transport} = fake({hub: (path, init) => {
    hubCalls += 1;
    return init.headers['X-Schola-Check-Token'] === 'tok-2' ? json(sampleHub()) : json({error: 'no', code: 'invalid_token', retryable: false}, 403);
  }});
  const service = createHubService({transport, zone: () => 'UTC', now: () => new Date(0)});
  await service.getHub();
  assert.equal(hubCalls, 2);
  assert.equal(calls.filter(call => call.path === '/api/checks/session').length, 2);
  await assert.rejects(createHubService({transport: fake({hub: () => json({error: 'no', code: 'invalid_token', retryable: false}, 403)}).transport, zone: () => 'UTC', now: () => new Date(0)}).getHub(),
    error => error.code === 'invalid_token');
});

test('the token is kept between calls', async () => {
  const {calls, transport} = fake({hub: () => json(sampleHub())});
  const service = createHubService({transport, zone: () => 'UTC', now: () => new Date(0)});
  await service.getHub();
  await service.getHub();
  assert.equal(calls.filter(call => call.path === '/api/checks/session').length, 1);
});

test('an unexpected shape is an error with a fixed message, not a crash and not a partial answer', async () => {
  const {transport} = fake({hub: () => json({...sampleHub(), schemaVersion: '9.9'})});
  const service = createHubService({transport, zone: () => 'UTC', now: () => new Date(0)});
  await assert.rejects(service.getHub(), error => error instanceof HubServiceError && error.code === 'invalid_response' && /could not read/.test(error.message));
});

test('text that is not JSON, or JSON that is not an object, is an invalid response', async () => {
  for (const body of [new Response('<html>oops</html>', {status: 200}), json([1, 2]), json('text'), json(null)]) {
    const {transport} = fake({hub: () => body.clone()});
    await assert.rejects(createHubService({transport, zone: () => 'UTC', now: () => new Date(0)}).getHub(), error => error.code === 'invalid_response');
  }
});

test('a network failure, a busy log, a damaged lesson store and an unknown fault each get their own plain code', async () => {
  const run = async handler => {
    const {transport} = fake({hub: handler});
    return createHubService({transport, zone: () => 'UTC', now: () => new Date(0)}).getHub().then(() => null, error => error);
  };
  const network = await run(() => { throw new TypeError('Failed to fetch'); });
  assert.equal(network.code, 'network_error');
  assert.equal(network.status, 0);
  assert.match(network.message, /could not connect/);
  const busy = await run(() => json({error: 'busy', code: 'lock_unavailable', retryable: true}, 503));
  assert.equal(busy.code, 'busy');
  const corrupt = await run(() => json({error: 'bad file', code: 'corrupt_file', retryable: false}, 500));
  assert.equal(corrupt.code, 'lessons_damaged');
  const odd = await run(() => json({error: 'x', code: 'something_new', retryable: false}, 500));
  assert.equal(odd.code, 'request_failed');
  assert.match(odd.message, /something_new/);
  const nothing = await run(() => new Response('', {status: 502}));
  assert.equal(nothing.code, 'request_failed');
});

test('server text is untrusted: the error message is fixed and the body is not kept', async () => {
  const {transport} = fake({hub: () => json({error: 'bad\u{202E}\u{7}text', code: 'internal_error', retryable: false}, 500)});
  const error = await createHubService({transport, zone: () => 'UTC', now: () => new Date(0)}).getHub().then(() => null, caught => caught);
  assert.doesNotMatch(error.message, /bad/);
  assert.equal(error.detail, '');
});

test('a code that is not a plain code is dropped from the message', async () => {
  const {transport} = fake({hub: () => json({error: 'x', code: 'x'.repeat(200) + '\u{202E}', retryable: false}, 500)});
  const error = await createHubService({transport, zone: () => 'UTC', now: () => new Date(0)}).getHub().then(() => null, caught => caught);
  assert.equal(error.code, 'request_failed');
  assert.doesNotMatch(error.message, /xxxx|\u{202e}/u);
});

test('every request carries a signal, and the caller cancelling aborts it', async () => {
  const controller = new AbortController();
  let seen;
  const transport = async (path, init) => { if (path === '/api/checks/session') return json({token: 't'}); seen = init.signal; return new Promise(() => {}); };
  const pending = createHubService({transport, zone: () => 'UTC', now: () => new Date(0), timeoutMs: 5000}).getHub({signal: controller.signal}).catch(() => {});
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(seen.aborted, false);
  controller.abort();
  await pending;
  assert.equal(seen.aborted, true);
});

test('a signal that is already aborted gives an abort at once, with no request', async () => {
  const {calls, transport} = fake({hub: () => json(sampleHub())});
  const controller = new AbortController();
  controller.abort();
  const error = await createHubService({transport, zone: () => 'UTC', now: () => new Date(0)}).getHub({signal: controller.signal}).then(() => null, caught => caught);
  assert.equal(error.name, 'AbortError');
  assert.equal(calls.length, 0);
});

test('a server that accepts and never answers ends in a timeout error that says so', async () => {
  const transport = async path => path === '/api/checks/session' ? json({token: 't'}) : new Promise(() => {});
  const started = Date.now();
  const error = await createHubService({transport, zone: () => 'UTC', now: () => new Date(0), timeoutMs: 40}).getHub().then(() => null, caught => caught);
  assert.ok(error instanceof HubServiceError);
  assert.equal(error.code, 'timeout');
  assert.match(error.message, /did not answer/);
  assert.doesNotMatch(error.message, /could not connect/);
  assert.ok(Date.now() - started < 2000);
});

test('the timeout also covers a token request that never answers, and a body that never ends', async () => {
  const hangingToken = async () => new Promise(() => {});
  const one = await createHubService({transport: hangingToken, zone: () => 'UTC', now: () => new Date(0), timeoutMs: 30}).getHub().then(() => null, caught => caught);
  assert.equal(one.code, 'timeout');
  const slowBody = async path => path === '/api/checks/session' ? json({token: 't'}) : {status: 200, ok: true, json: () => new Promise(() => {})};
  const two = await createHubService({transport: slowBody, zone: () => 'UTC', now: () => new Date(0), timeoutMs: 30}).getHub().then(() => null, caught => caught);
  assert.equal(two.code, 'timeout');
});

test('the timeout aborts the request it started, and the default is 30 seconds', async () => {
  let seen;
  const transport = async (path, init) => { if (path === '/api/checks/session') return json({token: 't'}); seen = init.signal; return new Promise(() => {}); };
  await createHubService({transport, zone: () => 'UTC', now: () => new Date(0), timeoutMs: 30}).getHub().catch(() => {});
  assert.equal(seen.aborted, true);
  const {DEFAULT_TIMEOUT_MS} = await import('../web/services/hub.js');
  assert.equal(DEFAULT_TIMEOUT_MS, 30_000);
});

test('a fast answer clears the timer and a later call has a fresh clock', async () => {
  const {transport} = fake({hub: () => json(sampleHub())});
  const service = createHubService({transport, zone: () => 'UTC', now: () => new Date(0), timeoutMs: 25});
  await service.getHub();
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.equal((await service.getHub()).hub.schemaVersion, '1.0');
});

test('a caller who cancels gets an abort, which is not a timeout and not a network error', async () => {
  const controller = new AbortController();
  const transport = async path => path === '/api/checks/session' ? json({token: 't'}) : new Promise(() => {});
  const pending = createHubService({transport, zone: () => 'UTC', now: () => new Date(0), timeoutMs: 5000}).getHub({signal: controller.signal}).then(() => null, caught => caught);
  setTimeout(() => controller.abort(), 20);
  const error = await pending;
  assert.equal(error.name, 'AbortError');
});

test('an error keeps its HTTP status and its server code, and nothing else of the body', async () => {
  const {transport} = fake({hub: () => json({error: 'secret body text', code: 'internal_error', retryable: false}, 500)});
  const error = await createHubService({transport, zone: () => 'UTC', now: () => new Date(0)}).getHub().then(() => null, caught => caught);
  assert.equal(error.status, 500);
  assert.equal(error.serverCode, 'internal_error');
  assert.equal(error.detail, '');
  assert.doesNotMatch(JSON.stringify({...error, message: error.message}), /secret/);
});

test('error facts name a timeout and a failed connection apart, and show status and code only', () => {
  assert.equal(errorFacts({code: 'timeout', status: 0}), 'Timed out waiting for the server.');
  assert.equal(errorFacts({code: 'network_error', status: 0}), 'Could not connect to the server.');
  assert.equal(errorFacts({code: 'request_failed', status: 500, serverCode: 'internal_error'}), 'HTTP 500 \u00b7 code internal_error');
  assert.equal(errorFacts({code: 'busy', status: 503, serverCode: 'lock_unavailable'}), 'HTTP 503 \u00b7 code lock_unavailable');
  assert.equal(errorFacts({code: 'request_failed', status: 502, serverCode: ''}), 'HTTP 502');
  assert.equal(errorFacts({code: 'invalid_response', status: 200, serverCode: ''}), 'HTTP 200 \u00b7 answer not readable');
  assert.equal(errorFacts({}), '');
});

test('a finished load leaves no timer behind: the request signal stays unaborted after the time limit passes', async () => {
  let seen;
  const transport = async (path, init) => { if (path !== '/api/checks/session') seen = init.signal; return path === '/api/checks/session' ? json({token: 't'}) : json(sampleHub()); };
  await createHubService({transport, zone: () => 'UTC', now: () => new Date(0), timeoutMs: 30}).getHub();
  await new Promise(resolve => setTimeout(resolve, 80));
  assert.equal(seen.aborted, false);
});
