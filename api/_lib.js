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
    res.setHeader('access-control-allow-headers', 'content-type, authorization, x-deckstream-secret, x-device-id');
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
  const secret = process.env.DECKSTREAM_SECRET || '';
  if (!secret) {
    res.status(503).json({ error: 'Disabled: set DECKSTREAM_SECRET in environment' });
    return false;
  }
  // Hash both sides so length differences don't leak through timingSafeEqual.
  const digest = (s) => crypto.createHash('sha256').update(String(s)).digest();
  const given = req.headers['x-deckstream-secret'] || '';
  if (!crypto.timingSafeEqual(digest(given), digest(secret))) {
    res.status(401).json({ error: 'Queue password missing or wrong' });
    return false;
  }
  return true;
}

// Expand an IPv6 address (any notation, optional %zone) to eight 16-bit numbers, or null.
function ipv6Hextets(ip) {
  let s = String(ip).toLowerCase().replace(/%.*$/, '');
  const dotted = s.match(/^(.*:)(\d+\.\d+\.\d+\.\d+)$/);
  if (dotted) {
    if (!net.isIPv4(dotted[2])) return null;
    const [a, b, c, d] = dotted[2].split('.').map(Number);
    s = dotted[1] + ((a << 8) | b).toString(16) + ':' + ((c << 8) | d).toString(16);
  }
  const halves = s.split('::');
  if (halves.length > 2) return null;
  const part = (x) => (x ? x.split(':') : []);
  const head = part(halves[0]);
  const tail = halves.length === 2 ? part(halves[1]) : [];
  const fill = halves.length === 2 ? 8 - head.length - tail.length : 0;
  if (fill < 0) return null;
  const all = [...head, ...Array(fill).fill('0'), ...tail];
  if (all.length !== 8 || !all.every(x => /^[0-9a-f]{1,4}$/.test(x))) return null;
  return all.map(x => parseInt(x, 16));
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
    const h = ipv6Hextets(ip);
    if (!h) return true;
    if (h.slice(0, 7).every(x => x === 0) && h[7] <= 1) return true;      // :: and ::1
    if ((h[0] & 0xfe00) === 0xfc00) return true;                          // fc00::/7 unique local
    if ((h[0] & 0xffc0) === 0xfe80) return true;                          // fe80::/10 link local
    if ((h[0] & 0xffc0) === 0xfec0) return true;                          // fec0::/10 old site local
    if ((h[0] & 0xff00) === 0xff00) return true;                          // ff00::/8 multicast
    // Forms that carry an IPv4 address in the last 32 bits: IPv4-mapped (::ffff:a.b.c.d, which
    // the URL parser rewrites to ::ffff:7f00:1), IPv4-compatible (::a.b.c.d) and NAT64 (64:ff9b::/96).
    const v4 = () => [h[6] >> 8, h[6] & 255, h[7] >> 8, h[7] & 255].join('.');
    if (h.slice(0, 5).every(x => x === 0) && (h[5] === 0xffff || h[5] === 0)) return isPrivateIp(v4());
    if (h[0] === 0x64 && h[1] === 0xff9b && h.slice(2, 6).every(x => x === 0)) return isPrivateIp(v4());
    if (h[0] === 0x2002) return isPrivateIp([h[1] >> 8, h[1] & 255, h[2] >> 8, h[2] & 255].join('.')); // 6to4
    return false;
  }
  return true;
}

// For user-supplied URLs the server fetches. Must be http(s) and resolve only to public
// addresses, so a relay can't be pointed at cloud metadata or internal services.
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

// Hosts a download job may fetch from: the storage server's own host, plus ALLOWED_DOWNLOAD_HOSTS (comma separated).
function storageHost() {
  try { return new URL(String(process.env.STORAGE_API_URL || '')).hostname.toLowerCase(); } catch { return ''; }
}
function downloadHosts() {
  const extra = (process.env.ALLOWED_DOWNLOAD_HOSTS || '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
  return [storageHost(), ...extra].filter(Boolean);
}
// https for any allowed host; plain http only for the storage server itself (a NAS on the home network).
function isAllowedDownloadUrl(raw) {
  try {
    const u = new URL(String(raw));
    const h = u.hostname.toLowerCase();
    if (u.protocol === 'http:') return h === storageHost();
    if (u.protocol !== 'https:') return false;
    return downloadHosts().some(d => h === d || h.endsWith('.' + d));
  } catch {
    return false;
  }
}

module.exports = { applyCors, requireSecret, assertPublicHttpUrl, isAllowedDownloadUrl, downloadHosts, isPrivateIp };
