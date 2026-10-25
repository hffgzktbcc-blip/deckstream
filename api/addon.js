// Prowlarr & Jackett Serverless Relay Proxy
// Bypasses browser CORS & HTTPS Mixed-Content blocking when querying self-hosted indexers.
const { applyCors, requireSecret, assertPublicHttpUrl } = require('./_lib');

module.exports = async (req, res) => {
  if (applyCors(req, res, 'GET, POST, OPTIONS')) return;
  if (!requireSecret(req, res)) return;

  const rawUrl = req.headers['x-addon-url'] || req.query.url || '';
  const apiKey = req.headers['x-addon-key'] || req.query.key || '';
  const action = req.query.action || 'health'; // 'health', 'search', 'test'
  const query = req.query.q || req.query.query || '';

  if (!rawUrl) {
    return res.status(400).json({ error: 'Missing add-on URL (query param "url" or "x-addon-url" header).' });
  }

  const cleanUrl = String(rawUrl).replace(/\/+$/, '');
  try {
    await assertPublicHttpUrl(cleanUrl);
  } catch (e) {
    return res.status(400).json({ error: 'Add-on URL rejected: ' + e.message });
  }

  try {
    if (action === 'health') {
      // 1. Try Prowlarr health endpoint
      try {
        const pHealth = await fetch(`${cleanUrl}/api/v1/health`, {
          headers: apiKey ? { 'X-Api-Key': apiKey } : {},
          signal: AbortSignal.timeout(6000)
        });
        if (pHealth.ok) {
          const data = await pHealth.json();
          return res.status(200).json({
            ok: true,
            type: 'prowlarr',
            message: 'Connected to Prowlarr successfully',
            data
          });
        }
      } catch (e) {
        // try Jackett below
      }

      // 2. Try Jackett indexer listing / status
      try {
        const jHealth = await fetch(`${cleanUrl}/api/v2.0/indexers?apikey=${apiKey || ''}`, {
          signal: AbortSignal.timeout(6000)
        });
        if (jHealth.ok) {
          const indexers = await jHealth.json();
          return res.status(200).json({
            ok: true,
            type: 'jackett',
            message: `Connected to Jackett successfully (${Array.isArray(indexers) ? indexers.length : 0} indexers)`,
            data: indexers
          });
        }
      } catch (e) {}

      // 3. Fallback: Check root or info
      const rootRes = await fetch(cleanUrl, {
        headers: apiKey ? { 'X-Api-Key': apiKey } : {},
        signal: AbortSignal.timeout(5000)
      });
      return res.status(200).json({
        ok: rootRes.ok,
        status: rootRes.status,
        message: rootRes.ok ? 'Add-on server reachable' : `Server responded with ${rootRes.status}`
      });
    }

    if (action === 'search') {
      if (!query) {
        return res.status(400).json({ error: 'Query parameter "q" is required for search.' });
      }

      // Try Prowlarr search first
      try {
        const pSearch = await fetch(`${cleanUrl}/api/v1/search?query=${encodeURIComponent(query)}&categories=1000&categories=4000`, {
          headers: apiKey ? { 'X-Api-Key': apiKey } : {},
          signal: AbortSignal.timeout(10000)
        });
        if (pSearch.ok) {
          const data = await pSearch.json();
          return res.status(200).json({ type: 'prowlarr', results: data });
        }
      } catch (e) {}

      // Try Jackett search
      try {
        const jSearch = await fetch(`${cleanUrl}/api/v2.0/indexers/all/results?apikey=${apiKey}&Query=${encodeURIComponent(query)}&Category[]=1000&Category[]=4000`, {
          signal: AbortSignal.timeout(10000)
        });
        if (jSearch.ok) {
          const data = await jSearch.json();
          return res.status(200).json({ type: 'jackett', results: data && data.Results ? data.Results : data });
        }
      } catch (e) {}

      return res.status(502).json({ error: 'Could not fetch search results from add-on service.' });
    }

    return res.status(400).json({ error: 'Invalid action. Use "health" or "search".' });
  } catch (err) {
    res.status(502).json({
      ok: false,
      error: `Could not reach add-on service at ${cleanUrl}: ${err.message || 'Connection timed out or failed'}`
    });
  }
};
