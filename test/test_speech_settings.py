"""Where spoken messages are made: woltspace.json `speech`, any OpenAI-style
/audio/speech endpoint. Mock transport and a local fake server only."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from speech_settings import SettingsInvalid, SpeechSettings, load_settings, save_settings  # noqa: E402

from server import notify, speech  # noqa: E402
from server.speech import SpeechError  # noqa: E402

OGG = b"OggS" + b"\x00" * 60


def test_defaults_are_openai():
    settings = SpeechSettings()
    assert settings.endpoint == "https://api.openai.com/v1/audio/speech"
    assert (settings.model, settings.voice, settings.key_env) == ("gpt-4o-mini-tts", "alloy", "OPENAI_API_KEY")
    assert not settings.local


@pytest.mark.parametrize("base_url", [
    "https://tts.example/v1", "http://127.0.0.1:8000/v1", "http://localhost:8000/v1/", "http://[::1]:8000/v1",
])
def test_a_local_or_https_server_is_accepted(base_url):
    settings = SpeechSettings.parse({"base_url": base_url, "key_env": ""})
    assert settings.endpoint == base_url.rstrip("/") + "/audio/speech"


@pytest.mark.parametrize("section", [
    "openai", {"base_url": "http://10.0.0.5/v1"}, {"base_url": "https://u:p@tts.example/v1"},
    {"base_url": "https://tts.example/v1?x=1"}, {"voice": "two words"}, {"model": ""},
    {"key_env": "lower"}, {"speed": 2},
])
def test_unusable_settings_are_refused(section):
    with pytest.raises(SettingsInvalid):
        SpeechSettings.parse(section)


def test_older_env_names_fill_only_what_the_file_leaves_out(tmp_path):
    path = tmp_path / "woltspace.json"
    assert load_settings(path, model="tts-1", voice="nova").as_config()["voice"] == "nova"
    path.write_text(json.dumps({"speech": {"voice": "alba"}}))
    settings = load_settings(path, model="tts-1", voice="nova")
    assert (settings.model, settings.voice) == ("tts-1", "alba")


def test_save_keeps_other_keys_and_clear_removes_only_its_own(tmp_path):
    path = tmp_path / "woltspace.json"
    path.write_text(json.dumps({"default_wolt": "bob", "transcription": {"model": "m"}}))
    save_settings(path, SpeechSettings.parse({"base_url": "http://127.0.0.1:8000/v1", "key_env": ""}))
    data = json.loads(path.read_text())
    assert data["default_wolt"] == "bob" and data["transcription"] == {"model": "m"}
    assert data["speech"]["base_url"] == "http://127.0.0.1:8000/v1"
    save_settings(path, None)
    assert json.loads(path.read_text()) == {"default_wolt": "bob", "transcription": {"model": "m"}}


def _local_speech_server(answer=OGG, status=200):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            hits.append(SimpleNamespace(path=self.path, json=json.loads(body),
                                        auth=self.headers.get("authorization")))
            self.send_response(status)
            self.send_header("content-type", "audio/ogg")
            self.send_header("content-length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *_a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


@pytest.mark.asyncio
async def test_a_local_server_is_reached_directly_with_no_key(tmp_path, monkeypatch):
    server, hits = _local_speech_server()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")    # nothing listens: a proxied call fails
        monkeypatch.delenv("NO_PROXY", raising=False)
        settings = SpeechSettings.parse(
            {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "pocket", "voice": "alba",
             "key_env": ""})
        target = tmp_path / ("a" * 32)
        size = await speech.synthesize(
            "hello", "opus", target, api_key="", model=settings.model, voice=settings.voice,
            url=settings.endpoint, key_env=settings.key_env, local=settings.local,
        )
        assert size == len(OGG) and target.read_bytes() == OGG
        (hit,) = hits
        assert hit.path == "/v1/audio/speech" and hit.auth is None
        assert hit.json == {"model": "pocket", "voice": "alba", "input": "hello", "response_format": "opus"}
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_a_refused_key_is_named_by_its_variable(tmp_path):
    server, _ = _local_speech_server(answer=b"{}", status=401)
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/audio/speech"
        with pytest.raises(SpeechError) as error:
            await speech.synthesize("hello", "opus", tmp_path / ("b" * 32), api_key="k", model="m", voice="v",
                                    url=url, key_env="TTS_KEY", local=True)
        assert "TTS_KEY" in str(error.value) and "OPENAI_API_KEY" not in str(error.value)
    finally:
        server.shutdown()


@pytest.fixture
def spoken(monkeypatch, tmp_path):
    """notify._spoken with the lodge's settings, the synthesizer recorded."""
    env, calls = {"OPENAI_API_KEY": "sk-test"}, []

    async def synthesize(text, speech_format, target, **where):
        calls.append(where)
        target.write_bytes(OGG)
        return len(OGG)

    monkeypatch.setattr(notify, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    monkeypatch.setattr(notify, "synthesize", synthesize)
    sender = SimpleNamespace(speech_format="opus", max_attachment_bytes=1024 * 1024)

    async def speak():
        return await notify._spoken(notify._Speech("hi", "c" * 32), sender, "telegram")

    return SimpleNamespace(env=env, calls=calls, speak=speak, config=tmp_path / "woltspace.json")


@pytest.mark.asyncio
async def test_notify_uses_openai_until_told_otherwise(spoken):
    await spoken.speak()
    assert spoken.calls[-1] == {
        "api_key": "sk-test", "model": "gpt-4o-mini-tts", "voice": "alloy", "max_bytes": 1024 * 1024,
        "url": "https://api.openai.com/v1/audio/speech", "key_env": "OPENAI_API_KEY", "local": False,
    }


@pytest.mark.asyncio
async def test_notify_reads_the_setting_on_every_message(spoken):
    spoken.config.write_text(json.dumps({"speech": {
        "base_url": "http://127.0.0.1:8000/v1", "model": "pocket", "voice": "alba", "key_env": ""}}))
    spoken.env.clear()                                           # a local server needs no key
    await spoken.speak()
    call = spoken.calls[-1]
    assert (call["url"], call["api_key"], call["voice"], call["local"]) == (
        "http://127.0.0.1:8000/v1/audio/speech", "", "alba", True)


@pytest.mark.asyncio
async def test_notify_refuses_a_missing_key_or_unusable_settings(spoken):
    spoken.config.write_text(json.dumps({"speech": {"key_env": "TTS_KEY"}}))
    with pytest.raises(SpeechError, match="needs TTS_KEY") as error:
        await spoken.speak()
    assert error.value.reason == "speech_not_configured"
    spoken.config.write_text(json.dumps({"speech": {"base_url": "http://10.0.0.5/v1"}}))
    with pytest.raises(SpeechError, match="unusable") as error:
        await spoken.speak()
    assert error.value.reason == "speech_not_configured"
    assert spoken.calls == []


def _cli(monkeypatch, tmp_path):
    from woltspace import cli

    monkeypatch.setattr(cli.RuntimeLayout, "from_env", classmethod(
        lambda cls, *a, **k: type("L", (), {"wolts_dir": tmp_path, "runtime_lib": ROOT / "container" / "lib"})()))
    return cli


def test_the_cli_sets_shows_and_clears(tmp_path, monkeypatch, capsys):
    cli = _cli(monkeypatch, tmp_path)
    assert cli.main(["speech", "set", "--base-url", "http://127.0.0.1:8000/v1",
                     "--model", "pocket", "--voice", "alba", "--no-key"]) == 0
    assert "http://127.0.0.1:8000/v1/audio/speech" in capsys.readouterr().out
    assert cli.main(["speech", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["speech"] == {
        "base_url": "http://127.0.0.1:8000/v1", "model": "pocket", "voice": "alba", "key_env": ""}
    assert cli.main(["speech", "set", "--voice", "marius"]) == 0          # merges
    assert load_settings(tmp_path / "woltspace.json").base_url == "http://127.0.0.1:8000/v1"
    assert cli.main(["speech", "clear"]) == 0
    assert load_settings(tmp_path / "woltspace.json") == SpeechSettings()


def test_the_cli_never_carries_a_key_to_a_new_server(tmp_path, monkeypatch, capsys):
    cli = _cli(monkeypatch, tmp_path)
    path = tmp_path / "woltspace.json"
    assert cli.main(["speech", "set", "--base-url", "https://tts.example/v1"]) == 1
    assert "OPENAI_API_KEY is not sent" in capsys.readouterr().err
    assert load_settings(path) == SpeechSettings()
    assert cli.main(["speech", "set", "--base-url", "https://tts.example/v1", "--key-env", "TTS_KEY"]) == 0
    assert load_settings(path).key_env == "TTS_KEY"


def test_the_cli_saves_only_what_it_is_told(tmp_path, monkeypatch, capsys):
    """A model or voice left out keeps following OPENAI_TTS_* in the lodge's .env."""
    cli = _cli(monkeypatch, tmp_path)
    path = tmp_path / "woltspace.json"
    assert cli.main(["speech", "set", "--model", "tts-1-hd"]) == 0
    assert json.loads(path.read_text())["speech"] == {"model": "tts-1-hd"}
    assert load_settings(path, voice="nova").voice == "nova"
    assert cli.main(["speech", "set", "--base-url", "http://127.0.0.1:8000/v1/", "--no-key"]) == 0
    assert json.loads(path.read_text())["speech"] == {
        "model": "tts-1-hd", "base_url": "http://127.0.0.1:8000/v1", "key_env": ""}
    capsys.readouterr()
    assert cli.main(["speech", "show"]) == 0
    assert "OPENAI_TTS_VOICE" in capsys.readouterr().out
