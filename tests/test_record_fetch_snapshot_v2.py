# tests/test_record_fetch_snapshot_v2.py
"""record_fetch_snapshot.py captures v2 replay surfaces with proper
provenance metadata. Each task in the 2B-capture plan adds a slice;
this file accumulates assertions across slices.

Spec: docs/superpowers/specs/2026-05-01-fixture-v2-schema.md.
"""
from __future__ import annotations

import json
import threading
import time
import unittest.mock as mock

import pytest

import scripts.record_fetch_snapshot as mod
from sniper.analysis.wallet_protocol.types import WalletData


@pytest.fixture
def synthetic_wallet_data():
    return WalletData(
        wallet="0xabc",
        closed_positions=[
            {"conditionId": "0xcid0", "outcome": "Yes", "avgPrice": 0.45,
             "size": 50, "realizedPnl": 25.0, "endDate": "2026-04-01T00:00:00Z"},
        ],
        open_positions=[
            {"conditionId": "0xcid1", "outcome": "Yes", "asset": "asset_b",
             "avgPrice": 0.52, "size": 100},
        ],
        activity_rows=[
            {"type": "TRADE", "side": "BUY", "conditionId": "0xcid0",
             "asset": "asset_a", "timestamp": 1700000000, "price": 0.45,
             "size": 50, "outcome": "Yes"},
            {"type": "TRADE", "side": "SELL", "conditionId": "0xcid0",
             "asset": "asset_a", "timestamp": 1700050000, "price": 0.50,
             "size": 50, "outcome": "Yes"},
        ],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1700000000,
    )


# Stub tenure dict reused across tests. Capture script's archetype
# branch calls _fetch_tag_map AND _fetch_wallet_tenure_metadata BEFORE
# the snapshot classifier; both must be stubbed in every test that
# reaches the archetype branch (otherwise tag_map fetch hits SQLite and
# tenure fetch hits live HTTP). The autouse fixture below applies these
# stubs to EVERY test in this file, so tests don't have to repeat them.
# Tests that explicitly skip the archetype branch (--no-archetype) still
# get the stubs; they're harmless when unused.
STUB_TENURE = {
    "wallet_first_activity_ts": 1_700_000_000,
    "wallet_last_activity_ts": 1_700_050_000,
    "wallet_lifetime_days": 0.58,
    "pagination_complete": True,
    "pages_fetched": 1,
    "profile_joined_ts": 1_690_000_000,
    "profile_lifetime_days": 365.0,
    "profile_tenure_source": "test",
    "wallet_lifetime_source": "profile_joined",
}


@pytest.fixture(autouse=True)
def _stub_live_producers(monkeypatch):
    """Block every live HTTP / SQLite producer the capture script
    reaches in main(), so unit tests stay fully offline.

    THREE producer families are stubbed:

    1. `_fetch_tag_map` (SQLite via wallet_archetype_extractor) — fired
       in the archetype branch BEFORE `classify_wallet_archetype_from_snapshot`
       to pre-materialize the tag_map input.
    2. `_fetch_wallet_tenure_metadata` (live HTTP via wallet_archetype_extractor)
       — fired in the archetype branch BEFORE the classifier to
       pre-materialize the tenure_metadata input. Tests asserting on
       tenure provenance can rely on the STUB_TENURE values above.
    3. `sqlite3.connect` (the binding inside record_fetch_snapshot) —
       fired by the DB-surfaces branch (Task 6) BEFORE the per-test
       `fetch_tag_lookups_from_db` / `db.peer_baseline_for_grain` mocks
       can intercept. Without this stub, every test that doesn't pass
       all three of `--no-tags --no-nav --no-peers` would touch the
       real `sniper_tracker.db` file. The MagicMock sentinel is fine
       because the per-test mocks ignore the `conn` arg.
    """
    import scripts.wallet_archetype_extractor as wae
    import scripts.record_fetch_snapshot as rfs

    monkeypatch.setattr(wae, "_fetch_tag_map", lambda *a, **kw: {})
    monkeypatch.setattr(
        wae, "_fetch_wallet_tenure_metadata",
        lambda *a, **kw: dict(STUB_TENURE),
    )
    monkeypatch.setattr(
        rfs.sqlite3, "connect", lambda *a, **kw: mock.MagicMock(),
    )


def test_capture_writes_archetype_with_meta(tmp_path, synthetic_wallet_data):
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "speculator_bot"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["archetype_layer1"] == "speculator_bot"
    assert payload["execution_profile"] == {"status": "populated"}
    meta = payload["replay_surface_meta"]
    assert meta["archetype_layer1"]["status"] == "captured"
    # Provenance must record that tenure_metadata was pre-fetched (not
    # left as None to fall back to live HTTP). Values come from the
    # autouse STUB_TENURE.
    assert meta["archetype_layer1"]["tenure_lifetime_source"] == "profile_joined"
    assert meta["archetype_layer1"]["tenure_pagination_complete"] is True
    assert meta["execution_profile"]["status"] == "captured"
    assert meta["execution_profile"]["inner_status"] == "populated"


