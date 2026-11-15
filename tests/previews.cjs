const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
(async()=>{
 const browser=await browserType.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
  const page=await context.newPage(),errors=[],requests=[];let rows=[];
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',async route=>{
   const url=new URL(route.request().url()),method=route.request().method();
   if(url.hostname.endsWith('-preview.test'))return route.fulfill({contentType:'text/html',body:'<h1>Application preview</h1>'});
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:`<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><div id="session-list"></div><button id="localhost" onclick="bridgePreviews.openLocal('http://localhost:3000/review?x=1#tab')">http://localhost:3000/review</button><script>let currentServer='mini',currentSession='work';window.notifications=[];function notifyUser(m){notifications.push(m)}</script><script type="module" src="/previews.js"></script>`});
   if(url.pathname==='/previews.js')return route.fulfill({contentType:'application/javascript',body:fs.readFileSync(path.join(__dirname,'../tmux_bridge_assets/previews.js'),'utf8')});
   if(url.pathname==='/api/previews')return route.fulfill({json:{enabled:true,previews:rows}});
   if(method==='POST'){
    const body=route.request().postDataJSON();requests.push(body);
    const row={...body,id:'a'.repeat(32),server:'mini',url:'https://'+'a'.repeat(32)+'-preview.test/',expires:Date.now()/1000+7200};rows=[row];return route.fulfill({json:row});
   }
   if(method==='DELETE'){rows=[];return route.fulfill({json:{stopped:true}});}
   return route.fulfill({status:404});
  });
  await page.goto('https://gateway.test/');await page.locator('#preview-panel').waitFor({state:'visible'});
  const popupPromise=context.waitForEvent('page');await page.locator('#localhost').click();
  const popup=await popupPromise;await popup.waitForURL('**/review?x=1#tab');
  assert.equal(await popup.evaluate(()=>window.opener),null);
  assert.equal(requests[0].port,3000);assert.equal(requests[0].session,'work');
  await page.locator('#preview-panel summary').click();
  await page.getByRole('button',{name:'Stop preview',exact:false}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.preview-row').length===0);
  await page.getByRole('spinbutton',{name:'Application port'}).fill('5173');
  await page.getByRole('button',{name:'Open',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.preview-row').length===1);
  assert.equal(requests[1].port,5173);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);
  await context.close();console.log(engine+': phone preview links, new-tab isolation, expiry display, stop, and manual port passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
