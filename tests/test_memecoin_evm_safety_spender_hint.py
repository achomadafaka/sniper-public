"""BSC cut 3 (Alpha, 2026-10-14): the slot probes start beside the sell quote when the spender is
known before the quote returns.

After the 56 slot-lead fix (056a9653f) the 56 lane's inline safety was still serial: code and the
sell quote together (the quote is a decimals read plus one batched getAmountsOut), then the slot
probes for the quote's spender (one round trip each, in parallel), then the funded simulation. The
lane's V2 exit quote always names the configured Router02 as its spender, so that spender is a
hint: with it, the balance and allowance probes run beside the quote, and the simulation starts as
soon as the quote lands. The simulation itself still needs the quote's own calldata.

Nothing about the verdict moves: the same probes prove the same slots, a quote naming another
spender is probed for that spender exactly as today, and a probe failure surfaces the same reason.
"""
import json
import threading

import pytest

from sniper.execution.evm_token_safety import build_evm_token_safety
from test_memecoin_evm_token_safety import ROUTER, _standard_erc20_http, _standard_quote, TAKER

MINT = "0x" + "ab" * 18 + "7777"
OTHER = "0x" + "5a" * 20


def _is_probe(body):
    req = json.loads(body)
    params = req.get("params") or []
    if req["method"] != "eth_call" or len(params) < 3 or not isinstance(params[0], dict):
        return False
    return (params[0].get("data") or "")[:10] in ("0x70a08231", "0xdd62ed3e")


def _safety(http, quote, hint):
    return build_evm_token_safety(chain_id="56", rpc_url="https://rpc", taker=TAKER, http=http,
                                  quote_fn=quote, **({"sell_spender_hint": hint} if hint else {}))


@pytest.mark.parametrize("hint, overlaps", [(ROUTER, True), (None, False)])
def test_the_slot_probes_start_beside_the_quote_only_with_a_spender_hint(hint, overlaps):
    inner = _standard_erc20_http(MINT, balance_slot=51, allowance_slot=52)
    probe_started = threading.Event()
    seen = {}

    def http(method, url, headers=None, body=None, timeout=10):
        if _is_probe(body):
            probe_started.set()
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    base = _standard_quote(MINT)

    def quote(*args, **kwargs):
        seen.setdefault("probe_before_quote_returned", probe_started.wait(0.3))
        return base(*args, **kwargs)
    ok, reason = _safety(http, quote, hint)(MINT)
    assert ok, reason
    assert seen["probe_before_quote_returned"] is overlaps


def _quote_naming(spender):
    base = _standard_quote(MINT)

    def quote(*args, **kwargs):
        got = base(*args, **kwargs)
        got["issues"] = {"allowance": {"spender": spender}}
        got["transaction"] = dict(got["transaction"], to=spender)
        return got
    return quote


@pytest.mark.parametrize("case", ["sellable", "honeypot", "other_spender", "not_a_contract",
                                  "probe_transport_error"])
def test_the_verdict_is_the_same_with_and_without_the_hint(case):
    spender = OTHER if case == "other_spender" else ROUTER

    def make_http():
        inner = _standard_erc20_http(MINT, balance_slot=51, allowance_slot=52,
                                     allowance_spender=spender, sell_blocked=case == "honeypot")

        def http(method, url, headers=None, body=None, timeout=10):
            if case == "not_a_contract" and json.loads(body)["method"] == "eth_getCode":
                return 200, {"result": "0x"}, None
            if case == "probe_transport_error" and _is_probe(body):
                raise OSError("connection reset")
            return inner(method, url, headers=headers, body=body, timeout=timeout)
        return http
    plain = _safety(make_http(), _quote_naming(spender), None)(MINT)
    hinted = _safety(make_http(), _quote_naming(spender), ROUTER)(MINT)
    assert hinted == plain
    assert plain[0] is (case in ("sellable", "other_spender")), plain


def test_a_wrong_hint_probes_the_quotes_own_spender():
    inner = _standard_erc20_http(MINT, balance_slot=51, allowance_slot=52, allowance_spender=OTHER)
    probed = []
    hint_probed = threading.Event()

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        if _is_probe(body) and req["params"][0]["data"].startswith("0xdd62ed3e"):
            probed.append(req["params"][0]["data"][-40:])
            if probed[-1] == ROUTER[2:].lower():
                hint_probed.set()
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    ok, reason = _safety(http, _quote_naming(OTHER), ROUTER)(MINT)
    assert ok, reason
    assert hint_probed.wait(1)  # unused hint may finish after the required own-spender simulation
    assert OTHER[2:] in probed and ROUTER[2:].lower() in probed


def test_the_bsc_lane_hands_its_router_as_the_hint(monkeypatch):
    from sniper.execution import evm_token_safety as S
    from sniper.services import memecoin_bsc_fast_lane as lane
    from sniper.settings import memecoin as cfg
    captured = {}
    monkeypatch.setattr(S, "build_evm_token_safety",
                        lambda **kw: captured.update(kw) or (lambda *a, **k: (True, "ok")))
    check = lane.lane_safety_fn("https://lane", None, TAKER, {})
    check.for_quote(MINT, "0x" + "bb" * 20)
    assert captured["sell_spender_hint"] == cfg.memecoin_evm_v2_exit_config("56")["router"]


