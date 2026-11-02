"""Paper->real translation gates (2026-07-16).

14d of matched arm data showed real losing on the SAME coins paper control won: every fresh-coin
real Solana entry was 25-262 min after the coin's first feed event at +101% to +986% above its
first-seen price (the armed-wallet trigger structurally fires on the 3rd+ buyer), sequential
re-entries bled (SOLdiers x3 = -$66), and the +60% full TP capped the only fat tail (TrumpCoin
+$27 real vs +$543 control ride). Four gates fix this without widening entry breadth (coins real
skipped were net LOSERS on control: -$918/87 — the allowlist SELECTS well, it just times badly):

1. FIRST-SEEN chase guard — refuse a copy when the coin already ran a ratio past the price at
   which OUR OWN feed first showed it (the existing chase guard only compares vs the copied
   event's price, which is blind to copying a late buyer).
2. RE-ENTRY COOLDOWN — one real campaign per coin per window (07-09 study: no re-entry edge).
3. TP-vs-DERISK-RIDE A/B — half of real fills replace the full +60% TP with a +60% derisk
   (recover entry, ride the remainder under stop/mirror/timecap). Read at 30-50 closes per arm.
4. DELAYED-CATCH alert suppression — stale follow-activity Telegram noise off by default; the
   front-run alert refuses to fire on signals older than MEMECOIN_TG_MAX_LAG_S.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_evm_risk as evm_risk
from sniper.services import memecoin_follow_activity as fa
from sniper.services import memecoin_runner as runner
from sniper.settings import memecoin as cfg


@pytest.fixture(autouse=True)
def _no_source_buy_floor(monkeypatch):
    """Pin the source-buy floor OFF for this module.

    Every event here carries their_size_usd = 100.0, and real_entry_hook refuses under the floor
    BEFORE it reaches the chase, cooldown and age gates these tests exercise. The fixture keeps
    this contract explicit for the module.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.0,
                        raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0,
                        raising=False)


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _feed_ev(fid, mint="So1mint", price=1.0, side="buy", network_id="solana",
             source="global_feed"):
    return {"fomo_event_id": fid, "source_user_id": "early", "source_handle": "early",
            "mint": mint, "ticker": "T", "side": side, "their_price": price,
            "their_market_cap": 500_000.0, "their_size_usd": 100.0,
            "created_at": "2026-07-15T13:00:00.000Z", "received_at": "2026-07-15T13:00:01.000Z",
            "lag_s": 1.0, "network_id": network_id, "signal_source": source}


class _EvObj:
    def __init__(self, fid="armed-1", uid="armed", mint="So1mint", network_id="solana",
                 side="buy", their_price=5.0, created_at="2026-07-15T13:30:00Z", lag_s=1.0):
        self.fomo_event_id = fid; self.source_user_id = uid; self.source_handle = uid
        self.mint = mint; self.ticker = "T"; self.side = side; self.network_id = network_id
        self.created_at = created_at; self.received_at = created_at
        self.their_market_cap = 500_000.0; self.their_price = their_price
        self.their_size_usd = 100.0; self.signal_source = "global_feed"; self.lag_s = lag_s


def _open_closed_real(c, *, mint="So1mint", network_id="solana", closed_at,
                      execution_mode="real"):
    pid = m.open_position(c, source_user_id="w1", source_handle="w1", mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="quote",
                          entry_lag_s=1.0, their_size_usd=100.0,
                          opened_at="2026-07-15T12:00:00Z", execution_mode=execution_mode,
                          network_id=network_id, strategy="alpha_b")
    m.close_position(c, pid, closed_at, -10.0, exit_reason="mirror")
    return pid


# ---------- 1. first-seen chase guard --------------------------------------------------------


def test_first_seen_buy_price_is_earliest_positive_buy_for_exact_coin_identity():
    c = _conn()
    m.insert_event(c, _feed_ev("e0", price=0.0))                       # non-positive: ignored
    m.insert_event(c, _feed_ev("e1", price=1.0))                       # <-- first real sighting
    m.insert_event(c, _feed_ev("e2", price=3.0))
    m.insert_event(c, _feed_ev("s1", price=0.5, side="sell"))          # sells never count
    m.insert_event(c, _feed_ev("o1", price=0.1, network_id="8453"))    # other chain: other coin
    assert m.first_seen_buy_price(c, "solana", "So1mint") == pytest.approx(1.0)
    assert m.first_seen_buy_price(c, "solana", "NoSuchMint") is None


