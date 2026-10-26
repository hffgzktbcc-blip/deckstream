#!/usr/bin/env node
// Local dev server for DeckStream: serves the app, the Deck files (/daemon/*, /decky/DeckStream.zip) and the real
// api/*.js handlers, with scripts/storage_server.js in --mock mode as the storage server.
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
const { create: createStorage, mockLibrary } = require('./storage_server');

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
  '.py': 'text/plain; charset=utf-8',
  '.zip': 'application/zip',
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
  const mock = createStorage({ mock: true, key: 'mock-storage-key' });

  return new Promise((resolve) => {
    mock.listen(0, HOST, () => {
      process.env.STORAGE_API_URL = `http://${HOST}:${mock.address().port}/v1/api`;
      const server = http.createServer(async (req, res) => {
        const u = new URL(req.url, `http://${HOST}`);
        try {
          const shipped = /^\/(daemon\/[A-Za-z0-9_.-]+|decky\/DeckStream\.zip)$/.exec(u.pathname);
          if (STATIC.has(u.pathname) || shipped) {
            const file = path.join(ROOT, shipped ? shipped[1] : STATIC.get(u.pathname));
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
            // Default dev secret for seamless local usage
            if (!req.headers['x-deckstream-secret']) {
              req.headers['x-deckstream-secret'] = process.env.DECKSTREAM_SECRET;
            }
            if (!req.headers['authorization']) {
              req.headers['authorization'] = 'Bearer mock-storage-key';
            }

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

      const port = opts.port ?? 8765;
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
