"""Where voice notes are transcribed: woltspace.json `transcription`, any
OpenAI-style endpoint. Mock transport and a local fake server only."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import transcription_settings as stt  # noqa: E402
from transcription_settings import SettingsInvalid, TranscriptionSettings, load_settings, save_settings  # noqa: E402


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
    {"base_url": "http://192.168.1.5:8080/v1"}, {"base_url": "http://transcribe.example/v1"},
    {"base_url": "ftp://127.0.0.1/v1"}, {"base_url": "https://user:pw@api.example/v1"},
    {"base_url": "https://api.example/v1?x=1"}, {"base_url": "https://api.example:99999/v1"},
    {"base_url": "http://[::1/v1"},
    {"model": ""}, {"model": "two words"}, {"model": 5},
    {"key_env": "lower"}, {"key_env": "A B"}, {"api": "chat"}, {"token": "sk-..."}, "not an object",
])
def test_unusable_settings_are_refused(section):
    with pytest.raises(SettingsInvalid):
        TranscriptionSettings.parse(section)


def test_save_keeps_other_keys_and_clear_removes_only_its_own(tmp_path):
    path = tmp_path / "woltspace.json"
    path.write_text(json.dumps({"active_wolt": "softie"}))
    save_settings(path, TranscriptionSettings.parse({"base_url": "http://127.0.0.1:8080/v1", "key_env": ""}))
    assert load_settings(path).base_url == "http://127.0.0.1:8080/v1"
    save_settings(path, None)
    assert json.loads(path.read_text()) == {"active_wolt": "softie"}
    assert load_settings(path) == TranscriptionSettings()


def _mock(monkeypatch, response):
    seen = {}
    real = httpx.Client

    def client(**kw):
        seen["trust_env"] = kw.pop("trust_env", True)
        make = response if callable(response) else (lambda: response)
        return real(transport=httpx.MockTransport(lambda r: seen.setdefault("requests", []).append(r) or make()),
                    trust_env=False, **kw)

    monkeypatch.setattr(httpx, "Client", client)
    return seen


def test_the_call_goes_to_the_configured_endpoint_model_and_key(tmp_path, monkeypatch):
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"ogg")
    seen = _mock(monkeypatch, lambda: httpx.Response(200, stream=httpx.ByteStream(b'{"text": " the  whole note "}')))
    local = TranscriptionSettings.parse({"base_url": "http://127.0.0.1:8080/v1", "model": "large-v3", "key_env": ""})
    assert stt.transcribe(audio, local, None) == "the whole note"
    request = seen["requests"][0]
    assert str(request.url) == "http://127.0.0.1:8080/v1/audio/transcriptions"
    assert b'name="model"\r\n\r\nlarge-v3' in request.content and "authorization" not in request.headers
    assert seen["trust_env"] is False
    stt.transcribe(audio, TranscriptionSettings(), "sk-1")
    assert seen["requests"][1].headers["authorization"] == "Bearer sk-1" and seen["trust_env"] is True


@pytest.mark.parametrize("response", [
    httpx.Response(401, text="bad key sk-secret"), httpx.Response(302, headers={"location": "https://evil.example"}),
    httpx.Response(200, stream=httpx.ByteStream(b"not json")),
    httpx.Response(200, stream=httpx.ByteStream(b'{"text": 5}')),
    httpx.Response(200, stream=httpx.ByteStream(b'{"text": "' + b"x" * 70000 + b'"}')),
    httpx.Response(500, stream=httpx.ByteStream(b"sk-secret " * 100000)),
    httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"\x1f\x8b")),
])
def test_a_failed_call_is_a_fixed_error_never_the_body(tmp_path, monkeypatch, response):
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"ogg")
    _mock(monkeypatch, response)
    with pytest.raises(stt.TranscriptionFailed) as failed:
        stt.transcribe(audio, TranscriptionSettings(), "sk-1")
    assert "sk-secret" not in str(failed.value) and "evil" not in str(failed.value)


@pytest.mark.parametrize("error,reason", [
    (httpx.ConnectError("refused 10.0.0.1"), "could not be reached"),
    (httpx.ReadTimeout("slow"), "timed out"),
])
def test_transport_failures_are_fixed_reasons(tmp_path, monkeypatch, error, reason):
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"ogg")
    real = httpx.Client

    def raising(request):
        raise error

    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(raising),
                                                           **{k: v for k, v in kw.items() if k != "trust_env"}))
    with pytest.raises(stt.TranscriptionFailed) as failed:
        stt.transcribe(audio, TranscriptionSettings(), "sk-1")
    assert reason in str(failed.value) and "10.0.0.1" not in str(failed.value)
    assert failed.value.__cause__ is None


def test_an_unreachable_local_server_is_a_fixed_reason(tmp_path):
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"ogg")
    settings = TranscriptionSettings.parse({"base_url": "http://127.0.0.1:9/v1", "key_env": ""})
    with pytest.raises(stt.TranscriptionFailed, match="could not be reached"):
        stt.transcribe(audio, settings, None)


def test_a_drip_feed_is_cut_at_the_total_deadline(tmp_path, monkeypatch):
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"ogg")
    monkeypatch.setattr(stt, "CALL_TIMEOUT_SECONDS", 0.2)
    import time

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(50):
                time.sleep(0.02)
                yield b" "

    _mock(monkeypatch, httpx.Response(200, stream=Drip()))
    with pytest.raises(stt.TranscriptionFailed, match="timed out"):
        stt.transcribe(audio, TranscriptionSettings(), "sk-1")


def test_a_loopback_call_with_a_proxy_set_reaches_the_local_server(tmp_path, monkeypatch):
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
        audio = tmp_path / "note.ogg"
        audio.write_bytes(b"ogg")
        settings = TranscriptionSettings.parse(
            {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "m", "key_env": ""})
        assert stt.transcribe(audio, settings, "secret") == "from local"
        assert hits == ["/v1/audio/transcriptions"]
    finally:
        server.shutdown()


def test_the_adapter_reads_the_lodge_setting_on_every_note(tmp_path, monkeypatch):
    from bot import core

    monkeypatch.setattr(core, "WOLTS_DIR", tmp_path)
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"ogg")
    calls = []
    monkeypatch.setattr(stt, "transcribe", lambda path, settings, key: calls.append((settings, key)) or "ok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-1")
    assert core.transcribe_audio(str(audio)) == "ok"
    assert calls[-1] == (TranscriptionSettings(), "sk-1")
    (tmp_path / "woltspace.json").write_text(json.dumps(
        {"transcription": {"base_url": "http://127.0.0.1:8080/v1", "model": "m", "key_env": ""}}))
    assert core.transcribe_audio(str(audio)) == "ok"
    assert calls[-1][0].base_url == "http://127.0.0.1:8080/v1" and calls[-1][1] is None
    (tmp_path / "woltspace.json").write_text(json.dumps({"transcription": {"key_env": "STT_KEY"}}))
    monkeypatch.delenv("STT_KEY", raising=False)
    assert core.transcribe_audio(str(audio)) == "[voice message — no STT_KEY set for transcription]"
    (tmp_path / "woltspace.json").write_text(json.dumps({"transcription": {"base_url": "http://10.0.0.1/v1"}}))
    assert core.transcribe_audio(str(audio)).startswith("[voice message — transcription settings unusable:")
    assert len(calls) == 2


def _cli(monkeypatch, tmp_path):
    from woltspace import cli

    monkeypatch.setattr(cli.RuntimeLayout, "from_env", classmethod(
        lambda cls, *a, **k: type("L", (), {"wolts_dir": tmp_path, "runtime_lib": ROOT / "container" / "lib"})()))
    return cli


def test_the_cli_sets_shows_and_clears(tmp_path, monkeypatch, capsys):
    cli = _cli(monkeypatch, tmp_path)
    assert cli.main(["transcription", "set", "--base-url", "http://127.0.0.1:8080/v1",
                     "--model", "large-v3", "--no-key"]) == 0
    assert "http://127.0.0.1:8080/v1/audio/transcriptions" in capsys.readouterr().out
    assert cli.main(["transcription", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["transcription"] == {
        "api": "transcriptions", "base_url": "http://127.0.0.1:8080/v1", "model": "large-v3", "key_env": ""}
    assert cli.main(["transcription", "set", "--model", "small"]) == 0          # merges
    assert load_settings(tmp_path / "woltspace.json").base_url == "http://127.0.0.1:8080/v1"
    assert cli.main(["transcription", "clear"]) == 0
    assert load_settings(tmp_path / "woltspace.json") == TranscriptionSettings()


def test_the_cli_never_carries_a_key_to_a_new_server(tmp_path, monkeypatch, capsys):
    cli = _cli(monkeypatch, tmp_path)
    path = tmp_path / "woltspace.json"
    assert cli.main(["transcription", "set", "--base-url", "https://stt.example/v1"]) == 1
    assert "OPENAI_API_KEY is not sent" in capsys.readouterr().err
    assert load_settings(path) == TranscriptionSettings()
    assert cli.main(["transcription", "set", "--base-url", "https://stt.example/v1", "--key-env", "STT_KEY"]) == 0
    assert cli.main(["transcription", "set", "--base-url", "https://stt.example/v2", "--model", "m2"]) == 0
    assert load_settings(path).key_env == "STT_KEY"
    assert cli.main(["transcription", "set", "--base-url", "https://other.example/v1"]) == 1
    assert load_settings(path).base_url == "https://stt.example/v2"


@pytest.mark.parametrize("base_url", ["https://example.test:99999/v1", "http://[::1/v1", "not a url"])
def test_the_cli_reports_a_malformed_url_without_a_traceback(tmp_path, monkeypatch, capsys, base_url):
    cli = _cli(monkeypatch, tmp_path)
    assert cli.main(["transcription", "set", "--base-url", base_url, "--no-key"]) == 1
    assert capsys.readouterr().err.startswith("woltspace transcription: ")
    assert not (tmp_path / "woltspace.json").exists()
