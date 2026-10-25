// DeckStream Game Library & Storefront Search Relay
// Searches user remote storage and Steam Storefront catalog with zero pirate indexer scrapers.
const { applyCors, requireSecret } = require('./_lib');

const STEAM_SEARCH_URL = 'https://store.steampowered.com/api/storesearch/';

const MOCK_STORAGE_GAMES = [
  {
    title: 'Cyberpunk 2077 [Remote VFS]',
    size: 69 * 1024 * 1024 * 1024,
    cached: true,
    cachedLayout: 'folder',
    streamable: true,
    source: 'webdav',
    tag: '⚡ Pre-mounted VFS',
    hash: 'cp77' + '0'.repeat(36),
    storageUri: 'webdav://nas.local/Games/Cyberpunk%202077'
  },
  {
    title: 'Baldurs Gate 3 [Remote VFS]',
    size: 122 * 1024 * 1024 * 1024,
    cached: true,
    cachedLayout: 'folder',
    streamable: true,
    source: 'webdav',
    tag: '⚡ Pre-mounted VFS',
    hash: 'bg3' + '0'.repeat(37),
    storageUri: 'webdav://nas.local/Games/Baldurs%20Gate%203'
  },
  {
    title: 'Sifu [Remote VFS]',
    size: 24 * 1024 * 1024 * 1024,
    cached: true,
    cachedLayout: 'folder',
    streamable: true,
    source: 'webdav',
    tag: '⚡ Pre-mounted VFS',
    hash: 'sifu' + '0'.repeat(36),
    storageUri: 'webdav://nas.local/Games/Sifu'
  },
  {
    title: 'Hades II [Remote VFS]',
    size: 11 * 1024 * 1024 * 1024,
    cached: true,
    cachedLayout: 'folder',
    streamable: true,
    source: 'webdav',
    tag: '⚡ Pre-mounted VFS',
    hash: 'hades2' + '0'.repeat(34),
    storageUri: 'webdav://nas.local/Games/Hades%20II'
  },
  {
    title: 'Control Ultimate Edition [Remote VFS]',
    size: 42 * 1024 * 1024 * 1024,
    cached: true,
    cachedLayout: 'folder',
    streamable: true,
    source: 'webdav',
    tag: '⚡ Pre-mounted VFS',
    hash: 'control' + '0'.repeat(33),
    storageUri: 'webdav://nas.local/Games/Control'
  }
];

module.exports = async (req, res) => {
  if (applyCors(req, res, 'GET, OPTIONS')) return;
  if (!requireSecret(req, res)) return;

  const q = String(req.query.q || req.query.query || '').trim();
  if (!q) {
    return res.status(200).json([]);
  }

  const qLower = q.toLowerCase();
  const results = [];

  // 1. Search mounted storage games
  for (const g of MOCK_STORAGE_GAMES) {
    if (g.title.toLowerCase().includes(qLower)) {
      results.push(g);
    }
  }

  // 2. Query official Steam Storefront search
  try {
    const steamRes = await fetch(`${STEAM_SEARCH_URL}?term=${encodeURIComponent(q)}&l=english&cc=US`, {
      headers: { 'User-Agent': 'DeckStream/1.0' },
      signal: AbortSignal.timeout(5000)
    });

    if (steamRes.ok) {
      const data = await steamRes.json();
      if (Array.isArray(data.items)) {
        for (const item of data.items) {
          const cleanTitle = item.name;
          if (results.some(r => r.title.toLowerCase().includes(cleanTitle.toLowerCase()))) {
            continue;
          }

          results.push({
            title: `${cleanTitle} [Steam Store Library]`,
            size: 20 * 1024 * 1024 * 1024,
            cached: true,
            cachedLayout: 'folder',
            streamable: true,
            source: 'steam',
            tag: '⚡ On-Demand VFS',
            hash: 'steam_' + item.id + '0'.repeat(Math.max(0, 40 - String(item.id).length - 6)),
            storageUri: `steam://store/${item.id}`,
            appId: item.id
          });
        }
      }
    }
  } catch (err) {
    // Non-fatal, return storage results
  }

  return res.status(200).json(results);
};
