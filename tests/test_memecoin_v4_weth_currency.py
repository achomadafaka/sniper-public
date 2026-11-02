"""Step 0: the WETH TOKEN as a second input currency on V4 (spec 2026-08-30 §A3.2).

`v4_quote_amount_out` accepted only pools whose currency0 is NATIVE, so a pool quoted against the
WETH ERC-20 was never quoted. Measured on armed-wallet 4663 coins (n=22): 55% have a sane
native pool, 36% have no sane ETH-side pool, and 9% have ONLY a sane WETH-token pool.

The live case, mint 0x94d84dac… ("JOE"): its one honest ETH-side pool is WETH-token at 7% fee,
while all eight of its NATIVE pools charge 15% to 93%. We quoted native-only, so we refused it as
no_route and would have been correct to refuse every pool we could see.

V4 PoolKeys are ADDRESS-SORTED. Native (0x00…0) is therefore always currency0, but the WETH token
can land on either side depending on the mint's address, and `zero_for_one` must follow. That is
the correctness trap in this change and every ordering is pinned below.
"""
import pytest

import sniper.execution.evm_v4_quote as V

CHAIN = "4663"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
NATIVE = V._NATIVE
LOW_MINT = "0x00aa" + "11" * 18          # sorts BEFORE weth
HIGH_MINT = "0xffbb" + "22" * 18         # sorts AFTER weth


def _key(c0, c1, fee=3000):
    return {"currency0": c0, "currency1": c1, "fee": fee, "tick_spacing": 60, "hooks": NATIVE}


@pytest.fixture
def wire(monkeypatch):
    """Drive the real resolver with synthetic pools; no network."""
    state = {"pools": [], "keys": {}, "quotes": {}, "calls": []}

    def pools_for_mint(chain, mint, gt_http=None, ds_http=None, rpc_url=None, http=None):
        return list(state["pools"])

    def pool_key(chain, pid, rpc_url, http=None):
        return state["keys"].get(pid)

    def quote_single(chain, key, zf1, amount_in, rpc_url, http=None):
        state["calls"].append((key["currency0"], key["currency1"], zf1))
        return state["quotes"].get((key["currency0"], key["currency1"], zf1))

    monkeypatch.setattr(V, "_pools_for_mint", pools_for_mint)
    monkeypatch.setattr(V, "_pool_key", pool_key)
    monkeypatch.setattr(V, "_quote_single", quote_single)
    monkeypatch.setattr(V, "begin_quote", lambda *a, **k: None, raising=False)
    # Step 0 ships DARK. These tests turn it on explicitly; the default-off behaviour has its
    # own test below so "it lands dark" is pinned rather than assumed.
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY", True, raising=False)
    return state


def _run(mint, sell, buy, amt=10**18):
    return V.v4_quote_amount_out(CHAIN, sell, buy, amt, rpc_url="http://rpc")


# --- no regression: native pools keep working exactly as before -----------------------------

def test_native_pool_still_quotes(wire):
    wire["pools"] = ["p_nat"]
    wire["keys"]["p_nat"] = _key(NATIVE, LOW_MINT)
    wire["quotes"][(NATIVE, LOW_MINT, True)] = 500
    r = _run(LOW_MINT, WETH, LOW_MINT)
    assert r and r["amount_out"] == 500
    assert r["pool_key"]["currency0"] == NATIVE


def test_native_fee_trap_still_refused(wire):
    """JOE's native pools charge 15-93%. This must not become reachable via step 0."""
    wire["pools"] = ["p_trap"]
    wire["keys"]["p_trap"] = _key(NATIVE, LOW_MINT, fee=864114)
    wire["quotes"][(NATIVE, LOW_MINT, True)] = 10**9
    assert _run(LOW_MINT, WETH, LOW_MINT) is None


# --- step 0: the WETH token, both address orderings ------------------------------------------