def test_first_seen_buy_price_uses_normalized_legacy_network_identity():
    c = _conn()
    # Legacy Solana rows arrived both untagged and with the raw numeric network id. They are the
    # same coin identity and must anchor the later canonical `solana` lookup.
    m.insert_event(c, _feed_ev("null", mint="SoLegacy", price=1.0, network_id=None))
    m.insert_event(c, _feed_ev("raw", mint="SoLegacy", price=2.0,
                               network_id="1399811149"))
    m.insert_event(c, _feed_ev("tagged", mint="SoLegacy", price=4.0,
                               network_id="solana"))
    assert m.first_seen_buy_price(c, "solana", "SoLegacy") == pytest.approx(1.0)

    # Untagged 0x rows remain unresolved EVM. They are never guessed as a concrete chain.
    m.insert_event(c, _feed_ev("evm-null", mint="0xabc", price=3.0, network_id=None))
    assert m.first_seen_buy_price(c, "evm-unknown", "0xabc") == pytest.approx(3.0)


def test_first_seen_chase_ok_pure_semantics():
    ok = R.first_seen_chase_ok
    assert ok(first_price=1.0, price_now=1.9, max_ratio=2.0) is True
    assert ok(first_price=1.0, price_now=2.1, max_ratio=2.0) is False
    assert ok(first_price=1.0, price_now=9.9, max_ratio=2.0) is False
    # fail-OPEN on absent data and when the guard is disabled
    assert ok(first_price=None, price_now=5.0, max_ratio=2.0) is True
    assert ok(first_price=0.0, price_now=5.0, max_ratio=2.0) is True
    assert ok(first_price=1.0, price_now=None, max_ratio=2.0) is True
    assert ok(first_price=1.0, price_now=0.0, max_ratio=2.0) is True
    assert ok(first_price=1.0, price_now=9.9, max_ratio=0.0) is True


def test_hook_refuses_a_coin_that_ran_past_its_first_seen_price(monkeypatch):
    """The exact SOLdiers shape: armed wallet buys at 5x the first-seen price; our executable
    price matches THEIR price (so the per-event chase guard passes) — the first-seen guard is
    the only thing standing between us and buying the top."""
    c = _conn()
    m.insert_event(c, _feed_ev("e1", price=1.0))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 2.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 40.0)
    called = {}

    def _spy(*a, **k):
        called["hit"] = True
        return {"status": "rejected", "reason": "x"}

    monkeypatch.setattr(R, "real_entry", _spy)
    res = R.real_entry_hook(c, _EvObj(their_price=5.0), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
                            trade_size=50.0, max_age_s=3600.0,
                            stats_fn=lambda mint, network_id=None: {"price": 5.0})
    assert res["status"] == "skipped_chase"
    assert "first-seen" in res["reason"]
    assert "hit" not in called            # never reached the executor


def test_hook_fails_open_when_the_coin_was_never_seen_before(monkeypatch):
    c = _conn()   # no prior events at all
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 2.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 40.0)
    called = {}

    def _spy(*a, **k):
        called["hit"] = True
        return {"status": "rejected", "reason": "x"}

    monkeypatch.setattr(R, "real_entry", _spy)
    res = R.real_entry_hook(c, _EvObj(their_price=5.0), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
                            trade_size=50.0, max_age_s=3600.0,
                            stats_fn=lambda mint, network_id=None: {"price": 5.0})
    assert res["status"] == "rejected"    # passed the guard, refused later by the spy executor
    assert called.get("hit") is True


