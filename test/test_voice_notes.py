"""The one list of what a voice note may be, shared by the command and the lodge."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

import voice_notes
from server import notify


@pytest.mark.parametrize("name, content_type", [
    ("note.ogg", "audio/ogg"),
    ("note.oga", "audio/ogg"),
    ("note.opus", "audio/ogg"),
    ("note.mp3", "audio/mpeg"),
    ("note.m4a", "audio/mp4"),
    ("NOTE.OGG", "audio/ogg"),
    ("two.dots.Mp3", "audio/mpeg"),
])
def test_names_that_can_be_a_voice_note(name, content_type):
    assert voice_notes.voice_content_type(name) == content_type


@pytest.mark.parametrize("name", ["note.wav", "note.flac", "note.html", "note", "ogg", ".ogg.txt"])
def test_names_that_cannot_be_a_voice_note(name):
    assert voice_notes.voice_content_type(name) is None


def test_the_usage_text_names_every_suffix():
    for suffix in voice_notes.VOICE_CONTENT_TYPES:
        assert suffix in voice_notes.VOICE_SUFFIXES_TEXT


def test_speech_limit_is_the_speech_service_limit():
    assert voice_notes.SPEECH_MAX_CHARS == 4096


def test_registered_senders_declare_their_speech_format():
    assert notify.SENDERS["telegram"].speech_format == "opus"
    assert notify.SENDERS["slack"].speech_format == "mp3"
