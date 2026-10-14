"""`woltspace access` restores remote access from the lodge's own machine,
and doctor names a published lodge that would refuse every remote request."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from woltspace.cli import main as cli_main  # noqa: E402
from woltspace.doctor import run_doctor  # noqa: E402
from woltspace.layout import RuntimeLayout, installation_root  # noqa: E402

VALUES = [
    "--team-domain", "team.cloudflareaccess.com", "--lodge-aud", "lodge-aud",
    "--owner-email", "Owner@Example.com",
]


@pytest.fixture
def colony(tmp_path, monkeypatch):
    wolts = tmp_path / "wolts"
    wolts.mkdir()
    (wolts / "woltspace.json").write_text(json.dumps({"default_harness": "claude"}))
    for name in ("WOLTSPACE_WOLTS_DIR", "WOLTS_DIR"):
        monkeypatch.setenv(name, str(wolts))
    monkeypatch.setenv("WOLTSPACE_DIR", str(ROOT))
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    return wolts


def test_set_show_and_clear(colony, capsys):
    assert cli_main(["access", "set", *VALUES]) == 0
    saved = json.loads((colony / "woltspace.json").read_text())
    assert saved["access"] == {
        "team_domain": "team.cloudflareaccess.com", "lodge_aud": "lodge-aud",
        "apps_aud": "", "owner_email": "owner@example.com",
    }
    assert saved["default_harness"] == "claude"

    capsys.readouterr()
    assert cli_main(["access", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["access"]["lodge_aud"] == "lodge-aud"

    assert cli_main(["access", "clear"]) == 0
    assert "access" not in json.loads((colony / "woltspace.json").read_text())


def test_invalid_values_are_refused_and_nothing_is_written(colony, capsys):
    before = (colony / "woltspace.json").read_text()
    bad = ["--team-domain", "https://team.example.com/x", "--lodge-aud", "a",
           "--owner-email", "o@example.com"]
    assert cli_main(["access", "set", *bad]) == 1
    assert "team_domain" in capsys.readouterr().err
    assert (colony / "woltspace.json").read_text() == before


def _layout(colony):
    return RuntimeLayout(colony, installation_root(), "127.0.0.1", 18777, "host")


def test_doctor_warns_when_a_published_lodge_does_not_verify_access(colony, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_TOKEN", "token")
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_URL", "https://lodge.example.com")
    monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "true")
    checks = {c.name: c for c in run_doctor(_layout(colony), check_port=False)}
    assert checks["remote-access"].status == "warn"
    assert "treated as you" in checks["remote-access"].detail
    assert "https://lodge.example.com" in checks["remote-access"].detail
    assert "woltspace access set" in checks["remote-access"].remedy

    assert cli_main(["access", "set", *VALUES]) == 0
    checks = {c.name: c for c in run_doctor(_layout(colony), check_port=False)}
    assert checks["remote-access"].status == "pass"


def test_doctor_is_quiet_about_access_when_nothing_is_published(colony, monkeypatch):
    for key in ("CLOUDFLARE_TUNNEL_TOKEN", "CLOUDFLARE_TUNNEL_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "false")
    checks = {c.name for c in run_doctor(_layout(colony), check_port=False)}
    assert "remote-access" not in checks


def _saved(colony):
    return json.loads((colony / "woltspace.json").read_text())["access"]


def test_omitting_apps_aud_keeps_the_saved_one_and_empty_clears_it(colony):
    assert cli_main(["access", "set", *VALUES, "--apps-aud", "apps-aud"]) == 0
    assert cli_main(["access", "set", *VALUES[:4], "--owner-email", "new@example.com"]) == 0
    assert _saved(colony)["apps_aud"] == "apps-aud"
    assert _saved(colony)["owner_email"] == "new@example.com"
    assert cli_main(["access", "set", *VALUES, "--apps-aud", ""]) == 0
    assert _saved(colony)["apps_aud"] == ""


def test_changing_team_needs_a_deliberate_apps_choice(colony, capsys):
    assert cli_main(["access", "set", *VALUES, "--apps-aud", "apps-aud"]) == 0
    moved = ["--team-domain", "other.cloudflareaccess.com", *VALUES[2:]]
    assert cli_main(["access", "set", *moved]) == 1
    assert "--apps-aud" in capsys.readouterr().err
    assert _saved(colony)["team_domain"] == "team.cloudflareaccess.com"
    assert cli_main(["access", "set", *moved, "--apps-aud", "new-apps"]) == 0
    assert _saved(colony)["apps_aud"] == "new-apps"


@pytest.mark.parametrize("block", [{"broken": True}, []])
def test_doctor_reads_broken_settings_the_way_the_lodge_does(block, colony, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_TOKEN", "token")
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_URL", "https://lodge.example.com")
    monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "true")
    (colony / "woltspace.json").write_text(json.dumps({"access": block}))
    check = {c.name: c for c in run_doctor(_layout(colony), check_port=False)}["remote-access"]
    assert check.status == "warn"
    assert "refuses every remote request" in check.detail
    assert "restore" in check.remedy and "remove" not in check.remedy


def test_doctor_treats_an_allowed_host_as_published(colony, monkeypatch):
    for key in ("CLOUDFLARE_TUNNEL_TOKEN", "CLOUDFLARE_TUNNEL_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "false")
    monkeypatch.setenv("WOLTSPACE_ALLOWED_HOSTS", "lodge.tail1234.ts.net")
    check = {c.name: c for c in run_doctor(_layout(colony), check_port=False)}["remote-access"]
    assert check.status == "warn"
    assert "lodge.tail1234.ts.net" in check.detail


def test_doctor_names_allowed_host_entries_that_never_match(colony, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_ALLOWED_HOSTS",
                       "ok.example, fd00::1, 10.0.0.5, *.ts.net, [fd00::1], host:8080, "
                       "https://x.example, studio.localhost, ..")
    check = {c.name: c for c in run_doctor(_layout(colony), check_port=False)}["allowed-hosts"]
    assert check.status == "warn"
    for bad in ("*.ts.net", "[fd00::1]", "host:8080", "https://x.example", "studio.localhost", ".."):
        assert bad in check.detail
    assert "ok.example" not in check.detail
    assert "fd00::1," not in check.detail.replace("[fd00::1]", "") + ","
    assert "10.0.0.5" not in check.detail
