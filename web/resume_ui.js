/* Resume drafts stay in memory/server, never in localStorage. */
const ResumeUI=(()=>{
  const groups=[
    ['基础资料',['school','major','academic_year','degree_years','graduation_year','graduation_month','gpa_raw','gpa_scale','class_rank','toefl_score','ielts_score','gre_score','completed_courses','skills','hardware_skills']],
    ['申请目标（简历没有的请补充）',['target_countries','target_degree','target_fields','target_schools','target_programs','budget','exam_plan','planned_enrollment_year','planned_enrollment_month','summer_preference']]
  ];
  const labels={school:'当前学校',major:'当前专业',academic_year:'本科年级',degree_years:'学制',graduation_year:'毕业年份',graduation_month:'毕业月份',gpa_raw:'GPA 原值',gpa_scale:'GPA 量表',class_rank:'专业排名',toefl_score:'TOEFL',ielts_score:'IELTS',gre_score:'GRE',completed_courses:'已修课程',skills:'编程与软件技能',hardware_skills:'硬件技能',target_countries:'目标国家',target_degree:'目标学位',target_fields:'目标方向',target_schools:'目标学校',target_programs:'目标项目',budget:'预算（JSON：amount/currency/period）',exam_plan:'考试安排（JSON：exam_type/next_exam_date）',planned_enrollment_year:'入学年份',planned_enrollment_month:'入学月份',summer_preference:'暑期偏好（research/internship/both/unknown）'};
  const types={research:'科研',project:'项目',internship:'实习',competition:'竞赛',paper:'论文'};
  labels.budget='留学预算';labels.exam_plan='下一次考试安排';
  const numeric=new Set(['academic_year','degree_years','graduation_year','graduation_month','gpa_raw','gpa_scale','toefl_score','ielts_score','gre_score','planned_enrollment_year','planned_enrollment_month']);
  const lists=new Set(['target_countries','target_fields','target_schools','target_programs','completed_courses','skills','hardware_skills']);
  const states={queued:'等待处理',reading:'读取文件',awaiting_consent:'需要增强识别',cloud_parsing:'识别扫描件 / 转换 Word',extracting:'AI 提取信息',review:'请修改并确认',failed:'处理未完成',interrupted:'处理已中断',confirmed:'资料已确认',cancelled:'导入已取消'};
  const cache=new Map(),dirty=new Set(),saving=new Set(),uploading=new Set();
  let root,fileInput,uploadSid=null,enhanced=false,timer=null;
  const url=(job,action='')=>'/api/resume/imports/'+job.import_id+(action?'/'+action:'')+'?session_id='+encodeURIComponent(job.session_id);
  const valueText=(value)=>value==null?'':Array.isArray(value)?value.join('\n'):typeof value==='object'?JSON.stringify(value):String(value);
  const blankFact=field=>({field,raw_value:null,normalized_value:null,confidence:1,source:'resume',needs_confirmation:true,evidence:'用户手工补充',operation:'set',block_ids:[],selected:false});
  function factFor(job,field){let f=job.draft.facts.find(f=>f.field===field);if(!f){f=blankFact(field);job.draft.facts.push(f)}return f}
  function statusNote(text,error=false){const note=root.querySelector('.resume-save-note');if(note){note.textContent=text;note.classList.toggle('resume-error',error)}}
  async function refresh(sid=activeId,force=false){
    if(!sid||(!force&&(dirty.has(sid)||saving.has(sid)||uploading.has(sid))))return;
    try {
      await waitForConversationCreation(sid);
      const {data}=await jsonRequest('/api/resume/imports?session_id='+encodeURIComponent(sid));
      const job=(data.imports||[]).sort((a,b)=>b.created_at.localeCompare(a.created_at))[0]||null;
      const previous=cache.get(sid);
      if(!previous||!job||previous.revision!==job.revision||previous.current_profile_revision!==job.current_profile_revision||force){
        cache.set(sid,job);if(activeId===sid)render();
      }
    }catch(error){if(activeId===sid)statusNote(error.message,true)}
  }
  function choose(force=false){enhanced=force;uploadSid=activeId;fileInput.value='';fileInput.click()}
  async function upload(file,sid,force){
    if(!file||uploading.has(sid))return;
    if(file.size>10*1024*1024){window.alert('文件不能超过 10 MB');return}
    const old=cache.get(sid);
    if(old&&old.status!=='cancelled'){
      if(!window.confirm('重新上传会删除旧导入草稿；已确认画像不会撤销。继续吗？'))return;
      await jsonRequest(url(old),{method:'DELETE'});
    }
    uploading.add(sid);dirty.delete(sid);if(sid===activeId){closeOnboarding();render();statusNote('正在上传…')}
    try{
      await waitForConversationCreation(sid);
      const body=new FormData();body.append('session_id',sid);body.append('request_id',crypto.randomUUID());body.append('enhanced',String(force));body.append('file',file);
      const {data}=await jsonRequest('/api/resume/imports',{method:'POST',body});cache.set(sid,data);
    }catch(error){if(sid===activeId)window.alert(error.message)}
    finally{uploading.delete(sid);if(sid===activeId){render();root.scrollIntoView({block:'start'})}}
  }
  function touch(sid){dirty.add(sid);statusNote('有未保存修改');clearTimeout(timer);timer=setTimeout(()=>save(sid),1000)}
  async function save(sid=activeId){
    const job=cache.get(sid);if(!job||job.status!=='review'||saving.has(sid))return null;
    if(sid===activeId&&root.querySelector(':invalid')){statusNote('请先修正格式有误的字段，再保存或确认。',true);return null}
    saving.add(sid);clearTimeout(timer);const version=JSON.stringify(job.draft);
    try{
      const {data}=await jsonRequest(url(job,'draft'),{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({session_id:sid,revision:job.revision,profile_revision:job.current_profile_revision,draft:job.draft})});
      if(version===JSON.stringify(job.draft)){
        // Input handlers still reference the displayed draft objects. Replacing
        // them without rerendering would silently discard the very next edit.
        const displayedDraft=job.draft;Object.assign(job,data);job.draft=displayedDraft;
        job.draft.experiences.forEach((e,i)=>{e.experience_id=data.draft.experiences[i]?.experience_id||e.experience_id});
        dirty.delete(sid);
      }
      else {job.revision=data.revision;job.current_profile_revision=data.current_profile_revision;timer=setTimeout(()=>save(sid),1000)}
      if(activeId===sid)statusNote('草稿已保存；正式画像尚未改变');
      return data;
    }catch(error){
      if(error.status===409){
        // Keep edits, but load the new baseline. Require a deliberate next
        // save after showing differences, never silently rebase a confirmation.
        const latest=(await jsonRequest(url(job))).data;
        job.revision=latest.revision;job.current_profile_revision=latest.current_profile_revision;job.current_profile=latest.current_profile;
        if(activeId===sid){render();statusNote('画像或草稿已在其他窗口修改，请核对当前值后重新保存。',true)}
      }else if(activeId===sid)statusNote(error.message,true);
      return null;
    }finally{saving.delete(sid)}
  }
  async function action(name){
    const sid=activeId,job=cache.get(sid);if(!job)return;
    const buttons=root.querySelectorAll('button');buttons.forEach(b=>b.disabled=true);
    try{
      if(name==='confirm-save'||name==='confirm-plan'){
        if(saving.has(sid)){statusNote('草稿正在保存，请稍后再次确认');return}
        const saved=await save(sid);if(!saved)return;
        const {data}=await jsonRequest(url(saved,'confirm'),{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({session_id:sid,revision:saved.revision,generate_plan:name==='confirm-plan'})});
        cache.set(sid,data.job);const item=conversations.find(c=>c.id===sid);
        if(item){applyServerData(item,data.snapshot);saveConversations();if(activeId===sid)openConversation(sid,false)}
        if(data.needs_profile&&name==='confirm-plan'&&activeId===sid)openOnboarding(data.snapshot.profile);
        if(item&&data.planning_action)await mutateConversation(item,'/api/roadmap/'+data.planning_action);
      } else if(name==='delete'){
        if(!window.confirm('删除此导入记录？已确认的画像需通过编辑资料修改。'))return;
        await jsonRequest(url(job),{method:'DELETE'});cache.delete(sid);dirty.delete(sid);
      } else if(name==='cloud'||name==='local'){
        const accept=name==='cloud';
        if(accept&&!window.confirm('将整份简历发送至 MinerU。其云端副本留存受该平台政策约束，本机删除不保证云端删除。是否同意？'))return;
        const {data}=await jsonRequest(url(job,'cloud-consent'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,accept})});cache.set(sid,data);
      } else if(name==='retry'||name==='paste'){
        const text=name==='paste'?root.querySelector('.resume-paste').value:undefined;
        const {data}=await jsonRequest(url(job,'retry'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,text})});cache.set(sid,data);dirty.delete(sid);
      } else if(name==='reextract'){
        if(!window.confirm('将丢弃尚未确认的简历草稿修改，并从已读取文本重新执行规则和 AI 提取。继续吗？'))return;
        const {data}=await jsonRequest(url(job,'reextract'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid})});cache.set(sid,data);dirty.delete(sid);
      } else if(name==='save'){await save(sid);return}
      else if(name==='enhance'){choose(true);return}
    }catch(error){if(activeId===sid)statusNote(error.message,true);return}
    finally{buttons.forEach(b=>b.disabled=false)}
    if(activeId===sid)render();
  }
  function structuredControl(card,field,fact,sid,value,check){
    const specs=field==='budget'?[['amount','number','金额'],['currency','text','币种，例如 CNY / USD'],['period','select','预算周期']]:
      [['exam_type','select','考试类型'],['next_exam_date','date','考试日期'],['target_score','number','目标分数（可空）']];
    const choices={period:[['','请选择周期'],['unknown','未确定'],['total','总预算'],['annual','每年']],exam_type:[['','请选择考试'],['TOEFL','TOEFL'],['IELTS','IELTS'],['GRE','GRE'],['other','其他']]};
    const box=card.querySelector('.resume-composite'),inputs={};
    for(const [key,type,label] of specs){
      const row=document.createElement('label');row.textContent=label;
      const input=document.createElement(type==='select'?'select':'input');inputs[key]=input;input.dataset.part=key;
      if(type==='select')input.innerHTML=choices[key].map(([v,t])=>'<option value="'+v+'">'+t+'</option>').join('');
      else{input.type=type;if(type==='number'){input.min='0';input.step='any'}}
      input.value=value?.[key]??'';row.append(input);box.append(row);
    }
    const change=()=>{
      for(const input of Object.values(inputs))input.setCustomValidity('');
      const entries=Object.entries(inputs).filter(([,input])=>input.value.trim()!=='');
      if(!entries.length){fact.normalized_value=fact.raw_value=null;fact.selected=check.checked=false;touch(sid);return}
      const required=field==='budget'?['amount','currency','period']:['exam_type','next_exam_date'];
      const missing=required.find(k=>inputs[k].value.trim()==='');
      if(missing){inputs[missing].setCustomValidity('请补齐这一项，或清空整个安排');statusNote('请补齐金额、币种和周期，或考试类型与日期。',true);return}
      fact.normalized_value=Object.fromEntries(entries.map(([key,input])=>[key,input.type==='number'?Number(input.value):input.value.trim()]));
      fact.raw_value=fact.normalized_value;fact.selected=check.checked=true;touch(sid);
    };
    Object.values(inputs).forEach(input=>{input.oninput=change;input.onchange=change});
  }
  function partialPreview(job){
    const draft=job.draft||{},facts=draft.facts||[],experiences=draft.experiences||[];
    if(!facts.length&&!experiences.length)return '';
    const factRows=facts.map(f=>'<div class="row"><span class="label">'+esc(labels[f.field]||f.field)+'</span>'+esc(valueText(f.normalized_value??f.raw_value))+'</div>').join('');
    const experienceRows=experiences.map(e=>experiencePreview(e)).join('');
    const running=job.status==='extracting';
    return '<section class="resume-partial"><h3>'+ (running?'已保存的提取结果':'已保留的提取结果') +'</h3><p class="resume-help">已写入服务端草稿，尚未确认到正式画像。'+(running?'正在继续提取其他片段；':'可直接重试未完成片段；')+'资料 '+facts.length+' 项，经历 '+experiences.length+' 条。</p>'+factRows+experienceRows+'</section>';
  }
  function experiencePreview(exp){
    const rows=[['机构',exp.organization],['时间',exp.period],['职责',exp.role],['技术与方法',exp.methods],['成果',exp.outcomes]].filter(([,value])=>value);
    return '<article class="resume-experience-summary"><div class="resume-experience-title"><span class="resume-kind">'+esc(types[exp.kind]||exp.kind)+'</span><strong>'+esc(exp.name||'未命名经历')+'</strong></div>'+rows.map(([label,value])=>'<div class="resume-experience-line"><span>'+esc(label)+'</span><div>'+esc(value)+'</div></div>').join('')+'</article>';
  }
  function render(){
    if(!root)return;
    const sid=activeId,job=cache.get(sid),busy=uploading.has(sid)||job&&['queued','reading','cloud_parsing','extracting'].includes(job.status);
    root.innerHTML='<h2>简历导入</h2><div class="resume-drop" tabindex="0" role="button">＋ 选择或拖放 PDF / DOC / DOCX</div><div class="resume-privacy">最多 10 MB / 20 页。文本会交当前 AI 服务提取；原件解析后从本机删除。扫描件与旧 DOC 需单独同意发送 MinerU。</div><div class="resume-save-note" role="status" aria-live="polite"></div>';
    const drop=root.querySelector('.resume-drop');drop.onclick=()=>choose();drop.onkeydown=e=>{if(e.key==='Enter')choose()};
    drop.ondragover=e=>{e.preventDefault();drop.classList.add('dragover')};drop.ondragleave=()=>drop.classList.remove('dragover');
    drop.ondrop=e=>{e.preventDefault();drop.classList.remove('dragover');if(e.dataTransfer.files.length!==1){window.alert('一次只能上传一份简历');return}upload(e.dataTransfer.files[0],sid,false)};
    if(!job){if(busy)statusNote('正在上传…');return}
    if(job.parsed){
      const factCount=job.draft?.facts?.length||0, experienceCount=job.draft?.experiences?.length||0;
      root.insertAdjacentHTML('beforeend','<p class="resume-help">已读取 '+job.parsed.blocks.length+' 个文本块（'+job.parsed.page_count+' 页）；当前已抽取 '+factCount+' 项资料、'+experienceCount+' 条经历。读取文本会保留，AI 分段失败不会删除其他已完成分段的结果。</p>');
    }
    const heading=document.createElement('div');heading.className='resume-status';heading.innerHTML=(busy?'<span class="resume-spinner"></span>':'')+esc(states[job.status]||job.status)+'<div class="resume-help">'+esc(job.filename)+' · '+esc(job.mode==='llm'?'AI 草稿':'规则 / 文件读取')+'</div>';root.append(heading);
    if(job.error){const note=document.createElement('div');note.className='resume-error';note.textContent=job.error;root.append(note)}
    if(['extracting','failed','interrupted'].includes(job.status))root.insertAdjacentHTML('beforeend',partialPreview(job));
    if(job.status==='awaiting_consent'){
      root.insertAdjacentHTML('beforeend','<p class="resume-help">本地读取可能不完整。'+(job.cloud_configured?'可以申请增强识别。':'尚未配置 MINERU_API_TOKEN，未向云端发送文件。')+'等待授权的原件最多保留 30 分钟。</p><div class="resume-actions"><button data-resume-action="cloud" '+(job.cloud_configured?'':'disabled')+'>同意云端增强</button><button class="secondary" data-resume-action="local">只使用本地文字</button></div>');
    }
    if(job.status==='review'){
      root.insertAdjacentHTML('beforeend','<div class="resume-actions"><button class="secondary" data-resume-action="retry" title="只使用已保存的读取文本，不会重新上传原件">补全未完成的 AI 提取</button><button class="secondary" data-resume-action="reextract" title="重新执行规则和全部 AI 分段；尚未确认的草稿修改会被替换">重新从已读取文本提取</button></div><p class="resume-help">“补全”保留现有草稿并仅继续未完成分段；“重新提取”会清空未确认草稿，按当前规则从已保存文本重新生成。</p>');
      for(const [title,fields] of groups){
        root.insertAdjacentHTML('beforeend','<h3>'+esc(title)+'</h3>');
        for(const field of fields){
          const fact=factFor(job,field),value=fact.normalized_value??fact.raw_value,old=job.current_profile[field];
          const card=document.createElement('div');card.className='resume-field';card.dataset.field=field;
          card.innerHTML='<label class="resume-select"><input type="checkbox" '+(fact.selected?'checked':'')+'>采纳 · '+esc(labels[field])+'</label>'+
            (['budget','exam_plan'].includes(field)?'<div class="resume-composite"></div>':lists.has(field)?'<textarea rows="2"></textarea>':'<input class="resume-value" type="'+(numeric.has(field)?'number':'text')+'" step="any">')+
            (old!=null&&JSON.stringify(old)!==JSON.stringify(value)?'<div class="resume-old">当前值：'+esc(valueText(old))+' · 不勾选则保留</div>':'')+
            '<details><summary>原文依据 · '+Math.round(fact.confidence*100)+'% 抽取置信度</summary>'+esc(fact.evidence||'简历未提供，请补充')+'\n'+esc((fact.block_ids||[]).map(id=>job.parsed?.blocks?.find(b=>b.block_id===id)?.locator||id).join(' / '))+'</details>';
          const input=card.querySelector('textarea,.resume-value'),check=card.querySelector('input[type=checkbox]');
          if(['budget','exam_plan'].includes(field))structuredControl(card,field,fact,sid,value,check);
          else {input.value=valueText(value);input.oninput=()=>{
            fact.normalized_value=input.value.trim()===''?null:numeric.has(field)?Number(input.value):lists.has(field)?input.value.split(/[\n，,、]+/).map(s=>s.trim()).filter(Boolean):input.value.trim();
            fact.raw_value=fact.normalized_value;fact.selected=check.checked=fact.normalized_value!==null;touch(sid);
          }}
          check.onchange=()=>{fact.selected=check.checked;touch(sid)};root.append(card);
        }
      }
      root.insertAdjacentHTML('beforeend','<h3>经历与项目</h3>');
      job.draft.experiences.forEach((exp,index)=>{
        const card=document.createElement('div');card.className='resume-experience';card.dataset.experience=index;
        card.innerHTML='<label><input type="checkbox" '+(exp.selected?'checked':'')+'> 采纳这条经历</label><label>分类<select>'+Object.entries(types).map(([value,label])=>'<option value="'+value+'" '+(value===exp.kind?'selected':'')+'>'+label+'</option>').join('')+'</select></label><div class="resume-experience-extracted"><div class="resume-experience-caption">提取结果（以下字段可逐项修改）</div>'+experiencePreview(exp)+'</div>';
        card.querySelector('input[type=checkbox]').onchange=e=>{exp.selected=e.target.checked;touch(sid)};
        card.querySelector('select').onchange=e=>{exp.kind=e.target.value;touch(sid)};
        const editor=document.createElement('div');editor.className='resume-experience-editor';
        for(const [key,label] of Object.entries({name:'名称',organization:'机构',period:'时间（保留原精度）',role:'个人职责',methods:'技术与方法',outcomes:'成果'})){
          const tag=document.createElement('label');tag.className='resume-experience-field';tag.textContent=label;const input=document.createElement('textarea');input.rows=key==='methods'||key==='outcomes'?2:1;input.value=exp[key]||'';input.oninput=()=>{exp[key]=input.value;touch(sid)};tag.append(input);
          editor.append(tag);
        }
        card.append(editor);
        card.insertAdjacentHTML('beforeend','<details><summary>原文证据 · '+Math.round(exp.confidence*100)+'%</summary>'+esc(exp.evidence)+'\n'+esc((exp.block_ids||[]).join(', '))+'</details>');root.append(card);
      });
      const add=document.createElement('button');add.className='secondary';add.textContent='＋ 添加一条经历';add.onclick=()=>{job.draft.experiences.push({kind:'project',name:'新项目',organization:'',period:'',role:'',methods:'',outcomes:'',evidence:'用户手工补充',block_ids:[],confidence:1,selected:true});touch(sid);render()};root.append(add);
      root.insertAdjacentHTML('beforeend','<div class="resume-help">'+esc((job.draft.warnings||[]).join('\n'))+'</div><div class="resume-actions"><button class="secondary" data-resume-action="save">保存草稿</button><button class="secondary" data-resume-action="confirm-save">仅保存资料</button><button data-resume-action="confirm-plan">确认并生成规划</button></div>');
    }
    if(job.parsed&&job.status!=='confirmed'){
      root.insertAdjacentHTML('beforeend','<details class="resume-help"><summary>查看读取文本（仅保存在服务端）</summary><pre>'+esc(job.parsed.blocks.map(b=>b.locator+'\n'+b.text).join('\n\n'))+'</pre></details>');
    }
    if(['failed','interrupted','review','awaiting_consent'].includes(job.status)){
      root.insertAdjacentHTML('beforeend','<details class="resume-help"><summary>识别不准确？重试或粘贴文字</summary><textarea class="resume-paste" placeholder="粘贴正确简历文本；将交当前 LLM 重新提取"></textarea><div class="resume-actions"><button class="secondary" data-resume-action="paste">从粘贴文字提取</button><button class="secondary" data-resume-action="retry">补全未完成提取</button><button class="secondary" data-resume-action="reextract">重新从已读取文本提取</button><button class="secondary" data-resume-action="enhance">重传原件并增强识别</button></div></details>');
    }
    if(job.status==='confirmed')root.insertAdjacentHTML('beforeend','<p class="resume-help">资料已确认。为保护隐私，已读取文本和原文件均已删除；如需重新读取简历，请使用上方“导入简历”重新上传。</p>');
    root.insertAdjacentHTML('beforeend','<div class="resume-actions"><button class="secondary" data-resume-action="delete">取消 / 删除导入记录</button></div>');
    root.querySelectorAll('[data-resume-action]').forEach(b=>b.onclick=()=>action(b.dataset.resumeAction));
  }
  function setup(){
    root=document.createElement('section');root.id='resume-panel';root.className='resume-panel';document.querySelector('aside.panel').prepend(root);
    fileInput=document.createElement('input');fileInput.type='file';fileInput.id='resume-file';fileInput.accept='.pdf,.doc,.docx';fileInput.hidden=true;document.body.append(fileInput);fileInput.onchange=()=>upload(fileInput.files[0],uploadSid,enhanced);
    const uploadButton=document.createElement('button');uploadButton.id='import-resume';uploadButton.className='secondary';uploadButton.type='button';uploadButton.textContent='导入简历';uploadButton.onclick=()=>choose();
    const header=document.createElement('div');header.className='resume-upload-header';document.querySelector('#edit-profile').after(header);header.append(document.querySelector('#edit-profile'),uploadButton);
    const entry=document.createElement('div');entry.className='onboarding-resume';entry.innerHTML='<button id="onboarding-import-resume" type="button" class="secondary">先导入现有简历</button> 自动预填背景信息，审核后生效；申请目标可稍后补充。';onboardingForm.prepend(entry);entry.querySelector('button').onclick=()=>choose();
    entry.ondragover=e=>e.preventDefault();entry.ondrop=e=>{e.preventDefault();if(e.dataTransfer.files.length===1)upload(e.dataTransfer.files[0],activeId,false)};
    // Keep the existing onboarding contract, but stop concatenating and then
    // comma-splitting research, papers and internships into projects.
    const old=document.querySelector('[name=experiences]');old.name='project_experiences';old.placeholder='每条经历独占一行，句内逗号保留';old.previousElementSibling.textContent='项目经历';
    for(const [kind,name] of Object.entries({research:'科研经历',internship:'实习经历',competition:'竞赛经历',paper:'论文经历'})){
      const field=document.createElement('div');field.className='field full';field.innerHTML='<label>'+name+'</label><textarea name="'+kind+'_experiences" rows="2" placeholder="每条经历独占一行"></textarea>';old.parentElement.after(field);
    }
    const baseOpen=openOnboarding;openOnboarding=function(profile={}){baseOpen(profile);for(const kind of Object.keys(types)){setFormValue(kind+'_experiences',(profile[kind+'_experiences']||[]).join('\n'))}};
    const basePayload=onboardingPayload;onboardingPayload=function(){const payload=basePayload();for(const kind of Object.keys(types)){payload[kind+'_experiences']=String(onboardingForm.elements[kind+'_experiences'].value||'').split(/\n+/).map(s=>s.trim()).filter(Boolean)}return payload};
    const baseConversation=openConversation;openConversation=function(id,showForm=true){baseConversation(id,showForm);render();refresh(id)};
    setInterval(()=>{const job=cache.get(activeId);if(job&&['queued','reading','awaiting_consent','cloud_parsing','extracting'].includes(job.status))refresh()},2000);
    window.addEventListener('focus',()=>refresh());render();if(activeId)refresh(activeId);
  }
  return {setup,refresh};
})();
ResumeUI.setup();
