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
  style.textContent=`.board-dialog button,#boards-root button{padding:7px 10px;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:7px;cursor:pointer}.board-dialog button:hover,#boards-root button:hover{border-color:var(--accent)}.board-tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:16px 0}.board-tools input,.board-tools select{padding:8px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--ink)}.board-columns{display:grid;grid-template-columns:repeat(5,minmax(210px,1fr));gap:12px;overflow:auto;padding-bottom:12px}.board-column{max-height:68vh;overflow:auto;background:var(--panel2);padding:10px;border-radius:12px}.board-task{background:var(--panel);border:1px solid var(--line);padding:13px;margin:10px 0;border-radius:9px}.board-task h4{margin:7px 0}.board-task small{display:block;color:var(--ink3);margin:6px 0}.board-task button{margin:4px 4px 0 0}.board-project{margin:22px 0}.board-project h2{font-size:19px}.board-focus{display:flex;gap:10px;flex-wrap:wrap}.board-dialog{width:min(800px,90vw);max-height:85vh;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:12px;padding:22px}.board-dialog textarea{box-sizing:border-box;width:100%;min-height:240px;background:var(--panel2);color:var(--ink);padding:12px}.board-dialog pre{white-space:pre-wrap;overflow-wrap:anywhere}.board-message{white-space:pre-wrap;color:var(--accent);margin:12px 0}.board-ai-row{display:flex;flex-wrap:wrap;gap:5px;margin:6px 0}.board-ai,.board-claim{font-size:10px;padding:3px 7px;border-radius:6px;border:1px solid var(--line);color:var(--ink2)}.board-ai-claude{border-color:#d97757;color:#d97757}.board-ai-codex{border-color:#36a7e7;color:#36a7e7}.board-claim{background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--ink)}.board-claim-stale{opacity:.75;font-style:italic}.board-launch{display:flex;flex-direction:column;gap:4px;margin-top:4px}.board-launch select{width:100%;box-sizing:border-box;font-size:11px;padding:6px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--ink)}.board-parallel{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:12px 0}.board-parallel>div{border:1px solid var(--line);border-radius:14px;padding:10px 14px;background:var(--panel)}.board-parallel h3{font-size:13px;margin:4px 0}.board-parallel p{font-size:12px;margin:8px 0}.board-parallel-empty{color:var(--ink3);font-size:12px}`;
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
    const lock=c?`<span class="board-claim${c.stale?' board-claim-stale':''}" title="${esc(c.run)} · ${esc(c.model||'')}">${c.stale?'⌛ Аренда истекла — можно перехватить':'🔒 В работе у '+esc(aiName(c.agent))} · ${esc(c.worker||'?')} · до ${esc(time(c.lease_until))}</span>`:'';
    return `<div class="board-ai-row">${who}${lock}</div>`;
  }
  function launchSelect(t,i){
    const keys=t.executor?[t.executor]:Object.keys(providers());
    const pick=t.executor||data.settings?.default_executor;
    return `<select data-launch-ai="${i}" aria-label="ИИ для запуска ${esc(t.id)}" title="Модель и усилия из плана модели задачи"${keys.length<2?' disabled':''}>${keys.map(k=>{const r=(t.routes||{})[k]||(k===t.route.provider?t.route:null);return `<option value="${esc(k)}" ${k===pick?'selected':''} title="${r?esc(r.model+' · '+r.effort+' — '+r.reason):''}">${esc(providers()[k]?.short||aiName(k))}${r?' · '+esc(r.model.replace(/^(gpt|claude)-/,''))+' · '+esc(r.effort):''}</option>`;}).join('')}</select>`;
  }
  function aiMatch(t){return !ai||(ai==='none'?!t.executor:ai==='busy'?!!(t.claim&&!t.claim.stale):t.executor===ai);}
  function column(t){return t.status==='done'?'done':t.status==='in_progress'?'in_progress':t.status==='review'?'review':t.blocked_by.length||t.status==='blocked'?'blocked':'queued';}
  function render(){
    try {localStorage.setItem('tt-board-filters',JSON.stringify({query,project,status,priority,ai,onlyFocus}));}catch{}
    root.innerHTML=`<h1>Доски задач</h1><p>Сначала план и критерии, затем выбор модели и запуск. Фокус влияет на порядок рекомендаций.</p>
      <details><summary>Фокус проектов (${data.focus.length})</summary><div class="board-focus">${Object.entries(data.projects).map(([k,p],i)=>`<label><input type="checkbox" data-focus="${i}" ${data.focus.includes(k)?'checked':''}> ${esc(p.name)}</label>`).join('')}</div></details>
      <div class="board-tools"><input id="board-search" aria-label="Поиск задач" placeholder="Найти задачу" value="${esc(query)}"><select id="board-project" aria-label="Проект доски"><option value="">Все проекты</option>${Object.entries(data.projects).map(([k,p])=>`<option value="${esc(k)}" ${project===k?'selected':''}>${esc(p.name)}</option>`).join('')}</select>
      <select id="board-status" aria-label="Статус задачи"><option value="">Все статусы</option>${Object.entries(labels).filter(([k])=>k!=='cancelled').map(([k,v])=>`<option value="${k}" ${status===k?'selected':''}>${v}</option>`).join('')}</select>
      <select id="board-priority" aria-label="Приоритет"><option value="">Все приоритеты</option>${['🔴','🟡','🟢'].map(k=>`<option ${priority===k?'selected':''}>${k}</option>`).join('')}</select>
      <select id="board-ai" aria-label="ИИ-исполнитель"><option value="">Все ИИ</option>${Object.entries(providers()).map(([k,p])=>`<option value="${esc(k)}" ${ai===k?'selected':''}>${esc(p.name)}</option>`).join('')}<option value="none" ${ai==='none'?'selected':''}>Не назначены</option><option value="busy" ${ai==='busy'?'selected':''}>Заняты сейчас</option></select>
      <label><input type="checkbox" id="board-only-focus" ${onlyFocus?'checked':''}> Только фокус</label><button id="board-next">Что взять следующим</button><button id="board-plan">Добавить план</button><button id="board-refresh">Обновить доски</button><button id="board-split">⇄ Распределить между ИИ</button><label>ИИ по умолчанию <select id="board-default-ai" aria-label="ИИ по умолчанию">${Object.entries(providers()).map(([k,p])=>`<option value="${esc(k)}" ${data.settings?.default_executor===k?'selected':''}>${esc(p.name)}</option>`).join('')}</select></label></div>${parallel()}
      <details><summary>Модели и правила экономии</summary><p>${esc(data.policy.availability)}. ${esc(data.policy.evidence)}.</p><ul>${data.policy.rules.map(v=>`<li>${esc(v)}</li>`).join('')}</ul><table><thead><tr><th>ИИ</th><th>Модель</th><th>Сильные стороны</th><th>Ограничения</th></tr></thead><tbody>${data.models.map(m=>`<tr><td>${esc(aiName(m.provider))}</td><td>${esc(m.id)}${Object.values(data.lineup?.[m.provider]||{}).includes(m.id)?'':' · только явно'}</td><td>${esc(m.strength)}</td><td>${esc(m.weakness)}</td></tr>`).join('')}</tbody></table></details><div id="board-list"></div>`;
    root.insertBefore(message,root.querySelector('#board-list'));
    const visible=data.tasks.filter(t=>(!project||t.project===project)&&(!onlyFocus||data.focus.includes(t.project))&&(!status||column(t)===status)&&(!priority||t.priority===priority)&&aiMatch(t)&&(!query||(t.title+' '+t.id).toLowerCase().includes(query.toLowerCase())));
    const keys=Object.keys(data.projects).filter(k=>(!project||project===k)&&(!onlyFocus||data.focus.includes(k))).sort((a,b)=>Number(data.focus.includes(b))-Number(data.focus.includes(a)));
    root.querySelector('#board-list').innerHTML=keys.map(k=>{const ts=visible.filter(t=>t.project===k);if(!ts.length&&(query||status||priority||ai))return '';return `<section class="board-project"><h2>${data.focus.includes(k)?'★ ':''}${esc(data.projects[k].name)} · ${ts.length}</h2><div class="board-columns">${['queued','in_progress','review','blocked','done'].map(c=>`<div class="board-column"><h3>${labels[c]} · ${ts.filter(t=>column(t)===c).length}</h3>${ts.filter(t=>column(t)===c).map(t=>{
      const i=data.tasks.indexOf(t),run=data.runs.find(r=>r.ref===t.ref&&['queued','running'].includes(r.status));
      return `<article class="board-task"><small>${esc(t.id)} · ${esc(t.size||'?')} · ${esc(t.priority||'')}</small><h4>${esc(t.title)}</h4>${aiBadge(t)}<small>${esc(t.route.model)} · ${esc(t.route.effort)}</small><small>${esc(t.route.reason)}${t.route.provisional?' — требуется уточнение':''}</small><small>Бюджет: ${t.route.total_token_budget.toLocaleString('ru')} токенов · ${t.route.max_attempts} попытки</small>${t.review_required?'<small>Лимит попыток достигнут: пересмотрите модель или декомпозицию</small>':""}${t.needs_plan&&t.status!=='done'?'<small>Нужна декомпозиция / критерии приёмки</small>':''}${t.blocked_by.length?`<small>Зависит от: ${esc(t.blocked_by.join(', '))}</small>`:''}${run&&run.status==='queued'?`<small>Ожидает исполнителя${run.agent?' '+esc(aiName(run.agent)):''} · ${esc(run.id)}${run.packet?.route?' · '+esc(run.packet.route.model)+' · '+esc(run.packet.route.effort):''}</small>`:''}<button data-detail="${i}">Паспорт</button><button data-priority="${i}">Приоритет</button>${!run&&t.status!=='done'&&!t.needs_plan&&!t.review_required&&!t.blocked_by.length?`<span class="board-launch">${launchSelect(t,i)}<button data-launch="${i}">Запустить</button></span>`:''}</article>`;
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
    root.querySelectorAll('[data-launch]').forEach(el=>el.onclick=async()=>{el.disabled=true;const t=data.tasks[Number(el.dataset.launch)],agent=root.querySelector(`[data-launch-ai="${el.dataset.launch}"]`)?.value;await change({op:'enqueue',ref:t.ref,agent});message.textContent+='\nЗапрос можно принять через tt_work claim. Сам сервер моделей не исполняет.';});
    root.querySelector('#board-plan').onclick=plan;
    root.querySelector('#board-plan').textContent='＋ Новая задача';
    BOARD_EXTRAS.decorate(root,data,visible,project);
  }
  function detail(t){
    modal(t.ref,`<p>${esc(t.title)}</p><p>${esc(t.why||'')}</p><p>Корень: ${esc(t.root||'не указан')} · зависимости: ${esc((t.depends_on||[]).join(', ')||'нет')}</p><h3>Критерии приёмки</h3><ul>${(t.acceptance||[]).map(a=>`<li>${esc(a)}</li>`).join('')}</ul><h3>План модели</h3><table><thead><tr><th>ИИ</th><th>Модель</th><th>Усилия</th><th>Бюджет</th><th>Почему</th></tr></thead><tbody>${Object.entries(t.routes||{}).map(([k,r])=>`<tr><td>${esc(aiName(k))}${k===t.route.provider?' ✓':''}</td><td>${esc(r.model)}</td><td>${esc(r.effort)}</td><td>${(r.total_token_budget||0).toLocaleString('ru')}</td><td>${esc(r.reason)}${r.source==='manual'?' (ручной выбор)':''}</td></tr>`).join('')}</tbody></table><details><summary>Текущий маршрут целиком</summary><pre>${esc(JSON.stringify(t.route,null,2))}</pre></details><p>Бюджеты — ориентир исполнителю, не технический лимит модели. При превышении остановиться и пересмотреть план.</p><button id="board-packet">Пакет исполнителю</button><button id="board-edit">Проработать задачу</button>`);
    BOARD_EXTRAS.checklist({task:t,dialog,api,refresh,modal});
    dialog.querySelector('#board-edit').onclick=()=>{dialog.close();BOARD_EXTRAS.form({data,project,task:t,modal,dialog,api,refresh});};
    dialog.querySelector('#board-packet').onclick=async()=>{try{const p=await api({op:'packet',ref:t.ref});dialog.close();modal('Пакет исполнителю',`<pre>${esc(JSON.stringify(p,null,2))}</pre><p>CLI: py tools/pm.py work packet ${esc(t.ref)} --agent ${esc(p.route.provider)}</p><p>Взять задачу: tt_work op=take ref=${esc(t.ref)} model=${esc(p.route.model)} worker=&lt;чат&gt;</p>`);}catch(e){message.textContent=e.message;dialog.close();}};
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
