#!/usr/bin/env node
// Site API and reference storage server, no browser: the storage relay's allowlist and its 503 when unset, download
// host rules, search's word matching, and scripts/storage_server.js on a real folder (auth, signed links, Range,
// path traversal, mount). Run: node scripts/test_api.js
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const storage = require('../scripts/storage_server');

const ROOT = path.resolve(__dirname, '..');
const api = (name) => require(path.join(ROOT, 'api', name));

// A Vercel-style (req, res) pair, enough for the handlers.
async function call(handler, { method = 'GET', query = {}, headers = {}, body } = {}) {
  const out = { code: 200, headers: {}, body: undefined };
  const res = {
    setHeader: (k, v) => { out.headers[k.toLowerCase()] = v; },
    getHeader: (k) => out.headers[k.toLowerCase()],
    status: (c) => { out.code = c; return res; },
    json: (d) => { out.body = d; return res; },
    send: (d) => { try { out.body = JSON.parse(d); } catch { out.body = d; } return res; },
    end: () => res,
  };
  await handler({ method, query, headers: { 'x-deckstream-secret': 'test-secret', ...headers }, body }, res);
  return out;
}

const listen = (srv) => new Promise((r) => srv.listen(0, '127.0.0.1', () => r(srv.address().port)));

(async () => {
  process.env.DECKSTREAM_SECRET = 'test-secret';

  // Relay: refuses until STORAGE_API_URL is set, then only the four contract endpoints.
  delete process.env.STORAGE_API_URL;
  const relay = api('storage.js');
  let r = await call(relay, { query: { p: 'games/mylist' } });
  assert.strictEqual(r.code, 503, 'no storage server configured');
  r = await call(relay, { query: { p: 'games/mylist' }, headers: { 'x-deckstream-secret': 'wrong' } });
  assert.strictEqual(r.code, 401, 'wrong queue password');

  // A real folder of "games" behind the reference server.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ds-games-'));
  fs.mkdirSync(path.join(dir, 'Tiny Game', 'bin'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'Tiny Game', 'bin', 'tiny.exe'), Buffer.alloc(4096, 1));
  fs.writeFileSync(path.join(dir, 'Tiny Game', 'data.pak'), Buffer.from('0123456789'.repeat(1000)));
  fs.writeFileSync(path.join(dir, 'secret.txt'), 'not a game');
  const srv = storage.create({ dir, key: 'k3y' });
  const port = await listen(srv);
  process.env.STORAGE_API_URL = `http://127.0.0.1:${port}/v1/api`;
  const auth = { authorization: 'Bearer k3y' };

  r = await call(relay, { query: { p: 'games/mylist' }, headers: auth });
  assert.strictEqual(r.code, 200);
  assert.strictEqual(r.body.data.length, 1, 'only folders are games');
  const game = r.body.data[0];
  assert.strictEqual(game.name, 'Tiny Game');
  assert.strictEqual(game.id, storage.gameId('Tiny Game'));
  assert.strictEqual(game.size, 4096 + 10000);
  assert.deepStrictEqual(game.files.map((f) => f.name).sort(), ['Tiny Game/bin/tiny.exe', 'Tiny Game/data.pak']);

  r = await call(relay, { query: { p: 'games/mylist' }, headers: { authorization: 'Bearer nope' } });
  assert.strictEqual(r.code, 401, 'storage key checked by the storage server');
  for (const bad of ['games/delete', '../etc/passwd', 'games//mylist', 'user/me/../../x']) {
    r = await call(relay, { query: { p: bad }, headers: auth });
    assert.ok(r.code === 400 || r.code === 403, `${bad} -> ${r.code}`);
  }

  // Signed links: Range works, a bad signature or a path outside a game is refused.
  const data = game.files.find((f) => f.name.endsWith('data.pak'));
  r = await call(relay, { query: { p: 'games/requestdl', id: String(game.id), file_id: String(data.id) }, headers: auth });
  assert.strictEqual(r.code, 200);
  const link = r.body.data;
  let f = await fetch(link, { headers: { range: 'bytes=10-19' } });
  assert.strictEqual(f.status, 206);
  assert.strictEqual(await f.text(), '0123456789');
  f = await fetch(link.replace(/sig=[0-9a-f]+/, 'sig=deadbeef'));
  assert.strictEqual(f.status, 403, 'bad signature');
  f = await fetch(`http://127.0.0.1:${port}/files/..%2Fsecret.txt?sig=x`);
  assert.strictEqual(f.status, 403, 'traversal');

  // Download hosts: the storage server's own host (http allowed: a NAS), extra hosts over https only.
  const lib = api('_lib.js');
  assert.ok(lib.isAllowedDownloadUrl(link));
  assert.ok(!lib.isAllowedDownloadUrl('http://evil.example/x'));
  process.env.ALLOWED_DOWNLOAD_HOSTS = 'cdn.example.com';
  assert.ok(lib.isAllowedDownloadUrl('https://eu.cdn.example.com/f'));
  assert.ok(!lib.isAllowedDownloadUrl('http://cdn.example.com/f'), 'extra hosts need https');
  assert.ok(!lib.isAllowedDownloadUrl('https://cdn.example.com.evil.io/f'));

  // Mount: a folder name in the games directory, nothing else.
  r = await call(relay, { method: 'POST', query: { p: 'games/mount' }, headers: auth, body: { path: 'Tiny Game' } });
  assert.strictEqual(r.code, 200);
  assert.strictEqual(r.body.data.id, game.id);
  r = await call(relay, { method: 'POST', query: { p: 'games/mount' }, headers: auth, body: { path: '../etc' } });
  assert.strictEqual(r.code, 400);
  r = await call(relay, { method: 'POST', query: { p: 'games/mount' }, headers: auth, body: { path: 'Missing' } });
  assert.strictEqual(r.code, 404);

  // Search: your library by words (plus Steam titles when the network allows).
  const search = api('search.js');
  assert.ok(search.matches("Baldur's Gate 3", 'baldurs gate'));
  assert.ok(search.matches('Hades II', 'hades'));
  assert.ok(!search.matches('Hades II', 'hollow'));
  assert.ok(!search.matches('Anything', ''));
  r = await call(search, { query: { q: 'tiny' }, headers: auth });
  assert.strictEqual(r.code, 200);
  const hit = r.body.find((x) => x.source === 'library');
  assert.ok(hit && hit.id === game.id && hit.title === 'Tiny Game', JSON.stringify(r.body));
  r = await call(search, { query: { q: 'tiny' } });
  assert.ok(!r.body.some((x) => x.source === 'library'), 'no storage key, no library results');

  // Plan: the planner reads the game's files through the storage server.
  r = await call(api('plan.js'), { query: { game_id: String(game.id) }, headers: auth });
  assert.strictEqual(r.code, 200, JSON.stringify(r.body));
  assert.strictEqual(r.body.plan.recommendation, 'stream', JSON.stringify(r.body.plan));

  srv.close();
  fs.rmSync(dir, { recursive: true, force: true });
  console.log('[ok] api: storage relay allowlist/503/401, storage server (signed links, Range, traversal, mount), download hosts, search, plan');
})().catch((e) => { console.error(e); process.exit(1); });
