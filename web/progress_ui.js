// UI operations share one server-authoritative mutation path.
const busySessions = new Set();
let selectedTargetId = null, syncPromise = null, lastCalendarDay = new Date().toDateString();
const statusLabels = {planned:'未开始',in_progress:'进行中',completed:'已完成',cancelled:'已取消'};
const timeLabels = {history:'历史',current:'当前',upcoming:'即将开始'};
const riskLabels = {overdue:'已到期，尚未记录完成',needs_backfill:'历史待补录',schedule_conflict:'日期超出阶段边界',ahead_of_schedule:'提前完成'};

function progressControls(id, kind, status) {
  return '<div class="progress-actions" data-target="'+esc(id)+'" data-kind="'+kind+'">'+
    [['start','开始'],['complete','完成'],['postpone','延期'],['cancel','取消'],['reset','重置']].map(([action,label])=>
      '<button type="button" class="secondary" data-progress-action="'+action+'">'+label+'</button>').join('')+'</div>';
}
function progressBadge(item) {
  const risk=item.risk_status&&item.risk_status!=='none'?item.risk_status:item.boundary_conflict?'schedule_conflict':item.is_overdue?'overdue':'none';
  return '<span class="time-badge">'+esc(timeLabels[item.time_status]||'阶段内')+'</span><span class="progress-badge '+esc(item.execution_status||'planned')+'">'+esc(statusLabels[item.execution_status]||'未开始')+'</span>'+
    (riskLabels[risk]?'<span class="risk-badge '+esc(risk)+'">'+esc(riskLabels[risk])+'</span>':'');
}
function taskProgressCard(task) {
  return '<div class="card progress-card" data-select-target="'+esc(task.progress_key)+'"><strong>'+esc(task.title)+'</strong>'+
    '<div>'+progressBadge(task)+'</div><div>'+esc(task.reason)+'</div>'+
    '<div class="meta">截止：'+esc(task.due_date||'阶段内')+(task.actual_date?' · 实际完成：'+esc(task.actual_date):'')+' · 规划来源：'+esc(task.source)+(task.progress_evidence?' · 状态置信度 '+Math.round(Number(task.progress_confidence||0)*100)+'%':'')+'</div>'+
    (task.progress_evidence?'<details><summary>变更依据</summary>'+esc(task.progress_evidence)+'</details>':'')+
    progressControls(task.progress_key,'task',task.execution_status)+'</div>';
}
function eventProgressCards(data, phaseId) {
  return (data.roadmap?.timeline?.events||[]).filter(e=>e.phase_id===phaseId&&!['winter_break','summer_break'].includes(e.kind)).map(e=>
    '<div class="card progress-card" data-select-target="'+esc(e.progress_key)+'"><strong>'+esc(e.title)+'</strong><div>'+progressBadge(e)+'</div>'+
    '<div class="meta">'+esc(e.event_date)+' · '+esc(timeLabels[e.time_status]||'')+(e.actual_date?' · 实际完成：'+esc(e.actual_date):'')+(e.progress_evidence?' · 状态置信度 '+Math.round(Number(e.progress_confidence||0)*100)+'%':'')+'</div>'+
    (e.progress_evidence?'<details><summary>变更依据</summary>'+esc(e.progress_evidence)+'</details>':'')+
    progressControls(e.progress_key,'event',e.execution_status)+'</div>').join('');
}
function availableTargets(data) {
  const timeline=data.roadmap?.timeline;
  return [...(timeline?.phases||[]).flatMap(p=>(p.plan?.tasks||[]).map(t=>({id:t.progress_key,title:t.title}))),
    ...(timeline?.events||[]).map(e=>({id:e.progress_key,title:e.title}))];
}
function confirmationsMarkup(data) {
  const options=availableTargets(data);
  return (data.pending_confirmations||[]).filter(c=>c.status==='pending').map(c=>{
    const choices=options.filter(t=>(c.candidate_target_ids||[]).includes(t.id));
    return '<div class="card confirmation-card" data-confirmation="'+esc(c.confirmation_id)+'"><strong>需要确认</strong><p>'+esc(c.question)+'</p>'+
      (c.progress_update?'<select aria-label="选择要更新的任务" class="confirmation-target"><option value="">请选择任务或事件</option>'+choices.map(t=>
        '<option value="'+esc(t.id)+'" '+(choices.length===1?'selected':'')+'>'+esc(t.title)+'</option>').join('')+'</select>':'')+
      (c.progress_update?.action==='postpone'?'<input type="date" class="confirmation-date" aria-label="延期日期" value="'+esc(c.progress_update.postponed_to||'')+'">':'')+
      '<div class="progress-actions"><button type="button" data-confirm="true">确认</button><button type="button" class="secondary" data-confirm="false">拒绝</button></div></div>';
  }).join('');
}
function bindProgressControls(data) {
  const root=document.querySelector('#roadmap');
  root.querySelectorAll('[data-select-target]').forEach(card=>card.addEventListener('click',()=>{selectedTargetId=card.dataset.selectTarget}));
  root.querySelectorAll('[data-progress-action]').forEach(button=>button.addEventListener('click',async event=>{
    event.stopPropagation();
    const controls=button.closest('[data-target]'), action=button.dataset.progressAction, item=current();
    selectedTargetId=controls.dataset.target;
    const fields={target_id:controls.dataset.target,target_kind:controls.dataset.kind,action};
    fields.evidence='任务操作：'+button.textContent+' — '+button.closest('.progress-card').querySelector('strong').textContent;
    if(action==='postpone'){
      const value=window.prompt('延期到哪一天？请输入 YYYY-MM-DD');
      if(value===null)return;
      if(!/^\d{4}-\d{2}-\d{2}$/.test(value)){window.alert('请输入 YYYY-MM-DD 格式的完整日期');return}
      fields.postponed_to=value;
      fields.evidence+='，延期至 '+value;
    }
    await mutateConversation(item,'/api/progress',fields);
  }));
  root.querySelectorAll('[data-confirm]').forEach(button=>button.addEventListener('click',async()=>{
    const card=button.closest('[data-confirmation]'),accept=button.dataset.confirm==='true';
    const fields={accept};
    if(accept){
      const target=card.querySelector('.confirmation-target'),when=card.querySelector('.confirmation-date');
      if(target&&!target.value){window.alert('请选择要更新的任务或事件');return}
      if(when&&!when.value){window.alert('请填写延期日期');return}
      if(target)fields.target_id=target.value;
      if(when)fields.postponed_to=when.value;
    }
    await mutateConversation(current(),'/api/confirmations/'+encodeURIComponent(card.dataset.confirmation),fields);
  }));
  root.querySelectorAll('button').forEach(b=>{if(busySessions.has(activeId))b.disabled=true});
}
function clientSnapshot(item) {
  return item.lastData?{...item.lastData,recent_messages:item.lastData.recent_messages}:null;
}
async function jsonRequest(url, options) {
  const response=await fetch(url,{cache:'no-store',...options});
  const data=await response.json();
  if(!response.ok){const error=Error(data.error||'请求失败');error.status=response.status;throw error}
  return {data,status:response.status};
}
async function reloadConversation(id) {
  try{
    const {data:record}=await jsonRequest('/api/conversations/'+encodeURIComponent(id));
    const item=conversations.find(c=>c.id===id);
    if(item)applyServerData(item,{...record.state,conversation_title:record.title,state_revision:record.revision});
  }catch(error){
    if([404,410].includes(error.status)){conversations=conversations.filter(c=>c.id!==id);if(activeId===id)activeId=conversations[0]?.id||null}
    else throw error;
  }
}
async function mutateConversation(item,path,fields={}) {
  if(busySessions.has(item.id))return null;
  item.lastMutationError='';
  busySessions.add(item.id);
  if(activeId===item.id){
    document.querySelector('#diagnostics').textContent='';
    document.querySelector('#plan-mode').textContent=path.includes('official/research')?'官网：查询中…':path.includes('roadmap')?'规划：AI 生成中':'进度：保存中';
    document.querySelectorAll('#roadmap button').forEach(b=>b.disabled=true);
    send.disabled=true;showLoading();
  }
  try{
    await waitForConversationCreation(item.id);
    const request_id=crypto.randomUUID();
    const {data,status}=await jsonRequest(path,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:item.id,request_id,conversation_title:item.title,...fields})});
    if(status===202)throw Error('该请求仍在处理，请稍后刷新查看结果');
    // A conversation may have been deleted while a response was in flight.
    const local=conversations.find(c=>c.id===item.id);
    if(local)applyServerData(local,data);
    saveConversations();
    if(activeId===item.id){renderMessages(current(),path==='/api/chat'?'bottom':'restore');render(data)}
    return data;
  }catch(error){
    // Keep the server's validation message for the form that initiated this
    // request.  Returning only null used to make onboarding replace it with a
    // generic "check your input" message.
    item.lastMutationError=friendlyDiagnostic(error.message||'连接服务失败');
    await reloadConversation(item.id).catch(()=>{});
    saveConversations();
    if(activeId===item.id){renderMessages(current());if(current().lastData)render(current().lastData);
      document.querySelector('#diagnostics').textContent='本次操作未完成：'+friendlyDiagnostic(error.message)}
    return null;
  }finally{
    busySessions.delete(item.id);
    if(activeId===item.id){
      hideLoading();send.disabled=false;
      if(current().lastData)bindProgressEnabled();
    }
  }
}
function bindProgressEnabled(){
  document.querySelectorAll('#roadmap button').forEach(b=>b.disabled=false);
  const button=document.querySelector('#manual-replan');
  if(button&&current().lastData?.planning_pending)button.disabled=true;
}
async function requestRoadmapEnrichment(item){if(item.lastData?.planning_pending)await mutateConversation(item,'/api/roadmap/enrich')}
async function requestManualReplan(item){await mutateConversation(item,'/api/roadmap/replan')}
async function requestOfficialResearch(item){
  // Older still-running servers do not have this field. Let the request reach
  // them so a real API error is shown, instead of silently treating `undefined`
  // as disabled while static files have already refreshed in the browser.
  if(item.lastData?.official_search_configured===false){
    document.querySelector('#diagnostics').textContent='官网搜索未配置：请在项目 .env 设置 TAVILY_API_KEY 和 OFFICIAL_SEARCH_ENABLED=1，然后重启服务。';
    return null;
  }
  const refreshId=crypto.randomUUID();
  const result=await mutateConversation(item,'/api/official/research',{refresh_id:refreshId});
  // Static files are served from disk, while the Python handler remains in
  // memory. Without this acknowledgement an old handler can make the button
  // appear to work while returning an earlier/stale research snapshot.
  if(result&&result.official_research?.refresh_id!==refreshId){
    document.querySelector('#diagnostics').textContent='官网刷新未被当前后端确认：浏览器已加载新页面，但 8766 服务仍是旧代码。请停止该服务后重新执行 python -m opportunity_agent.web_app。';
    return null;
  }
  return result;
}
async function retryAgentMessage(item,eventId){await mutateConversation(item,'/api/a2a/retry',{event_id:eventId})}
function closeOnboarding(){onboardingOverlay.classList.remove('visible');onboardingError.textContent=''}

