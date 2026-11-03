const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const script=fs.readFileSync(path.join(__dirname,'../tmux_bridge_assets/accounts.js'),'utf8');
(async()=>{
 const browser=await browserType.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const now=Date.now()/1000,requests=[];let loginError=false;
  const account=(time,remaining)=>({provider:'codex',account_key:'a'.repeat(64),label:'pe…@example.com',plan:'pro',status:'ok',source:'codex app-server',observed_at:time,stale:false,windows:[{label:'Weekly',remaining_percent:remaining,resets_at:now+3600,reset_passed:false}]});
  const nodes=[{server:'studio',connected:true,accounts:[account(now-60,70),{provider:'claude',account_key:'b'.repeat(64),label:'<img src=x onerror=alert(1)>',status:'ok',observed_at:now-3600,stale:true,windows:[{label:'5 hours',remaining_percent:50,resets_at:now-1,reset_passed:true}]}]},
   {server:'hpc',connected:true,accounts:[account(now,67)]}];
  nodes[1].accounts[0].windows.push({label:'5 hours',remaining_percent:20,resets_at:now+600});
  await page.route('**/*',route=>{
   const pathname=new URL(route.request().url()).pathname;
   if(pathname==='/')return route.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><input id="terminal" value="unsent draft"><div id="right-panel" class="open"><div id="rp-content"><div class="gauge-grid">CPU 10% · Memory 20%</div></div></div><script>let currentServer="studio";let openedSession="";async function loadSessions(){};function connect(s){openedSession=s};function closeSidebar(){};</script><script src="/accounts.js"></script>'});
   if(pathname==='/accounts.js')return route.fulfill({contentType:'application/javascript',body:script});
   if(pathname.includes('/account-login/')){requests.push([pathname,route.request().method()]);return route.fulfill({status:loginError?409:200,contentType:'application/json',body:JSON.stringify(loginError?{error:'CLI is not installed on this device'}:{ok:true,session:'bridge-signin-claude'})});}
   return route.fulfill({contentType:'application/json',body:JSON.stringify({nodes})});
  });
  await page.goto('https://usage.test/',{waitUntil:'networkidle'});
  await page.locator('#terminal').focus();
  assert.equal(await page.locator('.usage-account').count(),2);
  assert.match(await page.locator('#account-usage').textContent(),/67% remaining/);
  assert.match(await page.locator('#account-usage').textContent(),/studio, hpc · shared allowance/);
  assert.match(await page.locator('#account-usage').textContent(),/Awaiting refreshed limit/);
  assert.match(await page.locator('#account-usage').textContent(),/Stale/);
  assert.equal(await page.locator('#account-usage [role=meter]').count(),2);
  assert.equal(await page.getByRole('meter',{name:'Weekly remaining',exact:true}).getAttribute('aria-valuenow'),'67');
  assert.equal(await page.getByRole('meter',{name:'5 hours remaining',exact:true}).getAttribute('aria-valuenow'),'20');
  assert.equal(await page.locator('#account-usage img').count(),0);
  assert.equal(await page.locator('#terminal').inputValue(),'unsent draft');
  await page.evaluate(()=>{bridgeAccounts.render();bridgeAccounts.render();});
  assert.equal(await page.evaluate(()=>document.activeElement.id),'terminal');
  await page.evaluate(()=>{currentServer='hpc';bridgeAccounts.render();});
  assert.equal(await page.locator('.usage-account').count(),1);
  assert.match(await page.locator('#account-usage').textContent(),/67% remaining/);
  await page.locator('.usage-login summary').click();
  await page.evaluate(()=>bridgeAccounts.render());
  assert.equal(await page.locator('.usage-login').getAttribute('open'),'');
  await page.getByRole('button',{name:'Claude Code',exact:true}).click();
  await page.waitForFunction(()=>openedSession==='bridge-signin-claude');
  assert.deepEqual(requests,[['/api/hpc/account-login/claude','POST']]);
  assert.equal(await page.locator('#terminal').inputValue(),'unsent draft');
  assert.equal(await page.locator('iframe').count(),0);
  loginError=true;
  await page.locator('.usage-login summary').click();
  await page.getByRole('button',{name:'Codex',exact:true}).click();
  await page.getByRole('status').filter({hasText:'CLI is not installed'}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Codex',exact:true}).isEnabled(),true);
  assert.deepEqual(errors,[]);
  console.log('PASS account doughnuts, grouping, stale resets, escaping, node switching, draft focus and isolated remote login/error recovery');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
