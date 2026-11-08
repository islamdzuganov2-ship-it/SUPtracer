const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1100}});
 const errors=[]; page.on('pageerror',e=>errors.push(e.message));
 const html=fs.readFileSync(path.join(__dirname,'..','dashboard.html'),'utf8');
 const data=JSON.parse(html.match(/<script id="DATA"[^>]*>([\s\S]*?)<\/script>/)[1]).workflow;
 let calls=0, offline=false;
 const base='http://127.0.0.1:8799/';
 try{
  await page.clock.install();
  await page.route(base,route=>route.fulfill({contentType:'text/html',body:html}));
  await page.route('**/api/work',async route=>{
   assert.equal(route.request().method(),'GET','view must not mutate state'); calls++;
   if(offline)return route.abort();
   const at=new Date().toISOString();
   data.activity={observed_at:at,fresh_seconds:180,queued:{ege:8},max_sessions:2,capacities:{codex:2,claude:0},workloads:[
    {project:'ege',agent:'codex',model:'gpt-6.1-sol',busy:1,signals:1,queued:8,paused:0,tasks:9},
    {project:'ege',agent:'claude',model:'claude-sonnet-5-5',busy:0,signals:0,queued:0,paused:1,tasks:1},
    {project:'tasktreker',agent:'codex',model:'other-model',busy:1,signals:1,queued:0,paused:0,tasks:1}],rows:[
    {run:'LIVE',ref:'ege/EGE-20',project:'ege',title:'Разбор деплоя',agent:'codex',role:'resolver',model:'gpt-6.1-sol',state:'recent',label:'Есть свежие сигналы',process_alive:true,pid:123,log_at:at},
    {run:'QUIET',ref:'ege/EGE-46',project:'ege',title:'Деплой',agent:'codex',role:'devops',state:'quiet',label:'Процесс жив; свежих сигналов нет',process_alive:true,pid:456},
    {run:'OLD',ref:'ege/EGE-08',project:'ege',title:'Старый запуск <script>',agent:'claude',role:'solution_architect',state:'unknown',label:'Активность не подтверждена',process_alive:null},
    {run:'OTHER',ref:'tasktreker/TT-100',project:'tasktreker',state:'paused',label:'Пауза',agent:'codex'}]};
   await route.fulfill({contentType:'application/json',body:JSON.stringify({ok:true,data})});
  });
  await page.goto(base); await page.click('#tab-boards');
  await page.locator('#board-project').selectOption('ege');
  await page.locator('[data-activity-run="LIVE"]').waitFor();
  assert.equal(await page.locator('.board-activity-card').count(),3);
  assert.equal(await page.locator('[data-state="recent"]').count(),1);
  assert.equal(await page.locator('.model-donut').count(),2);
  assert.match(await page.locator('.model-donut').first().getAttribute('aria-label'),/50%.*1 из 2/);
  assert.match(await page.locator('.model-donut').last().getAttribute('aria-label'),/—.*0 из 0/);
  assert.match(await page.locator('#board-activity').innerText(),/Активность не подтверждена/);
  assert.equal(await page.locator('#board-activity script').count(),0);
  await page.locator('#board-search').fill('EGE-20');
  const before=calls; await page.clock.fastForward(21000);
  await page.waitForFunction(n=>document.querySelector('#board-search').value==='EGE-20',before);
  assert.ok(calls>before);
  assert.equal(await page.locator('#board-search').inputValue(),'EGE-20');
  await page.locator('#board-search').blur();
  await page.locator('[data-detail]').first().click();
  await page.locator('dialog').evaluate(d=>d.dataset.kept='yes');
  await page.clock.fastForward(21000);
  assert.equal(await page.locator('dialog').getAttribute('data-kept'),'yes');
  await page.locator('#board-close').click();
  await page.locator('#board-activity').screenshot({path:'ops/logs/activity-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('#board-activity').evaluate(el=>el.scrollWidth<=el.clientWidth+1),true);
  await page.locator('#board-activity').screenshot({path:'ops/logs/activity-mobile.png'});
  offline=true; await page.clock.fastForward(21000);
  await page.locator('.activity-stale').waitFor();
  assert.match(await page.locator('.activity-observed[role="status"]').innerText(),/Данные устарели/);
  assert.deepEqual(errors,[]);
  console.log('Activity UI passed: evidence, filtering, escaping, polling, preserved input/dialog, offline, desktop/mobile.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
