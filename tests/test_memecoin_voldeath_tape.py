"""Volume-death on Base (8453) from our own swap tape (operator order 2026-10-01, via Alpha).

Before this, every non-4663 EVM position read `stream_healthy()`, which only sees Solana fast-lane
marks, so 8453 and 56 were `blind_frozen` for their whole life and left no audit row.

The tape is `data/memecoin_onchain_volume.db`, written by a separate process. Its silence is a
statement about the coin only when THREE things hold, and each test below breaks one of them:

  - the writer is alive and its covered window spans the silence (`volume_feed`);
  - every pool the coin trades on is one the tape subscribes to (`onchain_pool_protocols` and
    `covered_protocols`);
  - the subscription that carries those pools delivered OTHER pools' swaps all through the window.
    A silent subscription is re-opened without resetting the window (8453 V4, 2026-09-29: an hour
    of nothing while V3 flowed), so the window alone cannot vouch for it.

Missing or stale tape is `blind_frozen:<cause>` and it is AUDITED, unlike the old bare literal.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_volume_death as vd
from sniper.services.memecoin_volume_writer import connect as tape_connect

CHAIN = "8453"
MINT = "0x" + "11" * 20
V4_POOL = "0x" + "ab" * 32
V3_POOL = "0x" + "cd" * 20
V2_POOL = "0x" + "ef" * 20
OTHER_V4 = "0x" + "99" * 32
OTHER_V3 = "0x" + "98" * 20
OTHER_V2 = "0x" + "97" * 20
T0 = 1_790_000_000.0                       # the open
NOW = T0 + 300.0                           # held 300 s
EXIT_S = 60.0


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch):
    monkeypatch.setenv("MEMECOIN_VOLDEATH_TAPE_CHAINS", CHAIN)
    monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", CHAIN)     # as .env has it for 8453
    vd._EXACT_TAPE.clear()
    yield
    vd._EXACT_TAPE.clear()


class Tape:
    """The production schema (the writer's own `connect`), filled the way the writer fills it."""

    def __init__(self, path):
        self.path = path
        self.c = tape_connect(path)
        self.feed(since=T0 - 600.0, received=NOW - 1.0)
        self.c.execute("INSERT OR REPLACE INTO volume_feed_optional VALUES (?,?)",
                       (CHAIN, T0 - 600.0))
        self.c.commit()

    def feed(self, *, since, received):
        self.c.execute("INSERT OR REPLACE INTO volume_feed VALUES (?,?,?,?,?)",
                       (CHAIN, since, received, received, 100))
        self.c.commit()

    def map(self, pool, protocol, mint=MINT):
        self.c.execute("INSERT OR REPLACE INTO onchain_pool_protocols VALUES (?,?,?,?)",
                       (CHAIN, pool, protocol, mint))
        self.c.commit()

    def swap(self, pool, ts, *, kind=None, header=True):
        block = int(ts * 10)               # unique per timestamp; only block_times maps it to time
        kind = kind or ("v2_swap" if pool in (V2_POOL, OTHER_V2) else "v3_swap")
        self.c.execute(
            "INSERT INTO pool_ticks (chain_id, pool, kind, block_no, tx_sig, log_index, "
            "observed_at, block_hash) VALUES (?,?,?,?,?,?,?,?)",
            (CHAIN, pool, kind, block, "0x%x" % block + pool[-6:], 0, _iso(ts + 0.2), "0xh"))
        if header:
            self.c.execute("INSERT OR REPLACE INTO block_times VALUES (?,?,?,?,?)",
                           (CHAIN, block, _iso(ts).replace(".000000", ""), _iso(ts), "0xh"))
        self.c.commit()

    def flow(self, pool, start, end, step=2.0):
        """Another pool trading every block: proof its subscription is delivering."""
        ts = start
        while ts <= end:
            self.swap(pool, ts)
            ts += step


@pytest.fixture()
def tape(tmp_path):
    t = Tape(str(tmp_path / "memecoin_onchain_volume.db"))
    yield t
    t.c.close()


def _live(tape, *families):
    """Every subscription in `families` delivers other pools' swaps through the last 120 s."""
    other = {"v4": OTHER_V4, "v3": OTHER_V3, "v2": OTHER_V2}
    for family in families or ("v4", "v3", "v2"):
        tape.flow(other[family], NOW - 120.0, NOW - 1.0)


def _pos(lp=V4_POOL, **kw):
    contract = {"classification": {"facts": {"liquidity_pair_address": lp}}}
    pos = {"id": 7, "mint": MINT, "network_id": CHAIN, "opened_at": _iso(T0),
           "evm_route_json": json.dumps({"hops": 1}), "funded_contract_json": json.dumps(contract)}
    pos.update(kw)
    return pos


def _decide(tape, pos=None, *, now=NOW, exit_s=EXIT_S, path=None):
    return vd.armed_exit_decision(
        None, pos or _pos(), now_iso=_iso(now), held_s=now - T0, exit_s=exit_s,
        min_opened_at=_iso(T0 - 3600.0), require_onchain=False, move_veto_pct=0.0,
        tape_path=path or tape.path)


# ------------------------------------------------------------------ the switch

def test_the_switch_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("MEMECOIN_VOLDEATH_TAPE_CHAINS", raising=False)
    from sniper.settings import memecoin as cfg
    assert cfg.memecoin_voldeath_tape_chains() == frozenset()


@pytest.mark.parametrize("raw", ["8453,solana", "base", "8453;56", "x"])
def test_a_malformed_switch_means_empty(monkeypatch, raw):
    monkeypatch.setenv("MEMECOIN_VOLDEATH_TAPE_CHAINS", raw)
    from sniper.settings import memecoin as cfg
    assert cfg.memecoin_voldeath_tape_chains() == frozenset()


def test_switch_off_keeps_todays_unaudited_blind(tape, monkeypatch):
    """Rollback is the env line: without it an 8453 position is exactly as before."""
    monkeypatch.delenv("MEMECOIN_VOLDEATH_TAPE_CHAINS", raising=False)
    tape.map(V4_POOL, "v4")
    _live(tape)
    import sqlite3
    marks = sqlite3.connect(":memory:")
    marks.execute("CREATE TABLE memecoin_position_marks "
                  "(chain_id TEXT, mint TEXT, sweep_ts TEXT, source TEXT, mid REAL)")
    fire, silence, why = vd.armed_exit_decision(
        marks, _pos(), now_iso=_iso(NOW), held_s=300.0, exit_s=EXIT_S,
        min_opened_at=_iso(T0 - 3600.0), require_onchain=False, tape_path=tape.path)
    assert (fire, silence, why) == (False, None, "blind_frozen")


def test_a_4663_position_keeps_its_own_rail_even_if_listed(tape, monkeypatch):
    monkeypatch.setenv("MEMECOIN_VOLDEATH_TAPE_CHAINS", "4663,8453")
    state = vd.RobinhoodVolumeState(health_window_s=5.0)        # no heads: its own blind answer
    fire, _silence, why = vd.armed_exit_decision(
        None, _pos(network_id="4663"), now_iso=_iso(NOW), held_s=300.0, exit_s=EXIT_S,
        min_opened_at=_iso(T0 - 3600.0), require_onchain=False, robinhood_state=state,
        tape_path=tape.path)
    assert fire is False and why == "blind_frozen"


# ------------------------------------------------------------------ the measurement

def test_a_silent_pool_on_a_live_tape_fires(tape):
    tape.map(V4_POOL, "v4")
    tape.swap(V4_POOL, NOW - 200.0)                             # last trade 200 s ago
    _live(tape)
    fire, silence, why = _decide(tape)
    assert fire is True and why == "measured_tape_still"
    assert silence == pytest.approx(200.0, abs=1.0)


def test_no_swap_since_the_open_on_a_covered_pool_is_silence_since_the_open(tape):
    tape.map(V4_POOL, "v4")
    _live(tape)
    fire, silence, why = _decide(tape)
    assert fire is True and why == "measured_tape_still"
    assert silence == pytest.approx(300.0, abs=1.0)


def test_a_recent_swap_holds_the_clock(tape):
    tape.map(V4_POOL, "v4")
    tape.swap(V4_POOL, NOW - 20.0)
    _live(tape)
    fire, silence, why = _decide(tape)
    assert fire is False and why == "measured"
    assert silence == pytest.approx(20.0, abs=1.0)


def test_a_swap_on_another_pool_of_the_same_coin_holds_the_clock(tape):
    """Every pool the coin has is bound: quiet on one pool says nothing while another trades."""
    tape.map(V4_POOL, "v4")
    tape.map(V3_POOL, "v3")
    tape.swap(V3_POOL, NOW - 10.0)
    _live(tape)
    fire, silence, why = _decide(tape, _pos(lp=V3_POOL))
    assert fire is False and why == "measured" and silence == pytest.approx(10.0, abs=1.0)


def test_a_window_reset_during_the_hold_restarts_the_clock(tape):
    """Before `since` the tape was not watching; that time is not silence."""
    tape.map(V4_POOL, "v4")
    tape.feed(since=NOW - 20.0, received=NOW - 1.0)
    _live(tape)
    fire, silence, why = _decide(tape)
    assert fire is False and why == "measured" and silence == pytest.approx(20.0, abs=1.0)


# ------------------------------------------------------------------ blind is audited, never fired

def test_a_stale_writer_is_blind(tape):
    tape.map(V4_POOL, "v4")
    _live(tape)
    tape.feed(since=T0 - 600.0, received=NOW - 30.0)
    assert _decide(tape) == (False, None, "blind_frozen:feed_stale")


def test_a_missing_feed_row_is_blind(tape):
    tape.map(V4_POOL, "v4")
    tape.c.execute("DELETE FROM volume_feed")
    tape.c.commit()
    assert _decide(tape) == (False, None, "blind_frozen:feed_absent")


def test_a_reset_window_with_no_start_is_blind(tape):
    tape.map(V4_POOL, "v4")
    tape.c.execute("UPDATE volume_feed SET since=NULL")
    tape.c.commit()
    assert _decide(tape) == (False, None, "blind_frozen:feed_absent")


def test_a_missing_store_is_blind(tape, tmp_path):
    assert _decide(tape, path=str(tmp_path / "absent.db")) == (
        False, None, "blind_frozen:store_unavailable")


def test_a_coin_the_tape_never_mapped_is_blind(tape):
    _live(tape)
    assert _decide(tape) == (False, None, "blind_frozen:no_pool")


def test_the_entry_pair_missing_from_the_tape_is_blind(tape):
    """The position's own pool must be one the tape knows, or its quiet is a stranger's quiet."""
    tape.map(V4_POOL, "v4")
    _live(tape)
    contract = {"classification": {"facts": {"liquidity_pair_address": V2_POOL}}}
    fire, silence, why = _decide(tape, _pos(funded_contract_json=json.dumps(contract)))
    assert (fire, silence, why) == (False, None, "blind_frozen:pool_unmapped")


def test_a_position_with_no_entry_pair_is_blind(tape):
    """Gamma LOW 1 on ef1bfa029: with no pair to check, mapped-pool silence could be a stranger's."""
    tape.map(V4_POOL, "v4")
    _live(tape)
    assert _decide(tape, _pos(funded_contract_json="{}")) == (
        False, None, "blind_frozen:entry_pool_unknown")


def test_a_v2_pool_without_a_confirmed_v2_subscription_is_blind(tape):
    tape.map(V2_POOL, "v2")
    tape.c.execute("DELETE FROM volume_feed_optional")
    tape.c.commit()
    _live(tape)
    assert _decide(tape, _pos(lp=V2_POOL)) == (False, None, "blind_frozen:protocol_uncovered")


def test_a_bonding_curve_route_is_blind(tape):
    tape.map(V4_POOL, "v4")
    _live(tape)
    pos = _pos(evm_route_json=json.dumps({"curve": "0x" + "44" * 20}))
    assert _decide(tape, pos) == (False, None, "blind_frozen:curve_route")


def test_a_swap_whose_header_has_not_landed_still_holds_the_clock(tape):
    """A log can beat its header. Its arrival time is later than its block time, so using it
    can only shorten the silence: the safe side."""
    tape.map(V4_POOL, "v4")
    tape.swap(V4_POOL, NOW - 20.0, header=False)
    _live(tape)
    fire, silence, why = _decide(tape)
    assert fire is False and why == "measured" and silence == pytest.approx(19.8, abs=0.5)


# ------------------------------------------------------------------ the subscription must vouch

def test_a_silent_v4_subscription_beside_live_v3_does_not_fire(tape):
    """THE 2026-09-29 8453 CASE: V4 delivered nothing for an hour while V3 flowed and heads kept
    the window open. Without this check the rail reads our dead subscription as a dead coin."""
    tape.map(V4_POOL, "v4")
    tape.flow(OTHER_V3, NOW - 120.0, NOW - 1.0)
    tape.flow(OTHER_V4, NOW - 120.0, NOW - 45.0)               # V4 stops 45 s ago
    fire, silence, why = _decide(tape)
    assert fire is False and why == "tape_unvouched:v4"
    assert silence == pytest.approx(300.0, abs=1.0)


def test_a_dead_v2_subscription_does_not_vouch_for_a_v2_pool(tape):
    """SubscriptionWatch watches only V4 and V3; a stalled V2 topic is never re-opened."""
    tape.map(V2_POOL, "v2")
    _live(tape, "v4", "v3")
    assert _decide(tape, _pos(lp=V2_POOL))[2] == "tape_unvouched:v2"


def test_a_subscription_quiet_at_the_window_edge_does_not_vouch(tape):
    tape.map(V3_POOL, "v3")
    tape.flow(OTHER_V3, NOW - 120.0, NOW - 15.0)               # nothing for the last 15 s
    _live(tape, "v4", "v2")
    assert _decide(tape, _pos(lp=V3_POOL))[2] == "tape_unvouched:v3"


def test_a_gap_shorter_than_the_tolerance_still_vouches(tape):
    tape.map(V3_POOL, "v3")
    tape.flow(OTHER_V3, NOW - 120.0, NOW - 1.0, step=6.0)      # 6 s gaps, as V2 shows on 8453
    assert _decide(tape, _pos(lp=V3_POOL))[:1] == (True,)


# ------------------------------------------------------------------ the exact quote vetoes

def test_a_moving_exact_quote_vetoes_the_fire(tape):
    tape.map(V4_POOL, "v4")
    _live(tape)
    for i, px in enumerate((1.0, 1.02, 1.09)):
        vd.record_exact_quote(7, quoted_at=_iso(NOW - 50.0 + 20.0 * i), price=px)
    assert _decide(tape)[::2] == (False, "exact_quote_moving")


# ------------------------------------------------------------------ every verdict is audited

def test_every_covered_protocol_has_an_unvouched_label():
    """A protocol the tape starts covering must have its own audited label, or the family lookup
    raises on the exit sweep (fail closed, but unlabelled)."""
    from sniper.services.memecoin_onchain_volume import COVERED_PROTOCOLS
    assert set(COVERED_PROTOCOLS) | {"v2"} <= set(vd.TAPE_UNVOUCHED)
    assert set(vd.TAPE_UNVOUCHED.values()) <= set(vd.HELD_WITNESS_REASONS)
    assert set(vd.HELD_WITNESS_REASONS) <= set(vd.POST_THRESHOLD_REASONS)
