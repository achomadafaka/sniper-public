"""Production Flap safety shares only a fresh, native BNB price; no network or signing."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from sniper.execution import evm_flap_curve as curve
from sniper.execution import evm_real_seams as seams
from sniper.execution import evm_wallet as wallet
from sniper.services import memecoin_evm_leg as leg
from sniper.settings import memecoin as cfg
from test_memecoin_evm_flap_curve_entry_safety import _state

MINT = "0x8d411a22bf0194b2f5ef665f8a2c7a9430ee7777"
BNB_USD = 500.0
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
_REAL_EXECUTOR = seams.build_real_evm_executor


def _context(monkeypatch, tmp_path, *, peek="actual", quote_token=None):
    calls = {"prices": [], "amounts": [], "state": [], "sell": []}
    state = _state()

    def price(chain, http=None):
        calls["prices"].append(str(chain))
        return BNB_USD

    def state_fn(chain, mint, rpc, *, http=None):
        calls["state"].append((chain, mint))
        return dict(state)

    def simulate(portal, source, destination, amount, taker, rpc, *, http=None, overrides=None):
        if source == curve._NATIVE:
            calls["amounts"].append(amount)
            return curve.curve_buy_out(state, amount)
        calls["sell"].append((amount, overrides is not None))
        return curve.curve_sell_out(state, amount) if overrides is not None else None

    def executor(**kwargs):
        ex = _REAL_EXECUTOR(**kwargs)
        if peek != "actual":
            ex["quote_price_peek_fn"] = peek
        if quote_token is not None:
            ex["quote_token"] = quote_token
        return ex

    def http(method, url, **kwargs):
        request = json.loads(kwargs["body"])
        return 200, {"result": "0x38" if request["method"] == "eth_chainId" else "0x"}, None

    keypair = tmp_path / "local-only-key.json"
    wallet.generate_keypair_file(str(keypair))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(keypair))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", True)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_56", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://bsc-dataseed.bnbchain.org")
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)
    monkeypatch.setattr(wallet, "native_usd_price", price)
    monkeypatch.setattr(seams, "native_usd_price", price)
    monkeypatch.setattr(seams, "build_real_evm_executor", executor)
    monkeypatch.setattr(curve, "curve_state", state_fn)
    monkeypatch.setattr(curve, "simulate_swap_exact_input", simulate)
    monkeypatch.setattr(curve, "discover_sell_overrides", lambda *a, **kw: {"simulation": True})
    ctx = leg.build_real_evm_context("56", http=http)
    assert ctx is not None
    return ctx, calls


def test_real_context_reuses_its_live_bnb_price_for_exact_flap_safety(monkeypatch, tmp_path):
    ctx, calls = _context(monkeypatch, tmp_path)
    assert ctx["warm_quote_price_fn"]() == BNB_USD
    assert ctx["token_safety_fn"](MINT, usd=50) == (True, "flap_curve_exit_quotable_at_clip")
    assert calls["prices"] == ["56"]  # One live read shared with safety, not a second HTTP call.
    assert calls["amounts"] == [10**17]
    assert calls["state"] == [("56", MINT)]  # One fresh snapshot binds this round-trip check.
    assert calls["sell"][-1][1] is True


@pytest.mark.parametrize("age_ms", [0, 999.9, 1000])
def test_fresh_price_sizes_hypothetical_buy_in_bnb_units(monkeypatch, tmp_path, age_ms):
    ctx, calls = _context(monkeypatch, tmp_path,
                          peek=lambda: {"px": 1000.0, "age_ms": age_ms})
    assert ctx["token_safety_fn"](MINT, usd=50)[0] is True
    assert calls["amounts"] == [5 * 10**16]
    assert calls["prices"] == []


@pytest.mark.parametrize("memo", [
    None, {}, {"px": 1000}, {"px": 1000, "age_ms": -0.1},
    {"px": 1000, "age_ms": 1000.1}, {"px": 1000, "age_ms": True},
    {"px": 1000, "age_ms": "1"}, {"px": 1000, "age_ms": float("nan")},
    {"px": True, "age_ms": 1}, {"px": 0, "age_ms": 1}, {"px": -1, "age_ms": 1},
    {"px": float("nan"), "age_ms": 1}, {"px": float("inf"), "age_ms": 1},
    {"px": "1000", "age_ms": 1}, [1000, 1],
])
def test_unusable_memo_preserves_existing_live_price_fallback(monkeypatch, tmp_path, memo):
    ctx, calls = _context(monkeypatch, tmp_path, peek=lambda: memo)
    assert ctx["token_safety_fn"](MINT, usd=50)[0] is True
    assert calls["prices"] == ["56"]
    assert calls["amounts"] == [10**17]


def test_price_peek_error_preserves_existing_live_fallback(monkeypatch, tmp_path):
    def broken():
        raise RuntimeError("memo unavailable")
    ctx, calls = _context(monkeypatch, tmp_path, peek=broken)
    assert ctx["token_safety_fn"](MINT, usd=50)[0] is True
    assert calls["prices"] == ["56"]


@pytest.mark.parametrize("token", ["0x" + "11" * 20, wallet.CHAIN_USDC["56"], None])
def test_foreign_or_stable_quote_price_is_never_treated_as_bnb(monkeypatch, tmp_path, token):
    ctx, calls = _context(monkeypatch, tmp_path,
                          peek=lambda: {"px": 1.0, "age_ms": 0}, quote_token=token or "")
    assert ctx["token_safety_fn"](MINT, usd=50)[0] is True
    assert calls["prices"] == ["56"]
    assert calls["amounts"] == [10**17]


@pytest.mark.parametrize("field,value", [("kind", "usd"), ("usd_symbol", "ETH"), ("decimals", 6)])
def test_only_canonical_bnb_quote_metadata_can_share_the_memo(monkeypatch, tmp_path, field, value):
    monkeypatch.setitem(wallet.CHAIN_QUOTE, "56", dict(wallet.CHAIN_QUOTE["56"], **{field: value}))
    ctx, calls = _context(monkeypatch, tmp_path, peek=lambda: {"px": 1.0, "age_ms": 0})
    assert ctx["token_safety_fn"](MINT, usd=50)[0] is True
    assert calls["prices"] == ["56"] and calls["amounts"] == [10**17]


def test_unknown_live_fallback_still_refuses_without_an_entry_or_exit_quote(monkeypatch, tmp_path):
    ctx, calls = _context(monkeypatch, tmp_path, peek=lambda: None)
    monkeypatch.setattr(wallet, "native_usd_price", lambda *a, **kw: None)
    assert ctx["token_safety_fn"](MINT, usd=50) == (False, "flap_curve_price_unavailable")
    assert calls["amounts"] == calls["sell"] == []


def test_existing_live_price_failure_still_propagates_to_the_leg_fail_closed_boundary(monkeypatch, tmp_path):
    ctx, calls = _context(monkeypatch, tmp_path, peek=lambda: None)
    def timeout(*a, **kw):
        raise TimeoutError("bounded existing price read")
    monkeypatch.setattr(wallet, "native_usd_price", timeout)
    with pytest.raises(TimeoutError, match="bounded existing price read"):
        ctx["token_safety_fn"](MINT, usd=50)
    assert calls["amounts"] == calls["sell"] == []


def test_deadline_expired_before_price_reuse_still_refuses(monkeypatch, tmp_path):
    ctx, calls = _context(monkeypatch, tmp_path, peek=lambda: {"px": 1000, "age_ms": 0})
    assert ctx["token_safety_fn"](MINT, usd=50, deadline=0) == (False, "safety_deadline_exceeded")
    assert calls["prices"] == calls["state"] == calls["amounts"] == []


def test_concurrent_safety_checks_share_fresh_price_without_replacing_live_curve_reads(monkeypatch, tmp_path):
    ctx, calls = _context(monkeypatch, tmp_path)
    ctx["warm_quote_price_fn"]()
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: ctx["token_safety_fn"](MINT, usd=50), range(2)))
    assert all(result == (True, "flap_curve_exit_quotable_at_clip") for result in results)
    assert calls["prices"] == ["56"]
    assert calls["amounts"] == [10**17, 10**17]
    assert calls["state"] == [("56", MINT), ("56", MINT)]  # Each check still reads fresh state.


def test_existing_exact_bound_amount_never_reads_price(monkeypatch, tmp_path):
    def forbidden():
        raise AssertionError("an exact token amount needs no price")
    ctx, calls = _context(monkeypatch, tmp_path, peek=forbidden)
    assert ctx["token_safety_fn"](MINT, usd=50, bought_amount_atomic=10**18)[0] is True
    assert calls["prices"] == [] and calls["amounts"] == []


def test_price_lookup_has_its_own_numeric_timing(monkeypatch, tmp_path):
    ctx, _calls = _context(monkeypatch, tmp_path)
    ctx["warm_quote_price_fn"]()
    timings = {}
    assert ctx["token_safety_fn"](MINT, usd=50, timings=timings)[0] is True
    assert isinstance(timings.get("curve_price_ms"), (int, float))
    assert timings["curve_price_ms"] >= 0
