// Opt-in: exercises a real managed desktop; never enters or records a password.
const engineName=process.env.BROWSER_ENGINE||'chromium';
const engine=require(process.env.PLAYWRIGHT_PATH||'playwright')[engineName];
const server=process.env.BRIDGE_SERVER;
if(!server||!/^[a-z][a-z0-9-]{0,47}$/.test(server))throw new Error('Set BRIDGE_SERVER explicitly to the authorized test node');
(async()=>{
 const base=process.env.BRIDGE_URL||'http://127.0.0.1:4601';
 for(const [name,browserType] of [[engineName,engine]]){
  const browser=await browserType.launch({headless:true});
  try{
   const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   for(let attempt=0;attempt<3;attempt++){
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(server=>!!window.bridgeDesktop&&typeof cachedServerList!=='undefined'&&cachedServerList.some(s=>s.name===server&&s.connected),server);
    await page.evaluate(server=>enterServer(server),server);
    await page.evaluate(server=>window.bridgeDesktop.open(server),server);
    try{
     await page.waitForFunction(()=>document.querySelector('#desktop-status')?.textContent==='Connected · Swipe to pan · Tap to click · Two fingers to scroll',{},{timeout:25000});
     await page.waitForFunction(()=>{const c=document.querySelector('#desktop-screen canvas');if(!c||c.width<100||c.height<100)return false;const p=c.getContext('2d').getImageData(0,0,c.width,c.height).data;const colors=new Set();for(let i=0;i<p.length;i+=4000)colors.add(p[i]+','+p[i+1]+','+p[i+2]);return colors.size>3;},{},{timeout:20000});
    }catch(e){console.log(server+' failed status:',await page.locator('#desktop-status').textContent());throw e;}
    const state=await page.evaluate(()=>{const canvas=document.querySelector('#desktop-screen canvas');return {credentialsHidden:document.querySelector('form.desktop-credentials').hidden,width:canvas.width,height:canvas.height,displayWidth:canvas.getBoundingClientRect().width};});
    if(!state.credentialsHidden)throw Error('Unexpected credential form');
    if(state.width>390||Math.abs(state.displayWidth-state.width)>1)throw Error('Phone desktop is scaled instead of readable');
    console.log(server,name,'attempt',attempt+1,'automatic login; framebuffer',state.width+'x'+state.height);
    await page.evaluate(()=>window.bridgeDesktop.close());await page.waitForTimeout(200);
   }
   if(errors.length)throw Error(errors.join('; '));
  }finally{await browser.close();}
 }
})().catch(e=>{console.error(e.message);process.exitCode=1;});
