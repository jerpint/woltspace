"""The browser PTY bridge works with only Python and tmux."""

import asyncio
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.pty_bridge import PtyBridgeError, attach_tmux
from server.pty_bridge import PtyTextDecoder
from woltspace.channels import CONNECTORS
from woltspace.doctor import run_doctor
from woltspace.layout import RuntimeLayout


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux unavailable")


def _layout(tmp_path):
    return RuntimeLayout(tmp_path / "wolts", ROOT, "127.0.0.1", 7799, "host")


def test_browser_bridge_is_not_a_supervised_node_connector():
    assert [connector.name for connector in CONNECTORS] == ["telegram", "slack", "wolf"]


def test_doctor_reports_the_embedded_browser_terminal(tmp_path):
    checks = {check.name: check for check in run_doctor(_layout(tmp_path), check_port=False)}
    assert checks["browser-terminal"].status == "pass"
    assert checks["browser-terminal"].detail == "embedded Python PTY bridge"


def test_incremental_decoder_preserves_split_unicode():
    decoder = PtyTextDecoder()
    encoded = "box ─ raccoon 🦝".encode()
    assert decoder.feed(encoded[:7]) + decoder.feed(encoded[7:16]) + decoder.feed(encoded[16:]) == (
        "box ─ raccoon 🦝"
    )


@pytest.mark.asyncio
async def test_rejects_invalid_session_name_without_shell_interpretation(tmp_path):
    with pytest.raises(PtyBridgeError, match="invalid session name"):
        await attach_tmux("name; touch nope", tmp_path)
    assert not (tmp_path / "nope").exists()


@pytest.mark.asyncio
async def test_does_not_invent_a_missing_named_session(tmp_path):
    name = f"missing-{uuid.uuid4().hex[:12]}"
    with pytest.raises(PtyBridgeError, match="does not exist"):
        await attach_tmux(name, tmp_path)


@pytest.mark.asyncio
async def test_attach_resize_disconnect_preserves_tmux_session(tmp_path, monkeypatch):
    name = f"bridge-{uuid.uuid4().hex[:12]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(tmp_path)],
        check=True,
    )
    attachment = None
    try:
        monkeypatch.setenv("TMUX", "/a/foreign/socket,123,0")
        attachment = await attach_tmux(name, tmp_path)
        monkeypatch.delenv("TMUX")
        await attachment.write("printf 'PYTHON_BRIDGE_READY\\n'\n")

        output = b""
        async with asyncio.timeout(5):
            while b"PYTHON_BRIDGE_READY" not in output:
                output += await attachment.read()
        assert b"PYTHON_BRIDGE_READY" in output
        attachment.resize(93, 31)

        async with asyncio.timeout(5):
            while True:
                size = subprocess.run(
                    ["tmux", "list-clients", "-t", name, "-F", "#{client_width}x#{client_height}"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if size == "93x31":
                    break
                await asyncio.sleep(0.05)

        attachment.resize(2, 1)
        await asyncio.sleep(0.05)
        size = subprocess.run(
            ["tmux", "list-clients", "-t", name, "-F", "#{client_width}x#{client_height}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert size == "93x31"

        await attachment.close()
        attachment = None
        assert subprocess.run(
            ["tmux", "has-session", "-t", name], capture_output=True
        ).returncode == 0
    finally:
        if attachment is not None:
            await attachment.close()
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


@pytest.mark.asyncio
async def test_two_browser_attachments_do_not_cross_sessions(tmp_path):
    names = [f"bridge-{uuid.uuid4().hex[:12]}" for _ in range(2)]
    attachments = []
    try:
        for name in names:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", name, "-c", str(tmp_path)],
                check=True,
            )
            attachments.append(await attach_tmux(name, tmp_path))

        await asyncio.gather(*(
            attachment.write(f"printf '{name}\\n'\n")
            for attachment, name in zip(attachments, names, strict=True)
        ))
        outputs = [b"", b""]
        async with asyncio.timeout(5):
            for index, (attachment, name) in enumerate(
                zip(attachments, names, strict=True)
            ):
                marker = name.encode()
                while marker not in outputs[index]:
                    outputs[index] += await attachment.read()

        assert names[0].encode() in outputs[0]
        assert names[1].encode() not in outputs[0]
        assert names[1].encode() in outputs[1]
        assert names[0].encode() not in outputs[1]
    finally:
        await asyncio.gather(*(attachment.close() for attachment in attachments))
        for name in names:
            subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
