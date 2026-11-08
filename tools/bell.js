/* Колокольчик владельца: всё, что ждёт именно его — вопросы Решалы, релизы в пошаговом режиме,
   сбои запуска. Виден на любой вкладке. С сервером досок обновляется сам и отвечает прямо из списка;
   без сервера (файл) — показывает состояние на момент сборки. */
const BELL = (() => {
  const served = location.protocol !== 'file:';
  let items = ((typeof D !== 'undefined' && D.workflow) || {}).notifications || [];
  const seen = new Set(items.map(n => n.id));
  const KIND = {question: '👤 Вопрос Решалы', release: '🚀 Релиз в main', oracle: '🧪 Изменён эталон приёмки', launch: '⚠ Сбой запуска',
                kill_switch: '⛔ Аварийная остановка', grant: '🔑 Грант автономии',
                environment: '🌐 Команда развёртывания', reconcile: '🧩 Сверка операции'};
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const when = v => { const d = new Date(v); return isNaN(d) ? '' : d.toLocaleString('ru', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}); };

  const btn = document.createElement('button');
  btn.className = 'bell'; btn.type = 'button'; btn.setAttribute('aria-haspopup', 'dialog');
  btn.innerHTML = '🔔<span class="bell-count" hidden></span>';
  const panel = document.createElement('div');
  panel.className = 'bell-panel'; panel.hidden = true; panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'Нужен владелец');
  document.body.append(btn, panel);

  async function post(url, body) {
    const r = await fetch(url, {method: 'POST', headers: ttOwnerHeaders(), body: JSON.stringify(body)});
    const j = await r.json(); if (!r.ok || !j.ok) throw Error(j.error || 'Не выполнено'); return j.data;
  }

  function render() {
    const n = items.length, badge = btn.querySelector('.bell-count');
    badge.hidden = !n; badge.textContent = n > 99 ? '99+' : n;
    btn.classList.toggle('bell-on', n > 0);
    btn.title = n ? `Нужен владелец: ${n}` : 'Ничего не ждёт вашего решения';
    panel.innerHTML = `<div class="bell-head"><b>Нужен владелец${n ? ' · ' + n : ''}</b>${served && !items.some(x => x.kind === 'kill_switch') ? '<button class="bell-stop" type="button" title="Аварийная остановка: запретить новые действия, сохранить работу и закрыть сессии ИИ">⛔ Стоп</button>' : ''}<div class="bell-err bell-stop-err" role="alert"></div><button class="bell-x" type="button" aria-label="Закрыть">✕</button></div>` +
      (n ? items.map((x, i) => `<div class="bell-item" data-i="${i}">
        <div class="bell-kind">${KIND[x.kind] || x.kind} · <span class="mono">${esc(x.ref)}</span> · ${esc(when(x.at))}</div>
        <div class="bell-title">${esc(x.title)}</div>
        <div class="bell-text">${esc(x.text)}</div>${x.why ? `<div class="bell-why">Почему вы: ${esc(x.why)}</div>` : ''}
        ${x.kind === 'question' ? `${(x.options || []).map((o, k) => `<button type="button" class="bell-opt" data-opt="${k}">${esc(o)}</button>`).join('')}
          <textarea class="bell-answer" rows="2" maxlength="2000" placeholder="Или свой ответ"></textarea>
          <button type="button" class="bell-send primary">Ответить</button>` : ''}
        ${x.kind === 'release' ? '<button type="button" class="bell-approve primary">👤 Одобрить релиз в main</button>' : ''}
        ${x.kind === 'oracle' ? '<button type="button" class="bell-oracle">👤 Одобрить изменение эталона</button>' : ''}
        ${x.kind === 'launch' ? '<button type="button" class="bell-relaunch">Открыть сессию снова</button>' : ''}
        ${x.kind === 'kill_switch' ? '<button type="button" class="bell-resume primary">Снять остановку и продолжить сессии</button>' : ''}
        ${x.kind === 'environment' ? '<button type="button" class="bell-env primary">👤 Разрешить эту команду</button>' : ''}
        ${x.kind === 'reconcile' ? '<button type="button" class="bell-rec-retry">Повторить действие</button><button type="button" class="bell-rec-done">Уже выполнено — проверил</button>' : ''}
        <div class="bell-err" role="alert"></div></div>`).join('')
        : `<p class="bell-empty">Ничего не ждёт вашего решения.${served ? '' : ' (Статичная страница: ответы — через py tools/pm.py serve.)'}</p>`);
    panel.querySelector('.bell-x').onclick = () => { panel.hidden = true; };
    const stop = panel.querySelector('.bell-stop');
    if (stop) stop.onclick = async () => {
      const why = prompt('Аварийная остановка: почему? Новые действия будут запрещены, работа сохранена, сессии ИИ закрыты.');
      if (!why || !why.trim()) return;
      try { await post('/api/work', {op: 'stop_all', reason: why.trim()}); await poll(); }
      catch (e) { panel.querySelector('.bell-stop-err').textContent = e.message; }
    };
    panel.querySelectorAll('.bell-item').forEach(el => {
      const x = items[+el.dataset.i], err = el.querySelector('.bell-err');
      const act = async body => {
        if (!served) { err.textContent = 'Ответ работает с сервером досок: py tools/pm.py serve'; return; }
        try { await body(); await poll(); } catch (e) { err.textContent = e.message; }
      };
      el.querySelectorAll('.bell-opt').forEach(b => b.onclick = () => act(() => post('/api/work', {op: 'answer', ref: x.ref, problem: x.problem, option: +b.dataset.opt})));
      const send = el.querySelector('.bell-send');
      if (send) send.onclick = () => { const a = el.querySelector('.bell-answer').value.trim(); if (!a) { err.textContent = 'Напишите ответ или выберите вариант'; return; }
        act(() => post('/api/work', {op: 'answer', ref: x.ref, problem: x.problem, answer: a})); };
      const ap = el.querySelector('.bell-approve');
      if (ap) ap.onclick = () => act(() => post('/api/work', {op: 'approve_release', ref: x.ref}));
      const orc = el.querySelector('.bell-oracle');
      if (orc) orc.onclick = () => act(() => post('/api/work', {op: 'approve_oracle', ref: x.ref}));
      const rl = el.querySelector('.bell-relaunch');
      if (rl) rl.onclick = () => act(() => post('/api/launch', {ref: x.ref, run: x.run}));
      const rs = el.querySelector('.bell-resume');
      if (rs) rs.onclick = () => act(() => post('/api/work', {op: 'resume_all'}));
      const ev = el.querySelector('.bell-env');
      if (ev) ev.onclick = () => act(() => post('/api/work', {op: 'settings', changes: {environments: {[x.env_key]: x.fingerprint}}}));
      el.querySelectorAll('.bell-rec-retry, .bell-rec-done').forEach(b => b.onclick = () =>
        act(() => post('/api/work', {op: 'reconcile', operation: x.operation, decision: b.classList.contains('bell-rec-done') ? 'done' : 'retry'})));
    });
  }

  // Уведомление браузера о новом — только при открытой странице и с разрешения владельца.
  function announce(fresh) {
    if (!fresh.length || typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    const x = fresh[0];
    new Notification(`TaskTreker: нужен владелец${fresh.length > 1 ? ' (' + fresh.length + ')' : ''}`, {body: `${x.ref}: ${x.text || x.title}`.slice(0, 200), tag: 'tasktreker-bell'});
  }

  async function poll() {
    if (!served) return;
    try {
      const r = await fetch('/api/work'); const j = await r.json();
      if (!j.ok) return;
      items = j.data.notifications || [];
      const fresh = items.filter(x => !seen.has(x.id)); fresh.forEach(x => seen.add(x.id));
      announce(fresh); render();
    } catch (e) { /* сервер недоступен — показываем последнее известное */ }
  }

  btn.onclick = () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden && typeof Notification !== 'undefined' && Notification.permission === 'default') Notification.requestPermission();
  };
  document.addEventListener('keydown', e => { if (e.key === 'Escape') panel.hidden = true; });
  render(); poll(); if (served) setInterval(poll, 30000);
  return {poll};
})();