def test_fresh_follow_no_longer_bypasses_first_seen(monkeypatch):
    """The fresh-follow bypass is REMOVED (2026-08-23).

    It assumed a low-lag follow is an early copy of the COIN, so the coin-origin ratio could not
    matter. A fresh follow of the source wallet's OWN RE-BUY breaks that: their second buy resets
    the reference price the drift gate compares against, so the drift reads small and the gate
    opens at the top. Live, Veyr / sambaxbt, both fresh follows of the same wallet — refused at
    2.442e-05 (+35.5%), then FILLED at 5.608e-05 (+16.8%) seven minutes later, 3.11x the
    first-seen price, stopped out for -$17.64.

    Measured over 201 funded closes since 2026-08-01: the 8 entries above 3.0x averaged -$20.60
    (25% wins) against -$1.02 (44% wins) for the 193 at or under it. Four percent of the trades
    carried 45% of the loss.
    """
    c = _conn()
    m.insert_event(c, _feed_ev("first", price=0.1))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 3.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FRESH_FOLLOW_MAX_LAG_S", 60.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", 40.0, raising=False)
    called = {}
    monkeypatch.setattr(
        R, "real_entry",
        lambda *_a, **_k: called.setdefault("hit", True) and {
            "status": "rejected", "reason": "test seam"},
    )
    ev = _EvObj(their_price=1.0, lag_s=40.0)       # fresh: lag 40s is inside the 60s band
    ev.signal_source = "follow_activity"

    result = R.real_entry_hook(
        c, ev, {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        stats_fn=lambda _mint, network_id=None: {"price": 1.3},   # 13x the 0.1 first-seen price
    )

    assert result["status"] == "skipped_chase"
    assert "first-seen" in result["reason"]
    assert called.get("hit") is None                # never reached the executor


def test_follow_over_sixty_seconds_keeps_first_seen_guard(monkeypatch):
    c = _conn()
    m.insert_event(c, _feed_ev("first", price=0.1))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 3.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FRESH_FOLLOW_MAX_LAG_S", 60.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", 40.0, raising=False)
    ev = _EvObj(their_price=1.0, lag_s=61.0)
    ev.signal_source = "follow_activity"

    result = R.real_entry_hook(
        c, ev, {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        stats_fn=lambda _mint, network_id=None: {"price": 1.3},
    )

    assert result["status"] == "skipped_chase"
    assert "first-seen" in result["reason"]


# ---------- 2. re-entry cooldown --------------------------------------------------------------


def test_recently_closed_real_coin_ids_scopes_mode_window_and_chain():
    c = _conn()
    _open_closed_real(c, closed_at="2026-07-15T13:00:00Z")                       # 1h ago: in
    _open_closed_real(c, mint="OldMint", closed_at="2026-07-13T12:00:00Z")       # 2d ago: out
    _open_closed_real(c, mint="PaperMint", closed_at="2026-07-15T13:00:00Z",
                      execution_mode="paper")                                    # paper: out
    ids = m.recently_closed_real_coin_ids(c, "2026-07-15T14:00:00Z", 86400)
    assert m.coin_identity("solana", "So1mint") in ids
    assert m.coin_identity("solana", "OldMint") not in ids
    assert m.coin_identity("solana", "PaperMint") not in ids
    assert m.recently_closed_real_coin_ids(c, "2026-07-15T14:00:00Z", 0) == frozenset()


def _entry_cfg(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_REENTRY_COOLDOWN_S", 86400.0)


def _ago(seconds):
    """closed_at N seconds before the builders' clock (real_utc_now_iso — the corrected clock,
    which can differ from the host clock; using the same source keeps the window math honest)."""
    from datetime import datetime, timedelta
    from sniper.util.clock import real_utc_now_iso
    anchor = datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    return (anchor - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_solana_gate_rejects_reentry_inside_the_cooldown(monkeypatch):
    c = _conn()
    _open_closed_real(c, closed_at=_ago(3600))               # closed 1h ago
    _entry_cfg(monkeypatch)
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=300.0, sol_balance=5.0,
                                           day="2026-07-15")
    d = risk.memecoin_entry_allowed(ctx, mint="So1mint", trade_size=ctx.trade_size_usd)
    assert not d.allowed
    assert "re-entry cooldown" in d.reason


def test_solana_gate_allows_the_same_coin_after_the_window(monkeypatch):
    c = _conn()
    _open_closed_real(c, closed_at=_ago(2 * 86400))          # closed 2 days ago
    _entry_cfg(monkeypatch)
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=300.0, sol_balance=5.0,
                                           day="2026-07-15")
    d = risk.memecoin_entry_allowed(ctx, mint="So1mint", trade_size=ctx.trade_size_usd)
    assert "re-entry cooldown" not in (d.reason or "")


def test_evm_gate_rejects_reentry_inside_the_cooldown(monkeypatch):
    c = _conn()
    _open_closed_real(c, mint="0xdeadbeef", network_id="8453", closed_at=_ago(3600))
    _entry_cfg(monkeypatch)
    ctx = evm_risk.build_evm_risk_context(c, usdc_balance=300.0, native_balance=1.0,
                                          day="2026-07-15", network_id="8453")
    d = evm_risk.evm_entry_allowed(ctx, mint="0xdeadbeef", trade_size=ctx.trade_size_usd)
    assert not d.allowed
    assert "re-entry cooldown" in d.reason


# ---------- 3. TP-vs-derisk-ride A/B ----------------------------------------------------------


def test_exit_override_round_trips_derisk_pct():
    c = _conn()
    m.set_position_exit_override(c, 7, "ride", tp_pct=0.0, derisk_pct=60.0,
                                 actor="system", note="tp-ab")
    ov = m.effective_exit_override(c, 7)
    assert ov["derisk_pct"] == pytest.approx(60.0)
    assert ov["tp_pct"] == pytest.approx(0.0)


def test_ensure_tables_adds_derisk_pct_to_a_pre_existing_overrides_table():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE memecoin_position_exit_overrides(
        id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL, source TEXT NOT NULL,
        tp_pct REAL, tp_target_mcap REAL, sl_pct REAL, sl_target_mcap REAL, trail_pct REAL,
        actor TEXT, note TEXT, active INTEGER DEFAULT 1, created_at TEXT, resolved_at TEXT)""")
    m.ensure_memecoin_tables(c)
    cols = [r[1] for r in c.execute("PRAGMA table_info(memecoin_position_exit_overrides)")]
    assert "derisk_pct" in cols


def _pos(mid_pct_gain, derisked=False):
    return {"id": 1, "entry_price": 1.0, "entry_notional": 50.0, "network_id": "solana",
            "opened_at": "2026-07-15T12:00:00Z",
            "derisked_at": "2026-07-15T12:30:00Z" if derisked else None,
            "stuck_bag": 0, "last_quote_ts": None}


def test_ride_override_derisks_at_the_tp_threshold_instead_of_full_exit(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 604800.0)
    ov = {"tp_pct": 0.0, "derisk_pct": 60.0}
    now = "2026-07-15T13:00:00Z"
    assert R.alpha_b_real_exit_reason(_pos(70), 1.7, now, override=ov) == "derisk"
    assert R.alpha_b_real_exit_reason(_pos(70), 1.7, now) == "real_tp"       # default arm
    # once derisked, the remainder rides — no second derisk, no TP resurrection
    assert R.alpha_b_real_exit_reason(_pos(70, derisked=True), 1.7, now, override=ov) is None


# tp-vs-derisk-ride A/B tests removed 2026-07-21: the A/B closed (enrollment structurally ended
# at the funded-contract cutover; 12/30 ride closes accrued — insufficient n, no verdict).
# Historical 'ride' rows keep their priority slot; see test_memecoin_micro_policy for coverage.


# ---------- 4. alert hygiene ------------------------------------------------------------------


def test_prepare_alert_refuses_stale_signals(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MAX_LAG_S", 60.0)
    c = _conn()
    assert runner._prepare_alert(c, _EvObj(fid="stale-1", lag_s=300.0)) is None
    fresh = runner._prepare_alert(c, _EvObj(fid="fresh-1", lag_s=2.0))
    assert fresh is not None
    unknown = runner._prepare_alert(c, _EvObj(fid="unk-1", lag_s=None))
    assert unknown is not None            # unknown lag: fail-open, labeled by the message builder


def _front_run_message(c, uid):
    job = runner._prepare_alert(c, _EvObj(fid="fr-" + uid, uid=uid, lag_s=1.0))
    assert job is not None
    return job["message"]


def test_front_run_alert_for_a_watch_only_wallet_states_the_refusal_not_a_pending_decision(
        monkeypatch):
    """2026-09-11 Faucet/Tekkerrss: the front-run alert said "our entry decision pending" for a
    wallet armed nowhere. The roster gate refused it `not_approved` 25 ms later, and a
    `not_approved` result pages nothing, so "pending" was the last word and it was false. The
    verdict is fixed by the same read the gate makes, so the alert must state it."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", True)
    c = _conn()
    m.add_wallet(c, "watcher", "watcher")
    msg = _front_run_message(c, "watcher")
    assert "decision pending" not in msg
    assert "REFUSED" in msg and "not_approved" in msg
    assert "not armed on solana" in msg


