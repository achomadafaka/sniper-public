"""The launchpad-curve rung inside the real entry/exit ladder (evm_real_seams.py).

Mirrors tests/test_memecoin_evm_quote_miss_attribution.py's fixture style. Verifies the flag gate,
the direction dispatch (`is_buy` picks `dex_launchpad_curve_quote` vs `_exit_quote`), and that
turning the flag on changes nothing for a coin the curve rung does not apply to (a graduated coin,
or a coin from a different launchpad) -- the other rungs still decide those.
"""
import threading

import pytest

import sniper.execution.evm_launchpad_curve as curve_mod
from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback


@pytest.fixture(autouse=True)
def _route_quote_workers_end_in_their_test():
    """The ladder returns on a curve hit and leaves its other rungs running; they ran the real v4 pool
    scan (evm_wallet._rpc -> urlopen) inside the NEXT test on the same xdist worker (Gamma, -n 10).
    Each test now waits for the evm-route-quote workers it started, and fails if one outlives 10 s."""
    before = set(threading.enumerate())
    yield
    mine = [t for t in threading.enumerate()
            if t not in before and t.name.startswith("evm-route-quote")]
    for t in mine:
        t.join(10.0)
    assert not [t.name for t in mine if t.is_alive()], "evm-route-quote worker outlived its test"

V3_QUOTE = {"sellAmount": "10000000", "buyAmount": "3",
           "transaction": {"to": "0xv3"}, "issues": {}}


def _measurable_v3():
    """A round-trip-measurable v3 stub, same shape test_memecoin_evm_quote_miss_attribution.py
    uses: answers the reverse (sell-side) leg at ~97% of the forward clip so the ladder's
    round-trip gate reads as ordinary fees rather than an unmeasured quote."""
    seen = {}

    def call(sell, buy, amount, *args, **kwargs):
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in seen:
            return {"sellAmount": str(amount), "buyAmount": str(int(seen[pair[::-1]] * 0.97)),
                    "transaction": {"to": "0xr"}, "issues": {"allowance": None}}
        seen[pair] = int(V3_QUOTE["sellAmount"])
        return V3_QUOTE
    return call


WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
MINT = "0x5e9ca04c6fe6397f287091130a63072a91afd14f"
CURVE = "0xd1255489d894452dd234a001f0fbcab6d4037040"
NATIVE = "0x" + "00" * 20


def _on(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", True)


def _stub_state_and_reserves(monkeypatch, *, state=0, counter=NATIVE):
    def state_fn(chain, mint, rpc_url, http=None):
        if str(mint).lower() != MINT:
            return None
        return {"coin": MINT, "curve": CURVE, "counter": counter, "fee_bps": 100, "state": state}

    def reserves_fn(curve, rpc_url, http=None):
        return (1689900000000000001, 994141665187289188709391089)

    monkeypatch.setattr(curve_mod, "curve_state", state_fn)
    monkeypatch.setattr(curve_mod, "curve_reserves", reserves_fn)


def test_ladder_skips_launchpad_curve_when_the_flag_is_off():
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="4663", quote_token=WETH, taker="0xtaker",
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )
    assert fn(WETH, MINT, 10 ** 16) is None
    assert rungs[0]["rung"] == "launchpad_curve"
    assert rungs[0]["skipped"] is True


def test_ladder_entry_hits_the_curve_when_the_flag_is_on(monkeypatch):
    _on(monkeypatch)
    _stub_state_and_reserves(monkeypatch)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="4663", quote_token=WETH, taker="0xtaker",
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )
    result = fn(WETH, MINT, 10 ** 16)
    assert result is not None
    assert result["route"]["protocol"] == "launchpad_curve"
    assert int(result["transaction"]["value"], 16) == 10 ** 16
    assert len(result["pre_transactions"]) == 1                 # WETH -> native unwrap first
    assert rungs[0]["rung"] == "launchpad_curve"
    assert rungs[0]["hit"] is True


def test_ladder_exit_hits_the_curve_when_the_flag_is_on(monkeypatch):
    _on(monkeypatch)
    _stub_state_and_reserves(monkeypatch)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="4663", quote_token=WETH, taker="0xtaker",
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )
    result = fn(MINT, WETH, 10 ** 20)
    assert result is not None
    assert result["route"]["protocol"] == "launchpad_curve"
    assert result["transaction"]["to"] == CURVE.lower()
    assert result["transaction"]["value"] == "0x0"               # curve pays US, we send nothing
    assert rungs[0]["hit"] is True


def _no_v4(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)


def test_ladder_falls_through_to_v3_once_the_coin_has_graduated(monkeypatch):
    """The curve rung's own state=0 gate makes it a clean miss on a graduated coin; the next rung
    in priority order still gets a chance -- this rung never blocks the ladder."""
    _on(monkeypatch)
    _no_v4(monkeypatch)
    _stub_state_and_reserves(monkeypatch, state=2)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        _measurable_v3(),
        chain="4663", quote_token=WETH, taker="0xtaker",
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )
    result = fn(WETH, MINT, 10 ** 16)
    assert result is not None
    assert result["buyAmount"] == "3"
    assert rungs[0]["rung"] == "launchpad_curve"
    assert rungs[0]["hit"] is False


def test_ladder_flag_on_does_not_touch_a_coin_this_rung_does_not_apply_to(monkeypatch):
    """A mint the launchpad has never heard of (curve_state -> None) must not change v3/v4
    behaviour at all -- the rung is a clean, unconditional miss for it."""
    _on(monkeypatch)
    _no_v4(monkeypatch)
    other_mint = "0x" + "77" * 20

    def state_fn(chain, mint, rpc_url, http=None):
        return None

    monkeypatch.setattr(curve_mod, "curve_state", state_fn)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        _measurable_v3(),
        chain="4663", quote_token=WETH, taker="0xtaker",
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )
    result = fn(WETH, other_mint, 10 ** 16)
    assert result["buyAmount"] == "3"
    assert rungs[0]["hit"] is False
