// Relays Remote Storage API requests (WebDAV / S3 / Local NAS)
// for DeckStream cloud game streaming companion.
const { applyCors, requireSecret } = require('./_lib');

const ALLOWED_ENDPOINTS = new Set([
  'games/mylist',
  'games/checkcached',
  'games/mount',
  'storage/mylist',
  'storage/checkcached',
  'storage/mount',
  'user/me'
]);

module.exports = async (req, res) => {
  if (applyCors(req, res, 'GET, POST, OPTIONS')) return;
  if (!requireSecret(req, res)) return;

  const STORAGE_API = process.env.STORAGE_API_URL || process.env.TORBOX_API_URL || 'http://127.0.0.1:8765/mock-storage';

  let token = req.headers['authorization'] || '';
  if (token.startsWith('Bearer ')) token = token.slice(7).trim();

  // Parse path and query
  let rawPath = String(req.query.p || req.query.path || req.url.replace(/^\/api\/storage(\?.*)?$/, '')).replace(/^\/+/, '');
  if (!rawPath) {
    return res.status(400).json({ error: 'Missing path parameter' });
  }

  // Handle case where path contains query string or duplicate ?
  const [basePath, existingQuery] = rawPath.split('?');
  if (!/^[A-Za-z0-9_\/-]{1,80}$/.test(basePath) || basePath.split('/').some(p => p === '' || p === '..')) {
    return res.status(400).json({ error: 'Invalid path' });
  }

  // Normalize aliases
  const normalizedPath = basePath.replace(/^storage\//, 'games/');
  if (!ALLOWED_ENDPOINTS.has(basePath) && !ALLOWED_ENDPOINTS.has(normalizedPath)) {
    return res.status(403).json({ error: `Path not allowed: ${basePath}` });
  }

  // Forward query parameters
  const targetParams = new URLSearchParams(existingQuery || '');
  for (const [k, v] of Object.entries(req.query)) {
    if (k !== 'p' && k !== 'path' && !targetParams.has(k)) {
      targetParams.set(k, String(v));
    }
  }

  const queryStr = targetParams.toString();
  const targetUrl = `${STORAGE_API}/${basePath}${queryStr ? '?' + queryStr : ''}`;

  try {
    const fetchHeaders = {};
    if (token) fetchHeaders['authorization'] = `Bearer ${token}`;

    const opts = {
      method: req.method,
      headers: fetchHeaders
    };

    if (req.method === 'POST') {
      fetchHeaders['content-type'] = 'application/json';
      opts.body = typeof req.body === 'string' ? req.body : JSON.stringify(req.body || {});
    }

    const resp = await fetch(targetUrl, opts);
    const contentType = resp.headers.get('content-type') || 'application/json';
    res.setHeader('content-type', contentType);
    const bodyText = await resp.text();
    return res.status(resp.status).send(bodyText);
  } catch (err) {
    return res.status(502).json({ error: 'Storage backend unreachable', detail: err.message });
  }
};