def test_front_run_alert_for_an_armed_wallet_reports_receipt(monkeypatch):
    """A source notice does not report a later entry result."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", True)
    c = _conn()
    m.add_wallet(c, "armed", "armed")
    m.set_wallet_real_approved(c, "armed", True, chains=["solana"], force=True)
    msg = _front_run_message(c, "armed")
    assert "ARMED" in msg
    assert "Source buy received. This notice does not report our entry result." in msg


def test_front_run_alert_reports_receipt_when_the_roster_gate_is_off(monkeypatch):
    """Roster gate off means ANY wallet may fill, so watch-only decides nothing."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", False)
    c = _conn()
    m.add_wallet(c, "watcher", "watcher")
    msg = _front_run_message(c, "watcher")
    assert "Source buy received. This notice does not report our entry result." in msg
    assert "REFUSED" not in msg


def test_front_run_alert_names_no_roster_refusal_on_an_unfunded_chain(monkeypatch):
    """An unfunded chain refuses `unroutable_chain` before the roster gate is reached, so the
    alert must not claim `not_approved` there. `eth` is not a funded chain in the test env."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", True)
    c = _conn()
    m.add_wallet(c, "watcher", "watcher")
    job = runner._prepare_alert(c, _EvObj(fid="fr-eth", uid="watcher", network_id="1"))
    assert job is not None
    assert "not_approved" not in job["message"]


def test_front_run_alert_uses_the_gates_authority_read_not_the_raw_chain_list(monkeypatch):
    """`wallet_armed_chains` reads `real_chains` only. The gate also requires `real_approved=1`,
    `paused=0` and `excluded=0`. A paused wallet keeps its chain list, so the raw read called it
    ARMED while the gate refused it — and the refusal is silenced for a non-armed wallet."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", True)
    c = _conn()
    m.add_wallet(c, "paused", "paused")
    m.set_wallet_real_approved(c, "paused", True, chains=["solana"], force=True)
    c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id='paused'")
    assert m.wallet_armed_chains(c, "paused") == ["solana"]     # the raw read still says armed
    msg = _front_run_message(c, "paused")
    assert "ARMED" not in msg
    assert "REFUSED" in msg and "not_approved" in msg


