/* Рендер дашборда. Две вкладки: диаграммы и полная информация по проектам.
   Все цифры приходят из data/*.json — здесь только отображение. */

const D = JSON.parse(document.getElementById('DATA').textContent);
const P = D.projects.projects, U = D.usage.projects, R = D.repos.repos;
const E = D.estimates.projects, C = D.competitors.projects;
const Q = (D.quality && D.quality.projects) || {};
const SIG = (D.quality && D.quality.portfolio_signals) || [];
const SN = D.snapshots || [];
const TREE = D.tree || {}, MEM = D.memory || {records: [], totals: {}};
const INBOX = (D.inbox || {}).items || [];

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

/* Свежесть слоёв (ОБН-05): кнопка «быстро» не трогает репозитории, обмен с хранилищем
   идёт отдельно — и цифры на странице могут быть разного возраста. Слой старше суток
   помечен: по нему видно, что и чем обновить. */
(function freshness() {
  const F = D.fresh || {}, now = Date.now();
  const LAYERS = [['usage', 'токены'], ['repos', 'репозитории'], ['memory', 'память'],
                  ['vault_pull', 'заметки из Obsidian'],
                  ['vault_push', 'выгрузка в Obsidian']];
  const ago = t => {
    const h = (now - Date.parse(t)) / 36e5;
    return h < 1 ? Math.max(1, Math.round(h * 60)) + ' мин назад' : h < 48 ? Math.round(h) + ' ч назад'
      : Math.round(h / 24) + ' дн назад';
  };
  document.getElementById('fresh').innerHTML = 'Свежесть: ' + LAYERS.map(([k, name]) => {
    const t = F[k];
    if (!t || isNaN(Date.parse(t))) return `<span class="old" title="слой ещё не собирался">${name} — нет</span>`;
    const old = now - Date.parse(t) > 864e5;
    return `<span class="${old ? 'old' : ''}" title="${esc(t)}">${name} ${ago(t)}${old ? ' ⚠' : ''}</span>`;
  }).join(' · ');
})();

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
  const reviewed = new Set((p.security?.reviewed_suspects || []).map(x => x.file + '|' + x.digest));
  (r.secrets_suspect || []).filter(s => !s.file.includes('.claude/worktrees') && !reviewed.has(s.file + '|' + s.digest)).forEach(s =>
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

  <div class="prow"><button class="btn prefresh" data-k="${esc(k)}"
    title="Пересканировать только репозиторий этого проекта, остальное — быстро">Обновить проект</button>
    <span class="plog" id="plog-${esc(k)}">${r.scanned_at ? 'срез репозитория: ' + esc(String(r.scanned_at).slice(0, 16).replace('T', ' ')) + ' UTC' : ''}</span></div>

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

/* ======================= память системы =======================
   Ради этого блока память и вынесена из контекста ассистента: видно, сколько
   знания хранится и во сколько обходится вход в работу. */
(function memoryBlock() {
  const el = document.getElementById('memWrap');
  const recs = (MEM.records || []).filter(r => r.status === 'активна');
  const t = MEM.totals || {}, cx = D.context || {};
  if (!recs.length) {
    el.innerHTML = `<div class="section-h">Память системы</div><div class="note">
      Память пуста. Она заменяет «держать всё в контексте»: заметки раскладываются
      командой <code>py tools/pm.py ingest файл.md</code>, знание пишется через
      <code>py tools/pm.py remember проект "заголовок" --text "…"</code>,
      а ассистент читает не данные, а брифинг — <code>py tools/pm.py context</code>.</div>`;
    return;
  }
  const byKind = {};
  recs.forEach(r => byKind[r.kind] = (byKind[r.kind] || 0) + 1);
  const top = recs.slice().sort((a, b) => (b.tokens || 0) - (a.tokens || 0)).slice(0, 8);
  el.innerHTML = `<div class="section-h">Память системы — ${recs.length} записей</div>
   <div class="kpis" style="margin-bottom:12px">
     ${[['Записей', recs.length, Object.entries(byKind).sort((a, b) => b[1] - a[1])
          .map(([k, v]) => k + ' ' + v).join(' · ')],
        ['Хранится', tok(t.stored_tokens || 0) + ' т.', 'знание на диске, а не в окне контекста'],
        ['Брифинг сессии', tok(cx.brief_tokens || 0) + ' т.', 'столько читает ассистент на старте'],
        ['Не читается', tok(Math.max(0, (cx.full_tokens || 0) - (cx.brief_tokens || 0))) + ' т.',
          cx.saved_pct != null ? cx.saved_pct + '% от полного чтения data/' : 'экономия контекста']]
       .map(([l, v, n]) => `<div class="kpi"><div class="v">${v}</div><div class="l">${esc(String(l))}</div><div class="n">${esc(String(n))}</div></div>`).join('')}
   </div>
   <div class="note" style="margin-bottom:11px">Записи лежат в <code>data/memory/*.md</code> —
     это готовый формат Obsidian. Ассистент видит только заголовки и достаёт нужное целиком:
     <code>py tools/pm.py recall id</code>.</div>
   <div class="memgrid">${top.map(r => `<div class="memcard">
     <div class="t"><span class="chip">${esc(r.kind)}</span> ${esc(r.title)}</div>
     <div class="d">${esc((r.digest || '').slice(0, 170))}</div>
     <div class="m">${esc(r.project)} · ${tok(r.tokens || 0)} т. · ${esc(r.id)}</div></div>`).join('')}</div>
   ${recs.length > top.length ? `<div style="color:var(--ink3);font-size:12.5px;margin-top:9px">
     Показаны 8 самых крупных из ${recs.length}. Остальные — в <code>data/memory/</code>.</div>` : ''}`;
})();

/* ======================= входящие из заметок ======================= */
(function inboxBlock() {
  const el = document.getElementById('inboxWrap');
  const rows = INBOX.filter(i => i.status === 'new');
  if (!rows.length) { el.innerHTML = ''; return; }
  el.innerHTML = `<div class="section-h">Входящие из заметок — ${rows.length}</div>
   <div class="alerts" style="border-left-color:var(--warn)">${rows.map(i => `<div class="alert">
     <span class="sev sev-warn">${esc(i.size || '')}</span>
     <div class="txt">${esc(i.priority || '')} ${esc(i.title)}
       <div class="who">${esc(i.project)} · ${esc(i.why || '')}${i.source ? ' · ' + esc(String(i.source).slice(0, 70)) : ''}
       · принять: <code>py tools/pm.py apply ${esc(i.id)}</code></div></div></div>`).join('')}</div>`;
})();

/* ======================= обновление данных =======================
   Под сервером (py tools/pm.py serve) кнопка действительно запускает сбор (ОБН-01).
   Открытый файлом дашборд запускать ничего не может — тогда показываем команду (ОБН-03). */
(function refreshBtn() {
  const btn = document.getElementById('refresh'), mode = document.getElementById('rmode'),
    wrap = document.getElementById('rlogWrap');
  const served = location.protocol === 'http:' || location.protocol === 'https:';
  const CMD = {fast: 'py tools/pm.py refresh --fast', full: 'py tools/pm.py refresh',
               vault: 'py tools/pm.py vault sync'};
  if (!served) {
    btn.title = 'Дашборд открыт файлом: страница не может запускать команды';
    btn.onclick = () => {
      const cmd = CMD[mode.value];
      wrap.innerHTML = `<div class="rlog">Дашборд открыт как файл, поэтому кнопка не может запустить сбор сама.
Выполните в каталоге системы:

    ${esc(cmd)}

Чтобы кнопка работала прямо отсюда, запустите дашборд под локальным сервером:

    py tools/pm.py serve
</div>`;
      if (navigator.clipboard) navigator.clipboard.writeText(cmd).then(() => {
        wrap.firstChild.insertAdjacentText('beforeend', '\n(команда скопирована в буфер обмена)');
      }, () => {});
    };
    return;
  }
  btn.onclick = async () => {
    const m = mode.value, label = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span>Обновляю…';
    wrap.innerHTML = `<div class="rlog">Запущено: ${esc(CMD[m])}
Это может занять от нескольких секунд до нескольких минут — сканирование репозиториев самое долгое.</div>`;
    try {
      const r = await fetch('/api/refresh', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-TaskTreker': '1'},
        body: JSON.stringify({mode: m}),
      });
      const j = await r.json();
      wrap.innerHTML = `<div class="rlog ${j.ok ? 'ok' : 'bad'}">${esc(j.log || j.error || '')}
${j.ok ? 'Готово за ' + j.seconds + ' с. Перезагружаю страницу…' : 'Обновление не прошло — данные остались прежними.'}</div>`;
      if (j.ok) setTimeout(() => location.reload(), 1200);
    } catch (e) {
      wrap.innerHTML = `<div class="rlog bad">Сервер не ответил: ${esc(String(e))}
Похоже, py tools/pm.py serve больше не запущен.</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  };
})();

/* Обновление одного проекта с его карточки (ОБН-04): пересканируется только его
   репозиторий. Без сервера кнопка показывает и копирует команду. */
document.getElementById('cards').addEventListener('click', async e => {
  const b = e.target.closest('.prefresh');
  if (!b) return;
  e.preventDefault();
  const k = b.dataset.k, log = document.getElementById('plog-' + k);
  const cmd = 'py tools/pm.py refresh --project ' + k;
  if (!(location.protocol === 'http:' || location.protocol === 'https:')) {
    log.textContent = 'Дашборд открыт файлом — выполните в каталоге системы: ' + cmd;
    if (navigator.clipboard) navigator.clipboard.writeText(cmd).catch(() => {});
    return;
  }
  const label = b.textContent;
  b.disabled = true;
  b.innerHTML = '<span class="spin"></span>Обновляю…';
  try {
    const r = await fetch('/api/refresh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-TaskTreker': '1'},
      body: JSON.stringify({mode: 'project', project: k}),
    });
    const j = await r.json();
    log.textContent = j.ok ? 'Готово за ' + j.seconds + ' с — перезагружаю…' : (j.error || 'Обновление не прошло');
    if (j.ok) setTimeout(() => { location.hash = 'proj'; location.reload(); }, 900);
  } catch (err) {
    log.textContent = 'Сервер не ответил: ' + err;
  } finally {
    b.disabled = false;
    b.textContent = label;
  }
});

/* ======================= вкладки и тема ======================= */
const PAGES = ['dash', 'tree', 'proj'];
function showTab(which) {
  const cur = PAGES.includes(which) ? which : 'dash';
  PAGES.forEach(p => {
    document.getElementById('page-' + p).hidden = (p !== cur);
    document.getElementById('tab-' + p).setAttribute('aria-selected', String(p === cur));
  });
  // Древо — сцена на весь экран поверх дашборда: открывается вкладкой,
  // закрывается кнопкой «← Дашборд» или Esc. Раскладка считается при первом открытии.
  if (cur === 'tree') {
    TREEVIEW.open(TREE, {
      onClose: () => showTab('dash'),
      onProject: k => {
        if (!P[k]) return;
        showTab('proj');
        const d = document.getElementById('p-' + k);
        if (d) { d.open = true; d.scrollIntoView({behavior: 'smooth', block: 'start'}); }
      },
      onEmpty: () => {
        document.getElementById('treeWrap').textContent =
          'Дерево пустое: в data/tree.json нет узлов. Соберите его: py tools/pm.py tree';
      },
    });
  } else TREEVIEW.close();
  try { history.replaceState(null, '', '#' + cur); } catch (e) {}
}
// Тема — до первой вкладки: древо читает её при открытии.
try { const s = localStorage.getItem('tt-theme'); if (s) document.documentElement.setAttribute('data-theme', s); } catch (e) {}
PAGES.forEach(p => document.getElementById('tab-' + p).onclick = () => showTab(p));
showTab((location.hash || '').replace('#', ''));

const t = document.getElementById('theme');
t.onclick = () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : cur === 'light' ? 'dark'
    : (matchMedia('(prefers-color-scheme: dark)').matches ? 'light' : 'dark');
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('tt-theme', next); } catch (e) {}
};
