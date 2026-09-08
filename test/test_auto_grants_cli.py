"""`woltspace auto` — issuing the consent that makes native Auto reachable.

The grant is the only thing standing between a native wolt and unattended
work, and until this command existed nothing on a host could issue one without
a running control plane to POST to. So these tests care about two things:

* the CLI writes the *same* grant the runtime reads — same store, same
  canonicalization, no second answer to "is this directory approved?";
* it reads the colony the rest of the CLI reads, and never the live one.

Every test pins both spellings of the wolts directory at a tmp_path colony and
pins `WOLTSPACE_DIR` at this checkout, so nothing here can resolve against a
real lodge or against some other install's bundled runtime.

Usage: uv run pytest test/test_auto_grants_cli.py -v
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from execution_policy import AutoGrantStore, resolve_execution_policy  # noqa: E402
from session_targets import SessionTarget  # noqa: E402
from woltspace.cli import main as cli_main  # noqa: E402


WOLTS_DIR_NAMES = ("WOLTSPACE_WOLTS_DIR", "WOLTS_DIR")


@pytest.fixture
def colony(tmp_path, monkeypatch):
    """A throwaway lodge with one wolt, pinned into every name that decides."""
    wolts = tmp_path / "wolts"
    (wolts / "testwolt").mkdir(parents=True)
    for name in WOLTS_DIR_NAMES:
        monkeypatch.setenv(name, str(wolts))
    # The install whose container/lib the CLI grafts. Without this the command
    # would reach into whatever install the ambient WOLTSPACE_DIR names.
    monkeypatch.setenv("WOLTSPACE_DIR", str(ROOT))
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    return wolts


def _store(colony):
    return AutoGrantStore(colony)


def _target(colony, workdir=None, wolt="testwolt"):
    return SessionTarget.resolve(wolt, workdir, wolts_dir=colony)


class TestGrant:
    def test_a_bare_grant_approves_the_wolt_s_own_home(self, colony, capsys):
        assert cli_main(["auto", "grant", "testwolt"]) == 0

        target = _target(colony)
        assert target.canonical_workdir == (colony / "testwolt").resolve()
        grant = _store(colony).find(target)
        assert grant is not None
        assert grant.approved_at > 0
        out = capsys.readouterr().out
        assert "auto approved: testwolt" in out
        assert str(target.canonical_workdir) in out

    def test_a_workdir_grant_approves_that_exact_directory(self, colony, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        assert cli_main(["auto", "grant", "testwolt", "--workdir", str(repo)]) == 0

        assert _store(colony).find(_target(colony, repo)) is not None
        # And nothing else. Consent is per directory, not per wolt.
        assert _store(colony).find(_target(colony)) is None

    def test_the_grant_is_the_one_a_spawn_actually_reads(self, colony, tmp_path):
        """The point of the whole feature: after this, a host session that
        asks for no particular policy runs Auto instead of waiting for a human
        who is not there."""
        repo = tmp_path / "repo"
        repo.mkdir()
        target = _target(colony, repo)
        store = _store(colony)

        before, _ = resolve_execution_policy(
            None, isolation="host", target=target, grants=store
        )
        assert before.mode == "prompt"

        assert cli_main(["auto", "grant", "testwolt", "--workdir", str(repo)]) == 0

        after, grant = resolve_execution_policy(
            None, isolation="host", target=target, grants=store
        )
        assert after.mode == "auto"
        assert grant is not None

    def test_granting_twice_leaves_one_grant(self, colony):
        cli_main(["auto", "grant", "testwolt"])
        cli_main(["auto", "grant", "testwolt"])
        assert len(_store(colony).list()) == 1

    def test_json_is_the_machine_contract(self, colony, capsys):
        assert cli_main(["auto", "grant", "testwolt", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["grant"]["wolt_id"] == "testwolt"
        assert payload["grant"]["canonical_workdir"] == str(
            (colony / "testwolt").resolve()
        )

    def test_an_unknown_wolt_is_refused_and_writes_nothing(self, colony, capsys):
        assert cli_main(["auto", "grant", "nosuchwolt"]) == 1
        assert "auto grant failed" in capsys.readouterr().out
        assert not _store(colony).path.exists()

    def test_a_workdir_that_does_not_exist_is_refused(self, colony, tmp_path, capsys):
        missing = tmp_path / "not-here"
        assert cli_main(
            ["auto", "grant", "testwolt", "--workdir", str(missing)]
        ) == 1
        assert "auto grant failed" in capsys.readouterr().out
        assert not _store(colony).path.exists()


class TestRevoke:
    def test_revoke_removes_the_grant_and_says_so(self, colony, capsys):
        cli_main(["auto", "grant", "testwolt"])
        capsys.readouterr()

        assert cli_main(["auto", "revoke", "testwolt"]) == 0

        assert _store(colony).find(_target(colony)) is None
        assert "auto revoked: testwolt" in capsys.readouterr().out

    def test_revoking_what_was_never_granted_is_not_an_error(self, colony, capsys):
        assert cli_main(["auto", "revoke", "testwolt"]) == 0
        assert "auto was not granted: testwolt" in capsys.readouterr().out

    def test_revoke_is_scoped_to_the_directory_it_names(self, colony, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        cli_main(["auto", "grant", "testwolt"])
        cli_main(["auto", "grant", "testwolt", "--workdir", str(repo)])

        assert cli_main(["auto", "revoke", "testwolt", "--workdir", str(repo)]) == 0

        assert _store(colony).find(_target(colony, repo)) is None
        assert _store(colony).find(_target(colony)) is not None

    def test_json_reports_whether_anything_was_there(self, colony, capsys):
        assert cli_main(["auto", "revoke", "testwolt", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["revoked"] is False
        cli_main(["auto", "grant", "testwolt"])
        capsys.readouterr()
        assert cli_main(["auto", "revoke", "testwolt", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["revoked"] is True


class TestList:
    def test_an_empty_lodge_says_what_that_means(self, colony, capsys):
        assert cli_main(["auto", "list"]) == 0
        out = capsys.readouterr().out
        assert "no auto grants" in out
        assert "asks before it acts" in out

    def test_the_listed_lines_carry_both_facts(self, colony, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        cli_main(["auto", "grant", "testwolt", "--workdir", str(repo)])
        capsys.readouterr()

        cli_main(["auto", "list"])
        out = capsys.readouterr().out

        assert "auto grants: 1" in out
        assert f"testwolt: {repo.resolve()}" in out

    def test_json_lists_the_records_themselves(self, colony, capsys):
        cli_main(["auto", "grant", "testwolt"])
        capsys.readouterr()

        assert cli_main(["auto", "list", "--json"]) == 0
        grants = json.loads(capsys.readouterr().out)["grants"]

        assert [g["wolt_id"] for g in grants] == ["testwolt"]
        assert grants[0]["policy_version"] == 1

    def test_listing_never_creates_the_colony_it_was_pointed_at(
        self, tmp_path, monkeypatch, capsys
    ):
        """A read must not conjure a lodge — least of all the default one."""
        absent = tmp_path / "no-lodge"
        for name in WOLTS_DIR_NAMES:
            monkeypatch.setenv(name, str(absent))
        monkeypatch.setenv("WOLTSPACE_DIR", str(ROOT))

        assert cli_main(["auto", "list"]) == 0
        assert "no auto grants" in capsys.readouterr().out
        assert not absent.exists()


class TestTheCommandReadsTheColonyTheCliReads:
    def test_the_wolts_dir_override_decides_where_the_grant_lands(
        self, colony, tmp_path, monkeypatch
    ):
        """Both spellings, one answer — and never the ambient default."""
        elsewhere = tmp_path / "other-wolts"
        (elsewhere / "testwolt").mkdir(parents=True)
        for name in WOLTS_DIR_NAMES:
            monkeypatch.setenv(name, str(elsewhere))

        assert cli_main(["auto", "grant", "testwolt"]) == 0

        assert AutoGrantStore(elsewhere).list()
        assert not _store(colony).path.exists()

    def test_a_bare_auto_names_its_verbs(self, colony, capsys):
        assert cli_main(["auto"]) == 1
        out = capsys.readouterr().out
        assert "grant" in out and "revoke" in out and "list" in out
