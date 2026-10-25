// Launch plan for one TorBox torrent or Usenet download: what it is, whether it can stream,
// which exe to run, and what to do instead if it cannot. The TorBox key comes from the
// Authorization header (the app already holds it); the server never stores it.
const { applyCors, requireSecret } = require('./_lib');
const { buildPlan } = require('./_plan');

async function fetchTorBox(endpoint, token, id) {
  const base = process.env.TORBOX_API_URL || 'https://api.torbox.app/v1/api';
  const url = /^\d{1,12}$/.test(id)
    ? `${base}/${endpoint}?id=${id}&bypass_cache=true`
    : `${base}/${endpoint}?bypass_cache=true`;
  return fetch(url, {
    headers: { authorization: `Bearer ${token}`, 'user-agent': 'DeckDebrid/1.0' },
    signal: AbortSignal.timeout(25000),
  });
}

function findInList(data, id, name) {
  const list = Array.isArray(data) ? data : data ? [data] : [];
  if (/^\d{1,12}$/.test(id)) {
    return list.find((t) => {
      const tid = String(t.id ?? t.usenetdownload_id ?? t.usenet_id ?? '');
      return tid === id;
    }) || (list.length === 1 && !Array.isArray(data) ? list[0] : null);
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
  if (!token) return res.status(400).json({ error: 'TorBox key required in the Authorization header' });

  const torrentId = String(req.query.torrent_id || '').trim();
  const usenetId = String(req.query.usenet_id || '').trim();
  const webdlId = String(req.query.webdl_id || '').trim();
  if (webdlId) {
    // Web downloads (SteamRIP / SteamGG file-host links) have their own TorBox list; a single .zip shows up as an archive.
    if (!/^\d{1,12}$/.test(webdlId)) return res.status(400).json({ error: 'webdl_id must be a number' });
    try {
      const r = await fetchTorBox('webdl/mylist', token, webdlId);
      if (r.status === 401 || r.status === 403) return res.status(401).json({ error: 'TorBox rejected the key' });
      const j = await r.json().catch(() => null);
      if (!r.ok || !j || j.success === false) return res.status(502).json({ error: 'TorBox did not return the web download list' });
      const item = findInList(j.data, webdlId, '');
      if (!item) return res.status(404).json({ error: 'Item not found in your TorBox library' });
      return res.json({ success: true, plan: buildPlan(item, { mbps: Number(req.query.mbps) || undefined }) });
    } catch (err) {
      return res.status(502).json({ error: 'Could not reach TorBox' });
    }
  }
  const rawId = String(req.query.id || '').trim();
  const name = String(req.query.name || '').trim();
  const type = String(req.query.type || '').trim().toLowerCase();

  const id = usenetId || torrentId || rawId;
  const isUsenet = Boolean(usenetId) || type === 'usenet';

  if ((id && !/^\d{1,12}$/.test(id)) || (!id && !name)) {
    return res.status(400).json({ error: 'Pass torrent_id / usenet_id (number) or name' });
  }

  try {
    let item = null;
    let usedUsenet = isUsenet;
    if (isUsenet) {
      const r = await fetchTorBox('usenet/mylist', token, id);
      if (r.status === 401 || r.status === 403) return res.status(401).json({ error: 'TorBox rejected the key' });
      const j = await r.json().catch(() => null);
      if (!r.ok || !j || j.success === false) {
        return res.status(502).json({ error: 'TorBox did not return the usenet list' });
      }
      item = findInList(j.data, id, name);

      // Fallback: if queried by name and not found in usenet, check torrents
      if (!item && name && !id) {
        const r2 = await fetchTorBox('torrents/mylist', token, '');
        if (r2.status === 401 || r2.status === 403) return res.status(401).json({ error: 'TorBox rejected the key' });
        const j2 = await r2.json().catch(() => null);
        if (r2.ok && j2 && j2.success) {
          item = findInList(j2.data, '', name);
          if (item) usedUsenet = false;
        }
      }
    } else {
      const r = await fetchTorBox('torrents/mylist', token, id);
      if (r.status === 401 || r.status === 403) return res.status(401).json({ error: 'TorBox rejected the key' });
      const j = await r.json().catch(() => null);
      if (!r.ok || !j || j.success === false) {
        return res.status(502).json({ error: 'TorBox did not return the torrent list' });
      }
      item = findInList(j.data, id, name);

      // Fallback: if queried by name (or generic id without torrent_id) and not found in torrents, check usenet
      if (!item && (name || (!torrentId && id))) {
        const r2 = await fetchTorBox('usenet/mylist', token, id);
        if (r2.status === 401 || r2.status === 403) return res.status(401).json({ error: 'TorBox rejected the key' });
        const j2 = await r2.json().catch(() => null);
        if (r2.ok && j2 && j2.success) {
          item = findInList(j2.data, id, name);
          if (item) usedUsenet = true;
        }
      }
    }

    if (!item) return res.status(404).json({ error: 'Item not found in your TorBox library' });

    return res.json({
      success: true,
      plan: buildPlan(item, { mbps: Number(req.query.mbps) || undefined, isUsenet: usedUsenet })
    });
  } catch (err) {
    return res.status(502).json({ error: 'Could not reach TorBox' });
  }
};
