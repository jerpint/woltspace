"""The named-tunnel token never appears on a command line."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

import tunnel


class _Proc:
    pid = 4242

    def poll(self):
        return None


def test_named_tunnel_token_goes_in_the_environment_not_argv(monkeypatch):
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel.time, "sleep", lambda _s: None)

    result = tunnel.start_named_tunnel("tok-very-secret", host_header="lodge.example")

    assert result["pid"] == 4242
    assert seen["cmd"][:3] == ["cloudflared", "tunnel", "run"]
    assert not any("tok-very-secret" in part for part in seen["cmd"])
    assert "--token" not in seen["cmd"]
    assert seen["env"]["TUNNEL_TOKEN"] == "tok-very-secret"
    assert seen["cmd"][-2:] == ["--http-host-header", "lodge.example"]
