#!/usr/bin/env node
// Local dev server for DeckStream. Serves the full cartridge-shelf PWA, Barlow fonts,
// and real api/*.js endpoints with mock storage integration.
//
//   node scripts/dev_server.js [--port 8765]
//   page:            http://127.0.0.1:8765/
//   queue password:  test-secret-123 (override with DECKSTREAM_SECRET)
//
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const HOST = '127.0.0.1';
const GB = 1024 ** 3;
const MB = 1024 ** 2;

function mockLibrary() {
  const f = (id, name, size) => ({ id, name, short_name: name.split('/').pop(), size });
  const base = {
    download_finished: true,
    download_present: true,
    cached: true,
    active: false,
    progress: 1,
    download_state: 'cached',
    seeds: 25,
    peers: 4,
    created_at: '2026-10-01T10:00:00Z',
    updated_at: '2026-10-25T11:00:00Z'
  };

  return [
    {
      ...base,
      id: 101,
      hash: 'cp77' + '0'.repeat(36),
      name: 'Cyberpunk.2077.Ultimate.Edition.v2.13-GOG',
      size: 69 * GB,
      files: [
        f(1, 'Cyberpunk 2077/bin/x64/Cyberpunk2077.exe', 85 * MB),
        f(2, 'Cyberpunk 2077/archive/pc/content/basegame_1.archive', 32 * GB),
        f(3, 'Cyberpunk 2077/archive/pc/content/basegame_2.archive', 30 * GB)
      ]
    },
    {
      ...base,
      id: 102,
      hash: 'hades2' + '0'.repeat(34),
      name: 'Hades.II.v1.0.104',
      size: 11 * GB,
      files: [
        f(1, 'Hades II/Ship/Hades2.exe', 60 * MB),
        f(2, 'Hades II/Content/Packages.pkg', 10 * GB)
      ]
    },
    {
      ...base,
      id: 103,
      hash: 'sifu' + '0'.repeat(36),
      name: 'Sifu',
      size: 24 * GB,
      files: [
        f(1, 'Sifu/Sifu.exe', 80 * MB),
        f(2, 'Sifu/Sifu/Content/Paks/pakchunk0-WindowsNoEditor.pak', 23 * GB)
      ]
    },
    {
      ...base,
      id: 104,
      hash: 'bg3' + '0'.repeat(37),
      name: 'Baldurs.Gate.3.v4.1.1',
      size: 122 * GB,
      files: [
        f(1, 'Baldurs Gate 3/bin/bg3.exe', 90 * MB),
        f(2, 'Baldurs Gate 3/bin/bg3_dx11.exe', 88 * MB),
        f(3, 'Baldurs Gate 3/Data/Gustav.pak', 60 * GB)
      ]
    },
    {
      ...base,
      id: 105,
      hash: 'elden' + '0'.repeat(35),
      name: 'ELDEN.RING.Deluxe.Edition',
      size: 58 * GB,
      files: [
        f(1, 'ELDEN RING/Game/eldenring.exe', 75 * MB),
        f(2, 'ELDEN RING/Game/Data0.bdt', 40 * GB)
      ]
    },
    {
      ...base,
      id: 106,
      hash: 'control' + '0'.repeat(33),
      name: 'Control.Ultimate.Edition',
      size: 42 * GB,
      files: [
        f(1, 'Control/Control_DX12.exe', 65 * MB),
        f(2, 'Control/data_pack.bin', 40 * GB)
      ]
    }
  ];
}

function startMockStorage() {
  const lib = mockLibrary();
  const srv = http.createServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    const send = (code, body) => {
      res.writeHead(code, { 'content-type': 'application/json' });
      res.end(JSON.stringify(body));
    };
    const p = u.pathname.replace(/^\/v1\/api\//, '');
    if (p === 'user/me') {
      return send(200, { success: true, data: { email: 'gamer@deckstream.local', plan: 2, is_subscribed: true } });
    }
    if (p === 'torrents/mylist' || p === 'storage/mylist') {
      const id = u.searchParams.get('id');
      return send(200, { success: true, data: id ? (lib.find((t) => String(t.id) === id) || null) : lib });
    }
    if (p === 'torrents/checkcached' || p === 'storage/checkcached') {
      return send(200, { success: true, data: {} });
    }
    return send(404, { success: false, detail: 'endpoint not found: ' + p });
  });
  return srv;
}

