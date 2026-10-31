(function(){
  const root=document.getElementById('inbox-list');
  const messages=window.__woltInbox&&window.__woltInbox.page(document.getElementById('inbox-messages'));
  const make=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el};
  const age=s=>s<60?`${Math.floor(s)}s`:s<3600?`${Math.floor(s/60)}m`:s<86400?`${Math.floor(s/3600)}h`:`${Math.floor(s/86400)}d`;
  function card(w,now){
    const waiting=w.queued_human+w.queued_agent;
    const el=make('section','connector-card inbox-card'+(w.queued_human?' human':'')+(w.held?' held':''));
    el.append(make('div','connector-icon',w.queued_human?'✉':'📭'));
    const copy=make('div','connector-copy');
    // Two separate controls: open the wolt's site, or show its messages below.
    const name=make('div','connector-name');
    const site=make('a','',w.wolt);site.href=`/wolt/${encodeURIComponent(w.wolt)}/site/`;
    name.append(site);
    if(messages){const show=make('button','tool-button','Show messages');show.type='button';
      show.addEventListener('click',()=>{messages.setFilter(w.wolt);document.getElementById('inbox-messages').scrollIntoView({block:'start'})});
      name.append(' ',show)}
    copy.append(name);
    const counts=[`${w.queued_human} from you`,`${w.queued_agent} from agents`,`${w.claimed} being read`];
    if(w.held)counts.push(`${w.held} held`);
    copy.append(make('div','connector-line',counts.join(' · ')));
    const state=make('div','connector-state');
    if(w.state!=='open')state.append(make('span','',`${w.state} · `));
    state.append(make('span','',waiting?`waiting · oldest ${age(Math.max(0,now-w.oldest_queued_at))}`:'nothing waiting'));
    if(w.held)state.append(make('span','warn',` · ${w.held} held: its reader is gone`));
    copy.append(state);
    el.append(copy);
    return el;
  }
  async function load(){
    try{
      const r=await fetch('/inbox/summary');const data=await r.json();
      if(r.status===404&&data.error==='inbox_disabled'){root.replaceChildren(make('p','','The inbox is off in this lodge.'));return}
      if(!r.ok)throw new Error(data.error||r.status);
      root.replaceChildren(...(data.wolts.length?data.wolts.map(w=>card(w,data.now)):[make('p','','No mail yet.')]));
      if(messages)messages.setWolts(data.wolts.map(w=>w.wolt));
    }catch(e){root.replaceChildren(make('p','',`Could not load the inbox: ${e.message}`))}
  }
  load();setInterval(()=>{if(document.visibilityState==='visible')load()},15000);
})();
