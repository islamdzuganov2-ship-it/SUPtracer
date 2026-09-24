/* Рендер дашборда. Две вкладки: диаграммы и полная информация по проектам.
   Все цифры приходят из data/*.json — здесь только отображение. */

const D = JSON.parse(document.getElementById('DATA').textContent);
const P = D.projects.projects, U = D.usage.projects, R = D.repos.repos;
const E = D.estimates.projects, C = D.competitors.projects;
const Q = (D.quality && D.quality.projects) || {};
const SIG = (D.quality && D.quality.portfolio_signals) || [];
const SN = D.snapshots || [];

const nf = n => (n == null || isNaN(n)) ? '—' : Math.round(n).toLocaleString('ru-RU').replace(/ /g, ' ');
const money = n => (n == null || isNaN(n)) ? '—' : '$' + Math.round(n).toLocaleString('ru-RU').replace(/ /g, ' ');
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const tok = n => n >= 1e9 ? (n / 1e9).toFixed(2).replace('.', ',') + ' млрд'
  : n >= 1e6 ? (n / 1e6).toFixed(0) + ' млн' : nf(n);
const hrs = n => (n == null) ? '—' : (n >= 10 ? Math.round(n) : n.toFixed(1).replace('.', ',')) + ' ч';

// usage.json строится по имени каталога — сопоставляем с ключом проекта.
function usageOf(key) {
  const path = (P[key].path || '').toLowerCase();
  const base = path.replace(/\/+$/, '').split('/').pop();
  for (const c of [key, key.toLowerCase(), base, 'doc:' + base, 'doc:' + base.replace(/_/g, '-')])
    if (U[c]) return U[c];
  return null;
}
const keys = Object.keys(P);
const own = keys.filter(k => P[k].tier < 4);          // свои проекты, без чужих инструментов
const named = k => P[k].name;
const byMoney = ks => ks.slice().sort((a, b) => (usageOf(b)?.usd || 0) - (usageOf(a)?.usd || 0));

function color(p) { return p >= 70 ? 'var(--ok)' : p >= 40 ? 'var(--warn)' : 'var(--bad)'; }
function bar(p) { return `<div class="bar"><i style="width:${Math.max(2, p)}%;background:${color(p)}"></i></div>`; }

/* ======================= шапка и KPI ======================= */
let spent = 0, spentTok = 0, rem = 0, remTok = 0, disk = 0, loc = 0, tasks = 0, sess = 0, hours = 0;
keys.forEach(k => {
  const u = usageOf(k);
  if (u) { spent += u.usd; spentTok += u.billable; sess += u.sessions; hours += (u.active_hours || 0); }
  const e = E[k]; if (e) { rem += e.remaining_usd; remTok += e.remaining_tokens; tasks += e.items.length; }
  const r = R[k]; if (r && r.exists) { disk += r.disk_bytes || 0; loc += (r.code && r.code.loc_total) || 0; }
});
const months = [...new Set(keys.flatMap(k => Object.keys(usageOf(k)?.by_month || {})))]
  .filter(m => m !== 'unknown').sort();
const calDays = (D.usage.total && D.usage.total.calendar_days) || 0;

document.getElementById('sub').textContent =
  `${own.length} собственных проектов · ${Math.round(hours)} ч работы за ${calDays} дней · данные собраны ` +
  D.generated_at.slice(0, 16).replace('T', ' ') + ' UTC';

document.getElementById('kpis').innerHTML = [
  ['Потрачено', money(spent), tok(spentTok) + ' токенов · ' + sess + ' сессий'],
  ['Осталось', money(rem), tok(remTok) + ' токенов · ' + tasks + ' задач'],
  ['Время', Math.round(hours) + ' ч', calDays + ' рабочих дней · ' + (hours / Math.max(1, calDays)).toFixed(1).replace('.', ',') + ' ч в день'],
  ['В месяц', money(months.length ? spent / months.length : 0), months.length + ' мес. · ориентир отрасли $150–250'],
  ['Кода', nf(loc), 'строк в ' + own.length + ' проектах'],
  ['На диске', (disk / 1e9).toFixed(1).replace('.', ',') + ' ГБ', 'свободно на C: — 27 ГБ'],
].map(([l, v, n]) => `<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div><div class="n">${n}</div></div>`).join('');

/* ======================= портфельные сигналы ======================= */
const LV = {act: 'действовать', watch: 'следить', ok: 'норма'};
document.getElementById('signals').innerHTML = SIG.map(s => `
  <div class="sig ${s.level}">
    <div class="hd"><span class="nm">${esc(s.title)}</span><span class="lv ${s.level}">${LV[s.level] || s.level}</span></div>
    <div class="val">${esc(s.value)}</div>
    <div class="what">${esc(s.what)}</div>
    <div class="why">${esc(s.why)}</div>
  </div>`).join('');

