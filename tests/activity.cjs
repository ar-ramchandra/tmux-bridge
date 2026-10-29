const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const root=path.join(__dirname,'..');
const html=fs.readFileSync(path.join(root,'gateway.py'),'utf8').split('HTML = r"""')[1].split('"""')[0];
const script=fs.readFileSync(path.join(root,'tmux_bridge_assets/activity.js'),'utf8');
(async()=>{
 const browser=await browserType.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  let events=[],seq=0;
  const event=kind=>({version:1,id:String(++seq),seq,server:'test',session:'work',provider:'codex',thread_id:'thread',turn_id:'turn',time:Date.now()/1000,kind});
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.hostname==='cdn.jsdelivr.net')return route.continue();
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
   if(url.pathname==='/activity.js')return route.fulfill({contentType:'application/javascript',body:script});
   let data=[];
   if(url.pathname==='/api/activity/snapshot')data={cursor:seq,sessions:events};
   else if(url.pathname==='/api/activity'){const after=Number(url.searchParams.get('after'));const next=events.filter(e=>e.seq>after);data={events:next,next_cursor:next.length?next.at(-1).seq:after};}
   else if(url.pathname==='/api/servers')data=[{name:'test',connected:true,sessions:[{name:'work'}]}];
   else if(url.pathname==='/api/test/sessions')data=[{name:'work'}];
   else if(url.pathname==='/api/layout')data={};
   else if(url.pathname.endsWith('/specs'))data={hostname:'test',disks:[]};
   else if(url.pathname.endsWith('/metrics'))data={current:{cpu:0,memory:0},sessions:{},cpu:[],memory:[]};
   return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.addInitScript(()=>{window.testSends=[];window.WebSocket=class{static OPEN=1;constructor(){this.readyState=1;setTimeout(()=>this.onopen?.(),0);}send(data){window.testSends.push(data);}close(){this.readyState=3;}};});
  await page.goto('https://inspection.test/',{waitUntil:'networkidle'});
  await page.evaluate(()=>{currentServer='test';cachedSessions=[{name:'work'}];renderSidebar();connect('work');hasInput=true;updateInputState();term.focus();});
  const focused=await page.evaluate(()=>document.activeElement===term.textarea);
  events.push(event('running'));
  await page.waitForFunction(()=>document.querySelector('.activity-status')?.textContent==='Working');
  events.push(event('turn_finished'));
  await page.waitForFunction(()=>document.querySelector('.activity-status')?.textContent==='Response finished');
  assert.equal(await page.evaluate(()=>document.activeElement===term.textarea),focused);
  console.log('PASS status follows provider events without stealing terminal focus');
  await page.evaluate(()=>window.bridgeActivity.settings());
  await page.locator('[data-kind="waiting"]').uncheck();
  await page.locator('[data-pref="sound"]').check();
  const prefs=await page.evaluate(()=>JSON.parse(localStorage.getItem('bridge.notificationSettings.v1')));
  assert(!prefs.kinds.includes('waiting'));assert(prefs.sound);
  assert(await page.locator('#notification-settings').evaluate(el=>el.getBoundingClientRect().right<=innerWidth));
  await page.getByRole('button',{name:'Test notification',exact:true}).click();
  assert((await page.locator('[data-message]').textContent()).includes('In-app test'));
  await page.getByRole('button',{name:'Close',exact:true}).click();
  console.log('PASS mobile settings, per-device preferences, and in-app test');
  await page.route('**/api/activity/push/test-sub/test',r=>r.fulfill({status:502,contentType:'application/json',body:JSON.stringify({error:'Push delivery failed on the gateway. Your device is still registered; please retry the test.'})}));
  await page.evaluate(()=>{const key='bridge.notificationSettings.v1',p=JSON.parse(localStorage.getItem(key));p.pushId='test-sub';localStorage.setItem(key,JSON.stringify(p));});
  await page.reload({waitUntil:'networkidle'});
  await page.evaluate(()=>window.bridgeActivity.settings());
  await page.getByRole('button',{name:'Test notification',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-message]').textContent.includes('Your device is still registered'));
  console.log('PASS push failure explains the server error without asking for re-registration');
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
