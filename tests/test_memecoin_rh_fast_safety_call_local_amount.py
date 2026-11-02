"""Each fast-lane safety call binds its OWN sell amount to its bridge check (2026-10-08 review EVM06).

routed_safety shared one `sold` dict per long-lived safety function: route_quote wrote sold[mint],
routed_safety popped it after the check and popped it again in `finally`. With a same-coin read
still running (abandoned or overrun) beside the busy-fallback check, call A popped B's amount and
B then popped None and raised BridgeLegFailed for a sound route. The amount now travels with the
call: token_safety_fn(probe_out=...) records the sell probe's amount in the caller's own dict.
"""
import threading
from unittest.mock import patch

import pytest

from sniper.execution.evm_token_safety import build_evm_token_safety
from sniper.services import memecoin_rh_fast_lane as R
from test_memecoin_evm_token_safety import TAKER, _standard_erc20_http, _standard_quote

MINT, OTHER = "0x" + "ab" * 20, "0x" + "cd" * 20
BRIDGE = {"kind": "v4_bridge", "key": {}, "pool": "0x" + "01" * 32, "bridge": "0x" + "ef" * 20,
          "a_key": {}}


class _Times:
    def __init__(self, on_note=None):
        self.rows, self.on_note = [], on_note

    def note(self, mint, kind, path, wall_ms, outcome):
        self.rows.append((threading.current_thread().name, mint, outcome))
        if self.on_note:
            self.on_note()


def _race(amounts, *, raise_in=None, mints=(MINT, MINT)):
    """Call A starts, records its amount and waits; call B records its own and waits for A to end."""
    a_in, b_in, a_done = threading.Event(), threading.Event(), threading.Event()
    seen, results = [], {}

    def check(mint, usd=None, probe_out=None, **_kw):
        name = threading.current_thread().name
        probe_out["amount"] = amounts[name]
        if name == "A":
            a_in.set()
            assert b_in.wait(2)
            if raise_in == "A":
                raise TimeoutError("abandoned read")
        else:
            assert a_in.wait(2)
            b_in.set()
            assert a_done.wait(2)
        return True, "safe"

    def bridge(entry, mint, amount, host, **_kw):
        seen.append((threading.current_thread().name, amount))
        return bool(amount)
    times = _Times(on_note=lambda: a_done.set() if threading.current_thread().name == "A" else None)
    safety = R.routed_safety(check, {m: BRIDGE for m in mints}, "offline", None, path="inline",
                             times=times)

    def run(mint):
        try:
            results[threading.current_thread().name] = safety(mint)
        except Exception as exc:
            results[threading.current_thread().name] = type(exc).__name__
    with patch.object(R, "bridge_leg_ok", bridge):
        threads = [threading.Thread(target=run, args=(m,), name=n) for n, m in zip("AB", mints)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
    return dict(seen), results


def test_overlapping_same_coin_checks_each_see_their_own_amount():
    seen, results = _race({"A": 100, "B": 200})
    assert seen == {"A": 100, "B": 200}, seen
    assert results == {"A": (True, "safe"), "B": (True, "safe")}, results


def test_equal_amounts_survive_the_other_calls_cleanup():
    seen, results = _race({"A": 100, "B": 100})
    assert seen == {"A": 100, "B": 100} and results["B"] == (True, "safe"), (seen, results)


def test_a_raising_call_touches_only_its_own_amount():
    seen, results = _race({"A": 100, "B": 200}, raise_in="A")
    assert results["A"] == "TimeoutError" and seen == {"B": 200} and results["B"] == (True, "safe")


def test_different_coins_stay_isolated():
    seen, results = _race({"A": 100, "B": 200}, mints=(MINT, OTHER))
    assert seen == {"A": 100, "B": 200}


def test_two_concurrent_token_safety_calls_each_report_their_own_probe_amount():
    """The seam the lane uses: the sell probe's amount lands in the CALLER's dict, from the safety's
    own read thread, for each of two calls running at once."""
    mint = "0x" + "33" * 20
    meet = threading.Barrier(2, timeout=5)
    standard = _standard_quote(mint)

    def quote(sell, buy, amount, **kw):
        if str(sell).lower() == mint:
            meet.wait()                             # both sell probes in flight together
        return standard(sell, buy, amount, **kw)
    safety = build_evm_token_safety(chain_id="4663", rpc_url="https://rpc", taker=TAKER,
                                    http=_standard_erc20_http(mint), quote_fn=quote)
    outs, verdicts = [{}, {}], [None, None]

    def call(i):
        verdicts[i] = safety(mint, probe_out=outs[i])
    threads = [threading.Thread(target=call, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert verdicts == [(True, "ok"), (True, "ok")], verdicts
    assert outs[0].get("amount") and outs[0] == outs[1] and outs[0] is not outs[1]


# --- through the lane: the busy fallback beside an overrunning read (acceptance 6) -----------------
from test_memecoin_rh_parallel_safety import dedicated, knob  # noqa: E402,F401
from test_memecoin_rh_fast_entry import MINT as LANE_MINT, _go, _payload, _stats  # noqa: E402


def test_the_busy_fallback_beside_an_overrunning_read_keeps_its_own_amount(dedicated, knob):
    """The first miss's read overruns and keeps running; the next miss is checked sequentially on the
    lane thread (busy). The overrunning read ends WHILE the busy check is in its safety call, which is
    when the shared `sold` let it take or clear the busy check's amount."""
    import time as _time
    built, sender, net, ledger = dedicated
    live = built["live"]
    release, seen = threading.Event(), []
    mint = LANE_MINT.lower()

    def check(m, usd=None, probe_out=None, **_kw):
        if threading.current_thread().name.startswith("fast-lane-safety"):
            probe_out["amount"] = 111                # the first, overrunning read
            release.wait(5.0)
            return True, None
        probe_out["amount"] = 222                    # the busy check, on the lane thread
        release.set()
        live._safety_future.result(timeout=5.0)      # the overrunning read ends inside this call
        return True, None

    def bridge(entry, m, amount, host, **_kw):
        seen.append((threading.current_thread().name, amount))
        return bool(amount)
    live.inline_safety_fn = R.routed_safety(check, {mint: BRIDGE}, "offline", None, path="inline",
                                            times=_Times())
    live.inline_facts_fn, live.stats_fn = (lambda m: (18, 10 ** 27)), (lambda m: _stats())
    live.inline_deadline_s = 0.2
    with patch.object(R, "bridge_leg_ok", bridge):
        first = _go(built, _payload())
        assert first["reason"] == "inline_unsafe:safety_deadline_exceeded", first
        for store in (built["cache"]._facts, built["cache"]._stats):
            store.pop(LANE_MINT, None)
        p = _payload()
        p["snapshot"] = dict(p["snapshot"], read_at=_time.time() + 2.0)
        again = _go(built, p)
    assert again["parallel_safety"] == "busy", again
    lane_thread = threading.current_thread().name
    assert (lane_thread, 222) in seen, seen
    assert again["safety_path"] == "inline" and "bridge" not in again["reason"], again
