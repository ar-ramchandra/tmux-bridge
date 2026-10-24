// Run with PLAYWRIGHT_PATH and optional CHROMIUM_PATH pointing to an installed Playwright/browser.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const source=fs.readFileSync(path.join(__dirname,'../gateway.py'),'utf8');
const html=source.split('HTML = r"""')[1].split('"""')[0];
const passed=[];
(async()=>{
 const browser=await browserType.launch({headless:true,...(engine==='chromium'&&process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  async function makePage(mobile=true){
   const context=await browser.newContext({viewport:mobile?{width:375,height:667}:{width:700,height:800},isMobile:mobile,hasTouch:mobile});
   const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',async r=>{
    const u=r.request().url();
    if(u.startsWith('https://cdn.jsdelivr.net/'))return r.continue();
    if(u==='https://inspection.test/')return r.fulfill({contentType:'text/html',body:html});
    let data=[];
    if(u.includes('/layout'))data={};
    if(u.includes('/history/'))data={ok:true,text:Array.from({length:200},(_,i)=>'History line '+i).join('\n')};
    if(u.includes('/specs'))data={hostname:'test',disks:[]};
    if(u.includes('/metrics'))data={current:{cpu:0,memory:0},sessions:{},cpu:[],memory:[]};
    return r.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   });
   await page.addInitScript(()=>{
    window.testSends=[];
    window.WebSocket=class{
     static OPEN=1;
     constructor(url){this.url=url;this.readyState=1;setTimeout(()=>this.onopen?.(),0);}
     send(data){window.testSends.push(data);if(typeof data==='string'&&data.startsWith('{')){const msg=JSON.parse(data);if(msg.type==='scroll')setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'scroll_result',ok:true,copy_mode:false,pane:'%1'})}),0);}}
     close(){this.readyState=3;}
    };
   });
   await page.goto('https://inspection.test/',{waitUntil:'networkidle'});
   return {page,context,errors};
  }
  const {page,context,errors}=await makePage();
  const test=async(name,fn)=>{await fn();passed.push(name);console.log('PASS '+name);};
  async function setup(){await page.evaluate(()=>{
   currentServer='test';onServer=true;cachedSessions=[{name:'alpha'}];presenceData={};_layout={};
   document.getElementById('home-view').classList.remove('visible');
   document.getElementById('server-landing').classList.remove('visible');
   connect('alpha');hasInput=true;updateInputState();fitAddon.fit();window.testSends=[];
  });}
  await setup();
  if(process.env.SCREENSHOT_PATH)await page.screenshot({path:process.env.SCREENSHOT_PATH});
  const cdp=engine==='chromium'?await context.newCDPSession(page):{send:async(method,event)=>{
   await page.evaluate(event=>{
    if(event.type==='touchStart')window.touchTestTarget=document.elementFromPoint(event.touchPoints[0].x,event.touchPoints[0].y);
    const target=window.touchTestTarget;
    const touches=event.touchPoints.map(p=>({identifier:1,target,clientX:p.x,clientY:p.y,pageX:p.x,pageY:p.y,screenX:p.x,screenY:p.y}));
    const touchEvent=new Event(event.type.toLowerCase(),{bubbles:true,cancelable:true});
    Object.defineProperties(touchEvent,{touches:{value:touches},targetTouches:{value:touches},changedTouches:{value:touches}});
    target.dispatchEvent(touchEvent);
   },event);
  }};
  async function swipe(x,y,end){
   await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
   for(let i=1;i<=10;i++){await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x,y:y+(end-y)*i/10}]});await page.waitForTimeout(15);}
   await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await page.waitForTimeout(100);
  }
  await test('native textarea retained and toolbar does not cover output',async()=>{
   const result=await page.evaluate(()=>({connected:term.textarea.isConnected,screen:document.querySelector('.xterm-screen').getBoundingClientRect().bottom,toolbar:document.getElementById('mobile-controls').getBoundingClientRect().top}));
   assert.equal(result.connected,true);assert(result.screen<=result.toolbar,JSON.stringify(result));
  });
  await test('normal touch scroll is local and Latest sends no keystrokes',async()=>{
   await page.evaluate(async()=>{await new Promise(r=>term.write(Array.from({length:200},(_,i)=>'line '+i+'\r\n').join(''),r));term.scrollToBottom();window.testSends=[];});
   const before=await page.evaluate(()=>term.buffer.active.viewportY);
   await swipe(180,220,420);
   assert((await page.evaluate(()=>term.buffer.active.viewportY))<before);
   await page.evaluate(()=>jumpToBottom());
   assert.deepEqual(await page.evaluate(()=>window.testSends),[]);
   assert.equal(await page.evaluate(()=>jumpBtn.classList.contains('visible')),false);
  });
  await test('application touch scrolling uses structured requests and real coordinates',async()=>{
   await page.evaluate(async()=>{hasInput=true;await new Promise(r=>term.write('\x1b[?1049h\x1b[?1000h\x1b[?1006h',r));window.testSends=[];});
   await swipe(180,220,420);
   const reports=await page.evaluate(()=>window.testSends);
   assert(reports.length>0);const scrolls=reports.map(s=>JSON.parse(s));
   assert(scrolls.every(s=>s.type==='scroll'&&s.lines<0));
   assert(scrolls.every(s=>s.col>1&&s.row>1));
   assert.equal(await page.locator('#history-modal').evaluate(d=>d.open),false);
   await page.evaluate(async()=>{await new Promise(r=>term.write('\x1b[?1000l\x1b[?1006l',r));});
  });
  await test('alternate-screen scrolling works without mouse reporting',async()=>{
   await page.evaluate(()=>{hasInput=true;window.testSends=[];});
   await swipe(180,220,420);
   const scrolls=await page.evaluate(()=>window.testSends.map(s=>JSON.parse(s)));
   assert(scrolls.length>0);assert(scrolls.every(s=>s.type==='scroll'&&s.lines<0));
   assert.equal(await page.locator('#history-modal').evaluate(d=>d.open),false);
  });
  await test('touch keeps scrolling remote copy mode after alternate screen ends',async()=>{
   await page.evaluate(async()=>{
    await new Promise(r=>term.write('\x1b[?1049l\x1b[?1000l\x1b[?1006l',r));
    remoteHistoryPane='%1';hasInput=true;window.testSends=[];
   });
   await swipe(180,220,420);
   const sends=await page.evaluate(()=>window.testSends.map(s=>JSON.parse(s)));
   assert(sends.some(s=>s.type==='scroll'&&s.lines<0));
   assert(sends.every(s=>s.type==='scroll'));
  });
  await test('read-only alternate-screen swipe opens scrollable history without input',async()=>{
   await page.evaluate(async()=>{await new Promise(r=>term.write('\x1b[?1049h',r));hasInput=false;updateInputState();window.testSends=[];});
   await swipe(180,220,420);
   assert.equal(await page.locator('#history-modal').evaluate(d=>d.open),true);
   assert.match(await page.locator('#history-text').textContent(),/History line 199/);
   assert.deepEqual(await page.evaluate(()=>window.testSends),[]);
   await page.evaluate(()=>closeHistory());
  });
  await test('paste failure keeps draft; modal swipes send nothing',async()=>{
   await page.evaluate(()=>{showPasteModal();document.getElementById('paste-area').value='important draft\n'.repeat(40);sendPaste();window.testSends=[];});
   assert.equal(await page.locator('#paste-modal').evaluate(d=>d.open),true);
   assert.match(await page.locator('#paste-area').inputValue(),/important draft/);
   const b=await page.locator('#paste-area').boundingBox();await swipe(b.x+40,b.y+20,b.y+80);
   assert.deepEqual(await page.evaluate(()=>window.testSends),[]);
   await page.evaluate(()=>closePasteModal());
   await page.evaluate(()=>showPasteModal());assert.match(await page.locator('#paste-area').inputValue(),/important draft/);
   await page.evaluate(()=>closePasteModal());
  });
  await test('paste uses bracketed paste and preserves target session',async()=>{
   await page.evaluate(async()=>{hasInput=true;updateInputState();await new Promise(r=>term.write('\x1b[?2004h',r));showPasteModal();document.getElementById('paste-area').value='a\nb';window.testSends=[];sendPaste();});
   assert.deepEqual(await page.evaluate(()=>window.testSends),['\x1b[200~a\rb\x1b[201~']);
  });
  await test('one-shot Ctrl and Alt; application-mode arrow; shortcut panel',async()=>{
   await page.evaluate(()=>{window.testSends=[];toggleModifier('ctrl');term.input('c',true);toggleModifier('alt');term.input('f',true);});
   assert.deepEqual(await page.evaluate(()=>window.testSends),['\x03','\x1bf']);
   assert.deepEqual(await page.evaluate(()=>modifiers),{ctrl:false,alt:false});
   await page.evaluate(async()=>{await new Promise(r=>term.write('\x1b[?1h',r));window.testSends=[];sendTerminalKey('ArrowUp');});
   assert.deepEqual(await page.evaluate(()=>window.testSends),['\x1bOA']);
   await page.evaluate(()=>{window.testSends=[];openShortcuts();});
   await page.getByRole('button',{name:'Ctrl+C — Interrupt',exact:true}).click();
   assert.deepEqual(await page.evaluate(()=>window.testSends),['\x03']);
  });
  await test('Backspace works for click and touch without duplication',async()=>{
   await page.evaluate(()=>window.testSends=[]);await page.locator('#mob-bksp').click();
   assert.deepEqual(await page.evaluate(()=>window.testSends),['\x7f']);
   await page.evaluate(()=>window.testSends=[]);await page.locator('#mob-bksp').tap();
   assert.deepEqual(await page.evaluate(()=>window.testSends),['\x7f']);
  });
  await test('compact keys fit small iPhones and retain keyboard focus',async()=>{
   for(const width of [320,375]){
    await page.setViewportSize({width,height:667});
    const layout=await page.evaluate(()=>{
     const bar=document.getElementById('mobile-controls').getBoundingClientRect();
     return {height:bar.height,inside:[...document.querySelectorAll('#mobile-controls button')].every(b=>{
      const r=b.getBoundingClientRect();return r.width>0&&r.left>=0&&r.right<=innerWidth&&r.bottom<=bar.bottom;
     })};
    });
    assert(layout.inside,JSON.stringify(layout));assert(layout.height<95,JSON.stringify(layout));
   }
   await page.evaluate(()=>term.blur());
   await page.locator('#kbd-btn').tap();
   assert(await page.evaluate(()=>document.activeElement===term.textarea));
   await page.evaluate(()=>window.testSends=[]);
   for(const key of ['Escape','Tab','ArrowUp','ArrowLeft','ArrowDown','ArrowRight','Enter']){
    await page.locator(`[data-terminal-key="${key}"]`).tap();
    assert(await page.evaluate(()=>document.activeElement===term.textarea),key+' stole focus');
   }
   assert.deepEqual(await page.evaluate(()=>window.testSends),['\x1b','\t','\x1bOA','\x1bOD','\x1bOB','\x1bOC','\r']);
   await page.locator('#ctrl-key').tap();await page.evaluate(()=>term.input('c',true));
   assert.equal(await page.evaluate(()=>window.testSends.at(-1)),'\x03');
   await page.locator('#kbd-btn').tap();
   assert.equal(await page.evaluate(()=>document.activeElement===term.textarea),false);
  });
  await test('original action buttons open their controls',async()=>{
   await page.getByRole('button',{name:'Paste',exact:true}).tap();
   assert(await page.locator('#paste-modal').evaluate(d=>d.open));await page.evaluate(()=>closePasteModal());
   await page.getByRole('button',{name:'Select / History',exact:true}).tap();
   assert(await page.locator('#history-modal').evaluate(d=>d.open));await page.evaluate(()=>closeHistory());
   await page.getByRole('button',{name:'Shortcuts',exact:true}).tap();
   assert(await page.locator('#shortcut-modal').evaluate(d=>d.open));await page.evaluate(()=>closeDialog('shortcut-modal'));
   const chooser=page.waitForEvent('filechooser');await page.locator('#image-btn').tap();await chooser;
  });
  await test('stale close and data callbacks cannot affect a new session',async()=>{
   const result=await page.evaluate(()=>{
    const oldClose=ws.onclose,oldMessage=ws.onmessage;connect('beta');setConnected(true);
    oldClose();oldMessage({data:'WRONG SESSION'});
    return {status:document.getElementById('status-label').textContent,retry:reconnectTimer};
   });assert.equal(result.status,'connected');assert.equal(result.retry,null);
  });
  await test('failed session deletion retains current session and connection',async()=>{
   await page.route('**/sessions/beta',r=>r.fulfill({status:500,contentType:'application/json',body:'{"error":"test failure"}'}));
   const result=await page.evaluate(async()=>{window.confirm=()=>true;await killSession('beta');return {session:currentSession,open:ws.readyState===WebSocket.OPEN};});
   assert.deepEqual(result,{session:'beta',open:true});
  });
  await test('cancelled group removal makes no changes',async()=>{
   await page.evaluate(()=>{_layout={projects:['one','two'],assignments:{alpha:'one'}};window.removePromise=deleteProject('one');});
   await page.locator('#choice-modal button[value="cancel"]').click();
   await page.evaluate(()=>window.removePromise);
   assert.deepEqual(await page.evaluate(()=>({projects:getProjects(),assignments:getAssignments()})),{projects:['one','two'],assignments:{alpha:'one'}});
  });
  await test('upload finishing after session switch saves original-session draft',async()=>{
   let startedResolve;const started=new Promise(r=>startedResolve=r);
   await page.route('**/upload',async r=>{startedResolve();await new Promise(resolve=>setTimeout(resolve,200));await r.fulfill({contentType:'application/json',body:'{"ok":true,"path":"/tmp/test.png"}'});});
   await page.evaluate(()=>{currentServer='test';hasInput=true;updateInputState();window.testSends=[];window.uploadPromise=handleUpload({files:[new File(['test'],'test.png',{type:'image/png'})],value:'test.png'});});
   await started;await page.evaluate(()=>connect('gamma'));await page.evaluate(()=>window.uploadPromise);
   assert.equal(await page.evaluate(()=>draftBySession.get('test/beta')),'Attached image: /tmp/test.png');
   assert.equal(await page.evaluate(()=>window.testSends.some(s=>typeof s==='string'&&s.includes('Attached image'))),false);
  });
  await test('keyboard viewport changes do not throw',async()=>{await page.setViewportSize({width:390,height:500});await page.waitForTimeout(150);assert.deepEqual(errors,[]);});
  await test('tabs switch across servers and deduplicate destinations',async()=>{
   await page.evaluate(()=>{closeConnection();terminalTabs=[];currentServer='test';currentSession='';connect('alpha');connect('beta');connect('alpha');});
   assert.equal(await page.getByRole('tab').count(),2);
   await page.getByRole('tab',{name:'beta on test',exact:true}).tap();
   assert.equal(await page.evaluate(()=>currentSession),'beta');
   await page.evaluate(()=>{enterServer('other');connect('alpha');});
   assert.equal(await page.getByRole('tab').count(),3);
   await page.getByRole('tab',{name:'alpha on test',exact:true}).tap();
   assert.deepEqual(await page.evaluate(()=>({server:currentServer,session:currentSession})),{server:'test',session:'alpha'});
   assert((await page.evaluate(()=>ws.url)).includes('/ws/test/alpha?'));
   assert.equal(await page.getByRole('tab',{name:'alpha on test',exact:true}).getAttribute('aria-selected'),'true');
  });
  await test('drag handles reorder without switching or reconnecting terminals',async()=>{
   await page.setViewportSize({width:900,height:667});
   await page.evaluate(()=>window.socketBeforeReorder=ws);
   const grip=page.getByRole('button',{name:'Reorder alpha on test',exact:true});
   const a=await grip.boundingBox(),b=await page.getByRole('tab',{name:'alpha on other',exact:true}).boundingBox();
   const start={x:a.x+a.width/2,y:a.y+a.height/2},end={x:b.x+b.width,y:start.y};
   if(engine==='chromium'){
    await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[start]});
    for(let i=1;i<=10;i++)await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:start.x+(end.x-start.x)*i/10,y:start.y}]});
    await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
   }else{
    await page.mouse.move(start.x,start.y);await page.mouse.down();await page.mouse.move(end.x,end.y,{steps:10});await page.mouse.up();
   }
   assert.deepEqual(await page.evaluate(()=>terminalTabs.map(t=>t.server+'/'+t.session)),['test/beta','other/alpha','test/alpha']);
   assert(await page.evaluate(()=>ws===window.socketBeforeReorder&&currentServer==='test'&&currentSession==='alpha'));
   await grip.focus();await page.keyboard.press('Home');
   assert.deepEqual(await page.evaluate(()=>terminalTabs.map(t=>t.server+'/'+t.session)),['test/alpha','test/beta','other/alpha']);
   assert(await page.evaluate(()=>ws===window.socketBeforeReorder));
   await page.setViewportSize({width:390,height:667});
  });
  await test('closing tabs keeps sessions alive and selects an adjacent tab',async()=>{
   const deletes=[];const watch=r=>{if(r.method()==='DELETE')deletes.push(r.url());};page.on('request',watch);
   await page.getByRole('button',{name:'Close tab beta on test',exact:true}).tap();
   assert.equal(await page.evaluate(()=>currentSession),'alpha');
   await page.getByRole('button',{name:'Close tab alpha on test',exact:true}).tap();
   assert.deepEqual(await page.evaluate(()=>({server:currentServer,session:currentSession})),{server:'other',session:'alpha'});
   await page.getByRole('button',{name:'Close tab alpha on other',exact:true}).tap();
   assert.equal(await page.locator('#terminal-tabs').isVisible(),false);
   assert.equal(await page.evaluate(()=>currentSession),'');assert.deepEqual(deletes,[]);page.off('request',watch);
  });
  await test('mobile tabs overflow horizontally and Plus opens the session picker',async()=>{
   await page.setViewportSize({width:320,height:667});
   await page.evaluate(()=>{currentServer='test';for(let i=0;i<8;i++)connect('long-terminal-name-'+i);});
   const layout=await page.evaluate(()=>{
    const list=document.getElementById('terminal-tab-list'),tab=list.querySelector('.active');
    const a=tab.getBoundingClientRect(),b=list.getBoundingClientRect();
    const top=document.getElementById('topbar').getBoundingClientRect();
    return {overflow:list.scrollWidth>list.clientWidth,visible:a.left>=b.left-1&&a.right<=b.right+1,pageWidth:document.documentElement.scrollWidth,viewport:innerWidth,inTopbar:b.top>=top.top&&b.bottom<=top.bottom,topHeight:top.height};
   });assert(layout.overflow&&layout.visible,JSON.stringify(layout));assert(layout.pageWidth<=layout.viewport,JSON.stringify(layout));assert(layout.inTopbar&&layout.topHeight===48,JSON.stringify(layout));
   await page.getByRole('button',{name:'Open another terminal',exact:true}).tap();
   assert(await page.evaluate(()=>sidebar.classList.contains('open')));await page.evaluate(()=>closeSidebar());
  });
  await test('tab shortcuts change sessions without sending terminal keys',async()=>{
   await page.evaluate(()=>window.testSends=[]);
   await page.keyboard.press('Alt+Shift+ArrowLeft');
   assert.equal(await page.evaluate(()=>currentSession),'long-terminal-name-6');
   assert(await page.evaluate(()=>window.testSends.every(s=>typeof s==='string'&&s.startsWith('{'))));
  });
  await test('reordering scrolls past hidden tabs and Escape cancels a drag',async()=>{
   await page.evaluate(()=>{window.socketBeforeReorder=ws;document.getElementById('terminal-tab-list').scrollLeft=0;});
   const handle=page.getByRole('button',{name:'Reorder long-terminal-name-0 on test',exact:true});
   const a=await handle.boundingBox(),list=await page.locator('#terminal-tab-list').boundingBox();
   await page.mouse.move(a.x+a.width/2,a.y+a.height/2);await page.mouse.down();
   await page.mouse.move(list.x+list.width-2,a.y+a.height/2,{steps:5});
   await page.waitForFunction(()=>document.getElementById('terminal-tab-list').scrollLeft>250);
   await page.mouse.up();
   assert((await page.evaluate(()=>terminalTabs.findIndex(t=>t.session==='long-terminal-name-0')))>0);
   const before=await page.evaluate(()=>JSON.stringify(terminalTabs));
   const moved=await handle.boundingBox();
   await page.mouse.move(moved.x+12,moved.y+20);await page.mouse.down();await page.mouse.move(moved.x+40,moved.y+20);
   await page.keyboard.press('Escape');await page.mouse.up();
   assert.equal(await page.evaluate(()=>JSON.stringify(terminalTabs)),before);
   assert(await page.evaluate(()=>ws===window.socketBeforeReorder&&cancelTerminalTabDrag===null));
  });
  await test('tabs, reordered positions and the active destination survive reload',async()=>{
   const order=await page.evaluate(()=>terminalTabs);
   await page.route('**/api/servers',r=>r.fulfill({contentType:'application/json',body:JSON.stringify([{name:'test',connected:true,sessions:[]}])}));
   await page.route('**/api/test/sessions',r=>r.fulfill({contentType:'application/json',body:JSON.stringify([{name:'long-terminal-name-6'}])}));
   await page.reload({waitUntil:'networkidle'});
   await page.waitForFunction(()=>currentSession==='long-terminal-name-6');
   assert.equal(await page.getByRole('tab').count(),8);
   assert.deepEqual(await page.evaluate(()=>terminalTabs),order);
   assert.equal(await page.getByRole('tab',{name:'long-terminal-name-6 on test',exact:true}).getAttribute('aria-selected'),'true');
   assert.deepEqual(errors,[]);
   if(process.env.SCREENSHOT_PATH)await page.screenshot({path:process.env.SCREENSHOT_PATH.replace('.png','-tabs.png')});
  });
  const desktop=await makePage(false);
  await test('narrow desktop sidebar and stats toggle open',async()=>{
   await desktop.page.evaluate(()=>{toggleSidebar();toggleRightPanel();});
   await desktop.page.waitForFunction(()=>document.getElementById('sidebar').getBoundingClientRect().left>=0);
   const result=await desktop.page.evaluate(()=>{const sidebar=document.getElementById('sidebar'),panel=document.getElementById('right-panel');return {left:sidebar.getBoundingClientRect().left,display:getComputedStyle(panel).display};});
   assert(result.left>=0);assert.equal(result.display,'flex');assert.deepEqual(desktop.errors,[]);
  });
  console.log(`${passed.length} browser regression checks passed`);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
