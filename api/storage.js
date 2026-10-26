// Same-origin relay to YOUR storage server (STORAGE_API_URL). The app never calls the storage server directly, so
// there is no CORS setup and the storage key never sits in a URL. The contract is in docs/STORAGE_API.md;
// scripts/storage_server.js is a reference server that serves a folder of games.
const { applyCors, requireSecret } = require('./_lib');

const ALLOWED_ENDPOINTS = new Set(['games/mylist', 'games/requestdl', 'games/mount', 'user/me']);

// Base URL of the storage API, without a trailing slash; '' when it is not configured.
function storageBase() {
  return String(process.env.STORAGE_API_URL || '').trim().replace(/\/+$/, '');
}

module.exports = async (req, res) => {
  if (applyCors(req, res, 'GET, POST, OPTIONS')) return;
  if (!requireSecret(req, res)) return;
  res.setHeader('cache-control', 'no-store');

  const base = storageBase();
  if (!base) return res.status(503).json({ success: false, error: 'Set STORAGE_API_URL on the server (see docs/STORAGE_API.md)' });

  let token = String(req.headers['authorization'] || '');
  if (token.startsWith('Bearer ')) token = token.slice(7).trim();

  const rawPath = String(req.query.p || req.query.path || '').replace(/^\/+/, '');
  if (!rawPath) return res.status(400).json({ success: false, error: 'Missing path parameter' });

  const [basePath, existingQuery] = rawPath.split('?');
  if (!/^[A-Za-z0-9_/-]{1,80}$/.test(basePath) || basePath.split('/').some((p) => p === '' || p === '..')) {
    return res.status(400).json({ success: false, error: 'Invalid path' });
  }
  if (!ALLOWED_ENDPOINTS.has(basePath)) {
    return res.status(403).json({ success: false, error: `Path not allowed: ${basePath}` });
  }

  const params = new URLSearchParams(existingQuery || '');
  for (const [k, v] of Object.entries(req.query || {})) {
    if (k !== 'p' && k !== 'path' && !params.has(k)) params.set(k, String(v));
  }
  const qs = params.toString();
  const targetUrl = `${base}/${basePath}${qs ? '?' + qs : ''}`;

  try {
    const headers = { 'user-agent': 'DeckStream/1.0' };
    if (token) headers.authorization = `Bearer ${token}`;
    const opts = { method: req.method === 'POST' ? 'POST' : 'GET', headers, signal: AbortSignal.timeout(25000) };
    if (opts.method === 'POST') {
      headers['content-type'] = 'application/json';
      opts.body = typeof req.body === 'string' ? req.body : JSON.stringify(req.body || {});
    }
    const resp = await fetch(targetUrl, opts);
    res.setHeader('content-type', resp.headers.get('content-type') || 'application/json');
    return res.status(resp.status).send(await resp.text());
  } catch (err) {
    return res.status(502).json({ success: false, error: 'Storage server unreachable', detail: err.message });
  }
};

module.exports.storageBase = storageBase;
module.exports.ALLOWED_ENDPOINTS = ALLOWED_ENDPOINTS;
