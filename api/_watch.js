// Stream when ready, on the site (user, 19 Oct: queue before bed with the phone closed). The app's button adds a
// pre-installed release to TorBox and hands it here; the site checks TorBox when the Deck's daemon polls and when the
// app reads the queue, and once TorBox has it cached the planner reads its files (AGENTS.md §0): an unpacked folder is
// queued to the Deck to stream, anything else is left for the app to open the install options. Cached alone never
// streams. The TorBox key is kept only while a watch is open, sealed with a key derived from DECKDEBRID_SECRET
// (AES-256-GCM), so the queue store alone can't read it; it is never sent back to the app.
const crypto = require('crypto');
const { buildPlan } = require('./_plan');

const WATCH_TTL_MS = 24 * 3600 * 1000;      // same lifetime the phone used
const WATCH_HARD_TTL_MS = 2 * WATCH_TTL_MS; // past a day it gets one last TorBox read; TorBox down that long: dropped
const CHECK_EVERY_MS = 60 * 1000;           // one TorBox read per minute per device, however often it is polled
const MAX_WATCHES = 10;
const STREAM_KINDS_BLOCKED = ['archive', 'repack', 'installer', 'switch', 'disc-image', 'not-a-game', 'suspicious',
  'unsupported-hypervisor', 'unsupported-uncracked', 'needs-manual-step', 'linux-installer', 'unknown'];

function sealKey() {
  return crypto.createHash('sha256').update('deckdebrid-watch:' + (process.env.DECKDEBRID_SECRET || '')).digest();
}
function seal(token) {
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv('aes-256-gcm', sealKey(), iv);
  const enc = Buffer.concat([c.update(String(token), 'utf8'), c.final()]);
  return [iv, c.getAuthTag(), enc].map(b => b.toString('base64')).join('.');
}
function unseal(blob) {
  try {
    const [iv, tag, enc] = String(blob || '').split('.').map(s => Buffer.from(s, 'base64'));
    const d = crypto.createDecipheriv('aes-256-gcm', sealKey(), iv);
    d.setAuthTag(tag);
    return Buffer.concat([d.update(enc), d.final()]).toString('utf8');
  } catch { return ''; }
}

// Same rule as the app's isItemCached (index.html).
function isCached(t) {
  if (!t) return false;
  if (t.download_finished === true || t.download_present === true || t.cached === true || t.finished === true) return true;
  if (typeof t.progress === 'number' && (t.progress >= 1 || t.progress === 100)) return true;
  return /completed|cached|downloaded|uploading|seeding/i.test(String(t.download_state || ''));
}

// What the app sees: no key.
const publicWatch = ({ key, ...w }) => w;

function addWatch(device, { torrentId, title, name, size, artwork, notifyTopic }, token) {
  const id = String(torrentId || '').trim();
  if (!/^\d{1,12}$/.test(id)) return { error: 'A TorBox torrent id is needed' };
  if (!token) return { error: 'TorBox key required in the Authorization header' };
  if (token.length > 512 || /[\s\0]/.test(token)) return { error: 'That TorBox key looks wrong' };
  const art = artwork && typeof artwork === 'object' ? artwork : null;
  device.watches = (device.watches || []).filter(w => w.torrentId !== id);
  if (device.watches.length >= MAX_WATCHES) return { error: 'Too many games waiting for TorBox' };
  const w = {
    torrentId: id,
    title: String(title || 'Game').trim().slice(0, 200),
    name: String(name || title || 'Game').trim().slice(0, 120),
    size: Number.isFinite(Number(size)) ? Math.max(0, Number(size)) : 0,
    artwork: art ? { hero: art.hero, grid: art.grid, logo: art.logo, icon: art.icon, header: art.header } : null,
    notifyTopic: /^[A-Za-z0-9_-]{1,64}$/.test(String(notifyTopic || '')) ? String(notifyTopic) : '',
    state: 'waiting',
    at: Date.now(),
    key: seal(token),
  };
  device.watches.push(w);
  device.watchCheckedAt = 0;   // check this one now
  return { watch: w };
}

function removeWatch(device, torrentId) {
  const before = (device.watches || []).length;
  device.watches = (device.watches || []).filter(w => w.torrentId !== String(torrentId));
  return device.watches.length !== before;
}

const AUTH_ERROR = /BAD_TOKEN|NO_AUTH|AUTH_ERROR|INVALID_TOKEN|TOKEN_EXPIRED|UNAUTHORI[SZ]ED/i;

// The stream job a watch makes has an id fixed by the watch, so a copy written back by a request that raced this one
// (see once() in checkWatches) is the same job, not a second one the Deck would run again.
const watchJobId = (w) => `job_w${w.torrentId}_${Number(w.at) || 0}`;

async function readTorrent(token, id, ms) {
  const base = process.env.TORBOX_API_URL || 'https://api.torbox.app/v1/api';
  const r = await fetch(`${base}/torrents/mylist?id=${id}&bypass_cache=true`, {
    headers: { authorization: `Bearer ${token}`, 'user-agent': 'DeckDebrid/1.0' },
    signal: AbortSignal.timeout(ms),
  });
  if (r.status === 401 || r.status === 403) return { rejected: true };
  const j = await r.json().catch(() => null);
  // An expired or revoked key can come back as 400/200 with success:false and an auth error code.
  const why = j ? String(j.error || '') + ' ' + String(j.detail || '') : '';
  if (j && j.success === false && AUTH_ERROR.test(why)) return { rejected: true };
  if (!r.ok || !j) return { retry: true };
  const list = Array.isArray(j.data) ? j.data : j.data ? [j.data] : [];
  const item = list.find(t => String(t.id) === id) || null;
  if (j.success === false && !item) return /not.?found|NOT_FOUND|DATABASE/i.test(why) ? { gone: true } : { retry: true };
  return item ? { item } : { gone: true };
}

