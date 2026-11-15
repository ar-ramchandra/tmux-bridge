/* Localhost links open explicitly scoped, expiring application previews. */
const make=(tag,text='')=>{const el=document.createElement(tag);el.textContent=text;return el;};
const panel=make('details');panel.id='preview-panel';panel.hidden=true;
const summary=make('summary','Previews');
const list=make('div');list.id='preview-list';
const form=make('form');
const port=make('input');port.type='number';port.min='1024';port.max='65535';port.placeholder='Local port';port.required=true;port.setAttribute('aria-label','Application port');
const add=make('button','Open');add.type='submit';
form.append(port,add);panel.append(summary,list,form);
document.getElementById('session-list').before(panel);
const style=make('style');style.textContent=`
#preview-panel{padding:8px 14px;font-size:13px}#preview-panel summary{cursor:pointer;color:var(--text-muted,#aaa);padding:6px 0}
#preview-panel form,.preview-row{display:flex;align-items:center;gap:8px;margin:8px 0}#preview-panel input{width:100px;min-width:0;padding:8px;background:transparent;color:inherit;border:1px solid #777;border-radius:6px}
#preview-panel button{min-height:36px;padding:6px 9px;cursor:pointer;background:transparent;color:inherit;border:1px solid #777;border-radius:6px}
.preview-row a{flex:1;overflow-wrap:anywhere;color:inherit}.preview-row small{display:block;opacity:.65;font-size:11px;margin-top:3px}
`;document.head.append(style);
let enabled=false,polling=false;
function selection(){return {server:typeof currentServer==='undefined'?'':currentServer,session:typeof currentSession==='undefined'?'':currentSession};}
function notice(message){if(typeof notifyUser==='function')notifyUser(message);}
async function create(target,number,title=''){
 const r=await fetch('/api/'+encodeURIComponent(target.server)+'/previews',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session:target.session,port:number,title})});
 const row=await r.json();if(!r.ok)throw new Error(row.error||'Preview could not be opened');
 if(!/^https:\/\/[a-f0-9]{32}-preview\.[a-z0-9.-]+\/$/.test(row.url))throw new Error('Invalid preview address');
 await refresh();return row;
}
async function launch(target,number,path='/',title=''){
 if(!target.server||!target.session){notice('Choose the session running this app first.');return;}
 const tab=window.open('about:blank','_blank');if(tab){tab.opener=null;tab.document.title='Opening private preview…';tab.document.body.textContent='Opening private preview…';}
 try{
  const row=await create(target,number,title);
  // Keep the registered origin even if a displayed URL begins with //.
  const local=new URL(path,'http://localhost');const dest=new URL(row.url);dest.pathname=local.pathname;dest.search=local.search;dest.hash=local.hash;
  if(tab)tab.location.replace(dest.href);else{panel.open=true;notice('Preview ready. Open it from Previews in the sidebar.');}
 }catch(e){if(tab)tab.close();notice(e.message);}
}
form.onsubmit=e=>{e.preventDefault();launch(selection(),Number(port.value));};
window.bridgePreviews={openLocal(value){
 let url;try{url=new URL(value);}catch{return false;}
 if(!['localhost','127.0.0.1','[::1]'].includes(url.hostname))return false;
 if(!enabled){notice('Private previews are not available yet.');return true;}
 if(url.protocol!=='http:'||!url.port||url.username||url.password){notice('Use an HTTP development server on a local port above 1023.');return true;}
 launch(selection(),Number(url.port),url.pathname+url.search+url.hash);return true;
},refresh};
async function refresh(){
 if(polling)return;polling=true;
 try{
  const r=await fetch('/api/previews');if(!r.ok)return;const data=await r.json();enabled=data.enabled===true;
  const target=selection();panel.hidden=!enabled||!target.server;form.hidden=!target.session;
  const rows=(data.previews||[]).filter(p=>p.server===target.server&&(!target.session||p.session===target.session));
  summary.textContent='Previews'+(rows.length?' · '+rows.length:'');list.replaceChildren();
  for(const row of rows){
   const entry=make('div');entry.className='preview-row';
   const link=make('a',row.title);link.href=row.url;link.target='_blank';link.rel='noopener noreferrer';
   link.append(make('small',row.session+' · '+Math.max(1,Math.ceil((row.expires-Date.now()/1000)/60))+' min left'));
   const stop=make('button','Stop');stop.type='button';stop.setAttribute('aria-label','Stop preview '+row.title);
   stop.onclick=async()=>{stop.disabled=true;try{const r=await fetch('/api/'+encodeURIComponent(row.server)+'/previews/'+row.id,{method:'DELETE'});if(!r.ok)throw new Error('Could not stop preview');await refresh();}catch(e){notice(e.message);stop.disabled=false;}};
   entry.append(link,stop);list.append(entry);
  }
  if(!rows.length)list.append(make('small','Click a localhost link in the terminal, or enter the app’s port below. Previews expire automatically.'));
 }catch{}finally{polling=false;}
}
window.addEventListener('bridge:server-changed',refresh);
setInterval(()=>{if(!document.hidden)refresh();},4000);refresh();