def test_delayed_catch_alert_is_off_by_default(monkeypatch):
    stale = _EvObj(their_price=1.0, lag_s=300.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_DELAYED_CATCH_ENABLED", False)
    assert fa._should_follow_alert(stale, armed=True, min_usd=0.0) is False
    monkeypatch.setattr(cfg, "MEMECOIN_TG_DELAYED_CATCH_ENABLED", True)
    assert fa._should_follow_alert(stale, armed=True, min_usd=0.0) is True
    # FRESH armed catches (<=MEMECOIN_TG_MAX_LAG_S) alert regardless of the delayed toggle —
    # they are real-eligible (2026-07-16 fast-lane real leg)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_DELAYED_CATCH_ENABLED", False)
    assert fa._should_follow_alert(_EvObj(their_price=1.0, lag_s=2.0), armed=True,
                                   min_usd=0.0) is True


# ---------- alert coverage (2026-07-17: operator sees fomo UI signals missing from TG) --------


def test_front_run_alert_covers_consensus_buys_from_any_wallet(monkeypatch):
    """Curated buys always alert; NON-curated buys alert only as the 2nd+ distinct buyer on a
    sub-$2M coin within 60m — the 49%-hit20 consensus signal, never the 13.8% solo noise."""
    from sniper.services.memecoin_runner import want_front_run_alert
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_CONSENSUS_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_CONSENSUS_MAX_MCAP", 2_000_000.0)
    curated = {"cur1"}
    ev_cur = _EvObj(uid="cur1", their_price=1.0); ev_cur.their_market_cap = 50e6
    ev_non = _EvObj(uid="rando", their_price=1.0); ev_non.their_market_cap = 900_000.0
    ev_big = _EvObj(uid="rando", their_price=1.0); ev_big.their_market_cap = 30e6
    assert want_front_run_alert(ev_cur, curated, prior_fn=lambda ev: 0) is True
    assert want_front_run_alert(ev_non, curated, prior_fn=lambda ev: 1) is True   # consensus
    assert want_front_run_alert(ev_non, curated, prior_fn=lambda ev: 0) is False  # solo noise
    assert want_front_run_alert(ev_big, curated, prior_fn=lambda ev: 3) is False  # dead band
    monkeypatch.setattr(cfg, "MEMECOIN_TG_CONSENSUS_ALERTS", False)
    assert want_front_run_alert(ev_non, curated, prior_fn=lambda ev: 1) is False


def test_front_run_alert_ignores_sells_and_size_floor(monkeypatch):
    from sniper.services.memecoin_runner import want_front_run_alert
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 500.0)
    sell = _EvObj(uid="cur1"); sell.side = "sell"
    small = _EvObj(uid="cur1"); small.their_size_usd = 100.0
    assert want_front_run_alert(sell, {"cur1"}, prior_fn=lambda ev: 9) is False
    assert want_front_run_alert(small, {"cur1"}, prior_fn=lambda ev: 9) is False


