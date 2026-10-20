// Creates and removes only its own disposable session on the chosen live node.
const assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'webkit';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const base=process.env.BRIDGE_URL||'http://127.0.0.1:4601',server=process.env.BRIDGE_SERVER||'test-node';
const name='bridge-selection-check-'+Date.now()+'-'+process.pid;
const quote=s=>"'"+s.replace(/'/g,"'\\''")+"'";
async function api(method,body){
 const response=await fetch(base+'/api/'+server+'/sessions/'+name,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
 const data=await response.json();assert(response.ok,JSON.stringify(data));return data;
}
(async()=>{
 let browser,created=false;
 try{
  const command='python3 -u -c '+quote("import os,tty,time\n"+
   "tty.setraw(0)\nos.write(1,b'\\x1b[2J\\x1b[H\\x1b[?1000h\\x1b[?1006hSELECT_COPY_READY\\r\\nhttps://example.com/terminal-copy-test\\r\\n')\ntime.sleep(90)\n");
  assert((await api('POST',{command})).created);created=true;
  browser=await browserType.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:800}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
   window.copied=[];window.opened=[];
   Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>window.copied.push(text)}});
   window.open=url=>{window.opened.push(url);return null;};
  });
  await page.goto(base,{waitUntil:'networkidle'});
  await page.evaluate(async({server,name})=>{enterServer(server);await loadSessions();connect(name);},{server,name});
  await page.waitForFunction(()=>localHistory().includes('SELECT_COPY_READY'),{},{timeout:20000});
  // Wait for tmux's initial attach/resize mode changes and redraw to finish.
  await page.evaluate(()=>{window.lastTerminalData=Date.now();ws.addEventListener('message',()=>window.lastTerminalData=Date.now());});
  await page.waitForFunction(()=>Date.now()-window.lastTerminalData>600,{},{timeout:10000});
  await page.evaluate(()=>{window.afterSelectionSends=[];const send=ws.send.bind(ws);ws.send=data=>{window.afterSelectionSends.push(data);send(data);};});
  assert.equal(await page.locator('#terminal-actions').count(),0);
  const rect=await page.locator('.xterm-screen').boundingBox(),size=await page.evaluate(()=>({cols:term.cols,rows:term.rows}));
  const width=rect.width/size.cols,height=rect.height/size.rows;
  await page.mouse.move(rect.x+.5*width,rect.y+.5*height);await page.mouse.down();
  await page.mouse.move(rect.x+17.5*width,rect.y+.5*height,{steps:12});await page.mouse.up();
  if(!(await page.evaluate(()=>term.hasSelection()))){
   console.log({originalRect:rect,originalSize:size,after:await page.evaluate(()=>({rect:document.querySelector('.xterm-screen').getBoundingClientRect().toJSON(),pointer:displayPointer,focused:document.activeElement?.outerHTML.slice(0,180),rows:term.rows,cols:term.cols,mouse:term.modes.mouseTrackingMode,history:localHistory().slice(0,200)}))});
   await page.screenshot({path:'/tmp/live-direct-debug-'+engine+'.png'});
  }
  assert.match(await page.evaluate(()=>term.getSelection()),/SELECT_COPY_READY/);
  await page.keyboard.press('Meta+c');
  if(!(await page.evaluate(()=>window.copied.length)))console.log(await page.evaluate(()=>({selected:term.getSelection(),focused:document.activeElement?.outerHTML.slice(0,180),pointer:displayPointer,rows:term.rows,cols:term.cols,hasInput})));
  assert.equal((await page.evaluate(()=>window.copied.at(-1))).trim(),'SELECT_COPY_READY');
  await page.mouse.click(rect.x+10*width,rect.y+1.5*height);
  assert.deepEqual(await page.evaluate(()=>window.opened),['https://example.com/terminal-copy-test']);
  assert.deepEqual(await page.evaluate(()=>window.afterSelectionSends),[]);
  console.log('PASS live node direct drag, Cmd+C, and single-click link with no app input or extra button row');
  await page.getByRole('button',{name:'Select / History',exact:true}).click();
  await page.waitForFunction(()=>document.getElementById('history-status').textContent.includes('snapshot stays still'),{},{timeout:15000});
  await page.locator('#history-search').fill('SELECT_COPY_READY');
  await page.getByRole('button',{name:'Find next',exact:true}).click();
  await page.getByRole('button',{name:'Copy selection',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.copied.at(-1)),'SELECT_COPY_READY');
  assert.equal(await page.locator('#history-status').textContent(),'Copied.');
  assert.equal(await page.locator('#history-text a').first().getAttribute('href'),'https://example.com/terminal-copy-test');
  await page.getByRole('button',{name:'Copy all loaded',exact:true}).click();
  assert.match(await page.evaluate(()=>window.copied.at(-1)),/SELECT_COPY_READY/);
  assert.deepEqual(await page.evaluate(()=>window.afterSelectionSends),[]);
  assert.deepEqual(errors,[]);
  console.log('PASS live node snapshot selection, copying, clickable URL, and no terminal input');
 }finally{
  if(browser)await browser.close();
  if(created)await api('DELETE');
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
