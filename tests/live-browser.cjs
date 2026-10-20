// Opt-in end-to-end test: creates and removes one disposable tmux session.
const assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const historyMode=process.env.BRIDGE_SCROLL_MODE==='history';
const base=process.env.BRIDGE_URL||'http://127.0.0.1:4601',server=process.env.BRIDGE_SERVER||'test-node';
const name='bridge-ui-check-'+server+'-'+Date.now()+'-'+process.pid;
const shellQuote=s=>"'"+s.replace(/'/g,"'\\''")+"'";
const program=historyMode?`import time
for i in range(150): print('history-marker-%d'%i,flush=True)
time.sleep(120)
`:`import os,sys,tty,time,select,re
for i in range(150): print('history-marker-%d'%i,flush=True)
tty.setraw(sys.stdin.fileno())
os.write(1,b'\\x1b[?1049h\\x1b[?1000h\\x1b[?1006h\\x1b[2JREADY')
end=time.time()+120
count=0
while time.time()<end:
 if select.select([0],[],[],1)[0]:
  data=os.read(0,4096)
  count+=len(re.findall(rb'\\x1b\\[<64;',data))
  os.write(1,('\\x1b[1;1HSCROLL_COUNT=%d '%count).encode())
  if b'\\x03' in data: os.write(1,b'\\x1b[2;1HCTRL_C_RECEIVED')
`;
async function api(path,options){const r=await fetch(base+'/api/'+server+path,options);const data=await r.json();assert(r.ok,JSON.stringify(data));return data;}
(async()=>{
 let browser,created=false;
 try{
  const result=await api('/sessions/'+name,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:'python3 -u -c '+shellQuote(program)})});assert.equal(result.created,true);created=true;
  browser=await browserType.launch({headless:true,...(engine==='chromium'&&process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
  const context=await browser.newContext({viewport:{width:375,height:667},isMobile:true,hasTouch:true});
  const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base,{waitUntil:'networkidle'});
  await page.evaluate(async({server,name})=>{enterServer(server);await loadSessions();connect(name);},{server,name});
  await page.waitForFunction(historyMode=>hasInput && (historyMode?localHistory().includes('history-marker-149'):term.modes.mouseTrackingMode!=='none'),historyMode,{timeout:20000});
  const before=await page.evaluate(()=>localHistory());
  const cdp=engine==='chromium'?await context.newCDPSession(page):{send:async(method,event)=>{
   await page.evaluate(event=>{
    if(event.type==='touchStart')window.touchTestTarget=document.elementFromPoint(event.touchPoints[0].x,event.touchPoints[0].y);
    const target=window.touchTestTarget,touches=event.touchPoints.map(p=>({identifier:1,target,clientX:p.x,clientY:p.y}));
    const e=new Event(event.type.toLowerCase(),{bubbles:true,cancelable:true});Object.defineProperties(e,{touches:{value:touches},changedTouches:{value:touches}});target.dispatchEvent(e);
   },event);
  }};
  await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:180,y:180}]});
  for(let y=200;y<=400;y+=20){await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:180,y}]});await page.waitForTimeout(30);}
  await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  if(historyMode){
   await page.waitForFunction(before=>remoteHistoryPane!==null && localHistory()!==before,before,{timeout:15000});
   console.log('PASS deployed touch swipe scrolls tmux history without mouse reporting');
   await page.evaluate(()=>returnToLive());
   await page.waitForFunction(()=>remoteHistoryPane===null,{},{timeout:10000});
   console.log('PASS Latest exits only tmux copy mode');
  }else{
   await page.waitForFunction(()=>/SCROLL_COUNT=[1-9]/.test(localHistory()),{},{timeout:10000});
   console.log('PASS deployed touch swipe reached the remote mouse-aware application');
   await page.evaluate(()=>openShortcuts());
   await page.getByRole('button',{name:'Ctrl+C — Interrupt',exact:true}).click();
   await page.waitForFunction(()=>localHistory().includes('CTRL_C_RECEIVED'),{},{timeout:10000});
   console.log('PASS deployed shortcut reached the remote application');
  }
  const history=await api('/history/'+name);
  assert.equal(history.ok,true);assert(history.text.length>0);
  console.log('PASS deployed read-only history RPC');
  await page.evaluate(()=>showHistory());
  await page.waitForFunction(()=>document.getElementById('history-status').textContent.startsWith('Active pane snapshot'),{},{timeout:10000});
  console.log('PASS deployed history dialog');
  assert.deepEqual(errors,[]);
 }finally{
  if(browser)await browser.close();
  if(created)await api('/sessions/'+name,{method:'DELETE'});
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
