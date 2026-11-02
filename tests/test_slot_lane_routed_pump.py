"""Routed PumpSwap/curve buys on the slot lane (2026-09-27, Alpha GO).

The lane's decoder takes a pump buy event only when its actor IS the subscribed owner, at any CPI
depth; DFlow, Jupiter, FLASHX and term9Y routes name the owner there and already reach the lane.
Router proVF4 names its own account instead (ARu4n5mF..., the same for three different owners), so
those buys reach only the routed pairer, which knows the coin and the signature but not the pool.
The fixture holds the 3 such buys the pairer emitted 17:25-21:45Z, captured from chain unchanged.

A routed buy whose own logs frame carries exactly one pump buy event into its coin (the PumpSwap
pool's cached base mint, or the curve event's mint) becomes an ordinary slot-lane buy built by the
existing builders. ONE SOURCE BUY, ONE FUNDED PATH: when the lane is live and takes the buy, the
routed hand-off does not write its detection row, so the on-chain lane cannot race the lane on the
same signature; the watcher's later row defers to the lane's `slotlane:<sig>` attempt
(test_memecoin_onchain_lane covers that rule). Anything the conversion refuses goes to the hand-off
exactly as before. Flag MEMECOIN_SLOT_LANE_ROUTED_PUMP (default 1, rollback 0).
"""
import copy
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from sniper.execution import pumpswap_buy as ps
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_slot_signal as s
from sniper.settings import memecoin as cfg

FIX = json.loads((Path(__file__).parent / "fixtures/memecoin/slot_routed_pump_provf4.json").read_text())
TXS = FIX["txs"]
ROUTER_ACCOUNT = "ARu4n5mFdZogZAravu7CcizaojWnS6oqka37gdLT5SZn"
WSOL = "So11111111111111111111111111111111111111112"
USDC = s.USDC_MINT


def _frame(tx, sub=1):
    return {"jsonrpc": "2.0", "method": "logsNotification", "params": {
        "result": {"context": {"slot": tx["slot"]},
                   "value": {"signature": tx["signature"], "err": None, "logs": list(tx["logs"])}},
        "subscription": sub}}


def _routed(tx, events=None):
    return s.RoutedBuy(
        source_user_id="uid-" + tx["handle"], handle=tx["handle"], owner=tx["owner"],
        signature=tx["signature"], slot=tx["slot"], mint=tx["coin"]["mint"],
        token_delta_atomic=tx["coin"]["post"] - tx["coin"]["pre"],
        token_decimals=tx["coin"]["decimals"],
        usdc_spent_atomic=tx["usdc"]["pre"] - tx["usdc"]["post"], recv_monotonic=time.monotonic(),
        recv_utc="2026-09-27T21:45:53.371324+00:00", paired_monotonic=time.monotonic(),
        venue_events=s.routed_pump_buys(_frame(tx), True) if events is None else events)


class Pools:
    """The PumpSwap builder's pool cache, as far as the conversion reads it."""

    def __init__(self, facts):
        self.facts, self.asked = facts, []

    def get(self, pool):
        self.asked.append(pool)
        return self.facts.get(pool)


def _facts(tx, **kw):
    base = {"base_mint": tx["coin"]["mint"], "quote_mint": tx["pool_quote_mints"][0],
            "pump_migrated": True, "base_supply": 10 ** 15, "base_decimals": tx["coin"]["decimals"],
            "virtual_quote": 0, "quote_token_program": ps.TOKEN_PROGRAM}
    base.update(kw)
    return base


def _pools(tx, **kw):
    return Pools({tx["pool"]: _facts(tx, **kw)})


@pytest.mark.parametrize("tx", TXS, ids=[t["handle"] for t in TXS])
def test_the_real_routed_pumpswap_buys_name_the_router_not_the_owner(tx):
    from solders.pubkey import Pubkey
    owner_raw = bytes(Pubkey.from_string(tx["owner"]))
    assert s.decode_owner_buy(_frame(tx), tx["owner"], owner_raw, "u", recv_monotonic=0.0,
                              recv_utc="x") is None, "the lane already reads this buy"
    found = s.routed_pump_buys(_frame(tx), True)
    assert len(found) == 1 and found[0].venue == "pump_amm"
    event = ps.parse_buy_event(found[0].trade_event_log)
    assert event["user"] == ROUTER_ACCOUNT != tx["owner"]
    assert event["pool"] == tx["pool"] == dict(found[0].fields)["pool"]
    # The event is the coin's own buy: its base out is exactly the owner's balance rise.
    assert event["base_amount_out"] == tx["coin"]["post"] - tx["coin"]["pre"]
    assert s.routed_pump_buys(_frame(tx), False) == ()