# ---------- 5. revival volume guard (2026-08-17) ---------------------------------------------


def _no_chase(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)


def _classification_context(effective_mcap, *, assumed_micro=False):
    return {
        "effective_mcap": effective_mcap,
        "assumed_micro": assumed_micro,
        "consensus_n_60m": None,
        "market_stats": None,
    }


def _set_volume_scope(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", 0.01)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_M5", 0.10)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_TURNOVER_M5", 1.50)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 1_000_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_CEILING_MAX_MCAP_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", 0.03)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_M5_REQUIRED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 2)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TXNS_M5_REQUIRED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS", frozenset(), raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STRICT_VOLUME_MIN_MCAP_USD", 500_000.0,
                        raising=False)


def test_volume_gates_use_the_canonical_effective_mcap(monkeypatch):
    c = _conn()
    _no_chase(monkeypatch)
    _set_volume_scope(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_M5", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 1_000_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_TURNOVER_M5", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 0)
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        classification_context=_classification_context(2_000_000.0),
        stats_fn=lambda mint, network_id=None: {
            "price": 5.0, "market_cap": 500_000.0, "volume_h1": 6_000.0,
        })

    assert res["reason"].startswith("h1 volume $6000 under floor $20000")


@pytest.mark.parametrize("volume_h1,blocked", [(0.0, True), (10_000.0, False)])
def test_hourly_floor_applies_at_the_m5_boundary(monkeypatch, volume_h1, blocked):
    c = _conn()
    _no_chase(monkeypatch)
    _set_volume_scope(monkeypatch)
    called = {}
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: called.setdefault("hit", True) and {
        "status": "rejected", "reason": "test seam",
    })
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        classification_context=_classification_context(1_000_000.0),
        stats_fn=lambda mint, network_id=None: {
            "price": 5.0, "market_cap": 2_000_000.0, "volume_h1": volume_h1,
            "volume_m5": 4_000_000.0, "txns_m5_buys": 0, "txns_m5_sells": 0,
        })

    if blocked:
        assert res["reason"].startswith("h1 volume $0 under floor $10000")
        assert not called
    else:
        assert res["reason"] == "test seam"
        assert called["hit"] is True


def test_strict_wallet_uses_the_volume_floor_above_five_hundred_thousand(monkeypatch):
    c = _conn()
    _no_chase(monkeypatch)
    _set_volume_scope(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS", frozenset({"strict"}))
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: {
        "status": "rejected", "reason": "test seam",
    })

    res = R.real_entry_hook(
        c, _EvObj(uid="strict"), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        classification_context=_classification_context(750_000.0),
        stats_fn=lambda mint, network_id=None: {
            "price": 5.0, "market_cap": 750_000.0, "volume_h1": 30_000.0,
            "volume_m5": 1_000.0, "txns_m5_buys": 2, "txns_m5_sells": 1,
        })

    assert "m5_turnover_under_big_floor" in res["reason"]


def test_volume_ceiling_is_off_above_the_configured_band(monkeypatch):
    c = _conn()
    _no_chase(monkeypatch)
    _set_volume_scope(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_M5", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 0)
    called = {}
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: called.setdefault("hit", True) and {
        "status": "rejected", "reason": "test seam",
    })
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        classification_context=_classification_context(1_000_001.0),
        stats_fn=lambda mint, network_id=None: {
            "price": 5.0, "market_cap": 1_000_001.0, "volume_h1": 20_000.0,
            "volume_m5": 2_000_000.0,
        })

    assert res["reason"] == "test seam"
    assert called["hit"] is True