def test_the_simulation_waits_for_the_hinted_discovery_instead_of_probing_twice():
    import time

    def run(hint):
        inner = _standard_erc20_http(MINT, balance_slot=51, allowance_slot=52)
        probes = []

        def http(method, url, headers=None, body=None, timeout=10):
            if _is_probe(body):
                probes.append(1)
                time.sleep(0.05)          # the quote lands while the hinted probes are in flight
            return inner(method, url, headers=headers, body=body, timeout=timeout)
        ok, reason = _safety(http, _standard_quote(MINT), hint)(MINT)
        assert ok, reason
        return len(probes)
    assert run(ROUTER) == run(None) == 2      # one balance and one allowance probe, never repeated


# --- Gamma LOWs on c693e19fd ---------------------------------------------------------------------

def test_a_permit2_hint_starts_no_early_discovery():
    """Permit2's branch caches a balance-only override when an existing allowance covers the probe
    amount; an amount-1 prewarm would cache that for a larger real sell. No prewarm for Permit2."""
    from test_memecoin_evm_token_safety import PERMIT2
    inner = _standard_erc20_http(MINT, balance_slot=51, allowance_slot=52)
    probe_started = threading.Event()
    seen = {}

    def http(method, url, headers=None, body=None, timeout=10):
        if _is_probe(body):
            probe_started.set()
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    base = _standard_quote(MINT)

    def quote(*args, **kwargs):
        seen.setdefault("early", probe_started.wait(0.2))
        return base(*args, **kwargs)
    _safety(http, quote, PERMIT2)(MINT)
    assert seen["early"] is False


def test_a_prewarm_transport_failure_is_discovered_again_at_the_simulation(monkeypatch):
    """The early discovery exhausts its retries (4 a mapping) on a blipping host; the simulation
    then runs today's discovery once more instead of refusing on the early attempt (Gamma LOW)."""
    import time as clock
    monkeypatch.setattr(clock, "sleep", lambda _s: None)           # _read_with_retry's backoff
    inner = _standard_erc20_http(MINT, balance_slot=51, allowance_slot=52)
    failures = {"left": 8}                       # balance x4 + allowance x4: the prewarm's attempts
    lock = threading.Lock()
    quoted = threading.Event()

    def http(method, url, headers=None, body=None, timeout=10):
        if _is_probe(body):
            with lock:
                fail = failures["left"] > 0
                failures["left"] -= fail
            if fail:
                raise OSError("connection reset")
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    base = _standard_quote(MINT)

    def quote(*args, **kwargs):
        end = clock.monotonic() + 2.0            # land the quote after the prewarm has failed
        while failures["left"] > 0 and clock.monotonic() < end:
            pass
        quoted.set()
        return base(*args, **kwargs)
    ok, reason = _safety(http, quote, ROUTER)(MINT)
    assert ok, reason


@pytest.mark.parametrize("family", ["closure", "bsc_fresh_jobs"])
def test_a_known_refusal_does_not_join_unused_prewarm_and_burst_is_bounded(family, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    inner = _standard_erc20_http(MINT, balance_slot=51, allowance_slot=52)
    started, release = threading.Event(), threading.Event()
    probes, lock = [], threading.Lock()

    def http(method, url, headers=None, body=None, timeout=10):
        if _is_probe(body):
            with lock:
                probes.append(1)
            started.set()
            assert release.wait(3), "test did not release the bounded fake provider"
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    def no_route(*args, **kwargs):
        assert started.wait(1)
        return None

    if family == "closure":
        check = _safety(http, no_route, ROUTER)
    else:
        from sniper.execution import evm_v2_exit_quote
        from sniper.services import memecoin_bsc_fast_lane as lane
        monkeypatch.setattr(evm_v2_exit_quote, "dex_v2_exit_quote", no_route)
        factory = lane.lane_safety_fn("https://rpc", http, TAKER, {})
        def check(mint, **kwargs):
            # Preparation jobs bind their own provider seam and therefore create fresh closures.
            return factory.for_quote(mint, lane.WBNB, request_http=http)(mint, **kwargs)
    with ThreadPoolExecutor(max_workers=4) as callers:
        first_timings = {}
        first = callers.submit(check, MINT, timings=first_timings)
        try:
            assert started.wait(1)
            # A route refusal is already known while its speculative slot reads are pending.
            result = first.result(timeout=0.5)
            assert result[0] is False and "route" in result[1]
            published_timings = dict(first_timings)
            burst = [callers.submit(check, MINT) for _ in range(8)]
            assert all(f.result(timeout=0.5)[0] is False for f in burst)
            assert len(probes) <= 2, "refusals accumulated speculative provider threads"
        finally:
            release.set()
    for worker in threading.enumerate():
        if worker.name.startswith("evm-safety-prewarm"):
            worker.join(1)
            assert not worker.is_alive()
    assert first_timings == published_timings, "unused completion mutated published timing evidence"
