/* Раздел «Токены ролей» вкладки «Дашборд»: куда уходят токены запусков конвейера и дешевеет ли полезная работа.
   Данные считает tools/tokview.py из журналов сессий и workflow при сборке — без ИИ. Главная мера —
   токены на одну принятую сдачу этапа: она учитывает и возвраты, и упавшие запуски. */

const TOKVIEW = (() => {
  const ROLE = {product_owner: 'Product Owner', system_analyst: 'Системный аналитик', solution_architect: 'Архитектор',
                designer: 'Дизайнер', marketer: 'Маркетолог', developer: 'Разработчик', reviewer: 'Ревьюер',
                devops: 'DevOps', qa: 'QA', teamlead: 'Тимлид', resolver: 'Решала', 'исполнитель': 'Исполнитель'};
  const STAGE = {idea: 'Идея', analysis: 'Анализ', development: 'Разработка', review: 'Ревью', deploy_test: 'Деплой в тест',
                 testing: 'Тестирование', deploy_main: 'Деплой в main', '': 'вне конвейера'};
  const NO_HANDOFF = new Set(['teamlead', 'resolver']);      // ворота и эскалации — не сдачи этапа
  const $ = id => document.getElementById(id);
  const h = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const tok = n => n == null ? '—' : !n ? '0' : n >= 1e9 ? (n / 1e9).toFixed(2).replace('.', ',') + ' млрд'
    : n >= 1e6 ? (n / 1e6).toFixed(1).replace('.', ',') + ' млн' : Math.round(n / 1e3) + ' тыс';
  const role = r => ROLE[r] || r;
  const day = iso => iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) : '';
  const project = () => { const v = ($('proj-select') || {}).value; return v && v !== '__all' ? v : ''; };
  let picked = '';

  function render() {
    const host = $('tokview');
    if (!host || host.offsetParent === null) return;
    const d = D.tokview;
    const head = '<div class="section-h">Токены ролей — куда уходят и дешевеет ли работа</div>';
    if (!d || d.error) { host.innerHTML = head + `<div class="tv-note">${d ? h(d.error) : 'нет данных: пересоберите дашборд'}</div>`; return; }
    const p = project(), v = d.views[p] || d.views[''], runs = d.runs.filter(r => !p || r.project === p);
    if (!runs.length) { host.innerHTML = head + '<div class="tv-note">У проекта нет запусков ролей с журналом сессии.</div>'; return; }
    const k = v.kpi, cr = k.total ? Math.round(100 * k.cache_read / k.total) : 0;
    host.innerHTML = head + `
      <div class="tv-bar"><span class="tv-note">из журналов сессий и workflow, без ИИ · снимок сборки ${day(d.generated_at)}
        · всё = вход + запись кеша + чтение кеша + выход</span></div>
      <div class="kpis">
        <div class="kpi"><div class="v">${tok(k.per)}</div><div class="l">на принятую сдачу</div>
          <div class="n">главная мера: ${tok(k.total)} на ${k.accepted} принятых сдач этапа, с возвратами и падениями</div></div>
        <div class="kpi"><div class="v">${cr}%</div><div class="l">чтение кеша</div>
          <div class="n">контекст, перечитанный в каждом ходе: короче контекст — дешевле каждый ход</div></div>
        <div class="kpi"><div class="v" style="color:${k.over_budget ? 'var(--warn)' : ''}">${k.over_budget}</div><div class="l">сверх ориентира</div>
          <div class="n">из ${k.runs} запусков обработали больше ориентира роли</div></div>
        <div class="kpi"><div class="v">${k.relays}</div><div class="l">эстафет</div><div class="n">свежая сессия с точки остановки при потолке контекста</div></div>
        <div class="kpi"><div class="v">${k.justified}</div><div class="l">полных чтений с причиной</div>
          <div class="n">сторож сберёг ~${tok(k.saved)}</div></div>
      </div>
      <div class="card-viz" id="tk-curve"></div>
      <div class="card-viz" id="tk-handoff"></div>
      <div class="card-viz" id="tk-compare"></div>
      <div class="card-viz" id="tk-matrix"></div>
      <div class="card-viz" id="tk-outliers"></div>`;
    curve($('tk-curve'), runs.filter(r => r.series), d.ceiling);
    handoff($('tk-handoff'), v.per_handoff);
    compare($('tk-compare'), v.compare);
    matrix($('tk-matrix'), v.matrix);
    outliers($('tk-outliers'), v.outliers);
  }

  // 1. Контекст по ходам одного запуска: потолок, новые сессии (эстафета, продолжение), полные чтения.
  function curve(host, runs, ceiling) {
    if (!runs.length) { host.innerHTML = '<div class="viz-title">Контекст по ходам</div><div class="tv-note">Нет недавних запусков с журналом.</div>'; return; }
    const list = runs.slice().reverse();
    const r = list.find(x => x.run === picked) || list[0];
    const opts = list.map(x => `<option value="${x.run}" ${x === r ? 'selected' : ''}>${h(x.run)} · ${h(x.ref)} · ${h(role(x.role))} · ${day(x.at)} · ${tok(x.total)}</option>`).join('');
    host.innerHTML = `<div class="viz-title">Контекст по ходам запуска</div>
      <div class="viz-sub">Сколько токенов модель перечитывает на каждом ходе. Площадь под линией — это и есть чтение кеша, главная статья расхода.
        Пунктир — потолок ${tok(ceiling)}: выше него роль сдаёт точку остановки, и сервер открывает свежую сессию.
        Вертикали — новая сессия запуска, точки — полное чтение файла.</div>
      <select class="sel" id="tk-run" style="max-width:100%;margin-bottom:10px">${opts}</select><div id="tk-svg"></div>
      <div class="tv-note">${r.turns} ходов · старт ${tok(r.start)} · пик ${tok(r.peak)} · всего ${tok(r.total)}${r.budget ? ' · ориентир ' + tok(r.budget) : ''}
        · сессий ${r.sessions} · полных чтений ${r.full_reads}${r.step > 1 ? ' · точка = максимум из ' + r.step + ' ходов' : ''}
        ${r.reasons && r.reasons.length ? '<br>Обоснования: ' + r.reasons.map(h).join(' · ') : ''}</div>`;
    $('tk-run').addEventListener('change', e => { picked = e.target.value; curve(host, runs, ceiling); });
    const box = $('tk-svg'), W = Math.max(340, Math.round(host.clientWidth || 900) - 30), H = 220, L = 52, B = 22, T = 8;
    const s = r.series, top = Math.max(ceiling * 1.1, ...s), n = Math.max(1, s.length - 1);
    const x = i => L + (W - L - 8) * i / n, y = v => T + (H - T - B) * (1 - v / top);
    const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('role', 'img');
    const add = (name, a, text) => { const e = document.createElementNS(NS, name); for (const q in a) e.setAttribute(q, a[q]); if (text != null) e.textContent = text; svg.appendChild(e); return e; };
    for (let g = 0; g <= 4; g++) {
      const v = top * g / 4;
      add('line', {x1: L, x2: W - 8, y1: y(v), y2: y(v), class: 'viz-grid'});
      add('text', {x: L - 6, y: y(v) + 4, 'text-anchor': 'end', class: 'viz-tick'}, tok(v));
    }
    const pts = s.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
    add('polygon', {points: `${x(0)},${y(0)} ${pts} ${x(s.length - 1)},${y(0)}`, fill: 'var(--q1)', opacity: .7});
    add('polyline', {points: pts, fill: 'none', stroke: 'var(--s1)', 'stroke-width': 2});
    if (ceiling) add('line', {x1: L, x2: W - 8, y1: y(ceiling), y2: y(ceiling), stroke: 'var(--bad)', 'stroke-dasharray': '5 4', 'stroke-width': 1.5});
    (r.breaks || []).forEach(b => add('line', {x1: x(b), x2: x(b), y1: T, y2: H - B, stroke: 'var(--s7)', 'stroke-width': 1.5}));
    (r.read_at || []).forEach(i => add('circle', {cx: x(i), cy: y(s[i] || 0), r: 3.5, fill: 'var(--s2)'}));
    add('text', {x: L, y: H - 5, class: 'viz-tick'}, 'ход 1');
    add('text', {x: W - 8, y: H - 5, 'text-anchor': 'end', class: 'viz-tick'}, 'ход ' + r.turns);
    box.appendChild(svg);
    const legend = document.createElement('div'); legend.className = 'viz-legend';
    legend.innerHTML = '<span><i style="background:var(--s1)"></i>контекст хода</span><span><i style="background:var(--bad)"></i>потолок</span>'
      + '<span><i style="background:var(--s7)"></i>новая сессия</span><span><i style="background:var(--s2)"></i>полное чтение</span>';
    box.appendChild(legend);
  }

  // 3. Токены на принятую сдачу — по ролям за всё время и по неделям.
  function handoff(host, rows) {
    const all = rows.filter(r => !r.week && !NO_HANDOFF.has(r.role) && r.per);
    const weeks = [...new Set(rows.filter(r => r.week).map(r => r.week))].sort().slice(-8);
    if (!all.length) { host.innerHTML = '<div class="viz-title">Токены на принятую сдачу этапа</div><div class="tv-note">Принятых сдач ещё нет.</div>'; return; }
    const top = Math.max(...rows.filter(r => r.per).map(r => r.per));
    VIZ.matrix(host, {
      title: 'Токены на принятую сдачу этапа — по ролям и неделям',
      sub: 'Все токены роли за неделю, включая возвращённые и упавшие запуски, делённые на число сдач, принятых сервером по DoD. Если оптимизации работают, ячейки светлеют слева направо.',
      rows: all.map(r => ({key: r.role, label: role(r.role)})),
      cols: weeks.map(w => ({key: w, label: w.slice(5)})).concat([{key: '', label: 'всего'}]),
      cell: (rw, c) => {
        const x = rows.find(q => q.role === rw.key && q.week === c.key);
        if (!x || !x.per) return {value: null, tip: `<b>${role(rw.key)}</b> · ${c.label}<br>${x ? tok(x.tokens) + ', принятых сдач нет' : 'запусков нет'}`};
        return {value: Math.round(100 * x.per / top), text: tok(x.per),
                tip: `<b>${role(rw.key)}</b> · ${c.label}<br>${tok(x.per)} на сдачу<br>${tok(x.tokens)} · запусков ${x.runs} · принято ${x.accepted}`};
      },
      note: 'Тимлид и Решала не сдают этапы — их расход виден в таблице «роль × этап».',
    });
  }

  // 4. Было и стало по каждой оптимизации — медианы по ролям.
  function compare(host, groups) {
    host.innerHTML = '<div class="viz-title">Было и стало — эффект оптимизаций</div>'
      + '<div class="viz-sub">Медианы запуска по ролям в группах «без» и «с»: сравнивается одинаковая работа. Разница — доказательство эффекта, а не ожидание.</div>'
      + groups.map(g => {
        const rows = g.roles.filter(r => r.before.n || r.after.n);
        const delta = r => !r.before.n || !r.after.n ? '—'
          : `<b style="color:${r.after.total <= r.before.total ? 'var(--ok)' : 'var(--bad)'}">${Math.round(100 * (r.after.total / r.before.total - 1))}%</b>`;
        const side = s => s.n ? `${tok(s.total)} <span class="tv-note">· старт ${tok(s.start)} · ${s.turns} ходов · N ${s.n}</span>` : '<span class="tv-note">нет запусков</span>';
        return `<div style="margin:12px 0 4px;font-weight:600">${h(g.title)}</div>
          ${g.after_runs ? '' : `<div class="tv-note">Запусков «${h(g.labels[1])}» ещё нет: сравнение появится после первых запусков ролей на сервере досок.</div>`}
          <table class="pf"><thead><tr><th>Роль</th><th>${h(g.labels[0])}</th><th>${h(g.labels[1])}</th><th>Δ всего</th></tr></thead><tbody>
          ${rows.map(r => `<tr><td>${h(role(r.role))}</td><td>${side(r.before)}</td><td>${side(r.after)}</td><td>${delta(r)}</td></tr>`).join('')}
          </tbody></table>`;
      }).join('');
  }

  // 7. Роль × этап: медиана токенов на запуск.
  function matrix(host, cells) {
    const roles = [...new Set(cells.map(c => c.role))], stages = Object.keys(STAGE).filter(s => cells.some(c => c.stage === s));
    const top = Math.max(1, ...cells.map(c => c.median));
    VIZ.matrix(host, {
      title: 'Роль × этап — медиана токенов на запуск',
      sub: 'Темнее — дороже. Подсказка показывает число запусков и самый дорогой из них.',
      rows: roles.map(r => ({key: r, label: role(r)})),
      cols: stages.map(s => ({key: s, label: STAGE[s]})),
      cell: (r, c) => {
        const x = cells.find(q => q.role === r.key && q.stage === c.key);
        return x ? {value: Math.round(100 * x.median / top), text: tok(x.median),
                    tip: `<b>${role(r.key)}</b> · ${STAGE[c.key]}<br>медиана ${tok(x.median)} · запусков ${x.n}<br>самый дорогой ${tok(x.max)}`} : {value: null};
      },
    });
  }

  function outliers(host, rows) {
    host.innerHTML = '<div class="viz-title">Запуски вдвое дороже медианы своей роли</div>'
      + '<div class="viz-sub">Кандидаты на разбор: долгий поиск, раздутый контекст, лишние чтения. Кривую такого запуска видно выше, если он среди недавних.</div>'
      + (rows.length ? `<table class="pf"><thead><tr><th>Запуск</th><th>Задача</th><th>Роль</th><th>Всего</th><th>× медианы</th><th>Ходов</th><th>Пик контекста</th><th>Дата</th></tr></thead><tbody>
        ${rows.map(r => `<tr data-run="${h(r.run)}"><td>${h(r.run)}</td><td>${h(r.ref)}</td><td>${h(role(r.role))}</td><td>${tok(r.total)}</td>
          <td><b>${String(r.ratio).replace('.', ',')}×</b> <span class="tv-note">(${tok(r.median)})</span></td><td>${r.turns}</td><td>${tok(r.peak)}</td><td>${day(r.at)}</td></tr>`).join('')}
        </tbody></table>` : '<div class="tv-note">Таких нет.</div>');
    host.querySelectorAll('tr[data-run]').forEach(tr => tr.addEventListener('click', () => {
      if (!D.tokview.runs.some(r => r.run === tr.dataset.run && r.series)) return;
      picked = tr.dataset.run; render(); $('tk-curve').scrollIntoView({behavior: 'smooth', block: 'start'});
    }));
  }

  addEventListener('DOMContentLoaded', () => {
    const sel = $('proj-select');
    if (sel) sel.addEventListener('change', () => render());
    const tab = $('tab-dash');
    if (tab) tab.addEventListener('click', () => setTimeout(render, 0));
    setTimeout(render, 0);
  });
  let rsz;
  addEventListener('resize', () => { clearTimeout(rsz); rsz = setTimeout(render, 160); });
  return {render};
})();