/* ======================= диаграммы ======================= */
// Ширина диаграмм берётся у контейнера, поэтому при изменении размера окна
// их надо перерисовать — иначе текст поедет вместе с масштабом viewBox.
const DIMS = [
  {key: 'functional', label: 'Функция'},
  {key: 'production', label: 'Эксплуат.'},
  {key: 'docs', label: 'Документы'},
  {key: 'tests', label: 'Тесты'},
  {key: 'security', label: 'Безопасн.'},
];

function renderCharts() {
const readyOrder = own.slice().sort((a, b) =>
  ((P[b].readiness || {}).functional || 0) - ((P[a].readiness || {}).functional || 0));

VIZ.matrix(document.getElementById('c-ready'), {
  title: 'Готовность по пяти измерениям',
  sub: 'Насыщенность тона — величина. Число напечатано в каждой ячейке, поэтому цвет здесь помогает, а не несёт смысл в одиночку.',
  rows: readyOrder.map(k => ({label: named(k), key: k})),
  cols: DIMS,
  cell: (r, c) => {
    const v = (P[r.key].readiness || {})[c.key];
    return {value: v == null ? null : v, text: v == null ? '—' : v + '',
      tip: `<b>${esc(named(r.key))}</b> · ${c.label}: ${v == null ? '—' : v + '%'}<br>` +
           `<span style="color:var(--ink3)">${esc(((P[r.key].readiness || {}).basis || '').slice(0, 180))}</span>`};
  },
  note: 'Разрыв между «Функция» и «Эксплуат.» в одной строке — главное, что здесь стоит искать: система умеет много, но её нельзя включить. Закрывается не функциями, а поставкой.',
});

const progRows = byMoney(own).filter(k => usageOf(k) || (E[k] || {}).remaining_usd);
VIZ.hstack(document.getElementById('c-progress'), {
  title: 'Вложено и осталось, в деньгах',
  sub: 'Длина всей полосы — полная стоимость проекта до закрытия бэклога. Доля синего — сколько пути пройдено.',
  rows: progRows.map(k => {
    const u = usageOf(k), e = E[k] || {};
    const a = u ? u.usd : 0, b = e.remaining_usd || 0;
    return {label: named(k), parts: [a, b], right: Math.round(100 * a / Math.max(1, a + b)) + '%'};
  }),
  series: [{label: 'вложено'}, {label: 'осталось по бэклогу'}],
  fmt: money,
  note: 'Процент справа — доля вложенного от полной стоимости, а не оценка готовности. Там, где он меньше 50%, проект пройден меньше чем наполовину, что бы ни показывала самооценка.',
});

const costRows = own.map(k => {
  const e = (Q[k] || {}).economics || {};
  return {k, v: e.usd_per_readiness_point};
}).filter(x => x.v != null).sort((a, b) => b.v - a.v);
VIZ.bars(document.getElementById('c-costpoint'), {
  title: 'Цена одного пункта готовности',
  sub: 'Сколько стоил каждый процент функциональной готовности. Сравнимо между проектами любого размера.',
  rows: costRows.map(x => ({
    label: named(x.k), value: x.v,
    tip: `<b>${esc(named(x.k))}</b><br>$${x.v} за пункт готовности<br>` +
         `<span style="color:var(--ink3)">${money(usageOf(x.k)?.usd)} на ${(P[x.k].readiness || {}).functional}% готовности</span>`,
  })),
  fmt: v => '$' + nf(v),
  note: 'Растущая цена пункта — первый признак, что проект входит в болото: работа идёт, а готовность почти не двигается. Заметно задолго до срыва сроков.',
});

const hourRows = own.map(k => ({k, v: (usageOf(k) || {}).active_hours || 0}))
  .filter(x => x.v > 0).sort((a, b) => b.v - a.v);
VIZ.bars(document.getElementById('c-hours'), {
  title: 'Затраченное время по проектам',
  sub: 'Активная работа из журналов сессий: сумма промежутков между ответами, паузы длиннее 15 минут отброшены как перерыв.',
  rows: hourRows.map(x => {
    const u = usageOf(x.k);
    return {label: named(x.k), value: x.v,
      tip: `<b>${esc(named(x.k))}</b><br>${hrs(x.v)} активной работы<br>` +
           `${u.calendar_days} календарных дней, ${u.sessions} сессий<br>` +
           `<span style="color:var(--ink3)">${hrs(u.hours_per_day)} в день, когда работа шла</span>`};
  }),
  fmt: hrs,
  note: 'Это время работы ассистента, а не ваше. Оно занижено там, где вы думали над задачей молча, и завышено там, где сессия висела открытой внутри 15-минутного окна.',
});

const intRows = own.map(k => {
  const u = usageOf(k), e = (Q[k] || {}).economics || {};
  return u && e.usd_per_hour != null ? {k, v: e.usd_per_hour, u} : null;
}).filter(Boolean).sort((a, b) => b.v - a.v);
VIZ.bars(document.getElementById('c-intensity'), {
  title: 'Интенсивность: стоимость часа работы',
  sub: 'Сколько стоил час активной работы над проектом.',
  rows: intRows.map(x => ({
    label: named(x.k), value: x.v,
    tip: `<b>${esc(named(x.k))}</b><br>$${x.v} за час<br>` +
         `<span style="color:var(--ink3)">${tok((Q[x.k].economics || {}).tokens_per_hour || 0)} токенов в час</span>`,
  })),
  fmt: v => '$' + v.toFixed(0),
  note: 'Дорогой час — это не плохо само по себе: так выглядит работа с большим контекстом. Плохо, когда дорогой час сочетается с дорогим пунктом готовности — значит платим за объём, а не за продвижение.',
});

/* --- ТЗ: количество против качества --- */
const specPts = own.map(k => {
  const s = (R[k] || {}).spec || {};
  return s.requirements ? {k, x: s.requirements, y: s.traceability_pct || 0, s} : null;
}).filter(Boolean);
if (specPts.length) {
  VIZ.scatter(document.getElementById('c-spec'), {
    title: 'Техническое задание: количество требований и их связь с кодом',
    sub: 'По горизонтали — сколько требований с кодами найдено в документах. По вертикали — какая их доля встречается в коде.',
    points: specPts.map(p => ({
      label: named(p.k), x: p.x, y: p.y,
      tip: `<b>${esc(named(p.k))}</b><br>${p.x} требований в ${(p.s.requirement_families || []).length} схемах нумерации<br>` +
           `${p.y}% встречается в коде (${p.s.traced_in_code})<br>` +
           `<span style="color:var(--ink3)">${p.s.doc_files} документов, ${nf(p.s.doc_lines)} строк</span>`,
    })),
    xLabel: 'требований в документах', yLabel: 'трассируемость, %',
    fmtX: v => nf(v), fmtY: v => Math.round(v) + '%',
    note: 'Правый нижний угол — опасное место: требований написано много, но в коде их нет. Это значит, что по такому ТЗ нельзя принимать работу: непонятно, что реализовано, а что только описано.',
  });
} else {
  document.getElementById('c-spec').closest('.card-viz').remove();
}

const specIdx = own.map(k => ({k, v: (Q[k] || {}).spec && Q[k].spec.score}))
  .filter(x => x.v != null).sort((a, b) => b.v - a.v);
VIZ.bars(document.getElementById('c-specidx'), {
  title: 'Индекс качества ТЗ',
  sub: 'Не объём документации, а её формализация и связь с кодом. Наведите курсор — видно, из чего складывается цифра.',
  rows: specIdx.map(x => ({
    label: named(x.k), value: x.v,
    tip: `<b>${esc(named(x.k))}</b> — ${x.v}<br>` + (Q[x.k].spec.components || [])
      .map(c => `${esc(c.label)}: ${c.score == null ? '—' : Math.round(c.score * 100) + '%'} <span style="color:var(--ink3)">(${esc(c.note)})</span>`)
      .join('<br>'),
  })),
  fmt: v => String(v), max: 100,
  note: 'Веса: трассируемость 2.5, формализация и дисциплина по 1.5, остальное по 1.0. Компонента без данных выбывает из знаменателя, а не обнуляет индекс.',
});

const worstSpec = specIdx.length ? Q[specIdx[specIdx.length - 1].k].spec : null;
renderParts('c-specparts', 'Что роняет индекс ТЗ',
  'Разбор по компонентам для проекта с самым слабым результатом.',
  specIdx.length ? named(specIdx[specIdx.length - 1].k) : '', worstSpec);

const engIdx = own.map(k => ({k, v: (Q[k] || {}).engineering && Q[k].engineering.score}))
  .filter(x => x.v != null).sort((a, b) => b.v - a.v);
VIZ.bars(document.getElementById('c-eng'), {
  title: 'Индекс качества проработки',
  sub: 'То, что отличает продукт от наброска: тесты, версионирование, гигиена поставки, чистота кода.',
  rows: engIdx.map(x => ({
    label: named(x.k), value: x.v,
    tip: `<b>${esc(named(x.k))}</b> — ${x.v}<br>` + (Q[x.k].engineering.components || [])
      .map(c => `${esc(c.label)}: ${c.score == null ? '—' : Math.round(c.score * 100) + '%'} <span style="color:var(--ink3)">(${esc(c.note)})</span>`)
      .join('<br>'),
  })),
  fmt: v => String(v), max: 100,
  note: 'Отсутствие git — единственная компонента, которая обнуляется жёстко: работа существует в одном экземпляре, и это не степень качества, а отложенная потеря.',
});

const worstEng = engIdx.length ? Q[engIdx[engIdx.length - 1].k].engineering : null;
renderParts('c-engparts', 'Что роняет качество проработки',
  'Разбор по компонентам для проекта с самым слабым результатом.',
  engIdx.length ? named(engIdx[engIdx.length - 1].k) : '', worstEng);

function renderParts(id, title, sub, projName, blk) {
  const host = document.getElementById(id);
  if (!blk) { host.remove(); return; }
  host.innerHTML = `<div class="viz-title">${esc(title)}</div>
    <div class="viz-sub">${esc(sub)} Сейчас это «${esc(projName)}» — ${blk.score}.</div>` +
    (blk.components || []).map(c => `
      <div class="qbreak">
        <span class="qn">${esc(c.label)}</span>
        <span class="qb"><i style="width:${Math.round((c.score || 0) * 100)}%"></i></span>
        <span class="qv">${c.score == null ? '—' : Math.round(c.score * 100) + '%'}</span>
      </div>
      <div style="font-size:11.5px;color:var(--ink3);margin:-2px 0 8px 0">${esc(c.note)} · вес ${c.weight}</div>`).join('');
}

/* --- расход по месяцам --- */
const topSpend = byMoney(keys.filter(k => (usageOf(k)?.usd || 0) > 0));
const namedS = topSpend.slice(0, 4), restS = topSpend.slice(4);
const series = namedS.map(k => ({
  label: named(k), values: months.map(m => (usageOf(k).by_month[m] || {}).usd || 0)}));
if (restS.length) series.push({
  label: 'прочее', values: months.map(m => restS.reduce((s, k) => s + ((usageOf(k).by_month[m] || {}).usd || 0), 0))});
VIZ.stacked(document.getElementById('c-months'), {
  title: 'Расход по месяцам',
  sub: 'Реальная история из журналов сессий. Пятый и далее проекты свёрнуты в «прочее» — девятого цвета не бывает.',
  cats: months, series, fmt: money,
  note: SN.length >= 2
    ? `Снимков накоплено ${SN.length}, первый — ${esc(SN[0].date)}. Изменения готовности между снимками видны в карточках проектов.`
    : `Снимков пока ${SN.length}. Они копятся при каждом обновлении и лягут в основу сравнения «было — стало».`,
});

}
renderCharts();
let rsz;
addEventListener('resize', () => { clearTimeout(rsz); rsz = setTimeout(renderCharts, 160); });

