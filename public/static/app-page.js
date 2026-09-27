(async function(){
  const root=document.getElementById('app-page'),name=root.dataset.appName;
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n};
  async function api(path,options){const r=await fetch(path,options),data=await r.json();if(!r.ok)throw new Error(data.error||`HTTP ${r.status}`);return data}
  function button(text,cls,fn){const b=el('button','tool-button '+(cls||''),text);b.type='button';b.onclick=fn;return b}
  function row(label,value,mono=false){const r=el('div','detail-row');r.append(el('span','',label),el(mono?'code':'span','',value||'—'));return r}
  async function action(verb){try{await api(`/apps/${encodeURIComponent(name)}/${verb}`,{method:'POST'});await load()}catch(e){alert(e.message)}}
  async function load(){
    try{const [app,wolts]=await Promise.all([api(`/apps/${encodeURIComponent(name)}`),api('/wolts')]);root.replaceChildren();
      const back=el('a','app-back','‹ All apps');back.href='/?view=apps';root.append(back);
      const header=el('header','detail-header'),identity=el('div','detail-identity');identity.append(el('div','detail-emoji',app.emoji||'🪓'));
      const titles=el('div');titles.append(el('h1','main-title',app.name),el('p','main-subtitle',(app.running?'● running':'○ stopped')+' · 🔒 Just me'),el('p','',app.description||''));identity.append(titles);header.append(identity);
      const acts=el('div','detail-actions');if(app.running){const open=el('a','tool-button primary','Open ↗');open.href=app.url;acts.append(open,button('■ Stop','danger',()=>action('stop')),button('↻ Restart','',()=>action('restart')))}else if(app.start){acts.append(button('▶ Start','primary',()=>action('start')))}header.append(acts);root.append(header);
      const grid=el('div','detail-grid'),sharing=el('section','detail-card private-card');sharing.append(el('div','private-icon','🔒'));const sc=el('div');sc.append(el('h2','','Who can open it'),el('strong','','Just me'),el('p','','Behind your lodge login. Sharing controls are parked for a later security review.'));sharing.append(sc);grid.append(sharing);
      const side=el('div','detail-side'),where=el('section','detail-card');where.append(el('h2','','Where it lives'),row('In the lodge',app.url,true),row('Its own address',app.tunnel_url||'not configured',true));side.append(where);
      const keep=el('section','detail-card');keep.append(el('h2','','Kept by'));const kr=el('div','keeper-row');kr.append(el('strong','',app.keeper||'unassigned'));const select=el('select');select.append(new Option('Change keeper…',''));wolts.filter(w=>['raccoon','rodent','beaver','otter'].includes(w.type)&&(w.name||w.dir)!==app.keeper).forEach(w=>select.append(new Option(w.name||w.dir,w.name||w.dir)));select.onchange=async()=>{if(!select.value)return;try{await api(`/apps/${encodeURIComponent(name)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({keeper:select.value})});await load()}catch(e){alert(e.message)}};kr.append(select);keep.append(kr);
      const ask=button(`💬 Ask ${app.keeper||'keeper'} about it`,'',async()=>{const r=await api('/sessions/new/lodge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wolt:app.keeper,prompt:`Help me with the ${app.name} app.`})});location.href='/tui?session='+encodeURIComponent(r.name)});const logs=button('▤ Logs','',()=>window.WoltspaceTerminal?.openLogs(app.name));const bar=el('div','detail-actions');bar.append(ask,logs);keep.append(bar);side.append(keep);
      const run=el('section','detail-card');run.append(el('h2','','How it runs'),row('Start',app.start,true),row('Install',app.install,true),row('Stack',`${app.stack||'—'} · port ${app.port}`,false),row('Source',app.source||'local',true));side.append(run);grid.append(side);root.append(grid);
    }catch(e){root.replaceChildren(el('p','',e.message))}
  }
  await load();
})();
