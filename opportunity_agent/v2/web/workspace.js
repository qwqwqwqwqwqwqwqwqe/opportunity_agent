/* Explicit user actions use deterministic APIs; model proposals retain the chat approval flow. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const state={tab:'chat',projects:[],applications:[],events:[],jobs:[],month:new Date(),background:null,editing:null};
  const panel=$('workspace-panel'),dialog=$('workspace-dialog');
  const command=(version=1)=>({request_id:crypto.randomUUID(),expected_version:version});
  async function api(path,method='GET',body){
    const r=await fetch('/api/v1'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
    const value=await r.json();if(!r.ok)throw new Error(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail));return value;
  }
  function fail(error){const e=document.createElement('p');e.className='error';e.textContent=error.message;panel.prepend(e);}
  function safeLink(url,label){try{const u=new URL(url);if(u.protocol==='https:'&&!u.username&&!u.password)return `<a href="${esc(u.href)}" target="_blank" rel="noopener noreferrer">${esc(label||url)}</a>`;}catch(_){}return esc(label||url);}
  const labels={university:'学校',program:'项目全称',intake:'申请季',degree:'学位',degree_award:'授予类型',country:'国家',campus:'校区',home_unit:'Home Unit',
    summary:'项目简介',objectives:'培养目标',background:'适合背景',study_type:'研究／授课性质',duration:'时长',credits:'总学分',graduation:'毕业／论文要求',prerequisites:'先修课',gpa:'GPA要求',fees:'费用',currency:'币种'};
  const lists={tracks:{title:'Tracks（未声明完整，不代表全部方向）',fields:{name:'名称',description:'方向说明',requirements:'单独要求'}},
    courses:{title:'课程（未声明完整，不代表全部课程）',fields:{name:'名称',group:'核心／选修／分组',credits:'学分'}},
    course_rules:{title:'选课规则（与单门课程分开）',fields:{group:'所属分组',requirement:'规则原文／说明',course_codes:'关联课程代码（逗号分隔）',choose:'需选课程数（可空）',credits:'分组学分要求（可空）'}},
    language:{title:'语言要求',fields:{exam:'考试',version:'版本／分制',total:'总分',sections:'单项',conditions:'适用条件'}},
    materials:{title:'材料要求',fields:{name:'材料名称',category:'类别（sop/cv/recommendation等）',requirement:'详细要求',prompt:'文书题目',limit:'限制数量',unit:'words / characters / pages / none',count:'份数'}},
    deadlines:{title:'截止日期（年份未知时日期留空）',fields:{label:'轮次／名称',raw_date:'原始日期',date:'完整日期 YYYY-MM-DD',time:'时间 HH:MM（可空）',timezone:'IANA时区（可空）',applicants:'适用申请者'}}};
  function input(name,label,value,type='text'){return `<label>${esc(label)}<input name="${esc(name)}" type="${type}" value="${esc(value)}"></label>`;}
  function listRow(key,value={}){return `<fieldset data-list-row="${key}">${Object.entries(lists[key].fields).map(([name,label])=>input(name,label,value[name]??'')).join('')}<button type="button" data-remove-row>删除此项</button></fieldset>`;}
  let importWatch=0;
  function close(){importWatch++;dialog.close();dialog.innerHTML='';}
  function open(html){dialog.innerHTML=html;dialog.showModal();dialog.querySelector('[data-close]').onclick=close;}
  async function show(tab){
    if($('app').hidden)return;
    state.tab=tab;$('chat-panel').hidden=tab!=='chat';panel.hidden=tab==='chat';
    document.querySelectorAll('[data-workspace]').forEach(b=>{b.classList.toggle('active',b.dataset.workspace===tab);if(b.dataset.workspace===tab)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
    if(tab==='chat')return;
    panel.innerHTML='<p>正在加载…</p>';
    try{
      if(tab==='library')await library();if(tab==='calendar')await calendar();if(tab==='todo')await todos();if(tab==='background')await background();
      panel.insertAdjacentHTML('afterbegin','<button class="mobile-panel" data-workspace-menu>☰ 导航</button>');
      panel.querySelector('[data-workspace-menu]').onclick=()=>document.querySelector('.left-panel').classList.toggle('open');
    }catch(e){panel.innerHTML='';fail(e);}
  }
  async function library(){
    [state.projects,state.jobs]=await Promise.all([api('/project-library'),api('/project-imports')]);
    panel.innerHTML='<h1>我的项目库</h1><p>网址读取和模型整理只产生草稿，用户确认不等于官方独立核验。</p><div class="workspace-actions"><button data-new>＋ 新项目／导入网址</button><button data-batch>批量导入（最多10个项目）</button><button data-recommend>根据全部经历推荐</button><button data-refresh>刷新</button></div><div id="workspace-recommendations"></div>'+
      state.projects.filter(p=>!p.archived).map(p=>`<article class="workspace-card"><h2>${esc(p.draft.university)} · ${esc(p.draft.program)}</h2><p>${esc(p.draft.intake)} · ${p.published_version?'已发布版本 '+p.published_version:'尚未确认'} · 索引 ${esc(p.index_status)}</p>`+
        state.jobs.filter(j=>j.project_id===p.id).slice(0,1).map(j=>`<p>导入阶段：${esc(({queued:'等待处理',reading:'读取网页',extracting:'整理字段',awaiting_confirmation:'等待确认',superseded:'已有更新，未覆盖',failed:'失败，可手填'})[j.status]||j.status)} ${esc(j.error_code)}</p>`).join('')+
        `<div class="workspace-actions"><button data-edit="${p.id}">编辑／确认信息</button><button data-select="${p.id}" ${p.published_version?'':'disabled'}>加入申请</button><button data-reindex="${p.id}">重新索引</button><button data-archive="${p.id}">归档</button></div></article>`).join('');
    panel.querySelector('[data-new]').onclick=importEditor;panel.querySelector('[data-refresh]').onclick=()=>show('library');
    panel.querySelector('[data-batch]').onclick=batchEditor;panel.querySelector('[data-recommend]').onclick=recommend;
  }
  function importEditor(){
    open('<form id="workspace-url-import"><h2>第一步：提供项目网址</h2><p>同一个项目可添加最多5个页面。系统先读取网页并自动填写草稿，再由你修改确认，无需先填写项目表。</p><div id="workspace-source-inputs">'+sourceRow()+'</div><button type="button" data-add-source>添加网址</button><p data-error class="error"></p><button type="button" data-close>取消</button><button type="button" data-manual>改为手动填写</button><button type="submit">提取并自动填表</button></form>');
    dialog.querySelector('[data-add-source]').onclick=()=>{const list=$('workspace-source-inputs');if(list.children.length<5)list.insertAdjacentHTML('beforeend',sourceRow());};
    dialog.querySelector('[data-manual]').onclick=()=>{close();editProject();};
    dialog.onclick=e=>{if(e.target.matches('[data-remove-row]'))e.target.closest('fieldset').remove();};
    $('workspace-url-import').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('[type="submit"]');button.disabled=true;
      try{const sources=[...dialog.querySelectorAll('[data-source]')].map(row=>({kind:row.querySelector('select').value,url:row.querySelector('input').value.trim(),text:row.querySelector('textarea').value.trim()})).filter(s=>s.url||s.text);
        if(!sources.length)throw Error('请至少提供一个项目网址或粘贴正文');
        const result=await api('/project-imports','POST',{...command(),projects:[{data:{},sources}]});
        close();await show('library');watchImport(result.projects[0].id);
      }catch(error){dialog.querySelector('[data-error]').textContent=error.message;button.disabled=false;}
    };
  }
  function watchImport(id){
    open('<h2>第二步：自动提取项目信息</h2><p data-import-status role="status">等待处理…</p><p>任务在后台继续，关闭窗口后可在项目库查看进度和编辑草稿。</p><button type="button" data-close>关闭</button><button type="button" data-edit-now>停止等待，手动编辑</button>');
    const token=++importWatch;
    dialog.querySelector('[data-edit-now]').onclick=()=>{close();editProject(id);};
    async function poll(){
      try{const jobs=await api('/project-imports');if(token!==importWatch||!dialog.open||$('app').hidden)return;
        const job=jobs.find(j=>j.project_id===id);
        if(job&&['awaiting_confirmation','failed','superseded'].includes(job.status)){close();await show('library');await editProject(id);return;}
        dialog.querySelector('[data-import-status]').textContent=({queued:'等待处理',reading:'正在读取网页',extracting:'正在整理字段'})[job?.status]||'等待处理';
      }catch(error){if(token!==importWatch)return;dialog.querySelector('[data-import-status]').textContent='进度暂时无法读取：'+error.message;}
      if(token===importWatch)setTimeout(poll,2000);
    }
    poll();
  }
  async function editProject(id){
    const p=id?await api('/project-library/'+id):null,d=p?.draft||{university:'',program:'',intake:''};state.editing=p;
    open(`<form id="workspace-project-form"><h2>${p?'编辑项目草稿':'新建项目／导入'}</h2><div class="workspace-fields">${Object.entries(labels).map(([k,l])=>input(k,l,d[k]||'')).join('')}</div>`+
      `<label>别名（逗号分隔，仅记录你确认的别名）<input name="aliases" value="${esc((d.aliases||[]).join(','))}"></label><label>GRE<select name="gre">${['unknown','required','optional','not_required','not_accepted'].map(x=>`<option ${d.gre===x?'selected':''}>${x}</option>`).join('')}</select></label>`+
      Object.entries(lists).map(([k,v])=>`<section><h3>${v.title}</h3><div data-list="${k}">${(d[k]||[]).map(row=>listRow(k,row)).join('')}</div><button type="button" data-add-row="${k}">添加一项</button></section>`).join('')+
      (p?'':`<h3>来源（最多5个，只读取这些网址，不爬站）</h3><div id="workspace-source-inputs">${sourceRow()}</div><button type="button" data-add-source>添加来源</button>`)+
      `<h3>警告与提取依据</h3><p>来源识别出的项目身份：${esc(d.observed_identity||'尚未识别；请对照正文核对学校、项目、学位和适用范围')}</p><ul>${(d.warnings||[]).map(w=>`<li>${esc(w)}</li>`).join('')}</ul><details><summary>字段引用（供人工核对）</summary><pre>${esc(JSON.stringify(d.provenance||{},null,2))}</pre></details>`+
      (p?`<h3>来源快照</h3>${p.sources.filter(s=>s.version===p.version).map(s=>`<details><summary>${safeLink(s.url,s.title||s.url||'粘贴正文')} · ${esc(s.status)} ${esc(s.error_code)}</summary><pre>${esc(s.content)}</pre><textarea data-source-paste="${s.id}" rows="4" placeholder="读取失败可粘贴正文；保存会创建新草稿，不覆盖已发布版本"></textarea><button type="button" data-source-save="${s.id}">保存替换正文</button></details>`).join('')}<button type="button" data-extract>重新整理当前来源</button>`:'')+
      `<p class="error" data-error></p><div class="workspace-actions"><button type="button" data-close>关闭</button><button type="submit">保存草稿</button>${p?'<button type="button" data-confirm>审阅并发布</button>':'<button type="button" data-import>保存并自动填写</button>'}</div></form>`);
    dialog.querySelectorAll('[data-add-row]').forEach(b=>b.onclick=()=>dialog.querySelector(`[data-list="${b.dataset.addRow}"]`).insertAdjacentHTML('beforeend',listRow(b.dataset.addRow)));
    dialog.onclick=e=>{if(e.target.matches('[data-remove-row]'))e.target.closest('fieldset').remove();};
    if(!p){dialog.querySelector('[data-add-source]').onclick=()=>{const list=$('workspace-source-inputs');if(list.children.length<5)list.insertAdjacentHTML('beforeend',sourceRow());};}
    function collect(){
      const f=$('workspace-project-form'),data={...d};for(const k of Object.keys(labels))data[k]=f.elements[k].value;
      data.aliases=f.elements.aliases.value.split(/[,，]/).map(s=>s.trim()).filter(Boolean);data.gre=f.elements.gre.value;
      for(const key of Object.keys(lists))data[key]=[...f.querySelectorAll(`[data-list="${key}"] fieldset`)].map(row=>{
        const item={};row.querySelectorAll('input').forEach(el=>{item[el.name]=el.value;});
        if(key==='materials'){for(const k of ['limit','count'])item[k]=item[k]===''?null:Number(item[k]);item.unit=item.unit||'none';}
        if(key==='course_rules'){item.course_codes=item.course_codes.split(/[,，]/).map(x=>x.trim()).filter(Boolean);item.choose=item.choose===''?null:Number(item.choose);}
        if(key==='deadlines')item.date=item.date||null;return item;
      });return data;
    }
    async function save(importing=false){
      const data=collect();let out;
      if(p)out=await api('/project-library/'+p.id,'PATCH',{...command(p.version),data});
      else{const sources=[...dialog.querySelectorAll('[data-source]')].map(row=>({kind:row.querySelector('select').value,url:row.querySelector('input').value,text:row.querySelector('textarea').value})).filter(s=>s.url||s.text);
        out=importing?await api('/project-imports','POST',{...command(),projects:[{data,sources}]}):await api('/project-library','POST',{...command(),data,sources});}
      return out;
    }
    async function guarded(action){try{await action();}catch(e){dialog.querySelector('[data-error]').textContent=e.message;}}
    $('workspace-project-form').onsubmit=e=>{e.preventDefault();guarded(async()=>{const saved=await save();close();await show('library');await editProject(saved.id);});};
    if(p){dialog.querySelector('[data-confirm]').onclick=()=>guarded(async()=>{const saved=await save();close();await confirmEditor(saved.id);});
      dialog.querySelectorAll('[data-source-save]').forEach(b=>b.onclick=()=>guarded(async()=>{const source=p.sources.find(s=>s.id===b.dataset.sourceSave);const text=dialog.querySelector(`[data-source-paste="${source.id}"]`).value;if(!text.trim())throw Error('请先粘贴正文');await api('/project-library/'+p.id+'/sources/'+source.id,'PUT',{...command(p.version),source:{url:source.url,kind:source.kind,text}});close();await editProject(p.id);}));
      dialog.querySelector('[data-extract]').onclick=()=>guarded(async()=>{await api('/project-library/'+p.id+'/reextract','POST',command(p.version));close();await show('library');});
    }else dialog.querySelector('[data-import]').onclick=()=>guarded(async()=>{await save(true);close();await show('library');});
  }
  function sourceRow(){return '<fieldset data-source><label>页面类型<select><option value="overview">项目介绍</option><option value="curriculum">课程</option><option value="requirements">申请要求</option></select></label><label>HTTPS网址<input type="url"></label><label>粘贴正文（网址失败时可用）<textarea rows="3"></textarea></label><button type="button" data-remove-row>删除来源</button></fieldset>';}
  async function confirmEditor(id){
    const p=await api('/project-library/'+id);
    open(`<form id="workspace-confirm"><h2>发布项目资料</h2><p>仅代表你审阅确认，不代表系统已独立验证官网。未勾选项保持待确认。</p>`+
      ['deadlines','timezone','language','gre','materials'].map(k=>`<label><input type="checkbox" name="critical" value="${k}">我已核对 ${esc(({deadlines:'截止日期与适用年度',timezone:'截止时间及时区',language:'语言要求及分制',gre:'GRE政策',materials:'材料要求及字数单位'})[k])}</label>`).join('')+
      '<h3>允许检索的来源正文</h3>'+p.sources.filter(s=>s.version===p.version&&s.content).map(s=>`<label><input type="checkbox" name="source" value="${s.id}">${esc(s.title||s.url||'粘贴正文')}</label>`).join('')+
      '<label><input type="checkbox" name="reviewed" required>我已审阅整张项目信息表，理解未确认项不用于确定结论</label><p class="error" data-error></p><button type="button" data-close>取消</button><button>确认发布</button></form>');
    $('workspace-confirm').onsubmit=async e=>{e.preventDefault();try{await api('/project-library/'+id+'/confirm','POST',{...command(p.version),confirmations:[...dialog.querySelectorAll('[name="critical"]:checked')].map(x=>x.value),approved_sources:[...dialog.querySelectorAll('[name="source"]:checked')].map(x=>x.value)});close();await show('library');}catch(error){dialog.querySelector('[data-error]').textContent=error.message;}};
  }
  function batchEditor(){
    open('<form id="workspace-batch"><h2>批量网址导入</h2><p>每行一个项目，只需网址；同一项目多个网址用 | 分隔（最多5个）。最多10行。自动填表后逐项审阅。</p><textarea name="rows" rows="10" required placeholder="https://example.edu/program | https://example.edu/admissions"></textarea><p data-error class="error"></p><button type="button" data-close>取消</button><button>开始导入</button></form>');
    $('workspace-batch').onsubmit=async e=>{e.preventDefault();try{const projects=e.target.elements.rows.value.split('\n').filter(s=>s.trim()).map(line=>{
      const urls=line.split('|').map(s=>s.trim());return {data:{},sources:urls.filter(Boolean).map((url,i)=>({url,kind:['overview','curriculum','requirements'][i]||'overview'}))};});
      await api('/project-imports','POST',{...command(),projects});close();await show('library');}catch(error){dialog.querySelector('[data-error]').textContent=error.message;}};
  }
  async function recommend(){
    const target=$('workspace-recommendations');target.textContent='正在综合背景与全部经历匹配…';
    try{const r=await api('/recommendations','POST',{query:''});target.innerHTML=`<p>画像版本 ${r.profile_version} · ${esc(r.retrieval_mode)} · ${esc(r.warning)}</p>`+r.results.map(p=>`<article class="workspace-card"><h3>${esc(p.university)} · ${esc(p.program)}</h3><p>相对排序，不是录取概率</p><ul>${p.evidence.map(e=>`<li>经历 ${esc(e.experience_id)} → ${esc(e.excerpt)}</li>`).join('')}</ul><button data-select="${p.project_id}">加入申请</button></article>`).join('');}catch(e){target.textContent=e.message;}
  }
  async function todos(){
    state.applications=await api('/workspace/applications');
    panel.innerHTML='<h1>申请 To-do</h1><p>打勾表示准备完成，不等于学校已收到。提交请单独标记。</p>'+state.applications.map(t=>{
      const d=t.snapshot,done=t.tasks.filter(x=>['completed','submitted','not_applicable'].includes(x.status)).length;
      return `<article class="workspace-card"><h2>${esc(d.university)} · ${esc(d.program)}</h2><p>${done}/${t.tasks.length} · 项目版本 ${t.project_version}</p>`+
        (t.update_available?`<p>项目资料有新版本，现有任务未被覆盖。<button data-sync="${t.application_id}">同步要求快照</button></p>`:'')+
        `<button data-generate="${t.application_id}">生成清单草稿</button>`+(t.checklist_draft?.length?`<details><summary>审阅清单草稿</summary><ul>${t.checklist_draft.map(x=>`<li>${esc(x.title)}：${esc(x.requirement)} ${esc(x.suggested_date||'')} ${esc(x.date_note||'')}</li>`).join('')}</ul><button data-edit-checklist="${t.application_id}">编辑清单草稿</button><button data-checklist="${t.application_id}">确认清单（保留现有进度）</button></details>`:'')+
        t.tasks.map(task=>`<div class="workspace-task"><label><input type="checkbox" data-task-check="${task.id}" ${['completed','submitted','not_applicable'].includes(task.status)?'checked':''}>${esc(task.title)}</label><p>${esc(task.details.requirement)}</p><select data-task-status="${task.id}">${Object.entries({planned:'未开始',in_progress:'进行中',completed:'准备完成',submitted:'已提交',not_applicable:'不适用'}).map(([v,l])=>`<option value="${v}" ${task.status===v?'selected':''}>${l}</option>`).join('')}</select><button data-task-event="${task.id}">安排时间／提醒</button>`+
          (task.details.category==='recommendation'?`<label>已邀请 <input type="number" data-count="invited" data-task="${task.id}" min="0" max="${task.details.required_count}" value="${task.details.invited||0}"></label><label>已收到 <input type="number" data-count="received" data-task="${task.id}" min="0" max="${task.details.required_count}" value="${task.details.received||0}"></label>`:'')+'</div>').join('')+'</article>';
    }).join('');
  }
  async function calendar(){
    const [r,notifications]=await Promise.all([api('/calendar-events'),api('/notifications')]);state.events=r.events;
    const year=state.month.getFullYear(),month=state.month.getMonth(),key=`${year}-${String(month+1).padStart(2,'0')}`;
    const days=new Date(year,month+1,0).getDate(),offset=new Date(year,month,1).getDay();
    panel.innerHTML=`<h1>日程与提醒</h1><p>${r.email_available?'邮件已配置；请在事件中主动开启。':'邮件未配置：目前只有站内提醒，关闭网页不会弹出通知。'}</p><div class="workspace-actions"><button data-month="-1">上月</button><strong>${key}</strong><button data-month="1">下月</button><button data-event-new>＋ 考试／签证／自定义事项</button><button data-refresh-calendar>刷新通知</button></div>`+
      `<div class="calendar-grid">${['日','一','二','三','四','五','六'].map(x=>`<strong>${x}</strong>`).join('')}${'<div></div>'.repeat(offset)}${Array.from({length:days},(_,i)=>{const date=key+'-'+String(i+1).padStart(2,'0');return `<div class="calendar-day"><strong>${i+1}</strong>${r.events.filter(e=>e.payload.date===date).map(e=>`<button data-event="${e.id}">${esc(e.payload.title)}</button>`).join('')}</div>`;}).join('')}</div>`+
      '<h2>全部时间节点</h2>'+r.events.sort((a,b)=>a.payload.date.localeCompare(b.payload.date)).map(e=>`<article class="workspace-card"><button data-event="${e.id}">${esc(e.payload.date)} ${esc(e.payload.time||'全天／日期事件')} · ${esc(e.payload.title)}</button><p>${esc(e.payload.timezone)} ${r.reminders.filter(x=>x.event_id===e.id).map(x=>esc(x.due_at)+' '+esc(x.status)).join('；')}</p><button data-event-cancel="${e.id}">取消</button></article>`).join('')+
      '<h2>通知</h2>'+notifications.map(n=>`<p>${n.delayed?'延迟提醒：':''}${esc(n.title)} · 邮件 ${esc(n.email_status)} ${n.is_read?'已读':`<button data-notification="${n.id}">标记已读</button>`}</p>`).join('');
    panel.querySelectorAll('[data-month]').forEach(b=>b.onclick=()=>{state.month=new Date(year,month+Number(b.dataset.month),1);calendar().catch(fail);});
    panel.querySelector('[data-event-new]').onclick=()=>eventEditor();panel.querySelector('[data-refresh-calendar]').onclick=()=>show('calendar');
  }
  function eventEditor(id,task){
    const e=state.events.find(x=>x.id===id),d=e?.payload||{title:task?.title||'',date:new Date().toLocaleDateString('en-CA'),timezone:localStorage.getItem('application-timezone')||'Asia/Shanghai'};
    open(`<form id="workspace-event"><h2>日程与一次性提醒</h2><p>请确认时区。未提供官网具体截止时间时保留日期，不推断23:59。</p>${input('title','事项',d.title)}${input('date','日期',d.date,'date')}${input('time','具体时间（可空，空表示日期事件）',d.time,'time')}${input('timezone','IANA时区',d.timezone)}<label>类型<select name="kind">${['custom','exam','visa','deadline','task'].map(k=>`<option ${d.kind===k?'selected':''}>${k}</option>`).join('')}</select></label><label>备注<textarea name="notes">${esc(d.notes||'')}</textarea></label>${input('reminder','提醒时间（按上面时区填写，可空）','','datetime-local')}<label><input type="checkbox" name="email">同时发送账户邮箱（需服务配置）</label><p data-error class="error"></p><button type="button" data-close>取消</button><button>保存</button></form>`);
    $('workspace-event').onsubmit=async ev=>{ev.preventDefault();try{
      const f=ev.target.elements,data={title:f.title.value,date:f.date.value,time:f.time.value,timezone:f.timezone.value,kind:f.kind.value,notes:f.notes.value,email:f.email.checked};
      // Send a wall-clock + IANA zone; server resolves it, not the browser's machine timezone.
      const body={...command(e?.version||1),data,reminder_local:f.reminder.value||null,task_id:task?.id||null};
      await api('/calendar-events'+(id?'/'+id:''),id?'PATCH':'POST',body);localStorage.setItem('application-timezone',data.timezone);close();await show('calendar');
    }catch(error){dialog.querySelector('[data-error]').textContent=error.message;}};
  }
  async function background(){
    state.background=await api('/workspace/background');const p=state.background.fields;
    const basics={school:'学校',major:'专业',gpa_raw:'GPA原始值',gpa_scale:'GPA分制',toefl_score:'TOEFL旧制总分（新版分制请在经历／补充资料注明）',ielts_score:'IELTS',gre_score:'GRE',target_degree:'目标学位',career_goal:'职业目标',target_countries:'目标国家（逗号分隔）',target_fields:'兴趣方向（逗号分隔）',completed_courses:'已修课程（逗号分隔）'};
    panel.innerHTML='<h1>个人背景与全部经历</h1><p>填写你确认的信息，不会凭空补充经历。更新后请重新获取推荐。</p><form id="workspace-background"><div class="workspace-fields">'+Object.entries(basics).map(([k,l])=>input(k,l,Array.isArray(p[k])?p[k].join(','):p[k]||'')).join('')+'</div><h2>语言考试（支持不同版本／分制）</h2><div id="workspace-exams">'+(p.language_exams||[]).map(examRow).join('')+'</div><button type="button" data-add-exam>添加考试成绩</button>'+input('gre_exam_date','GRE考试日期',p.gre_exam_date||'','date')+'<h2>经历</h2><div id="workspace-experiences">'+state.background.experiences.map(experienceRow).join('')+'</div><button type="button" data-add-experience>添加经历</button><button>保存已确认背景</button></form>';
    panel.querySelector('[data-add-exam]').onclick=()=>$('workspace-exams').insertAdjacentHTML('beforeend',examRow());
    panel.querySelector('[data-add-experience]').onclick=()=>$('workspace-experiences').insertAdjacentHTML('beforeend',experienceRow());
    $('workspace-background').onsubmit=async e=>{e.preventDefault();try{const fields={};for(const k of Object.keys(basics)){const value=e.target.elements[k].value;if(['target_countries','target_fields','completed_courses'].includes(k))fields[k]=value.split(/[,，]/).map(s=>s.trim()).filter(Boolean);else fields[k]=['gpa_raw','gpa_scale','toefl_score','ielts_score','gre_score'].includes(k)?(value?Number(value):null):value;}
      fields.language_exams=[...panel.querySelectorAll('[data-exam]')].map(row=>{const item={};row.querySelectorAll('input').forEach(x=>item[x.name]=x.value);return item;});fields.gre_exam_date=e.target.elements.gre_exam_date.value||null;
      const experiences=[...panel.querySelectorAll('[data-experience]')].map(row=>{const item={id:row.dataset.experience};row.querySelectorAll('input,textarea,select').forEach(x=>item[x.name]=x.value);return item;});
      await api('/workspace/background','PUT',{...command(state.background.version),fields,experiences});await show('background');
    }catch(error){fail(error);}};
  }
  function experienceRow(item={id:crypto.randomUUID()}){return `<fieldset data-experience="${esc(item.id)}"><label>类别<select name="category">${['research','project','internship','competition','paper'].map(v=>`<option ${item.category===v?'selected':''}>${v}</option>`).join('')}</select></label>${input('name','名称',item.name)}${input('role','角色',item.role)}${input('dates','时间',item.dates)}<label>工作内容<textarea name="description">${esc(item.description)}</textarea></label>${input('skills','方法／技能',item.skills)}${input('outcomes','成果',item.outcomes)}<button type="button" data-remove-experience>删除</button></fieldset>`;}
  function examRow(item={}){return `<fieldset data-exam>${input('exam','考试（IELTS / TOEFL）',item.exam)}${input('version','版本／分制',item.version)}${input('total','总分',item.total)}${input('sections','单项成绩',item.sections)}${input('date','考试日期',item.date,'date')}<button type="button" data-remove-experience>删除</button></fieldset>`;}
  panel.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{
    const d=b.dataset;
    if(d.edit)await editProject(d.edit);
    if(d.editChecklist){const t=state.applications.find(x=>x.application_id===d.editChecklist);
      open('<form id="workspace-checklist-edit"><h2>编辑待确认任务</h2>'+t.checklist_draft.map(x=>`<fieldset data-checklist-item="${esc(x.stable_key)}">${input('title','任务',x.title)}${input('suggested_date','建议准备日期（可清空）',x.suggested_date||'','date')}<label>要求<textarea name="requirement">${esc(x.requirement)}</textarea></label></fieldset>`).join('')+'<p data-error class="error"></p><button type="button" data-close>取消</button><button>保存草稿</button></form>');
      $('workspace-checklist-edit').onsubmit=async ev=>{ev.preventDefault();try{const items=[...dialog.querySelectorAll('[data-checklist-item]')].map(row=>{const old=t.checklist_draft.find(x=>x.stable_key===row.dataset.checklistItem);return {...old,title:row.querySelector('[name="title"]').value,requirement:row.querySelector('textarea').value,suggested_date:row.querySelector('[name="suggested_date"]').value||null};});await api('/applications/'+t.application_id+'/checklist','PUT',{...command(t.version),items});close();await show('todo');}catch(error){dialog.querySelector('[data-error]').textContent=error.message;}};
    }
    if(d.select){const p=state.projects.find(x=>x.id===d.select)||await api('/project-library/'+d.select);await api('/project-library/'+p.id+'/select','POST',command(p.version));await show('todo');}
    for(const action of ['archive','reindex'])if(d[action]){const p=state.projects.find(x=>x.id===d[action]);await api('/project-library/'+p.id+'/'+action,'POST',command(p.version));await show('library');}
    for(const [key,action] of [['generate','checklist/generate'],['checklist','checklist/confirm'],['sync','sync-project']])if(d[key]){const t=state.applications.find(t=>t.application_id===d[key]);await api('/applications/'+t.application_id+'/'+action,'POST',command(t.version));await show('todo');}
    if(d.event)eventEditor(d.event);
    if(d.taskEvent){const task=state.applications.flatMap(x=>x.tasks).find(t=>t.id===d.taskEvent);eventEditor(null,task);}
    if(d.eventCancel){const v=state.events.find(x=>x.id===d.eventCancel);await api('/calendar-events/'+v.id+'/cancel','POST',command(v.version));await show('calendar');}
    if(d.notification){await api('/notifications/'+d.notification+'/read','POST',command());await show('calendar');}
    if(b.matches('[data-remove-experience]'))b.closest('fieldset').remove();
  }catch(error){fail(error);}});
  panel.addEventListener('change',async e=>{const d=e.target.dataset,id=d.taskCheck||d.taskStatus||d.task;if(!id)return;try{
    const task=state.applications.flatMap(x=>x.tasks).find(x=>x.id===id);const body={...command(task.version),status:d.taskCheck?(e.target.checked?'completed':'planned'):(d.taskStatus?e.target.value:task.status)};
    if(d.count)body[d.count]=Number(e.target.value);await api('/tasks/'+id+'/progress','POST',body);await show('todo');
  }catch(error){fail(error);}});
  document.querySelectorAll('[data-workspace]').forEach(b=>b.onclick=()=>show(b.dataset.workspace));
  document.addEventListener('workspace-chat',()=>show('chat'));
  document.addEventListener('workspace-reset',()=>{
    state.tab='chat';state.projects=[];state.applications=[];state.events=[];state.jobs=[];state.background=null;state.editing=null;
    panel.innerHTML='';panel.hidden=true;$('chat-panel').hidden=false;close();
  });
  document.addEventListener('workspace-results',event=>{
    const recommendation=event.detail?.research_result?.diagnostics?.recommendation;
    if(!recommendation?.results?.length)return;
    const block=document.createElement('article');block.className='workspace-card';
    block.innerHTML='<h3>项目库推荐／查询结果</h3><p>用户确认资料，不代表录取概率。画像版本 '+Number(recommendation.profile_version)+'</p>'+
      recommendation.results.map(p=>`<section><strong>${esc(p.university)} · ${esc(p.program)}</strong><details><summary>匹配依据与待核对要求</summary><pre>${esc(JSON.stringify({evidence:p.evidence,requirements:p.requirements},null,2))}</pre></details><button data-chat-select="${p.project_id}">加入申请</button></section>`).join('');
    block.addEventListener('click',async e=>{const id=e.target.dataset.chatSelect;if(!id)return;try{const p=await api('/project-library/'+id);await api('/project-library/'+id+'/select','POST',command(p.version));await show('todo');}catch(error){fail(error);}});
    $('messages').append(block);
  });
  // Poll only while authenticated workspace is visible, never replace unsaved dialog input.
  setInterval(()=>{if(!$('app').hidden&&!dialog.open&&['library','calendar'].includes(state.tab))show(state.tab);},30000);
})();
