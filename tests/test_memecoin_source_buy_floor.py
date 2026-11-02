"""The funded entry hook uses the copied buy as a fraction of entry market cap."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services import memecoin_real_runner as runner
from sniper.settings import memecoin as cfg

NOW = "2026-07-15T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP", 0.0)
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *a, **k: None)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "alice")
    m.set_wallet_real_approved(conn, "u1", True, chains=["solana"], force=True)
    return conn


def _event(their_size_usd, *, market_cap=100_000.0):
    return SimpleNamespace(
        side="buy", source_user_id="u1", source_handle="alice", mint="So1Mint", ticker="ABC",
        network_id="solana", their_price=0.25, their_market_cap=market_cap,
        their_size_usd=their_size_usd, is_first_buy=True, fomo_event_id="event-1",
        created_at="2026-07-15T11:59:59+00:00", received_at="legacy-host-clock",
        received_at_normalized="2026-07-15T11:59:59.900000+00:00",
        clock_domain="observed_real_utc", lag_s=0.1, signal_source="global_feed",
    )


def _context(reached):
    def execute(conn, pending_id, mint, size, now_iso, impact_cap_pct=None, **kw):
        reached.append(mint)
        return {"status": "unknown", "reason": "confirmation unknown", "sig": "sig"}

    return {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
            "token_safety_fn": lambda mint, **kw: (True, "ok"),
            "execute_fn": execute, "kill_path": None}


def _run(conn, ev, *, classification_context=None):
    reached = []
    res = runner.real_entry_hook(
        conn, ev, {"solana": _context(reached)}, now_fn=lambda: NOW,
        trade_size=10.0, max_age_s=120,
        classification_context=classification_context)
    return res, reached


def _ratio_floor(monkeypatch):
    """The RATIO gate in isolation, with the absolute band explicitly OFF.

    This helper used to set `MEMECOIN_REAL_MIN_SOURCE_BUY_USD = 249.0`. That name was a real
    setting once (`8659c818`), was REPLACED by the ratio in `556d50d8`, and the monkeypatch was
    left behind — inert for months because `raising=False` happily sets a name nothing reads.
    The name became live again on 2026-09-08 when the absolute band was added ALONGSIDE the ratio
    rather than in place of it, and the stale patch immediately refused three of these tests.

    The tests below are about the ratio, and their names say so ("the OLD dollar floor"), so the
    band is pinned off here. `test_the_ratio_and_the_band_compose` covers them running together.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.005,
                        raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 100.0,
                        raising=False)


def test_source_buy_under_mcap_floor_is_refused_before_any_reservation(monkeypatch):
    _ratio_floor(monkeypatch)
    conn = _conn()
    res, reached = _run(conn, _event(2_000.0, market_cap=1_000_000.0))
    assert res["status"] == "rejected"
    assert "source buy $2000 (0.200%) under mcap floor 0.500% ($5000)" in res["reason"]
    assert reached == []                                   # never reached the executor
    # no bankroll was encumbered: an under-floor signal must not hold a slot
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_small_coin_source_buy_below_the_old_dollar_floor_passes(monkeypatch):
    _ratio_floor(monkeypatch)
    conn = _conn()
    res, reached = _run(conn, _event(200.0, market_cap=10_000.0))
    assert res["status"] != "rejected", res
    assert reached == ["So1Mint"]


def test_resolved_mcap_controls_the_floor_when_the_feed_mcap_is_missing(monkeypatch):
    _ratio_floor(monkeypatch)
    conn = _conn()
    res, reached = _run(
        conn, _event(200.0, market_cap=None),
        classification_context={"effective_mcap": 10_000.0},
    )
    assert res["status"] != "rejected", res
    assert reached == ["So1Mint"]


def test_make_hook_uses_resolved_mcap_for_the_source_buy_floor(monkeypatch):
    _ratio_floor(monkeypatch)
    conn = _conn()
    event = _event(500.0, market_cap=None)
    m.insert_event(conn, {
        "fomo_event_id": event.fomo_event_id,
        "source_user_id": event.source_user_id,
        "source_handle": event.source_handle,
        "mint": event.mint,
        "ticker": event.ticker,
        "side": event.side,
        "their_price": event.their_price,
        "their_market_cap": event.their_market_cap,
        "their_size_usd": event.their_size_usd,
        "is_first_buy": event.is_first_buy,
        "created_at": event.created_at,
        "received_at": event.received_at,
        "lag_s": event.lag_s,
        "network_id": event.network_id,
    })
    reached = []
    hook = runner.make_real_hook(
        None, {"solana": _context(reached)}, now_fn=lambda: NOW,
        trade_size=10.0, max_age_s=120,
        stats_fn=lambda *_args, **_kwargs: {"market_cap": 100_000.0, "price": 0.25},
    )
    result = hook(conn, event)
    assert result["status"] != "rejected", result
    assert reached == ["So1Mint"]
    row = conn.execute(
        "SELECT real_decision, trace_json FROM memecoin_events e "
        "JOIN memecoin_entry_attempts a ON a.origin_event_id=e.fomo_event_id "
        "WHERE e.fomo_event_id=?", (event.fomo_event_id,),
    ).fetchone()
    assert row["real_decision"] == "unknown"
    assert '"effective_mcap":100000.0' in row["trace_json"]
    assert '"floor_usd":500.0' in row["trace_json"]


def test_unknown_mcap_uses_the_hard_fallback(monkeypatch):
    _ratio_floor(monkeypatch)
    conn = _conn()
    res, reached = _run(conn, _event(99.0, market_cap=None))
    assert res["status"] == "rejected"
    assert "source buy $99 under unknown-mcap floor $100" in res["reason"]
    assert reached == []


def test_wallet_ratio_zero_override_bypasses_only_ratio(monkeypatch):
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET', {'u1': 0.0}, raising=False)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MIN_SOURCE_BUY_USD', 200.0)
    conn = _conn()
    res, reached = _run(conn, _event(2000, market_cap=1_000_000))
    assert reached == ['So1Mint'], res
    small, reached = _run(_conn(), _event(50, market_cap=1_000_000))
    assert 'under absolute floor $200' in small['reason'] and reached == []
    unknown, reached = _run(_conn(), _event(50, market_cap=None))
    assert 'under unknown-mcap floor $100' in unknown['reason'] and reached == []


