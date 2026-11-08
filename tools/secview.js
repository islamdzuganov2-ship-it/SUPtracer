/* Карточка «Безопасность кода» вкладки «Дашборд» (SEC-012…014).
   Данные — security_triage.snapshot() при сборке: последний скан по проекту, статус каждого сканера,
   открытые находки по серьёзности. not_run — не «чисто»: сканер не запускался, и это видно.
   Снять находку можно только решением владельца: pm.py security triage <key> в терминале владельца. */

const SECVIEW = (() => {
  const $ = id => document.getElementById(id);
  const h = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const ST = {ran: '✓', not_run: 'не запускался', error: 'сбой'};
  let open = '';

  function render() {
    const host = $('secview');
    if (!host || host.offsetParent === null) return;
    const d = D.secview;
    const head = '<div class="section-h">Безопасность кода — сканеры и находки</div>';
    if (!d || d.error) { host.innerHTML = head + `<div class="tv-note">${d ? h(d.error) : 'нет данных: пересоберите дашборд'}</div>`; return; }
    if (!(d.projects || []).length) {
      host.innerHTML = head + '<div class="tv-note">Сканирований ещё не было. Скан идёт сам после зелёного CI тестовой ветки; вручную — py tools/pm.py security scan &lt;проект&gt;.</div>';
      return;
    }
    const rows = d.projects.map(p => {
      const c = p.counts || {}, block = (c.high || 0) + (c.critical || 0);
      const sc = Object.entries(p.scanners || {}).map(([k, v]) => `${h(k)}: <span class="${v === 'ran' ? '' : 'bad'}">${h(ST[v] || v)}</span>`).join(' · ');
      return `<tr><td>${h(p.project)}</td><td class="mono">${h(p.sha)}</td><td>${h((p.at || '').slice(0, 16).replace('T', ' '))}</td>
        <td>${block ? `<b class="bad">${block}</b>` : '0'}</td><td>${c.medium || 0}</td><td>${(c.low || 0) + (c.info || 0)}</td><td>${p.waived || 0}</td>
        <td><small>${sc}</small></td><td>${(p.top || []).length ? `<button class="btn" data-sv="${h(p.project)}">${open === p.project ? 'скрыть' : 'находки'}</button>` : ''}</td></tr>`
        + (open === p.project ? `<tr><td colspan="9"><table class="pf"><tbody>${(p.top || []).map(f =>
            `<tr><td>${h(f.severity)}</td><td>${h(f.rule)}</td><td class="mono">${h(f.file)}:${h(f.line ?? '?')}</td><td>${h(f.message)}</td><td class="mono"><small>${h(f.key)}</small></td></tr>`).join('')}</tbody></table></td></tr>` : '');
    }).join('');
    host.innerHTML = head
      + `<div class="tv-note">High/Critical в файлах задачи блокируют релиз в main. Сканов в журнале: ${d.scans}.</div>`
      + `<table class="pf"><thead><tr><th>Проект</th><th>Коммит</th><th>Когда</th><th>High+Critical</th><th>Medium</th><th>Low</th><th>Снято владельцем</th><th>Сканеры</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
    host.querySelectorAll('[data-sv]').forEach(b => b.addEventListener('click', () => { open = open === b.dataset.sv ? '' : b.dataset.sv; render(); }));
  }

  addEventListener('DOMContentLoaded', () => {
    const tab = $('tab-dash');
    if (tab) tab.addEventListener('click', () => setTimeout(render, 0));
    setTimeout(render, 0);
  });
  return {render};
})();
