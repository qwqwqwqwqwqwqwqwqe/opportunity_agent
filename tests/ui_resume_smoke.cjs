// Actual browser interaction. Provider responses are mock, documents/parsers are real.
const {chromium}=require('playwright'),{spawn}=require('child_process'),{once}=require('events');
const assert=require('assert/strict'),fs=require('fs'),os=require('os'),path=require('path');
(async()=>{
 const server=spawn('python',['-B','tests/ui_resume_fixture_server.py'],{cwd:path.resolve(__dirname,'..'),windowsHide:true,stdio:['pipe','pipe','pipe']});
 server.stderr.on('data',data=>process.stderr.write(data));
 let browser;
 try {
   const fixture=await new Promise((resolve,reject)=>{let data='';server.stdout.on('data',chunk=>{data+=chunk;if(data.includes('\n'))resolve(JSON.parse(data.split('\n')[0]))});server.on('error',reject);server.on('exit',code=>reject(Error('fixture '+code)))});
   const url='http://127.0.0.1:'+fixture.port;
   const executablePath=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Google/Chrome/Application/chrome.exe'].find(fs.existsSync);
   browser=await chromium.launch({headless:true,...(executablePath?{executablePath}:{})});
   const a=await browser.newContext({viewport:{width:1280,height:850}}),b=await browser.newContext({viewport:{width:1280,height:850}});
   const page=await a.newPage(),other=await b.newPage(),errors=[];
   for(const p of [page,other]){p.setDefaultTimeout(12000);p.on('pageerror',error=>{errors.push(error.message);console.error('page error:',error.message)})}
   await page.goto(url);await page.locator('#onboarding-overlay.visible').waitFor();
   const chooser=page.waitForEvent('filechooser');await page.locator('#onboarding-import-resume').click();
   await (await chooser).setFiles({name:'synthetic.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:Buffer.from(fixture.docx,'base64')});
   await page.locator('#onboarding-overlay.visible').waitFor({state:'hidden'});
   await page.locator('.resume-field[data-field=school] .resume-value').waitFor();
   assert.equal(await page.locator('.resume-field[data-field=school] .resume-value').inputValue(),'Synthetic University');
   await page.locator('[data-resume-action=retry]').first().waitFor();
   const list=await (await page.request.get(url+'/api/conversations')).json(),sid=list.conversations[0].session_id;
   const before=await (await page.request.get(url+'/api/conversations/'+sid)).json();
   assert.equal(before.state.profile.school,null);
   const school=page.locator('.resume-field[data-field=school]');
   await school.locator('details summary').click();await school.getByText('synthetic',{exact:false}).waitFor();
   await school.locator('.resume-value').fill('Reviewed University');
   await page.waitForFunction(()=>document.querySelector('.resume-save-note')?.textContent.includes('草稿已保存'));
   const exp=page.locator('.resume-experience').first();
   await exp.locator('textarea').nth(5).fill('精度 92%，延迟下降 12%，完整描述');
   await page.waitForFunction(()=>document.querySelector('.resume-save-note')?.textContent.includes('草稿已保存'));
   await page.reload();await page.locator('#onboarding-overlay.visible').waitFor();await page.locator('#cancel-onboarding').click();
   await page.locator('.resume-field[data-field=school] .resume-value').waitFor();
   assert.equal(await page.locator('.resume-field[data-field=school] .resume-value').inputValue(),'Reviewed University');
   await other.goto(url);await other.locator('#onboarding-overlay.visible').waitFor();await other.locator('#cancel-onboarding').click();
   await other.locator('.resume-field[data-field=school] .resume-value').waitFor();
   assert.equal(await other.locator('.resume-field[data-field=school] .resume-value').inputValue(),'Reviewed University');
   // Draft document blocks never enter browser persistence.
   assert(!await page.evaluate(()=>Object.values(localStorage).some(v=>v.includes('原始简历全量')||v.includes('cloud_consent')||v.includes('paragraph_blocks'))));
   const budget=page.locator('.resume-field[data-field=budget] [data-part=amount]');
   await budget.fill('300000');await page.locator('[data-resume-action=confirm-plan]').click();
   await page.getByText('请先修正格式有误的字段，再保存或确认。',{exact:true}).waitFor();
   await budget.fill('');await page.waitForFunction(()=>document.querySelector('.resume-save-note')?.textContent.includes('草稿已保存'));
   const shots=fs.mkdtempSync(path.join(os.tmpdir(),'resume-ui-shots-'));
   await page.setViewportSize({width:390,height:844});
   await page.locator('[data-resume-action=confirm-plan]').scrollIntoViewIfNeeded();
   const layout=await page.evaluate(()=>({w:innerWidth,scroll:document.documentElement.scrollWidth,side:document.querySelector('aside.panel').clientHeight,content:document.querySelector('aside.panel').scrollHeight}));
   assert(layout.scroll<=layout.w+1,JSON.stringify(layout));assert(layout.content>layout.side);
   await page.screenshot({path:path.join(shots,'mobile-review.png'),fullPage:true});
   await page.setViewportSize({width:1280,height:850});
   await page.locator('[data-resume-action=confirm-plan]').click();
   await page.getByText('资料已确认',{exact:false}).first().waitFor();
   await page.locator('.timeline-node').first().waitFor();
   const after=await (await page.request.get(url+'/api/conversations/'+sid)).json();
   assert.equal(after.state.profile.school,'Reviewed University');
   assert.equal(after.state.profile.research_experiences.length,1);
   assert(after.state.profile.research_experiences[0].includes('完整描述'));
   assert.equal(after.state.profile.project_experiences.length,0);
   await page.locator('#edit-profile').click();
   assert((await page.locator('[name=research_experiences]').inputValue()).includes('精度 92%'));
   assert.equal(await page.locator('[name=project_experiences]').inputValue(),'');
   await page.locator('#cancel-onboarding').click();
   // Upload scan and check separate cloud authorization (mock cloud provider).
   await page.locator('#new-chat').click();
   await page.locator('#onboarding-overlay.visible').waitFor();
   const scanChooser=page.waitForEvent('filechooser');await page.locator('#onboarding-import-resume').click();
   await (await scanChooser).setFiles({name:'scan.pdf',mimeType:'application/pdf',buffer:Buffer.from(fixture.scan,'base64')});
   await page.locator('[data-resume-action=cloud]').waitFor();
   page.once('dialog',dialog=>dialog.accept());
   await page.locator('[data-resume-action=cloud]').click();
   await page.locator('.resume-field[data-field=school] .resume-value').waitFor();
   await page.screenshot({path:path.join(shots,'desktop-review.png'),fullPage:true});
   page.once('dialog',dialog=>dialog.accept());await page.locator('[data-resume-action=delete]').click();
   await page.locator('.resume-field').first().waitFor({state:'hidden'});
   assert.deepEqual(errors,[]);
   console.log(JSON.stringify({passed:true,checks:['upload without onboarding','real DOCX extraction','draft edits and evidence','cross-browser restore','invalid input guard','small-screen scrolling','confirm and timeline','classified experiences','scan consent','cancel'],screenshots:shots}));
 } finally {
   if(browser)await browser.close();
   if(server.exitCode===null){const done=once(server,'exit');server.stdin.end('\n');const timer=setTimeout(()=>server.kill(),4000);await done;clearTimeout(timer)}
 }
})().catch(error=>{console.error(error);process.exitCode=1});
