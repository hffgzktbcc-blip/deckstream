// Launch timings from the Deck: one short summary per streamed-game session (seconds and MB only), so every launch is
// measured without anyone watching. Behind the queue password; the newest 200 per device are kept for 180 days.

const { applyCors, requireSecret } = require('./_lib');
const { loadDoc, saveDoc } = require('./_store');

const DEVICE_ID_RE = /^[a-z0-9_-]{1,40}$/;
const SLUG_RE = /^[a-z0-9_.-]{1,120}$/;
const MAX_KEEP = 200;
const TTL_SEC = 180 * 24 * 3600;
const NUM_FIELDS = ['play', 'fill_seconds', 'play_to_launch_seconds', 'session_seconds', 'fill_mb',
  'downloaded_while_playing_mb', 'fill_rc', 'exit_seconds'];
const EXIT_REASONS = ['dxvk', 'vkd3d', 'oom', 'access_violation', 'd3d11', 'prefix_timeout', 'anticheat', 'steam_api',
  'unknown_early'];       // an enum, never free text from the Deck
const MODES = ['gaming', 'desktop', 'unknown'];

function cleanLaunch(l) {
  if (!l || typeof l !== 'object' || Array.isArray(l)) return null;
  const slug = String(l.slug || '');
  if (!SLUG_RE.test(slug)) return null;
  const out = { slug, mode: MODES.includes(l.mode) ? l.mode : 'unknown' };
  for (const k of NUM_FIELDS) {
    const v = l[k];
    if (v === undefined || v === null) continue;
    if (!Number.isSafeInteger(v) || v < -1000 || v > 4e9) return null;
    out[k] = v;
  }
  if (EXIT_REASONS.includes(l.exit_reason)) out.exit_reason = l.exit_reason;
  if (!out.play) return null;
  return out;
}

module.exports = async (req, res) => {
  res.setHeader('cache-control', 'no-store');
  if (applyCors(req, res)) return;
  if (!requireSecret(req, res)) return;
  try {
    if (req.method === 'GET') {
      const device = String((req.query || {}).device || 'deck-main');
      if (!DEVICE_ID_RE.test(device)) return res.status(400).json({ error: 'Bad device id' });
      const doc = await loadDoc('launches', device);
      return res.status(200).json({ launches: (doc && doc.launches) || [] });
    }
    if (req.method === 'POST') {
      let body = req.body;
      if (typeof body === 'string') { try { body = JSON.parse(body); } catch { body = null; } }
      const device = String((body || {}).device || '');
      if (!DEVICE_ID_RE.test(device)) return res.status(400).json({ error: 'Bad device id' });
      const launch = cleanLaunch((body || {}).launch);
      if (!launch) return res.status(400).json({ error: 'Not a valid launch summary' });
      const doc = (await loadDoc('launches', device)) || { launches: [] };
      const list = (doc.launches || []).filter(x => !(x.slug === launch.slug && x.play === launch.play));  // a resend replaces
      list.unshift(launch);
      await saveDoc('launches', device, { launches: list.slice(0, MAX_KEEP) }, TTL_SEC);
      return res.status(200).json({ ok: true, kept: Math.min(list.length, MAX_KEEP) });
    }
    res.setHeader('allow', 'GET, POST');
    return res.status(405).json({ error: 'Method not allowed' });
  } catch (e) {
    return res.status(502).json({ error: 'Launch storage unavailable' });
  }
};

module.exports.cleanLaunch = cleanLaunch;
