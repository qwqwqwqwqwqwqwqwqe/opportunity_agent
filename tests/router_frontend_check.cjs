// Unit-level DOM/EventSource harness, not a substitute for browser acceptance.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
class Element {
  constructor(){this.children=[];this.textContent='';this.innerHTML='';this.value='';this.hidden=false;this.classList={toggle(){},add(){},remove(){}};}
  append(child){this.children.push(child);this.lastElementChild=child;}
  querySelector(){return this.button||(this.button=new Element());}
  querySelectorAll(){return [];}
}
const elements = new Map(), streams=[], intervals=[];
let cleared = 0;
const document = {
  getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
  createElement(){return new Element();}, querySelectorAll(){return [];}
};
class EventSource {
  constructor(){this.handlers={};streams.push(this);}
  addEventListener(name,handler){this.handlers[name]=handler;}
  close(){this.closed=true;}
}
const sandbox = {document,EventSource,console,URL,encodeURIComponent,
  setInterval(callback){intervals.push(callback);return intervals.length;},clearInterval(){cleared++;},
  setTimeout(){return 1;},clearTimeout(){},
  async fetch(url){
    const body=url.includes('/runs/')?{id:'r1',status:'failed',trace_id:'a'.repeat(32),error:'Router failed'}:
      url.endsWith('/profile')?{payload:{},version:1}:[];
    return {ok:true,status:200,json:async()=>body};
  }
};
const source=fs.readFileSync(path.join(__dirname,'../opportunity_agent/v2/web/app.js'),'utf8')
  .replace('  loadInitial();','  globalThis.routerTest={followRun,state,ui};');
vm.createContext(sandbox);vm.runInContext(source,sandbox);
async function check(){
  const {followRun,state,ui}=sandbox.routerTest;
  state.conversationId='c';state.running=true;
  let followed=followRun('r1','CMU MSCS','c');
  streams[0].handlers.trace_started({data:JSON.stringify({payload:{trace_id:'a'.repeat(32)}})});
  streams[0].handlers.run_failed({data:JSON.stringify({payload:{error:'Router failed'}})});
  await followed;
  assert.equal(document.getElementById('run-status').textContent,'执行失败');
  assert.equal(ui.steps.textContent,'执行失败');
  assert.ok(ui.messages.children[0].innerHTML.includes('Router failed'));
  assert.equal(ui.messages.lastElementChild.children[0].textContent,'重试这条消息');
  assert.ok(document.getElementById('run-info').innerHTML.includes('a'.repeat(32)));
  assert.equal(state.running,false);assert.equal(cleared,1);
  state.running=true;
  followed=followRun('r2','查询截止日期','c');
  await intervals.at(-1)(); // No SSE event arrives: terminal polling must recover.
  await followed;
  assert.equal(state.running,false);assert.equal(cleared,2);
  assert.equal(document.getElementById('run-status').textContent,'执行失败');
  console.log('frontend failure persistence, trace display and silent-SSE recovery passed');
}
const failTimer=setTimeout(()=>{console.error('frontend harness did not reach a terminal state',sandbox.routerTest.state,streams.length,intervals.length);process.exitCode=1;},2000);
check().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>clearTimeout(failTimer));
