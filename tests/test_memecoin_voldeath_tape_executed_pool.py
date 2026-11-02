"""8453 vol_death: the entry pool is the one our own receipt touched (Alpha G2, 2026-10-08).

`tape_exit_decision` named the held pool from the classification's `liquidity_pair_address` (the
provider's pair pick) and the route's `pool_id`. A Kyber-routed position carries no pool_id, and with
DexScreener leaving the money path the provider pair is often absent, so the rail read
`blind_frozen:entry_pool_unknown` for the whole hold (position 71670, 2026-10-07: both of its audit
rows). Replaying the tape decision on the 21 8453 coins attempted since 10-05: 18 read
entry_pool_unknown. The position's own `executed_pool` (from our entry receipt) is set on every
funded 8453 position since 09-29.

The "pool we hold" guard is the UNION of the executed pool, the route's pool id and the provider pair
(Alpha, on Gamma's LOW): a provider pair the tape never mapped still reads blind, never a measured
silence on our pool alone. Once DexScreener is off the provider pair is absent and the guard is ours.
"""
import json

from test_memecoin_voldeath_tape import (  # noqa: F401 (fixtures)
    EXIT_S, MINT, NOW, OTHER_V4, T0, V4_POOL, _decide, _iso, _live, _switch_on, tape)

UNMAPPED = "0x" + "77" * 32


def _pos(*, executed=None, pool_id=None, lp=None):
    contract = {"classification": {"facts": ({"liquidity_pair_address": lp} if lp else {})}}
    route = {"protocol": "kyberswap"} if pool_id is None else {"protocol": "uniswap_v4",
                                                               "pool_id": pool_id}
    return {"id": 71670, "mint": MINT, "network_id": "8453", "opened_at": _iso(T0),
            "evm_route_json": json.dumps(route), "funded_contract_json": json.dumps(contract),
            "executed_pool": executed}


def test_a_kyber_position_with_no_provider_pair_reads_its_executed_pool(tape):
    tape.map(V4_POOL, "v4")
    tape.swap(V4_POOL, NOW - 200.0)
    _live(tape)
    fire, _silence, why = _decide(tape, _pos(executed=V4_POOL))
    assert why != "blind_frozen:entry_pool_unknown"
    assert fire is True and why == "measured_tape_still"


def test_a_provider_pair_the_tape_never_mapped_keeps_the_rail_blind(tape):
    """Alpha's call on Gamma's LOW (2026-10-08): the guard is the UNION of our pools and the provider
    pair. A provider pair the tape never mapped may be the coin's main pool on a protocol the tape
    does not cover; quiet on our pool is then no proof the coin stopped. A false vol_death fire sells
    a live coin, blind only holds."""
    tape.map(V4_POOL, "v4")
    tape.swap(V4_POOL, NOW - 200.0)
    _live(tape)
    fire, _silence, why = _decide(tape, _pos(executed=V4_POOL, lp=UNMAPPED))
    assert fire is False and why == "blind_frozen:pool_unmapped", why


def test_with_no_own_pool_the_provider_pair_still_guards(tape):
    tape.map(V4_POOL, "v4")
    tape.swap(V4_POOL, NOW - 200.0)
    _live(tape)
    _fire, _silence, why = _decide(tape, _pos(lp=V4_POOL))
    assert why == "measured_tape_still"


def test_an_executed_pool_the_tape_never_mapped_stays_blind(tape):
    tape.map(V4_POOL, "v4")
    _live(tape)
    _fire, _silence, why = _decide(tape, _pos(executed=UNMAPPED))
    assert why == "blind_frozen:pool_unmapped"