def test_wallet_ratio_override_is_scoped_to_one_solana_wallet(monkeypatch):
    from sniper.services.memecoin_real_runner import _wallet_source_buy_ratio
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET', {'u1': 0.0}, raising=False)
    assert _wallet_source_buy_ratio('solana', 'u1', .008) == 0.0
    assert _wallet_source_buy_ratio('solana', 'someone-else', .008) == .008
    assert _wallet_source_buy_ratio('8453', 'u1', .008) == .008


def test_unknown_mcap_at_the_hard_fallback_passes(monkeypatch):
    _ratio_floor(monkeypatch)
    conn = _conn()
    res, reached = _run(conn, _event(100.0, market_cap=None))
    assert res["status"] != "rejected", res
    assert reached == ["So1Mint"]


def test_missing_source_size_fails_closed(monkeypatch):
    _ratio_floor(monkeypatch)
    conn = _conn()
    res, reached = _run(conn, _event(None))
    assert res["status"] == "rejected"
    assert "source buy unknown under mcap floor 0.500% ($500)" in res["reason"]
    assert reached == []


def test_zero_floors_disable_the_gate(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.0,
                        raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0,
                        raising=False)
    conn = _conn()
    res, reached = _run(conn, _event(1.0))
    assert res["status"] != "rejected", res
    assert reached == ["So1Mint"]


# --- the ratio and the absolute band run TOGETHER (2026-09-08) --------------------------------

def test_the_ratio_and_the_band_compose(monkeypatch):
    """The band ADDS to the ratio. Either one may refuse, and the ratio speaks first.

    A $200 buy on a $10k coin passes the 0.005 ratio (floor $50) and is refused by a $1,000
    absolute floor — which is the whole point of adding one, since the ratio refuses least where
    coins are smallest.
    """
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 1_000.0, raising=False)
    conn = _conn()
    res, _reached = _run(conn, _event(200.0, market_cap=10_000.0))
    assert res["status"] == "rejected"
    assert "absolute floor" in res["reason"], res


def test_a_ratio_refusal_wins_over_the_band(monkeypatch):
    """One entry, one reason. The ratio is checked first and the band defers to it."""
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 1_000.0, raising=False)
    conn = _conn()
    res, _reached = _run(conn, _event(200.0, market_cap=1_000_000.0))
    assert res["status"] == "rejected"
    assert "mcap floor" in res["reason"], res


def test_the_band_alone_works_with_the_ratio_off(monkeypatch):
    """The size parse used to live inside the ratio guard, so the band could not run without it."""
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 10_000.0, raising=False)
    conn = _conn()
    res, _reached = _run(conn, _event(50_000.0, market_cap=1_000_000.0))
    assert res["status"] == "rejected"
    assert "absolute ceiling" in res["reason"], res


def test_unknown_size_passes_the_BAND_but_the_ratio_still_refuses_it(monkeypatch):
    """Two gates, two postures, both deliberate.

    The band passes an unreported size — a field the source did not send says nothing about the
    trade. The RATIO has always failed closed on it (`test_missing_source_size_fails_closed`), and
    adding the band must not quietly change that. With the ratio off, the same event passes.
    """
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 1_000.0, raising=False)
    res, _ = _run(_conn(), _event(None, market_cap=1_000_000.0))
    assert res["status"] == "rejected"
    assert "mcap floor" in res["reason"], "the ratio, not the band, refuses an unknown size"

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0, raising=False)
    res2, _ = _run(_conn(), _event(None, market_cap=1_000_000.0))
    assert res2["status"] != "rejected", "the band alone must let an unknown size through"


def test_unknown_mcap_still_faces_the_ABSOLUTE_floor(monkeypatch):
    """An absolute floor needs no market cap to be meaningful, so an unknown mcap does not excuse
    a buy from it.

    This is the deliberate answer to a real design question, not an expectation update. The ratio
    leg cannot judge a coin whose mcap we could not resolve and falls back to
    `MIN_SOURCE_BUY_UNKNOWN_MCAP_USD`; the absolute leg is independent of market cap entirely and
    is precisely the gate that still has an opinion there. A $100 buy that clears the $100 unknown-
    mcap fallback is still under a $249 absolute floor, and is refused by the absolute leg.
    """
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 249.0, raising=False)
    res, reached = _run(_conn(), _event(100.0, market_cap=None))
    assert res["status"] == "rejected"
    assert "under absolute floor $249" in res["reason"], res
    assert reached == []


def test_unknown_mcap_passes_when_the_absolute_floor_is_off(monkeypatch):
    """The same event, band off: the unknown-mcap fallback alone lets it through.

    Pinned as a pair so the two legs stay separable and neither is mistaken for the other.
    """
    _ratio_floor(monkeypatch)
    res, reached = _run(_conn(), _event(100.0, market_cap=None))
    assert res["status"] != "rejected", res
    assert reached == ["So1Mint"]


# --- per-band floor (2026-09-12, operator "improve the per band logic") ------------------------

def _band_floor(monkeypatch, bands):
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.008, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND",
                        cfg._parse_source_buy_band_ratios(bands), raising=False)


def test_a_nano_band_floor_admits_a_buy_the_global_ratio_refuses(monkeypatch):
    _band_floor(monkeypatch, "nano=0.005")
    res, reached = _run(_conn(), _event(240.0, market_cap=40_000.0))     # 0.60% of a $40k coin
    assert res["status"] != "rejected", res
    assert reached == ["So1Mint"]


def test_an_unlisted_band_keeps_the_global_ratio(monkeypatch):
    _band_floor(monkeypatch, "nano=0.005")
    res, reached = _run(_conn(), _event(600.0, market_cap=100_000.0))    # 0.60% of a micro coin
    assert res["status"] == "rejected"
    assert "under mcap floor 0.800%" in res["reason"]
    assert reached == []


