// Run with Playwright available in NODE_PATH; checks the real local dashboard.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({channel: process.env.TT_BROWSER || 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.stack || e.message));
    await page.goto(process.env.TT_URL || 'http://127.0.0.1:8790/');
    await page.locator('#kpis .kpi').first().waitFor();
    assert.equal(await page.locator('#kpis .kpi').count(), 6);
    await page.selectOption('#data-source', 'codex');
    await page.locator('#source-info').getByText('GPT / Codex', {exact: true}).waitFor();
    assert.equal(await page.locator('#source-info .source-card').count(), 1);
    await page.reload();
    assert.equal(await page.locator('#data-source').inputValue(), 'codex');
    await page.selectOption('#data-source', 'all');
    assert.equal(await page.locator('#source-info .source-card').count(), 2);
    const readiness = await page.evaluate(() => {
      const data=JSON.parse(document.getElementById('DATA').textContent);
      return Object.entries(data.projects.projects).filter(([,p])=>p.readiness?.review)
        .map(([key,p])=>({key,readiness:p.readiness}));
    });
    for(const {key,readiness:rd} of readiness) {
      for(const dim of ['functional','production','docs','tests','security']) {
        const cell=page.locator('#c-ready rect');
        const rendered=await cell.evaluateAll((els,arg)=>els.find(e=>e.dataset.row===arg.key&&e.dataset.col===arg.dim)?.getAttribute('aria-label'),{key,dim});
        assert.ok(rendered?.endsWith(': '+rd[dim]),'numeric matrix cell '+key+'/'+dim);
      }
    }
    fs.mkdirSync(path.join(__dirname, '../ops/logs'), {recursive: true});
    await page.screenshot({path: path.join(__dirname, '../ops/logs/dashboard.png')});
    await page.click('#tab-tree');
    await page.locator('.f-intro').waitFor({state: 'detached', timeout: 20000});
    await page.waitForFunction(() => TREEVIEW.debug()?.staticMs > 0);
    await page.waitForFunction(() => !TREEVIEW.debug()?.growing);
    const search = page.getByRole('combobox', {name: 'Поиск по дереву'});
    await search.waitFor();
    const imported = await page.evaluate(() => JSON.parse(document.getElementById('DATA').textContent).codex_sessions);
    if (imported?.requests) {
      await search.fill('GPT / Codex');
      await page.locator('#forest-search-results [role="option"]').first().waitFor();
      assert.ok(await page.locator('#forest-search-results [role="option"]').count());
      await search.press('ArrowDown');
      await search.press('Enter');
    }
    await search.fill('TaskTreker');
    await page.locator('#forest-search-results [role="option"]').first().waitFor();
    await search.press('ArrowDown');
    assert.equal(await search.getAttribute('aria-expanded'), 'true');
    await search.press('Enter');
    await page.screenshot({path: path.join(__dirname, '../ops/logs/tree.png')});
    const unknownKey = Object.keys(imported?.projects || {}).find(k => !imported.projects[k].registered);
    if (unknownKey) {
      await search.fill(unknownKey);
      await page.locator('#forest-search-results [role="option"]').first().waitFor();
      await search.press('ArrowDown');
      await search.press('Enter');
      await page.getByRole('button', {name: 'Карточка проекта', exact: true}).click();
      await page.locator('#page-proj').waitFor({state: 'visible'});
      assert.equal(await page.evaluate(k => document.getElementById('p-' + k)?.open, unknownKey), true);
    } else await page.getByRole('button', {name: '← Дашборд', exact: true}).click();
    await page.click('#tab-proj');
    assert.ok(await page.locator('#cards .card').count());
    for(const {key} of readiness) {
      const details=page.locator(`[id="p-${key}"]`);
      await details.evaluate(el=>{el.open=true;});
      await details.locator('.readiness-evidence').evaluate(el=>{el.open=true;});
      assert.equal(await details.locator('.readiness-axis').count(),5);
      assert.ok((await details.locator('.readiness-evidence').innerText()).includes('Открыто:'));
      await details.evaluate(el=>{el.open=false;});
    }
    if (imported?.projects) {
      const unknown = Object.values(imported.projects).filter(p => !p.registered);
      assert.ok(await page.locator('#discoveredWrap .card').count() >= unknown.length);
    }
    const codexProjects = await page.evaluate(() => {
      const data = JSON.parse(document.getElementById('DATA').textContent);
      return Object.entries(data.projects.projects).filter(([k,p]) => p.source_providers?.includes('codex'))
        .map(([k,p]) => ({key:k, backlog:p.backlog?.length || 0}));
    });
    for (const project of codexProjects) {
      const card = page.locator('details.card').filter({has: page.locator('h2')}).filter({has: page.locator('.provider-activity')});
      assert.ok(await card.count());
      const details = page.locator(`[id="p-${project.key}"]`);
      await details.evaluate(el => {el.open = true;});
      assert.ok((await details.innerText()).toLowerCase().includes('что сделано'));
      assert.ok((await details.innerText()).toLowerCase().includes('бэклог и оценка затрат'));
      assert.equal(await details.locator('table.bl tbody tr').count(), project.backlog);
      assert.ok((await details.locator('.provider-activity').innerText()).includes('GPT / Codex'));
      await details.evaluate(el => {el.open = false;});
    }
    const family = await page.evaluate(() => {
      const data = JSON.parse(document.getElementById('DATA').textContent);
      const keys=['asok-is','asok-is_site','asok-is_Qatar'];
      if (!keys.every(k => data.projects.projects[k])) return null;
      return keys.map(k => ({key:k, node:data.tree.root.children.find(n => n.id==='p:'+k),
        backlog:data.projects.projects[k].backlog.length}));
    });
    if (family) {
      assert.ok(family.every(p => p.node), 'three independent portfolio branches');
      assert.equal(new Set(family.map(p => p.node.label)).size, 3);
      for (const p of family) {
        const wrong = node => node.project && node.project !== p.key || (node.children || []).some(wrong);
        assert.equal(!!wrong(p.node), false, 'no other project nodes inside '+p.key);
      }
      const core=page.locator('[id="p-asok-is"]'); await core.evaluate(el => {el.open=true;});
      await core.locator('button.related-project[data-project="asok-is_Qatar"]').click();
      const qatar=page.locator('[id="p-asok-is_Qatar"]');
      assert.equal(await qatar.evaluate(el => el.open),true);
      assert.equal(await qatar.locator('table.bl tbody tr').count(),family[2].backlog);
    }
    await page.setViewportSize({width: 390, height: 844});
    await page.click('#tab-dash');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    assert.equal(overflow, false, 'dashboard must fit a narrow viewport');
    assert.deepEqual(errors, [], 'no browser runtime errors');
    console.log('Browser smoke passed: source selection/persistence, charts, tree search, project cards, mobile layout.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
