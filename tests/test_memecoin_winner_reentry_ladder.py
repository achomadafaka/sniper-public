"""Winner re-entry size ladder (spec 2026-09-28-memecoin-winner-reentry-size-ladder-design.md).

Operator order via Alpha, 2026-09-28: re-enter a coin we closed at a profit, each re-entry sized
down: the 1st at 0.5x the clip, the 2nd at 0.25x, no 3rd inside the window. The ladder rides the
funded risk_scale that the market regime already uses, so these tests assert the SIZE the
reservation holds, not a helper's return value.

The clip is a fixed $100 so that neither step lands on the $5 floor: at the conftest's $10 clip,
0.5x and 0.25x would both read $5 and the floor would hide a missing ladder.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as er
from sniper.services import memecoin_risk as r
from sniper.services.memecoin_market_state import classify_funded_entry

NOW = "2026-09-28T20:00:00+00:00"
MINT = "Coin11111111111111111111111111111111111111"
EVM_MINT = "0x1111111111111111111111111111111111111111"
CLIP = 100.0


@pytest.fixture
def conn(monkeypatch):
    cfg = r.cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", CLIP)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 5.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 10_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_REENTRY_COOLDOWN_S", 86400.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BLOCK_LOSING_REENTRY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REENTRY_WINNER_UNBLOCKED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REENTRY_WINNER_SIZE_LADDER", "0.5,0.25")
    monkeypatch.setattr(cfg, "MEMECOIN_REENTRY_WINNER_CHAINS", "solana")
    # An empty test ledger reads as an unknown market regime (scale 0.75). Neutral here, so a size
    # is the ladder's alone; test_the_ladder_multiplies_the_regime_scale covers the product.
    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    monkeypatch.setattr(r, "real_utc_now_iso", lambda: NOW)
    monkeypatch.setattr(er, "real_utc_now_iso", lambda: NOW)
    # A module constant built from os.getenv is empty under pytest: prove the patch is live.
    assert cfg.MEMECOIN_REENTRY_WINNER_SIZE_LADDER == "0.5,0.25"
    assert cfg.winner_reentry_chain_on("solana") and not cfg.winner_reentry_chain_on("4663")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _close(c, *, pnl, closed_at, mint=MINT, network="solana"):
    c.execute(
        "INSERT INTO memecoin_positions(mint, network_id, execution_mode, is_open, opened_at, "
        "closed_at, realized_pnl, entry_notional) VALUES (?,?,'real',0,?,?,?,100.0)",
        (mint, network, closed_at, closed_at, pnl))
    c.commit()


def _classify(c, *, mint=MINT, network="solana", signal_source="follow_activity"):
    return classify_funded_entry(
        c, network_id=network, mint=mint, decision_at=NOW, signal_source=signal_source,
        effective_mcap=100_000.0, assumed_micro=False, market_stats={}, base_clip_usd=None,
        consensus_n_60m=0)


def _reserve(c, classification, *, attempt="att-1", mint=MINT):
    m.start_entry_attempt(c, attempt_id=attempt, decision_started_at=NOW,
                          network_id="solana", mint=mint)
    return r.reserve_entry(
        c, mint=mint, source_user_id="u1", strategy="control", attempt_id=attempt,
        day=NOW[:10], usdc_balance=1000.0, sol_balance=1.0, now_iso=NOW,
        classification=classification)


# ---------------------------------------------------------------- sizes the reservation holds

def test_a_first_entry_keeps_the_full_clip_and_no_reentry_facts(conn):
    cl = _classify(conn)
    assert cl.risk_scale == 1.0
    assert "reentry_rank" not in cl.facts
    dec, pid = _reserve(conn, cl)
    assert dec.allowed and pid is not None
    assert m.pending_totals(conn) == (1, CLIP)


def test_the_first_reentry_after_a_win_reserves_half_the_clip(conn):
    _close(conn, pnl=12.0, closed_at="2026-09-28T19:00:00")
    cl = _classify(conn)
    assert cl.facts["reentry_rank"] == 1 and cl.facts["reentry_factor"] == 0.5
    assert cl.risk_scale == 0.5
    dec, pid = _reserve(conn, cl)
    assert dec.allowed and pid is not None, dec.reason
    assert m.pending_totals(conn) == (1, 50.0)


def test_the_second_reentry_reserves_a_quarter(conn):
    _close(conn, pnl=12.0, closed_at="2026-09-28T17:00:00")
    _close(conn, pnl=3.0, closed_at="2026-09-28T19:00:00")
    cl = _classify(conn)
    assert cl.facts["reentry_rank"] == 2 and cl.risk_scale == 0.25
    dec, pid = _reserve(conn, cl)
    assert dec.allowed and pid is not None, dec.reason
    assert m.pending_totals(conn) == (1, 25.0)


def test_a_third_reentry_is_refused_and_reserves_nothing(conn):
    for at in ("2026-09-28T15:00:00", "2026-09-28T17:00:00", "2026-09-28T19:00:00"):
        _close(conn, pnl=5.0, closed_at=at)
    dec, pid = _reserve(conn, _classify(conn))
    assert not dec.allowed and pid is None
    assert "winner re-entry ladder exhausted" in dec.reason
    assert m.pending_totals(conn) == (0, 0.0)


def test_the_ladder_multiplies_the_regime_scale(conn, monkeypatch):
    monkeypatch.setattr(r.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 0.75)
    _close(conn, pnl=12.0, closed_at="2026-09-28T19:00:00")
    cl = _classify(conn)
    assert cl.market_regime != "normal" and cl.risk_scale == 0.375
    dec, pid = _reserve(conn, cl)
    assert dec.allowed and pid is not None, dec.reason
    assert m.pending_totals(conn) == (1, 37.5)


def test_a_close_outside_the_window_does_not_count(conn):
    _close(conn, pnl=5.0, closed_at="2026-09-27T19:00:00")       # 25 h before NOW
    _close(conn, pnl=5.0, closed_at="2026-09-28T19:00:00")
    cl = _classify(conn)
    assert cl.facts["reentry_rank"] == 1 and cl.risk_scale == 0.5


def test_a_losing_last_close_stays_refused(conn):
    _close(conn, pnl=12.0, closed_at="2026-09-28T17:00:00")
    _close(conn, pnl=-4.0, closed_at="2026-09-28T19:00:00")
    cl = _classify(conn)
    assert "reentry_rank" not in cl.facts
    dec, pid = _reserve(conn, cl)
    assert not dec.allowed and pid is None
    assert "cooldown" in dec.reason or "losing re-entry" in dec.reason


@pytest.mark.parametrize("pnl", [0.0, None])
def test_breakeven_or_unknown_is_not_a_win(conn, pnl):
    _close(conn, pnl=pnl, closed_at="2026-09-28T19:00:00")
    cl = _classify(conn)
    assert "reentry_rank" not in cl.facts
    dec, _ = _reserve(conn, cl)
    assert not dec.allowed and "cooldown" in dec.reason


def test_a_manual_signal_is_never_scaled(conn):
    _close(conn, pnl=12.0, closed_at="2026-09-28T19:00:00")
    cl = _classify(conn, signal_source="manual")
    assert cl.risk_scale == 1.0 and "reentry_rank" not in cl.facts


def test_a_malformed_ladder_refuses_and_never_sizes_full(conn, monkeypatch):
    monkeypatch.setattr(r.cfg, "MEMECOIN_REENTRY_WINNER_SIZE_LADDER", "0.5,abc")
    _close(conn, pnl=12.0, closed_at="2026-09-28T19:00:00")
    dec, pid = _reserve(conn, _classify(conn))
    assert not dec.allowed and pid is None
    assert "winner re-entry ladder exhausted" in dec.reason


def test_a_close_between_classification_and_reservation_refuses(conn):
    """The rank is re-read under the reservation lock. A held position on the coin that closes
    after classification would otherwise let the re-entry reserve one ladder step too big."""
    _close(conn, pnl=12.0, closed_at="2026-09-28T17:00:00")
    cl = _classify(conn)
    assert cl.risk_scale == 0.5
    _close(conn, pnl=3.0, closed_at="2026-09-28T19:59:00")
    dec, pid = _reserve(conn, cl)
    assert not dec.allowed and pid is None
    assert "rank changed" in dec.reason


# ---------------------------------------------------------------- off means today

def test_the_knob_off_keeps_todays_cooldown(conn, monkeypatch):
    monkeypatch.setattr(r.cfg, "MEMECOIN_REENTRY_WINNER_UNBLOCKED", False)
    _close(conn, pnl=12.0, closed_at="2026-09-28T19:00:00")
    cl = _classify(conn)
    assert cl.risk_scale == 1.0 and "reentry_rank" not in cl.facts
    dec, pid = _reserve(conn, cl)
    assert not dec.allowed and pid is None
    assert dec.reason == "re-entry cooldown: a real position on this coin closed within the window"


def test_an_unset_ladder_keeps_the_old_full_size_winner_reentry(conn, monkeypatch):
    monkeypatch.setattr(r.cfg, "MEMECOIN_REENTRY_WINNER_SIZE_LADDER", "")
    _close(conn, pnl=12.0, closed_at="2026-09-28T19:00:00")
    cl = _classify(conn)
    assert cl.risk_scale == 1.0
    assert cl.facts["reentry_rank"] == 1 and cl.facts["reentry_factor"] == 1.0
    dec, pid = _reserve(conn, cl)
    assert dec.allowed and pid is not None
    assert m.pending_totals(conn) == (1, CLIP)


# ---------------------------------------------------------------- chain scope (amendment A)

def test_a_chain_outside_the_scope_keeps_todays_cooldown(conn):
    """MEMECOIN_REENTRY_WINNER_CHAINS=solana: a 4663 winner is cooled exactly as today."""
    _close(conn, pnl=12.0, closed_at="2026-09-28T19:00:00", mint=EVM_MINT, network="4663")
    assert m.coin_identity("4663", EVM_MINT) in m.recently_closed_real_coin_ids(conn, NOW, 86400)
    cl = _classify(conn, mint=EVM_MINT, network="4663")
    assert cl.risk_scale == 1.0 and "reentry_rank" not in cl.facts
    m.start_entry_attempt(conn, attempt_id="att-evm", decision_started_at=NOW,
                          network_id="4663", mint=EVM_MINT)
    dec, pid = er.reserve_entry_evm(
        conn, mint=EVM_MINT, source_user_id="u1", strategy="control", network_id="4663",
        day=NOW[:10], usdc_balance=1000.0, native_balance=1.0, now_iso=NOW,
        attempt_id="att-evm", classification=cl)
    assert not dec.allowed and pid is None
    assert dec.reason == "re-entry cooldown: a real position on this coin closed within the window"


def test_an_empty_scope_is_no_chain(conn, monkeypatch):
    monkeypatch.setattr(r.cfg, "MEMECOIN_REENTRY_WINNER_CHAINS", "")
    _close(conn, pnl=12.0, closed_at="2026-09-28T19:00:00")
    assert m.coin_identity("solana", MINT) in m.recently_closed_real_coin_ids(conn, NOW, 86400)
    assert "reentry_rank" not in _classify(conn).facts


# ---------------------------------------------------------------- EVM and the fill path

def test_an_evm_reentry_reserves_half_the_clip(conn, monkeypatch):
    monkeypatch.setattr(r.cfg, "MEMECOIN_REENTRY_WINNER_CHAINS", "solana,robin")
    _close(conn, pnl=12.0, closed_at="2026-09-28T19:00:00", mint=EVM_MINT, network="4663")
    cl = _classify(conn, mint=EVM_MINT, network="4663")
    assert cl.risk_scale == 0.5
    m.start_entry_attempt(conn, attempt_id="att-evm", decision_started_at=NOW,
                          network_id="4663", mint=EVM_MINT)
    dec, pid = er.reserve_entry_evm(
        conn, mint=EVM_MINT, source_user_id="u1", strategy="control", network_id="4663",
        day=NOW[:10], usdc_balance=1000.0, native_balance=1.0, now_iso=NOW,
        attempt_id="att-evm", classification=cl)
    assert dec.allowed and pid is not None, dec.reason
    assert m.pending_totals(conn) == (1, 50.0)


def test_an_evm_reentry_past_the_ladder_is_refused_under_the_lock(conn, monkeypatch):
    monkeypatch.setattr(r.cfg, "MEMECOIN_REENTRY_WINNER_CHAINS", "solana,4663")
    for at in ("2026-09-28T15:00:00", "2026-09-28T17:00:00", "2026-09-28T19:00:00"):
        _close(conn, pnl=5.0, closed_at=at, mint=EVM_MINT, network="4663")
    cl = _classify(conn, mint=EVM_MINT, network="4663")
    m.start_entry_attempt(conn, attempt_id="att-evm", decision_started_at=NOW,
                          network_id="4663", mint=EVM_MINT)
    dec, pid = er.reserve_entry_evm(
        conn, mint=EVM_MINT, source_user_id="u1", strategy="control", network_id="4663",
        day=NOW[:10], usdc_balance=1000.0, native_balance=1.0, now_iso=NOW,
        attempt_id="att-evm", classification=cl)
    assert not dec.allowed and pid is None
    assert "winner re-entry ladder exhausted" in dec.reason
    assert m.pending_totals(conn) == (0, 0.0)


def test_the_rank_query_uses_the_closed_at_index(conn):
    """It runs on every funded classification once the knob is on. A scan here would put the
    whole real-position table on the fill path."""
    sql = []
    conn.set_trace_callback(sql.append)
    m.winner_reentry_rank(conn, "solana", MINT, NOW, 86400.0)
    conn.set_trace_callback(None)
    stmt = next(s for s in sql if "FROM memecoin_positions" in s)
    plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + stmt))
    assert "idx_mc_pos_mode_open_closed" in plan and "closed_at>?" in plan, plan


# ---------------------------------------------------------------- cooldown-exempt wallet (Lambda F2)

@pytest.mark.parametrize("ladder", ["0.5,0.25", "0.5,abc"])
def test_a_cooldown_exempt_wallet_cannot_reenter_at_full_size(conn, monkeypatch, ladder):
    """An exempt wallet reserves with include_reentry_policy=False. The ladder refusal must still
    run, or an exhausted or malformed ladder leaves the clip unscaled and the wallet re-enters at
    full size."""
    monkeypatch.setattr(r.cfg, "MEMECOIN_REENTRY_WINNER_SIZE_LADDER", ladder)
    for at in ("2026-09-28T15:00:00", "2026-09-28T17:00:00", "2026-09-28T19:00:00"):
        _close(conn, pnl=5.0, closed_at=at)
    cl = _classify(conn)
    assert cl.risk_scale == 1.0                     # exhausted/malformed: nothing to scale by
    m.start_entry_attempt(conn, attempt_id="att-x", decision_started_at=NOW,
                          network_id="solana", mint=MINT)
    dec, pid = r.reserve_entry(
        conn, mint=MINT, source_user_id="u1", strategy="control", attempt_id="att-x",
        day=NOW[:10], usdc_balance=1000.0, sol_balance=1.0, now_iso=NOW,
        classification=cl, include_reentry_policy=False)
    assert not dec.allowed and pid is None
    assert "winner re-entry ladder exhausted" in dec.reason


# ---------------------------------------------------------------- early quote (Lambda F4)

def test_the_early_quote_clip_is_the_ladder_clip(monkeypatch, tmp_path):
    """Rank 1: the early quote is sized at the clip the reservation approves, not the full one."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from sniper.services import memecoin_market_state as MS
    from sniper.services import memecoin_real_runner as R
    from test_memecoin_entry_identity import _arm
    cfg = r.cfg
    _arm(monkeypatch)
    for name, value in (("MEMECOIN_REAL_SIZING", "fixed"), ("MEMECOIN_TRADE_SIZE_USD", CLIP),
                        ("MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 10_000.0),
                        ("MEMECOIN_REAL_REENTRY_COOLDOWN_S", 86400.0),
                        ("MEMECOIN_REENTRY_WINNER_UNBLOCKED", True),
                        ("MEMECOIN_REENTRY_WINNER_SIZE_LADDER", "0.5,0.25"),
                        ("MEMECOIN_REENTRY_WINNER_CHAINS", "solana")):
        monkeypatch.setattr(cfg, name, value)
    monkeypatch.setattr(MS, "market_risk_scale", lambda *a, **k: 1.0)
    path = str(tmp_path / "ledger.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    an_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    _close(c, pnl=12.0, closed_at=an_hour_ago)
    pre = SimpleNamespace(balances=lambda: (1000.0, 1.0, False, 5.0))
    clip, why = R._early_quote_clip(SimpleNamespace(mint=MINT, signal_source="j7"), pre, path)
    assert why is None and clip == 50.0, (clip, why)


# ---------------------------------------------------------------- stale mirror (amendment B)

ORIGIN_BUY = "2026-09-28T19:59:58+00:00"          # the round-2 buy the re-entry copied


def _open_reentry(c, *, rank, opened_at="2026-09-28T20:00:00+00:00", attempt="att-re"):
    import json
    m.start_entry_attempt(c, attempt_id=attempt, decision_started_at=opened_at,
                          network_id="solana", mint=MINT, source_created_at=ORIGIN_BUY)
    contract = {"classification": {"facts": ({"reentry_rank": rank} if rank else {})}}
    c.execute(
        "INSERT INTO memecoin_positions(source_user_id, mint, network_id, strategy, "
        "execution_mode, is_open, opened_at, entry_notional, funded_contract_json, attempt_id) "
        "VALUES ('u1', ?, 'solana', ?, 'real', 1, ?, 50.0, ?, ?)",
        (MINT, r.cfg.MEMECOIN_REAL_POLICY, opened_at, json.dumps(contract), attempt))
    c.commit()
    return dict(c.execute("SELECT * FROM memecoin_positions WHERE attempt_id=?",
                          (attempt,)).fetchone())


class _Sell:
    side = "sell"
    source_user_id = "u1"
    mint = MINT
    network_id = "solana"
    fomo_event_id = "sell-1"

    def __init__(self, created_at):
        self.created_at = created_at


def test_a_late_delivered_sell_from_the_earlier_round_does_not_mirror_a_reentry(conn):
    from sniper.services.memecoin_real_runner import real_mirror_on_sell
    _open_reentry(conn, rank=1)
    res = real_mirror_on_sell(conn, _Sell("2026-09-28T19:55:00+00:00"), {}, now_fn=lambda: NOW)
    assert res["status"] == "stale_sell", res
    assert "predates the re-entry" in res["reason"]


def test_a_round2_sell_after_the_origin_buy_still_mirrors(conn):
    """Lambda F1: the source bought round 2 at 19:59:58 and sold at 19:59:59, before our 20:00:00
    decision start. That sell belongs to OUR round and must mirror."""
    from sniper.services.memecoin_real_runner import _stale_sell_for_reentry
    pos = _open_reentry(conn, rank=1)
    assert _stale_sell_for_reentry(conn, pos, _Sell("2026-09-28T19:59:59+00:00")) is None


def test_a_first_entry_and_unreadable_times_keep_the_mirror(conn):
    from sniper.services.memecoin_real_runner import _stale_sell_for_reentry
    reentry = _open_reentry(conn, rank=1)
    first = dict(reentry, funded_contract_json='{"classification": {"facts": {}}}')
    assert _stale_sell_for_reentry(conn, first, _Sell("2026-09-28T19:00:00+00:00")) is None
    assert _stale_sell_for_reentry(conn, reentry, _Sell(None)) is None
    # A naive stamp is read as UTC.
    assert _stale_sell_for_reentry(conn, reentry, _Sell("2026-09-28T19:55:00")) is not None
    orphan = dict(reentry, attempt_id="att-missing")
    assert _stale_sell_for_reentry(conn, orphan, _Sell("2026-09-28T19:55:00+00:00")) is None
