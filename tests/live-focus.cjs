// Opt-in: creates and removes only its own session on the selected test node.
const assert=require('node:assert/strict');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const base=process.env.BRIDGE_URL||'http://127.0.0.1:4601',server=process.env.BRIDGE_SERVER||'test-node';
const name='bridge-focus-check-'+Date.now()+'-'+process.pid;
const quote=s=>"'"+s.replace(/'/g,"'\\''")+"'";
async function api(method,body){
 const response=await fetch(base+'/api/'+server+'/sessions/'+name,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
 const data=await response.json();assert(response.ok,JSON.stringify(data));return data;
}
(async()=>{
 let browser,created=false;
 try{
  const command='python3 -u -c '+quote("import os,tty\ntty.setraw(0)\nos.write(1,b'FOCUS_READY\\r\\n')\nwhile True:\n data=os.read(0,4096)\n if not data:break\n os.write(1,data)\n");
  assert((await api('POST',{command})).created);created=true;
  browser=await browserType.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:800}});
  await page.goto(base,{waitUntil:'networkidle'});
  await page.evaluate(async server=>{enterServer(server);await loadSessions();},server);
  await page.locator('[data-session="'+name+'"] .s-name').click();
  await page.waitForFunction(()=>hasInput&&localHistory().includes('FOCUS_READY'),{},{timeout:20000});
  assert(await page.evaluate(()=>document.activeElement===term.textarea));
  await page.keyboard.type('FOCUS_INPUT_OK');
  await page.waitForFunction(()=>localHistory().includes('FOCUS_INPUT_OK'));
  await page.getByRole('tab',{name:name+' on '+server,exact:true}).click();
  assert(await page.evaluate(()=>document.activeElement===term.textarea));
  await page.keyboard.type('_AGAIN');
  await page.waitForFunction(()=>localHistory().includes('FOCUS_INPUT_OK_AGAIN'));
  console.log('PASS live desktop session selection and tab click accept typing without an extra terminal click');
 }finally{
  if(browser)await browser.close();
  if(created)await api('DELETE');
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
