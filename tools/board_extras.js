/* Общие формы и фокус-сессия; никаких вызовов модели для интерфейсной логики. */
window.BOARD_EXTRAS = (() => {
  const load=(k,d)=>{try{return JSON.parse(localStorage.getItem(k))??d;}catch{return d;}};
  const save=(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v));}catch{}};
  let timer=load('tt-focus-timer',{state:'idle',remaining:1500000,end:null,project:''});
  let view=load('tt-board-view','board');
  const palette=['#8b7cf8','#36a7e7','#e55b95','#36b695','#e6a34c','#8596bb'];
  const style=document.createElement('style');style.textContent=`
  #boards-root{padding:22px;border:1px solid var(--line);border-radius:24px;background:radial-gradient(ellipse at top right,color-mix(in srgb,var(--accent) 9%,transparent),transparent 65%)}
  .board-column{border:1px solid var(--line);border-radius:18px!important;background:color-mix(in srgb,var(--panel2) 85%,transparent)!important;min-height:170px;align-self:start}.board-column h3{font-size:13px}.board-task{border-radius:15px!important;box-shadow:var(--shadow)}.board-task h4{font-size:13px;line-height:1.5}.board-task small{font-size:11px}.board-project h2{font-size:17px!important}.board-task .route-detail{display:none}.board-tag{display:inline-block;background:color-mix(in srgb,var(--project-color) 18%,transparent);border-radius:6px;padding:4px 7px;font-size:10px;color:var(--ink2)}
  .board-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:20px 0}.board-stat{padding:16px;border:1px solid var(--line);border-radius:15px;background:var(--panel)}.board-stat small{display:block;color:var(--ink3)}.board-stat strong{font-size:27px;display:block;margin-top:5px}.board-actions{display:flex;gap:8px;flex-wrap:wrap;margin:15px 0}.board-actions [aria-pressed=true]{background:var(--accent)!important;color:white!important}
  #boards-root[data-view=list] .board-columns{display:block}#boards-root[data-view=list] .board-column{max-height:none!important;margin:10px 0;min-height:0}#boards-root[data-view=list] .board-task{display:flex;align-items:center;gap:12px;margin:8px 0}#boards-root[data-view=list] .board-task h4{flex:1;min-width:180px}#boards-root[data-view=list] .board-task small{display:none}#boards-root[data-view=list] .board-task>div{min-width:120px}#boards-root[data-view=list] .board-task .board-tag{min-width:90px}
  .board-progress{height:4px;background:var(--line);border-radius:5px;overflow:hidden}.board-progress span{display:block;height:100%;background:var(--project-color)}.board-meta{display:flex;justify-content:space-between;font-size:11px;color:var(--ink3);gap:8px;margin:9px 0}.board-overdue{color:var(--bad)}
  .board-dialog{border-radius:20px!important}.board-dialog::backdrop{background:rgba(0,0,0,.5);backdrop-filter:blur(3px)}.board-form{display:grid;grid-template-columns:1fr 1fr;gap:12px}.board-form label{display:flex;flex-direction:column;gap:6px;font-size:12px}.board-form .wide{grid-column:1/-1}.board-form input,.board-form select,.board-form textarea{width:100%;padding:10px;border:1px solid var(--line);border-radius:9px;background:var(--panel2);color:var(--ink);font:inherit;box-sizing:border-box}.board-form textarea{min-height:90px}.board-dialog [role=alert]{color:var(--bad)}.board-checklist label{display:flex;gap:8px;margin:10px 0}.board-model-plan{border:1px solid var(--line);border-radius:12px;padding:10px 14px;display:grid;gap:10px}.board-model-plan legend{font-size:12px;padding:0 6px}.board-plan-row{display:grid;grid-template-columns:110px 1fr 1fr;gap:10px;align-items:end}.board-plan-row .board-plan-rec{grid-column:1/-1;color:var(--ink3);font-size:11px}.board-plan-off{opacity:.45}.board-star{color:var(--bad);font-weight:700}.board-form.tried :invalid,.board-form .is-missing{border-color:var(--bad)!important;box-shadow:0 0 0 2px color-mix(in srgb,var(--bad) 25%,transparent)}.board-form fieldset{border:1px solid var(--line);border-radius:12px;padding:10px 14px}.board-form legend{font-size:12px;padding:0 6px}.board-types{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px}.board-type{flex-direction:row!important;align-items:flex-start;gap:8px!important;border:1px solid var(--line);border-radius:10px;padding:9px;cursor:pointer}.board-type input{width:auto!important;margin-top:3px}.board-type span{display:flex;flex-direction:column;gap:3px}.board-type small{color:var(--ink3)}.board-type.is-active{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,transparent)}.board-inline{display:inline-flex!important;flex-direction:row!important;align-items:center;gap:6px!important;margin-right:16px}.board-inline input,.board-dirs input{width:auto!important}.board-root>div{display:grid;gap:10px;margin-top:8px}.board-dirs{display:flex;flex-wrap:wrap;gap:10px 18px}.board-dirs label{flex-direction:row!important;align-items:center;gap:6px!important}.board-workspace{display:flex;flex-direction:column;gap:3px;font-size:12px;padding:10px 12px;border-radius:10px;background:var(--panel2);overflow-wrap:anywhere}.board-workspace code{font-size:11px}.board-type-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}.board-type-fields:empty{display:none}.board-muted{color:var(--ink3);font-size:12px;margin:0}@media(max-width:650px){.board-type-fields{grid-template-columns:1fr}}@media(max-width:650px){.board-plan-row{grid-template-columns:1fr}}
  .board-timer{display:flex;align-items:center;justify-content:space-between;gap:15px;flex-wrap:wrap;margin:15px 0;padding:16px 20px;background:var(--panel2);border:1px solid var(--line);border-radius:16px}.board-timer strong{display:block;font-size:28px;font-variant-numeric:tabular-nums}.board-timer small{color:var(--ink3)}.board-timer button{margin:3px}
  @media(max-width:650px){#boards-root{padding:12px}.board-stats{grid-template-columns:1fr 1fr}.board-form{grid-template-columns:1fr}.board-form .wide{grid-column:auto}#boards-root[data-view=list] .board-task{flex-wrap:wrap}.board-tools select{max-width:100%}}
  `;document.head.append(style);
  const date=d=>[d.getFullYear(),String(d.getMonth()+1).padStart(2,'0'),String(d.getDate()).padStart(2,'0')].join('-');
  function decorate(root,data,visible,project){
    root.dataset.view=view;
    const today=date(new Date()),last=new Date();last.setDate(last.getDate()+7);
    const open=visible.filter(t=>!['done','cancelled'].includes(t.status));
    const stats=document.createElement('div');stats.className='board-stats';
    stats.innerHTML=[['Открыто',open.length],['На ближайшие 7 дней',open.filter(t=>t.due_date&&t.due_date>=today&&t.due_date<=date(last)).length],['Просрочено',open.filter(t=>t.due_date&&t.due_date<today).length],['Завершено',visible.filter(t=>t.status==='done').length]].map(([label,n])=>`<div class="board-stat"><small>${label}</small><strong>${n}</strong></div>`).join('');
    root.insertBefore(stats,root.querySelector('.board-tools'));
    const actions=document.createElement('div');actions.className='board-actions';actions.innerHTML=`<button id="board-view" aria-pressed="${view==='board'}">▥ Доска</button><button id="board-list-view" aria-pressed="${view==='list'}">☷ Список</button><button id="board-appearance">Оформление</button>`;
    root.insertBefore(actions,root.querySelector('#board-list'));
    for(const [id,mode] of [['board-view','board'],['board-list-view','list']])root.querySelector('#'+id).onclick=()=>{view=mode;save('tt-board-view',view);root.dataset.view=view;actions.querySelector('#board-view').setAttribute('aria-pressed',view==='board');actions.querySelector('#board-list-view').setAttribute('aria-pressed',view==='list');};
    root.querySelector('#board-appearance').onclick=()=>{const next=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';document.documentElement.setAttribute('data-theme',next);try{localStorage.setItem('tt-theme',next);}catch{}};
    root.querySelectorAll('.board-task').forEach(el=>{
      const index=Number(el.querySelector('[data-detail]').dataset.detail),t=data.tasks[index],color=palette[Object.keys(data.projects).indexOf(t.project)%palette.length];
      el.style.setProperty('--project-color',color);
      const tag=document.createElement('span');tag.className='board-tag';tag.textContent=data.projects[t.project].name;el.prepend(tag);
      const small=el.querySelectorAll('small');if(small[2])small[2].classList.add('route-detail');if(small[3])small[3].classList.add('route-detail');
      const total=(t.acceptance||[]).length,done=(t.acceptance_done||[]).length;
      const meta=document.createElement('div');meta.innerHTML=`${total?`<div class="board-progress" role="progressbar" aria-label="Проверены критерии" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}"><span style="width:${100*done/total}%"></span></div>`:''}<div class="board-meta"><span class="${t.due_date&&t.due_date<today&&t.status!=='done'?'board-overdue':''}">${esc(t.due_date||'Без срока')}</span><span>${total?done+' / '+total:''}</span></div>`;el.insertBefore(meta,el.querySelector(':scope > button'));
    });
    const box=document.createElement('div');box.className='board-timer';box.innerHTML='<div><small id="focus-caption">Фокус-сессия</small><strong id="focus-clock"></strong></div><div><button id="focus-start">Начать 25 минут</button><button id="focus-pause">Пауза</button><button id="focus-add">＋ 5 минут</button><button id="focus-stop">Завершить</button></div>';root.insertBefore(box,actions);
    const persist=()=>{save('tt-focus-timer',timer);tick();};
    box.querySelector('#focus-start').onclick=()=>{timer={state:'running',remaining:1500000,end:Date.now()+1500000,project:data.projects[project]?.name||data.projects[data.focus[0]]?.name||''};persist();};
    box.querySelector('#focus-pause').onclick=()=>{if(timer.state==='running'){timer.remaining=Math.max(0,timer.end-Date.now());timer.end=null;timer.state='paused';}else if(timer.state==='paused'){timer.end=Date.now()+timer.remaining;timer.state='running';}persist();};
    box.querySelector('#focus-add').onclick=()=>{if(timer.state==='running')timer.end+=300000;else timer.remaining+=300000;persist();};
    box.querySelector('#focus-stop').onclick=()=>{timer={state:'idle',remaining:1500000,end:null,project:''};persist();};tick();
  }
  function tick(){const clock=document.getElementById('focus-clock');if(!clock)return;let ms=timer.state==='running'?Math.max(0,timer.end-Date.now()):timer.remaining;if(timer.state==='running'&&ms===0){timer.state='finished';timer.remaining=0;timer.end=null;save('tt-focus-timer',timer);}const seconds=Math.ceil(ms/1000);clock.textContent=String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');document.getElementById('focus-caption').textContent=({idle:'Фокус-сессия',running:'В фокусе',paused:'Пауза',finished:'Время вышло — перерыв'}[timer.state]||'Фокус')+(timer.project?' · '+timer.project:'');document.getElementById('focus-start').disabled=['running','paused'].includes(timer.state);document.getElementById('focus-add').disabled=!['running','paused'].includes(timer.state);document.getElementById('focus-pause').disabled=!['running','paused'].includes(timer.state);document.getElementById('focus-pause').textContent=timer.state==='paused'?'Продолжить':'Пауза';}
  setInterval(tick,1000);addEventListener('storage',e=>{if(e.key==='tt-focus-timer'){timer=load('tt-focus-timer',timer);tick();}});
  // Форма задачи. Проект задаёт рабочее место ИИ, тип — набор обязательных полей,
  // исходный запрос по умолчанию новый. Обязательное помечено «*» и подсвечивается.
  function form({data,project,task,modal,dialog,api,refresh}){
    const types=data.task_types||{}, dirs=data.directions||[];
    const k=task?.project||project||'';
    const val=(key,d='')=>esc(task?.[key]??d);
    const options=(rows,current)=>rows.map(([v,label])=>`<option value="${esc(v)}" ${v===current?'selected':''}>${esc(label)}</option>`).join('');
    const star='<b class="board-star" aria-hidden="true">*</b>';
    const kinds={implementation:'Разработка',test:'Проверка',docs:'Документация',architecture:'Архитектура',research:'Исследование'};
    // Поля типа: подпись берётся из каталога сервера, чтобы не расходиться с проверкой.
    const typeFields={
      dev_by_spec:()=>`<label class="wide"><span>${esc(types.dev_by_spec.fields.spec_ref)} ${star}</span><input id="task-spec_ref" value="${val('spec_ref')}" required maxlength="2000" placeholder="docs/ТЗ_01.md §4, требования ИНТ-06…08"></label>`,
      analysis_spec:()=>`<label class="wide"><span>${esc(types.analysis_spec.fields.question)} ${star}</span><textarea id="task-question" required maxlength="2000">${val('question')}</textarea></label><label class="wide"><span>${esc(types.analysis_spec.fields.inputs)} ${star}</span><textarea id="task-inputs" required maxlength="2000" placeholder="Интервью, документы, данные, ссылки">${val('inputs')}</textarea></label><label class="wide"><span>${esc(types.analysis_spec.fields.deliverable)} ${star}</span><input id="task-deliverable" value="${val('deliverable')}" required maxlength="2000" placeholder="docs/ТЗ_02_….md"></label>`,
      decomposition:()=>`<fieldset class="wide board-dirs" id="task-directions"><legend>${esc(types.decomposition.fields.directions)} ${star}</legend>${dirs.map(d=>`<label><input type="checkbox" value="${esc(d)}" ${(task?.directions||[]).includes(d)?'checked':''}> ${esc(d)}</label>`).join('')}</fieldset>`,
      direction_task:()=>`<label><span>${esc(types.direction_task.fields.direction)} ${star}</span><select id="task-direction" required><option value="">Выберите</option>${options(dirs.map(d=>[d,d]),task?.direction||'')}</select></label><label class="wide"><span>${esc(types.direction_task.fields.deliverable)} ${star}</span><input id="task-deliverable" value="${val('deliverable')}" required maxlength="2000"></label>`,
    };
    const typeCards=Object.entries(types).map(([id,t])=>`<label class="board-type"><input type="radio" name="task-type" value="${esc(id)}" ${task?.task_type===id?'checked':''} ${!task||task.task_type?'required':''}><span><b>${esc(t.name)}</b><small>${esc(t.hint)}</small></span></label>`).join('');
    modal(task?'Редактирование задачи':'Новая задача',`<p>Поля со ${star} обязательны. Набор полей зависит от типа задачи.</p>
<form id="board-form" class="board-form" novalidate>
 <label class="wide"><span>Проект — где работает ИИ ${star}</span><select id="task-project" required ${task?'disabled':''}><option value="">Выберите проект</option>${options(Object.entries(data.projects).map(([key,p])=>[key,p.name]),k)}</select></label>
 <div class="wide board-workspace" id="task-workspace"></div>
 <fieldset class="wide board-types" id="task-type"><legend>Тип задачи ${!task||task.task_type?star:'<small>(задача создана до появления типов — можно выбрать)</small>'}</legend>${typeCards}</fieldset>
 <fieldset class="wide board-root"><legend>Исходный запрос ${star}</legend>
  <label class="board-inline"><input type="radio" name="root-mode" value="new" ${task?.root?'':'checked'}> Новый запрос</label>
  <label class="board-inline"><input type="radio" name="root-mode" value="existing" ${task?.root?'checked':''}> Из памяти проекта</label>
  <div id="root-new"><label><span>Запрос коротко ${star}</span><input id="task-root-title" maxlength="200" required placeholder="По умолчанию — название задачи"></label><label><span>Что просили, своими словами ${star}</span><textarea id="task-root-text" maxlength="5000" required></textarea></label></div>
  <div id="root-existing" hidden><label><span>Запрос ${star}</span><select id="task-root" required></select></label></div>
 </fieldset>
 <label>Номер задачи<input id="task-id" value="${val('id')}" ${task?'disabled':''} maxlength="60"></label>
 <label><span>Размер ${star}</span><select id="task-size" ${task?'disabled':''}>${options([['S','Небольшая'],['M','Средняя'],['L','Крупная']],task?.size||'S')}</select></label>
 <label class="wide"><span>Название ${star}</span><input id="task-title" value="${val('title')}" required maxlength="1000"></label>
 <label class="wide"><span>Зачем нужна задача ${star}</span><input id="task-why" value="${val('why')}" required maxlength="1000"></label>
 <div class="wide board-type-fields" id="task-type-fields"></div>
 <label class="wide"><span>Критерии приёмки — по одному на строку ${star}</span><textarea id="task-acceptance" required>${esc((task?.acceptance||[]).join('\n'))}</textarea></label>
 <label><span>Вид работы ${star}</span><select id="task-kind" required></select></label>
 <label><span>Приоритет ${star}</span><select id="task-priority">${options([['🔴','Высокий'],['🟡','Обычный'],['🟢','Низкий']],task?.priority||'🟡')}</select></label>
 <label><span>Риск ${star}</span><select id="task-risk">${options([['low','Низкий'],['medium','Средний'],['high','Высокий']],task?.risk||'low')}</select></label>
 <label>Срок<input type="date" id="task-due" value="${val('due_date')}"></label>
 <label>Зависимости — номера через запятую<input id="task-deps" value="${esc((task?.depends_on||[]).join(', '))}" ${task?'disabled':''}></label>
 <label>ИИ-исполнитель<select id="task-executor">${options([['','Любой — решит тот, кто возьмёт'],...Object.entries(data.providers||{}).map(([key,p])=>[key,p.name])],task?.executor||'')}</select></label>
 <label class="wide">Область правки — пути через запятую<input id="task-scope" value="${esc((task?.scope||[]).join(', '))}" placeholder="tools/boards.js, tests/"><small id="task-scope-hint"></small></label>
 <label class="wide" id="task-constraints-box" hidden>Ограничения и рамки<textarea id="task-constraints" maxlength="2000">${val('constraints')}</textarea></label>
 <fieldset class="wide board-model-plan"><legend>План модели — модель и усилия под эту задачу для каждого ИИ</legend>${Object.entries(data.providers||{}).map(([key,p])=>{const m=task?.model_plan?.[key],manual=m&&m.source==='manual';return `<div class="board-plan-row" data-plan="${esc(key)}"><b>${esc(p.name)}</b><label>Модель<select id="plan-model-${esc(key)}"><option value="">Рекомендованная</option>${options(data.models.filter(x=>x.provider===key).map(x=>[x.id,x.id]),manual?m.model:'')}</select></label><label>Усилия<select id="plan-effort-${esc(key)}"><option value="">Рекомендованные</option>${options(((data.efforts||{})[key]||[]).map(e=>[e,e]),manual?m.effort:'')}</select></label><small id="plan-rec-${esc(key)}" class="board-plan-rec"></small></div>`;}).join('')}<label>Обоснование ручного выбора (нужно, если меняете рекомендацию)<input id="task-model-reason" value="${esc(Object.values(task?.model_plan||{}).find(m=>m.source==='manual')?.reason||task?.model_reason||'')}"></label></fieldset>
 <div class="wide"><button id="board-save" type="submit">Сохранить задачу</button><p id="board-plan-error" role="alert"></p></div>
</form>`);
    const $=sel=>dialog.querySelector(sel), f=$('#board-form');
    const typeOf=()=>f.querySelector('[name=task-type]:checked')?.value||'';
    const rootMode=()=>f.querySelector('[name=root-mode]:checked')?.value||'existing';
    // Скрытые поля выключаются: required на них не мешает сохранить форму.
    const show=(el,on)=>{el.hidden=!on;el.querySelectorAll('input,select,textarea').forEach(x=>x.disabled=!on);};
    function workspace(){
      const p=data.projects[$('#task-project').value];
      $('#task-workspace').innerHTML=p?`<b>Рабочее место ИИ</b><span>Папка: <code>${esc(p.path||'не указана')}</code>${p.exists===false?' — <em>не найдена на диске</em>':''}</span>${p.active_copy?`<span>Активная копия: <code>${esc(p.active_copy)}</code></span>`:''}<span>Репозиторий: ${p.repo?`<code>${esc(p.repo)}</code>`:'без удалённого репозитория'}</span>`:'<span>Выберите проект: от него зависят папка, репозиторий и память, с которыми будет работать ИИ.</span>';
      if(!task)$('#task-id').placeholder=p?.next_id?'Автоматически: '+p.next_id:'Автоматически';
      roots();
    }
    function roots(){
      const p=$('#task-project').value,records=(data.roots||[]).filter(r=>r.project===p);
      if(task?.root&&!records.some(r=>r.id===task.root))records.push({id:task.root,title:task.root});
      $('#task-root').innerHTML='<option value="">Выберите запрос</option>'+options(records.map(r=>[r.id,r.title]),task?.root||'');
      const existing=f.querySelector('[name=root-mode][value=existing]');
      existing.disabled=!records.length;existing.parentElement.title=records.length?'':'В памяти проекта нет активных запросов';
      if(!records.length)f.querySelector('[name=root-mode][value=new]').checked=true;
      showRoot();
    }
    function showRoot(){show($('#root-new'),rootMode()==='new');show($('#root-existing'),rootMode()==='existing');}
    function showType(){
      const t=typeOf(),spec=types[t];
      f.querySelectorAll('.board-type').forEach(el=>el.classList.toggle('is-active',el.querySelector('input').checked));
      $('#task-type-fields').innerHTML=spec?typeFields[t]():'<p class="board-muted">Выберите тип задачи — появятся нужные для него поля.</p>';
      const allowed=spec?spec.work_types:Object.keys(kinds),cur=$('#task-kind').value||task?.work_type;
      $('#task-kind').innerHTML=options(allowed.map(w=>[w,kinds[w]||w]),allowed.includes(cur)?cur:allowed[0]);
      show($('#task-constraints-box'),['analysis_spec','decomposition'].includes(t));
      $('#task-scope-hint').textContent=t==='dev_by_spec'?'Рекомендуется: при параллельной работе двух ИИ область не даёт править одни файлы.':'';
      recalc();
    }
    // Рекомендация модели считается сервером по тем же правилам, что и при записи плана.
    let rec=task?.routes||{};
    function showRec(){for(const [key,r] of Object.entries(rec)){const el=$('#plan-rec-'+key);if(el)el.textContent=`Рекомендация: ${r.model} · ${r.effort} — ${r.reason}`;}
      const ex=$('#task-executor').value;f.querySelectorAll('.board-plan-row').forEach(row=>row.classList.toggle('board-plan-off',!!ex&&row.dataset.plan!==ex));}
    async function recalc(){try{const r=await api({op:'route',task:{size:$('#task-size').value,risk:$('#task-risk').value,work_type:$('#task-kind').value}});rec=r.model_plan;}catch{} showRec();}
    function modelPlan(){const out={};for(const key of Object.keys(data.providers||{})){const m=$('#plan-model-'+key).value,e=$('#plan-effort-'+key).value;out[key]=m||e?{model:m||rec[key]?.model,effort:e||rec[key]?.effort,reason:$('#task-model-reason').value.trim()}:null;}return out;}
    $('#task-project').onchange=workspace;
    f.querySelectorAll('[name=task-type]').forEach(el=>el.onchange=showType);
    f.querySelectorAll('[name=root-mode]').forEach(el=>el.onchange=showRoot);
    $('#task-executor').onchange=showRec;
    for(const id of ['size','risk','kind'])$('#task-'+id).addEventListener('change',recalc);
    $('#task-title').addEventListener('blur',()=>{const t=$('#task-root-title');if(!t.value.trim())t.value=$('#task-title').value.trim().slice(0,200);});
    workspace();showType();
    f.onsubmit=async e=>{
      e.preventDefault();f.classList.add('tried');
      const get=id=>$('#task-'+id).value.trim(), t=typeOf(), error=$('#board-plan-error');
      const dirs=[...f.querySelectorAll('#task-directions input:checked')].map(x=>x.value);
      $('#task-type').classList.toggle('is-missing',!t&&!task);
      const dirsBox=$('#task-directions');if(dirsBox)dirsBox.classList.toggle('is-missing',!dirs.length);
      if(!f.checkValidity()||(!t&&!task)||(t==='decomposition'&&!dirs.length)){
        error.textContent='Заполните подсвеченные поля.';
        (f.querySelector(':invalid')||f.querySelector('.is-missing'))?.scrollIntoView({block:'center'});return;
      }
      const changes={title:get('title'),why:get('why'),acceptance:get('acceptance').split('\n').map(v=>v.trim()).filter(Boolean),
        priority:get('priority'),risk:get('risk'),work_type:get('kind'),due_date:get('due'),model_reason:get('model-reason'),
        executor:get('executor'),model_plan:modelPlan(),scope:get('scope').split(',').map(v=>v.trim()).filter(Boolean)};
      if(t){changes.task_type=t;
        for(const key of Object.keys(types[t].fields))changes[key]=key==='directions'?dirs:get(key);
        if(!$('#task-constraints-box').hidden&&get('constraints'))changes.constraints=get('constraints');}
      try{
        if(task){
          if(rootMode()==='new')changes.new_root={title:get('root-title')||changes.title,text:get('root-text')};
          else if(get('root')!==task.root)changes.root=get('root');
          await api({op:'update',ref:task.ref,changes});}
        else{
          const item={...changes,model_plan:Object.fromEntries(Object.entries(changes.model_plan).filter(([,v])=>v)),
            size:get('size'),depends_on:get('deps').split(',').map(v=>v.trim()).filter(Boolean)};
          if(get('id'))item.id=get('id');
          if(rootMode()==='new')item.new_root={title:get('root-title')||changes.title,text:get('root-text')};else item.root=get('root');
          const r=await api({op:'plan',project:get('project'),tasks:[item]});
          dialog.close();await refresh();
          const box=document.querySelector('#boards-root .board-message');if(box)box.textContent='Задача '+r.added.join(', ')+' создана в проекте '+data.projects[get('project')]?.name;
          return;
        }
        dialog.close();await refresh();
      }catch(err){error.textContent=err.message;}
    };
  }
  function checklist({task,dialog,api,refresh,modal}){
    const box=document.createElement('div');box.className='board-checklist';box.innerHTML=(task.acceptance||[]).map((a,i)=>`<label><input type="checkbox" data-criterion="${i}" ${(task.acceptance_done||[]).includes(i)?'checked':''}>${esc(a)}</label>`).join('');const old=dialog.querySelector('ul');if(old)old.replaceWith(box);else dialog.append(box);
    const error=document.createElement('p');error.setAttribute('role','alert');box.after(error);
    box.querySelectorAll('input').forEach(el=>el.onchange=async()=>{try{const indices=[...box.querySelectorAll('input:checked')].map(v=>Number(v.dataset.criterion));await api({op:'update',ref:task.ref,changes:{acceptance_done:indices}});task.acceptance_done=indices;await refresh();}catch(e){el.checked=!el.checked;error.textContent=e.message;}});
    if(!['done','cancelled'].includes(task.status)){const complete=document.createElement('button');complete.textContent='Завершить с проверкой';complete.id='board-complete';error.after(complete);complete.onclick=()=>{dialog.close();modal('Результат проверки','<label>Что проверено и какой результат<textarea id="board-evidence" aria-label="Результат проверки"></textarea></label><button id="board-confirm">Завершить задачу</button><p id="board-complete-error" role="alert"></p>');dialog.querySelector('#board-confirm').onclick=async()=>{try{await api({op:'complete',ref:task.ref,evidence:dialog.querySelector('textarea').value});dialog.close();await refresh();}catch(e){dialog.querySelector('#board-complete-error').textContent=e.message;}};};}
  }
  return {decorate,form,checklist};
})();
