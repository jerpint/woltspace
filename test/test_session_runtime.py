"""Named-session runtime unit tests plus one harmless host-tmux integration."""

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from runtime_context import RuntimeContext
from session_runtime import RuntimeHandle, SessionRuntime, TmuxSessionRuntime

from conftest import requires_tmux


class FakeRunner:
    """subprocess.run stand-in. outputs feed stdout call-by-call; a mapping
    keyed by argv verb answers whichever call actually arrives, which matters
    once a method makes more than one tmux call."""

    def __init__(self, outputs=None, by_verb=None):
        self.outputs = list(outputs or [])
        self.by_verb = dict(by_verb or {})
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        verb = command[1] if len(command) > 1 else command[0]
        if verb in self.by_verb:
            return SimpleNamespace(stdout=self.by_verb[verb], returncode=0)
        # A process snapshot takes two ps calls with different field sets; a
        # test keys them by the field string to answer each one separately.
        if command[-1] in self.by_verb:
            return SimpleNamespace(stdout=self.by_verb[command[-1]], returncode=0)
        if command[0] == "ps" and "ps" in self.by_verb:
            return SimpleNamespace(stdout=self.by_verb["ps"], returncode=0)
        stdout = self.outputs.pop(0) if self.outputs else ""
        return SimpleNamespace(stdout=stdout, returncode=0)

    def commands(self):
        return [call[0] for call in self.calls]


def context(tmp_path=None, tmux_bin="tmux"):
    return RuntimeContext(tmux_bin=tmux_bin)


class TestRuntimeContext:
    def test_from_env_is_injectable(self, tmp_path):
        ctx = RuntimeContext.from_env({
            "WOLTSPACE_TMUX_BIN": "tmux-test",
            "WOLTSPACE_PS_BIN": "ps-test",
        })
        assert ctx.tmux_bin == "tmux-test"
        assert ctx.ps_bin == "ps-test"
        assert RuntimeContext.from_env({}).tmux_bin == "tmux"
        assert RuntimeContext.from_env({}).ps_bin == "ps"


class TestRuntimeHandle:
    def test_round_trips_registry_shape(self):
        handle = RuntimeHandle("wolt-mossy-log-aabbcc", "wolt-mossy-log-aabbcc", "%42")
        restored = RuntimeHandle.from_record({"name": handle.woltspace_session_id, "runtime": handle.to_record()})
        assert restored == handle

    def test_old_record_falls_back_to_exact_named_session(self):
        handle = RuntimeHandle.from_record({"name": "legacy-session"})
        assert handle.woltspace_session_id == "legacy-session"
        assert handle.tmux_session_name == "legacy-session"
        assert handle.pane_id == ""


