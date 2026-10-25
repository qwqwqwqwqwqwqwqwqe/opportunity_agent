/* One application form; the server performs one atomic project.save command. */
(() => {
  'use strict';
  const {escape:esc}=window.ApplyOpsBridge;
  const materialStatuses=[['missing','未准备'],['invited','已邀请'],['draft','准备中'],['ready','已准备'],['submitted','已提交']];
  const select=(name,options,value,extra='')=>`<select name="${name}" ${extra}>${options.map(([v,t])=>`<option value="${esc(v)}" ${String(v)===String(value)?'selected':''}>${esc(t)}</option>`).join('')}</select>`;
  const input=(name,value='',type='text',extra='')=>`<input name="${name}" type="${type}" value="${esc(value??'')}" ${extra}>`;
  const obligations=[['required','必需'],['optional','可选'],['unknown','待确认']];
  const uid=()=>crypto.randomUUID().replaceAll('-','');
  const decorate=r=>({...r,_key:uid(),materials:r.materials||[]});
  function show(root,{data=null,onSave,onCancel}){
    const draft={university:'',name:'',degree:'',home_unit:'',cycle:'2027 Fall',status:'preparing',deadline_date:null,deadline_at:null,
      timezone:'Asia/Shanghai',date_precision:'unknown',gre_policy:'unknown',source_url:'',notes:'',requirements:[],...structuredClone(data||{})};
    const language=draft.requirements.find(r=>r.kind==='score'&&(r.title==='语言成绩'||r.criteria?.any_of?.length||['toefl_score','ielts_score'].includes(r.criteria?.profile_field)));
    const gre=draft.requirements.find(r=>r.kind==='score'&&r.criteria?.profile_field==='gre_score');
    let rows=draft.requirements.filter(r=>r!==language&&r!==gre).map(decorate);
    let languageEntry=language?decorate(language):decorate({title:'语言成绩',kind:'score',obligation:'unknown',required_count:1,criteria:{},materials:[{slot_key:'语言成绩',status:'missing'}]});
    let greEntry=gre?decorate(gre):decorate({title:'GRE 成绩',kind:'score',obligation:'required',required_count:1,criteria:{profile_field:'gre_score'},materials:[{slot_key:'GRE 成绩',status:'missing'}]});
    if(!languageEntry.materials.length)languageEntry.materials=[{slot_key:'语言成绩',status:'missing'}];
    if(!greEntry.materials.length)greEntry.materials=[{slot_key:'GRE 成绩',status:'missing'}];
    const alternatives=language?.criteria?.any_of?.length?language.criteria.any_of:language?.criteria?.profile_field?[{profile_field:language.criteria.profile_field,minimum:language.criteria.minimum}]:[];
    let lang={policy:language?.obligation||(data?'none':'unknown'),toefl:alternatives.some(a=>a.profile_field==='toefl_score'),ielts:alternatives.some(a=>a.profile_field==='ielts_score')};
    for(const field of ['toefl','ielts']){const a=alternatives.find(a=>a.profile_field===field+'_score');lang[field+'_min']=a?.minimum??'';for(const part of ['reading','listening','speaking','writing'])lang[field+'_'+part]=a?.subscores?.[part]??'';}
    const greValues={minimum:gre?.criteria?.minimum??'',notes:gre?.criteria?.notes||''};
    let requestId=crypto.randomUUID(),saving=false;
    const label=(text,content,wide=false)=>`<label class="${wide?'full-width':''}">${text}${content}</label>`;
    const numeric=v=>v===''||v===null?null:Number(v);
    function capture(){
      const form=root.querySelector('#project-entry');if(!form)return;
      const fd=new FormData(form);
      for(const key of ['university','name','degree','home_unit','cycle','status','date_precision','timezone','gre_policy','source_url','notes'])draft[key]=String(fd.get(key)||'');
      draft.deadline_date=fd.get('deadline_date')||null;
      draft.deadline_at=fd.get('deadline_at')||null;
      if(draft.deadline_at){const raw=draft.deadline_at;draft.deadline_at=/Z$|[+-]\d{2}:\d{2}$/.test(raw)?raw:raw+String(fd.get('deadline_offset')||'+08:00');}
      for(const row of rows){
        row.title=String(fd.get(row._key+'_title')||'');row.obligation=fd.get(row._key+'_obligation');
        row.required_count=numeric(fd.get(row._key+'_count'));
        row.materials.forEach((slot,i)=>{slot.slot_key=String(fd.get(row._key+'_slot_'+i)||'');slot.status=fd.get(row._key+'_status_'+i)||'missing';});
      }
      lang.policy=fd.get('language_policy');
      for(const field of ['toefl','ielts']){lang[field]=fd.has('language_'+field);lang[field+'_min']=fd.get(field+'_minimum');for(const part of ['reading','listening','speaking','writing'])lang[field+'_'+part]=fd.get(field+'_'+part);}
      languageEntry.materials[0].status=fd.get('language_prepared')||'missing';
      greValues.minimum=fd.get('gre_minimum');greValues.notes=fd.get('gre_notes')||'';greEntry.materials[0].status=fd.get('gre_prepared')||'missing';
    }
    function render(){
      const deadline=draft.deadline_at?.match(/^(.+?)(Z|[+-]\d{2}:\d{2})$/);
      root.innerHTML=`<form id="project-entry" class="project-entry"><div class="form-intro"><div><h2>${data?'编辑申请项目':'新增申请项目'}</h2><p class="hint">一次填写学校、日期和材料要求。未知信息可标记为待确认。</p></div><button type="button" data-project-cancel class="quiet-button">返回列表</button></div>
      <fieldset><legend>基本信息</legend><div class="form-grid">${label('学校名称 <span class="required">*</span>',input('university',draft.university,'text','required maxlength="200"'))}${label('项目名称 <span class="required">*</span>',input('name',draft.name,'text','required maxlength="200"'))}${label('申请周期 <span class="required">*</span>',input('cycle',draft.cycle,'text','required placeholder="例如 2027 Fall"'))}${label('申请状态',select('status',[['considering','考虑中'],['preparing','准备中'],['submitted','已提交'],['offered','已录取'],['rejected','未录取'],['withdrawn','已撤回']],draft.status))}${label('学位',input('degree',draft.degree))}${label('院系',input('home_unit',draft.home_unit))}</div></fieldset>
      <fieldset><legend>申请截止日期</legend><div class="form-grid">${label('日期精度',select('date_precision',[['unknown','暂不确定'],['date','只有日期'],['instant','有具体截止时刻']],draft.date_precision))}${label('截止日期',input('deadline_date',draft.deadline_date,'date'))}<details class="full-width" ${draft.date_precision==='instant'?'open':''}><summary>具体截止时刻与时区</summary><div class="form-grid">${label('截止时刻',input('deadline_at',deadline?.[1]||draft.deadline_at||'','datetime-local'))}${label('UTC 偏移',select('deadline_offset',[['-12:00','UTC−12:00'],['-08:00','UTC−08:00'],['-07:00','UTC−07:00'],['-05:00','UTC−05:00'],['-04:00','UTC−04:00'],['+00:00','UTC+00:00'],['+08:00','UTC+08:00'],['+09:00','UTC+09:00']],deadline?.[2]==='Z'?'+00:00':deadline?.[2]||'+08:00'))}${label('来源时区（IANA）',input('timezone',draft.timezone,'text','placeholder="Asia/Shanghai"'))}</div></details></div></fieldset>
      <fieldset><legend>材料要求</legend><p class="hint">每项材料占一组进度；推荐信按所需份数计算整组是否准备完成。</p><div class="requirement-editor">${rows.map(row=>`<article class="requirement-row" data-requirement-row="${row._key}"><div class="requirement-row-heading">${input(row._key+'_title',row.title,'text','required aria-label="材料名称" maxlength="200"')}<button type="button" class="icon-button" data-remove-requirement="${row._key}" aria-label="移除此项要求">×</button></div><div class="form-grid compact">${label('是否必需',select(row._key+'_obligation',obligations,row.obligation))}${label('需要份数（未知可留空）',input(row._key+'_count',row.required_count,'number','min="1" max="20" data-slot-count="'+row._key+'"'))}</div><div class="material-slots">${row.materials.map((slot,i)=>`<div class="material-slot">${input(row._key+'_slot_'+i,slot.slot_key,'text','required aria-label="材料或推荐人名称" maxlength="160"')}${select(row._key+'_status_'+i,materialStatuses,slot.status,'aria-label="材料准备情况"')}</div>`).join('')}</div></article>`).join('')||'<p class="empty">尚未添加材料。选择常用项，或添加自定义材料。</p>'}</div><div class="material-shortcuts">${[['CV','简历 CV','document'],['SOP','SOP 最终版','document'],['transcript','成绩单','document'],['letters','推荐信','recommendation'],['custom','自定义材料','document']].map(([key,title,kind])=>`<button type="button" class="text-button" data-add-requirement="${key}" data-title="${title}" data-kind="${kind}">＋ ${title}</button>`).join('')}</div></fieldset>
      <fieldset><legend>语言要求</legend><div class="form-grid">${label('学校是否要求语言成绩',select('language_policy',[...obligations,['none','不要求']],lang.policy))}${label('当前证明材料准备情况',select('language_prepared',materialStatuses,languageEntry.materials[0]?.status||'missing'))}</div><p class="hint">接受多种考试时，满足其中一种即可。未填写的门槛保持未知。</p>${['toefl','ielts'].map(field=>`<div class="score-option"><label class="check-label"><input type="checkbox" name="language_${field}" ${lang[field]?'checked':''}>接受 ${field.toUpperCase()}</label><div class="form-grid">${label('总分最低要求',input(field+'_minimum',lang[field+'_min'],'number','min="0" step="0.5"'))}<details><summary>单项要求</summary><div class="form-grid">${[['reading','阅读'],['listening','听力'],['speaking','口语'],['writing','写作']].map(([part,title])=>label(title,input(field+'_'+part,lang[field+'_'+part],'number','min="0" step="0.5"'))).join('')}</div></details></div></div>`).join('')}</fieldset>
      <fieldset><legend>GRE 要求</legend><div class="form-grid">${label('GRE 政策',select('gre_policy',[['unknown','待确认'],['required','必需'],['optional','可选'],['not_required','不要求'],['not_accepted','不接受']],draft.gre_policy))}${label('总分最低要求',input('gre_minimum',greValues.minimum,'number','min="0"'))}${label('当前证明材料准备情况',select('gre_prepared',materialStatuses,greEntry.materials[0]?.status||'missing'))}${label('其他要求说明',input('gre_notes',greValues.notes))}</div></fieldset>
      <fieldset><legend>来源与备注</legend><div class="form-grid">${label('来源链接',input('source_url',draft.source_url,'url','placeholder="https://…"'),true)}${label('要求原文或备注',`<textarea name="notes" rows="4" maxlength="10000">${esc(draft.notes)}</textarea>`,true)}</div></fieldset><p id="project-save-error" class="error" role="alert"></p><div class="project-form-actions"><button type="button" data-project-cancel>取消</button><button type="submit" class="primary">${data?'保存修改':'添加项目'}</button></div></form>`;
    }
    render();
    root.onclick=event=>{
      const b=event.target.closest('button');if(!b)return;
      if(b.hasAttribute('data-project-cancel')){onCancel();return;}
      if(saving)return;
      if(b.dataset.addRequirement){capture();const count=b.dataset.kind==='recommendation'?3:1;rows.push(decorate({title:b.dataset.addRequirement==='custom'?'':b.dataset.title,kind:b.dataset.kind,obligation:'required',required_count:count,criteria:{},materials:Array.from({length:count},(_,i)=>({slot_key:count===1?b.dataset.title:`推荐人 ${i+1}`,status:'missing'}))}));requestId=crypto.randomUUID();render();}
      if(b.dataset.removeRequirement){capture();rows=rows.filter(r=>r._key!==b.dataset.removeRequirement);requestId=crypto.randomUUID();render();}
    };
    root.onchange=event=>{
      if(!event.target.closest('#project-entry'))return;
      requestId=crypto.randomUUID();
      const form=event.target.form;
      if(event.target.name==='deadline_date'&&event.target.value){form.elements.date_precision.value='date';form.elements.deadline_at.value='';}
      if(event.target.name==='deadline_at'&&event.target.value){form.elements.date_precision.value='instant';form.elements.deadline_date.value='';}
      if(event.target.name==='date_precision'&&event.target.value==='unknown'){form.elements.deadline_date.value='';form.elements.deadline_at.value='';}
      if(event.target.dataset.slotCount){capture();const row=rows.find(r=>r._key===event.target.dataset.slotCount),n=row.required_count;
        if(n===null){return;}if(!Number.isInteger(n)||n<1||n>20)return;
        while(row.materials.length<n)row.materials.push({slot_key:row.kind==='recommendation'?`推荐人 ${row.materials.length+1}`:`${row.title} ${row.materials.length+1}`,status:'missing'});
        row.materials=row.materials.slice(0,n);render();}
    };
    root.onsubmit=async event=>{
      if(event.target.id!=='project-entry')return;
      event.preventDefault();if(saving)return;capture();
      const clean=row=>{const {_key,...rest}=row;return rest;};
      const requirements=rows.map(clean);
      if(lang.policy!=='none'){
        const any_of=['toefl','ielts'].filter(field=>lang[field]).map(field=>({profile_field:field+'_score',minimum:numeric(lang[field+'_min']),subscores:Object.fromEntries(['reading','listening','speaking','writing'].filter(part=>lang[field+'_'+part]!=='').map(part=>[part,Number(lang[field+'_'+part])]))}));
        requirements.push({...clean(languageEntry),obligation:lang.policy,criteria:{...languageEntry.criteria,profile_field:null,minimum:null,any_of}});
      }
      if(['required','optional'].includes(draft.gre_policy)||gre&&draft.gre_policy==='unknown')requirements.push({...clean(greEntry),obligation:draft.gre_policy==='required'?'required':draft.gre_policy==='unknown'?'unknown':'optional',criteria:{...greEntry.criteria,profile_field:'gre_score',minimum:numeric(greValues.minimum),notes:greValues.notes}});
      const {application_id,...body}=draft;
      body.request_id=requestId;body.requirements=requirements;
      if(body.date_precision==='unknown'){body.deadline_date=null;body.deadline_at=null;}
      if(body.date_precision==='date')body.deadline_at=null;
      if(body.date_precision==='instant')body.deadline_date=null;
      const button=event.target.querySelector('[type=submit]');button.disabled=true;saving=true;
      try{await onSave(body);}
      catch(error){const el=root.querySelector('#project-save-error');if(el){el.textContent=error.message;el.scrollIntoView({block:'center',behavior:'smooth'});}button.disabled=false;saving=false;}
    };
  }
  window.ApplyOpsProjectForm={show};
})();
