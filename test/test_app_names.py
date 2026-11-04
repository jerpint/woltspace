"""Sharing and the Mill agree on which app names an address can hold."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import app_sharing  # noqa: E402
from server.content_hosts import parse_app_host  # noqa: E402

GOOD = ["blog", "3d-viewer", "a", "x" * 63, "pixel-art"]
BAD = ["site-v1", "site-", "a_b", "-a", "a-", "x" * 64, "a.b"]


@pytest.mark.parametrize("name", GOOD)
def test_a_name_that_can_be_shared_can_be_opened(name):
    assert parse_app_host(f"{name}.localhost:7777").name == name


@pytest.mark.parametrize("name", BAD)
def test_a_name_that_cant_be_opened_is_never_shared(name, tmp_path):
    assert parse_app_host(f"{name}.localhost:7777") is None
    with pytest.raises(RuntimeError, match="nothing shared"):
        app_sharing.share(tmp_path, name, 7777, by="owner",
                          start=lambda *_a, **_k: pytest.fail("no tunnel for a bad name"))
