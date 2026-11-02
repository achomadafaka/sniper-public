"""Item 4 (runner wiring) — the DORMANT real leg. Constructed only when armed with creds; reuses the
paper alpha_b EXIT DECISION but executes on-chain via the bound exit_fn. Entry reuses real_entry with
a tight staleness gate; mirror-on-sell exits a matching real position when the trader sells. All logic
is unit-tested with injected seams (no live endpoint); the live smoke test gates any arm."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sniper.execution.solana_quote import norm_mint
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open_real(c, mint, entry=0.01, uid="u1", opened="2026-07-09T00:00:00Z"):
    return m.open_position(c, source_user_id=uid, source_handle="h", mint=mint, ticker="T",
                           entry_price=entry, entry_notional=10.0, entry_fill_source="chain",
                           entry_lag_s=-1.0, their_size_usd=0.0, opened_at=opened,
                           strategy="alpha_b", execution_mode="real", network_id="solana")


class _Ev:
    def __init__(self, mint, uid="u1", created_at="2026-07-09T00:00:00Z", side="buy",
                 event_id="event-1"):
        self.mint = mint; self.source_user_id = uid; self.created_at = created_at
        self.network_id = "solana"; self.side = side; self.fomo_event_id = event_id


def _feed_observation(event_id, uid, observed_at, *, mint="0xcoin", network_id="4663",
                      side="buy", raw_type=None, remaining_usd=100.0):
    raw_type = raw_type or ("large_buy" if side == "buy" else "large_sell")
    return {
        "upstream_event_id": event_id,
        "raw_type": raw_type,
        "normalized_side": side,
        "source_user_id": uid,
        "source_handle": uid,
        "network_id": network_id,
        "mint": mint,
        "ticker": "T",
        "their_price": 1.0,
        "their_market_cap": 1_000_000.0,
        "their_size_usd": remaining_usd,
        "source_ts_raw": observed_at,
        "source_ts": observed_at,
        "observed_at": observed_at,
        "lag_s": 0.0,
        "raw_payload": {"body": {"currentSizeUsd": remaining_usd}},
    }


def _insert_observation(c, *args, **kwargs):
    observation = _feed_observation(*args, **kwargs)
    m.insert_feed_observation_v2(c, observation, was_curated=True, was_real_approved=True)


def _arm(c, uid, *, paused=False, excluded=False):
    m.add_wallet(c, uid, uid)
    m.set_wallet_real_approved(c, uid, True, chains=["solana", "8453", "4663"], force=True)
    c.execute("UPDATE memecoin_wallets SET paused=?, excluded=? WHERE source_user_id=?",
              (1 if paused else 0, 1 if excluded else 0, uid))
    c.commit()


def test_volume_floor_exemption_reaches_the_real_leg(monkeypatch):
    c = _c()
    m.add_wallet(c, "rowdy", "Rowdy")
    m.set_wallet_gate_exemption(
        c, "rowdy", "m5_turnover_under_floor", active=True, actor="test",
        reason="operator override", now_iso="2026-08-31T23:00:00+00:00",
    )
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 2_000_000.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", 0.10)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 0.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", 0.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MIN_TURNOVER_M5", 0.10)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MAX_TURNOVER_M5", 0.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MAX_SELL_SHARE_PCT", 0.0)
    calls = []
    monkeypatch.setattr(
        R, "real_entry",
        lambda *_args, **_kwargs: calls.append(1) or {"status": "test_reached"},
    )
    event = _Ev("0xau", uid="rowdy", created_at="2026-08-31T23:00:00+00:00")
    event.their_price = 1.0
    event.their_size_usd = 792.0
    event.signal_source = "follow_activity"

    result = R.real_entry_hook(
        c, event, {"4663": {}}, now_fn=lambda: "2026-08-31T23:00:01+00:00",
        trade_size=100.0, max_age_s=120.0,
        resolved_identity=R.ResolvedEntryIdentity("4663", "resolved", "test"),
        classification_context={
            "effective_mcap": 1_213_470.0,
            "assumed_micro": False,
            "consensus_n_60m": 1,
            "market_stats": {
                "price": 1.0, "market_cap": 1_213_470.0,
                "volume_m5": 18_426.0, "volume_h1": 1_000_000.0,
                "liquidity": 100_000.0,
            },
        },
    )

    assert result == {"status": "test_reached"}
    assert calls == [1]
    payload = json.loads(c.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])
    shadow = next(e for e in payload["substage_events"] if e["name"] == "funded_entry_shadow_v1")
    gates = shadow["gate_provenance_v1"]["selection_gate_states"]
    assert gates["m5_turnover_under_big_floor"] == "out_of_scope"
    assert gates["m5_turnover_over_ceiling"] == "disabled"
    assert gates["chase_first_seen"] == "disabled"
    assert gates["sell_share"] == "disabled"


def test_the_hook_resolves_exemptions_for_the_entry_chain(monkeypatch):
    """Chain-wide exemptions (MEMECOIN_GATE_EXEMPT_FAMILIES_<CHAIN>) are keyed by the RESOLVED
    chain; the exemption logic itself is tested in test_memecoin_wallet_gate_exemptions."""
    c = _c()
    m.add_wallet(c, "rowdy", "Rowdy")
    seen = []
    monkeypatch.setattr(R.m, "entry_gate_exemptions",
                        lambda _c, uid, network: seen.append((uid, network)) or frozenset())
    monkeypatch.setattr(R, "real_entry", lambda *_a, **_k: {"status": "test_reached"})
    event = _Ev("0xau", uid="rowdy", created_at="2026-08-31T23:00:00+00:00")
    event.their_price = 1.0
    event.their_size_usd = 792.0
    event.signal_source = "follow_activity"
    R.real_entry_hook(
        c, event, {"4663": {}}, now_fn=lambda: "2026-08-31T23:00:01+00:00",
        trade_size=100.0, max_age_s=120.0,
        resolved_identity=R.ResolvedEntryIdentity("4663", "resolved", "test"),
        classification_context={"effective_mcap": 50_000.0, "assumed_micro": False,
                                "consensus_n_60m": 1, "market_stats": {"price": 1.0}})
    assert seen and seen[0] == ("rowdy", "4663")


# --- pure alpha_b exit decision ---

def test_b_stop_fires_on_catastrophe_drop():
    pos = {"entry_price": 1.0, "opened_at": "2026-07-09T00:00:00Z"}
    mid = 1.0 * (1 - (cfg.MEMECOIN_B_STOP_PCT + 1) / 100.0)
    assert R.alpha_b_real_exit_reason(pos, mid, "2026-07-09T00:01:00Z") == "b_stop"


def test_no_exit_when_healthy():
    pos = {"entry_price": 1.0, "opened_at": "2026-07-09T00:00:00Z"}
    assert R.alpha_b_real_exit_reason(pos, 1.5, "2026-07-09T00:01:00Z") is None


def test_revival_watch_uses_the_live_over_band_floor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 1_000_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", 0.03)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_TURNOVER_M5", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 0)
    event = _Ev("MINT")
    event.their_market_cap = 3_000_000.0
    event.their_size_usd = 3_000.0

    assert not R.revival_watch_volume_cleared(
        {"volume_m5": 92_000.0, "volume_h1": 100_000.0}, event)
    assert R.revival_watch_volume_cleared(
        {"volume_m5": 94_000.0, "volume_h1": 100_000.0}, event)


# --- catastrophe floor (2026-07-18): an absolute max-loss backstop that fires on EVERY funded
#     position regardless of a chain default of 0 (4663) or an override that disabled/widened the
#     stop. Root cause of "SL doesn't work": 0/4 live positions had any stop (meow -52%, VIM -73%). ---

_CAT = cfg.MEMECOIN_REAL_CATASTROPHE_PCT


def test_catastrophe_fires_on_4663_where_default_stop_is_zero():
    # 4663 default stop = 0 -> the normal b_stop path never nominates; the floor must.
    pos = {"entry_price": 1.0, "opened_at": "2026-07-18T00:00:00Z", "network_id": "4663"}
    mid = 1.0 * (1 - (_CAT + 5) / 100.0)                    # below the floor
    assert R.alpha_b_real_exit_reason(pos, mid, "2026-07-18T00:05:00Z") == "catastrophe"


def test_catastrophe_does_not_fire_above_floor_on_4663():
    # a -30/-50% dip on 4663 must still ride (the 07-12 recover-thesis) — floor only catches death spirals.
    pos = {"entry_price": 1.0, "opened_at": "2026-07-18T00:00:00Z", "network_id": "4663"}
    mid = 1.0 * (1 - (_CAT - 10) / 100.0)                   # above the floor (smaller loss)
    assert R.alpha_b_real_exit_reason(pos, mid, "2026-07-18T00:05:00Z") is None


def test_catastrophe_fires_even_when_operator_disabled_stop():
    pos = {"entry_price": 1.0, "opened_at": "2026-07-18T00:00:00Z", "network_id": "solana"}
    ov = {"source": "operator", "sl_pct": 0.0}              # operator turned the stop OFF
    mid = 1.0 * (1 - (_CAT + 5) / 100.0)
    assert R.alpha_b_real_exit_reason(pos, mid, "2026-07-18T00:05:00Z", override=ov) == "catastrophe"


def test_tighter_operator_stop_still_wins_over_floor():
    # operator's -30% stop must fire first (more protection) — catastrophe is only a backstop.
    pos = {"entry_price": 1.0, "opened_at": "2026-07-18T00:00:00Z", "network_id": "solana"}
    ov = {"source": "operator", "sl_pct": -30.0}
    mid = 1.0 * (1 - 0.40)                                  # -40%: past the -30 stop, above the -80 floor
    assert R.alpha_b_real_exit_reason(pos, mid, "2026-07-18T00:05:00Z", override=ov) == "b_stop"


def test_catastrophe_is_executable_reason():
    assert "catastrophe" in R._EXECUTABLE_PRICE_REASONS


# --- rollover / profit-lock engine (2026-07-18): default trail replaces the fixed TP ---

def _rollover(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)          # hard TP dropped
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 40.0)     # recover principal at +40%
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", 30.0)      # exit on 30% giveback from peak
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_ARM_PCT", 15.0)  # arm once peak > +15%
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 80.0)


def test_default_trail_catches_modest_top_then_crash(monkeypatch):
    # Modest executable top: peaked +30% (below +40% derisk), gave back 30% -> trail at ~-9%.
    _rollover(monkeypatch)
    pos = {"entry_price": 1.0, "opened_at": "2026-07-18T00:00:00Z", "peak_price": 1.30,
           "executable_peak_price": 1.30,
           "derisked_at": None, "network_id": "4663"}
    mid = 1.30 * (1 - 0.30)                                         # 0.91 = -9%, 30% off the +30% peak
    assert R.alpha_b_real_exit_reason(pos, mid, "2026-07-18T00:05:00Z") == "real_trail"


def test_default_trail_does_not_arm_on_early_dip_never_climbed(monkeypatch):
    # a coin that dipped -30% early WITHOUT ever climbing past +15% must NOT trail-sell (4663 dips
    # recover) — only the catastrophe floor guards it until it climbs.
    _rollover(monkeypatch)
    pos = {"entry_price": 1.0, "opened_at": "2026-07-18T00:00:00Z", "peak_price": 1.05,
           "executable_peak_price": 1.05,  # executable peak only +5%
           "derisked_at": None, "network_id": "4663"}
    assert R.alpha_b_real_exit_reason(pos, 0.70, "2026-07-18T00:05:00Z") is None          # -30%, trail unarmed


def test_default_derisk_recovers_principal_before_trail(monkeypatch):
    _rollover(monkeypatch)
    pos = {"entry_price": 1.0, "opened_at": "2026-07-18T00:00:00Z", "peak_price": 1.45,
           "executable_peak_price": 1.45,
           "derisked_at": None, "network_id": "4663"}
    assert R.alpha_b_real_exit_reason(pos, 1.45, "2026-07-18T00:05:00Z") == "derisk"       # +45% >= +40%


def test_no_hard_tp_lets_winner_ride_past_60(monkeypatch):
    # with the fixed TP dropped, a coin holding near its high past +60% keeps riding (trail not tripped)
    _rollover(monkeypatch)
    pos = {"entry_price": 1.0, "opened_at": "2026-07-18T00:00:00Z", "peak_price": 2.00,
           "executable_peak_price": 2.00,
           "derisked_at": "2026-07-18T00:01:00Z", "network_id": "4663"}   # already de-risked
    assert R.alpha_b_real_exit_reason(pos, 1.90, "2026-07-18T00:05:00Z") is None           # +90%, only 5% off peak
    assert R.alpha_b_real_exit_reason(pos, 1.40, "2026-07-18T00:05:00Z") == "real_trail"   # gave back 30%


def test_timecap_fires_after_max_hold_even_unquotable():
    pos = {"entry_price": 1.0, "opened_at": "2026-07-09T00:00:00+00:00"}
    old = (datetime(2026, 7, 9, tzinfo=timezone.utc)
           + timedelta(seconds=cfg.MEMECOIN_B_MAX_HOLD_S + 60)).isoformat()
    assert R.alpha_b_real_exit_reason(pos, None, old) == "b_timecap"       # unquotable + old -> time
    assert R.alpha_b_real_exit_reason(pos, None, "2026-07-09T00:01:00+00:00") is None  # young -> hold


# --- rollover ENGINE end-to-end (2026-07-18): nomination -> executable confirmation -> execution.
#     The pure tests above only cover nomination; these drive the whole real_exit_sweep so a trail
#     threshold that nominates but is then rejected by the confirmation layer (the F1 bug) or an exit
#     that skips confirmation/audit entirely (the F2 bug on 4663) is caught. ---

def _entry_fill(c, pid, *, qty=10.0, price=1.0, opened="2026-07-09T00:00:00Z"):
    m.insert_fill(c, position_id=pid, kind="entry", price=price, qty=qty, notional=qty * price,
                  fill_source="chain", fraction=1.0, created_at=opened)


def _audit_rows(c, pid, reason):
    return c.execute("SELECT allowed FROM memecoin_exit_trigger_audit WHERE position_id=? AND reason=?",
                     (pid, reason)).fetchall()


def test_sweep_default_trail_fires_on_priced_solana(monkeypatch):
    # F1 regression: a priced-venue default trail nominates AND the executable confirmation lets it
    # through. Before the fix the confirmation recomputed the trail as 0.0 -> executable_threshold_not_met
    # -> the position never sold.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    _rollover(monkeypatch)
    c = _c()
    pid = _open_real(c, "MintTrailSol", entry=1.0)            # entry_notional=10, entry_price=1.0
    _entry_fill(c, pid, qty=10.0, price=1.0)                  # open_qty=10 -> executable_price = exp/10
    m.update_position_peak(c, pid, 1.30, "2026-07-09T00:01:00Z", 0)   # peak +30% (armed, below +40 derisk)
    m.update_position_executable_peak(c, pid, 1.30)
    c.commit()
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append((pos["mint"], reason)),
                                                     {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}   # 10 tokens @ 0.90 -> executable 0.90
    pmap = {norm_mint("MintTrailSol"): {"price": 0.90, "liquidity": 1e9}}   # 0.90 <= 1.30*0.70=0.91
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == [("MintTrailSol", "real_trail")]
    audit = _audit_rows(c, pid, "real_trail")
    assert audit and audit[0]["allowed"] == 1


def test_exit_sweep_releases_prior_writes_before_provider_calls(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", 0.0)
    c = _c()
    _open_real(c, "MintNoWalPin", entry=1.0)
    c.commit()
    c.execute("UPDATE memecoin_positions SET ticker='PIN' WHERE mint='MintNoWalPin'")
    observed = []

    R.real_exit_sweep(
        c,
        {"solana": {"preview_exit_fn": lambda _pos: observed.append(c.in_transaction) or None}},
        price_fn=lambda _mints: observed.append(c.in_transaction) or {},
        now_fn=lambda: "2026-07-09T00:05:00Z",
    )

    assert observed == [False, False]


def test_sweep_default_trail_fires_on_blind_4663_with_audit(monkeypatch):
    # F2 regression: the blind chain (4663, no feed price) must ALSO run the executable confirmation and
    # write an audit row. Before the fix its precomputed price-fallback preview made the confirm/audit
    # block skip entirely — the trail "worked" only by bypassing the guard, leaving zero audit trail.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    _rollover(monkeypatch)
    c = _c()
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="0xblindtrail", ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                          strategy="alpha_b", execution_mode="real", network_id="4663")
    _entry_fill(c, pid, qty=10.0, price=1.0)
    m.update_position_peak(c, pid, 1.30, "2026-07-09T00:01:00Z", 0)
    m.update_position_executable_peak(c, pid, 1.30)
    c.commit()
    calls = []
    # exp=9.0 over 10 open tokens -> exact unit price 0.90 <= 0.91 trail floor.
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append((pos["mint"], reason)),
                                                     {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}
    R.real_exit_sweep(c, {"4663": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == [("0xblindtrail", "real_trail")]
    audit = _audit_rows(c, pid, "real_trail")
    assert audit and audit[0]["allowed"] == 1        # the guard actually ran on 4663


def test_blind_4663_trail_tracks_exact_peak_after_derisk(monkeypatch):
    _rollover(monkeypatch)
    c = _c()
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="0xpostderisk", ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-18T00:00:00Z",
                          strategy="alpha_b", execution_mode="real", network_id="4663")
    _entry_fill(c, pid, qty=10.0, price=1.0, opened="2026-07-18T00:00:00Z")
    sold_qty = 10.0 / 1.4
    m.insert_fill(c, position_id=pid, kind="derisk", price=1.4, qty=sold_qty, notional=10.0,
                  fill_source="chain", fraction=sold_qty / 10.0,
                  created_at="2026-07-18T00:01:00Z")
    m.set_position_derisked(c, pid, "2026-07-18T00:01:00Z")
    m.update_position_peak(c, pid, 1.4, "2026-07-18T00:01:00Z", 0)
    remaining = m.open_qty(c, pid)
    exact_price = [3.0]
    calls = []
    rc = {"preview_exit_fn": lambda pos: {"expected_usdc": remaining * exact_price[0]},
          "exit_fn": lambda conn, pos, reason, now: (calls.append(reason),
                                                       {"status": "closed"})[1]}

    R.real_exit_sweep(c, {"4663": rc}, price_fn=lambda mints: {},
                      now_fn=lambda: "2026-07-18T00:02:00Z")
    assert m.get_position(c, pid)["executable_peak_price"] == pytest.approx(3.0)

    exact_price[0] = 2.0  # 33% giveback from the executable 3.0 peak; 30% floor is 2.1
    R.real_exit_sweep(c, {"4663": rc}, price_fn=lambda mints: {},
                      now_fn=lambda: "2026-07-18T00:03:00Z")
    assert calls == ["real_trail"]
    assert _audit_rows(c, pid, "real_trail")[-1]["allowed"] == 1


def test_priced_trail_peak_uses_exact_balance_not_display_quote(monkeypatch):
    _rollover(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    pid = _open_real(c, "MintExactPeak", entry=1.0, opened="2026-07-18T00:00:00Z")
    _entry_fill(c, pid, qty=10.0, price=1.0, opened="2026-07-18T00:00:00Z")
    rc = {"preview_exit_fn": lambda pos: {"expected_usdc": 13.0},
          "exit_fn": lambda *args: pytest.fail("display-only high must not exit")}
    pmap = {norm_mint("MintExactPeak"): {"price": 2.0, "liquidity": 1e9}}

    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-18T00:01:00Z")

    pos = m.get_position(c, pid)
    # One sweep is one display tick: unconfirmed until a second agrees (2026-10-17).
    assert pos["peak_unconfirmed_price"] == pytest.approx(2.0) and pos["peak_price"] == pytest.approx(1.0)
    assert pos["executable_peak_price"] == pytest.approx(1.3)


def test_priced_trail_nominates_from_exact_giveback(monkeypatch):
    _rollover(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    pid = _open_real(c, "MintExactGiveback", entry=1.0, opened="2026-07-18T00:00:00Z")
    _entry_fill(c, pid, qty=10.0, price=1.0, opened="2026-07-18T00:00:00Z")
    exact_price = [2.0]
    calls = []
    rc = {"preview_exit_fn": lambda pos: {"expected_usdc": 10.0 * exact_price[0]},
          "exit_fn": lambda conn, pos, reason, now: (calls.append(reason),
                                                       {"status": "closed"})[1]}
    display = [2.1]

    R.real_exit_sweep(
        c, {"solana": rc},
        price_fn=lambda mints: {norm_mint("MintExactGiveback"): {
            "price": display[0], "liquidity": 1e9}},
        now_fn=lambda: "2026-07-18T00:01:00Z",
    )
    exact_price[0] = 1.39  # below exact 2.0 * 0.70 = 1.40
    display[0] = 1.50      # above nominal 2.1 * 0.70 = 1.47: display would miss it
    R.real_exit_sweep(
        c, {"solana": rc},
        price_fn=lambda mints: {norm_mint("MintExactGiveback"): {
            "price": display[0], "liquidity": 1e9}},
        now_fn=lambda: "2026-07-18T00:02:00Z",
    )

    assert calls == ["real_trail"]
    assert _audit_rows(c, pid, "real_trail")[-1]["allowed"] == 1


def test_exact_policy_failed_quote_still_nominates_loss_rail(monkeypatch):
    # decision_mid regression (2026-07-18): under the rollover exact-policy a FAILED exact quote
    # (preview None) must NOT blind the loss rails. Before the fix `decision_mid = exact_mid if
    # exact_policy else mid` set decision_mid=None on any exact-quote failure, so alpha_b's
    # `if mid and mid > 0` gate skipped EVERY rail — no b_stop, no catastrophe, no audit row, while
    # the display mid clearly showed a crash.
    #
    # SUPERSEDED IN PART by ec09dd44 (2026-09-03): an exact-policy position no longer NOMINATES from
    # the nominal mid either. Measured over 1,554 4663 marks paired within 15s, |dexscreener -
    # executable| is 11.1% at the median and 37.8% at p90, and 53.0% disagree by more than 10%; a
    # stop armed off that series is noise pointing both ways (it hid a real -62.8% on 29763 and
    # invented a -47% on 49013). The surviving invariant is what this test is really for: a failed
    # exact quote must never SELL, and the gap must be VISIBLE rather than silent. So the assertion
    # moves from "an audit row rejecting the nomination" to "no nomination, no sale, and the blind
    # state recorded" — which is the honest gap, not the wrong number.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    _rollover(monkeypatch)
    c = _c()
    pid = _open_real(c, "MintFailQuote", entry=1.0, opened="2026-07-18T00:00:00Z")
    _entry_fill(c, pid, qty=10.0, price=1.0, opened="2026-07-18T00:00:00Z")
    c.commit()
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(reason), {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: None}                      # exact quote FAILS every sweep
    pmap = {norm_mint("MintFailQuote"): {
        "price": 0.30,
        "liquidity": 1e9,
        "source": "fastlane_sol_swap",
        "quality": "live",
    }}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-18T00:05:00Z")
    assert calls == []                    # THE MONEY INVARIANT: never sell without executable proceeds
    assert _audit_rows(c, pid, "b_stop") == [], (
        "an exact-policy position must not nominate a stop from the nominal mid")
    assert (m.get_position(c, pid)["rails_blind_streak"] or 0) >= 1, (
        "a rail that cannot see must SAY so — silence here is the failure mode, not the gap itself")


def test_exact_policy_failed_quote_still_stamps_last_quote_from_display(monkeypatch):
    # The nomination fix must NOT starve the timecap / zombie guard of a price: when the exact quote
    # fails but a display price exists, last_quote is still stamped from the display mid (the coin is
    # priceable — the zombie guard is only for coins we can't price at all). Mirrors the pre-existing
    # test_exit_sweep_stamps_last_quote_on_real_positions invariant under exact_policy.
    _rollover(monkeypatch)
    c = _c()
    pid = _open_real(c, "MintStampDisp", entry=1.0, opened="2026-07-18T00:00:00Z")
    _entry_fill(c, pid, qty=10.0, price=1.0, opened="2026-07-18T00:00:00Z")
    c.commit()
    rc = {"exit_fn": lambda *a: {"status": "closed"},
          "preview_exit_fn": lambda pos: None}                          # exact quote fails
    pmap = {norm_mint("MintStampDisp"): {"price": 0.90, "liquidity": 1e9}}  # healthy display, no rail
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-18T00:05:00Z")
    p = m.get_position(c, pid)
    assert p["last_quote_ts"] == "2026-07-18T00:05:00Z"          # advanced from the display price
    assert p["last_quote_price"] == pytest.approx(0.90)


def test_non_exact_policy_unchanged_display_drives_decision_and_quote(monkeypatch):
    # Regression guard: legacy (non-rollover, no derisk/trail) positions are unchanged — decision on the
    # display mid, and last_quote_ts advances on the display price as before.
    _rollover(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", 0.0)            # exact_policy = False
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    c = _c()
    pid = _open_real(c, "MintLegacy", entry=1.0, opened="2026-07-18T00:00:00Z")
    _entry_fill(c, pid, qty=10.0, price=1.0, opened="2026-07-18T00:00:00Z")
    c.commit()
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append(reason), {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 3.0}}        # exact available for confirm
    pmap = {norm_mint("MintLegacy"): {"price": 0.30, "liquidity": 1e9}}  # -70%: past the 60% stop
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-18T00:05:00Z")
    assert calls == ["b_stop"]                                           # display-driven stop executes
    assert m.get_position(c, pid)["last_quote_ts"] == "2026-07-18T00:05:00Z"


def test_default_trail_excludes_pre_cohort_position(monkeypatch):
    # F3: a rollout cutoff makes the DEFAULT trail new-entries-only. A bag opened before the cutoff is
    # NOT force-sold on deploy; one opened after trails normally; an operator override bypasses the gate.
    _rollover(monkeypatch)
    # cutoff 30s after `pre` opened; all `now`s within max-hold so the timecap can't pre-empt the trail
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT", "2026-07-18T00:00:30Z")
    mid = 1.30 * (1 - 0.30)                                    # 30% off a +30% peak
    now = "2026-07-18T00:01:00Z"
    pre = {"entry_price": 1.0, "opened_at": "2026-07-18T00:00:00Z", "peak_price": 1.30,
           "executable_peak_price": 1.30,
           "derisked_at": None, "network_id": "4663"}
    assert R.alpha_b_real_exit_reason(pre, mid, now) is None                          # pre-cutoff -> untouched
    post = dict(pre, opened_at="2026-07-18T00:00:45Z")                                # opened after cutoff
    assert R.alpha_b_real_exit_reason(post, mid, now) == "real_trail"
    ov = {"source": "operator", "trail_pct": 30.0}
    over = dict(pre, derisked_at="2026-07-18T00:00:15Z")       # pre-cutoff but override bypasses the gate
    assert R.alpha_b_real_exit_reason(over, mid, now, override=ov) == "real_trail"


def test_rollover_cutoff_selects_the_complete_default_policy(monkeypatch):
    _rollover(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_LEGACY_TP_PCT", 60.0, raising=False)
    monkeypatch.setattr(
        cfg, "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT", "2026-07-18T00:00:30Z", raising=False,
    )
    now = "2026-07-18T00:01:00Z"
    pre = {"entry_price": 1.0, "opened_at": "2026-07-18T00:00:00Z", "peak_price": 1.61,
           "derisked_at": None, "network_id": "4663"}
    assert R.alpha_b_real_exit_reason(pre, 1.45, now) is None
    assert R.alpha_b_real_exit_reason(pre, 1.61, now) == "real_tp"

    post = dict(pre, opened_at="2026-07-18T00:00:45Z")
    assert R.alpha_b_real_exit_reason(post, 1.45, now) == "derisk"
    post["derisked_at"] = "2026-07-18T00:00:50Z"
    assert R.alpha_b_real_exit_reason(post, 1.61, now) is None


def test_rollover_cutoff_compares_equivalent_instants(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT", "2026-07-18T08:00:00Z")
    # 04:00:01-04:00 is 08:00:01Z: one second after the cutoff.
    assert R._position_in_rollover_cohort({"opened_at": "2026-07-18T04:00:01-04:00"}) is True


def test_validate_rejects_nan_and_negative_trail_settings(monkeypatch):
    # F5: the armed runner must fail closed on garbage trail settings instead of computing a NaN floor.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", float("nan"))
    with pytest.raises(RuntimeError, match="MEMECOIN_REAL_TRAIL_PCT"):
        cfg.validate_real_exit_policy()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_ARM_PCT", -100.0)
    with pytest.raises(RuntimeError, match="MEMECOIN_REAL_TRAIL_ARM_PCT"):
        cfg.validate_real_exit_policy()


def test_validate_rejects_garbage_cohort_cutoff(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT", "not-a-timestamp")
    with pytest.raises(RuntimeError, match="MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT"):
        cfg.validate_real_exit_policy()


def test_validate_requires_aware_cutoff_when_funded_trail_is_armed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT", "")
    with pytest.raises(RuntimeError, match="cutoff"):
        cfg.validate_real_exit_policy()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT", "2026-07-18T08:00:00")
    with pytest.raises(RuntimeError, match="cutoff"):
        cfg.validate_real_exit_policy()


# --- real exit sweep executes on-chain, only for real rows ---

def test_real_exit_sweep_stops_only_real_positions(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)   # subject: mode scoping
    c = _c()
    _open_real(c, "MintDrop", entry=1.0)
    _open_real(c, "MintOK", entry=1.0)
    m.open_position(c, source_user_id="u2", source_handle="h", mint="MintDrop", ticker="T",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="j", entry_lag_s=0.1,
                    their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z", strategy="alpha_b")  # PAPER
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append((pos["mint"], reason)),
                                                     {"status": "closed"})[1],
          "preview_exit_fn": lambda pos: {"expected_usdc": 4.0}}
    drop = 1.0 * (1 - (cfg.MEMECOIN_B_STOP_PCT + 1) / 100.0)
    pmap = {norm_mint("MintDrop"): {"price": drop, "liquidity": 1e9},
            norm_mint("MintOK"): {"price": 2.0, "liquidity": 1e9}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == [("MintDrop", "b_stop")]              # real dropper only; paper + healthy untouched


def test_real_exit_sweep_persists_price_marks_for_charts(monkeypatch):
    # Every open real position must accrue an in-house chart tape — the real sweep persists THIS sweep's
    # priced marks, the gap that left open real Solana positions (KITTY) chart-less while the paper sweep
    # marked its coins. A healthy position that never exits is still marked (2026-07-18).
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    c = _c()
    _open_real(c, "MintHealthy", entry=1.0)
    rc = {"exit_fn": lambda *a, **k: pytest.fail("healthy position must not exit"),
          "preview_exit_fn": lambda pos: {"expected_usdc": 20.0}}
    pmap = {norm_mint("MintHealthy"): {"price": 1.2, "liquidity": 1e9, "mcap": 2_000_000}}  # +20%, no rail
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap, now_fn=lambda: "2026-07-09T00:05:00Z")
    marks = c.execute("SELECT mid FROM memecoin_position_marks WHERE mint=?",
                      (norm_mint("MintHealthy"),)).fetchall()
    assert marks and marks[0]["mid"] == 1.2              # the sweep persisted a chart mark


# --- entry hook: staleness gate then real_entry ---

def test_entry_hook_drops_stale_buy():
    c = _c()
    hit = {"n": 0}
    rc = {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
          "token_safety_fn": lambda mint, **kw: (True, "ok"),
          "execute_fn": lambda *a, **k: (hit.__setitem__("n", hit["n"] + 1), {"status": "filled"})[1],
          "kill_path": None}
    ev = _Ev("M", created_at="2020-01-01T00:00:00Z")     # ancient -> stale
    r = R.real_entry_hook(c, ev, {"solana": rc}, now_fn=lambda: "2026-07-09T00:00:00Z", trade_size=10.0, max_age_s=120)
    assert r["status"] == "stale" and hit["n"] == 0


def test_entry_hook_fresh_buy_reaches_executor(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _c()
    execs = {"n": 0}

    def ex(conn, pid, mint, size, now, impact_cap_pct=None, *, entry_min_slot=None,
           entry_max_slot=None, entry_head_fn):
        assert (entry_min_slot, entry_max_slot) == (None, None)
        assert callable(entry_head_fn)
        execs["n"] += 1
        return {"status": "filled", "position_id": pid}
    rc = {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
          "token_safety_fn": lambda mint, **kw: (True, "ok"), "execute_fn": ex,
          "kill_path": str(tmp_path / "k")}
    ev = _Ev("Mfresh", created_at="2026-07-09T00:00:00Z")
    ev.source_block_number = 100
    r = R.real_entry_hook(c, ev, {"solana": rc}, now_fn=lambda: "2026-07-09T00:00:00Z", trade_size=10.0, max_age_s=120)
    assert r["status"] == "filled" and execs["n"] == 1


def test_entry_hook_blocks_on_typed_sol_read_failure(monkeypatch, tmp_path):
    from sniper.execution.solana_wallet import NativeSolBalanceRead

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _c()
    rc = {
        "usdc_fn": lambda: 300.0,
        "sol_fn": lambda: NativeSolBalanceRead(
            ok=False, balance=None, failure="rpc_error"),
        "token_safety_fn": lambda *_args, **_kwargs: (True, "ok"),
        "execute_fn": lambda *_args, **_kwargs: pytest.fail("must not execute"),
        "kill_path": str(tmp_path / "k"),
    }

    result = R.real_entry_hook(
        c, _Ev("Mfail", created_at="2026-07-09T00:00:00Z"), {"solana": rc},
        now_fn=lambda: "2026-07-09T00:00:00Z", trade_size=10.0, max_age_s=120)

    assert result["status"] == "rejected"
    assert result["reason"] == "SOL balance unavailable"


@pytest.mark.parametrize(("effective_mcap", "assumed_micro", "expected_band"), [
    (25_000.0, False, "nano"),
    (100_000.0, False, "micro"),
    (500_000.0, False, "small"),
    (1_500_000.0, False, "mid"),
    (None, True, "assumed_nano"),
])
def test_entry_hook_classifies_every_reservable_band_before_real_leg(
    monkeypatch, effective_mcap, assumed_micro, expected_band
):
    c = _c()
    captured = {}
    monkeypatch.setattr(
        cfg, "MEMECOIN_REAL_RESERVABLE_BANDS", ("nano", "micro", "small", "mid"))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)

    def fake_real_entry(*_args, **kwargs):
        captured["classification"] = kwargs.get("classification")
        return {"status": "rejected", "reason": "test seam"}

    monkeypatch.setattr(R, "real_entry", fake_real_entry)
    ev = _Ev("BandMint", created_at="2026-07-18T11:59:59+00:00")
    ev.their_price = 1.0
    result = R.real_entry_hook(
        c, ev, {"solana": {}}, now_fn=lambda: "2026-07-18T12:00:00+00:00",
        trade_size=100.0, max_age_s=120.0,
        stats_fn=lambda *_args, **_kwargs: {
            "price": 1.0, "liquidity": 10_000.0, "pair_created_at": 1_784_372_400_000,
        },
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={
            "effective_mcap": effective_mcap,
            "assumed_micro": assumed_micro,
            "consensus_n_60m": 1,
        },
    )

    assert result["status"] == "rejected"
    assert captured["classification"].coin_band == expected_band
    assert captured["classification"].facts["pair_age_s"] == pytest.approx(3600.0)


def test_entry_hook_passes_fresh_follow_chase_cap_to_real_leg(monkeypatch):
    c = _c()
    captured = {}
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", 37.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)

    def fake_real_entry(*_args, **kwargs):
        captured["chase_max_pct"] = kwargs.get("chase_max_pct")
        return {"status": "rejected", "reason": "test seam"}

    monkeypatch.setattr(R, "real_entry", fake_real_entry)
    event = _Ev("FreshMint", created_at="2026-07-18T11:59:59+00:00")
    event.their_price = 1.0
    event.signal_source = "follow_activity"
    event.lag_s = 1.0

    result = R.real_entry_hook(
        c, event, {"solana": {}}, now_fn=lambda: "2026-07-18T12:00:00+00:00",
        trade_size=100.0, max_age_s=120.0,
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={
            "effective_mcap": 500_000.0,
            "assumed_micro": False,
            "consensus_n_60m": 1,
            "market_stats": {"price": 1.0, "liquidity": 10_000.0},
        },
    )

    assert result == {"status": "rejected", "reason": "test seam"}
    assert captured["chase_max_pct"] == 37.0


def test_chase_exemption_disables_the_exact_quote_chase_cap(monkeypatch):
    c = _c()
    captured = {}
    m.add_wallet(c, "u1", "fmpumpguy")
    m.set_wallet_gate_exemption(
        c, "u1", "chase_past_entry", active=True, actor="test",
        reason="operator override", now_iso="2026-08-31T09:30:00+00:00")
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)

    def fake_real_entry(*_args, **kwargs):
        captured["chase_max_pct"] = kwargs.get("chase_max_pct")
        return {"status": "rejected", "reason": "test seam"}

    monkeypatch.setattr(R, "real_entry", fake_real_entry)
    event = _Ev("ExemptMint", created_at="2026-08-31T09:29:59+00:00")
    event.their_price = 1.0
    event.signal_source = "follow_activity"
    event.lag_s = 1.0

    result = R.real_entry_hook(
        c, event, {"solana": {}}, now_fn=lambda: "2026-08-31T09:30:00+00:00",
        trade_size=37.72, max_age_s=120.0,
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={
            "effective_mcap": 161_600.0,
            "assumed_micro": False,
            "consensus_n_60m": 1,
            "market_stats": {"price": 1.0, "market_cap": 161_600.0},
        },
    )

    assert result == {"status": "rejected", "reason": "test seam"}
    assert captured["chase_max_pct"] == 0.0


def test_chase_exemption_keeps_assumed_nano_exact_quote_cap(monkeypatch):
    c = _c()
    captured = {}
    m.add_wallet(c, "u1", "fmpumpguy")
    m.set_wallet_gate_exemption(
        c, "u1", "chase_past_entry", active=True, actor="test",
        reason="operator override", now_iso="2026-08-31T09:30:00+00:00")
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT", 40.0,
                        raising=False)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)

    def fake_real_entry(*_args, **kwargs):
        captured["chase_max_pct"] = kwargs.get("chase_max_pct")
        return {"status": "rejected", "reason": "test seam"}

    monkeypatch.setattr(R, "real_entry", fake_real_entry)
    event = _Ev("AssumedNanoMint", created_at="2026-08-31T09:29:59+00:00")
    event.their_price = 1.0
    event.signal_source = "follow_activity"
    event.lag_s = 1.0

    result = R.real_entry_hook(
        c, event, {"solana": {}}, now_fn=lambda: "2026-08-31T09:30:00+00:00",
        trade_size=37.72, max_age_s=120.0,
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={
            "effective_mcap": None,
            "assumed_micro": True,
            "consensus_n_60m": 1,
            "market_stats": {"price": 1.0},
        },
    )

    assert result == {"status": "rejected", "reason": "test seam"}
    assert captured["chase_max_pct"] == 40.0


def test_entry_hook_uses_the_nano_quote_chase_cap(monkeypatch):
    c = _c()
    captured = {}
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT", 40.0, raising=False)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)

    def fake_real_entry(*_args, **kwargs):
        captured["chase_max_pct"] = kwargs.get("chase_max_pct")
        return {"status": "rejected", "reason": "test seam"}

    monkeypatch.setattr(R, "real_entry", fake_real_entry)
    event = _Ev("NanoMint", created_at="2026-08-30T18:50:53+00:00")
    event.their_price = 1.0
    event.signal_source = "follow_activity"
    event.lag_s = 3.0

    result = R.real_entry_hook(
        c, event, {"solana": {}}, now_fn=lambda: "2026-08-30T18:50:56+00:00",
        trade_size=37.72, max_age_s=120.0,
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={
            "effective_mcap": 11_867.01,
            "assumed_micro": False,
            "consensus_n_60m": 1,
            "market_stats": {"price": 1.0, "market_cap": 11_867.01},
        },
    )

    assert result == {"status": "rejected", "reason": "test seam"}
    assert captured["chase_max_pct"] == 40.0


def test_entry_hook_defers_evm_route_capacity_probe_until_after_balance(monkeypatch):
    c = _c()
    captured = {}
    probes = []
    m.add_wallet(c, "koy", "koy")
    m.set_wallet_chase_cap(
        c, "koy", network_id="4663", coin_band="nano", cap_pct=75.0,
        actor="operator", reason="approved", now_iso="2026-08-30T18:50:00+00:00",
    )
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT", 40.0, raising=False)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT", 10.0,
                        raising=False)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)

    def fake_real_entry(*_args, **kwargs):
        captured.update(kwargs)
        return {"status": "rejected", "reason": "test seam"}

    def probe(_mint, size):
        probes.append(size)
        return "route_capacity" if size > 10.0 else None

    monkeypatch.setattr(R, "real_entry", fake_real_entry)
    event = _Ev("EvmNanoMint", created_at="2026-08-30T18:50:53+00:00")
    event.network_id = "4663"
    event.their_price = 1.0
    event.signal_source = "follow_activity"
    event.lag_s = 3.0

    result = R.real_entry_hook(
        c, event, {"4663": {"entry_quote_probe_fn": probe}},
        now_fn=lambda: "2026-08-30T18:50:56+00:00",
        trade_size=37.72, max_age_s=120.0,
        resolved_identity=R.ResolvedEntryIdentity("4663", "resolved", "test"),
        classification_context={
            "effective_mcap": 11_867.01,
            "assumed_micro": False,
            "consensus_n_60m": 1,
            "market_stats": {"price": 0.80, "market_cap": 11_867.01},
        },
    )

    assert result == {"status": "rejected", "reason": "test seam"}
    assert captured["chase_max_pct"] == 10.0
    assert captured["wallet_chase_cap_pct"] is None
    assert probes == []
    assert captured["trade_size"] == 37.72
    assert captured["capacity_clip"] is None


def test_evm_entry_does_not_duplicate_capacity_quote_before_first_execution(monkeypatch):
    c = _c()
    event = _Ev("0xcapacity")
    event.network_id = "4663"
    probes = []
    captured = {}

    def probe(_mint, clip):
        probes.append(clip)
        return None

    def fake_entry(*_args, **kwargs):
        captured.update(kwargs)
        return {"status": "filled"}

    import sniper.services.memecoin_evm_leg as evm_leg
    monkeypatch.setattr(evm_leg, "evm_real_entry", fake_entry)
    entry = R._make_evm_entry_fn({
        "4663": {
            "usdc_fn": lambda: 200.0,
            "native_fn": lambda: 1.0,
            "entry_quote_probe_fn": probe,
            "token_safety_fn": lambda _mint: (True, "ok"),
            "execute_fn": lambda *_args, **_kwargs: {"status": "filled"},
        },
    })

    result = entry(
        c, event, route="evm:4663", day="2026-08-31",
        now_iso="2026-08-31T22:57:03+00:00", trade_size=96.66,
    )

    assert result == {"status": "filled"}
    assert probes == []
    assert captured["capacity_clip"] is None


def test_evm_entry_retries_an_exact_capacity_miss_with_a_smaller_reservation(monkeypatch):
    c = _c()
    event = _Ev("0xcapacity-retry")
    event.network_id = "4663"
    probes = []
    entries = []

    def probe(_mint, clip):
        probes.append(clip)
        return None

    def fake_entry(*_args, **kwargs):
        entries.append(kwargs)
        if len(entries) == 1:
            return {
                "status": "aborted",
                "reason": "no quote (route_capacity: relay rejected this clip for route capacity)",
                "quote_miss_code": "route_capacity",
            }
        return {"status": "filled", "pending_id": 2}

    import sniper.services.memecoin_evm_leg as evm_leg
    monkeypatch.setattr(evm_leg, "evm_real_entry", fake_entry)
    entry = R._make_evm_entry_fn({
        "4663": {
            "usdc_fn": lambda: 200.0,
            "native_fn": lambda: 1.0,
            "entry_quote_probe_fn": probe,
            "token_safety_fn": lambda _mint: (True, "ok"),
            "execute_fn": lambda *_args, **_kwargs: {"status": "filled"},
        },
    })

    result = entry(
        c, event, route="evm:4663", day="2026-08-31",
        now_iso="2026-08-31T22:57:03+00:00", trade_size=96.66,
    )

    assert result == {"status": "filled", "pending_id": 2}
    assert probes == [48.33]
    assert entries[0]["capacity_clip"] is None
    assert entries[1]["trade_size"] == 48.33
    assert entries[1]["capacity_clip"] == 48.33
    assert entries[1]["trace_attempt_base"] == 2


def test_the_capacity_retry_halves_what_was_APPROVED_not_the_configured_base(monkeypatch):
    """Attempt 2cf2b654 (4663, 2026-09-24T06:48:06Z) refused "insufficient bankroll" on its OWN
    retry. The wallet held $49.79 spendable (0.01853 WETH at the runner's mark), the risk gate
    approved exactly that, the quote missed on route_capacity — and the retry then halved the
    CONFIGURED base of 100 and asked for $50, which is more than the wallet has. The base is a
    ceiling, never the size that was approved.

    At the 100%-equity tier now configured (MEMECOIN_EVM_TIER_CLIP_USD_4663=100,100,170) every
    route-capacity retry on a wallet under the base hits this, which is why n=1 in seven days
    understates it."""
    c = _c()
    event = _Ev("0xapproved-clip-retry")
    event.network_id = "4663"
    probes, entries = [], []

    def probe(_mint, clip):
        probes.append(clip)
        return None

    def fake_entry(*_args, **kwargs):
        entries.append(kwargs)
        if len(entries) == 1:
            return {
                "status": "aborted",
                "reason": "no quote (route_capacity: relay rejected this clip for route capacity)",
                "quote_miss_code": "route_capacity",
                # what the risk gate actually approved, carried back by evm_real_entry
                "approved_clip_usd": 49.79,
            }
        return {"status": "filled", "pending_id": 3}

    import sniper.services.memecoin_evm_leg as evm_leg
    monkeypatch.setattr(evm_leg, "evm_real_entry", fake_entry)
    entry = R._make_evm_entry_fn({
        "4663": {
            "usdc_fn": lambda: 49.79,          # the whole wallet, at the same snapshot
            "native_fn": lambda: 0.01853,
            "entry_quote_probe_fn": probe,
            "token_safety_fn": lambda _mint: (True, "ok"),
            "execute_fn": lambda *_args, **_kwargs: {"status": "filled"},
        },
    })

    result = entry(c, event, route="evm:4663", day="2026-09-24",
                   now_iso="2026-09-24T06:48:06+00:00", trade_size=100.0)

    assert result == {"status": "filled", "pending_id": 3}
    # 49.79 / 2 rounds to 24.89 under the existing `round(x / 2.0, 2)`, not 24.90 -- the halving
    # arithmetic is unchanged, only WHAT it halves.
    assert probes == [24.89], probes           # half of the APPROVED 49.79, not half of 100
    assert entries[1]["trade_size"] == 24.89
    assert entries[1]["capacity_clip"] == 24.89
    assert entries[1]["trade_size"] <= 49.79, "the retry may never exceed what is spendable"


def test_the_capacity_retry_is_clamped_to_spendable_even_without_an_approved_clip(monkeypatch):
    """The belt to that brace: a result shape with no `approved_clip_usd` (an older release, or a
    path that returns before the gate) falls back to today's arithmetic, but the retry is still
    clamped to the balance read at the SAME snapshot, so it cannot ask for money we do not have."""
    c = _c()
    event = _Ev("0xno-approved-clip")
    event.network_id = "4663"
    probes, entries = [], []

    def probe(_mint, clip):
        probes.append(clip)
        return None

    def fake_entry(*_args, **kwargs):
        entries.append(kwargs)
        if len(entries) == 1:
            return {"status": "aborted", "quote_miss_code": "route_capacity",
                    "reason": "no quote (route_capacity)"}
        return {"status": "filled", "pending_id": 4}

    import sniper.services.memecoin_evm_leg as evm_leg
    monkeypatch.setattr(evm_leg, "evm_real_entry", fake_entry)
    entry = R._make_evm_entry_fn({
        "4663": {
            "usdc_fn": lambda: 49.79,
            "native_fn": lambda: 0.01853,
            "entry_quote_probe_fn": probe,
            "token_safety_fn": lambda _mint: (True, "ok"),
            "execute_fn": lambda *_args, **_kwargs: {"status": "filled"},
        },
    })

    entry(c, event, route="evm:4663", day="2026-09-24",
          now_iso="2026-09-24T06:48:06+00:00", trade_size=100.0)

    assert probes and probes[0] <= 49.79, probes
    assert entries[1]["trade_size"] <= 49.79


def test_a_wallet_that_can_afford_the_base_still_halves_the_base(monkeypatch):
    """The clamp must not shrink a retry that was already affordable: with $200 spendable and a
    $96.66 trade size the retry is 48.33, exactly as before this change."""
    c = _c()
    event = _Ev("0xrich-wallet-retry")
    event.network_id = "4663"
    probes, entries = [], []

    def probe(_mint, clip):
        probes.append(clip)
        return None

    def fake_entry(*_args, **kwargs):
        entries.append(kwargs)
        if len(entries) == 1:
            return {"status": "aborted", "quote_miss_code": "route_capacity",
                    "reason": "no quote (route_capacity)", "approved_clip_usd": 96.66}
        return {"status": "filled", "pending_id": 5}

    import sniper.services.memecoin_evm_leg as evm_leg
    monkeypatch.setattr(evm_leg, "evm_real_entry", fake_entry)
    entry = R._make_evm_entry_fn({
        "4663": {
            "usdc_fn": lambda: 200.0,
            "native_fn": lambda: 1.0,
            "entry_quote_probe_fn": probe,
            "token_safety_fn": lambda _mint: (True, "ok"),
            "execute_fn": lambda *_args, **_kwargs: {"status": "filled"},
        },
    })

    entry(c, event, route="evm:4663", day="2026-09-24",
          now_iso="2026-09-24T06:48:06+00:00", trade_size=96.66)

    assert probes == [48.33], probes
    assert entries[1]["capacity_clip"] == 48.33

def test_evm_entry_retries_a_provider_quote_fault_immediately(monkeypatch):
    c = _c()
    event = _Ev("0xprovider-retry")
    event.network_id = "4663"
    entries = []

    def fake_entry(*_args, **kwargs):
        entries.append(kwargs)
        if len(entries) == 1:
            return {
                "status": "aborted",
                "reason": "no quote (provider_rpc_transport: v4 could not read: rpc_transport)",
                "quote_miss_code": "provider_rpc_transport",
            }
        return {"status": "filled", "pending_id": 2}

    import sniper.services.memecoin_evm_leg as evm_leg
    monkeypatch.setattr(evm_leg, "evm_real_entry", fake_entry)
    entry = R._make_evm_entry_fn({
        "4663": {
            "usdc_fn": lambda: 200.0,
            "native_fn": lambda: 1.0,
            "entry_quote_probe_fn": lambda _mint, _clip: None,
            "token_safety_fn": lambda _mint: (True, "ok"),
            "execute_fn": lambda *_args, **_kwargs: {"status": "filled"},
        },
    })

    result = entry(
        c, event, route="evm:4663", day="2026-09-01",
        now_iso="2026-09-01T13:04:39+00:00", trade_size=140.50,
    )

    assert result == {"status": "filled", "pending_id": 2}
    assert len(entries) == 2
    assert entries[0]["trade_size"] == entries[1]["trade_size"] == 140.50
    assert entries[0]["capacity_clip"] is entries[1]["capacity_clip"] is None
    assert entries[1]["trace_attempt_base"] == 2


def test_entry_hook_uses_the_normal_cap_and_scoped_wallet_cap_for_a_matching_4663_nano(monkeypatch):
    c = _c()
    captured = {}
    m.add_wallet(c, "koy", "koy")
    m.set_wallet_chase_cap(
        c, "koy", network_id="4663", coin_band="nano", cap_pct=75.0,
        actor="operator", reason="approved", now_iso="2026-08-31T16:30:00+00:00",
    )
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", 40.0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT", 40.0, raising=False)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT", 10.0,
                        raising=False)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)

    def fake_real_entry(*_args, **kwargs):
        captured.update(kwargs)
        return {"status": "rejected", "reason": "test seam"}

    monkeypatch.setattr(R, "real_entry", fake_real_entry)
    event = _Ev("EvmNanoMint", uid="koy", created_at="2026-08-31T16:33:06+00:00")
    event.network_id = "4663"
    event.their_price = 1.0
    event.signal_source = "follow_activity"
    event.lag_s = 30.6

    result = R.real_entry_hook(
        c, event, {"4663": {}}, now_fn=lambda: "2026-08-31T16:33:36+00:00",
        trade_size=37.72, max_age_s=120.0,
        resolved_identity=R.ResolvedEntryIdentity("4663", "resolved", "test"),
        classification_context={
            "effective_mcap": 46_083.52,
            "assumed_micro": False,
            "consensus_n_60m": 1,
            "market_stats": {"price": 0.98, "market_cap": 46_083.52},
        },
    )

    assert result == {"status": "rejected", "reason": "test seam"}
    assert captured["chase_max_pct"] == 40.0
    assert captured["wallet_chase_cap_pct"] == 75.0
    assert captured["wallet_chase_cap_expires_at"] == "2026-08-31T16:34:06+00:00"


def test_entry_hook_refuses_nonreservable_band_before_balance_reads(monkeypatch):
    c = _c()
    reads = {"n": 0}
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_RESERVABLE_BANDS", ("small", "mid"))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)
    monkeypatch.setattr(
        R, "real_entry",
        lambda *_a, **_k: {"status": "rejected", "reason": "reservation seam"},
    )

    def balance_read():
        reads["n"] += 1
        return 100.0

    result = R.real_entry_hook(
        c,
        _Ev("MicroMint", created_at="2026-07-28T12:00:00+00:00"),
        {"solana": {
            "usdc_fn": balance_read,
            "sol_fn": balance_read,
            "token_safety_fn": lambda _mint: (True, "ok"),
            "execute_fn": lambda *_a, **_k: {"status": "filled"},
            "kill_path": None,
        }},
        now_fn=lambda: "2026-07-28T12:00:01+00:00",
        trade_size=100.0,
        max_age_s=120.0,
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={
            "effective_mcap": 100_000.0,
            "assumed_micro": False,
            "consensus_n_60m": 1,
            "market_stats": {"price": 1.0, "liquidity": 10_000.0},
        },
    )

    assert result == {"status": "rejected", "reason": "coin band is not reservable: 'micro'"}
    assert reads["n"] == 0


def test_entry_hook_reuses_already_fetched_market_stats_for_classification(monkeypatch):
    c = _c()
    captured = {}
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)
    monkeypatch.setattr(
        R, "real_entry",
        lambda *_a, **kw: (captured.setdefault("classification", kw["classification"]),
                           {"status": "rejected", "reason": "test seam"})[1],
    )
    ev = _Ev("CachedMint", created_at="2026-07-18T11:59:59+00:00")
    ev.their_price = 1.0

    R.real_entry_hook(
        c, ev, {"solana": {}}, now_fn=lambda: "2026-07-18T12:00:00+00:00",
        trade_size=100.0, max_age_s=120.0,
        stats_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("duplicate stats call")),
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={
            "effective_mcap": 500_000.0,
            "assumed_micro": False,
            "consensus_n_60m": 1,
            "market_stats": {"price": 1.0, "liquidity": 5_000.0},
        },
    )

    assert captured["classification"].coin_band == "small"


def test_entry_hook_carries_cached_market_symbol_to_follow_alert_event(monkeypatch):
    c = _c()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)
    ev = _Ev("ClawdMint", created_at="2026-08-12T17:18:59+00:00")
    ev.ticker = ""
    ev.their_price = 1.0

    result = R.real_entry_hook(
        c, ev, {"solana": {}}, now_fn=lambda: "2026-08-12T17:19:00+00:00",
        trade_size=100.0, max_age_s=120.0,
        stats_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("duplicate stats call")),
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={
            "effective_mcap": 325_787.0,
            "assumed_micro": False,
            "consensus_n_60m": 2,
            "market_stats": {"price": 4.5, "liquidity": 78_228.0, "symbol": "CLAWD"},
        },
    )

    assert result["status"] == "skipped_chase"
    assert ev.ticker == "CLAWD"


def test_make_hook_persists_classification_for_terminal_pre_reservation_attempt(monkeypatch):
    c = _c()
    m.add_wallet(c, "not-approved", "known-source")
    hook = R.make_real_hook(
        None, {"solana": object()}, now_fn=lambda: "2026-07-18T12:00:00+00:00",
        trade_size=10.0, max_age_s=120.0, blind_mcap_fn=lambda *_a: None,
    )
    ev = _Ev("AuditMint", uid="not-approved", created_at="2026-07-18T11:59:59+00:00")
    ev.their_market_cap = 500_000.0
    ev.signal_source = "global_feed"

    result = hook(c, ev)

    assert result["status"] == "not_approved"
    row = c.execute(
        "SELECT classification_json,funded_contract_json FROM memecoin_entry_attempts"
    ).fetchone()
    assert json.loads(row["classification_json"])["coin_band"] == "small"
    assert row["funded_contract_json"] is None


def test_make_hook_refuses_identity_free_automatic_source_even_if_stale_armed(monkeypatch):
    c = _c()
    m.add_wallet(c, "unknown", "")
    c.execute("UPDATE memecoin_wallets SET real_approved=1, real_chains='solana' "
              "WHERE source_user_id='unknown'")
    c.commit()
    monkeypatch.setattr(R, "real_entry_hook", lambda *_a, **_kw: {"status": "would_execute"})
    hook = R.make_real_hook(
        None, {"solana": object()}, now_fn=lambda: "2026-07-18T12:00:00+00:00",
        trade_size=10.0, max_age_s=120.0,
    )
    ev = _Ev("IdentityMint", uid="unknown", created_at="2026-07-18T11:59:59+00:00")
    ev.signal_source = "follow_activity"
    ev.source_handle = ""
    ev.their_market_cap = 500_000.0

    result = hook(c, ev)

    assert result["status"] == "unknown_source"
    row = c.execute("SELECT terminal_status, terminal_reason FROM memecoin_entry_attempts").fetchone()
    assert dict(row) == {"terminal_status": "refused", "terminal_reason": "unknown_source"}


def test_mid_funded_hook_persists_mid_contract_without_legacy_override(monkeypatch, tmp_path):
    c = _c()
    m.add_wallet(c, "mid-wallet", "mid")
    m.set_wallet_real_approved(c, "mid-wallet", True, chains=["solana", "8453", "4663"], force=True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 10.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 2_000_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)
    monkeypatch.setattr(R.m, "prior_buyers_60m", lambda *_a, **_k: 1)

    def execute(conn, pending_id, mint, size, now_iso, **_kwargs):
        pending = m.pending_row(conn, pending_id)
        contract_fields = m.validated_pending_funded_contract(conn, pending)
        position_id = m.open_position(
            conn, source_user_id=pending["source_user_id"], source_handle="(real)",
            mint=mint, ticker="", entry_price=1.0, entry_notional=size,
            entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
            opened_at=now_iso, strategy=pending["strategy"], execution_mode="real",
            network_id="solana", attempt_id=pending["attempt_id"], **contract_fields,
        )
        m.confirm_pending(conn, pending_id)
        return {"status": "filled", "position_id": position_id, "notional": size}

    context = {
        "usdc_fn": lambda: 300.0,
        "sol_fn": lambda: 1.0,
        "token_safety_fn": lambda _mint: (True, "ok"),
        "execute_fn": execute,
        "kill_path": str(tmp_path / "kill"),
    }
    hook = R.make_real_hook(
        None, {"solana": context}, now_fn=lambda: "2026-07-18T12:00:00+00:00",
        trade_size=10.0, max_age_s=120.0,
        stats_fn=lambda *_a, **_k: {
            "price": 1.0, "liquidity": 10_000.0,
            "pair_created_at": 1_784_372_400_000,
        },
        blind_mcap_fn=lambda *_a: None,
    )
    ev = _Ev("MidMint", uid="mid-wallet", created_at="2026-07-18T11:59:59+00:00")
    ev.their_market_cap = 1_500_000.0
    ev.their_price = 1.0
    ev.signal_source = "global_feed"
    ev.source_handle = "mid"
    ev.ticker = "MID"

    result = hook(c, ev)

    assert result["status"] == "filled"
    position = m.get_position(c, result["position_id"])
    contract = json.loads(position["funded_contract_json"])
    assert contract["classification"]["coin_band"] == "mid"
    assert contract["policy"]["policy_id"] == "funded-mid-v1"
    assert contract["policy"]["derisk_pct"] == pytest.approx(30.0)
    assert contract["policy"]["trail_pct"] == pytest.approx(35.0)
    assert m.effective_exit_override(c, result["position_id"]) is None


# --- mirror-on-sell ---


def test_exact_sell_remaining_position_preserves_zero_and_partial_usd():
    c = _c()
    _insert_observation(c, "partial-sell", "u1", "2026-07-15T14:59:00+00:00",
                        side="sell", remaining_usd=2394.31)
    _insert_observation(c, "full-sell", "u1", "2026-07-15T15:00:00+00:00",
                        side="sell", remaining_usd=0.0)

    identity = {"source_user_id": "u1", "network_id": "4663", "mint": "0xcoin"}
    assert m.source_remaining_usd_for_event(
        c, "partial-sell", **identity) == pytest.approx(2394.31)
    assert m.source_remaining_usd_for_event(c, "full-sell", **identity) == 0.0
    assert m.source_remaining_usd_for_event(c, "missing", **identity) is None


def test_a_j7_relayed_sell_is_not_read_as_a_remaining_holding():
    """Beta, 2026-09-15: J7 (sniper/feeds/j7_feed.py) relays only the TRADE size and used to write
    it under "currentSizeUsd" -- the same key fomo's own /swaps-poll responses use for a genuine
    remaining position (memecoin_holder_evidence.reconcile_follow_poll). db_memecoin's reader had
    no way to tell the two apart, so a J7 sell's own trade size read as "source still holds
    $<trade size>", reaching the live mirror-on-sell gate. J7 now writes "tradeSizeUsd" instead;
    this pins that source_remaining_usd_for_event reports the holding as unknown (None) for a
    J7-shaped body, not the trade's own size."""
    c = _c()
    m.insert_feed_observation_v2(c, {
        "upstream_event_id": "j7-sell-1", "raw_type": "large_sell", "normalized_side": "sell",
        "source_user_id": "u1", "source_handle": "u1", "network_id": "4663", "mint": "0xcoin",
        "ticker": "T", "their_price": 1.0, "their_market_cap": 1_000_000.0,
        "their_size_usd": 729.14, "source_ts_raw": "2026-09-15T09:56:02+00:00",
        "source_ts": "2026-09-15T09:56:02+00:00", "observed_at": "2026-09-15T09:56:02+00:00",
        "lag_s": 0.2,
        # the exact shape j7_feed.fomo_event_to_feed_item writes today -- no "currentSizeUsd"
        "raw_payload": {"body": {"userId": "u1", "userHandle": "u1", "ticker": "T", "price": 1.0,
                                 "marketCap": 1_000_000.0, "tradeSizeUsd": 729.14,
                                 "networkId": "4663", "j7_received_at": 1789000000000,
                                 "j7_source": "trading_activity_ws"}},
    }, was_curated=True, was_real_approved=True)

    remaining = m.source_remaining_usd_for_event(
        c, "j7-sell-1", source_user_id="u1", network_id="4663", mint="0xcoin")

    assert remaining is None, (
        "a J7-relayed sell's own trade size ($729.14) must not read as the trader still "
        "holding that amount")


def test_active_armed_holders_require_latest_fresh_exact_chain_material_position():
    c = _c()
    for uid in ("holder", "sold", "paused", "excluded", "cross-chain", "stale", "dust"):
        _arm(c, uid, paused=(uid == "paused"), excluded=(uid == "excluded"))
    m.add_wallet(c, "unarmed", "unarmed")

    _insert_observation(c, "holder-buy", "holder", "2026-07-15T14:40:00+00:00",
                        remaining_usd=500.0)
    _insert_observation(c, "sold-buy", "sold", "2026-07-15T14:30:00+00:00",
                        remaining_usd=800.0)
    _insert_observation(c, "sold-full", "sold", "2026-07-15T14:50:00+00:00",
                        side="sell", remaining_usd=0.0)
    _insert_observation(c, "paused-buy", "paused", "2026-07-15T14:40:00+00:00",
                        remaining_usd=900.0)
    _insert_observation(c, "excluded-buy", "excluded", "2026-07-15T14:40:00+00:00",
                        remaining_usd=900.0)
    _insert_observation(c, "unarmed-buy", "unarmed", "2026-07-15T14:40:00+00:00",
                        remaining_usd=900.0)
    _insert_observation(c, "cross-buy", "cross-chain", "2026-07-15T14:40:00+00:00",
                        network_id="8453", remaining_usd=900.0)
    _insert_observation(c, "stale-buy", "stale", "2026-07-15T13:59:59+00:00",
                        remaining_usd=900.0)
    _insert_observation(c, "dust-buy", "dust", "2026-07-15T14:40:00+00:00",
                        remaining_usd=0.99)

    holders = m.active_armed_holders_for_coin(
        c, network_id="4663", mint="0xcoin", exclude_user_id="origin",
        observed_at="2026-07-15T15:00:00+00:00", window_s=3600,
        min_remaining_usd=1.0,
    )

    assert holders == [{
        "source_user_id": "holder",
        "source_handle": "holder",
        "remaining_usd": 500.0,
        "observed_at": "2026-07-15T14:40:00+00:00",
    }]


def test_mirror_on_sell_exits_matching_real_position(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)  # legacy feed mode
    c = _c()
    pid = _open_real(c, "Msell")
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append((pos["id"], reason)),
                                                     {"status": "closed"})[1]}
    R.real_mirror_on_sell(c, _Ev("Msell", side="sell"), {"solana": rc}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == [(pid, "mirror")]


def test_mirror_on_sell_holds_when_exact_source_sell_reports_remaining_position(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)  # legacy feed mode
    c = _c()
    pid = _open_real(c, "Mpartial")
    _insert_observation(c, "partial-event", "u1", "2026-07-15T14:59:59+00:00",
                        mint="Mpartial", network_id="solana", side="sell",
                        remaining_usd=2394.31)
    calls = []
    rc = {"exit_fn": lambda *args: calls.append(args) or {"status": "closed"}}

    res = R.real_mirror_on_sell(
        c, _Ev("Mpartial", side="sell", event_id="partial-event"), {"solana": rc},
        now_fn=lambda: "2026-07-15T15:00:00+00:00",
    )

    assert res == {
        "status": "mirror_held",
        "reason": "source_still_holds",
        "position_id": pid,
        "source_remaining_usd": pytest.approx(2394.31),
        "consensus_count": 0,
        "consensus_handles": [],
    }
    assert calls == []
    audit = c.execute(
        "SELECT allowed, detail FROM memecoin_exit_trigger_audit WHERE position_id=? AND reason='mirror'",
        (pid,),
    ).fetchone()
    assert audit["allowed"] == 0
    assert json.loads(audit["detail"])["code"] == "source_still_holds"
    assert json.loads(audit["detail"])["origin_event_id"] == "partial-event"


def test_mirror_on_sell_holds_when_other_fresh_active_armed_wallet_still_holds(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)  # legacy feed mode
    c = _c()
    pid = _open_real(c, "Mconsensus")
    _insert_observation(c, "full-event", "u1", "2026-07-15T14:59:59+00:00",
                        mint="Mconsensus", network_id="solana", side="sell", remaining_usd=0.0)
    _arm(c, "u2")
    _insert_observation(c, "other-buy", "u2", "2026-07-15T14:45:00+00:00",
                        mint="Mconsensus", network_id="solana", remaining_usd=500.0)
    calls = []
    rc = {"exit_fn": lambda *args: calls.append(args) or {"status": "closed"}}

    res = R.real_mirror_on_sell(
        c, _Ev("Mconsensus", side="sell", event_id="full-event"), {"solana": rc},
        now_fn=lambda: "2026-07-15T15:00:00+00:00",
    )

    assert res["status"] == "mirror_held"
    assert res["reason"] == "armed_consensus"
    assert res["consensus_count"] == 1
    assert res["consensus_handles"] == ["u2"]
    assert calls == []
    audit = c.execute(
        "SELECT allowed, detail FROM memecoin_exit_trigger_audit WHERE position_id=? AND reason='mirror'",
        (pid,),
    ).fetchone()
    detail = json.loads(audit["detail"])
    assert audit["allowed"] == 0
    assert detail["code"] == "armed_consensus"
    assert detail["network_id"] == "solana"
    assert detail["mint"] == "Mconsensus"


def test_mirror_on_sell_full_exit_without_consensus_preserves_existing_executor_path(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)  # legacy feed mode
    c = _c()
    pid = _open_real(c, "Mfull")
    _insert_observation(c, "full-event", "u1", "2026-07-15T14:59:59+00:00",
                        mint="Mfull", network_id="solana", side="sell", remaining_usd=0.0)
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (
        calls.append((pos["id"], reason, now)), {"status": "closed"})[1]}

    res = R.real_mirror_on_sell(
        c, _Ev("Mfull", side="sell", event_id="full-event"), {"solana": rc},
        now_fn=lambda: "2026-07-15T15:00:00+00:00",
    )

    assert res == {"status": "closed"}
    assert calls == [(pid, "mirror", "2026-07-15T15:00:00+00:00")]
    audit = c.execute(
        "SELECT allowed, detail FROM memecoin_exit_trigger_audit WHERE position_id=? AND reason='mirror'",
        (pid,),
    ).fetchone()
    assert audit["allowed"] == 1
    assert json.loads(audit["detail"])["code"] == "mirror_allowed"


