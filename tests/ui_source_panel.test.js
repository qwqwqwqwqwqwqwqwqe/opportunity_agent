const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('opportunity_agent/v2/web/app.js','utf8');
const nodes=new Map();
nodes.set('run-info',{after(node){nodes.set(node.id,node);}});
const context={URL,Map,document:{createElement(){return {id:'',innerHTML:''};}},$:id=>nodes.get(id)};
vm.createContext(context);
vm.runInContext(source.match(/^  const escape =.*$/m)[0]+'\n'+source.slice(source.indexOf('  function showSources('),source.indexOf('  function showRunFailure(')),context);
context.run={source_progress:[
 {url:'javascript:alert(1)',title:'unsafe'},
 {url:'https://brown.edu/apply',title:'<img src=x onerror=alert(1)>',source_status:'read_ok'},
 {url:'https://brown.edu/apply',source_status:'verification_failed',field:'deadline',value:'2027-01-15',reason:'program_mismatch'},
 {url:'https://brown.edu/apply',source_status:'verified',field:'gre_policy',value:'required'}]};
vm.runInContext('showSources(run)',context);
const html=nodes.get('official-source-status').innerHTML;
assert(!html.includes('javascript:'));
assert(!html.includes('<img'));
assert(html.includes('&lt;img'));
assert(html.includes('program_mismatch') && html.includes('2027-01-15') && html.includes('required'));
assert.equal((html.match(/<a /g)||[]).length,1);
assert(html.includes('source-page'));
assert(!html.startsWith('<details open>'));
vm.runInContext('showSources(null)',context);
assert.equal(nodes.get('official-source-status').innerHTML,'');
console.log('PASS: link grouping, all stages, value/reason, escaping, unsafe URL rejection, reset');
