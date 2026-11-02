"""A pool too thin for the WHOLE bag must still sell most of it.

The exit leg used to quote the full held amount once and give up, so a pool deep enough for most
of a position but not all of it produced NO exit and the position sat holding 100% of the risk.
Measured on LARP 50682: leg 2 reverted at the full amount and cleared at about 84.4% in one hop.
"""
import sniper.execution.evm_v4_multihop as MH


def _wire(monkeypatch, ceiling_atomic):
    """A coin leg that quotes only at or below `ceiling_atomic`."""
    calls = []

    def quote_leg(chain, key, zero_for_one, amount, rpc_url, **kw):
        calls.append(int(amount))
        if key is _COIN:
            return 1234 if int(amount) <= ceiling_atomic else None
        return 5678                                   # bridge leg always quotes

    monkeypatch.setattr(MH, "_quote_leg", quote_leg)
    monkeypatch.setattr(MH, "_pair_of", lambda key, a, b: (True, True))
    monkeypatch.setattr(MH, "as_zerox_exit_quote", None, raising=False)
    return calls


_COIN = {"currency0": "0xcoin", "currency1": "0xbridge", "fee": 3000}
_BRIDGE = {"currency0": "0x0", "currency1": "0xbridge", "fee": 500}


def _run(monkeypatch, ceiling_atomic, held=1000):
    calls = _wire(monkeypatch, ceiling_atomic)
    import sniper.execution.evm_v4_multihop_tx as TX
    monkeypatch.setattr(TX, "as_zerox_exit_quote",
                        lambda *a, **k: {"sellAmount": a[4]}, raising=False)
    route = {"bridge": "0xbridge", "bridge_key": _BRIDGE, "coin_key": _COIN}
    out = MH.dex_v4_multihop_exit_quote(
        "4663", "0xcoin", held, "0xtaker", route=route, rpc_url="http://rpc",
        slippage_bps=100, clip_usd=139.0)
    return out, calls


def test_the_whole_bag_costs_exactly_one_quote_when_it_works(monkeypatch):
    out, calls = _run(monkeypatch, ceiling_atomic=1000, held=1000)
    assert out is not None
    assert calls[0] == 1000                      # asked for the full amount first
    assert out.get("partial_exit") is None       # and did not mark a partial


def test_a_bag_too_big_for_the_pool_still_sells_most_of_itself(monkeypatch):
    # Ceiling 800 of 1000 held: 90% (900) fails, 75% (750) clears.
    out, calls = _run(monkeypatch, ceiling_atomic=800, held=1000)
    assert out is not None
    assert out["partial_exit"] is True
    assert out["sell_amount"] == 750
    assert out["requested_sell_amount"] == 1000
    assert calls[:3] == [1000, 900, 750]         # largest first, stops at the first that clears


def test_a_pool_that_cannot_take_any_size_still_returns_nothing(monkeypatch):
    out, _calls = _run(monkeypatch, ceiling_atomic=0, held=1000)
    assert out is None                           # fail-closed, unchanged
