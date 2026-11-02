"""The speech client: text in, an audio file out, and a clear word when it cannot."""

import errno
import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))

from server import speech
from server.speech import SPEECH_TIMEOUT, SpeechError, synthesize

KEY = "sk-test-key-0123456789"
OGG = b"OggS" + b"\x00" * 60
MP3 = b"ID3" + b"\x00" * 60
SETTINGS = {"api_key": KEY, "model": "gpt-4o-mini-tts", "voice": "alloy"}


class _Service:
    """The speech endpoint behind a real httpx.AsyncClient on a MockTransport."""

    def __init__(self, monkeypatch, answer=None, error=None):
        self.requests, self.stream_kwargs = [], []
        service = self

        def handler(request):
            service.requests.append(request)
            if error is not None:
                raise error
            if callable(answer):
                return answer()  # a fresh answer for every request, retries included
            if isinstance(answer, list):
                return answer.pop(0) if len(answer) > 1 else answer[0]
            return answer if answer is not None else httpx.Response(200, content=OGG)

        class Client(httpx.AsyncClient):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(handler), **kwargs)

            def stream(self, method, url, **kwargs):
                service.stream_kwargs.append(kwargs)
                return super().stream(method, url, **kwargs)

        monkeypatch.setattr(speech.httpx, "AsyncClient", Client)


class _CutShort(httpx.AsyncByteStream):
    """A body that starts as audio and then loses its connection."""

    async def __aiter__(self):
        yield OGG
        raise httpx.ReadError("connection lost")


class _NoRoom:
    """A file that opens and closes like any other and takes no byte."""

    def __init__(self, file, error):
        self._file, self._error = file, error

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._file.close()

    def write(self, data):
        raise self._error


async def _failure(tmp_path, speech_format="opus") -> SpeechError:
    target = tmp_path / ("c" * 32)
    with pytest.raises(SpeechError) as error:
        await synthesize("hello", speech_format, target, **SETTINGS)
    assert not target.exists()
    assert KEY not in str(error.value)
    return error.value


@pytest.mark.asyncio
async def test_it_asks_for_the_text_in_the_format_and_writes_a_private_file(monkeypatch, tmp_path):
    service = _Service(monkeypatch)
    target = tmp_path / ("c" * 32)

    size = await synthesize("The build is green.", "opus", target, **SETTINGS)

    (request,) = service.requests
    assert request.method == "POST" and str(request.url) == speech.SPEECH_URL
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "model": "gpt-4o-mini-tts", "voice": "alloy",
        "input": "The build is green.", "response_format": "opus",
    }
    assert service.stream_kwargs[0]["timeout"] is SPEECH_TIMEOUT
    assert target.read_bytes() == OGG and size == len(OGG)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("instructions, extra", [(None, {}), ("Speak slowly.", {"instructions": "Speak slowly."})])
@pytest.mark.asyncio
async def test_instructions_are_sent_only_when_set(monkeypatch, tmp_path, instructions, extra):
    service = _Service(monkeypatch)

    await synthesize("hello", "opus", tmp_path / ("c" * 32), **SETTINGS, instructions=instructions)

    assert json.loads(service.requests[0].content) == {
        "model": "gpt-4o-mini-tts", "voice": "alloy", "input": "hello", "response_format": "opus",
    } | extra


@pytest.mark.parametrize("body", [MP3, b"\xff\xfb\x90\x00" + b"\x00" * 60])
@pytest.mark.asyncio
async def test_mp3_is_recognised_by_its_tag_or_its_frame_sync(monkeypatch, tmp_path, body):
    _Service(monkeypatch, httpx.Response(200, content=body))
    target = tmp_path / ("c" * 32)

    assert await synthesize("hello", "mp3", target, **SETTINGS) == len(body)
    assert target.read_bytes() == body


@pytest.mark.asyncio
async def test_a_refused_key_says_which_key_and_never_shows_it(monkeypatch, tmp_path):
    _Service(monkeypatch, httpx.Response(401, json={
        "error": {"message": f"Incorrect API key provided: {KEY}"},
    }))

    error = await _failure(tmp_path)

    assert error.reason == "speech_failed"
    assert str(error) == (
        "the speech service refused OPENAI_API_KEY (HTTP 401); "
        "check the key in the lodge's .env"
    )


@pytest.mark.asyncio
async def test_a_quota_error_carries_the_service_s_own_words(monkeypatch, tmp_path):
    _Service(monkeypatch, httpx.Response(429, json={
        "error": {"message": "You exceeded your current quota, please check your plan."},
    }))

    error = await _failure(tmp_path)

    assert str(error) == (
        "the speech service answered HTTP 429: "
        "You exceeded your current quota, please check your plan."
    )


@pytest.mark.asyncio
async def test_an_error_that_echoes_the_key_is_redacted_and_bounded(monkeypatch, tmp_path):
    _Service(monkeypatch, httpx.Response(400, json={
        "error": {"message": f"bad request with {KEY} " + "x" * 500},
    }))

    error = await _failure(tmp_path)

    assert "<redacted>" in str(error)
    assert len(str(error)) <= len("the speech service answered HTTP 400: ") + 200


@pytest.mark.asyncio
async def test_an_empty_key_leaves_the_service_s_words_as_they_are(monkeypatch, tmp_path):
    _Service(monkeypatch, httpx.Response(429, json={"error": {"message": "slow down"}}))
    target = tmp_path / ("c" * 32)

    with pytest.raises(SpeechError) as error:
        await synthesize("hello", "opus", target, api_key="", model="m", voice="v")

    assert str(error.value) == "the speech service answered HTTP 429: slow down"


