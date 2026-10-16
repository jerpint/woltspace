"""Where voice notes are transcribed: woltspace.json `transcription`, any
OpenAI-style endpoint. Fake provider and mock transport only."""

import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server import connector_transcribe  # noqa: E402
from server.connector_transcribe import (  # noqa: E402
    SettingsInvalid, TranscriptionSettings, Transcriber, load_settings, openai_transcribe, save_settings,
)
from test_connector_transcribe import Provider, _accepted, _pasted, lodge  # noqa: E402,F401


def test_defaults_are_openai_with_a_whole_note_model():
    settings = TranscriptionSettings.parse(None)
    assert settings.endpoint == "https://api.openai.com/v1/audio/transcriptions"
    assert (settings.model, settings.key_env, settings.api) == ("gpt-4o-transcribe", "OPENAI_API_KEY", "transcriptions")


@pytest.mark.parametrize("base_url", [
    "http://127.0.0.1:8080/v1", "http://localhost:9000/v1/", "http://[::1]:8000/v1", "https://api.groq.com/openai/v1",
])
def test_a_local_or_https_server_is_accepted(base_url):
    settings = TranscriptionSettings.parse({"base_url": base_url, "model": "whisper-large-v3", "key_env": ""})
    assert settings.endpoint == base_url.rstrip("/") + "/audio/transcriptions"


@pytest.mark.parametrize("section", [
    {"base_url": "http://192.168.1.5:8080/v1"},             # clear text off this machine
    {"base_url": "http://transcribe.example/v1"},
    {"base_url": "ftp://127.0.0.1/v1"},
    {"base_url": "https://user:pw@api.example/v1"},           # credentials belong in key_env
    {"base_url": "https://api.example/v1?x=1"},
    {"base_url": "https://api.example:99999/v1"},
    {"model": ""}, {"model": "two words"}, {"model": 5},
    {"key_env": "lower"}, {"key_env": "A B"},
    {"api": "chat"},                                          # not yet
    {"token": "sk-..."},                                      # unknown keys, a key above all
    "not an object",
])
def test_unusable_settings_are_refused(section):
    with pytest.raises(SettingsInvalid):
        TranscriptionSettings.parse(section)


def test_save_keeps_other_keys_and_clear_removes_only_its_own(tmp_path):
    path = tmp_path / "woltspace.json"
    path.write_text(json.dumps({"telegram": {"runtime": "connector"}}))
    save_settings(path, TranscriptionSettings.parse({"base_url": "http://127.0.0.1:8080/v1", "key_env": ""}))
    assert load_settings(path).base_url == "http://127.0.0.1:8080/v1"
    assert json.loads(path.read_text())["telegram"] == {"runtime": "connector"}
    save_settings(path, None)
    assert json.loads(path.read_text()) == {"telegram": {"runtime": "connector"}}
    assert load_settings(path) == TranscriptionSettings()


def test_the_call_goes_to_the_configured_endpoint_and_model(monkeypatch):
    seen = []
    real = httpx.AsyncClient
    monkeypatch.setattr(connector_transcribe.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(
                            lambda r: seen.append(r) or httpx.Response(200, stream=httpx.ByteStream(b'{"text": "local"}'))), **kw))
    local = TranscriptionSettings.parse({"base_url": "http://127.0.0.1:8080/v1", "model": "large-v3", "key_env": ""})
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", None, local) == {"text": "local"}
    assert str(seen[0].url) == "http://127.0.0.1:8080/v1/audio/transcriptions"
    assert b'name="model"\r\n\r\nlarge-v3' in seen[0].content
    assert "authorization" not in seen[0].headers
    openai_transcribe(b"x", "voice.ogg", "audio/ogg", "sk-1", TranscriptionSettings())
    assert seen[1].headers["authorization"] == "Bearer sk-1"
    assert b"gpt-4o-transcribe" in seen[1].content


def _transcriber(lodge, settings):
    lodge.router.transcriber = Transcriber(
        lodge.store, lodge.space, call=lodge.provider, settings=settings, slot_wait=0.2)


def test_the_key_comes_from_the_named_variable(lodge, monkeypatch):
    monkeypatch.setenv("LOCAL_STT_KEY", "k-local")
    _transcriber(lodge, lambda: TranscriptionSettings.parse({"key_env": "LOCAL_STT_KEY"}))
    lodge.router(_accepted(lodge))
    assert lodge.provider.key == "k-local" and lodge.provider.settings.key_env == "LOCAL_STT_KEY"


