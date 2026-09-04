"""Python and npm artifacts declare one exact compatibility pair."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from woltspace import __version__
from woltspace.compatibility import TUI_BINARY, TUI_PACKAGE, TUI_VERSION, tui_spec


def test_python_embeds_exact_scoped_tui_version():
    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "tui" / "package.json").read_text()
    )
    assert manifest["name"] == TUI_PACKAGE == "@woltspace/tui"
    assert manifest["version"] == TUI_VERSION == __version__
    assert TUI_BINARY == "woltspace-tui"
    assert tui_spec() == "@woltspace/tui@0.2.2"