def test_no_archetype_flag_skips_with_meta(tmp_path, synthetic_wallet_data):
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}):
        rc = mod.main(["0xabc", "--out", str(out), "--no-archetype"])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["archetype_layer1"] is None
    assert payload["replay_surface_meta"]["archetype_layer1"]["status"] == "skipped"


def test_capture_wires_progress_emitter_for_fetch_termination(
    tmp_path, synthetic_wallet_data,
):
    out = tmp_path / "0xabc_test.json"

    def fake_fetch_wallet_data(*args, **kwargs):
        emitter = kwargs["progress_emitter"]
        emitter("fetch_terminated", {
            "endpoint": "closed_positions",
            "reason": "partial_page",
        })
        synthetic_wallet_data.fetch_termination = {
            "closed_positions": "partial_page",
            "open_positions": "empty_page",
            "activity": "empty_page",
        }
        return synthetic_wallet_data

    with mock.patch.object(mod, "fetch_wallet_data",
                           side_effect=fake_fetch_wallet_data), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}):
        rc = mod.main([
            "0xabc", "--out", str(out), "--no-archetype",
            "--no-price-history", "--no-tags", "--no-nav", "--no-peers",
        ])

    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["fetch_termination"]["closed_positions"] == "partial_page"


def test_capture_refuses_fixture_when_fetch_terminated_request_failed(
    tmp_path, synthetic_wallet_data,
):
    out = tmp_path / "0xabc_test.json"
    synthetic_wallet_data.fetch_termination = {
        "closed_positions": "partial_page",
        "open_positions": "empty_page",
        "activity": "request_failed",
    }

    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}):
        rc = mod.main([
            "0xabc", "--out", str(out), "--no-archetype",
            "--no-price-history", "--no-tags", "--no-nav", "--no-peers",
        ])

    assert rc == 2
    assert not out.exists()


def test_no_execution_profile_flag_skips_with_meta(
    tmp_path, synthetic_wallet_data,
):
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ):
        rc = mod.main(["0xabc", "--out", str(out), "--no-execution-profile"])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["execution_profile"] is None
    assert payload["replay_surface_meta"]["execution_profile"]["status"] == "skipped"


def test_archetype_failure_records_failed_status(
    tmp_path, synthetic_wallet_data,
):
    out = tmp_path / "0xabc_test.json"
    # The capture script fetches tag_map and tenure (autouse stubs)
    # BEFORE invoking classify, so the RuntimeError below only fires on
    # the classify call. The script's try/except records that failure
    # in replay_surface_meta.
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             side_effect=RuntimeError("boom"),
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["archetype_layer1"] is None
    assert payload["replay_surface_meta"]["archetype_layer1"]["status"] == "failed"


def test_capture_price_history_for_sells_and_flips(
    tmp_path, synthetic_wallet_data,
):
    """Capture must request prices at sell_ts+24h for every SELL and at
    raw buy_ts/sell_ts for every reconstructed Flip (post-Bug-4 contract)."""
    requested: list[tuple[str, int]] = []

    def fake_price_history_fn(asset_id: str, ts: int) -> float:
        requested.append((asset_id, int(ts)))
        return 0.50

    def fake_build_price_history_fn(**_):
        return fake_price_history_fn

    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           side_effect=fake_build_price_history_fn):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert isinstance(payload["price_history_lookups"], dict)
    # SELL row at sell_ts=1700050000 → expect (asset_a, 1700050000+86400)
    assert ("asset_a", 1700050000 + 86400) in requested
    # Flip reconstructed from BUY 1700000000 / SELL 1700050000 → expect raw
    assert ("asset_a", 1700000000) in requested
    assert ("asset_a", 1700050000) in requested
    assert payload["replay_surface_meta"]["price_history_lookups"]["status"] == "captured"


def test_capture_price_history_lookups_runs_concurrently_and_writes_sorted_keys():
    active = 0
    peak_active = 0
    lock = threading.Lock()

    def fake_price_history_fn(asset_id: str, ts: int) -> float:
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        try:
            time.sleep(0.02)
            return float(ts) / 100.0
        finally:
            with lock:
                active -= 1

    required = {
        ("asset_b", 30),
        ("asset_a", 20),
        ("asset_a", 10),
        ("asset_c", 40),
    }

    lookups, n_missing = mod._capture_price_history_lookups(
        required,
        price_fn_factory=lambda: fake_price_history_fn,
        max_workers=4,
    )

    assert n_missing == 0
    assert peak_active > 1
    assert list(lookups) == [
        "asset_a|10",
        "asset_a|20",
        "asset_b|30",
        "asset_c|40",
    ]
    assert lookups["asset_c|40"] == 0.4


