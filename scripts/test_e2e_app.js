#!/usr/bin/env node
// Browser smoke test: boots scripts/dev_server.js (real api/*.js + mock storage), opens the app in headless Chromium
// and walks every tab, the library views, the game sheet and a Send to Deck. Fails on any page error, on any
// /api/* crash (HTTP 500; 502 from an offline Steam is fine), and when a step's expected element is missing. External hosts are blocked (offline run).
const assert = require('assert');
const { start } = require('./dev_server');

let chromium;
try { ({ chromium } = require('playwright')); } catch (_) {
  if (process.env.CI) throw new Error('playwright is required in CI');
  console.log('[skip] playwright not installed (npm i --no-save playwright to run the browser smoke test)');
  process.exit(0);
}

(async () => {
  process.env.DECKSTREAM_SECRET = 'test-secret-123';
  const srv = await start({ port: 0 });
  const base = `http://127.0.0.1:${srv.port}`;
  let browser;
  try {
    browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  } catch (e) {
    srv.close();
    if (process.env.CI) throw e;
    console.log('[skip] no Chromium available: ' + e.message.split('\n')[0]);
    process.exit(0);
  }
  const errors = [];
  const apiFailures = [];
  try {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: 'block' });
    await ctx.route('**/*', (route) => {
      const u = new URL(route.request().url());
      return u.hostname === '127.0.0.1' ? route.continue() : route.abort();
    });
    await ctx.addInitScript(() => {
      try {
        localStorage.setItem('deck_queue_secret', 'test-secret-123');
        localStorage.setItem('storage_api_key', 'mock-storage-key');
        localStorage.setItem('deck_welcome', 'off');
        localStorage.setItem('deck_intro', 'off');
      } catch (_) {}
    });
    const page = await ctx.newPage();
    page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
    page.on('response', (r) => {
      if (r.url().includes('/api/') && r.status() === 500) apiFailures.push(`${r.status()} ${r.url()}`);
    });
    page.on('dialog', (d) => d.accept());

    await page.goto(base + '/', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(800);

    for (const tab of ['discover', 'cloud', 'search', 'queue', 'settings']) {
      await page.evaluate((t) => switchTab(t), tab);
      await page.waitForTimeout(300);
      assert.ok(await page.locator('#tab-' + tab).isVisible(), `tab ${tab} visible`);
    }

    // Library: the mock storage's games load and render in every view mode.
    await page.evaluate(() => switchTab('cloud'));
    await page.evaluate(() => loadLibrary(true));
    await page.waitForTimeout(600);
    const count = await page.evaluate(() => (state.libraryItems || []).length);
    assert.ok(count >= 5, 'library loaded from mock storage, got ' + count);
    for (const mode of ['covers', 'grid', 'list']) {
      await page.evaluate((m) => setLibraryViewMode(m), mode);
      await page.waitForTimeout(150);
      const html = await page.locator('#libraryListContainer').innerHTML();
      assert.ok(html.length > 200, `library ${mode} view rendered`);
    }
    for (const f of ['all', 'cached', 'downloading']) await page.evaluate((x) => setLibraryFilter(x), f);

    // Search: the Steam catalog is offline here, but the page must not throw.
    await page.evaluate(() => switchTab('search'));
    await page.fill('#gameSearchInput', 'Cyberpunk');
    await page.evaluate(() => searchGamesOnline());
    await page.waitForTimeout(800);
    const found = await page.locator('#searchResultsContainer').innerText();
    assert.ok(/Cyberpunk 2077/.test(found) && /Streams from your storage/.test(found), 'search finds the library copy: ' + found.slice(0, 200));

    // Game page: the library copy shows under the title.
    await page.evaluate(() => openGamePage({ title: 'Hades II', appId: 0 }));
    await page.waitForTimeout(400);
    await page.evaluate(() => loadGameReleases('Hades II', gamePageSeq));
    await page.waitForTimeout(800);
    assert.ok(/Hades II/.test(await page.locator('#gpReleases').innerText()), 'game page lists the library copy');
    await page.evaluate(() => closeGamePage());

    // Install sheet for a library game, then Send to Deck: a job reaches the queue.
    await page.evaluate(() => switchTab('cloud'));
    await page.evaluate(() => openInstallModal(state.libraryItems[0].id));
    await page.waitForTimeout(600);
    assert.ok(await page.locator('#installModal.open').count(), 'install sheet opens');
    await page.evaluate(() => confirmEnqueue());
    await page.waitForTimeout(800);
    const q = await page.evaluate(async () => (await fetch('/api/queue?device=' + encodeURIComponent(state.deviceId) + '&action=status', {
      headers: { 'x-deckstream-secret': 'test-secret-123' } })).json());
    assert.ok(Array.isArray(q.jobs) && q.jobs.length >= 1, 'Send to Deck queued a job: ' + JSON.stringify(q).slice(0, 200));
    const job = q.jobs[0];
    assert.strictEqual(job.storageId, '101', 'job carries the storage id');
    assert.strictEqual(job.webdavPath, 'Cyberpunk 2077', 'job streams the game folder');
    assert.ok(['stream', 'cloudinstall'].includes(job.mode), 'job mode ' + job.mode);

    // One-tap stream path.
    await page.evaluate(() => sendToDeckDirect(state.libraryItems.find(t => t.name === 'Sifu').id));
    await page.evaluate(() => streamWhenReady(state.libraryItems.find(t => t.name === 'Hollow Knight').id));
    await page.waitForTimeout(800);

    // Settings save keeps the storage key.
    await page.evaluate(() => switchTab('settings'));
    await page.evaluate(() => saveSettings({ quiet: true }));
    assert.strictEqual(await page.evaluate(() => localStorage.getItem('storage_api_key')), 'mock-storage-key');

    await page.evaluate(() => switchTab('queue'));
    await page.waitForTimeout(500);
    if (process.env.SCREENSHOT_DIR) {
      await page.evaluate(() => setLibraryFilter('all'));
      await page.waitForTimeout(3000);   // toasts gone
      for (const tab of ['discover', 'cloud', 'search', 'queue', 'settings']) {
        await page.evaluate((t) => switchTab(t), tab);
        await page.waitForTimeout(300);
        await page.screenshot({ path: `${process.env.SCREENSHOT_DIR}/${tab}.png`, fullPage: false });
      }
    }
  } finally {
    await browser.close();
    srv.close();
  }
  assert.deepStrictEqual(errors, [], 'page errors:\n' + errors.join('\n'));
  assert.deepStrictEqual(apiFailures, [], 'api failures:\n' + apiFailures.join('\n'));
  console.log('[ok] app smoke: every tab, 3 library views, search, install sheet, Send to Deck, settings, no page errors');
})().catch((e) => { console.error(e.stack || e.message); process.exit(1); });
