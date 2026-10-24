"""`woltspace db show`: a read-only print of one lodge.db table."""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import lodge_db  # noqa: E402
from conftest import make_session  # noqa: E402
from sessions import SessionRegistry  # noqa: E402
from woltspace.cli import main as cli_main  # noqa: E402


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    wolts = tmp_path / "wolts"
    for name in ("WOLTSPACE_WOLTS_DIR", "WOLTS_DIR"):
        monkeypatch.setenv(name, str(wolts))
    monkeypatch.setenv("WOLTSPACE_DIR", str(ROOT))
    reg = SessionRegistry(wolts)
    make_session(reg, name="pal-a", wolt="pal", status="resting", created_at=1,
                 title="007", rest={"reason": "build"})
    make_session(reg, name="nunu-b", wolt="nunu", status="running", created_at=2)
    return wolts


def _show(capsys, *args):
    code = cli_main(["db", "show", *args])
    return code, capsys.readouterr()


def test_jsonl_has_every_row_with_its_record_as_an_object(lodge, capsys):
    code, out = _show(capsys, "sessions", "--json")
    rows = [json.loads(line) for line in out.out.splitlines()]
    assert code == 0 and [r["name"] for r in rows] == ["pal-a", "nunu-b"]
    assert rows[0]["record"]["title"] == "007"


@pytest.mark.parametrize("where, names", [
    (["status=running"], ["nunu-b"]),
    (["wolt=pal", "status=resting"], ["pal-a"]),
    (["created_at=2"], ["nunu-b"]),
    (["record.rest.reason=build"], ["pal-a"]),
    (["record.title=nothing"], []),
    (["record.title=007"], ["pal-a"]),
    (["record.created_at=2"], ["nunu-b"]),
])
def test_where_matches_columns_and_record_keys(lodge, capsys, where, names):
    args = [a for w in where for a in ("--where", w)]
    code, out = _show(capsys, "sessions", "--json", *args)
    assert code == 0 and [json.loads(line)["name"] for line in out.out.splitlines()] == names


def test_the_default_is_a_markdown_table(lodge, capsys):
    code, out = _show(capsys, "sessions", "--where", "wolt=pal")
    lines = out.out.splitlines()
    assert code == 0 and lines[0].startswith("| name | wolt | status |")
    assert "record" not in lines[0] and set(lines[1]) <= {"|", "-"}
    assert len(lines) == 3 and lines[2].startswith("| pal-a | pal | resting |")


@pytest.mark.parametrize("args, message", [
    (["nope"], "no table nope"),
    (["sessions", "--where", "colour=red"], "no column colour"),
    (["sessions", "--where", "status"], "col=value"),
    (["sessions", "--where", "record.title;drop=x"], "not a record key"),
])
def test_bad_names_are_refused_and_nothing_is_run(lodge, capsys, args, message):
    code, out = _show(capsys, *args)
    assert code == 2 and message in out.err


def test_it_reads_without_writing_and_says_when_there_is_no_db(lodge, tmp_path, capsys, monkeypatch):
    db = lodge_db.path(lodge)
    os.chmod(db, 0o400)  # a reader that tried to write would fail here
    try:
        assert _show(capsys, "sessions")[0] == 0
    finally:
        os.chmod(db, 0o600)
    empty = tmp_path / "empty"
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(empty))
    monkeypatch.setenv("WOLTS_DIR", str(empty))
    code, out = _show(capsys, "sessions")
    assert code == 1 and "no lodge.db" in out.err and not lodge_db.path(empty).exists()