class TestTmuxSessionRuntime:
    def test_implements_the_declared_runtime_contract(self, tmp_path):
        runtime = TmuxSessionRuntime(context(tmp_path), runner=FakeRunner())
        assert isinstance(runtime, SessionRuntime)

    def test_process_table_uses_portable_ps_form_from_context(self, tmp_path):
        runner = FakeRunner(["100 1 claude\n", "100 claude --resume abc\n"])
        ctx = RuntimeContext(tmux_bin="tmux-test", ps_bin="ps-test")
        runtime = TmuxSessionRuntime(ctx, runner=runner)

        # Two calls, each with its wide column last: BSD ps truncates comm to
        # 16 characters the moment another column follows it.
        assert runtime._process_table() == ({"1": ["100"]}, {"100": "claude"}, {})
        assert runner.commands() == [
            ["ps-test", "-axo", "pid=,ppid=,comm="],
            ["ps-test", "-axo", "pid=,args="],
        ]

    def test_process_table_names_the_script_an_interpreter_is_running(self, tmp_path):
        runner = FakeRunner([
            "101 100 bash\n102 101 python3\n",
            "101 bash /opt/woltspace/container/bin/run-session.sh mywolt\n"
            "102 python3 -u /opt/woltspace/container/bin/session-reg prepare\n",
        ])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        _, commands, scripts = runtime._process_table()
        assert commands == {"101": "bash", "102": "python3"}
        assert scripts == {"101": "run-session.sh", "102": "session-reg"}

    def test_process_table_survives_a_missing_argv_snapshot(self, tmp_path):
        """A failed second ps degrades to comms alone, never to no table."""
        runner = FakeRunner(by_verb={"pid=,ppid=,comm=": "100 1 claude\n"})

        def explode(command, **kwargs):
            if command[-1] == "pid=,args=":
                raise OSError("no argv for you")
            return runner(command, **kwargs)

        runtime = TmuxSessionRuntime(context(tmp_path), runner=explode)
        assert runtime._process_table() == ({"1": ["100"]}, {"100": "claude"}, {})

    def test_spawn_returns_exact_pane_handle(self, tmp_path):
        runner = FakeRunner(["%17\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        handle = runtime.spawn("named-session", str(tmp_path), "cat")

        assert handle == RuntimeHandle("named-session", "named-session", "%17")
        command = runner.calls[0][0]
        assert command[:6] == ["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}"]
        assert command[-1].endswith(" cat")

    def test_spawn_carries_current_native_paths_across_old_tmux_server(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", "/native/wolts")
        monkeypatch.setenv("WOLTSPACE_DIR", "/installed/woltspace")
        monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
        monkeypatch.setenv("HOME", "/real/home")
        monkeypatch.setenv("PATH", "/native/bin:/usr/bin")
        runner = FakeRunner(["%17\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        runtime.spawn("named-session", str(tmp_path), "run-session")

        launched = runner.calls[0][0][-1]
        assert launched.startswith("env ")
        assert "WOLTS_DIR=/native/wolts" in launched
        assert "WOLTSPACE_DIR=/installed/woltspace" in launched
        assert "WOLTSPACE_ISOLATION=host" in launched
        assert "HOME=/real/home" in launched
        assert "PATH=/native/bin:/usr/bin" in launched
        assert launched.endswith(" run-session")

    def test_spawn_tells_the_session_which_control_plane_owns_it(
        self, tmp_path, monkeypatch
    ):
        """`notify` and `push-view` used to bake in :7777 and reach whoever
        held it. They read WOLTSPACE_API now, so it has to survive the spawn —
        tmux hands panes the *server's* environment, which on a pre-existing
        tmux server may name a different instance entirely."""
        monkeypatch.setenv("WOLTSPACE_API", "http://127.0.0.1:8080")
        monkeypatch.setenv("WOLTSPACE_HOST", "127.0.0.1")
        monkeypatch.setenv("WOLTSPACE_PORT", "8080")
        runner = FakeRunner(["%17\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        runtime.spawn("named-session", str(tmp_path), "run-session")

        launched = runner.calls[0][0][-1]
        assert "WOLTSPACE_API=http://127.0.0.1:8080" in launched
        assert "WOLTSPACE_HOST=127.0.0.1" in launched
        assert "WOLTSPACE_PORT=8080" in launched

    def test_spawn_puts_the_install_bin_first_on_the_session_path(
        self, tmp_path, monkeypatch
    ):
        bin_dir = tmp_path / "container" / "bin"
        bin_dir.mkdir(parents=True)
        monkeypatch.setenv("WOLTSPACE_DIR", str(tmp_path))
        monkeypatch.setenv("PATH", "/usr/bin")
        runner = FakeRunner(["%17\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        runtime.spawn("named-session", str(tmp_path), "run-session")

        launched = runner.calls[0][0][-1]
        assert f"PATH={bin_dir}:/usr/bin" in launched

    def test_spawn_does_not_stack_a_bin_dir_already_on_path(
        self, tmp_path, monkeypatch
    ):
        bin_dir = tmp_path / "container" / "bin"
        bin_dir.mkdir(parents=True)
        monkeypatch.setenv("WOLTSPACE_DIR", str(tmp_path))
        monkeypatch.setenv("PATH", f"/usr/bin:{bin_dir}")
        runner = FakeRunner(["%17\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        runtime.spawn("named-session", str(tmp_path), "run-session")

        launched = runner.calls[0][0][-1]
        assert f"PATH=/usr/bin:{bin_dir}" in launched

    def test_spawn_leaves_path_alone_without_a_resolvable_install_bin(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("WOLTSPACE_DIR", raising=False)
        monkeypatch.setenv("PATH", "/usr/bin")
        runner = FakeRunner(["%17\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        runtime.spawn("named-session", str(tmp_path), "run-session")
        assert "PATH=/usr/bin " in runner.calls[0][0][-1]

        monkeypatch.setenv("WOLTSPACE_DIR", str(tmp_path / "gone"))
        runner = FakeRunner(["%17\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        runtime.spawn("named-session", str(tmp_path), "run-session")
        assert "PATH=/usr/bin " in runner.calls[0][0][-1]

    def test_liveness_is_session_level_across_all_windows(self, tmp_path):
        runner = FakeRunner(["named-session\t%17\t123\t1\nnamed-session\t%18\t456\t0\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        assert runtime.is_alive(RuntimeHandle("named-session", "named-session", "%18")) is True
        command = runner.calls[0][0]
        assert command[1:5] == ["list-panes", "-s", "-t", "=named-session"]

    def test_stale_pane_does_not_make_a_live_session_look_dead(self, tmp_path):
        """The orphan trap: pane-strict liveness leaves a session that the stop
        paths refuse to kill and the vulture refuses to reap."""
        runner = FakeRunner(["named-session\t%17\t123\t1\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)
        stale = RuntimeHandle("named-session", "named-session", "%99")

        assert runtime.is_alive(stale) is True
        # Pane identity is still available where it actually matters.
        assert runtime.handle_is_alive(stale) is False

    def test_spawn_in_session_creates_detached_dedicated_window(self, tmp_path):
        runner = FakeRunner(["%18\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)
        original = RuntimeHandle("named-session", "named-session", "%17")

        replacement = runtime.spawn_in_session(original, str(tmp_path), "cat")

        assert replacement == original.at_pane("%18")
        command = runner.commands()[0]
        assert command[:-1] == [
            "tmux", "new-window", "-d", "-P", "-F", "#{pane_id}",
            "-t", "=named-session", "-c", str(tmp_path),
        ]
        assert command[-1].endswith(" cat")

    def test_liveness_false_only_when_session_has_no_panes(self, tmp_path):
        runtime = TmuxSessionRuntime(context(tmp_path), runner=FakeRunner([""]))
        assert runtime.is_alive(RuntimeHandle("gone", "gone", "%1")) is False

    def test_paste_targets_pane_id_atomically(self, tmp_path):
        runner = FakeRunner()
        sleeps = []
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner, sleeper=sleeps.append)

        runtime.paste(RuntimeHandle("named-session", "named-session", "%17"), "hello\nworld", settle=0.5)

        commands = [call[0] for call in runner.calls]
        assert commands[0] == ["tmux", "send-keys", "-t", "%17", "-X", "cancel"]
        assert commands[1] == ["tmux", "set-buffer", "-b", "paste-named-session", "hello\nworld"]
        assert commands[2] == ["tmux", "paste-buffer", "-r", "-b", "paste-named-session",
                               "-d", "-t", "%17"]
        assert commands[3] == ["tmux", "send-keys", "-t", "%17", "Enter"]
        assert sleeps == [0.5]

    def test_a_long_paste_is_paced_and_arrives_byte_for_byte(self, tmp_path):
        """The claude TUI drops the *leading* 1024-byte blocks of a big paste,
        so a 2200-character message showed up as its last 147 characters. Until
        the harness reads its own input reliably, delivery is paced: small
        pastes, a beat apart, and the text itself untouched."""
        runner = FakeRunner()
        sleeps = []
        runtime = TmuxSessionRuntime(
            context(tmp_path), runner=runner, sleeper=sleeps.append
        )
        # Newlines and a multi-byte codepoint on purpose: chunking may not
        # split on lines, and may not cut a character in half.
        text = ("a" * 499 + "\n" + "🦫 bark\n") * 3

        runtime.paste(RuntimeHandle("s", "s", "%17"), text, settle=0.4)

        commands = runner.commands()
        buffers = [c[-1] for c in commands if c[1] == "set-buffer"]
        pastes = [c for c in commands if c[1] == "paste-buffer"]
        assert "".join(buffers) == text
        assert len(buffers) == 4
        assert all(len(chunk) <= 500 for chunk in buffers)
        assert len(pastes) == len(buffers)
        # One cancel up front, one Enter at the end — pacing added neither.
        assert commands[0] == ["tmux", "send-keys", "-t", "%17", "-X", "cancel"]
        assert [c for c in commands if c[-1] == "Enter"] == [
            ["tmux", "send-keys", "-t", "%17", "Enter"]
        ]
        assert commands[-1][-1] == "Enter"
        # One pause between chunks, never before the first, then the settle.
        assert sleeps == [0.3, 0.3, 0.3, 0.4]

    def test_a_chunk_sized_paste_is_delivered_exactly_as_before(self, tmp_path):
        """Every message the lodge sends today is short. Nothing about those
        may change: one buffer, one paste, no inter-chunk pause."""
        runner = FakeRunner()
        sleeps = []
        runtime = TmuxSessionRuntime(
            context(tmp_path), runner=runner, sleeper=sleeps.append
        )
        text = "x" * 500

        runtime.paste(RuntimeHandle("s", "s", "%17"), text)

        assert runner.commands() == [
            ["tmux", "send-keys", "-t", "%17", "-X", "cancel"],
            ["tmux", "set-buffer", "-b", "paste-s", text],
            ["tmux", "paste-buffer", "-r", "-b", "paste-s", "-d", "-t", "%17"],
            ["tmux", "send-keys", "-t", "%17", "Enter"],
        ]
        assert sleeps == []

    def test_paste_chunks_never_reorder_or_pad_the_text(self, tmp_path):
        """The splitter alone, over the sizes that were measured losing bytes."""
        from session_runtime import _paste_chunks

        for length in (0, 1, 499, 500, 501, 1200, 1700, 3000):
            text = "".join(chr(0x2000 + (i % 64)) for i in range(length))
            chunks = _paste_chunks(text)
            assert "".join(chunks) == text
            assert all(len(chunk) <= 500 for chunk in chunks)
            assert len(chunks) == max(1, -(-length // 500))

    def test_every_paste_keeps_its_linefeeds(self, tmp_path):
        """`-r`, on every paste-buffer, or multi-line messages arrive mangled.

        tmux replaces every LF in a buffer with a separator on the way out, and
        the default separator is CR. Without `-r` an IWCL envelope delivered on
        this rung reaches the agent with `\\r` wherever it was written with
        `\\n` — measured in the delivery-integrity matrix, which read back 2598
        bytes that differed from the 2598 sent at every line break. A CR is
        also exactly the byte a TUI reads as Enter, so the substitution is what
        made a newline at a chunk boundary dangerous in the first place.
        """
        runner = FakeRunner()
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner,
                                     sleeper=lambda _: None)

        runtime.paste(RuntimeHandle("s", "s", "%17"), "one\ntwo\n" * 200)

        pastes = [c for c in runner.commands() if "paste-buffer" in c]
        assert pastes, "nothing was pasted"
        for paste in pastes:
            assert "-r" in paste, (
                f"paste-buffer without -r: {paste!r} — tmux will turn every "
                f"linefeed in this chunk into a carriage return"
            )

    def test_no_chunk_ever_begins_with_a_newline(self, tmp_path):
        """A chunk that opens on a newline is one Enter away from being half a
        message. `paste-buffer` is sent without `-p`, so nothing tells the TUI
        a paste is a paste — it guesses from burst timing, and the 0.3s pause
        between chunks defeats the guess, leaving the first byte after the gap
        to be read as a keystroke. `-r` (see above) means that byte is now an
        LF rather than the CR tmux used to substitute, and claude's composer
        takes an LF as a literal newline — but codex and opencode ride this
        same rung with their own readlines, and the boundary rule costs
        nothing. Every case here puts a newline exactly on, or immediately
        around, a 500-character boundary."""
        from session_runtime import _paste_chunks

        payloads = [
            "a" * 500 + "\n" + "b" * 900,          # newline first byte of #2
            "a" * 499 + "\n" + "b" * 900,          # newline last byte of #1
            "a" * 500 + "\n\n\n" + "b" * 900,      # a run astride the boundary
            "a" * 498 + "\n\n\n\n" + "b" * 900,    # run starting inside #1
            "line\n" * 400,                        # newlines everywhere
            "\n" * 1200,                           # nothing but newlines
            "\n" + "a" * 1200,                     # a leading newline stays put
        ]
        for text in payloads:
            chunks = _paste_chunks(text)
            assert "".join(chunks) == text, "the text is never altered"
            assert all(len(chunk) <= 500 for chunk in chunks)
            assert all(chunk for chunk in chunks), "no empty chunk"
            # The first chunk keeps whatever the message starts with; it is not
            # preceded by a pause. Every *later* one must not open on Enter.
            starts = [chunk[0] for chunk in chunks[1:]]
            if text.strip("\n"):
                assert "\n" not in starts, f"a chunk opens on a newline: {starts!r}"

    def test_a_mid_sequence_tmux_failure_never_reaches_the_submit(self, tmp_path):
        """Pacing cost chunked paste its atomicity: a tmux failure part-way
        leaves what landed sitting in the composer. That is survivable only
        because it is never *submitted* — a truncated message pressed with
        Enter is a question the agent answers wrong, and delivery would have
        reported success."""
        class Flaky(FakeRunner):
            def __call__(self, command, **kwargs):
                if command[1] == "set-buffer" and command[-1].startswith("b"):
                    raise subprocess.CalledProcessError(1, command)
                return super().__call__(command, **kwargs)

        runner = Flaky()
        runtime = TmuxSessionRuntime(
            context(tmp_path), runner=runner, sleeper=lambda _s: None
        )

        with pytest.raises(subprocess.CalledProcessError):
            runtime.paste(RuntimeHandle("s", "s", "%17"), "a" * 500 + "b" * 500)

        assert not [c for c in runner.commands() if c[-1] == "Enter"], \
            "a half-delivered message must not be submitted"

    def test_a_long_message_starting_with_a_slash_is_defused(self, tmp_path):
        """claude dispatches composer content beginning with "/" as a slash
        command however it got there, and a long one is silently destroyed
        (`Unknown command`, no transcript record). A leading space makes it
        prose. Short messages are left alone: `/compact` must stay a command."""
        runner = FakeRunner()
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner,
                                     sleeper=lambda _: None)
        long = "/not-a-command " + "x" * 600
        runtime.paste(RuntimeHandle("s", "s", "%17"), long)
        buffers = [c[-1] for c in runner.commands() if c[1] == "set-buffer"]
        assert "".join(buffers) == " " + long

        runner = FakeRunner()
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner,
                                     sleeper=lambda _: None)
        runtime.paste(RuntimeHandle("s", "s", "%17"), "/compact")
        buffers = [c[-1] for c in runner.commands() if c[1] == "set-buffer"]
        assert buffers == ["/compact"]

    def test_legacy_paste_resolves_the_only_pane(self, tmp_path):
        """A record with no persisted pane — i.e. every session already on
        disk — resolves to a real pane rather than the bare session name."""
        runner = FakeRunner(by_verb={"list-panes": "legacy\t%3\t123\t1\n"})
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        runtime.paste(RuntimeHandle("legacy", "legacy"), "hello")

        pastes = [c for c in runner.commands() if "paste-buffer" in c]
        assert pastes[0][-1] == "%3"

    def test_legacy_paste_falls_back_to_bare_name_never_equals_prefix(self, tmp_path):
        """With nothing to resolve, address the session by bare name.

        tmux honors the '=' exact-match prefix for a target-session but
        rejects it for a target-pane ("can't find pane: =legacy"), so the
        fallback must pass the name unprefixed — the pre-refactor behavior.
        """
        runner = FakeRunner(by_verb={"list-panes": ""})
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        runtime.paste(RuntimeHandle("legacy", "legacy"), "hello")

        commands = runner.commands()
        cancel = [c for c in commands if "cancel" in c][0]
        paste = [c for c in commands if "paste-buffer" in c][0]
        enter = [c for c in commands if c[-1] == "Enter"][0]
        assert cancel == ["tmux", "send-keys", "-t", "legacy", "-X", "cancel"]
        assert paste[-1] == "legacy"
        assert enter == ["tmux", "send-keys", "-t", "legacy", "Enter"]

    def test_legacy_paste_prefers_the_pane_carrying_the_agent(self, tmp_path):
        """Multi-window legacy session: detection sees every window (-s), so
        delivery must land in the SAME pane, not the active one."""
        runner = FakeRunner(by_verb={
            # window 1's pane is active; the agent lives in window 0's pane.
            "list-panes": "legacy\t%1\t100\t0\nlegacy\t%2\t200\t1\n",
            "ps": "100 1 bash\n300 100 claude\n200 1 bash\n",
        })
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        runtime.paste(RuntimeHandle("legacy", "legacy"), "hello",
                      process_names={"claude"})

        pastes = [c for c in runner.commands() if "paste-buffer" in c]
        assert pastes[0][-1] == "%1"

    def test_capture_omits_history_flag_when_start_is_none(self, tmp_path):
        """Visible-pane capture. deliver_boot_prompt waits for a marker to
        CLEAR on repaint; -S would keep a scrolled-off marker forever present
        and strand the boot prompt until timeout."""
        runner = FakeRunner(by_verb={"list-panes": "s\t%4\t1\t1\n",
                                     "capture-pane": "pane text"})
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        assert runtime.capture(RuntimeHandle("s", "s"), start=None) == "pane text"
        captures = [c for c in runner.commands() if "capture-pane" in c]
        assert "-S" not in captures[0]
        assert captures[0] == ["tmux", "capture-pane", "-t", "%4", "-p"]

        runtime.capture(RuntimeHandle("s", "s", "%4"), start="-200")
        captures = [c for c in runner.commands() if "capture-pane" in c]
        assert captures[1][-2:] == ["-S", "-200"]


class TestAgentDetection:
    """has_descendant_process is the vulture's kill decision — cover it."""

    PS = "100 1 bash\n101 100 run-session.sh\n102 101 claude\n200 1 bash\n"

    def _runtime(self, tmp_path, panes, ps=None, args=""):
        return TmuxSessionRuntime(
            context(tmp_path),
            runner=FakeRunner(by_verb={
                "list-panes": panes,
                "ps": ps or self.PS,
                "pid=,args=": args,
            }),
        )

    def test_finds_agent_under_the_wrapper_chain(self, tmp_path):
        runtime = self._runtime(tmp_path, "s\t%1\t100\t1\n")
        assert runtime.has_descendant_process(RuntimeHandle("s", "s"), {"claude"}) is True

    def test_false_when_no_pane_carries_the_agent(self, tmp_path):
        runtime = self._runtime(tmp_path, "s\t%2\t200\t1\n")
        assert runtime.has_descendant_process(RuntimeHandle("s", "s"), {"claude"}) is False

    def test_none_when_session_is_gone(self, tmp_path):
        runtime = self._runtime(tmp_path, "")
        assert runtime.has_descendant_process(RuntimeHandle("s", "s"), {"claude"}) is None

    def test_persisted_pane_scopes_the_search(self, tmp_path):
        runtime = self._runtime(tmp_path, "s\t%1\t100\t1\ns\t%2\t200\t0\n")
        assert runtime.has_descendant_process(RuntimeHandle("s", "s", "%2"), {"claude"}) is False
        assert runtime.has_descendant_process(RuntimeHandle("s", "s", "%1"), {"claude"}) is True

    def test_stale_pane_widens_to_the_session_rather_than_going_blind(self, tmp_path):
        """A vanished pane is not evidence the agent left the session — and
        answering None here is what made stale-pane sessions unreapable."""
        runtime = self._runtime(tmp_path, "s\t%1\t100\t1\n")
        assert runtime.has_descendant_process(RuntimeHandle("s", "s", "%99"), {"claude"}) is True

    def test_detection_and_delivery_agree_on_the_pane(self, tmp_path):
        panes = "s\t%1\t100\t0\ns\t%2\t200\t1\n"
        runtime = self._runtime(tmp_path, panes)
        handle = RuntimeHandle("s", "s")
        found = runtime.resolve_process_handle(handle, {"claude"})
        assert found is not None
        assert found.pane_id == "%1"
        assert runtime.resolve_delivery_pane(handle, {"claude"}).pane_id == "%1"

    def test_explicit_pane_is_trusted_without_a_second_lookup(self, tmp_path):
        runner = FakeRunner()
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)
        handle = RuntimeHandle("s", "s", "%7")
        assert runtime.resolve_delivery_pane(handle).pane_id == "%7"
        assert runner.calls == []

    def test_a_booting_session_is_found_by_its_shim_script_name(self, tmp_path):
        """The real shape of a session that has not reached its agent yet.

        `ps -o comm=` reports the interpreter, never the script: the launching
        `run-session.sh` shows up as plain `bash` on macOS and on GNU ps alike,
        so matching LAUNCHING_NAMES against comm could never fire and a booting
        session read as dead — orphaned in the TUI, `agent-gone` to IWCL.
        """
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
        from harnesses import _wanted_processes

        ps = "100 1 bash\n101 100 bash\n"
        args = ("100 -bash\n"
                "101 bash /opt/woltspace/container/bin/run-session.sh mywolt\n")
        runtime = self._runtime(tmp_path, "s\t%1\t100\t1\n", ps=ps, args=args)
        handle = RuntimeHandle("s", "s")

        assert runtime.has_descendant_process(
            handle, _wanted_processes("claude", True)) is True
        # ...and excluding the shim still means "no agent is ready in there".
        assert runtime.has_descendant_process(
            handle, _wanted_processes("claude", False)) is False

    def test_session_names_come_from_server_wide_pane_snapshot(self, tmp_path):
        runner = FakeRunner(["main\t%1\t10\t1\nnamed-a\t%2\t20\t1\nnamed-a\t%3\t30\t0\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        assert runtime.list_session_names() == {"named-a"}
        assert runner.calls[0][0][1:3] == ["list-panes", "-a"]


def test_start_session_persists_runtime_handle(tmp_path, monkeypatch):
    import paths
    import sessions
    import sites

    monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(sessions, "RUN_SESSION_SCRIPT", Path("/bin/true"))
    monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)

    wolt_dir = tmp_path / "testwolt" / "wolt"
    (wolt_dir / "site").mkdir(parents=True)
    (wolt_dir / "site" / "index.html").write_text("<h1>test</h1>")
    (wolt_dir / "wolt.json").write_text(json.dumps({"name": "testwolt", "type": "raccoon"}))

    monkeypatch.setattr(
        sessions.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="%42\n", returncode=0),
    )

    result = sessions.start_session(wolt="testwolt", prompt="hello")
    stored = sessions.SessionRegistry(tmp_path).get(result["name"], check_alive=False)

    assert stored["runtime"] == {
        "woltspace_session_id": result["name"],
        "tmux_session_name": result["name"],
        "pane_id": "%42",
        "kind": "tmux",
    }


@requires_tmux
def test_host_tmux_named_session_round_trip(tmp_path):
    runtime = TmuxSessionRuntime(context(tmp_path))
    name = f"test-runtime-{uuid.uuid4().hex[:10]}"
    handle = None
    try:
        handle = runtime.spawn(name, str(tmp_path), "cat")
        assert handle.pane_id.startswith("%")
        assert runtime.is_alive(handle)

        marker = f"marker-{uuid.uuid4().hex}"
        runtime.paste(handle, marker)
        deadline = time.time() + 3
        captured = ""
        while time.time() < deadline:
            captured = runtime.capture(handle, start="-20")
            if marker in captured:
                break
            time.sleep(0.05)
        assert marker in captured
    finally:
        if handle is not None:
            runtime.stop(handle)
        else:
            subprocess.run(["tmux", "kill-session", "-t", f"={name}"], capture_output=True)

    assert runtime.is_alive(RuntimeHandle(name, name)) is False


@requires_tmux
def test_host_tmux_legacy_record_round_trip(tmp_path):
    """A session record predating runtime handles must still be reachable.

    Every session in an existing registry has no `runtime` field, so its
    handle carries no pane_id. Real tmux rejects the '=' exact-match prefix
    on a target-pane, so that fallback has to address the bare session name
    or paste/capture break for every session a user already has.
    """
    runtime = TmuxSessionRuntime(context(tmp_path))
    name = f"test-legacy-{uuid.uuid4().hex[:10]}"
    legacy = RuntimeHandle.from_record({"name": name})
    assert legacy.pane_id == ""

    subprocess.run(["tmux", "new-session", "-d", "-s", name, "cat"], check=True)
    try:
        assert runtime.is_alive(legacy)

        marker = f"marker-{uuid.uuid4().hex}"
        runtime.paste(legacy, marker)
        deadline = time.time() + 3
        captured = ""
        while time.time() < deadline:
            captured = runtime.capture(legacy, start="-20")
            if marker in captured:
                break
            time.sleep(0.05)
        assert marker in captured
    finally:
        runtime.stop(legacy)

    assert runtime.is_alive(legacy) is False


class TestStopWithStalePane:
    """Finding #2: a stale pane must not make a session unkillable."""

    def test_stop_session_kills_when_only_the_pane_is_gone(self, tmp_path, monkeypatch, fake_runtime):
        import paths
        import sessions

        monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
        monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)
        (tmp_path / "testwolt" / "wolt").mkdir(parents=True)

        reg = sessions.SessionRegistry(tmp_path)
        reg.create("testwolt-stale-pane-aa11", wolt="testwolt")
        # tmux still has the session, but on a pane this record never sees
        # again — the shape left behind by a pane that died or was recreated.
        reg.update("testwolt-stale-pane-aa11", wolt="testwolt",
                   runtime=RuntimeHandle("testwolt-stale-pane-aa11",
                                         "testwolt-stale-pane-aa11", "%9999").to_record())

        result = sessions.stop_session("testwolt-stale-pane-aa11")

        assert result["was_alive"] is True
        assert fake_runtime.stops == ["testwolt-stale-pane-aa11"]
        assert reg.get("testwolt-stale-pane-aa11", check_alive=False)["status"] == "stopped"

    def test_vulture_can_still_tell_a_stale_pane_session_has_no_agent(self, tmp_path):
        """The other half: has_descendant_process must not answer None here,
        or the reaper treats it as alive-on-uncertainty and never reaps."""
        runner = FakeRunner(by_verb={"list-panes": "s\t%1\t100\t1\n",
                                     "ps": "100 1 bash\n"})
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)
        stale = RuntimeHandle("s", "s", "%9999")
        assert runtime.has_descendant_process(stale, {"claude"}) is False


@requires_tmux
def test_host_tmux_legacy_multi_window_delivers_to_the_agent_pane(tmp_path):
    """Finding #4, end to end on real tmux.

    A legacy record (no persisted pane) whose session has the agent in
    window 0 while window 1 is active. Detection sees window 0 because the
    pane walk uses -s; delivery has to land there too. Addressing the bare
    session name would paste into window 1 — silently, and reported as
    delivered.
    """
    runtime = TmuxSessionRuntime(context(tmp_path))
    name = f"test-multiwin-{uuid.uuid4().hex[:10]}"
    legacy = RuntimeHandle.from_record({"name": name})
    assert legacy.pane_id == ""

    # Window 0 runs `cat` (stands in for the agent); window 1 is created
    # second, so tmux makes it the session's current window.
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "cat"], check=True)
    try:
        subprocess.run(["tmux", "new-window", "-t", f"={name}", "bash"], check=True)
        time.sleep(0.3)

        panes = runtime.panes_for_session(name)
        assert len(panes) == 2, "both windows must be visible to the -s walk"

        agent = runtime.resolve_process_handle(legacy, {"cat"})
        assert agent is not None
        agent_pane = agent.pane_id

        marker = f"marker-{uuid.uuid4().hex}"
        runtime.paste(legacy, marker, process_names={"cat"})

        deadline = time.time() + 3
        landed = ""
        while time.time() < deadline:
            landed = runtime.capture(legacy.at_pane(agent_pane), start="-20")
            if marker in landed:
                break
            time.sleep(0.05)
        assert marker in landed, "prompt did not reach the agent's pane"

        # ...and did NOT land in the active window the human was looking at.
        other = [p.pane_id for p in panes if p.pane_id != agent_pane][0]
        assert marker not in runtime.capture(legacy.at_pane(other), start="-20")
    finally:
        runtime.stop(legacy)