def test_mirror_on_sell_rejects_cross_chain_source_remainder_evidence(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)  # legacy feed mode
    c = _c()
    pid = _open_real(c, "Msame")
    _insert_observation(c, "wrong-chain-event", "u1", "2026-07-15T14:59:59+00:00",
                        mint="Msame", network_id="8453", side="sell", remaining_usd=500.0)
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (
        calls.append((pos["id"], reason)), {"status": "closed"})[1]}

    res = R.real_mirror_on_sell(
        c, _Ev("Msame", side="sell", event_id="wrong-chain-event"), {"solana": rc},
        now_fn=lambda: "2026-07-15T15:00:00+00:00",
    )

    assert res == {"status": "closed"}
    assert calls == [(pid, "mirror")]


def test_mirror_on_sell_noop_without_position():
    c = _c()
    rc = {"exit_fn": lambda *a, **k: pytest.fail("exit_fn must not fire without a held position")}
    assert R.real_mirror_on_sell(c, _Ev("nothing", side="sell"), {"solana": rc}, now_fn=lambda: "t") is None


# --- per-position mirror-exit override (operator can hold past the trader's sell) ---

def test_mirror_disabled_column_defaults_off():
    c = _c()
    pid = _open_real(c, "Mdefault")
    assert not m.get_position(c, pid).get("mirror_disabled")   # default = mirror ACTIVE


