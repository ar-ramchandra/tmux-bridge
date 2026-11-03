/* Provider account quotas, separate from machine resource percentages. */
(() => {
  'use strict';
  let nodes=[];
  const explanations={signed_out:'Default CLI is not signed in.',unsupported:'This billing method does not expose a subscription quota.',awaiting_report:'Waiting for Claude to report usage after a response.',unavailable:'Usage is unavailable. No remaining allowance is assumed.'};
  function element(tag,cls,value){const e=document.createElement(tag);if(cls)e.className=cls;if(value!==undefined)e.textContent=value;return e;}
  function age(time){if(!time)return 'No reading';const minutes=Math.max(0,Math.floor((Date.now()/1000-time)/60));return minutes<1?'Just updated':minutes<60?minutes+'m ago':Math.floor(minutes/60)+'h ago';}
  function resetLabel(time){
    const minutes=Math.max(1,Math.ceil((time-Date.now()/1000)/60));
    return minutes<60?minutes+'m':minutes<1440?Math.floor(minutes/60)+'h '+minutes%60+'m':Math.floor(minutes/1440)+'d '+Math.floor(minutes%1440/60)+'h';
  }
  function signInControls(){
    const details=element('details','usage-login');
    details.append(element('summary','','Sign in on this device'));
    details.append(element('p','usage-note','Opens a separate login terminal on the selected machine. Open its provider link in your browser and follow the code instructions. This changes the default CLI account for that machine’s user.'));
    const actions=element('div','usage-login-actions'),status=element('p','usage-note');status.setAttribute('role','status');
    for(const provider of ['claude','codex']){
      const button=element('button','',provider==='claude'?'Claude Code':'Codex');button.type='button';
      button.addEventListener('click',async()=>{
        const server=currentServer;
        for(const b of actions.children)b.disabled=true;
        status.textContent='Opening '+provider+' sign-in on '+server+'…';
        try{
          const r=await fetch('/api/'+encodeURIComponent(server)+'/account-login/'+provider,{method:'POST',signal:AbortSignal.timeout(15000)});
          const data=await r.json();if(!r.ok||!data.ok)throw new Error(data.error||'Could not open sign-in');
          if(server!==currentServer){status.textContent='Login terminal opened on '+server+'. Select that device to continue.';return;}
          await loadSessions();
          if(server!==currentServer)return;
          connect(data.session);closeSidebar();
          document.getElementById('right-panel')?.classList.remove('open');
          details.open=false;status.textContent='';
        }catch(e){status.textContent=e.name==='TimeoutError'?'Opening sign-in timed out. Retry to reopen the same terminal.':e.message;}
        finally{for(const b of actions.children)b.disabled=false;}
      });
      actions.append(button);
    }
    details.append(actions,status);return details;
  }
  function quotaRing(value,label,stale){
    const ring=element('div','usage-ring'+(stale?' usage-stale':''));
    ring.setAttribute('role','meter');ring.setAttribute('aria-label',label+' remaining'+(stale?' (last reported)':''));
    ring.setAttribute('aria-valuemin','0');ring.setAttribute('aria-valuemax','100');ring.setAttribute('aria-valuenow',String(value));
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 52 52');svg.setAttribute('aria-hidden','true');
    const circumference=2*Math.PI*21;
    for(const foreground of [false,true]){
      const circle=document.createElementNS(ns,'circle');
      for(const [key,v] of Object.entries({cx:26,cy:26,r:21,fill:'none','stroke-width':5,stroke:foreground?(stale?'#9a9996':value<=10?'#e69a81':value<=25?'#dfbc79':'#8fc697'):'#2b2a27'}))circle.setAttribute(key,String(v));
      if(foreground){circle.setAttribute('stroke-dasharray',String(circumference));circle.setAttribute('stroke-dashoffset',String(circumference*(1-value/100)));circle.setAttribute('stroke-linecap','round');if(value===0)circle.setAttribute('visibility','hidden');}
      svg.append(circle);
    }
    ring.append(svg,element('span','usage-ring-center',value.toFixed(value%1?1:0)+'%'));return ring;
  }
  function groups(){
    const all=new Map();
    for(const node of nodes)for(const [index,account] of (node.accounts||[]).entries()){
      const key=account.provider+':'+(account.account_key||node.server+':'+index);
      let group=all.get(key);
      if(!group){group={account,devices:new Set(),connected:node.connected};all.set(key,group);}
      group.devices.add(node.server);
      // One account has one allowance, even when several machines report it.
      if((account.observed_at||0)>(group.account.observed_at||0)){group.account=account;group.connected=node.connected;}
    }
    return [...all.values()].filter(g=>g.devices.has(currentServer));
  }
  function render(){
    if(typeof currentServer==='undefined'||!currentServer)return;
    const panel=document.getElementById('rp-content');if(!panel)return;
    let section=document.getElementById('account-usage');
    if(!section){
      section=element('section','account-usage');section.id='account-usage';
      section.append(element('h3','','Quota remaining'));
      const cards=element('div');cards.id='account-usage-cards';section.append(cards,signInControls());
      panel.prepend(section);
    }
    const content=document.createDocumentFragment();
    const accounts=groups();
    if(!accounts.length)content.append(element('p','usage-note','Waiting for this machine’s account information.'));
    for(const [index,{account:a,devices,connected}] of accounts.entries()){
      const card=element('div','usage-account');
      const provider=a.provider==='codex'?'Codex':'Claude';
      const stale=a.stale||!connected||!a.observed_at||Date.now()/1000-a.observed_at>900;
      const limits=a.windows?.length?a.windows:[null];
      for(const [windowIndex,w] of limits.entries()){
        const expired=w&&(w.reset_passed||(w.resets_at&&w.resets_at<=Date.now()/1000));
        const remaining=w?Number(w.remaining_percent):NaN;
        const valid=w&&Number.isFinite(remaining)&&remaining>=0&&remaining<=100&&w.remaining_percent!==null;
        const tile=element('div','usage-window');
        if(!expired&&valid)tile.append(quotaRing(remaining,w.label,stale));
        else {const unknown=element('div','usage-ring-unknown','—');unknown.setAttribute('aria-hidden','true');tile.append(unknown);}
        const details=element('details','usage-details');details.dataset.account=currentServer+':'+a.provider+':'+(a.account_key||index)+':'+windowIndex;
        const summary=element('summary');summary.append(element('span','usage-provider',provider),element('span','usage-caption',w?w.label:'Account'));details.append(summary);
        details.append(element('div','usage-note',(a.account_key?a.label:'Account identity unavailable')+(a.plan?' · '+a.plan:'')));
        details.append(element('div','usage-note','Used on '+[...devices].join(', ')+(devices.size>1?' · shared allowance':'')));
        if(!a.account_key)details.append(element('div','usage-note','Not combined with other devices.'));
        if(a.status!=='ok')details.append(element('div','usage-note',explanations[a.status]||explanations.unavailable));
        details.append(element('div','usage-note',(stale?'Last reported allowance · ':'')+age(a.observed_at)));
        if(w?.resets_at)details.append(element('div','usage-note','Reset '+new Date(w.resets_at*1000).toLocaleString()));
        tile.append(details);
        if(expired||!valid)tile.append(element('div','usage-note',expired?'Awaiting refreshed limit':({signed_out:'Signed out',awaiting_report:'Waiting for usage'})[a.status]||'Usage unavailable'));
        else if(w.resets_at){const reset=element('div','usage-reset','↻ '+resetLabel(w.resets_at));reset.title='Resets '+new Date(w.resets_at*1000).toLocaleString();reset.setAttribute('aria-label',reset.title);tile.append(reset);}
        if(stale)tile.append(element('div','usage-stale-label','Stale'));
        card.append(tile);
      }
      content.append(card);
    }
    // Avoid unnecessary DOM changes while a user is selecting account text.
    const signature=JSON.stringify(accounts.map(g=>[g.account,[...g.devices],g.connected]))+Math.floor(Date.now()/60000);
    if(section.dataset.snapshot!==signature){
      const open=new Set([...section.querySelectorAll('.usage-details[open]')].map(d=>d.dataset.account));
      for(const d of content.querySelectorAll('.usage-details'))d.open=open.has(d.dataset.account);
      section.querySelector('#account-usage-cards').replaceChildren(content);section.dataset.snapshot=signature;
    }
  }
  async function poll(){
    try{const response=await fetch('/api/account-usage',{signal:AbortSignal.timeout(8000)});if(response.ok){const data=await response.json();if(Array.isArray(data.nodes))nodes=data.nodes;}}catch{}
    render();
    setTimeout(poll,document.hidden?30000:10000);
  }
  const style=element('style');style.textContent=`
    .account-usage{padding:12px 10px 4px;border-bottom:1px solid #444039;margin-bottom:10px}.account-usage h3{font-size:12px;margin:0 0 10px;color:#d4cfc4;letter-spacing:.03em}
    #account-usage-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-bottom:8px}.usage-account{display:contents}
    .usage-window{background:#1a1915;border-radius:8px;padding:10px 6px;display:flex;flex-direction:column;align-items:center;gap:5px;min-width:0;text-align:center}
    .usage-ring{position:relative;width:52px;height:52px;flex:0 0 52px}.usage-ring svg{width:52px;height:52px;transform:rotate(-90deg)}
    .usage-ring-center{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;font-family:monospace;color:#e8e4db}
    .usage-ring-unknown{width:42px;height:42px;margin:5px;border:2px dashed #736658;box-sizing:border-box;border-radius:50%;display:grid;place-items:center;font-size:16px;color:#b9a78c}
    .usage-provider{display:block;color:#c9c1b3;font-size:10px;font-weight:700}.usage-caption{display:block;font-size:9px;font-weight:600;color:#a9a192;overflow-wrap:anywhere;margin-top:2px}
    .usage-note{font-size:10px;color:#b6b0a5;line-height:1.4;overflow-wrap:anywhere}.usage-reset{font-size:9px;color:#a9a192;font-variant-numeric:tabular-nums}.usage-stale-label{font-size:9px;color:#d1ad71}.usage-stale{opacity:.6}
    .usage-details{width:100%}.usage-details summary{cursor:pointer;list-style:none}.usage-details summary::-webkit-details-marker{display:none}.usage-details[open] .usage-note{margin-top:6px}
    .usage-login{margin:0 2px 8px}.usage-login summary{cursor:pointer;font-size:11px;padding:6px 0;color:#d6c8b7}
    .usage-login-actions{display:flex;gap:8px}.usage-login button{min-height:40px;padding:7px 12px;border:1px solid #625d53;border-radius:6px;background:#302e29;color:#eee;cursor:pointer}
    .usage-login button:disabled{opacity:.5;cursor:wait}.usage-login button:focus-visible,.usage-login summary:focus-visible,.usage-details summary:focus-visible{outline:2px solid #dfbc79;outline-offset:1px}
  `;document.head.append(style);
  window.bridgeAccounts={render};poll();
})();
