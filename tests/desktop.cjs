// Real noVNC browser handshake, drawing and input against a local fake desktop.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const playwright=require(process.env.PLAYWRIGHT_PATH||'playwright');
const {wsServer}=require(path.join(path.dirname(require.resolve('playwright-core/package.json')),'lib/utilsBundle.js'));
const assets=path.join(__dirname,'../tmux_bridge_assets');
const events=[];
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
 let pending=Buffer.alloc(0),stage='version',sentFrame=false,pixelFormat=Buffer.from([32,24,0,1,0,255,0,255,0,255,16,8,0,0,0,0]);
 socket.send(Buffer.from('RFB 003.008\n'));
 socket.on('message',data=>{
  pending=Buffer.concat([pending,data]);
  const take=n=>{if(pending.length<n)return null;const b=pending.subarray(0,n);pending=pending.subarray(n);return b;};
  while(pending.length){
   if(stage==='version'){if(!take(12))return;socket.send(Buffer.from([1,2]));stage='security';}
   else if(stage==='security'){if(!take(1))return;socket.send(Buffer.alloc(16,7));stage='password';}
   else if(stage==='password'){if(!take(16))return;socket.send(Buffer.alloc(4));stage='init';}
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
 const browser=await playwright[process.env.BROWSER_ENGINE||'chromium'].launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.getByRole('button',{name:'Desktop',exact:true}).click();
  await page.getByLabel('Desktop password',{exact:true}).waitFor({state:'visible'});
  await page.getByLabel('Desktop password',{exact:true}).fill('test-password');
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await page.getByRole('status').filter({hasText:'Connected ·'}).waitFor();
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
  console.log('PASS real noVNC: phone credentials, framebuffer, touch pointer, keyboard, Enter and disconnect');
 }finally{await browser.close();sockets.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;sockets.close();server.close();});
