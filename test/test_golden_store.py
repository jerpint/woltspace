"""The golden transcript store: a repeated result is a pointer to its first copy."""

import json

import pytest

import golden_store


def test_save_then_load_returns_every_scenario(tmp_path):
    path = tmp_path / "g.json"
    data = {"b": {"x": 1}, "a": {"x": 1}, "c": {"x": 2}}
    golden_store.save(path, data)
    assert golden_store.load(path) == data
    raw = json.loads(path.read_text())
    assert raw["a"] == {"x": 1} and raw["b"] == {"same_as": "a"} and raw["c"] == {"x": 2}


def test_saving_what_was_loaded_writes_the_same_file(tmp_path):
    path = tmp_path / "g.json"
    golden_store.save(path, {"a": [1], "b": [1], "c": []})
    before = path.read_text()
    golden_store.save(path, golden_store.load(path))
    assert path.read_text() == before


def test_a_pointer_to_a_missing_scenario_fails_loudly(tmp_path):
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"a": {"same_as": "gone"}}))
    with pytest.raises(KeyError):
        golden_store.load(path)


def test_a_pointer_to_a_pointer_is_not_a_result(tmp_path):
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"a": {"x": 1}, "b": {"same_as": "a"}, "c": {"same_as": "b"}}))
    assert golden_store.load(path)["c"] != {"x": 1}
