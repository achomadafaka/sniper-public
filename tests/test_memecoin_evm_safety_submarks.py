"""Display-only sub-marks inside EVM token safety (2026-09-28, Alpha GO).

On Arc (5042) the clip-size exit check runs AFTER the entry quote, by design: it sells exactly the
quoted bought amount, because a dust probe passes where a clip-sized sell is blocked (09-16). It cost
317-378 ms on the critical path on the two serial fills, and the trace could not say where. The
`evm.token_safety` substage now carries code_ms, sell_quote_ms, slot_discovery_ms and simulation_ms.
Nothing else changes: the verdict is byte-identical with or without the timings, and a safety
function that does not NAME `timings` (a **kwargs wrapper) is never handed it.
"""
import json
import time

import pytest

from sniper.execution.evm_token_safety import build_evm_token_safety
from sniper.services import memecoin_evm_leg as leg
from sniper.settings import memecoin as cfg
from test_memecoin_evm_leg import _c, _Ev
from test_memecoin_evm_token_safety import TAKER, _standard_erc20_http, _standard_quote

KEYS = {"code_ms", "sell_quote_ms", "slot_discovery_ms", "simulation_ms"}
MINT = "0x" + "42" * 20


def _safety(*, sell_blocked=False, quote_delay=0.0, sim_delay=0.0):
    inner = _standard_erc20_http(MINT, sell_blocked=sell_blocked)

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        if (sim_delay and req["method"] == "eth_call"
                and (req["params"][0].get("data") or "").startswith("0xdeadbeef")):
            time.sleep(sim_delay)                # the venue sell transaction: the simulation
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    base = _standard_quote(MINT)

    def quote(*args, **kwargs):
        time.sleep(quote_delay)
        return base(*args, **kwargs)
    return build_evm_token_safety(chain_id="5042", rpc_url="https://rpc", taker=TAKER, http=http,
                                  quote_fn=quote)


@pytest.fixture(autouse=True)
def _arc(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", {"5042"})
    # evm_real_entry refuses a chain outside the allowlist (Gamma H3, 2026-09-30); production has a
    # context only for allowlisted chains, so the test allowlists the chain it drives.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663", "5042"})


@pytest.mark.parametrize("blocked", [False, True])
def test_the_timings_never_change_the_verdict(blocked):
    plain = _safety(sell_blocked=blocked)(MINT, usd=50.0, bought_amount_atomic=10 ** 18)
    timings = {}
    timed = _safety(sell_blocked=blocked)(MINT, usd=50.0, bought_amount_atomic=10 ** 18,
                                          timings=timings)
    assert timed == plain and plain[0] is (not blocked)
    assert KEYS <= set(timings) and all(v >= 0 for v in timings.values())


def test_each_mark_times_its_own_work():
    slow_quote = {}
    _safety(quote_delay=0.08)(MINT, usd=50.0, bought_amount_atomic=10 ** 18, timings=slow_quote)
    slow_sim = {}
    _safety(sim_delay=0.08)(MINT, usd=50.0, bought_amount_atomic=10 ** 18, timings=slow_sim)
    assert slow_quote["sell_quote_ms"] >= 70 and slow_quote["simulation_ms"] < 70
    assert slow_sim["simulation_ms"] >= 70 and slow_sim["sell_quote_ms"] < 70


class _Trace:
    def __init__(self):
        self.substages = []

    def mark_substage(self, name, **kw):
        self.substages.append((name, kw))

    def mark(self, *_a, **_kw):
        pass


def _enter(monkeypatch, tmp_path, safety):
    from types import SimpleNamespace
    c, kill = _c(monkeypatch, tmp_path)
    monkeypatch.setattr(leg, "reserve_entry_evm", lambda *a, **kw:
                        (SimpleNamespace(allowed=True, approved_size_usdc=50.0), 1))

    def execute(conn, pid, mint, size, now_iso, quote_safety_fn=None, trace=None):
        assert quote_safety_fn({"buyAmount": "777"}) == (True, "ok")
        return {"status": "filled", "tx_hash": "0xh", "position_id": pid}
    trace = _Trace()
    result = leg.evm_real_entry(
        c, _Ev(), route="evm:5042", day="2026-07-09", usdc_balance=100, native_balance=100,
        now_iso="2026-07-09T00:00:00Z", trade_size=150, token_safety_fn=safety,
        execute_fn=execute, kill_path=kill, trace=trace)
    assert result["status"] == "filled"
    return [kw for name, kw in trace.substages if name == "evm.token_safety"]


def test_the_leg_puts_the_timings_on_the_token_safety_substage(monkeypatch, tmp_path):
    def safety(mint, *, usd=None, bought_amount_atomic=None, timings=None):
        timings.update(code_ms=1.0, sell_quote_ms=2.0, slot_discovery_ms=3.0, simulation_ms=4.0)
        return True, "ok"
    marks = _enter(monkeypatch, tmp_path, safety)
    assert len(marks) == 1
    assert {k: marks[0][k] for k in KEYS} == {
        "code_ms": 1.0, "sell_quote_ms": 2.0, "slot_discovery_ms": 3.0, "simulation_ms": 4.0}


def test_a_passing_safety_trace_keeps_its_admission_reason(monkeypatch, tmp_path):
    def safety(mint, *, usd=None, bought_amount_atomic=None, timings=None):
        return True, "ok"
    marks = _enter(monkeypatch, tmp_path, safety)
    assert marks[0]["outcome"] == "passed"
    assert marks[0]["reason"] == "ok"


def test_a_kwargs_wrapper_is_never_handed_timings(monkeypatch, tmp_path):
    """A wrapper that forwards **kwargs to a function without `timings` would raise, and a raised
    safety check fails closed: an entry refused for a display field."""
    def inner(mint, *, usd=None, bought_amount_atomic=None):
        return True, "ok"

    def wrapper(mint, **kwargs):
        return inner(mint, **kwargs)
    marks = _enter(monkeypatch, tmp_path, wrapper)
    assert len(marks) == 1 and not (KEYS & set(marks[0]))
