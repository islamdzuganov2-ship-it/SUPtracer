/* Доски используют тот же контракт, что CLI и MCP. */
(() => {
  let data=D.workflow, query='', project='', status='', priority='', onlyFocus=false;
  try { const saved=JSON.parse(localStorage.getItem('tt-board-filters')||'{}');
    query=typeof saved.query==='string'?saved.query:''; project=typeof saved.project==='string'?saved.project:'';
    status=typeof saved.status==='string'?saved.status:''; priority=typeof saved.priority==='string'?saved.priority:''; onlyFocus=saved.onlyFocus===true;
  } catch {}
  const root=document.getElementById('boards-root');
  const labels={queued:'К выполнению',in_progress:'В работе',review:'Проверка',blocked:'Заблокировано',done:'Готово',cancelled:'Отменено'};
  const style=document.createElement('style');
  style.textContent=`.board-dialog button,#boards-root button{padding:7px 10px;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:7px;cursor:pointer}.board-dialog button:hover,#boards-root button:hover{border-color:var(--accent)}.board-tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:16px 0}.board-tools input,.board-tools select{padding:8px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--ink)}.board-columns{display:grid;grid-template-columns:repeat(5,minmax(210px,1fr));gap:12px;overflow:auto;padding-bottom:12px}.board-column{max-height:68vh;overflow:auto;background:var(--panel2);padding:10px;border-radius:12px}.board-task{background:var(--panel);border:1px solid var(--line);padding:13px;margin:10px 0;border-radius:9px}.board-task h4{margin:7px 0}.board-task small{display:block;color:var(--ink3);margin:6px 0}.board-task button{margin:4px 4px 0 0}.board-project{margin:22px 0}.board-project h2{font-size:19px}.board-focus{display:flex;gap:10px;flex-wrap:wrap}.board-dialog{width:min(800px,90vw);max-height:85vh;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:12px;padding:22px}.board-dialog textarea{box-sizing:border-box;width:100%;min-height:240px;background:var(--panel2);color:var(--ink);padding:12px}.board-dialog pre{white-space:pre-wrap;overflow-wrap:anywhere}.board-message{white-space:pre-wrap;color:var(--accent);margin:12px 0}`;
  document.head.append(style);
  const message=document.createElement('div'); message.className='board-message'; message.setAttribute('role','status');
  const dialog=document.createElement('dialog'); dialog.className='board-dialog'; document.body.append(dialog);
  function modal(title,body){dialog.innerHTML=`<h2>${esc(title)}</h2>${body}<p><button id="board-close">Закрыть</button></p>`;dialog.querySelector('#board-close').onclick=()=>dialog.close();dialog.showModal();}
  async function api(req){
    if(location.protocol==='file:')throw Error('Для изменения задач запустите py tools/pm.py serve. Пакеты можно просматривать и без сервера.');
    const r=await fetch('/api/work',{method:'POST',headers:{'Content-Type':'application/json','X-TaskTreker':'1'},body:JSON.stringify(req)});
    const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||'Операция не выполнена');return j.data;
  }
  async function refresh(){if(location.protocol!=='file:'){const r=await fetch('/api/work');const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||'Нет связи');data=j.data;}render();}
  async function change(req){try{await api(req);await refresh();message.textContent='Сохранено';}catch(e){message.textContent=e.message;}}
  function column(t){return t.status==='done'?'done':t.status==='in_progress'?'in_progress':t.status==='review'?'review':t.blocked_by.length||t.status==='blocked'?'blocked':'queued';}
  function render(){
    try {localStorage.setItem('tt-board-filters',JSON.stringify({query,project,status,priority,onlyFocus}));}catch{}
    root.innerHTML=`<h1>Доски задач</h1><p>Сначала план и критерии, затем выбор модели и запуск. Фокус влияет на порядок рекомендаций.</p>
      <details><summary>Фокус проектов (${data.focus.length})</summary><div class="board-focus">${Object.entries(data.projects).map(([k,p],i)=>`<label><input type="checkbox" data-focus="${i}" ${data.focus.includes(k)?'checked':''}> ${esc(p.name)}</label>`).join('')}</div></details>
      <div class="board-tools"><input id="board-search" aria-label="Поиск задач" placeholder="Найти задачу" value="${esc(query)}"><select id="board-project" aria-label="Проект доски"><option value="">Все проекты</option>${Object.entries(data.projects).map(([k,p])=>`<option value="${esc(k)}" ${project===k?'selected':''}>${esc(p.name)}</option>`).join('')}</select>
      <select id="board-status" aria-label="Статус задачи"><option value="">Все статусы</option>${Object.entries(labels).filter(([k])=>k!=='cancelled').map(([k,v])=>`<option value="${k}" ${status===k?'selected':''}>${v}</option>`).join('')}</select>
      <select id="board-priority" aria-label="Приоритет"><option value="">Все приоритеты</option>${['🔴','🟡','🟢'].map(k=>`<option ${priority===k?'selected':''}>${k}</option>`).join('')}</select>
      <label><input type="checkbox" id="board-only-focus" ${onlyFocus?'checked':''}> Только фокус</label><button id="board-next">Что взять следующим</button><button id="board-plan">Добавить план</button><button id="board-refresh">Обновить доски</button></div>
      <details><summary>Модели и правила экономии</summary><p>${esc(data.policy.availability)}. ${esc(data.policy.evidence)}.</p><ul>${data.policy.rules.map(v=>`<li>${esc(v)}</li>`).join('')}</ul><table><thead><tr><th>Модель</th><th>Сильные стороны</th><th>Ограничения</th></tr></thead><tbody>${data.models.map(m=>`<tr><td>${esc(m.id)}</td><td>${esc(m.strength)}</td><td>${esc(m.weakness)}</td></tr>`).join('')}</tbody></table></details><div id="board-list"></div>`;
    root.insertBefore(message,root.querySelector('#board-list'));
    const visible=data.tasks.filter(t=>(!project||t.project===project)&&(!onlyFocus||data.focus.includes(t.project))&&(!status||column(t)===status)&&(!priority||t.priority===priority)&&(!query||(t.title+' '+t.id).toLowerCase().includes(query.toLowerCase())));
    const keys=Object.keys(data.projects).filter(k=>(!project||project===k)&&(!onlyFocus||data.focus.includes(k))).sort((a,b)=>Number(data.focus.includes(b))-Number(data.focus.includes(a)));
    root.querySelector('#board-list').innerHTML=keys.map(k=>{const ts=visible.filter(t=>t.project===k);if(!ts.length&&(query||status||priority))return '';return `<section class="board-project"><h2>${data.focus.includes(k)?'★ ':''}${esc(data.projects[k].name)} · ${ts.length}</h2><div class="board-columns">${['queued','in_progress','review','blocked','done'].map(c=>`<div class="board-column"><h3>${labels[c]} · ${ts.filter(t=>column(t)===c).length}</h3>${ts.filter(t=>column(t)===c).map(t=>{
      const i=data.tasks.indexOf(t),run=data.runs.find(r=>r.ref===t.ref&&['queued','running'].includes(r.status));
      return `<article class="board-task"><small>${esc(t.id)} · ${esc(t.size||'?')} · ${esc(t.priority||'')}</small><h4>${esc(t.title)}</h4><small>${esc(t.route.model)} · ${esc(t.route.effort)}</small><small>${esc(t.route.reason)}${t.route.provisional?' — требуется уточнение':''}</small><small>Бюджет: ${t.route.total_token_budget.toLocaleString('ru')} токенов · ${t.route.max_attempts} попытки</small>${t.review_required?'<small>Лимит попыток достигнут: пересмотрите модель или декомпозицию</small>':""}${t.needs_plan&&t.status!=='done'?'<small>Нужна декомпозиция / критерии приёмки</small>':''}${t.blocked_by.length?`<small>Зависит от: ${esc(t.blocked_by.join(', '))}</small>`:''}${run?`<small>${run.status==='queued'?'Ожидает исполнителя':'Принята исполнителем'} · ${esc(run.id)}</small>`:''}<button data-detail="${i}">Паспорт</button><button data-priority="${i}">Приоритет</button>${!run&&t.status!=='done'&&!t.needs_plan&&!t.review_required&&!t.blocked_by.length?`<button data-launch="${i}">Запустить</button>`:''}</article>`;
    }).join('')}</div>`).join('')}</div></section>`;}).join('')||'<p>Нет досок по выбранным фильтрам.</p>';
    const search=root.querySelector('#board-search');search.oninput=()=>{const caret=search.selectionStart;query=search.value;render();const el=root.querySelector('#board-search');el.focus();el.setSelectionRange(caret,caret);};
    root.querySelector('#board-project').onchange=e=>{project=e.target.value;render();};
    root.querySelector('#board-status').onchange=e=>{status=e.target.value;render();};
    root.querySelector('#board-priority').onchange=e=>{priority=e.target.value;render();};
    root.querySelector('#board-only-focus').onchange=e=>{onlyFocus=e.target.checked;render();};
    root.querySelectorAll('[data-focus]').forEach(el=>el.onchange=()=>{const k=Object.keys(data.projects)[Number(el.dataset.focus)];change({op:'focus',projects:el.checked?[...data.focus,k]:data.focus.filter(v=>v!==k)});});
    root.querySelector('#board-refresh').onclick=()=>refresh().catch(e=>message.textContent=e.message);
    root.querySelector('#board-next').onclick=()=>{const ts=data.tasks.filter(t=>(!project||project===t.project)&&!['done','cancelled','in_progress','review','blocked'].includes(t.status)&&!t.needs_plan&&!t.review_required&&!t.blocked_by.length&&!data.runs.some(r=>r.ref===t.ref&&['queued','running'].includes(r.status))).slice(0,5);modal('Следующие задачи',`<p>Порядок: фокус → приоритет → размер. Зависимости и незавершённая проработка исключают запуск.</p>${ts.map(t=>`<p><b>${esc(t.ref)}</b> — ${esc(t.title)}<br>${esc(t.route.model)}: ${esc(t.route.reason)}</p>`).join('')||'<p>Готовых к запуску задач нет.</p>'}`);};
    root.querySelectorAll('[data-detail]').forEach(el=>el.onclick=()=>detail(data.tasks[Number(el.dataset.detail)]));
    root.querySelectorAll('[data-priority]').forEach(el=>el.onclick=()=>{const t=data.tasks[Number(el.dataset.priority)];change({op:'update',ref:t.ref,changes:{priority:['🔴','🟡','🟢'][(['🔴','🟡','🟢'].indexOf(t.priority)+1)%3]}});});
    root.querySelectorAll('[data-launch]').forEach(el=>el.onclick=async()=>{el.disabled=true;await change({op:'enqueue',ref:data.tasks[Number(el.dataset.launch)].ref});message.textContent+='\nЗапрос можно принять через tt_work claim. Сам сервер моделей не исполняет.';});
    root.querySelector('#board-plan').onclick=plan;
    root.querySelector('#board-plan').textContent='＋ Новая задача';
    BOARD_EXTRAS.decorate(root,data,visible,project);
  }
  function detail(t){
    modal(t.ref,`<p>${esc(t.title)}</p><p>${esc(t.why||'')}</p><p>Корень: ${esc(t.root||'не указан')} · зависимости: ${esc((t.depends_on||[]).join(', ')||'нет')}</p><h3>Критерии приёмки</h3><ul>${(t.acceptance||[]).map(a=>`<li>${esc(a)}</li>`).join('')}</ul><pre>${esc(JSON.stringify(t.route,null,2))}</pre><p>Бюджеты — ориентир исполнителю, не технический лимит модели. При превышении остановиться и пересмотреть план.</p><button id="board-packet">Пакет исполнителю</button><button id="board-edit">Проработать задачу</button>`);
    BOARD_EXTRAS.checklist({task:t,dialog,api,refresh,modal});
    dialog.querySelector('#board-edit').onclick=()=>{dialog.close();BOARD_EXTRAS.form({data,project,task:t,modal,dialog,api,refresh});};
    dialog.querySelector('#board-packet').onclick=async()=>{try{const p=await api({op:'packet',ref:t.ref});dialog.close();modal('Пакет исполнителю',`<pre>${esc(JSON.stringify(p,null,2))}</pre><p>CLI: py tools/pm.py work packet ${esc(t.ref)}</p>`);}catch(e){message.textContent=e.message;dialog.close();}};
  }
  function plan(){ BOARD_EXTRAS.form({data,project,modal,dialog,api,refresh}); }
  render();
  if(location.protocol!=='file:')refresh().catch(e=>message.textContent=e.message);
})();
