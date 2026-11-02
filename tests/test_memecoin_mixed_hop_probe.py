"""The PCS V3 -> V2 mixed hop is MEASURED before it is ever executed.

8 of 13 chain-56 no_route signals (8.8 days to 2026-09-22) are Pancake V2 pairs quoted in a
non-connector base token. The V2-ONLY path through the coin's own pair token is already live
(MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS defaults to "56"), and they refused anyway, so the missing
leg is the V3 bridge from the quote token to that base token.

THE PROPERTY THESE TESTS EXIST FOR: the probe can never admit an entry. Two legs are two
transactions, so a fill on the first with a failure on the second is an open position in a coin the
ladder refused. `hit` is False on every path through this module, including the success path.
"""
import pytest

from sniper.execution import evm_mixed_hop_probe as P


COIN = "0x" + "cc" * 20
BASE = "0x" + "bb" * 20          # a non-connector base token, e.g. BEM / MCDon / BNCB
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
TAKER = "0x" + "11" * 20


class _Cfg:
    MEMECOIN_EVM_MIXED_HOP_PROBE_ENABLED = True
    MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS = frozenset({"56"})

    @staticmethod
    def memecoin_evm_v2_exit_config(chain):
        return {"connectors": (WBNB,)}


def _probe(**kw):
    kw.setdefault("cfg", _Cfg())
    kw.setdefault("pair_tokens_fn", lambda chain, mint: (BASE,))
    kw.setdefault("v3_quote_fn", lambda *a, **k: {"buyAmount": 500})
    kw.setdefault("v2_quote_fn", lambda *a, **k: {"buyAmount": 9_000})
    return P.probe_v3_v2("56", WBNB, COIN, 1_000, TAKER, **kw)


def test_the_probe_never_reports_a_hit_even_when_both_legs_quote():
    """THE SAFETY PROPERTY. A selectable mixed-hop quote is an entry without an atomic exit."""
    out = _probe()
    assert out["hit"] is False
    assert out["measurement_only"] is True
    assert out["reason"] == "would_quote"
    assert out["coin_out_atomic"] == 9_000
    assert out["bridged_atomic"] == 500
    assert out["base_token"] == BASE


def test_it_is_off_by_default():
    class _Off:
        MEMECOIN_EVM_MIXED_HOP_PROBE_ENABLED = False
        MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS = frozenset({"56"})
    called = []
    out = _probe(cfg=_Off(), pair_tokens_fn=lambda *a: called.append(1) or (BASE,))
    assert out["reason"] == "probe_disabled"
    assert called == [], "a disabled probe still did work"


def test_a_chain_outside_the_set_is_not_probed():
    out = P.probe_v3_v2("8453", WBNB, COIN, 1_000, TAKER, cfg=_Cfg(),
                        pair_tokens_fn=lambda *a: (BASE,))
    assert out["reason"] == "chain_not_probed"


def test_a_connector_base_token_is_not_counted():
    """The existing ladder already routes through connectors. Counting those would inflate the
    addressable set with signals that were never refused."""
    out = _probe(pair_tokens_fn=lambda chain, mint: (WBNB,))
    assert out["reason"] == P.REASON_NO_PAIR_TOKEN


def test_a_missing_bridge_and_a_missing_coin_leg_are_different_reasons():
    """The two ask for completely different builds, so one bucket would waste the measurement."""
    no_bridge = _probe(v3_quote_fn=lambda *a, **k: None)
    assert no_bridge["reason"] == P.REASON_NO_V3_BRIDGE

    no_coin_leg = _probe(v2_quote_fn=lambda *a, **k: None)
    assert no_coin_leg["reason"] == P.REASON_NO_V2_LEG


def test_the_deepest_base_token_wins():
    quotes = {("a" * 40): 1_000, ("d" * 40): 7_000}
    bases = ["0x" + k for k in quotes]

    def v2(chain, sell, buy, amt, taker, **kw):
        return {"buyAmount": quotes[sell[2:]]}

    out = _probe(pair_tokens_fn=lambda *a: tuple(bases), v2_quote_fn=v2)
    assert out["coin_out_atomic"] == 7_000
    assert out["base_token"] == "0x" + "d" * 40


def test_a_raising_leg_never_raises_out_of_the_probe():
    """A measurement must never cost a decision."""
    def boom(*a, **k):
        raise RuntimeError("rpc down")
    out = _probe(v3_quote_fn=boom)
    assert out["hit"] is False
    assert out["reason"] in (P.REASON_ERROR, P.REASON_NO_V3_BRIDGE)


def test_the_tuple_rung_shapes_are_understood():
    """Rungs answer as a dict, a 2-tuple or a 3-tuple; reading only dicts loses real quotes."""
    assert P._amount_out({"buyAmount": 5}) == 5
    assert P._amount_out(({"buyAmount": 5}, None)) == 5
    assert P._amount_out(({"buyAmount": 5}, None, "reason")) == 5
    assert P._amount_out((None, "blind")) == 0
    assert P._amount_out(None) == 0
    assert P._amount_out({"buyAmount": 0}) == 0
