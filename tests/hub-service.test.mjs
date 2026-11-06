import assert from 'node:assert/strict';
import test from 'node:test';
import {createHubService, HubServiceError, hubQuery, offsetMinutes} from '../web/services/hub.js';
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
  const hub = await service.getHub();
  assert.deepEqual(hub, sampleHub());
  assert.equal(calls.length, 2);
  assert.equal(calls[1].path, '/api/hub?timeZone=Europe%2FParis');
  assert.equal(calls[1].init.method, 'GET');
  assert.equal(calls[1].init.credentials, 'omit');
  assert.equal(calls[1].init.headers['X-Schola-Check-Token'], 'tok-1');
});

test('a 400 for the zone field is retried once with the offset', async () => {
  const {calls, transport} = fake({hub: path => path.includes('timeZone=') ? bad('timeZone') : json(sampleHub())});
  const service = createHubService({transport, zone: () => 'Nowhere/Land', now: () => ({getTimezoneOffset: () => -120})});
  const hub = await service.getHub();
  assert.equal(hub.schemaVersion, '1.0');
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
    await createHubService({transport, zone, now: () => ({getTimezoneOffset: () => 240})}).getHub();
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

test('server text is untrusted: the error message is fixed and the detail has no control or bidi characters', async () => {
  const {transport} = fake({hub: () => json({error: 'bad\u{202E}\u{7}text', code: 'internal_error', retryable: false}, 500)});
  const error = await createHubService({transport, zone: () => 'UTC', now: () => new Date(0)}).getHub().then(() => null, caught => caught);
  assert.doesNotMatch(error.message, /bad/);
  assert.ok(![...error.detail].some(char => char.codePointAt(0) < 32 || char === '\u{202e}'), 'no control or bidi character in the detail');
});

test('a code that is not a plain code is dropped from the message', async () => {
  const {transport} = fake({hub: () => json({error: 'x', code: 'x'.repeat(200) + '\u{202E}', retryable: false}, 500)});
  const error = await createHubService({transport, zone: () => 'UTC', now: () => new Date(0)}).getHub().then(() => null, caught => caught);
  assert.equal(error.code, 'request_failed');
  assert.doesNotMatch(error.message, /xxxx|\u{202e}/u);
});

test('a request can be cancelled', async () => {
  const controller = new AbortController();
  const {calls, transport} = fake({hub: () => json(sampleHub())});
  const service = createHubService({transport, zone: () => 'UTC', now: () => new Date(0)});
  await service.getHub({signal: controller.signal});
  assert.equal(calls.at(-1).init.signal, controller.signal);
});
