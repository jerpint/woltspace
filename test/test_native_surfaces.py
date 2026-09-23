"""Sites, livereload, and the tunnel under the native supervisor.

These prove the surrounding experience resolves from RuntimeLayout rather than
from whatever ambient paths happen to be set — the thing a native run changes.
"""

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from woltspace.layout import RuntimeLayout  # noqa: E402
from woltspace.supervisor import Supervisor  # noqa: E402


def run_in_clean_process(script: str, env_extra: dict) -> dict:
    """Run a probe with no inherited woltspace paths, and return its JSON."""
    import os

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("WOLTS_", "WOLTSPACE_", "WOLT_"))
    }
    env["PYTHONPATH"] = str(ROOT / "src")
    # These HTTP/livereload probes exercise a private native server, no tunnel.
    env["WOLTSPACE_PUBLIC_TUNNEL"] = "false"
    env["WOLTSPACE_ISOLATION"] = "host"
    env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True, text=True, env=env, cwd=str(Path.home()), timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture
def native_root(tmp_path):
    wolts = tmp_path / "wolts"
    site = wolts / "sitewolt" / "wolt" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<html><body>native site</body></html>")
    return wolts


class TestSitePathsResolveFromTheLayout:
    def test_chat_shell_and_local_assets_are_packaged_surfaces(self, native_root):
        payload = run_in_clean_process(
            """
            import json
            from woltspace.layout import RuntimeLayout
            RuntimeLayout.from_env().apply_environment()
            from starlette.testclient import TestClient
            import server.app as app_module
            with TestClient(app_module.app) as client:
                chat = client.get("/chat")
                css = client.get("/static/chat.css")
                js = client.get("/static/chat.js")
                wasm = client.get("/static/pkg/matrix_sdk_crypto_wasm_bg.wasm")
            print(json.dumps({
                "chat_status": chat.status_code,
                "chat_body": chat.text,
                "css_status": css.status_code,
                "css_body": css.text,
                "js_status": js.status_code,
                "js_body": js.text,
                "wasm_status": wasm.status_code,
                "wasm_type": wasm.headers.get("content-type"),
            }))
            """,
            {"WOLTSPACE_WOLTS_DIR": str(native_root), "WOLTSPACE_DIR": str(ROOT)},
        )
        assert payload["chat_status"] == 200
        assert "Chat with n00b" in payload["chat_body"]
        assert 'href="/tui"' in payload["chat_body"]
        assert 'type="module"' in payload["chat_body"]
        assert payload["css_status"] == 200
        assert "@media(max-width:760px){.shell{display:block;position:relative}" in payload["css_body"]
        assert ".chat{position:absolute;inset:0;width:100%}" in payload["css_body"]
        assert ".details{display:none}" in payload["css_body"]
        assert ".composer-wrap{flex:0 0 auto}" in payload["css_body"]
        assert payload["js_status"] == 200
        assert "MatrixEventEvent.Decrypted" in payload["js_body"]
        assert "getLiveTimeline().getEvents()" in payload["js_body"]
        assert "clearStores" in payload["js_body"]
        assert payload["wasm_status"] == 200
        assert payload["wasm_type"] == "application/wasm"

    def test_site_modules_follow_the_data_root(self, native_root):
        payload = run_in_clean_process(
            """
            import json
            from woltspace.layout import RuntimeLayout
            layout = RuntimeLayout.from_env()
            layout.apply_environment()
            import sites
            from server import config
            print(json.dumps({
                "sites_wolts_dir": str(sites.WOLTS_DIR),
                "config_wolts_dir": str(config.WOLTS_DIR),
                "site_dir": str(sites.site_dir("sitewolt")),
                "layout_wolts_dir": str(layout.wolts_dir),
            }))
            """,
            {"WOLTSPACE_WOLTS_DIR": str(native_root), "WOLTSPACE_DIR": str(ROOT)},
        )
        assert payload["sites_wolts_dir"] == payload["layout_wolts_dir"]
        assert payload["config_wolts_dir"] == payload["layout_wolts_dir"]
        assert payload["site_dir"] == str(native_root / "sitewolt" / "wolt" / "site")

    def test_the_server_serves_that_site_with_livereload_injected(self, native_root):
        payload = run_in_clean_process(
            """
            import json
            from woltspace.layout import RuntimeLayout
            RuntimeLayout.from_env().apply_environment()
            from starlette.testclient import TestClient
            import server.app as app_module
            with TestClient(app_module.app) as client:
                response = client.get("/wolt/sitewolt/site/")
                listing = client.get("/sites").json()
            print(json.dumps({
                "status": response.status_code,
                "body": response.text,
                "sites": listing,
            }))
            """,
            {"WOLTSPACE_WOLTS_DIR": str(native_root), "WOLTSPACE_DIR": str(ROOT)},
        )
        assert payload["status"] == 200
        assert "native site" in payload["body"]
        assert "/wolt/sitewolt/site/livereload" in payload["body"]
        assert {"wolt": "sitewolt", "url": "/wolt/sitewolt/site/"} in payload["sites"]

    def test_livereload_socket_pushes_on_a_real_edit(self, native_root):
        payload = run_in_clean_process(
            """
            import json, threading, time
            from pathlib import Path
            from woltspace.layout import RuntimeLayout
            layout = RuntimeLayout.from_env()
            layout.apply_environment()
            from starlette.testclient import TestClient
            import server.app as app_module
            page = layout.wolts_dir / "sitewolt" / "wolt" / "site" / "index.html"
            disconnected = threading.Event()
            async def observed_app(scope, receive, send):
                try:
                    await app_module.app(scope, receive, send)
                finally:
                    if scope["type"] == "websocket":
                        disconnected.set()
            with TestClient(observed_app) as client:
                with client.websocket_connect(
                    "/wolt/sitewolt/site/livereload"
                ) as socket:
                    def edit():
                        time.sleep(1.0)
                        page.write_text("<html><body>edited</body></html>")
                    editor = threading.Thread(target=edit)
                    editor.start()
                    message = socket.receive_text()
                    editor.join(timeout=5)
                    assert not editor.is_alive()
                    # TestClient's context exit closes the socket and immediately
                    # cancels its ASGI scope. Let the real disconnect finish the
                    # watcher first, as a browser disconnect would.
                    socket.close()
                    assert disconnected.wait(timeout=10), "livereload did not disconnect"
            print(json.dumps({"message": message}))
            """,
            {"WOLTSPACE_WOLTS_DIR": str(native_root), "WOLTSPACE_DIR": str(ROOT)},
        )
        assert payload["message"] == "reload"