def test_a_chain_band_entry_wins_over_the_band_entry(monkeypatch):
    _band_floor(monkeypatch, "micro=0.008,solana:micro=0.02")
    res, reached = _run(_conn(), _event(1_500.0, market_cap=100_000.0))  # 1.5% of a Solana micro
    assert res["status"] == "rejected"
    assert "under mcap floor 2.000%" in res["reason"]


def test_the_band_list_parser_skips_malformed_entries():
    assert cfg._parse_source_buy_band_ratios("nano=0.005, solana:MICRO=0.02,bad,x=y,mid=-1") == {
        "nano": 0.005, "solana:micro": 0.02}


# ── cumulative net source buy (2026-09-13, operator: "keep track of how much they've invested") ──

def _prior(conn, eid, side, usd, at, mint="So1Mint", uid="u1"):
    m.insert_event(conn, {"fomo_event_id": eid, "source_user_id": uid, "source_handle": "alice",
                          "mint": mint, "ticker": "ABC", "side": side, "their_price": 0.25,
                          "their_market_cap": 100_000.0, "their_size_usd": usd, "is_first_buy": 0,
                          "created_at": at, "received_at": at, "lag_s": 0.1, "network_id": "solana"})


def test_spaced_buys_count_toward_the_floor(monkeypatch):
    """APU: $200 then $100 seconds apart, each under the floor alone."""
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 1800.0)
    conn = _conn()
    _prior(conn, "e0", "buy", 300.0, "2026-07-15T11:50:00Z")          # 10 min earlier
    res, reached = _run(conn, _event(300.0, market_cap=100_000.0))    # floor 0.5% = $500
    assert res["status"] != "rejected", res
    assert reached == ["So1Mint"]


def test_sells_and_old_buys_do_not_count(monkeypatch):
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 1800.0)
    conn = _conn()
    _prior(conn, "e0", "buy", 300.0, "2026-07-15T11:20:00Z")          # 40 min earlier: outside
    _prior(conn, "e1", "buy", 300.0, "2026-07-15T11:55:00Z")
    _prior(conn, "e2", "sell", 250.0, "2026-07-15T11:57:00Z")         # net prior = $50
    _prior(conn, "e3", "buy", 900.0, "2026-07-15T11:56:00Z", mint="Other")
    res, reached = _run(conn, _event(300.0, market_cap=100_000.0))
    assert res["status"] == "rejected"
    assert "source buy $300 + $50 earlier = $350 (0.350%) under mcap floor 0.500% ($500)" in res["reason"]
    assert reached == []


def test_off_by_default_judges_the_single_buy(monkeypatch):
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 0.0)
    conn = _conn()
    _prior(conn, "e0", "buy", 900.0, "2026-07-15T11:55:00Z")
    res, _reached = _run(conn, _event(300.0, market_cap=100_000.0))
    assert res["status"] == "rejected" and "source buy $300 (0.300%)" in res["reason"]


def test_the_event_being_judged_is_not_counted_twice():
    conn = _conn()
    _prior(conn, "event-1", "buy", 300.0, "2026-07-15T11:59:59Z")
    got = m.source_prior_net_buy_usd(conn, source_user_id="u1", network_id="solana", mint="So1Mint",
                                     before_iso="2026-07-15T12:00:00+00:00", window_s=1800,
                                     exclude_event_id="event-1")
    assert got == 0.0


def test_a_cumulative_admit_records_what_was_judged(monkeypatch):
    """SHI 4663, 2026-09-25: a $877 buy was admitted on $1,232 bought earlier, and the trace showed
    the $877 beside ratio_pct 0.83% with nothing to connect them. The row names both now."""
    import json
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 1800.0)
    conn = _conn()
    _prior(conn, "e0", "buy", 300.0, "2026-07-15T11:50:00Z")
    _run(conn, _event(300.0, market_cap=100_000.0))
    (raw,) = conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()
    row = next(ev for ev in json.loads(raw)["substage_events"] if ev.get("name") == "source_buy_floor")
    assert row["source_size_usd"] == 300.0
    assert row["prior_net_usd"] == 300.0 and row["judged_usd"] == 600.0
    assert row["ratio_pct"] == pytest.approx(0.6)


def test_the_alert_names_an_earlier_buy_that_carried_the_floor():
    from sniper.services.memecoin_telegram import render_entry_notification as render
    base = {"status": "filled", "ticker": "SHI", "network_id": "4663", "origin_event_id": "e1",
            "source_handle": "NotARandomUser", "source_size_usd": 877.421737}
    cumulative = dict(base, gate_evidence=[{"name": "source_buy_floor", "prior_net_usd": 1231.90032,
                                            "judged_usd": 2109.322057}])
    text = render(cumulative)
    assert "NotARandomUser bought $877 (+$1.2k earlier = $2.1k)" in text
    single = dict(base, gate_evidence=[{"name": "source_buy_floor", "prior_net_usd": None,
                                        "judged_usd": 877.421737}])
    text = render(single)
    assert "NotARandomUser bought $877" in text and "earlier" not in text
    assert "earlier" not in render(base)


# ---------------------------------------------------------------------------- the same trade, twice
# CARDANO e3c706bf (2026-09-29): the on-chain lane judged ResellCalendar's buy as $1,000 plus $998.50
# "earlier" -- J7's report of the SAME transaction, keyed 'swap:raybot:<sig>:...' where the lane's is
# 'onchain:<sig>'. Excluding only the judged event's own id missed it. Gamma's causal replay: 11 of
# 995 floor-judged attempts since 09-22 carried their own trade, 0 flipped the floor.
SIG = "5ivfHtRQooXunzZ8KagMx41dSdoTkt4GvX7Mqy2PwTwifj6XaeBoX276NbdwmPAxMM6zbht44JuHz8fuRQZwXDxC"
TX = "0x" + "8b" * 32


def _prior_net(conn, exclude, *, at="2026-07-15T12:00:00+00:00", usd=None, mint="So1Mint", net="solana"):
    return m.source_prior_net_buy_usd(conn, source_user_id="u1", network_id=net, mint=mint,
                                      before_iso=at, window_s=1800, exclude_event_id=exclude, judged_usd=usd)


