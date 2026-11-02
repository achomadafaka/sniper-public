"""Full entry-then-exit through the real runner seams (build_real_evm_executor), for the
launchpad-curve venue -- the same pattern test_memecoin_evm_seams.py's
test_build_executor_execute_fn_fills uses for the V4 rung, so reservation identity, write-ahead,
and balance-delta accounting are proven the SAME WAY, not a bespoke one for this rung.

Fake send/confirm only (an http stub); nothing is broadcast. `venue_quote_fn` is this rung's own
direction-aware closure -- the same dispatch `evm_real_seams.py`'s ladder wiring uses
(`is_buy` picks the buy quote vs. the exit quote), injected directly so this test proves the
seam contract without needing the full multi-rung ladder race.
"""
import json
import sqlite3

from eth_account import Account

import sniper.execution.evm_launchpad_curve as curve_mod
from sniper.execution.evm_real_seams import build_real_evm_executor
from sniper.persistence import db_memecoin as m

MINT = "0x5e9ca04c6fe6397f287091130a63072a91afd14f"
CURVE = "0xd1255489d894452dd234a001f0fbcab6d4037040"
NATIVE = "0x" + "00" * 20
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
GRADUATED_KEY = ("0xpoolid", {"currency0": NATIVE, "currency1": MINT,
                              "fee": 0, "tick_spacing": 200, "hooks": "0xhook"}, True)


_SENT = []      # raw transactions this fake node has accepted


def _http(method, url, headers=None, body=None, timeout=10):
    req = json.loads(body)
    meth = req.get("method")
    if meth == "eth_getTransactionCount":
        return 200, {"result": "0x5"}, None
    if meth == "eth_gasPrice":
        return 200, {"result": "0x3b9aca00"}, None
    if meth == "eth_sendRawTransaction":
        _SENT.append(req["params"][0])
        return 200, {"result": "0xhash"}, None
    if meth == "eth_getTransactionReceipt":
        return 200, {"result": {"status": "0x1"}}, None
    if meth == "eth_getBalance":
        # 100 native, covering any exit gas check; once a transaction went out (the unwrap), the
        # node shows its ETH, as a real one does: the plain path waits for exactly that (8453
        # PICNIC 2026-10-06), pre-unwrap native plus the unwrap.
        return 200, {"result": hex(10 ** 20 + (10 ** 21 if _SENT else 0))}, None
    if meth == "eth_call":
        # a huge held balance either way -- the EXIT reads the real raw on-chain balance before
        # ever calling venue_quote_fn (erc20_balance_raw), so a zero balance here means the exit
        # bails before this test's own quote closure runs at all.
        return 200, {"result": "0x" + "f" * 64}, None
    return 200, {"result": "0x0"}, None


TAKER = "0x" + "9e" * 20


def _venue_quote_fn(state):
    """The same direction-aware dispatch evm_real_seams.py's ladder wires in: pick the buy or the
    exit quote by which side of the call is the chain's WETH.

    The LADDER separately measures a round trip by quoting the REVERSE of whatever this answers
    (test_memecoin_evm_seams.py's own `venue_quote` stub does the same thing, for the same
    reason): a real reverse quote against this test's static, non-chain-tracking reserves stub
    would price the reverse leg off pre-trade reserves and read as a nonsense round trip. Answer
    the reverse call at ~97% of the forward clip -- ordinary fees -- the same way the V4 rung's
    own test does; the round-trip formula itself is proven separately, with real chain numbers,
    in the design spec's phase-3/4 evidence, not re-derived here.
    """
    seen = {}

    def quote(sell, buy, amount, *, slippage_bps=None):
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in seen:
            return {"sellAmount": str(amount), "buyAmount": str(int(seen[pair[::-1]] * 0.97)),
                    "transaction": {"to": CURVE, "data": "0x", "value": "0"},
                    "issues": {"allowance": None}}
        is_buy = str(sell).lower() == WETH
        state_fn = lambda chain, mint, rpc_url, http=None: state
        reserves_fn = lambda curve, rpc_url, http=None: (
            1689900000000000001, 994141665187289188709391089)
        fn = curve_mod.dex_launchpad_curve_quote if is_buy else curve_mod.dex_launchpad_curve_exit_quote
        out = fn(
            "4663", sell, buy, amount, TAKER, rpc_url="https://rpc",
            slippage_bps=slippage_bps, state_fn=state_fn, reserves_fn=reserves_fn)
        if out is not None:
            # Store what was PAID (sellAmount), not what was received -- the reverse call's 97%
            # answer must land back near the ORIGINAL spend, matching the established
            # `_measurable()` pattern (test_memecoin_evm_quote_miss_attribution.py). Storing the
            # received amount instead sends the reverse leg's units off by orders of magnitude
            # (coin units standing in for WETH units) and reads as a nonsense round trip.
            seen[pair] = int(out["sellAmount"])
        return out
    return quote


def _seq(vals):
    it = {"i": 0}

    def f(*_a, **_k):
        v = vals[min(it["i"], len(vals) - 1)]
        it["i"] += 1
        return v
    return f


ON_CURVE = {"coin": MINT, "curve": CURVE, "counter": NATIVE, "fee_bps": 100, "state": 0}
GRADUATED = {"coin": MINT, "curve": CURVE, "counter": NATIVE, "fee_bps": 100, "state": 2}