class TestOnboardingReadsTheRealHome:
    """Native status may report auth, but it no longer controls first run.

    Nothing on the native start path set WOLTSPACE_CONTAINER_HOME, so the
    The server still reports legacy auth status for diagnostics. The lodge's
    harness prompt is driven by explicit setup state instead, so credentials
    neither skip nor trigger it.
    """

    @pytest.fixture
    def logged_in_home(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {}}')
        return home

    def probe(self, native_root, home):
        return run_in_clean_process(
            """
            import json
            from woltspace.layout import RuntimeLayout
            layout = RuntimeLayout.from_env()
            layout.apply_environment()
            from starlette.testclient import TestClient
            import server.app as app_module
            from server import config, state
            with TestClient(app_module.app) as client:
                status = client.get("/onboard-status").json()
            print(json.dumps({
                "container_home": str(config.CONTAINER_HOME),
                "has_oauth": status["has_oauth"],
                "auth_source": status["auth_source"],
                "onboarding": state._is_onboarding(),
            }))
            """,
            {
                "WOLTS_DIR": str(native_root),
                "WOLTSPACE_DIR": str(ROOT),
                "HOME": str(home),
                # An env token authenticates on its own — blank them out, or
                # this proves nothing about the path that was broken.
                "CLAUDE_CODE_OAUTH_TOKEN": "",
                "ANTHROPIC_API_KEY": "",
            },
        )

    def test_credentials_are_reported_but_an_empty_lodge_still_needs_a_choice(
        self, native_root, logged_in_home
    ):
        payload = self.probe(native_root, logged_in_home)

        assert payload["container_home"] == str(logged_in_home)
        assert payload["has_oauth"] is True
        assert payload["auth_source"] == "credentials-file"
        assert payload["onboarding"] is True

    def test_a_native_colony_without_credentials_still_onboards(
        self, native_root, tmp_path
    ):
        empty = tmp_path / "fresh-home"
        empty.mkdir()

        payload = self.probe(native_root, empty)

        assert payload["container_home"] == str(empty)
        assert payload["has_oauth"] is False
        assert payload["auth_source"] == "none"
        assert payload["onboarding"] is True


RUNTIME_ENV_KEYS = (
    "WOLTSPACE_WOLTS_DIR", "WOLTS_DIR", "WOLTSPACE_WOLT_DIR", "WOLT_DIR",
    "WOLTSPACE_DIR", "WOLTSPACE_ISOLATION",
    "WOLTSPACE_HOST", "WOLTSPACE_INSTANCE_ID", "WOLTSPACE_PUBLIC_TUNNEL",
    "WOLTSPACE_ENTRYPOINT", "PORT",
)