def test_the_same_source_tx_reported_by_j7_is_not_an_earlier_buy():
    conn = _conn()
    _prior(conn, "swap:raybot:%s:dxm1hfy8:so1mint" % SIG, "buy", 998.5, "2026-07-15T11:59:59.700Z")
    assert _prior_net(conn, "onchain:" + SIG) == 0.0
    _prior(conn, "swap:raybot:%s:dxm1hfy8:so1mint" % ("4" * 88), "buy", 300.0, "2026-07-15T11:59:00Z")
    assert _prior_net(conn, "onchain:" + SIG) == 300.0, "another tx still counts"


def test_an_evm_hash_is_matched_the_same_way():
    conn = _conn()
    _prior(conn, "swap:raybot:%s:0x838a:0xc465" % TX, "buy", 2497.6, "2026-07-15T11:59:59Z")
    assert _prior_net(conn, "onchain:4663:%s:0xc465" % TX) == 0.0


def test_a_sell_carrying_the_judged_tx_is_excluded_and_no_other_sell_is():
    conn = _conn()
    _prior(conn, "swap:raybot:%s:owner:so1mint" % ("4" * 88), "buy", 500.0, "2026-07-15T11:58:00Z")
    _prior(conn, "swap:raybot:%s:owner:so1mint" % SIG, "sell", 200.0, "2026-07-15T11:59:58Z")
    _prior(conn, "swap:9f1c2e2a-uuid", "sell", 150.0, "2026-07-15T11:59:59Z")
    assert _prior_net(conn, "onchain:" + SIG) == 350.0, "the uuid sell counts, the judged tx's sell does not"


def test_a_j7_uuid_report_with_no_tx_carrying_twin_still_counts():
    """A uuid report is an earlier buy unless a trade WITH a transaction pairs with it (operator order
    2026-09-30, which replaces the 09-29 rule "never matched by size and time")."""
    conn = _conn()
    _prior(conn, "swap:9f1c2e2a-uuid", "buy", 199.6, "2026-07-15T11:59:58Z")
    assert _prior_net(conn, "swap:0ae2b9d1-uuid", usd=199.6) == 199.6, "two uuid reports, no tx anywhere"
    assert _prior_net(conn, "onchain:" + SIG) == 199.6, "the judged size is unknown: nothing pairs by amount"
    assert _prior_net(conn, "onchain:" + SIG, usd=260.0) == 199.6, "more than 5% apart: another trade"


def test_the_double_count_no_longer_carries_a_buy_over_the_floor(monkeypatch):
    """The CARDANO shape at the hook: $300 alone is under the floor; the same trade twice was over."""
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 1800.0)
    conn = _conn()
    _prior(conn, "swap:raybot:%s:owner:so1mint" % SIG, "buy", 300.0, "2026-07-15T11:59:58Z")
    ev = _event(300.0, market_cap=100_000.0)                  # floor 0.5% of $100k = $500
    ev.fomo_event_id = "onchain:" + SIG
    res, reached = _run(conn, ev)
    assert res["status"] == "rejected" and "source buy $300 (0.300%)" in res["reason"], res
    assert reached == []


def test_an_address_or_a_mint_is_never_read_as_a_tx():
    """Gamma LOW: a widened regex would let an id whose first matching part is an address or a mint
    exclude every event carrying that owner or coin."""
    assert m.source_tx_of("x:0x" + "a" * 40) is None
    assert m.source_tx_of("x:" + "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") is None
    assert m.source_tx_of("x:0x" + "a" * 64) == "0x" + "a" * 64


# ---------------------------------------------------------------------------- one trade, two reports
# Operator order 2026-09-30 ("Fix both"), from Lambda's trace of the running release a931a2b86.
# 1. DOUBLE COUNT. J7 sends some trades twice: 'swap:raybot:<tx>:...' and 'swap:<uuid>' (Fomo app time,
#    a median 18 s before the tx report; no transaction in the payload). The 09-29 rule excluded only
#    events carrying the judged tx, so the uuid report of the judged trade counted as an earlier buy:
#    attempt 2841a5c0 (2026-09-27 15:55:56Z) judged a $270 buy as $540 against a $402 floor.
# 2. UNDERCOUNT. A buy seen only by the on-chain lane is a row in onchain_wallet_detections, not in
#    memecoin_events, so the sum missed it: Roveryaps' second $200 buy (2026-09-29 22:41:56Z) was
#    judged with prior 0.
# Trades are keyed by transaction. A uuid report pairs with a tx-carrying trade of the same wallet,
# coin and side within 120 s and 5%, each transaction used once; a paired report is that trade, an
# unpaired one is its own trade. A paired trade is "earlier" by the judged trade's own clock.
#
# Rule 5: money path (the funded source-buy floor). One more indexed read on the caller's connection,
# no provider call. Unknown size adds nothing and pairs with nothing. Unreadable history is None.
# NOT handled: a uuid report whose tx twin has not arrived yet counts on its own; equal-size slices
# of which only some have a tx record can pair with the wrong slice.
def _sig(n):
    return "%d%s" % (n, "A" * 87)


def _det(conn, sig, usd, at, *, block=None, mint="So1Mint", uid="u1", net="solana"):
    conn.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, mint, "
                 "source_size_usd, block_time, detected_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (sig, uid, "addr", net, mint, usd, block, at, at))


def _si(conn, *, dets, reports):
    """Roveryaps on SI, 2026-09-29 22:41-22:42Z, moved to 2026-07-15 11:41-11:42Z: four real $200 buys
    on chain at :54, :55, 12:08, 12:08 and J7's uuid reports of them stamped :47, :49, 12:02, 12:19."""
    chain = {1: "11:41:54", 2: "11:41:55", 3: "11:42:08", 4: "11:42:08.500"}
    app = {1: "11:41:47", 2: "11:41:49", 3: "11:42:02", 4: "11:42:19"}
    for n in dets:
        _det(conn, _sig(n), 200.0, "2026-07-15T%s+00:00" % chain[n])
    for n in reports:
        _prior(conn, "swap:0000000%d-uuid" % n, "buy", 199.6, "2026-07-15T%sZ" % app[n])
    return chain, app


