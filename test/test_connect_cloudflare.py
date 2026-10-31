"""One code connects a lodge to Cloudflare."""

import base64
import json
import stat

import pytest

from woltspace import connect_cloudflare as connect
from woltspace.layout import RuntimeLayout

SECRET = "eyJhIjoiYWNjb3VudCIsInQiOiJ0dW5uZWwifQ"
FIELDS = {
    "url": "https://lodge.ery.woltspace.app",
    "tunnel_token": SECRET,
    "team_domain": "team.cloudflareaccess.com",
    "apps_aud": "aud-tag",
    "owner_email": "ery@example.com",
}


@pytest.fixture(autouse=True)
def no_address_from_the_session(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_TUNNEL_URL", raising=False)


def code(**changes):
    body = json.dumps({**FIELDS, **changes}).encode()
    return connect.PREFIX + base64.urlsafe_b64encode(body).decode().rstrip("=")


def layout(root):
    return RuntimeLayout.from_env({"WOLTSPACE_WOLTS_DIR": str(root)})


def test_the_gatekeep_format_decodes():
    decoded = connect.decode(f"  {code()}\n")
    assert decoded.url == FIELDS["url"] and decoded.tunnel_token == SECRET and decoded.apps_aud == "aud-tag"


@pytest.mark.parametrize("bad", [
    "", "hello", connect.PREFIX, connect.PREFIX + "!!!", connect.PREFIX + "bm90IGpzb24",
    code(url="http://lodge.ery.woltspace.app"), code(url="https://woltspace.app"),
    code(url="https://lodge.ery.woltspace.app/path"), code(tunnel_token=""), code(tunnel_token="a b"),
    code(tunnel_token="a\nEVIL=1"), code(apps_aud=""), code(owner_email="nope"), code(team_domain="nodots"),
])
def test_a_bad_code_is_refused_without_repeating_it(bad):
    with pytest.raises(connect.BadCode) as caught:
        connect.decode(bad)
    assert SECRET not in str(caught.value)


def test_it_saves_everything_the_lodge_needs(tmp_path):
    connect.apply(layout(tmp_path), connect.decode(code()))
    env = (tmp_path / ".env").read_text()
    assert f"CLOUDFLARE_TUNNEL_TOKEN={SECRET}" in env
    assert "CLOUDFLARE_TUNNEL_URL=https://lodge.ery.woltspace.app" in env
    assert "WOLTSPACE_PUBLIC_TUNNEL=true" in env
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    config = json.loads((tmp_path / "woltspace.json").read_text())
    assert config["ways_in"]["cloudflare"] == {
        "team_domain": "team.cloudflareaccess.com", "apps_aud": "aud-tag", "owner_email": "ery@example.com"}
    assert config["mill"]["remote_sites"] is True


def test_it_leaves_everything_else_alone_and_can_run_twice(tmp_path):
    env = "A=1\nKEY=\"-----BEGIN\nCLOUDFLARE_NOT_THIS=2\n-----END\"\nCLOUDFLARE_TUNNEL_URL=https://old.example.com\nZ=3\n"
    (tmp_path / ".env").write_text(env)
    (tmp_path / "woltspace.json").write_text(json.dumps({"telegram": {"x": 1}, "mill": {"other": 2}}))
    connect.apply(layout(tmp_path), connect.decode(code()))
    first = (tmp_path / ".env").read_text(), (tmp_path / "woltspace.json").read_text()
    assert first[0].startswith("A=1\nKEY=\"-----BEGIN\nCLOUDFLARE_NOT_THIS=2\n-----END\"\nCLOUDFLARE_TUNNEL_URL=https://lodge.ery")
    config = json.loads(first[1])
    assert config["telegram"] == {"x": 1} and config["mill"] == {"other": 2, "remote_sites": True}
    connect.apply(layout(tmp_path), connect.decode(code()))
    assert ((tmp_path / ".env").read_text(), (tmp_path / "woltspace.json").read_text()) == first


def test_an_app_holding_a_reserved_name_keeps_remote_sites_off(tmp_path):
    (tmp_path / "apps" / "site-old").mkdir(parents=True)
    result = connect.apply(layout(tmp_path), connect.decode(code()))
    assert result["remote_sites_held_by"] == ["site-old"]
    assert "mill" not in json.loads((tmp_path / "woltspace.json").read_text())


def _settings(root):
    return json.loads((root / "woltspace.json").read_text())


def test_a_code_doesnt_decide_a_domain_the_lodge_already_used_is_clean(tmp_path):
    (tmp_path / ".env").write_text("CLOUDFLARE_TUNNEL_URL=https://other.ery.woltspace.app\n")
    result = connect.apply(layout(tmp_path), connect.decode(code()))
    assert result["remote_sites"] is False and "remote_sites" not in _settings(tmp_path).get("mill", {})


@pytest.mark.parametrize("env", ["", "CLOUDFLARE_TUNNEL_URL=https://lodge.ery.woltspace.app\n",
                                 "CLOUDFLARE_TUNNEL_URL=https://old.elsewhere.example.com\n"])
def test_a_code_never_overrides_the_owners_refusal(tmp_path, env):
    (tmp_path / ".env").write_text(env)
    (tmp_path / "woltspace.json").write_text(json.dumps({"mill": {"remote_sites": False}}))
    connect.apply(layout(tmp_path), connect.decode(code()))
    assert _settings(tmp_path)["mill"]["remote_sites"] is False


def test_a_domain_in_use_is_found_wherever_the_lodge_reads_it(tmp_path, monkeypatch):
    used = "https://other.ery.woltspace.app"
    (tmp_path / ".env").write_text(f"export CLOUDFLARE_TUNNEL_URL={used}\n")
    assert connect.current_url(layout(tmp_path)) == used
    (tmp_path / ".env").write_text("")
    monkeypatch.setattr("woltspace.lifecycle.read_tunnel_url", lambda _layout: used)
    assert connect.current_url(layout(tmp_path)) == used
    monkeypatch.setattr("woltspace.lifecycle.read_tunnel_url", lambda _layout: "")
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_URL", used)
    assert connect.current_url(layout(tmp_path)) == used
    assert connect.apply(layout(tmp_path), connect.decode(code()))["remote_sites"] is False


def test_moving_to_a_new_domain_turns_remote_sites_on(tmp_path):
    (tmp_path / ".env").write_text("CLOUDFLARE_TUNNEL_URL=https://old.elsewhere.example.com\n")
    connect.apply(layout(tmp_path), connect.decode(code()))
    assert _settings(tmp_path)["mill"]["remote_sites"] is True


def _run(tmp_path, monkeypatch, capsys, *args, stdin=None):
    import io
    from woltspace import cli
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(code() if stdin is None else stdin))
    monkeypatch.setattr(cli, "_wolf_api", lambda: "http://127.0.0.1:9")
    status = cli.main(["connect", "cloudflare", *args])
    out = capsys.readouterr()
    return status, out.out + out.err


