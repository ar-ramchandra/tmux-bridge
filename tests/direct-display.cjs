// Direct display interactions: no selection mode, toolbar, or modifier needed.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const html=fs.readFileSync(path.join(__dirname,'../gateway.py'),'utf8').split('HTML = r"""')[1].split('"""')[0];
(async()=>{
 const browser=await browserType.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:800},hasTouch:process.env.TOUCH_MODE!=='0'}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>{
   const u=r.request().url();
   if(u.startsWith('https://cdn.jsdelivr.net/'))return r.continue();
   return r.fulfill({contentType:u==='https://inspection.test/'?'text/html':'application/json',body:u==='https://inspection.test/'?html:u.includes('/layout')?'{}':'[]'});
  });
  await page.addInitScript(()=>{
   window.sent=[];window.copied=[];window.opened=[];
   Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>window.copied.push(text)}});
   window.open=(url,target,features)=>{window.opened.push({url,target,features});return null;};
   window.WebSocket=class{
    static OPEN=1;constructor(){this.readyState=1;setTimeout(()=>this.onopen?.(),0);}
    send(data){window.sent.push(data);}close(){this.readyState=3;}
   };
  });
  await page.goto('https://inspection.test/',{waitUntil:'networkidle'});
  await page.evaluate(()=>{
   currentServer='test';onServer=true;cachedSessions=[{name:'direct'}];presenceData={};_layout={};
   document.getElementById('home-view').classList.remove('visible');
   document.getElementById('server-landing').classList.remove('visible');
   connect('direct');hasInput=true;updateInputState();fitAddon.fit();
  });
  async function write(text,reset=true){
   await page.evaluate(async({text,reset})=>{if(reset)term.reset();await new Promise(r=>term.write(text,r));window.sent=[];window.opened=[];},{text,reset});
   await page.waitForTimeout(80);
  }
  async function point(col,row=0){
   const rect=await page.locator('.xterm-screen').boundingBox(),size=await page.evaluate(()=>({cols:term.cols,rows:term.rows}));
   return {x:rect.x+(col+.5)*rect.width/size.cols,y:rect.y+(row+.5)*rect.height/size.rows};
  }
  async function drag(start,end){
   const a=await point(start),b=await point(end);
   await page.mouse.move(a.x,a.y);
   await page.evaluate(()=>window.sent=[]);
   await page.mouse.down();await page.mouse.move(b.x,b.y,{steps:14});await page.mouse.up();
  }
  assert.equal(await page.locator('#terminal-actions').count(),0);
  for(const mode of [0,1000,1002,1003]){
   await write((mode?'\x1b[?'+mode+'h\x1b[?1006h':'')+'Direct selection works here');
   await drag(0,16);
   assert.match(await page.evaluate(()=>term.getSelection()),/Direct selection/);
   assert.deepEqual(await page.evaluate(()=>window.sent),[]);
   await page.keyboard.press('Meta+c');
   assert.match(await page.evaluate(()=>window.copied.at(-1)),/Direct selection/);
   assert.deepEqual(await page.evaluate(()=>window.sent),[]);
  }
  console.log('PASS ordinary drag and Cmd+C work in normal, click, drag, and motion mouse modes without app input');

  await write('\x1b[?1000h\x1b[?1006hDouble click selects words');
  let p=await point(3);await page.mouse.dblclick(p.x,p.y);
  assert.equal(await page.evaluate(()=>term.getSelection()),'Double');
  await page.keyboard.press('Control+c');
  assert.equal(await page.evaluate(()=>window.copied.at(-1)),'Double');
  // Headless WebKit cannot dismiss its OS context menu through page keyboard
  // injection. Suppress that popup only in this test; still verify right-click
  // retains the selection and sends no application mouse packets.
  await page.evaluate(()=>document.querySelector('.xterm-screen').addEventListener('contextmenu',e=>e.preventDefault(),{once:true}));
  await page.mouse.click(p.x,p.y,{button:'right'});
  assert.equal(await page.evaluate(()=>term.getSelection()),'Double');
  assert.deepEqual(await page.evaluate(()=>window.sent),[]);
  await page.keyboard.press('Escape');
  await page.evaluate(()=>{term.clearSelection();window.sent=[];term.focus();});
  await page.keyboard.press('Control+c');
  assert.deepEqual(await page.evaluate(()=>window.sent),['\x03']);
  console.log('PASS double-click word selection, native context-menu selection, and Ctrl+C copy/interrupt');

  await write('\x1b[?1000h\x1b[?1006hNo link here yet');
  p=await point(5);await page.mouse.move(p.x,p.y);
  await write('\x1b[H\x1b[2Khttps://example.com/new-link',false);
  await page.mouse.click(p.x,p.y);
  assert.deepEqual(await page.evaluate(()=>window.opened.map(l=>l.url)),['https://example.com/new-link']);
  assert.deepEqual(await page.evaluate(()=>window.sent),[]);
  await write('\x1b[H\x1b[2Khttps://example.com/updated',false);
  await page.mouse.click(p.x,p.y);
  assert.deepEqual(await page.evaluate(()=>window.opened.map(l=>l.url)),['https://example.com/updated']);
  assert.deepEqual(await page.evaluate(()=>window.sent),[]);
  console.log('PASS a single click opens a newly painted or changed URL under the same pointer exactly once');

  await write('\x1b[?1000h\x1b[?1006hhttps://example.com/select-this');
  await drag(0,24);
  assert.match(await page.evaluate(()=>term.getSelection()),/https:/);
  assert.deepEqual(await page.evaluate(()=>window.opened),[]);
  assert.deepEqual(await page.evaluate(()=>window.sent),[]);
  console.log('PASS dragging across a link selects it without opening it or sending mouse input');

  await write('\x1b[?1000h\x1b[?1006h\x1b]8;;https://example.com/actual-target\x07Click this label\x1b]8;;\x07');
  p=await point(4);
  await page.mouse.click(p.x,p.y);
  assert.deepEqual(await page.evaluate(()=>window.opened.map(l=>l.url)),['https://example.com/actual-target']);
  assert.deepEqual(await page.evaluate(()=>window.sent),[]);
  assert((await page.evaluate(()=>window.opened)).every(l=>l.features.includes('noopener')));
  await write('\x1b[H\x1b[2K\x1b]8;;https://example.com/repainted-target\x07Click this label\x1b]8;;\x07',false);
  await page.mouse.click(p.x,p.y);
  assert.deepEqual(await page.evaluate(()=>window.opened.map(l=>l.url)),['https://example.com/repainted-target']);
  assert.deepEqual(await page.evaluate(()=>window.sent),[]);
  console.log('PASS labeled terminal links open their actual destination once without app input');

  await write('\x1b[?1000h\x1b[?1006hApplication click target');
  p=await point(5);await page.keyboard.down('Alt');await page.mouse.click(p.x,p.y);await page.keyboard.up('Alt');
  assert((await page.evaluate(()=>window.sent)).some(s=>typeof s==='string'&&s.startsWith('\x1b[<0;')));
  assert.equal(await page.evaluate(()=>term.hasSelection()),false);
  console.log('PASS Alt/Option-click explicitly retains application mouse control');

  await page.evaluate(()=>term.resize(30,term.rows));
  const longURL='https://example.com/'+('long-segment-'.repeat(5));
  await write('\x1b[?1000h\x1b[?1006h'+longURL);
  p=await point(5,1);await page.mouse.click(p.x,p.y);
  assert.deepEqual(await page.evaluate(()=>window.opened.map(l=>l.url)),[longURL]);
  await write('\x1b[?1000h\x1b[?1006h界 https://example.com');
  p=await point(8);await page.mouse.click(p.x,p.y);
  assert.deepEqual(await page.evaluate(()=>window.opened.map(l=>l.url)),['https://example.com/']);
  console.log('PASS wrapped URLs and URLs following wide characters remain clickable');

  if(process.env.TOUCH_MODE!=='0'){
  await write('\x1b[?1000h\x1b[?1006hhttps://example.com/tap');
  p=await point(6);await page.touchscreen.tap(p.x,p.y);
  assert.deepEqual(await page.evaluate(()=>window.opened.map(l=>l.url)),['https://example.com/tap']);
  assert.deepEqual(await page.evaluate(()=>window.sent),[]);
  console.log('PASS direct touch tap opens a URL without terminal input');
  }
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
