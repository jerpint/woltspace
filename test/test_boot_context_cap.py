"""A new session starts even when the wolt's boot memory is too big (#578).

The opening prompt carries identity.md, context.md and learnings.md whole, and
claude, codex and pi take it as one command-line argument. Linux refuses any
single argument over 128 KiB (MAX_ARG_STRLEN), so a wolt whose memory outgrew
that never got a session again: "Argument list too long", and the message that
asked for it was lost. Over BOOT_PROMPT_MAX_BYTES the lodge cuts the boot files
at a line break, tells the wolt where to read them in full, and warns once on
stderr (run-session.sh copies it into session-boot.log). Under it, nothing
changes.

The registry and the wolt live in a temporary folder and the runtime is faked:
no tmux, no engine. The one real exec runs /bin/true in place of the wrapper.
"""

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container" / "lib"))

import harnesses  # noqa: E402
import sessions  # noqa: E402

WOLT = "probe"
HELLO = "hello from the pond"
START_CHAT = "/woltspace-start-chat"


def _text(size: int, line: str = "a memory line " * 5) -> str:
    """`size` bytes of ASCII lines, with no edge whitespace for strip() to take."""
    unit = (line.strip() + "\n").encode()
    body = (unit * (size // len(unit) + 1))[:size - 1]
    return body.decode() + "."


@pytest.fixture(autouse=True)
def wolts_dir(tmp_path, monkeypatch, fake_runtime):
    import paths
    import sites

    monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(sessions, "RUN_SESSION_SCRIPT", Path("/bin/true"))
    monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)
    # Building a codex command asks `codex --help` once; never here.
    monkeypatch.setattr(harnesses, "_codex_no_daemon", False)
    return tmp_path


def _memory(wolts_dir: Path, identity: str, context: str, learnings: str) -> Path:
    memory = wolts_dir / WOLT / "wolt" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    for name, text in (("identity.md", identity), ("context.md", context),
                       ("learnings.md", learnings)):
        (memory / name).write_text(text)
    return memory


def _oversized(wolts_dir: Path, context: str = "") -> tuple[Path, str, str, str]:
    """The shape that failed: a tiny identity, a huge context.md, a mid-size learnings."""
    identity, learnings = _text(60), _text(22_000)
    context = context or _text(200_000)
    return _memory(wolts_dir, identity, context, learnings), identity, context, learnings


def _spawn_command(wolts_dir: Path, harness: str = "claude", prompt: str = HELLO) -> str:
    wolt_dir = wolts_dir / WOLT / "wolt"
    (wolt_dir / "site").mkdir(parents=True, exist_ok=True)
    (wolt_dir / "site" / "index.html").write_text("<h1>probe</h1>")
    (wolt_dir / "wolt.json").write_text(json.dumps(
        {"name": WOLT, "type": "beaver", "harness": harness}))
    result = sessions.start_session(wolt=WOLT, prompt=prompt,
                                    routing={"adapter": "telegram", "chat_id": "1"})
    return sessions.prepare_session_command(result["name"], "spawn", prompt)


def _opening_prompt(wolts_dir: Path, prompt: str = HELLO) -> str:
    """The argument claude receives: the last word of its spawn command."""
    return shlex.split(_spawn_command(wolts_dir, prompt=prompt))[-1]


def test_small_memory_prompt_is_unchanged(wolts_dir, capsys):
    identity, context, learnings = _text(60), _text(9_000), _text(6_000)
    _memory(wolts_dir, identity, context, learnings)

    prompt = _opening_prompt(wolts_dir)

    assert (f"## identity.md\n{identity}\n\n## context.md\n{context}\n\n"
            f"## learnings.md\n{learnings}\n[/Woltspace boot context]") in prompt
    assert "[Cut by the lodge" not in prompt
    assert capsys.readouterr().err == ""


def test_oversized_memory_fits_under_the_limit(wolts_dir):
    memory, identity, context, learnings = _oversized(wolts_dir)

    prompt = _opening_prompt(wolts_dir)

    limit = sessions.BOOT_PROMPT_MAX_BYTES
    assert len(prompt.encode()) <= limit
    # The cut gives up what it must and little more: the rest of the room is used.
    assert len(prompt.encode()) > limit - 1024
    assert f"## identity.md\n{identity}\n\n## context.md\n" in prompt
    assert f"## learnings.md\n{learnings}\n[/Woltspace boot context]" in prompt
    assert prompt.count("[Cut by the lodge") == 1
    assert (f"[Cut by the lodge: context.md is 200000 bytes and the boot limit is "
            f"{limit}.") in prompt
    assert re.search(r"Lines 1-\d+ of it are shown above\. Read "
                     + re.escape(str(memory / "context.md"))
                     + r" from line \d+ on, and trim your boot memory\.\]", prompt)
    assert prompt.count(HELLO) == 1
    assert prompt.count(START_CHAT) == 1


def test_cut_ends_at_a_line_and_keeps_characters_whole(wolts_dir):
    line = "a beaver — builds → the dam 🦫 and keeps building — far → wider 🦫\n"
    context = (line * (200_000 // len(line.encode()) + 1)).strip()
    _oversized(wolts_dir, context=context)

    prompt = _opening_prompt(wolts_dir)

    kept = prompt.split("## context.md\n", 1)[1].split("[Cut by the lodge", 1)[0]
    assert kept and kept.endswith("\n")
    assert context.startswith(kept)
    shown = int(re.search(r"Lines 1-(\d+) of it are shown above", prompt).group(1))
    assert shown == kept.count("\n")
    assert "\ufffd" not in prompt


def test_cut_line_names_the_first_line_not_shown(wolts_dir):
    """Numbered as on disk: strip() takes the two blank lines off the top, and
    the wolt reads on from the line the note names, not from the start again."""
    context = "\n\n" + "".join(f"line {n:06d} of the context\n" for n in range(3, 8_000))
    memory, *_ = _oversized(wolts_dir, context=context)

    prompt = _opening_prompt(wolts_dir)

    on_disk = (memory / "context.md").read_text().splitlines()
    shown, next_line = map(int, re.search(
        r"Lines 1-(\d+) of it are shown above\. Read \S+ from line (\d+) on", prompt).groups())
    assert next_line == shown + 1
    assert f"{on_disk[shown - 1]}\n[Cut by the lodge" in prompt
    assert on_disk[shown] not in prompt


def test_cut_with_no_whole_line_says_read_in_full(wolts_dir):
    context = "x" * 150_000 + "\n" + _text(50_000)
    memory, *_ = _oversized(wolts_dir, context=context)

    prompt = _opening_prompt(wolts_dir)

    limit = sessions.BOOT_PROMPT_MAX_BYTES
    assert len(prompt.encode()) <= limit
    assert (f"## context.md\n[Cut by the lodge: context.md is {len(context.encode())} bytes "
            f"and the boot limit is {limit}. Nothing of it is shown. Read "
            f"{memory / 'context.md'} in full, and trim your boot memory.]") in prompt
    assert "Lines 1-" not in prompt


def test_long_telegram_message_still_fits(wolts_dir):
    _oversized(wolts_dir)
    message = "—" * 4096  # Telegram's longest message, 3 bytes a character

    prompt = _opening_prompt(wolts_dir, prompt=message)

    assert len(prompt.encode()) <= sessions.BOOT_PROMPT_MAX_BYTES
    assert prompt.count(message) == 1
    assert prompt.count(START_CHAT) == 1


def test_cut_is_logged_once(wolts_dir, capsys):
    _oversized(wolts_dir)

    _opening_prompt(wolts_dir)

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    found = re.search(r"boot memory of probe cut from 222060 to (\d+) bytes "
                      rf"\(limit {sessions.BOOT_PROMPT_MAX_BYTES}\); trim wolt/memory/",
                      lines[0])
    assert found and int(found.group(1)) < sessions.BOOT_PROMPT_MAX_BYTES


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="MAX_ARG_STRLEN is Linux's")
@pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
def test_real_exec_with_oversized_memory(wolts_dir, tmp_path, harness):
    """The spawn command runs the way run-session.sh runs it: read back, then eval."""
    _oversized(wolts_dir)
    command = _spawn_command(wolts_dir, harness)
    wrapper = shlex.quote(harnesses.get_harness(harness)["wrapper"])
    assert command.startswith(wrapper + " ")
    script = tmp_path / "cmd.txt"
    script.write_text("/bin/true" + command[len(wrapper):])

    run = subprocess.run(["bash", "-c", 'CMD=$(cat "$1"); eval "$CMD"', "run-session",
                          str(script)], capture_output=True, text=True)

    assert run.returncode == 0, run.stderr