def test_volume_exemption_cannot_bypass_a_gate_above_one_million(monkeypatch):
    c = _conn()
    _no_chase(monkeypatch)
    _set_volume_scope(monkeypatch)
    monkeypatch.setattr(R, "band_is_reservable", lambda *a, **k: True)
    m.set_wallet_gate_exemption(
        c, "armed", "m5_turnover_under_floor", active=True,
        actor="armed", reason="small-cap override", now_iso="2026-07-15T13:00:00Z",
    )
    called = {}
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: called.setdefault("hit", True) and {
        "status": "rejected", "reason": "test seam",
    })
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        classification_context=_classification_context(2_000_000.0),
        stats_fn=lambda mint, network_id=None: {
            "price": 5.0, "market_cap": 2_000_000.0, "volume_h1": 30_000.0,
            "volume_m5": 1_000.0, "txns_m5_buys": 2, "txns_m5_sells": 1,
        })

    assert res["status"] == "rejected"
    assert "m5_turnover_under_big_floor" in res["reason"]
    assert "hit" not in called
    row = c.execute(
        "SELECT exemptions_applied FROM memecoin_entry_attempts ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert row[0] is None


def test_volume_gates_are_off_when_effective_mcap_is_unknown(monkeypatch):
    c = _conn()
    _no_chase(monkeypatch)
    _set_volume_scope(monkeypatch)
    called = {}
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: called.setdefault("hit", True) and {
        "status": "rejected", "reason": "test seam",
    })
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        classification_context=_classification_context(None, assumed_micro=True),
        stats_fn=lambda mint, network_id=None: {
            "price": 5.0, "market_cap": 20_000_000.0, "volume_h1": 0.0,
            "volume_m5": 100_000_000.0, "txns_m5_buys": 0, "txns_m5_sells": 0,
        })

    assert res["reason"] == "test seam"
    assert called["hit"] is True


def test_hook_refuses_a_priced_coin_with_dead_h1_volume(monkeypatch):
    """A coin DexScreener prices but shows under the h1 volume floor is a corpse being revived —
    our clip would be its only liquidity event."""
    c = _conn()
    _no_chase(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 5_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 1_000_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_TURNOVER_M5", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 0)
    called = {}
    monkeypatch.setattr(R, "real_entry",
                        lambda *a, **k: called.setdefault("hit", True) and
                        {"status": "rejected", "reason": "x"})
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        classification_context=_classification_context(1_500_000.0),
        stats_fn=lambda mint, network_id=None: {"price": 5.0, "volume_h1": 900.0})
    assert res["status"] == "rejected"
    assert res["reason"].endswith("(under_abs_floor)")
    assert "hit" not in called            # never reached the executor


def test_h1_refusal_reports_the_organic_volume_used_by_the_gate(monkeypatch):
    c = _conn()
    _no_chase(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", 0.01)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_M5", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 1_000_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_TURNOVER_M5", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", True)
    ev = _EvObj()
    ev.their_size_usd = 1_100.0

    res = R.real_entry_hook(
        c, ev, {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        classification_context=_classification_context(1_500_000.0),
        stats_fn=lambda mint, network_id=None: {
            "price": 5.0, "market_cap": 64_666.0, "volume_h1": 732.54,
        })

    assert res["reason"] == (
        "h1 organic volume $733 under floor $15000 (turnover_under_floor)")
    # 2026-09-12: $732.54 is below the $1,100 buy, so the buy is provably not in it and is not
    # subtracted (it used to clamp to "$0").


def test_volume_guard_fails_open_when_the_field_is_absent(monkeypatch):
    """An unindexed fresh pool has no volume data; absence must not block (chase-guard convention)."""
    c = _conn()
    _no_chase(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 5_000.0)
    called = {}
    monkeypatch.setattr(R, "real_entry",
                        lambda *a, **k: called.setdefault("hit", True) and
                        {"status": "rejected", "reason": "test seam"})
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        stats_fn=lambda mint, network_id=None: {"price": 5.0})
    assert res["status"] == "rejected" and res["reason"] == "test seam"
    assert called["hit"] is True


