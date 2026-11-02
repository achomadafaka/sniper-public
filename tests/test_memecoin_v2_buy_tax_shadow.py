"""V2 buy tax in shadow, on a RESEARCH host after the attempt (entry-safety spec, section 2).

Rebuilt after Gamma's HOLD on 6578cf227: the first build fired the funded quote ladder, the price
seam and eth_simulateV1 on the funded read pool at the moment the entry went on to quote, sign and
read its receipt there. Standing lesson (Alpha): a shadow leg never calls a funded seam. This one
starts a daemon thread at the safety PASS (no I/O on the fill path), waits until the attempt is
long over, and reads ONLY the chain's research host: getAmountsOut for the quote, one
eth_simulateV1 for what a buy delivers. It logs; it never gates.
"""
import json
import logging

import pytest

from sniper.services import memecoin_v2_buy_tax_shadow as shadow

WETH_4663 = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
MINT = "0x6535b6f6e925801a820d9e403e118ebe2ea9dc14"
RESEARCH = "https://research.example/k/"


def _post_fake(received, *, simulate="ok", seen=None):
    def post(url, method, params):
        if seen is not None:
            seen.append((url, method))
        if method == "eth_call":                                  # getAmountsOut
            assert params[0]["data"].startswith("0xd06ca61f")
            return {"result": "0x" + "%064x" % 32 + "%064x" % 2 + "%064x" % 10 ** 16 + "%064x" % 1000}
        if method == "eth_simulateV1":
            if simulate == "unsupported":
                return {"error": {"code": -32601, "message": "method not found"}}
            calls = params[0]["blockStateCalls"][0]["calls"]
            rows = [{"status": "0x1", "returnData": "0x"} for _ in calls]
            rows[0]["returnData"] = "0x" + "%064x" % 0                # mint before the buy
            rows[-1]["returnData"] = "0x" + "%064x" % received        # mint after the buy
            if simulate == "reverts":
                rows[-2]["status"] = "0x0"
            return {"result": [{"calls": rows}]}
        raise AssertionError(method)
    return post


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if "evm.buy_tax_shadow" in r.getMessage()]


def test_a_taxed_buy_logs_its_tax_from_the_research_host_only(caplog, monkeypatch):
    monkeypatch.setattr(shadow, "research_url", lambda chain: RESEARCH)
    seen, slept = [], []
    with caplog.at_level(logging.WARNING):
        shadow.measure("4663", [MINT, WETH_4663], post=_post_fake(800, seen=seen), sleep=slept.append)
    [line] = _lines(caplog)
    assert "chain=4663" in line and "tax_bps=2000.0" in line and "quoted=1000" in line, line
    assert "received=800" in line and "host=research.example" in line and "why=ok" in line, line
    assert {url for url, _m in seen} == {RESEARCH}
    assert [m for _u, m in seen] == ["eth_call", "eth_simulateV1"]
    assert slept == [shadow.DELAY_S]                       # after the attempt, never during it


@pytest.mark.parametrize("mode, why", [("unsupported", "simulate_unsupported"),
                                       ("reverts", "buy_reverted")])
def test_an_unmeasurable_buy_logs_unknown_never_zero(mode, why, caplog, monkeypatch):
    monkeypatch.setattr(shadow, "research_url", lambda chain: RESEARCH)
    with caplog.at_level(logging.WARNING):
        shadow.measure("4663", [MINT, WETH_4663], post=_post_fake(800, simulate=mode),
                       sleep=lambda s: None)
    [line] = _lines(caplog)
    assert "tax_bps=unknown" in line and "why=%s" % why in line, line


def test_a_chain_without_a_research_host_schedules_nothing(monkeypatch):
    monkeypatch.setattr(shadow, "research_url", lambda chain: None)
    assert shadow.schedule("56", [MINT, "0xwbnb"]) is None


def test_a_path_that_does_not_end_in_the_chain_quote_token_is_skipped(caplog, monkeypatch):
    monkeypatch.setattr(shadow, "research_url", lambda chain: RESEARCH)
    with caplog.at_level(logging.WARNING):
        shadow.measure("4663", [MINT, "0x" + "11" * 20], post=_post_fake(800), sleep=lambda s: None)
    [line] = _lines(caplog)
    assert "why=not_a_quote_token_path" in line, line


def _safety_fakes():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "_safety_fakes", Path(__file__).with_name("test_memecoin_evm_token_safety.py"))
    fakes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fakes)
    return fakes


def test_the_safety_pass_schedules_the_shadow_and_touches_nothing_funded(monkeypatch):
    """The trigger: a PASS on a uniswap_v2 sell route starts it with the route's own path; the
    funded read path sees no simulation and no extra call because of it."""
    from sniper.settings import memecoin as cfg
    fakes = _safety_fakes()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_BUY_TAX_GATE", "shadow")
    scheduled = []
    monkeypatch.setattr(shadow, "schedule",
                        lambda chain, path, router=None: scheduled.append((chain, path, router)))
    route = dict(fakes._V2_ROUTE, tokens=["0xtok", "0x4200000000000000000000000000000000000006"])
    methods = []
    inner = fakes._router_reverts_http(router_ok=True)

    def http(method, url, headers=None, body=None, timeout=10):
        methods.append(json.loads(body)["method"])
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    ok, reason = fakes._safety_for(http, route)("0xtok")
    assert ok and reason == "ok", reason
    # Gamma LOW: the sale's own router, never the configured one (a fork router is another factory)
    assert scheduled == [("8453", ["0xtok", "0x4200000000000000000000000000000000000006"],
                          route["router"])]
    assert "eth_simulateV1" not in methods


