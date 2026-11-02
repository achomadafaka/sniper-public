"""`CurveGraduated()` is a stale route, not a verdict on the coin.

VLAD 0xd7bac75b (DegenCapitalLLC, chain 4663), 2026-09-08T20:24:06Z. The pons_v2 rung was the only
rung that hit — v3, v2 and v4 all missed and v4_multihop had `no_bridge_candidates` — and it quoted
a round trip of -5.87% twice. Both simulations then reverted:

    attempt 1  entry_simulation_min_out                        quote 23.8% over source
    attempt 2  entry_simulation_reverted ... 0x025ac17e        quote 38.9% over source, 1.1s later

`0x025ac17e` is `CurveGraduated()` — verified by keccak, and the assertion below re-derives it
rather than trusting this docstring. The token's Pons curve reads `graduated()=1` and
`sellableTokens()=0` today, so what happened is that the coin graduated off the bonding curve
between our quote and our simulation.

`_curve_state` DOES refuse a graduated curve, so this is not a missing check: a quote and a
simulation are two RPC calls and cannot be atomic. The defect was the response. The refusal was
terminal, which throws the coin away at the exact moment it becomes routable — a graduated coin
has a real V2/V3/V4 pool, and the next pass is the one that can find it. `PoolNotInitialized()`
already sets the precedent: a statement about a stale view of the route is retried, and the retry
re-runs the whole decision so nothing skips a gate.

The operator saw `custom_error 0x025ac17e` and had to ask what it was. 4byte.directory returns 403
to this host and would not carry a 4663 contract anyway, so the name has to come from us.
"""
from eth_utils import keccak

import sniper.execution.evm_real_seams as s
import sniper.services.memecoin_real_runner as R


def _reason(detail):
    return "entry_simulation_reverted: exact route reverted; detail=" + detail


# --- the identification is derived, never asserted from memory -----------------------------------

def test_the_selector_really_is_curve_graduated():
    assert keccak(text="CurveGraduated()")[:4].hex() == "025ac17e"
    assert keccak(text="PoolNotInitialized()")[:4].hex() == "486aa307"
    for sel, sig in s._KNOWN_ERROR_SELECTORS.items():
        assert keccak(text=sig)[:4].hex() == sel, (sig, sel)


# --- naming ---------------------------------------------------------------------------------------

def test_a_known_selector_is_named_and_keeps_its_hex():
    assert s._revert_detail("0x025ac17e") == "CurveGraduated() 0x025ac17e"


def test_an_unknown_selector_still_says_it_is_unknown():
    """A guessed name would be worse than the hex. Only verified names appear."""
    assert s._revert_detail("0xdeadbeef") == "custom_error 0xdeadbeef"


def test_a_named_selector_keeps_its_payload():
    payload = "%064x" % 7
    assert s._revert_detail("0x025ac17e" + payload) == \
        "CurveGraduated() 0x025ac17e payload=0x" + payload


def test_naming_does_not_disturb_error_string_or_panic():
    assert s._revert_detail("0x4e487b71" + "%064x" % 0x11) == "Panic(0x11)"
    assert s._revert_detail("0xdeadbee", "execution reverted") == "execution reverted"


# --- retry ------------------------------------------------------------------------------------

def test_curve_graduated_is_retried():
    """THE FIX. VLAD's second attempt was terminal; it must not be."""
    assert R._is_transient_entry_miss(
        {"status": "route_failed", "reason": _reason("CurveGraduated() 0x025ac17e")})


def test_the_retry_survives_the_rename():
    """The matcher keys on the hex, so an older row written before the name still retries."""
    assert R._is_transient_entry_miss(
        {"status": "route_failed", "reason": _reason("custom_error 0x025ac17e")})


def test_pool_not_initialized_still_retries_both_ways():
    for detail in ("custom_error 0x486aa307", "PoolNotInitialized() 0x486aa307"):
        assert R._is_transient_entry_miss({"status": "route_failed", "reason": _reason(detail)})


def test_an_unexplained_revert_is_still_terminal():
    """Fail-closed stays fail-closed: only selectors we have identified earn a retry."""
    assert not R._is_transient_entry_miss(
        {"status": "route_failed", "reason": _reason("execution reverted")})
    assert not R._is_transient_entry_miss(
        {"status": "route_failed", "reason": _reason("custom_error 0xdeadbeef")})


def test_a_graduated_curve_is_not_quoted_in_the_first_place():
    """The retry is the response to a RACE, not a licence to quote a curve we can see is done."""
    from sniper.execution import evm_pons_v2_quote as P
    calls = {}

    def call_fn(to, data):
        sel = data[2:10]
        calls[sel] = True
        return {
            P._selector("token()"):        "0x" + "00" * 12 + "11" * 20,
            P._selector("pairToken()"):    "0x" + "00" * 32,
            P._selector("getReserves()"):  "0x" + "%064x%064x" % (10**18, 10**24),
            P._selector("feeBps()"):       "0x%064x" % 100,
            P._selector("creatorTaxBps()"):"0x%064x" % 100,
            P._selector("graduated()"):    "0x%064x" % 1,          # <- the coin has graduated
            P._selector("sellableTokens()"): "0x%064x" % 0,
            P._CURVE:                      "0x" + "00" * 12 + "22" * 20,
        }.get(sel)

    assert P.dex_pons_v2_quote(
        "4663", P._WETH, "0x" + "11" * 20, 10**16, "0x" + "33" * 20,
        slippage_bps=300, call_fn=call_fn, now_fn=lambda: 1788900000) is None