def test_no_price_history_flag_skips_with_meta(tmp_path, synthetic_wallet_data):
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}):
        rc = mod.main(["0xabc", "--out", str(out), "--no-price-history"])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["price_history_lookups"] is None
    assert payload["replay_surface_meta"]["price_history_lookups"]["status"] == "skipped"


def test_capture_price_history_empty_for_zero_sell_wallet(tmp_path):
    """Wallet with zero SELLs and zero flips → captured with empty dict."""
    data = WalletData(
        wallet="0xabc", closed_positions=[], open_positions=[],
        activity_rows=[
            {"type": "TRADE", "side": "BUY", "conditionId": "0xcid0",
             "asset": "asset_a", "timestamp": 1700000000, "price": 0.45,
             "size": 50, "outcome": "Yes"},
        ],
        activity_cap_hit=False, archetype_layer1=None, execution_profile=None,
        fetched_at=1700000000,
    )
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data", return_value=data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           return_value=lambda *_a, **_kw: 0.5):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    # Empty captured surface, NOT None.
    assert payload["price_history_lookups"] == {}
    assert payload["replay_surface_meta"]["price_history_lookups"]["status"] == "captured"
    assert payload["replay_surface_meta"]["price_history_lookups"]["n_entries"] == 0


def test_capture_tags_includes_flip_only_condition_ids(tmp_path):
    """Tag enumeration must include condition_ids that appear only in
    reconstructed flips (not in closed_positions or open_positions).
    Spec rule from Codex blocker #3."""
    # Build a synthetic wallet: one closed position, one open, plus an
    # activity sequence that produces a flip on a THIRD condition_id.
    data = WalletData(
        wallet="0xabc",
        closed_positions=[
            {"conditionId": "0xcid_closed", "outcome": "Yes",
             "avgPrice": 0.45, "size": 50, "realizedPnl": 0.0,
             "endDate": "2026-04-01T00:00:00Z"},
        ],
        open_positions=[
            {"conditionId": "0xcid_open", "outcome": "Yes",
             "asset": "asset_open", "avgPrice": 0.5, "size": 100},
        ],
        activity_rows=[
            {"type": "TRADE", "side": "BUY", "conditionId": "0xcid_flip",
             "asset": "asset_flip", "timestamp": 1700000000, "price": 0.45,
             "size": 50, "outcome": "Yes"},
            {"type": "TRADE", "side": "SELL", "conditionId": "0xcid_flip",
             "asset": "asset_flip", "timestamp": 1700050000, "price": 0.50,
             "size": 50, "outcome": "Yes"},
        ],
        activity_cap_hit=False, archetype_layer1=None, execution_profile=None,
        fetched_at=1700000000,
    )

    captured_cids: list[list[str]] = []

    def fake_fetch_tag_lookups(conn, cids):
        captured_cids.append(sorted(cids))
        return ({c: ["politics"] for c in cids}, {"politics": "politics"})

    def fake_peer_baseline(conn, grain, tag, *, exclude_wallet, **kw):
        return (0.6, 50)

    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data", return_value=data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           return_value=lambda *_a, **_kw: 0.5), \
         mock.patch.object(mod, "fetch_tag_lookups_from_db",
                           side_effect=fake_fetch_tag_lookups), \
         mock.patch("db.peer_baseline_for_grain",
                    side_effect=fake_peer_baseline):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    assert captured_cids, "fetch_tag_lookups_from_db not called"
    cids_seen = set(captured_cids[0])
    assert {"0xcid_closed", "0xcid_open", "0xcid_flip"}.issubset(cids_seen)


def test_no_tags_no_nav_no_peers_flags_each_skip_independently(
    tmp_path, synthetic_wallet_data,
):
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           return_value=lambda *_a, **_kw: 0.5):
        rc = mod.main([
            "0xabc", "--out", str(out),
            "--no-tags", "--no-nav", "--no-peers",
        ])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["leafs_by_cid"] is None
    assert payload["nav_lookup"] is None
    assert payload["peer_baselines"] is None
    for k in ("leafs_by_cid", "nav_lookup", "peer_baselines"):
        assert payload["replay_surface_meta"][k]["status"] == "skipped"


