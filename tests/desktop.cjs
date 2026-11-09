// Real noVNC browser handshake, drawing and input against a local fake desktop.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const playwright=require(process.env.PLAYWRIGHT_PATH||'playwright');
const {wsServer}=require(path.join(path.dirname(require.resolve('playwright-core/package.json')),'lib/utilsBundle.js'));
const assets=path.join(__dirname,'../tmux_bridge_assets');
const crypto=require('node:crypto'),os=require('node:os');
const events=[],authMode=process.env.AUTH_MODE||'ard';
let expectedPassword='test-password',acceptedLogins=0;
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'bridge-desktop-profile-'));
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://local');
 if(url.pathname==='/'){
  res.setHeader('Content-Type','text/html');res.end('<meta name="viewport" content="width=device-width,initial-scale=1"><div id="welcome-item"></div><script>let currentServer="test",onServer=true;</script><script type="module" src="/desktop.js"></script>');return;
 }
 if(url.pathname==='/api/test/desktop'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({available:true}));return;}
 const file=url.pathname==='/desktop.js'?path.join(assets,'desktop.js'):path.join(assets,'novnc',url.pathname.replace('/desktop-assets/',''));
 if(!file.startsWith(assets+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type','application/javascript');res.end(fs.readFileSync(file));
});
const sockets=new wsServer({server});
sockets.on('connection',socket=>{
 const dh=crypto.getDiffieHellman('modp14');dh.generateKeys();
 let pending=Buffer.alloc(0),stage='version',sentFrame=false,pixelFormat=Buffer.from([32,24,0,1,0,255,0,255,0,255,16,8,0,0,0,0]);
 socket.send(Buffer.from('RFB 003.008\n'));
 socket.on('message',data=>{
  pending=Buffer.concat([pending,data]);
  const take=n=>{if(pending.length<n)return null;const b=pending.subarray(0,n);pending=pending.subarray(n);return b;};
  while(pending.length){
   if(stage==='version'){if(!take(12))return;socket.send(Buffer.from([1,authMode==='ard'?30:2]));stage='security';}
   else if(stage==='security'){
    if(!take(1))return;
    if(authMode==='ard'){
     const prime=dh.getPrime(),header=Buffer.alloc(4);header.writeUInt16BE(2,0);header.writeUInt16BE(prime.length,2);
     socket.send(Buffer.concat([header,prime,dh.getPublicKey()]));
    }else socket.send(Buffer.alloc(16,7));
    stage='password';
   }
   else if(stage==='password'){
    const reply=take(authMode==='ard'?128+dh.getPrime().length:16);if(!reply)return;
    let valid;
    if(authMode==='ard'){
     const key=crypto.createHash('md5').update(dh.computeSecret(reply.subarray(128))).digest();
     const cipher=crypto.createDecipheriv('aes-128-ecb',key,null);cipher.setAutoPadding(false);
     const plain=Buffer.concat([cipher.update(reply.subarray(0,128)),cipher.final()]);
     const text=b=>b.subarray(0,b.indexOf(0)).toString();
     valid=text(plain.subarray(0,64))==='test-user'&&text(plain.subarray(64))===expectedPassword;
    }else{
     const key=Buffer.alloc(8);Buffer.from(expectedPassword).copy(key,0,0,8);
     for(let i=0;i<8;i++){let b=key[i],v=0;for(let j=0;j<8;j++){v=(v<<1)|(b&1);b>>=1;}key[i]=v;}
     const cipher=crypto.createCipheriv('des-ede3',Buffer.concat([key,key,key]),null);cipher.setAutoPadding(false);
     valid=reply.equals(Buffer.concat([cipher.update(Buffer.alloc(16,7)),cipher.final()]));
    }
    if(!valid){const reason=Buffer.from('Incorrect credentials'),result=Buffer.alloc(8);result.writeUInt32BE(1);result.writeUInt32BE(reason.length,4);socket.send(Buffer.concat([result,reason]));stage='failed';return;}
    acceptedLogins++;socket.send(Buffer.alloc(4));stage='init';
   }
   else if(stage==='init'){
    if(!take(1))return;
    const name=Buffer.from('Bridge test desktop'),init=Buffer.alloc(24+name.length);
    init.writeUInt16BE(80,0);init.writeUInt16BE(60,2);Buffer.from([32,24,0,1,0,255,0,255,0,255,16,8,0,0,0,0]).copy(init,4);init.writeUInt32BE(name.length,20);name.copy(init,24);socket.send(init);stage='ready';
   }else{
    const type=pending[0];let length;
    if(type===0)length=20;
    else if(type===2){if(pending.length<4)return;length=4+pending.readUInt16BE(2)*4;}
    else if(type===3)length=10;
    else if(type===4)length=8;
    else if(type===5)length=6;
    else if(type===6){if(pending.length<8)return;length=8+pending.readUInt32BE(4);}
    else throw new Error('Unexpected VNC client message '+type);
    const message=take(length);if(!message)return;
    if(type===0)pixelFormat=message.subarray(4);
    if(type===3&&!sentFrame){
     sentFrame=true;const frame=Buffer.alloc(16+80*60*4);frame[3]=1;frame.writeUInt16BE(80,8);frame.writeUInt16BE(60,10);
     const pixel=(180<<pixelFormat[10])|(90<<pixelFormat[11])|(40<<pixelFormat[12]);
     for(let i=16;i<frame.length;i+=4){if(pixelFormat[2])frame.writeUInt32BE(pixel>>>0,i);else frame.writeUInt32LE(pixel>>>0,i);}
     socket.send(frame);
    }
    if(type===4)events.push({key:message.readUInt32BE(4),down:message[1]});
    if(type===5)events.push({pointer:true,buttons:message[1]});
   }
  }
 });
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const engine=playwright[process.env.BROWSER_ENGINE||'chromium'];
 const launch=()=>engine.launchPersistentContext(profile,{headless:true,viewport:{width:390,height:844},isMobile:true,hasTouch:true});
 let browser=await launch();
 try{
  let page=await browser.newPage();const errors=[];
  const url='http://127.0.0.1:'+server.address().port;
  const ready=async()=>{await page.waitForFunction(()=>document.querySelector('#desktop-status').textContent==='Connected · Tap to click; use Keyboard to type.');assert.equal(await page.locator('form').evaluate(e=>e.hidden),true);};
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.getByRole('button',{name:'Desktop',exact:true}).click();
  await page.getByLabel('Desktop password',{exact:true}).waitFor({state:'visible'});
  if(authMode==='ard')await page.getByLabel('Desktop username',{exact:true}).fill('test-user');
  await page.getByLabel('Desktop password',{exact:true}).fill('test-password');
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await ready();
  assert.equal(await page.getByLabel('Desktop password',{exact:true}).inputValue(),'');
  await page.waitForFunction(()=>{const canvas=document.querySelector('#desktop-screen canvas');return canvas&&canvas.width===80&&canvas.height===60&&canvas.getContext('2d').getImageData(20,20,1,1).data[0]===180;});
  await page.locator('#desktop-screen canvas').tap();
  await page.getByRole('button',{name:'Keyboard',exact:true}).tap();
  await page.keyboard.type('a');
  await page.getByRole('button',{name:'Enter',exact:true}).tap();
  for(let i=0;i<50&&!events.some(e=>e.key===0xff0d);i++)await page.waitForTimeout(20);
  assert(events.some(e=>e.key===97&&e.down===1));assert(events.some(e=>e.key===0xff0d&&e.down===1));assert(events.some(e=>e.pointer&&e.buttons===1));
  await page.getByRole('button',{name:'Close',exact:true}).tap();
  for(let i=0;i<50&&sockets.clients.size;i++)await page.waitForTimeout(20);
  assert.equal(sockets.clients.size,0);assert.deepEqual(errors,[]);
  console.log('PASS real noVNC '+authMode+': verified credentials, framebuffer, touch and keyboard');
  await page.reload();await page.getByRole('button',{name:'Desktop',exact:true}).click();await ready();
  assert.equal(acceptedLogins,2);
  await page.close();page=await browser.newPage();await page.goto(url);
  await page.getByRole('button',{name:'Desktop',exact:true}).click();await ready();assert.equal(acceptedLogins,3);
  await browser.close();browser=await launch();page=await browser.newPage();await page.goto(url);
  await page.getByRole('button',{name:'Desktop',exact:true}).click();await ready();assert.equal(acceptedLogins,4);
  console.log('PASS saved login survives reload, a new tab and browser process restart');
  const stored=await page.evaluate(()=>new Promise((resolve,reject)=>{const r=indexedDB.open('tmux-bridge-desktop-login',1);r.onsuccess=()=>{const db=r.result,q=db.transaction('credentials').objectStore('credentials').get('test');q.onsuccess=()=>{const v=q.result;resolve({extractable:v.key.extractable,plaintext:JSON.stringify(v).includes('test-password'),encrypted:v.encrypted.byteLength});db.close();};};r.onerror=()=>reject(r.error);}));
  assert.equal(stored.extractable,false);assert.equal(stored.plaintext,false);assert(stored.encrypted>16);
  expectedPassword='new-password';
  await page.reload();await page.getByRole('button',{name:'Desktop',exact:true}).click();
  await page.getByRole('status').filter({hasText:'Desktop login failed'}).waitFor();
  await page.getByRole('button',{name:'Close',exact:true}).click();
  await page.getByRole('button',{name:'Desktop',exact:true}).click();
  await page.getByLabel('Desktop password',{exact:true}).waitFor({state:'visible'});
  if(authMode==='ard')await page.getByLabel('Desktop username',{exact:true}).fill('test-user');
  await page.getByLabel('Desktop password',{exact:true}).fill('new-password');
  await page.getByRole('button',{name:'Sign in',exact:true}).click();await ready();
  await page.reload();await page.getByRole('button',{name:'Desktop',exact:true}).click();await ready();
  assert.equal(acceptedLogins,6);
  console.log('PASS rejected saved credentials are removed; corrected password persists');
 }finally{await browser.close();sockets.close();server.close();fs.rmSync(profile,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;sockets.close();server.close();});