def test_volume_guard_off_is_inert(monkeypatch):
    c = _conn()
    _no_chase(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 0.0)
    called = {}
    monkeypatch.setattr(R, "real_entry",
                        lambda *a, **k: called.setdefault("hit", True) and
                        {"status": "rejected", "reason": "test seam"})
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        stats_fn=lambda mint, network_id=None: {"price": 5.0, "volume_h1": 1.0})
    assert res["status"] == "rejected" and res["reason"] == "test seam"
    assert called["hit"] is True


# ---------- 6. coin-age ceiling (2026-08-17, "Sorajiro no movement") -------------------------


def test_hook_refuses_an_old_coin(monkeypatch):
    """3-day evidence: <1h coins broke even; every older bucket lost (-$234, 32% wins)."""
    c = _conn()
    _no_chase(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_PAIR_AGE_S", 3600.0)
    called = {}
    monkeypatch.setattr(R, "real_entry",
                        lambda *a, **k: called.setdefault("hit", True) and
                        {"status": "rejected", "reason": "x"})
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        stats_fn=lambda mint, network_id=None: {"price": 5.0, "pair_age_s": 349142.0})
    assert res["status"] == "rejected"
    assert "no-movement class" in res["reason"]
    assert "hit" not in called


def test_age_ceiling_fails_open_on_unknown_age(monkeypatch):
    c = _conn()
    _no_chase(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_PAIR_AGE_S", 3600.0)
    called = {}
    monkeypatch.setattr(R, "real_entry",
                        lambda *a, **k: called.setdefault("hit", True) and
                        {"status": "rejected", "reason": "test seam"})
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        stats_fn=lambda mint, network_id=None: {"price": 5.0})
    assert res["status"] == "rejected" and res["reason"] == "test seam"
    assert called["hit"] is True


def test_age_ceiling_off_is_inert(monkeypatch):
    c = _conn()
    _no_chase(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_PAIR_AGE_S", 0.0)
    called = {}
    monkeypatch.setattr(R, "real_entry",
                        lambda *a, **k: called.setdefault("hit", True) and
                        {"status": "rejected", "reason": "test seam"})
    res = R.real_entry_hook(
        c, _EvObj(), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
        trade_size=50.0, max_age_s=3600.0,
        stats_fn=lambda mint, network_id=None: {"price": 5.0, "pair_age_s": 349142.0})
    assert res["status"] == "rejected" and res["reason"] == "test seam"
    assert called["hit"] is True


# ---- 2026-09-05: a price from a dust pair is unknown, not a run-up (ESKER, FORESKIN) ------------

def test_first_seen_guard_ignores_a_price_read_from_a_dust_pair(monkeypatch):
    """ESKER 12:50: the only indexed pairs held $12; their price was 3.4x the wallet's fill while
    the real $22.7k pool sat at 1.5x. The guard must not compare a dust-pair number against the
    first-seen fill; it falls open to the source price, exactly as when the provider is dark."""
    c = _conn()
    m.insert_event(c, _feed_ev("e1", price=1.0))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 2.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SNAPSHOT_MIN_PAIR_LIQUIDITY_USD", 500.0, raising=False)
    called = {}

    def _spy(*a, **k):
        called["hit"] = True
        return {"status": "rejected", "reason": "x"}

    monkeypatch.setattr(R, "real_entry", _spy)
    res = R.real_entry_hook(c, _EvObj(their_price=1.0), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
                            trade_size=50.0, max_age_s=3600.0,
                            stats_fn=lambda mint, network_id=None: {"price": 3.4, "liquidity": 12.18})
    assert res["status"] != "skipped_chase", res
    assert called.get("hit"), "a dust-pair price must not refuse the entry"


def test_first_seen_guard_still_refuses_a_real_run_up_from_a_deep_pair(monkeypatch):
    c = _conn()
    m.insert_event(c, _feed_ev("e1", price=1.0))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 2.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SNAPSHOT_MIN_PAIR_LIQUIDITY_USD", 500.0, raising=False)
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: {"status": "rejected", "reason": "x"})
    res = R.real_entry_hook(c, _EvObj(their_price=3.4), {}, now_fn=lambda: "2026-07-15T13:30:01Z",
                            trade_size=50.0, max_age_s=3600.0,
                            stats_fn=lambda mint, network_id=None: {"price": 3.4, "liquidity": 22700.0})
    assert res["status"] == "skipped_chase"
    assert "first-seen" in res["reason"]