def test_equal_size_slices_still_sum_the_third_buy_has_a_true_prior_of_400():
    """The required case. What was in the ledger when the third buy was judged: detections 1 and 2 and
    the uuid reports of buys 1 and 2. The prior is the two earlier trades, each counted once."""
    conn = _conn()
    _si(conn, dets=(1, 2), reports=(1, 2))
    assert _prior_net(conn, "onchain:" + _sig(3), at="2026-07-15T11:42:08+00:00", usd=200.0) == 400.0


def test_a_buy_only_the_on_chain_lane_saw_is_an_earlier_buy():
    """The undercount: at the second buy's decision the ledger held detection 1 and no J7 event."""
    conn = _conn()
    _si(conn, dets=(1,), reports=())
    assert _prior_net(conn, "onchain:" + _sig(2), at="2026-07-15T11:41:55+00:00", usd=200.0) == 200.0
    assert _prior_net(conn, "onchain:" + _sig(1), at="2026-07-15T11:41:54+00:00", usd=200.0) == 0.0, \
        "the judged trade's own detection is not an earlier buy"


def test_the_fourth_buy_sums_three_trades_once_each():
    conn = _conn()
    _si(conn, dets=(1, 2, 3), reports=(1, 2, 3))
    assert _prior_net(conn, "onchain:" + _sig(4), at="2026-07-15T11:42:08.500+00:00", usd=200.0) == 600.0


def test_a_judged_uuid_report_reads_its_earlier_trades_on_its_own_clock():
    """The uuid report of buy 2 is stamped 11:41:49, before either buy reached the chain (11:41:54,
    :55). Buy 1 is still earlier: its report is stamped 11:41:47. It counts once, at the size its
    transaction record carries, and the judged trade's own transaction is not an earlier buy."""
    conn = _conn()
    _si(conn, dets=(1, 2), reports=(1, 2))
    assert _prior_net(conn, "swap:00000002-uuid", at="2026-07-15T11:41:49+00:00", usd=199.6) == 200.0
    assert _prior_net(conn, "swap:00000001-uuid", at="2026-07-15T11:41:47+00:00", usd=199.6) == 0.0


def test_the_uuid_report_of_the_judged_trade_is_not_an_earlier_buy():
    """The required failing case, 2841a5c0: one buy on chain ($270) and J7's uuid report of it
    ($269.62, stamped 4 s earlier). The 09-29 rule judged $270 + $270."""
    conn = _conn()
    _prior(conn, "swap:b77fc7f2-uuid", "buy", 269.62, "2026-07-15T11:59:51Z")
    assert _prior_net(conn, "onchain:" + SIG, at="2026-07-15T11:59:55+00:00", usd=270.0) == 0.0
    _det(conn, SIG, None, "2026-07-15T11:59:55.400000+00:00", block=1784116795)       # its own detection row,
    assert _prior_net(conn, "onchain:" + SIG, at="2026-07-15T11:59:55+00:00", usd=270.0) == 0.0   # no size on it
    assert _prior_net(conn, "onchain:" + SIG, at="2026-07-15T11:59:55+00:00") == 269.62, \
        "with no size for the judged trade anywhere, the report cannot be paired with it"


def test_the_double_count_no_longer_carries_2841a5c0_over_the_floor(monkeypatch):
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 1800.0)
    conn = _conn()
    _prior(conn, "swap:b77fc7f2-uuid", "buy", 269.62, "2026-07-15T11:59:55Z")
    ev = _event(270.0, market_cap=80_400.0)                   # floor 0.5% of $80,400 = $402
    ev.fomo_event_id = "onchain:" + SIG
    res, reached = _run(conn, ev)
    assert res["status"] == "rejected" and "source buy $270 (0.336%) under mcap floor" in res["reason"], res
    assert reached == []


def test_one_transaction_is_one_trade_whoever_reported_it():
    """J7's tx-id event and the watcher's detection of the same transaction: once, at J7's size (the
    field the floor's evidence was measured on). An EVM detection is keyed '<chain>:<tx>:<mint>'."""
    conn = _conn()
    _prior(conn, "swap:raybot:%s:owner:so1mint" % _sig(1), "buy", 998.5, "2026-07-15T11:50:00Z")
    _det(conn, _sig(1), 1000.0, "2026-07-15T11:50:01+00:00")
    assert _prior_net(conn, "onchain:" + _sig(2), usd=50.0) == 998.5
    evm = "0x" + "ab" * 32
    conn.execute("UPDATE memecoin_events SET network_id='4663', mint='0xc465', fomo_event_id=? ",
                 ("swap:raybot:%s:0x838a:0xc465" % evm.upper().replace("0X", "0x"),))
    _det(conn, "4663:%s:0xc465" % evm, 1000.0, "2026-07-15T11:50:01+00:00", mint="0xc465", net="4663")
    assert _prior_net(conn, "onchain:4663:%s:0xc465" % TX, usd=50.0, mint="0xc465", net="4663") == 998.5


def test_each_transaction_pairs_with_one_report_only():
    conn = _conn()
    _det(conn, _sig(1), 200.0, "2026-07-15T11:50:05+00:00")
    _prior(conn, "swap:00000001-uuid", "buy", 199.6, "2026-07-15T11:50:00Z")
    _prior(conn, "swap:00000002-uuid", "buy", 199.6, "2026-07-15T11:50:02Z")
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == pytest.approx(399.6), \
        "one report is the detected trade, the other is a trade of its own"


def test_reports_take_their_transactions_in_time_order():
    """Two trades 120 s apart and their two reports: the earlier report takes the earlier
    transaction, so the later one still finds its own (115 s away). Served the other way round the
    later report would take the near transaction and the earlier one would be a third trade."""
    conn = _conn()
    _det(conn, _sig(1), 200.0, "2026-07-15T11:50:00+00:00")
    _det(conn, _sig(2), 200.0, "2026-07-15T11:52:00+00:00")
    _prior(conn, "swap:00000002-uuid", "buy", 199.6, "2026-07-15T11:50:05Z")
    _prior(conn, "swap:00000001-uuid", "buy", 199.6, "2026-07-15T11:49:55Z")
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == 400.0