def test_the_command_never_prints_the_token_and_says_when_it_opens(tmp_path, monkeypatch, capsys):
    status, output = _run(tmp_path, monkeypatch, capsys)
    assert status == 0 and SECRET not in output and "lodge.ery.woltspace.app" in output
    assert (tmp_path / ".env").is_file()


def test_the_command_wont_move_a_lodge_unless_told_to(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("CLOUDFLARE_TUNNEL_URL=https://lodge.other.example.com\n")
    status, output = _run(tmp_path, monkeypatch, capsys)
    assert status == 1 and "already at" in output
    assert "ery" not in (tmp_path / ".env").read_text()
    status, _ = _run(tmp_path, monkeypatch, capsys, "--replace")
    assert status == 0 and "lodge.ery" in (tmp_path / ".env").read_text()


def test_the_command_refuses_a_bad_code_and_saves_nothing(tmp_path, monkeypatch, capsys):
    status, _ = _run(tmp_path, monkeypatch, capsys, stdin="nope")
    assert status == 1 and not (tmp_path / ".env").exists()


def test_reconnect_reads_the_file_and_starts_the_tunnel(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from server import tunnel as tunnel_mgr
    (tmp_path / ".env").write_text(
        f"WOLTSPACE_PUBLIC_TUNNEL=true\nCLOUDFLARE_TUNNEL_URL={FIELDS['url']}\nCLOUDFLARE_TUNNEL_TOKEN={SECRET}\n")
    monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "false")
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_URL", "https://stale.example.com")
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_TOKEN", "stale")
    started = []

    def start():
        started.append((tunnel_mgr.os.environ["WOLTSPACE_PUBLIC_TUNNEL"], tunnel_mgr.os.environ["CLOUDFLARE_TUNNEL_URL"],
                        tunnel_mgr.os.environ["CLOUDFLARE_TUNNEL_TOKEN"]))
        monkeypatch.setattr(tunnel_mgr, "_tunnel_url", FIELDS["url"])

    monkeypatch.setattr(tunnel_mgr, "start_tunnel", start)
    assert tunnel_mgr.reconnect(tmp_path / ".env") is True
    assert started == [("true", FIELDS["url"], SECRET)]


def test_a_failed_tunnel_says_so(tmp_path, monkeypatch):
    from server import tunnel as tunnel_mgr
    (tmp_path / ".env").write_text("")
    monkeypatch.setattr(tunnel_mgr, "_tunnel_url", "https://old.example.com")
    monkeypatch.setattr(tunnel_mgr, "start_tunnel", lambda: None)
    assert tunnel_mgr.reconnect(tmp_path / ".env") is False


def test_a_wolts_key_may_start_the_tunnel_the_lodge_names():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "container" / "lib"))
    import lodge_auth
    assert lodge_auth.wolt_may("POST", "/tunnel/reconnect")


def _api(monkeypatch, tmp_path, up=True, **env):
    from starlette.testclient import TestClient
    import server.app as server_app
    monkeypatch.delenv("WOLTSPACE_LODGE_AUTH", raising=False)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(server_app.tunnel_mgr, "reconnect", lambda env_file: up)
    return TestClient(server_app.app, base_url="http://localhost:7777", client=("127.0.0.1", 50000))


def test_the_lodge_reconnects_on_request(tmp_path, monkeypatch):
    assert _api(monkeypatch, tmp_path).post("/tunnel/reconnect").status_code == 200


def test_the_lodge_says_when_the_tunnel_didnt_start(tmp_path, monkeypatch):
    assert _api(monkeypatch, tmp_path, up=False).post("/tunnel/reconnect").status_code == 502


def test_a_guest_lodge_doesnt_publish(tmp_path, monkeypatch):
    client = _api(monkeypatch, tmp_path, WOLTSPACE_ISOLATION="external")
    assert client.post("/tunnel/reconnect").status_code == 409
