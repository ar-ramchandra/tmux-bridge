// Opt-in: creates/removes only two disposable sessions and checks tab input isolation.
const assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'webkit';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const base=process.env.BRIDGE_URL||'http://127.0.0.1:4601',server=process.env.BRIDGE_SERVER||'test-node';
const prefix='bridge-tabs-'+Date.now()+'-'+process.pid,names=[prefix+'-a',prefix+'-b'];
const quote=s=>"'"+s.replace(/'/g,"'\\''")+"'";
async function api(path,method='GET',body){
 const r=await fetch(base+'/api/'+server+path,{method,...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
 const data=await r.json();assert(r.ok,JSON.stringify(data));return data;
}
(async()=>{
 let browser;const created=[];
 try{
  for(const [i,name] of names.entries()){
   const program=`import os,tty,time,select\ntty.setraw(0)\nos.write(1,b'TAB_${i}_READY\\r\\n')\nend=time.time()+120\nwhile time.time()<end:\n if select.select([0],[],[],1)[0]:\n  data=os.read(0,4096)\n  os.write(1,b'RECEIVED:'+data+b'\\r\\n')\n`;
   assert((await api('/sessions/'+name,'POST',{command:'python3 -u -c '+quote(program)})).created);created.push(name);
  }
  browser=await browserType.launch({headless:true,...(engine==='chromium'&&process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
  const page=await browser.newPage({viewport:{width:375,height:667},isMobile:true,hasTouch:true});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base,{waitUntil:'networkidle'});
  await page.evaluate(async({server,names})=>{enterServer(server);await loadSessions();connect(names[0]);},{server,names});
  await page.waitForFunction(()=>hasInput&&localHistory().includes('TAB_0_READY'),{},{timeout:20000});
  await page.evaluate(name=>connect(name),names[1]);
  await page.waitForFunction(()=>hasInput&&localHistory().includes('TAB_1_READY'),{},{timeout:20000});
  await page.evaluate(()=>window.socketBeforeReorder=ws);
  await page.getByRole('button',{name:'Reorder '+names[1]+' on '+server,exact:true}).focus();
  await page.keyboard.press('Home');
  assert.deepEqual(await page.evaluate(()=>terminalTabs.map(t=>t.session)),[names[1],names[0]]);
  assert(await page.evaluate(()=>ws===window.socketBeforeReorder&&ws.readyState===WebSocket.OPEN));
  console.log('PASS deployed tab reordering keeps the live terminal connection');
  await page.getByRole('tab',{name:names[0]+' on '+server,exact:true}).tap();
  await page.waitForFunction(()=>hasInput&&localHistory().includes('TAB_0_READY'),{},{timeout:20000});
  await page.evaluate(()=>sendKey('ONLY_TAB_A'));
  await page.waitForFunction(()=>localHistory().includes('ONLY_TAB_A'));
  await page.getByRole('tab',{name:names[1]+' on '+server,exact:true}).tap();
  await page.waitForFunction(()=>hasInput&&localHistory().includes('TAB_1_READY'),{},{timeout:20000});
  await page.evaluate(()=>sendKey('ONLY_TAB_B'));
  await page.waitForFunction(()=>localHistory().includes('ONLY_TAB_B'));
  const a=(await api('/history/'+names[0])).text,b=(await api('/history/'+names[1])).text;
  assert(a.includes('ONLY_TAB_A')&&!a.includes('ONLY_TAB_B'));
  assert(b.includes('ONLY_TAB_B')&&!b.includes('ONLY_TAB_A'));
  console.log('PASS live tabs route input only to the selected terminal');
  await page.getByRole('button',{name:'Close tab '+names[1]+' on '+server,exact:true}).tap();
  await page.waitForFunction(name=>currentSession===name&&hasInput,names[0],{timeout:15000});
  assert((await api('/sessions')).some(s=>s.name===names[1]));
  console.log('PASS closing the active tab selects its neighbor and keeps tmux running');
  await page.reload({waitUntil:'networkidle'});
  await page.waitForFunction(name=>currentSession===name&&hasInput,names[0],{timeout:20000}).catch(async e=>{console.log(await page.evaluate(()=>({server:currentServer,session:currentSession,hasInput,tabs:terminalTabs,saved:localStorage.getItem('activeTerminalTab'),socket:ws?.readyState})));throw e;});
  console.log('PASS deployed tabs restore the selected session after reload');
  assert.deepEqual(errors,[]);
  if(process.env.SCREENSHOT_PATH)await page.screenshot({path:process.env.SCREENSHOT_PATH});
 }finally{
  if(browser)await browser.close();
  for(const name of created)await api('/sessions/'+name,'DELETE');
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
