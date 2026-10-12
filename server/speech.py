"""Speech: text in, an audio file out. The one module that talks to the speech service."""

import json
import os
from pathlib import Path

import httpx

SPEECH_URL = "https://api.openai.com/v1/audio/speech"
# The first byte of audio can take a while for a long text.
SPEECH_TIMEOUT = httpx.Timeout(30.0, read=120.0)
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

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _is_audio(speech_format: str, head: bytes) -> bool:
    """Whether a body starts the way audio in this format does."""
    if speech_format == "opus":
        return head.startswith(b"OggS")
    # An MP3 opens with an ID3 tag, or goes straight to a frame: eleven set bits.
    return head.startswith(b"ID3") or (
        len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0
    )


def _refusal(status: int, body: bytes, api_key: str) -> str:
    """What to say about an answer that is not 200, in the service's words if it has any."""
    if status == 401:
        return (
            "the speech service refused OPENAI_API_KEY (HTTP 401); "
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
) -> int:
    """Write `text`, spoken, to the new file `target`; return its size in bytes.

    A failure leaves no file behind, and a file already at `target` is left alone.
    """
    if speech_format not in SPEECH_FILES:
        raise SpeechError("speech_failed", f"unknown speech format: {speech_format}")
    size, head, created, complete = 0, b"", False, False
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST", SPEECH_URL,
                json={
                    "model": model, "voice": voice,
                    "input": text, "response_format": speech_format,
                },
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=SPEECH_TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    raise SpeechError(
                        "speech_failed",
                        _refusal(response.status_code, await response.aread(), api_key),
                    )
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
                with os.fdopen(descriptor, "wb") as audio:
                    async for chunk in response.aiter_bytes():
                        audio.write(chunk)
                        size += len(chunk)
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
        ) from None
    except OSError as exc:
        # The lodge's own fault, not the service's. The OS text names the
        # lodge's paths, so the error carries the reason alone.
        raise RuntimeError(
            "could not write the spoken audio: "
            f"{exc.strerror.lower() if exc.strerror else type(exc).__name__}"
        ) from None
    finally:
        if created and not complete:
            Path(target).unlink(missing_ok=True)
