const {test} = require('node:test');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

test('unit menu edits details, preserves drafts and supports compact viewports', async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  try {
    const page = await browser.newPage({locale: 'en-US', viewport:{width:1280,height:900}});
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    let u = {id:7,title:'Discuss the project',summary:'Original details',kind:'todo',status:'open',priority:'P2',
      tags:['project:demo','type:todo','source:demo'],created_at:1700000000,start_at:1700000000,end_at:1700003600};
    const writes = [];
    await page.route('**/api/**',async route => {
      const path = new URL(route.request().url()).pathname;
      let data = [];
      if (path === '/api/health') data={ok:true,unit_edit:1};
      if (path === '/api/today') data = {todo:{overdue:[],upcoming:[],undated:[u]},schedule:{now:[],upcoming:[],tbd:[],past:[]},recent:{days:7,units:[],units_total:0,messages:[],messages_total:0}};
      if (path === '/api/unit/7') {
        if (route.request().method() === 'POST') { const b=route.request().postDataJSON(); writes.push(b); u={...u,...b}; data={ok:true}; }
        else data = {...u,parents:[],children:[],messages:[]};
      }
      if (path === '/api/units') data=[u];
      if (path === '/api/graph') data={nodes:[{...u,n_messages:4,seed:true}],edges:[]};
      await route.fulfill({json:data});
    });
    await page.goto('http://127.0.0.1:8765/?tab=today');
    await page.locator('.it').first().click({button:'right'});
    await page.getByRole('menuitem',{name:'Edit',exact:true}).click();
    const dialog=page.getByRole('dialog');
    await dialog.getByLabel('Title',{exact:true}).fill('Updated title');
    await dialog.getByLabel('Summary',{exact:true}).fill('');
    await dialog.getByLabel('Kind',{exact:true}).selectOption('event');
    await dialog.getByLabel('Due',{exact:true}).fill('2026-11-05T09:30');
    await dialog.getByLabel('Tags',{exact:true}).fill('project:new\nimportance:high');
    await page.evaluate(() => render());
    assert.equal(await dialog.getByLabel('Title',{exact:true}).inputValue(),'Updated title');
    await page.screenshot({path:process.env.TEMP+'/infohub-unit-edit-desktop.png'});
    await dialog.getByRole('button',{name:'Save',exact:true}).click();
    await dialog.waitFor({state:'detached'});
    assert.equal(writes[0].summary,'');
    assert.equal(writes[0].kind,'event');
    assert.deepEqual(writes[0].remove_tags,['project:demo']);
    assert.ok(!('start_at' in writes[0]));
    await page.locator('.it').first().click({button:'right'});
    await page.getByRole('menuitem',{name:'Mark done'}).click();
    await page.waitForFunction(() => document.querySelector('.it.done'));
    assert.equal(u.status,'done');
    await page.locator('.it').first().click({button:'right'});
    await page.getByRole('menuitem',{name:'Reopen'}).click();
    await page.waitForFunction(() => !document.querySelector('.it.done'));
    await page.setViewportSize({width:390,height:844});
    await page.getByRole('button',{name:'Edit',exact:true}).first().click();
    await dialog.getByLabel('Title',{exact:true}).fill('Unsaved draft');
    await page.screenshot({path:process.env.TEMP+'/infohub-unit-edit-mobile.png'});
    assert.ok(await dialog.evaluate(e => e.scrollWidth <= e.clientWidth));
    await dialog.getByRole('button',{name:'Cancel'}).click();
    assert.equal(u.title,'Updated title');
    await page.setViewportSize({width:1280,height:900});
    await page.goto('http://127.0.0.1:8765/?unit=7');
    await page.locator('.float-card').waitFor();
    await page.keyboard.press('Escape');
    await page.evaluate(() => { localStorage.setItem('exploreView','gantt'); });
    await page.goto('http://127.0.0.1:8765/?tab=explore');
    await page.locator('.gantt-bar').first().click({button:'right'});
    await page.getByRole('menuitem',{name:'Edit',exact:true}).click();
    await dialog.waitFor();
    await dialog.getByRole('button',{name:'Cancel'}).click();
    await page.evaluate(() => { localStorage.setItem('exploreView','graph'); });
    await page.goto('http://127.0.0.1:8765/?tab=explore');
    await page.waitForFunction(() => GRAPH_POS[7]);
    await page.evaluate(() => {
      document.documentElement.dataset.theme='light';
      const canvas=document.querySelector('.graph-host canvas'), rect=canvas.getBoundingClientRect();
      canvas.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,
        clientX:rect.left+GRAPH_POS[7].x,clientY:rect.top+GRAPH_POS[7].y}));
    });
    await page.getByRole('menuitem',{name:'Edit',exact:true}).waitFor();
    await page.screenshot({path:process.env.TEMP+'/infohub-unit-menu-light.png'});
    await page.getByRole('menuitem',{name:'Edit',exact:true}).click();
    await dialog.waitFor();
    await page.screenshot({path:process.env.TEMP+'/infohub-unit-edit-light.png'});
    await dialog.getByRole('button',{name:'Cancel'}).click();
    await page.addInitScript(data => { window.__INFOHUB_VIEWER__={units:[data],tags:[],graph:{nodes:[data],edges:[]}}; },u);
    await page.goto('http://127.0.0.1:8765/');
    await page.evaluate(() => editUnit(7));
    assert.equal(await page.getByRole('dialog').count(),0);
    assert.deepEqual(errors,[]);
  } finally { await browser.close(); }
});
