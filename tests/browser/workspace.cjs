/* Run against an isolated local test server. Uses installed Edge on Windows. */
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||path.resolve('.tmp-workspace-browser/node_modules/playwright'));
const base=process.env.APPLYOPS_BROWSER_URL||'http://127.0.0.1:18032';
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.EDGE_EXECUTABLE||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try{
    const context=await browser.newContext({viewport:{width:1440,height:1050},reducedMotion:'no-preference'}),page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/v3');
    await page.locator('#auth-mode').click();
    await page.locator('#email').fill(`browser-${Date.now()}@test.local`);
    await page.locator('#password').fill('browser-test-password-20261020');
    await page.locator('#auth-form [type=submit]').click();
    await page.locator('.overview-stats').waitFor();
    assert.equal(await page.locator('#ops-content #new-project').count(),0);
    const date=(await (await page.request.get(base+'/api/v3/snapshot')).json()).today;
    async function addProject(name,materials){
      await page.locator('[data-ops-view=projects]').click();await page.locator('#new-project').click();
      await page.locator('[name=university]').fill('CMU');await page.locator('[name=name]').fill(name);
      await page.locator('[name=cycle]').fill('2027 Fall');await page.locator('[name=deadline_date]').fill('2027-02-01');
      if(materials){
        await page.locator('[data-add-requirement=CV]').click();
        await page.locator('.material-slot select').first().selectOption('ready');
        await page.locator('[data-add-requirement=letters]').click();
        const letters=page.locator('.requirement-row').nth(1);
        await letters.locator('.material-slot select').nth(0).selectOption('ready');
        await letters.locator('.material-slot select').nth(1).selectOption('ready');
      }
      await page.locator('[name=language_policy]').selectOption('none');
      await page.locator('[name=notes]').fill('浏览器测试：用户确认的材料要求');
      await page.locator('#project-entry [type=submit]').click();
      await page.locator('.application-card').filter({hasText:name}).waitFor();
    }
    await addProject('MSAII',true);
    const card=page.locator('.application-card').filter({hasText:'MSAII'});
    assert.match(await card.innerText(),/1 \/ 2/);assert.match(await card.innerText(),/已准备 2 \/ 3/);
    assert.match(await card.innerText(),/2027-02-01/);
    fs.mkdirSync('data/workspace-screenshots',{recursive:true});
    await page.screenshot({path:'data/workspace-screenshots/overview.png',fullPage:true});
    await addProject('MSCS',false);
    await page.locator('[data-ops-view=tasks]').click();await page.locator('.todo-project-card').first().waitFor();
    assert.equal(await page.locator('.todo-project-card').count(),2);
    const projectCard=page.locator('.todo-project-card').filter({has:page.getByRole('heading',{name:'MSAII',exact:true})});
    async function addTask(title){
      await projectCard.locator('.add-todo').click();
      await page.locator('#todo-form [name=title]').fill(title);await page.locator('#todo-form [name=due_date]').fill(date);
      await page.locator('#todo-form [name=show_in_calendar]').check();await page.locator('#todo-form [type=submit]').click();
      await projectCard.locator('.todo-items .todo-row').filter({hasText:title}).waitFor();
    }
    await addTask('完成 SOP 最终版');await addTask('联系第三位推荐人');
    await projectCard.locator('.add-todo').click();await page.locator('#todo-form [name=title]').fill('检查日期校验');
    await page.locator('#todo-form [name=show_in_calendar]').check();await page.locator('#todo-form [type=submit]').click();
    assert.match(await page.locator('#todo-error').innerText(),/先填写截止日期/);await page.locator('[data-close=todo-dialog]').click();
    const first=projectCard.locator('.todo-items .todo-row').filter({hasText:'完成 SOP 最终版'}),id=await first.getAttribute('data-todo-row');
    await first.locator('.todo-circle').click();
    await page.waitForFunction(id=>!document.querySelector(`.todo-items [data-todo-row="${id}"]`),id);
    await page.waitForFunction(()=>document.querySelector('[data-task-card] .task-count')?.textContent==='1 项待办');
    let events=await (await page.request.get(`${base}/api/v3/calendar?from=${date}&to=${date}`)).json();
    assert(!events.some(e=>e.id===id));assert(events.some(e=>e.title==='联系第三位推荐人'));
    await page.locator('#undo-task').click();await projectCard.locator(`.todo-items [data-todo-row="${id}"]`).waitFor();
    events=await (await page.request.get(`${base}/api/v3/calendar?from=${date}&to=${date}`)).json();assert(events.some(e=>e.id===id));
    await projectCard.locator(`.todo-items [data-todo-row="${id}"] .todo-circle`).click();
    await page.waitForFunction(id=>!document.querySelector(`.todo-items [data-todo-row="${id}"]`),id);
    await page.waitForFunction(()=>document.querySelector('[data-task-card] .completed-tasks summary')?.textContent==='已完成 1 项');
    await page.screenshot({path:'data/workspace-screenshots/todos.png',fullPage:true});
    await page.reload();await page.locator('.overview-stats').waitFor();
    assert.match(await page.locator('.application-card').filter({hasText:'MSAII'}).innerText(),/1 \/ 2/);
    await page.locator('[data-ops-view=calendar]').click();await page.locator('.calendar-grid').waitFor();
    await page.screenshot({path:'data/workspace-screenshots/calendar.png',fullPage:true});
    await page.locator('[data-ops-view=projects]').click();await page.locator('#new-project').click();
    await page.screenshot({path:'data/workspace-screenshots/project-form.png',fullPage:true});
    await page.locator('[data-project-cancel]').first().click();
    const row=page.locator('.project-register-row').filter({hasText:'MSAII'});
    await row.click();await page.locator('#project-entry').waitFor();
    const materialRow=page.locator('.requirement-row').filter({has:page.locator('input[value="推荐信"]')});
    await materialRow.locator('.material-slot select').nth(2).selectOption('ready');
    await page.locator('#project-entry [type=submit]').click();await page.locator('.overview-stats').waitFor();
    assert.match(await page.locator('.application-card').filter({hasText:'MSAII'}).innerText(),/2 \/ 2/);
    assert.equal(await page.locator('.application-card').filter({hasText:'MSAII'}).getByText('语言成绩',{exact:true}).count(),0);
    await page.locator('.overview-stat[data-open-inspector]').click();
    assert(await page.locator('#ops-inspector').evaluate(el=>el.open));
    await page.locator('[data-close=ops-inspector]').click();
    await page.locator('[data-ops-view=dashboard]').click();await page.locator('.overview-stats').waitFor();
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:'data/workspace-screenshots/mobile.png',fullPage:true});
    assert.deepEqual(errors,[]);console.log('PASS: project form, school directory, completion, undo, calendar sync, persistence, mobile layout; no page errors');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