def test_a_report_takes_the_nearest_transaction_not_any_in_range():
    """A judged uuid report at 12:00:00. One earlier trade (transaction 11:59:20, report 11:59:30) and a
    LATER trade already in the ledger (transaction 12:00:30). The report is the near transaction's;
    given to the far one, the later trade would read as earlier on the app clock."""
    conn = _conn()
    _det(conn, _sig(1), 200.0, "2026-07-15T11:59:20+00:00")
    _det(conn, _sig(2), 200.0, "2026-07-15T12:00:30+00:00")
    _prior(conn, "swap:00000001-uuid", "buy", 199.6, "2026-07-15T11:59:30Z")
    assert _prior_net(conn, "swap:0000000j-uuid") == 200.0


@pytest.mark.parametrize("usd, at, counted", [
    (199.6, "11:50:00Z", 200.0),             # the twin: the trade once
    (189.9, "11:50:00Z", 389.9),             # more than 5% under 200: another trade
    (210.1, "11:50:00Z", 410.1),             # more than 5% over
    (190.0, "11:50:00Z", 200.0),             # 5% exactly pairs
    (199.6, "11:47:59Z", 399.6),             # 121 s before the transaction: another trade
    (199.6, "11:48:00Z", 200.0),             # 120 s pairs
    (199.6, "11:52:01Z", 399.6),             # 121 s after
])
def test_a_report_pairs_within_120_seconds_and_5_percent(usd, at, counted):
    conn = _conn()
    _det(conn, _sig(1), 200.0, "2026-07-15T11:50:00+00:00")
    _prior(conn, "swap:00000001-uuid", "buy", usd, "2026-07-15T" + at)
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == pytest.approx(counted)


def test_a_sell_report_pairs_with_a_sell_only_and_sells_still_net_out():
    conn = _conn()
    _det(conn, _sig(1), 500.0, "2026-07-15T11:50:00+00:00")
    _prior(conn, "swap:raybot:%s:owner:so1mint" % _sig(2), "sell", 200.0, "2026-07-15T11:55:10Z")
    _prior(conn, "swap:00000002-uuid", "sell", 200.4, "2026-07-15T11:55:00Z")       # the same sale
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == 300.0
    _prior(conn, "swap:00000003-uuid", "sell", 500.0, "2026-07-15T11:50:01Z")       # a SELL never pairs with a buy
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == -200.0


def test_an_unknown_size_adds_nothing_and_pairs_with_nothing():
    """646 of 3,633 detections carried no size on 2026-09-30. Unknown is not a known 0: it cannot pair
    by amount, so the uuid report of that trade stays a trade, counted once."""
    conn = _conn()
    _det(conn, _sig(1), None, "2026-07-15T11:50:00+00:00")
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == 0.0
    _prior(conn, "swap:00000001-uuid", "buy", 199.6, "2026-07-15T11:49:55Z")
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == 199.6
    # a J7 tx-id event without a size takes the detection's
    _prior(conn, "swap:raybot:%s:owner:so1mint" % _sig(2), "buy", None, "2026-07-15T11:40:00Z")
    _det(conn, _sig(2), 300.0, "2026-07-15T11:40:01+00:00")
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == pytest.approx(499.6)


def test_detections_outside_the_wallet_coin_chain_or_window_do_not_count():
    conn = _conn()
    _det(conn, _sig(1), 100.0, "2026-07-15T11:29:59+00:00")                       # 1 s before the window
    _det(conn, _sig(2), 100.0, "2026-07-15T12:00:00+00:00")                       # at the judged time
    _det(conn, _sig(3), 100.0, "2026-07-15T11:50:00+00:00", uid="u2")
    _det(conn, _sig(4), 100.0, "2026-07-15T11:50:00+00:00", mint="Other")
    _det(conn, _sig(5), 100.0, "2026-07-15T11:50:00+00:00", net="4663")
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == 0.0
    _det(conn, _sig(6), 100.0, "2026-07-15T11:30:00+00:00")                       # the window's first second
    _det(conn, _sig(7), 100.0, "2026-07-15T12:05:00+00:00", block=1784116500)     # chain time 11:55:00, detected late
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == 200.0, "chain time decides when there is one"


def test_two_buys_in_the_same_chain_second_are_not_each_others_earlier_buy():
    """Replay 2026-09-27 04:31:39Z (attempts e4e39bdf, 4f558ad1): two $200 slices in one chain second,
    both detected before either decision. Judged at the moment the watcher saw them (a second after
    the block), each read the other as earlier and both cleared the floor. Their order inside the
    second is unknown, so neither is the other's prior."""
    conn = _conn()
    block = 1784116795                                          # 2026-07-15T11:59:55Z
    _det(conn, _sig(1), 200.0, "2026-07-15T11:59:56.100000+00:00", block=block)
    _det(conn, _sig(2), 200.0, "2026-07-15T11:59:56.200000+00:00", block=block)
    _det(conn, _sig(3), 200.0, "2026-07-15T11:59:55.900000+00:00", block=block - 1)
    assert _prior_net(conn, "onchain:" + _sig(1), at="2026-07-15T11:59:56.100000+00:00", usd=200.0) == 200.0
    assert _prior_net(conn, "onchain:" + _sig(2), at="2026-07-15T11:59:56.200000+00:00", usd=200.0) == 200.0
    # a judged trade whose own record is not in the ledger yet is read at the time it was seen
    assert _prior_net(conn, "onchain:" + _sig(9), at="2026-07-15T11:59:56.200000+00:00", usd=200.0) == 600.0


