/* Вкладка «Архитектура систем» (TT-ARCH-001 §7A, TT-136).
   Данные — только с сервера досок через /api/arch/* (сессия, флаг, права arch_acl), в dashboard.html их нет:
   топология — чувствительные сведения. Неизвестное показывается как неизвестное, разрыв — с причиной.
   Состояние вкладки живёт в адресе: #arch?view=…&repo=…&node=…&edge=…&process=… — ссылка переживает
   перезагрузку и открывается в новой вкладке. */
window.ARCHVIEW = (function () {
  const VIEWS = [['overview', 'Обзор'], ['ports', 'Порты и контейнеры'], ['internal', 'Внутренняя архитектура'],
                 ['processes', 'Бизнес-процессы'], ['relation', 'Связь']];
  const KIND = {module: 'модуль', package: 'пакет', class: 'класс', function: 'функция', route: 'маршрут HTTP',
                cli_command: 'команда CLI', job: 'задание', external: 'внешний модуль'};
  const ETYPE = {imports: 'импорт', calls_static: 'вызов', handles_route: 'обрабатывает маршрут',
                 handles_command: 'обрабатывает команду', contains: 'содержит', defines: 'определяет',
                 inherits: 'наследует', schedules_job: 'запускает задание'};
  const SOCK = {listening_observed: 'слушает (наблюдалось)', bound_observed: 'занят (наблюдалось)',
                declared_only: 'только объявлен', not_observed: 'не наблюдался', unknown: 'неизвестно'};
  const SRC = {available: 'ok', partial: 'warn', unavailable: 'bad', refused: 'bad', not_installed: 'idle'};
  const SEEDS = {tasktreker_pipeline: 'Конвейер задачи TaskTreker (docs/КОНВЕЙЕР.md)'};
  const st = {view: 'overview', repo: '', node: '', edge: '', process: '', q: '', tests: false, loaded: false};
  const isTest = n => /^tests?\//.test((n && n.file) || '');
  const cache = {};
  let root = null, poll = null;

  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const chip = (text, cls) => `<span class="chip ${cls || ''}">${esc(text)}</span>`;
  const when = iso => { if (!iso) return 'неизвестно'; const d = new Date(typeof iso === 'number' ? iso * 1000 : iso); return isNaN(d) ? esc(iso) : d.toLocaleString('ru-RU'); };
  const served = () => location.protocol === 'http:' || location.protocol === 'https:';
  const short = id => esc(String(id || '').replace(/^([a-z_]+)-([0-9a-f]{6})[0-9a-f]*$/, '$1-$2…'));

  function headers() { return window.ttOwnerHeaders ? window.ttOwnerHeaders() : {'X-TaskTreker': '1'}; }
  async function api(path, body) {
    const opt = body === undefined ? {headers: headers()} : {method: 'POST', headers: headers(), body: JSON.stringify(body)};
    let r, j;
    try { r = await fetch(path, opt); j = await r.json(); }
    catch (e) { return {ok: false, status: 0, error: 'сервер досок не ответил'}; }
    return Object.assign({status: r.status}, j);
  }

  /* ---------- адрес ---------- */
  function readHash(q) {
    const p = new URLSearchParams(q || '');
    st.view = VIEWS.some(v => v[0] === p.get('view')) ? p.get('view') : 'overview';
    for (const k of ['repo', 'node', 'edge', 'process']) st[k] = p.get(k) || '';
  }
  function writeHash() {
    const p = new URLSearchParams();
    p.set('view', st.view);
    for (const k of ['repo', 'node', 'edge', 'process']) if (st[k]) p.set(k, st[k]);
    try { history.replaceState(null, '', '#arch?' + p.toString()); } catch (e) {}
  }
  function go(patch) {
    if (patch.view && patch.view !== st.view) st.q = '';     // поиск у каждого раздела свой
    Object.assign(st, patch); writeHash(); render();
  }

  /* ---------- оболочка ---------- */
  function css() {
    if (document.getElementById('arch-css')) return;
    const s = document.createElement('style'); s.id = 'arch-css';
    s.textContent = `
#arch-root .a-nav{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 14px}
#arch-root .a-nav button[aria-current="page"]{background:var(--accent);border-color:var(--accent);color:#fff}
#arch-root .a-box{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px;overflow-x:auto;min-width:0}
#arch-root .a-split>div{min-width:0}
#arch-root{max-width:100%;overflow-wrap:anywhere}
#arch-root .a-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:14px}
#arch-root .a-kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
#arch-root .a-kpi b{display:block;font-size:24px;line-height:1.2}
#arch-root .a-kpi span{font-size:12px;color:var(--ink3)}
#arch-root .a-note{font-size:13px;color:var(--ink2)}
#arch-root .a-warn{border-color:var(--warn)}
#arch-root .a-split{display:grid;grid-template-columns:minmax(240px,340px) 1fr;gap:14px}
#arch-root .a-list{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
#arch-root .a-list button{display:block;width:100%;text-align:left;background:none;border:0;border-bottom:1px solid var(--line2);
  padding:7px 10px;color:var(--ink);font:inherit;font-size:13px;cursor:pointer}
#arch-root .a-list button:hover,#arch-root .a-list button[aria-current="true"]{background:var(--panel2)}
#arch-root .a-list small{color:var(--ink3)}
#arch-root .a-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
#arch-root input.a-q{flex:1;min-width:160px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit}
#arch-root table.bl td code{font-size:12px}
#arch-root .a-map{width:100%;height:auto;display:block;margin:8px 0}
#arch-root .a-map text{font-size:12px;fill:var(--ink)}
#arch-root .a-map .n rect{fill:var(--panel2);stroke:var(--line)}
#arch-root .a-map .n.c rect{fill:var(--accent);stroke:var(--accent)}
#arch-root .a-map .n.c text{fill:#fff}
#arch-root .a-map .n{cursor:pointer}
#arch-root .a-map .n:focus rect,#arch-root .a-map .n:hover rect{stroke:var(--accent);stroke-width:2}
#arch-root .a-map path{fill:none;stroke:var(--ink3);stroke-width:1.3}
#arch-root .a-map path.dash{stroke-dasharray:4 3}
#arch-root .a-hop{font-size:13px;padding:3px 0}
#arch-root .a-hop code{font-size:12px}
#arch-root .a-btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
@media (max-width:760px){#arch-root .a-split{grid-template-columns:1fr}}`;
    document.head.appendChild(s);
  }

  function shell(body) {
    const nav = VIEWS.map(([k, t]) => `<button class="btn" data-view="${k}"${k === st.view ? ' aria-current="page"' : ''}>${t}</button>`).join('');
    root.innerHTML = `<div class="section-h">Архитектура систем</div><div class="a-nav" role="navigation" aria-label="Разделы архитектуры">${nav}</div><div id="arch-body">${body}</div>`;
    root.querySelectorAll('[data-view]').forEach(b => b.onclick = () => go({view: b.dataset.view}));
  }

  function refusal(res) {
    if (!served()) return `<div class="a-box a-warn">Вкладка читает данные с сервера досок. Откройте доску ярлыком TaskTreker или командой <code>py tools/pm.py serve</code>, а не файлом.</div>`;
    if (res.status === 409 && res.disabled) return disabledBox();
    if (res.status === 403) return `<div class="a-box a-warn">Сервер отклонил запрос: ${esc(res.error)}. Обновите страницу.</div>`;
    if (res.status === 404 && /нет прав/.test(res.error || '')) return `<div class="a-box a-warn">Топология — чувствительные сведения: её видит владелец. Откройте доску ссылкой владельца — её печатает <code>py tools/pm.py owner-link</code> в вашем терминале.</div>`;
    return `<div class="a-box a-warn">${esc(res.error || 'нет данных')}</div>`;
  }

  function disabledBox() {
    return `<div class="a-box a-warn"><b>Модуль «Архитектура систем» выключен.</b>
      <p class="a-note">Он собирает сведения о портах, контейнерах и коде ваших проектов — только чтением, без запуска кода проектов
      и без удалённого сканирования. Включает модуль владелец; выключение закрывает и чтение, и сбор, данные остаются на диске.</p>
      <div class="a-btns"><button class="btn primary" id="arch-enable">Включить модуль</button></div>
      <p class="a-note" id="arch-enable-msg"></p></div>`;
  }
  function bindEnable() {
    const b = document.getElementById('arch-enable'); if (!b) return;
    b.onclick = async () => {
      b.disabled = true;
      const r = await api('/api/work', {op: 'settings', changes: {flags: {network_architecture_enabled: true}}});
      if (r.ok) { Object.keys(cache).forEach(k => delete cache[k]); render(); }
      else { document.getElementById('arch-enable-msg').textContent = r.error || 'не удалось'; b.disabled = false; }
    };
  }

  async function load(key, path, force) {
    if (!force && cache[key]) return cache[key];
    const r = await api(path);
    if (r.ok) cache[key] = r;
    return r;
  }

  /* ---------- Обзор ---------- */
  async function viewOverview() {
    const r = await load('overview', '/api/arch/overview', true);
    if (!r.ok) return shell(refusal(r)), bindEnable();
    const d = r.data, inv = d.inventory, counts = (inv && inv.counts) || {};
    const kpi = (n, t) => `<div class="a-kpi"><b>${n == null ? '—' : esc(n)}</b><span>${t}</span></div>`;
    let html = `<div class="a-grid">
      ${kpi(inv ? (counts.binding || 0) : null, 'привязок портов')}
      ${kpi(inv ? (counts.container || 0) : null, 'контейнеров')}
      ${kpi(inv ? (counts.process || 0) : null, 'процессов ОС')}
      ${kpi(inv ? inv.conflicts : null, 'возможных конфликтов портов')}
      ${kpi(d.code.length, 'графов кода')}
      ${kpi(d.processes.count, 'бизнес-процессов')}</div>`;
    html += `<div class="a-box"><b>Снимок ОС и Docker</b><p class="a-note">`;
    html += inv ? `Собран ${when(inv.taken_at)} · ${inv.complete ? chip('полный', 'ok') : chip('частичный', 'warn')}</p>
      <div class="a-row">${(inv.sources || []).map(s => chip(s.collector_id + ': ' + s.state, SRC[s.state] || '') + (s.reason ? ` <small class="a-note">${esc(s.reason)}</small>` : '')).join(' ')}</div>`
      : `Снимка ещё нет — данных о портах и контейнерах нет, а не «портов нет».</p>`;
    html += `</div><div class="a-box"><b>Графы кода</b>`;
    html += d.code.length ? `<table class="bl"><thead><tr><th>Репозиторий</th><th>Проект</th><th>Версия</th><th>Модули · функции · маршруты · CLI</th><th>Неразрешено</th></tr></thead><tbody>` +
      d.code.map(c => `<tr><td><a href="#" data-repo="${esc(c.repo)}">${esc(c.repo)}</a></td><td>${c.project ? esc(c.project) : chip(c.project_method || 'не привязан', 'warn')}</td>
        <td><code>${esc((c.commit_sha || '').slice(0, 10) || '—')}</code>${c.dirty ? ' ' + chip('есть незакоммиченное', 'warn') : ''}</td>
        <td>${[c.counts.modules, c.counts.functions, c.counts.routes, c.counts.cli_commands].map(x => x == null ? '—' : x).join(' · ')}${c.truncated ? ' ' + chip('урезан по лимиту', 'warn') : ''}</td>
        <td>${c.unresolved}</td></tr>`).join('') + `</tbody></table>`
      : `<p class="a-note">Графов кода нет. Сбор разбирает исходники только чтением (AST): код проектов не исполняется.</p>`;
    html += `</div>`;
    if (d.can_scan) html += scanBox(d.scan);
    shell(html);
    root.querySelectorAll('[data-repo]').forEach(a => a.onclick = e => { e.preventDefault(); go({view: 'internal', repo: a.dataset.repo, node: ''}); });
    bindScan(d.scan);
  }

  function scanBox(scan) {
    const projects = Object.entries((typeof D !== 'undefined' && D.projects && D.projects.projects) || {}).filter(([k, p]) => k !== 'tasktreker' && p && p.path);
    const steps = scan && scan.steps && scan.steps.length ? `<div>${scan.steps.map(s => `<div class="a-hop">${s.ok ? chip('готово', 'ok') : chip('сбой', 'bad')} ${esc(s.name)} — ${esc(s.note)}</div>`).join('')}</div>` : '';
    const state = scan && scan.running ? chip('идёт сбор…', 'warn') : scan && scan.finished ? (scan.ok ? chip('последний сбор успешен', 'ok') : chip('последний сбор с ошибками', 'bad')) + ` <small class="a-note">${when(scan.finished)}</small>` : '';
    return `<div class="a-box"><b>Собрать снимок</b>
      <p class="a-note">Порты, службы и резервации Windows, контейнеры Docker и объявления Compose — командами только для чтения;
      граф кода TaskTreker и выбранных проектов — разбором исходников без их исполнения. Удалённых хостов и запуска приложений нет.</p>
      <div class="a-row"><label class="a-note" for="arch-proj">Ещё разобрать код проекта:</label>
      <select class="sel" id="arch-proj" multiple size="3">${projects.map(([k, p]) => `<option value="${esc(k)}">${esc(p.name || k)}</option>`).join('')}</select>
      <button class="btn primary" id="arch-scan"${scan && scan.running ? ' disabled' : ''}>Собрать</button> ${state}</div>${steps}<p class="a-note" id="arch-scan-msg"></p></div>`;
  }
  function bindScan(scan) {
    const b = document.getElementById('arch-scan');
    if (b) b.onclick = async () => {
      b.disabled = true;
      const sel = [...document.getElementById('arch-proj').selectedOptions].map(o => o.value);
      const r = await api('/api/arch/scan', {projects: sel});
      document.getElementById('arch-scan-msg').textContent = r.ok ? 'Сбор начат: ' + r.data.repos.join(', ') : (r.error || 'не удалось');
      if (r.ok) watch(); else b.disabled = false;
    };
    if (scan && scan.running) watch();
  }
  function watch() {
    clearTimeout(poll);
    poll = setTimeout(async () => {
      if (st.view !== 'overview' || !root || root.closest('[hidden]')) return;
      Object.keys(cache).forEach(k => delete cache[k]);
      await viewOverview();
    }, 2500);
  }

  /* ---------- Порты и контейнеры ---------- */
  async function viewPorts() {
    const r = await load('graph', '/api/arch/graph');
    if (!r.ok) {
      if (r.status === 404 && /снимка/.test(r.error || '')) return shell(`<div class="a-box">Снимка ещё нет. Соберите его на вкладке «Обзор».</div>`);
      return shell(refusal(r)), bindEnable();
    }
    const objs = r.data.objects || [], by = {}; objs.forEach(o => by[o.id] = o);
    const ownerName = o => { const w = o.owner && by[o.owner]; return w ? (w.name || w.image || ('pid ' + (w.pid || '?'))) : (o.host_id === 'declared' ? (o.owner || 'объявлен') : 'неизвестно'); };
    const projOf = o => { const w = o.owner && by[o.owner]; return (w && w.project) || (o.host_id === 'declared' ? o.owner : '') || ''; };
    const q = st.q.trim().toLowerCase();
    const binds = objs.filter(o => o.kind === 'binding').filter(o => !q || [o.port, o.bind_ip, ownerName(o), projOf(o), o.protocol].join(' ').toLowerCase().includes(q))
      .sort((a, b) => a.port - b.port || a.protocol.localeCompare(b.protocol));
    const conts = objs.filter(o => o.kind === 'container');
    const conf = r.data.conflicts || [];
    let html = `<div class="a-row"><input class="a-q" id="arch-q" placeholder="Порт, адрес, процесс или проект — например 8080" value="${esc(st.q)}"></div>`;
    if (conf.length) html += `<div class="a-box a-warn"><b>Возможные конфликты портов: ${conf.length}</b><p class="a-note">Порт объявлен несколькими владельцами. Одновременный запуск не доказан — это подозрение, а не подтверждённый конфликт.</p>
      ${conf.slice(0, 30).map(c => `<div class="a-hop">${esc(c.protocol)} ${esc(c.port)}: ${c.owners.map(x => esc(x || 'неизвестно')).join(' ↔ ')}</div>`).join('')}</div>`;
    html += `<div class="a-box"><b>Привязки портов (${binds.length})</b><table class="bl"><thead><tr><th>Порт</th><th>Адрес</th><th>Состояние</th><th>Чей</th><th>Проект</th><th>Доступ</th></tr></thead><tbody>` +
      binds.slice(0, 400).map(b => `<tr><td><b>${esc(b.protocol)} ${esc(b.port)}</b>${b.container_port ? ` → ${esc(b.container_port)}` : ''}</td><td><code>${esc(b.bind_ip)}</code> ${chip(b.scope || 'неизвестно', b.scope === 'wildcard' ? 'warn' : '')}</td>
        <td>${chip(SOCK[b.socket_state] || b.socket_state, b.socket_state === 'listening_observed' ? 'ok' : 'idle')}</td><td>${esc(ownerName(b))}</td>
        <td>${projOf(b) ? esc(projOf(b)) : chip('не привязан', 'idle')}</td><td>${chip(b.reachability === 'not_probed' ? 'не проверялся' : (b.reachability || 'неизвестно'), 'idle')}</td></tr>`).join('') +
      `</tbody></table>${binds.length > 400 ? '<p class="a-note">Показаны первые 400 — уточните поиск.</p>' : ''}</div>`;
    html += `<div class="a-box"><b>Контейнеры (${conts.length})</b>` + (conts.length ? `<table class="bl"><thead><tr><th>Имя</th><th>Образ</th><th>Состояние</th><th>Проект</th></tr></thead><tbody>` +
      conts.map(c => `<tr><td>${esc(c.name)}</td><td><code>${esc(c.image || '—')}</code></td><td>${esc(c.lifecycle || 'неизвестно')}</td>
        <td>${c.project ? esc(c.project) + ' ' + chip(c.project_method, 'idle') : chip(c.project_candidates && c.project_candidates.length ? 'неоднозначно: ' + c.project_candidates.join(', ') : 'не привязан', 'warn')}</td></tr>`).join('') + `</tbody></table>`
      : `<p class="a-note">Контейнеров в снимке нет${(r.data.sources || []).some(s => /docker/.test(s.collector_id) && s.state !== 'available') ? ' — Docker был недоступен при сборе, это не значит, что их нет' : ''}.</p>`) + `</div>`;
    shell(html);
    const inp = document.getElementById('arch-q');
    inp.oninput = () => { st.q = inp.value; clearTimeout(inp._t); inp._t = setTimeout(() => { viewPorts().then(() => { const i = document.getElementById('arch-q'); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 250); };
  }

  /* ---------- Внутренняя архитектура ---------- */
  async function codeFor(repo) {
    const ov = await load('overview', '/api/arch/overview');
    if (!ov.ok) return {err: ov};
    const repos = ov.data.code.map(c => c.repo);
    if (!repos.length) return {empty: true};
    const pick = repos.includes(repo) ? repo : (repos.includes('tasktreker') ? 'tasktreker' : repos[0]);
    const r = await load('code:' + pick, '/api/arch/code?repo=' + encodeURIComponent(pick));
    if (!r.ok) return {err: r};
    const g = r.data;
    if (!g._ix) {
      const nodes = {}, inc = {}, out = {};
      g.nodes.forEach(n => nodes[n.id] = n);
      g.edges.forEach(e => { (out[e.source] ||= []).push(e); (inc[e.target] ||= []).push(e); });
      g._ix = {nodes, inc, out, edges: Object.fromEntries(g.edges.map(e => [e.id, e]))};
    }
    return {repo: pick, repos, g};
  }

  const label = n => n ? (n.kind === 'route' || n.kind === 'cli_command' ? n.qualname.split(':').slice(1).join(':') || n.qualname : n.qualname) : '?';

  async function viewInternal() {
    const c = await codeFor(st.repo);
    if (c.err) return shell(refusal(c.err)), bindEnable();
    if (c.empty) return shell(`<div class="a-box">Графов кода ещё нет. Соберите снимок на вкладке «Обзор».</div>`);
    if (st.repo !== c.repo) { st.repo = c.repo; writeHash(); }
    const {g} = c, ix = g._ix, q = st.q.trim().toLowerCase();
    const kinds = ['route', 'cli_command', 'module', 'class', 'function', 'job'];
    const list = g.nodes.filter(n => kinds.includes(n.kind) && (st.tests || !isTest(n)) && (!q || n.qualname.toLowerCase().includes(q)))
      .sort((a, b) => kinds.indexOf(a.kind) - kinds.indexOf(b.kind) || a.qualname.localeCompare(b.qualname)).slice(0, 600);
    const cur = ix.nodes[st.node];
    const cov = g.coverage || {};
    let html = `<div class="a-row"><label class="a-note" for="arch-repo">Репозиторий</label><select class="sel" id="arch-repo">${c.repos.map(r => `<option${r === c.repo ? ' selected' : ''}>${esc(r)}</option>`).join('')}</select>
      <span class="a-note">версия <code>${esc(((g.version_ref || {}).commit_sha || '').slice(0, 10))}</code> · ${cov.modules || 0} модулей · ${cov.functions || 0} функций · ${cov.routes || 0} маршрутов · ${cov.cli_commands || 0} команд · неразрешено ${g.unresolved.length}</span>
      ${g.truncated ? chip('ответ урезан по лимиту', 'warn') : ''}
      <label class="a-note"><input type="checkbox" id="arch-tests"${st.tests ? ' checked' : ''}> показывать тесты</label></div>
      <div class="a-split"><div><input class="a-q" id="arch-q" placeholder="Поиск: модуль, функция, маршрут" value="${esc(st.q)}" style="width:100%;margin-bottom:8px">
      <div class="a-list" role="listbox" aria-label="Узлы кода">${list.map(n => `<button role="option" data-node="${esc(n.id)}" aria-current="${n.id === st.node}">${esc(label(n))}<br><small>${KIND[n.kind] || n.kind}${n.file ? ' · ' + esc(n.file) : ''}</small></button>`).join('') || '<p class="a-note" style="padding:8px">Ничего не найдено</p>'}</div></div>
      <div>${cur ? nodeCard(cur, ix) : `<div class="a-box"><p class="a-note">Выберите маршрут, команду, модуль или функцию слева: покажем, кто её вызывает, что вызывает она и где это в коде.
        Связи строятся разбором исходников; динамические вызовы, которые определяются только при исполнении, перечислены как неразрешённые, а не дорисованы.</p></div>`}</div></div>`;
    shell(html);
    document.getElementById('arch-repo').onchange = e => go({repo: e.target.value, node: ''});
    document.getElementById('arch-tests').onchange = e => go({tests: e.target.checked});
    root.querySelectorAll('[data-node]').forEach(b => b.onclick = () => go({node: b.dataset.node}));
    root.querySelectorAll('[data-edge]').forEach(b => b.onclick = e => { e.preventDefault(); go({view: 'relation', edge: b.dataset.edge}); });
    const inp = document.getElementById('arch-q');
    inp.oninput = () => { st.q = inp.value; clearTimeout(inp._t); inp._t = setTimeout(() => viewInternal().then(() => { const i = document.getElementById('arch-q'); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }), 250); };
    const sel = root.querySelector('.a-list [aria-current="true"]'); if (sel) sel.scrollIntoView({block: 'nearest'});
  }

  function nodeCard(n, ix) {
    const inc = (ix.inc[n.id] || []).filter(e => e.type !== 'contains'), out = (ix.out[n.id] || []).filter(e => e.type !== 'contains');
    const parent = (ix.inc[n.id] || []).find(e => e.type === 'contains');
    const kids = (ix.out[n.id] || []).filter(e => e.type === 'contains');
    const row = (e, other) => `<tr><td>${ETYPE[e.type] || esc(e.type)}</td><td><a href="#" data-node-go="${esc(other)}">${esc(label(ix.nodes[other]))}</a></td>
      <td>${chip(e.evidence_quality || '—', e.evidence_quality === 'verified' ? 'ok' : 'idle')} ${e.resolution && e.resolution !== 'resolved' ? chip(e.resolution, 'warn') : ''}</td>
      <td><a href="#" data-edge="${esc(e.id)}">паспорт связи</a></td></tr>`;
    const table = (rows, empty) => rows.length ? `<table class="bl"><thead><tr><th>Связь</th><th>С кем</th><th>Доказательство</th><th></th></tr></thead><tbody>${rows.join('')}</tbody></table>` : `<p class="a-note">${empty}</p>`;
    const html = `<div class="a-box"><b>${esc(label(n))}</b> ${chip(KIND[n.kind] || n.kind)}
      <p class="a-note">${n.file ? `<code>${esc(n.file)}${n.line ? ':' + esc(n.line) : ''}</code>` : 'места в коде нет'}${parent ? ` · входит в <a href="#" data-node-go="${esc(parent.source)}">${esc(label(ix.nodes[parent.source]))}</a>` : ''}</p>
      ${miniMap(n, inc, out, ix)}
      <div class="section-h">Входящие (${inc.length})</div>${table(inc.slice(0, 80).map(e => row(e, e.source)), 'Входящих связей в графе нет. Это не значит, что код не вызывается: динамические вызовы и внешние точки входа графу не видны.')}
      <div class="section-h">Исходящие (${out.length})</div>${table(out.slice(0, 80).map(e => row(e, e.target)), 'Исходящих связей нет.')}
      ${kids.length ? `<div class="section-h">Состав (${kids.length})</div><div class="a-row">${kids.slice(0, 120).map(e => `<a href="#" class="chip" data-node-go="${esc(e.target)}">${esc(label(ix.nodes[e.target]).split('.').pop())}</a>`).join('')}</div>` : ''}</div>`;
    setTimeout(() => root.querySelectorAll('[data-node-go]').forEach(a => a.onclick = ev => { ev.preventDefault(); go({node: a.dataset.nodeGo}); }), 0);
    return html;
  }

  /* Окрестность узла: входящие слева, исходящие справа. Стрелка — направление, пунктир — неразрешённая связь;
     у каждого узла есть фокус клавиатурой и Enter. */
  function miniMap(n, inc, out, ix) {
    const L = inc.slice(0, 8), R = out.slice(0, 8), rows = Math.max(L.length, R.length, 1), H = rows * 34 + 20, W = 760;
    const box = (x, y, w, id, text, cls) => `<g class="n ${cls || ''}" tabindex="0" role="button" data-node-go="${esc(id)}" aria-label="${esc(text)}"><rect x="${x}" y="${y}" width="${w}" height="26" rx="6"></rect><text x="${x + 8}" y="${y + 17}">${esc(text.length > 34 ? '…' + text.slice(-33) : text)}</text></g>`;
    const cy = H / 2 - 13;
    let s = `<svg class="a-map" viewBox="0 0 ${W} ${H}" role="img" aria-label="Окрестность узла"><defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="var(--ink3)" stroke="none"></path></marker></defs>`;
    L.forEach((e, i) => { const y = 10 + i * 34; s += `<path class="${e.resolution && e.resolution !== 'resolved' ? 'dash' : ''}" d="M230 ${y + 13} C 270 ${y + 13}, 260 ${cy + 13}, 290 ${cy + 13}" marker-end="url(#arr)"></path>` + box(0, y, 230, e.source, label(ix.nodes[e.source])); });
    R.forEach((e, i) => { const y = 10 + i * 34; s += `<path class="${e.resolution && e.resolution !== 'resolved' ? 'dash' : ''}" d="M470 ${cy + 13} C 500 ${cy + 13}, 490 ${y + 13}, 530 ${y + 13}" marker-end="url(#arr)"></path>` + box(530, y, 230, e.target, label(ix.nodes[e.target])); });
    s += box(290, cy, 180, n.id, label(n), 'c') + `</svg>`;
    setTimeout(() => root.querySelectorAll('.a-map .n').forEach(g => g.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go({node: g.dataset.nodeGo}); } }), 0);
    return s + (inc.length > 8 || out.length > 8 ? `<p class="a-note">На схеме — первые 8 связей с каждой стороны, полный список ниже.</p>` : '');
  }

  /* ---------- Связь ---------- */
  async function viewRelation() {
    const c = await codeFor(st.repo);
    if (c.err) return shell(refusal(c.err)), bindEnable();
    if (c.empty) return shell(`<div class="a-box">Графов кода ещё нет.</div>`);
    const e = c.g._ix.edges[st.edge];
    if (!e) return shell(`<div class="a-box">Связь не выбрана или её нет в версии <code>${esc(((c.g.version_ref || {}).commit_sha || '').slice(0, 10))}</code> репозитория ${esc(c.repo)}.
      Выберите связь в паспорте узла на вкладке «Внутренняя архитектура».</div>`);
    const ix = c.g._ix, s = ix.nodes[e.source], t = ix.nodes[e.target];
    const html = `<div class="a-box"><b>Связь <code>${short(e.id)}</code></b>
      <table class="bl"><tbody>
      <tr><td>Откуда</td><td><a href="#" data-node-go="${esc(e.source)}">${esc(label(s))}</a> ${chip(KIND[s && s.kind] || '?')}</td></tr>
      <tr><td>Куда</td><td><a href="#" data-node-go="${esc(e.target)}">${esc(label(t))}</a> ${chip(KIND[t && t.kind] || '?')}</td></tr>
      <tr><td>Смысл</td><td>${ETYPE[e.type] || esc(e.type)}${e.handler_kind ? ' · ' + esc(e.handler_kind) : ''}</td></tr>
      <tr><td>Разрешение</td><td>${chip(e.resolution || '—', e.resolution === 'resolved' ? 'ok' : 'warn')}</td></tr>
      <tr><td>Доказательство</td><td>${chip(e.evidence_quality || '—', e.evidence_quality === 'verified' ? 'ok' : 'idle')} статический разбор, версия <code>${esc(((c.g.version_ref || {}).commit_sha || '').slice(0, 10))}</code> — исполнение не наблюдалось</td></tr>
      <tr><td>Где в коде</td><td>${(e.locations || []).slice(0, 20).map(l => `<div><code>${esc(l.file)}:${esc(l.line)}</code></div>`).join('') || 'неизвестно'}</td></tr>
      </tbody></table>
      <div class="a-btns"><button class="btn" id="arch-back">⇦ На внутреннюю схему</button></div></div>`;
    shell(html);
    document.getElementById('arch-back').onclick = () => go({view: 'internal', node: st.node || e.source});
    root.querySelectorAll('[data-node-go]').forEach(a => a.onclick = ev => { ev.preventDefault(); go({view: 'internal', node: a.dataset.nodeGo}); });
  }

  /* ---------- Бизнес-процессы ---------- */
  async function viewProcesses() {
    const [r, ov] = [await load('processes', '/api/arch/processes', true), await load('overview', '/api/arch/overview')];
    if (!r.ok) return shell(refusal(r)), bindEnable();
    const list = r.data.processes, seeds = (ov.ok && ov.data.seeds) || [];
    const have = new Set(list.map(p => p.process.key));
    const seedBtns = seeds.filter(s => !have.has(s.replace(/^tasktreker_/, ''))).map(s => `<button class="btn" data-seed="${esc(s)}">Внести: ${esc(SEEDS[s] || s)}</button>`).join(' ');
    let html = '';
    if (!list.length) html += `<div class="a-box"><p class="a-note">Процессов в реестре нет. Процесс входит в реестр только из документа или с подтверждения владельца — ИИ его не выдумывает.</p>${seedBtns ? `<div class="a-btns">${seedBtns}</div>` : ''}<p class="a-note" id="arch-seed-msg"></p></div>`;
    else {
      const cur = list.find(p => p.process.id === st.process) || list[0];
      html += `<div class="a-row">${list.map(p => `<button class="btn" data-proc="${esc(p.process.id)}"${p === cur ? ' aria-current="page"' : ''}>${esc(p.process.name)}</button>`).join('')} ${seedBtns}</div><p class="a-note" id="arch-seed-msg"></p>`;
      html += procCard(cur);
    }
    shell(html);
    root.querySelectorAll('[data-proc]').forEach(b => b.onclick = () => go({process: b.dataset.proc}));
    root.querySelectorAll('[data-seed]').forEach(b => b.onclick = async () => {
      b.disabled = true;
      const x = await api('/api/arch/seed', {name: b.dataset.seed});
      document.getElementById('arch-seed-msg').textContent = x.ok ? 'Процесс внесён черновиком: утверждает владелец.' : (x.error || 'не удалось');
      if (x.ok) { delete cache.processes; delete cache.overview; render(); } else b.disabled = false;
    });
    root.querySelectorAll('[data-code-go]').forEach(a => a.onclick = ev => { ev.preventDefault(); go({view: 'internal', node: a.dataset.codeGo, repo: a.dataset.repo || st.repo}); });
  }

  function procCard(x) {
    const p = x.process, tr = x.traceability || {};
    const STATE = {draft: ['черновик', 'warn'], proposed: ['предложение', 'warn'], approved: ['утверждён', 'ok'], deprecated: ['устарел', 'idle']};
    const [stT, stC] = STATE[p.state] || [p.state, ''];
    const GATE = {start: 'начало процесса', end: 'завершение', error_end: 'завершение с ошибкой', gateway_exclusive: 'развилка: одна ветка',
                  gateway_inclusive: 'развилка: одна или несколько веток', gateway_parallel: 'параллельные ветки'};
    const steps = x.steps.map(({step, chains, structural}) => {
      if (structural) return `<tr><td><b>${esc(step.name || step.key)}</b><br><small class="a-note">${esc(GATE[step.type] || step.type)}${step.actor ? ' · ' + esc(step.actor) : ''}</small></td><td>${chip('форма процесса — реализация в коде не требуется', 'idle')}</td></tr>`;
      const body = chains.map(ch => {
        const hops = ch.hops.map(h => `<div class="a-hop">→ ${esc(h.type)} ${chip(h.quality, h.quality === 'verified' ? 'ok' : 'idle')} ${h.layer === 'logical' || h.layer === 'call' ? `<a href="#" data-code-go="${esc(h.layer === 'logical' ? h.to : h.frm)}" data-repo="${esc(x.code_repo || '')}">к коду</a>` : ''}${h.port ? ` порт ${esc(h.protocol)} ${esc(h.port)}` : ''}${h.file ? ` <code>${esc(h.file)}:${esc(h.line || '')}</code>` : ''}</div>`).join('');
        const end = ch.broken ? `<div class="a-hop">${chip('разрыв', 'warn')} ${esc(ch.broken.text)} <small class="a-note">(${esc(ch.broken.reason)})</small></div>` : `<div class="a-hop">${chip(ch.confirmed ? 'путь подтверждён' : 'путь есть, не всё подтверждено', ch.confirmed ? 'ok' : 'warn')}</div>`;
        const iss = (ch.issues || []).map(i => `<div class="a-hop">${chip('замечание', 'warn')} ${esc(i.text)}</div>`).join('');
        return hops + end + iss;
      }).join('<hr style="border:0;border-top:1px dashed var(--line)">');
      return `<tr><td><b>${esc(step.name || step.key)}</b><br><small class="a-note">${esc(step.type)}${step.actor ? ' · ' + esc(step.actor) : ''}</small></td><td>${body}</td></tr>`;
    }).join('');
    return `<div class="a-box"><b>${esc(p.name)}</b> ${chip(stT, stC)} ${chip('версия ' + p.version)}
      <p class="a-note">${esc(p.goal || '')}</p>
      <p class="a-note">Источник: ${esc(p.source.kind)}${p.source.ref ? ' · <code>' + esc(p.source.ref) + '</code>' : ''}${p.source.revision ? ' @ <code>' + esc(String(p.source.revision).slice(0, 10)) + '</code>' : ''} ·
        путей от начала до конца: ${(x.paths.paths || []).length}${x.paths.truncated ? '+' : ''} · шагов без привязки к коду: ${(tr.steps_without_mapping || []).length} · ручных: ${(tr.manual_steps || []).length}</p>
      <p class="a-note">Трассировка «шаг → код → runtime → порт» строится только по подтверждённым привязкам. Где привязки нет, показан разрыв с причиной — путь не дорисовывается.</p>
      <table class="bl"><thead><tr><th>Шаг</th><th>Реализация</th></tr></thead><tbody>${steps}</tbody></table></div>`;
  }

  /* ---------- вход ---------- */
  async function render() {
    if (!root) return;
    css();
    if (!served()) return shell(refusal({}));
    if (!st.loaded) { shell('<p class="a-note">Загрузка…</p>'); st.loaded = true; }
    const fn = {overview: viewOverview, ports: viewPorts, internal: viewInternal, processes: viewProcesses, relation: viewRelation}[st.view];
    await fn();
  }

  return {
    shown(query) {
      root = document.getElementById('arch-root');
      readHash(query);
      writeHash();
      render();
    },
    state: st,
  };
})();
