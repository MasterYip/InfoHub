const {test} = require('node:test');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

test('units render Markdown, safe math and navigable citations on desktop and mobile', async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({locale: 'en-US', viewport: {width: 1280, height: 900}});
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    const summary = String.raw`## Research notes

**Bold** and *italic*, ~~old~~, [[u:42]] and [Linked topic](?unit=42).

- Parent
  - Nested item
- [x] Done
- [ ] Pending

| Method | Formula |
| :--- | ---: |
| PPO | $x_i^2$ |

Inline $a_i + b^2$ and \(\frac{1}{2}\).

$$
\begin{aligned}Q(s,a) &= r + \gamma V(s') \\
V(s) &= \sum_i p_i v_i\end{aligned}
$$

\[\frac{a}{b}\]

> Quote **with** [[u:42]].

Escaped \$5 and $10 or $20.

<img src=x onerror="window.injected=true">

[unsafe](javascript:alert%281%29)

$\href{javascript:alert(1)}{unsafe}$

$\unsupportedCommand{x}$
` + '\n`[[u:42]] $x_i$`\n\n```text\n[[u:42]] $x_i$\n```';
    const unit = {id: 7, title: 'Notes $x_i$ [[u:42]]', summary, kind: 'topic', status: 'open', tags: []};
    await page.route('**/api/**', route => {
      const path = new URL(route.request().url()).pathname;
      const data = path === '/api/today' ? {todo:{overdue:[],upcoming:[],undated:[unit]},schedule:{now:[],upcoming:[],tbd:[],past:[]},recent:{days:7,units:[],units_total:0,messages:[],messages_total:0}}
        : path === '/api/unit/42' ? {...unit, id: 42, title: 'Referenced topic', summary: '**Detail** [[u:43]]'}
        : path === '/api/unit/43' ? {...unit, id: 43, title: 'Second reference', summary: 'Next detail'}
        : path === '/api/unit/99999' ? {error: 'missing'} : [];
      return route.fulfill({json: data});
    });
    await page.goto(process.env.UNIT_CONTENT_URL || 'http://127.0.0.1:8765/?tab=today');
    const content = page.locator('#main .sum').first();
    await content.locator('math').first().waitFor();
    const urls = await page.evaluate(() => {
      const samples = [
        'https://github.com/frenzymath/Danus\uff0c\u5e76\u6307\u51fa\u8be5\u4ed3\u5e93',
        'https://example.com/a\u3002\u540e\u6587 https://example.org/b\uff1b\u8bf4\u660e',
        '[Chinese path](https://example.com/\u4e2d\u6587\uff0c\u8def\u5f84)',
        'https://example.com/\u4e2d\u6587?q=hello&n=1#part\uff0c\u8bf4\u660e',
        'https://en.wikipedia.org/wiki/Function_(mathematics)',
        '`https://example.com/a\uff0c\u8bf4\u660e`',
        'www.example.com/a\uff0c\u8bf4\u660e'
      ];
      return samples.map(text => {
        const node = renderMarkdown(text);
        return {links: [...node.querySelectorAll('a')].map(a => decodeURI(a.href)), text: node.textContent};
      });
    });
    assert.deepEqual(urls.map(r => r.links), [
      ['https://github.com/frenzymath/Danus'],
      ['https://example.com/a', 'https://example.org/b'],
      ['https://example.com/\u4e2d\u6587\uff0c\u8def\u5f84'],
      ['https://example.com/\u4e2d\u6587?q=hello&n=1#part'],
      ['https://en.wikipedia.org/wiki/Function_(mathematics)'], [], ['http://www.example.com/a']
    ]);
    assert.ok(urls[0].text.endsWith('\uff0c\u5e76\u6307\u51fa\u8be5\u4ed3\u5e93'));
    assert.equal(await content.locator('h4').textContent(), 'Research notes');
    assert.equal(await content.locator('ul ul li').textContent(), 'Nested item');
    assert.equal(await content.locator('input:checked').count(), 1);
    assert.equal(await content.locator('table math').count(), 1);
    assert.equal(await content.locator('.md-math.display math').count(), 2);
    assert.equal(await content.locator('math msub').count() > 0, true);
    assert.equal(await content.locator('code .ref, code math, img, script, a[href^="javascript:"]').count(), 0);
    assert.equal(await content.locator('.math-error').count(), 1);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.ok((await content.textContent()).includes('Escaped $5 and $10 or $20.'));
    await content.getByRole('button', {name: 'unit 42'}).first().click();
    await page.locator('.float-card .title').waitFor();
    assert.equal(await page.locator('.float-card .title').textContent(), 'Referenced topic');
    await page.locator('.float-card').getByRole('button', {name: 'unit 43'}).click();
    await page.waitForFunction(() => document.querySelector('.float-card .title')?.textContent === 'Second reference');
    assert.equal(await page.locator('.float-card').count(), 1);
    await page.keyboard.press('Escape');
    await content.getByRole('link', {name: 'Linked topic'}).click();
    await page.locator('.float-card').waitFor();
    await page.keyboard.press('Escape');
    await page.screenshot({path: process.env.TEMP + '/infohub-unit-content-desktop.png'});
    await page.setViewportSize({width: 390, height: 844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({path: process.env.TEMP + '/infohub-unit-content-mobile.png'});
    await content.getByRole('button', {name: 'unit 42'}).first().focus();
    await page.keyboard.press('Enter');
    await page.locator('.float-card').waitFor();
    const bounds = await page.locator('.float-card').boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 391);
    await page.evaluate(() => openUnit(99999));
    await page.locator('.float-card [role=status]').waitFor();
    assert.match(await page.locator('.float-card').textContent(), /not available/);
    await page.keyboard.press('Escape');
    // The same renderer is used by assistant responses.
    await page.evaluate(() => {
      document.getElementById('main').replaceChildren(renderMarkdown('**Chat** [[u:42]] $x_i$'));
    });
    assert.equal(await page.locator('#main math').count(), 1);
    await page.getByRole('button', {name: 'unit 42'}).click();
    await page.locator('.float-card').waitFor();
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
