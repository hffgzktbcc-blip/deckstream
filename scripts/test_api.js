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

  // Public-address check for user-supplied URLs: literal IPv6 forms, non-http schemes, credentials and DNS rebinding are refused.
  for (const u of ['http://127.0.0.1/', 'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://[::ffff:7f00:1]/',
    'http://[::7f00:1]/', 'http://[64:ff9b::a9fe:a9fe]/', 'http://[fe90::1]/', 'http://[febf::1]/',
    'http://[fd00::1]/', 'http://[ff02::1]/', 'http://[2002:c0a8:101::1]/', 'http://169.254.169.254/']) {
    await assert.rejects(lib.assertPublicHttpUrl(u), /public address/, u);
  }
  await assert.rejects(lib.assertPublicHttpUrl('not a url'), /Invalid URL/);
  await assert.rejects(lib.assertPublicHttpUrl('ftp://1.1.1.1/'), /Only http\(s\) URLs/);
  await assert.rejects(lib.assertPublicHttpUrl('https://user:pass@1.1.1.1/'), /Credentials/);
  const dnsPromises = require('dns').promises;
  const origLookup = dnsPromises.lookup;
  try {
    dnsPromises.lookup = async () => [{ address: '127.0.0.1' }];
    await assert.rejects(lib.assertPublicHttpUrl('https://rebind.example/'), /public address/);
    dnsPromises.lookup = async () => [{ address: '93.184.216.34' }, { address: '10.0.0.5' }];
    await assert.rejects(lib.assertPublicHttpUrl('https://mixed.example/'), /public address/);
    dnsPromises.lookup = async () => [];
    await assert.rejects(lib.assertPublicHttpUrl('https://empty.example/'), /public address/);
    dnsPromises.lookup = async () => [{ address: '93.184.216.34' }];
    assert.strictEqual((await lib.assertPublicHttpUrl('https://public.example/path')).hostname, 'public.example');
  } finally {
    dnsPromises.lookup = origLookup;
  }
  for (const u of ['https://[2606:4700:4700::1111]/', 'https://1.1.1.1/', 'https://[::ffff:1.1.1.1]/']) {
    assert.strictEqual((await lib.assertPublicHttpUrl(u)).protocol, 'https:', u);
  }
  assert.ok(lib.isPrivateIp('fe80::1%eth0') && lib.isPrivateIp('not-an-ip') && !lib.isPrivateIp('8.8.8.8'));

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

  // Profile validator (api/profile.js cleanProfile): rejects path traversal, absolute paths, and malformed ranges.
  const { cleanProfile } = api('profile.js');
  assert.strictEqual(cleanProfile(null), null);
  assert.strictEqual(cleanProfile({ folder: '../evil', ranges: [{ f: 'a.pak', o: 0, n: 64 }] }), null);
  assert.strictEqual(cleanProfile({ folder: '/etc', ranges: [{ f: 'a.pak', o: 0, n: 64 }] }), null);
  assert.strictEqual(cleanProfile({ folder: 'Game\\Sub', ranges: [{ f: 'a.pak', o: 0, n: 64 }] }), null);
  assert.strictEqual(cleanProfile({ folder: 'Game', ranges: [{ f: '../etc/passwd', o: 0, n: 64 }] }), null);
  assert.strictEqual(cleanProfile({ folder: 'Game', ranges: [{ f: '/etc/passwd', o: 0, n: 64 }] }), null);
  assert.strictEqual(cleanProfile({ folder: 'Game', ranges: [{ f: 'a/./b.pak', o: 0, n: 64 }] }), null);
  assert.strictEqual(cleanProfile({ folder: 'Game', ranges: [] }), null, 'empty profile rejected');
  assert.strictEqual(cleanProfile({ folder: 'Game', ranges: [{ f: 'a.pak', o: 0, n: 0 }] }), null, 'zero length rejected');
  assert.strictEqual(cleanProfile({ folder: 'Game', ranges: [{ f: 'a.pak', o: -4, n: 64 }] }), null, 'negative offset rejected');
  assert.strictEqual(cleanProfile({ folder: 'Game', ranges: [{ f: 'a.pak', o: 0, n: 2 ** 41 }] }), null, 'oversized length rejected');
  const validProf = cleanProfile({
    folder: 'Tiny Game',
    ranges: [
      { t: 1.25, f: 'bin/tiny.exe', o: 0, n: 4096, extra: 'ignored' },
      { t: -5, f: 'data.pak', o: 1024, n: 2048 }, // overlapping / negative t clamped to 0
    ],
    later: [{ t: 45.5, f: 'data.pak', o: 0, n: 4096 }],
    demand: [{ f: 'data.pak', o: 4096, n: 1024 }],
    runs: 3,
    total_bytes: 8192,
    stable: true,
    unexpected: 'stripped',
  });
  assert.ok(validProf, 'valid profile accepted');
  assert.strictEqual(validProf.version, 1);
  assert.strictEqual(validProf.folder, 'Tiny Game');
  assert.strictEqual(validProf.ranges.length, 2);
  assert.deepStrictEqual(validProf.ranges[0], { t: 1.25, f: 'bin/tiny.exe', o: 0, n: 4096 });
  assert.strictEqual(validProf.ranges[1].t, 0, 'negative timestamp clamped to 0');
  assert.strictEqual(validProf.later.length, 1);
  assert.strictEqual(validProf.demand.length, 1);
  assert.strictEqual(validProf.runs, 3);
  assert.strictEqual(validProf.total_bytes, 8192);
  assert.strictEqual(validProf.stable, true);
  assert.strictEqual(validProf.unexpected, undefined);

  // ProtonDB title sanitizer (api/protondb.js cleanTitle): strips versions, edition words, and punctuation.
  const { cleanTitle } = api('protondb.js');
  assert.strictEqual(cleanTitle('Game v1.2.3 GOTY Edition'), 'Game');
  assert.strictEqual(cleanTitle('Game (Deluxe)'), 'Game');
  assert.strictEqual(cleanTitle('Game_Name-Remastered'), 'Game Name');
  assert.strictEqual(cleanTitle('Cyberpunk 2077 [v1.6.1] Ultimate Complete Edition'), 'Cyberpunk 2077');
  assert.strictEqual(cleanTitle('Death Stranding Directors Cut Enhanced'), 'Death Stranding');
  assert.strictEqual(cleanTitle(''), '');

  // Status relative timestamp helper (api/status.js timeAgo): boundaries from missing/future to days.
  const { timeAgo } = api('status.js');
  const now = Date.now();
  assert.strictEqual(timeAgo(0), 'never');
  assert.strictEqual(timeAgo(null), 'never');
  assert.strictEqual(timeAgo(NaN), 'never');
  assert.strictEqual(timeAgo(now + 30_000), 'just now', 'future timestamp clamps to 0s');
  assert.strictEqual(timeAgo(now - 5_000), 'just now', '< 10s');
  assert.strictEqual(timeAgo(now - 25_000), '25s ago', '< 60s');
  assert.strictEqual(timeAgo(now - 5 * 60_000), '5m ago', '< 3600s');
  assert.strictEqual(timeAgo(now - 3 * 3600_000), '3h ago', '< 86400s');
  assert.strictEqual(timeAgo(now - 2 * 86400_000), '2d ago', '>= 86400s');

  srv.close();
  fs.rmSync(dir, { recursive: true, force: true });
  console.log('[ok] api: storage relay allowlist/503/401, storage server (signed links, Range, traversal, mount), download hosts, search, plan, profile, protondb, status');
})().catch((e) => { console.error(e); process.exit(1); });