def test_a_keyless_server_is_called_without_a_key(lodge, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _transcriber(lodge, lambda: TranscriptionSettings.parse({"base_url": "http://127.0.0.1:8080/v1", "key_env": ""}))
    lodge.router(_accepted(lodge))
    assert "[voice note transcript]" in _pasted(lodge) and lodge.provider.key is None


def test_a_missing_named_key_is_no_key(lodge, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _transcriber(lodge, TranscriptionSettings)
    lodge.router(_accepted(lodge))
    assert "not transcribed (no_key)" in _pasted(lodge) and lodge.provider.calls == []


def test_unusable_settings_are_misconfigured_with_no_call(lodge):
    def broken():
        raise SettingsInvalid("nope")

    _transcriber(lodge, broken)
    lodge.router(_accepted(lodge))
    assert "not transcribed (misconfigured)" in _pasted(lodge) and lodge.provider.calls == []
    queue = lodge.store._queue("tg-main")
    assert queue.transcribe_budget.get("requests", 0) == 0 and queue.transcribe_budget.get("bytes", 0) == 0
    assert all(entry.get("attempts", 0) == 0 for entry in queue.transcriptions.values())


@pytest.mark.parametrize("base_url,trusts_env", [
    ("http://127.0.0.1:8080/v1", False), ("http://localhost:8080/v1", False), ("https://api.openai.com/v1", True),
])
def test_a_local_server_is_never_reached_through_an_ambient_proxy(monkeypatch, base_url, trusts_env):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    seen = {}
    real = httpx.AsyncClient

    def client(**kw):
        seen["trust_env"] = kw.get("trust_env", True)
        kw.pop("trust_env", None)
        return real(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=httpx.ByteStream(b'{"text": "ok"}'))), trust_env=False, **kw)

    monkeypatch.setattr(connector_transcribe.httpx, "AsyncClient", client)
    settings = TranscriptionSettings.parse({"base_url": base_url, "key_env": ""})
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", "k", settings) == {"text": "ok"}
    assert seen["trust_env"] is trusts_env


def test_a_loopback_call_with_a_proxy_set_reaches_the_local_server(monkeypatch):
    """End to end: a real local server answers, though HTTP_PROXY points elsewhere."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("content-length", 0)))
            body = b'{"text": "from local"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")      # nothing listens: a proxied call fails
        monkeypatch.delenv("NO_PROXY", raising=False)
        settings = TranscriptionSettings.parse(
            {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "m", "key_env": ""})
        assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", "secret", settings) == {"text": "from local"}
        assert hits == ["/v1/audio/transcriptions"]
    finally:
        server.shutdown()


def test_the_router_reads_woltspace_json_on_every_call(tmp_path):
    from server.connector_router import Router
    from server.connectors import ConnectorStore

    router = Router(ConnectorStore(tmp_path / "connectors"), wolts_dir=tmp_path)
    assert router.transcriber.settings() == TranscriptionSettings()
    (tmp_path / "woltspace.json").write_text(json.dumps({"transcription": {"model": "gpt-4o-mini-transcribe"}}))
    assert router.transcriber.settings().model == "gpt-4o-mini-transcribe"


def test_the_cli_sets_shows_and_clears(tmp_path, monkeypatch, capsys):
    from woltspace import cli

    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    monkeypatch.setattr(cli.RuntimeLayout, "from_env",
                        classmethod(lambda cls, *a, **k: type("L", (), {"wolts_dir": tmp_path, "install_root": ROOT})()))
    assert cli.main(["transcription", "set", "--base-url", "http://127.0.0.1:8080/v1",
                     "--model", "large-v3", "--no-key"]) == 0
    assert "http://127.0.0.1:8080/v1/audio/transcriptions" in capsys.readouterr().out
    assert cli.main(["transcription", "show", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)["transcription"]
    assert shown == {"api": "transcriptions", "base_url": "http://127.0.0.1:8080/v1", "model": "large-v3", "key_env": ""}
    assert cli.main(["transcription", "set", "--base-url", "http://10.0.0.2/v1", "--no-key"]) == 1
    assert "only for a server on this machine" in capsys.readouterr().err
    assert cli.main(["transcription", "set", "--model", "small"]) == 0          # merges
    assert load_settings(tmp_path / "woltspace.json").base_url == "http://127.0.0.1:8080/v1"
    assert cli.main(["transcription", "clear"]) == 0
    assert load_settings(tmp_path / "woltspace.json") == TranscriptionSettings()


def test_the_cli_never_carries_a_key_to_a_new_server(tmp_path, monkeypatch, capsys):
    from woltspace import cli

    monkeypatch.setattr(cli.RuntimeLayout, "from_env",
                        classmethod(lambda cls, *a, **k: type("L", (), {"wolts_dir": tmp_path, "install_root": ROOT})()))
    path = tmp_path / "woltspace.json"
    # From the defaults (OpenAI + OPENAI_API_KEY), a new host must name its key.
    assert cli.main(["transcription", "set", "--base-url", "https://stt.example/v1"]) == 1
    assert "OPENAI_API_KEY is not sent" in capsys.readouterr().err
    assert load_settings(path) == TranscriptionSettings()
    assert cli.main(["transcription", "set", "--base-url", "https://stt.example/v1", "--key-env", "STT_KEY"]) == 0
    assert load_settings(path).key_env == "STT_KEY"
    # Same server, another path or model: the key stays.
    assert cli.main(["transcription", "set", "--base-url", "https://stt.example/v2", "--model", "m2"]) == 0
    assert load_settings(path).key_env == "STT_KEY"
    # Another server again: refused without a key choice.
    assert cli.main(["transcription", "set", "--base-url", "https://other.example/v1"]) == 1
    assert load_settings(path).base_url == "https://stt.example/v2"


@pytest.mark.parametrize("base_url", ["https://example.test:99999/v1", "http://[::1/v1", "not a url"])
def test_the_cli_reports_a_malformed_url_without_a_traceback(tmp_path, monkeypatch, capsys, base_url):
    from woltspace import cli

    monkeypatch.setattr(cli.RuntimeLayout, "from_env",
                        classmethod(lambda cls, *a, **k: type("L", (), {"wolts_dir": tmp_path, "install_root": ROOT})()))
    assert cli.main(["transcription", "set", "--base-url", base_url, "--no-key"]) == 1
    assert capsys.readouterr().err.startswith("woltspace transcription: ")
    assert not (tmp_path / "woltspace.json").exists()
