/* Full chat-only creation on the isolated chat_actions_app, with real HTTP/SSE. */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.resolve('.tmp-workspace-browser/node_modules/playwright'));
const base = 'http://127.0.0.1:18035';
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
    page.on('pageerror', e=>errors.push(e.message));
    await page.goto(base+'/v3');
    await page.locator('#auth-mode').click();
    await page.locator('#email').fill(`chat-actions-${Date.now()}@test.local`);
    await page.locator('#password').fill('isolated-browser-test-password');
    await page.locator('#auth-form [type=submit]').click();
    await page.locator('.overview-stats').waitFor();
    await page.locator('[data-ops-view=chat]').click();
    await page.getByRole('heading',{name:'聊天助手',exact:true}).waitFor();
    async function send(text) {
      const before = await page.locator('.message.assistant').count();
      await page.locator('#chat-input').fill(text);
      await page.locator('#chat-form [type=submit]').click();
      await page.waitForFunction(n=>document.querySelectorAll('.message.assistant').length>n,before);
      await page.waitForFunction(()=>!document.querySelector('#chat-form button').disabled);
      return page.locator('.message.assistant').last().innerText();
    }
    assert.match(await send('添加 CMU MSAII，2027 Fall，并新增修改SOP待办和见推荐人日程'), /尚未保存/);
    assert.equal((await (await page.request.get(base+'/api/v3/snapshot')).json()).applications.length,0);
    // Reload between draft and confirmation: the pending state is server-owned.
    await page.reload();
    await page.locator('.overview-stats').waitFor();
    await page.locator('[data-ops-view=chat]').click();
    await page.locator('#conversations .conversation').first().click();
    await page.locator('.message.assistant').first().waitFor();
    assert.match(await send('确认添加'), /已确认保存/);
    const saved = await (await page.request.get(base+'/api/v3/snapshot')).json();
    assert.equal(saved.applications.length,1);
    assert.equal(saved.tasks.length,1);
    assert.equal(saved.calendar_events.length,1);
    await page.locator('[data-ops-view=dashboard]').click();
    await page.locator('[data-project-card]').waitFor();
    assert.match(await page.locator('.application-card').innerText(), /CMU.*MSAII/);
    await page.locator('[data-ops-view=tasks]').click();
    await page.getByText('修改 SOP',{exact:true}).waitFor();
    await page.locator('[data-ops-view=calendar]').click();
    const month = saved.tasks[0].due_date.slice(0,7);
    for(let i=0;i<24;i++) {
      const visible = await page.locator('.calendar-toolbar strong').innerText();
      if(visible===month)break;
      await page.locator(`[data-calendar-shift="${visible<month?'1':'-1'}"]`).click();
      await page.waitForFunction(old=>document.querySelector('.calendar-toolbar strong')?.textContent!==old,visible);
    }
    await page.locator('.calendar-entry.task').waitFor();
    await page.getByRole('button',{name:/见推荐人/}).waitFor();
    await page.locator('[data-ops-view=chat]').click();
    assert.match(await send('帮我查 CMU MSML 的官网截止日期'), /候选官网链接/);
    await page.locator('.message.assistant').last().getByRole('link',{name:'查看候选页面'}).waitFor();
    // Pending website consent is also durable across a browser reload.
    await page.reload();
    await page.locator('.overview-stats').waitFor();
    await page.locator('[data-ops-view=chat]').click();
    await page.locator('#conversations .conversation').first().click();
    await page.locator('.message.assistant').first().waitFor();
    assert.match(await send('确认第1个'), /官网原文/);
    await page.locator('.message.assistant').last().getByRole('link',{name:'官网页面'}).waitFor();
    assert.equal((await (await page.request.get(base+'/api/v3/snapshot')).json()).applications.length,1);
    fs.mkdirSync('data/chat-actions-screenshots',{recursive:true});
    await page.screenshot({path:'data/chat-actions-screenshots/chat-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:'data/chat-actions-screenshots/chat-mobile.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS: chat-only project + todo + event, website candidate consent after reload, cited fixture answer, no search writes, overview/todo/calendar synchronization, mobile width, no JS errors');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
