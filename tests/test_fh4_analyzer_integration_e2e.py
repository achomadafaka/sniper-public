"""End-to-end test: real `analyze_wallet` construction path → FH4 inference
runs → persist_flipper_tag_grains succeeds.

Built specifically to catch the two Phase 2 wiring blockers that the
unit-suite missed:

  1. `analyze_wallet` not threading `flip_roi_baseline_fn` → real
     analyzer runs would produce `inference_not_run` regardless of
     Phase 2.
  2. `compute_wallet_fixture_hash` reading fields that don't exist on
     real `ProtocolReport` → live-mode persistence would raise
     ValueError on every real wallet.

Unlike `test_wallet_protocol_fh4_flip_roi_inference.py` (which calls
`_build_fh4_grain` and `run_flipper_branch` directly with handcrafted
flips), these tests build a real `WalletData`, call `analyze_wallet`,
and observe the resulting `ProtocolReport`.

Per feedback-e2e-test-real-protocolreport: at least one acceptance
test must use the real `analyze_wallet` construction path.
"""
from __future__ import annotations

import time

import pytest

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.ledger.flipper_tag_grain_writer import (
    persist_flipper_tag_grains,
)
from sniper.analysis.wallet_protocol.ledger.supabase_io import StubSupabaseHttp
from sniper.analysis.wallet_protocol.types import WalletData


# ----------------------------------------------------------------------
# Fixture construction — real WalletData with synthetic but well-shaped
# closed_positions + activity_rows. Tags are wired via leafs_by_cid +
# nav_lookup so the flipper branch has tag-grain context.
# ----------------------------------------------------------------------

def _build_flipper_wallet_data(
    wallet: str = "0xfliptest",
    n_flips: int = 40,
    flip_roi: float = 0.30,
) -> WalletData:
    """Synthetic flipper wallet: `n_flips` round-trips (buy + sell)
    same condition_id, all profitable at `flip_roi`. Includes one
    closed position per flip and matching activity rows."""
    closed = []
    activity = []
    # Start activity within the last 7 days so the Stage 1 dormancy
    # gate (90-day threshold) doesn't reject the wallet outright.
    now = int(time.time())
    base_ts = now - (n_flips * 7200 + 86400)
    for i in range(n_flips):
        cid = f"0xcid{i:04d}"
        buy_price = 0.50
        sell_price = buy_price * (1.0 + flip_roi)
        size = 100.0
        notional_buy = buy_price * size
        notional_sell = sell_price * size
        closed.append({
            "conditionId": cid,
            "outcome": "Yes",
            "asset": f"asset{i}",
            "realizedPnl": str(notional_sell - notional_buy),
            "avgPrice": str(buy_price),
            "size": str(size),
            "initialValue": str(notional_buy),
            "totalBought": str(notional_buy),
            "totalSold": str(notional_sell),
            "endDate": "2025-12-31T00:00:00Z",
            "curPrice": str(sell_price),
        })
        ts_buy = base_ts + i * 7200
        ts_sell = ts_buy + 3600
        activity.append({
            "id": f"buy-{i}",
            "transactionHash": f"0xbuy{i:04d}",
            "conditionId": cid,
            "asset": f"asset{i}",
            "side": "BUY",
            "outcome": "Yes",
            "price": str(buy_price),
            "size": str(size),
            "usdcSize": str(notional_buy),
            "timestamp": str(ts_buy),
        })
        activity.append({
            "id": f"sell-{i}",
            "transactionHash": f"0xsell{i:04d}",
            "conditionId": cid,
            "asset": f"asset{i}",
            "side": "SELL",
            "outcome": "Yes",
            "price": str(sell_price),
            "size": str(size),
            "usdcSize": str(notional_sell),
            "timestamp": str(ts_sell),
        })
    return WalletData(
        wallet=wallet,
        closed_positions=closed,
        open_positions=[],
        activity_rows=activity,
        activity_cap_hit=False,
        archetype_layer1="flipper",
        execution_profile=None,
        fetched_at=now,
        walk_full_history=True,
    )


# ----------------------------------------------------------------------
# Real-path tests
# ----------------------------------------------------------------------

def test_real_protocolreport_wallet_fixture_hash_is_stamped():
    """The whole point of Phase 2 wiring: `analyze_wallet` must stamp
    `report.wallet_fixture_hash` from `WalletData` so the writer's
    live-mode read finds something."""
    data = _build_flipper_wallet_data()
    report = analyze_wallet("0xfliptest", pre_fetched_data=data)
    assert report.wallet_fixture_hash is not None
    assert len(report.wallet_fixture_hash) == 16


def test_real_protocolreport_wallet_fixture_hash_is_deterministic():
    """Same raw data → same hash. The whole supersession contract."""
    data = _build_flipper_wallet_data()
    r1 = analyze_wallet("0xfliptest", pre_fetched_data=data)
    r2 = analyze_wallet("0xfliptest", pre_fetched_data=data)
    assert r1.wallet_fixture_hash == r2.wallet_fixture_hash