@pytest.mark.parametrize("tx", TXS, ids=[t["handle"] for t in TXS])
def test_a_real_routed_buy_becomes_one_pumpswap_slot_buy_the_lane_can_price(tx):
    routed = _routed(tx)
    pools = _pools(tx)
    buy, reason = lane.pump_slot_buy(routed, pools)
    assert reason is None and buy.venue == "pump_amm"
    event = ps.parse_buy_event(buy.trade_event_log)
    assert (buy.pool, buy.mint, buy.signature, buy.slot) == (
        tx["pool"], tx["coin"]["mint"], tx["signature"], tx["slot"])
    assert (buy.source_user_id, buy.handle, buy.owner) == (
        routed.source_user_id, routed.handle, routed.owner)
    assert buy.recv_monotonic == routed.recv_monotonic and buy.recv_utc == routed.recv_utc
    assert buy.token_amount == event["base_amount_out"]
    assert (buy.real_token_reserves, buy.real_sol_reserves) == (
        event["pool_base_token_reserves"], event["pool_quote_token_reserves"])
    assert buy.refusal is None
    reason, facts = lane.amm_market_facts(buy, pools, 150.0)
    assert reason is None and facts["mint"] == tx["coin"]["mint"] and facts["price_usd"] > 0
    # What the trader PAID, pool fees included: the lanes' one source-size basis (2026-09-28).
    assert facts["their_usd"] == pytest.approx(ps.buy_event_gross_quote(event) / 1e9 * 150.0)


def test_a_real_routed_curve_buy_converts_with_its_mint_and_token_program():
    cv = FIX["curve_via_dflow"]
    tx = {"slot": cv["slot"], "signature": cv["signature"], "logs": cv["logs"]}
    events = s.routed_pump_buys(_frame(tx), True)
    routed = s.RoutedBuy(source_user_id="u", handle="h", owner=cv["owner"], signature=cv["signature"],
                         slot=cv["slot"], mint=cv["mint"], token_delta_atomic=1, token_decimals=6,
                         usdc_spent_atomic=1, recv_monotonic=1.0, recv_utc="x", paired_monotonic=1.0,
                         venue_events=events)
    buy, reason = lane.pump_slot_buy(routed, None)        # the curve needs no pool cache
    assert reason is None and buy.venue == "pump_curve" and buy.mint == cv["mint"]
    assert buy.pool is None and buy.token_program == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    assert buy.virtual_sol_reserves > 0 and buy.virtual_token_reserves > 0
    assert lane.curve_price_usd(buy, 150.0) > 0
    other = s.RoutedBuy(**{**routed.__dict__, "mint": "OtherCoin1111111111111111111111111111111111"})
    assert lane.pump_slot_buy(other, None) == (None, "no_pump_event_into_coin")


def test_a_pool_the_cache_has_not_learned_refuses_and_is_queued_for_learning():
    tx = TXS[2]
    cache = ps.PumpSwapPoolCache(rpc=None)
    assert lane.pump_slot_buy(_routed(tx), cache) == (None, "pool_unknown")
    assert cache.state(tx["pool"]) == "queued"


@pytest.mark.parametrize("facts, reason", [
    ({"base_mint": "OtherCoin1111111111111111111111111111111111"}, "no_pump_event_into_coin"),
    ({"quote_mint": USDC}, "pump_quote_not_sol"),
    ({"pump_migrated": False}, "pump_pool_not_migrated"),
])
def test_only_a_migrated_wsol_pool_of_the_coin_itself_converts(facts, reason):
    tx = TXS[0]
    assert lane.pump_slot_buy(_routed(tx), _pools(tx, **facts)) == (None, reason)


def test_two_pump_events_into_the_coin_refuse():
    tx = TXS[1]
    one = s.routed_pump_buys(_frame(tx), True)
    assert lane.pump_slot_buy(_routed(tx, events=one + one), _pools(tx)) == (
        None, "multi_pump_event")


def test_a_frame_without_a_pump_event_is_not_a_pump_route():
    tx = TXS[0]
    assert lane.pump_slot_buy(_routed(tx, events=()), _pools(tx)) == (None, None)
    assert lane.pump_slot_buy(_routed(tx), None) == (None, "pool_unknown")