def test_no_shadow_on_a_non_v2_route(monkeypatch):
    from sniper.settings import memecoin as cfg
    fakes = _safety_fakes()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_BUY_TAX_GATE", "shadow")
    scheduled = []
    monkeypatch.setattr(shadow, "schedule",
                        lambda chain, path, router=None: scheduled.append(chain))
    ok, _ = fakes._safety_for(fakes._router_reverts_http(router_ok=True), fakes._V4_ROUTE)("0xtok")
    assert ok and scheduled == []


def test_the_knob_off_schedules_nothing(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_BUY_TAX_GATE", "off")
    scheduled = []
    monkeypatch.setattr(shadow, "schedule",
                        lambda chain, path, router=None: scheduled.append(chain))
    from sniper.execution.evm_token_safety import _maybe_schedule_buy_tax_shadow
    _maybe_schedule_buy_tax_shadow("4663", {"route": {"protocol": "uniswap_v2", "tokens": [MINT, WETH_4663]}})
    assert scheduled == []


ROUTE_ROUTER = "0x" + "77" * 20


def test_the_buy_quotes_on_the_sale_routes_own_router(caplog, monkeypatch):
    monkeypatch.setattr(shadow, "research_url", lambda chain: RESEARCH)
    seen = []

    def post(url, method, params):
        seen.append((method, params[0].get("to") if method == "eth_call" else None))
        return _post_fake(800)(url, method, params)
    with caplog.at_level(logging.WARNING):
        shadow.measure("4663", [MINT, WETH_4663], router=ROUTE_ROUTER, post=post,
                       sleep=lambda s: None)
    assert ("eth_call", ROUTE_ROUTER) in seen


def test_4663_reads_only_the_paced_drpc_research_key_never_the_safety_caches_quicknode_key(
        monkeypatch):
    """Gamma HOLD on 15d338664: MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663 is the RH fast lane safety
    cache's key (MEMECOIN_RH_FAST_SAFETY_RPC_URL falls back to it); a shadow burst there can pause
    the cache 120 s and send 4663 funded entries to the fallback."""
    monkeypatch.setenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "https://qn.example/k/")
    monkeypatch.setenv("MEMECOIN_RH_FAST_SAFETY_RPC_URL", "https://qn.example/k/")
    monkeypatch.delenv("MEMECOIN_RESEARCH_DRPC_RPC_URL_4663", raising=False)
    monkeypatch.setenv("MEMECOIN_V2_BUY_TAX_RESEARCH_RPC_4663", "https://qn.example/k/")
    assert shadow.research_url("4663") is None                 # no dRPC key: no shadow on 4663
    monkeypatch.setenv("MEMECOIN_RESEARCH_DRPC_RPC_URL_4663", "https://lb.drpc.org/x?dkey=k")
    assert shadow.research_url("4663") == "https://lb.drpc.org/x?dkey=k"


def test_the_default_post_goes_through_the_drpc_accounts_shared_pacer(caplog, monkeypatch):
    from sniper.services import memecoin_research_pacer as pacer
    monkeypatch.setattr(shadow, "research_url", lambda chain: RESEARCH)
    monkeypatch.setattr(pacer, "drpc_paused_s", lambda url: 0.0)
    paced = []

    def wrap(fetch):
        def go(url, **kw):
            paced.append(kw["method"])
            return fetch(url, **kw)
        return go
    monkeypatch.setattr(pacer, "drpc_paced_json", wrap)
    raw = _post_fake(800)
    monkeypatch.setattr(shadow, "_post", lambda url, method, params: raw(url, method, params))
    with caplog.at_level(logging.WARNING):
        shadow.measure("4663", [MINT, WETH_4663], sleep=lambda s: None)
    assert paced == ["eth_call", "eth_simulateV1"]
    [line] = _lines(caplog)
    assert "why=ok" in line, line


def test_a_paused_research_account_skips_the_measurement(caplog, monkeypatch):
    from sniper.services import memecoin_research_pacer as pacer
    monkeypatch.setattr(shadow, "research_url", lambda chain: RESEARCH)
    monkeypatch.setattr(pacer, "drpc_paused_s", lambda url: 42.0)
    monkeypatch.setattr(shadow, "_post", lambda *a: pytest.fail("posted while the account is paused"))
    with caplog.at_level(logging.WARNING):
        shadow.measure("4663", [MINT, WETH_4663], sleep=lambda s: None)
    [line] = _lines(caplog)
    assert "tax_bps=unknown" in line and "why=research_paused" in line, line