def test_weth_token_pool_is_quoted_when_mint_sorts_high(wire):
    """WETH is currency0, mint currency1. Buy = zeroForOne True."""
    wire["pools"] = ["p_w"]
    wire["keys"]["p_w"] = _key(WETH, HIGH_MINT)
    wire["quotes"][(WETH, HIGH_MINT, True)] = 700
    r = _run(HIGH_MINT, WETH, HIGH_MINT)
    assert r is not None, "WETH-token pool not reachable (step 0 missing)"
    assert r["amount_out"] == 700
    assert r["zero_for_one"] is True


def test_weth_token_pool_is_quoted_when_mint_sorts_low(wire):
    """Mint is currency0, WETH currency1. Buy must be zeroForOne FALSE.

    Getting this backwards quotes the pool in the wrong direction and returns a number that is
    not the trade — the worst kind of wrong, because it still looks like a quote.
    """
    wire["pools"] = ["p_w"]
    wire["keys"]["p_w"] = _key(LOW_MINT, WETH)
    wire["quotes"][(LOW_MINT, WETH, False)] = 900
    r = _run(LOW_MINT, WETH, LOW_MINT)
    assert r is not None
    assert r["amount_out"] == 900
    assert r["zero_for_one"] is False


def test_sell_direction_inverts_for_both_orderings(wire):
    wire["pools"] = ["p_a"]
    wire["keys"]["p_a"] = _key(WETH, HIGH_MINT)
    wire["quotes"][(WETH, HIGH_MINT, False)] = 42
    r = V.v4_quote_amount_out(CHAIN, HIGH_MINT, WETH, 10**18, rpc_url="http://rpc")
    assert r and r["zero_for_one"] is False

    wire["pools"] = ["p_b"]
    wire["keys"]["p_b"] = _key(LOW_MINT, WETH)
    wire["quotes"] = {(LOW_MINT, WETH, True): 43}
    r = V.v4_quote_amount_out(CHAIN, LOW_MINT, WETH, 10**18, rpc_url="http://rpc")
    assert r and r["zero_for_one"] is True


def test_weth_pool_fee_is_screened_independently(wire):
    """JOE's WETH pool is sane at 7% while its native pools are traps. The reverse must also hold:
    a trap WETH pool is refused even when a native pool is absent."""
    wire["pools"] = ["p_w"]
    wire["keys"]["p_w"] = _key(WETH, HIGH_MINT, fee=900000)
    wire["quotes"][(WETH, HIGH_MINT, True)] = 10**9
    assert _run(HIGH_MINT, WETH, HIGH_MINT) is None


def test_deepest_wins_across_currencies(wire):
    """A sane WETH pool must beat a sane-but-thinner native pool, and vice versa."""
    wire["pools"] = ["p_nat", "p_w"]
    wire["keys"]["p_nat"] = _key(NATIVE, HIGH_MINT)
    wire["keys"]["p_w"] = _key(WETH, HIGH_MINT)
    wire["quotes"][(NATIVE, HIGH_MINT, True)] = 100
    wire["quotes"][(WETH, HIGH_MINT, True)] = 800
    r = _run(HIGH_MINT, WETH, HIGH_MINT)
    assert r["amount_out"] == 800 and r["pool_key"]["currency0"] == WETH


def test_quote_reports_which_currency_it_used(wire):
    """The exit must bind to the entry currency, so the quote has to say which one it picked.

    Without this a coin with an honest WETH pool and trap native pools could exit through the
    trap it avoided on entry.
    """
    wire["pools"] = ["p_w"]
    wire["keys"]["p_w"] = _key(WETH, HIGH_MINT)
    wire["quotes"][(WETH, HIGH_MINT, True)] = 700
    r = _run(HIGH_MINT, WETH, HIGH_MINT)
    assert r.get("quote_currency") == WETH

    wire["pools"] = ["p_n"]
    wire["keys"]["p_n"] = _key(NATIVE, HIGH_MINT)
    wire["quotes"] = {(NATIVE, HIGH_MINT, True): 700}
    r = _run(HIGH_MINT, WETH, HIGH_MINT)
    assert r.get("quote_currency") == NATIVE


