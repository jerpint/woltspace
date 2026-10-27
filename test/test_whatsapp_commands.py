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


def test_foreign_provider_message_does_not_replace_creator_binding(control,tmp_path):
    adapter=control.adapter
    previous=adapter._owner_path().read_bytes()
    seen=[]
    poll(adapter,tmp_path,[item(sender='user:foreign')],lambda *_:seen.append(1))
    assert not seen and adapter._owner_path().read_bytes()==previous


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


def test_fifo_creator_binding_fails_closed_without_blocking(control,tmp_path):
    import os
    adapter=control.adapter
    path=adapter._owner_path()
    path.unlink()
    os.mkfifo(path,0o600)
    with pytest.raises(adapter.WhatsAppConfigurationError):
        poll(adapter,tmp_path,[item()],lambda *_:pytest.fail('no dispatch'))
    assert not control.effects