def test_mirror_on_sell_skipped_when_position_mirror_disabled():
    c = _c()
    pid = _open_real(c, "Mhold")
    m.set_position_mirror_disabled(c, pid, True)
    rc = {"exit_fn": lambda *a, **k: pytest.fail("mirror-disabled position must NOT auto-sell on trader sell")}
    res = R.real_mirror_on_sell(c, _Ev("Mhold", side="sell"), {"solana": rc},
                                now_fn=lambda: "2026-07-09T00:05:00Z")
    assert res == {"status": "mirror_disabled", "position_id": pid}


def test_mirror_re_enabled_resumes_mirror_exit(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)  # legacy feed mode
    c = _c()
    pid = _open_real(c, "Mtoggle")
    m.set_position_mirror_disabled(c, pid, True)
    m.set_position_mirror_disabled(c, pid, False)     # operator turns the copy-exit back on
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append((pos["id"], reason)),
                                                     {"status": "closed"})[1]}
    R.real_mirror_on_sell(c, _Ev("Mtoggle", side="sell"), {"solana": rc},
                          now_fn=lambda: "2026-07-09T00:05:00Z")
    assert calls == [(pid, "mirror")]


def test_real_mark_producer_keys_same_address_by_chain():
    mark = R.make_real_mark_fn({
        "8453": {"preview_exit_fn": lambda _pos: {"expected_usdc": 20.0}},
        "4663": {"preview_exit_fn": lambda _pos: {"expected_usdc": 30.0}},
    })
    positions = [
        {"id": 1, "mint": "0xAbC", "network_id": "8453",
         "entry_price": 0.1, "entry_notional": 10.0, "open_qty": 100.0},
        {"id": 2, "mint": "0xAbC", "network_id": "4663",
         "entry_price": 0.1, "entry_notional": 10.0, "open_qty": 100.0},
    ]

    assert mark(positions) == pytest.approx({
        ("8453", "0xabc"): 0.2,
        ("4663", "0xabc"): 0.3,
    })