const STATIC = new Map([
  ['/', 'index.html'],
  ['/index.html', 'index.html'],
  ['/sw.js', 'sw.js'],
  ['/manifest.webmanifest', 'manifest.webmanifest'],
  ['/fonts/barlow-latin-400-normal.woff2', 'fonts/barlow-latin-400-normal.woff2'],
  ['/fonts/barlow-latin-500-normal.woff2', 'fonts/barlow-latin-500-normal.woff2'],
  ['/fonts/barlow-latin-600-normal.woff2', 'fonts/barlow-latin-600-normal.woff2'],
  ['/fonts/barlow-condensed-latin-600-normal.woff2', 'fonts/barlow-condensed-latin-600-normal.woff2'],
  ['/fonts/barlow-condensed-latin-700-normal.woff2', 'fonts/barlow-condensed-latin-700-normal.woff2']
]);

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.webmanifest': 'application/manifest+json',
  '.sh': 'text/plain; charset=utf-8',
  '.json': 'application/json',
  '.woff2': 'font/woff2'
};

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', () => resolve(Buffer.alloc(0)));
  });
}

function start(opts = {}) {
  process.env.DECKSTREAM_SECRET = process.env.DECKSTREAM_SECRET || 'test-secret-123';
  process.env.DECKDEBRID_SECRET = process.env.DECKSTREAM_SECRET; // backward compatibility
  const mock = startMockStorage();

  return new Promise((resolve) => {
    mock.listen(0, HOST, () => {
      process.env.TORBOX_API_URL = `http://${HOST}:${mock.address().port}/v1/api`;
      const server = http.createServer(async (req, res) => {
        const u = new URL(req.url, `http://${HOST}`);
        try {
          if (STATIC.has(u.pathname)) {
            let file = path.join(ROOT, STATIC.get(u.pathname));
            if (!fs.existsSync(file)) {
              file = path.join(ROOT, 'src', STATIC.get(u.pathname));
            }
            if (!fs.existsSync(file)) {
              res.writeHead(404);
              return res.end('not found');
            }
            res.writeHead(200, {
              'content-type': TYPES[path.extname(file)] || 'application/octet-stream',
              'cache-control': 'no-store',
              'content-security-policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'"
            });
            return res.end(fs.readFileSync(file));
          }

          const m = u.pathname.match(/^\/api\/([a-z0-9_-]+)$/);
          if (m && !m[1].startsWith('_') && fs.existsSync(path.join(ROOT, 'api', m[1] + '.js'))) {
            const handler = require(path.join(ROOT, 'api', m[1] + '.js'));
            req.query = Object.fromEntries(u.searchParams);
            const ctype = String(req.headers['content-type'] || '');
            if (/json/i.test(ctype)) {
              const raw = await readBody(req);
              try { req.body = raw.length ? JSON.parse(raw.toString('utf8')) : {}; } catch { req.body = {}; }
            } else if (req.method === 'GET' || req.method === 'HEAD' || req.method === 'OPTIONS') {
              req.body = {};
            }
            res.status = (c) => { res.statusCode = c; return res; };
            res.json = (d) => {
              if (!res.getHeader('content-type')) res.setHeader('content-type', 'application/json; charset=utf-8');
              res.end(JSON.stringify(d));
              return res;
            };
            res.send = (d) => {
              res.end(typeof d === 'string' || Buffer.isBuffer(d) ? d : JSON.stringify(d));
              return res;
            };
            return await handler(req, res);
          }

          res.writeHead(404, { 'content-type': 'text/plain' });
          res.end('The page could not be found');
        } catch (e) {
          if (!res.headersSent) res.writeHead(500, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ error: 'dev server: ' + (e && e.message) }));
        }
      });

      const port = opts.port || 8765;
      server.listen(port, HOST, () => {
        resolve({
          port: server.address().port,
          mockPort: mock.address().port,
          close: () => { server.close(); mock.close(); }
        });
      });
    });
  });
}

module.exports = { start, mockLibrary };

if (require.main === module) {
  const i = process.argv.indexOf('--port');
  const port = i > 0 ? Number(process.argv[i + 1]) : 8765;
  start({ port }).then((s) => {
    console.log(`🎮 DeckStream app dev server running at: http://${HOST}:${s.port}/`);
    console.log('Queue password: ' + (process.env.DECKSTREAM_SECRET === 'test-secret-123' ? 'test-secret-123' : '(from DECKSTREAM_SECRET)'));
  });
}