async function serverRecords(){return (await jsonRequest('/api/conversations')).data.conversations||[]}
function serverItem(record,previous=null){
  const data={...record.state,conversation_title:record.title,state_revision:record.revision};
  return {id:record.session_id,title:record.title,messages:serverMessages(data),lastData:data,
    onboardingCompleted:Boolean(data.profile?.onboarding_completed),serverManaged:true,
    updatedAt:Date.parse(record.updated_at)||Date.now(),scrollTop:Number(previous?.scrollTop)||0};
}
async function syncServerConversations(){
  if(syncPromise)return syncPromise;
  if(busySessions.size||pendingCreations.size)return;
  syncPromise=(async()=>{
    let records=await serverRecords(),known=new Set(records.map(r=>r.session_id));
    // Only legacy browser records are import candidates; server records missing
    // from the list are deletions, never requests to recreate them.
    for(const item of conversations.filter(c=>!c.serverManaged&&!known.has(c.id))){
      try{await jsonRequest('/api/conversations/import',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({session_id:item.id,conversation_title:item.title,client_state:clientSnapshot(item),
          messages:(item.messages||[]).map(m=>({role:m.who==='agent'?'assistant':'user',content:m.text}))})})}
      catch(error){if(error.status!==410)throw error}
    }
    records=await serverRecords();
    const priorById=new Map(conversations.map(item=>[item.id,item]));
    const loaded=await Promise.all(records.map(async record=>{
      try{return serverItem((await jsonRequest('/api/conversations/'+encodeURIComponent(record.session_id))).data,priorById.get(record.session_id))}
      catch(error){if([404,410].includes(error.status))return null;throw error}
    }));
    conversations=loaded.filter(Boolean);saveConversations();
  })();
  try{await syncPromise}finally{syncPromise=null}
}
async function bootstrapConversations(){
  try{await syncServerConversations()}catch(error){console.warn(error);document.querySelector('#diagnostics').textContent='服务端同步失败，暂时显示本地缓存'}
  if(!conversations.some(c=>c.id===activeId))activeId=conversations[0]?.id||null;
  if(!activeId)createConversation();
  openConversation(activeId);
}
async function refreshConversationsOnFocus(){
  if(busySessions.size||pendingCreations.size||onboardingOverlay.classList.contains('visible'))return;
  const id=activeId;
  try{
    await syncServerConversations();
    activeId=conversations.some(c=>c.id===id)?id:conversations[0]?.id||null;
    if(activeId)openConversation(activeId,false);
    else{messages.innerHTML='';resetDetails();renderConversationList()}
  }catch(error){console.warn('会话同步失败：',error)}
}
function setupConversationUI(){
  let scrollPersistTimer;
  messages.addEventListener('scroll',()=>{
    const item=conversations.find(entry=>entry.id===activeId);
    if(!item)return;
    item.scrollTop=messages.scrollTop;
    clearTimeout(scrollPersistTimer);
    scrollPersistTimer=setTimeout(persistConversationPositions,180);
  });
  timelineUpdateForm.addEventListener('submit',async event=>{
    event.preventDefault();const item=current(),detail=String(timelineUpdateForm.elements.detail.value||'').trim(),button=document.querySelector('#submit-timeline-update');
    if(!timelineUpdateNode||!detail)return;
    button.disabled=true;timelineUpdateError.textContent='正在写入画像…';
    const data=await mutateConversation(item,'/api/timeline-update',{node_title:timelineUpdateNode.title,node_date:timelineUpdateNode.date||null,fact_field:String(timelineUpdateForm.elements.fact_field.value),detail,occurred_on:String(timelineUpdateForm.elements.occurred_on.value||'')||null});
    button.disabled=false;
    if(!data){timelineUpdateError.textContent='补录未保存，请稍后重试';return}
    closeTimelineUpdate();
    if(data.replan_required&&window.confirm('资料已加入画像。是否现在重新生成规划文章？'))await requestManualReplan(item);
  });
  document.querySelector('#cancel-timeline-update').addEventListener('click',closeTimelineUpdate);
  document.querySelector('#close-timeline-update').addEventListener('click',closeTimelineUpdate);
  onboardingForm.addEventListener('submit',async event=>{
    event.preventDefault();const item=current(),button=document.querySelector('#submit-onboarding');
    button.disabled=true;onboardingError.textContent='正在保存资料…';
    const data=await mutateConversation(item,'/api/onboarding',{profile:onboardingPayload()});
    button.disabled=false;
    if(data){closeOnboarding();if(activeId===item.id){selectedPhaseId=null;render(data)}requestRoadmapEnrichment(item)}
    else onboardingError.textContent='提交失败：'+(item.lastMutationError||'请检查输入或连接后重试');
  });
  document.querySelector('#edit-profile').addEventListener('click',()=>openOnboarding(current().lastData?.profile||{}));
  document.querySelector('#cancel-onboarding').addEventListener('click',closeOnboarding);
  document.querySelector('#close-onboarding').addEventListener('click',closeOnboarding);
  form.addEventListener('submit',async event=>{
    event.preventDefault();const text=input.value.trim(),item=current();if(!text||busySessions.has(item.id))return;
    input.value='';add(text,'user',false);
    const result=await mutateConversation(item,'/api/chat',{message:text,selected_target_id:selectedTargetId});
    if(!result)input.value=text;
    input.focus();
  });
  document.querySelector('#new-chat').addEventListener('click',()=>{createConversation();openConversation(activeId);input.focus()});
  window.addEventListener('focus',refreshConversationsOnFocus);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshConversationsOnFocus()});
  setInterval(()=>{const today=new Date().toDateString();if(today!==lastCalendarDay){lastCalendarDay=today;refreshConversationsOnFocus()}},60000);
  bootstrapConversations();
}
