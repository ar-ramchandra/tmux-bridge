// Desktop session navigation must leave the terminal ready for normal typing.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const html=fs.readFileSync(path.join(__dirname,'../gateway.py'),'utf8').split('HTML = r"""')[1].split('"""')[0];
(async()=>{
 const browser=await browserType.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1280,height:800}});
  await page.route('**/*',r=>{
   const u=r.request().url();if(u.startsWith('https://cdn.jsdelivr.net/'))return r.continue();
   return r.fulfill({contentType:u==='https://inspection.test/'?'text/html':'application/json',body:u==='https://inspection.test/'?html:u.includes('/layout')?'{}':'[]'});
  });
  await page.addInitScript(()=>{
   window.sent=[];
   window.WebSocket=class {
    static OPEN=1;
    constructor(){this.readyState=1;setTimeout(()=>{this.onopen?.();hasInput=true;updateInputState();},0);}
    send(data){window.sent.push(data);}close(){this.readyState=3;}
   };
  });
  await page.goto('https://inspection.test/',{waitUntil:'networkidle'});
  await page.evaluate(()=>{
   currentServer='test';onServer=true;cachedSessions=[{name:'focus-one'},{name:'focus-two'}];presenceData={};_layout={};
   document.getElementById('home-view').classList.remove('visible');
   document.getElementById('server-landing').classList.remove('visible');renderSidebar();
  });
  async function type(text){
   await page.waitForFunction(()=>hasInput);
   assert(await page.evaluate(()=>document.activeElement===term.textarea),'Terminal should have keyboard focus after session navigation');
   await page.evaluate(()=>window.sent=[]);await page.keyboard.type(text);
   assert.equal(await page.evaluate(()=>window.sent.filter(x=>typeof x==='string'&&!x.startsWith('{')).join('')),text);
  }
  await page.locator('[data-session="focus-one"] .s-name').click();await type('first');
  await page.locator('[data-session="focus-two"] .s-name').click();await type('second');
  await page.getByRole('tab',{name:'focus-one on test',exact:true}).click();await type('third');
  await page.evaluate(()=>{term.blur();renderSidebar();});
  await page.locator('[data-session="focus-one"] .s-name').click();await type('same');
  console.log('PASS desktop session selection, tab switching, and reselecting restore typing focus');
  await page.getByRole('tab',{name:'focus-two on test',exact:true}).focus();
  await page.keyboard.press('ArrowLeft');
  assert.equal(await page.locator('#terminal-tab-list [aria-selected="true"]').evaluate(el=>document.activeElement===el),true);
  console.log('PASS keyboard tab-list navigation retains accessible tab focus');
  await page.evaluate(()=>showPasteModal());
  await page.locator('#paste-area').fill('keep draft');
  await page.evaluate(()=>updateInputState());
  assert.equal(await page.locator('#paste-area').evaluate(el=>document.activeElement===el),true);
  console.log('PASS presence updates do not steal focus from a draft');
  await page.evaluate(()=>closeDialog('paste-modal'));
  await page.setViewportSize({width:375,height:667});
  await page.waitForFunction(()=>isTouchDevice);
  await page.evaluate(()=>{term.blur();openTerminalPicker();});
  await page.locator('[data-session="focus-one"] .s-name').click();
  assert(await page.evaluate(()=>document.activeElement!==term.textarea));
  console.log('PASS mobile session navigation does not force the virtual keyboard open');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
