"""Where voice notes are transcribed: `woltspace.json` `transcription`.

Any server with the OpenAI-style `POST <base_url>/audio/transcriptions`
endpoint: OpenAI by default (`gpt-4o-transcribe`), or a local whisper
server. The key comes from the environment variable the settings name,
never from the file. Read on every voice note, so a change applies
without a restart. `transcribe()` makes the call: the configured endpoint
only, no redirects, no ambient proxy for a server on this machine.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-transcribe"
DEFAULT_KEY_ENV = "OPENAI_API_KEY"
APIS = ("transcriptions",)
_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
CALL_TIMEOUT_SECONDS = 60.0
TRANSCRIPT_MAX_CHARS = 4000


class SettingsInvalid(ValueError):
    """`woltspace.json` `transcription` cannot be used as written."""


@dataclass(frozen=True)
class TranscriptionSettings:
    """Where voice notes go: one OpenAI-style transcription endpoint.

    `api` is the request style; only "transcriptions" (multipart upload to
    `<base_url>/audio/transcriptions`) exists today. `base_url` must be
    https, or http to this machine only (a local server), so a key and audio
    never cross a network in clear. `key_env` names the environment variable
    holding the key; empty means the server needs none.

    Trust boundary: this is the owner's configuration. Whoever can write
    `woltspace.json` chooses where audio goes and which of the lodge's
    environment variables is sent as the Bearer key. That is no new power:
    the same OS user can already read the lodge's environment. The CLI
    refuses a new endpoint that would silently keep the old key.
    """

    api: str = "transcriptions"
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    key_env: str = DEFAULT_KEY_ENV

    @property
    def endpoint(self) -> str:
        return self.base_url + "/audio/transcriptions"

    @classmethod
    def parse(cls, section: object) -> "TranscriptionSettings":
        if section is None:
            return cls()
        if not isinstance(section, dict):
            raise SettingsInvalid("transcription must be an object")
        unknown = set(section) - {"api", "base_url", "model", "key_env"}
        if unknown:
            raise SettingsInvalid(f"unknown transcription keys: {', '.join(sorted(unknown))}")
        api = section.get("api", "transcriptions")
        if api not in APIS:
            raise SettingsInvalid(f"api must be one of {', '.join(APIS)}")
        base_url = section.get("base_url", DEFAULT_BASE_URL)
        if not isinstance(base_url, str):
            raise SettingsInvalid("base_url must be a string")
        base_url = base_url.strip().rstrip("/")
        check_base_url(base_url)
        model = section.get("model", DEFAULT_MODEL)
        if not isinstance(model, str) or not _MODEL_RE.match(model):
            raise SettingsInvalid("model must be a model name")
        key_env = section.get("key_env", DEFAULT_KEY_ENV)
        if not isinstance(key_env, str) or (key_env and not _KEY_ENV_RE.match(key_env)):
            raise SettingsInvalid("key_env must be an environment variable name, or empty")
        return cls(api=api, base_url=base_url, model=model, key_env=key_env)

    def as_config(self) -> dict:
        return {"api": self.api, "base_url": self.base_url, "model": self.model, "key_env": self.key_env}


def check_base_url(base_url: str) -> None:
    try:
        parts = urlsplit(base_url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError:
        raise SettingsInvalid("base_url is not a URL") from None
    if parts.scheme not in ("https", "http") or not host or parts.query or parts.fragment:
        raise SettingsInvalid("base_url must be an http(s) URL with no query")
    if parts.username or parts.password:
        raise SettingsInvalid("base_url must not carry credentials; use key_env")
    if parts.scheme == "http" and not _loopback(host):
        raise SettingsInvalid("plain http is only for a server on this machine; use https")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_settings(config_path: Path) -> TranscriptionSettings:
    """The lodge's settings, defaults when unset. Raises SettingsInvalid."""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, ValueError):
        raise SettingsInvalid(f"{config_path.name} does not parse") from None
    if not isinstance(data, dict):
        raise SettingsInvalid(f"{config_path.name} has an unknown shape")
    return TranscriptionSettings.parse(data.get("transcription"))


def save_settings(config_path: Path, settings: TranscriptionSettings | None) -> None:
    """Write (or with None, remove) `transcription`; other keys are kept."""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, ValueError):
        raise SettingsInvalid(f"{config_path.name} does not parse") from None
    if not isinstance(data, dict):
        raise SettingsInvalid(f"{config_path.name} has an unknown shape")
    if settings is None:
        data.pop("transcription", None)
    else:
        data["transcription"] = settings.as_config()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, config_path.stat().st_mode & 0o7777)
        except FileNotFoundError:
            pass
        os.replace(tmp, config_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def config_path(wolts_dir: Path) -> Path:
    return Path(wolts_dir) / "woltspace.json"


class TranscriptionFailed(RuntimeError):
    """The service did not return a transcript (a fixed reason, never its body)."""


def transcribe(audio: Path, settings: TranscriptionSettings, key: str | None) -> str:
    """One call to the configured endpoint; the transcript text."""
    import httpx

    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # A server on this machine is reached directly: an ambient HTTP(S)_PROXY
    # would otherwise carry its clear-text audio and key off the machine.
    local = _loopback(urlsplit(settings.base_url).hostname or "")
    with httpx.Client(follow_redirects=False, timeout=CALL_TIMEOUT_SECONDS, trust_env=not local) as client:
        with open(audio, "rb") as handle:
            response = client.post(
                settings.endpoint, headers=headers,
                files={"file": (Path(audio).name, handle, "application/octet-stream")},
                data={"model": settings.model, "response_format": "json"},
            )
    if response.status_code != 200:
        raise TranscriptionFailed(f"the transcription service answered HTTP {response.status_code}")
    try:
        text = response.json()["text"]
    except (ValueError, KeyError, TypeError):
        raise TranscriptionFailed("the transcription service sent no transcript") from None
    if not isinstance(text, str):
        raise TranscriptionFailed("the transcription service sent no transcript")
    return " ".join(text.split())[:TRANSCRIPT_MAX_CHARS]
