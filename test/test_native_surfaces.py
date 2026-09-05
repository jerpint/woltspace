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
            {"WOLTS_DIR": str(native_root), "WOLTSPACE_DIR": str(ROOT)},
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
            {"WOLTS_DIR": str(native_root), "WOLTSPACE_DIR": str(ROOT)},
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
            with TestClient(app_module.app) as client:
                with client.websocket_connect(
                    "/wolt/sitewolt/site/livereload"
                ) as socket:
                    def edit():
                        time.sleep(1.0)
                        page.write_text("<html><body>edited</body></html>")
                    threading.Thread(target=edit, daemon=True).start()
                    message = socket.receive_text()
            print(json.dumps({"message": message}))
            """,
            {"WOLTS_DIR": str(native_root), "WOLTSPACE_DIR": str(ROOT)},
        )
        assert payload["message"] == "reload"


RUNTIME_ENV_KEYS = (
    "WOLTS_DIR", "WOLT_DIR", "WOLTSPACE_DIR", "WOLTSPACE_ISOLATION",
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
            {"WOLTS_DIR": str(native_root), "WOLTSPACE_DIR": str(ROOT)},
        )
        assert payload["url"] == ""
        assert payload["state_exists"] is False
