/* Provider account quotas, separate from machine resource percentages. */
(() => {
  'use strict';
  let nodes=[];
  const explanations={signed_out:'Default CLI is not signed in.',unsupported:'This billing method does not expose a subscription quota.',awaiting_report:'Waiting for Claude to report usage after a response.',unavailable:'Usage is unavailable. No remaining allowance is assumed.'};
  function element(tag,cls,value){const e=document.createElement(tag);if(cls)e.className=cls;if(value!==undefined)e.textContent=value;return e;}
  function age(time){if(!time)return 'Not observed yet';const minutes=Math.max(0,Math.floor((Date.now()/1000-time)/60));return minutes<1?'Updated just now':'Updated '+minutes+' min ago';}
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
    if(!section){section=element('section','account-usage');section.id='account-usage';const gauges=panel.querySelector('.gauge-grid');if(gauges)gauges.after(section);else panel.append(section);}
    const content=document.createDocumentFragment();
    content.append(element('h3','', 'AI accounts'));
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
        row.append(element('div','usage-caption',w.label+' · '+(expired?'Awaiting refreshed limit':(stale?'Last reported: ':'')+remaining.toFixed(remaining%1?1:0)+'% remaining')));
        if(!expired){const bar=element('progress');bar.max=100;bar.value=remaining;bar.setAttribute('aria-label',w.label+' remaining');if(stale)bar.classList.add('usage-stale');row.append(bar);}
        if(w.resets_at)row.append(element('div','usage-note',(expired?'Reset was ':'Resets ')+new Date(w.resets_at*1000).toLocaleString()));
        card.append(row);
      }
      card.append(element('div','usage-note',(stale&&a.observed_at?'Stale · ':'')+age(a.observed_at)));
      content.append(card);
    }
    content.append(element('p','usage-note','Limits belong to accounts, not machines. Default CLI accounts and observed Claude sessions are shown.'));
    // Avoid unnecessary DOM changes while a user is selecting account text.
    const signature=JSON.stringify(accounts.map(g=>[g.account,[...g.devices],g.connected]))+Math.floor(Date.now()/60000);
    if(section.dataset.snapshot!==signature){section.replaceChildren(content);section.dataset.snapshot=signature;}
  }
  async function poll(){
    try{const response=await fetch('/api/account-usage',{signal:AbortSignal.timeout(8000)});if(response.ok){const data=await response.json();if(Array.isArray(data.nodes))nodes=data.nodes;}}catch{}
    render();
    setTimeout(poll,document.hidden?30000:10000);
  }
  const style=element('style');style.textContent='.account-usage{padding:12px 0;border-bottom:1px solid #444039}.account-usage h3{font-size:13px;margin:0 0 10px}.usage-account{padding:9px 0}.usage-title{font-size:12px;overflow-wrap:anywhere}.usage-note{font-size:11px;color:#aaa;line-height:1.5;overflow-wrap:anywhere}.usage-window{margin:10px 0}.usage-caption{font-size:12px;margin-bottom:4px}.usage-window progress{width:100%;height:7px;accent-color:#8fc697}.usage-stale{opacity:.4}';document.head.append(style);
  window.bridgeAccounts={render};poll();
})();
