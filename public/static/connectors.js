(async function(){
  const root=document.getElementById('connector-list');
  const make=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el};
  function card({icon,name,line,state,connected,skill,whatsapp=false}){
    const el=make('section','connector-card'+(connected?' connected':''));
    el.append(make('div','connector-icon',icon));
    const copy=make('div','connector-copy');copy.append(make('div','connector-name',name),make('div','connector-line',line));
    copy.append(make('div','connector-state'+(connected?' connected':''),state));el.append(copy);
    if(skill||whatsapp){
      const b=make('button','tool-button',connected?'Fix with a wolt':'Set up with a wolt');
      b.type='button';
      b.onclick=async()=>{
        b.disabled=true;
        try { await setup(skill,name,whatsapp); }
        catch(e) { alert(e.message||'Could not start a setup session.'); }
        finally { b.disabled=false; }
      };
      el.append(b);
    }
    return el;
  }
  async function setup(skill,label,whatsapp){
    const wolts=await fetch('/wolts').then(r=>r.json());const w=wolts.find(x=>['raccoon','rodent','beaver','otter'].includes(x.base_type||x.type));
    if(!w){alert('Create a wolt first.');return}
    const prompt=`Help me ${label.toLowerCase()}. Use /skill:${skill} and guide me through it.`;
    const wolt=w.dir||w.name;
    const body=whatsapp?{wolt,setup:'whatsapp'}:{wolt,prompt};
    const r=await fetch('/sessions/new/lodge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await r.json();if(r.ok&&data.name)location.href='/tui?session='+encodeURIComponent(data.name);else alert(data.error||data.detail||'Could not start a session.');
  }
  try{
    const [tunnel,health]=await Promise.all([fetch('/tunnel').then(r=>r.json()),fetch('/health').then(r=>r.json())]);root.replaceChildren();
    const permanent=tunnel.mode==='named',connected=tunnel.mode!=='off';root.append(card({icon:'☁',name:'Cloudflare',line:tunnel.url||(connected?'Connected':'No lodge address yet'),state:permanent?'permanent address · connected':tunnel.mode==='quick'?'temporary address · connected':'not connected',connected,skill:'woltspace-cloudflare'}));
    const states=new Map((health.connectors||[]).filter(c=>c.name!=='wolf').map(c=>[c.name,c]));
    for(const [name,icon,skill] of [['telegram','✈','woltspace-setup-telegram'],['slack','S','woltspace-setup-slack']]){const s=states.get(name);const on=!!s&&(s.state==='running'||s.status==='running'||s.running===true);root.append(card({icon,name:name[0].toUpperCase()+name.slice(1),line:on?'Ready for messages.':'Not connected yet.',state:on?'connected':s?.state||'not connected',connected:on,skill}))}
    const whatsapp=states.get('whatsapp');
    if(whatsapp){
      const running=whatsapp.state==='running';
      root.append(card({icon:'💬',name:'WhatsApp',line:running?'Send /help in the agent chat to verify delivery.':'Connect your WhatsApp agent.',
        state:whatsapp.state,connected:running,whatsapp:true}));
    }
  }catch(e){root.replaceChildren(make('p','',`Could not load connectors: ${e.message}`))}
})();
