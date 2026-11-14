// Device queue storage. Vercel functions don't share memory, so state lives in Redis when
// UPSTASH_REDIS_REST_URL/TOKEN (or the Vercel KV equivalents) are set. Falls back to memory for
// local runs and tests.
const URL_ = process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL || '';
const TOKEN = process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN || '';
const TTL_SEC = 7 * 24 * 3600;
const mem = new Map();

const persistent = () => Boolean(URL_ && TOKEN);

async function redis(args) {
  const r = await fetch(URL_, {
    method: 'POST',
    headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json' },
    body: JSON.stringify(args),
    signal: AbortSignal.timeout(5000)
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error('Queue storage error: ' + (j.error || r.status));
  return j.result;
}

const key = (id) => `deckstream:device:${id}`;

async function loadDevice(id) {
  if (!persistent()) return mem.get(id) || null;
  const raw = await redis(['GET', key(id)]);
  return raw ? JSON.parse(raw) : null;
}

async function saveDevice(id, device) {
  if (!persistent()) { mem.set(id, device); return; }
  await redis(['SET', key(id), JSON.stringify(device), 'EX', String(TTL_SEC)]);
}

// Small JSON documents under their own prefix (start-up profiles), with their own lifetime.
const docKey = (kind, id) => `deckstream:${kind}:${id}`;

async function loadDoc(kind, id) {
  if (!persistent()) return mem.get(docKey(kind, id)) || null;
  const raw = await redis(['GET', docKey(kind, id)]);
  return raw ? JSON.parse(raw) : null;
}

async function saveDoc(kind, id, doc, ttlSec) {
  if (!persistent()) { mem.set(docKey(kind, id), doc); return; }
  await redis(['SET', docKey(kind, id), JSON.stringify(doc), 'EX', String(ttlSec)]);
}

// A short lock: true for the first caller only, until ttlSec passes (SET NX). Keeps two requests from doing the same work.
async function claim(kind, id, ttlSec) {
  const k = docKey(kind, id);
  if (!persistent()) {
    const until = mem.get(k);
    if (until && until > Date.now()) return false;
    mem.set(k, Date.now() + ttlSec * 1000);
    return true;
  }
  return (await redis(['SET', k, '1', 'NX', 'EX', String(ttlSec)])) === 'OK';
}

// Devices only: docs and locks share the map under their own prefix and don't count toward the local device cap.
const memSize = () => [...mem.keys()].filter(k => !k.startsWith('deckstream:')).length;

module.exports = { loadDevice, saveDevice, loadDoc, saveDoc, claim, persistent, memSize };
