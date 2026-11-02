"""Wallet Map D3 (spec docs/superpowers/specs/2026-10-07-wallet-map-overhaul-design.md): each supporting
coin carries OUR funded positions for the same wallet, mint and chain, under real_stats_by_wallet's own
filters (execution_mode='real', robot-only, grade exclusions). Return % is realized / notional on
closed rows only. A coin we never entered says why only from an attempt row for that exact origin;
otherwise "no attempt recorded". A ledger we cannot read is unavailable, never "not entered"."""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_wallet_map as wm
from test_memecoin_wallet_map import NOW, _herder, _ledger, _view


SIG88 = "5" * 87                                          # a base58 signature-shaped token


def _origin(mint, sig=None):
    """The relay's origin shape for a source buy: swap:raybot:<signature>:<trader>:<mint>."""
    return "swap:raybot:%s:trader:%s" % (sig or mint.replace("-m", "-s"), mint)


def _pos(led, mint, *, mode="real", source="global_feed", pnl=None, notional=50.0, is_open=0, net="solana",
         uid="A", origin="this", **extra):
    cols = dict(source_user_id=uid, mint=mint, network_id=net, execution_mode=mode, signal_source=source,
                origin_event_id=_origin(mint) if origin == "this" else origin,
                is_open=is_open, realized_pnl=pnl, entry_notional=notional, strategy="alpha_b",
                opened_at="2026-09-29T16:40:05+00:00", closed_at=None if is_open else "2026-09-29T16:50:00+00:00",
                entry_lag_s=1.2, fill_gap_pct=0.5, total_copy_drift_pct=3.0, exit_reason=None if is_open else "real_tp")
    cols.update(extra)
    led.execute("INSERT INTO memecoin_positions (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                list(cols.values()))
    return led.execute("SELECT last_insert_rowid()").fetchone()[0]


def _attempt(led, mint, origin, *, status="refused", reason="mcap over ceiling", at="2026-09-29T16:40:01+00:00",
             uid="A", lane="j7_wallets"):
    led.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, network_id, mint, source_user_id, "
                "decision_started_at, terminal_status, terminal_reason, completed_at, delivery_lane) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("att-%s-%s" % (mint, at), origin, "solana", mint, uid, at, status, reason, at, lane))


def _coin(view, mint):
    return next(c for c in view["coins"] if c["mint"] == mint)


def test_a_closed_funded_entry_shows_its_return_on_notional():
    led = _ledger()
    _pos(led, "A-m0", pnl=5.0, notional=50.0)
    a, fa = _herder("A", 3)
    funded = _coin(_view(a, fa, "A", ledger=led), "A-m0")["funded"]
    assert funded["state"] == "entered"
    [p] = funded["positions"]
    assert p["return_pct"] == pytest.approx(10.0) and p["is_open"] is False and p["exit_reason"] == "real_tp"
    assert (p["entry_lag_s"], p["fill_gap_pct"], p["total_copy_drift_pct"]) == (1.2, 0.5, 3.0)
    assert p["opened_vs_detected_s"] == pytest.approx(5.0), "opened 5 s after we detected the buy (T0)"
    assert p["opened_vs_source_s"] is None, "no chain time stored for this buy: unknown, never detection"


def test_an_earlier_trade_on_the_same_coin_is_another_event_never_entered():
    """Live: an Aug-30 position on the same mint showed as 'entered' under a late-September source buy."""
    led = _ledger()
    _pos(led, "A-m0", pnl=1.0, opened_at="2026-08-30T01:45:51+00:00", origin=_origin("A-m0", SIG88))
    a, fa = _herder("A", 3)
    funded = _coin(_view(a, fa, "A", ledger=led), "A-m0")["funded"]
    assert funded["state"] == "not_entered" and "positions" not in funded
    [p] = funded["other_positions_same_coin"]
    assert p["linkage"] == "other_event" and p["opened_vs_detected_s"] < -30 * 86400
    assert p["return_pct"] == pytest.approx(2.0), "dated and kept, never the coin's return"


def test_the_exact_origin_position_is_entered_and_the_earlier_one_sits_beside_it():
    led = _ledger()
    _pos(led, "A-m0", pnl=1.0, opened_at="2026-08-30T01:45:51+00:00", origin=_origin("A-m0", SIG88))
    _pos(led, "A-m0", pnl=5.0, notional=50.0)
    a, fa = _herder("A", 3)
    funded = _coin(_view(a, fa, "A", ledger=led), "A-m0")["funded"]
    assert funded["state"] == "entered"
    assert [(p["return_pct"], p["linkage"]) for p in funded["positions"]] == [(pytest.approx(10.0), "origin_event_id")]
    assert [p["linkage"] for p in funded["other_positions_same_coin"]] == ["other_event"]


def test_a_position_without_an_origin_links_through_its_attempt():
    led = _ledger()
    _attempt(led, "A-m0", _origin("A-m0"), status="filled", reason="filled")
    att = led.execute("SELECT attempt_id FROM memecoin_entry_attempts").fetchone()[0]
    _pos(led, "A-m0", pnl=5.0, origin=None, attempt_id=att)
    _pos(led, "A-m1", pnl=5.0, origin=None)                                # no origin, no attempt
    _pos(led, "A-m2", pnl=5.0, origin="act-44844e58-70de-432c-813f-35b392365aad")   # carries no signature
    a, fa = _herder("A", 3)
    v = _view(a, fa, "A", ledger=led)
    f0 = _coin(v, "A-m0")["funded"]
    assert f0["state"] == "entered" and f0["positions"][0]["linkage"] == "attempt_id"
    for mint in ("A-m1", "A-m2"):
        f = _coin(v, mint)["funded"]
        assert f["state"] == "not_entered" and [p["linkage"] for p in f["other_positions_same_coin"]] == ["unknown"]


def test_paper_manual_other_chain_and_grade_excluded_rows_never_appear_anywhere():
    led = _ledger()
    _pos(led, "A-m0", mode="paper", pnl=999.0)
    _pos(led, "A-m0", source=m.MANUAL_SIGNAL_SOURCE, pnl=-10.0)
    _pos(led, "A-m0", net="8453", pnl=7.0)
    excluded = _pos(led, "A-m0", pnl=3.0)
    led.execute("INSERT INTO memecoin_wallet_grade_exclusions (position_id, reason, actor, created_at) "
                "VALUES (?, 'test', 'beta', 't')", (excluded,))
    a, fa = _herder("A", 3)
    funded = _coin(_view(a, fa, "A", ledger=led), "A-m0")["funded"]
    assert funded["state"] == "not_entered" and funded.get("positions") is None
    assert funded["other_positions_same_coin"] == []


def test_an_open_entry_and_an_unknown_notional_carry_no_return():
    led = _ledger()
    _pos(led, "A-m0", is_open=1, pnl=None)
    _pos(led, "A-m1", pnl=4.0, notional=0.0)
    _pos(led, "A-m2", pnl=4.0, notional=None)
    a, fa = _herder("A", 3)
    v = _view(a, fa, "A", ledger=led)
    assert [p["return_pct"] for p in _coin(v, "A-m0")["funded"]["positions"]] == [None]
    assert _coin(v, "A-m0")["funded"]["positions"][0]["is_open"] is True
    assert _coin(v, "A-m1")["funded"]["positions"][0]["return_pct"] is None
    assert _coin(v, "A-m2")["funded"]["positions"][0]["return_pct"] is None


def test_not_entered_names_a_reason_only_from_the_exact_origins_attempt():
    led = _ledger()
    _attempt(led, "A-m0", "swap:raybot:A-s0:trader:A-m0", reason="first try", at="2026-09-29T16:40:01+00:00")
    _attempt(led, "A-m0", "swap:raybot:A-s0:trader:A-m0", reason="mcap over ceiling", at="2026-09-29T16:40:09+00:00")
    _attempt(led, "A-m1", "swap:raybot:SOME-OTHER-BUY:trader:A-m1", reason="a later buy's refusal")
    _attempt(led, "A-m2", "swap:raybot:A-s2x:trader:A-m2", reason="a signature that only contains it")
    a, fa = _herder("A", 3)
    v = _view(a, fa, "A", ledger=led)
    f0 = _coin(v, "A-m0")["funded"]
    assert (f0["state"], f0["reason"], f0["attempts"]) == ("not_entered", "mcap over ceiling", 2)
    assert f0["attempt"]["status"] == "refused"
    for mint in ("A-m1", "A-m2"):
        f = _coin(v, mint)["funded"]
        assert (f["state"], f["reason"], f["attempt"], f["attempts"]) == ("not_entered", "no attempt recorded", None, 0)


def test_the_wallet_level_block_is_real_stats_by_wallet_unchanged():
    led = _ledger()
    _pos(led, "A-m0", pnl=5.0, notional=50.0)
    a, fa = _herder("A", 3)
    v = _view(a, fa, "A", ledger=led)
    want = m.real_stats_by_wallet(led, "solana")["A"]
    assert v["funded"] == dict(state="ok", **want)
    assert "only wallets we armed" in v["funded_scope"]
    assert _view(a, fa, "B", ledger=led)["funded"] == {"state": "none", "open_count": 0, "closed_count": 0}


def test_an_unreadable_ledger_is_unavailable_never_not_entered(monkeypatch):
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(m, "real_positions_for_wallet", boom)
    a, fa = _herder("A", 3)
    v = _view(a, fa, "A")
    assert {c["funded"]["state"] for c in v["coins"]} == {"unavailable"}
    assert v["funded"] == {"state": "unavailable"}


def test_the_funded_join_is_one_query_per_view_not_one_per_coin(monkeypatch):
    calls = []
    real = m.real_positions_for_wallet
    monkeypatch.setattr(m, "real_positions_for_wallet", lambda *a, **k: calls.append(a) or real(*a, **k))
    a, fa = _herder("A", 12)
    _view(a, fa, "A")
    assert len(calls) == 1


# --- Gamma's note on D1: the per-wallet cap counts both endpoints -------------------------------------

def test_the_edge_cap_counts_a_wallets_inbound_edges_too(tmp_path, monkeypatch):
    from test_wallet_map_live_links import _edge, _graph as _live_graph
    monkeypatch.setattr(wm, "UNIVERSE_WALLET_EDGE_CAP", 2)
    path = tmp_path / "g.db"
    _live_graph(path, edges=[_edge("follows", "S%d" % i, "HUB", src_uid="S%d" % i, dst_uid="HUB", null_p=p)
                             for i, p in enumerate((0.3, 0.001, 0.01))])
    out = wm.live_universe_edges(path, chain="solana", now_iso=_now_iso())
    assert sorted(e["null_p"] for e in out["edges"]) == [0.001, 0.01], "HUB keeps its 2 strongest inbound"
    assert out["graph"]["omitted_by_wallet"] == {"HUB": 1, "S0": 1}, "an omitted edge is missing from both views"
    assert out["link_counts"]["HUB"] == {"follows": 3}, "counts are of the graph, not the page"


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def test_a_shadow_attempt_never_names_why_we_did_not_enter():
    """Gamma HOLD on D3: a later j7_shadow row won mine[-1] and printed the shadow's own checks as the
    reason; shadow is non-funded by doctrine (brain 137fb844). Funded refusal first, shadow rows after."""
    led = _ledger()
    _attempt(led, "A-m0", _origin("A-m0"), reason="mcap $459,180 over ceiling $250,000",
             at="2026-09-29T16:40:01+00:00")
    _attempt(led, "A-m0", _origin("A-m0"), status="shadow", lane="j7_shadow",
             reason="j7_shadow: pre-entry checks passed; reservation and quote checks not run",
             at="2026-09-29T16:40:03+00:00")
    _attempt(led, "A-m0", _origin("A-m0"), lane="j7_shadow", reason="mcap $309,336 over ceiling $250,000",
             at="2026-09-29T16:40:05+00:00")
    _attempt(led, "A-m1", _origin("A-m1"), status="shadow", lane="j7_shadow", reason="shadow only")
    a, fa = _herder("A", 3)
    v = _view(a, fa, "A", ledger=led)
    f0 = _coin(v, "A-m0")["funded"]
    assert (f0["reason"], f0["attempts"], f0["attempt"]["lane"]) == (
        "mcap $459,180 over ceiling $250,000", 1, "j7_wallets")
    f1 = _coin(v, "A-m1")["funded"]
    assert (f1["reason"], f1["attempts"], f1["attempt"]) == ("no attempt recorded", 0, None)


def test_a_malformed_timestamp_leaves_the_offset_unknown_instead_of_failing_the_view():
    led = _ledger()
    _pos(led, "A-m0", pnl=5.0, opened_at="not a time")
    a, fa = _herder("A", 3)
    [p] = _coin(_view(a, fa, "A", ledger=led), "A-m0")["funded"]["positions"]
    assert p["opened_vs_source_s"] is None and p["opened_vs_detected_s"] is None
    assert p["return_pct"] is not None


# --- S2 (Codex two-day review 2026-10-07): chain time, detection time and open time apart --------

def _detection(led, *, sig="A-s0", mint="A-m0", block_time=None, detected_at="2026-09-29T16:40:00+00:00"):
    led.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, mint, "
                "block_time, detected_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (sig, "A", "addrA", "solana", mint, block_time, detected_at, detected_at))


def test_a_late_detection_never_reads_as_a_lead_over_the_chain_buy():
    """The audit's case: the chain buy at T0-60, our detection at T0, our open at T0+5. Before: one
    source time (the detection), so the open read +5 s after the buy instead of +65 s."""
    from test_memecoin_wallet_map import T0
    led = _ledger()
    _pos(led, "A-m0", pnl=5.0)                                    # opened 16:40:05 = T0 + 5
    _detection(led, block_time=int(T0 - 60))
    a, fa = _herder("A", 3)
    coin = _coin(_view(a, fa, "A", ledger=led), "A-m0")
    src = coin["source"]
    assert src["source_time_basis"] == "chain_block_time"
    assert src["source_at"] == "2026-09-29T16:39:00+00:00" and src["detected_at"] == src["at"]
    [p] = coin["funded"]["positions"]
    assert p["opened_vs_source_s"] == pytest.approx(65.0) and p["opened_vs_detected_s"] == pytest.approx(5.0)


def test_a_detection_without_a_block_time_leaves_the_chain_delta_unknown():
    led = _ledger()
    _pos(led, "A-m0", pnl=5.0)
    _detection(led, block_time=None)
    a, fa = _herder("A", 3)
    coin = _coin(_view(a, fa, "A", ledger=led), "A-m0")
    assert coin["source"]["source_time_basis"] == "unknown" and coin["source"]["source_at"] is None
    [p] = coin["funded"]["positions"]
    assert p["opened_vs_source_s"] is None and p["opened_vs_detected_s"] == pytest.approx(5.0)


def test_the_chain_time_lookup_is_one_bounded_indexed_read():
    from sniper.persistence import db_memecoin as dbm
    led = _ledger()
    _detection(led, block_time=1)
    plan = " ".join(str(r[-1]) for r in led.execute(
        "EXPLAIN QUERY PLAN " + dbm.DETECTION_BLOCK_TIMES_SQL % "?", ("A", "A-m0")).fetchall())
    assert "idx_mc_onchain_detect_wallet_coin" in plan
    assert dbm.detection_block_times(led, "A", ["A-m0", "A-m0", None]) == {"A-s0": 1}
    assert dbm.detection_block_times(led, "A", []) == {}