@pytest.mark.parametrize("block", [0, -1, "abc"])
def test_a_block_time_that_is_not_a_chain_time_is_absent(block):
    """Gamma MEDIUM 2026-09-30. The EVM watcher stores whatever the node's log carries, and 3 of 216
    EVM detections hold 0 (4663, 2026-09-28 15:14Z). Read as a chain time, 0 put a judged trade's
    window at 1970 (prior 0) and hid an earlier buy for good: attempts 275d89cc and 36944806 fell from
    $1,790.7 to $0. Such a record is timed by when the watcher saw it."""
    conn = _conn()
    _prior(conn, "swap:raybot:%s:owner:so1mint" % _sig(1), "buy", 1000.0, "2026-07-15T11:55:00Z")
    _det(conn, _sig(2), 200.0, "2026-07-15T12:00:01+00:00", block=block)          # the judged trade's own record
    assert _prior_net(conn, "onchain:" + _sig(2), at="2026-07-15T12:00:01+00:00", usd=200.0) == 1000.0
    _det(conn, _sig(3), 500.0, "2026-07-15T11:59:00+00:00", block=block)          # an earlier buy only the lane saw
    assert _prior_net(conn, "onchain:" + _sig(2), at="2026-07-15T12:00:01+00:00", usd=200.0) == 1500.0


def test_a_detections_chain_time_is_the_transactions_time_whoever_else_reported_it():
    """Gamma LOW 2026-09-30. J7 stamps a transaction 0.2 to 2.4 s after its block (902 shared
    transactions, p50 0.9 s). Kept as the transaction's time, that stamp (a) hid a buy one chain second
    earlier from an on-chain-judged trade and (b) made a J7-judged trade count a slice in its own
    chain second, which the on-chain lane does not."""
    block = 1784116795                                          # 2026-07-15T11:59:55Z
    conn = _conn()
    _prior(conn, "swap:raybot:%s:owner:so1mint" % _sig(1), "buy", 300.0, "2026-07-15T11:59:55.400Z")
    _det(conn, _sig(1), 300.0, "2026-07-15T11:59:55.100000+00:00", block=block - 1)
    _det(conn, _sig(2), 200.0, "2026-07-15T11:59:56.100000+00:00", block=block)
    assert _prior_net(conn, "onchain:" + _sig(2), at="2026-07-15T11:59:56.100000+00:00", usd=200.0) == 300.0
    conn = _conn()
    judged = "swap:raybot:%s:owner:so1mint" % _sig(1)
    _prior(conn, judged, "buy", 200.0, "2026-07-15T11:59:56Z")                     # J7: its block + 1 s
    _det(conn, _sig(1), 200.0, "2026-07-15T11:59:56.100000+00:00", block=block)
    _det(conn, _sig(2), 200.0, "2026-07-15T11:59:56.200000+00:00", block=block)   # a slice in the same second
    assert _prior_net(conn, judged, at="2026-07-15T11:59:56+00:00", usd=200.0) == 0.0
    # a record with no chain time leaves J7's stamp alone: seen late, the buy is still ten minutes old
    conn = _conn()
    _prior(conn, judged, "buy", 300.0, "2026-07-15T11:50:00Z")
    _det(conn, _sig(1), 300.0, "2026-07-15T12:00:30+00:00")
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == 300.0


def test_a_judged_uuid_report_not_yet_in_the_ledger_still_pairs_with_its_own_transaction():
    """Gamma LOW 2026-09-30: the branch that adds the judged report when the rows do not hold it had no
    test. Its transaction reached the chain 10 s before its app stamp; unpaired, it read as earlier."""
    conn = _conn()
    _det(conn, _sig(1), 200.0, "2026-07-15T11:59:50+00:00")
    assert _prior_net(conn, "swap:0000000j-uuid", usd=199.6) == 0.0
    assert _prior_net(conn, "swap:0000000j-uuid") == 200.0, "no judged size: nothing pairs by amount"


def test_a_twin_just_outside_the_window_is_still_read_so_its_report_is_not_a_trade_of_its_own():
    conn = _conn()
    # the transaction is 30 s BEFORE the window opens, its report 10 s after: one trade, outside
    _det(conn, _sig(1), 200.0, "2026-07-15T11:29:30+00:00")
    _prior(conn, "swap:00000001-uuid", "buy", 199.6, "2026-07-15T11:30:10Z")
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == 0.0
    # the transaction is stamped 30 s AFTER the judged trade, its report 10 s before: a later trade
    _prior(conn, "swap:raybot:%s:owner:so1mint" % _sig(2), "buy", 300.0, "2026-07-15T12:00:30Z")
    _prior(conn, "swap:00000002-uuid", "buy", 300.9, "2026-07-15T11:59:50Z")
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) == 0.0


def test_the_promotion_lane_hands_the_judged_size_to_the_same_read(monkeypatch):
    """One evaluator, two callers: the shadow's prior must pair the judged trade as funded does."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "promo_loop", Path(__file__).resolve().parents[1] / "scripts" / "memecoin_promotion_lane_loop.py")
    loop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loop)
    conn = _conn()
    _prior(conn, "swap:b77fc7f2-uuid", "buy", 269.62, "2026-07-15T11:59:51Z")
    attempt = {"attempt_id": "a1", "source_user_id": "u1", "network_id": "solana", "mint": "So1Mint",
               "origin_event_id": "onchain:" + SIG, "source_created_at": "2026-07-15T11:59:55+00:00"}
    fake = SimpleNamespace(MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S=1800.0)
    assert loop.source_prior_net(conn, attempt, cfg_module=fake, judged_usd=270.0) == 0.0
    assert loop.source_prior_net(conn, attempt, cfg_module=fake) == 269.62, "no size, nothing pairs"
    src = Path(loop.__file__).read_text()
    assert "judged_usd=source_size)" in src


def test_both_reads_use_an_index_range():
    """Fill path: each read is a range on an index, never a scan of the wallet's history."""
    conn = _conn()
    seen = []
    conn.set_trace_callback(seen.append)
    _prior_net(conn, "onchain:" + _sig(9), usd=50.0)
    conn.set_trace_callback(None)
    events = next(q for q in seen if "FROM memecoin_events" in q)
    dets = next(q for q in seen if "FROM onchain_wallet_detections" in q)
    plan = lambda q: " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + q))      # noqa: E731
    assert "idx_mc_events_user_time (source_user_id=? AND created_at>? AND created_at<?)" in plan(events)
    assert ("idx_mc_onchain_detect_wallet_coin (source_user_id=? AND mint=? AND detected_at>? AND detected_at<?)"
            in plan(dets))
    assert len([q for q in seen if q.lstrip().upper().startswith("SELECT")]) == 2, "two reads, no more"


