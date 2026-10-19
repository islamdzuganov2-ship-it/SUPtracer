/* Доски используют тот же контракт, что CLI и MCP. */
(() => {
  let data=D.workflow, query='', project='', status='', priority='', ai='', onlyFocus=false;
  try { const saved=JSON.parse(localStorage.getItem('tt-board-filters')||'{}');
    query=typeof saved.query==='string'?saved.query:''; project=typeof saved.project==='string'?saved.project:'';
    status=typeof saved.status==='string'?saved.status:''; priority=typeof saved.priority==='string'?saved.priority:''; ai=typeof saved.ai==='string'?saved.ai:''; onlyFocus=saved.onlyFocus===true;
  } catch {}
  const root=document.getElementById('boards-root');
  const labels={queued:'К выполнению',in_progress:'В работе',review:'Проверка',blocked:'Заблокировано',done:'Готово',cancelled:'Отменено'};
  const style=document.createElement('style');
  style.textContent=`.board-dialog button,#boards-root button{padding:7px 10px;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:7px;cursor:pointer}.board-dialog button:hover,#boards-root button:hover{border-color:var(--accent)}.board-tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:16px 0}.board-tools input,.board-tools select{padding:8px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--ink)}.board-columns{display:grid;grid-template-columns:repeat(5,minmax(210px,1fr));gap:12px;overflow:auto;padding-bottom:12px}.board-column{max-height:68vh;overflow:auto;background:var(--panel2);padding:10px;border-radius:12px}.board-task{background:var(--panel);border:1px solid var(--line);padding:13px;margin:10px 0;border-radius:9px}.board-task h4{margin:7px 0}.board-task small{display:block;color:var(--ink3);margin:6px 0}.board-task button{margin:4px 4px 0 0}.board-project{margin:22px 0}.board-project h2{font-size:19px}.board-focus{display:flex;gap:10px;flex-wrap:wrap}.board-dialog{width:min(800px,90vw);max-height:85vh;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:12px;padding:22px}.board-dialog textarea{box-sizing:border-box;width:100%;min-height:240px;background:var(--panel2);color:var(--ink);padding:12px}.board-dialog pre{white-space:pre-wrap;overflow-wrap:anywhere}.board-message{white-space:pre-wrap;color:var(--accent);margin:12px 0}.board-ai-row{display:flex;flex-wrap:wrap;gap:5px;margin:6px 0}.board-ai,.board-claim{font-size:10px;padding:3px 7px;border-radius:6px;border:1px solid var(--line);color:var(--ink2)}.board-ai-claude{border-color:#d97757;color:#d97757}.board-ai-codex{border-color:#36a7e7;color:#36a7e7}.board-claim{background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--ink)}.board-claim-stale{opacity:.75;font-style:italic}.board-type-tag{border-style:dashed}.board-columns{grid-template-columns:repeat(6,minmax(200px,1fr))!important}.board-column.is-drop{outline:2px dashed var(--accent)}.board-task.is-dragging{opacity:.5}.board-task[draggable=true]{cursor:grab}.board-waiting,.board-note,.board-assess{font-size:11px;margin:6px 0;padding:6px 8px;border-radius:8px;background:color-mix(in srgb,var(--accent) 10%,transparent)}.board-waiting span{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}.board-waiting button{font-size:11px;padding:4px 7px!important}.board-note{background:color-mix(in srgb,var(--bad) 10%,transparent)}.board-stuck{background:color-mix(in srgb,var(--bad) 14%,transparent)}.board-waiting small{display:block;margin-top:3px}.board-assess{background:color-mix(in srgb,#e6a34c 16%,transparent)}.board-status-actions{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.board-dialog textarea#board-prompt{min-height:180px;font-family:ui-monospace,monospace;font-size:12px}.board-reason{display:flex;flex-direction:column;gap:6px}.board-reason textarea{min-height:90px!important}.board-dialog .is-missing{border-color:var(--bad)!important}.board-muted{color:var(--ink3);font-size:12px}.board-dl{display:grid;grid-template-columns:minmax(140px,30%) 1fr;gap:6px 12px;font-size:13px}.board-dl dt{color:var(--ink3)}.board-dl dd{margin:0;overflow-wrap:anywhere}.board-workspace{display:flex;flex-direction:column;gap:3px;font-size:12px;padding:10px 12px;border-radius:10px;background:var(--panel2);overflow-wrap:anywhere}.board-launch{display:flex;flex-direction:column;gap:4px;margin-top:4px}.board-launch select{width:100%;box-sizing:border-box;font-size:11px;padding:6px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--ink)}.board-parallel{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:12px 0}.board-parallel>div{border:1px solid var(--line);border-radius:14px;padding:10px 14px;background:var(--panel)}.board-parallel h3{font-size:13px;margin:4px 0}.board-parallel p{font-size:12px;margin:8px 0}.board-parallel-empty{color:var(--ink3);font-size:12px}`;
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
  const providers=()=>data.providers||{codex:{name:'GPT / Codex'}};
  const aiName=k=>providers()[k]?.name||k;
  const time=v=>{const d=new Date(v);return isNaN(d)?'без срока':d.toLocaleString('ru',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});};
  // Метка занятости: по ней и человек, и другой ИИ видят, что задача уже в работе.
  function aiBadge(t){
    const def=data.settings?.default_executor;
    const who=t.executor?`<span class="board-ai board-ai-${esc(t.executor)}">ИИ: ${esc(aiName(t.executor))}</span>`:`<span class="board-ai">ИИ: любой${def?' · по умолчанию '+esc(aiName(def)):''}</span>`;
    const c=t.claim;
    const r=c&&data.runs.find(x=>x.id===c.run);
    const beat=r?.heartbeat_at&&r.heartbeat_at!==r.claimed_at?` · сигнал ${esc(time(r.heartbeat_at))}`:'';
    const lock=c?`<span class="board-claim${c.stale?' board-claim-stale':''}" title="${esc(c.run)} · ${esc(c.model||'')}">${c.stale?'⌛ Аренда истекла — можно перехватить':'🔒 Принята '+esc(aiName(c.agent))+(r?.claimed_at?' '+esc(time(r.claimed_at)):'')+beat} · ${esc(c.worker||'?')} · до ${esc(time(c.lease_until))}</span>`:'';
    const type=data.task_types?.[t.task_type];
    return `<div class="board-ai-row">${type?`<span class="board-ai board-type-tag">${esc(type.name)}</span>`:''}${who}${lock}</div>`;
  }
  function launchSelect(t,i){
    const keys=t.executor?[t.executor]:Object.keys(providers());
    const pick=t.executor||data.settings?.default_executor;
    return `<select data-launch-ai="${i}" aria-label="ИИ для запуска ${esc(t.id)}" title="Модель и усилия из плана модели задачи"${keys.length<2?' disabled':''}>${keys.map(k=>{const r=(t.routes||{})[k]||(k===t.route.provider?t.route:null);return `<option value="${esc(k)}" ${k===pick?'selected':''} title="${r?esc(r.model+' · '+r.effort+' — '+r.reason):''}">${esc(providers()[k]?.short||aiName(k))}${r?' · '+esc(r.model.replace(/^(gpt|claude)-/,''))+' · '+esc(r.effort):''}</option>`;}).join('')}</select>`;
  }
  // Тип задачи и рабочее место — то, что получит исполнитель в паспорте.
  function typeInfo(t){
    const type=data.task_types?.[t.task_type],p=data.projects[t.project]||{};
    const rows=type?Object.entries(type.fields).map(([k,label])=>[label,Array.isArray(t[k])?t[k].join(', '):t[k]]):[];
    if(t.constraints)rows.push(['Ограничения',t.constraints]);
    return `<p><b>${esc(type?.name||'Тип не указан')}</b></p>${rows.length?`<dl class="board-dl">${rows.map(([a,b])=>`<dt>${esc(a)}</dt><dd>${esc(b||'—')}</dd>`).join('')}</dl>`:''}<p class="board-workspace"><b>Рабочее место ИИ</b><span>Папка: <code>${esc(p.path||'не указана')}</code></span>${p.active_copy?`<span>Активная копия: <code>${esc(p.active_copy)}</code></span>`:''}<span>Репозиторий: ${p.repo?`<code>${esc(p.repo)}</code>`:'без удалённого репозитория'}</span></p>`;
  }
  function aiMatch(t){return !ai||(ai==='none'?!t.executor:ai==='busy'?!!(t.claim&&!t.claim.stale):t.executor===ai);}
  const COLUMNS=['queued','in_progress','review','blocked','done','cancelled'];
  // Запущенная с доски задача сразу в «В работе»: запуск — это решение владельца, даже если ИИ ещё не принял.
  function column(t){return t.status==='cancelled'?'cancelled':t.status==='done'?'done':t.status==='review'?'review':(t.status==='blocked'||(t.blocked_by.length&&t.status!=='in_progress'))?'blocked':(t.status==='in_progress'||t.claim||t.queued_run)?'in_progress':'queued';}
  function render(){
    try {localStorage.setItem('tt-board-filters',JSON.stringify({query,project,status,priority,ai,onlyFocus}));}catch{}
    root.innerHTML=`<h1>Доски задач</h1><p>Сначала план и критерии, затем выбор модели и запуск. Фокус влияет на порядок рекомендаций.</p>
      <details><summary>Фокус проектов (${data.focus.length})</summary><div class="board-focus">${Object.entries(data.projects).map(([k,p],i)=>`<label><input type="checkbox" data-focus="${i}" ${data.focus.includes(k)?'checked':''}> ${esc(p.name)}</label>`).join('')}</div></details>
      <div class="board-tools"><input id="board-search" aria-label="Поиск задач" placeholder="Найти задачу" value="${esc(query)}"><select id="board-project" aria-label="Проект доски"><option value="">Все проекты</option>${Object.entries(data.projects).map(([k,p])=>`<option value="${esc(k)}" ${project===k?'selected':''}>${esc(p.name)}</option>`).join('')}</select>
      <select id="board-status" aria-label="Статус задачи"><option value="">Все статусы</option>${Object.entries(labels).map(([k,v])=>`<option value="${k}" ${status===k?'selected':''}>${v}</option>`).join('')}</select>
      <select id="board-priority" aria-label="Приоритет"><option value="">Все приоритеты</option>${['🔴','🟡','🟢'].map(k=>`<option ${priority===k?'selected':''}>${k}</option>`).join('')}</select>
      <select id="board-ai" aria-label="ИИ-исполнитель"><option value="">Все ИИ</option>${Object.entries(providers()).map(([k,p])=>`<option value="${esc(k)}" ${ai===k?'selected':''}>${esc(p.name)}</option>`).join('')}<option value="none" ${ai==='none'?'selected':''}>Не назначены</option><option value="busy" ${ai==='busy'?'selected':''}>Заняты сейчас</option></select>
      <label><input type="checkbox" id="board-only-focus" ${onlyFocus?'checked':''}> Только фокус</label><button id="board-next">Что взять следующим</button><button id="board-plan">Добавить план</button><button id="board-refresh">Обновить доски</button><button id="board-split">⇄ Распределить между ИИ</button><label>ИИ по умолчанию <select id="board-default-ai" aria-label="ИИ по умолчанию">${Object.entries(providers()).map(([k,p])=>`<option value="${esc(k)}" ${data.settings?.default_executor===k?'selected':''}>${esc(p.name)}</option>`).join('')}</select></label></div>${parallel()}
      <details><summary>Модели и правила экономии</summary><p>${esc(data.policy.availability)}. ${esc(data.policy.evidence)}.</p><ul>${data.policy.rules.map(v=>`<li>${esc(v)}</li>`).join('')}</ul><table><thead><tr><th>ИИ</th><th>Модель</th><th>Сильные стороны</th><th>Ограничения</th></tr></thead><tbody>${data.models.map(m=>`<tr><td>${esc(aiName(m.provider))}</td><td>${esc(m.id)}${Object.values(data.lineup?.[m.provider]||{}).includes(m.id)?'':' · только явно'}</td><td>${esc(m.strength)}</td><td>${esc(m.weakness)}</td></tr>`).join('')}</tbody></table></details><div id="board-list"></div>`;
    root.insertBefore(message,root.querySelector('#board-list'));
    const visible=data.tasks.filter(t=>(!project||t.project===project)&&(!onlyFocus||data.focus.includes(t.project))&&(!status||column(t)===status)&&(!priority||t.priority===priority)&&aiMatch(t)&&(!query||(t.title+' '+t.id).toLowerCase().includes(query.toLowerCase())));
    const keys=Object.keys(data.projects).filter(k=>(!project||project===k)&&(!onlyFocus||data.focus.includes(k))).sort((a,b)=>Number(data.focus.includes(b))-Number(data.focus.includes(a)));
    root.querySelector('#board-list').innerHTML=keys.map(k=>{const ts=visible.filter(t=>t.project===k);if(!ts.length&&(query||status||priority||ai))return '';return `<section class="board-project"><h2>${data.focus.includes(k)?'★ ':''}${esc(data.projects[k].name)} · ${ts.length}</h2><div class="board-columns">${COLUMNS.map(c=>`<div class="board-column" data-column="${c}"><h3>${labels[c]} · ${ts.filter(t=>column(t)===c).length}</h3>${ts.filter(t=>column(t)===c).map(t=>{
      const i=data.tasks.indexOf(t),run=data.runs.find(r=>r.ref===t.ref&&['queued','running'].includes(r.status));
      return `<article class="board-task" draggable="true" data-drag="${i}"><small>${esc(t.id)} · ${esc(t.size||'?')} · ${esc(t.priority||'')}</small><h4>${esc(t.title)}</h4>${aiBadge(t)}<small>${esc(t.route.model)} · ${esc(t.route.effort)}</small><small>${esc(t.route.reason)}${t.route.provisional?' — требуется уточнение':''}</small><small>Бюджет: ${t.route.total_token_budget.toLocaleString('ru')} токенов · ${t.route.max_attempts} попытки</small>${t.review_required?'<small>Лимит попыток достигнут: пересмотрите модель или декомпозицию</small>':""}${t.needs_plan&&t.status!=='done'?'<small>Нужна декомпозиция / критерии приёмки</small>':''}${t.blocked_by.length?`<small>Зависит от: ${esc(t.blocked_by.join(', '))}</small>`:''}${run&&run.status==='queued'?launchState(t,run,i):''}${t.status_note&&['blocked','cancelled'].includes(t.status)?`<div class="board-note">${t.status==='blocked'?'⛔':'✕'} ${esc(t.status_note)}</div>`:''}${t.complexity?.needs_assessment&&!['done','cancelled'].includes(t.status)?`<div class="board-assess" title="${esc((t.complexity.conflicts||[]).join('; ')||'аналитика и декомпозиция оцениваются ИИ по умолчанию')}">🔍 Сначала оценка сложности · уверенность ${t.complexity.confidence}</div>`:''}<button data-detail="${i}">Паспорт</button><button data-priority="${i}">Приоритет</button>${!run&&!['done','cancelled','blocked','review','in_progress'].includes(t.status)&&!t.needs_plan&&!t.review_required&&!t.blocked_by.length?`<span class="board-launch">${launchSelect(t,i)}<button data-launch="${i}">${launchLabel(t,null)}</button></span>`:''}</article>`;
    }).join('')}</div>`).join('')}</div></section>`;}).join('')||'<p>Нет досок по выбранным фильтрам.</p>';
    const search=root.querySelector('#board-search');search.oninput=()=>{const caret=search.selectionStart;query=search.value;render();const el=root.querySelector('#board-search');el.focus();el.setSelectionRange(caret,caret);};
    root.querySelector('#board-project').onchange=e=>{project=e.target.value;render();};
    root.querySelector('#board-status').onchange=e=>{status=e.target.value;render();};
    root.querySelector('#board-priority').onchange=e=>{priority=e.target.value;render();};
    root.querySelector('#board-ai').onchange=e=>{ai=e.target.value;render();};
    root.querySelector('#board-default-ai').onchange=e=>change({op:'settings',changes:{default_executor:e.target.value}});
    root.querySelector('#board-split').onclick=splitDialog;
    root.querySelector('#board-only-focus').onchange=e=>{onlyFocus=e.target.checked;render();};
    root.querySelectorAll('[data-focus]').forEach(el=>el.onchange=()=>{const k=Object.keys(data.projects)[Number(el.dataset.focus)];change({op:'focus',projects:el.checked?[...data.focus,k]:data.focus.filter(v=>v!==k)});});
    root.querySelector('#board-refresh').onclick=()=>refresh().catch(e=>message.textContent=e.message);
    root.querySelector('#board-next').onclick=()=>{const ts=data.tasks.filter(t=>(!project||project===t.project)&&!['done','cancelled','in_progress','review','blocked'].includes(t.status)&&!t.needs_plan&&!t.review_required&&!t.blocked_by.length&&!data.runs.some(r=>r.ref===t.ref&&['queued','running'].includes(r.status))).slice(0,5);modal('Следующие задачи',`<p>Порядок: фокус → приоритет → размер. Зависимости и незавершённая проработка исключают запуск.</p>${ts.map(t=>`<p><b>${esc(t.ref)}</b> — ${esc(t.title)}<br>${Object.entries(t.routes||{[t.route.provider]:t.route}).map(([k,r])=>`${esc(aiName(k))}: ${esc(r.model)} · ${esc(r.effort)}`).join(' · ')}<br><small>${esc(t.route.reason)}</small></p>`).join('')||'<p>Готовых к запуску задач нет.</p>'}`);};
    root.querySelectorAll('[data-detail]').forEach(el=>el.onclick=()=>detail(data.tasks[Number(el.dataset.detail)]));
    root.querySelectorAll('[data-priority]').forEach(el=>el.onclick=()=>{const t=data.tasks[Number(el.dataset.priority)];change({op:'update',ref:t.ref,changes:{priority:['🔴','🟡','🟢'][(['🔴','🟡','🟢'].indexOf(t.priority)+1)%3]}});});
    root.querySelectorAll('[data-launch-ai]').forEach(sel=>sel.onchange=()=>{const b=root.querySelector(`[data-launch="${sel.dataset.launchAi}"]`);if(b)b.textContent=launchLabel(data.tasks[Number(sel.dataset.launchAi)],sel.value);});
    root.querySelectorAll('[data-launch]').forEach(el=>{const sel=root.querySelector(`[data-launch-ai="${el.dataset.launch}"]`);if(sel)el.textContent=launchLabel(data.tasks[Number(el.dataset.launch)],sel.value);});
    root.querySelectorAll('[data-launch]').forEach(el=>el.onclick=async()=>{el.disabled=true;const t=data.tasks[Number(el.dataset.launch)],agent=root.querySelector(`[data-launch-ai="${el.dataset.launch}"]`)?.value;
      try{if(canLaunch(agent)){await openSession(t,agent);}else{const r=await api({op:'enqueue',ref:t.ref,agent});await refresh();showPrompt(t,r);}}catch(e){message.textContent=e.message;el.disabled=false;}});
    root.querySelectorAll('[data-relaunch]').forEach(el=>el.onclick=async()=>{el.disabled=true;const t=data.tasks[Number(el.dataset.relaunch)],run=data.runs.find(r=>r.ref===t.ref&&r.status==='queued');try{await openSession(t,run?.agent||data.settings?.default_executor);}catch(e){message.textContent=e.message;el.disabled=false;}});
    root.querySelectorAll('[data-prompt]').forEach(el=>el.onclick=async()=>{const t=data.tasks[Number(el.dataset.prompt)];try{showPrompt(t,await api({op:'prompt',ref:t.ref}));}catch(e){message.textContent=e.message;}});
    root.querySelectorAll('[data-unlaunch]').forEach(el=>el.onclick=()=>{const t=data.tasks[Number(el.dataset.unlaunch)],run=data.runs.find(r=>r.ref===t.ref&&r.status==='queued');if(run)change({op:'cancel',ref:t.ref,run:run.id,evidence:'Владелец отменил запуск с доски'});});
    dragAndDrop();
    root.querySelector('#board-plan').onclick=plan;
    root.querySelector('#board-plan').textContent='＋ Новая задача';
    BOARD_EXTRAS.decorate(root,data,visible,project);
  }
  function detail(t){
    modal(t.ref,`<p>${esc(t.title)}</p><p>${esc(t.why||'')}</p>${typeInfo(t)}<p>Корень: ${esc(t.root||'не указан')} · зависимости: ${esc((t.depends_on||[]).join(', ')||'нет')}</p><h3>Критерии приёмки</h3><ul>${(t.acceptance||[]).map(a=>`<li>${esc(a)}</li>`).join('')}</ul><h3>План модели</h3><table><thead><tr><th>ИИ</th><th>Модель</th><th>Усилия</th><th>Бюджет</th><th>Почему</th></tr></thead><tbody>${Object.entries(t.routes||{}).map(([k,r])=>`<tr><td>${esc(aiName(k))}${k===t.route.provider?' ✓':''}</td><td>${esc(r.model)}</td><td>${esc(r.effort)}</td><td>${(r.total_token_budget||0).toLocaleString('ru')}</td><td>${esc(r.reason)}${r.source==='manual'?' (ручной выбор)':''}</td></tr>`).join('')}</tbody></table><details><summary>Текущий маршрут целиком</summary><pre>${esc(JSON.stringify(t.route,null,2))}</pre></details><p>Бюджеты — ориентир исполнителю, не технический лимит модели. При превышении остановиться и пересмотреть план.</p>${complexityInfo(t)}<h3>Статус: ${esc(labels[column(t)]||t.status)}</h3>${t.status_note?`<p>${esc(t.status_note)}</p>`:''}<div class="board-status-actions">${statusButtons(t)}</div>${(t.status_history||[]).length?`<details><summary>История статуса</summary><ul>${t.status_history.slice().reverse().map(h=>`<li>${esc(h.at.slice(0,16).replace('T',' '))}: ${esc(labels[h.from_status]||h.from_status)} → ${esc(labels[h.to]||h.to)}${h.reason?' — '+esc(h.reason):''}</li>`).join('')}</ul></details>`:''}<button id="board-packet">Пакет исполнителю</button><button id="board-edit">Проработать задачу</button>`);
    dialog.querySelectorAll('[data-move]').forEach(el=>el.onclick=()=>{dialog.close();moveTo(t,el.dataset.move);});
    BOARD_EXTRAS.checklist({task:t,dialog,api,refresh,modal});
    dialog.querySelector('#board-edit').onclick=()=>{dialog.close();BOARD_EXTRAS.form({data,project,task:t,modal,dialog,api,refresh});};
    dialog.querySelector('#board-packet').onclick=async()=>{try{const p=await api({op:'packet',ref:t.ref});dialog.close();modal('Пакет исполнителю',`<pre>${esc(JSON.stringify(p,null,2))}</pre><p>CLI: py tools/pm.py work packet ${esc(t.ref)} --agent ${esc(p.route.provider)}</p><p>Взять задачу: tt_work op=take ref=${esc(t.ref)} model=${esc(p.route.model)} worker=&lt;чат&gt;</p>`);}catch(e){message.textContent=e.message;dialog.close();}};
  }
  // Открыть сессию можно, если сервер нашёл командную строку ИИ и доска открыта с сервера.
  const canLaunch=agent=>location.protocol!=='file:'&&!!(data.launchers?.[agent]?.available&&data.launchers[agent].can_open_window);
  function launchLabel(t,agent){agent=agent||t.executor||data.settings?.default_executor;const verb=t.complexity?.needs_assessment?'Оценить':'Запустить';return canLaunch(agent)?verb:'Поставить в очередь';}
  const ago=v=>Math.max(0,Math.round((Date.now()-new Date(v))/60000));
  // Честное состояние: поставлена → сессия открыта → принята (🔒 на карточке) → работает.
  function launchState(t,run,i){
    const who=run.agent?esc(aiName(run.agent)):'ИИ',route=run.packet?.route?` · ${esc(run.packet.route.model)} · ${esc(run.packet.route.effort)}`:'';
    const phase=run.phase==='assess'?' (оценка)':'';
    let head,cls='board-waiting',extra='';
    if(run.diagnosis){cls+=' board-stuck';head=`⛔ Сессия ${who} открылась, но завершилась с ошибкой: ${esc(run.diagnosis.message)}`;extra=canLaunch(run.agent)?`<button data-relaunch="${i}">Перезапустить</button>`:'';}
    else if(!run.launched_at){head=`📥 Поставлена в очередь для ${who}${phase} — сессия не открыта`;if(canLaunch(run.agent))extra=`<button data-relaunch="${i}">Открыть сессию</button>`;}
    else if(ago(run.launched_at)<5){head=`🖥 Сессия ${who}${phase} открыта ${esc(time(run.launched_at))} — ждём, пока ИИ примет`;}
    else{cls+=' board-stuck';head=`⚠ Сессия ${who} открыта ${ago(run.launched_at)} мин назад, ИИ не принял задачу. Проверьте окно сессии или перезапустите`;extra=canLaunch(run.agent)?`<button data-relaunch="${i}">Перезапустить</button>`:'';}
    const wt=run.launch?.worktree?`<small>Рабочая копия: ${esc(run.launch.worktree.branch)}</small>`:'';
    return `<div class="${cls}">${head}${route} · ${esc(run.id)}${wt}<span>${extra}<button data-prompt="${i}">Задание для ИИ</button><button data-unlaunch="${i}">Отменить запуск</button></span></div>`;
  }
  async function openSession(t,agent){
    const r=await fetch('/api/launch',{method:'POST',headers:{'Content-Type':'application/json','X-TaskTreker':'1'},body:JSON.stringify({ref:t.ref,agent})});
    const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||'Сессию открыть не удалось');
    await refresh();const d=j.data;
    modal('Сессия открыта: '+t.id,`<p>Открыто окно <b>${esc(aiName(d.agent))}</b>: ${esc(d.route.model)} · ${esc(d.route.effort)}, запуск ${esc(d.run)}.</p><p>Папка: <code>${esc(d.cwd)}</code>${d.worktree?`<br>Отдельная рабочая копия, ветка <code>${esc(d.worktree.branch)}</code>`:''}</p><p>Первым шагом ИИ примет задачу. Тогда на карточке появится 🔒 с именем <code>${esc(d.worker)}</code>. Если окно спросит о доверии к папке или о разрешениях — ответьте в нём.</p><details><summary>Задание, переданное ИИ</summary><pre>${esc(d.prompt)}</pre></details>`);
  }
  // Задание для ИИ: для очереди без открытой сессии — вставляется в чат вручную.
  function showPrompt(t,r){
    const ai=aiName(r.route?.provider||'');
    modal('Задача запущена: '+t.id,`<p>Запуск <b>${esc(r.run)}</b> ждёт ${esc(ai)}${r.phase==='assess'?' — сначала оценка сложности':''}: ${esc(r.route?.model||'')} · ${esc(r.route?.effort||'')}.</p><p>Откройте новую сессию ${esc(ai)} в папке проекта: брифинг сам покажет этот запуск. Или вставьте задание в уже открытый чат:</p><textarea id="board-prompt" readonly rows="9">${esc(r.prompt)}</textarea><p><button id="board-copy">Скопировать задание</button> <span id="board-copied" role="status"></span></p><p class="board-muted">Когда ИИ примет запуск (claim), на карточке появится 🔒 с его именем и сроком аренды.</p>`);
    dialog.querySelector('#board-copy').onclick=async()=>{const area=dialog.querySelector('#board-prompt');try{await navigator.clipboard.writeText(area.value);}catch{area.select();document.execCommand('copy');}dialog.querySelector('#board-copied').textContent='Скопировано';};
  }
  // Ручной перенос: мышью между колонками или кнопками в паспорте.
  function dragAndDrop(){
    root.querySelectorAll('[data-drag]').forEach(el=>el.ondragstart=e=>{e.dataTransfer.setData('text/plain',el.dataset.drag);el.classList.add('is-dragging');});
    root.querySelectorAll('[data-drag]').forEach(el=>el.ondragend=()=>el.classList.remove('is-dragging'));
    root.querySelectorAll('[data-column]').forEach(col=>{
      col.ondragover=e=>{e.preventDefault();col.classList.add('is-drop');};
      col.ondragleave=()=>col.classList.remove('is-drop');
      col.ondrop=e=>{e.preventDefault();col.classList.remove('is-drop');const t=data.tasks[Number(e.dataTransfer.getData('text/plain'))];if(t&&column(t)!==col.dataset.column)moveTo(t,col.dataset.column);};
    });
  }
  function moveTo(t,target){
    if(target==='done'){message.textContent='«Готово» — только с проверкой критериев: отметьте их в паспорте и нажмите «Завершить с проверкой».';detail(t);return;}
    const needReason=['blocked','cancelled'].includes(target)||t.status==='done';
    if(!needReason){change({op:'status',ref:t.ref,status:target});return;}
    const title={blocked:'Заблокировать',cancelled:'Отменить задачу',queued:'Вернуть закрытую задачу'}[target]||'Сменить статус';
    modal(title+': '+t.id,`<label class="board-reason">Причина — увидят исполнители и история задачи<textarea id="board-reason" required maxlength="1000"></textarea></label><p><button id="board-reason-ok">${esc(title)}</button></p><p id="board-reason-error" role="alert"></p>`);
    dialog.querySelector('#board-reason-ok').onclick=async()=>{const reason=dialog.querySelector('#board-reason').value.trim();if(!reason){dialog.querySelector('#board-reason').classList.add('is-missing');return;}
      try{await api({op:'status',ref:t.ref,status:target,reason});dialog.close();await refresh();message.textContent='Сохранено';}catch(e){dialog.querySelector('#board-reason-error').textContent=e.message;}};
  }
  function statusButtons(t){
    const cur=column(t),open=!['done','cancelled'].includes(t.status);
    const b=[];
    if(t.status!=='in_progress'&&open&&!t.claim)b.push(['in_progress','▶ В работу']);
    if(t.status!=='review'&&open)b.push(['review','Проверка']);
    if(t.status!=='blocked'&&open)b.push(['blocked','⛔ Заблокировать']);
    if(['blocked','cancelled','in_progress','review','done'].includes(t.status))b.push(['queued','↺ Вернуть в «К выполнению»']);
    if(t.status!=='cancelled')b.push(['cancelled','✕ Отменить']);
    return b.filter(([s])=>s!==cur||s==='queued').map(([s,l])=>`<button data-move="${s}">${l}</button>`).join('');
  }
  function complexityInfo(t){
    const c=t.complexity;if(!c)return '';
    const a=t.assessment;
    return `<h3>Сложность задачи</h3><p>Балл <b>${c.score}</b> · класс модели ${c.tier} · уверенность ${c.confidence}${c.needs_assessment?' · <b>нужна оценка ИИ</b>':''}${c.floor?' · высокий риск поднял класс':''}</p>${c.conflicts.length?`<p>Противоречия: ${esc(c.conflicts.join('; '))}</p>`:''}${c.unknown.length?`<p>Не указано: ${esc(c.unknown.join(', '))}</p>`:''}${a?`<p>Оценка ИИ (${esc(a.by?.model||a.by?.agent||'')}): сложность ${a.complexity}, неопределённость ${a.uncertainty}, широта ${a.breadth} — ${esc(a.reasons)}</p>`:''}<details><summary>Вклад факторов</summary><table><thead><tr><th>Фактор</th><th>Значение</th><th>Вес</th><th>Вклад</th></tr></thead><tbody>${c.factors.map(f=>`<tr><td>${esc(f.label)}</td><td>${esc(f.note)}</td><td>${f.weight}</td><td>${f.contribution}</td></tr>`).join('')}</tbody></table></details>`;
  }
  // Кто из ИИ что держит прямо сейчас: одна строка на запуск.
  function parallel(){
    const running=data.tasks.filter(t=>t.claim&&(!project||t.project===project));
    if(!running.length)return '<p class="board-parallel-empty">Параллельная работа: сейчас ни один ИИ не держит задач.</p>';
    const by={};running.forEach(t=>(by[t.claim.agent||'?']??=[]).push(t));
    return `<div class="board-parallel" aria-label="Параллельная работа ИИ">${Object.entries(by).map(([k,ts])=>`<div><h3>${esc(aiName(k))} · ${ts.length}</h3>${ts.map(t=>`<p class="${t.claim.stale?'board-claim-stale':''}"><b>${esc(t.ref)}</b> ${esc(t.title)}<br><small>${esc(t.claim.worker||'?')} · ${esc(t.claim.model||'')} · ${t.claim.stale?'аренда истекла':'до '+esc(time(t.claim.lease_until))}${(t.scope||[]).length?' · область: '+esc(t.scope.join(', ')):''}</small></p>`).join('')}</div>`).join('')}</div>`;
  }
  async function splitDialog(){
    const k=project||data.focus[0];
    if(!k){message.textContent='Выберите проект доски: распределение делается внутри одного проекта.';return;}
    const agents=Object.keys(providers());
    try{
      const r=await api({op:'split',project:k,agents,dry_run:true});
      modal('Распределение: '+data.projects[k].name,`<p>Связанные задачи (зависимости, общая область scope) уходят одному ИИ целиком; группы раскладываются по наименьшей загрузке в очках размера S=1, M=3, L=8. Назначенные вручную, занятые и закрытые задачи не меняются.</p><p>${agents.map(a=>`${esc(aiName(a))}: ${r.load[a]} очк.`).join(' · ')}</p>${r.groups.map(g=>`<p><b>${esc(aiName(g.agent))}</b> · ${g.points} очк. · ${esc(g.why)}<br>${g.tasks.map(esc).join(', ')}</p>`).join('')||'<p>Свободных задач для распределения нет.</p>'}${r.groups.length?'<button id="board-split-apply">Применить распределение</button>':''}`);
      const apply=dialog.querySelector('#board-split-apply');
      if(apply)apply.onclick=async()=>{dialog.close();await change({op:'split',project:k,agents,dry_run:false});};
    }catch(e){message.textContent=e.message;}
  }
  function plan(){ BOARD_EXTRAS.form({data,project,modal,dialog,api,refresh}); }
  render();
  if(location.protocol!=='file:')refresh().catch(e=>message.textContent=e.message);
})();
