// Creates/deletes ONLY its own disposable session; never stops a live tmux server.
const assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'webkit';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const base=process.env.BRIDGE_URL||'http://127.0.0.1:4601';
const server=process.env.BRIDGE_SERVER||'test-node';
const name='bridge-recovery-check-'+Date.now()+'-'+process.pid;
const quote=s=>"'"+s.replace(/'/g,"'\\''")+"'";
async function api(method,body){
 const r=await fetch(base+'/api/'+server+'/sessions/'+name,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
 const result=await r.json();assert(r.ok,JSON.stringify(result));return result;
}
const command=n=>({command:'python3 -u -c '+quote(`import os,sys,tty,time,select
tty.setraw(0)
print('GENERATION_${n}',flush=True)
end=time.time()+90
while time.time()<end:
 if select.select([0],[],[],1)[0]:
  data=os.read(0,4096)
  if b'\\x03' in data: print('INPUT_AFTER_RECOVERY',flush=True)
`)});
(async()=>{
 let browser,created=false;
 try{
  assert((await api('POST',command(1))).created);created=true;
  browser=await browserType.launch({headless:true,...(engine==='chromium'&&process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
  const page=await browser.newPage({viewport:{width:375,height:667},isMobile:true,hasTouch:true});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base,{waitUntil:'networkidle'});
  await page.evaluate(async({server,name})=>{enterServer(server);await loadSessions();connect(name);},{server,name});
  await page.waitForFunction(()=>hasInput&&localHistory().includes('GENERATION_1'),{},{timeout:20000});
  await page.evaluate(()=>window.beforeRecoverySocket=ws);
  await api('DELETE');created=false;
  await page.waitForFunction(()=>window.beforeRecoverySocket.readyState===WebSocket.CLOSED,{},{timeout:10000});
  console.log('PASS live terminal exit closes the stale browser connection');
  await page.waitForTimeout(4000);
  assert((await api('POST',command(2))).created);created=true;
  await page.waitForFunction(()=>hasInput&&ws!==window.beforeRecoverySocket&&localHistory().includes('GENERATION_2'),{},{timeout:20000});
  console.log('PASS restored session reconnects automatically while the agent stays online');
  await page.evaluate(()=>sendKey('\x03'));
  await page.waitForFunction(()=>localHistory().includes('INPUT_AFTER_RECOVERY'),{},{timeout:10000});
  console.log('PASS input reaches the restored terminal');assert.deepEqual(errors,[]);
 }finally{
  if(browser)await browser.close();
  if(created)await api('DELETE');
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