// Runs from api/queue.js with the device loaded (daemon poll, app queue read, the app's watch request).
// enqueueStream(device, fields) puts a stream job on the queue the same way the app's enqueue does (fields.jobId is
// the job's id; an existing job with it, or for the same folder, is returned instead); notify({title, body, tag, topic})
// tells the phone. once(key) is true for the first caller only, across requests (a SET NX lock): two requests that load
// the same stored copy at once both may act on a watch, and only the first tells the phone. Returns true when the
// device changed.
async function checkWatches(device, { enqueueStream, notify, once = async () => true, now = Date.now(), force = false, budgetMs = 5000 } = {}) {
  const list = device.watches || [];
  if (!list.length) return false;
  if (!force && now - (device.watchCheckedAt || 0) < CHECK_EVERY_MS) return false;
  device.watchCheckedAt = now;
  // The daemon's poll gives up after 10 s, so all TorBox reads together get 5 s; the rest wait for the next check.
  const deadline = Date.now() + budgetMs;
  const online = now - (Number(device.status && device.status.lastSeen) || 0) < 45000;
  const tell = async (w, what, title, body) => {
    try {
      if (!(await once(`${w.torrentId}:${Number(w.at) || 0}:${what}`))) return;
      await notify({ title, body, tag: `watch-${w.torrentId}`, topic: w.notifyTopic });
    } catch {}
  };
  const rejected = new Map();   // TorBox key -> watches: one TorBox read and one message per bad key, however many games
  const keep = [];
  for (const w of list) {
    if (!w) continue;
    const age = now - (Number(w.at) || 0);
    if (w.state !== 'waiting') { if (age < WATCH_TTL_MS) keep.push(w); continue; }   // 'install': waiting for the app
    const expired = !(age < WATCH_TTL_MS);
    const giveUp = () => tell(w, 'expired', 'Still not in TorBox', `${w.title} didn't finish in TorBox within a day. Try another release.`);
    if (!(age < WATCH_HARD_TTL_MS)) { await giveUp(); continue; }
    const token = unseal(w.key);
    if (!token) { await tell(w, 'sealed', 'Stream when ready stopped', `Open DeckDebrid and press Stream when ready on ${w.title} again.`); continue; }
    if (rejected.has(token)) { rejected.get(token).push(w); continue; }
    const left = deadline - Date.now();
    if (left < 500) { keep.push(w); continue; }
    let got;
    try { got = await readTorrent(token, w.torrentId, left); } catch { got = { retry: true }; }
    if (got.retry) { keep.push(w); continue; }   // TorBox down: past a day it is still kept, up to two
    if (got.rejected) { rejected.set(token, [w]); continue; }
    if (got.gone) { await tell(w, 'gone', 'Removed from TorBox', `${w.title} is no longer in your TorBox library.`); continue; }
    // Past a day, the last read decides: cached goes on as usual (the Deck or the app may have been off all that time).
    if (!isCached(got.item)) { if (expired) await giveUp(); else keep.push(w); continue; }
    let plan;
    try { plan = buildPlan(got.item); } catch { keep.push(w); continue; }   // odd TorBox data: the others still go
    if (plan && plan.recommendation === 'stream' && !STREAM_KINDS_BLOCKED.includes(plan.kind)) {
      const job = enqueueStream(device, { ...w, plan, webdavPath: plan.webdavFolder || got.item.name || w.title, jobId: watchJobId(w) });
      if (job) {
        await tell(w, 'sent', 'Sent to the Deck', online
          ? `${w.title} is in TorBox and streams. It is getting ready on the Deck now.`
          : `${w.title} is in TorBox and streams. It gets ready to play when the Deck is on.`);
        continue;
      }
      keep.push(w);   // queue full: try again next check
      await tell(w, 'full', 'Deck queue is full', `${w.title} is in TorBox and goes to the Deck once its queue has room.`);
      continue;
    }
    // The files say install (zip, repack, installer...): the app opens the install options next time it is open.
    keep.push({ ...publicWatch(w), state: 'install', kind: plan ? plan.kind : 'unknown' });
    await tell(w, 'install', 'In TorBox: needs installing', `${w.title} is in TorBox, but its files need installing. Open DeckDebrid to choose how.`);
  }
  for (const ws of rejected.values()) {
    const names = ws.length > 1 ? `${ws[0].title} and ${ws.length - 1} more` : ws[0].title;
    await tell(ws[0], 'rejected', 'TorBox rejected the key', `Stream when ready stopped for ${names}. Check your TorBox key in Settings.`);
  }
  device.watches = keep;
  return true;
}

module.exports = { addWatch, removeWatch, checkWatches, publicWatch, isCached, seal, unseal, watchJobId, WATCH_TTL_MS, WATCH_HARD_TTL_MS, CHECK_EVERY_MS };