def test_real_mark_uses_remaining_quantity_after_derisk():
    remaining = 10.0 - (10.0 / 1.4)
    mark = R.make_real_mark_fn({
        "4663": {"preview_exit_fn": lambda _pos: {"expected_usdc": remaining * 3.0}},
    })

    result = mark([{
        "id": 1, "mint": "0xPostDerisk", "network_id": "4663",
        "entry_price": 1.0, "entry_notional": 10.0, "open_qty": remaining,
    }])

    assert result[("4663", "0xpostderisk")] == pytest.approx(3.0)


# --- build_real_context guards ---

def test_build_real_context_none_when_disarmed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)
    assert R.build_real_context() is None


def test_build_real_context_none_when_keypair_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "nope.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "https://rpc")
    assert R.build_real_context() is None


def test_build_real_context_none_when_no_rpc(monkeypatch, tmp_path):
    from sniper.execution.solana_wallet import generate_keypair_file
    kpf = str(tmp_path / "kp.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "")
    assert R.build_real_context() is None


def test_build_real_context_builds_when_armed_with_creds(monkeypatch, tmp_path):
    import json

    from sniper.execution.solana_wallet import generate_keypair_file
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    kpf = str(tmp_path / "kp.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "https://rpc")
    monkeypatch.delenv("MEMECOIN_SOLANA_SUBMIT_MODE", raising=False)
    def http(_method, _url, **kwargs):
        rpc_method = json.loads(kwargs["body"])["method"]
        if rpc_method == "getHealth":
            return 200, {"result": "ok"}, None
        if rpc_method == "getGenesisHash":
            return 200, {"result": SOLANA_MAINNET_GENESIS}, None
        return 200, {}, None

    rc = R.build_real_context(http=http)
    assert rc is not None
    assert rc["submission_backend"] == "auto_free"      # free-direct is the zero-config default
    for key in ("execute_fn", "exit_fn", "token_safety_fn", "usdc_fn", "sol_fn", "token_fn"):
        assert callable(rc[key]), key


def test_build_real_context_shares_credit_pool_and_keeps_sidecar_on_public_fallback(
        monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    from sniper.execution import submission_backends as backends
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    from sniper.execution.solana_wallet import generate_keypair_file
    import sniper.execution.solana_tpu_sidecar as sidecar

    kpf = str(tmp_path / "kp.json")
    generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_POOL_URLS", (
        "https://h1/rpc", "https://h2/rpc", "https://h3/rpc"))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "https://public/rpc")
    monkeypatch.delenv("MEMECOIN_SOLANA_SUBMIT_MODE", raising=False)
    sidecar_urls = []
    monkeypatch.setattr(
        sidecar, "resolve_tpu_sidecar",
        lambda **kwargs: sidecar_urls.append(kwargs["rpc_url"])
        or SimpleNamespace(process=None, reason="test_no_process"),
    )
    calls = []

    def http(_method, url, **kwargs):
        rpc_method = json.loads(kwargs["body"])["method"]
        calls.append((url, rpc_method))
        if rpc_method == "getHealth":
            return 200, {"result": "ok"}, None
        if rpc_method == "getGenesisHash":
            if url == "https://h1/rpc":
                return 200, {"error": {"code": -32429, "message": "max usage reached"}}, None
            return 200, {"result": SOLANA_MAINNET_GENESIS}, None
        if rpc_method == "getBalance":
            return 200, {"result": {"value": 1_000_000_000}}, None
        return 200, {}, None

    try:
        context = R.build_real_context(http=http)
        assert context is not None
        _snap = context["rpc_pool_status_fn"]()
        assert {k: _snap[k] for k in
                ("active_index", "endpoint_count", "exhausted_count", "fallback_active")} == {
            "active_index": 1,
            "endpoint_count": 4,
            "exhausted_count": 1,
            "fallback_active": False,
        }
        # Added 2026-08-25 so the heartbeat can answer "are we on the fastest path"; the old
        # snapshot reported healthy all day while rotation was unreachable.
        assert _snap["degraded"] is True and _snap["last_reason"] == "credit_exhausted"
        assert sidecar_urls == ["https://public/rpc"]
        sol_read = context["sol_fn"]()
        assert sol_read.ok is True and sol_read.balance == 1.0
        assert calls[-1] == ("https://h2/rpc", "getBalance")
    finally:
        backends.close_all()


