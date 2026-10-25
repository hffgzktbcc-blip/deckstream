// Start-up profile backup for the Deck (which byte ranges of a release a game reads at start-up and later). The Deck
// uploads a profile after it grows and restores it before the first launch on a new card or reinstall, so a game
// learned once starts warm again. Private: behind the queue password, keyed by a hash of the release folder name, and
// it holds only file paths and byte offsets inside that release (no game data, no credentials).

const { applyCors, requireSecret } = require('./_lib');
const { loadDoc, saveDoc } = require('./_store');

const KEY_RE = /^[0-9a-f]{64}$/;          // sha256 of the release folder name, computed on the Deck
const TTL_SEC = 180 * 24 * 3600;
const MAX_BYTES = 900 * 1024;             // under the storage's per-request limit
const MAX_RANGES = 20000;

function safePath(s) {
  const v = String(s || '');
  if (!v || v.length > 400 || v.startsWith('/') || /[\0\\]/.test(v) || v.split('/').some(p => p === '..' || p === '.' || p === '')) return '';
  return v;
}

const isInt = (x, max) => Number.isSafeInteger(x) && x >= 0 && x <= max;

function cleanRanges(list) {
  if (!Array.isArray(list) || list.length > MAX_RANGES) return null;
  const out = [];
  for (const r of list) {
    if (!r || typeof r !== 'object') return null;
    const f = safePath(r.f);
    const t = typeof r.t === 'number' && Number.isFinite(r.t) && r.t >= 0 && r.t <= 1e7 ? r.t : 0;
    if (!f || !isInt(r.o, 2 ** 50) || !isInt(r.n, 2 ** 40) || r.n === 0) return null;
    out.push({ t, f, o: r.o, n: r.n });
  }
  return out;
}

// Only the known fields survive, each checked; anything malformed rejects the whole profile.
function cleanProfile(p) {
  if (!p || typeof p !== 'object' || Array.isArray(p)) return null;
  const folder = safePath(p.folder);
  const ranges = cleanRanges(p.ranges || []);
  const later = cleanRanges(p.later || []);
  const demand = p.demand !== undefined ? cleanRanges(p.demand) : [];
  if (!folder || !ranges || !later || !demand || ranges.length + later.length === 0) return null;
  const out = { version: 1, folder, ranges, later };
  if (demand.length) out.demand = demand;
  for (const k of ['runs', 'last_added_bytes', 'later_bytes', 'total_bytes', 'early_fill_bytes']) if (isInt(p[k], 2 ** 50)) out[k] = p[k];
  if (typeof p.stable === 'boolean') out.stable = p.stable;
  return out;
}

module.exports = async (req, res) => {
  res.setHeader('cache-control', 'no-store');
  if (applyCors(req, res)) return;
  if (!requireSecret(req, res)) return;
  try {
    if (req.method === 'GET') {
      const key = String((req.query || {}).key || '');
      if (!KEY_RE.test(key)) return res.status(400).json({ error: 'Bad profile key' });
      const doc = await loadDoc('profile', key);
      if (!doc) return res.status(404).json({ error: 'No profile saved for this release' });
      return res.status(200).json({ profile: doc.profile, savedAt: doc.savedAt });
    }
    if (req.method === 'POST') {
      let body = req.body;
      if (typeof body === 'string') { try { body = JSON.parse(body); } catch { body = null; } }
      const key = String((body || {}).key || '');
      if (!KEY_RE.test(key)) return res.status(400).json({ error: 'Bad profile key' });
      const profile = cleanProfile((body || {}).profile);
      if (!profile) return res.status(400).json({ error: 'Not a valid start-up profile' });
      if (Buffer.byteLength(JSON.stringify(profile)) > MAX_BYTES) return res.status(413).json({ error: 'Profile too large' });
      await saveDoc('profile', key, { profile, savedAt: Date.now() }, TTL_SEC);
      return res.status(200).json({ ok: true, ranges: profile.ranges.length, later: profile.later.length });
    }
    res.setHeader('allow', 'GET, POST');
    return res.status(405).json({ error: 'Method not allowed' });
  } catch (e) {
    return res.status(502).json({ error: 'Profile storage unavailable' });
  }
};

module.exports.cleanProfile = cleanProfile;
