"""Per-chain QUOTE-token abstraction (Robinhood Chain 4663 readiness, 2026-07-10). All 4663
memecoin pools quote in WETH (zero USDC), so the EVM executor's sell/receive token, sizing, and
bankroll must convert USD<->quote-token through a live ETH/USD price — FAIL-CLOSED on unknown
price. Also locks the unit-free tax ratio (the old *1e6 sizing failed OPEN on 18-dec BSC USDC)."""
import pytest
import json
import sqlite3

from sniper.execution import evm_real_seams as seams
from sniper.execution import evm_wallet as W
from sniper.execution.evm_token_safety import build_evm_token_safety
from sniper.persistence import db_memecoin as m

WETH_4663 = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"


@pytest.fixture(autouse=True)
def _route_cost_is_not_what_this_file_tests():
    """Every hit measures cheap, so these fixtures pin what they are actually about.

    The entry ladder measures a round trip by quoting the reverse of the bag a hit would buy
    (2026-09-06). A fixture with no `sellAmount`, or one answering the same amount in both
    directions, cannot be measured and the hit is correctly REFUSED as `round_trip_unmeasured`.
    Nothing in this file is about the cost of a route, so without this each test would be asserting
    on a refusal it was never written to describe. The tests that ARE about measurement live in
    tests/test_memecoin_evm_measured_ladder.py.
    """
    import sniper.execution.evm_real_seams as _S
    original = _S._round_trip_for_hit
    _S._round_trip_for_hit = lambda *_a, **_k: (-3.0, None)
    try:
        yield
    finally:
        _S._round_trip_for_hit = original


def test_chain_quote_map_shape():
    assert W.CHAIN_QUOTE["4663"] == {"token": WETH_4663, "decimals": 18, "kind": "eth",
                                     "usd_symbol": "ETH"}
    # 8453 MOVED FROM USDC TO WETH (2026-09-06, Base funded parity). Base memecoin pools are
    # native/WETH-paired the same way 4663's are, so a USDC bankroll needed a USDC -> WETH leg on
    # every route and a USDC -> ETH gas top-up that does not exist.
    assert W.CHAIN_QUOTE["8453"]["kind"] == "eth" and W.CHAIN_QUOTE["8453"]["decimals"] == 18
    assert W.CHAIN_QUOTE["56"]["decimals"] == 18          # Binance-Peg USDC truth (was assumed 6)


def test_quote_usd_price_stablecoin_is_one_without_price_lookup():
    called = []
    px = W.quote_usd_price("5042", price_fn=lambda: called.append(1) or 2500.0)   # chain 1 quotes in WETH since 2026-09-30 (eth-mainnet spec); Arc (5042) is the USD-quoted 6-decimal chain
    assert px == 1.0 and called == []                     # never consults ETH price for USDC chains


def test_quote_usd_price_on_base_now_reads_the_live_eth_mark():
    called = []
    px = W.quote_usd_price("8453", price_fn=lambda: called.append(1) or 2500.0)
    assert px == 2500.0 and called == [1]
    # fail-closed: no ETH mark means no sizing, exactly as on 4663
    assert W.quote_usd_price("8453", price_fn=lambda: None) is None


def test_quote_usd_price_eth_chain_uses_price_fn_and_fails_closed():
    assert W.quote_usd_price("4663", price_fn=lambda: 2500.0) == 2500.0
    assert W.quote_usd_price("4663", price_fn=lambda: None) is None
    assert W.quote_usd_price("4663", price_fn=lambda: 0.0) is None
    assert W.quote_usd_price("999999") is None            # unknown chain


