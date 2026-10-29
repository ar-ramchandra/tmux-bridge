/* Completion metadata only. Terminal rendering and input never wait for this. */
(() => {
  'use strict';
  const key = 'bridge.notificationSettings.v1', seenKey = 'bridge.activitySeen.v1';
  function read(k, fallback) { try { return JSON.parse(localStorage.getItem(k)) || fallback; } catch { return fallback; } }
  function save(k, value) { try { localStorage.setItem(k, JSON.stringify(value)); } catch {} }
  const saved=read(key,{});
  let prefs = {...{foregroundCue:saved.inApp!==false||saved.sound===true,kinds:['turn_finished','waiting','failed'],servers:[]},...saved};
  let seen=read(seenKey,{}), cursor=0, initialized=false, registration=null, dialog=null, pollTimer=null, polling=false;
  const latest=new Map(), labels={running:'Working',waiting:'Needs input',turn_finished:'Response finished',failed:'Failed',interrupted:'Interrupted',idle:'Idle',stopped:'Session ended',task_completed:'Task reported complete'};
  const identity=(server,session)=>JSON.stringify([server,session]);
  async function api(path, options={}) {
    const response=await fetch(path,{...options,signal:AbortSignal.timeout(12000),headers:{'Content-Type':'application/json',...options.headers}});
    if(!response.ok){
      let detail;try{detail=await response.json();}catch{}
      throw new Error(typeof detail?.error==='string'?detail.error.slice(0,300):'Request failed ('+response.status+')');
    }
    return response.json();
  }
  const interested=e=>prefs.kinds.includes(e.kind)&&(!prefs.servers.length||prefs.servers.includes(e.server));
  function remember(event) {
    if(!event.session)return;
    const id=identity(event.server,event.session),old=latest.get(id);
    if(!old||event.time>old.time||(event.time===old.time&&event.seq>old.seq))latest.set(id,event);
  }
  function decorateItem(item,server,session) {
    let badge=item.querySelector('.activity-status');
    if(!badge){badge=document.createElement('span');badge.className='activity-status';item.querySelector('.s-name')?.appendChild(badge);}
    const event=latest.get(identity(server,session));
    badge.textContent=event?(labels[event.kind]||event.kind):'';
    badge.dataset.kind=event?.kind||'';
    badge.classList.toggle('unread',!!event&&event.seq>(seen[identity(server,session)]||0)&&['turn_finished','waiting','failed'].includes(event.kind));
    badge.title=event?event.provider+' · '+new Date(event.time*1000).toLocaleString()+' · Prompt status; outcome not verified':'';
  }
  function decorate() {
    if(typeof currentServer==='undefined')return;
    if(currentSession&&!document.hidden&&document.hasFocus()) {
      const id=identity(currentServer,currentSession),event=latest.get(id);
      if(event&&seen[id]!==event.seq){seen[id]=event.seq;save(seenKey,seen);}
    }
    document.querySelectorAll('#session-list .session-item[data-session]').forEach(item=>decorateItem(item,currentServer,item.dataset.session));
    const active=latest.get(identity(currentServer,currentSession));
    let status=document.getElementById('active-prompt-status');
    if(!status){status=document.createElement('span');status.id='active-prompt-status';status.setAttribute('role','status');document.querySelector('.channel-name')?.appendChild(status);}
    status.textContent=active?(labels[active.kind]||active.kind):'';
    status.title=active?'Prompt status; outcome not verified':'';
  }
  const foreground=()=>!document.hidden&&document.hasFocus();
  function foregroundCue() {
    if(!prefs.foregroundCue||!foreground()||(typeof isDeafened!=='undefined'&&isDeafened))return;
    // iPhone has no web vibration API; use a brief tone there instead.
    try{if(navigator.vibrate?.(60))return;}catch{}
    if(typeof playTone==='function')playTone(880,0.15,'sine',0.1);
  }
  async function deliverForeground(events) {
    if(!foreground())return;
    const fresh=events.filter(e=>interested(e)&&Date.now()/1000-e.time<=300);
    if(!fresh.length)return;
    // Receipt means the visible app handled the event, even when muted. Do not
    // suppress pushes in the service worker: Safari requires a visible push.
    const receipt=prefs.pushId?api('/api/activity/push/'+encodeURIComponent(prefs.pushId)+'/received',
      {method:'POST',body:JSON.stringify({sequences:fresh.map(e=>e.seq)})}).catch(()=>{}):Promise.resolve();
    foregroundCue();
    await receipt;
  }
  async function poll() {
    if(polling)return;
    polling=true;clearTimeout(pollTimer);
    try {
      if(!initialized){
        const data=await api('/api/activity/snapshot');
        if(!Array.isArray(data.sessions))throw new Error('Invalid activity snapshot');
        data.sessions.forEach(remember);cursor=data.cursor;initialized=true;
      }
      // Cursor is the last delivered event, never a separately-read high watermark.
      const data=await api('/api/activity?after='+cursor);
      if(!Array.isArray(data.events))throw new Error('Invalid activity page');
      for(const event of data.events)remember(event);
      await deliverForeground(data.events);
      cursor=data.next_cursor;
      if(data.history_gap){const snapshot=await api('/api/activity/snapshot');snapshot.sessions.forEach(remember);}
      decorate();
      const target=Number(new URL(location.href).searchParams.get('activity'));
      if(target){
        const event=[...latest.values()].find(e=>e.seq===target);
        if(event&&typeof enterServer==='function'){history.replaceState(null,'',location.pathname);await enterServer(event.server);connect(event.session);}
      }
    } catch { /* Transport status remains separate; never fabricate completion. */ }
    polling=false;pollTimer=setTimeout(poll,document.hidden?10000:2500);
  }
  function message(text){dialog.querySelector('[data-message]').textContent=text;}
  async function pushRegistration(){
    if(!('serviceWorker'in navigator)||!('PushManager'in window)||!('Notification'in window))throw new Error('Push is unavailable here. On iPhone, add this gateway to the Home Screen and open it there.');
    registration=await navigator.serviceWorker.register('/activity-sw.js');
    return navigator.serviceWorker.ready;
  }
  async function syncSubscription(sub){
    const result=await api('/api/activity/push',{method:'POST',body:JSON.stringify({subscription:sub.toJSON(),preferences:{kinds:prefs.kinds,servers:prefs.servers}})});
    prefs.pushId=result.id;save(key,prefs);
  }
  async function enablePush(){
    try {
      // Request permission directly from the click, including on iOS.
      if(!('Notification'in window))throw new Error('On iPhone, add the gateway to the Home Screen, then enable notifications there.');
      const permission=await Notification.requestPermission();
      if(permission!=='granted')throw new Error('Notifications are not allowed. Change this site’s notification permission in your browser settings.');
      const reg=await pushRegistration(),info=await api('/api/activity/push');
      if(!info.available)throw new Error('Push is not configured on this gateway.');
      const key=Uint8Array.from(atob(info.public_key.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-info.public_key.length%4)%4)),c=>c.charCodeAt(0));
      const sub=await reg.pushManager.getSubscription()||await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:key});
      await syncSubscription(sub);message('Browser and phone push enabled on this device.');
    }catch(error){message(error.message);}
  }
  async function disablePush(){
    try {
      if(prefs.pushId)await api('/api/activity/push/'+encodeURIComponent(prefs.pushId),{method:'DELETE'});
      if('serviceWorker'in navigator){const reg=await navigator.serviceWorker.getRegistration();const sub=await reg?.pushManager.getSubscription();if(sub)await sub.unsubscribe();}
      delete prefs.pushId;save(key,prefs);message('Push disabled on this device.');
    }catch(error){message(error.message);}
  }
  function settings(){
    if(!dialog){
      dialog=document.createElement('dialog');dialog.id='notification-settings';
      dialog.innerHTML=`<form method="dialog"><h2>Notifications</h2><p>Preferences for this browser or Home Screen app.</p>
        <label><input type="checkbox" data-pref="foregroundCue"> Vibrate or play a tone while using the gateway</label>
        <p>No banners while you’re using the app. Push notifications appear when it’s in the background or your phone is locked.</p>
        <fieldset><legend>Notify me when</legend>
        <label><input type="checkbox" data-kind="turn_finished"> A response finishes</label>
        <label><input type="checkbox" data-kind="waiting"> An agent needs input</label>
        <label><input type="checkbox" data-kind="failed"> An agent reports an error</label></fieldset>
        <fieldset data-servers><legend>Nodes</legend><p>Leave all unchecked for every node.</p></fieldset>
        <p>“Response finished” means the agent stopped answering; it does not verify the requested outcome.</p>
        <p>Phone alerts: on iPhone, add the gateway to the Home Screen first. Notification previews contain only the provider and status.</p>
        <div class="notification-actions"><button type="button" data-enable>Enable push</button><button type="button" data-disable>Disable push</button><button type="button" data-test>Test notification</button></div>
        <p data-message role="status"></p><button value="close">Close</button></form>`;
      document.body.appendChild(dialog);
      dialog.querySelector('[data-enable]').onclick=enablePush;
      dialog.querySelector('[data-disable]').onclick=disablePush;
      dialog.querySelector('[data-test]').onclick=async()=>{
        try{
          foregroundCue();
          message(prefs.foregroundCue?'Foreground cue played. To test a push banner, lock your phone before an agent finishes.':'Foreground cue is off. Background push follows your push settings.');
        }catch(error){message(error.message);}
      };
      dialog.addEventListener('change',async()=>{
        for(const input of dialog.querySelectorAll('[data-pref]'))prefs[input.dataset.pref]=input.checked;
        prefs.kinds=[...dialog.querySelectorAll('[data-kind]:checked')].map(i=>i.dataset.kind);
        prefs.servers=[...dialog.querySelectorAll('[data-server]:checked')].map(i=>i.dataset.server);
        save(key,prefs);message('Preferences saved on this device.');
        if(prefs.pushId){try{const reg=await pushRegistration(),sub=await reg.pushManager.getSubscription();if(sub)await syncSubscription(sub);else message('Push subscription expired. Enable push again.');}catch(error){message('Saved locally; push preferences could not sync. '+error.message);}}
      });
    }
    for(const input of dialog.querySelectorAll('[data-pref]'))input.checked=!!prefs[input.dataset.pref];
    for(const input of dialog.querySelectorAll('[data-kind]'))input.checked=prefs.kinds.includes(input.dataset.kind);
    const nodes=dialog.querySelector('[data-servers]');nodes.querySelectorAll('label').forEach(n=>n.remove());
    const names=new Set([...prefs.servers,...(typeof cachedServerList==='undefined'?[]:cachedServerList.map(s=>s.name))]);
    for(const name of names){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.dataset.server=name;input.checked=prefs.servers.includes(name);label.append(input,document.createTextNode(' '+name));nodes.appendChild(label);}
    message(prefs.pushId?'Background push enabled on this device.':'Background push is off.');
    dialog.showModal();
  }
  const style=document.createElement('style');
  style.textContent=`.activity-status{display:block;font-size:10px;line-height:14px;color:#aaa;font-weight:400}.activity-status:empty{display:none}.activity-status[data-kind="running"]{color:#d7b674}.activity-status[data-kind="waiting"]{color:#e6ae62}.activity-status[data-kind="turn_finished"]{color:#8fc697}.activity-status[data-kind="failed"]{color:#ef9c95}.activity-status.unread:before{content:'● ';color:#da7756}#active-prompt-status{font-size:11px;color:#aaa;margin-left:10px}#notification-settings{background:#292724;color:#eee;border:1px solid #57524a;border-radius:10px;padding:22px;max-width:min(440px,calc(100vw - 32px));max-height:85dvh;overflow:auto}#notification-settings::backdrop{background:#0009}#notification-settings h2{font-size:18px;margin-top:0}#notification-settings p{font-size:12px;line-height:1.5;color:#c5c0b8}#notification-settings label{display:block;padding:7px 0;font-size:13px}#notification-settings input{accent-color:#da7756}#notification-settings fieldset{margin:12px 0;border:1px solid #57524a;border-radius:5px}#notification-settings button{background:#444039;color:#eee;border:1px solid #6a6258;border-radius:5px;padding:8px 12px;cursor:pointer}.notification-actions{display:flex;flex-wrap:wrap;gap:8px}@media(max-width:600px){#active-prompt-status{display:none}}`;
  document.head.appendChild(style);
  window.bridgeActivity={settings,decorateItem};
  // Unlock audio during interaction, not when a later network event arrives.
  function primeAudio(){if(prefs.foregroundCue&&typeof ensureAudio==='function'){try{ensureAudio().resume().catch(()=>{});}catch{}}}
  document.addEventListener('pointerdown',primeAudio,{passive:true});
  document.addEventListener('keydown',primeAudio);
  function visibilityChanged(){decorate();if(foreground())poll();}
  document.addEventListener('visibilitychange',visibilityChanged);
  window.addEventListener('focus',visibilityChanged);
  poll();
})();