def test_foreign_pool_is_still_refused(wire):
    """A pool quoted against SPY/USDG is NOT unlocked by step 0 — that is B and C."""
    SPY = "0x117cc2133c37b721f49de2a7a74833232b3b4c0c"
    wire["pools"] = ["p_spy"]
    wire["keys"]["p_spy"] = _key(SPY, HIGH_MINT, fee=0)
    wire["quotes"][(SPY, HIGH_MINT, True)] = 10**6
    assert _run(HIGH_MINT, WETH, HIGH_MINT) is None


def test_step0_is_OFF_by_default(monkeypatch, wire):
    """It must land dark: with the flag off, a WETH-token pool stays unreachable."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY", False, raising=False)
    wire["pools"] = ["p_w"]
    wire["keys"]["p_w"] = _key(WETH, HIGH_MINT)
    wire["quotes"][(WETH, HIGH_MINT, True)] = 700
    assert _run(HIGH_MINT, WETH, HIGH_MINT) is None


def test_default_flag_value_is_false():
    """Pinned separately from behaviour: a default flipped by accident is how dark code ships lit."""
    import os, importlib
    from sniper.settings import memecoin as cfg
    assert os.getenv("MEMECOIN_EVM_V4_WETH_CURRENCY") or cfg.MEMECOIN_EVM_V4_WETH_CURRENCY is False


# --- per-chain override (2026-09-16, Arc build) -----------------------------------------------
# Arc's quote currency is an ERC-20 (USDC), so it needs this ON to quote at all, but the global
# flag also changes pool selection on 4663/8453 -- an unassessed blast radius. These tests pin
# that the per-chain set turns the behaviour on for the named chain ONLY.

ARC = "5042"
from sniper.execution.evm_wallet import CHAIN_QUOTE as _CHAIN_QUOTE
ARC_QUOTE = _CHAIN_QUOTE[ARC]["token"]   # real Arc USDC -- v4_quote_amount_out compares against
                                          # this exact address, not the 4663 WETH constant above


def test_per_chain_set_enables_weth_currency_without_the_global_flag(monkeypatch, wire):
    """Global flag stays False; only ARC is listed. ARC must still see the WETH-token pool."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS", {ARC}, raising=False)
    wire["pools"] = ["p_w"]
    wire["keys"]["p_w"] = _key(ARC_QUOTE, HIGH_MINT)
    wire["quotes"][(ARC_QUOTE, HIGH_MINT, True)] = 700
    r = V.v4_quote_amount_out(ARC, ARC_QUOTE, HIGH_MINT, 10**18, rpc_url="http://rpc")
    assert r is not None and r["amount_out"] == 700


def test_a_chain_not_in_the_set_is_unaffected_by_the_set(monkeypatch, wire):
    """The set names ARC only. A different chain must stay dark exactly as before."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS", {ARC}, raising=False)
    wire["pools"] = ["p_w"]
    wire["keys"]["p_w"] = _key(WETH, HIGH_MINT)
    wire["quotes"][(WETH, HIGH_MINT, True)] = 700
    assert _run(HIGH_MINT, WETH, HIGH_MINT) is None    # CHAIN = "4663", not in the set


def test_4663_and_8453_selection_is_byte_identical_when_the_set_holds_only_arc(monkeypatch, wire):
    """The load-bearing pin Alpha asked for: with MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS = {'5042'},
    both funded chains' pool selection must come out byte-for-byte the same as with the set empty
    -- same result whether it is refused (dark) or resolved (a native pool, unrelated to WETH
    currency), on both a native-pool coin and a WETH-token-only coin."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY", False, raising=False)

    for chain in ("4663", "8453"):
        chain_weth = _CHAIN_QUOTE[chain]["token"]   # each chain's REAL quote token, not the
                                                     # 4663-only WETH constant used elsewhere
        for pools, keys, quotes in (
            (["p_nat"], {"p_nat": _key(NATIVE, LOW_MINT)}, {(NATIVE, LOW_MINT, True): 500}),
            (["p_w"], {"p_w": _key(chain_weth, HIGH_MINT)}, {(chain_weth, HIGH_MINT, True): 700}),
        ):
            results = []
            for chain_set in (set(), {ARC}):
                monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS", chain_set,
                                    raising=False)
                wire["pools"], wire["keys"], wire["quotes"] = list(pools), dict(keys), dict(quotes)
                mint = LOW_MINT if "p_nat" in pools else HIGH_MINT
                results.append(V.v4_quote_amount_out(chain, chain_weth, mint, 10**18,
                                                      rpc_url="http://rpc"))
            assert results[0] == results[1], (
                "chain=%s pool selection changed when the WETH-currency set gained an unrelated "
                "chain (5042)" % chain)


