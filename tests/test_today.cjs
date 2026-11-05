// Today page and language switch, against this checkout's index.html with a mocked API.
// Run: node --test tests/test_today.cjs   (needs Playwright + Microsoft Edge)
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const WEB = path.join(__dirname, '..', 'infohub', 'apps', 'web');
const NOW = Math.floor(Date.now() / 1000), DAY = 86400;
const unit = (id, kind, title, extra = {}) => ({id, kind, title, status: 'open', tags: ['type:' + kind], for_me: 1,
  created_at: NOW - 30 * DAY, last_activity_at: NOW - 30 * DAY, ...extra});

async function openPage(browser, {lang, width = 1440, height = 1000, onToday} = {}) {
  const page = await browser.newPage({viewport: {width, height}});
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  const calls = [], writes = [];
  const state = {pinned: null};
  await page.route('http://infohub.test/**', async route => {
    const url = new URL(route.request().url()), p = url.pathname;
    if (p === '/' || p === '/index.html') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(WEB, 'index.html'))});
    if (p.startsWith('/vendor/')) return route.fulfill({contentType: 'text/javascript', body: fs.readFileSync(path.join(WEB, p))});
    let data = [];
    if (p === '/api/health') data = {ok: true, unit_edit: 1};
    if (p === '/api/views') data = [{name: 'Homework', tags: ['type:homework']}, {name: 'Reading', name_zh: '阅读', tags: ['topic:reading']}];
    if (p === '/api/today') {
      calls.push(Object.fromEntries(url.searchParams));
      const recentUnits = Array.from({length: 75}, (_, i) => unit(1000 + i, 'topic', 'Topic ' + i, {last_activity_at: NOW - i * 60}));
      const days = Number(url.searchParams.get('days')), limit = Number(url.searchParams.get('limit'));
      const old = unit(826, 'question', 'Old open question', {for_me_pinned: state.pinned});
      data = {scope: url.searchParams.get('scope') || 'mine', now: NOW,
        todo: {overdue: [unit(1, 'todo', 'Overdue report', {due_at: NOW - DAY})], upcoming: [unit(2, 'todo', 'Due soon', {due_at: NOW + DAY})],
               undated: [old]},
        schedule: {now: [unit(3, 'event', 'Workshop in progress', {start_at: NOW - 3600, end_at: NOW + 3600})],
                   upcoming: [unit(4, 'event', 'Group meeting', {start_at: NOW + 2 * DAY}), unit(5, 'event', 'Far conference', {start_at: NOW + 40 * DAY})],
                   tbd: [], past: []},
        types: {todo: {todo: 2, question: 1, homework: 1}, schedule: {event: 3}, recent: {topic: 75}},
        recent: {days, units: recentUnits.slice(0, limit), units_total: days === 7 ? 75 : 90, messages: [], messages_total: 0}};
      onToday?.(data);
    }
    if (p === '/api/unit/826' && route.request().method() === 'POST') {
      const body = route.request().postDataJSON(); writes.push(body);
      if ('for_me' in body) state.pinned = body.for_me;
      data = {ok: true, id: 826};
    }
    if (p === '/api/tags') data = [];
    if (p === '/api/units') data = [];
    if (p === '/api/summary') data = {hours: 72, needs_you: [], digest: [{source: 'lark', account: '', channel: 'g1',
      channel_name: 'Lab group', is_group: 1, policy: 'watch', n: 12, important: 2, units: [], highlights: []}], counts: [], ignored: []};
    if (p === '/api/live') data = {segments: [{id: 1, source: 'lark', account: '', channel: 'g1', channel_name: 'Lab group',
      n: 3, start_at: NOW - 600, end_at: NOW - 60, messages: [{id: 9, sender_name: 'A', sent_at: NOW - 60, text: 'hello'}]}]};
    if (p === '/api/status') data = {totals: {messages: 10, messages_24h: 2, units: 1, segments_open: 0, channels: {watch: 1}, sources: {}},
      health: {ok: 1, waiting: 0, error: 0}, ai: {enabled: false, usage: []}, jobs: {done: 3}, failing: [], units: {todo: 1}, runs: []};
    if (p === '/api/settings') data = {values: {'me.names': [], 'apps.today_scope': 'mine'}, restart_needed_for: [], api_key_configured: false};
    if (p === '/api/backups') data = {enabled: false, copies: 0, directory: 'data/backups'};
    if (p === '/api/people') data = {people: [], vips: [], watchers: []};
    if (p === '/api/agent-tasks/overview') data = {enabled: false, sandbox: 'read-only', counts: {}, tasks: [], requests: []};
    return route.fulfill({json: data});
  });
  await page.addInitScript(lang => { localStorage.clear(); if (lang) localStorage.setItem('lang', lang); }, lang);
  await page.goto('http://infohub.test/');
  return {page, errors, calls, writes};
}