/* ======================= вкладка «Проекты» ======================= */
const alerts = [];
keys.forEach(k => {
  const p = P[k], r = R[k] || {};
  if (p.tier >= 4) return;
  if (r.exists && !r.git && (r.code?.loc_total || 0) > 500) {
    const gb = r.disk_bytes / 1e9;
    alerts.push(['crit', `<b>${esc(p.name)}</b> — нет git. ${nf(r.code.loc_total)} строк кода${gb >= 0.5 ? ` и ${gb.toFixed(1).replace('.', ',')} ГБ данных` : ''} существуют в одном экземпляре: истории нет, откатиться некуда.`]);
  }
  (p.security?.open_critical || []).forEach(s =>
    alerts.push(['crit', `<b>${esc(p.name)}</b> — ${esc(s.split('—')[0].trim())}: ${esc(s.split('—').slice(1).join('—').trim())}`]));
  (r.secrets_suspect || []).filter(s => !s.file.includes('.claude/worktrees')).forEach(s =>
    alerts.push(['warn', `<b>${esc(p.name)}</b> — возможный секрет в коде: <span class="mono">${esc(s.file)}</span> (${esc(s.kind)})`]));
  (r.weird_paths || []).forEach(w =>
    alerts.push(['warn', `<b>${esc(p.name)}</b> — мусорный файл с зарезервированным именем Windows: <span class="mono">${esc(w)}</span>`]));
  if (r.exists && r.hygiene && !r.hygiene.ci && (r.code?.loc_total || 0) > 20000)
    alerts.push(['warn', `<b>${esc(p.name)}</b> — ${nf(r.code.loc_total)} строк кода без CI.`]);
  if (r.git && r.git.dirty_files > 0)
    alerts.push(['warn', `<b>${esc(p.name)}</b> — ${r.git.dirty_files} незакоммиченных файлов в рабочем дереве.`]);
});
if (alerts.length) document.getElementById('alertsWrap').innerHTML =
  `<div class="section-h">Требует внимания — ${alerts.length}</div><div class="alerts">` +
  alerts.sort((a, b) => a[0] === 'crit' ? -1 : 1).map(([s, t]) =>
    `<div class="alert"><span class="sev sev-${s}">${s === 'crit' ? 'КРИТ' : 'ВНИМ'}</span><div class="txt">${t}</div></div>`).join('') + `</div>`;

