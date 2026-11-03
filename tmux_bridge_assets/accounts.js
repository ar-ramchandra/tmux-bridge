/* Provider account quotas, separate from machine resource percentages. */
(() => {
  'use strict';
  let nodes=[];
  const explanations={signed_out:'Default CLI is not signed in.',unsupported:'This billing method does not expose a subscription quota.',awaiting_report:'Waiting for Claude to report usage after a response.',unavailable:'Usage is unavailable. No remaining allowance is assumed.'};
  function element(tag,cls,value){const e=document.createElement(tag);if(cls)e.className=cls;if(value!==undefined)e.textContent=value;return e;}
  function age(time){if(!time)return 'Not observed yet';const minutes=Math.max(0,Math.floor((Date.now()/1000-time)/60));return minutes<1?'Updated just now':'Updated '+minutes+' min ago';}
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
  function doughnut(value,label,stale){
    const ring=element('div','usage-ring'+(stale?' usage-stale':''));
    ring.style.setProperty('--remaining',value+'%');
    ring.style.setProperty('--quota-color',stale?'#9a9996':value<=10?'#e69a81':value<=25?'#dfbc79':'#8fc697');
    ring.setAttribute('role','meter');ring.setAttribute('aria-label',label+' remaining'+(stale?' (last reported)':''));
    ring.setAttribute('aria-valuemin','0');ring.setAttribute('aria-valuemax','100');ring.setAttribute('aria-valuenow',String(value));
    ring.append(element('span','',value.toFixed(value%1?1:0)+'%'));return ring;
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
      section.append(element('h3','','AI accounts'),signInControls());
      const cards=element('div');cards.id='account-usage-cards';section.append(cards);
      const gauges=panel.querySelector('.gauge-grid');if(gauges)gauges.after(section);else panel.append(section);
    }
    const content=document.createDocumentFragment();
    const accounts=groups();
    if(!accounts.length)content.append(element('p','usage-note','Waiting for this machine’s account information.'));
    for(const {account:a,devices,connected} of accounts){
      const card=element('div','usage-account');
      card.append(element('strong','usage-title',(a.provider==='codex'?'Codex':'Claude')+' · '+a.label+(a.plan?' · '+a.plan:'')));
      card.append(element('div','usage-note','Used on '+[...devices].join(', ')+(devices.size>1?' · shared allowance':'')));
      if(!a.account_key)card.append(element('div','usage-note','Account identity unavailable; not combined with other devices.'));
      if(a.status!=='ok')card.append(element('p','usage-note',explanations[a.status]||explanations.unavailable));
      const stale=a.stale||!connected||!a.observed_at||Date.now()/1000-a.observed_at>900;
      for(const w of a.windows||[]){
        const expired=w.reset_passed||(w.resets_at&&w.resets_at<=Date.now()/1000);
        const row=element('div','usage-window');
        const remaining=Number(w.remaining_percent);
        const valid=Number.isFinite(remaining)&&remaining>=0&&remaining<=100&&w.remaining_percent!==null;
        if(!expired&&valid)row.append(doughnut(remaining,w.label,stale));
        const description=element('div','usage-window-description');
        description.append(element('div','usage-caption',w.label));
        description.append(element('div','usage-note',expired?'Awaiting refreshed limit':!valid?'Allowance unavailable':(stale?'Last reported: ':'')+remaining.toFixed(remaining%1?1:0)+'% remaining'));
        if(w.resets_at)description.append(element('div','usage-note',(expired?'Reset was ':'Resets ')+new Date(w.resets_at*1000).toLocaleString()));
        row.append(description);
        card.append(row);
      }
      card.append(element('div','usage-note',(stale&&a.observed_at?'Stale · ':'')+age(a.observed_at)));
      content.append(card);
    }
    content.append(element('p','usage-note','Limits belong to accounts, not machines. Default CLI accounts and observed Claude sessions are shown.'));
    // Avoid unnecessary DOM changes while a user is selecting account text.
    const signature=JSON.stringify(accounts.map(g=>[g.account,[...g.devices],g.connected]))+Math.floor(Date.now()/60000);
    if(section.dataset.snapshot!==signature){section.querySelector('#account-usage-cards').replaceChildren(content);section.dataset.snapshot=signature;}
  }
  async function poll(){
    try{const response=await fetch('/api/account-usage',{signal:AbortSignal.timeout(8000)});if(response.ok){const data=await response.json();if(Array.isArray(data.nodes))nodes=data.nodes;}}catch{}
    render();
    setTimeout(poll,document.hidden?30000:10000);
  }
  const style=element('style');style.textContent=`
    .account-usage{padding:12px 0;border-bottom:1px solid #444039}.account-usage h3{font-size:13px;margin:0 0 10px}
    .usage-account{padding:9px 0}.usage-title{font-size:12px;overflow-wrap:anywhere}.usage-note{font-size:11px;color:#aaa;line-height:1.5;overflow-wrap:anywhere}
    .usage-window{display:flex;align-items:center;gap:12px;margin:14px 0}.usage-window-description{min-width:0}.usage-caption{font-size:12px;font-weight:600;margin-bottom:4px}
    .usage-ring{width:64px;height:64px;flex:0 0 64px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--quota-color) var(--remaining),#494740 0);position:relative}
    .usage-ring:before{content:'';position:absolute;inset:6px;border-radius:50%;background:#252422}.usage-ring span{position:relative;font-size:13px;font-weight:600;color:#eee}
    .usage-stale{opacity:.65}.usage-login{margin-bottom:10px}.usage-login summary{cursor:pointer;font-size:12px;padding:6px 0;color:#d6c8b7}
    .usage-login-actions{display:flex;gap:8px}.usage-login button{min-height:40px;padding:7px 12px;border:1px solid #625d53;border-radius:6px;background:#302e29;color:#eee;cursor:pointer}
    .usage-login button:disabled{opacity:.5;cursor:wait}.usage-login button:focus-visible,.usage-login summary:focus-visible{outline:2px solid #dfbc79;outline-offset:3px}
  `;document.head.append(style);
  window.bridgeAccounts={render};poll();
})();