def test_an_unreadable_history_is_none_not_zero():
    conn = _conn()
    conn.execute("DROP TABLE onchain_wallet_detections")
    assert _prior_net(conn, "onchain:" + _sig(9), usd=50.0) is None


# --- the absolute floor judges the trader's NET, like the ratio (Alpha/operator 2026-10-02) -----
# degen_ak AGNET 4663: $498 at 09:05:25Z filled; his $100 add-on at 09:14:22Z was refused "source buy
# $100 under absolute floor $197" while the ratio leg of the SAME gate judged $100 + $498 = $598.

def _agnet(monkeypatch, *, ratio=0.005, window=1800.0):
    _ratio_floor(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 197.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", ratio, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", window)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT", 2.0, raising=False)


def test_the_absolute_floor_judges_the_net_the_ratio_judges(monkeypatch):
    _agnet(monkeypatch)
    conn = _conn()
    _prior(conn, "e0", "buy", 498.42, "2026-07-15T11:51:00Z")          # 9 min earlier
    res, reached = _run(conn, _event(99.62, market_cap=44_587.8))      # ratio floor 0.5% = $223
    assert res["status"] != "rejected", res
    assert reached == ["So1Mint"]


def test_with_the_ratio_family_off_the_floor_still_judges_the_net(monkeypatch):
    _agnet(monkeypatch, ratio=0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0, raising=False)
    conn = _conn()
    _prior(conn, "e0", "buy", 498.42, "2026-07-15T11:51:00Z")
    res, reached = _run(conn, _event(99.62, market_cap=44_587.8))
    assert res["status"] != "rejected" and reached == ["So1Mint"], res


def test_an_unknown_mcap_buy_judges_the_net_at_the_absolute_floor(monkeypatch):
    _agnet(monkeypatch)
    conn = _conn()
    _prior(conn, "e0", "buy", 498.42, "2026-07-15T11:51:00Z")
    res, reached = _run(conn, _event(100.0, market_cap=None))           # unknown-mcap floor $100
    assert res["status"] != "rejected" and reached == ["So1Mint"], res


@pytest.mark.parametrize("prior_side, prior_usd", [("sell", 498.42), (None, None)])
def test_no_positive_prior_net_judges_the_single_buy(monkeypatch, prior_side, prior_usd):
    _agnet(monkeypatch, ratio=0.0)                 # ratio off: the band is the leg that answers
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0, raising=False)
    conn = _conn()
    if prior_side:
        _prior(conn, "e0", prior_side, prior_usd, "2026-07-15T11:51:00Z")   # net prior <= 0
    res, reached = _run(conn, _event(99.62, market_cap=44_587.8))
    assert res["status"] == "rejected" and reached == []
    assert "source buy $100 under absolute floor $197" in res["reason"], res


def test_a_floor_refusal_on_the_net_names_both_parts(monkeypatch):
    _agnet(monkeypatch, ratio=0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0, raising=False)
    conn = _conn()
    _prior(conn, "e0", "buy", 50.0, "2026-07-15T11:51:00Z")
    res, _reached = _run(conn, _event(99.62, market_cap=44_587.8))
    assert res["status"] == "rejected"
    assert "source buy $100 + $50 earlier = $150 under absolute floor $197" in res["reason"], res
    from sniper.services.memecoin_refusal_capture import classify_gate
    assert classify_gate(res["reason"]) == "source_buy_abs_floor"


def test_the_ceiling_stays_on_the_single_buy(monkeypatch):
    _agnet(monkeypatch, ratio=0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 500.0, raising=False)
    conn = _conn()
    _prior(conn, "e0", "buy", 498.42, "2026-07-15T11:51:00Z")
    res, reached = _run(conn, _event(400.0, market_cap=44_587.8))       # net $898 > $500 ceiling
    assert res["status"] != "rejected" and reached == ["So1Mint"], res


def test_no_extra_ledger_read_when_the_single_buy_clears_and_the_ratio_is_off(monkeypatch):
    """Fill-path cost: the prior-net read (p50 0.13 ms, p90 0.25 ms on 202 production traces,
    2026-10-01/02) is added only for a buy the absolute floor would refuse on its own."""
    _agnet(monkeypatch, ratio=0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0, raising=False)
    # The add-on LABEL (memecoin_addon_label) reads the prior net at attempt finish, after the funded
    # result; this pin is about the decision path, so the label is off here.
    monkeypatch.setattr(cfg, "MEMECOIN_ADDON_LABEL", False, raising=False)
    monkeypatch.setattr(m, "source_prior_net_buy_usd",
                        lambda *a, **k: pytest.fail("prior-net read for a buy that clears alone"))
    res, reached = _run(_conn(), _event(300.0, market_cap=44_587.8))
    assert res["status"] != "rejected" and reached == ["So1Mint"]


def test_the_band_function_only_ever_raises_what_the_floor_judges():
    from sniper.services.memecoin_real_runner import source_buy_band_reason as band
    assert band(99.62, 197.0, 0.0, 2.0, judged_usd=598.04) is None
    assert band(99.62, 197.0, 0.0, 2.0, judged_usd=150.0) == \
        "source buy $100 + $50 earlier = $150 under absolute floor $197"
    assert band(99.62, 197.0, 0.0, 2.0, judged_usd=50.0) == "source buy $100 under absolute floor $197"
    assert band(None, 197.0, 0.0, 2.0, judged_usd=598.0) is None             # unknown still passes
    assert band(400.0, 0.0, 500.0, 0.0, judged_usd=898.0) is None            # ceiling: single buy


def test_a_net_sell_never_lowers_what_the_ratio_floor_judges(monkeypatch):
    """Only ever raises: a prior net <= 0 judges the single buy at BOTH floors, never less."""
    _agnet(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0.0, raising=False)
    conn = _conn()
    _prior(conn, "e0", "sell", 80.0, "2026-07-15T11:51:00Z")          # prior net -$80
    res, reached = _run(conn, _event(100.0, market_cap=10_000.0))     # ratio floor 0.5% = $50
    assert res["status"] != "rejected" and reached == ["So1Mint"], res