const order = keys.slice().sort((a, b) => (P[a].tier - P[b].tier) || ((usageOf(b)?.usd || 0) - (usageOf(a)?.usd || 0)));
document.getElementById('tableWrap').innerHTML = `<table class="pf"><thead><tr>
<th>Проект</th><th>Цель</th><th>Готовность</th><th>ИБ</th><th class="num">Часов</th><th class="num">Потрачено</th>
<th class="num">Осталось</th><th class="num">ТЗ</th><th class="num">Инж.</th><th class="num">Коммитов</th><th>Последнее</th>
</tr></thead><tbody>` + order.map(k => {
  const p = P[k], u = usageOf(k), r = R[k] || {}, e = E[k] || {}, rd = p.readiness || {}, g = r.git || {}, q = Q[k] || {};
  return `<tr data-k="${k}">
   <td><div class="pname">${esc(p.name)}</div><div class="ptag">${esc(p.status)}</div></td>
   <td>${esc(p.goal || '')}</td>
   <td><span class="pct">${rd.functional ?? '—'}%</span>${bar(rd.functional || 0)}</td>
   <td><span class="chip ${(rd.security || 0) >= 70 ? 'ok' : (rd.security || 0) >= 40 ? 'warn' : 'bad'}">${rd.security ?? '—'}%</span></td>
   <td class="num">${u ? hrs(u.active_hours) : '—'}</td>
   <td class="num">${u ? money(u.usd) : '—'}</td>
   <td class="num">${e.remaining_usd ? money(e.remaining_usd) : '—'}</td>
   <td class="num">${q.spec?.score ?? '—'}</td>
   <td class="num">${q.engineering?.score ?? '—'}</td>
   <td class="num">${g.commits != null ? g.commits : '<span style="color:var(--bad)">нет git</span>'}</td>
   <td class="mono" style="color:var(--ink3)">${esc(g.last_commit || (u ? u.last_seen.slice(0, 10) : '—'))}</td>
  </tr>`;
}).join('') + `</tbody></table>`;

