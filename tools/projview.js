/* Вкладка «Дашборд»: выпадающий список (весь портфель или проект) и метрики работ по спринтам.
   Спринт — окно лимита токенов (data/metrics.json, собирает tools/metrics.py).
   Все цифры — подсчёт записанных событий: журналы сессий, git, конвейер. Здесь только отображение. */

const PROJVIEW = (() => {
  const ALL = '__all';
  const LEVEL = {unit: 'модульные', integration: 'интеграционные', e2e: 'сквозные (e2e/UI)', load: 'нагрузочные',
                 manual: 'ручные', static: 'статический анализ'};
  const METHOD = {spec_change: 'изменение ТЗ', code_to_spec: 'код приведён к ТЗ', workaround: 'обходная мера', test_fix: 'поправка теста'};
  const SEV = {critical: 'критичный', major: 'существенный', minor: 'мелкий'};
  const ROLE_FOCUS = ['designer', 'marketer'];
  const CHART_SPRINTS = 12;
  let ready = false, current = ALL, drawn = null;

  const $ = id => document.getElementById(id);
  const fmtDay = iso => { const d = new Date(iso); return isNaN(d) ? '—' :
    `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}`; };
  const fmtTime = iso => { const d = new Date(iso); return isNaN(d) ? '' :
    `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
  const period = s => `${fmtDay(s.start)} ${fmtTime(s.start)}–${fmtTime(s.end)}`;
  const pct = v => v == null ? '—' : v + '%';
  const ai = n => n === 'codex' ? 'GPT / Codex' : n === 'claude' ? 'Claude' : n;
  const metricsOf = k => k === ALL ? (D.metrics || {}).portfolio : ((D.metrics || {}).projects || {})[k];

  function init() {
    if (ready) return;
    ready = true;
    const sel = $('proj-select');
    const own = Object.keys(P).sort((a, b) => (P[a].tier - P[b].tier) || String(P[a].name).localeCompare(String(P[b].name), 'ru'));
    const all = metricsOf(ALL);
    sel.innerHTML = `<option value="${ALL}">Весь портфель${all && all.sprints.length ? ' · ' + all.sprints.length + ' спр.' : ''}</option>` +
      `<optgroup label="Проекты">${own.map(k => {
        const m = metricsOf(k), n = m ? m.sprints.length : 0;
        return `<option value="${esc(k)}">${esc(P[k].name)}${n ? ' · ' + n + ' спр.' : ' · нет спринтов'}</option>`;
      }).join('')}</optgroup>`;
    sel.onchange = () => select(sel.value);
    let saved = ALL;
    try { saved = localStorage.getItem('tt-proj') || ALL; } catch (e) {}
    apply([...sel.options].some(o => o.value === saved) ? saved : ALL);
  }

  function select(k) {
    init();
    if (![...$('proj-select').options].some(o => o.value === k)) k = ALL;
    try { localStorage.setItem('tt-proj', k); } catch (e) {}
    apply(k);
  }

  function apply(k) {
    current = k;
    $('proj-select').value = k;
    $('signalsWrap').hidden = k !== ALL;          // портфельные сигналы — в обзоре портфеля
    const m = metricsOf(k);
    $('proj-hint').innerHTML = (m && m.sprints.length ? `${m.sprints.length} спринтов с работой · спринт — окно лимита токенов`
      : 'Метрик по спринтам ещё нет') +
      (k !== ALL && P[k] ? ` · <a href="#proj" id="proj-card-link">карточка проекта →</a>` : '');
    const link = $('proj-card-link');
    if (link) link.onclick = e => {
      e.preventDefault(); showTab('proj');
      const d = $('p-' + k); if (d) { d.open = true; d.scrollIntoView({behavior: 'smooth', block: 'start'}); }
    };
    drawn = null;
    shown();
  }

  // Графики считают ширину по видимому контейнеру: рисуются, когда вкладка открыта.
  function shown() {
    init();
    if ($('page-dash').hidden || drawn === current) return;
    drawn = current;
    render(current);
  }

  // Ширина окна изменилась — перерисовать, иначе подписи поедут вместе с масштабом viewBox.
  let rsz;
  addEventListener('resize', () => { clearTimeout(rsz); rsz = setTimeout(() => { drawn = null; shown(); }, 160); });

  function kpi(v, l, n) {
    return `<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div>${n ? `<div class="n">${n}</div>` : ''}</div>`;
  }

  function stat(label, value, tip) {
    return `<div class="pm-stat"${tip ? ` title="${esc(tip)}"` : ''}><span>${label}</span><b>${value}</b></div>`;
  }

  function render(k) {
    const host = $('proj-metrics'), m = metricsOf(k), name = k === ALL ? 'весь портфель' : (P[k] || {}).name || k;
    if (!m) {
      host.innerHTML = `<div class="note" style="margin-bottom:14px">Для «${esc(name)}» нет метрик работ. Их собирает
        <span class="mono">py tools/pm.py refresh</span> (шаг «метрики работ по спринтам лимита»).</div>`;
      return;
    }
    const S = m.sprints, T = m.totals, tests = m.tests, sc = m.spec_changes, ca = m.code_after_testing, df = m.defects;
    const tokAll = T.tokens.claude + T.tokens.codex;
    host.innerHTML = `
      <div class="section-h">Агенты — выполнено, токены, предстоит, возвраты — ${esc(name)}</div>
      <div class="pm-note">Агент — роль конвейера со своим скиллом (и исполнитель задач вне конвейера). Выполнено — задачи,
        где запуск роли закончен сдачей, возвратом или решением (не эскалацией). Токены — по сессиям запусков роли из журналов
        Claude и Codex. Предстоит — этапы впереди с учётом решения PO о дизайне и маркетинге; «возможно» — пока PO не решил.
        Какой ИИ ведёт роль — таблица ролей (окно «DoR / DoD» на доске): идея и анализ — Claude, разработка, QA и DevOps — Codex.</div>
      ${m.team ? `<div class="kpis">${aiKpis(m.team)}</div>` : ''}
      ${(m.team && m.team.agents.length) || (m.team && Object.keys(m.team.roles).length) ? `<div class="pm-grid">
        <div class="card-viz" id="pm-team-done"></div><div class="card-viz" id="pm-team-tokens"></div></div>
        <div class="pm-grid"><div class="card-viz" id="pm-team-upcoming"></div><div class="card-viz" id="pm-team-returns"></div></div>
        <div class="block"><h3>Причины возвратов по агентам</h3>${returnsTable(m.team)}</div>` : '<div class="pm-note">Запусков агентов по проекту ещё не было.</div>'}
      <div class="section-h" style="margin-top:22px">Реализация по спринтам — ${esc(name)}</div>
      <div class="pm-note">${esc((D.metrics || {}).basis || '')} Спринт попадает сюда, если в нём был расход или коммит проекта.
        Коммитов вне спринтов (без работы ИИ): ${nf(m.outside_commits)}.</div>
      <div class="kpis">
        ${kpi(nf(T.sprints), 'спринтов', `${nf(T.limit_hits)} закончились исчерпанием лимита`)}
        ${kpi(tok(T.tokens.claude), 'токенов Claude', money(T.usd.claude))}
        ${kpi(tok(T.tokens.codex), 'токенов Codex', money(T.usd.codex))}
        ${kpi(nf(T.features), 'фич закрыто', `${nf(T.commits)} коммитов`)}
        ${kpi(nf(T.docs), 'документов изменено', `из них ТЗ: ${nf(T.spec_docs)}`)}
        ${kpi('+' + nf(T.code_added), 'строк кода', `−${nf(T.code_removed)}; тестов +${nf(T.test_added)}`)}
        ${kpi(`${nf(df.found)} / ${nf(df.fixed)}`, 'дефектов найдено / исправлено', `проверено QA: ${nf(df.verified)}, открыто: ${nf(df.open)}`)}
      </div>
      ${S.length ? `<div class="card-viz" id="pm-tokens"></div>
      <div class="card-viz" id="pm-code"></div>
      <div class="card-viz" id="pm-work"></div>
      <div class="block"><h3>Спринты</h3><div class="pm-scroll">${sprintTable(S)}</div></div>`
      : '<div class="note">Спринтов с работой по проекту нет.</div>'}
      <div class="pm-grid" style="margin-top:14px">
        <div class="card-viz">${testsBlock(tests)}</div>
        <div class="card-viz" id="pm-tests"></div>
      </div>
      <div class="pm-grid">
        <div class="card-viz">${changesBlock(sc, ca)}</div>
        <div class="card-viz" id="pm-defects"></div>
      </div>
      ${df.found ? `<div class="card-viz">${defectList(df)}</div>` : ''}
      <div class="block"><h3>Качество работы ролей и ИИ</h3>${rolesBlock(m.roles, m.agents)}</div>`;
    drawLevels(tests);
    if (m.team) drawTeam(m.team);
    drawDefects(df);
    if (!S.length) return;
    const room = host.clientWidth < 500 ? 4 : host.clientWidth < 800 ? 7 : CHART_SPRINTS;   // подписи оси не налезают
    const last = S.slice(-room), cats = last.map(s => fmtDay(s.start) + ' ' + fmtTime(s.start).slice(0, 2) + 'ч');
    const cut = S.length > last.length ? `Последние ${last.length} из ${S.length} спринтов; все — в таблице ниже.` : '';
    VIZ.stacked($('pm-tokens'), {
      title: 'Токены по спринтам: Claude и Codex',
      sub: 'Оплачиваемые токены (вход, выход, кеш) за окно лимита. ' + (tokAll ? `Всего ${tok(tokAll)}.` : ''),
      cats, series: [{label: 'Claude', values: last.map(s => s.tokens.claude)}, {label: 'GPT / Codex', values: last.map(s => s.tokens.codex)}],
      fmt: tok, note: cut});
    VIZ.stacked($('pm-code'), {
      title: 'Строки кода и тестов по спринтам',
      sub: 'Добавленные строки по коммитам спринта (все ветки, без слияний).',
      cats, series: [{label: 'Код', values: last.map(s => s.code.added)}, {label: 'Тесты', values: last.map(s => s.tests.added)}],
      fmt: nf, note: cut});
    VIZ.stacked($('pm-work'), {
      title: 'Фичи и документы по спринтам',
      sub: 'Фича — закрытая задача бэклога. Документ — изменённый .md и т.п.; ТЗ — документы требований и артефакты анализа.',
      cats, series: [{label: 'Фичи', values: last.map(s => s.features.length)},
                     {label: 'Документы', values: last.map(s => s.docs.changed - s.docs.spec)},
                     {label: 'ТЗ', values: last.map(s => s.docs.spec)}],
      fmt: nf, note: 'Высота столбца — число событий спринта разной природы; сравнивайте сегменты одного цвета.'});
  }

  // Агенты: по ролям, Claude и Codex — отдельными сегментами.
  function teamRoles(team) {
    const order = Object.keys((D.workflow || {}).pipeline?.roles || {});
    const names = new Set([...team.agents.map(a => a.role), ...Object.keys(team.roles)]);
    return [...names].sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
  }
  function teamTitle(team, r) {
    return (team.roles[r] || {}).title || (team.agents.find(a => a.role === r) || {}).title || roleTitle(r);
  }
  function drawTeam(team) {
    const roles = teamRoles(team), by = (r, n, f) => team.agents.filter(a => a.role === r && a.agent === n).reduce((s, a) => s + (a[f] || 0), 0);
    const rows = f => roles.map(r => ({label: teamTitle(team, r), parts: [by(r, 'claude', f), by(r, 'codex', f)]})).filter(x => x.parts.some(v => v > 0));
    const series = [{label: 'Claude'}, {label: 'GPT / Codex'}];
    const done = rows('done'), tokens = rows('tokens');
    if (done.length) VIZ.hstack($('pm-team-done'), {title: 'Выполнено задач', sub: 'По агентам-ролям; Claude и Codex — отдельно.', rows: done, series, fmt: nf});
    else $('pm-team-done').innerHTML = '<div class="viz-title">Выполнено задач</div><div class="pm-note">Пока ни одной.</div>';
    if (tokens.length) VIZ.hstack($('pm-team-tokens'), {title: 'Потрачено токенов', sub: 'Оплачиваемые токены сессий запусков роли.', rows: tokens, series, fmt: tok,
      note: 'Деньги: ' + roles.map(r => { const usd = team.agents.filter(a => a.role === r).reduce((s, a) => s + a.usd, 0); return usd ? `${teamTitle(team, r)} — ${money(usd)}` : ''; }).filter(Boolean).join(', ')});
    else $('pm-team-tokens').innerHTML = '<div class="viz-title">Потрачено токенов</div><div class="pm-note">Сессии агентов ещё не сопоставлены с журналами.</div>';
    const up = roles.map(r => ({label: teamTitle(team, r), value: (team.roles[r] || {}).upcoming || 0,
      tip: `<b>${esc(teamTitle(team, r))}</b><br>предстоит: ${nf((team.roles[r] || {}).upcoming || 0)}${(team.roles[r] || {}).maybe ? `<br>возможно: ${nf(team.roles[r].maybe)}` : ''}`})).filter((x, i) => x.value > 0 || (team.roles[roles[i]] || {}).maybe);
    if (up.length) VIZ.bars($('pm-team-upcoming'), {title: 'Предстоит задач', sub: 'Сколько задач ещё пройдёт через агента.', rows: up, fmt: nf,
      note: roles.filter(r => (team.roles[r] || {}).maybe).map(r => `${teamTitle(team, r)}: возможно ещё ${nf(team.roles[r].maybe)}`).join('; ')});
    else $('pm-team-upcoming').innerHTML = '<div class="viz-title">Предстоит задач</div><div class="pm-note">Открытых задач нет.</div>';
    const ret = roles.map(r => ({label: teamTitle(team, r), value: ((team.roles[r] || {}).returns || 0) + ((team.roles[r] || {}).dod_rejects || 0),
      tip: `<b>${esc(teamTitle(team, r))}</b><br>возвратов: ${nf((team.roles[r] || {}).returns || 0)}<br>отказов DoD: ${nf((team.roles[r] || {}).dod_rejects || 0)}`})).filter(x => x.value > 0);
    if (ret.length) VIZ.bars($('pm-team-returns'), {title: 'Возвраты к агенту', sub: 'Возвраты на его этап и не принятые сервером передачи (DoD).', rows: ret, fmt: nf, color: 'var(--warn)'});
    else $('pm-team-returns').innerHTML = '<div class="viz-title">Возвраты к агенту</div><div class="pm-note">Возвратов не было.</div>';
  }
  // Сводка по ИИ: кто сколько сделал, сколько стоил, сколько запусков сорвалось.
  function aiKpis(team) {
    return ['claude', 'codex'].map(n => {
      const rows = team.agents.filter(a => a.agent === n);
      const sum = f => rows.reduce((s, a) => s + (a[f] || 0), 0);
      return kpi(nf(sum('done')), (n === 'claude' ? 'Claude' : 'GPT / Codex') + ': задач выполнено',
        `${tok(sum('tokens'))} токенов · ${money(sum('usd'))} · запусков ${nf(sum('runs'))}${sum('failed') ? `, сорвалось ${nf(sum('failed'))}` : ''}`);
    }).join('');
  }
  function returnsTable(team) {
    const roles = teamRoles(team).filter(r => (team.roles[r] || {}).returns || (team.roles[r] || {}).dod_rejects);
    if (!roles.length) return '<div class="pm-note">Возвратов и отказов DoD не было.</div>';
    return `<div class="pm-scroll"><table class="bl"><thead><tr><th>Агент</th><th class="num">Возвратов</th><th>Кто возвращал</th><th>Причины (последние)</th><th>Чаще всего не сдано по DoD</th></tr></thead><tbody>${
      roles.map(r => { const v = team.roles[r];
        return `<tr><td>${esc(v.title)}</td><td class="num">${nf(v.returns)}${v.dod_rejects ? ` <span class="chip warn">DoD ${nf(v.dod_rejects)}</span>` : ''}</td>
          <td>${Object.entries(v.returned_by || {}).map(([k, n]) => `${esc(k)} — ${nf(n)}`).join('<br>') || '—'}</td>
          <td>${(v.reasons || []).slice().reverse().slice(0, 5).map(x => `<div style="font-size:12px;margin-bottom:4px"><span class="mono">${esc(x.task)}</span> · ${esc(x.by)}: ${esc(x.reason)}</div>`).join('') || '—'}</td>
          <td>${(v.dod_top || []).map(([id, n]) => `<span class="chip">${esc(id)} × ${nf(n)}</span>`).join(' ') || '—'}</td></tr>`; }).join('')}</tbody></table></div>`;
  }

  function drawLevels(tests) {
    const lv = Object.entries(tests.levels || {}).sort((a, b) => b[1] - a[1]);
    if (lv.length) VIZ.bars($('pm-tests'), {
      title: 'Тесты по уровням', sub: 'Файлы тестов в репозитории по пути и имени: e2e, integration, load…; остальные — модульные.',
      rows: lv.map(([l, n]) => ({label: LEVEL[l] || l, value: n})), fmt: nf});
    else $('pm-tests').innerHTML = '<div class="viz-title">Тесты по уровням</div><div class="pm-note">Файлов тестов в репозитории не найдено.</div>';
  }

  function sprintTable(S) {
    return `<table class="bl"><thead><tr><th>Спринт</th><th>Окно</th><th>Лимит</th><th class="num">Коммитов</th><th>Фичи</th>
      <th>Документы</th><th class="num">Код +/−</th><th class="num">Тесты +/−</th><th class="num">Claude</th><th class="num">Codex</th><th class="num">$</th></tr></thead><tbody>` +
      S.slice().reverse().map(s => `<tr>
        <td class="mono">${esc(s.id)}</td>
        <td style="white-space:nowrap">${esc(period(s))}${s.basis === 'activity' ? ' <span class="chip idle" title="Время сброса неизвестно: окно — 5 часов от первой активности">по активности</span>' : ''}</td>
        <td>${s.limit_hit.length ? s.limit_hit.map(n => `<span class="chip warn" title="Лимит исчерпан в этом окне">${esc(ai(n))}</span>`).join(' ') : '—'}${s.weekly ? ' <span class="chip bad">недельный</span>' : ''}</td>
        <td class="num">${nf(s.commits)}</td>
        <td>${s.features.length ? s.features.map(f => `<span class="chip ok" title="${esc(f.title)}">${esc(f.id)}</span>`).join(' ') : '—'}</td>
        <td>${s.docs.changed ? `<details><summary>${nf(s.docs.changed)}${s.docs.spec ? ` · ТЗ ${nf(s.docs.spec)}` : ''}</summary>
          ${s.docs.files.map(d => `<div class="mono" style="font-size:11.5px">${d.kind === 'spec' ? '<b>ТЗ</b> ' : ''}${esc(d.path)} <span style="color:var(--ink3)">+${nf(d.added)}/−${nf(d.removed)}</span></div>`).join('')}</details>` : '—'}</td>
        <td class="num">+${nf(s.code.added)} / −${nf(s.code.removed)}</td>
        <td class="num">+${nf(s.tests.added)} / −${nf(s.tests.removed)}</td>
        <td class="num">${tok(s.tokens.claude)}</td><td class="num">${tok(s.tokens.codex)}</td>
        <td class="num">${money((s.usd.claude || 0) + (s.usd.codex || 0))}</td></tr>`).join('') + '</tbody></table>';
  }

  function testsBlock(t) {
    const last = t.last;
    return `<div class="viz-title">Статусы тестирования</div>
      <div class="pm-note">Итоги автопроверок веток задач, фоновых проверок конвейера и подписей QA.</div>
      ${stat('Последняя автопроверка', last ? `<span class="chip ${last.ok ? 'ok' : 'bad'}">${last.ok ? 'пройдена' : 'не пройдена'}</span> ${esc(fmtDay(last.at))}` : '—')}
      ${stat('Автопроверок: пройдено / не пройдено', `${nf(t.passed)} / ${nf(t.failed)}`)}
      ${stat('Проверки конвейера: всего / провалов', `${nf(t.pipeline_checks)} / ${nf(t.pipeline_failed)}`, 'verify, слияние, деплой — фоновые пункты DoD')}
      ${stat('Подписей QA (Sign-off)', nf(t.signoffs))}
      <div class="pm-chips">${Object.entries(t.levels || {}).map(([l, n]) => `<span class="chip">${esc(LEVEL[l] || l)}: ${nf(n)}</span>`).join('') || '<span class="chip idle">тестов нет</span>'}</div>`;
  }

  function changesBlock(sc, ca) {
    const items = (list) => list.length ? `<details><summary>события (${list.length})</summary>${list.slice().reverse().map(e =>
      `<div style="font-size:12px;margin:3px 0"><span class="mono">${esc(e.task)}</span> · ${esc(fmtDay(e.at))} · ${esc(e.source)}${e.files && e.files.length ? ` <span class="mono" style="color:var(--ink3)">${esc(e.files.join(', '))}</span>` : ''}</div>`).join('')}</details>` : '';
    return `<div class="viz-title">Правки ТЗ и кода после передачи дальше</div>
      <div class="pm-note">Частота = события / задачи, прошедшие этап. Правка ТЗ — изменение артефактов анализа после его сдачи,
        возврат в анализ, отклонение от ТЗ по ревью. Изменение кода по итогам тестирования — коммиты кода после начала тестирования,
        возвраты QA и DevOps, доработки по автопроверке и ревью.</div>
      ${stat('Правок ТЗ после передачи в разработку', `${nf(sc.events)} на ${nf(sc.base)} задач${sc.per_task != null ? ' · ' + String(sc.per_task).replace('.', ',') + ' на задачу' : ''}`)}
      ${items(sc.items || [])}
      ${stat('Изменений кода по итогам тестирования', `${nf(ca.events)} на ${nf(ca.base)} задач${ca.per_task != null ? ' · ' + String(ca.per_task).replace('.', ',') + ' на задачу' : ''}`)}
      ${Object.keys(ca.rework_runs || {}).length ? `<div class="pm-chips">${Object.entries(ca.rework_runs).map(([k, n]) =>
        `<span class="chip warn">доработок по ${k === 'autocheck' ? 'автопроверке' : 'ревью'}: ${nf(n)}</span>`).join('')}</div>` : ''}
      ${items(ca.items || [])}`;
  }

  function drawDefects(df) {
    const host = $('pm-defects');
    if (!df.found) {
      host.innerHTML = `<div class="viz-title">Дефекты и способы исправления</div><div class="pm-note">Дефектов не зарегистрировано.
        Их регистрируют QA, ревьюер и DevOps при возврате задачи конвейера; способ исправления сервер сверяет с коммитами.</div>`;
      return;
    }
    const rows = Object.entries(METHOD).map(([k, label]) => ({label, value: df.by_method[k] || 0}));
    VIZ.bars(host, {title: 'Дефекты по способу исправления',
      sub: `Найдено ${nf(df.found)}, исправлено ${nf(df.fixed)}, проверено QA ${nf(df.verified)}, открыто ${nf(df.open)}.`,
      rows, fmt: nf,
      note: Object.keys(df.test_fix_levels || {}).length ? 'Поправки тестов по уровням: ' +
        Object.entries(df.test_fix_levels).map(([l, n]) => `${LEVEL[l] || l} — ${n}`).join(', ') + '.' : ''});
  }

  function defectList(df) {
    const chips = (obj, map) => Object.entries(obj || {}).map(([k, n]) => `<span class="chip">${esc((map && map[k]) || k)}: ${nf(n)}</span>`).join('');
    return `<div class="viz-title">Реестр дефектов</div>
      <div class="pm-chips">${chips(df.by_severity, SEV)}${chips(df.by_level, LEVEL)}</div>
      <div class="pm-chips">${Object.entries(df.by_finder || {}).map(([k, n]) => `<span class="chip">нашёл ${esc(roleTitle(k))}: ${nf(n)}</span>`).join('')}</div>
      ${(df.items || []).length ? `<table class="bl"><thead><tr><th>Дефект</th><th>Уровень</th><th>Статус</th><th>Исправлен</th></tr></thead><tbody>${
        df.items.slice().reverse().slice(0, 15).map(d => `<tr><td><span class="mono">${esc(d.task)}/${esc(d.id)}</span> ${esc(d.title)}
          <div style="font-size:11.5px;color:var(--ink3)">${esc(SEV[d.severity] || d.severity)} · нашёл ${esc(roleTitle(d.found_by))}</div></td>
          <td>${esc(LEVEL[d.level] || d.level)}</td>
          <td><span class="chip ${d.status === 'open' ? 'bad' : d.status === 'verified' ? 'ok' : 'warn'}">${d.status === 'open' ? 'открыт' : d.status === 'verified' ? 'проверен' : 'исправлен'}</span></td>
          <td>${d.method ? esc(METHOD[d.method] || d.method) + (d.test_level ? ' (' + esc(LEVEL[d.test_level] || d.test_level) + ')' : '') : '—'}</td></tr>`).join('')}</tbody></table>` : ''}`;
  }

  function roleTitle(role) {
    const spec = ((D.workflow || {}).pipeline || {}).roles || {};
    return (spec[role] || {}).title || role || '—';
  }

  function rolesBlock(rows, agents) {
    const focus = ROLE_FOCUS.map(r => {
      const own = (rows || []).filter(x => x.role === r);
      if (!own.length) return `<b>${esc(roleTitle(r))}</b>: задач с этой ролью ещё не было — правок по анализу не измерить.`;
      const tasks = own.reduce((s, x) => s + x.tasks, 0), back = own.reduce((s, x) => s + x.returns, 0), edits = own.reduce((s, x) => s + x.rework_edits, 0);
      return `<b>${esc(roleTitle(r))}</b>: ${nf(tasks)} задач, возвратов на доработку ${nf(back)}, правок своих артефактов после ворот анализа ${nf(edits)}` +
        ` — ${tasks ? Math.round(100 * Math.max(0, tasks - Math.min(tasks, back + edits)) / tasks) : 0}% задач без внесения правок.`;
    }).join('<br>');
    const table = (rows || []).length ? `<div class="pm-scroll"><table class="bl"><thead><tr><th>Роль</th><th>ИИ</th><th class="num">Задач</th><th class="num">Передач</th>
      <th class="num" title="Доля запусков, сданных с первой передачи и прошедших проверки сервера">С первой попытки</th><th class="num" title="Передачи, не принятые по DoD, и провалы фоновых проверок">Отказов DoD</th>
      <th class="num" title="Сколько раз задачу возвращали на этап этой роли">Возвратов к роли</th><th class="num" title="Коммиты, менявшие артефакты роли после сдачи анализа">Правок артефактов после ворот</th>
      <th class="num" title="Задачи, которые к роли ни разу не возвращались">Без возвратов</th></tr></thead><tbody>` +
      rows.map(r => `<tr class="${ROLE_FOCUS.includes(r.role) ? 'pm-focus' : ''}"><td>${esc(r.title)}</td><td>${esc(ai(r.agent))}</td>
        <td class="num">${nf(r.tasks)}</td><td class="num">${nf(r.handoffs)}</td><td class="num">${pct(r.first_try_pct)}</td>
        <td class="num">${nf(r.rejected)}</td><td class="num">${nf(r.returns)}</td><td class="num">${nf(r.rework_edits)}</td>
        <td class="num">${pct(r.clean_pct)}</td></tr>`).join('') + '</tbody></table></div>'
      : '<div class="pm-note">Задачи проекта ещё не проходили конвейер: качество ролей считается по передачам, отказам DoD и возвратам.</div>';
    const ais = Object.entries(agents || {}).filter(([, a]) => a.runs || a.handoffs || a.defects_found || a.defects_fixed);
    return `<div class="pm-note">Считается из истории конвейера и запусков, без оценок ИИ: передача принята или нет по DoD,
        возвраты на этап, правки артефактов после ворот, дефекты.</div>
      <div class="note" style="margin-bottom:10px">${focus}</div>${table}
      ${ais.length ? `<div class="pm-scroll" style="margin-top:12px"><table class="bl"><thead><tr><th>ИИ</th><th class="num">Запусков</th><th class="num">Провалено</th>
        <th class="num">Доработок</th><th class="num">Передач с первой попытки</th><th class="num">Отказов DoD</th><th class="num">Дефектов нашёл</th><th class="num">Дефектов исправил</th></tr></thead><tbody>${
        ais.map(([n, a]) => `<tr><td>${esc(ai(n))}</td><td class="num">${nf(a.runs)}</td><td class="num">${nf(a.failed_runs)}</td><td class="num">${nf(a.rework_runs)}</td>
          <td class="num">${nf(a.first_try)} из ${nf(a.handoffs)}</td><td class="num">${nf(a.rejected)}</td><td class="num">${nf(a.defects_found)}</td><td class="num">${nf(a.defects_fixed)}</td></tr>`).join('')}</tbody></table></div>` : ''}`;
  }

  return {select, shown, init};
})();
window.PROJVIEW = PROJVIEW;
