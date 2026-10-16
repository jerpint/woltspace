"""Voice notes are transcribed with a model that keeps the whole note."""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from bot import core  # noqa: E402


def test_transcription_uses_the_whole_note_model(tmp_path, monkeypatch):
    seen = {}

    class Transcriptions:
        def create(self, model, file):
            seen["model"] = model
            return types.SimpleNamespace(text="the whole note")

    class Client:
        def __init__(self, api_key):
            self.audio = types.SimpleNamespace(transcriptions=Transcriptions())

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(core, "OpenAI", Client)
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"ogg")

    assert core.transcribe_audio(str(audio)) == "the whole note"
    assert seen["model"] == "gpt-4o-transcribe"
