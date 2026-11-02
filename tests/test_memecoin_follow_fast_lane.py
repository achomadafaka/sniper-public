"""Fast-lane follow-activity polling + live-mcap fallback on the real-entry gate.
Spec: docs/superpowers/specs/2026-07-15-memecoin-follow-fast-lane-design.md
Latency cliff (verified 2026-07-15): <60s follow catches +$79/copy 78% WR (n=9, honest
alpha_b); 180-600s −$14/copy 17% WR — the 300s single-lane poll puts nearly every catch
in the losing buckets by construction."""
import sqlite3
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.services.memecoin_follow_activity import fast_lane_wallets, follow_activity_pass

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NOW = "2026-07-06T12:00:00Z"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _add(c, uid, handle, *, armed=False, paused=0):
    m.add_wallet(c, uid, handle, source="follow")
    if paused:
        c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id=?", (uid,))
    if armed:
        c.execute("UPDATE memecoin_wallets SET real_approved=1, real_chains='solana' "
                  "WHERE source_user_id=?", (uid,))   # arming is per chain (2026-07-30)
    c.commit()


def _run(c, swaps_by_uid, **kw):
    polled = []

    def swaps_fn(uid):
        polled.append(uid)
        return swaps_by_uid.get(uid, [])

    res = follow_activity_pass(
        c, None, 100.0, now_fn=lambda: NOW, real_now_fn=lambda: NOW,
        fill_fn=lambda *a, **k: (0.005, "jupiter"),
        swaps_fn=swaps_fn, sleep_fn=lambda s: None, **kw)
    return res, polled


# ---- lane roster selection -----------------------------------------------------------------

def test_fast_lane_wallets_is_effective_armed_set_only():
    c = _conn()
    _add(c, "u_armed", "alice", armed=True)
    _add(c, "u_watch", "bob")
    _add(c, "u_paused_armed", "carol", armed=True, paused=1)
    rows = fast_lane_wallets(c)
    assert [w["source_user_id"] for w in rows] == ["u_armed"]


def test_pass_polls_only_the_given_wallets():
    c = _conn()
    _add(c, "u_armed", "alice", armed=True)
    _add(c, "u_watch", "bob")
    _, polled = _run(c, {}, wallets=fast_lane_wallets(c))
    assert polled == ["u_armed"]


def test_pass_excludes_given_uids():
    c = _conn()
    _add(c, "u_armed", "alice", armed=True)
    _add(c, "u_watch", "bob")
    _, polled = _run(c, {}, exclude_uids={"u_armed"})
    assert polled == ["u_watch"]


def test_pass_default_roster_unchanged():
    c = _conn()
    _add(c, "u_armed", "alice", armed=True)
    _add(c, "u_watch", "bob")
    _, polled = _run(c, {})
    assert sorted(polled) == ["u_armed", "u_watch"]


def test_fast_lane_fetch_workers_overlap_wallet_requests():
    import threading

    c = _conn()
    _add(c, "u1", "one", armed=True)
    _add(c, "u2", "two", armed=True)
    barrier = threading.Barrier(2)
    polled = []

    def swaps_fn(uid):
        polled.append(uid)
        barrier.wait(timeout=2)
        return []

    follow_activity_pass(
        c, None, 100.0, now_fn=lambda: NOW, real_now_fn=lambda: NOW,
        swaps_fn=swaps_fn, sleep_fn=lambda _seconds: None,
        wallets=fast_lane_wallets(c), fetch_workers=2,
    )
    assert sorted(polled) == ["u1", "u2"]


def test_overlapping_lanes_never_double_open():
    c = _conn()
    _add(c, "u_armed", "alice", armed=True)
    swap = {"id": "s1", "inTokenAddress": USDC, "outTokenAddress": "MINT",
            "humanUsdAmountIn": "3000", "humanUsdAmountOut": "3000",
            "inHumanAmount": "3000", "outHumanAmount": "600000",
            "createdAt": "2026-07-06T11:55:00Z", "isOffPlatform": "False",
            "networkId": 1399811149}
    r1, _ = _run(c, {"u_armed": [swap]}, wallets=fast_lane_wallets(c))   # fast lane copies
    r2, _ = _run(c, {"u_armed": [swap]})                                 # slow lane sees same swap
    assert r1["copied"] == 1 and r2["copied"] == 0
    n = c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE mint='MINT' "
                  "AND strategy='control'").fetchone()[0]
    assert n == 1


# ---- live-mcap fallback on the real-entry gate ----------------------------------------------

class _EvObj(SimpleNamespace):
    def __init__(self, fid, **kw):
        base = dict(fomo_event_id=fid, source_user_id="u_armed", source_handle="alice",
                    mint="So1FastMint", ticker="FAST", side="buy", their_price=0.005,
                    their_market_cap=0.0, their_size_usd=3000.0, network_id="solana",
                    signal_source="follow_activity", created_at=NOW, received_at=NOW,
                    lag_s=10.0, received_at_normalized=NOW, clock_domain="observed_real_utc")
        base.update(kw)
        super().__init__(**base)


def _armed_conn():
    c = _conn()
    _add(c, "u_armed", "alice", armed=True)
    return c


def _hook(c, stats_fn):
    return R.make_real_hook(
        None, {"solana": {"usdc_fn": lambda: 1000.0, "sol_fn": lambda: 1.0,
                          "token_safety_fn": lambda mint, **kw: (False, "stop here"),
                          "execute_fn": lambda *a, **k: {"status": "rejected"},
                          "kill_path": None}},
        now_fn=lambda: NOW, trade_size=50.0, max_age_s=120, stats_fn=stats_fn)


def test_unresolved_mcap_falls_back_to_live_stats_and_proceeds():
    c = _armed_conn()
    # event mcap 0.0, no feed history -> old behavior: skipped_mcap. Live stats says $500k.
    hook = _hook(c, lambda mint, **k: {"price": 0.005, "market_cap": 500_000.0})
    res = hook(c, _EvObj("f1"))
    assert res["status"] != "skipped_mcap"   # gate passed; downstream gates may still refuse


def test_live_mcap_over_ceiling_still_skips():
    c = _armed_conn()
    hook = _hook(c, lambda mint, **k: {"price": 0.005, "market_cap": 45_000_000.0})
    res = hook(c, _EvObj("f2"))
    # AUTO_TEMPLATES defaults off in tests -> over-ceiling skips
    assert res["status"] == "skipped_mcap"
    assert res["mcap"] == 45_000_000.0


def test_live_mcap_unavailable_assumes_micro_on_solana(monkeypatch):
    """2026-07-16: triple-unresolvable mcap on Solana = a minutes-old micro by construction —
    the entry proceeds (assume-micro) instead of fail-closed-skipping the sub-$1M pocket.
    The old fail-closed behavior survives behind MEMECOIN_REAL_MCAP_ASSUME_MICRO=0."""
    from sniper.settings import memecoin as _cfg
    c = _armed_conn()
    hook = _hook(c, lambda mint, **k: {"price": 0.005})   # no market_cap in the response
    res = hook(c, _EvObj("f3"))
    assert res["status"] != "skipped_mcap"                # proceeds past the mcap gate
    monkeypatch.setattr(_cfg, "MEMECOIN_REAL_MCAP_ASSUME_MICRO", False)
    res = hook(c, _EvObj("f3b"))
    assert res["status"] == "skipped_mcap"
    assert res["mcap"] is None