@pytest.fixture
def isolated_runtime_env():
    """`prepare()` writes os.environ directly — keep that inside the test.

    Snapshot and restore by hand: `monkeypatch.delenv(..., raising=False)` on a
    variable that is *absent* records nothing to undo, so a value the test then
    sets would survive and repoint WOLTS_DIR (or WOLTSPACE_ISOLATION) for every
    test that runs afterwards.
    """
    import os

    snapshot = {key: os.environ.get(key) for key in RUNTIME_ENV_KEYS}
    try:
        yield
    finally:
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.mark.usefixtures("isolated_runtime_env")
class TestTunnelPolicy:
    def test_native_default_is_tunnel_off(self, tmp_path, monkeypatch):
        import os

        monkeypatch.delenv("WOLTSPACE_PUBLIC_TUNNEL", raising=False)
        monkeypatch.delenv("WOLTSPACE_ENTRYPOINT", raising=False)
        layout = RuntimeLayout(tmp_path / "wolts", ROOT, isolation="host")
        Supervisor(layout).prepare()
        assert os.environ["WOLTSPACE_PUBLIC_TUNNEL"] == "false"

    def test_native_enabling_is_explicit_and_respected(self, tmp_path, monkeypatch):
        import os

        monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "true")
        monkeypatch.delenv("WOLTSPACE_ENTRYPOINT", raising=False)
        layout = RuntimeLayout(tmp_path / "wolts", ROOT, isolation="host")
        Supervisor(layout).prepare()
        assert os.environ["WOLTSPACE_PUBLIC_TUNNEL"] == "true"

    def test_the_native_entrypoint_also_defaults_to_tunnel_off(self, tmp_path, monkeypatch):
        """`woltspace start` on a Mac published a quick tunnel: prepare() returned
        early for the entrypoint before setting the default, and the server's
        own default is on. The lodge must not be public by accident."""
        import os

        monkeypatch.delenv("WOLTSPACE_PUBLIC_TUNNEL", raising=False)
        monkeypatch.setenv("WOLTSPACE_ENTRYPOINT", "1")
        layout = RuntimeLayout(tmp_path / "wolts", ROOT, isolation="host")
        Supervisor(layout).prepare()
        assert os.environ["WOLTSPACE_PUBLIC_TUNNEL"] == "false"

    def test_the_native_entrypoint_can_still_opt_in(self, tmp_path, monkeypatch):
        import os

        monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "true")
        monkeypatch.setenv("WOLTSPACE_ENTRYPOINT", "1")
        layout = RuntimeLayout(tmp_path / "wolts", ROOT, isolation="host")
        Supervisor(layout).prepare()
        assert os.environ["WOLTSPACE_PUBLIC_TUNNEL"] == "true"

    def test_the_container_entrypoint_keeps_its_tunnel(self, tmp_path, monkeypatch):
        import os

        monkeypatch.delenv("WOLTSPACE_PUBLIC_TUNNEL", raising=False)
        monkeypatch.setenv("WOLTSPACE_ENTRYPOINT", "1")
        layout = RuntimeLayout(tmp_path / "wolts", ROOT, isolation="external")
        layout.wolts_dir.mkdir(parents=True)  # a container always has the mount
        Supervisor(layout).prepare()
        assert "WOLTSPACE_PUBLIC_TUNNEL" not in os.environ

    def test_a_guest_in_the_container_never_publishes(self, tmp_path, monkeypatch):
        """The container exports WOLTSPACE_PUBLIC_TUNNEL=true to everything.

        A stray serve inheriting it would race the real cloudflared and, on
        shutdown, delete the incumbent's tunnel state.
        """
        import os

        monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "true")
        monkeypatch.delenv("WOLTSPACE_ENTRYPOINT", raising=False)
        layout = RuntimeLayout(tmp_path / "wolts", ROOT, isolation="external")
        layout.wolts_dir.mkdir(parents=True)
        Supervisor(layout).prepare()
        assert os.environ["WOLTSPACE_PUBLIC_TUNNEL"] == "false"

    def test_disabled_tunnel_starts_no_process_and_writes_no_state(self, native_root):
        payload = run_in_clean_process(
            """
            import json
            from woltspace.layout import RuntimeLayout
            from woltspace.supervisor import Supervisor
            layout = RuntimeLayout.from_env()
            Supervisor(layout).prepare()
            from server import tunnel
            tunnel.start_tunnel()
            print(json.dumps({
                "url": tunnel.get_tunnel_url(),
                "state_exists": tunnel.TUNNEL_STATE_FILE.exists(),
            }))
            """,
            {"WOLTSPACE_WOLTS_DIR": str(native_root), "WOLTSPACE_DIR": str(ROOT)},
        )
        assert payload["url"] == ""
        assert payload["state_exists"] is False
