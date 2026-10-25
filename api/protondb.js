// ProtonDB & Steam Compatibility Relay
// Resolves game titles to Steam App IDs and queries ProtonDB reports for Deck compatibility tier.
const { applyCors, requireSecret } = require('./_lib');

const CACHE = new Map();
const CACHE_TTL = 3600 * 1000 * 24; // 24 hours in memory

module.exports = async (req, res) => {
  if (applyCors(req, res, 'GET, OPTIONS')) return;
  if (!requireSecret(req, res)) return;

  let appId = req.query.appid || req.query.appId || '';
  const title = (req.query.title || req.query.q || '').trim();

  if (!appId && !title) {
    return res.status(400).json({ error: 'Either "appid" or "title" parameter is required.' });
  }

  try {
    // 1. If no appId given, look up Steam App ID from title
    let cleanGameTitle = title;
    if (!appId && title) {
      // Strip scene release tags, repacks, and noise for clean Steam search
      cleanGameTitle = title
        .replace(/\b(v?\d+(\.\d+)+|deluxe|ultimate|complete|goty|edition|remastered|directors cut|enhanced)\b/gi, '')
        .replace(/[\[\]\(\)\-_]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();

      const cachedApp = CACHE.get(`steam:${cleanGameTitle.toLowerCase()}`);
      if (cachedApp && cachedApp.expires > Date.now()) {
        appId = cachedApp.appId;
      } else {
        const steamSearchUrl = `https://store.steampowered.com/api/storesearch/?term=${encodeURIComponent(cleanGameTitle)}&l=english&cc=US`;
        const steamRes = await fetch(steamSearchUrl, {
          headers: { 'User-Agent': 'DeckStream/1.0' },
          signal: AbortSignal.timeout(6000)
        });
        if (steamRes.ok) {
          const steamData = await steamRes.json();
          if (steamData.items && steamData.items.length > 0) {
            appId = String(steamData.items[0].id);
            CACHE.set(`steam:${cleanGameTitle.toLowerCase()}`, { appId, expires: Date.now() + CACHE_TTL });
          }
        }
      }
    }

    if (!appId) {
      return res.status(200).json({
        found: false,
        title: cleanGameTitle,
        tier: 'unknown',
        badge: '❓ Unknown'
      });
    }

    // 2. Fetch ProtonDB compatibility report
    const cacheKey = `proton:${appId}`;
    const cachedProton = CACHE.get(cacheKey);
    if (cachedProton && cachedProton.expires > Date.now()) {
      return res.status(200).json(cachedProton.data);
    }

    const protonUrl = `https://www.protondb.com/api/v1/reports/summaries/${appId}.json`;
    const protonRes = await fetch(protonUrl, {
      headers: { 'User-Agent': 'DeckStream/1.0' },
      signal: AbortSignal.timeout(6000)
    });

    if (!protonRes.ok) {
      const fallback = {
        found: true,
        appId,
        title: cleanGameTitle,
        tier: 'pending',
        badge: '⏳ Unverified',
        confidence: 'none',
        score: null,
        total: 0
      };
      return res.status(200).json(fallback);
    }

    const protonData = await protonRes.json();
    const tier = (protonData.tier || protonData.trendingTier || protonData.bestReportedTier || 'unknown').toLowerCase();

    // Map tier to friendly visual badge
    const tierMap = {
      native: { badge: '🐧 Steam Native', color: '#10b981', tier: 'native' },
      platinum: { badge: '✨ Platinum (Flawless)', color: '#b4c6e7', tier: 'platinum' },
      gold: { badge: '🥇 Gold (Great)', color: '#f59e0b', tier: 'gold' },
      silver: { badge: '🥈 Silver (Playable)', color: '#94a3b8', tier: 'silver' },
      bronze: { badge: '🥉 Bronze (Tweaks Needed)', color: '#d97706', tier: 'bronze' },
      borked: { badge: '❌ Borked (Unplayable)', color: '#ef4444', tier: 'borked' }
    };

    const tierInfo = tierMap[tier] || { badge: '❓ Unknown', color: '#6b7280', tier };

    const payload = {
      found: true,
      appId,
      title: cleanGameTitle,
      tier,
      tierName: tierInfo.tier,
      badge: tierInfo.badge,
      color: tierInfo.color,
      score: protonData.score,
      confidence: protonData.confidence,
      total: protonData.total || 0,
      trendingTier: protonData.trendingTier,
      bestReportedTier: protonData.bestReportedTier,
      url: `https://www.protondb.com/app/${appId}`
    };

    CACHE.set(cacheKey, { data: payload, expires: Date.now() + CACHE_TTL });
    res.setHeader('cache-control', 'public, max-age=86400');
    return res.status(200).json(payload);
  } catch (err) {
    return res.status(500).json({ error: 'Failed to retrieve ProtonDB info: ' + err.message });
  }
};
