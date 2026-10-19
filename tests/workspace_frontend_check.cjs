const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
  constructor(){this.innerHTML='';this.hidden=false;this.dataset={};this.classList={toggle(){}};this.children=[];this.listeners={};}
  querySelector(){return new Element();}querySelectorAll(){return [];}addEventListener(k,v){this.listeners[k]=v;}
  append(e){this.children.push(e);}prepend(e){this.children.unshift(e);}showModal(){this.open=true;}close(){this.open=false;}
  insertAdjacentHTML(position,html){this.innerHTML=position==='afterbegin'?html+this.innerHTML:this.innerHTML+html;}
}
const elements=new Map(),handlers={};
const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
  querySelectorAll(){return [];},addEventListener(k,v){handlers[k]=v;},createElement(){return new Element();}};
const projects=[{id:'p',version:1,published_version:1,index_status:'ready',draft:{university:'U <img src=x>',program:'CS',intake:'2027 Fall'}}];
const sandbox={document,console,URL,crypto:{randomUUID(){return 'id';}},setInterval(){},localStorage:{getItem(){return null;},setItem(){}},
  fetch:async path=>({ok:true,json:async()=>path.endsWith('/project-library')?projects:path.endsWith('/project-imports')?[]:
    path.endsWith('/calendar-events')?{events:[],reminders:[],email_available:false}:[]})};
let source=fs.readFileSync('opportunity_agent/v2/web/workspace.js','utf8').replace('  // Poll only while authenticated','  globalThis.workspaceTest={show,state,safeLink,importEditor};\n  // Poll only while authenticated');
vm.createContext(sandbox);vm.runInContext(source,sandbox);
(async()=>{
  const markup=fs.readFileSync('opportunity_agent/v2/web/index.html','utf8');
  const loginMarkup=markup.slice(markup.indexOf('id="auth"'),markup.indexOf('id="app"'));
  assert.ok(!loginMarkup.includes('data-workspace='),'workspace navigation must not appear before login');
  assert.ok(markup.slice(markup.indexOf('id="app"')).includes('class="workspace-nav"'));
  const t=sandbox.workspaceTest;
  t.importEditor();
  assert.ok(elements.get('workspace-dialog').innerHTML.includes('第一步：提供项目网址'));
  assert.ok(!elements.get('workspace-dialog').innerHTML.includes('name="university"'));
  assert.ok(elements.get('workspace-dialog').innerHTML.includes('提取并自动填表'));
  assert.ok(!t.safeLink('javascript:alert(1)','x').includes('href='));
  assert.ok(!t.safeLink('https://user:pass@example.edu','x').includes('href='));
  await t.show('library');
  const html=elements.get('workspace-panel').innerHTML;
  assert.ok(!html.includes('<img'));assert.ok(html.includes('加入申请'));assert.ok(html.includes('根据全部经历推荐'));
  await t.show('calendar');assert.ok(elements.get('workspace-panel').innerHTML.includes('邮件未配置'));
  assert.ok(elements.get('workspace-panel').innerHTML.includes('calendar-grid'));
  assert.ok(handlers['workspace-results']);
  await t.show('chat');
  assert.equal(elements.get('workspace-panel').hidden,true);
  assert.equal(elements.get('chat-panel').hidden,false);
  elements.get('app').hidden=true;
  await t.show('library');
  assert.equal(t.state.tab,'chat','logged out users cannot open workspace');
  handlers['workspace-reset']();
  assert.equal(t.state.projects.length,0);
  elements.get('app').hidden=false;
  await t.show('todo');
  handlers['workspace-chat']();
  assert.equal(t.state.tab,'chat');
  console.log('workspace navigation, project rendering, calendar, SMTP disclosure and link escaping passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
