/* Uses a test-only SQLite database and offline worker driver, never a real site/model. */
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const {execFileSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||path.resolve('.tmp-workspace-browser/node_modules/playwright'));
const base=process.env.APPLYOPS_BROWSER_URL||'http://127.0.0.1:18033';
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.EDGE_EXECUTABLE||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try{
    const context=await browser.newContext({viewport:{width:1440,height:1050}}),page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    const email=`monitor-browser-${Date.now()}@test.local`;
    await page.goto(base+'/v3');await page.locator('#auth-mode').click();
    await page.locator('#email').fill(email);await page.locator('#password').fill('browser-test-password-20261023');
    await page.locator('#auth-form [type=submit]').click();await page.locator('.overview-stats').waitFor();
    assert.equal(await page.locator('[data-ops-view=plans]').count(),0);
    await page.locator('[data-ops-view=projects]').click();await page.locator('#new-project').click();
    await page.locator('[name=university]').fill('Example University');await page.locator('[name=name]').fill('MSCS');
    await page.locator('[name=cycle]').fill('2027 Fall');await page.locator('[name=deadline_date]').fill('2027-02-01');
    await page.locator('[name=language_policy]').selectOption('none');await page.locator('#project-entry [type=submit]').click();
    await page.locator('.application-card').waitFor();
    await page.locator('[data-ops-view=monitor]').click();await page.locator('[data-monitor-config]').click();
    await page.locator('[name=project_url]').fill('https://example.edu/program');
    await page.locator('#monitor-config-form [name=enabled]').check();await page.locator('#monitor-config-form [name=confirmed]').check();
    await page.locator('#monitor-config-form [type=submit]').click();await page.locator('.monitor-dialog').waitFor({state:'hidden'});
    await page.locator('.monitor-card').waitFor();
    const run=stage=>execFileSync(path.resolve('.venv-research/Scripts/python.exe'),['tests/browser/monitor_tick.py',email,stage],{
      cwd:process.cwd(),env:{...process.env,DATABASE_URL:'sqlite+aiosqlite:///./.tmp-workspace-browser/monitor-browser.db',OPPORTUNITY_AGENT_DISABLE_DOTENV:'1'},timeout:30000});
    run('before');await page.locator('#ops-refresh').click();await page.getByText('已建立基线',{exact:true}).waitFor();
    assert.equal(await page.locator('#inbox-dot').isVisible(),false);
    run('after');await page.locator('#ops-refresh').click();await page.getByText('有个人影响',{exact:true}).waitFor();
    await page.locator('#inbox-dot').waitFor({state:'visible'});
    fs.mkdirSync('data/monitor-screenshots',{recursive:true});await page.screenshot({path:'data/monitor-screenshots/monitor.png',fullPage:true});
    await page.locator('#inbox-button').click();await page.locator('.inbox-card').waitFor();
    assert.match(await page.locator('.inbox-card').innerText(),/申请截止日期有变化/);
    await page.screenshot({path:'data/monitor-screenshots/inbox.png',fullPage:true});
    await page.locator('.inbox-card').click();await page.locator('.monitor-dialog blockquote').waitFor();
    await page.locator('#inbox-dot').waitFor({state:'hidden'});
    await page.screenshot({path:'data/monitor-screenshots/evidence.png',fullPage:true});
    await page.locator('[data-monitor-close]').click();await page.getByText('已读',{exact:true}).waitFor();
    await page.locator('[data-ops-view=monitor]').click();await page.locator('[data-monitor-history]').click();
    await page.locator('[data-monitor-analysis]').waitFor();await page.locator('[data-monitor-close]').click();
    await page.setViewportSize({width:390,height:844});await page.screenshot({path:'data/monitor-screenshots/mobile.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.deepEqual(errors,[]);console.log('PASS: monitoring config, baseline, offline change/Agent/real inbox, red dot, explicit read, evidence, history, mobile, no JS errors');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