def test_non_owner_context_never_resolves_or_rebinds_tpu_sidecar(monkeypatch, tmp_path):
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    from sniper.execution.solana_wallet import generate_keypair_file
    import sniper.execution.solana_tpu_sidecar as sidecar

    kpf = str(tmp_path / "kp.json")
    generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "https://rpc")
    monkeypatch.delenv("MEMECOIN_SOLANA_SUBMIT_MODE", raising=False)
    monkeypatch.setattr(
        sidecar, "resolve_tpu_sidecar",
        lambda **kwargs: pytest.fail("non-owner attempted to resolve TPU sidecar"),
    )

    def http(_method, _url, **kwargs):
        rpc_method = json.loads(kwargs["body"])["method"]
        if rpc_method == "getHealth":
            return 200, {"result": "ok"}, None
        if rpc_method == "getGenesisHash":
            return 200, {"result": SOLANA_MAINNET_GENESIS}, None
        return 200, {}, None

    assert R.build_real_context(http=http, own_sidecar=False) is not None


def test_context_withdrawal_uses_funded_reads_and_audited_same_byte_transport(monkeypatch, tmp_path):
    import base64
    import json
    from solders.keypair import Keypair
    from solders.transaction import Transaction
    from sniper.execution import submission_backends as backends
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    from sniper.execution.solana_wallet import generate_keypair_file, load_keypair
    from sniper.execution.solana_usdc_transfer import build_signed_transfer

    kpf = str(tmp_path / 'kp.json')
    generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_KEYPAIR', kpf)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_POOL_URLS', ('https://h1/rpc', 'https://h2/rpc', 'https://h3/rpc'))
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_URL', 'https://public/rpc')
    monkeypatch.delenv('MEMECOIN_SOLANA_SUBMIT_MODE', raising=False)
    calls = []

    def http(_method, url, **kwargs):
        body = json.loads(kwargs['body'])
        calls.append((url, body['method'], body.get('params', [])))
        result = {'getHealth': 'ok', 'getGenesisHash': SOLANA_MAINNET_GENESIS,
                  'getAccountInfo': {'value': None},
                  'getLatestBlockhash': {'value': {'blockhash': '11111111111111111111111111111111', 'lastValidBlockHeight': 500}}}.get(body['method'])
        if body['method'] == 'sendTransaction':
            result = str(Transaction.from_bytes(base64.b64decode(body['params'][0])).signatures[0])
        return 200, {'result': result}, None

    c = _c()
    now = '2026-10-10T22:50:00+00:00'
    try:
        ctx = R.build_real_context(http=http, own_sidecar=False)
        assert not any(method in ('getAccountInfo', 'getLatestBlockhash', 'sendTransaction') for _, method, _ in calls)
        assert ctx['withdraw_rpc_url'] == 'https://h1/rpc'
        kp = load_keypair(kpf)
        to = str(Keypair().pubkey())
        signed, sig, height, _ = build_signed_transfer(kp, str(kp.pubkey()), to, 100_000_000,
                rpc_url='https://public/rpc', call_fn=ctx['withdraw_call_fn'])
        assert all(url == 'https://h1/rpc' for url, method, _ in calls if method in ('getAccountInfo', 'getLatestBlockhash'))
        iid = m.enqueue_withdraw(c, 'solana', to, 100, now_iso=now)
        m.resolve_withdraw(c, iid, 'submitted', now_iso=now, sig=sig, last_valid_height=height)
        sender = ctx['withdraw_send_factory_fn'](c, iid)
        assert sender(signed, validity={'last_valid_height': height}) == sig
        sends = [params for _, method, params in calls if method == 'sendTransaction']
        assert sends and all(params[0] == signed for params in sends)
        assert all(params[1]['preflightCommitment'] == 'confirmed' for params in sends)
        row = c.execute('SELECT action, owner_kind, owner_id, tx_identity FROM memecoin_submission_attempts LIMIT 1').fetchone()
        assert tuple(row) == ('withdraw', 'withdraw_intent', iid, sig)
        assert c.execute('SELECT COUNT(*) FROM memecoin_pending_entries').fetchone()[0] == 0
    finally:
        c.close()
        backends.close_all()


