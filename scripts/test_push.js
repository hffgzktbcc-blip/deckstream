#!/usr/bin/env node
// Push notifications, no network: subscribe/unsubscribe, the endpoint check that keeps the server from POSTing to
// private hosts, Web Push dispatch and expiry, and the ntfy relay's topic rule and game-free wording.
// web-push and fetch are stubbed; endpoints are IP literals so no DNS lookup happens. Run: node scripts/test_push.js
const assert = require('assert');
const path = require('path');
const Module = require('module');

const ROOT = path.resolve(__dirname, '..');

// Stub web-push before api/push.js loads it, whether or not the real package is installed.
const sent = [];
let sendError = null;
const fakeWebpush = {
  setVapidDetails: () => {},
  sendNotification: async (sub, payload) => {
    if (sendError) throw sendError;
    sent.push({ sub, payload: JSON.parse(payload) });
  },
};
const realResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === 'web-push') return 'web-push-stub';
  return realResolve.call(this, request, ...rest);
};
require.cache['web-push-stub'] = { id: 'web-push-stub', filename: 'web-push-stub', loaded: true, exports: fakeWebpush };

const fetched = [];
global.fetch = async (url, opts) => { fetched.push({ url, opts }); return { ok: true, status: 200 }; };

const push = require(path.join(ROOT, 'api', 'push.js'));

async function call({ method = 'GET', query = {}, body } = {}) {
  const out = { code: 200, body: undefined };
  const res = {
    setHeader: () => {}, getHeader: () => undefined,
    status: (c) => { out.code = c; return res; },
    json: (d) => { out.body = d; return res; },
    end: () => res,
  };
  await push({ method, query, headers: { 'x-deckstream-secret': 'test-secret' }, body }, res);
  return out;
}

const PUBLIC_EP = 'https://93.184.216.34/push/abc';
const keys = { p256dh: 'p'.repeat(20), auth: 'a'.repeat(10) };
const TOPIC = 'Abcdefghijklmnopqrstuvwxyz012345';

