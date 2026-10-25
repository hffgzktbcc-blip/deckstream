// Relays SteamGridDB API requests for game artwork (grids, heroes, logos, icons)
const { applyCors, requireSecret } = require('./_lib');
const SGDB_API = 'https://www.steamgriddb.com/api/v2';
const DEFAULT_KEY = process.env.STEAMGRIDDB_API_KEY || '';

module.exports = async (req, res) => {
  if (applyCors(req, res, 'GET, OPTIONS')) return;
  if (!requireSecret(req, res)) return;

  const apiKey = req.headers['authorization'] || req.query.key || DEFAULT_KEY;
  const path = String(req.query.p || req.query.path || '').replace(/^\/+/, '');

  if (path && (!/^[A-Za-z0-9_%\/. ,-]{1,200}$/.test(path) || path.split('/').some(p => p === '..'))) {
    return res.status(400).json({ error: 'Invalid SteamGridDB path' });
  }

  if (!path) {
    return res.status(400).json({ error: 'Missing SteamGridDB path parameter' });
  }

  if (!apiKey) {
    return res.status(401).json({ error: 'SteamGridDB API key required. Obtain a free key at steamgriddb.com/profile/preferences/api' });
  }

  const targetUrl = `${SGDB_API}/${path}`;

  try {
    const headers = {
      'authorization': apiKey.startsWith('Bearer ') ? apiKey : `Bearer ${apiKey}`,
      'user-agent': 'DeckStream/1.0'
    };

    const r = await fetch(targetUrl, { headers });
    res.status(r.status);
    const ct = r.headers.get('content-type');
    if (ct) res.setHeader('content-type', ct);
    res.setHeader('cache-control', 'public, max-age=86400'); // cache artwork metadata for 24h

    const text = await r.text();
    res.send(text);
  } catch (err) {
    res.status(502).json({ error: 'Could not reach SteamGridDB: ' + (err.message || 'connection failed') });
  }
};