def test_tpu_status_for_contexts_is_sanitized_and_explicit_when_unavailable():
    status = {
        "configured": True, "ready": False, "running": True,
        "restart_count": 1, "leader_count": None,
        "last_health_age_s": 3.0, "reason": "socket_unavailable",
    }
    contexts = {"solana": {"tpu_status_fn": lambda: status}}

    assert R.tpu_status_for_contexts(contexts) == status
    assert R.tpu_status_for_contexts({}) == {
        "configured": False, "ready": False, "running": False,
        "restart_count": 0, "leader_count": None,
        "last_health_age_s": None, "reason": "not_configured",
    }


def test_rpc_pool_status_for_contexts_is_sanitized_and_explicit_when_unavailable():
    status = {
        "active_index": 2, "endpoint_count": 4,
        "exhausted_count": 2, "fallback_active": False,
    }
    contexts = {"solana": {"rpc_pool_status_fn": lambda: status}}

    got = R.rpc_pool_status_for_contexts(contexts)
    assert {k: got[k] for k in status} == status
    assert R.rpc_pool_status_for_contexts({}) == {
        "active_index": 0, "endpoint_count": 0,
        "exhausted_count": 0, "fallback_active": False,
        "degraded": False, "blocked_indexes": [], "last_reason": None,
        "rotation_age_s": None, "calls_by_label": {}, "calls_per_minute": [],
        "call_count_failures": 0,
    }


