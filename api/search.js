// Search: games in YOUR storage library (by name, through STORAGE_API_URL) plus the official Steam store catalog
// (titles and app ids only, for the game page). Nothing else is searched: DeckStream has no indexers or scrapers.
//
//   GET /api/search?q=hades   ->  [ { source: 'library', id, title, size, cached, streamable, kind },
//                                   { source: 'steam', appId, title } ]
const { applyCors, requireSecret } = require('./_lib');
const { buildPlan } = require('./_plan');
const { storageBase } = require('./storage');

const STEAM_SEARCH_URL = 'https://store.steampowered.com/api/storesearch/';

const words = (s) => String(s || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '')
  .replace(/['’`]/g, '').replace(/[^a-z0-9]+/g, ' ').trim().split(' ').filter(Boolean);

function matches(name, q) {
  const have = words(name);
  const want = words(q);
  return want.length > 0 && want.every((w) => have.some((h) => h === w || h.startsWith(w)));
}

async function searchLibrary(q, token) {
  if (!token) return [];
  const r = await fetch(`${storageBase()}/games/mylist`, {
    headers: { authorization: `Bearer ${token}`, 'user-agent': 'DeckStream/1.0' },
    signal: AbortSignal.timeout(10000),
  });
  if (!r.ok) return [];
  const j = await r.json().catch(() => null);
  const list = j && Array.isArray(j.data) ? j.data : [];
  return list.filter((g) => g && matches(g.name, q)).slice(0, 20).map((g) => {
    const plan = Array.isArray(g.files) && g.files.length ? buildPlan(g) : null;
    return {
      source: 'library',
      id: g.id,
      title: String(g.name || ''),
      size: Number(g.size) || 0,
      cached: g.cached === true || g.download_finished === true,
      streamable: Boolean(plan && plan.recommendation === 'stream'),
      kind: plan ? plan.kind : 'unknown',
    };
  });
}

async function searchSteam(q) {
  const r = await fetch(`${STEAM_SEARCH_URL}?term=${encodeURIComponent(q)}&l=english&cc=US`, {
    headers: { 'user-agent': 'DeckStream/1.0' },
    signal: AbortSignal.timeout(6000),
  });
  if (!r.ok) return [];
  const data = await r.json().catch(() => null);
  return (data && Array.isArray(data.items) ? data.items : []).slice(0, 15)
    .filter((it) => it && Number.isInteger(it.id) && typeof it.name === 'string')
    .map((it) => ({ source: 'steam', appId: it.id, title: it.name }));
}

module.exports = async (req, res) => {
  if (applyCors(req, res, 'GET, OPTIONS')) return;
  if (!requireSecret(req, res)) return;
  res.setHeader('cache-control', 'no-store');

  const q = String(req.query.q || req.query.query || '').trim().slice(0, 100);
  if (!q) return res.status(200).json([]);

  let token = String(req.headers['authorization'] || '');
  if (token.startsWith('Bearer ')) token = token.slice(7).trim();

  const [lib, steam] = await Promise.all([
    searchLibrary(q, token).catch(() => []),
    searchSteam(q).catch(() => []),
  ]);
  return res.status(200).json([...lib, ...steam]);
};

module.exports.matches = matches;
