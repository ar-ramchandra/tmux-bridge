// Opt-in: creates and removes one disposable terminal; never opens user sessions.
const assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'webkit';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const base=process.env.BRIDGE_URL||'http://127.0.0.1:4601';
const server=process.env.BRIDGE_SERVER||'test-node';
const name='bridge-wheel-check-'+Date.now()+'-'+process.pid;
const quote=s=>"'"+s.replace(/'/g,"'\\''")+"'";
const program=`import os,tty,time,select
for i in range(150): print('history-marker-%d'%i,flush=True)
tty.setraw(0)
os.write(1,b'\\x1b[?1049h\\x1b[?1000l\\x1b[2JREADY_FOR_WHEEL')
end=time.time()+120
received=b''
while time.time()<end:
 if select.select([0],[],[],1)[0]:
  received+=os.read(0,4096)
  if b'CHECK_WHEEL' in received:
   arrows=received.count(b'\\x1b[A')+received.count(b'\\x1bOA')+received.count(b'\\x1b[B')+received.count(b'\\x1bOB')
   os.write(1,('\\r\\nWHEEL_CHECK_ARROWS=%d\\r\\n'%arrows).encode())
`;
async function api(path,method='GET',body){
 const r=await fetch(base+'/api/'+server+path,{method,...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
 const data=await r.json();assert(r.ok,JSON.stringify(data));return data;
}
(async()=>{
 let browser,created=false;
 try{
  assert((await api('/sessions/'+name,'POST',{command:'python3 -u -c '+quote(program)})).created);created=true;
  browser=await browserType.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:800}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base,{waitUntil:'networkidle'});
  await page.evaluate(async({server,name})=>{enterServer(server);await loadSessions();connect(name);},{server,name});
  await page.waitForFunction(()=>hasInput&&localHistory().includes('READY_FOR_WHEEL'),{},{timeout:20000});
  await page.locator('.xterm-screen').hover();
  await page.mouse.wheel(0,-180);
  await page.waitForFunction(()=>remoteHistoryPane!==null,{},{timeout:15000});
  console.log('PASS live trackpad wheel enters remote history');
  await page.evaluate(()=>returnToLive());
  await page.waitForFunction(()=>remoteHistoryPane===null,{},{timeout:10000});
  await page.evaluate(()=>sendKey('CHECK_WHEEL'));
  await page.waitForFunction(()=>localHistory().includes('WHEEL_CHECK_ARROWS='),{},{timeout:10000});
  assert(await page.evaluate(()=>localHistory().includes('WHEEL_CHECK_ARROWS=0')));
  assert.deepEqual(errors,[]);
  console.log('PASS live application received zero arrow keys; typing works after Latest');
 }finally{
  if(browser)await browser.close();
  if(created)await api('/sessions/'+name,'DELETE');
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
