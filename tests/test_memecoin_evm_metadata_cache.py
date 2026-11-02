"""Request-count regressions for immutable EVM metadata caching.

Balances and executable quotes remain live.  Only ERC-20 decimals may survive a
consumer boundary inside one armed wallet/chain context.
"""
import json

import pytest

from sniper.execution import evm_real_seams as S
from sniper.execution import evm_wallet as W
from sniper.execution.evm_token_safety import build_evm_token_safety
from sniper.execution.metadata_cache import BoundedLru
from sniper.services import memecoin_evm_leg as leg
from sniper.settings import memecoin as cfg


OWNER = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20


def _counting_rpc(*, decimals=18, balance=3 * 10**18, fail_decimals_once=False,
                  fail_decimals_first=0):
    calls = {"decimals": 0, "balanceOf": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        data = (req.get("params") or [{}])[0].get("data", "")
        if data.startswith("0x" + W.SEL_DECIMALS):
            calls["decimals"] += 1
            if fail_decimals_once and calls["decimals"] == 1:
                return 200, {"error": {"code": -32000}}, None
            if calls["decimals"] <= int(fail_decimals_first):
                return 200, {"error": {"code": -32000}}, None
            return 200, {"result": hex(decimals)}, None
        if data.startswith("0x" + W.SEL_BALANCE_OF):
            calls["balanceOf"] += 1
            return 200, {"result": hex(balance)}, None
        raise AssertionError(req)

    return http, calls


def _cached_decimals(http, *, maxsize=256):
    cache = BoundedLru(maxsize)

    def decimals_fn(token):
        def load():
            value = W.erc20_decimals(token, "https://rpc", http=http)
            return value if isinstance(value, int) and value >= 0 else None

        return cache.get_or_load(token.lower(), load)

    return cache, decimals_fn


def _real_context(monkeypatch, tmp_path, *, chain="56", decimals=18):
    path = str(tmp_path / "metadata-key.json")
    if not (tmp_path / "metadata-key.json").exists():
        W.generate_keypair_file(path)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", path)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_" + chain, "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_" + chain, "https://rpc-" + chain)
    monkeypatch.setenv("MEMECOIN_EVM_SUBMIT_MODE", "rpc")
    from sniper.execution import evm_token_safety
    monkeypatch.setattr(evm_token_safety, "warm_simulate_members", lambda *a, **k: None)
    counted, calls = _counting_rpc(decimals=decimals)
    def http(method, url, **kwargs):
        req = json.loads(kwargs["body"])
        if req["method"] == "eth_chainId":
            return 200, {"result": hex(int(chain))}, None
        return counted(method, url, **kwargs)
    context = leg.build_real_evm_context(chain, http=http)
    assert context is not None
    return context, calls


@pytest.mark.parametrize("decimals", [0, 18, 36, 255])
def test_checked_source_decimals_skip_receiver_metadata_but_keep_balances_live(
        monkeypatch, tmp_path, decimals):
    context, calls = _real_context(monkeypatch, tmp_path)
    context["put_token_decimals_fn"](TOKEN, decimals)
    assert context["decimals_fn"](TOKEN.upper()) == decimals
    assert context["token_fn"](TOKEN) == pytest.approx(3 * 10 ** (18 - decimals))
    assert context["token_fn"](TOKEN) == pytest.approx(3 * 10 ** (18 - decimals))
    assert calls == {"decimals": 0, "balanceOf": 2}


@pytest.mark.parametrize("decimals", [None, False, True, 18.0, "18", -1, 256])
def test_unknown_or_malformed_source_decimals_still_read_the_token(
        monkeypatch, tmp_path, decimals):
    context, calls = _real_context(monkeypatch, tmp_path)
    context["put_token_decimals_fn"](TOKEN, decimals)
    assert context["decimals_fn"](TOKEN) == 18
    assert calls == {"decimals": 1, "balanceOf": 0}


@pytest.mark.parametrize("mint", [None, "0xtok", "0x" + "22" * 19, "0x" + "gg" * 20])
def test_source_decimals_require_an_exact_erc20_address(monkeypatch, tmp_path, mint):
    context, calls = _real_context(monkeypatch, tmp_path)
    context["put_token_decimals_fn"](mint, 0)
    assert context["decimals_fn"](TOKEN) == 18
    assert calls == {"decimals": 1, "balanceOf": 0}


def test_source_decimals_do_not_overwrite_existing_chain_metadata(monkeypatch, tmp_path):
    context, calls = _real_context(monkeypatch, tmp_path)
    context["put_token_facts_fn"](TOKEN, (0, 123))
    context["put_token_decimals_fn"](TOKEN, 18)
    assert context["decimals_fn"](TOKEN) == 0
    assert calls == {"decimals": 0, "balanceOf": 0}


def test_source_decimals_stay_inside_the_original_chain_context(monkeypatch, tmp_path):
    bsc_context, bsc_calls = _real_context(monkeypatch, tmp_path)
    base_context, base_calls = _real_context(monkeypatch, tmp_path, chain="8453")
    bsc_context["put_token_decimals_fn"](TOKEN, 8)
    assert bsc_context["decimals_fn"](TOKEN) == 8
    assert base_context["decimals_fn"](TOKEN) == 18
    assert bsc_calls == {"decimals": 0, "balanceOf": 0}
    assert base_calls == {"decimals": 1, "balanceOf": 0}