def test_an_unreadable_frame_never_raises_into_the_socket():
    assert s.routed_pump_buys({"params": {}}, True) == ()
    tx = copy.deepcopy(TXS[0])
    tx["logs"] = [line.replace("Program data: ", "Program data: !!") for line in tx["logs"]]
    assert s.routed_pump_buys(_frame(tx), True) == ()


# --- one source buy, one funded path ---------------------------------------------------------------

class Handoff:
    def __init__(self):
        self.written = []

    def write(self, buy):
        self.written.append(buy.signature)
        return "inserted"


def _route(tx, *, pump_on, lane_live, pools, handoff, cpmm_on=False):
    puts, lines = [], []
    lane.route_routed_buy(_routed(tx), handoff=handoff, log=lines.append, put=puts.append,
                          amm_pools=pools, cpmm_on=cpmm_on, pump_on=pump_on, lane_live=lane_live)
    return puts, lines


@pytest.mark.parametrize("pump_on", [True, False])
@pytest.mark.parametrize("lane_live", [True, False])
@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("routed_live", [True, False])
def test_one_source_buy_reaches_at_most_one_funded_path(pump_on, lane_live, known, routed_live):
    tx = TXS[1]
    handoff = Handoff() if routed_live else None
    pools = _pools(tx) if known else Pools({})
    puts, lines = _route(tx, pump_on=pump_on, lane_live=lane_live, pools=pools, handoff=handoff)
    funded = len(handoff.written if handoff else []) + (len(puts) if lane_live else 0)
    assert funded <= 1
    if pump_on and known and lane_live:
        assert [p.venue for p in puts] == ["pump_amm"] and (handoff is None or not handoff.written)
        assert lines == [lane.routed_log_line(_routed(tx), "lane")]
    elif pump_on and known:
        # Lane in shadow: the lane only builds and times it; the hand-off acts as before.
        assert [p.venue for p in puts] == ["pump_amm"]
        assert (handoff.written if handoff else None) in ([tx["signature"]], None)
    else:
        assert puts == []
        assert (handoff.written if handoff else None) in ([tx["signature"]], None)
    if pump_on and not known:
        assert any("pool_unknown" in line for line in lines)


def test_a_conversion_that_raises_still_hands_the_buy_off():
    class Broken:
        def get(self, pool):
            raise RuntimeError("cache gone")
    handoff = Handoff()
    puts, lines = _route(TXS[0], pump_on=True, lane_live=True, pools=Broken(), handoff=handoff)
    assert puts == [] and handoff.written == [TXS[0]["signature"]]
    assert any("pump_conversion_error" in line for line in lines)


def test_converted_pump_buy_keeps_fallback_on_wallet_busy():
    tx = TXS[0]
    handoff = Handoff()
    [buy], _ = _route(tx, pump_on=True, lane_live=True, pools=_pools(tx), handoff=handoff)
    slot = lane.SlotLane.__new__(lane.SlotLane)
    slot.mode, slot.routed_handoff = "live", handoff
    slot._routed_fallback(buy, {"status": "rejected", "reason": "slot_lane_wallet_busy"})
    assert handoff.written == [tx["signature"]]


def test_the_watcher_row_defers_to_the_lanes_attempt_on_the_same_signature(tmp_path):
    """The routed path writes nothing when the live lane takes the buy; the watcher's own row for
    the same transaction then finds the lane's attempt, keyed by the source signature."""
    from sniper.persistence import db_memecoin as m
    from test_memecoin_onchain_lane import _slot_attempt, _slot_conn, _sweep
    tx = TXS[0]
    db = str(tmp_path / "l.db")

    def factory():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c
    m.ensure_memecoin_tables(factory())
    handoff = lane.RoutedHandoff(conn_factory=factory, wake=threading.Event())
    puts, _ = _route(tx, pump_on=True, lane_live=True, pools=_pools(tx), handoff=handoff)
    assert factory().execute("SELECT COUNT(*) FROM onchain_wallet_detections").fetchone()[0] == 0
    (buy,) = puts
    conn = _slot_conn(sig=buy.signature)                 # the watcher's row for the transaction
    _slot_attempt(conn, sig=buy.signature, pending="reserved")   # the lane's `slotlane:<sig>`
    calls, deferred = [], {}
    _, results = _sweep(conn, calls, deferred)
    assert calls == [] and results[0]["result"]["reason"] == "slot_lane_unresolved"