test('Today: three lanes, nothing ages out, paging and the activity window', async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const {page, errors, calls} = await openPage(browser, {lang: 'en'});
    const lanes = page.locator('#main .lane');
    await lanes.first().getByText('Old open question').waitFor();
    assert.deepEqual(await lanes.locator('h2').allTextContents(), ['To do3', 'Schedule3', 'Recent75']);
    assert.deepEqual(await lanes.nth(0).locator('summary').allTextContents(), ['Overdue1', 'Within 7 days1', 'No due date1']);
    // Every future date has a range; far events get their own open group instead of a 7-day cut-off.
    assert.deepEqual(await lanes.nth(1).locator('summary').allTextContents(), ['Happening now1', 'Within 7 days1', 'Future1']);
    assert.equal(await lanes.nth(1).locator('details', {hasText: 'Future'}).evaluate(d => d.open), true);
    // Both task lanes link to what was completed recently.
    assert.equal(await lanes.nth(1).getByRole('button', {name: 'Completed in the last 30 days →'}).count(), 1);
    // Recent is one flat list: no fold around the units.
    assert.equal(await lanes.nth(2).locator('details').count(), 0);
    assert.equal(await lanes.nth(2).locator('.it').count(), 30);
    // Old Todos / Calendar tabs are folded into Today.
    assert.deepEqual(await page.locator('#tabs button').allTextContents(), ['Today', 'Explore', 'Groups', 'Live', 'Homework', 'Reading']);
    // Assistant, Status and Settings are icon buttons on the right, named for screen readers and tooltips.
    assert.deepEqual(await page.locator('#navIcons button').evaluateAll(bs => bs.map(b => [b.title, b.getAttribute('aria-label')])),
      [['Chat', 'Chat'], ['Status', 'Status'], ['Settings', 'Settings']]);
    // Show more pages through the full total instead of silently stopping.
    await page.getByText('Showing 30 of 75').waitFor();
    await page.getByRole('button', {name: 'Show 30 more'}).click();
    await page.getByText('Showing 60 of 75').waitFor();
    // The window only changes the Recent lane and is remembered.
    await page.getByRole('button', {name: '30 days', exact: true}).click();
    await page.getByText('Showing 30 of 90').waitFor();
    assert.equal(calls.at(-1).days, '30');
    assert.equal(await page.evaluate(() => localStorage.getItem('recentDays')), '30');
    // No explicit choice yet: the server's configured default decides (no scope param sent).
    assert.equal(calls[0].scope, undefined);
    await page.locator('#scopeToggle .chip', {hasText: 'Everything'}).click();
    await page.waitForFunction(() => localStorage.getItem('scope') === 'all');
    assert.equal(calls.at(-1).scope, 'all');
    // The scope switch is hidden where it filters nothing.
    await page.getByRole('button', {name: 'Chat', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('#navIcons [data-tab=chat]').classList.contains('on'));
    assert.equal(await page.locator('#navIcons [data-tab=chat]').getAttribute('aria-current'), 'page');
    assert.equal(await page.locator('#tabs .tab.on').count(), 0);
    assert.equal(await page.locator('#scopeToggle').isVisible(), false);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('Type chips filter each lane, All toggles everything, choice is remembered', async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const {page, errors, calls} = await openPage(browser, {lang: 'en'});
    const chips = page.locator('#main .lane').first().locator('.type-chips button');
    await chips.first().waitFor();
    assert.deepEqual(await chips.allTextContents(), ['All', 'todo 2', 'homework 1', 'question 1']);
    assert.equal(await page.locator('#main .lane .type-chips button.on').count(), (1 + 3) + (1 + 1) + (1 + 1));   // all on by default, in all three lanes
    await chips.nth(1).click();                                   // hide "todo" in To do only
    await page.waitForFunction(() => JSON.parse(localStorage.getItem('todayHide') || '{}').todo?.includes('todo'));
    assert.equal(calls.at(-1).hide_todo, 'todo');
    assert.equal(calls.at(-1).hide_schedule, undefined);
    // All turns everything back on; clicking All again hides every type in that lane.
    await chips.first().click();
    await page.waitForFunction(() => !JSON.parse(localStorage.getItem('todayHide')).todo.length);
    assert.equal(calls.at(-1).hide_todo, undefined);
    await chips.first().click();
    await page.waitForFunction(() => JSON.parse(localStorage.getItem('todayHide')).todo.length === 3);
    assert.equal(calls.at(-1).hide_todo.split(',').sort().join(), 'homework,question,todo');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('Mark as for me / Not for me from the unit menu', async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const {page, errors, writes} = await openPage(browser, {lang: 'en'});
    const card = page.locator('.it', {hasText: 'Old open question'});
    await card.click({button: 'right'});
    await page.getByRole('menuitem', {name: 'Not for me'}).click();
    await page.waitForFunction(() => !document.querySelector('.unit-menu'));
    assert.deepEqual(writes.at(-1), {for_me: false});
    await card.click({button: 'right'});
    await page.getByRole('menuitem', {name: 'Detect "for me" automatically'}).click();
    assert.deepEqual(writes.at(-1), {for_me: null});
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('Chinese / English switch translates the shell and the page, and is remembered', async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const {page, errors} = await openPage(browser, {lang: 'zh'});
    await page.getByText('无截止日期').waitFor();
    assert.equal(await page.locator('html').getAttribute('lang'), 'zh-CN');
    // Saved views: built-in Chinese name, or name_zh from config.
    assert.deepEqual(await page.locator('#tabs button').allTextContents(), ['今日', '探索', '群聊', '进行中', '作业', '阅读']);
    assert.deepEqual(await page.locator('#navIcons button').evaluateAll(bs => bs.map(b => b.title)), ['助手', '状态', '设置']);
    assert.equal(await page.locator('#langSwitch .on').textContent(), '中');
    assert.deepEqual(await page.locator('#main .lane h2').allTextContents(), ['待办3', '日程3', '近期动态75']);
    assert.equal(await page.locator('#poll').textContent(), '立即同步');
    assert.equal(await page.locator('#q').getAttribute('placeholder'), '搜索（回车）');
    assert.match(await page.locator('#main .lane').first().locator('.src').first().textContent(), /待办/);
    await page.screenshot({path: path.join(require('node:os').tmpdir(), 'infohub-today-zh.png')});
    await page.locator('#langSwitch [data-lang=en]').click();
    await page.getByText('No due date').waitFor();
    assert.equal(await page.locator('#langSwitch .on').textContent(), 'EN');
    assert.equal(await page.locator('#tabs button').nth(4).textContent(), 'Homework');
    assert.equal(await page.evaluate(() => localStorage.getItem('lang')), 'en');
    // Every remaining tab renders in Chinese without errors.
    await page.locator('#langSwitch [data-lang=zh]').click();
    for (const name of ['探索', '群聊', '进行中', '作业']) {
      await page.getByRole('button', {name, exact: true}).click();
      await page.waitForFunction(n => document.querySelector('#tabs .tab.on')?.textContent === n, name);
    }
    assert.equal(await page.locator('#main .lane h2').first().textContent(), '作业0');
    for (const tab of ['chat', 'status', 'settings']) {
      await page.locator(`#navIcons [data-tab=${tab}]`).click();
      await page.waitForFunction(t => document.querySelector('#main').dataset.tab === t, tab);
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('Today lanes stack on a phone', async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const {page, errors} = await openPage(browser, {lang: 'zh', width: 390, height: 844});
    await page.getByText('无截止日期').waitFor();
    const boxes = await page.locator('#main .lane').evaluateAll(ls => ls.map(l => l.getBoundingClientRect()));
    assert.ok(boxes[1].top > boxes[0].bottom - 1, 'lanes stack vertically');
    assert.ok(boxes.every(b => b.right <= 391), 'no horizontal overflow');
    await page.screenshot({path: path.join(require('node:os').tmpdir(), 'infohub-today-mobile.png'), fullPage: false});
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
