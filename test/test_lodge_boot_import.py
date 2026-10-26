"""The boot import of old state files into lodge.db runs once, whatever stores it holds."""

import json
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

import lodge_db
from woltspace.adoption import adopt_runtime_sessions
from woltspace.layout import RuntimeLayout, installation_root

# One old file per store an older release wrote; a store this build does not
# import yet keeps its file, so the test needs no edit when one is added.
OLD_FILES = {
    "sessions": ("alpha/.state/sessions/alpha-a.json",
                 {"name": "alpha-a", "wolt": "alpha", "status": "resting", "created_at": 1}),
    "grants": (".space/digs/grants.json",
               {"grants": [{"name": "box-1", "wolt": "alpha", "created_at": 1}]}),
    "wolf": (".space/wolf/alpha/daily.last", "2026-10-25T09:00"),
    "chat": (".space/telegram/chats/1001.json", {"active_wolt": "alpha"}),
    "apps": (".space/apps/beta-app.json", {"name": "beta-app", "pid": 7}),
}


def test_a_second_boot_imports_nothing(tmp_path, capsys):
    wolts = tmp_path / "wolts"
    for relative, content in OLD_FILES.values():
        path = wolts / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    layout = RuntimeLayout(wolts_dir=wolts, install_root=installation_root(),
                           host="127.0.0.1", port=18779, isolation="host")

    def imported():
        with closing(lodge_db.connect(wolts, readonly=True)) as conn:
            return lodge_db.imports(conn)

    assert "import_errors" not in adopt_runtime_sessions(layout)
    first = imported()
    assert "sessions" in first and set(first) <= set(OLD_FILES)
    capsys.readouterr()

    assert "import_errors" not in adopt_runtime_sessions(layout)
    assert imported() == first
    assert "[lodge-db] imported" not in capsys.readouterr().err
    assert (wolts / "alpha" / ".state" / "sessions.imported" / "alpha-a.json").exists()
    for store in set(first) - {"sessions"}:
        path = wolts / OLD_FILES[store][0]
        assert not path.exists() and path.with_name(path.name + ".imported").exists()
