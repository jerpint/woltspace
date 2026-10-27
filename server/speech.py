"""Speech: text in, an audio file out. The one module that talks to the speech service.

The service is any OpenAI-style `/audio/speech` endpoint: `woltspace.json` `speech`
(container/lib/speech_settings.py) picks it; OpenAI when unset.
"""

import json
import os
from pathlib import Path

import httpx

SPEECH_URL = "https://api.openai.com/v1/audio/speech"
# A healthy answer starts within seconds; a stalled one is retried rather than
# waited on, and two tries stay well inside a caller's two-minute patience.
SPEECH_TIMEOUT = httpx.Timeout(30.0, read=30.0)
SPEECH_ATTEMPTS = 2
DEFAULT_SPEECH_MODEL = "gpt-4o-mini-tts"
DEFAULT_SPEECH_VOICE = "alloy"
# For each format a sender may declare: the name the audio travels under, and its type.
SPEECH_FILES = {
    "opus": ("voice-note.ogg", "audio/ogg"),
    "mp3": ("voice-note.mp3", "audio/mpeg"),
}
_MAX_DETAIL_CHARS = 200


class SpeechError(RuntimeError):
    """The text could not be turned into audio. `reason` is machine-readable."""

    def __init__(self, reason: str, message: str, *, transient: bool = False):
        super().__init__(message)
        self.reason = reason
        # The service stalled or dropped the line; the same request may well work again.
        self.transient = transient


def write_failure(exc: OSError) -> RuntimeError:
    """The lodge could not keep the audio: its own fault, not the service's.

    The OS text names the lodge's paths, so the error carries the reason alone.
    """
    return RuntimeError(
        "could not write the spoken audio: "
        f"{exc.strerror.lower() if exc.strerror else type(exc).__name__}"
    )


def _is_audio(speech_format: str, head: bytes) -> bool:
    """Whether a body starts the way audio in this format does."""
    if speech_format == "opus":
        return head.startswith(b"OggS")
    # An MP3 opens with an ID3 tag, or goes straight to a frame: eleven set bits.
    return head.startswith(b"ID3") or (
        len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0
    )


def _refusal(status: int, body: bytes, api_key: str, key_env: str = "OPENAI_API_KEY") -> str:
    """What to say about an answer that is not 200, in the service's words if it has any."""
    if status == 401:
        if not key_env:
            return "the speech service asked for a key (HTTP 401); set key_env in woltspace.json speech"
        return (
            f"the speech service refused {key_env} (HTTP 401); "
            "check the key in the lodge's .env"
        )
    try:
        detail = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        detail = None
    if not isinstance(detail, str) or not detail.strip():
        return f"the speech service answered HTTP {status}"
    if api_key:
        # A service may quote the key it was given back in its complaint.
        detail = detail.replace(api_key, "<redacted>")
    detail = " ".join(detail.split())[:_MAX_DETAIL_CHARS]
    return f"the speech service answered HTTP {status}: {detail}"


async def synthesize(
    text: str, speech_format: str, target: Path, *, api_key: str, model: str, voice: str,
    max_bytes: int | None = None, url: str = SPEECH_URL, key_env: str = "OPENAI_API_KEY",
    local: bool = False, instructions: str | None = None,
) -> int:
    """Write `text`, spoken, to the new file `target`; return its size in bytes.

    A failure leaves no file behind, and a file already at `target` is left alone.
    `max_bytes` stops the download as soon as the audio outgrows what the
    sender can carry, instead of measuring it once it is all on disk.
    A stalled or dropped request is tried once more before it fails.
    `url` is the endpoint; an empty `api_key` sends no Authorization header, and
    a `local` server (on this machine) is reached directly, never through a proxy.
    `instructions`, a hint on how to speak, goes in the request only when set.
    """
    for attempt in range(1, SPEECH_ATTEMPTS + 1):
        try:
            return await _synthesize_once(
                text, speech_format, target, api_key=api_key, model=model, voice=voice,
                max_bytes=max_bytes, url=url, key_env=key_env, local=local,
                instructions=instructions,
            )
        except SpeechError as exc:
            if not exc.transient or attempt == SPEECH_ATTEMPTS:
                raise
    raise AssertionError("unreachable")


async def _synthesize_once(
    text: str, speech_format: str, target: Path, *, api_key: str, model: str, voice: str,
    max_bytes: int | None = None, url: str = SPEECH_URL, key_env: str = "OPENAI_API_KEY",
    local: bool = False, instructions: str | None = None,
) -> int:
    if speech_format not in SPEECH_FILES:
        raise SpeechError("speech_failed", f"unknown speech format: {speech_format}")
    size, head, created, complete = 0, b"", False, False
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=not local) as client:
            async with client.stream(
                "POST", url,
                json={
                    "model": model, "voice": voice,
                    "input": text, "response_format": speech_format,
                } | ({"instructions": instructions} if instructions else {}),
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                timeout=SPEECH_TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    raise SpeechError(
                        "speech_failed",
                        _refusal(response.status_code, await response.aread(), api_key, key_env),
                        transient=response.status_code >= 500,
                    )
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
                with os.fdopen(descriptor, "wb") as audio:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if max_bytes is not None and size > max_bytes:
                            raise SpeechError(
                                "speech_too_large",
                                f"the spoken audio is over {max_bytes} bytes; "
                                "send a shorter message",
                            )
                        audio.write(chunk)
                        if len(head) < 4:
                            head = (head + chunk)[:4]
        # A 200 can still be an error page, or audio in another format.
        if not _is_audio(speech_format, head):
            raise SpeechError("speech_failed", "the speech service returned no audio")
        complete = True
        return size
    except httpx.HTTPError as exc:
        raise SpeechError(
            "speech_failed", f"could not reach the speech service: {type(exc).__name__}",
            transient=True,
        ) from None
    except OSError as exc:
        raise write_failure(exc) from None
    finally:
        if created and not complete:
            Path(target).unlink(missing_ok=True)