(async () => {
  process.env.DECKSTREAM_SECRET = 'test-secret';
  process.env.VAPID_PUBLIC_KEY = 'pub';
  process.env.VAPID_PRIVATE_KEY = 'priv';
  const device = { device: 'deck-test' };

  // Endpoint rule: https on a public address only.
  for (const bad of ['http://93.184.216.34/x', 'https://127.0.0.1/x', 'https://169.254.169.254/latest',
    'https://10.0.0.5/x', 'https://[::1]/x', 'https://[::ffff:192.168.1.2]/x', 'https://u:p@93.184.216.34/x',
    'ftp://93.184.216.34/x', 'not a url']) {
    await assert.rejects(push.assertPushEndpoint(bad), Error, bad);
  }
  assert.strictEqual((await push.assertPushEndpoint(PUBLIC_EP)).hostname, '93.184.216.34');

  // Wrong secret and bad device ids are refused before anything is stored.
  let r = await call({ query: { device: 'Bad Device!' } });
  assert.strictEqual(r.code, 400, 'invalid device id');

  r = await call({ query: device });
  assert.deepStrictEqual([r.code, r.body.subscribed, r.body.vapidPublicKey], [200, false, 'pub']);

  // Subscribe: missing and private endpoints are refused and nothing is saved.
  r = await call({ method: 'POST', query: device, body: { action: 'subscribe' } });
  assert.strictEqual(r.code, 400, 'missing endpoint');
  r = await call({ method: 'POST', query: device, body: { subscription: { endpoint: 'https://127.0.0.1:6379/', keys } } });
  assert.strictEqual(r.code, 400, 'private endpoint refused');
  assert.match(r.body.error, /public address/);
  r = await call({ query: device });
  assert.strictEqual(r.body.subscribed, false, 'refused endpoint not stored');

  r = await call({ method: 'POST', query: device, body: { subscription: { endpoint: PUBLIC_EP, keys } } });
  assert.deepStrictEqual([r.code, r.body.subscribed], [200, true]);
  r = await call({ query: device });
  assert.strictEqual(r.body.subscribed, true);

  // Notify: Web Push gets a capped payload and only a same-site click URL.
  r = await call({ method: 'POST', query: device,
    body: { action: 'notify', title: 'T'.repeat(500), body: 'Ready', url: 'https://evil.example/' } });
  assert.deepStrictEqual([r.code, r.body.webpush, r.body.ntfy], [200, 'sent', 'skipped']);
  assert.strictEqual(sent.length, 1);
  assert.strictEqual(sent[0].sub.endpoint, PUBLIC_EP);
  assert.strictEqual(sent[0].payload.title.length, 100, 'title capped');
  assert.strictEqual(sent[0].payload.data.url, '/', 'off-site click URL replaced');
  r = await call({ method: 'POST', query: device, body: { action: 'notify', url: '//evil.example/' } });
  assert.strictEqual(sent[1].payload.data.url, '/', 'protocol-relative URL replaced');
  r = await call({ method: 'POST', query: device, body: { action: 'notify', url: '/library' } });
  assert.strictEqual(sent[2].payload.data.url, '/library');

  // A send failure is reported, not thrown; 410 Gone drops the subscription.
  sendError = Object.assign(new Error('boom'), { statusCode: 500 });
  r = await call({ method: 'POST', query: device, body: { action: 'notify' } });
  assert.strictEqual(r.body.webpush, 'failed');
  sendError = Object.assign(new Error('gone'), { statusCode: 410 });
  r = await call({ method: 'POST', query: device, body: { action: 'notify' } });
  assert.strictEqual(r.body.webpush, 'expired');
  r = await call({ query: device });
  assert.strictEqual(r.body.subscribed, false, 'expired subscription removed');
  sendError = null;

  // A stored endpoint that is no longer public (older data, or DNS that moved) is not sent to.
  const store = require(path.join(ROOT, 'api', '_store.js'));
  await store.saveDoc('push', 'deck-test', { endpoint: 'https://127.0.0.1/x', keys }, 60);
  const before = sent.length;
  r = await call({ method: 'POST', query: device, body: { action: 'notify' } });
  assert.strictEqual(r.body.webpush, 'blocked');
  assert.strictEqual(sent.length, before, 'nothing sent to a private host');

  // No VAPID keys: Web Push is skipped quietly.
  delete process.env.VAPID_PRIVATE_KEY;
  r = await call({ method: 'POST', query: device, body: { action: 'notify' } });
  assert.strictEqual(r.body.webpush, 'none');
  process.env.VAPID_PRIVATE_KEY = 'priv';

  // ntfy: short (guessable) topics are refused; valid ones get generic wording that names no game.
  r = await call({ method: 'POST', query: device, body: { action: 'notify', topic: 'short' } });
  assert.deepStrictEqual([r.code, r.body.ntfy], [400, 'rejected_invalid_topic']);
  assert.strictEqual(fetched.length, 0, 'nothing posted for a refused topic');
  r = await call({ method: 'POST', query: device, body: { action: 'notify', title: 'Secret Game is ready', topic: TOPIC } });
  assert.strictEqual(r.body.ntfy, 'sent');
  assert.strictEqual(fetched.length, 1);
  assert.strictEqual(fetched[0].url, `https://ntfy.sh/${TOPIC}`);
  assert.ok(!JSON.stringify(fetched[0].opts).includes('Secret Game'), 'ntfy message names no game');

  // ntfy outage is reported as failed, not a 500.
  global.fetch = async () => { throw new Error('offline'); };
  r = await call({ method: 'POST', query: device, body: { action: 'notify', topic: TOPIC } });
  assert.deepStrictEqual([r.code, r.body.ntfy], [200, 'failed']);

  // Unsubscribe, unknown actions and methods.
  r = await call({ method: 'POST', query: device, body: { subscription: { endpoint: PUBLIC_EP, keys } } });
  r = await call({ method: 'POST', query: device, body: { action: 'unsubscribe' } });
  assert.strictEqual(r.body.subscribed, false);
  r = await call({ method: 'POST', query: device, body: { action: 'explode' } });
  assert.strictEqual(r.code, 400);
  r = await call({ method: 'DELETE', query: device });
  assert.strictEqual(r.code, 405);

  console.log('push: all checks passed');
})().catch((e) => { console.error(e); process.exit(1); });
