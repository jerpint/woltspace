import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from woltspace.matrix_outbox import enqueue, pending, read_entry, validate_room_id  # noqa: E402


def test_private_atomic_outbox_round_trip(tmp_path):
    outbox = tmp_path / "state" / "matrix" / "outbox"
    path = enqueue(
        outbox,
        room_id="!room:example.test",
        message="hello encrypted room",
        session="n00b-maple-123456",
    )
    assert pending(outbox) == [path]
    assert os.stat(outbox).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert read_entry(path) == {
        "v": 1,
        "room_id": "!room:example.test",
        "message": "hello encrypted room",
        "session": "n00b-maple-123456",
        "created_at": read_entry(path)["created_at"],
    }
    assert not list(outbox.glob("*.partial"))


@pytest.mark.parametrize("value", ["", "room:example.test", "!room", "!../room:host", "!room:\nhost"])
def test_room_id_is_never_used_as_a_path(value):
    with pytest.raises(ValueError):
        validate_room_id(value)


def test_invalid_entry_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"v": 9, "room_id": "!room:host", "message": "x"}))
    with pytest.raises(ValueError, match="unsupported"):
        read_entry(path)
