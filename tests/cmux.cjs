// Browser behavior for native workspace controls and atomic screen snapshots.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const engine=process.env.BROWSER_ENGINE||'chromium';
const browserType=require(process.env.PLAYWRIGHT_PATH||'playwright')[engine];
const root=path.join(__dirname,'..');
const html=fs.readFileSync(path.join(root,'gateway.py'),'utf8').split('HTML = r"""')[1].split('"""')[0];
const windowId='11111111-1111-4111-8111-111111111111',workspace='22222222-2222-4222-8222-222222222222',surface='33333333-3333-4333-8333-333333333333';
const name='cmux:'+workspace+':'+surface,label='日本語 <img src=x onerror="window.badLabel=true"> / café';
const fixture={render_grid:{format:'cmux.render-grid.v1',full:true,columns:80,rows:24,
  styles:[{id:1,foreground:'#ff0000',bold:true}],row_spans:[{row:0,column:0,style_id:1,text:'café 日本語 🧪'}],cursor:{row:1,column:2,visible:true},modes:[]}};
const encoded=spawnSync(process.env.PYTHON||'python3',['-c','import json,sys;from cmux_backend import screen_frame;print(json.dumps(screen_frame(json.load(sys.stdin))))'],{cwd:root,input:JSON.stringify(fixture),encoding:'utf8'});
assert.equal(encoded.status,0,encoded.stderr);const frame=JSON.parse(encoded.stdout);
(async()=>{
 const browser=await browserType.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1100,height:800}}),errors=[],actions=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async r=>{
   const u=r.request().url();
   if(u.startsWith('https://cdn.jsdelivr.net/'))return r.continue();
   if(u==='https://inspection.test/')return r.fulfill({contentType:'text/html',body:html});
   let data=[];
   if(u.endsWith('/sessions'))data=[{name,label,backend:'cmux',workspace_id:workspace,surface_id:surface}];
   if(u.endsWith('/cmux/workspaces'))data={backend:'cmux',windows:[{id:windowId,title:'Mac window'}],workspaces:[{id:workspace,title:'日本語 workspace'}]};
   if(r.request().method()==='POST'&&u.includes('/cmux/')){actions.push({action:u.split('/').pop(),body:r.request().postDataJSON()});data={ok:true,workspace_id:workspace,surface_id:surface};}
   if(r.request().method()==='DELETE'){actions.push({action:'delete',url:u});data={ok:true};}
   if(u.includes('/layout'))data={};
   if(u.includes('/specs'))data={hostname:'test',disks:[],backend:'cmux'};
   if(u.includes('/metrics'))data={current:{},sessions:{}};
   return r.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.addInitScript(()=>{
   window.testSends=[];window.WebSocket=class{static OPEN=1;constructor(){this.readyState=1;setTimeout(()=>this.onopen?.(),0);}send(data){window.testSends.push(data);}close(){this.readyState=3;}};
  });
  await page.goto('https://inspection.test/',{waitUntil:'networkidle'});
  await page.evaluate(async()=>{await enterServer('mac');});
  assert.equal(await page.locator('#cmux-controls').evaluate(e=>e.hidden),false);
  assert.equal(await page.locator('#session-list .session-item .s-name').textContent(),label);
  assert.equal(await page.evaluate(()=>!!window.badLabel),false);
  await page.locator('#session-list .session-item .s-name').click();
  await page.waitForFunction(()=>ws?.readyState===1);
  await page.evaluate(frame=>{window.testSends=[];handleMessage({data:JSON.stringify(frame)});},frame);
  await page.waitForFunction(()=>term.buffer.active.getLine(0)?.translateToString().includes('café 日本語 🧪'));
  assert.deepEqual(await page.evaluate(()=>({cols:term.cols,rows:term.rows})),{cols:80,rows:24});
  assert.equal(await page.evaluate(()=>term.buffer.active.getLine(0).getCell(0).getFgColor()),0xff0000);
  assert.equal(await page.evaluate(()=>testSends.some(s=>typeof s==='string'&&s.includes('"resize"'))),false);
  assert.equal(await page.locator('#chan-title').textContent(),label);
  console.log('PASS native Unicode/color screen, geometry without resize feedback, safe labels');
  await page.evaluate(()=>selectCmuxWorkspace());
  assert.deepEqual(actions.pop(),{action:'select',body:{workspace_id:workspace}});
  await page.evaluate(()=>splitCmuxTerminal());
  assert.deepEqual(actions.pop(),{action:'split',body:{session:name,direction:'right'}});
  page.once('dialog',d=>d.accept('New workspace 🧪'));
  await page.evaluate(()=>createCmuxWorkspace());
  const created=actions.pop();assert.equal(created.action,'create');assert.equal(created.body.title,'New workspace 🧪');assert.equal(created.body.window_id,windowId);assert.match(created.body.operation_id,/^[a-f0-9-]{36}$/);
  page.once('dialog',d=>d.dismiss());
  await page.evaluate(()=>closeCmuxWorkspace());
  assert.equal(actions.length,0);
  page.once('dialog',d=>d.accept());
  await page.evaluate(()=>closeCmuxWorkspace());
  assert.deepEqual(actions.pop(),{action:'close',body:{workspace_id:workspace,confirmed:true}});
  assert.equal(await page.evaluate(()=>currentSession),'');
  console.log('PASS native create/select/split controls and confirmed workspace close');
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
