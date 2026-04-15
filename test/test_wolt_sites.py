"""Tests for wolt site management (container/lib/sites.py)."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add container/lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

import sites


@pytest.fixture(autouse=True)
def tmp_wolts(tmp_path, monkeypatch):
    """Set up a temporary wolts directory structure."""
    import paths
    monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)

    # Create a wolt with a site dir
    wolt_dir = tmp_path / "testwolt" / "wolt" / "site"
    wolt_dir.mkdir(parents=True)
    (wolt_dir / "index.html").write_text("<h1>test</h1>")

    return tmp_path


class TestSiteDir:
    def test_site_dir_path(self, tmp_wolts):
        assert sites.site_dir("testwolt") == tmp_wolts / "testwolt" / "wolt" / "site"

    def test_site_dir_nonexistent_wolt(self, tmp_wolts):
        path = sites.site_dir("nonexistent")
        assert not path.exists()


class TestSiteState:
    def test_no_running_sites(self, tmp_wolts):
        assert sites.running_sites() == []

    def test_get_site_state_not_running(self, tmp_wolts):
        assert sites.get_site_state("testwolt") is None

    def test_stale_state_cleaned(self, tmp_wolts):
        """State file with dead PID should be cleaned up."""
        state_file = tmp_wolts / "testwolt" / ".state" / "site.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({
            "wolt": "testwolt", "port": 6001, "pid": 999999, "dir": "/fake",
        }))
        # PID 999999 is almost certainly dead
        assert sites.get_site_state("testwolt") is None
        # State file should be cleaned up
        assert not state_file.exists()


class TestPortAllocation:
    def test_first_port(self, tmp_wolts):
        port = sites._allocate_port()
        assert port == 6001

    def test_skips_assigned_ports(self, tmp_wolts):
        """Port allocator checks wolt.json for permanently assigned ports."""
        wolt_json = tmp_wolts / "testwolt" / "wolt" / "wolt.json"
        wolt_json.parent.mkdir(parents=True, exist_ok=True)
        wolt_json.write_text(json.dumps({"name": "testwolt", "site_port": 6001}))
        port = sites._allocate_port()
        assert port == 6002

    def test_no_collision_with_project_range(self, tmp_wolts):
        """Site ports (6001+) don't overlap with project ports (4000-5999)."""
        port = sites._allocate_port()
        assert port >= 6001

    def test_get_or_assign_reads_existing(self, tmp_wolts):
        """Returns existing site_port from wolt.json."""
        wolt_json = tmp_wolts / "testwolt" / "wolt" / "wolt.json"
        wolt_json.write_text(json.dumps({"name": "testwolt", "site_port": 6042}))
        port = sites._get_or_assign_port("testwolt")
        assert port == 6042

    def test_get_or_assign_persists_new(self, tmp_wolts):
        """Assigns and persists a port for wolts without one."""
        wolt_json = tmp_wolts / "testwolt" / "wolt" / "wolt.json"
        wolt_json.write_text(json.dumps({"name": "testwolt", "type": "raccoon"}))
        port = sites._get_or_assign_port("testwolt")
        assert port == 6001
        # Verify it was persisted
        data = json.loads(wolt_json.read_text())
        assert data["site_port"] == 6001


class TestStartStop:
    @patch("sites.subprocess.Popen")
    def test_start_creates_process(self, mock_popen, tmp_wolts):
        mock_popen.return_value.pid = 12345
        state = sites.start_site("testwolt")
        assert state["wolt"] == "testwolt"
        assert state["port"] == 6001
        assert "pid" not in state
        mock_popen.assert_called_once()

    @patch("sites.subprocess.Popen")
    def test_start_idempotent(self, mock_popen, tmp_wolts):
        """Starting an already-running site returns existing state."""
        mock_popen.return_value.pid = 12345
        state1 = sites.start_site("testwolt")

        with patch("sites._is_port_alive", return_value=True):
            state2 = sites.start_site("testwolt")

        assert state1 == state2
        # Popen only called once
        assert mock_popen.call_count == 1

    @patch("sites.subprocess.Popen")
    def test_start_creates_default_index(self, mock_popen, tmp_wolts):
        """Starting a site for a wolt without index.html creates one."""
        mock_popen.return_value.pid = 12345
        # Create a wolt without site dir
        new_wolt = tmp_wolts / "newwolt" / "wolt"
        new_wolt.mkdir(parents=True)

        state = sites.start_site("newwolt")
        assert (tmp_wolts / "newwolt" / "wolt" / "site" / "index.html").exists()

    def test_stop_not_running(self, tmp_wolts):
        assert sites.stop_site("testwolt") is False

    @patch("sites.subprocess.Popen")
    def test_stop_running_site(self, mock_popen, tmp_wolts):
        mock_popen.return_value.pid = 12345
        sites.start_site("testwolt")

        result = sites.stop_site("testwolt")

        assert result is True
        # State should be cleared
        assert sites.get_site_state("testwolt") is None


class TestDefaultIndex:
    def test_default_index_content(self, tmp_wolts):
        sdir = tmp_wolts / "freshwolt" / "wolt" / "site"
        sdir.mkdir(parents=True)
        sites._write_default_index("freshwolt", sdir)
        content = (sdir / "index.html").read_text()
        assert "freshwolt" in content
        assert "<!DOCTYPE html>" in content
