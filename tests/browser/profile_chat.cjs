/* Run against the isolated browser DB with no configured model or real web fetch. */
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
const {execFileSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||path.resolve('.tmp-workspace-browser/node_modules/playwright'));
const base=process.env.APPLYOPS_BROWSER_URL||'http://127.0.0.1:18034';
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.EDGE_EXECUTABLE||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try{
    const context=await browser.newContext({viewport:{width:1440,height:1050}}),page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    const email=`profile-browser-${Date.now()}@test.local`;
    await page.goto(base+'/v3');await page.locator('#auth-mode').click();
    await page.locator('#email').fill(email);await page.locator('#password').fill('browser-test-password-20261023');
    await page.locator('#auth-form [type=submit]').click();await page.locator('.overview-stats').waitFor();
    await page.locator('[data-ops-view=chat]').click();await page.locator('#chat-welcome').waitFor();
    fs.mkdirSync('data/profile-chat-screenshots',{recursive:true});await page.screenshot({path:'data/profile-chat-screenshots/welcome.png',fullPage:true});
    async function send(text){const before=await page.locator('.message.assistant').count();await page.locator('#chat-input').fill(text);await page.locator('#chat-form [type=submit]').click();await page.waitForFunction(n=>document.querySelectorAll('.message.assistant').length>n,before);await page.waitForFunction(()=>!document.querySelector('#chat-form button').disabled);return page.locator('.message.assistant').last().innerText();}
    assert.match(await send('我的托福是105分'),/尚未保存/);
    assert.equal((await (await page.request.get(base+'/api/v1/profile')).json()).payload.toefl_score,undefined);
    await page.locator('.profile-chat-header [data-open-inspector=profile]').click();await page.locator('[data-tab=approvals]').click();
    await page.locator('[data-decision=accept]').click();await page.waitForFunction(()=>!document.querySelector('#approval-count').textContent.trim());
    await page.locator('[data-close=ops-inspector]').click();
    assert.match(await send('查看我的画像'),/105/);
    assert.match(await send('你好'),/个人画像/);
    await page.screenshot({path:'data/profile-chat-screenshots/conversation.png',fullPage:true});
    await page.locator('[data-ops-view=projects]').click();await page.locator('#new-project').click();
    await page.locator('[name=university]').fill('Example University');await page.locator('[name=name]').fill('MSCS');
    await page.locator('[name=cycle]').fill('2027 Fall');await page.locator('[name=deadline_date]').fill('2027-02-01');
    await page.locator('[name=language_policy]').selectOption('none');await page.locator('#project-entry [type=submit]').click();await page.locator('.application-card').waitFor();
    await page.locator('[data-ops-view=chat]').click();assert.match(await send('我目前有哪些申请项目？'),/MSCS/);
    await page.locator('[data-ops-view=monitor]').click();await page.locator('[data-monitor-config]').click();
    await page.locator('[name=project_url]').fill('https://example.edu/program');await page.locator('#monitor-config-form [name=enabled]').check();await page.locator('#monitor-config-form [name=confirmed]').check();
    await page.locator('#monitor-config-form [type=submit]').click();await page.locator('.monitor-dialog').waitFor({state:'hidden'});
    for(const stage of ['before','after'])execFileSync(path.resolve('.venv-research/Scripts/python.exe'),['tests/browser/monitor_tick.py',email,stage],{env:{...process.env,DATABASE_URL:'sqlite+aiosqlite:///./.tmp-workspace-browser/monitor-browser.db',OPPORTUNITY_AGENT_DISABLE_DOTENV:'1'},timeout:30000});
    await page.locator('#inbox-button').click();await page.locator('.inbox-card').click();
    await page.locator('[data-explain-notification]').click();await page.locator('.profile-chat').waitFor({state:'visible'});
    await page.waitForFunction(()=>document.querySelector('.message.assistant:last-child')?.textContent.includes('历史分析'));
    await page.waitForFunction(()=>!document.querySelector('#chat-form button').disabled);
    assert.match(await page.locator('.message.assistant').last().innerText(),/December 15/);
    await page.screenshot({path:'data/profile-chat-screenshots/notification.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});await page.locator('#messages').evaluate(el=>el.scrollTop=el.scrollHeight);await page.screenshot({path:'data/profile-chat-screenshots/mobile.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.reload();await page.locator('.overview-stats').waitFor();await page.locator('[data-ops-view=chat]').click();
    await page.locator('#conversations .conversation').first().click();await page.locator('.message-audit').first().waitFor();
    assert.deepEqual(errors,[]);console.log('PASS: V3 profile chat styling, welcome, profile approval, owned queries, ordinary fallback, notification explanation link, history, mobile, no JS errors');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
