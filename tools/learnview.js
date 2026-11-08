/* Карточка «Качество ИИ-разработчика» вкладки «Дашборд» (LEARN-08, ТЗ §12.1–12.2).
   Данные — learn.snapshot() при сборке: журнал опыта локального разработчика local_mimo, без ИИ.
   Каждая цифра показывает числитель и знаменатель; доля без знаменателя не показывается.
   «Недостаточно данных» — не «модель плохая». Обучения из интерфейса нет: решение — владелец. */

const LEARNVIEW = (() => {
  const $ = id => document.getElementById(id);
  const h = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const pct = m => (m && m.denominator && m.value != null) ? Math.round(100 * m.value) + '%' : '—';
  const frac = m => m ? `${m.numerator ?? '—'} / ${m.denominator ?? 0}` : '—';
  const STATUS = {insufficient_data: 'недостаточно данных', diagnostics_first: 'сначала диагностика причин', analysable: 'данных достаточно для анализа'};
  let open = false, cause = '';

  function tile(title, m, value) {
    const tip = m ? `${m.title || ''}: ${frac(m)}${m.unknown_count ? ' · неизвестно ' + m.unknown_count : ''}${m.note ? ' · ' + m.note : ''}` : '';
    return `<div class="lv-m" title="${h(tip)}"><small>${h(title)}</small><b>${value}</b><small>${h(frac(m))}</small></div>`;
  }

  function render() {
    const host = $('learnview');
    if (!host || host.offsetParent === null) return;
    const d = D.learnview;
    const head = '<div class="section-h">Качество ИИ-разработчика — локальная модель</div>';
    if (!d || d.error) { host.innerHTML = head + `<div class="tv-note">${d ? h(d.error) : 'нет данных: пересоберите дашборд'}</div>`; return; }
    const c = d.config || {}, M = d.metrics || {};
    const cfg = c.id ? `${h(c.model)} · ${h(c.quantization || '')} · ${h(c.id)}` : 'конфигурация не зарегистрирована (pm.py learn model-register)';
    const causes = (d.top_causes || []).map(x => `<button class="btn" data-lv-cause="${h(x.category)}">${h(x.title || x.category)} — ${x.count}</button>`).join(' ');
    const rows = (d.returns || []).filter(r => !cause || (cause === 'unknown' ? (r.cause || {}).confidence !== 'verified' : (r.cause || {}).category === cause && (r.cause || {}).confidence === 'verified'));
    host.innerHTML = head
      + `<div class="tv-note">Конфигурация: ${cfg}. Принято задач: <b>${d.accepted}</b>, открыто: ${d.open}.
         Статус данных: <span class="lv-status">${h(STATUS[d.data_status] || d.data_status)}</span>
         · Рекомендация: <span class="lv-status">${h(d.status)}</span>${d.decision ? ' <small>' + h(d.decision) + '</small>' : ''}
         · Кандидатов в опыт ждут проверки: ${d.candidates || 0}</div>`
      + '<div class="lv-grid">'
      + tile('Принято с первого раза', M.M01, pct(M.M01))
      + tile('С возвратом', M.M02, pct(M.M02))
      + tile('Попыток на задачу', M.M03, M.M03 && M.M03.value != null ? String(M.M03.value).replace('.', ',') : '—')
      + tile('Ошибки модели', M.M04, pct(M.M04))
      + tile('Причина подтверждена', M.M07, pct(M.M07))
      + tile('Регрессии', M.M05, pct(M.M05))
      + '</div>'
      + `<div class="tv-note">Основные подтверждённые причины: ${causes || 'пока нет'}${d.unclassified ? ` · не классифицировано: <button class="btn" data-lv-cause="unknown">${d.unclassified}</button>` : ''}
         ${cause ? ' · <button class="btn" data-lv-cause="">все возвраты</button>' : ''}
         · <button class="btn" id="lv-toggle">${open ? 'Скрыть возвраты' : 'Возвраты подробно'}</button></div>`
      + (open ? (rows.length ? `<table class="pf"><thead><tr><th>Задача</th><th>Этап</th><th>Кто вернул</th><th>Причина возврата</th><th>Классификация</th><th>Дата</th></tr></thead><tbody>
          ${rows.map(r => `<tr><td>${h(r.task_ref)}</td><td>${h(r.stage)}</td><td>${h(r.by)}</td><td>${h(r.reason)}</td>
            <td>${h((r.cause || {}).category || 'не классифицировано')} <small>${h((r.cause || {}).confidence || '')}</small></td><td>${h((r.at || '').slice(0, 10))}</td></tr>`).join('')}
          </tbody></table><div class="tv-note">Разбор попытки: py tools/pm.py learn inspect &lt;проект/id&gt; · классификация: pm.py learn classify --event … --cause …</div>`
        : '<div class="tv-note">Возвратов нет.</div>') : '');
    host.querySelectorAll('[data-lv-cause]').forEach(b => b.addEventListener('click', () => { cause = b.dataset.lvCause; open = true; render(); }));
    const t = $('lv-toggle'); if (t) t.addEventListener('click', () => { open = !open; render(); });
  }

  addEventListener('DOMContentLoaded', () => {
    const tab = $('tab-dash');
    if (tab) tab.addEventListener('click', () => setTimeout(render, 0));
    setTimeout(render, 0);
  });
  return {render};
})();
