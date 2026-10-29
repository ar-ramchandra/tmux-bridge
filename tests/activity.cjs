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
  let events=[],seq=0,receipts=[];
  const event=kind=>({version:1,id:String(++seq),seq,server:'test',session:'work',provider:'codex',thread_id:'thread',turn_id:'turn',time:Date.now()/1000,kind});
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.hostname==='cdn.jsdelivr.net')return route.continue();
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
   if(url.pathname==='/activity.js')return route.fulfill({contentType:'application/javascript',body:script});
   let data=[];
   if(url.pathname.endsWith('/received')){receipts.push(JSON.parse(route.request().postData()));data={ok:true};}
   else if(url.pathname==='/api/activity/snapshot')data={cursor:seq,sessions:events};
   else if(url.pathname==='/api/activity'){const after=Number(url.searchParams.get('after'));const next=events.filter(e=>e.seq>after);data={events:next,next_cursor:next.length?next.at(-1).seq:after};}
   else if(url.pathname==='/api/servers')data=[{name:'test',connected:true,sessions:[{name:'work'}]}];
   else if(url.pathname==='/api/test/sessions')data=[{name:'work'}];
   else if(url.pathname==='/api/layout')data={};
   else if(url.pathname.endsWith('/specs'))data={hostname:'test',disks:[]};
   else if(url.pathname.endsWith('/metrics'))data={current:{cpu:0,memory:0},sessions:{},cpu:[],memory:[]};
   return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.addInitScript(()=>{localStorage.setItem('bridge.notificationSettings.v1',JSON.stringify({inApp:true,sound:false,pushId:'test-sub'}));window.testSends=[];window.WebSocket=class{static OPEN=1;constructor(){this.readyState=1;setTimeout(()=>this.onopen?.(),0);}send(data){window.testSends.push(data);}close(){this.readyState=3;}};});
  await page.goto('https://inspection.test/',{waitUntil:'networkidle'});
  await page.evaluate(()=>{currentServer='test';cachedSessions=[{name:'work'}];renderSidebar();connect('work');hasInput=true;updateInputState();term.focus();});
  await page.evaluate(()=>{window.cues=0;window.banners=0;playTone=(freq,duration)=>{if(freq===880&&duration===0.15)window.cues++;};notifyUser=()=>window.banners++;Object.defineProperty(navigator,'vibrate',{configurable:true,value:()=>false});});
  const focused=await page.evaluate(()=>document.activeElement===term.textarea);
  events.push(event('running'));
  await page.waitForFunction(()=>document.querySelector('.activity-status')?.textContent==='Working');
  events.push(event('turn_finished'));
  await page.waitForFunction(()=>document.querySelector('.activity-status')?.textContent==='Response finished');
  assert.equal(await page.evaluate(()=>document.activeElement===term.textarea),focused);
  assert.equal(await page.evaluate(()=>window.cues),1);assert.equal(await page.evaluate(()=>window.banners),0);
  assert.deepEqual(receipts,[{sequences:[2]}]);
  console.log('PASS foreground completion plays one cue, acknowledges delivery, and shows no banner or focus change');
  await page.evaluate(()=>window.bridgeActivity.settings());
  await page.locator('[data-kind="waiting"]').uncheck();
  await page.locator('[data-pref="foregroundCue"]').uncheck();
  const prefs=await page.evaluate(()=>JSON.parse(localStorage.getItem('bridge.notificationSettings.v1')));
  assert(!prefs.kinds.includes('waiting'));assert.equal(prefs.foregroundCue,false);
  assert(await page.locator('#notification-settings').evaluate(el=>el.getBoundingClientRect().right<=innerWidth));
  await page.getByRole('button',{name:'Test notification',exact:true}).click();
  assert((await page.locator('[data-message]').textContent()).includes('cue is off'));
  events.push(event('turn_finished'));
  await page.waitForFunction(()=>document.querySelector('.activity-status')?.title.includes('codex'));
  await page.waitForTimeout(2800);
  assert.equal(receipts.length,2);assert.equal(await page.evaluate(()=>window.cues),1);
  await page.locator('[data-pref="foregroundCue"]').check();
  await page.getByRole('button',{name:'Test notification',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.cues),2);assert.equal(await page.evaluate(()=>window.banners),0);
  await page.getByRole('button',{name:'Close',exact:true}).click();
  console.log('PASS mobile settings, per-device preferences, and in-app test');
  // A locked/background page must not consume the device's push delivery.
  await page.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,get:()=>true}));
  events.push(event('turn_finished'));
  await page.waitForFunction(()=>window.bridgeActivity&&document.querySelector('.activity-status')?.textContent==='Response finished');
  await page.waitForTimeout(3000);
  assert.equal(receipts.length,2);assert.equal(await page.evaluate(()=>window.cues),2);
  await page.evaluate(()=>{delete document.hidden;window.dispatchEvent(new Event('focus'));});
  await page.waitForTimeout(300);
  events.push(event('waiting'));
  await page.waitForFunction(()=>document.querySelector('.activity-status')?.textContent==='Needs input');
  assert.equal(receipts.length,2);assert.equal(await page.evaluate(()=>window.cues),2);
  console.log('PASS hidden and filtered events leave push delivery alone');
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