def test_the_global_flag_still_works_standalone(monkeypatch, wire):
    """Regression: the per-chain set is additive, not a replacement for the global flag."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS", set(), raising=False)
    wire["pools"] = ["p_w"]
    wire["keys"]["p_w"] = _key(WETH, HIGH_MINT)
    wire["quotes"][(WETH, HIGH_MINT, True)] = 700
    r = _run(HIGH_MINT, WETH, HIGH_MINT)
    assert r is not None and r["amount_out"] == 700


def test_default_chains_set_is_empty():
    import os
    from sniper.settings import memecoin as cfg
    assert os.getenv("MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS") or cfg.MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS == set()


# --- transaction shape: the part where getting it wrong loses money -------------------------

def _tx(monkeypatch, quote_currency, acct=object()):
    """dex_v4_quote with the resolver stubbed to return a chosen currency."""
    monkeypatch.setattr(V, "v4_quote_amount_out", lambda *a, **k: {
        "amount_out": 1000, "pool_id": "p", "pool_key": _key(quote_currency, HIGH_MINT),
        "zero_for_one": True, "pool_fee": 3000, "quote_currency": quote_currency})
    monkeypatch.setattr(V, "_permit2_input", lambda *a, **k: "00" * 64)
    return V.dex_v4_quote(CHAIN, WETH, HIGH_MINT, 10**18, "0xtaker",
                          rpc_url="http://rpc", acct=acct, now_fn=lambda: 0)


def test_native_buy_still_unwraps_and_pays_value(monkeypatch):
    r = _tx(monkeypatch, NATIVE)
    assert r["pre_transactions"], "native buy must still unwrap WETH"
    assert V._SEL_WETH_WITHDRAW in r["pre_transactions"][0]["data"]
    assert r["transaction"]["value"] == hex(10**18)


def test_weth_buy_does_NOT_unwrap_and_sends_zero_value(monkeypatch):
    """The failure this pins: unwrapping to native and then paying native into a pool whose
    currency is the WETH ERC-20. That is a malformed funded transaction, not a missed fill."""
    r = _tx(monkeypatch, WETH)
    assert r["pre_transactions"] == [], "WETH route must NOT emit WETH.withdraw"
    assert r["transaction"]["value"] == "0x0", "an ERC-20 cannot be sent as tx value"


def test_weth_buy_fails_closed_without_a_signer(monkeypatch):
    """An ERC-20 payment needs a permit2 signature; no signer must refuse, never fall back."""
    assert _tx(monkeypatch, WETH, acct=None) is None


def test_route_records_the_currency_for_the_exit(monkeypatch):
    for cur in (NATIVE, WETH):
        r = _tx(monkeypatch, cur)
        assert r["route"]["quote_currency"] == cur, "exit cannot bind to an unrecorded currency"


def test_missing_quote_currency_defaults_to_native(monkeypatch):
    """Back-compat: a resolver result predating step 0 keeps the old native behaviour."""
    monkeypatch.setattr(V, "v4_quote_amount_out", lambda *a, **k: {
        "amount_out": 1000, "pool_id": "p", "pool_key": _key(NATIVE, HIGH_MINT),
        "zero_for_one": True, "pool_fee": 3000})
    r = V.dex_v4_quote(CHAIN, WETH, HIGH_MINT, 10**18, "0xtaker",
                       rpc_url="http://rpc", acct=object(), now_fn=lambda: 0)
    assert r["pre_transactions"], "absent quote_currency must behave as native"


# --- the invariant 16 focused tests missed --------------------------------------------------

def test_any_branch_emitting_a_permit2_command_must_declare_its_spender(monkeypatch):
    """A Permit2 permit only works if the ERC-20 has ALREADY approved Permit2 as spender.

    The executor sets that approval ONLY when the quote declares one
    (evm_swap_executor.py: `allow = (quote.get("issues") or {}).get("allowance")` then
    `if allow and allow.get("spender")`). So a branch that emits _CMD_PERMIT2_PERMIT while
    reporting `allowance: None` asks the router to run a permit that cannot succeed, and the swap
    reverts on-chain.

    This is asserted as an INVARIANT over every branch rather than for the WETH buy specifically.
    The bug it pins was introduced by copying the NATIVE buy's shape — where `allowance: None` is
    correct, because native input has no allowance concept — into a branch that pays an ERC-20.
    A per-branch test would not have stopped that; this does, including for branches not yet
    written.
    """
    import sniper.execution.evm_v4_quote as M
    conf = M.V4[CHAIN]
    seen = []

    def capture(commands, inputs, deadline):
        seen.append(bytes(commands))
        return "0xdata"

    monkeypatch.setattr(M, "_ur_execute_data", capture)
    monkeypatch.setattr(M, "_permit2_input", lambda *a, **k: "00" * 64)

    for currency in (NATIVE, WETH):
        for sell, buy in ((WETH, HIGH_MINT), (HIGH_MINT, WETH)):   # buy and sell
            seen.clear()
            monkeypatch.setattr(M, "v4_quote_amount_out", lambda *a, **k: {
                "amount_out": 1000, "pool_id": "p", "pool_key": _key(currency, HIGH_MINT),
                "zero_for_one": True, "pool_fee": 3000, "quote_currency": currency})
            r = M.dex_v4_quote(CHAIN, sell, buy, 10**18, "0xtaker",
                               rpc_url="http://rpc", acct=object(), now_fn=lambda: 0)
            if r is None:
                continue
            commands = seen[-1] if seen else b""
            allowance = (r.get("issues") or {}).get("allowance")
            if bytes([M._CMD_PERMIT2_PERMIT]) in commands:
                assert allowance and allowance.get("spender") == conf["permit2"], (
                    "branch emits PERMIT2_PERMIT but declares no allowance spender, so the "
                    "executor never approves Permit2 on the ERC-20 and the swap reverts "
                    "(currency=%s sell=%s)" % (currency, sell)
                )
            else:
                assert allowance is None, (
                    "branch declares an allowance it never uses (currency=%s sell=%s)"
                    % (currency, sell)
                )


# --- the EXIT half. Making the buy currency-aware and leaving the sell native-only is worse
# --- than not shipping either, because the position opens and cannot close correctly.

def _sell_tx(monkeypatch, quote_currency, zero_for_one, mint=HIGH_MINT):
    import sniper.execution.evm_v4_quote as M
    seen = {}

    def capture(commands, inputs, deadline):
        seen["commands"] = bytes(commands)
        return "0xdata"

    def swap_input(key, zf1, amt, min_out, take_all_to_sender=False):
        seen["zf1"] = zf1
        seen["take_all_to_sender"] = take_all_to_sender
        return "00" * 32

    monkeypatch.setattr(M, "_ur_execute_data", capture)
    monkeypatch.setattr(M, "_permit2_input", lambda *a, **k: "00" * 64)
    monkeypatch.setattr(M, "_v4_swap_input", swap_input)
    key = _key(quote_currency, mint) if quote_currency < mint else _key(mint, quote_currency)
    monkeypatch.setattr(M, "v4_quote_amount_out", lambda *a, **k: {
        "amount_out": 1000, "pool_id": "p", "pool_key": key,
        "zero_for_one": zero_for_one, "pool_fee": 3000, "quote_currency": quote_currency})
    r = M.dex_v4_quote(CHAIN, mint, WETH, 10**18, "0xtaker",
                       rpc_url="http://rpc", acct=object(), now_fn=lambda: 0)
    return r, seen, M


def test_native_sell_still_wraps(monkeypatch):
    r, seen, M = _sell_tx(monkeypatch, NATIVE, zero_for_one=False)
    assert bytes([M._CMD_WRAP_ETH]) in seen["commands"], "native sell must still wrap to WETH"


def test_weth_sell_must_NOT_wrap(monkeypatch):
    """Selling into a WETH-CURRENCY pool already yields the WETH token. Emitting WRAP_ETH there
    wraps a native balance that does not exist — a malformed exit on a funded position."""
    r, seen, M = _sell_tx(monkeypatch, WETH, zero_for_one=True, mint=LOW_MINT)
    assert bytes([M._CMD_WRAP_ETH]) not in seen["commands"], (
        "WETH-currency sell emitted WRAP_ETH; there is no native balance to wrap"
    )


def test_weth_sell_takes_output_to_the_sender(monkeypatch):
    """With no wrap step there is nothing to collect from the router, so the swap must deliver
    straight to us. take_all_to_sender=False would strand the proceeds in the router."""
    r, seen, M = _sell_tx(monkeypatch, WETH, zero_for_one=True, mint=LOW_MINT)
    assert seen["take_all_to_sender"] is True


def test_sell_uses_the_quotes_direction_not_a_hardcoded_one(monkeypatch):
    """The sell branch hardcoded zero_for_one=False. That is right only when the mint is
    currency1, which is always true for native pools and NOT true for WETH pools whose mint
    sorts low. A hardcoded direction quotes one pool and swaps another."""
    r, seen, M = _sell_tx(monkeypatch, WETH, zero_for_one=True, mint=LOW_MINT)
    assert seen["zf1"] is True, "sell ignored the quote's direction"
    r, seen, M = _sell_tx(monkeypatch, NATIVE, zero_for_one=False)
    assert seen["zf1"] is False


@pytest.mark.parametrize("currency,is_buy,zf1", [
    (NATIVE, True, True), (NATIVE, True, False),
    (WETH, True, True), (WETH, True, False),
    (NATIVE, False, False), (NATIVE, False, True),
    (WETH, False, True), (WETH, False, False),
])
def test_every_branch_passes_the_quotes_direction_to_the_swap(monkeypatch, currency, is_buy, zf1):
    """All four branches must hand `_v4_swap_input` the direction the QUOTE resolved.

    Added after mutation testing: hardcoding the direction at three of the four call sites went
    undetected by 21 tests, because each branch had its own site and only one was covered. A
    hardcoded direction quotes one side of the pool and swaps the other, which returns a
    plausible amount and executes the wrong trade.
    """
    import sniper.execution.evm_v4_quote as M
    seen = {}
    monkeypatch.setattr(M, "_ur_execute_data", lambda c, i, d: "0xdata")
    monkeypatch.setattr(M, "_permit2_input", lambda *a, **k: "00" * 64)
    monkeypatch.setattr(M, "_v4_swap_input",
                        lambda key, z, amt, mo, take_all_to_sender=False: seen.__setitem__("zf1", z) or "00")
    monkeypatch.setattr(M, "v4_quote_amount_out", lambda *a, **k: {
        "amount_out": 1000, "pool_id": "p", "pool_key": _key(currency, HIGH_MINT),
        "zero_for_one": zf1, "pool_fee": 3000, "quote_currency": currency})
    sell, buy = (WETH, HIGH_MINT) if is_buy else (HIGH_MINT, WETH)
    r = M.dex_v4_quote(CHAIN, sell, buy, 10**18, "0xtaker",
                       rpc_url="http://rpc", acct=object(), now_fn=lambda: 0)
    assert r is not None
    assert seen["zf1"] is zf1, (
        "branch (currency=%s is_buy=%s) ignored the quote's direction" % (currency, is_buy)
    )
