const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.TT_URL||'http://127.0.0.1:8792/');
  await page.click('#tab-boards');
  await page.locator('#board-project').selectOption('tasktreker');
  assert.equal(await page.locator('.board-project').count(),1);
  assert.equal(await page.locator('.board-columns').count(),1);
  await page.locator('#board-search').fill('TT-24');
  assert.equal(await page.locator('.board-task').count(),1);
  await page.locator('[data-detail]').click();
  await page.locator('dialog').waitFor({state:'visible'});
  assert.match(await page.locator('dialog').innerText(),/gpt-6.1-sol/);
  await page.locator('#board-close').click();
  await page.locator('#board-search').fill('No such task');
  assert.equal(await page.locator('.board-task').count(),0);
  await page.locator('#board-search').fill('');
  await page.locator('#board-status').selectOption('blocked');
  const expectedBlocked=await page.evaluate(async()=>{const j=await(await fetch('/api/work')).json();return j.data.tasks.filter(t=>t.project==='tasktreker'&&t.status!=='done'&&t.status!=='in_progress'&&t.status!=='review'&&(t.blocked_by.length||t.status==='blocked')).length;});
  assert.equal(await page.locator('.board-task').count(),expectedBlocked);
  await page.locator('#board-status').selectOption('');
  const cardCount=await page.locator('.board-task').count();
  await page.click('#board-list-view');
  assert.equal(await page.locator('#boards-root').getAttribute('data-view'),'list');
  assert.equal(await page.locator('.board-task').count(),cardCount);
  await page.click('#board-view');
  await page.locator('#board-plan').click();
  await page.locator('#task-project').selectOption('tasktreker');
  await page.locator('#board-save').click();
  assert.ok(await page.locator('#board-form.tried :invalid').count()>0);
  await page.locator('[name=task-type][value=dev_by_spec]').check();
  assert.equal(await page.locator('#task-spec_ref').count(),1);
  await page.locator('#task-spec_ref').fill('docs/ИСПОЛНЕНИЕ_ЗАДАЧ.md');
  await page.locator('[name=root-mode][value=existing]').check();
  await page.locator('#task-id').fill('TT-24');
  await page.locator('#task-title').fill('Проверка запрета дубликата');
  await page.locator('#task-why').fill('Браузерный тест');
  await page.locator('#task-root').selectOption('mem-doski-proektov-zapusk-zada-3ca14a');
  await page.locator('#task-acceptance').fill('Дубликат должен быть отклонён');
  await page.locator('#board-save').click();
  await page.waitForFunction(()=>/Повтор/.test(document.getElementById('board-plan-error').textContent));
  assert.match(await page.locator('#board-plan-error').innerText(),/Повтор/);
  await page.locator('#board-close').click();
  await page.click('#focus-start');
  await page.click('#focus-pause');
  const paused=await page.locator('#focus-clock').innerText();
  await page.reload();
  assert.equal(await page.locator('#focus-clock').innerText(),paused);
  assert.equal(await page.locator('#focus-pause').innerText(),'Продолжить');
  await page.click('#focus-add');
  assert.ok((await page.locator('#focus-clock').innerText()).startsWith('29:')||(await page.locator('#focus-clock').innerText()).startsWith('30:'));
  await page.click('#focus-stop');
  assert.equal(await page.locator('#focus-clock').innerText(),'25:00');
  await page.locator('#board-project').selectOption('tasktreker');
  await page.click('#board-appearance');
  await page.screenshot({path:'ops/logs/boards.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'ops/logs/boards-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);
  await checkPolling(browser);
  console.log('Boards smoke passed: filters, project boards, passport, plan validation, desktop/mobile, no JS errors, polling and lease warning');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

// Автообновление досок (TT-43): /api/work опрашивается, пока вкладка видна;
// при открытом диалоге опроса нет и перерисовки нет; аренда за 5 минут подсвечивается.
// /api/work и страница подменены, поэтому сервер не нужен.
async function checkPolling(browser){
 const base=process.env.TT_URL||'http://127.0.0.1:8792/';
 const html=fs.readFileSync(path.join(__dirname,'..','dashboard.html'),'utf8');
 const wf=JSON.parse(html.match(/<script id="DATA"[^>]*>([\s\S]*?)<\/script>/)[1]).workflow;
 const soonUntil=new Date(Date.now()+3*60*1000).toISOString();
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 let calls=0;
 page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.route('**/api/work',async route=>{
   calls++;
   const copy=JSON.parse(JSON.stringify(wf));
   const t=copy.tasks.find(x=>x.project==='tasktreker'&&x.status!=='done'&&x.status!=='cancelled')||copy.tasks.find(x=>x.project==='tasktreker');
   t.claim={agent:'claude',worker:'claude-cli:TEST',run:'RUN-TEST',model:'claude-haiku-5-5',lease_until:soonUntil,stale:false};
   copy.runs.push({id:'RUN-TEST',ref:t.ref,status:'running',agent:'claude',worker:'claude-cli:TEST',model:'claude-haiku-5-5',claimed_at:new Date().toISOString(),heartbeat_at:new Date().toISOString(),lease_until:soonUntil});
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,data:copy})});
  });
  await page.route(base,route=>route.fulfill({status:200,contentType:'text/html',body:html}));
  await page.goto(base);
  await page.click('#tab-boards');
  await page.locator('#board-project').selectOption('tasktreker');
  await page.waitForFunction(()=>document.querySelector('.board-claim-soon'),null,{timeout:15000});
  assert.ok(calls>=1,'board must read /api/work');

  const beforePoll=calls;
  await page.waitForTimeout(21000);
  assert.ok(calls>beforePoll,'poll must fetch /api/work while the tab is visible');

  await page.locator('[data-detail]').first().click();
  await page.locator('dialog').waitFor({state:'visible'});
  await page.evaluate(()=>{document.querySelector('dialog').dataset.mark='kept';});
  const beforeDialog=calls;
  await page.waitForTimeout(21000);
  assert.equal(calls,beforeDialog,'no /api/work fetch while the dialog is open');
  assert.equal(await page.locator('dialog').getAttribute('data-mark'),'kept','dialog must not be re-rendered');
  assert.equal(await page.locator('dialog').evaluate(d=>d.open),true,'dialog stays open');

  await page.locator('#board-close').click();
  const afterClose=calls;
  await page.waitForTimeout(21000);
  assert.ok(calls>afterClose,'polling resumes after the dialog closes');
  assert.deepEqual(errors,[]);
 }finally{await page.close();}
}
