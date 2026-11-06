/* Desktop viewer uses the same gateway admission boundary as terminal access. */
let viewer=null,dialog=null,generation=0,connected=false;
const make=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;};
function closeDesktop(){generation++;connected=false;viewer?.disconnect();viewer=null;dialog?.close();if(dialog){dialog.querySelector('form').reset();dialog.querySelector('#desktop-keyboard').value='';}}
function build(){
  if(dialog)return;
  dialog=make('dialog');dialog.id='desktop-dialog';dialog.setAttribute('aria-label','Remote desktop');
  const header=make('div','desktop-header'),title=make('strong');title.id='desktop-title';
  const keyboard=make('button','','Keyboard');keyboard.type='button';keyboard.onclick=()=>{if(connected)dialog.querySelector('#desktop-keyboard').focus();};
  const fit=make('button','','Fit');fit.type='button';fit.setAttribute('aria-pressed','true');fit.onclick=()=>{if(viewer){viewer.scaleViewport=!viewer.scaleViewport;fit.setAttribute('aria-pressed',String(viewer.scaleViewport));}};
  const close=make('button','','Close');close.type='button';close.onclick=closeDesktop;header.append(title,keyboard,fit,close);
  const status=make('div','desktop-status');status.id='desktop-status';status.setAttribute('role','status');
  const form=make('form','desktop-credentials');form.hidden=true;
  const userLabel=make('label','','Desktop username'),username=make('input');username.name='username';username.autocomplete='off';userLabel.append(username);
  const passLabel=make('label','','Desktop password'),password=make('input');password.name='password';password.type='password';password.autocomplete='off';passLabel.append(password);
  const submit=make('button','','Sign in');submit.type='submit';form.append(userLabel,passLabel,submit);
  form.onsubmit=e=>{e.preventDefault();if(viewer){viewer.sendCredentials({username:username.value,password:password.value});form.reset();form.hidden=true;status.textContent='Signing in…';}};
  const screen=make('div','desktop-screen');screen.id='desktop-screen';
  const input=make('textarea','desktop-keyboard');input.id='desktop-keyboard';input.setAttribute('aria-label','Type into remote desktop');input.setAttribute('autocapitalize','off');input.autocomplete='off';input.spellcheck=false;input.setAttribute('autocorrect','off');
  const keysyms={Enter:0xff0d,Backspace:0xff08,Tab:0xff09,Escape:0xff1b,ArrowLeft:0xff51,ArrowUp:0xff52,ArrowRight:0xff53,ArrowDown:0xff54,Delete:0xffff};
  input.onkeydown=e=>{e.stopPropagation();if(connected&&keysyms[e.key]){e.preventDefault();viewer.sendKey(keysyms[e.key],e.code);}};
  input.onbeforeinput=e=>{if(connected&&e.inputType==='deleteContentBackward'){e.preventDefault();viewer.sendKey(0xff08,'Backspace');}};
  input.oninput=e=>{if(e.isComposing)return;if(connected)for(const char of input.value){const cp=char.codePointAt(0);viewer.sendKey(char==='\n'?0xff0d:cp<=255?cp:0x01000000|cp);}input.value='';};
  const keys=make('div','desktop-keys');
  for(const [label,key] of [['Esc','Escape'],['Tab','Tab'],['←','ArrowLeft'],['↑','ArrowUp'],['↓','ArrowDown'],['→','ArrowRight'],['⌫','Backspace'],['Enter','Enter']]){const button=make('button','',label);button.type='button';button.setAttribute('aria-label',key);button.onpointerdown=e=>e.preventDefault();button.onclick=()=>{if(connected)viewer.sendKey(keysyms[key],key);};keys.append(button);}
  dialog.append(header,status,form,screen,input,keys);document.body.append(dialog);
  dialog.addEventListener('cancel',e=>{e.preventDefault();closeDesktop();});
}
async function openDesktop(server){
  if(!server)return;
  build();closeDesktop();const token=++generation;dialog.showModal();
  if(typeof term!=='undefined')term.blur();
  dialog.querySelector('#desktop-title').textContent=server+' · Desktop';
  const status=dialog.querySelector('#desktop-status'),form=dialog.querySelector('form'),screen=dialog.querySelector('#desktop-screen');
  screen.replaceChildren();form.hidden=true;status.textContent='Checking desktop…';
  try{
    const response=await fetch('/api/'+encodeURIComponent(server)+'/desktop',{signal:AbortSignal.timeout(12000)}),state=await response.json();
    if(token!==generation)return;
    if(!response.ok||!state.available)throw new Error(state.message||state.error||'Desktop unavailable');
    const {default:RFB}=await import('/desktop-assets/core/rfb.js');
    if(token!==generation)return;
    const socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/desktop/'+encodeURIComponent(server));
    socket.addEventListener('close',e=>{if(token===generation&&!connected&&e.reason)status.textContent=e.reason;});
    viewer=new RFB(screen,socket,{shared:true});viewer.scaleViewport=true;viewer.resizeSession=false;viewer.background='#171714';dialog.querySelector('[aria-pressed]').setAttribute('aria-pressed','true');
    viewer.compressionLevel=2;viewer.qualityLevel=6;status.textContent='Connecting…';
    viewer.addEventListener('credentialsrequired',e=>{if(token!==generation)return;const types=e.detail.types;form.hidden=false;form.querySelector('input[name=username]').parentElement.hidden=!types.includes('username');status.textContent='Enter this device’s desktop credentials.';});
    viewer.addEventListener('connect',()=>{if(token!==generation)return;connected=true;status.textContent='Connected · Tap to click; use Keyboard to type.';form.reset();form.hidden=true;});
    viewer.addEventListener('securityfailure',()=>{if(token===generation)status.textContent='Desktop login failed. Close and reopen to try again.';});
    viewer.addEventListener('disconnect',()=>{if(token!==generation)return;connected=false;form.reset();form.hidden=true;if(!/failed|already open|offline/.test(status.textContent))status.textContent='Desktop disconnected. Close and reopen to reconnect.';});
  }catch(e){if(token===generation)status.textContent=e.name==='TimeoutError'?'Desktop check timed out. Close and retry.':e.message;}
}
const launch=make('button','session-item','Desktop');launch.id='desktop-open';launch.type='button';launch.title='Open this device’s desktop';launch.onclick=()=>openDesktop(currentServer);
document.getElementById('welcome-item').after(launch);
function update(){launch.hidden=typeof currentServer==='undefined'||!currentServer||typeof onServer==='undefined'||!onServer;}
window.addEventListener('bridge:server-changed',update);update();
window.bridgeDesktop={open:openDesktop,close:closeDesktop};
const style=make('style');style.textContent=`
 #desktop-open{margin:1px 8px;width:calc(100% - 16px);border:0;background:transparent;color:inherit;text-align:left;font:inherit;cursor:pointer}#desktop-open[hidden]{display:none}
 #desktop-dialog{box-sizing:border-box;width:100vw;max-width:100vw;height:100dvh;max-height:100dvh;margin:0;padding:0;border:0;background:#1c1b17;color:#eee;overflow:hidden}
 #desktop-dialog[open]{display:flex;flex-direction:column}#desktop-dialog::backdrop{background:#000b}.desktop-header{display:flex;align-items:center;gap:8px;padding:10px;padding-top:max(10px,env(safe-area-inset-top));border-bottom:1px solid #444039}.desktop-header strong{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}
 #desktop-dialog button{background:#34312c;color:#eee;border:1px solid #5d574e;border-radius:5px;padding:8px;min-height:38px;cursor:pointer}.desktop-status{padding:6px 12px;font-size:12px;color:#c6bda9}.desktop-screen{flex:1;min-height:0;overflow:auto}.desktop-credentials{padding:12px;display:flex;gap:10px;align-items:end;flex-wrap:wrap}.desktop-credentials[hidden]{display:none}.desktop-credentials label{font-size:12px;display:flex;flex-direction:column;gap:4px}.desktop-credentials label[hidden]{display:none}.desktop-credentials input{font-size:16px;background:#292724;color:#eee;border:1px solid #625d53;border-radius:4px;padding:8px;max-width:calc(100vw - 40px)}
 .desktop-keyboard{position:fixed;opacity:.01;left:0;bottom:0;width:1px;height:1px;padding:0;border:0;pointer-events:none}.desktop-keys{display:flex;gap:5px;justify-content:center;flex-wrap:wrap;padding:6px;padding-bottom:max(6px,env(safe-area-inset-bottom));border-top:1px solid #444039}.desktop-keys button{flex:1;max-width:80px}#desktop-dialog button:focus-visible{outline:2px solid #dfbc79;outline-offset:2px}
`;document.head.append(style);
