"""Owner-only Telegram command parity; all provider/control side effects mocked."""
import hashlib
import json
import types
import sys
import pytest

from test_whatsapp_agent import _load_adapter


@pytest.fixture
def control(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    effects, replies, spawns, messages = [], [], [], []
    records = {
        "owned": {"name":"owned", "adapter":"whatsapp", "chat_id":"user:1",
                  "whatsapp_account":adapter.ACCOUNT, "status":"running", "alive":True,
                  "hold":{"until":None, "reason":"building"}},
        "foreign": {"name":"foreign", "adapter":"whatsapp", "chat_id":"user:2",
                    "whatsapp_account":adapter.ACCOUNT, "status":"running", "alive":True},
        "rotated": {"name":"rotated", "adapter":"whatsapp", "chat_id":"user:1",
                    "whatsapp_account":"old-account", "status":"running", "alive":True},
        "lodge": {"name":"lodge", "adapter":"lodge", "status":"running", "alive":True},
        "stale": {"name":"stale", "adapter":"whatsapp", "chat_id":"user:1",
                  "whatsapp_account":adapter.ACCOUNT, "status":"stopped", "alive":False},
    }
    apps = [{"name":"dashboard", "running":True, "tunnel_url":"https://app.example.com"}]
    def call(method, path, body=None):
        effects.append((method, path, body))
        if path == "/sessions": return list(records.values())
        if method == "GET" and path.startswith("/sessions/"): return records.get(path.split('/')[-1])
        if path.endswith("/stop") and path.startswith("/sessions/"): return {"status":"stopped"}
        if path.endswith("/hold"): return {"ok":True}
        if path == "/apps": return list(apps)
        if path.startswith("/apps/"): return {"ok":True}
        if path == "/settings/session-expiry": return body or {"idle_timeout_seconds":86400}
        raise AssertionError("unexpected fixed endpoint")
    core = sys.modules["bot.core"]
    monkeypatch.setattr(core, "_call_server", call, raising=False)
    monkeypatch.setattr(core, "get_tunnel_url", lambda:"https://lodge.example.com", raising=False)
    monkeypatch.setattr(adapter, "eligible_wolts", lambda:{"beaver":{}, "otter":{}})
    monkeypatch.setattr(adapter, "api", lambda method,path,payload: (200,replies.append(payload["text"]["body"]) or {}))
    def spawn(prompt, **kwargs):
        spawns.append((prompt,kwargs))
        return {"name":"fresh-session", "url":"https://lodge.example.com/tui?session=fresh-session"}
    monkeypatch.setattr(adapter, "start_claude_session", spawn)
    monkeypatch.setattr(adapter, "message_session", lambda *args: messages.append(args) or {"ok":True})
    return types.SimpleNamespace(adapter=adapter, effects=effects, replies=replies, spawns=spawns,
                                 messages=messages, records=records, apps=apps)


def send(control, text, routes=None, sender="user:1"):
    routes = routes if routes is not None else {}
    control.adapter.handle({"id":"synthetic", "from":sender, "timestamp":"100",
                            "type":"text", "text":{"body":text}}, routes, 100)
    return routes


@pytest.mark.parametrize("command", ["/sessions", "/holds", "/idle", "/wolt", "/new beaver", "/apps", "/kill owned", "/help", "/start"])
def test_all_telegram_command_names_are_owner_controls(control, command):
    send(control, command)
    assert control.replies
    assert not control.messages
    assert len(control.spawns) == (1 if command.startswith('/new') else 0)


def test_wolt_switch_and_legacy_alias_do_not_kill_old_session(control):
    routes = {"user:1":{"wolt":"beaver", "session":"owned"}}
    send(control, "/wolt otter", routes)
    assert routes == {"user:1":{"wolt":"otter"}}
    send(control, "wolt beaver", routes)
    assert routes == {"user:1":{"wolt":"beaver"}}
    assert not control.spawns and not control.effects
    assert "Selected beaver" in control.replies[-1]


def test_new_immediately_creates_with_current_account_and_persisted_route(control):
    routes = {"user:1":{"wolt":"beaver", "session":"owned"}}
    send(control, "/new", routes)
    assert len(control.spawns) == 1
    prompt, kwargs = control.spawns[0]
    assert "Wait for their next message" in prompt and "/new" not in prompt
    assert kwargs["routing"] == {"adapter":"whatsapp", "chat_id":"user:1", "whatsapp_account":control.adapter.ACCOUNT}
    assert routes["user:1"]["session"] == "fresh-session"
    assert control.adapter.load(control.adapter.ROUTES) == routes
    assert not control.effects


def test_new_ack_timeout_does_not_retry_creation_or_claim_start_failed(control, monkeypatch):
    attempts = []
    def timeout(*args):
        attempts.append(1)
        assert control.adapter.load(control.adapter.ROUTES)["user:1"]["session"] == "fresh-session"
        raise TimeoutError("synthetic-private")
    monkeypatch.setattr(control.adapter, "api", timeout)
    send(control, "/new beaver")
    assert len(control.spawns) == 1 and attempts == [1]
    assert not control.replies


def test_new_persistence_failure_after_creation_never_claims_start_failure(control, monkeypatch):
    real_save = control.adapter.atomic_json
    def save(path, value):
        if path == control.adapter.ROUTES and value.get("user:1",{}).get("session"):
            raise OSError("synthetic-private")
        real_save(path,value)
    monkeypatch.setattr(control.adapter, "atomic_json", save)
    send(control,"/new beaver")
    assert len(control.spawns)==1
    assert "session started" in control.replies[-1].lower()
    assert "could not save" in control.replies[-1].lower()
    assert "retry" not in control.replies[-1].lower()


@pytest.mark.parametrize("target", ["foreign", "rotated", "stale", "missing", "../owned", "main"])
@pytest.mark.parametrize("command", ["/kill", "/holds release"])
def test_mutations_reject_foreign_stale_missing_or_malformed_session(control, target, command):
    send(control, f"{command} {target}")
    assert not [effect for effect in control.effects if effect[0] != 'GET']
    assert not control.spawns and not control.messages


def test_kill_uses_fresh_target_and_clears_only_owned_reference(control):
    routes = {"user:1":{"wolt":"beaver", "session":"owned"},
              "user:2":{"wolt":"otter", "session":"owned"}}
    send(control,"/kill owned",routes)
    assert control.effects == [('GET','/sessions/owned',None),('POST','/sessions/owned/stop',None)]
    assert routes == {"user:1":{"wolt":"beaver"},"user:2":{"wolt":"otter","session":"owned"}}
    assert control.adapter.load(control.adapter.ROUTES)==routes


def test_hold_release_uses_same_control_plane_hook(control):
    send(control,"/holds release owned")
    assert control.effects == [('GET','/sessions/owned',None),('DELETE','/sessions/owned/hold',None)]
    assert "Released" in control.replies[-1]


def test_session_list_permissions_are_not_cached_for_later_mutation(control):
    send(control,"/sessions")
    control.records['owned']['whatsapp_account']='foreign-now'
    send(control,"/kill owned")
    assert not [effect for effect in control.effects if effect[0] != 'GET']


@pytest.mark.parametrize("choice,value", [("never",None),("1h",3600),("4h",14400),("1d",86400)])
def test_idle_change_is_explicitly_lodgewide(control, choice, value):
    send(control,f"/idle {choice}")
    assert control.effects == [('POST','/settings/session-expiry',{'idle_timeout_seconds':value})]
    assert "whole lodge" in control.replies[-1]


@pytest.mark.parametrize("action", ['start','stop'])
def test_apps_action_uses_fresh_roster_then_existing_hook(control, action):
    send(control,f"/apps {action} dashboard")
    assert control.effects == [('GET','/apps',None),('POST',f'/apps/dashboard/{action}',None)]


@pytest.mark.parametrize("text", ['/unknown','/sessions owned','/kill','/kill owned extra','/holds release','/idle 5h','/apps stop ../dashboard','/apps delete dashboard','/wolt beaver extra','/new missing'])
def test_bad_or_unknown_commands_never_model_forward(control,text):
    send(control,text)
    assert not control.spawns and not control.messages
    assert not [effect for effect in control.effects if effect[0] != 'GET']


def test_missing_app_and_changed_roster_refuse_action(control):
    send(control,'/apps')
    control.apps.clear()
    send(control,'/apps stop dashboard')
    assert not [effect for effect in control.effects if effect[0]!='GET']


@pytest.mark.parametrize("command", ['/sessions','/holds','/idle never','/wolt beaver','/new beaver','/apps start dashboard','/kill owned','/help','/start'])
def test_foreign_participant_cannot_even_read_lodge_controls(control,command):
    send(control,command,sender='user:foreign')
    assert not control.effects and not control.replies and not control.spawns and not control.messages


def test_unbound_direct_handle_cannot_dispatch_controls_or_model(control):
    control.adapter._owner_path().unlink()
    send(control,'/sessions')
    send(control,'hello',{'user:1':{'wolt':'beaver'}})
    assert not control.effects and not control.replies and not control.spawns and not control.messages


def test_key_account_mismatch_fails_before_control_dispatch(control,monkeypatch):
    monkeypatch.setattr(control.adapter,'TOKEN','changed-synthetic-key')
    with pytest.raises(control.adapter.WhatsAppConfigurationError):
        send(control,'/kill owned')
    assert not control.effects and not control.replies


def test_lists_exclude_foreign_whatsapp_and_only_use_public_lodge_links(control,monkeypatch):
    control.records['owned']['url']='http://127.0.0.1:7777/private?token=secret'
    send(control,'/sessions')
    text=control.replies[-1]
    assert 'owned' in text and 'lodge' in text
    assert 'foreign' not in text and 'rotated' not in text and 'stale' not in text
    assert 'https://lodge.example.com/tui?session=owned' in text
    assert '127.0.0.1' not in text and 'secret' not in text
    core=sys.modules['bot.core']
    monkeypatch.setattr(core,'get_tunnel_url',lambda:'https://user:secret@localhost:7777/?token=secret')
    send(control,'/sessions')
    assert 'https://' not in control.replies[-1] and 'secret' not in control.replies[-1]


def poll(adapter, tmp_path, items, dispatch):
    body={'next_offset':10,'entry':[{'changes':[{'value':{'messages':items}}]}]}
    return adapter.poll_once({'offset':0,'processed':[]},{},100,cursor_path=tmp_path/'cursor.json',
        request=lambda *_:(200,body),dispatch=dispatch)


def item(text='/help',sender='user:1',**extra):
    return {'id':'fresh','from':sender,'timestamp':'100','type':'text','text':{'body':text},**extra}


def test_first_authenticated_fresh_provider_text_binds_before_dispatch(control,tmp_path):
    adapter=control.adapter
    adapter._owner_path().unlink()
    observed=[]
    def dispatch(message,*_):
        observed.append(adapter._creator())
        adapter.handle(message,{},100)
    poll(adapter,tmp_path,[item()],dispatch)
    assert observed==['user:1'] and control.replies
    assert adapter._owner_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('invalid', [item(timestamp='99'),item(timestamp='bad'),item(id=''),item(sender='foreign'),item(text=' '),item(text='x',type='image'),item(text='x',text_override=True)])
def test_stale_or_malformed_fresh_items_cannot_establish_binding(control,tmp_path,invalid):
    adapter=control.adapter
    adapter._owner_path().unlink()
    if invalid.pop('text_override',False): invalid={**invalid,'text':{}}
    seen=[]
    poll(adapter,tmp_path,[invalid],lambda *_:seen.append(1))
    assert not seen and not adapter._owner_path().exists()


def test_creator_persistence_failure_prevents_dispatch_even_after_checkpoint(control,tmp_path,monkeypatch):
    adapter=control.adapter
    adapter._owner_path().unlink()
    save=adapter.atomic_json
    def fail(path,value):
        if path==adapter._owner_path():raise OSError('synthetic-private')
        save(path,value)
    monkeypatch.setattr(adapter,'atomic_json',fail)
    seen=[]
    with pytest.raises(adapter.WhatsAppConfigurationError):
        poll(adapter,tmp_path,[item()],lambda *_:seen.append(1))
    assert not seen and not adapter._owner_path().exists()
    assert json.loads((tmp_path/'cursor.json').read_text())['processed']==['fresh']


@pytest.mark.parametrize('corrupt',[{'account':'other','participant':'user:1'}, {'account':'current','participant':[]}, {}])
def test_malformed_or_foreign_binding_is_never_overwritten(control,tmp_path,corrupt):
    adapter=control.adapter
    adapter.atomic_json(adapter._owner_path(),corrupt)
    before=adapter._owner_path().read_bytes()
    with pytest.raises(adapter.WhatsAppConfigurationError):
        poll(adapter,tmp_path,[item()],lambda *_:pytest.fail('no dispatch'))
    assert adapter._owner_path().read_bytes()==before


def test_upgrade_same_key_can_bind_without_changing_existing_route(control,tmp_path):
    adapter=control.adapter
    adapter._owner_path().unlink()
    routes={'user:1':{'wolt':'beaver','session':'owned'}}
    adapter.atomic_json(adapter.ROUTES,routes)
    poll(adapter,tmp_path,[item()],lambda *_:None)
    assert adapter._creator()=='user:1' and adapter.load(adapter.ROUTES)==routes


def test_rotated_key_uses_distinct_binding_without_inheriting_owner(control,tmp_path,monkeypatch):
    adapter=control.adapter
    previous=adapter._owner_path()
    previous_bytes=previous.read_bytes()
    token='new-synthetic-key'
    monkeypatch.setattr(adapter,'TOKEN',token)
    monkeypatch.setattr(adapter,'ACCOUNT',hashlib.sha256(token.encode()).hexdigest()[:20])
    assert adapter._creator() is None
    send(control,'/sessions')
    assert not control.effects
    poll(adapter,tmp_path,[item(sender='user:newcreator')],lambda *_:None)
    assert adapter._creator()=='user:newcreator'
    assert previous.read_bytes()==previous_bytes
    send(control,'/sessions',sender='user:1')
    assert not control.effects


def test_ack_timeout_after_mutation_never_repeats_mutation(control,monkeypatch):
    attempts=[]
    def timeout(*_):attempts.append(1);raise TimeoutError('synthetic-private')
    monkeypatch.setattr(control.adapter,'api',timeout)
    with pytest.raises(TimeoutError):send(control,'/idle never')
    assert attempts==[1]
    assert [effect for effect in control.effects if effect[0]=='POST']==[('POST','/settings/session-expiry',{'idle_timeout_seconds':None})]


def test_foreign_provider_message_does_not_replace_creator_binding(control,tmp_path):
    adapter=control.adapter
    previous=adapter._owner_path().read_bytes()
    seen=[]
    poll(adapter,tmp_path,[item(sender='user:foreign')],lambda *_:seen.append(1))
    assert not seen and adapter._owner_path().read_bytes()==previous


def test_foreign_nested_routing_and_malformed_routing_are_not_mutable(control):
    control.records['lodge']['routing']=[{'adapter':'whatsapp','chat_id':'user:1','whatsapp_account':'foreign'}]
    send(control,'/kill lodge')
    control.records['lodge']['routing']='malformed'
    send(control,'/kill lodge')
    assert not [effect for effect in control.effects if effect[0]!='GET']


def test_hold_release_rejects_dead_held_session(control):
    control.records['owned']['alive']=False
    send(control,'/holds release owned')
    assert not [effect for effect in control.effects if effect[0]!='GET']


def test_provider_non_success_cannot_establish_creator_binding(control,tmp_path):
    adapter=control.adapter
    adapter._owner_path().unlink()
    body={'next_offset':10,'entry':[{'changes':[{'value':{'messages':[item()]}}]}]}
    with pytest.raises(ValueError,match='did not succeed'):
        adapter.poll_once({'offset':0,'processed':[]},{},100,cursor_path=tmp_path/'cursor.json',
            request=lambda *_:(403,body),dispatch=lambda *_:pytest.fail('no dispatch'))
    assert not adapter._owner_path().exists()


@pytest.mark.parametrize('kind',['mode','symlink'])
def test_unsafe_creator_binding_is_never_read_or_overwritten(control,tmp_path,kind):
    adapter=control.adapter
    path=adapter._owner_path()
    if kind=='mode':path.chmod(0o644)
    else:
        path.unlink()
        target=tmp_path/'synthetic-binding'
        target.write_text(json.dumps({'account':adapter.ACCOUNT,'participant':'user:1'}))
        target.chmod(0o600)
        path.symlink_to(target)
    with pytest.raises(adapter.WhatsAppConfigurationError):
        poll(adapter,tmp_path,[item()],lambda *_:pytest.fail('no dispatch'))
    assert not control.effects and not control.replies


def test_listing_output_is_bounded_to_one_provider_message(control):
    control.records.clear()
    control.records.update({f'session-{index}':{'name':f'session-{index}','status':'running','alive':True,'adapter':'lodge'} for index in range(300)})
    send(control,'/sessions')
    assert len(control.replies)==1 and len(control.replies[0])<=4096
    assert 'truncated' in control.replies[0]


@pytest.mark.parametrize('url',[
    'http://localhost:7777','https://127.0.0.1','https://127.0.0.1.',
    'https://localhost.','https://[::1]','https://10.0.0.1',
    'https://user:secret@lodge.example.com','https://lodge.example.com/?token=secret',
    'https://lodge.example.com/#secret','https://service.internal.',
    'https://lodge.example.com/private-path','https://127.0.0.1%2e',
])
def test_nonpublic_or_credential_bearing_link_is_omitted(control,url):
    assert control.adapter._public_url(url)==''


def test_fifo_creator_binding_fails_closed_without_blocking(control,tmp_path):
    import os
    adapter=control.adapter
    path=adapter._owner_path()
    path.unlink()
    os.mkfifo(path,0o600)
    with pytest.raises(adapter.WhatsAppConfigurationError):
        poll(adapter,tmp_path,[item()],lambda *_:pytest.fail('no dispatch'))
    assert not control.effects


def test_new_without_tunnel_always_acknowledges_session_name(control,monkeypatch):
    monkeypatch.setattr(sys.modules['bot.core'],'get_tunnel_url',lambda:None)
    monkeypatch.setattr(control.adapter,'start_claude_session',lambda *_,**__: {'name':'fresh-session','url':''})
    send(control,'/new beaver')
    assert control.replies==['Session started: fresh-session']


def test_new_ack_does_not_expose_private_returned_url(control,monkeypatch):
    monkeypatch.setattr(control.adapter,'start_claude_session',lambda *_,**__: {'name':'fresh-session','url':'http://127.0.0.1:7777/private?token=secret'})
    send(control,'/new beaver')
    assert control.replies==['Session started: https://lodge.example.com/tui?session=fresh-session']
