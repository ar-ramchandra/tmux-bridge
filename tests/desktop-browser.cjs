// Exercise authentication lifecycle without connecting to a personal desktop.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const source=fs.readFileSync(path.join(__dirname,'../tmux_bridge_assets/desktop.js'),'utf8');
(async()=>{
 const browser=await require('playwright')[engine].launch({headless:true});
 try{
  const page=await browser.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>{
   const u=new URL(r.request().url());
   if(u.pathname==='/desktop.js')return r.fulfill({contentType:'application/javascript',body:source});
   if(u.pathname==='/desktop-assets/core/rfb.js')return r.fulfill({contentType:'application/javascript',body:`
    export default class RFB extends EventTarget {
     constructor(){super();this.sent=[];window.rfb=this;}
     sendCredentials(value){this.sent.push({...value});}
     disconnect(){this.dispatchEvent(new Event('disconnect'));}
    }`});
   if(u.pathname.startsWith('/api/'))return r.fulfill({json:{available:true}});
   return r.fulfill({contentType:'text/html',body:`<div id="welcome-item"></div>
    <script>let currentServer='macstudio',onServer=true;window.WebSocket=class extends EventTarget {};</script>
    <script type="module" src="/desktop.js"></script>`});
  });
  await page.goto('https://desktop.test');
  const open=async(server='macstudio')=>{await page.evaluate(()=>window.rfb=null);await page.evaluate(s=>window.bridgeDesktop.open(s),server);await page.waitForFunction(()=>!!window.rfb);};
  const event=async(type,types=['username','password'])=>{await page.evaluate(({type,types})=>window.rfb.dispatchEvent(new CustomEvent(type,{detail:{types}})),{type,types});if(type==='connect')await page.waitForFunction(()=>document.querySelector('#desktop-status').textContent==='Connected · Tap to click; use Keyboard to type.');};
  const signIn=async(user='test-user',pass='test-password')=>{
   await page.getByLabel('Desktop username').fill(user);
   await page.getByLabel('Desktop password').fill(pass);
   await page.getByRole('button',{name:'Sign in',exact:true}).click();
  };
  const hidden=()=>page.locator('form').evaluate(e=>e.hidden);
  await open();await event('credentialsrequired');await signIn();await event('connect');
  assert.equal(await hidden(),true);
  await page.getByRole('button',{name:'Close',exact:true}).click();
  await open();await event('credentialsrequired');
  assert.deepEqual(await page.evaluate(()=>rfb.sent),[{username:'test-user',password:'test-password'}]);
  assert.equal(await hidden(),true);
  console.log('PASS successful credentials reused on reopen');

  await open('other');await event('credentialsrequired');
  assert.deepEqual(await page.evaluate(()=>rfb.sent),[]);assert.equal(await hidden(),false);
  console.log('PASS credentials isolated between devices');

  await open();await event('credentialsrequired');await event('securityfailure');
  await open();await event('credentialsrequired');
  assert.deepEqual(await page.evaluate(()=>rfb.sent),[]);assert.equal(await hidden(),false);
  await signIn('wrong','wrong');await event('disconnect');
  await open();await event('credentialsrequired');
  assert.deepEqual(await page.evaluate(()=>rfb.sent),[]);
  console.log('PASS rejected and unconfirmed credentials not reused');

  await signIn();await event('connect');await open();await event('credentialsrequired');await event('credentialsrequired');
  assert.equal((await page.evaluate(()=>rfb.sent)).length,1);assert.equal(await hidden(),false);
  console.log('PASS repeated challenge cannot cause an automatic login loop');

  await signIn();await event('connect');
  assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
  await page.reload();await open();await event('credentialsrequired');
  assert.deepEqual(await page.evaluate(()=>rfb.sent),[{username:'test-user',password:'test-password'}]);
  assert.equal(await page.getByLabel('Desktop username').getAttribute('autocomplete'),'section-desktop-macstudio username');
  assert.equal(await page.getByLabel('Desktop password').getAttribute('autocomplete'),'section-desktop-macstudio current-password');
  console.log('PASS remembered login survives reload without localStorage or sessionStorage secrets');
  await open('temporary');await event('credentialsrequired');
  await page.getByLabel('Remember on this browser').uncheck();await signIn();await event('connect');
  await open('temporary');await event('credentialsrequired');await event('connect');
  await page.reload();await open('temporary');await event('credentialsrequired');
  assert.deepEqual(await page.evaluate(()=>rfb.sent),[]);
  console.log('PASS opting out prevents persistence');
  await open('no-storage');await event('credentialsrequired');
  await page.evaluate(()=>{Object.defineProperty(IDBFactory.prototype,'open',{value:()=>{throw new Error('Storage blocked');},configurable:true});});
  await signIn();
  await page.evaluate(()=>rfb.dispatchEvent(new Event('connect')));
  await page.getByRole('status').filter({hasText:'could not save your sign-in'}).waitFor();
  console.log('PASS storage failure is visible instead of claiming sign-in was saved');
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
