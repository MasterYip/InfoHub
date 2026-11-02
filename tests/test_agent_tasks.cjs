// Requires a running local dashboard and Playwright.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

test('agent settings save permissions and expose session links without rendering task HTML', async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const values = {'agent_tasks.enabled': true, 'agent_tasks.workspace': '', 'agent_tasks.sandbox': 'workspace-write'};
    await page.route('**/api/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      let data = [];
      if (pathname === '/api/settings') {
        if (route.request().method() === 'POST') {
          Object.assign(values, route.request().postDataJSON());
          data = {ok: true, saved: values};
        } else data = {values, restart_needed_for: [], editable: Object.keys(values)};
      }
      if (pathname === '/api/agent-tasks') data = [{id: 1, unit_id: 1,
        prompt: '<img src=x onerror=alert(1)> Test task', status: 'completed',
        thread_id: '01a1907e-c8cf-7501-b81a-1719ce4883e2', result: 'INFOHUB_OK'}];
      if (pathname === '/api/backups') data = {enabled: false, running: false, backups: []};
      await route.fulfill({json: data});
    });
    await page.goto('http://127.0.0.1:8765/?tab=settings');
    await page.locator('#agent-permissions').selectOption('danger-full-access');
    await page.waitForFunction(() => document.getElementById('status').textContent.includes('saved agent_tasks.sandbox'));
    assert.equal(values['agent_tasks.sandbox'], 'danger-full-access');
    assert.equal(await page.getByRole('link', {name: 'Open in Codex'}).getAttribute('href'),
      'codex://threads/01a1907e-c8cf-7501-b81a-1719ce4883e2');
    assert.equal(await page.locator('img[src="x"]').count(), 0);
    await page.getByText('Local agent tasks', {exact: true}).scrollIntoViewIfNeeded();
    await page.screenshot({path: `${process.env.TEMP}/infohub-agent-settings.png`});
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('Status tracks Codex sessions and todo status updates without replacing the card', async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1400,height:950}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let task = {id:1,unit_id:7,message_id:9,prompt:'Find HumanPlus papers',status:'pending',updated_at:1793430573};
    const overview = () => ({enabled:true,sandbox:'danger-full-access',counts:{[task.status]:1},tasks:[task],requests:[]});
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      let data = [];
      if (path === '/api/agent-tasks/overview') data = overview();
      if (path === '/api/status') data = {totals:{messages:10,messages_24h:2,units:1,segments_open:0,channels:{},sources:{}},
        health:{ok:1,waiting:0,error:0},ai:{enabled:true,provider:'demo',model:'demo',spent_today_usd:0,budget_usd:5,usage:[]},
        jobs:{},failing:[],units:{todo:1},runs:[]};
      if (path === '/api/today') data = {needs:[{id:7,kind:'todo',title:'Find HumanPlus papers',summary:'A personal task',
        status:'open',tags:[],agent_task:{...task}}],events:[],unscheduled_events:[],unsorted:[]};
      await route.fulfill({json:data});
    });
    await page.goto('http://127.0.0.1:8765/?tab=status');
    const panel = page.getByRole('region', {name:'Codex tasks'});
    await panel.getByText('Codex · Queued', {exact:true}).waitFor();
    task = {...task,status:'running',thread_id:'01a1907e-c8cf-7501-b81a-1719ce4883e2'};
    await panel.getByText('Codex · Running', {exact:true}).waitFor();
    assert.equal(await panel.getByRole('link',{name:'Open in Codex'}).getAttribute('href'), 'codex://threads/' + task.thread_id);
    await panel.scrollIntoViewIfNeeded();
    await page.screenshot({path:`${process.env.TEMP}/infohub-codex-status.png`});
    await page.goto('http://127.0.0.1:8765/?tab=today');
    const card = page.locator('.it').filter({has:page.locator('.title', {hasText:'Find HumanPlus papers'})});
    await card.getByRole('link',{name:'Session status'}).waitFor();
    await card.getByRole('button', {name:'Tag', exact:false}).click();
    const draft = card.getByPlaceholder('add tag');
    await draft.fill('project:keep-my-input');
    task = {...task,status:'completed',result:'Found three references'};
    await card.getByText('Codex · Completed', {exact:true}).waitFor();
    assert.equal(await draft.inputValue(), 'project:keep-my-input');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('compact queue controls start pause delete and open the matching session', async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1400,height:1000}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    let task={message_id:99,unit_id:7,prompt:'Find HumanPlus papers and download the PDF',status:'not_queued',
      error:'Message predates activation',updated_at:1793430573};
    const actions=[];
    await page.route('**/api/**',async route=>{
      const path=new URL(route.request().url()).pathname;
      let data=[];
      if(path==='/api/status')data={totals:{messages:10,messages_24h:2,units:1,segments_open:0,channels:{},sources:{}},
        health:{ok:1,waiting:0,error:0},ai:{enabled:true,provider:'demo',model:'demo',spent_today_usd:0,budget_usd:5,usage:[]},
        jobs:{},failing:[],units:{todo:1},runs:[]};
      if(path==='/api/agent-tasks/control'){
        const body=route.request().postDataJSON();actions.push(body);
        if(body.action==='delete')task=null;
        else task={...task,status:body.action==='pause'?'paused':'running',error:null,thread_id:'01a1907e-c8cf-7501-b81a-1719ce4883e2'};
        data={ok:true,task};
      }
      if(path==='/api/agent-tasks/overview') data={enabled:true,sandbox:'danger-full-access',
        counts:{},tasks:task&&task.status!=='not_queued'?[task]:[],requests:task?.status==='not_queued'?[task]:[]};
      await route.fulfill({json:data});
    });
    await page.goto('http://127.0.0.1:8765/?tab=status');
    const panel=page.getByRole('region',{name:'Codex tasks'});
    await panel.getByRole('button',{name:'Start task',exact:true}).click();
    await panel.getByText('Codex · Running',{exact:true}).waitFor();
    assert.equal(await panel.getByRole('link',{name:'Open in Codex'}).getAttribute('href'),'codex://threads/'+task.thread_id);
    await panel.getByRole('button',{name:'Pause task'}).click();
    await panel.getByRole('button',{name:'Resume task'}).waitFor();
    assert.ok((await panel.locator('.agent-row').boundingBox()).height < 95);
    const settingsStyle=await panel.getByRole('link',{name:'Settings',exact:true}).evaluate(n=>{
      const s=getComputedStyle(n);return {size:s.fontSize,decoration:s.textDecorationLine};
    });
    assert.deepEqual(settingsStyle,{size:'12px',decoration:'none'});
    await panel.screenshot({path:process.env.TEMP+'/infohub-compact-agent-queue.png'});
    await panel.getByRole('button',{name:'Resume task'}).click();
    await panel.getByText('Codex · Running',{exact:true}).waitFor();
    await panel.getByRole('button',{name:'Delete and stop task'}).click();
    await panel.getByText('No Codex tasks',{exact:true}).waitFor();
    assert.deepEqual(actions.map(a=>a.action),['start','pause','start','delete']);
    assert.ok(actions.every(a=>a.message_id===99));
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});
