"""Two own PumpSwap buy events in one frame, named exactly (Alpha 2026-10-16).

11 multi_own_buy:2xpump_amm skips since 10-09, none on one pool: 10 chains (SOL -> A, then A -> the coin
in a pool quoted in A) and 1 two-pool frame (2gCFS) that was a split route into ONE coin. The decoder
tells them apart from the events alone (the owner's base and quote accounts), and the lane names a
chain's hop from its pool cache, so the skip table says what each frame was. Nothing is split or
bought: a chain's first coin is spent inside the same transaction, and the two-pool frame's second
base account belonged to another wallet."""
import json
from pathlib import Path

import base58
import pytest

from sniper.execution.pump_curve_buy import USDC_MINT, WSOL_MINT
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_slot_signal as sig

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/pump_amm_two_leg_logs_20261016.json").read_text())


def _decode(case):
    rec = FX[case]
    frame = {"params": {"result": {"context": {"slot": rec["slot"]}, "value": {
        "signature": rec["signature"], "err": None, "logs": rec["logs"]}}}}
    return sig.decode_owner_buy(frame, rec["owner"], base58.b58decode(rec["owner"]), "uid", "h",
                                recv_monotonic=1.0, recv_utc="2026-10-16T00:00:00+00:00")


@pytest.mark.parametrize("case", ["apiANK_chain", "starman_chain", "usdc_chain"])
def test_a_chain_is_one_named_refusal_with_both_pools(case):
    buy = _decode(case)
    assert buy.refusal == sig.CHAIN_REFUSAL and len(buy.legs) == 2 and buy.legs[0] != buy.legs[1]
    assert buy.pool in buy.legs                        # the refusal carries a leg's own event


def test_the_split_route_frame_is_two_pools_never_two_buys():
    buy = _decode("two_pools")
    assert buy.refusal == sig.TWO_POOLS_REFUSAL and len(buy.legs) == 2


class Pools(dict):
    pass


@pytest.mark.parametrize("facts, reason", [
    ({"base_mint": "apiANKpA5BSUkrqqdEmehJPCUmvo4obWJ6nLxdaGxyL", "quote_mint": WSOL_MINT},
     "multi_own_buy:chain_exotic_quote:apiANKpA5BSUkrqqdEmehJPCUmvo4obWJ6nLxdaGxyL"),
    ({"base_mint": "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn", "quote_mint": USDC_MINT},
     "multi_own_buy:chain_quote_usdc"),
    (None, "multi_own_buy:pool_unresolved"),
])
def test_the_lane_names_the_chain_from_its_pool_cache(facts, reason):
    buy = _decode("apiANK_chain")
    pools = Pools({buy.legs[0]: facts} if facts else {})
    assert lane.chain_refusal(buy.legs, pools) == reason
    assert lane.chain_refusal(buy.legs, None) == "multi_own_buy:pool_unresolved"


def test_the_lane_rejects_a_chain_under_its_name_with_no_read(tmp_path, monkeypatch):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from test_memecoin_slot_lane import _live_lane
    conn = sqlite3.connect(tmp_path / "l.db")
    m.ensure_memecoin_tables(conn)
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    buy = _decode("apiANK_chain")
    ln.amm_pools = Pools({buy.legs[0]: {"base_mint": "apiANKpA5BSUkrqqdEmehJPCUmvo4obWJ6nLxdaGxyL",
                                        "quote_mint": WSOL_MINT}})
    result = ln.handle(buy)
    assert result == {"status": "rejected",
                      "reason": "multi_own_buy:chain_exotic_quote:apiANKpA5BSUkrqqdEmehJPCUmvo4obWJ6nLxdaGxyL"}
    assert sent == []


def test_named_refusals_keep_the_on_chain_lanes_shape_class():
    from sniper.services.memecoin_onchain_lane import MULTI_OWN_BUY_PREFIX
    for reason in (sig.CHAIN_REFUSAL, sig.TWO_POOLS_REFUSAL, "multi_own_buy:chain_quote_usdc",
                   "multi_own_buy:pool_unresolved", "multi_own_buy:chain_exotic_quote:x"):
        assert reason.startswith(MULTI_OWN_BUY_PREFIX)


# --- Gamma LOW pins (2026-10-16): the predicate's remaining guards -------------------------------------

def _found(monkeypatch, accounts):
    """found-list stand-ins whose BuyEvent accounts are `accounts` (pool, base, quote) per leg."""
    table = {"l%d" % i: acc for i, acc in enumerate(accounts)}
    monkeypatch.setattr(sig, "_amm_accounts", lambda line: table[line])
    return [({"venue": "pump_amm"}, "l%d" % i, 10 * i) for i in range(len(accounts))]


def test_two_legs_on_one_pool_are_neither_a_chain_nor_two_pools(monkeypatch):
    assert sig._two_amm_refusal(_found(monkeypatch, [("P", "A", "Q"), ("P", "B", "A")])) is None
    assert sig._two_amm_refusal(_found(monkeypatch, [("P", "A", "Q"), ("P", "B", "Q")])) is None


def test_two_pools_needs_two_different_base_accounts(monkeypatch):
    assert sig._two_amm_refusal(_found(monkeypatch, [("P1", "A", "Q"), ("P2", "A", "Q")])) is None
    assert sig._two_amm_refusal(_found(monkeypatch, [("P1", "A", "Q"), ("P2", "B", "Q")]))[0] == sig.TWO_POOLS_REFUSAL


def test_a_cached_pool_without_a_base_mint_is_unresolved():
    assert lane.chain_refusal(("P1", "P2"), Pools({"P1": {"quote_mint": WSOL_MINT}})) == "multi_own_buy:pool_unresolved"
