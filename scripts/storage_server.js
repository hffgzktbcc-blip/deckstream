#!/usr/bin/env node
// Reference storage server for DeckStream (the contract is in docs/STORAGE_API.md). Serves a folder of games you own:
// every folder directly inside GAMES_DIR is one game. No dependencies.
//
//   STORAGE_KEY=change-me node scripts/storage_server.js --dir /srv/games [--port 8780] [--public-url http://nas.lan:8780]
//
// The Deck streams the same folder over WebDAV, for example:  rclone serve webdav /srv/games --addr :8781 --user deckstream --pass "$STORAGE_KEY"
// --mock serves a made-up library with no files (used by scripts/dev_server.js and the tests).
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const GB = 1024 ** 3;
const MB = 1024 ** 2;
const MAX_FILES = 20000;

// A stable numeric id per game folder, so the app, the queue and the Deck agree on it across restarts.
function gameId(name) {
  return parseInt(crypto.createHash('sha1').update(name).digest('hex').slice(0, 8), 16) % 1e9 + 1;
}

function mockLibrary() {
  const f = (id, name, size) => ({ id, name, short_name: name.split('/').pop(), size });
  const base = { cached: true, download_finished: true, progress: 1, download_state: 'ready',
    created_at: '2026-10-01T10:00:00Z', updated_at: '2026-10-25T11:00:00Z' };
  return [
    { ...base, id: 101, name: 'Cyberpunk 2077', size: 69 * GB, files: [
      f(1, 'Cyberpunk 2077/bin/x64/Cyberpunk2077.exe', 85 * MB),
      f(2, 'Cyberpunk 2077/archive/pc/content/basegame_1.archive', 32 * GB),
      f(3, 'Cyberpunk 2077/archive/pc/content/basegame_2.archive', 30 * GB)] },
    { ...base, id: 102, name: 'Hades II', size: 11 * GB, files: [
      f(1, 'Hades II/Ship/Hades2.exe', 60 * MB),
      f(2, 'Hades II/Content/Packages.pkg', 10 * GB)] },
    { ...base, id: 103, name: 'Sifu', size: 24 * GB, files: [
      f(1, 'Sifu/Sifu.exe', 80 * MB),
      f(2, 'Sifu/Sifu/Content/Paks/pakchunk0-WindowsNoEditor.pak', 23 * GB)] },
    { ...base, id: 104, name: "Baldur's Gate 3", size: 122 * GB, files: [
      f(1, "Baldur's Gate 3/bin/bg3.exe", 90 * MB),
      f(2, "Baldur's Gate 3/bin/bg3_dx11.exe", 88 * MB),
      f(3, "Baldur's Gate 3/Data/Gustav.pak", 60 * GB)] },
    { ...base, id: 105, name: 'The Witcher 3 (GOG offline installer)', size: 50 * GB, files: [
      f(1, 'The Witcher 3 (GOG offline installer)/setup_the_witcher_3_wild_hunt.exe', 30 * MB),
      f(2, 'The Witcher 3 (GOG offline installer)/setup_the_witcher_3_wild_hunt-1.bin', 4 * GB)] },
    { ...base, id: 106, name: 'Control', size: 42 * GB, files: [
      f(1, 'Control/Control_DX12.exe', 65 * MB),
      f(2, 'Control/data_pack.bin', 40 * GB)] },
    { id: 107, name: 'Hollow Knight', size: 9 * GB, cached: false, download_finished: false, progress: 0.42,
      download_state: 'syncing', created_at: '2026-10-25T09:00:00Z', updated_at: '2026-10-26T06:00:00Z', files: [] },
  ];
}

function walk(root, rel, out) {
  if (out.length >= MAX_FILES) return;
  let entries = [];
  try { entries = fs.readdirSync(path.join(root, rel), { withFileTypes: true }); } catch { return; }
  for (const e of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (e.name.startsWith('.')) continue;
    const r = rel ? `${rel}/${e.name}` : e.name;
    if (e.isDirectory()) walk(root, r, out);
    else if (e.isFile()) {
      let size = 0;
      try { size = fs.statSync(path.join(root, r)).size; } catch {}
      out.push({ id: out.length + 1, name: r, short_name: e.name, size });
    }
    if (out.length >= MAX_FILES) return;
  }
}

function folderLibrary(dir) {
  let entries = [];
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return []; }
  return entries.filter((e) => e.isDirectory() && !e.name.startsWith('.')).map((e) => {
    const files = [];
    walk(dir, e.name, files);
    let mtime = 0;
    try { mtime = fs.statSync(path.join(dir, e.name)).mtimeMs; } catch {}
    return {
      id: gameId(e.name), name: e.name, size: files.reduce((n, f) => n + f.size, 0), files,
      cached: true, download_finished: true, progress: 1, download_state: 'ready',
      created_at: new Date(mtime).toISOString(), updated_at: new Date(mtime).toISOString(),
    };
  });
}