def test_the_flag_defaults_on_and_reads_zero_as_off(monkeypatch):
    import importlib
    monkeypatch.delenv("MEMECOIN_SLOT_LANE_ROUTED_PUMP", raising=False)
    assert importlib.reload(cfg).MEMECOIN_SLOT_LANE_ROUTED_PUMP is True
    monkeypatch.setenv("MEMECOIN_SLOT_LANE_ROUTED_PUMP", "0")
    assert importlib.reload(cfg).MEMECOIN_SLOT_LANE_ROUTED_PUMP is False
    monkeypatch.delenv("MEMECOIN_SLOT_LANE_ROUTED_PUMP")
    importlib.reload(cfg)


def test_the_pump_decode_runs_only_when_a_pair_emits(monkeypatch):
    """A frame that never pairs costs nothing: the decode is deferred to emission."""
    tx = TXS[2]
    calls = []
    real = s.routed_pump_buys
    monkeypatch.setattr(s, "routed_pump_buys", lambda f, on: calls.append(1) or real(f, on))
    pairer = s.RoutedPairer()
    pairer.watch(tx["owner"], "uid-c", tx["handle"])
    for kind, _program in s.ROUTED_PROGRAMS:
        pairer.set_baseline(tx["owner"], kind, tx["slot"] - 1,
                            {tx["usdc"]["account"]: tx["usdc"]["pre"]} if kind == "token" else {})
    later = (s._PumpLater(_frame(tx)),)
    assert pairer.on_logs(tx["owner"], tx["slot"] - 5, "otherSig", lane=False, recv_monotonic=1.0,
                          recv_utc="x", venue_events=later) == [] and calls == []
    assert pairer.on_logs(tx["owner"], tx["slot"], tx["signature"], lane=False, recv_monotonic=1.0,
                          recv_utc="x", venue_events=later) == [] and calls == []
    assert pairer.on_token(tx["owner"], "token", tx["slot"], tx["usdc"]["account"], USDC,
                           tx["usdc"]["post"], 6, recv_monotonic=1.0, recv_utc="x") == []
    (routed,) = pairer.on_token(tx["owner"], "token", tx["slot"], tx["coin"]["account"],
                                tx["coin"]["mint"], tx["coin"]["post"], tx["coin"]["decimals"],
                                recv_monotonic=1.0, recv_utc="x")
    assert calls == [1] and [e.venue for e in routed.venue_events] == ["pump_amm"]


# --- the socket carries the event onto the pair ------------------------------------------------------

def test_the_socket_puts_the_frames_pump_event_on_the_routed_buy(monkeypatch):
    """chrollo0o0's real buy end to end: USDC and coin accounts move in the slot, the logs frame
    names the router, and the emitted RoutedBuy carries the PumpSwap event (flag on) or not (off)."""
    from test_slot_lane_routed import _captured_frame, _run_socket
    tx = TXS[2]
    assert tx["coin"]["program"] == s.TOKEN_PROGRAM == tx["usdc"]["program"]

    def token_frame(row):
        f = _captured_frame("programNotification", "token")
        f["params"]["result"]["context"]["slot"] = tx["slot"]
        v = f["params"]["result"]["value"]
        v["pubkey"] = row["account"]
        info = v["account"]["data"]["parsed"]["info"]
        info.update(mint=row["mint"], owner=tx["owner"])
        info["tokenAmount"].update(amount=str(row["post"]), decimals=row["decimals"])
        return f

    def baseline(owner):
        return {"token": (tx["slot"] - 1, {tx["usdc"]["account"]: tx["usdc"]["pre"]}),
                "token2022": (tx["slot"] - 1, {})}
    frames = [(tx["owner"], "token", token_frame(tx["usdc"])),
              (tx["owner"], "token", token_frame(tx["coin"])), (tx["owner"], "logs", _frame(tx))]
    for flag, expect in ((True, 1), (False, 0)):
        monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_ROUTED_PUMP", flag)
        routed, buys, status, _ws = _run_socket(
            monkeypatch, frames, owners={tx["owner"]: ("uid-c", tx["handle"])}, baseline_fn=baseline)
        assert buys == [] and len(routed) == 1
        assert routed[0].mint == tx["coin"]["mint"] and routed[0].signature == tx["signature"]
        pumps = [e for e in routed[0].venue_events if isinstance(e, s.RoutedPumpBuy)]
        assert len(pumps) == expect