def test_real_protocolreport_wallet_fixture_hash_differs_on_data_change():
    """A new activity row → different hash → writer would supersede."""
    d1 = _build_flipper_wallet_data(n_flips=40)
    d2 = _build_flipper_wallet_data(n_flips=41)
    r1 = analyze_wallet("0xfliptest", pre_fetched_data=d1)
    r2 = analyze_wallet("0xfliptest", pre_fetched_data=d2)
    assert r1.wallet_fixture_hash != r2.wallet_fixture_hash


def test_persist_flipper_tag_grains_does_not_raise_on_real_protocolreport():
    """Direct regression for Codex Finding #2 — live mode raised
    ValueError because the writer read phantom fields. With the
    stamped hash, the writer can derive a key without re-scanning
    the report."""
    data = _build_flipper_wallet_data()
    leafs_by_cid = {p["conditionId"]: ["geopolitics"] for p in data.closed_positions}
    nav_lookup = {"geopolitics": "geopolitics_nav"}
    report = analyze_wallet(
        "0xfliptest",
        pre_fetched_data=data,
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
    )
    # If the writer is reachable here (flipper branch active,
    # walk_full_history True, grains exist) the call must not raise.
    # Independent of whether grains have peer baselines.
    db = StubSupabaseHttp()
    result = persist_flipper_tag_grains(report, supabase=db)
    # Either it wrote rows (flipper grains existed) or it skipped with
    # a typed reason — but NEVER a ValueError about the fixture hash.
    assert result.wallet == "0xfliptest"


def test_real_analyze_wallet_threads_flip_roi_baseline_fn():
    """Direct regression for Codex Finding #1 — analyze_wallet must
    accept and forward flip_roi_baseline_fn into the flipper branch.
    Spy the fn to verify it's actually invoked, then check that FH4
    grains carry a peer_count from it (proving the wiring is end-to-end)."""
    data = _build_flipper_wallet_data()
    leafs_by_cid = {p["conditionId"]: ["geopolitics"] for p in data.closed_positions}
    nav_lookup = {"geopolitics": "geopolitics_nav"}

    captured: list[tuple[str, str, str]] = []

    def _spy_flip_roi(grain: str, tag: str, wallet: str):
        captured.append((grain, tag, wallet))
        # Return a peer mean well below the wallet's mean ROI (0.30)
        # so FH4 inference can fire. peer_count = 30 to clear the
        # post-pool low-population guard.
        return (0.05, 30)

    report = analyze_wallet(
        "0xfliptest",
        pre_fetched_data=data,
        flip_roi_baseline_fn=_spy_flip_roi,
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
    )

    # The fn was actually called from within analyze_wallet.
    assert captured, "flip_roi_baseline_fn never invoked by analyze_wallet"
    # The wallet under analysis is forwarded so the baseline can
    # self-exclude — a property the unit-mismatch test pins.
    assert all(w == "0xfliptest" for _, _, w in captured)

    # FH4 inference ran end-to-end: at least one tag grain should now
    # carry the supplied peer_count and a non-None metric_value.
    flipper_branch = report.flipper_branch
    assert flipper_branch is not None
    grains_with_peers = [
        g for g in (flipper_branch.tag_grains or []) if g.peer_count > 0
    ]
    assert grains_with_peers, (
        "no flipper tag grain received peer_count from the baseline fn"
    )


def test_e2e_supported_verdict_when_wallet_above_peers():
    """Full integration: real analyze_wallet → FH4 fires → at least
    one grain reaches `supported` after the BH pool because:
      - Wallet flip ROI = +0.30 (well above peer 0.05)
      - n_flips = 40 (above FH4_MIN_FLIPS_FOR_INFERENCE=30)
      - peer_count = 30 (clears PEER_COUNT_OK_THRESHOLD)
    """
    data = _build_flipper_wallet_data(n_flips=40, flip_roi=0.30)
    leafs_by_cid = {p["conditionId"]: ["geopolitics"] for p in data.closed_positions}
    nav_lookup = {"geopolitics": "geopolitics_nav"}

    report = analyze_wallet(
        "0xfliptest",
        pre_fetched_data=data,
        flip_roi_baseline_fn=lambda g, t, w: (0.05, 30),
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
        n_iter=300,
        seed=1,
    )

    flipper_branch = report.flipper_branch
    assert flipper_branch is not None and flipper_branch.is_active

    supported = [
        g for g in (flipper_branch.tag_grains or [])
        if g.verdict == "supported"
    ]
    # At least one grain (the geopolitics leaf) should be supported.
    assert supported, (
        f"expected at least one supported FH4 grain; got "
        f"{[(g.grain, g.tag, g.verdict) for g in (flipper_branch.tag_grains or [])]}"
    )