@pytest.mark.parametrize("answer", [
    httpx.Response(500, text="<html>bad gateway</html>"),
    httpx.Response(503, json={"unexpected": True}),
    httpx.Response(500, json={"error": {"message": 7}}),
])
@pytest.mark.asyncio
async def test_a_failure_without_words_names_the_status(monkeypatch, tmp_path, answer):
    _Service(monkeypatch, answer)

    error = await _failure(tmp_path)

    assert str(error) == f"the speech service answered HTTP {answer.status_code}"


@pytest.mark.parametrize("body", [b"", b'{"error": "nope"}', b"<html>login</html>", MP3])
@pytest.mark.asyncio
async def test_an_ok_answer_that_is_not_the_audio_asked_for_is_a_failure(monkeypatch, tmp_path, body):
    _Service(monkeypatch, httpx.Response(200, content=body))

    error = await _failure(tmp_path, "opus")

    assert str(error) == "the speech service returned no audio"


@pytest.mark.asyncio
async def test_a_network_failure_names_its_kind(monkeypatch, tmp_path):
    _Service(monkeypatch, error=httpx.ConnectError("no route"))

    error = await _failure(tmp_path)

    assert str(error) == "could not reach the speech service: ConnectError"


@pytest.mark.asyncio
async def test_a_download_cut_short_leaves_no_file(monkeypatch, tmp_path):
    service = _Service(monkeypatch, lambda: httpx.Response(200, stream=_CutShort()))

    error = await _failure(tmp_path)

    assert str(error) == "could not reach the speech service: ReadError"
    assert len(service.requests) == 2  # tried once more before giving up


@pytest.mark.parametrize("build, words", [
    (lambda path: OSError(errno.ENOSPC, "No space left on device", path), "no space left on device"),
    (lambda path: OSError(), "OSError"),
], ids=["with-a-reason", "without-one"])
@pytest.mark.asyncio
async def test_a_write_failure_is_the_lodge_s_own_error_without_the_path(
    monkeypatch, tmp_path, build, words,
):
    _Service(monkeypatch)
    target = tmp_path / ("c" * 32)
    fdopen = os.fdopen
    monkeypatch.setattr(
        speech.os, "fdopen",
        lambda descriptor, *args: _NoRoom(fdopen(descriptor, *args), build(str(target))),
    )

    with pytest.raises(RuntimeError) as error:
        await synthesize("hello", "opus", target, **SETTINGS)

    assert not isinstance(error.value, SpeechError)      # the route answers 500, not 502
    assert str(error.value) == f"could not write the spoken audio: {words}"
    assert not target.exists()


@pytest.mark.asyncio
async def test_a_file_already_at_the_target_is_refused_and_left_alone(monkeypatch, tmp_path):
    _Service(monkeypatch)
    target = tmp_path / ("c" * 32)
    target.write_bytes(b"another entry")

    with pytest.raises(RuntimeError) as error:
        await synthesize("hello", "opus", target, **SETTINGS)

    assert str(error.value) == "could not write the spoken audio: file exists"
    assert target.read_bytes() == b"another entry"


@pytest.mark.asyncio
async def test_an_unknown_format_is_refused_without_a_request(monkeypatch, tmp_path):
    service = _Service(monkeypatch)

    error = await _failure(tmp_path, "wav")

    assert str(error) == "unknown speech format: wav"
    assert service.requests == []


@pytest.mark.asyncio
async def test_a_stalled_request_is_tried_once_more(monkeypatch, tmp_path):
    """The service stalls now and then (a ReadTimeout), and works on the next try."""
    calls = []

    def answer():
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("stalled")
        return httpx.Response(200, content=OGG)

    service = _Service(monkeypatch, answer)
    target = tmp_path / ("c" * 32)

    assert await synthesize("hello", "opus", target, **SETTINGS) == len(OGG)
    assert target.read_bytes() == OGG
    assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_a_server_error_is_retried(monkeypatch, tmp_path):
    service = _Service(monkeypatch, [httpx.Response(503), httpx.Response(200, content=OGG)])
    assert await synthesize("hello", "opus", tmp_path / ("a" * 32), **SETTINGS) == len(OGG)
    assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_a_refusal_is_not_retried(monkeypatch, tmp_path):
    service = _Service(monkeypatch, lambda: httpx.Response(429))
    await _failure(tmp_path)
    assert len(service.requests) == 1


def test_two_read_waits_stay_under_a_two_minute_caller():
    """Only the read waits: connecting and sending have their own limits, so this
    is no bound on the whole call."""
    timeout = speech.SPEECH_TIMEOUT
    assert (timeout.connect, timeout.write, timeout.pool, timeout.read, speech.SPEECH_ATTEMPTS) == (
        30.0, 30.0, 30.0, 50.0, 2)
    assert timeout.read * speech.SPEECH_ATTEMPTS < 120


def test_a_slow_start_gets_more_than_thirty_seconds():
    """A long message on a busy service can take over 30 s before the first byte."""
    assert speech.SPEECH_TIMEOUT.read >= 50


@pytest.mark.asyncio
async def test_a_retry_after_partial_audio_writes_a_clean_file(monkeypatch, tmp_path):
    """The first try writes some audio and drops; the second must not trip over
    the leftover or append to it."""
    answers = [httpx.Response(200, stream=_CutShort()), httpx.Response(200, content=OGG + b"tail")]
    service = _Service(monkeypatch, lambda: answers.pop(0))
    target = tmp_path / ("c" * 32)

    assert await synthesize("hello", "opus", target, **SETTINGS) == len(OGG) + 4
    assert target.read_bytes() == OGG + b"tail"
    assert len(service.requests) == 2
