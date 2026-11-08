/* Раздел «Задачи» вкладки «Дашборд» (TT-95): что сделано и что нет, где стоят задачи, что двигалось в окне.
   Данные считает tools/taskview.py из workflow, бэклога и журналов сессий — без ИИ. Под сервером досок
   (pm.py serve) раздел живой: /api/taskview с выбором окна и проекта. Файлом — снимок сборки за 24 ч. */

const TASKVIEW = (() => {
  const WINDOWS = [[5, '5 ч'], [24, '24 ч'], [168, '7 дн'], [720, '30 дн']];
  const STAGE_SLOT = {idea: 0, analysis: 1, development: 2, review: 3, deploy_test: 4, testing: 5, deploy_main: 6};
  const STAGE_TITLE = {idea: 'Идея', analysis: 'Анализ', development: 'Разработка', review: 'Ревью',
                       deploy_test: 'Деплой в тест', testing: 'Тестирование', deploy_main: 'Деплой в main'};
  const ROLE = {product_owner: 'Product Owner', system_analyst: 'Системный аналитик', solution_architect: 'Архитектор',
                designer: 'Дизайнер', marketer: 'Маркетолог', developer: 'Разработчик', reviewer: 'Ревьюер',
                devops: 'DevOps', qa: 'QA', teamlead: 'Тимлид', resolver: 'Решала'};
  const live = /^https?:/.test(location.protocol);
  let hours = 24, data = null, loading = false;
  try { hours = +localStorage.getItem('tt-tv-hours') || 24; } catch (e) {}

  const $ = id => document.getElementById(id);
  const h = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const num = n => Number(n || 0).toLocaleString('ru-RU');
  const tok = n => !n ? '—' : n >= 1e6 ? (n / 1e6).toFixed(1).replace('.', ',') + ' млн' : Math.round(n / 1e3) + ' тыс';
  const hrs = v => !v ? '0 ч' : v < 1 ? Math.round(v * 60) + ' мин' : v < 48 ? v.toFixed(1).replace('.', ',') + ' ч'
    : (v / 24).toFixed(1).replace('.', ',') + ' дн';
  const day = iso => iso.slice(8, 10) + '.' + iso.slice(5, 7);
  const clock = iso => { const d = new Date(iso); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };
  const project = () => { const v = ($('proj-select') || {}).value; return v && v !== '__all' ? v : ''; };
  const slot = r => r.role === 'teamlead' || r.role === 'resolver' ? 7 : (STAGE_SLOT[r.stage] ?? 7);

  async function load() {
    if (!live) { data = D.taskview; return render(); }
    loading = true; render();
    try {
      const r = await fetch(`/api/taskview?hours=${hours}&project=${encodeURIComponent(project())}`);
      const j = await r.json();
      data = j.ok ? j.data : {error: j.error || 'ошибка сервера'};
    } catch (e) { data = D.taskview || {error: String(e)}; }
    loading = false; render();
  }

  // Файл без сервера: снимок по всему портфелю, фильтр проекта — на месте.
  function view() {
    const p = project();
    if (live || !p || !data || data.error) return data;
    return Object.assign({}, data, {
      matrix: data.matrix.filter(r => r.key === p),
      window: Object.assign({}, data.window, {tasks: data.window.tasks.filter(t => t.project === p)})});
  }

  function render() {
    const host = $('taskview');
    if (!host || host.offsetParent === null) return;          // вкладка скрыта: ширину не измерить
    const d = view();
    const tabs = WINDOWS.map(([v, l]) => `<button class="btn ${v === hours ? 'on' : ''}" data-h="${v}" ${live ? '' : 'disabled'}>${l}</button>`).join('');
    const head = `<div class="section-h">Задачи — факт по данным системы</div>
      <div class="tv-bar">Окно: ${tabs}
        <span class="tv-note">${loading ? 'считаю…' : d && d.generated_at ? (live ? 'посчитано ' : 'снимок сборки ') + clock(d.generated_at) : ''}
        · из workflow, бэклога и журналов сессий, без ИИ${live ? '' : ' · окно и проект живьём — в pm.py serve'}</span></div>`;
    if (!d || d.error) { host.innerHTML = head + `<div class="tv-note">${d ? h(d.error) : 'нет данных: пересоберите дашборд'}</div>`; return; }
    const k = d.kpi, ws = d.window.summary, pct = k.points_total ? Math.round(100 * k.points_done / k.points_total) : 0;
    host.innerHTML = head + `
      <div class="kpis">
        <div class="kpi"><div class="v">${num(k.done)} / ${num(k.total)}</div><div class="l">задач сделано</div>
          <div class="n">${pct}% по размеру: ${num(k.points_done)} из ${num(k.points_total)} очков (S 1 · M 3 · L 8 · XL 20)</div></div>
        <div class="kpi"><div class="v">${num(k.working)}</div><div class="l">в работе сейчас</div><div class="n">открыта сессия роли</div></div>
        <div class="kpi"><div class="v" style="color:${k.waiting ? 'var(--warn)' : ''}">${num(k.waiting)}</div><div class="l">ждут сессии</div>
          <div class="n">запуск поставлен, сессия не открыта</div></div>
        <div class="kpi"><div class="v" style="color:${k.blocked ? 'var(--bad)' : ''}">${num(k.blocked)}</div><div class="l">заблокировано</div>
          <div class="n">в конвейере ${num(k.pipeline)}${k.cancelled ? ' · отменено ' + num(k.cancelled) : ''}</div></div>
        <div class="kpi"><div class="v">${num(ws.runs)}</div><div class="l">запусков за ${WINDOWS.find(w => w[0] === ws.hours)?.[1] || ws.hours + ' ч'}</div>
          <div class="n">задач ${num(ws.tasks)} · токенов ${tok(ws.tokens)}</div></div>
        <div class="kpi"><div class="v">${num(ws.stage_passed)}</div><div class="l">этапов сдано</div>
          <div class="n">возвратов ${num(ws.returns)} · задач закрыто ${num(ws.completed)}</div></div>
      </div>
      <div class="card-viz" id="tv-gantt"></div>
      <div class="card-viz" id="tv-wtable"></div>
      <div class="card-viz" id="tv-matrix"></div>
      <div class="pm-grid"><div class="card-viz" id="tv-done"></div><div class="card-viz" id="tv-flow"></div></div>
      <div class="card-viz" id="tv-stage"></div>`;
    host.querySelectorAll('[data-h]').forEach(b => b.onclick = () => {
      hours = +b.dataset.h; try { localStorage.setItem('tt-tv-hours', hours); } catch (e) {} load();
    });
    gantt($('tv-gantt'), d.window);
    wtable($('tv-wtable'), d.window);
    matrix($('tv-matrix'), d);
    done($('tv-done'), d);
    flow($('tv-flow'), d.flow);
    stages($('tv-stage'), d.stage_time);
  }

  /* Запуски ролей по задачам в окне: полоса — сессия роли от открытия до сдачи (цвет — этап),
     метки — события конвейера: ▲ этап сдан, ◆ задача закрыта, ✕ возврат. Справа — токены задачи. */
  function gantt(host, win) {
    const tasks = win.tasks;
    host.innerHTML = '';
    const t0 = Date.parse(win.summary.start), t1 = Date.parse(win.summary.end);
    const w = Math.max(340, host.clientWidth || 900), LW = Math.min(260, w * 0.3), RW = 74, RH = 24, HDR = 22;
    const H = HDR + Math.max(1, tasks.length) * RH + 6;
    const title = `Работа по задачам за окно — ${tasks.length ? tasks.length + ' задач' : 'нет активности'}`;
    const svg = VIZ_SVG(host, w, H, title,
      'Полоса — сессия роли: от открытия до сдачи или до сейчас (пунктир — идёт). Цвет — этап. ▲ этап сдан · ✕ возврат · ◆ задача закрыта.');
    const X = v => LW + (w - LW - RW) * Math.min(1, Math.max(0, (v - t0) / Math.max(1, t1 - t0)));
    const ticks = 6;
    for (let i = 0; i <= ticks; i++) {
      const v = t0 + (t1 - t0) * i / ticks, x = X(v);
      svg.appendChild(node('line', {x1: x, y1: HDR - 4, x2: x, y2: H, class: 'viz-grid'}));
      svg.appendChild(node('text', {x, y: HDR - 8, 'text-anchor': 'middle', class: 'viz-tick'},
        (t1 - t0 > 864e5 * 2 ? day(new Date(v).toISOString()) + ' ' : '') + clock(new Date(v).toISOString())));
    }
    const top = Math.max(1, ...tasks.map(t => t.tokens || 0));
    tasks.forEach((t, i) => {
      const y = HDR + i * RH;
      const lab = node('text', {x: LW - 8, y: y + RH / 2 + 4, 'text-anchor': 'end', class: 'viz-label'},
        (t.ref.split('/')[1] || t.ref) + ' ' + (t.title.length > 26 ? t.title.slice(0, 25) + '…' : t.title));
      tipOn(lab, `<b>${h(t.ref)}</b> ${h(t.title)}<br>этап: ${h(STAGE_TITLE[t.column] || t.column)} · пройдено ${t.progress}%`);
      svg.appendChild(lab);
      t.runs.forEach(r => {
        const x0 = X(Date.parse(r.start)), x1 = Math.max(x0 + 3, X(Date.parse(r.end)));
        const bar = node('rect', {x: x0, y: y + 6, width: x1 - x0, height: RH - 12, rx: 3, fill: VIZ.CAT[slot(r)],
          'stroke-dasharray': r.live ? '3 2' : null, stroke: r.live ? 'var(--ink)' : null, 'fill-opacity': r.status === 'cancelled' ? 0.35 : 1});
        tipOn(bar, `<b>${h(ROLE[r.role] || r.role)}</b> · ${h(STAGE_TITLE[r.stage] || r.stage || '')}<br>${h(r.id)} · ${h(r.agent || '')} · ${h(r.status)}`
          + `<br>${clock(r.start)}–${r.live ? 'идёт' : clock(r.end)} · ${hrs((Date.parse(r.end) - Date.parse(r.start)) / 36e5)}`
          + `<br>токенов: ${tok(r.tokens)}`);
        svg.appendChild(bar);
      });
      t.events.forEach(e => {
        const x = X(Date.parse(e.at));
        const sym = e.kind === 'stage_passed' ? '▲' : e.kind === 'return' ? '✕' : e.kind === 'completed' ? '◆' : '';
        if (!sym) return;
        const m = node('text', {x, y: y + RH / 2 + 4, 'text-anchor': 'middle', class: 'viz-value',
          fill: e.kind === 'return' ? 'var(--bad)' : 'var(--ok)', style: 'font-size:11px;font-weight:700'}, sym);
        tipOn(m, `<b>${e.kind === 'stage_passed' ? 'Этап сдан' : e.kind === 'return' ? 'Возврат' : 'Задача закрыта'}</b> ${clock(e.at)}`
          + (e.stage ? `<br>${h(STAGE_TITLE[e.stage] || e.stage)}${e.to_stage ? ' → ' + h(STAGE_TITLE[e.to_stage] || e.to_stage) : ''}` : ''));
        svg.appendChild(m);
      });
      const bw = (RW - 8) * (t.tokens || 0) / top;
      svg.appendChild(node('rect', {x: w - RW + 4, y: y + 8, width: Math.max(1, bw), height: RH - 16, rx: 2, fill: 'var(--q4)'}));
      svg.appendChild(node('text', {x: w - 2, y: y + RH / 2 + 4, 'text-anchor': 'end', class: 'viz-value', style: 'font-size:10.5px'}, tok(t.tokens)));
    });
    const leg = document.createElement('div'); leg.className = 'viz-legend';
    leg.innerHTML = Object.keys(STAGE_SLOT).map(s => `<span><i style="background:${VIZ.CAT[STAGE_SLOT[s]]}"></i>${STAGE_TITLE[s]}</span>`).join('')
      + `<span><i style="background:${VIZ.CAT[7]}"></i>Тимлид и Решала</span>`;
    host.appendChild(leg);
  }

  // Таблица окна: что сдвинулось по каждой задаче — проверяемо по истории конвейера.
  function wtable(host, win) {
    const rows = win.tasks.map(t => {
      const passed = t.events.filter(e => e.kind === 'stage_passed').map(e => STAGE_TITLE[e.stage] || e.stage);
      const ret = t.events.filter(e => e.kind === 'return').length;
      const roles = [...new Set(t.runs.map(r => ROLE[r.role] || r.role))].join(', ');
      return `<tr><td class="mono">${h(t.ref)}</td><td>${h(t.title)}</td>
        <td>${h(STAGE_TITLE[t.column] || (t.column === 'done' ? 'Сделано' : 'Вне конвейера'))}
          <div class="bar"><i style="width:${Math.max(2, t.progress)}%;background:${t.progress >= 70 ? 'var(--ok)' : 'var(--s1)'}"></i></div></td>
        <td>${passed.length ? 'сдано: ' + h(passed.join(', ')) : '—'}${ret ? ` · <span style="color:var(--bad)">возвратов ${ret}</span>` : ''}
          ${t.events.some(e => e.kind === 'completed') ? ' · <b>закрыта</b>' : ''}</td>
        <td>${t.runs.length}${roles ? `<div class="tv-note">${h(roles)}</div>` : ''}</td>
        <td class="num">${tok(t.tokens)}</td></tr>`;
    }).join('');
    host.innerHTML = `<div class="viz-title">Что сдвинулось за окно</div>
      <div class="viz-sub">Этап и доля пройденного конвейера (этапы до текущего и сданные роли текущего), события окна, запуски и токены по журналам сессий.</div>
      ${rows ? `<table class="pf"><thead><tr><th>Задача</th><th>Название</th><th>Где сейчас</th><th>За окно</th><th>Запусков</th><th class="num">Токенов</th></tr></thead><tbody>${rows}</tbody></table>`
        : '<div class="tv-note">В окне не было запусков ролей и событий конвейера.</div>'}`;
  }

  // Где стоят задачи: проект × этап, число задач в ячейке; 🔒 — сколько из них заблокировано.
  function matrix(host, d) {
    const rows = d.matrix.slice(0, 14);
    const max = Math.max(1, ...rows.flatMap(r => d.cols.map(c => r.counts[c.key])));
    VIZ.matrix(host, {
      title: 'Где стоят задачи — проект × этап',
      sub: 'Число задач в каждой колонке. Темнее — больше. 🔒 — из них заблокировано. «Вне конвейера» — бэклог без запуска ролей.',
      rows: rows.map(r => ({key: r.key, label: r.name.length > 24 ? r.name.slice(0, 23) + '…' : r.name, r})),
      cols: d.cols.map(c => ({key: c.key, label: c.title.replace('Деплой в ', '→ ').replace('Вне конвейера', 'Бэклог')})),
      cell: (row, col) => {
        const n = row.r.counts[col.key], b = row.r.blocked[col.key];
        return {value: n ? Math.max(8, Math.round(100 * n / max)) : null, text: n ? (b ? `${n} 🔒${b}` : String(n)) : '·',
                tip: `<b>${h(row.r.name)}</b> · ${h(col.label)}<br>задач: ${n}${b ? ', заблокировано ' + b : ''}`};
      },
      note: d.matrix.length > rows.length ? `Показаны 14 проектов с наибольшим числом задач из ${d.matrix.length}.` : null,
    });
  }

  // Сделано и не сделано по проектам — в очках размера, чтобы крупная задача весила больше мелкой.
  function done(host, d) {
    const rows = d.matrix.slice(0, 12);
    VIZ.hstack(host, {
      title: 'Сделано и не сделано — по размеру задач',
      sub: 'Очки размера: S 1 · M 3 · L 8 · XL 20. Справа — доля сделанного.',
      series: d.groups.map(g => ({label: g.title})),
      rows: rows.map(r => {
        const parts = d.groups.map(g => r.points[g.key] || 0), total = parts.reduce((a, b) => a + b, 0);
        return {label: r.name.length > 22 ? r.name.slice(0, 21) + '…' : r.name, parts,
                right: total ? Math.round(100 * (r.points.done || 0) / total) + '%' : '—'};
      }),
      fmt: v => num(v) + ' очк.',
    });
  }

  function flow(host, f) {
    VIZ.stacked(host, {
      title: 'Поток за 14 дней',
      sub: 'По дням: сдано этапов конвейера, закрыто задач, возвратов на доработку.',
      cats: f.days.map(day),
      series: [{label: 'этапов сдано', values: f.stage_passed}, {label: 'задач закрыто', values: f.completed},
               {label: 'возвратов', values: f.return}],
    });
  }

  // Время на этапах: медиана работы сессий и ожидания у сданных за 30 дней; справа — сколько стоит сейчас.
  function stages(host, st) {
    VIZ.hstack(host, {
      title: 'Время на этапах — работа и ожидание',
      sub: 'Медиана по этапам, сданным за 30 дней: работа — сессии ролей, ожидание — остальное время на этапе. Справа — сколько задач стоит на этапе сейчас и медиана их возраста.',
      series: [{label: 'работа сессий'}, {label: 'ожидание'}],
      rows: st.map(s => ({label: s.title, parts: [s.work_h, s.wait_h],
                          right: s.open ? `${s.open} стоят ~${hrs(s.open_age_h)}` : (s.passed ? `сдано ${s.passed}` : '—')})),
      fmt: hrs, rightWidth: 150,
    });
  }

  // --- мелкие помощники SVG поверх VIZ (своя диаграмма-таймлайн)
  const NS = 'http://www.w3.org/2000/svg';
  function node(name, attrs, text) {
    const n = document.createElementNS(NS, name);
    for (const k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }
  function VIZ_SVG(host, w, hgt, title, sub) {
    host.innerHTML = `<div class="viz-title">${h(title)}</div><div class="viz-sub">${h(sub)}</div>`;
    const svg = node('svg', {viewBox: `0 0 ${w} ${hgt}`, role: 'img'});
    host.appendChild(svg);
    return svg;
  }
  let tip;
  function tipOn(n, html) {
    if (!tip) { tip = document.createElement('div'); tip.className = 'viz-tip'; document.body.appendChild(tip); }
    n.addEventListener('pointerenter', e => { tip.innerHTML = html; tip.classList.add('on'); move(e); });
    n.addEventListener('pointermove', move);
    n.addEventListener('pointerleave', () => tip.classList.remove('on'));
    function move(e) {
      const x = e.clientX + 14 + tip.offsetWidth > innerWidth ? e.clientX - tip.offsetWidth - 14 : e.clientX + 14;
      tip.style.left = Math.max(8, x) + 'px'; tip.style.top = Math.max(8, e.clientY + 14) + 'px';
    }
  }

  function shown() { if (data) render(); else load(); }
  addEventListener('DOMContentLoaded', () => {
    const sel = $('proj-select');
    if (sel) sel.addEventListener('change', () => (live ? load() : render()));
    const tab = $('tab-dash');
    if (tab) tab.addEventListener('click', () => setTimeout(shown, 0));
    setTimeout(shown, 0);
    if (live) setInterval(() => { if (!document.hidden && $('taskview')?.offsetParent) load(); }, 60000);
  });
  let rsz;
  addEventListener('resize', () => { clearTimeout(rsz); rsz = setTimeout(render, 160); });
  return {load, render, shown};
})();