def test_eth_usd_price_uses_fmp_when_binance_fails(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")

    def http(method, url, headers=None, body=None, timeout=10):
        if "binance.com" in url:
            raise OSError("Binance unavailable")
        if "financialmodelingprep.com/stable/quote" in url:
            return 200, [{"symbol": "ETHUSD", "price": 2475.25}], None
        raise AssertionError("unexpected price source")

    assert W.eth_usd_price(http=http) == 2475.25


def test_eth_usd_price_keeps_binance_primary(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")

    def http(method, url, headers=None, body=None, timeout=10):
        if "binance.com" in url:
            return 200, {"symbol": "ETHUSDT", "price": "2500.50"}, None
        raise AssertionError("the fallback must not run")

    assert W.eth_usd_price(http=http) == 2500.50


def test_eth_usd_price_fails_closed_when_both_sources_fail(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")

    def http(method, url, headers=None, body=None, timeout=10):
        if "binance.com" in url:
            raise OSError("Binance unavailable")
        return 200, [{"symbol": "ETHUSD", "price": 0}], None

    assert W.eth_usd_price(http=http) is None


def test_usd_to_quote_atomic_conversions():
    # $30 on a USDC-quoted chain -> 30e6 atomic, with no price lookup at all
    assert seams.usd_to_quote_atomic("5042", 30.0) == 30 * 10 ** 6      # Arc USDC is 6-decimal
    # $30 at ETH=$2500 -> 0.012 WETH -> 12e15 atomic, on BOTH wrapped-native chains
    for chain in ("4663", "8453", "56"):        # 56 joined the WETH-shaped side 2026-09-06
        assert seams.usd_to_quote_atomic(chain, 30.0, price_fn=lambda: 2500.0) == \
            12_000_000_000_000_000
        assert seams.usd_to_quote_atomic(chain, 30.0, price_fn=lambda: None) is None  # fail-closed
    assert seams.usd_to_quote_atomic("999999", 30.0) is None


def test_quote_balance_usd_eth_chain_math_and_fail_closed():
    def http(method, url, headers=None, body=None, timeout=10):
        b = json.loads(body)
        if b["method"] == "eth_call" and b["params"][0]["data"].startswith("0x313ce567"):
            return 200, {"result": hex(18)}, None                       # decimals()
        return 200, {"result": hex(2 * 10**18)}, None                   # balanceOf = 2 WETH
    usd = W.quote_balance_usd("0x" + "11" * 20, "4663", "http://rpc", http=http,
                              price_fn=lambda: 2500.0)
    assert abs(usd - 5000.0) < 1e-6                                     # 2 WETH x $2500
    assert W.quote_balance_usd("0x" + "11" * 20, "4663", "http://rpc", http=http,
                               price_fn=lambda: None) == 0.0            # no price -> no bankroll


def test_quote_balance_usd_or_none_is_the_display_twin():
    """The cockpit must never render an unknown balance as $0 — that reads as 'wallet empty' when it
    means 'we could not price it'. Money path stays fail-closed at 0.0; the display path says None."""
    def http(method, url, headers=None, body=None, timeout=10):
        b = json.loads(body)
        if b["method"] == "eth_call" and b["params"][0]["data"].startswith("0x313ce567"):
            return 200, {"result": hex(18)}, None
        return 200, {"result": hex(2 * 10**18)}, None                   # balanceOf = 2 WETH
    owner = "0x" + "11" * 20
    usd = W.quote_balance_usd_or_none(owner, "4663", "http://rpc", http=http,
                                      price_fn=lambda: 2500.0)
    assert abs(usd - 5000.0) < 1e-6                                     # WETH bankroll, not "0 USDC"
    assert W.quote_balance_usd_or_none(owner, "4663", "http://rpc", http=http,
                                       price_fn=lambda: None) is None   # unpriceable -> unknown
    assert W.quote_balance_usd_or_none(owner, "999999", "http://rpc", http=http) is None


def test_funded_quote_balance_reports_an_rpc_failure_as_unknown():
    def http(*_args, **_kwargs):
        raise OSError("RPC unavailable")

    ex = seams.build_real_evm_executor(
        acct=_acct(), chain_id="4663", rpc_url="http://rpc", http=http,
        price_fn=lambda: 2500.0,
    )

    assert ex["usdc_fn"]() is None


def test_weth_fill_delta_uses_one_price_snapshot(monkeypatch):
    """Never subtract two USD-valued whole-wallet balances priced at different ETH marks. A 0.4%
    ETH move on a 1-WETH wallet turns a real $30 spend into a fake ~$20 spend."""
    balances = iter([10**18, 988 * 10**15])              # 1.000 -> 0.988 WETH = 0.012 spent
    prices = iter([2500.0, 2510.0])
    price_calls = []

    def price_fn():
        price_calls.append(1)
        return next(prices)

    def http(method, url, headers=None, body=None, timeout=10):
        request = json.loads(body)
        data = request["params"][0]["data"]
        if data.startswith("0x313ce567"):
            return 200, {"result": hex(18)}, None
        return 200, {"result": hex(next(balances))}, None

    captured = {}

    def fake_entry(*args, **kwargs):
        balance = kwargs["usdc_fn"]
        captured["spent"] = balance() - balance()
        captured["entry_preflight"] = kwargs["entry_preflight_fn"]
        return {"status": "filled"}

    monkeypatch.setattr("sniper.execution.evm_swap_executor.execute_entry_evm", fake_entry)
    monkeypatch.setattr(seams, "preflight_weth_entry_quote", lambda *_a, **_kw: "wired")
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pending = m.reserve_pending(
        c, mint="0xToken", source_user_id="u1", strategy="alpha_b", notional=30.0,
        reserved_at="t", network_id="4663",
    )
    ex = seams.build_real_evm_executor(
        acct=_acct(), chain_id="4663", rpc_url="http://rpc", http=http, price_fn=price_fn,
    )

    ex["execute_fn"](c, pending, "0xToken", 30.0, "t")

    assert len(price_calls) == 1
    assert abs(captured["spent"] - 30.0) < 1e-9          # 0.012 WETH * one $2500 snapshot
    assert captured["entry_preflight"]({}) == "wired"


def test_weth_entry_does_not_broadcast_without_an_accounting_price(monkeypatch):
    called = []
    monkeypatch.setattr(
        "sniper.execution.evm_swap_executor.execute_entry_evm",
        lambda *args, **kwargs: called.append(1) or {"status": "filled"},
    )
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pending = m.reserve_pending(
        c, mint="0xToken", source_user_id="u1", strategy="alpha_b", notional=30.0,
        reserved_at="t", network_id="4663",
    )
    ex = seams.build_real_evm_executor(
        acct=_acct(), chain_id="4663", rpc_url="http://rpc",
        http=lambda *args, **kwargs: (500, {}, None), price_fn=lambda: None,
    )

    result = ex["execute_fn"](c, pending, "0xToken", 30.0, "t")

    assert result == {"status": "aborted", "reason": "quote-token price unavailable"}
    assert called == []
    assert m.pending_row(c, pending)["status"] == "failed"


def test_quote_symbol_names_the_token_the_bankroll_is_held_in():
    assert W.quote_symbol("4663") == "WETH"                             # NOT USDC — 4663 has none
    assert W.quote_symbol("8453") == "WETH"                             # 2026-09-06: Base too
    assert W.quote_symbol("56") == "WBNB"                               # and BSC, in WBNB
    assert W.quote_symbol("1") == "WETH"                                # 2026-09-30: Ethereum too
    assert W.quote_symbol("999999") is None


def _acct():
    from eth_account import Account
    return Account.from_key("0x" + "42" * 32)


def test_preview_preserves_exact_amounts_and_conversion(monkeypatch):
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_EVM_RELAY_ENABLED", False)
    monkeypatch.setattr(seams, "venue_quote_with_v2_fallback", lambda direct, **_: direct)
    amount = 99999999999999999999
    monkeypatch.setattr(W, "erc20_balance_raw", lambda *_a, **_k: amount)
    payload = {"sellAmount": str(amount), "buyAmount": "90000000000000000",
               "minBuyAmount": "63000000000000000", "route": {"protocol": "relay_same_chain", "request_id": "R"},
               "transaction": {"to": "0x" + "33" * 20, "data": "SECRET"}}
    ex = seams.build_real_evm_executor(
        acct=_acct(), chain_id="4663", rpc_url="rpc", price_fn=lambda: 1000,
        http=lambda *_a, **_k: pytest.fail("unexpected network request"),
        venue_quote_fn=lambda *_a, **_k: payload,
    )
    preview = ex["preview_exit_fn"]({"mint": "M", "id": 1})
    evidence = preview.quote_evidence
    assert evidence["input_atomic"] == str(amount)
    assert evidence["venue"] == "relay_same_chain"
    assert evidence["minimum_output_atomic"] == "63000000000000000"
    assert evidence["quote_asset_usd"] == 1000
    assert preview.expected_usdc == 90
    assert "SECRET" not in json.dumps(evidence)


@pytest.mark.parametrize("expected,minimum,qty,mcap,supply,allowed", [
    (90, 60, 10, None, None, False), (60, 40, 10, None, None, True),
    (45, 30, 5, None, None, False),
    # Display cap 50 cannot establish a cap crossing without a position supply basis.
    (90, 60, 10, 50, None, False), (90, 60, 10, 50, 10, True),
])
def test_exit_rechecks_expected_output_and_resets_a_false_stop(
        monkeypatch, expected, minimum, qty, mcap, supply, allowed):
    from test_memecoin_evm_exit_executor import _c, _pos
    c = _c(); pos = _pos(c, entry_notional=100)
    m.set_position_stop_hits(c, pos["id"], 2)
    m.set_position_stop_breach_started(c, pos["id"], "2026-07-09T00:01:00Z")
    # The entry fill contains ten tokens at $10 each.
    c.execute("UPDATE memecoin_positions SET entry_price=10 WHERE id=?", (pos["id"],))
    if supply is not None:
        c.execute("UPDATE memecoin_positions SET fdv_supply=? WHERE id=?", (supply, pos["id"]))
    c.commit()
    pos = m.get_position(c, pos["id"])
    pos["_exit_current_mcap"] = mcap
    monkeypatch.setattr(m, "effective_exit_override", lambda *_: {
        "sl_pct": -35, "sl_target_mcap": 100 if mcap else None})
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_EVM_RELAY_ENABLED", False)
    seen = []

    def execute(conn, position, reason, now, **kwargs):
        quote = {"buyAmount": str(expected * 10**15), "minBuyAmount": str(minimum * 10**15)}
        seen.append(kwargs["quote_check_fn"](position, quote, qty))
        return {"status": "deferred"}

    monkeypatch.setattr("sniper.execution.evm_exit_executor.execute_exit_evm", execute)
    ex = seams.build_real_evm_executor(
        acct=_acct(), chain_id="4663", rpc_url="http://rpc", price_fn=lambda: 1000,
        http=lambda *_a, **_k: pytest.fail("unexpected network request"),
        usdc_fn=lambda: 1000,
    )
    ex["exit_fn"](c, pos, "b_stop", "2026-07-09T00:05:00Z")
    assert seen[0][0] is allowed
    row = m.get_position(c, pos["id"])
    assert row["stop_hits"] == (2 if allowed else 0)
    assert bool(row["stop_breach_started_at"]) is allowed
    audit = c.execute("SELECT allowed,detail FROM memecoin_exit_trigger_audit ORDER BY id DESC").fetchone()
    assert bool(audit["allowed"]) is allowed
    detail = json.loads(audit["detail"])
    assert detail["execution_quote"] is True
    if supply is not None:
        assert detail["executable_mcap"] == 90 and detail["mcap_stop_confirmed"] is True


def test_entry_quote_on_4663_sells_weth_sized_by_eth_price():
    calls = []

    def quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        calls.append((sell_token, buy_token, amount_atomic, slippage_bps))
        return {"sellAmount": str(amount_atomic), "buyAmount": "1",
                "transaction": {"to": "0x" + "33" * 20, "data": "0x"}}

    ex = seams.build_real_evm_executor(acct=_acct(), chain_id="4663", rpc_url="http://rpc",
                                       venue_quote_fn=quote, price_fn=lambda: 2500.0)
    from sniper.execution.evm_real_seams import usd_to_quote_atomic
    atomic = usd_to_quote_atomic("4663", 30.0, price_fn=lambda: 2500.0)
    q = ex["venue_quote_fn"](
        W.CHAIN_QUOTE["4663"]["token"], "0xmint", atomic)
    assert q is not None
    assert calls == [(WETH_4663, "0xmint", 12_000_000_000_000_000, None)]


def test_token_safety_on_4663_uses_weth_for_the_sell_check():
    calls = []

    def quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        calls.append((sell_token, buy_token, amount_atomic))
        if sell_token.lower() == WETH_4663.lower():
            buy_amount = 1_000_000
        elif amount_atomic == 1_000_000:
            buy_amount = int(1.9e15)
        else:
            buy_amount = amount_atomic
        return {"sellAmount": str(amount_atomic), "buyAmount": str(buy_amount),
                "transaction": {"to": "0x" + "33" * 20, "data": "0x"}}

    def http(method, url, headers=None, body=None, timeout=10):
        b = json.loads(body)
        if b["method"] == "eth_getCode":
            return 200, {"result": "0xdeadbeef"}, None
        if b["method"] == "eth_call":
            d = b["params"][0].get("data", "")
            if d.startswith("0x313ce567"):
                return 200, {"result": hex(9)}, None                   # token decimals
            # The sell simulation uses state overrides. The slot probes must return the overridden
            # word so the test can resolve the standard ERC-20 layout.
            if d.startswith(("0x70a08231", "0xdd62ed3e")) and len(b["params"]) > 2:
                diff = {}
                for acct in b["params"][2].values():
                    diff.update(acct.get("stateDiff") or {})
                return 200, {"result": next(iter(diff.values()), "0x0")}, None
            return 200, {"result": "0x"}, None                         # sell sim ok (no revert)
        return 200, {"result": "0x0"}, None
    fn = build_evm_token_safety(chain_id="4663", rpc_url="http://rpc", taker="0x" + "11" * 20,
                                http=http, quote_fn=quote)
    ok, reason = fn("0x" + "22" * 20)
    assert ok, reason
    assert len(calls) == 1
    assert calls[0][1].lower() == WETH_4663.lower()


def test_a_stops_route_capacity_partial_carries_the_execution_quote_check(monkeypatch):
    """The b_stop route-capacity fallback sells half the bag through `derisk_fn`. That sell must
    carry the same execution-quote check as the full exit, or a phantom stop sells half the bag
    unchecked. An ordinary de-risk is not a stop, so it must not carry the check."""
    from test_memecoin_evm_exit_executor import _c, _pos
    c = _c(); pos = _pos(c, entry_notional=100)
    c.execute("UPDATE memecoin_positions SET entry_price=10 WHERE id=?", (pos["id"],))
    c.commit()
    pos = m.get_position(c, pos["id"])
    monkeypatch.setattr(m, "effective_exit_override", lambda *_: {"sl_pct": -35})
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_EVM_RELAY_ENABLED", False)
    checks = []

    def full_exit(conn, position, reason, now, **kwargs):
        return kwargs["partial_exit_fn"](0.5)         # the route-capacity fallback

    def partial_exit(conn, position, fraction, now, **kwargs):
        checks.append(kwargs.get("quote_check_fn"))
        return {"status": "deferred"}

    monkeypatch.setattr("sniper.execution.evm_exit_executor.execute_exit_evm", full_exit)
    monkeypatch.setattr("sniper.execution.evm_exit_executor.execute_partial_exit_evm", partial_exit)
    ex = seams.build_real_evm_executor(
        acct=_acct(), chain_id="4663", rpc_url="http://rpc", price_fn=lambda: 1000,
        http=lambda *_a, **_k: pytest.fail("unexpected network request"),
        usdc_fn=lambda: 1000,
    )

    ex["exit_fn"](c, pos, "b_stop", "2026-07-09T00:05:00Z")
    assert checks and checks[0] is not None, "the stop's partial sold without the quote check"
    # The wired check refuses a quote above the stop: 5 tokens for 0.045 ETH at $1000 is $9 each,
    # -10% against the $10 entry.
    allowed, _detail = checks[0](pos, {"buyAmount": str(45 * 10**15)}, 5)
    assert allowed is False

    ex["derisk_fn"](c, pos, 0.5, "2026-07-09T00:06:00Z")
    assert checks[-1] is None, "an ordinary de-risk is not a stop"


# ---- 2026-09-11 Alpha: dust check on the sell side (Gamma F1, SUIT 50977) -------------------
from sniper.settings import memecoin as cfg  # noqa: E402
RS = seams
BASE, ROBINHOOD = "8453", "4663"


def _dust_ladder(monkeypatch, relay_delay_s):
    import time
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", {BASE, ROBINHOOD}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_SELL_DUST_WAIT_S", 1.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False, raising=False)

    def dust_v3(*a, **k):                      # the drained pool answers first, always
        return {"buyAmount": "1000", "venue": "v3"}

    def relay(*a, **k):                        # the real market answers later
        time.sleep(relay_delay_s)
        return {"buyAmount": "47550400000000000", "venue": "relay"}

    return RS.venue_quote_with_v2_fallback(
        dust_v3, chain=ROBINHOOD, quote_token=W.CHAIN_QUOTE[ROBINHOOD]["token"],
        taker="0x" + "11" * 20, v2_quote_fn=lambda *a, **k: None,
        v4_quote_fn=lambda *a, **k: None, relay_quote_fn=relay, enabled=True)


def test_an_implausible_first_sell_answer_waits_for_the_best_venue(monkeypatch):
    ladder = _dust_ladder(monkeypatch, relay_delay_s=0.2)
    token = W.CHAIN_QUOTE[ROBINHOOD]["token"]
    out = ladder("0x" + "22" * 20, token, 10 ** 18, slippage_bps=300,
                 sell_plausible_fn=lambda buy_atomic: buy_atomic > 10 ** 12)
    assert out["venue"] == "relay"


def test_a_plausible_first_sell_answer_keeps_the_priority_rule(monkeypatch):
    ladder = _dust_ladder(monkeypatch, relay_delay_s=0.2)
    token = W.CHAIN_QUOTE[ROBINHOOD]["token"]
    assert ladder("0x" + "22" * 20, token, 10 ** 18, slippage_bps=300,
                  sell_plausible_fn=lambda buy_atomic: True)["venue"] == "v3"
    assert ladder("0x" + "22" * 20, token, 10 ** 18, slippage_bps=300)["venue"] == "v3"


def test_the_dust_wait_is_bounded(monkeypatch):
    import time
    ladder = _dust_ladder(monkeypatch, relay_delay_s=3.0)
    token = W.CHAIN_QUOTE[ROBINHOOD]["token"]
    started = time.monotonic()
    out = ladder("0x" + "22" * 20, token, 10 ** 18, slippage_bps=300,
                 sell_plausible_fn=lambda buy_atomic: False)
    assert out["venue"] == "v3", "nothing better arrived inside the cap"
    assert time.monotonic() - started < 2.5


# ---- 2026-09-11 Alpha: one deadline for the whole venue ladder -----------------------------

def _hung_ladder(monkeypatch, measured):
    import time
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", {BASE, ROBINHOOD}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LADDER_WAIT_S", 0.6, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", measured, raising=False)

    def hung_v3(*a, **k):                      # the 14:28Z shape: one rung never comes back
        time.sleep(4.0)
        return {"buyAmount": "999999999999999999", "venue": "v3"}

    return RS.venue_quote_with_v2_fallback(
        hung_v3, chain=ROBINHOOD, quote_token=W.CHAIN_QUOTE[ROBINHOOD]["token"],
        taker="0x" + "11" * 20,
        v2_quote_fn=(lambda *a, **k: {"buyAmount": "7", "sellAmount": "1", "venue": "v2"}) if direction_buy[0] else (lambda *a, **k: None),
        v4_quote_fn=lambda *a, **k: None,
        relay_quote_fn=lambda *a, **k: {"buyAmount": "5", "venue": "relay"}, enabled=True)


direction_buy = [False]


@pytest.mark.parametrize("measured,direction", [(False, "sell"), (False, "buy"), (True, "buy")])
def test_a_hung_rung_cannot_hold_the_ladder_past_its_deadline(monkeypatch, measured, direction):
    import time
    direction_buy[0] = direction == "buy"
    ladder = _hung_ladder(monkeypatch, measured)
    token = W.CHAIN_QUOTE[ROBINHOOD]["token"]
    coin = "0x" + "22" * 20
    started = time.monotonic()
    if direction == "sell":
        out = ladder(coin, token, 10 ** 18, slippage_bps=300)
    else:
        out = ladder(token, coin, 10 ** 15, slippage_bps=300)
    assert time.monotonic() - started < 2.0, "the runner's main loop must not wait on a hung rung"
    assert out is not None and out["venue"] == ("v2" if direction == "buy" else "relay"), \
        "an answer that arrived is used"