const list = (arr, cls) => !arr || !arr.length ? '' :
  `<ul class="tight ${cls || ''}">` + arr.map(x => `<li>${esc(x)}</li>`).join('') + `</ul>`;

const LBL = {style:'Стиль',backend:'Бэкенд',frontend:'Фронтенд',stack:'Стек',modules:'Модули',packages:'Пакеты',
  facts:'Факты',rbac:'Доступ',guard:'Сверка',docs:'Документы',data:'Данные',deploy:'Публикация',state:'Состояние',
  notes:'Заметки',quality_gate:'Гейт качества',device_profile:'Профиль устройства',where:'Где',containers:'Контейнеры',
  limits:'Лимиты',gpu:'GPU',disk:'Диск',target:'Целевое',runtime:'Рантайм',active_model:'Модель',catalog:'Каталог',
  pipeline:'Конвейер',role:'Роль',models:'Модели',note:'Примечание'};

document.getElementById('cards').innerHTML = order.map(k => {
  const p = P[k], u = usageOf(k), r = R[k] || {}, e = E[k] || {}, c = C[k] || {};
  const rd = p.readiness || {}, g = r.git || {}, q = Q[k] || {};
  const sec = p.security || {}, ar = p.architecture || {}, cm = p.compute || {}, llm = p.llm || {}, sp = r.spec || {};

  const readRows = DIMS.map(d => `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <div style="width:112px;color:var(--ink3);font-size:12.5px">${d.label}</div>
      <div style="flex:1">${bar(rd[d.key] || 0)}</div>
      <div class="pct" style="width:38px;text-align:right">${rd[d.key] ?? '—'}%</div></div>`).join('');

  const backlog = (e.items || []).length ? `<table class="bl"><thead><tr>
    <th>#</th><th>Задача</th><th>Разм.</th><th>Статус</th><th style="text-align:right">Токенов</th><th style="text-align:right">$</th>
    </tr></thead><tbody>` + e.items.map(b => `<tr>
      <td class="mono" style="white-space:nowrap">${esc(b.priority)} ${esc(b.id)}</td>
      <td>${esc(b.title)}${b.why ? `<div style="color:var(--ink3);font-size:12px;margin-top:3px">${esc(b.why)}</div>` : ''}</td>
      <td><span class="size ${b.size}">${b.size}</span></td>
      <td><span class="chip ${b.blocked ? 'bad' : b.status === 'done' ? 'ok' : 'idle'}">${esc(b.status || '—')}</span>${b.days ? `<div style="color:var(--ink3);font-size:11.5px;margin-top:3px">${esc(b.days)}</div>` : ''}</td>
      <td class="num">${tok(b.est_tokens)}</td><td class="num">${money(b.est_usd)}</td></tr>`).join('') +
    `</tbody></table><div style="margin-top:9px;font-size:12.5px;color:var(--ink3)">
      Итого по бэклогу: <b style="color:var(--ink)">${tok(e.remaining_tokens)} токенов · ${money(e.remaining_usd)}</b>.
      Оценка по ${esc(e.rate_basis)} — ${nf(e.tokens_per_commit)} токенов (${money(e.usd_per_commit)}) на коммит.</div>`
    : '<p style="color:var(--ink3)">Бэклог пуст.</p>';

  const secOpen = [].concat(sec.open_critical || [], sec.open_high || [], sec.open || []);
  const competitors = (c.items || []).map(it => `<div class="comp">
      <div class="nm">${esc(it.name)}${it.url ? ` <a href="${esc(it.url)}" target="_blank" rel="noopener" style="font-weight:400;font-size:12px">↗</a>` : ''}</div>
      <div class="ln">${esc(it.what)}</div>
      <div class="ln"><b>Сильная сторона:</b> ${esc(it.strength)}</div>
      <div class="ln"><b>Чего не делает:</b> ${esc(it.gap)}</div>
      ${it.note ? `<div class="ln" style="color:var(--ink3)">${esc(it.note)}</div>` : ''}</div>`).join('');

  const qBlock = (name, blk) => !blk || blk.score == null ? '' : `
    <div style="margin-bottom:12px"><b style="font-size:12.5px">${name} — ${blk.score}</b>` +
    (blk.components || []).map(cc => `
      <div class="qbreak"><span class="qn">${esc(cc.label)}</span>
      <span class="qb"><i style="width:${Math.round((cc.score || 0) * 100)}%"></i></span>
      <span class="qv">${cc.score == null ? '—' : Math.round(cc.score * 100) + '%'}</span></div>
      <div style="font-size:11.5px;color:var(--ink3);margin:-2px 0 6px 0">${esc(cc.note)}</div>`).join('') + '</div>';

  return `<details class="card" id="p-${k}"><summary>
    <span class="arrow">▶</span><h2>${esc(p.name)}</h2>
    <span class="tag">${esc(p.tagline)}</span>
    <span class="chip">${esc(p.goal || '')}</span>
    <span class="chip ${rd.functional >= 70 ? 'ok' : rd.functional >= 40 ? 'warn' : 'bad'}">${rd.functional ?? '—'}% готово</span>
    ${u ? `<span class="chip">${money(u.usd)} · ${hrs(u.active_hours)}</span>` : ''}
  </summary><div class="body">

  <div class="grid2">
    <div class="block"><h3>Цель</h3><p>${esc(p.goal_long || '')}</p></div>
    <div class="block"><h3>Готовность</h3>${readRows}
      <div style="color:var(--ink3);font-size:12px;margin-top:7px">${esc(rd.basis || '')}</div></div>
  </div>

  <div class="block"><h3>Что сделано</h3>${list(p.done)}</div>

  <div class="grid2">
    <div class="block"><h3>Архитектура</h3><dl class="kv">
      ${Object.entries(ar).map(([kk, v]) => `<dt>${esc(LBL[kk] || kk)}</dt><dd>${esc(v)}</dd>`).join('')}</dl></div>
    <div class="block"><h3>Мощности и LLM</h3><dl class="kv">
      ${Object.entries(cm).map(([kk, v]) => `<dt>${esc(LBL[kk] || kk)}</dt><dd>${esc(v)}</dd>`).join('')}
      ${llm.used ? Object.entries(llm).filter(([kk]) => kk !== 'used').map(([kk, v]) => `<dt>${esc(LBL[kk] || kk)}</dt><dd>${esc(v)}</dd>`).join('')
        : `<dt>LLM</dt><dd>не используется${llm.note ? ' — ' + esc(llm.note) : ''}</dd>`}</dl></div>
  </div>

  <div class="block"><h3>Безопасность</h3>
    <div class="note" style="margin-bottom:10px">${esc(sec.verdict || '')}</div>
    ${secOpen.length ? `<div>${secOpen.map(s => `<div class="sec-item">${esc(s)}</div>`).join('')}</div>` : ''}
    ${sec.open_medium_low ? `<div style="margin-top:9px;color:var(--ink2);font-size:13px">${esc(sec.open_medium_low)}</div>` : ''}
    ${sec.good && sec.good !== '—' ? `<div style="margin-top:11px"><b style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--ok)">Сделано правильно</b><div style="font-size:13.5px;margin-top:4px">${esc(sec.good)}</div></div>` : ''}
    ${sec.audit ? `<div style="margin-top:9px;color:var(--ink3);font-size:12.5px">Источник: <span class="mono">${esc(sec.audit)}</span></div>` : ''}
  </div>

  <div class="block"><h3>Бэклог и оценка затрат</h3>${backlog}</div>
  <div class="block"><h3>Что делать дальше</h3>${list(p.next_actions)}</div>

  ${(q.spec || q.engineering) ? `<div class="block"><h3>Индексы качества</h3>
    ${qBlock('Качество ТЗ', q.spec)}${qBlock('Качество проработки', q.engineering)}</div>` : ''}

  <div class="block"><h3>Технический срез</h3><dl class="kv">
    <dt>Строк кода</dt><dd>${r.code ? nf(r.code.loc_total) : '—'}${r.code && Object.keys(r.code.loc).length ? ' — ' + Object.entries(r.code.loc).slice(0, 4).map(([l, n]) => `${l} ${nf(n)}`).join(', ') : ''}</dd>
    <dt>Файлов / тестовых</dt><dd>${r.code ? nf(r.code.files_total) + ' / ' + r.code.test_files : '—'}</dd>
    <dt>Документация</dt><dd>${sp.doc_files != null ? `${sp.doc_files} файлов, ${nf(sp.doc_lines)} строк, ${nf(sp.headings)} заголовков` : '—'}</dd>
    <dt>Требования</dt><dd>${sp.requirements ? `${sp.requirements} кодов в ${(sp.requirement_families || []).length} схемах; в коде встречается ${sp.traced_in_code} (${sp.traceability_pct}%)` : 'схем нумерации не найдено'}</dd>
    <dt>Дисциплина документирования</dt><dd>${sp.doc_discipline_pct != null ? `${sp.doc_discipline_pct}% коммитов с кодом трогали и документацию (${sp.commits_with_docs} из ${sp.code_commits})` : '—'}</dd>
    <dt>Открытых вопросов в доках</dt><dd>${sp.open_questions ?? '—'}</dd>
    <dt>TODO/FIXME</dt><dd>${r.code ? r.code.todo_markers : '—'}</dd>
    <dt>Git</dt><dd>${g.commits != null ? `${g.commits} коммитов, ${g.branches} веток, ветка <span class="mono">${esc(g.branch)}</span>, ${g.first_commit} → ${g.last_commit}${g.dirty_files ? `, <b style="color:var(--warn)">${g.dirty_files} незакоммичено</b>` : ''}${g.remote ? `<br><span class="mono" style="color:var(--ink3)">${esc(g.remote)}</span>` : ''}` : '<b style="color:var(--bad)">репозитория нет</b>'}</dd>
    ${g.last_subject ? `<dt>Последний коммит</dt><dd>${esc(g.last_subject)}</dd>` : ''}
    <dt>На диске</dt><dd>${r.disk_bytes ? (r.disk_bytes / 1e9).toFixed(2).replace('.', ',') + ' ГБ' : '—'}</dd>
    <dt>Гигиена</dt><dd>${r.hygiene ? Object.entries({gitignore:'.gitignore',readme:'README',license:'LICENSE',ci:'CI',docker:'Docker',lockfile:'lock-файл',env_example:'.env.example',claude_md:'CLAUDE.md'}).map(([f, l]) => `<span class="chip ${r.hygiene[f] ? 'ok' : 'idle'}" style="margin:0 3px 3px 0">${l}${r.hygiene[f] ? '' : ' нет'}</span>`).join('') : '—'}</dd>
    ${u ? `<dt>Время</dt><dd>${hrs(u.active_hours)} активной работы, ${u.calendar_days} дней, ${u.sessions} сессий, ${hrs(u.hours_per_day)} в день</dd>
    <dt>Токены</dt><dd class="mono">вход ${nf(u.input)} · выход ${nf(u.output)} · запись кеша ${nf(u.cache_write)} · чтение кеша ${nf(u.cache_read)} · размышление ${nf(u.thinking)}</dd>
    <dt>Модели</dt><dd class="mono">${Object.entries(u.by_model).filter(([m]) => !m.startsWith('<')).map(([m, v]) => `${m} — ${nf(v.requests)} запр., ${money(v.usd)}`).join(' · ')}</dd>
    <dt>По месяцам</dt><dd class="mono">${Object.entries(u.by_month).map(([m, v]) => `${m}: ${money(v.usd)}`).join(' · ')}</dd>` : ''}
  </dl></div>

  ${(c.items || []).length ? `<div class="block"><h3>Конкуренты</h3>
    ${c.market ? `<div class="note" style="margin-bottom:11px">${esc(c.market)}</div>` : ''}
    ${competitors}
    ${(c.advantage || []).length ? `<div style="margin-top:13px"><b style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--ok)">В чём этот проект превосходит</b>${list(c.advantage)}</div>` : ''}
    ${(c.threats || []).length ? `<div style="margin-top:11px"><b style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--bad)">Чем они сильнее</b>${list(c.threats)}</div>` : ''}
    ${c.benchmark ? `<div class="note" style="margin-top:11px">${esc(c.benchmark)}</div>` : ''}
    ${c.verdict ? `<div class="verdict" style="margin-top:11px">${esc(c.verdict)}</div>` : ''}</div>` : ''}

  ${p.assessment && p.assessment.verdict !== '—' ? `<div class="block"><h3>Оценка</h3>
    ${p.assessment.strength && p.assessment.strength !== '—' ? `<p><b>Сильно.</b> ${esc(p.assessment.strength)}</p>` : ''}
    ${p.assessment.weakness && p.assessment.weakness !== '—' ? `<p><b>Слабо.</b> ${esc(p.assessment.weakness)}</p>` : ''}
    <div class="verdict">${esc(p.assessment.verdict)}</div></div>` : ''}
  </div></details>`;
}).join('');

