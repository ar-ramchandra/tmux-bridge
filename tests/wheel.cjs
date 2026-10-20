// Desktop wheel regression: never turn a scroll into terminal arrow keys.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const html=fs.readFileSync(path.join(__dirname,'../gateway.py'),'utf8').split('HTML = r"""')[1].split('"""')[0];
(async()=>{
 const browser=await browserType.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:800}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>{
   const url=r.request().url();
   if(url.startsWith('https://cdn.jsdelivr.net/'))return r.continue();
   if(url==='https://inspection.test/')return r.fulfill({contentType:'text/html',body:html});
   return r.fulfill({contentType:'application/json',body:JSON.stringify(url.includes('/history/')?{ok:true,text:'Captured history'}:url.includes('/layout')?{}:[])});
  });
  await page.addInitScript(()=>{
   window.testSends=[];
   window.WebSocket=class{
    static OPEN=1;
    constructor(){this.readyState=1;setTimeout(()=>this.onopen?.(),0);}
    send(data){
     window.testSends.push(data);
     if(typeof data==='string'&&data.startsWith('{')&&JSON.parse(data).type==='scroll')
      setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'scroll_result',ok:true,copy_mode:true,pane:'%1'})}),0);
    }
    close(){this.readyState=3;}
   };
  });
  await page.goto('https://inspection.test/',{waitUntil:'networkidle'});
  await page.evaluate(()=>{
   currentServer='test';onServer=true;cachedSessions=[{name:'wheel-test'}];presenceData={};_layout={};
   document.getElementById('home-view').classList.remove('visible');
   document.getElementById('server-landing').classList.remove('visible');
   connect('wheel-test');hasInput=true;updateInputState();fitAddon.fit();
  });
  async function mode(sequence){
   await page.evaluate(async sequence=>{resetTouchScroll();await new Promise(r=>term.write(sequence,r));window.testSends=[];},sequence);
   await page.locator('.xterm-screen').hover();
  }
  const scrolls=()=>page.evaluate(()=>window.testSends.map(s=>{try{return JSON.parse(s);}catch{return {raw:s};}}));
  await mode('\x1b[?1049h\x1b[?1000l\x1b[?1002l\x1b[?1003l');
  await page.mouse.wheel(0,-120);await page.waitForTimeout(150);
  let sent=await scrolls();
  assert(sent.some(m=>m.type==='scroll'&&m.lines<0),JSON.stringify(sent));
  assert(sent.every(m=>m.type==='scroll'),JSON.stringify(sent));
  console.log('PASS alternate-screen wheel scrolls history without Up/Down keys');

  await mode('\x1b[?1000h\x1b[?1006h');
  await page.mouse.wheel(0,120);await page.waitForTimeout(150);
  sent=await scrolls();assert(sent.some(m=>m.type==='scroll'&&m.lines>0));
  assert(sent.every(m=>m.type==='scroll'),JSON.stringify(sent));
  console.log('PASS mouse-aware terminal scrolling uses one RPC path');

  await mode('\x1b[?1000l\x1b[?1049l'+Array.from({length:200},(_,i)=>'line '+i+'\r\n').join(''));
  await page.evaluate(()=>term.scrollToBottom());
  await page.mouse.wheel(0,-150);await page.waitForTimeout(150);
  assert(await page.evaluate(()=>term.buffer.active.viewportY<term.buffer.active.baseY));
  assert.deepEqual(await scrolls(),[]);
  console.log('PASS normal scrollback stays local and sends no input');

  await mode('\x1b[?1049h');
  await page.evaluate(()=>{
   const screen=document.querySelector('.xterm-screen'),rect=screen.getBoundingClientRect();
   for(let i=0;i<80;i++)screen.dispatchEvent(new WheelEvent('wheel',{bubbles:true,cancelable:true,deltaY:-.5,clientX:rect.left+20,clientY:rect.top+20}));
  });
  await page.waitForTimeout(150);sent=await scrolls();
  assert(sent.some(m=>m.type==='scroll'&&m.lines<0));assert(sent.every(m=>m.type==='scroll'));
  console.log('PASS fractional trackpad deltas accumulate into scroll lines');

  await mode('');
  await page.evaluate(()=>{
   const screen=document.querySelector('.xterm-screen'),rect=screen.getBoundingClientRect();
   screen.dispatchEvent(new WheelEvent('wheel',{bubbles:true,cancelable:true,deltaY:-2,deltaMode:1,clientX:rect.left+20,clientY:rect.top+20}));
  });
  await page.waitForTimeout(150);sent=await scrolls();assert.equal(sent[0].lines,-2);
  console.log('PASS wheel line units retain their intended distance');

  await mode('');
  await page.evaluate(()=>{
   const screen=document.querySelector('.xterm-screen');
   screen.dispatchEvent(new WheelEvent('wheel',{bubbles:true,cancelable:true,deltaY:-120,ctrlKey:true}));
   screen.dispatchEvent(new WheelEvent('wheel',{bubbles:true,cancelable:true,deltaX:120,deltaY:0}));
  });
  await page.waitForTimeout(150);assert.deepEqual(await scrolls(),[]);
  console.log('PASS pinch zoom and horizontal gestures send no terminal keys');

  await mode('');await page.evaluate(()=>{hasInput=false;updateInputState();});
  await page.mouse.wheel(0,-120);await page.waitForTimeout(150);
  assert.deepEqual(await scrolls(),[]);assert(await page.locator('#history-modal').evaluate(d=>d.open));
  assert.deepEqual(errors,[]);
  console.log('PASS viewing-only scrolling opens read-only history without input');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