function create({ dir = '', key = '', publicUrl = '', mock = false } = {}) {
  if (!key) throw new Error('Set STORAGE_KEY (the key the app and the Deck use)');
  const library = () => (mock ? mockLibrary() : folderLibrary(dir));
  const sign = (p) => crypto.createHmac('sha256', key).update(p).digest('hex').slice(0, 32);
  const authed = (req) => {
    const given = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
    const a = crypto.createHash('sha256').update(given).digest();
    const b = crypto.createHash('sha256').update(key).digest();
    return crypto.timingSafeEqual(a, b);
  };

  return http.createServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    const send = (code, body) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };

    // Signed file links (games/requestdl hands these out, so the Deck can fetch with a plain GET).
    if (u.pathname.startsWith('/files/')) {
      if (mock) return send(404, { success: false, error: 'mock library has no files' });
      const rel = decodeURIComponent(u.pathname.slice('/files/'.length));
      if (!rel || rel.split('/').some((p) => p === '..' || p === '') || u.searchParams.get('sig') !== sign(rel)) {
        return send(403, { success: false, error: 'bad link' });
      }
      const file = path.join(dir, rel);
      let st;
      try { st = fs.statSync(file); } catch { return send(404, { success: false, error: 'not found' }); }
      const m = /^bytes=(\d*)-(\d*)$/.exec(String(req.headers.range || ''));
      let start = 0, end = st.size - 1;
      if (m && (m[1] || m[2])) {
        start = m[1] ? Number(m[1]) : Math.max(0, st.size - Number(m[2]));
        end = m[1] && m[2] ? Math.min(Number(m[2]), st.size - 1) : st.size - 1;
        if (start > end) { res.writeHead(416, { 'content-range': `bytes */${st.size}` }); return res.end(); }
        res.writeHead(206, { 'content-range': `bytes ${start}-${end}/${st.size}`, 'content-length': end - start + 1, 'accept-ranges': 'bytes' });
      } else {
        res.writeHead(200, { 'content-length': st.size, 'accept-ranges': 'bytes' });
      }
      return fs.createReadStream(file, { start, end }).pipe(res);
    }

    const p = u.pathname.replace(/^\/v1\/api\//, '').replace(/^\/+/, '');
    if (!authed(req)) return send(401, { success: false, error: 'UNAUTHORIZED' });

    if (p === 'user/me') return send(200, { success: true, data: { name: 'DeckStream storage', mock } });

    if (p === 'games/mylist') {
      const lib = library();
      const id = u.searchParams.get('id');
      if (!id) return send(200, { success: true, data: lib });
      const one = lib.find((g) => String(g.id) === id);
      return one ? send(200, { success: true, data: one }) : send(404, { success: false, error: 'NOT_FOUND' });
    }

    if (p === 'games/requestdl') {
      const g = library().find((x) => String(x.id) === u.searchParams.get('id'));
      if (!g) return send(404, { success: false, error: 'NOT_FOUND' });
      const fid = u.searchParams.get('file_id');
      const f = fid ? g.files.find((x) => String(x.id) === fid) : g.files.slice().sort((a, b) => b.size - a.size)[0];
      if (!f) return send(404, { success: false, error: 'NO_FILE' });
      const base = (publicUrl || `http://${req.headers.host}`).replace(/\/+$/, '');
      const enc = f.name.split('/').map(encodeURIComponent).join('/');
      return send(200, { success: true, data: `${base}/files/${enc}?sig=${sign(f.name)}` });
    }

    if (p === 'games/mount') {
      let body = '';
      req.on('data', (c) => { body += c; if (body.length > 4096) req.destroy(); });
      req.on('end', () => {
        let name = '';
        try { name = String(JSON.parse(body || '{}').path || '').trim().replace(/^\/+|\/+$/g, ''); } catch {}
        if (!name || name.includes('/') || name === '..') return send(400, { success: false, error: 'Give the name of a folder in the games directory' });
        const found = library().find((g) => g.name === name);
        if (!found) return send(404, { success: false, error: `No folder called "${name}" in the games directory` });
        return send(200, { success: true, data: { id: found.id, name: found.name } });
      });
      return undefined;
    }

    return send(404, { success: false, error: 'endpoint not found: ' + p });
  });
}

module.exports = { create, mockLibrary, folderLibrary, gameId };

if (require.main === module) {
  const arg = (n, d = '') => { const i = process.argv.indexOf(n); return i > 0 ? process.argv[i + 1] : d; };
  const mock = process.argv.includes('--mock');
  const dir = path.resolve(arg('--dir', process.env.GAMES_DIR || '.'));
  const port = Number(arg('--port', process.env.PORT || 8780));
  const key = process.env.STORAGE_KEY || '';
  const srv = create({ dir, key, mock, publicUrl: arg('--public-url', process.env.PUBLIC_URL || '') });
  srv.listen(port, '0.0.0.0', () => {
    console.log(`DeckStream storage server on :${port} (${mock ? 'mock library' : dir})`);
    console.log('Set STORAGE_API_URL=http://<this-host>:' + port + '/v1/api on the site.');
  });
}