document.querySelectorAll('table.pf tbody tr').forEach(tr => tr.onclick = () => {
  const d = document.getElementById('p-' + tr.dataset.k);
  d.open = true;
  d.scrollIntoView({behavior: 'smooth', block: 'start'});
});

const CM = D.comments.comments || [];
document.getElementById('commentsWrap').innerHTML = `<div class="section-h">Комментарии — ${CM.length}</div>` +
 (CM.length ? `<div class="alerts" style="border-left-color:var(--accent)">` + CM.map(c => `<div class="alert">
    <span class="sev ${c.status === 'processed' ? 'sev-warn' : 'sev-crit'}" style="${c.status === 'processed' ? 'background:rgba(12,163,12,.13);color:var(--ok)' : ''}">${c.status === 'processed' ? 'В БЭКЛОГЕ' : 'НОВЫЙ'}</span>
    <div class="txt">${esc(c.text)}<div class="who">${esc(c.project || 'портфель')} · ${esc(c.kind || 'комментарий')} · ${esc(c.date || '')}${c.moved_to ? ' → ' + esc(c.moved_to) : ''}</div></div></div>`).join('') + `</div>`
  : `<div class="note">Комментариев нет. Добавить: <code>py tools/pm.py comment &lt;проект&gt; "текст"</code> — при следующем обновлении он станет задачей бэклога.</div>`);

