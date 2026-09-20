import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from digging import DIG_VERSION, DigError, DigRequest, DigStore


def request(now=100):
    return DigRequest.create(
        source_colony="alice-root-key",
        source_wolt="n00b",
        task="Create a hello file in the disposable guest workspace",
        target="guest-work",
        now=now,
        lifetime_seconds=600,
    )


def test_owned_colony_happy_path_is_explicit_and_one_use(tmp_path):
    store = DigStore(tmp_path / "digs")
    req = request()
    pending = store.receive(req, verified_peer="alice-root-key", now=101)
    assert pending["state"] == "pending"

    capability = store.approve(req.request_id, now=102, lifetime_seconds=120)
    assert capability not in json.dumps(store.status(req.request_id, now=103))
    active = store.consume(
        req.request_id, capability, verified_peer="alice-root-key", now=104
    )
    assert active["state"] == "active"
    with pytest.raises(DigError, match="already consumed"):
        store.consume(req.request_id, capability, verified_peer="alice-root-key", now=105)

    assert store.finish(req.request_id, now=106)["state"] == "completed"


def test_wire_pairing_does_not_implicitly_authorize_a_dig(tmp_path):
    store = DigStore(tmp_path / "digs")
    req = request()
    store.receive(req, verified_peer="alice-root-key", now=101)
    with pytest.raises(DigError, match="state pending"):
        store.consume(req.request_id, "anything", verified_peer="alice-root-key", now=102)


def test_transport_peer_must_match_claimed_source(tmp_path):
    store = DigStore(tmp_path / "digs")
    with pytest.raises(DigError, match="does not match"):
        store.receive(request(), verified_peer="mallory-root-key", now=101)


def test_grant_is_bound_to_source_peer(tmp_path):
    store = DigStore(tmp_path / "digs")
    req = request()
    store.receive(req, verified_peer="alice-root-key", now=101)
    capability = store.approve(req.request_id, now=102)
    with pytest.raises(DigError, match="does not match"):
        store.consume(req.request_id, capability, verified_peer="mallory-root-key", now=103)


def test_expired_or_revoked_grants_cannot_be_consumed(tmp_path):
    store = DigStore(tmp_path / "digs")
    expired = request()
    store.receive(expired, verified_peer="alice-root-key", now=101)
    cap = store.approve(expired.request_id, now=102, lifetime_seconds=2)
    with pytest.raises(DigError, match="state expired"):
        store.consume(expired.request_id, cap, verified_peer="alice-root-key", now=104)

    revoked = request(now=200)
    store.receive(revoked, verified_peer="alice-root-key", now=201)
    cap = store.approve(revoked.request_id, now=202)
    assert store.revoke(revoked.request_id, now=203)["state"] == "revoked"
    with pytest.raises(DigError, match="state revoked"):
        store.consume(revoked.request_id, cap, verified_peer="alice-root-key", now=204)


def test_request_schema_and_target_are_strict():
    req = request()
    payload = req.to_dict()
    payload["surprise"] = True
    with pytest.raises(DigError, match="unknown or missing"):
        DigRequest.from_dict(payload)
    with pytest.raises(DigError, match="destination-relative"):
        DigRequest.create(
            source_colony="alice",
            source_wolt="n00b",
            task="nope",
            target="../host",
            now=1,
        )
    assert req.version == DIG_VERSION


def test_store_is_private_and_never_persists_raw_capability(tmp_path):
    store = DigStore(tmp_path / "digs")
    req = request()
    store.receive(req, verified_peer="alice-root-key", now=101)
    capability = store.approve(req.request_id, now=102)
    path = tmp_path / "digs" / f"{req.request_id}.json"
    assert (tmp_path / "digs").stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert capability not in path.read_text()


def test_only_one_concurrent_redeemer_wins(tmp_path):
    store = DigStore(tmp_path / "digs")
    req = request()
    store.receive(req, verified_peer="alice-root-key", now=101)
    capability = store.approve(req.request_id, now=102)
    barrier = threading.Barrier(12)
    outcomes = []

    def redeem():
        barrier.wait()
        try:
            store.consume(
                req.request_id,
                capability,
                verified_peer="alice-root-key",
                now=103,
            )
            outcomes.append("won")
        except DigError:
            outcomes.append("lost")

    workers = [threading.Thread(target=redeem) for _ in range(12)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert outcomes.count("won") == 1
    assert outcomes.count("lost") == 11
