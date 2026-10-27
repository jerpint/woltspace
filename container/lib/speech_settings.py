"""Where spoken messages are made: `woltspace.json` `speech`.

Any server with the OpenAI-style `POST <base_url>/audio/speech` endpoint:
OpenAI by default (`gpt-4o-mini-tts`, voice `alloy`), or a speech model on
this machine. Same rules as `transcription` (see transcription_settings): the
key comes from the variable the settings name, never from the file; plain
http only to this machine. Read on every spoken message, so a change applies
without a restart.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from transcription_settings import (
    _KEY_ENV_RE,
    _MODEL_RE,
    DEFAULT_BASE_URL,
    DEFAULT_KEY_ENV,
    SettingsInvalid,
    _loopback,
    check_base_url,
    config_path,
    read_config,
    write_section,
)

DEFAULT_MODEL = "gpt-4o-mini-tts"
DEFAULT_VOICE = "alloy"
INSTRUCTIONS_MAX_CHARS = 500

__all__ = [
    "DEFAULT_MODEL", "DEFAULT_VOICE", "SettingsInvalid", "SpeechSettings",
    "check_base_url", "config_path", "load_settings", "parse_wolt_speech", "read_section", "save_section", "save_settings",
]


@dataclass(frozen=True)
class SpeechSettings:
    """Where spoken messages are made: one OpenAI-style speech endpoint.

    `base_url` must be https, or http to this machine only. `key_env` names
    the variable in the lodge's `.env` holding the key; empty means the server
    needs none. The trust boundary is the one `transcription` describes.
    """

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    key_env: str = DEFAULT_KEY_ENV

    @property
    def endpoint(self) -> str:
        return self.base_url + "/audio/speech"

    @property
    def local(self) -> bool:
        """A server on this machine: reached directly, never through a proxy."""
        from urllib.parse import urlsplit

        return _loopback(urlsplit(self.base_url).hostname or "")

    @classmethod
    def parse(cls, section: object, *, model: str = DEFAULT_MODEL,
              voice: str = DEFAULT_VOICE) -> "SpeechSettings":
        """`model` and `voice` are the defaults for keys the section leaves out."""
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise SettingsInvalid("speech must be an object")
        unknown = set(section) - {"base_url", "model", "voice", "key_env"}
        if unknown:
            raise SettingsInvalid(f"unknown speech keys: {', '.join(sorted(unknown))}")
        base_url = section.get("base_url", DEFAULT_BASE_URL)
        if not isinstance(base_url, str):
            raise SettingsInvalid("base_url must be a string")
        base_url = base_url.strip().rstrip("/")
        check_base_url(base_url)
        model = section.get("model", model)
        if not isinstance(model, str) or not _MODEL_RE.match(model):
            raise SettingsInvalid("model must be a model name")
        voice = section.get("voice", voice)
        if not isinstance(voice, str) or not _MODEL_RE.match(voice):
            raise SettingsInvalid("voice must be a voice name")
        key_env = section.get("key_env", DEFAULT_KEY_ENV)
        if not isinstance(key_env, str) or (key_env and not _KEY_ENV_RE.match(key_env)):
            raise SettingsInvalid("key_env must be an environment variable name, or empty")
        return cls(base_url=base_url, model=model, voice=voice, key_env=key_env)

    def as_config(self) -> dict:
        return {"base_url": self.base_url, "model": self.model, "voice": self.voice,
                "key_env": self.key_env}


def parse_wolt_speech(section: object) -> dict:
    """A wolt's own `speech` in its wolt.json: only the keys it sets. Raises SettingsInvalid.

    `voice` overrides the lodge's; `instructions` is a short hint on how to speak.
    The server, model and key stay the lodge's.
    """
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SettingsInvalid("speech must be an object")
    unknown = set(section) - {"voice", "instructions"}
    if unknown:
        raise SettingsInvalid(f"unknown speech keys: {', '.join(sorted(unknown))}")
    wolt = {}
    if "voice" in section:
        if not isinstance(section["voice"], str) or not _MODEL_RE.match(section["voice"]):
            raise SettingsInvalid("voice must be a voice name")
        wolt["voice"] = section["voice"]
    if "instructions" in section:
        instructions = section["instructions"]
        if not isinstance(instructions, str):
            raise SettingsInvalid("instructions must be text")
        instructions = instructions.strip()
        if not 0 < len(instructions) <= INSTRUCTIONS_MAX_CHARS:
            raise SettingsInvalid(f"instructions must be 1 to {INSTRUCTIONS_MAX_CHARS} characters")
        if any(unicodedata.category(char) == "Cc" for char in instructions):
            raise SettingsInvalid("instructions must not hold control characters")
        wolt["instructions"] = instructions
    return wolt


def load_settings(config_path: Path, *, model: str = DEFAULT_MODEL,
                  voice: str = DEFAULT_VOICE) -> SpeechSettings:
    """The lodge's settings, defaults when unset. Raises SettingsInvalid.

    `model` and `voice` fill keys the file leaves out: the lodge passes the
    older `OPENAI_TTS_MODEL` / `OPENAI_TTS_VOICE` here, so they keep working.
    """
    return SpeechSettings.parse(read_config(config_path).get("speech"), model=model, voice=voice)


def read_section(config_path: Path) -> dict:
    """The `speech` section as saved: only the keys the owner set. Raises SettingsInvalid."""
    section = read_config(config_path).get("speech")
    if section is None:
        return {}
    SpeechSettings.parse(section)
    return dict(section)


def save_section(config_path: Path, section: dict) -> None:
    """Write `speech` with only the keys given, after checking them."""
    SpeechSettings.parse(section)
    write_section(config_path, "speech", section)


def save_settings(config_path: Path, settings: SpeechSettings | None) -> None:
    """Write (or with None, remove) `speech`; other keys are kept."""
    write_section(config_path, "speech", None if settings is None else settings.as_config())
