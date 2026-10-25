/* Focused application pages, stable tasks and calendar projections. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id),bridge=window.ApplyOpsBridge,U=window.ApplyOpsState,{escape:esc,toast}=bridge;
  const api=(p,o={})=>bridge.request('/api/v3'+p,o);
  const titles={dashboard:'申请总览',projects:'项目与材料',tasks:'待办',calendar:'日历',monitor:'官网监控',inbox:'站内信箱',notifications:'提醒',chat:'画像助手'};
  const labels={considering:'考虑中',preparing:'准备中',submitted:'已提交',offered:'已录取',rejected:'未录取',withdrawn:'已撤回',draft:'草稿',active:'当前计划',superseded:'历史版本',completed:'已完成'};
  const state={view:'dashboard',snapshot:null,dashboard:null,notifications:[],school:null,project:null,date:U.localDate(),period:'month',formOpen:false,pending:new Set(),epoch:0,undo:null};
  const cmd=(type,payload={},target_id=null,expected_version=null)=>({type,payload,target_id,expected_version});
  const send=(commands,request_id=crypto.randomUUID())=>api('/commands',{method:'POST',body:JSON.stringify({request_id,commands})});
  const task=id=>state.snapshot?.tasks.find(t=>t.id===id),project=id=>state.dashboard?.projects.find(p=>p.id===id);
  const projectName=id=>{const p=project(id);return p?`${p.university} · ${p.name}`:'全局待办';};
  const empty=t=>`<div class="workspace-empty">${t}</div>`;
  bridge.resolveEntity=(kind,id)=>kind==='application'?projectName(id):kind==='task'?task(id)?.title||id:kind==='requirement'?state.snapshot?.requirements.find(r=>r.id===id)?.title||id:id;
  const channel='BroadcastChannel' in window?new BroadcastChannel('applyops-workspace'):null;
  const monitoring=window.ApplyOpsMonitorUI({api,esc,toast,projectName,getView:()=>state.view,onChanged:()=>{changed();refresh();},onForm:open=>{state.formOpen=open;}});
  function changed(){channel?.postMessage({changed:true});}
  if(channel)channel.onmessage=()=>{if(!state.formOpen&&!state.pending.size)refresh();};
  async function refresh({render=true}={}){
    if($('app').hidden||state.pending.size)return;const epoch=++state.epoch;
    try{const [snapshot,dashboard,notifications]=await Promise.all([api('/snapshot'),api('/dashboard'),api('/notifications')]);
      if(epoch!==state.epoch||state.pending.size)return;Object.assign(state,{snapshot,dashboard,notifications});$('ops-error').textContent='';monitoring.unread().catch(()=>{});if(render&&!state.formOpen)await renderView();
    }catch(e){$('ops-error').textContent=e.message;}
  }
  function clearHandlers(){const r=$('ops-content');r.onclick=r.onchange=r.onsubmit=r.oninput=null;}
  function navigate(view){
    if(state.formOpen&&view!==state.view&&!confirm('表单尚未保存，确定离开吗？'))return;
    state.view=view;state.formOpen=false;clearHandlers();$('ops-main').hidden=view==='chat';document.querySelector('.chat-panel').hidden=view!=='chat';$('ops-chat-history').hidden=view!=='chat';$('ops-title').textContent=titles[view];
    document.querySelectorAll('[data-ops-view]').forEach(b=>b.classList.toggle('active',b.dataset.opsView===view));refresh();
  }
  function inspector(tab='approvals'){if(!$('ops-inspector').open)$('ops-inspector').showModal();document.querySelector(`[data-tab="${tab}"]`)?.click();bridge.reloadApprovals?.();}
  function dashboard(root){const d=state.dashboard;
    root.innerHTML=`<p class="page-description">查看每个项目的材料准备情况，按自己的节奏推进申请。</p><div class="overview-stats"><div class="overview-stat"><span>申请项目</span><strong>${d.applications}</strong></div><button class="overview-stat" data-open-inspector="approvals"><span>待确认变更</span><strong>${d.pending_approvals}</strong></button><div class="overview-stat"><span>待完成任务</span><strong>${d.open_tasks}</strong></div></div><div class="application-card-list">${d.projects.map(p=>`<article class="application-card" data-project-card="${esc(p.id)}"><div class="application-card-heading"><div><button class="project-title" data-project-edit="${esc(p.id)}">${esc(p.university)} · ${esc(p.name)}</button><p class="project-meta">${esc(p.cycle)} · ${p.deadline?'截止日期 '+esc(p.deadline):'截止日期待确认'}</p></div><span class="status-pill">${esc(labels[p.status]||p.status)}</span></div><div class="preparation-heading"><h2>材料准备进度</h2><span>${p.progress.completed} / ${p.progress.total}${p.progress.unknown?`<small>另有 ${p.progress.unknown} 项待确认</small>`:''}</span></div><div class="preparation-track" role="progressbar" aria-label="材料准备进度" aria-valuemin="0" aria-valuemax="${p.progress.total||1}" aria-valuenow="${p.progress.completed}"><div style="width:${Math.min(p.progress.percent,p.progress.unknown?99:100)}%"></div></div><ul class="preparation-list">${p.requirements.map(r=>`<li class="${r.complete?'is-prepared':''}"><span class="preparation-check" aria-label="${r.complete?'已准备':'未完成'}">${r.complete?'✓':''}</span><span>${esc(r.title)}</span>${r.unknown?'<small>待确认要求</small>':r.required>1?`<small>已准备 ${Math.min(r.prepared,r.required)} / ${r.required}</small>`:''}</li>`).join('')||'<li class="hint">尚未记录材料要求，请到项目与材料补充。</li>'}</ul></article>`).join('')||empty('尚无申请项目，请到左侧「项目与材料」添加。')}</div>`;
  }
  function projects(root){root.innerHTML=`<div class="section-toolbar"><p class="page-description">在一张表单中维护项目、截止日期和材料要求。</p><button class="primary" id="new-project">＋ 新增项目</button></div><div class="project-register">${state.dashboard.projects.map(p=>`<button class="project-register-row" data-project-edit="${esc(p.id)}"><span><strong>${esc(p.university)} · ${esc(p.name)}</strong><small>${esc(p.cycle)}</small></span><span>${esc(p.deadline||'截止日期待确认')}</span><span aria-hidden="true">›</span></button>`).join('')||empty('添加第一个申请项目，从学校和申请周期开始。')}</div>`;}
  async function openProject(id=null){
    const data=id?await api('/projects/'+encodeURIComponent(id)):null;state.view='projects';state.formOpen=true;clearHandlers();$('ops-main').hidden=false;document.querySelector('.chat-panel').hidden=true;$('ops-chat-history').hidden=true;$('ops-title').textContent='项目与材料';
    document.querySelectorAll('[data-ops-view]').forEach(b=>b.classList.toggle('active',b.dataset.opsView==='projects'));
    window.ApplyOpsProjectForm.show($('ops-content'),{data,onCancel:()=>{state.formOpen=false;clearHandlers();renderView();},onSave:async body=>{
      await api('/projects'+(id?'/'+encodeURIComponent(id):''),{method:id?'PUT':'POST',body:JSON.stringify(body)});state.formOpen=false;toast(id?'项目已更新':'项目已添加');changed();navigate('dashboard');
    }});
  }
  function todoRow(t,done=false){return `<div class="todo-row ${done?'is-done':''}" data-todo-row="${esc(t.id)}"><button class="todo-circle" data-complete-task="${esc(t.id)}" ${done?'disabled':''} aria-label="${done?'已完成':'完成'} ${esc(t.title)}">${done?'✓':''}</button><button class="todo-body" data-edit-todo="${esc(t.id)}"><span class="todo-title">${esc(t.title)}</span><span class="todo-date ${t.due_date&&t.due_date<state.snapshot.today&&!done?'overdue':''}">${esc(t.due_date||'未设截止日期')}${t.show_in_calendar?' · 日历':''}</span></button>${done?`<button class="quiet-button" data-restore-task="${esc(t.id)}">恢复</button>`:''}</div>`;}
  function todoCard(id){const p=project(id),open=U.visibleTasks(state.snapshot.tasks,id),done=U.visibleTasks(state.snapshot.tasks,id,true);
    return `<article class="todo-project-card" data-task-card="${esc(id||'global')}"><div class="todo-project-heading"><div><h2>${esc(p?.name||'全局待办')}</h2><p class="hint">${p?esc(p.university)+' · '+esc(p.cycle):'不属于特定申请项目的任务'}</p></div><span class="task-count">${open.length} 项待办</span></div><div class="todo-items">${open.map(t=>todoRow(t)).join('')||'<p class="task-empty">暂无待办，可以从右下角添加。</p>'}</div><footer><details class="completed-tasks"><summary>已完成 ${done.length} 项</summary>${done.map(t=>todoRow(t,true)).join('')||'<p class="hint">暂无已完成任务。</p>'}</details><button class="add-todo" data-add-todo="${esc(id||'global')}" aria-label="为${esc(p?.name||'全局待办')}新增待办">＋</button></footer></article>`;
  }
  function todos(root){const dir=U.directory(state.dashboard.projects,state.snapshot.tasks);
    if(state.school===null)state.school=dir.schools[0]?.key||'global';
    if(state.school!=='global'&&!dir.schools.some(s=>s.key===state.school)){state.school='global';state.project=null;}
    const school=dir.schools.find(s=>s.key===state.school),items=school?school.projects.filter(p=>!state.project||p.id===state.project):[];
    root.innerHTML=`<p class="page-description">按学校整理申请任务。完成一件，就少一件。</p><div class="todo-workspace"><nav class="task-directory" aria-label="待办目录"><button data-school="global" class="${state.school==='global'?'selected':''}"><span>全局待办</span><small>${dir.global}</small></button><p>申请学校</p>${dir.schools.map(s=>`<div class="school-group"><button data-school="${esc(s.key)}" class="${s.key===state.school&&!state.project?'selected':''}"><span>${esc(s.name)}</span><small>${s.count}</small></button>${s.key===state.school?`<div class="school-projects">${s.projects.map(p=>`<button data-directory-project="${esc(p.id)}" class="${p.id===state.project?'selected':''}">${esc(p.name)} <small>${p.open_count}</small></button>`).join('')}</div>`:''}</div>`).join('')}</nav><section class="todo-projects"><h2 class="school-title">${esc(school?.name||'全局待办')}</h2>${school?items.map(p=>todoCard(p.id)).join(''):todoCard(null)}</section></div>`;
  }
  function openTodo(applicationId,t=null){state.editingTodo=t;state.todoApplication=applicationId;const f=$('todo-form');f.reset();f.elements.title.value=t?.title||'';f.elements.due_date.value=t?.due_date||'';f.elements.show_in_calendar.checked=t?.show_in_calendar||false;
    $('todo-heading').textContent=t?'编辑待办':'新增待办';$('todo-scope').textContent=projectName(applicationId);$('todo-error').textContent='';f.querySelector('[type=submit]').textContent=t?'保存':'添加';f.querySelector('[type=submit]').disabled=false;state.todoRequestId=crypto.randomUUID();state.todoStableKey=crypto.randomUUID();$('todo-dialog').showModal();f.elements.title.focus();}
  $('todo-form').onsubmit=async e=>{e.preventDefault();const f=e.target,t=state.editingTodo,b=f.querySelector('[type=submit]'),due=f.elements.due_date.value||null,show=f.elements.show_in_calendar.checked;
    if(show&&!due){$('todo-error').textContent='显示在日历前，请先填写截止日期。';return;}
    const payload={title:f.elements.title.value.trim(),due_date:due,show_in_calendar:show,date_certainty:due?'confirmed':'unknown'};
    if(!payload.title){$('todo-error').textContent='请填写待办内容。';return;}if(!t){payload.application_id=state.todoApplication;payload.stable_key=state.todoStableKey;}
    if(!due||t?.start_date&&t.start_date>due)payload.start_date=null;b.disabled=true;
    try{await send([cmd(t?'task.update':'task.create',payload,t?.id||null,t?.version||null)],state.todoRequestId);$('todo-dialog').close();toast(t?'待办已更新':'待办已添加');changed();await refresh();}
    catch(error){$('todo-error').textContent=error.message;b.disabled=false;}};
  async function completeTask(id){if(state.pending.has(id))return;const t=task(id);if(!t||!U.isOpen(t))return;state.epoch++;const owner=$('account-email').textContent;
    const row=document.querySelector(`[data-todo-row="${CSS.escape(id)}"]`),b=row?.querySelector('.todo-circle');state.pending.add(id);if(b){b.disabled=true;b.textContent='✓';row.classList.add('is-saving');}
    let updated;
    try{updated=(await send([cmd('task.update',{status:'completed'},id,t.version)])).changed_resources[0];}
    catch(error){try{const actual=await api('/tasks/'+id);if(actual.status==='completed')updated=actual;}catch{}
      if(!updated){state.pending.delete(id);if(b){b.disabled=false;b.textContent='';row.classList.remove('is-saving');}toast(error.message);return;}}
    if($('app').hidden||$('account-email').textContent!==owner){state.pending.delete(id);return;}
    Object.assign(t,updated);
    if(row?.isConnected){row.classList.remove('is-saving');row.classList.add('is-finishing');
      if(!matchMedia('(prefers-reduced-motion: reduce)').matches){const h=row.getBoundingClientRect().height;await row.animate([{opacity:1,height:h+'px',paddingTop:'12px',paddingBottom:'12px'},{opacity:0,height:'0px',paddingTop:'0px',paddingBottom:'0px',margin:'0px'}],{duration:480,easing:'cubic-bezier(.2,.7,.2,1)',fill:'forwards'}).finished.catch(()=>{});}row.remove();}
    state.pending.delete(id);if(!state.pending.size&&state.view==='tasks')todos($('ops-content'));clearTimeout(state.undoTimer);state.undo=updated;$('undo-toast').hidden=false;state.undoTimer=setTimeout(()=>{$('undo-toast').hidden=true;state.undo=null;},5500);changed();await refresh();
  }
  async function restoreTask(t){try{await send([cmd('task.update',{status:'planned'},t.id,t.version)]);$('undo-toast').hidden=true;state.undo=null;changed();await refresh();toast('已恢复待办及原日历设置');}catch(e){toast(e.message);await refresh();}}
  $('undo-task').onclick=()=>{if(state.undo)restoreTask(state.undo);};
  async function calendar(root){const first=new Date(state.date+'T12:00:00'),last=new Date(first);
    if(state.period==='week'){first.setDate(first.getDate()-(first.getDay()+6)%7);last.setTime(first.getTime());last.setDate(last.getDate()+6);}else{first.setDate(1);first.setDate(first.getDate()-(first.getDay()+6)%7);last.setMonth(last.getMonth()+1,0);last.setDate(last.getDate()+(7-last.getDay())%7);}
    const events=await api(`/calendar?from=${U.localDate(first)}&to=${U.localDate(last)}`);if(state.view!=='calendar')return;
    const days=[];for(let d=new Date(first);d<=last;d.setDate(d.getDate()+1)){const key=U.localDate(d);days.push(`<div class="calendar-day ${key===state.snapshot.today?'is-today':''}"><span class="day-number">${d.getDate()}</span>${events.filter(x=>(x.date||x.due_date)===key).map(x=>`<button class="calendar-entry ${esc(x.type)}" ${x.type==='task'?`data-edit-todo="${esc(x.id)}"`:''}><small>${x.type==='deadline'?'申请截止':x.type==='draft'?'计划草稿':x.type==='task'?esc(projectName(x.application_id)):'固定日程'}</small>${esc(x.title)}</button>`).join('')}</div>`);}
    root.innerHTML=`<div class="calendar-toolbar"><div><button data-calendar-shift="-1" aria-label="上一页">‹</button><strong>${esc(state.date.slice(0,7))}</strong><button data-calendar-shift="1" aria-label="下一页">›</button><button id="calendar-today">今天</button></div><div><select id="ops-period" aria-label="日历视图"><option value="month" ${state.period==='month'?'selected':''}>月视图</option><option value="week" ${state.period==='week'?'selected':''}>周视图</option></select><button id="add-calendar-event">＋ 固定日程</button></div></div><p class="hint">仅显示已勾选日历的未完成待办。完成后移除该条内容，同日其他事项保留。</p><div class="calendar-scroll"><div class="calendar-grid">${['一','二','三','四','五','六','日'].map(d=>`<div class="weekday">周${d}</div>`).join('')}${days.join('')}</div></div>`;
  }
  function notifications(root){root.innerHTML=`<div class="notification-heading"><h2>待确认变更</h2><button data-open-inspector="approvals">查看 ${state.dashboard.pending_approvals} 项</button></div><h2 class="notification-subheading">截止提醒与系统通知</h2><div class="notification-list">${state.notifications.map(n=>`<article class="notification-row ${n.read_at?'is-read':''}"><div><strong>${n.type==='plan_draft'?'有新的计划草稿':'任务日期提醒'}</strong><p>${esc(n.message)}</p><small>${esc(n.created_at?.slice(0,16).replace('T',' '))}</small></div>${!n.read_at?`<button class="quiet-button" data-read-notification="${esc(n.id)}">标记已读</button>`:'<span class="hint">已读</span>'}</article>`).join('')||empty('暂无新的提醒。')}</div>`;}
  async function renderView(){if(!state.snapshot||state.formOpen||state.view==='chat')return;const root=$('ops-content');clearHandlers();if(state.view==='dashboard')dashboard(root);else if(state.view==='projects')projects(root);else if(state.view==='tasks')todos(root);else if(state.view==='calendar')await calendar(root);else if(state.view==='monitor')await monitoring.render(root,state.dashboard.projects);else if(state.view==='inbox')await monitoring.inbox(root);else if(state.view==='notifications')notifications(root);}
  function fixedEvent(){$('ops-form-title').textContent='新增固定日程';$('ops-fields').innerHTML='<label>日程名称<input name="title" required maxlength="300"></label><label>日期<input name="date" type="date" required></label><label>类型<select name="type"><option value="appointment">预约</option><option value="exam">考试</option><option value="unavailable">不可用时间</option><option value="other">其他</option></select></label>';$('ops-form-error').textContent='';$('ops-dialog').showModal();}
  $('ops-form').onsubmit=async e=>{e.preventDefault();const f=e.target,b=f.querySelector('[type=submit]');b.disabled=true;try{await send([cmd('calendar_event.create',{title:f.elements.title.value.trim(),date:f.elements.date.value,type:f.elements.type.value,date_precision:'date'})]);$('ops-dialog').close();changed();await refresh();}catch(error){$('ops-form-error').textContent=error.message;}finally{b.disabled=false;}};
  $('ops-cancel').onclick=()=>$('ops-dialog').close();$('ops-refresh').onclick=()=>refresh();
  document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{
    if(b.dataset.opsView)navigate(b.dataset.opsView);if(b.dataset.openInspector)inspector(b.dataset.openInspector);if(b.dataset.close)$(b.dataset.close).close();
    if(b.id==='new-project')await openProject();if(b.dataset.projectEdit)await openProject(b.dataset.projectEdit);
    if(b.dataset.school){state.school=b.dataset.school;state.project=null;todos($('ops-content'));}if(b.dataset.directoryProject){state.project=b.dataset.directoryProject;todos($('ops-content'));}
    if(b.dataset.addTodo)openTodo(b.dataset.addTodo==='global'?null:b.dataset.addTodo);if(b.dataset.editTodo){const t=task(b.dataset.editTodo);openTodo(t.application_id,t);}
    if(b.dataset.completeTask)await completeTask(b.dataset.completeTask);if(b.dataset.restoreTask)await restoreTask(task(b.dataset.restoreTask));
    if(b.dataset.calendarShift){const d=new Date(state.date+'T12:00:00');if(state.period==='week')d.setDate(d.getDate()+Number(b.dataset.calendarShift)*7);else{d.setDate(1);d.setMonth(d.getMonth()+Number(b.dataset.calendarShift));}state.date=U.localDate(d);await renderView();}
    if(b.id==='calendar-today'){state.date=U.localDate();await renderView();}if(b.id==='add-calendar-event')fixedEvent();
    if(b.dataset.readNotification){await send([cmd('notification.read',{},b.dataset.readNotification)]);await refresh();}if(b.id==='new-conversation'||b.dataset.conversation)navigate('chat');
  }catch(error){toast(error.message);b.disabled=false;}});
  document.addEventListener('change',e=>{if(e.target.id==='ops-period'){state.period=e.target.value;renderView();}});
  window.addEventListener('applyops:ready',()=>{state.snapshot=null;state.school=null;state.formOpen=false;navigate('dashboard');});
  window.addEventListener('applyops:approval-decided',()=>{changed();refresh();});
  window.addEventListener('applyops:explain-notification',event=>{
    const id=event.detail?.id;if(typeof id!=='string'||!/^[a-f0-9]{32}$/i.test(id))return;
    navigate('chat');bridge.sendMessage(`请解释官网变化通知 ${id}，说明当时为什么会影响我。`);
  });
  document.addEventListener('click',event=>{const prompt=event.target.closest('[data-chat-prompt]');if(prompt)bridge.sendMessage(prompt.dataset.chatPrompt);});
  if($('chat-welcome'))new MutationObserver(()=>{$('chat-welcome').hidden=$('messages').children.length>0;}).observe($('messages'),{childList:true});
  window.addEventListener('focus',()=>{if(!state.formOpen&&!state.pending.size&&!document.querySelector('dialog[open]'))refresh();});
  $('logout').addEventListener('click',()=>{state.epoch++;state.snapshot=null;state.formOpen=false;state.pending.clear();state.undo=null;$('undo-toast').hidden=true;document.querySelectorAll('dialog[open]').forEach(d=>d.close());});
  setInterval(()=>{if(!$('app').hidden&&!state.formOpen&&!state.pending.size&&!document.querySelector('dialog[open]')&&state.view==='notifications')refresh();},10000);
  setInterval(()=>{if(!document.hidden&&!$('app').hidden){monitoring.unread().catch(()=>{});if(!state.formOpen&&['monitor','inbox'].includes(state.view))refresh();}},30000);
  if(!$('app').hidden)navigate('dashboard');
})();
