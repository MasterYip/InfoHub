// Run with Playwright available: node --test tests/test_link_previews.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

test('web links open directly without iframes; unit cards dismiss on focus loss', async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 850}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      const data = path === '/api/unit/42'
        ? {id: 42, title: 'Preview topic', kind: 'topic', status: 'open', tags: []}
        : path === '/api/today' ? {needs: [], events: [], unscheduled_events: [], unsorted: []}
        : path === '/api/health' ? {ok: true, app: 'infohub'} : [];
      await route.fulfill({json: data});
    });
    await page.context().route('https://blocked.example/**', route => route.fulfill({
      headers: {'X-Frame-Options': 'DENY', 'Content-Security-Policy': "frame-ancestors 'none'"},
      contentType: 'text/html', body: '<h1>Top-level preview works</h1>'}));
    await page.goto('http://127.0.0.1:8765/?tab=chat');
    assert.equal(await page.locator('#app-brand svg path').count(), 2);
    assert.ok((await page.locator('#app-favicon').getAttribute('href')).startsWith('data:image/svg+xml,'));
    const darkFavicon = await page.locator('#app-favicon').getAttribute('href');
    await page.evaluate(() => applyTheme('light'));
    assert.notEqual(await page.locator('#app-favicon').getAttribute('href'), darkFavicon);
    await page.evaluate(() => applyTheme('dark'));
    await page.locator('#chat-input').waitFor();
    await page.evaluate(async () => {
      CHAT = [{role: 'assistant', content: '[Website](https://blocked.example/page)\n\n[Topic](/?unit=42)\n\nhttps://blocked.example/plain.\n\n`https://blocked.example/code`'}];
      await render();
    });
    assert.equal(await page.locator('.preview-link').count(), 3);
    assert.equal(await page.locator('code a').count(), 0);
    // Both the link and its preview icon must directly open a top-level window.
    for (const control of [page.getByRole('link', {name: 'Website', exact: true}),
                           page.getByRole('button', {name: 'Preview Website', exact: true})]) {
      const opened = page.waitForEvent('popup');
      await control.click();
      const popup = await opened;
      await popup.getByRole('heading', {name: 'Top-level preview works'}).waitFor();
      assert.equal(await popup.evaluate(() => window.top === window.self && window.opener === null), true);
      assert.equal(await page.locator('iframe').count(), 0);
      await popup.close();
    }
    await page.getByRole('link', {name: 'Topic', exact: true}).click();
    await page.locator('.float-card .title').waitFor();
    assert.equal(await page.locator('.float-card .title').textContent(), 'Preview topic');
    await page.locator('#chat-input').focus();
    assert.equal(await page.locator('.float-card').count(), 0);
    await page.setViewportSize({width: 390, height: 700});
    await page.getByRole('link', {name: 'Topic', exact: true}).click();
    await page.locator('.float-card').waitFor();
    const bounds = await page.locator('.float-card').boundingBox();
    assert.ok(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= 390 && bounds.y + bounds.height <= 700);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.float-card').count(), 0);
    // Desktop selects its native bridge instead of creating a browser popup or frame.
    await page.evaluate(() => {
      window.previewCalls = [];
      window.previewCloses = 0;
      window.pywebview = {api: {
        open_preview: async (url, hover) => { window.previewCalls.push({url, hover}); return {ok: true}; },
        close_hover_preview: async () => { window.previewCloses++; }
      }};
    });
    await page.getByRole('button', {name: 'Preview Website', exact: true}).click();
    assert.deepEqual(await page.evaluate(() => window.previewCalls), [{url: 'https://blocked.example/page', hover: null}]);
    await page.locator('#chat-input').click();
    await page.evaluate(() => { window.previewCalls = []; });
    await page.getByRole('link', {name: 'Website', exact: true}).hover();
    await page.waitForFunction(() => window.previewCalls.length === 1);
    const hovered = await page.evaluate(() => window.previewCalls[0]);
    assert.equal(hovered.url, 'https://blocked.example/page');
    assert.ok(hovered.hover.left <= 0 && hovered.hover.right >= 0);
    assert.ok(hovered.hover.top <= 0 && hovered.hover.bottom >= 0);
    await page.locator('#chat-input').click();
    assert.ok(await page.evaluate(() => window.previewCloses > 0));
    await page.evaluate(() => { window.previewCalls = []; });
    await page.getByRole('link', {name: 'Website', exact: true}).hover();
    await page.locator('#chat-input').hover();
    await page.waitForTimeout(550);
    assert.equal(await page.evaluate(() => window.previewCalls.length), 0);
    assert.equal(await page.locator('iframe').count(), 0);
    assert.equal(await page.evaluate(() => previewLink('javascript:alert(1)', 'unsafe').nodeType), 3);
    await page.evaluate(() => {
      window.windowActions = [];
      window.pywebview.api.set_theme = async () => ({ok: true});
      window.pywebview.api.window_action = async action => {
        window.windowActions.push(action);
        return {ok: true, maximized: action === 'maximize' && !document.documentElement.hasAttribute('data-maximized')};
      };
      dispatchEvent(new Event('pywebviewready'));
    });
    await page.getByRole('button', {name: 'Maximize', exact: true}).click();
    await page.getByRole('button', {name: 'Restore', exact: true}).click();
    await page.getByRole('button', {name: 'Minimize', exact: true}).click();
    await page.getByRole('button', {name: 'Hide to tray', exact: true}).click();
    assert.deepEqual(await page.evaluate(() => window.windowActions), ['maximize', 'maximize', 'minimize', 'close']);
    for (const width of [1280, 760]) {
      await page.setViewportSize({width, height: 850});
      const brand = await page.locator('#app-brand').boundingBox();
      const controls = await page.locator('.window-controls').boundingBox();
      await page.screenshot({path: `${process.env.TEMP}/infohub-borderless-${width}.png`});
      assert.ok(brand.x >= 0 && controls.x + controls.width <= width);
      assert.ok(brand.y < 30 && brand.x + brand.width < controls.x, JSON.stringify({brand, controls}));
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