def test_rpc_pool_status_carries_the_degraded_diagnostics_to_the_heartbeat():
    """Regression: the projection is a whitelist, so snapshot() fields it does not name are
    dropped. `degraded` and friends were added to answer "are we on the fastest path" and never
    reached the heartbeat because this function did not list them."""
    import time as _time
    snap = {
        "active_index": 3, "endpoint_count": 5, "exhausted_count": 2,
        "fallback_active": False, "degraded": True, "blocked_indexes": [0, 1],
        "last_reason": "http_429", "last_rotation_at": _time.monotonic() - 12.0,
    }
    got = R.rpc_pool_status_for_contexts({"solana": {"rpc_pool_status_fn": lambda: snap}})
    assert got["degraded"] is True
    assert got["blocked_indexes"] == [0, 1]
    assert got["last_reason"] == "http_429"
    # a monotonic stamp is meaningless off-process; it is reported as an age
    assert 11.0 <= got["rotation_age_s"] <= 20.0


@pytest.mark.parametrize(("guard", "error"), [
    (None, False), (None, True), (("tx_sig", "signed"), False),
    (("submission_bound_at", "bound"), False), (("status", "reserved"), False),
])
def test_capacity_retry_freezes_a_new_contract_and_finishes_its_own_attempt(monkeypatch, guard, error):
    import time
    from concurrent.futures import Future
    from sniper.services.memecoin_entry_trace import EntryAttemptTrace
    from sniper.services import memecoin_evm_risk as E
    from test_memecoin_funded_contract_storage import _classification

    c = _c()
    ev = _Ev('0xMINT', created_at='2026-07-18T12:00:00+00:00')
    ev.network_id = '4663'
    ev.signal_source = 'global_feed'
    trace = EntryAttemptTrace.start(c, ev, ev.created_at, time)
    tape_read, momentum_read = Future(), Future()
    tape_read.set_result({'move_pct': 0.0, 'why': 'ok', 'measured_at_s': 100.0})
    momentum_read.set_exception(TimeoutError('shadow_queue_expired'))
    trace.tape_move_shadow = {'future': tape_read, 'peeked': True,
                             'decision_at_s': 99.0, 'display_move_pct': 0.0}
    trace.momentum_own = {'future': momentum_read, 'peeked': 'not_ready',
                         'classified_mono': time.monotonic()}
    from sniper.services.memecoin_evm_post_trade import EvmChaseReference
    source_tx = '0x' + 'a9' * 32
    receipt_read = Future()
    observation = {'network_id': '4663', 'source_tx': source_tx, 'source_block_number': 123}
    receipt_read.set_result({'source_receipt_observation': observation})
    trace.evm_receipt_timing = ('4663', source_tx, EvmChaseReference('4663', future=receipt_read))
    classification = _classification(network_id='4663', mint=ev.mint, risk_scale=1.0)
    for key, value in {'MEMECOIN_REAL_ENABLED': True, 'MEMECOIN_REAL_SIZING': 'fixed',
                       'MEMECOIN_TRADE_SIZE_USD': 93.4, 'MEMECOIN_MIN_TRADE_FLOOR_USD': 1.0,
                       'MEMECOIN_MAX_TOTAL_EXPOSURE_USD': 700.0}.items():
        monkeypatch.setattr(cfg, key, value)
    reservations = []
    trace.copy_cap_band_without_override = 30.0       # the hook's trace-only band for an override
    bands = []

    def entry(conn, event, **kw):
        bands.append(getattr(kw['trace'], 'copy_cap_band_without_override', None))
        decision, pid = E.reserve_entry_evm(
            conn, mint=event.mint, source_user_id=event.source_user_id, strategy='alpha_b',
            network_id='4663', day='2026-07-18', usdc_balance=300.0, native_balance=1.0,
            now_iso=ev.created_at, attempt_id=kw['trace'].attempt_id,
            origin_event_id=event.fomo_event_id, signal_source=event.signal_source,
            classification=kw['classification'], size_override=kw['capacity_clip'])
        if not decision.allowed:
            return {'status': 'rejected', 'reason': decision.reason}
        reservations.append(m.pending_row(c, pid))
        m.release_pending(c, pid, 'quote_route_capacity')
        if guard is not None and len(reservations) == 1:
            key, value = guard
            c.execute(f'UPDATE memecoin_pending_entries SET {key}=? WHERE id=?', (value, pid))
            c.commit()
        return {'status': 'aborted', 'reason': 'no quote (route_capacity)',
                'quote_miss_code': 'route_capacity', 'pending_id': pid,
                'approved_clip_usd': decision.approved_size_usdc}

    import sniper.services.memecoin_evm_leg as leg
    monkeypatch.setattr(leg, 'evm_real_entry', entry)
    notified = []
    monkeypatch.setattr(R, '_notify_entry_projection', notified.append)
    fn = R._make_evm_entry_fn({'4663': {'usdc_fn': lambda: 300.0, 'native_fn': lambda: 1.0,
        'entry_quote_probe_fn': lambda *a: None, 'token_safety_fn': None, 'execute_fn': None}})
    result = fn(c, ev, route='evm:4663', day='2026-07-18', now_iso=ev.created_at,
                trade_size=93.4, trace=trace, classification=classification)
    if guard is not None:
        assert len(reservations) == 1
        assert result['reason'] == 'no quote (route_capacity)'
        assert not hasattr(trace, 'retry_trace')
        return
    assert len(reservations) == 2, result
    assert bands == [30.0, 30.0], 'the retry trace keeps the band without the override'
    assert reservations[0]['attempt_id'] != reservations[1]['attempt_id']
    assert [r['reserved_notional_usd'] for r in reservations] == [93.4, 46.7]
    if error:
        R._finish_trace_error_best_effort(trace, RuntimeError('retry failed'))
    else:
        R._finish_trace_for_result(trace, result)
    attempts = [dict(r) for r in c.execute('SELECT * FROM memecoin_entry_attempts ORDER BY rowid')]
    assert len(attempts) == 2
    assert notified == [attempts[1]['attempt_id']]
    for i, (attempt, pending) in enumerate(zip(attempts, reservations)):
        assert attempt['terminal_status'] == ('error' if error and i == 1 else 'aborted')
        if not error or i == 0:
            assert attempt['pending_id'] == pending['id']
        assert attempt['funded_contract_json'] == pending['funded_contract_json']
        assert json.loads(attempt['classification_json'])['facts']['base_clip_usd'] == pending['reserved_notional_usd']
    assert json.loads(attempts[1]['trace_json'])['substage_events'][0]['parent_attempt_id'] == attempts[0]['attempt_id']
    child_marks = {p['name']: p for p in json.loads(attempts[1]['trace_json'])['substage_events']}
    assert child_marks['evm_source_receipt_observation']['observation'] == observation
    parent_marks = {p['name']: p for p in json.loads(attempts[0]['trace_json'])['substage_events']}
    assert parent_marks['chase_tape_move_shadow_late']['move_pct'] == 0.0
    assert parent_marks['momentum_own_late']['reason'] == 'queue_expired'
    assert m.pending_totals(c) == (0, 0.0)
