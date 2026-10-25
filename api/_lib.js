// Shared helpers for the serverless relays. Files starting with "_" are not deployed as routes.
const crypto = require('crypto');
const dns = require('dns').promises;
const net = require('net');

// Same-origin requests need no CORS. Set ALLOWED_ORIGINS (comma separated) only if a
// different site must call these endpoints from a browser.
function applyCors(req, res, methods = 'GET, POST, OPTIONS') {
  const allowed = (process.env.ALLOWED_ORIGINS || '').split(',').map(s => s.trim()).filter(Boolean);
  const origin = req.headers.origin;
  if (origin && allowed.includes(origin)) {
    res.setHeader('access-control-allow-origin', origin);
    res.setHeader('vary', 'Origin');
    res.setHeader('access-control-allow-methods', methods);
    res.setHeader('access-control-allow-headers', 'content-type, authorization, x-deckstream-secret, x-deckstream-secret, x-device-id');
  }
  res.setHeader('x-content-type-options', 'nosniff');
  if (req.method === 'OPTIONS') {
    res.status(204).end();
    return true;
  }
  return false;
}

// Constant-time check of the shared DECKSTREAM_SECRET. Returns true when the request may proceed;
// otherwise it has already sent the error response.
function requireSecret(req, res) {
  const secret = process.env.DECKSTREAM_SECRET || process.env.DECKSTREAM_SECRET || '';
  if (!secret) {
    res.status(503).json({ error: 'Disabled: set DECKSTREAM_SECRET in environment' });
    return false;
  }
  // Hash both sides so length differences don't leak through timingSafeEqual.
  const digest = (s) => crypto.createHash('sha256').update(String(s)).digest();
  const given = req.headers['x-deckstream-secret'] || req.headers['x-deckstream-secret'] || '';
  if (!crypto.timingSafeEqual(digest(given), digest(secret))) {
    res.status(401).json({ error: 'Queue password missing or wrong' });
    return false;
  }
  return true;
}

function isPrivateIp(ip) {
  if (net.isIPv4(ip)) {
    const [a, b] = ip.split('.').map(Number);
    return (
      a === 0 || a === 10 || a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      a >= 224
    );
  }
  if (net.isIPv6(ip)) {
    const l = ip.toLowerCase();
    if (l === '::1' || l === '::') return true;
    if (l.startsWith('fc') || l.startsWith('fd') || l.startsWith('fe80')) return true;
    const mapped = l.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
    if (mapped) return isPrivateIp(mapped[1]);
    return false;
  }
  return true;
}

// For user-supplied add-on URLs (Prowlarr/Jackett). Must be http(s) and resolve only to public
// addresses, so the relay can't be pointed at cloud metadata or internal services.
async function assertPublicHttpUrl(raw) {
  let u;
  try { u = new URL(String(raw)); } catch { throw new Error('Invalid URL'); }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') throw new Error('Only http(s) URLs are allowed');
  if (u.username || u.password) throw new Error('Credentials in URL are not allowed');
  const host = u.hostname.replace(/^\[|\]$/g, '');
  const addrs = net.isIP(host) ? [{ address: host }] : await dns.lookup(host, { all: true });
  if (!addrs.length || addrs.some(a => isPrivateIp(a.address))) {
    throw new Error('URL must point to a public address');
  }
  return u;
}

// Hosts a download job may fetch from.
const DEFAULT_DOWNLOAD_HOSTS = ['nas.local', 'storage.local', 's3.amazonaws.com'];
function downloadHosts() {
  const extra = (process.env.ALLOWED_DOWNLOAD_HOSTS || '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
  return [...DEFAULT_DOWNLOAD_HOSTS, ...extra];
}
function isAllowedDownloadUrl(raw) {
  try {
    const u = new URL(String(raw));
    if (u.protocol !== 'https:') return false;
    const h = u.hostname.toLowerCase();
    return downloadHosts().some(d => h === d || h.endsWith('.' + d));
  } catch {
    return false;
  }
}

module.exports = { applyCors, requireSecret, assertPublicHttpUrl, isAllowedDownloadUrl, downloadHosts };
