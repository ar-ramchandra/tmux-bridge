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
  function quotaBar(value,label,stale){
    const bar=element('div','usage-bar'+(stale?' usage-stale':''));
    bar.style.setProperty('--remaining',value+'%');
    bar.style.setProperty('--quota-color',stale?'#9a9996':value<=10?'#e69a81':value<=25?'#dfbc79':'#8fc697');
    bar.setAttribute('role','meter');bar.setAttribute('aria-label',label+' remaining'+(stale?' (last reported)':''));
    bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');bar.setAttribute('aria-valuenow',String(value));
    return bar;
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
      const card=element('div','usage-account '+(a.provider==='codex'?'usage-codex':'usage-claude'));
      const header=element('div','usage-card-heading'),mark=element('span','usage-provider-mark',a.provider==='codex'?'›_':'✳');mark.setAttribute('aria-hidden','true');
      header.append(mark,element('strong','usage-title',a.provider==='codex'?'Codex':'Claude'));
      if(a.plan)header.append(element('span','usage-plan',a.plan));
      card.append(header,element('div','usage-identity',a.account_key?a.label:'Account unknown'));
      const stale=a.stale||!connected||!a.observed_at||Date.now()/1000-a.observed_at>900;
      const windows=element('div','usage-windows');
      for(const w of a.windows||[]){
        const expired=w.reset_passed||(w.resets_at&&w.resets_at<=Date.now()/1000);
        const row=element('div','usage-window');
        const remaining=Number(w.remaining_percent);
        const valid=Number.isFinite(remaining)&&remaining>=0&&remaining<=100&&w.remaining_percent!==null;
        row.append(element('div','usage-caption',w.label));
        row.append(element('strong','usage-value',!expired&&valid?remaining.toFixed(remaining%1?1:0)+'%':'—'));
        if(!expired&&valid)row.append(quotaBar(remaining,w.label,stale));
        else {const unknown=element('div','usage-bar-unknown');unknown.setAttribute('aria-hidden','true');row.append(unknown);}
        if(expired||!valid)row.append(element('div','usage-note',expired?'Awaiting refreshed limit':'Unavailable'));
        else if(w.resets_at){
          const reset=element('div','usage-reset','↻ '+resetLabel(w.resets_at));
          reset.title='Resets '+new Date(w.resets_at*1000).toLocaleString();reset.setAttribute('aria-label',reset.title);row.append(reset);
        }
        windows.append(row);
      }
      if(windows.children.length)card.append(windows);
      else {
        const empty=element('div','usage-empty'),symbol=element('span','usage-empty-symbol','◌');symbol.setAttribute('aria-hidden','true');
        empty.append(symbol,element('span','',({signed_out:'Signed out',unsupported:'No quota available',awaiting_report:'Waiting for usage',unavailable:'Usage unavailable'})[a.status]||'Waiting for usage'));card.append(empty);
      }
      const details=element('details','usage-details');details.dataset.account=currentServer+':'+a.provider+':'+(a.account_key||index);
      const summary=element('summary'),dot=element('span','usage-freshness'+(stale?' usage-old':''));dot.setAttribute('aria-hidden','true');
      summary.append(dot,element('span','',stale?'Stale · '+age(a.observed_at):age(a.observed_at)),element('span','usage-device-count',devices.size>1?devices.size+' devices':'1 device'));details.append(summary);
      details.append(element('div','usage-note','Used on '+[...devices].join(', ')+(devices.size>1?' · shared allowance':'')));
      if(!a.account_key)details.append(element('div','usage-note','Account identity unavailable; not combined with other devices.'));
      if(a.status!=='ok')details.append(element('div','usage-note',explanations[a.status]||explanations.unavailable));
      if(stale)details.append(element('div','usage-note','Last reported allowance; a fresh reading is not available.'));
      for(const w of a.windows||[])if(w.resets_at)details.append(element('div','usage-note',w.label+' · Reset '+new Date(w.resets_at*1000).toLocaleString()));
      card.append(details);
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
    .account-usage{padding:12px 8px 4px;border-bottom:1px solid #444039;margin-bottom:10px}.account-usage h3{font-size:12px;margin:0 2px 12px;color:#d4cfc4;letter-spacing:.03em}
    .usage-account{--provider:#a7c9ae;padding:8px 10px 0;margin-bottom:8px;border:1px solid #42483e;border-radius:9px;background:linear-gradient(135deg,#2c342d,#22231f 65%);overflow:hidden}
    .usage-claude{--provider:#e4ac89;border-color:#514036;background:linear-gradient(135deg,#3b2e28,#26221f 65%)}
    .usage-card-heading{display:flex;align-items:center;gap:7px;min-width:0}.usage-provider-mark{color:var(--provider);font-size:20px;line-height:1;font-weight:700}
    .usage-title{font-size:13px;color:#f1ede4}.usage-plan{margin-left:auto;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;border:1px solid #625b4d;border-radius:12px;padding:2px 6px;color:#ddd2c1}
    .usage-identity{font-size:10px;color:#b9b4aa;margin:4px 0 9px;overflow-wrap:anywhere}.usage-note{font-size:11px;color:#b6b0a5;line-height:1.5;overflow-wrap:anywhere}
    .usage-windows{display:grid;gap:12px;padding:2px 0 10px}.usage-window{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:5px;min-width:0}
    .usage-caption{font-size:11px;font-weight:600;color:#ddd6c9;overflow-wrap:anywhere}.usage-value{font-size:12px;font-variant-numeric:tabular-nums;color:#eee7da}.usage-window>.usage-note{grid-column:1/-1}
    .usage-bar{grid-column:1/-1;width:100%;height:6px;border-radius:3px;background:linear-gradient(to right,var(--quota-color) var(--remaining),#46423b 0)}
    .usage-bar-unknown{grid-column:1/-1;height:4px;border:1px dashed #736658;border-radius:3px}
    .usage-reset{grid-column:1/-1;text-align:right;font-size:10px;color:#c2b8a5;font-variant-numeric:tabular-nums}
    .usage-stale{opacity:.6}.usage-empty{display:flex;align-items:center;gap:10px;font-size:11px;color:#c9beac;padding:0 0 12px}.usage-empty-symbol{font-size:32px;color:var(--provider)}
    .usage-details{border-top:1px solid #ffffff12;padding:0 0 2px}.usage-details summary{display:flex;align-items:center;gap:5px;cursor:pointer;min-height:34px;font-size:9px;color:#b9b2a4;list-style:none}.usage-details summary::-webkit-details-marker{display:none}.usage-device-count{margin-left:auto;white-space:nowrap}.usage-device-count:after{content:' ⌄'}.usage-details[open] .usage-device-count:after{content:' ⌃'}.usage-details .usage-note{margin:0 0 8px}
    .usage-freshness{width:5px;height:5px;background:#93ba97;border-radius:50%;flex-shrink:0}.usage-freshness.usage-old{background:#d1ad71}
    .usage-login{margin:0 2px 8px}.usage-login summary{cursor:pointer;font-size:11px;padding:6px 0;color:#d6c8b7}
    .usage-login-actions{display:flex;gap:8px}.usage-login button{min-height:40px;padding:7px 12px;border:1px solid #625d53;border-radius:6px;background:#302e29;color:#eee;cursor:pointer}
    .usage-login button:disabled{opacity:.5;cursor:wait}.usage-login button:focus-visible,.usage-login summary:focus-visible,.usage-details summary:focus-visible{outline:2px solid #dfbc79;outline-offset:1px}
  `;document.head.append(style);
  window.bridgeAccounts={render};poll();
})();
