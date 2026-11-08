/* Доски используют тот же контракт, что CLI и MCP. */
(() => {
  let data=D.workflow, query='', project='', status='', priority='', ai='', onlyFocus=false, activityOffline=false;
  try { const saved=JSON.parse(localStorage.getItem('tt-board-filters')||'{}');
    query=typeof saved.query==='string'?saved.query:''; project=typeof saved.project==='string'?saved.project:'';
    status=typeof saved.status==='string'?saved.status:''; priority=typeof saved.priority==='string'?saved.priority:''; ai=typeof saved.ai==='string'?saved.ai:''; onlyFocus=saved.onlyFocus===true;
  } catch {}
  const root=document.getElementById('boards-root');
  const labels={queued:'К выполнению',in_progress:'В работе',review:'Проверка',blocked:'Заблокировано',done:'Готово',cancelled:'Отменено'};
  const message=document.createElement('div'); message.className='board-message'; message.setAttribute('role','status');
  const dialog=document.createElement('dialog'); dialog.className='board-dialog'; document.body.append(dialog);
  function modal(title,body){dialog.innerHTML=`<h2>${esc(title)}</h2>${body}<p><button id="board-close">Закрыть</button></p>`;dialog.querySelector('#board-close').onclick=()=>dialog.close();dialog.showModal();hints(dialog);}
  async function api(req){
    if(location.protocol==='file:')throw Error('Для изменения задач запустите py tools/pm.py serve. Пакеты можно просматривать и без сервера.');
    const r=await fetch('/api/work',{method:'POST',headers:ttOwnerHeaders(),body:JSON.stringify(req)});
    const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||'Операция не выполнена');return j.data;
  }
  async function refresh(){if(location.protocol!=='file:'){const r=await fetch('/api/work');const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||'Нет связи');data=j.data;activityOffline=false;}render();}
  async function change(req){try{await api(req);await refresh();message.textContent='Сохранено';}catch(e){message.textContent=e.message;}}
  const providers=()=>data.providers||{codex:{name:'GPT / Codex'}};
  const aiName=k=>providers()[k]?.name||k;
  const time=v=>{const d=new Date(v);return isNaN(d)?'без срока':d.toLocaleString('ru',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});};
  // Аренда за 5 минут до конца подсвечивается, чтобы владелец успел продлить или перехватить.
  // Аренда без срока (старые запуски) не истекает никогда.
  const LEASE_WARN_MS=5*60*1000;
  function leaseSoon(c,now=Date.now()){
    if(!c||c.stale||!c.lease_until)return false;
    const until=new Date(c.lease_until).getTime();
    return !isNaN(until)&&until-now<=LEASE_WARN_MS;
  }
  // Метка занятости: по ней и человек, и другой ИИ видят, что задача уже в работе.
  function aiBadge(t){
    const def=data.settings?.default_executor;
    const who=t.executor?`<span class="board-ai board-ai-${esc(t.executor)}">ИИ: ${esc(aiName(t.executor))}</span>`:`<span class="board-ai">ИИ: любой${def?' · по умолчанию '+esc(aiName(def)):''}</span>`;
    const c=t.claim;
    const r=c&&data.runs.find(x=>x.id===c.run);
    const beat=r?.heartbeat_at&&r.heartbeat_at!==r.claimed_at?` · сигнал ${esc(time(r.heartbeat_at))}`:'';
    const soon=leaseSoon(c);
    const lock=c?`<span class="board-claim${c.stale?' board-claim-stale':''}${soon?' board-claim-soon':''}" title="${esc(c.run)} · ${esc(c.model||'')}">${c.stale?'⌛ Аренда истекла — можно перехватить':(soon?'⚠ Аренда истекает — продлите или перехватите; ':'')+'🔒 Принята '+esc(aiName(c.agent))+(r?.claimed_at?' '+esc(time(r.claimed_at)):'')+beat} · ${esc(c.worker||'?')} · до ${esc(time(c.lease_until))}</span>`:'';
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
  // Колонки — этапы конвейера; их задаёт спецификация сервера (pipeline.py), «Отменено» — в конце.
  const STAGES=()=>(data.pipeline?.stages)||[];
  const COLUMNS=()=>[...STAGES().map(s=>s.id),'cancelled'];
  const stageTitle=id=>id==='cancelled'?'Отменено':id==='done'?'Готово':(STAGES().find(s=>s.id===id)?.title||id);
  const roleTitle=r=>data.pipeline?.roles?.[r]?.title||r;
  // Старые задачи вне конвейера: перенос в колонку этапа меняет их статус, как раньше.
  const LEGACY_STATUS={idea:'queued',analysis:'queued',development:'in_progress',review:'review',testing:'review',deploy_test:'review',deploy_main:'done',cancelled:'cancelled'};
  // Задача конвейера стоит на своём этапе; старая — по статусу: готовые в «Деплой в main», непроработанные в «Идея».
  function column(t){
    if(t.status==='cancelled')return 'cancelled';
    if(t.pipeline)return t.pipeline.stage==='done'?'deploy_main':t.pipeline.stage;
    if(t.status==='done')return 'deploy_main';
    if(t.status==='review')return 'review';
    if(t.status==='queued'&&t.needs_plan)return 'idea';
    return 'development';
  }
  // Шаблоны: каждый возвращает строку разметки и не трогает DOM.
  const headTemplate=()=>`<h1>Доски задач</h1><p>Сначала план и критерии, затем выбор модели и запуск. Фокус влияет на порядок рекомендаций.</p>
      <details><summary>Фокус проектов (${data.focus.length})</summary><div class="board-focus">${Object.entries(data.projects).map(([k,p],i)=>`<label><input type="checkbox" data-focus="${i}" ${data.focus.includes(k)?'checked':''}> ${esc(p.name)}</label>`).join('')}</div></details>`;
  const toolsTemplate=()=>`<div class="board-tools"><input id="board-search" aria-label="Поиск задач" placeholder="Найти задачу" value="${esc(query)}"><select id="board-project" aria-label="Проект доски"><option value="">Все проекты</option>${Object.entries(data.projects).map(([k,p])=>`<option value="${esc(k)}" ${project===k?'selected':''}>${esc(p.name)}</option>`).join('')}</select>
      <select id="board-status" aria-label="Этап"><option value="">Все этапы</option>${COLUMNS().map(k=>`<option value="${k}" ${status===k?'selected':''}>${esc(stageTitle(k))}</option>`).join('')}</select>
      <select id="board-priority" aria-label="Приоритет"><option value="">Все приоритеты</option>${['🔴','🟡','🟢'].map(k=>`<option ${priority===k?'selected':''}>${k}</option>`).join('')}</select>
      <select id="board-ai" aria-label="ИИ-исполнитель"><option value="">Все ИИ</option>${Object.entries(providers()).map(([k,p])=>`<option value="${esc(k)}" ${ai===k?'selected':''}>${esc(p.name)}</option>`).join('')}<option value="none" ${ai==='none'?'selected':''}>Не назначены</option><option value="busy" ${ai==='busy'?'selected':''}>Заняты сейчас</option></select>
      <label><input type="checkbox" id="board-only-focus" ${onlyFocus?'checked':''}> Только фокус</label><button id="board-next">Что взять следующим</button><button id="board-plan">Добавить план</button><button id="board-idea">💡 Идея</button><button id="board-refresh">Обновить доски</button><button id="board-split">⇄ Распределить между ИИ</button><label>ИИ по умолчанию <select id="board-default-ai" aria-label="ИИ по умолчанию">${Object.entries(providers()).map(([k,p])=>`<option value="${esc(k)}" ${data.settings?.default_executor===k?'selected':''}>${esc(p.name)}</option>`).join('')}</select></label><details class="board-settings"><summary>⚙ Исполнение и ревью</summary><label>Ревью делает <select id="board-reviewer">${[['same','тот же ИИ, что выполнял'],['other','другой ИИ'],...Object.entries(providers()).map(([k,p])=>[k,'всегда '+p.name])].map(([v,l])=>`<option value="${esc(v)}" ${data.settings?.reviewer===v?'selected':''}>${esc(l)}</option>`).join('')}</select></label><label><input type="checkbox" id="board-auto-review" ${data.settings?.auto_launch_review!==false?'checked':''}> Открывать ревью автоматически после автопроверки</label><label><input type="checkbox" id="board-auto-merge" ${data.settings?.auto_merge!==false?'checked':''}> Сливать ветку в базовую после одобренного ревью</label><label>Согласие владельца <select id="board-autonomy"><option value="step"${data.settings?.autonomy!=='full'?' selected':''}>пошаговое — окна, ИИ спрашивает</option><option value="full"${data.settings?.autonomy==='full'?' selected':''}>полная автономия — одно согласие</option></select></label><label><input type="checkbox" id="board-auto-pipeline" ${data.settings?.auto_pipeline!==false?'checked':''}> Конвейер: открывать сессии ролей автоматически</label><label><input type="checkbox" id="board-failover" ${data.settings?.failover!==false?'checked':''}> Кончились токены у ИИ — переводить задачу на другой ИИ</label><label>Одновременно сессий, не больше <select id="board-max-sessions">${[1,2,3,4,5,6,8,10,12].map(v=>`<option value="${v}"${(data.settings?.max_sessions||4)===v?' selected':''}>${v}</option>`).join('')}</select></label>${Object.entries(providers()).map(([k,p])=>`<label>Из них ${esc(p.name)}, не больше <select data-ai-sessions="${esc(k)}">${[0,1,2,3,4,6,8,12].map(v=>`<option value="${v}"${((data.settings?.sessions_per_ai||{})[k]??2)===v?' selected':''}>${v}</option>`).join('')}</select></label>`).join('')}<label>Задач в анализе на проект, не больше <select id="board-wip">${[2,3,4,6,8,10,15,20].map(v=>`<option value="${v}"${(data.settings?.wip_analysis||6)===v?' selected':''}>${v}</option>`).join('')}</select></label>${Object.keys(data.pipeline?.roles||{}).map(r=>`<label>${esc(roleTitle(r))} <select data-role-ai="${esc(r)}">${[['','по умолчанию'],...Object.entries(providers()).map(([k,p])=>[k,p.name])].map(([v,l])=>`<option value="${esc(v)}" ${(data.settings?.roles||{})[r]===v||(!v&&!(data.settings?.roles||{})[r])?'selected':''}>${esc(l)}</option>`).join('')}</select></label>`).join('')}</details></div>${parallel()}${teamleadJournal()}`;
  const policyTemplate=()=>`<details><summary>Модели и правила экономии</summary><p>${esc(data.policy.availability)}. ${esc(data.policy.evidence)}.</p><ul>${data.policy.rules.map(v=>`<li>${esc(v)}</li>`).join('')}</ul><table><thead><tr><th>ИИ</th><th>Модель</th><th>Сильные стороны</th><th>Ограничения</th></tr></thead><tbody>${data.models.map(m=>`<tr><td>${esc(aiName(m.provider))}</td><td>${esc(m.id)}${Object.values(data.lineup?.[m.provider]||{}).includes(m.id)?'':' · только явно'}</td><td>${esc(m.strength)}</td><td>${esc(m.weakness)}</td></tr>`).join('')}</tbody></table></details><div id="board-list"></div>`;
  function cardTemplate(t){
    const i=data.tasks.indexOf(t),run=data.runs.find(r=>r.ref===t.ref&&['queued','running'].includes(r.status));
    return `<article class="board-task" draggable="true" data-drag="${i}"><small>${esc(t.id)} · ${esc(t.size||'?')} · ${esc(t.priority||'')}</small><h4>${esc(t.title)}</h4>${t.pipeline?pipelineBadge(t,i):aiBadge(t)+reviewBadge(t)+manualBadge(t)}${t.pipeline?'':`<small>${esc(t.route.model)} · ${esc(t.route.effort)}</small><small>${esc(t.route.reason)}${t.route.provisional?' — требуется уточнение':''}</small><small>Бюджет: ${t.route.total_token_budget.toLocaleString('ru')} токенов · ${t.route.max_attempts} попытки</small>${t.review_required?'<small>Лимит попыток достигнут: пересмотрите модель или декомпозицию</small>':""}${t.needs_plan&&t.status!=='done'?'<small>Нужна декомпозиция / критерии приёмки</small>':''}`}${t.blocked_by.length?`<small>Зависит от: ${esc(t.blocked_by.join(', '))}</small>`:''}${!t.pipeline&&run&&run.status==='queued'?launchState(t,run,i):''}${t.status_note&&['blocked','cancelled'].includes(t.status)?`<div class="board-note">${t.status==='blocked'?'⛔':'✕'} ${esc(t.status_note)}</div>`:''}${t.complexity?.needs_assessment&&!['done','cancelled'].includes(t.status)?`<div class="board-assess" title="${esc((t.complexity.conflicts||[]).join('; ')||'аналитика и декомпозиция оцениваются ИИ по умолчанию')}">🔍 Сначала оценка сложности · уверенность ${t.complexity.confidence}</div>`:''}<button data-detail="${i}">Паспорт</button><button data-priority="${i}">Приоритет</button>${!t.pipeline&&!run&&!['done','cancelled','blocked','review','in_progress'].includes(t.status)&&!t.needs_plan&&!t.review_required&&!t.blocked_by.length?`<span class="board-launch">${launchSelect(t,i)}<button data-launch="${i}">${launchLabel(t,null)}</button></span>`:''}</article>`;
  }
  const columnTemplate=(c,ts)=>{const items=ts.filter(t=>column(t)===c);const st=STAGES().find(s=>s.id===c);
    return `<div class="board-column" data-column="${c}"><h3>${esc(stageTitle(c))} · ${items.length}</h3>${st?`<div class="board-stage-roles">${st.roles.map(r=>`<span class="board-role${st.optional?.[r]?' board-role-opt':''}" title="${esc(data.pipeline.roles[r].mission)}">${esc(roleTitle(r))}</span>`).join('')}${st.gate?'<span class="board-role board-role-gate" title="Ворота ИИ-тимлида">🚦 тимлид</span>':''}${st.owner_approval?'<span class="board-role board-role-gate" title="Нужно «да» владельца">👤 владелец</span>':''}</div><button class="board-dod" data-stage-info="${c}">DoR / DoD</button>`:''}${items.map(cardTemplate).join('')}</div>`;};
  const projectTemplate=(k,ts)=>`<section class="board-project"><h2>${data.focus.includes(k)?'★ ':''}${esc(data.projects[k].name)} · ${ts.length} ${(data.active_projects||[]).includes(k)?`<span class="chip ok">в автономной работе</span> <button class="board-start" data-stop-project="${esc(k)}">⏸ Остановить проект</button>`:`<button class="board-start" data-start="${esc(k)}">▶ Запустить проект</button>`} <button class="board-start" data-to-idea="${esc(k)}">💡 Всё в «Идея»</button></h2><div class="board-columns">${COLUMNS().map(c=>columnTemplate(c,ts)).join('')}</div></section>`;
  const filtered=()=>data.tasks.filter(t=>(!project||t.project===project)&&(!onlyFocus||data.focus.includes(t.project))&&(!status||column(t)===status)&&(!priority||t.priority===priority)&&aiMatch(t)&&(!query||(t.title+' '+t.id).toLowerCase().includes(query.toLowerCase())));
  const saveFilters=()=>{try {localStorage.setItem('tt-board-filters',JSON.stringify({query,project,status,priority,ai,onlyFocus}));}catch{}};
  // Полная отрисовка: панель инструментов, список, баннеры.
  function render(){
    saveFilters();
    root.innerHTML=headTemplate()+toolsTemplate()+policyTemplate();
    if(data.sprint){
      const s=data.sprint, box=document.createElement('details');
      // Спринт у каждого проекта свой: строка на проект, чужие задачи приём не держат.
      const by=s.projects||{}, keys=Object.keys(by).filter(k=>!project||k===project);
      const rows=keys.map(k=>{const x=by[k], n=x.current?.refs?.length||0;
        return `<p><b>${esc(k)}</b>: ${x.legacy?.length?'завершаем ранее начатые':n+' / '+s.limit+' задач'} · ждут main: ${esc((x.pending||[]).join(', ')||'—')}${x.hold?' · новые не берём':''}</p>`;}).join('');
      const waiting=keys.reduce((a,k)=>a+((by[k].pending||[]).length),0);
      box.innerHTML=`<summary>Спринты проектов: ${keys.length} · ждут main: ${waiting}</summary><p>${esc(s.rule)}</p>${rows||'<p>Нет незавершённого состава</p>'}`;
      root.insertBefore(box,root.querySelector('#board-list'));
    }
    if(data.flow){
      // FR-014 п.10: счётчик потока и почему новые задачи сейчас не берём.
      const f=data.flow, c=f.counts||{}, box=document.createElement('details');
      const why=f.mode==='rolling'?(f.reasons?.rolling||[]):(f.reasons?.batch?[f.reasons.batch]:Object.entries(f.batch_by_project||{}).filter(([k])=>!project||k===project).map(([,v])=>v));
      const shadow=f.mode==='batch'?`Режим rolling (FR-014) считается рядом: новые задачи ${f.allowed?.rolling?'брал бы':'не брал бы'}${(f.reasons?.rolling||[]).length?' — '+f.reasons.rolling.join('; '):''}. Отличий в журнале: ${(f.divergences||[]).length}.`:'';
      box.innerHTML=`<summary>Поток (${esc(f.mode)}): в работе ${c.in_work||0} · ждёт ревью ${c.review_waiting||0} · ждёт тест ${c.test_waiting||0} · ждёт выпуска ${c.release_waiting||0} · заблокировано ${c.blocked||0} · ${f.new_task_admission_allowed?'новые задачи берём':'новые задачи временно не берём'}</summary>`+
        `${why.map(x=>`<p>${esc(x)}</p>`).join('')}${(f.finish_candidates||[]).length?`<p>Сначала довести: ${esc(f.finish_candidates.map(x=>x.ref+' ('+x.stage+')').join(', '))}</p>`:''}`+
        `${(f.blocked||[]).map(b=>`<p>⛓ <b>${esc(b.ref)}</b> — ${esc(b.reason)} · <b>${esc(b.responsible)}</b>${b.recheck_at?' · перепроверка '+esc(b.recheck_at.slice(0,16)):''}${b.age_days!=null?' · '+esc(b.age_days)+' дн.':''}${b.overdue?' · срок прошёл':''}</p>`).join('')}`+
        `${shadow?`<p><small>${esc(shadow)}</small></p>`:''}<p><small>Режим и лимиты (wip_global ${f.policy?.wip_global}, wip_per_project ${f.policy?.wip_per_project}) меняет владелец в настройках.</small></p>`;
      root.insertBefore(box,root.querySelector('#board-list'));
    }
    root.insertBefore(message,root.querySelector('#board-list'));
    renderList();
    root.querySelector('#board-search').oninput=e=>{query=e.target.value;saveFilters();renderList();};
    root.querySelector('#board-project').onchange=e=>{project=e.target.value;render();};
    root.querySelector('#board-status').onchange=e=>{status=e.target.value;render();};
    root.querySelector('#board-priority').onchange=e=>{priority=e.target.value;render();};
    root.querySelector('#board-ai').onchange=e=>{ai=e.target.value;render();};
    root.querySelector('#board-default-ai').onchange=e=>change({op:'settings',changes:{default_executor:e.target.value}});
    root.querySelector('#board-split').onclick=splitDialog;
    root.querySelector('#board-reviewer').onchange=e=>change({op:'settings',changes:{reviewer:e.target.value}});
    root.querySelector('#board-auto-review').onchange=e=>change({op:'settings',changes:{auto_launch_review:e.target.checked}});
    root.querySelector('#board-auto-merge').onchange=e=>change({op:'settings',changes:{auto_merge:e.target.checked}});
    root.querySelector('#board-only-focus').onchange=e=>{onlyFocus=e.target.checked;render();};
    root.querySelectorAll('[data-focus]').forEach(el=>el.onchange=()=>{const k=Object.keys(data.projects)[Number(el.dataset.focus)];change({op:'focus',projects:el.checked?[...data.focus,k]:data.focus.filter(v=>v!==k)});});
    root.querySelector('#board-refresh').onclick=()=>refresh().catch(e=>message.textContent=e.message);
    root.querySelector('#board-next').onclick=()=>{const ts=data.tasks.filter(t=>(!project||project===t.project)&&!['done','cancelled','in_progress','review','blocked'].includes(t.status)&&!t.needs_plan&&!t.review_required&&!t.blocked_by.length&&!data.runs.some(r=>r.ref===t.ref&&['queued','running'].includes(r.status))).slice(0,5);modal('Следующие задачи',`<p>Порядок: фокус → приоритет → размер. Зависимости и незавершённая проработка исключают запуск.</p>${ts.map(t=>`<p><b>${esc(t.ref)}</b> — ${esc(t.title)}<br>${Object.entries(t.routes||{[t.route.provider]:t.route}).map(([k,r])=>`${esc(aiName(k))}: ${esc(r.model)} · ${esc(r.effort)}`).join(' · ')}<br><small>${esc(t.route.reason)}</small></p>`).join('')||'<p>Готовых к запуску задач нет.</p>'}`);};
    root.querySelector('#board-plan').onclick=plan;
    root.querySelector('#board-idea').onclick=ideaDialog;
    root.querySelector('#board-auto-pipeline').onchange=e=>change({op:'settings',changes:{auto_pipeline:e.target.checked}});
    root.querySelector('#board-max-sessions').onchange=e=>change({op:'settings',changes:{max_sessions:Number(e.target.value)}});
    root.querySelectorAll('[data-ai-sessions]').forEach(el=>el.onchange=()=>change({op:'settings',changes:{sessions_per_ai:{[el.dataset.aiSessions]:Number(el.value)}}}));
    root.querySelector('#board-wip').onchange=e=>change({op:'settings',changes:{wip_analysis:Number(e.target.value)}});
    root.querySelector('#board-failover').onchange=e=>change({op:'settings',changes:{failover:e.target.checked}});
    const lim=Object.entries(data.provider_limits||{});
    if(lim.length)root.insertAdjacentHTML('afterbegin',`<div class="board-banner" role="status">⏳ Лимит токенов: ${lim.map(([a,v])=>`${esc(aiName(a))} — до ${esc(time(v.until))}`).join('; ')}. ${data.settings?.failover!==false?'Задачи этого ИИ переходят к свободному ИИ с моделью и усилиями под роль.':'Передача выключена: задачи ждут сброса лимита.'}</div>`);
    root.querySelector('#board-autonomy').onchange=e=>{const v=e.target.value;e.target.value=data.settings?.autonomy||'step';autonomyDialog(v);};
    root.querySelectorAll('[data-stop-project]').forEach(el=>el.onclick=async()=>{try{const r=await api({op:'stop_project',project:el.dataset.stopProject});await refresh();message.textContent=`Проект остановлен: ${r.held} ждущих ролей не будут открываться; открытые сессии доработают.`;}catch(e){message.textContent=e.message;}});
    root.querySelectorAll('[data-run-log]').forEach(el=>el.onclick=()=>sessionLog(el.dataset.runLog));
    root.querySelectorAll('[data-ci-log]').forEach(el=>el.onclick=()=>ciLog(el.dataset.ciLog));
    root.querySelectorAll('[data-role-ai]').forEach(el=>el.onchange=()=>change({op:'settings',changes:{roles:{[el.dataset.roleAi]:el.value||data.settings?.default_executor}}}));
    root.querySelector('#board-plan').textContent='＋ Новая задача';
    BOARD_EXTRAS.decorateToolbar(root,data,project);
    if(data.server?.stale)root.insertAdjacentHTML('afterbegin','<div class="board-banner" role="alert">⚠ Сервер досок запущен со старым кодом: часть новых функций не работает. Перезапустите его: Ctrl+C в окне сервера, затем <code>py tools/pm.py serve</code>.</div>');
    hints(root);
  }
  // Только список задач: ввод в поиск обновляет его, не пересоздавая панель инструментов.
  function renderList(){
    const visible=filtered();
    const keys=Object.keys(data.projects).filter(k=>(!project||project===k)&&(!onlyFocus||data.focus.includes(k))).sort((a,b)=>Number(data.focus.includes(b))-Number(data.focus.includes(a)));
    const list=root.querySelector('#board-list');
    list.innerHTML=keys.map(k=>{const ts=visible.filter(t=>t.project===k);return !ts.length&&(query||status||priority||ai)?'':projectTemplate(k,ts);}).join('')||'<p>Нет досок по выбранным фильтрам.</p>';
    root.querySelectorAll('[data-stage-info]').forEach(el=>el.onclick=()=>stageInfo(el.dataset.stageInfo));
    root.querySelectorAll('[data-start]').forEach(el=>el.onclick=()=>startDialog(el.dataset.start));
    root.querySelectorAll('[data-to-idea]').forEach(el=>el.onclick=()=>toIdea(null,el.dataset.toIdea));
    root.querySelectorAll('[data-approve]').forEach(el=>el.onclick=()=>{const t=data.tasks[Number(el.dataset.approve)];change({op:'approve_release',ref:t.ref});});
    root.querySelectorAll('[data-role-run]').forEach(el=>el.onclick=async()=>{el.disabled=true;const t=data.tasks[Number(el.dataset.roleTask)];try{await openSession(t,null,el.dataset.roleRun);}catch(e){message.textContent=e.message;el.disabled=false;}});
    root.querySelectorAll('[data-detail]').forEach(el=>el.onclick=()=>detail(data.tasks[Number(el.dataset.detail)]));
    root.querySelectorAll('[data-priority]').forEach(el=>el.onclick=()=>{const t=data.tasks[Number(el.dataset.priority)];change({op:'update',ref:t.ref,changes:{priority:['🔴','🟡','🟢'][(['🔴','🟡','🟢'].indexOf(t.priority)+1)%3]}});});
    root.querySelectorAll('[data-launch-ai]').forEach(sel=>sel.onchange=()=>{const b=root.querySelector(`[data-launch="${sel.dataset.launchAi}"]`);if(b)b.textContent=launchLabel(data.tasks[Number(sel.dataset.launchAi)],sel.value);});
    root.querySelectorAll('[data-launch]').forEach(el=>{const sel=root.querySelector(`[data-launch-ai="${el.dataset.launch}"]`);if(sel)el.textContent=launchLabel(data.tasks[Number(el.dataset.launch)],sel.value);});
    root.querySelectorAll('[data-launch]').forEach(el=>el.onclick=async()=>{el.disabled=true;const t=data.tasks[Number(el.dataset.launch)],agent=root.querySelector(`[data-launch-ai="${el.dataset.launch}"]`)?.value;
      try{if(canLaunch(agent)){await openSession(t,agent);}else{const r=await api({op:'enqueue',ref:t.ref,agent});await refresh();showPrompt(t,r);}}catch(e){message.textContent=e.message;el.disabled=false;}});
    root.querySelectorAll('[data-relaunch]').forEach(el=>el.onclick=async()=>{el.disabled=true;const t=data.tasks[Number(el.dataset.relaunch)],run=data.runs.find(r=>r.ref===t.ref&&r.status==='queued');
      const agent=root.querySelector(`[data-relaunch-ai="${el.dataset.relaunch}"]`)?.value||run?.agent||data.settings?.default_executor;
      try{await openSession(t,agent);}catch(e){message.textContent=e.message;el.disabled=false;}});
    root.querySelectorAll('[data-prompt]').forEach(el=>el.onclick=async()=>{const t=data.tasks[Number(el.dataset.prompt)];try{showPrompt(t,await api({op:'prompt',ref:t.ref}));}catch(e){message.textContent=e.message;}});
    root.querySelectorAll('[data-unlaunch]').forEach(el=>el.onclick=()=>{const t=data.tasks[Number(el.dataset.unlaunch)],run=data.runs.find(r=>r.ref===t.ref&&r.status==='queued');if(run)change({op:'cancel',ref:t.ref,run:run.id,evidence:'Владелец отменил запуск с доски'});});
    dragAndDrop();
    BOARD_EXTRAS.decorateList(root,data,visible);
    hints(list);
  }
  function detail(t){
    if(t.pipeline)return pipelineDetail(t);
    modal(t.ref,`<p>${esc(t.title)}</p>${nextStep(t)}<p>${esc(t.why||'')}</p>${reviewInfo(t)}${typeInfo(t)}<p>Корень: ${esc(t.root||'не указан')} · зависимости: ${esc((t.depends_on||[]).join(', ')||'нет')}</p><h3>Критерии приёмки</h3><ul>${(t.acceptance||[]).map(a=>`<li>${esc(a)}</li>`).join('')}</ul><h3>План модели</h3><table><thead><tr><th>ИИ</th><th>Модель</th><th>Усилия</th><th>Бюджет</th><th>Почему</th></tr></thead><tbody>${Object.entries(t.routes||{}).map(([k,r])=>`<tr><td>${esc(aiName(k))}${k===t.route.provider?' ✓':''}</td><td>${esc(r.model)}</td><td>${esc(r.effort)}</td><td>${(r.total_token_budget||0).toLocaleString('ru')}</td><td>${esc(r.reason)}${r.source==='manual'?' (ручной выбор)':''}</td></tr>`).join('')}</tbody></table><details><summary>Текущий маршрут целиком</summary><pre>${esc(JSON.stringify(t.route,null,2))}</pre></details><p>Бюджеты — ориентир исполнителю, не технический лимит модели. При превышении остановиться и пересмотреть план.</p>${complexityInfo(t)}<h3>Статус: ${esc(labels[t.status]||t.status)} · колонка «${esc(stageTitle(column(t)))}»</h3>${t.status_note?`<p>${esc(t.status_note)}</p>`:''}<div class="board-status-actions">${statusButtons(t)}</div>${(t.status_history||[]).length?`<details><summary>История статуса</summary><ul>${t.status_history.slice().reverse().map(h=>`<li>${esc(h.at.slice(0,16).replace('T',' '))}: ${esc(labels[h.from_status]||h.from_status)} → ${esc(labels[h.to]||h.to)}${h.reason?' — '+esc(h.reason):''}</li>`).join('')}</ul></details>`:''}<button id="board-packet">Пакет исполнителю</button><button id="board-edit">Проработать задачу</button>`);
    dialog.querySelectorAll('[data-move]').forEach(el=>el.onclick=()=>{dialog.close();moveTo(t,el.dataset.move);});
    const ac=dialog.querySelector('#board-autocheck');
    if(ac)ac.onclick=async()=>{ac.disabled=true;try{await api({op:'autocheck',ref:t.ref});dialog.close();await refresh();message.textContent='Автопроверка запущена: результат появится в паспорте задачи.';}catch(e){message.textContent=e.message;ac.disabled=false;}};
    BOARD_EXTRAS.checklist({task:t,dialog,api,refresh,modal});hints(dialog);
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
    let head,cls='board-waiting',verb='';
    const ph=run.phase==='review'?' (ревью)':phase;
    if(run.diagnosis){cls+=' board-stuck';head=`⛔ Сессия ${who} открылась, но завершилась с ошибкой: ${esc(run.diagnosis.message)}`;verb='Перезапустить в';}
    else if(!run.launched_at){head=`📥 Поставлена в очередь для ${who}${ph} — сессия не открыта`;verb='Открыть сессию в';}
    else if(ago(run.launched_at)<5){head=`🖥 Сессия ${who}${ph} открыта ${esc(time(run.launched_at))} — ждём, пока ИИ примет`;}
    else{cls+=' board-stuck';head=`⚠ Сессия ${who} открыта ${ago(run.launched_at)} мин назад, ИИ не принял задачу. Проверьте окно сессии или перезапустите`;verb='Перезапустить в';}
    // Какой ИИ откроется — решает владелец здесь же; выбор другого ИИ перезакрепит запуск.
    const keys=t.executor?[t.executor]:Object.keys(providers()).filter(k=>canLaunch(k));
    const extra=verb&&keys.length?`<button data-relaunch="${i}">${verb}</button><select data-relaunch-ai="${i}" aria-label="ИИ для сессии ${esc(t.id)}"${keys.length<2?' disabled':''}>${keys.map(k=>{const r=(t.routes||{})[k];return `<option value="${esc(k)}" ${k===run.agent?'selected':''}>${esc(providers()[k]?.short||aiName(k))}${r?' · '+esc(r.model.replace(/^(gpt|claude)-/,''))+' · '+esc(r.effort):''}</option>`;}).join('')}</select>`:'';
    const wt=run.launch?.worktree?`<small>Рабочая копия: ${esc(run.launch.worktree.branch)}</small>`:'';
    return `<div class="${cls}">${head}${route} · ${esc(run.id)}${wt}<span>${extra}<button data-prompt="${i}">Задание для ИИ</button><button data-unlaunch="${i}">Отменить запуск</button></span></div>`;
  }
  async function openSession(t,agent,run){
    const r=await fetch('/api/launch',{method:'POST',headers:ttOwnerHeaders(),body:JSON.stringify(Object.assign({ref:t.ref},agent?{agent}:{},run?{run}:{}))});
    const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||'Сессию открыть не удалось');
    await refresh();const d=j.data;
    modal('Сессия открыта: '+t.id,`<p>Открыто окно <b>${esc(aiName(d.agent))}</b>: ${esc(d.route.model)} · ${esc(d.route.effort)}, запуск ${esc(d.run)}.</p><p>Папка: <code>${esc(d.cwd)}</code>${d.worktree?`<br>Отдельная рабочая копия, ветка <code>${esc(d.worktree.branch)}</code>`:''}</p><p>Первым шагом ИИ примет задачу. Тогда на карточке появится 🔒 с именем <code>${esc(d.worker)}</code>. Если окно спросит о доверии к папке или о разрешениях — ответьте в нём.</p><details><summary>Задание, переданное ИИ</summary><pre>${esc(d.prompt)}</pre></details>`);
  }
  const runOf=(t,phase)=>data.runs.filter(r=>r.ref===t.ref&&(!phase||r.phase===phase)).slice(-1)[0];
  const verdicts={approve:'Принято',changes:'Нужна доработка',spec_change:'Принято с изменением ТЗ'};
  function reviewBadge(t){
    if(['done','cancelled'].includes(t.status))return '';
    const ac=t.auto_check,rv=runOf(t,'review');
    if(ac?.state==='pending')return '<div class="board-review">🔎 Идёт автопроверка ветки</div>';
    if(t.status==='review'&&rv?.status==='running')return `<div class="board-review">🧐 Идёт ревью: ${esc(aiName(rv.agent))}</div>`;
    if(t.status==='review'&&rv?.status==='queued')return `<div class="board-review">✅ Автопроверка пройдена · ${rv.launch_error?'⚠ ревью не открылось':'ждёт ревью ИИ'}</div>`;
    if(t.status==='review'&&t.review&&t.review.verdict!=='changes')return '<div class="board-review board-stuck">⚠ Ревью принято, слияние не выполнено</div>';
    if(t.status==='review'&&!ac)return '<div class="board-review">⏸ Ждёт автопроверки — откройте паспорт</div>';
    if(t.review?.verdict==='changes'&&t.status==='queued')return '<div class="board-review board-stuck">↩ Доработка по ревью</div>';
    if(ac&&ac.ok===false&&t.status==='queued')return '<div class="board-review board-stuck">✗ Автопроверка не пройдена</div>';
    return '';
  }
  // Что происходит с задачей и что делать владельцу — простыми словами, для каждого состояния.
  function nextStep(t){
    const run=data.runs.find(r=>r.ref===t.ref&&['queued','running'].includes(r.status)),ac=t.auto_check,rv=runOf(t,'review'),branch=ac?.branch||data.runs.filter(r=>r.ref===t.ref&&r.launch?.worktree).slice(-1)[0]?.launch.worktree.branch;
    let now,todo,btn='';
    if(t.status==='cancelled'){now='Задача отменена'+(t.status_note?': '+t.status_note:'')+'.';todo='Ничего. Если она снова нужна — «↺ Вернуть в «К выполнению»».';}
    else if(t.status==='done'){now='Готово. '+(t.review?`Ревью ${verdicts[t.review.verdict]||''} (${t.review.by?.model||''}): ${t.review.conclusion}`:(t.evidence||''));todo='Ничего.'+(t.review?.verdict==='spec_change'?' Создана задача на изменение ТЗ — она в «К выполнению».':'');}
    else if(t.status==='blocked'){now='Заблокирована: '+(t.status_note||'причина не указана')+'.';todo='Когда причина снята — «↺ Вернуть в «К выполнению»».';}
    else if(t.status==='review'){
      if(ac?.state==='pending'){now=`Идёт автопроверка ветки ${branch||''}: коммиты, область правки, проверки проекта, слияние без конфликтов.`;todo='Ничего — через минуту-две результат появится здесь.';}
      else if(rv?.status==='running'){now=`Идёт ревью: ${aiName(rv.agent)} (${rv.model}) проверяет критерии приёмки и соответствие ТЗ.`;todo='Ничего. После заключения задача сама перейдёт в «Готово» или вернётся на доработку.';}
      else if(rv?.status==='queued'){now='Автопроверка пройдена, ждёт ревью ИИ ('+aiName(rv.agent)+').';todo=rv.launch_error?'Ревью не открылось: '+rv.launch_error+'. Откройте его кнопкой «Запустить» на карточке или исправьте причину.':rv.launched_at?'Сессия ревью открыта — ИИ сейчас примет задачу.':(data.settings?.auto_launch_review?'Сервер досок откроет ревью сам в течение минуты — если он запущен.':'Нажмите «Запустить» на карточке, чтобы открыть ревью.');}
      else if(t.review&&t.review.verdict!=='changes'){now=`Ревью: ${verdicts[t.review.verdict]}. ${t.status_note||''}`;todo='Слейте ветку вручную и нажмите «Завершить с проверкой».';}
      else{now='Работа выполнена'+(branch?' в ветке '+branch:'')+', но ещё не проверена'+(ac&&ac.ok===false?': автопроверка не пройдена.':'.');todo='Нажмите «🔎 Запустить автопроверку»: она проверит ветку и передаст задачу на ревью ИИ.';btn='<button id="board-autocheck">🔎 Запустить автопроверку</button>';}
    }
    else if(t.claim){now=`Выполняет ${aiName(t.claim.agent)} (${t.claim.worker}), аренда до ${time(t.claim.lease_until)}.`;todo=t.claim.stale?'Аренда истекла: ИИ, вероятно, остановился. Перезапустите или «↺ Вернуть».':'Ничего — ИИ сам сообщит результат.';}
    else if(run){now=!run.launched_at?`Поставлена в очередь для ${aiName(run.agent||'')}, сессия не открыта.`:`Сессия ${aiName(run.agent||'')} открыта, ИИ ещё не принял задачу.`;todo=!run.launched_at?'Нажмите «Открыть сессию» на карточке или вставьте «Задание для ИИ» в открытый чат.':'Посмотрите окно сессии: возможно, ИИ ждёт вашего подтверждения.';}
    else if(t.status==='in_progress'){now='В работе вручную.';todo='Когда закончите — отметьте критерии и «Завершить с проверкой».';}
    else if(t.needs_plan){now='Не хватает проработки: критерии приёмки, исходный запрос или задача XL.';todo='«Проработать задачу» — заполнить недостающее.';}
    else if(t.blocked_by.length){now='Ждёт задач: '+t.blocked_by.join(', ')+'.';todo='Ничего — станет доступна, когда они закроются.';}
    else{now=(t.review?.verdict==='changes'?'Вернулась с ревью на доработку: '+t.review.conclusion:ac&&ac.ok===false?(t.status_note||'Автопроверка не пройдена.'):'Готова к запуску.')+'';todo=t.complexity?.needs_assessment?'Нажмите «Оценить» на карточке: ИИ оценит сложность, затем система назначит модель.':'Выберите ИИ на карточке и нажмите «Запустить».';}
    return `<div class="board-next"><p><b>Сейчас:</b> ${esc(now)}</p><p><b>Что делать:</b> ${esc(todo)}</p>${btn}</div>`;
  }
  function reviewInfo(t){
    const ac=t.auto_check,r=t.review;
    const checks=ac?.checks?`<h3>Автопроверка ${ac.ok?'пройдена':'не пройдена'}</h3><ul class="board-checks">${ac.checks.map(c=>`<li>${c.ok?'✅':'❌'} ${esc(c.name)}<small>${esc(c.detail)}</small></li>`).join('')}</ul>`:'';
    const rev=r?`<h3>Заключение ревью: ${esc(verdicts[r.verdict]||r.verdict)}</h3><p>${esc(r.conclusion)}</p><ul class="board-checks">${r.criteria.map(c=>`<li>${c.ok?'✅':'❌'} ${esc(c.text)}${c.note?`<small>${esc(c.note)}</small>`:''}</li>`).join('')}</ul>${r.spec?`<p><b>Отклонение от ТЗ:</b> ${esc(r.spec.deviation)}<br><b>Изменить в ТЗ:</b> ${esc(r.spec.proposal)}</p>`:''}<p class="board-muted">${esc(r.by?.agent||'')} · ${esc(r.by?.model||'')} · ${esc((r.at||'').slice(0,16).replace('T',' '))}</p>`:'';
    return checks+rev;
  }
  // Подсказки на кнопках: что произойдёт по нажатию.
  const HINTS={
    '[data-detail]':'Открыть паспорт: что сейчас происходит, что делать, критерии, план модели, статус',
    '[data-priority]':'Переключить приоритет: высокий → обычный → низкий',
    '[data-launch-ai]':'Какой ИИ выполнит задачу; рядом — модель и усилия из плана задачи',
    '[data-prompt]':'Показать задание для ИИ — чтобы вставить его в уже открытый чат',
    '[data-unlaunch]':'Снять запуск из очереди; задача вернётся в «К выполнению»',
    '[data-relaunch]':'Открыть новое окно сессии ИИ для этого запуска',
    '[data-move="in_progress"]':'Взять в работу: запустить ИИ или отметить, что выполняете вручную',
    '#start-ai':'Открыть окно сессии ИИ с моделью и усилиями из плана задачи',
    '#start-manual':'Отметить ручную работу: ИИ не запускается',
    '#start-edit':'Заполнить критерии приёмки и остальное, без чего ИИ не запустить',
    '[data-move="review"]':'Перевести задачу на проверку вручную',
    '[data-move="blocked"]':'Заблокировать с причиной — её увидят исполнители',
    '[data-move="queued"]':'Вернуть в «К выполнению»; для закрытой задачи нужна причина',
    '[data-move="cancelled"]':'Отменить задачу с причиной; активный запуск ИИ остановится',
    '#board-packet':'Паспорт для исполнителя: модель, бюджет, критерии, рабочая копия',
    '#board-edit':'Изменить задачу: тип, критерии, область правки, план модели',
    '#board-complete':'Закрыть вручную: нужны все отмеченные критерии и описание проверки',
    '#board-autocheck':'Проверить ветку: коммиты, область, тесты проекта, слияние; затем ревью ИИ и «Готово»',
    '#board-next':'Пять задач, готовых к запуску: по фокусу, приоритету и размеру',
    '#board-plan':'Создать задачу вручную, вне конвейера: проект, тип, запрос и критерии',
    '#board-idea':'Отправить идею в конвейер: Product Owner → анализ → разработка → ревью → тест → main',
    '[data-stage-info]':'Роли этапа, DoR и DoD — ровно то, что проверяет сервер',
    '[data-approve]':'Разрешить деплой в main: ворота тимлида пройдены, нужен ваш ответ',
    '#pipe-approve':'Разрешить деплой в main',
    '[data-role-run]':'Открыть сессию роли: модель роли, скилл роли, рабочая копия задачи',
    '#board-auto-pipeline':'Если включено — сервер досок сам открывает сессии ролей конвейера',
    '[data-role-ai]':'Какой ИИ выполняет эту роль во всех задачах конвейера',
    '#board-refresh':'Перечитать задачи и запуски с сервера',
    '#board-split':'Предложить распределение задач проекта между ИИ — с предпросмотром',
    '#board-autonomy':'Пошаговое — окна, ИИ спрашивает разрешения; полная автономия — одно согласие: фон, без вопросов, релиз в main без отдельного «да»',
    '[data-stop-project]':'Не открывать новые сессии ролей проекта; уже открытые доработают свой шаг',
    '[data-run-log]':'Что делает фоновая сессия: последние строки её журнала',
    '[data-ci-log]':'Прогон CI: шаги, их итог и журнал',
    '#board-failover':'Если у ИИ исчерпан лимит токенов, его задача сразу переходит к другому ИИ — с точкой остановки, моделью и усилиями под роль и сложность',
    '[data-to-idea]':'Перенести все незакрытые задачи проекта в «Идея»: агенты пройдут их заново по конвейеру; сначала предпросмотр',
    '#board-max-sessions':'Сколько окон ИИ сервер держит открытыми одновременно: автозапуск и «Запустить проект» не превышают это число',
    '[data-move="idea"]':'Начать задачу заново по конвейеру: PO → анализ → разработка → ревью → тест → main',
    '[data-start]':'Запустить работу по проекту: готовые задачи бэклога одним пакетом, число сессий — по объёму бэклога; сначала предпросмотр',
    '#start-go':'Поставить задачи пакета в очередь и открыть сессии выбранных ИИ',
    '[data-start-ai]':'Какие ИИ получают сессии пакета; сессии делятся между ними поровну',
    '#start-sessions':'Сколько сессий открыть; «по бэклогу» — 1, 2 или 3 на ИИ при готовом бэклоге до 10, до 40 и от 40 очков',
    '#board-default-ai':'ИИ, который предлагается для задач без назначения; на карточке его можно сменить перед запуском',
    '[data-relaunch-ai]':'Какой ИИ откроется; выбор другого ИИ перезакрепит запуск за ним',
    '#board-reviewer':'Кто проверяет готовую работу: тот же ИИ, другой или всегда конкретный',
    '#board-auto-review':'Если включено — сервер досок сам открывает сессию ревью после пройденной автопроверки',
    '#board-auto-merge':'Если включено — одобренная ревью ветка сама сливается в базовую, задача становится «Готово»',
    '#board-ai':'Показать задачи одного ИИ, неназначенные или занятые сейчас',
    '#board-close':'Закрыть окно',
    '#board-copy':'Скопировать задание в буфер обмена',
    '#board-view':'Показать задачи колонками по статусам',
    '#board-list-view':'Показать задачи списком',
    '#focus-start':'Личный таймер на 25 минут — не влияет на задачи',
    '#focus-pause':'Пауза или продолжение таймера',
    '#focus-add':'Добавить к таймеру 5 минут',
    '#focus-stop':'Остановить таймер',
    '#board-appearance':'Переключить светлую и тёмную тему'};
  function hints(scope){
    for(const [sel,text] of Object.entries(HINTS))scope.querySelectorAll(sel).forEach(el=>{if(!el.title)el.title=text;});
    scope.querySelectorAll('[data-launch]').forEach(el=>{const sel=scope.querySelector(`[data-launch-ai="${el.dataset.launch}"]`);el.title=canLaunch(sel?.value)?'Открыть окно сессии выбранного ИИ с моделью и усилиями из плана; ИИ сам примет задачу':'Поставить задачу в очередь выбранному ИИ и показать задание для вставки в его чат';});
  }
  // Задание для ИИ: для очереди без открытой сессии — вставляется в чат вручную.
  function showPrompt(t,r){
    const ai=aiName(r.route?.provider||'');
    modal('Задача запущена: '+t.id,`<p>Запуск <b>${esc(r.run)}</b> ждёт ${esc(ai)}${r.phase==='assess'?' — сначала оценка сложности':''}: ${esc(r.route?.model||'')} · ${esc(r.route?.effort||'')}.</p><p>Откройте новую сессию ${esc(ai)} в папке проекта: брифинг сам покажет этот запуск. Или вставьте задание в уже открытый чат:</p><textarea id="board-prompt" readonly rows="9" aria-label="Задание запуска">${esc(r.prompt)}</textarea><p><button id="board-copy">Скопировать задание</button> <span id="board-copied" role="status"></span></p><p class="board-muted">Когда ИИ примет запуск (claim), на карточке появится 🔒 с его именем и сроком аренды.</p>`);
    dialog.querySelector('#board-copy').onclick=async()=>{const area=dialog.querySelector('#board-prompt');try{await navigator.clipboard.writeText(area.value);}catch{area.select();document.execCommand('copy');}dialog.querySelector('#board-copied').textContent='Скопировано';};
  }
  // Ручной перенос: мышью между колонками или кнопками в паспорте.
  function dragAndDrop(){
    root.querySelectorAll('[data-drag]').forEach(el=>el.ondragstart=e=>{e.dataTransfer.setData('text/plain',el.dataset.drag);el.classList.add('is-dragging');});
    root.querySelectorAll('[data-drag]').forEach(el=>el.ondragend=()=>el.classList.remove('is-dragging'));
    root.querySelectorAll('[data-column]').forEach(col=>{
      col.ondragover=e=>{e.preventDefault();col.classList.add('is-drop');};
      col.ondragleave=()=>col.classList.remove('is-drop');
      col.ondrop=e=>{e.preventDefault();col.classList.remove('is-drop');const t=data.tasks[Number(e.dataTransfer.getData('text/plain'))];
        if(t&&col.dataset.column==='idea'&&column(t)!=='idea'){toIdea(t.ref,null);return;}const target=t&&(t.pipeline?col.dataset.column:LEGACY_STATUS[col.dataset.column]);if(t&&target&&(t.pipeline?column(t)!==target:t.status!==target))moveTo(t,target);};
    });
  }
  function manualBadge(t){
    if(t.status!=='in_progress'||t.claim||t.queued_run)return '';
    return `<div class="board-review board-stuck">✋ В работе вручную — ИИ не запущен${t.needs_plan?'. Для запуска ИИ сначала «Проработать задачу»: не хватает критериев приёмки':''}</div>`;
  }
  // «В работе» значит разное: запустить ИИ или отметить свою работу. Спросить, а не угадывать.
  function startDialog(t){
    const gaps=[!(t.acceptance||[]).length&&'критерии приёмки',!t.root&&'исходный запрос',t.size==='XL'&&'размер XL — сначала разбить'].filter(Boolean);
    const agent=t.executor||data.settings?.default_executor;
    const blocked=t.blocked_by.length?`Ждёт задач: ${t.blocked_by.join(', ')}.`:'';
    const launchBtn=!t.needs_plan&&!blocked&&canLaunch(agent)?`<button id="start-ai">▶ Запустить ИИ (${esc(aiName(agent))})</button>`:'';
    modal('Взять в работу: '+t.id,`<p>Перенос в «В работе» сам ИИ не запускает. Что сделать?</p>${t.needs_plan?`<p class="board-note">Запустить ИИ нельзя: у задачи нет — ${esc(gaps.join(', ')||'проработки')}. Нажмите «Проработать задачу».</p><button id="start-edit">Проработать задачу</button>`:''}${blocked?`<p class="board-note">${esc(blocked)}</p>`:''}${launchBtn}<button id="start-manual">✋ Выполняю вручную</button>`);
    dialog.querySelector('#start-edit')?.addEventListener('click',()=>{dialog.close();BOARD_EXTRAS.form({data,project,task:t,modal,dialog,api,refresh});});
    dialog.querySelector('#start-ai')?.addEventListener('click',async()=>{dialog.close();try{await openSession(t,agent);}catch(e){message.textContent=e.message;}});
    dialog.querySelector('#start-manual').onclick=()=>{dialog.close();change({op:'status',ref:t.ref,status:'in_progress'});};
  }
  // Режим согласия: одно согласие на всё или вопросы на каждом шаге.
  function autonomyDialog(v){
    if(v==='step'){change({op:'settings',changes:{autonomy:'step'}});return;}
    modal('Полная автономия — одно согласие',`<p>С этим согласием сервер досок ведёт задачи сам:</p><ul>
      <li>Сессии ИИ открываются <b>без окон</b>, в фоне; ход работы — кнопка «📄 Журнал» у роли.</li>
      <li>Claude работает в режиме <b>auto</b>: безопасные действия разрешаются сами, опасные классификатор Claude Code отклоняет. Codex — в песочнице рабочей копии с автоматической проверкой запросов (<code>--approve-for-me</code>).</li>
      <li>Агенты <b>не задают вопросов</b>: неясное решают в рамках роли и записывают допущения; блокер — эскалация тимлиду.</li>
      <li>Сессия, закончившая ответ без сдачи этапа, продолжается автоматически; после ${2} попыток — эскалация тимлиду.</li>
      <li>После ворот тимлида «тест → main» релиз в main выполняется <b>без отдельного «да»</b>.</li>
      <li>Одновременно — не больше ${data.settings?.max_sessions||4} сессий. Исчерпан лимит токенов — пауза до сброса.</li>
      <li>В удалённый репозиторий (GitHub) система <b>никогда</b> не отправляет — это остаётся за вами.</li></ul>
      <p>Вернуть пошаговое согласие можно в любой момент; уже открытые фоновые сессии доработают свой шаг.</p>
      <p><button id="autonomy-yes" class="primary">Даю согласие на полную автономию</button></p><p id="autonomy-error" class="board-start-error" role="alert"></p>`);
    dialog.querySelector('#autonomy-yes').onclick=async()=>{try{await api({op:'settings',changes:{autonomy:'full',consent:true}});dialog.close();await refresh();message.textContent='Полная автономия включена.';}catch(e){dialog.querySelector('#autonomy-error').textContent=e.message;}};
  }
  async function sessionLog(run){
    try{const r=await fetch('/api/sessionlog?run='+encodeURIComponent(run));const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||'Журнала нет');
      modal('Журнал сессии '+run+(j.data.alive?' · работает':' · завершена'),`<pre class="board-log">${esc(j.data.log||'пусто')}</pre><p><button id="log-again">Обновить</button></p>`);
      dialog.querySelector('#log-again').onclick=()=>sessionLog(run);
      const pre=dialog.querySelector('.board-log');pre.scrollTop=pre.scrollHeight;
    }catch(e){message.textContent=e.message;}
  }
  async function ciLog(run){
    try{const r=await fetch('/api/cilog?run='+encodeURIComponent(run));const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||'Журнала нет');
      modal('CI '+run,`<p>${esc(j.data.summary)}</p><pre class="board-log">${esc(j.data.log||'пусто')}</pre>`);
      const pre=dialog.querySelector('.board-log');pre.scrollTop=pre.scrollHeight;
    }catch(e){message.textContent=e.message;}
  }
  // Перенос в «Идея»: задача начинается заново по конвейеру — PO, анализ, разработка…
  async function toIdea(ref,projectKey){
    let r;
    try{r=await api(ref?{op:'to_idea',ref}:{op:'to_idea',project:projectKey});}catch(e){message.textContent=e.message;return;}
    if(!r.moved){message.textContent='Переносить нечего: '+(r.skipped_items.map(x=>x.why).filter((v,i,a)=>a.indexOf(v)===i).join(', ')||'задач нет');return;}
    modal('В «Идея»: '+(ref||data.projects[projectKey].name),`<p>Задач: <b>${r.moved}</b>${r.cancel_runs?`, будут отменены запуски: ${r.cancel_runs}`:''}. Закрытые и отменённые не переносятся (${r.skipped}).</p>
      <p>Задача начнётся заново по конвейеру: Product Owner проработает её описание в BRD, дальше анализ, разработка, ревью, тест, деплой. Окна сами не откроются — «▶ Запустить проект».</p>
      ${r.items.length>1?`<details><summary>Список</summary>${r.items.map(x=>`<div>${esc(x.ref.split('/')[1])} ${esc(x.title)} <i>(${esc(x.stage?'этап '+stageTitle(x.stage):labels[x.from_status]||x.from_status||'—')})</i></div>`).join('')}</details>`:''}
      <label class="board-reason">Причина (необязательно)<textarea id="idea-reason" maxlength="1000"></textarea></label>
      <p><button id="idea-go" class="primary">💡 Перенести в «Идея»</button></p><p id="idea-error" class="board-start-error" role="alert"></p>`);
    dialog.querySelector('#idea-go').onclick=async()=>{
      const reason=dialog.querySelector('#idea-reason').value.trim();
      try{const res=await api(Object.assign(ref?{op:'to_idea',ref}:{op:'to_idea',project:projectKey},{dry_run:false},reason?{reason}:{}));
        dialog.close();await refresh();message.textContent=`В «Идея» перенесено: ${res.moved}.`;}
      catch(e){dialog.querySelector('#idea-error').textContent=e.message;}
    };
  }
  function moveTo(t,target){
    if(target==='idea'){toIdea(t.ref,null);return;}
    if(t.pipeline&&target!=='cancelled'){message.textContent='Этап задачи конвейера меняет тимлид по DoR/DoD — вручную её можно только отменить или заблокировать в паспорте.';return;}
    if(target==='in_progress'&&!t.claim&&!t.queued_run){startDialog(t);return;}
    if(target==='done'){message.textContent='«Готово» — только с проверкой критериев: отметьте их в паспорте и нажмите «Завершить с проверкой».';detail(t);return;}
    const needReason=['blocked','cancelled'].includes(target)||t.status==='done';
    if(!needReason){change({op:'status',ref:t.ref,status:target});return;}
    const title={blocked:'Заблокировать',cancelled:'Отменить задачу',queued:'Вернуть закрытую задачу'}[target]||'Сменить статус';
    modal(title+': '+t.id,`<label class="board-reason">Причина — увидят исполнители и история задачи<textarea id="board-reason" required maxlength="1000"></textarea></label><p><button id="board-reason-ok">${esc(title)}</button></p><p id="board-reason-error" role="alert"></p>`);
    dialog.querySelector('#board-reason-ok').onclick=async()=>{const reason=dialog.querySelector('#board-reason').value.trim();if(!reason){dialog.querySelector('#board-reason').classList.add('is-missing');return;}
      try{await api({op:'status',ref:t.ref,status:target,reason});dialog.close();await refresh();message.textContent='Сохранено';}catch(e){dialog.querySelector('#board-reason-error').textContent=e.message;}};
  }
  function statusButtons(t){
    const cur=t.status,open=!['done','cancelled'].includes(t.status);
    const b=[];
    if(open&&column(t)!=='idea')b.push(['idea','💡 В «Идея» — заново по конвейеру']);
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
  function modelGauges(a){
    const groups={};
    (a.workloads||[]).filter(g=>(!project||g.project===project)&&(!ai||g.agent===ai)).forEach(g=>{
      const key=g.agent+'|'+g.model, sum=groups[key]??={agent:g.agent,model:g.model,busy:0,signals:0,queued:0,paused:0,tasks:0};
      for(const field of ['busy','signals','queued','paused','tasks'])sum[field]+=g[field]||0;
    });
    const models=Object.values(groups).sort((a,b)=>b.busy-a.busy||b.signals-a.signals||b.queued-a.queued||a.model.localeCompare(b.model));
    if(!models.length)return '';
    return `<div class="model-loads" aria-label="Загрузка моделей по задачам">${models.map(g=>{
      const cap=a.capacities?.[g.agent]??0, percent=cap>0?Math.round(g.busy/cap*100):null;
      const label=percent===null?'—':percent+'%';
      const limit=data.provider_limits?.[g.agent];
      return `<article class="model-load"><div class="model-donut" role="img" aria-label="${esc(g.model)}: ${esc(label)}, занято ${g.busy} из ${cap} слотов ${esc(aiName(g.agent))}" style="--load:${Math.min(percent||0,100)}%;--load-color:${g.agent==='codex'?'#3986dd':'#a77bda'}"><span>${label}</span></div><div><b>${esc(g.model)}</b><p>${g.busy} / ${cap} слотов ${esc(aiName(g.agent))}</p><small>${g.signals} со свежими сигналами · ${g.queued} запусков в очереди · ${g.paused} на паузе<br>${g.tasks} назначенных задач${limit?'<br>Лимит ИИ до '+esc(time(limit.until)):''}</small></div></article>`;
    }).join('')}</div><p class="activity-observed">Процент = живые процессы модели / настроенный лимит слотов её ИИ. Модели делят общую ёмкость провайдера; общий предел — ${a.max_sessions??'—'} сессии. Показана загрузка задачами в выбранном проекте.</p>`;
  }
  // Пассивное наблюдение за процессами и журналами, а не вывод из аренды задачи.
  function parallel(){
    const a=data.activity;
    if(!a)return '<section id="board-activity" class="board-activity"><h3>Активность агентов</h3><p>Текущие сигналы недоступны. Нужен обновлённый локальный сервер.</p></section>';
    const rows=a.rows.filter(r=>(!project||r.project===project)&&(!ai||r.agent===ai));
    const queued=ai?(a.workloads||[]).filter(g=>(!project||g.project===project)&&g.agent===ai).reduce((n,g)=>n+g.queued,0):project?(a.queued[project]||0):Object.values(a.queued).reduce((n,x)=>n+x,0);
    const age=v=>{if(!v)return 'нет';const sec=Math.max(0,Math.floor((Date.now()-Date.parse(v))/1000));return sec<60?`${sec} с назад`:sec<3600?`${Math.floor(sec/60)} мин назад`:`${Math.floor(sec/3600)} ч назад`;};
    const stale=activityOffline||Date.now()-Date.parse(a.observed_at)>45000;
    const groups=[['recent','Свежие сигналы'],['quiet','Процессы без свежих сигналов'],['attention','Паузы и проблемы']];
    const card=r=>`<article class="board-activity-card" data-activity-run="${esc(r.run)}" data-state="${esc(r.state)}"><b>${esc(aiName(r.agent))} · ${esc(roleTitle(r.role)||r.role||'Исполнитель')}</b><p><strong>${esc(r.ref)}</strong> ${esc(r.title)}</p><p class="activity-state">${esc(r.label)}</p><small>${esc(r.model||'Модель не указана')} · ${esc(r.run)}<br>Процесс: ${r.process_alive===true?'жив':r.process_alive===false?'завершён':'не подтверждён'}${r.pid?' · PID '+esc(r.pid):''}<br>Журнал: ${esc(age(r.log_at))} · heartbeat: ${esc(age(r.heartbeat_at))}${r.resume_at?'<br>Продолжение не ранее '+esc(time(r.resume_at)):''}${r.error?'<br>'+esc(r.error):''}</small></article>`;
    return `<section id="board-activity" class="board-activity${stale?' activity-stale':''}" aria-label="Фактическая активность агентов"><h3>Активность агентов <span class="chip">${rows.filter(r=>r.state==='recent').length} со свежими сигналами</span></h3><p class="activity-observed" role="status">${stale?'⚠ Данные устарели; текущая активность не подтверждена. ':''}Замер: ${esc(new Date(a.observed_at).toLocaleTimeString('ru'))} · в очереди: ${queued}. Обновляется вместе с доской, без запросов к ИИ.</p>${modelGauges(a)}<div class="board-activity-grid">${groups.map(([key,title])=>{const list=rows.filter(r=>key==='attention'?!['recent','quiet'].includes(r.state):r.state===key);return `<div><h4>${title} · ${list.length}</h4>${list.map(card).join('')||'<p class="board-parallel-empty">Нет</p>'}</div>`;}).join('')}</div><small>Свежий сигнал — запись журнала или heartbeat за последние ${Math.round(a.fresh_seconds/60)} минуты. Живой процесс сам по себе не подтверждает продвижение задачи.</small></section>`;
  }
  // «▶ Запустить проект»: предпросмотр пакета (что, кому, почему), затем один запуск.
  async function startDialog(k){
    const agents=new Set([data.settings?.default_executor||'claude']);let sessions=0;
    const show=async()=>{
      let r;
      try{r=await api({op:'start',project:k,agents:[...agents],sessions,dry_run:true});}catch(e){message.textContent=e.message;return;}
      const b=r.backlog,n=r.picks.length;
      modal('Запустить проект: '+data.projects[k].name,`
        <p>Бэклог: открыто <b>${b.open}</b>, готово к запуску <b>${b.ready}</b> (${b.points} очк.), в работе ${b.in_work}, ждут зависимостей или заблокированы ${b.blocked}, без плана ${b.needs_plan}${b.pipeline?`, в конвейере ${b.pipeline}`:''}.</p>
        <p>${esc(r.reason)}</p>
        <p>ИИ: ${Object.entries(providers()).map(([a,p])=>`<label><input type="checkbox" data-start-ai="${esc(a)}"${agents.has(a)?' checked':''}> ${esc(p.name)}</label>`).join(' ')}
          · Сессий: <select id="start-sessions" aria-label="Число сессий"><option value="0">по бэклогу (${r.auto_sessions})</option>${[1,2,3,4,5,6].filter(v=>v<=Math.max(b.ready,1)).map(v=>`<option value="${v}"${sessions===v?' selected':''}>${v}</option>`).join('')}</select></p>
        ${r.picks.length?`<table class="board-start-table"><thead><tr><th>Задача</th><th>Разм.</th><th>ИИ · модель</th><th>Почему сейчас</th></tr></thead><tbody>${r.picks.map(p=>`<tr><td>${esc(p.priority||'')} <b>${esc(p.ref.split('/')[1])}</b> ${esc(p.title)}${p.kind==='role'?`<div class="board-note">конвейер: ${esc(roleTitle(p.role))}</div>`:''}</td><td>${esc(p.size||'')}</td><td>${esc(aiName(p.agent))} · ${esc(p.model)} · ${esc(p.effort)}</td><td>${p.waiting?`ждут её: ${p.waiting}`:'по приоритету и размеру'}</td></tr>`).join('')}</tbody></table>`:''}
        ${r.later.length?`<details><summary>Готовы, но в следующий запуск (${r.later.length})</summary>${r.later.map(x=>`<div>${esc(x.ref.split('/')[1])} ${esc(x.title)}</div>`).join('')}</details>`:''}
        ${r.skipped.length?`<details><summary>Не запускаются сейчас (${r.skipped.length})</summary>${r.skipped.map(x=>`<div>${esc(x.ref.split('/')[1])} ${esc(x.title)} — <i>${esc(x.why)}</i></div>`).join('')}</details>`:''}
        ${n?`<p><button id="start-go" class="primary">▶ Запустить ${n} ${n===1?'сессию':n<5?'сессии':'сессий'}</button></p>`:''}
        <p class="board-start-error" id="start-error" role="alert" hidden></p>`);
      dialog.querySelectorAll('[data-start-ai]').forEach(el=>el.onchange=()=>{el.checked?agents.add(el.dataset.startAi):agents.delete(el.dataset.startAi);if(!agents.size){agents.add(el.dataset.startAi);el.checked=true;}show();});
      dialog.querySelector('#start-sessions').value=String(sessions);
      dialog.querySelector('#start-sessions').onchange=e=>{sessions=Number(e.target.value);show();};
      const go=dialog.querySelector('#start-go');
      if(go)go.onclick=async()=>{
        go.disabled=true;go.textContent='Открываю сессии…';
        try{
          const res=await fetch('/api/start',{method:'POST',headers:ttOwnerHeaders(),body:JSON.stringify({project:k,agents:[...agents],sessions})});
          const j=await res.json();if(!res.ok||!j.ok)throw Error(j.error||'Запуск не выполнен');
          if(j.data.failed.length){   // часть окон не открылась — причины здесь же, а не за окном
            const err=dialog.querySelector('#start-error');err.hidden=false;
            err.innerHTML=`Открыто ${j.data.opened.length}, не открылись ${j.data.failed.length}:<br>`+j.data.failed.map(f=>`${esc(f.ref.split('/')[1])} — ${esc(f.error)}`).join('<br>');
            go.remove();await refresh();return;
          }
          dialog.close();await refresh();
          message.textContent=`Запущено: ${j.data.opened.length} сессий.`;
        }catch(e){
          go.disabled=false;go.textContent='Повторить запуск';
          const err=dialog.querySelector('#start-error');err.hidden=false;err.textContent='Запуск не выполнен: '+e.message;
        }
      };
    };
    if(location.protocol==='file:'){message.textContent='Запуск проекта работает с сервером досок: py tools/pm.py serve';return;}
    await show();
  }

  async function splitDialog(){
    const k=project||data.focus[0];
    if(!k){message.textContent='Выберите проект доски: распределение делается внутри одного проекта.';return;}
    const agents=Object.keys(providers());
    try{
      const r=await api({op:'split',project:k,agents,dry_run:true});
      modal('Распределение: '+data.projects[k].name,`<p>Связанные задачи (зависимости, общая область scope) уходят одному ИИ целиком; группы раскладываются по наименьшей загрузке в очках размера S=1, M=3, L=8. Назначенные вручную, занятые и закрытые задачи не меняются.</p><p>${agents.map(a=>`${esc(aiName(a))}: ${r.load[a]} очк.`).join(' · ')}</p>${r.groups.map((g,i)=>`<p><label>ИИ: <select data-split-group="${i}">${agents.map(a=>`<option value="${esc(a)}"${a===g.agent?' selected':''}>${esc(aiName(a))}</option>`).join('')}</select></label> · ${g.points} очк. · ${esc(g.why)}<br>${g.tasks.map(esc).join(', ')}</p>`).join('')||'<p>Свободных задач для распределения нет.</p>'}${r.groups.length?'<p><small>Переключатель меняет ИИ сразу у всей группы. Применяется показанное здесь.</small></p><button id="board-split-apply">Применить распределение</button>':''}`);
      const apply=dialog.querySelector('#board-split-apply');
      if(apply)apply.onclick=async()=>{
        // Берём распределение из переключателей, а не пересчитываем: что показано, то и записывается.
        const assign={};
        dialog.querySelectorAll('select[data-split-group]').forEach(sel=>r.groups[+sel.dataset.splitGroup].tasks.forEach(ref=>{assign[ref]=sel.value;}));
        dialog.close();await change({op:'split',project:k,agents,dry_run:false,assign});
      };
    }catch(e){message.textContent=e.message;}
  }
  function plan(){ BOARD_EXTRAS.form({data,project,modal,dialog,api,refresh}); }
  // ---------- конвейер
  const ROLE_STATE={queued:['⏳','ждёт сессии'],running:['▶','работает'],checking:['🔎','проверки сервера'],done:['✅','сдал(а)'],returned:['↩','возврат'],pending:['·','ждёт']};
  function pipelineBadge(t,i){
    const p=t.pipeline, st=STAGES().find(s=>s.id===p.stage);
    if(p.stage==='done')return `<div class="board-review">✅ В main: все этапы сданы по DoD</div>`;
    const roles=(st?.roles||[]).filter(r=>!st.optional?.[r]||(p.needs||{})[st.optional[r]]);
    const chips=roles.map(r=>{const e=(p.roles||{})[p.stage+'/'+r]||{};const [ic,tx]=ROLE_STATE[e.state]||ROLE_STATE.pending;
      const any=data.runs.find(x=>x.id===e.run&&['queued','running'].includes(x.status));
      const run=any&&any.status==='queued'&&!any.launched_at?any:null;
      const live=any&&any.launched_at?(any.paused?'⏸ пауза по лимиту до '+time(any.paused.resume_at):any.status==='running'?'работает':'сессия открыта, ждёт claim'):'';
      const fails=(e.result||[]).filter(x=>x.ok===false);
      return `<div class="board-role-row" title="${esc(fails.map(f=>f.id+': '+f.detail).join('\n'))}">${ic} <b>${esc(roleTitle(r))}</b> — ${esc(live||tx)}${fails.length?` · ✗ DoD: ${fails.length}`:''}${run&&canLaunch(run.agent)?` <button data-role-run="${esc(run.id)}" data-role-task="${i}">Открыть</button>`:''}${any?.launch?.headless?` <button data-run-log="${esc(any.id)}">📄 Журнал</button>`:''}</div>`;}).join('');
    const prob=p.blocked?.problem?(data.problems||[]).find(x=>x.id===p.blocked.problem):null;
    const blockLine=prob?`<div class="board-role-row">${prob.status==='owner'?'👤 Нужен владелец — ответ в колокольчике 🔔':prob.status==='resolving'?'🧩 Решала разбирает проблему':'🧩 Проблема'} ${esc(prob.id)}: ${esc((prob.question||prob.reason||'').slice(0,160))}</div>`:'';
    const gate=st?.gate&&roles.every(r=>((p.roles||{})[p.stage+'/'+r]||{}).state==='done')?`<div class="board-role-row">🚦 Ворота тимлида: ${esc((p.gates||{})[st.gate]?.decision||'ждут решения')}</div>`:'';
    const approve=p.stage==='deploy_main'&&(p.gates||{}).release?.decision==='approve'&&!(p.approvals||{}).release?`<button class="board-approve" data-approve="${i}">👤 Одобрить релиз в main</button>`:'';
    const note=t.status_note?`<div class="board-note${t.status==='blocked'?'':' board-note-soft'}">${t.status==='blocked'?'⛔ ':''}${esc(t.status_note)}</div>`:'';
    const CIS={task:'ветка задачи',test:'тест',main:'main'}, CIC={passed:'✅',failed:'❌',running:'⏳',skipped:'➖',not_configured:'⚠️',stale:'🕓',error:'⛔'};
    const cis=Object.entries(t.ci||{}).filter(([k])=>CIS[k]);
    const ciLine=cis.length?`<div class="board-role-row">CI: ${cis.map(([k,r])=>`<button class="board-ci" data-ci-log="${esc(r.id)}" title="${esc(r.note||((r.branch||'')+'@'+(r.sha||'')))}">${CIC[r.status]||'·'} ${esc(CIS[k])}</button>`).join(' ')}</div>`:'';
    const EVS={PASS:'✅ PASS',STALE:'🕓 STALE',FAILED:'❌ FAILED',NOT_RUN:'➖ NOT_RUN'}, ev=t.evidence_state;
    const evLine=ev&&['deploy_test','testing','deploy_main','done'].includes(p.stage)?`<div class="board-role-row board-ev board-ev-${esc(ev.state.toLowerCase())}" title="${esc((ev.source||'server')+(ev.at?' · '+ev.at:''))}">Доказательства выпуска: ${EVS[ev.state]||esc(ev.state)} — ${esc(ev.detail||'')}${ev.at?` <small>${esc(String(ev.at).slice(0,16).replace('T',' '))}</small>`:''}</div>`:'';
    return `<div class="board-pipe">${blockLine}${chips}${gate}${ciLine}${evLine}</div>${note}${approve}`;
  }
  const kindRu={artifact:'артефакт',evidence:'доказательство',machine:'сервер',gate:'ворота'};
  function stageInfo(id){
    const st=STAGES().find(s=>s.id===id);if(!st)return;
    const items=its=>`<ul class="board-checks">${its.map(i=>`<li><code>${esc(i.id)}</code> ${esc(i.text)} <small>${esc(kindRu[i.kind]||i.kind)}${i.allow_na?' · можно «неприменимо» с причиной':''}</small></li>`).join('')}</ul>`;
    const AI={claude:'Claude',codex:'GPT / Codex'};
    const routing=r=>{const x=data.pipeline.roles[r].routing;if(!x)return '';const pin=(data.settings?.roles||{})[r];
      return `<li><b>${esc(roleTitle(r))}</b>: ${pin?`закреплена за ${esc(AI[pin]||pin)} (ваш выбор); иначе `:''}${x.prefer.map(a=>esc(AI[a]||a)).join(' → ')}${x.cross?' (другой ИИ, чем разработчик)':''} · класс модели по сложности ${x.tier.join('/')} · усилия ${x.effort.join('/')}<br><small>${esc(x.why)}</small></li>`;};
    modal('Этап «'+st.title+'»',`<p>Роли: ${st.roles.map(r=>esc(roleTitle(r))+(st.optional?.[r]?' (если нужен по решению PO)':'')+' — скилл <code>'+esc(data.pipeline.roles[r].skill)+'</code>').join('; ')}.</p>
      <h3>ИИ, модель и усилия</h3><ul class="board-checks">${st.roles.map(routing).join('')}</ul><p><small>Класс и усилия — для простой / средней / сложной задачи по оценке сложности. ИИ на лимите токенов пропускается: задача переходит к следующему.</small></p>${st.gate?'<p>🚦 После сдачи ролей — ворота ИИ-тимлида.</p>':''}${st.owner_approval?'<p>👤 Нужно «да» владельца.</p>':''}<h3>DoR — без него роли не получают задачу</h3>${items(st.dor)}${Object.entries(st.dod).map(([r,its])=>`<h3>DoD: ${esc(roleTitle(r))} — без него передача не принимается</h3>${items(its)}`).join('')}`);
  }
  function pipelineDetail(t){
    const p=t.pipeline;
    const res=(rows)=>`<ul class="board-checks">${(rows||[]).map(x=>`<li>${x.ok===true?'✅':x.ok===false?'❌':'⏳'} <code>${esc(x.id)}</code><small>${esc(x.detail||'')}</small></li>`).join('')}</ul>`;
    const stages=STAGES().map(st=>{const s=(p.stages||{})[st.id]||{};const cur=p.stage===st.id;
      const roles=st.roles.filter(r=>(p.roles||{})[st.id+'/'+r]);
      return `<details${cur?' open':''}><summary>${s.passed?'✅':cur?'▶':'·'} ${esc(st.title)}${cur?' — текущий':''}</summary>${s.dor?`<p><b>DoR</b></p>${res(s.dor)}`:''}${roles.map(r=>{const e=p.roles[st.id+'/'+r];return `<p><b>${esc(roleTitle(r))}</b> — ${esc((ROLE_STATE[e.state]||[])[1]||e.state)}${e.handoff?.summary?': '+esc(e.handoff.summary):''}</p>${e.result?res(e.result):''}${e.feedback?`<p class="board-note">Замечания: ${esc(e.feedback.map(f=>f.detail||f.text).join('; '))}</p>`:''}`;}).join('')}</details>`;}).join('');
    const gates=Object.entries(p.gates||{}).map(([g,v])=>`<li>🚦 ${esc(g)}: ${esc(v.decision)} — ${esc(v.notes||'')}</li>`).join('');
    const wt=p.worktree?`<p class="board-workspace"><b>Ветка задачи</b><span><code>${esc(p.worktree.branch)}</code> · <code>${esc(p.worktree.path)}</code></span><span>Артефакты: <code>docs/tasks/${esc(t.id)}/</code></span></p>`:'';
    const journal=(p.journal||[]).slice().reverse().map(e=>`<li><small>${esc(e.at.slice(5,16).replace('T',' '))}</small> ${esc(e.text)}</li>`).join('');
    const i=data.tasks.indexOf(t);
    modal(t.ref,`<p><b>${esc(t.title)}</b></p><div class="board-next"><p><b>Сейчас:</b> ${esc(p.stage==='done'?'Задача в main.':'Этап «'+stageTitle(p.stage)+'». '+(t.status_note||''))}</p>${p.blocked?`<p><b>Блокер:</b> ${esc(p.blocked.reason)}</p>`:''}</div><p>${esc(t.why||'')}</p>${wt}${(t.acceptance||[]).length?`<h3>Критерии приёмки</h3><ul>${t.acceptance.map(a=>`<li>${esc(a)}</li>`).join('')}</ul>`:''}<h3>Этапы</h3>${stages}${gates?`<h3>Ворота</h3><ul>${gates}</ul>`:''}<h3>Журнал задачи</h3><ul class="board-journal">${journal}</ul>${p.stage==='deploy_main'&&(p.gates||{}).release?.decision==='approve'&&!(p.approvals||{}).release?`<button id="pipe-approve">👤 Одобрить релиз в main</button>`:''}<div class="board-status-actions">${t.status!=='cancelled'?'<button data-move="cancelled">✕ Отменить</button>':''}</div>`);
    dialog.querySelector('#pipe-approve')?.addEventListener('click',()=>{dialog.close();change({op:'approve_release',ref:t.ref});});
    dialog.querySelectorAll('[data-move]').forEach(el=>el.onclick=()=>{dialog.close();moveTo(t,el.dataset.move);});
  }
  function teamleadJournal(){
    const ev=(data.teamlead||[]).filter(e=>!project||e.ref.split('/')[0]===project).slice(-30).reverse();
    if(!ev.length)return '';
    const icon={handoff:'📨',handoff_rejected:'⛔',check_failed:'❌',check_passed:'✅',stage_passed:'🏁',stage_started:'▶',gate:'🚦',escalation:'🆘',returned:'↩',idea:'💡',run:'⏳',dor_failed:'⚠',dor_wait:'👤',approved:'👤',done:'🎉'};
    return `<details class="board-journal-box"><summary>📋 Журнал тимлида (${ev.length})</summary><ul class="board-journal">${ev.map(e=>`<li>${icon[e.kind]||'·'} <small>${esc(e.at.slice(5,16).replace('T',' '))} · ${esc(e.ref)}</small> ${esc(e.text)}</li>`).join('')}</ul></details>`;
  }
  function ideaDialog(){
    modal('💡 Новая идея',`<p>Идея попадёт в колонку «Идея»; дальше её подхватит Product Owner, затем аналитика и остальные роли по DoR/DoD.</p><form id="idea-form" class="board-form" novalidate><label class="wide"><span>Проект <b class="board-star">*</b></span><select id="idea-project" required><option value="">Выберите проект</option>${Object.entries(data.projects).map(([k,p])=>`<option value="${esc(k)}" ${project===k?'selected':''}>${esc(p.name)}</option>`).join('')}</select></label><label class="wide"><span>Идея коротко <b class="board-star">*</b></span><input id="idea-title" required minlength="5" maxlength="200"></label><label class="wide"><span>Что и зачем, своими словами <b class="board-star">*</b></span><textarea id="idea-text" required minlength="20"></textarea></label><div class="wide"><button type="submit">Отправить в конвейер</button><p id="idea-error" role="alert"></p></div></form>`);
    const f=dialog.querySelector('#idea-form');
    f.onsubmit=async e=>{e.preventDefault();f.classList.add('tried');if(!f.checkValidity()){dialog.querySelector('#idea-error').textContent='Заполните подсвеченные поля.';return;}
      try{const r=await api({op:'idea',project:dialog.querySelector('#idea-project').value,title:dialog.querySelector('#idea-title').value.trim(),text:dialog.querySelector('#idea-text').value.trim(),source:'board'});dialog.close();await refresh();message.textContent='Идея '+r.id+' в конвейере: Product Owner получит её автоматически.';}catch(err){dialog.querySelector('#idea-error').textContent=err.message;}};
  }
  // Автообновление: пока вкладка видна, доска подтягивает чужие запуски и аренды.
  // Перерисовка не идёт при открытом диалоге и пока пользователь печатает или выбирает в фильтрах:
  // иначе сбросится фокус и введённое.
  const POLL_MS=20000;
  let polling=false;
  const typing=()=>{const el=document.activeElement;return !!el&&root.contains(el)&&/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName);};
  async function poll(){
    if(polling||document.hidden||!root.offsetParent||location.protocol==='file:')return;
    polling=true;
    try{const r=await fetch('/api/work');const j=await r.json();if(!r.ok||!j.ok)throw Error('Нет связи');activityOffline=false;if(dialog.open||typing()){data.activity=j.data.activity;root.querySelector('#board-activity')?.replaceWith(document.createRange().createContextualFragment(parallel()));}else{data=j.data;render();}}catch{activityOffline=true;root.querySelector('#board-activity')?.replaceWith(document.createRange().createContextualFragment(parallel()));}finally{polling=false;}
  }
  setInterval(poll,POLL_MS);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
  render();
  if(location.protocol!=='file:')refresh().catch(e=>message.textContent=e.message);
})();
