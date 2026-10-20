// Real browser selection, clipboard gestures, safe links, and stable snapshots.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const html=fs.readFileSync(path.join(__dirname,'../gateway.py'),'utf8').split('HTML = r"""')[1].split('"""')[0];
const transcript='Select this sentence and copy it.\nhttps://example.com/path?q=hello&next=world\nwww.example.org/docs\n<script>window.injected=true</script>\njavascript:alert(1)\nMore text with https://example.com/a_(b).\n';
(async()=>{
 const browser=await browserType.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1280,height:800},hasTouch:true});
  let holdHistory=false,releaseHistory;
  await context.route('**/*',async r=>{
   const url=r.request().url();
   if(url.startsWith('https://cdn.jsdelivr.net/'))return r.continue();
   if(url==='https://inspection.test/')return r.fulfill({contentType:'text/html',body:html});
   if(url.includes('/history/')&&holdHistory)await new Promise(resolve=>releaseHistory=resolve);
   return r.fulfill({contentType:'application/json',body:JSON.stringify(url.includes('/history/')?{ok:true,text:transcript}:url.includes('/layout')?{}:[])});
  });
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
   window.testSends=[];window.copies=[];window.openedLinks=[];
   Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>window.copies.push(text)}});
   window.open=(url,target,features)=>{window.openedLinks.push({url,target,features});return null;};
   window.WebSocket=class{
    static OPEN=1;
    constructor(){this.readyState=1;setTimeout(()=>this.onopen?.(),0);}
    send(data){window.testSends.push(data);}
    close(){this.readyState=3;}
   };
  });
  await page.goto('https://inspection.test/',{waitUntil:'networkidle'});
  await page.evaluate(async()=>{
   currentServer='test';onServer=true;cachedSessions=[{name:'selection-test'}];presenceData={};_layout={};
   document.getElementById('home-view').classList.remove('visible');
   document.getElementById('server-landing').classList.remove('visible');
   connect('selection-test');hasInput=true;updateInputState();fitAddon.fit();
   await new Promise(r=>term.write('\x1b[?1049h\x1b[?1000h\x1b[?1006hLive text to copy\r\n\x1b]8;;https://example.com/hidden\x07Friendly label\x1b]8;;\x07',r));
   window.testSends=[];
  });
  const liveBox=await page.locator('.xterm-screen').boundingBox();
  const liveRows=await page.evaluate(()=>term.rows);

  await page.mouse.move(liveBox.x+1,liveBox.y+liveBox.height/liveRows*.5);await page.mouse.down();
  await page.mouse.move(liveBox.x+65,liveBox.y+liveBox.height/liveRows*.5,{steps:10});await page.mouse.up();

  const liveSelection=await page.evaluate(()=>term.getSelection());
  assert(liveSelection.includes('Live'),JSON.stringify(liveSelection));
  await page.keyboard.press('Meta+c');
  assert.equal(await page.evaluate(()=>window.copies.at(-1)),liveSelection);
  assert.deepEqual(await page.evaluate(()=>window.testSends),[]);
  console.log('PASS plain drag selects live terminal text and Cmd+C copies it');
  await page.getByRole('button',{name:'Select / History',exact:true}).click();
  await page.waitForFunction(()=>historyText.includes('Select this sentence'));
  const box=await page.locator('#history-text').boundingBox();
  await page.mouse.move(box.x+13,box.y+22);await page.mouse.down();
  await page.mouse.move(box.x+210,box.y+22,{steps:12});await page.mouse.up();
  const selected=await page.evaluate(()=>historySelection());
  assert(selected.includes('Select this'),JSON.stringify(selected));
  await page.getByRole('button',{name:'Copy selection',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.copies.at(-1)),selected);
  assert.deepEqual(await page.evaluate(()=>window.testSends),[]);
  console.log('PASS native drag and Copy work even when the terminal app captures mouse input');

  const urls=await page.locator('#history-text a').evaluateAll(links=>links.map(a=>({href:a.href,rel:a.rel,target:a.target})));
  assert.equal(urls.length,3);assert(urls.every(a=>a.href.startsWith('https:')&&a.rel.includes('noopener')&&a.target==='_blank'));
  assert(urls.some(a=>a.href==='https://example.com/a_(b)'));
  assert.equal(await page.evaluate(()=>window.injected),undefined);
  await page.getByRole('button',{name:'Copy all loaded',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.copies.at(-1)),transcript);
  console.log('PASS links preserve URLs; terminal markup stays inert; Copy all preserves exact text');

  await page.locator('#history-search').fill('hello&next');
  await page.getByRole('button',{name:'Find next',exact:true}).click();
  assert.equal(await page.evaluate(()=>historySelection()),'hello&next');
  await page.getByRole('button',{name:'Copy selection',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.copies.at(-1)),'hello&next');
  console.log('PASS search selects text across linkified output and copies it');

  await page.evaluate(()=>closeHistory());
  await page.evaluate(()=>showHistory(false,true));
  await page.waitForFunction(()=>historyText.includes('Select this sentence'));
  assert(await page.locator('#history-links').evaluate(d=>d.open));
  assert.equal(await page.locator('#history-link-list a[href="https://example.com/hidden"]').count(),1);
  const popupPromise=page.waitForEvent('popup');
  await page.locator('#history-link-list a').first().click();
  const popup=await popupPromise;await popup.waitForLoadState();
  assert.equal(popup.url(),'https://example.com/path?q=hello&next=world');
  assert.equal(await popup.evaluate(()=>window.opener),null);await popup.close();
  console.log('PASS Links opens an ordinary browser link with no opener access');

  await page.evaluate(()=>closeHistory());
  await page.evaluate(()=>{term.select(0,0,9);window.copies=[];window.testSends=[];term.focus();});
  await page.keyboard.press('Meta+c');
  assert.equal(await page.evaluate(()=>window.copies.at(-1)),'Live text');
  assert.deepEqual(await page.evaluate(()=>window.testSends),[]);
  await page.evaluate(()=>{term.clearSelection();term.focus();});
  await page.keyboard.press('Control+c');
  assert.equal(await page.evaluate(()=>window.testSends.at(-1)),'\x03');
  console.log('PASS Cmd+C copies selection while Ctrl+C still interrupts the terminal');

  await page.evaluate(async()=>{
   term.reset();await new Promise(r=>term.write('https://example.com/plain\r\n\x1b]8;;https://example.com/osc\x07Friendly link\x1b]8;;\x07',r));
   window.openedLinks=[];window.testSends=[];
  });
  const screen=await page.locator('.xterm-screen').boundingBox(),rows=await page.evaluate(()=>term.rows);
  // This pinned xterm caches empty link results per row; enter a different
  // row after replacing the screen. The separate Links view has no such cache.
  await page.mouse.move(screen.x+35,screen.y+screen.height/rows*2.5);await page.waitForTimeout(150);
  await page.mouse.move(screen.x+35,screen.y+screen.height/rows*.5);
  await page.waitForFunction(()=>document.getElementById('terminal-container').title==='https://example.com/plain');
  await page.mouse.click(screen.x+35,screen.y+screen.height/rows*.5);
  await page.mouse.move(screen.x+35,screen.y+screen.height/rows*1.5);
  await page.waitForFunction(()=>document.getElementById('terminal-container').title==='https://example.com/osc');
  await page.mouse.click(screen.x+35,screen.y+screen.height/rows*1.5);
  const opened=await page.evaluate(()=>window.openedLinks);
  assert(opened.some(l=>l.url==='https://example.com/plain'),JSON.stringify(opened));
  assert(opened.some(l=>l.url==='https://example.com/osc'),JSON.stringify(opened));
  assert(opened.every(l=>l.features.includes('noopener')));
  await page.evaluate(()=>term.options.linkHandler.activate(new MouseEvent('click'),'javascript:alert(1)'));
  assert.equal((await page.evaluate(()=>window.openedLinks)).length,2);
  console.log('PASS actual terminal URL and OSC-8 link clicks open safely without confirmation prompts');

  holdHistory=true;
  await page.evaluate(async()=>{term.reset();await new Promise(r=>term.write('Keep this selected text',r));});
  await page.getByRole('button',{name:'Select / History',exact:true}).click();
  await page.waitForFunction(()=>historyText.includes('Keep this selected text'));
  await page.locator('#history-text').dispatchEvent('pointerdown');
  await page.evaluate(()=>{
   const node=document.getElementById('history-text').firstChild,range=document.createRange();
   range.setStart(node,0);range.setEnd(node,9);const s=window.getSelection();s.removeAllRanges();s.addRange(range);
  });
  releaseHistory();holdHistory=false;
  await page.waitForFunction(()=>pendingHistoryText!==null);
  assert.equal(await page.evaluate(()=>historySelection()),'Keep this');
  await page.getByRole('button',{name:'Load full history',exact:true}).click();
  assert.equal(await page.evaluate(()=>historyText),transcript);
  console.log('PASS delayed full history cannot overwrite an in-progress selection');

  await page.evaluate(()=>{window.fallbackCopies=[];navigator.clipboard.writeText=async()=>{throw Error('denied');};document.execCommand=cmd=>{window.fallbackCopies.push(document.activeElement.value);return cmd==='copy';};});
  await page.getByRole('button',{name:'Copy all loaded',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.fallbackCopies.at(-1)),transcript);
  console.log('PASS clipboard fallback works inside the modal');

  await page.evaluate(()=>closeHistory());
  await page.setViewportSize({width:375,height:667});
  await page.getByRole('button',{name:'Select / History',exact:true}).tap();
  await page.waitForFunction(()=>historyText.includes('Select this sentence'));
  assert.equal(await page.locator('#history-text').evaluate(e=>getComputedStyle(e).getPropertyValue('user-select')||getComputedStyle(e).getPropertyValue('-webkit-user-select')),'text');
  assert.equal(await page.locator('#history-text').evaluate(e=>getComputedStyle(e).touchAction),'pan-y pinch-zoom');
  assert.equal(await page.locator('#terminal-actions').count(),0);
  await page.evaluate(()=>{
   const range=document.createRange(),node=document.getElementById('history-text').firstChild;
   range.setStart(node,0);range.setEnd(node,11);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
  });
  await page.getByRole('button',{name:'Copy selection',exact:true}).tap();
  assert.equal(await page.evaluate(()=>window.fallbackCopies.at(-1)),'Select this');
  assert(await page.locator('#history-modal').evaluate(d=>{
   const rect=d.getBoundingClientRect(),title=d.querySelector('h2').getBoundingClientRect(),actions=d.querySelector('.dialog-actions').getBoundingClientRect();
   return title.top>=rect.top && actions.bottom<=rect.bottom;
  }));
  await page.screenshot({path:'/tmp/bridge-selection-'+engine+'.png'});
  assert.deepEqual(errors,[]);
  console.log('PASS touch-sized selection controls fit and retain native text selection');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