document.getElementById('foot').innerHTML =
  `Данные: <code>data/projects.json</code> (ручной источник истины) · <code>usage.json</code> (${nf(D.usage.scan.deduped_rows)} ответов из ${D.usage.scan.files} журналов) · ` +
  `<code>repos.json</code> · <code>estimates.json</code> · <code>quality.json</code> · <code>competitors.json</code> · <code>comments.json</code>.<br>` +
  `Обновление: <code>py tools/pm.py refresh</code>. Конкуренты исследованы ${esc(D.competitors.researched)}. ` +
  `Деньги — эквивалент по прайсу Anthropic API, а не счёт подписки. Время — активная работа ассистента из журналов сессий, паузы длиннее 15 минут отброшены.`;

/* ======================= вкладки и тема ======================= */
function showTab(which) {
  const isDash = which !== 'proj';
  document.getElementById('page-dash').hidden = !isDash;
  document.getElementById('page-proj').hidden = isDash;
  document.getElementById('tab-dash').setAttribute('aria-selected', String(isDash));
  document.getElementById('tab-proj').setAttribute('aria-selected', String(!isDash));
  try { history.replaceState(null, '', '#' + (isDash ? 'dash' : 'proj')); } catch (e) {}
}
document.getElementById('tab-dash').onclick = () => showTab('dash');
document.getElementById('tab-proj').onclick = () => showTab('proj');
showTab(location.hash === '#proj' ? 'proj' : 'dash');

const t = document.getElementById('theme');
t.onclick = () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : cur === 'light' ? 'dark'
    : (matchMedia('(prefers-color-scheme: dark)').matches ? 'light' : 'dark');
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('tt-theme', next); } catch (e) {}
};
try { const s = localStorage.getItem('tt-theme'); if (s) document.documentElement.setAttribute('data-theme', s); } catch (e) {}
