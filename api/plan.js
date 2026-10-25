// Launch plan for one remote storage game: what it is, whether it can stream,
// which exe to run, and what to do instead if it cannot.
const { applyCors, requireSecret } = require('./_lib');
const { buildPlan } = require('./_plan');

async function fetchRemoteStorage(endpoint, token, id) {
  const base = process.env.STORAGE_API_URL || process.env.TORBOX_API_URL || 'http://127.0.0.1:8765/mock-storage';
  const url = /^\d{1,12}$/.test(id)
    ? `${base}/${endpoint}?id=${id}&bypass_cache=true`
    : `${base}/${endpoint}?bypass_cache=true`;
  return fetch(url, {
    headers: { authorization: `Bearer ${token}`, 'user-agent': 'DeckStream/1.0' },
    signal: AbortSignal.timeout(25000),
  });
}

function findInList(data, id, name) {
  const list = Array.isArray(data) ? data : data ? [data] : [];
  if (/^\d{1,12}$/.test(id)) {
    return list.find((t) => String(t.id || '') === id) || (list.length === 1 && !Array.isArray(data) ? list[0] : null);
  }
  if (name) {
    return list.find((t) => String(t.name || '').toLowerCase() === name.toLowerCase()) || null;
  }
  return null;
}

module.exports = async (req, res) => {
  if (applyCors(req, res, 'GET, OPTIONS')) return;
  if (!requireSecret(req, res)) return;
  res.setHeader('cache-control', 'no-store');
  if (req.method !== 'GET') return res.status(405).json({ error: 'Method not allowed' });

  let token = String(req.headers['authorization'] || '');
  if (token.startsWith('Bearer ')) token = token.slice(7).trim();
  if (!token) return res.status(400).json({ error: 'Storage credentials required in the Authorization header' });

  const gameId = String(req.query.game_id || req.query.id || '').trim();
  const name = String(req.query.name || '').trim();

  if ((gameId && !/^\d{1,12}$/.test(gameId)) || (!gameId && !name)) {
    return res.status(400).json({ error: 'Pass game_id (number) or name' });
  }

  try {
    const r = await fetchRemoteStorage('games/mylist', token, gameId);
    if (r.status === 401 || r.status === 403) return res.status(401).json({ error: 'Storage backend rejected credentials' });
    const j = await r.json().catch(() => null);
    if (!r.ok || !j || j.success === false) {
      return res.status(502).json({ error: 'Could not fetch game list from remote storage' });
    }

    const item = findInList(j.data, gameId, name);
    if (!item) return res.status(404).json({ error: 'Item not found in your remote storage library' });

    return res.json({
      success: true,
      plan: buildPlan(item, { mbps: Number(req.query.mbps) || undefined })
    });
  } catch (err) {
    return res.status(502).json({ error: 'Could not reach storage backend' });
  }
};
