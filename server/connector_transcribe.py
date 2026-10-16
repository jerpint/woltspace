"""Voice transcription for connector events: owner opt-in, bounded, cached.

A voice or audio file that came with an accepted event is transcribed by
core, never by the connector, and only when the binding's owner turned on
`may_transcribe`: the audio then goes to the lodge's transcription
service. That is any server with the OpenAI-style
`POST <base_url>/audio/transcriptions` endpoint (OpenAI itself by
default, or a local whisper server), set in `woltspace.json` under
`transcription` (see `TranscriptionSettings`) and read on every call. The
key comes from the environment variable the settings name, never from the
file. The transcript is message content for the session, nothing more:
the command parser never sees it.

Bounds are bytes and requests, measured on the stored file, never a
duration: at most `transcribe_max_bytes` per file, and a per-instance
UTC-day budget of bytes and requests, charged when an attempt is claimed
and never refunded (a timed-out call may still have been billed). At most
two calls run lodge-wide at once (flock slots, held until the HTTP client
has closed, cancellation included); policy is re-checked after a slot is
won. Each (event, attachment) has at most `transcribe_max_attempts`
attempts, ever; results are cached and reused by every retry, so a crash
after a call and before its result is saved can call again, within that
cap. Every outcome is saved: the text, or a fixed reason (not_enabled,
no_key, misconfigured, too_large, unsupported_format, unreadable, over_budget,
retries_exhausted, timeout, network, provider_<status>, bad_response,
response_too_large). A skip saved while the grant was off, or with no
key, stays a skip for that attachment even after the owner turns it on.
Provider error bodies are never read or relayed; nothing here logs audio,
transcripts or headers. The call has an end-to-end deadline (connect,
upload, headers, body), after which it is cancelled and its client closed
before the slot is released; the response is read raw with identity
encoding only, so the bound applies to what is buffered.

Only an event still waiting for its receipt is worked on: a cancelled,
delivered or pruned event stops the work (no call, no paste). A claim
replaced by a newer attempt defers its handoff until the replacement's
result is saved; only a saved disposition is ever pasted. The API runs
all of this on worker threads, off the lodge's event loop.

The file is opened through the uploads directory with no-follow
traversal and the bytes sent come from that same descriptor, after a
size check against what was accepted. Like everything in the lodge this
runs as the owner's OS user: a same-user process that can rewrite the
lodge's files is outside what this can contain (the trusted-local-process
limit).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import stat
import time
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .connectors import AcceptedEvent, ConnectorStore

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-transcribe"
DEFAULT_KEY_ENV = "OPENAI_API_KEY"
APIS = ("transcriptions",)
_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
AUDIO_TYPES = {
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".mp3": "audio/mpeg", ".mpga": "audio/mpeg",
    ".m4a": "audio/mp4", ".wav": "audio/wav", ".webm": "audio/webm", ".flac": "audio/flac",
}
TRANSCRIPT_MAX_CHARS = 4000
RESPONSE_MAX_BYTES = 64 * 1024
CALL_DEADLINE_SECONDS = 60.0
SLOTS = 2
SLOT_WAIT_SECONDS = 20.0


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


class TranscriptionPending(RuntimeError):
    """Another attempt holds this attachment, or no slot came free: defer the handoff."""


class TranscriptionStopped(RuntimeError):
    """The event is over (cancelled, delivered, pruned): no call and no paste."""


async def _call(data: bytes, filename: str, content_type: str, key: str | None,
                settings: TranscriptionSettings) -> dict:
    body = bytearray()
    headers = {"Accept-Encoding": "identity"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # A server on this machine is reached directly: an ambient HTTP(S)_PROXY
    # would otherwise carry its clear-text audio and key off the machine.
    local = _loopback(urlsplit(settings.base_url).hostname or "")
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=httpx.Timeout(CALL_DEADLINE_SECONDS, connect=10.0),
        trust_env=not local,
    ) as client:
        async with client.stream(
            "POST", settings.endpoint,
            headers=headers,
            files={"file": (filename, data, content_type)},
            data={"model": settings.model, "response_format": "json"},
        ) as response:
            if response.status_code != 200:
                return {"reason": f"provider_{response.status_code}"}     # body never read
            if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                return {"reason": "bad_response"}
            # Raw bytes, identity encoding only: what is buffered is what arrived.
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > RESPONSE_MAX_BYTES:
                    return {"reason": "response_too_large"}
                body += chunk
    try:
        text = json.loads(bytes(body))["text"]
    except (ValueError, KeyError, TypeError):
        return {"reason": "bad_response"}
    if not isinstance(text, str):
        return {"reason": "bad_response"}
    return {"text": " ".join(text.split())[:TRANSCRIPT_MAX_CHARS]}


def openai_transcribe(data: bytes, filename: str, content_type: str, key: str | None,
                      settings: TranscriptionSettings | None = None) -> dict:
    """One call: `{"text"}` or `{"reason"}`. The configured endpoint only, no redirects, bounded everything.

    `CALL_DEADLINE_SECONDS` is end to end (connect, upload, headers, body):
    past it the call is cancelled and its client closed before this
    returns, so the caller's slot is released only after cleanup. A
    timeout does not prove the provider stopped or did not bill.
    """
    async def bounded() -> dict:
        try:
            return await asyncio.wait_for(
                _call(data, filename, content_type, key, settings or TranscriptionSettings()),
                CALL_DEADLINE_SECONDS,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return {"reason": "timeout"}
        except httpx.HTTPError:
            return {"reason": "network"}

    return asyncio.run(bounded())


def read_anchored(root: Path, parts: tuple[str, ...], name: str, size: int, cap: int) -> bytes | None:
    """The file's bytes through no-follow traversal from `root`, if it is the regular file of `size`."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except OSError:
        os.close(fd)
        return None
    os.close(fd)
    try:
        info = os.fstat(leaf)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size or size > cap:
            return None
        chunks, total = [], 0
        while total <= size:
            chunk = os.read(leaf, min(1 << 16, size + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        return data if len(data) == size else None
    except OSError:
        return None
    finally:
        os.close(leaf)


@contextlib.contextmanager
def lodge_slot(root: Path, *, slots: int = SLOTS, wait: float = SLOT_WAIT_SECONDS):
    """One of `slots` lodge-wide transcription slots (flock, across processes), or TranscriptionPending."""
    directory = root / "transcribe"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        for index in range(slots):
            fd = os.open(directory / f"slot-{index}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            return
        if time.monotonic() >= deadline:
            raise TranscriptionPending("no transcription slot free")
        time.sleep(0.1)


class Transcriber:
    def __init__(
        self, store: ConnectorStore, uploads_root: Path, *,
        call: Callable[..., dict] = openai_transcribe,
        key: Callable[[], str | None] | None = None,
        settings: Callable[[], TranscriptionSettings] = TranscriptionSettings,
        slot_wait: float = SLOT_WAIT_SECONDS,
    ):
        self.store = store
        self.uploads_root = uploads_root
        self.call = call
        self.key = key
        self.settings = settings
        self.slot_wait = slot_wait

    def _settings_and_key(self) -> tuple[TranscriptionSettings | None, str | None, bool]:
        """(settings or None when invalid, the key, whether a call can authenticate)."""
        try:
            settings = self.settings()
        except SettingsInvalid:
            return None, None, False
        if self.key is not None:
            key = self.key()
        else:
            key = (os.environ.get(settings.key_env) or None) if settings.key_env else None
        return settings, key, key is not None or not settings.key_env

    def notes(self, event: AcceptedEvent, files: list[Path]) -> list[str]:
        """One line per audio attachment: its transcript, or why there is none."""
        lines = []
        for attachment, path in zip(event.attachments, files):
            extension = Path(attachment.get("name", "")).suffix.lower()
            if extension not in AUDIO_TYPES:
                continue
            outcome = self._one(event, attachment, Path(path), extension)
            if "text" in outcome:
                lines.append(f"[voice note transcript]: {outcome['text']}")
            else:
                lines.append(f"[voice note: not transcribed ({outcome['reason']})]")
        return lines

    def _one(self, event: AcceptedEvent, attachment: dict, path: Path, extension: str) -> dict:
        key, upload_id, size = event.dedup_key, attachment["upload_id"], attachment.get("size")
        settings, api_key, key_present = self._settings_and_key()
        with lodge_slot(self.store.root, wait=self.slot_wait):
            claimed = self.store.claim_transcription(
                event.instance_id, key, upload_id, size, key_present=key_present,
                configured=settings is not None,
            )
            if claimed.get("stopped"):
                raise TranscriptionStopped("the event is over")
            if "done" in claimed:
                return claimed["done"]
            if claimed.get("busy"):
                raise TranscriptionPending("another attempt is transcribing this file")
            if claimed.get("paused"):
                raise TranscriptionPending("the instance is paused")
            claim = claimed["claim"]
            data = read_anchored(
                self.uploads_root, (event.channel, "uploads"), path.name, size,
                self.store.limits.transcribe_max_bytes,
            )
            if data is None:
                outcome = {"reason": "unreadable"}
            else:
                name = "voice.ogg" if extension in (".ogg", ".oga") else f"audio{extension}"
                try:
                    outcome = self.call(data, name, AUDIO_TYPES[extension], api_key, settings)
                except Exception:  # noqa: BLE001 - a fixed reason, never the provider's words
                    outcome = {"reason": "network"}
            settled = self.store.settle_transcription(event.instance_id, key, upload_id, claim, outcome)
            if settled.get("stopped"):
                raise TranscriptionStopped("the event is over")
            if settled.get("pending"):
                raise TranscriptionPending("a replacement attempt owns this file")
            return settled["done"]
