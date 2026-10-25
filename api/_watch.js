// Stream when ready: monitors remote transfers until they are ready to stream.
const crypto = require('crypto');
const { buildPlan } = require('./_plan');

const WATCH_TTL_MS = 24 * 3600 * 1000;
const WATCH_HARD_TTL_MS = 2 * WATCH_TTL_MS;
const CHECK_EVERY_MS = 60 * 1000;
const MAX_WATCHES = 10;
const STREAM_KINDS_BLOCKED = ['archive', 'installer', 'disc-image', 'not-a-game', 'suspicious', 'unknown'];

function sealKey() {
  return crypto.createHash('sha256').update('deckstream-watch:' + (process.env.DECKSTREAM_SECRET || '')).digest();
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

function isCached(t) {
  if (!t) return false;
  if (t.download_finished === true || t.download_present === true || t.cached === true || t.finished === true) return true;
  if (typeof t.progress === 'number' && (t.progress >= 1 || t.progress === 100)) return true;
  return /completed|cached|downloaded|ready/i.test(String(t.download_state || ''));
}

const publicWatch = ({ key, ...w }) => w;

function addWatch(device, { storageId, remoteId, torrentId, title, name, size, artwork, notifyTopic }, token) {
  const id = String(storageId || remoteId || torrentId || '').trim();
  if (!/^\d{1,12}$/.test(id)) return { error: 'A storage game id is needed' };
  if (!token) return { error: 'Storage credentials required in the Authorization header' };
  if (token.length > 512 || /[\s\0]/.test(token)) return { error: 'That storage credential looks invalid' };
  const art = artwork && typeof artwork === 'object' ? artwork : null;
  device.watches = (device.watches || []).filter(w => w.storageId !== id && w.torrentId !== id);
  if (device.watches.length >= MAX_WATCHES) return { error: 'Too many games waiting for remote storage' };
  const w = {
    storageId: id,
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
  device.watchCheckedAt = 0;
  return { watch: w };
}

function removeWatch(device, id) {
  const targetId = String(id);
  const before = (device.watches || []).length;
  device.watches = (device.watches || []).filter(w => w.storageId !== targetId && w.torrentId !== targetId);
  return device.watches.length !== before;
}

const AUTH_ERROR = /BAD_TOKEN|NO_AUTH|AUTH_ERROR|INVALID_TOKEN|TOKEN_EXPIRED|UNAUTHORI[SZ]ED/i;
const watchJobId = (w) => `job_w${w.storageId || w.torrentId}_${Number(w.at) || 0}`;

async function readRemoteStorage(token, id, ms) {
  const base = process.env.STORAGE_API_URL || process.env.TORBOX_API_URL || 'http://127.0.0.1:8765/mock-storage';
  const r = await fetch(`${base}/games/mylist?id=${id}&bypass_cache=true`, {
    headers: { authorization: `Bearer ${token}`, 'user-agent': 'DeckStream/1.0' },
    signal: AbortSignal.timeout(ms),
  });
  if (r.status === 401 || r.status === 403) return { rejected: true };
  const j = await r.json().catch(() => null);
  const why = j ? String(j.error || '') + ' ' + String(j.detail || '') : '';
  if (j && j.success === false && AUTH_ERROR.test(why)) return { rejected: true };
  if (!r.ok || !j) return { retry: true };
  const list = Array.isArray(j.data) ? j.data : j.data ? [j.data] : [];
  const item = list.find(t => String(t.id) === id) || null;
  if (j.success === false && !item) return /not.?found|NOT_FOUND|DATABASE/i.test(why) ? { gone: true } : { retry: true };
  return item ? { item } : { gone: true };
}

async function checkWatches(device, notifyFn, msPerWatch = 8000) {
  if (!device || !Array.isArray(device.watches) || !device.watches.length) return false;
  const now = Date.now();
  if (now - (Number(device.watchCheckedAt) || 0) < CHECK_EVERY_MS) return false;
  device.watchCheckedAt = now;

  const keep = [];
  let modified = false;

  for (const w of device.watches) {
    const age = now - (Number(w.at) || 0);
    if (age > WATCH_HARD_TTL_MS) { modified = true; continue; }

    const token = unseal(w.key);
    if (!token) { modified = true; continue; }

    const id = w.storageId || w.torrentId;
    const res = await readRemoteStorage(token, id, msPerWatch);
    if (res.rejected) { modified = true; continue; }
    if (res.retry) { keep.push(w); continue; }
    if (res.gone) { modified = true; continue; }

    const item = res.item;
    if (!isCached(item)) { keep.push(w); continue; }

    // Ready!
    const plan = buildPlan(item);
    if (STREAM_KINDS_BLOCKED.includes(plan.kind)) {
      w.state = 'needs-install';
      keep.push(w);
      modified = true;
      continue;
    }

    // Streamable!
    const job = {
      id: watchJobId(w),
      title: w.title,
      mode: 'stream',
      webdavPath: plan.webdavFolder || item.name,
      plan,
      artwork: w.artwork,
      createdAt: now,
      status: 'pending'
    };
    device.jobs = device.jobs || [];
    if (!device.jobs.some(j => j.id === job.id)) {
      device.jobs.push(job);
    }
    modified = true;
  }

  device.watches = keep;
  return modified;
}

module.exports = {
  addWatch,
  removeWatch,
  checkWatches,
  publicWatch,
  CHECK_EVERY_MS,
  WATCH_HARD_TTL_MS
};