def test_repeated_balance_reads_load_decimals_once_but_balance_each_time():
    http, calls = _counting_rpc()
    _cache, decimals_fn = _cached_decimals(http)

    assert W.erc20_balance(OWNER, TOKEN, "https://rpc", http=http,
                           decimals_fn=decimals_fn) == pytest.approx(3.0)
    assert W.erc20_balance(OWNER, TOKEN.upper(), "https://rpc", http=http,
                           decimals_fn=decimals_fn) == pytest.approx(3.0)

    assert calls == {"decimals": 1, "balanceOf": 2}


def test_quote_token_balance_uses_chain_quote_decimals_without_rpc_metadata():
    """The decimals come from CHAIN_QUOTE, never from the token's own `decimals()`.

    Arc (5042), not 8453 or 1: Base's quote token became 18-decimal WETH on 2026-09-06 and
    Ethereum's on 2026-09-30, so neither exercises the 6-decimal case this test is about.
    """
    http, calls = _counting_rpc(decimals=999, balance=25_000_000)

    assert W.quote_balance_usd(OWNER, "5042", "https://rpc", http=http) == 25.0
    assert W.quote_balance_usd(OWNER, "5042", "https://rpc", http=http) == 25.0

    assert calls == {"decimals": 0, "balanceOf": 2}


def test_failed_decimals_lookup_is_not_cached():
    """A GENUINE failure must not be remembered as an answer.

    `fail_decimals_first=2` rather than `fail_decimals_once` since 2026-08-30: `erc20_decimals`
    now retries once, so failing a single call no longer produces a failed LOOKUP — it produces a
    successful one that cost two requests. Failing one call here would leave this test asserting
    the retry rather than the cache. Two failures make the first lookup genuinely fail, which is
    the state whose non-caching this test exists to pin.
    """
    http, calls = _counting_rpc(fail_decimals_first=2)
    _cache, decimals_fn = _cached_decimals(http)

    assert W.erc20_balance(OWNER, TOKEN, "https://rpc", http=http,
                           decimals_fn=decimals_fn) == 0.0
    assert W.erc20_balance(OWNER, TOKEN, "https://rpc", http=http,
                           decimals_fn=decimals_fn) == pytest.approx(3.0)

    # 2 failed attempts on the first lookup + 1 successful on the second. balanceOf runs only
    # once, because an unreadable decimals short-circuits before the balance call.
    assert calls == {"decimals": 3, "balanceOf": 1}


def test_safety_and_executor_share_one_memecoin_decimals_lookup():
    calls = {"decimals": 0, "balanceOf": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        if method == "GET":
            return 200, {"liquidityAvailable": False, "transaction": None}, None
        req = json.loads(body)
        if req["method"] == "eth_getCode":
            return 200, {"result": "0x6000"}, None
        data = (req.get("params") or [{}])[0].get("data", "")
        if data.startswith("0x" + W.SEL_DECIMALS):
            calls["decimals"] += 1
            return 200, {"result": hex(18)}, None
        if data.startswith("0x" + W.SEL_BALANCE_OF):
            calls["balanceOf"] += 1
            return 200, {"result": hex(2 * 10**18)}, None
        raise AssertionError(req)

    _cache, decimals_fn = _cached_decimals(http)
    safety = build_evm_token_safety(
        chain_id="8453", rpc_url="https://rpc", taker=OWNER, http=http,
        decimals_fn=decimals_fn, quote_fn=lambda *_a, **_k: None,
    )
    executor = S.build_real_evm_executor(
        acct=type("Account", (), {"address": OWNER})(), chain_id="8453",
        rpc_url="https://rpc", http=http, usdc_fn=lambda: 100.0,
        decimals_fn=decimals_fn,
    )

    assert safety(TOKEN)[0] is False  # fixture has no route; decimals was still required
    assert executor["token_fn"](TOKEN) == pytest.approx(2.0)
    assert executor["token_fn"](TOKEN) == pytest.approx(2.0)
    assert calls == {"decimals": 1, "balanceOf": 2}


def test_bounded_lru_evicts_least_recently_used():
    cache = BoundedLru(2)
    loads = {"a": 0, "b": 0, "c": 0}

    def load(key):
        loads[key] += 1
        return key.upper()

    assert cache.get_or_load("a", lambda: load("a")) == "A"
    assert cache.get_or_load("b", lambda: load("b")) == "B"
    assert cache.get_or_load("a", lambda: load("a")) == "A"  # A is now most recent
    assert cache.get_or_load("c", lambda: load("c")) == "C"  # evicts B
    assert len(cache) == 2
    assert cache.get_or_load("b", lambda: load("b")) == "B"
    assert loads == {"a": 1, "b": 2, "c": 1}
    assert len(cache) == 2


def test_bounded_lru_does_not_cache_none_or_loader_failure():
    cache = BoundedLru(2)
    calls = {"n": 0}

    def none_then_value():
        calls["n"] += 1
        return None if calls["n"] == 1 else 7

    assert cache.get_or_load("x", none_then_value) is None
    assert cache.get_or_load("x", none_then_value) == 7
    assert calls["n"] == 2

    with pytest.raises(RuntimeError):
        cache.get_or_load("y", lambda: (_ for _ in ()).throw(RuntimeError("rpc")))
    assert cache.get_or_load("y", lambda: 9) == 9