def test_peers_enabled_when_tags_and_nav_skipped(
    tmp_path, synthetic_wallet_data,
):
    """Regression: --no-tags --no-nav must not leave the peer-baselines
    branch's `cids` undefined. Capture script must compute cids ONCE
    up front."""
    requested: list[tuple[str, str]] = []

    def fake_fetch_tag_lookups(conn, cids):
        # Re-fetched inside the peers branch when leafs/navs were skipped
        return ({c: ["politics"] for c in cids},
                {"politics": "politics"})

    def fake_peer_baseline(conn, grain, tag, *, exclude_wallet, **kw):
        requested.append((grain, tag))
        return (0.6, 50)

    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           return_value=lambda *_a, **_kw: 0.5), \
         mock.patch.object(mod, "fetch_tag_lookups_from_db",
                           side_effect=fake_fetch_tag_lookups), \
         mock.patch("db.peer_baseline_for_grain",
                    side_effect=fake_peer_baseline):
        rc = mod.main([
            "0xabc", "--out", str(out),
            "--no-tags", "--no-nav",  # peers stays enabled
        ])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["leafs_by_cid"] is None
    assert payload["nav_lookup"] is None
    assert payload["peer_baselines"] is not None  # captured under peers
    assert requested, "peer_baseline_for_grain not invoked"


def test_peer_baselines_excludes_self_in_meta(
    tmp_path, synthetic_wallet_data,
):
    """Provenance must record exclude_wallet so future readers know the
    snapshot is per-wallet self-excluded."""
    def fake_fetch_tag_lookups(conn, cids):
        return (
            {c: ["politics"] for c in cids},
            {"politics": "politics"},
        )

    def fake_peer_baseline(conn, grain, tag, *, exclude_wallet, **kw):
        # Capture the exclude_wallet for later assertion via the meta.
        return (0.6, 50)

    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           return_value=lambda *_a, **_kw: 0.5), \
         mock.patch.object(mod, "fetch_tag_lookups_from_db",
                           side_effect=fake_fetch_tag_lookups), \
         mock.patch("db.peer_baseline_for_grain",
                    side_effect=fake_peer_baseline):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["replay_surface_meta"]["peer_baselines"]["exclude_wallet"] == "0xabc"


def test_capture_writes_copy_latency_grid(tmp_path):
    """Review #4 — the capture runs the copy-latency post-entry grid as a
    SEPARATE fidelity=1 pass: the grid-specific offsets (+5m/+15m/+60m) are
    requested and copy_latency_prices is populated. Guards against a
    regression dropping the grid capture entirely."""
    entry = 1_700_000_000
    wd = WalletData(
        wallet="0xabc",
        closed_positions=[
            {"conditionId": "0xcid0", "asset": "asset_a", "outcome": "Yes",
             "avgPrice": 0.45, "size": 50, "realizedPnl": 25.0,
             "endDate": "2026-04-01T00:00:00Z"},
        ],
        open_positions=[],
        activity_rows=[
            {"type": "TRADE", "side": "BUY", "conditionId": "0xcid0",
             "asset": "asset_a", "timestamp": entry, "price": 0.45,
             "size": 50, "outcome": "Yes"},
            # a SELL so the H6/FH6 pass has work — without it that pass
            # short-circuits on an empty set and never builds its price fn.
            {"type": "TRADE", "side": "SELL", "conditionId": "0xcid0",
             "asset": "asset_a", "timestamp": entry + 50_000, "price": 0.55,
             "size": 50, "outcome": "Yes"},
        ],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=entry,
    )
    requested: list[tuple[str, int]] = []
    fidelities: list[int] = []

    def fake_price_history_fn(asset_id: str, ts: int) -> float:
        requested.append((asset_id, int(ts)))
        return 0.50

    def fake_build(**kw):
        fidelities.append(kw.get("fidelity", 60))
        return fake_price_history_fn

    out = tmp_path / "0xabc_grid.json"
    with mock.patch.object(mod, "fetch_wallet_data", return_value=wd), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           side_effect=fake_build):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    # grid-specific offsets — unique to copy-latency (H6 = +86400, FH6 = raw)
    assert ("asset_a", entry + 300) in requested
    assert ("asset_a", entry + 900) in requested
    assert ("asset_a", entry + 3600) in requested
    assert isinstance(payload["copy_latency_prices"], dict)
    assert payload["copy_latency_prices"]
    grid_meta = payload["replay_surface_meta"]["copy_latency_grid"]
    assert grid_meta["status"] == "captured"
    # the grid pass must request fidelity=1 (minute-scale drift precedent)
    assert 1 in fidelities
    # ...and the H6/FH6 pass must keep the default (coarse) fidelity 60 —
    # the grid's fidelity=1 must not have leaked into the shared path.
    assert 60 in fidelities