def test_entry_fills_through_the_curve_rung_with_the_same_write_ahead_as_v4(monkeypatch):
    _SENT.clear()
    from sniper.execution import evm_real_seams as S
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint=MINT, source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="4663")
    from evm_receipt_fake import wrap
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    acct = Account.create()
    ex = build_real_evm_executor(
        acct=acct, chain_id="4663", rpc_url="https://rpc",
        http=wrap(_http, owner=acct.address, mint=MINT, quote=CHAIN_QUOTE["4663"]["token"],
                  coin=5, paid=10 * 10 ** 18),           # the swap's receipt: 5 coins, 10 WETH
        venue_quote_fn=_venue_quote_fn(ON_CURVE),
        usdc_fn=_seq([100.0, 90.0]), token_fn=_seq([0.0, 5.0]))
    r = ex["execute_fn"](c, pid, MINT, 10.0, "2026-09-17T00:00:01Z")
    assert r["status"] == "filled"
    pos = m.get_position(c, r["position_id"])
    assert pos["execution_mode"] == "real" and pos["network_id"] == "4663"
    assert abs(pos["entry_price"] - 2.0) < 1e-9                 # SAME balance-delta math as V4
    assert m.pending_row(c, pid)["nonce"] == 5                  # SAME write-ahead as V4
    route = json.loads(pos["evm_route_json"] or "{}")
    assert route.get("protocol") == "launchpad_curve"


def test_exit_closes_through_the_curve_rung_while_on_curve(monkeypatch):
    from sniper.execution import evm_real_seams as S
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(
        c, source_user_id="u1", source_handle="(real)", mint=MINT, ticker="",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at="2026-09-17T00:00:01Z", strategy="alpha_b",
        execution_mode="real", network_id="4663")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="2026-09-17T00:00:01Z", tx_sig="E")
    position = m.get_position(c, pid)
    ex = build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc", http=_http,
        venue_quote_fn=_venue_quote_fn(ON_CURVE),
        usdc_fn=_seq([0.0, 15.0]), token_fn=lambda _mint: 10.0)
    result = ex["exit_fn"](c, position, "mirror_exit", "2026-09-17T00:05:00Z")
    assert result["status"] == "closed"


def test_a_position_that_graduates_mid_hold_switches_to_the_derived_v4_key(monkeypatch):
    """The graduation-mid-hold switch through the runner exit path. No stored per-position venue
    field blocks this -- exit_fn re-quotes fresh through venue_quote_fn on every call (confirmed
    in the integration-points research: evm_route_json is consulted only for the
    uniswap_v4_multihop stored-route optimization, every other protocol re-derives), so a coin
    that graduates between entry and exit needs no special handling: this position was opened
    while on-curve, and the SAME venue_quote_fn now sees state=2 and must not build a curve sell.
    """
    from sniper.execution import evm_real_seams as S
    from sniper.services import memecoin_exit_dust_guard as G
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    # The dust guard runs ON with an own 4663 tape mark PRESENT and down too (the quote pays
    # $0.009 for a $10 bag): a real collapse, so the switch to the derived key is what decides.
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_DUST_GUARD", True)
    monkeypatch.setattr(G, "_tape_mark", lambda *a: {"price": 0.001, "source": "test_tape_down_too"})
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(
        c, source_user_id="u1", source_handle="(real)", mint=MINT, ticker="",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at="2026-09-17T00:00:01Z", strategy="alpha_b",
        execution_mode="real", network_id="4663",
        evm_route_json=json.dumps({"protocol": "launchpad_curve", "curve": CURVE}))
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="2026-09-17T00:00:01Z", tx_sig="E")
    position = m.get_position(c, pid)

    calls = {"curve_sell_attempted": False}

    def graduated_venue_quote(sell, buy, amount, *, slippage_bps=None):
        is_buy = str(sell).lower() == WETH
        if not is_buy:
            state_fn = lambda chain, mint, rpc_url, http=None: GRADUATED
            out = curve_mod.dex_launchpad_curve_exit_quote(
                "4663", sell, buy, amount, TAKER, rpc_url="https://rpc",
                slippage_bps=slippage_bps, state_fn=state_fn)
            if out is not None:
                calls["curve_sell_attempted"] = True
                return out
            # graduated: fall back to a plain (non-curve) quote, standing in for the derived
            # V4-key path this rung hands off to (proven separately and already, on a real
            # graduated coin, in the design spec's phase-4 evidence -- exit_quote() there got
            # back the exact real confirmed pool id the coin's own graduation tx initialized).
            router = "0x" + "77" * 20
            return {"sellAmount": str(amount), "buyAmount": "9000000000000000",
                    "minBuyAmount": "8000000000000000",
                    "transaction": {"to": router, "data": "0x", "value": "0",
                                    "gas": "300000", "gasPrice": "1000000000"},
                    "issues": {"allowance": {"spender": router}},
                    "route": {"protocol": "derived_v4"}}
        return None

    ex = build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc", http=_http,
        venue_quote_fn=graduated_venue_quote,
        usdc_fn=_seq([0.0, 9.0]), token_fn=lambda _mint: 10.0)
    result = ex["exit_fn"](c, position, "mirror_exit", "2026-09-17T00:05:00Z")
    assert result["status"] == "closed"
    assert calls["curve_sell_attempted"] is False, \
        "a curve sell must never be built once the launchpad reports state != 0"
