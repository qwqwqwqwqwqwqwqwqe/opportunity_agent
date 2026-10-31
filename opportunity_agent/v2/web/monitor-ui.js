/* Official monitoring and an impact-only inbox. Remote HTML is never rendered. */
(() => {
  'use strict';
  const status={not_configured:'尚未配置',paused:'已暂停',ready:'待检查',uninitialized:'等待建立基线',queued:'排队中',running:'检查中',analyzing:'分析个人影响',baseline:'已建立基线',unchanged:'无有效变化',affected:'有个人影响',unknown:'待核实（不通知）',no_direct_impact:'无直接影响',not_applicable:'不适用',fetch_failed:'网页获取失败',analysis_failed:'影响分析失败',failed:'分析失败',superseded:'输入已变更，旧结果已停用'};
  const direction={POSITIVE:'正面影响',NEGATIVE:'负面影响',MIXED:'正负影响',NONE:'无直接影响',UNKNOWN:'待核实'};
  const roleName={project:'项目页',college:'学院页',faq:'FAQ'};
  window.ApplyOpsMonitorUI = ({api,esc,toast,projectName,getView,onChanged,onForm}) => {
    let generation=0,authEpoch=0;
    const dialog=document.createElement('dialog');dialog.className='monitor-dialog';document.body.append(dialog);
    const time=(value,zone='Asia/Shanghai')=>value?new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value)?value:value+'Z').toLocaleString('zh-CN',{timeZone:zone}):'尚无记录';
    const link=url=>{try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)?`<a href="${esc(u.href)}" target="_blank" rel="noopener noreferrer">${esc(u.href)}</a>`:'无效链接';}catch{return '无效链接';}};
    function open(html){onForm(true);dialog.innerHTML=`<div class="section-toolbar"><h2>官网监控</h2><button data-monitor-close aria-label="关闭">×</button></div>${html}`;if(!dialog.open)dialog.showModal();}
    dialog.addEventListener('close',()=>{onForm(false);if(getView()==='inbox')onChanged();});
    dialog.addEventListener('click',event=>{if(event.target.closest('[data-monitor-close]'))dialog.close();});
    async function config(appId){
      const account=authEpoch;
      const value=await api(`/applications/${appId}/monitor`),urls={};
      if(account!==authEpoch||document.getElementById('app').hidden)return;
      for(const source of value.sources)for(const role of source.roles)urls[role]=source.url;
      open(`<h3>${esc(projectName(appId))}</h3><p class="hint">仅支持静态 HTML 官网。首次检查建立基线；资料不足或官网冲突不会发信。</p><form id="monitor-config-form" class="monitor-form">
        <label>项目页（必填）<input name="project_url" type="url" required value="${esc(urls.project||'')}" placeholder="https://..."></label>
        <label>学院页（选填）<input name="college_url" type="url" value="${esc(urls.college||'')}"></label>
        <label>FAQ（选填）<input name="faq_url" type="url" value="${esc(urls.faq||'')}"></label>
        <label>检查模式<select name="mode"><option value="manual" ${value.mode!=='daily'?'selected':''}>仅手动检查</option><option value="daily" ${value.mode==='daily'?'selected':''}>每日自动检查（也可手动）</option></select></label>
        <label>每日检查时间<input type="time" name="daily_time" required value="${esc(value.daily_time||'09:00')}"></label>
        <label>时区<input name="timezone" required value="${esc(value.timezone||'Asia/Shanghai')}"></label>
        <label class="monitor-checkbox"><input type="checkbox" name="enabled" ${value.enabled?'checked':''}>启用监控</label>
        <label class="monitor-checkbox"><input type="checkbox" name="confirmed" required>我确认以上页面属于该申请项目的官方网站</label>
        <p class="hint">自动检查需要电脑和 Worker 持续运行。Agent 只分析影响，不修改项目、材料或待办。</p><p class="error" id="monitor-form-error" role="alert"></p>
        <div class="section-toolbar"><button type="button" data-monitor-close>取消</button><button class="primary" type="submit">保存配置</button></div></form>`);
      const requestId=crypto.randomUUID();
      dialog.querySelector('form').onsubmit=async event=>{event.preventDefault();const form=event.target,button=form.querySelector('[type=submit]');button.disabled=true;
        const fields=new FormData(form),body={request_id:requestId,expected_version:value.version,enabled:fields.has('enabled'),confirmed:fields.has('confirmed')};
        for(const name of ['project_url','college_url','faq_url','timezone','daily_time','mode'])body[name]=fields.get(name).trim();
        try{await api(`/applications/${appId}/monitor`,{method:'PUT',body:JSON.stringify(body)});dialog.close();toast('监控配置已保存');onChanged();}
        catch(error){dialog.querySelector('#monitor-form-error').textContent=error.message;button.disabled=false;}
      };
    }
    async function history(appId,offset=0){
      const account=authEpoch;
      const data=await api(`/applications/${appId}/monitor/history?offset=${offset}&limit=10`);
      if(account!==authEpoch||document.getElementById('app').hidden)return;
      open(`<h3>${esc(projectName(appId))} · 检查历史</h3><div class="monitor-history">${data.items.map(c=>`<article><div class="section-toolbar"><strong>${esc(status[c.status]||c.status)}</strong><small>${esc(time(c.created_at))}</small></div>${c.error?`<p class="error">${esc(c.error)}</p>`:''}${c.coverage.map(s=>`<p>${esc(s.roles.map(r=>roleName[r]||r).join(' / '))}：${s.status==='failed'?`获取失败 ${esc(s.error)}`:'已获取'} · ${link(s.url)}</p>`).join('')}${c.analyses.map(a=>`<button data-monitor-analysis="${esc(a.id)}">查看影响分析与证据</button>`).join('')}</article>`).join('')||'<p>暂无检查记录。启用后点击「立即检查」建立基线。</p>'}</div><div class="section-toolbar">${offset?`<button data-monitor-history="${esc(appId)}" data-offset="${offset-10}">上一页</button>`:'<span></span>'}${offset+10<data.total?`<button data-monitor-history="${esc(appId)}" data-offset="${offset+10}">下一页</button>`:''}</div>`);
    }
    async function detail(analysisId,notificationId=null){
      const account=authEpoch;
      const a=await api('/monitor-analyses/'+analysisId),r=a.result||{};
      if(account!==authEpoch||document.getElementById('app').hidden)return;
      open(`<h3>${esc(r.notification_title||status[a.status]||'影响分析')}</h3><p>${esc(r.notification_body||'尚无可通知的个人影响结论。')}</p><p class="status-pill">${esc(status[a.status]||a.status)}</p>${a.error?`<p class="error">${esc(a.error)}</p>`:''}
        <h4>个人影响与建议</h4><ul>${[...(r.affected_items||[]),...(r.suggested_actions||[]),...(r.conflicts||[]),...(r.missing_information||[])].map(x=>`<li>${esc(x)}</li>`).join('')||'<li>暂无确定结论</li>'}</ul>
        <h4>引用证据</h4>${(r.official_evidence_refs||[]).map(e=>{const page=a.evidence.find(p=>p.id===e.snapshot_id);return `<blockquote>${esc(e.quote)}<p>${page?link(page.url):'来源不可用'} · ${esc(page?time(page.fetched_at):'')}</p></blockquote>`;}).join('')||'<p class="hint">暂无已验证引用</p>'}
        <details><summary>变化片段与核查记录</summary><pre>${esc(JSON.stringify({changes:a.changes,personal_basis:r.personal_basis_refs,tools:a.trace},null,2))}</pre></details>
        ${notificationId?`<button class="primary" data-explain-notification="${esc(notificationId)}">在聊天助手中解释</button>`:''}
        ${['unknown','failed'].includes(a.status)?`<button data-monitor-retry="${esc(a.id)}">重新读取资料并分析</button>`:''}`);
    }
    async function render(root,projects){
      const current=++generation,values=await Promise.all(projects.map(p=>api(`/applications/${p.id}/monitor`)));
      if(current!==generation||getView()!=='monitor')return;
      root.innerHTML=`<p class="page-description">关注官方申请信息变化，仅在明确影响当前申请时发送站内信。每日自动检查或手动检查，不自动改动你的安排。</p><div class="monitor-grid">${projects.map((p,i)=>{const m=values[i];return `<article class="monitor-card"><div class="section-toolbar"><h2>${esc(projectName(p.id))}</h2><span class="status-pill">${esc(status[m.status]||m.status)}</span></div><p>${esc(p.cycle||'申请周期未记录')} · ${m.mode==='daily'?'每日 '+esc(m.daily_time)+' '+esc(m.timezone):'手动检查'}</p>${!m.analysis_available?'<p class="error">尚未配置模型：可检查网页，但个人影响分析不可用，不会发送站内信。</p>':''}<div class="monitor-sources">${m.sources.map(s=>`<p><strong>${esc(s.roles.map(r=>roleName[r]||r).join(' / '))}</strong> ${link(s.url)}</p>`).join('')||'<p class="hint">请先配置官网并确认来源</p>'}</div><p class="hint">上次全部页面获取成功：${esc(time(m.last_success_at,m.timezone))}<br>下次自动检查：${m.mode==='daily'?esc(time(m.next_due,m.timezone)):'仅手动'}</p><div class="monitor-actions"><button data-monitor-config="${esc(p.id)}">${m.version?'配置 / 暂停':'配置官网'}</button><button data-monitor-check="${esc(p.id)}" ${!m.enabled?'disabled':''}>立即检查</button><button data-monitor-history="${esc(p.id)}">检查历史</button></div></article>`;}).join('')||'<div class="workspace-empty">请先在项目与材料中添加申请项目。</div>'}</div>`;
    }
    async function unread(){if(document.getElementById('app').hidden)return;const epoch=authEpoch,value=await api('/inbox/unread-count');if(epoch!==authEpoch||document.getElementById('app').hidden)return;const button=document.getElementById('inbox-button');button.setAttribute('aria-label',`站内信箱，${value.count} 条未读`);document.getElementById('inbox-dot').hidden=!value.count;}
    async function inbox(root,offset=0){
      const current=++generation,data=await api(`/inbox?offset=${offset}&limit=20`);if(current!==generation||getView()!=='inbox')return;
      root.innerHTML=`<p class="page-description">只展示已确认对你有影响的官网变化，不包含普通页面变化和待核实结果。</p><div class="inbox-list">${data.items.map(n=>`<button class="inbox-card ${n.read_at?'is-read':''}" data-inbox-open="${esc(n.id)}"><div class="section-toolbar"><strong>${esc(n.result.notification_title||n.message)}</strong><span>${n.read_at?'已读':'未读'}</span></div><p>${esc(projectName(n.target_id))} · ${esc(direction[n.result.direction]||'个人影响')}</p><p>${esc(n.result.notification_body||'')}</p><small>${esc(time(n.created_at))}</small></button>`).join('')||'<div class="workspace-empty">暂无影响通知。未确认影响的变化可在官网监控的检查历史中查看。</div>'}</div><div class="section-toolbar">${offset?`<button data-inbox-offset="${offset-20}">上一页</button>`:'<span></span>'}${offset+20<data.total?`<button data-inbox-offset="${offset+20}">下一页</button>`:''}</div>`;
    }
    document.addEventListener('click',async event=>{const b=event.target.closest('button');if(!b||document.getElementById('app').hidden)return;
      try{
        if(b.dataset.monitorConfig)await config(b.dataset.monitorConfig);
        if(b.dataset.monitorCheck){b.disabled=true;const r=await api(`/applications/${b.dataset.monitorCheck}/monitor/checks`,{method:'POST',body:JSON.stringify({request_id:crypto.randomUUID()})});toast(`检查已提交：${status[r.status]||r.status}`);onChanged();}
        if(b.dataset.monitorHistory)await history(b.dataset.monitorHistory,Number(b.dataset.offset||0));
        if(b.dataset.monitorAnalysis)await detail(b.dataset.monitorAnalysis);
        if(b.dataset.monitorRetry){b.disabled=true;await api(`/monitor-analyses/${b.dataset.monitorRetry}/retry`,{method:'POST',body:JSON.stringify({request_id:crypto.randomUUID()})});dialog.close();toast('已提交重新分析');onChanged();}
        if(b.dataset.inboxOpen){const n=await api('/inbox/'+b.dataset.inboxOpen);await detail(n.analysis_id,n.id);await api('/inbox/'+b.dataset.inboxOpen+'/read',{method:'POST'});await unread();}
        if(b.dataset.explainNotification){dialog.close();onForm(false);window.dispatchEvent(new CustomEvent('applyops:explain-notification',{detail:{id:b.dataset.explainNotification}}));}
        if(b.dataset.inboxOffset!==undefined)await inbox(document.getElementById('ops-content'),Number(b.dataset.inboxOffset));
      }catch(error){toast(error.message);b.disabled=false;}
    });
    document.getElementById('logout').addEventListener('click',()=>{generation++;authEpoch++;dialog.close();document.getElementById('inbox-dot').hidden=true;});
    return {render,inbox,unread};
  };
})();
