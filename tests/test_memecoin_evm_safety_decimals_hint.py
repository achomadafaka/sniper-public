"""The 56 lane's sell probe sizes one whole token, and read the coin's decimals from the lane host
first: one round trip in front of the quote on the inline safety's critical path (BSC cut 3b, Alpha
yes 2026-10-14). The signal already carries decimals the watcher read from chain, and the lane's facts
cache may hold them. Order: the facts cache's (the value the entry gate's decimals_disagree checks,
Alpha/Gamma facts-first), then the signal's, then the host read. Only an int in
0..36 is taken; anything else falls through. The source is recorded with the verdict's timings.
Decimals only size the probe; the lane still refuses `decimals_disagree` against its own facts."""
import json

import pytest

from sniper.execution.evm_token_safety import build_evm_token_safety
from test_memecoin_evm_token_safety import ROUTER, TAKER, _standard_erc20_http, _standard_quote

MINT = "0x" + "ab" * 18 + "7777"


def _run(hint):
    inner = _standard_erc20_http(MINT, balance_slot=51, allowance_slot=52)
    host_reads = []

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        params = req.get("params") or []
        if params and isinstance(params[0], dict) and (params[0].get("data") or "").startswith("0x313ce567"):
            host_reads.append(1)
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    timings = {}
    ok, reason = build_evm_token_safety(
        chain_id="56", rpc_url="https://rpc", taker=TAKER, http=http, quote_fn=_standard_quote(MINT),
        sell_spender_hint=ROUTER, **({"decimals_hint_fn": hint} if hint else {}))(MINT, timings=timings)
    assert ok, reason
    return len(host_reads), timings.get("decimals_source")


def test_the_signals_decimals_skip_the_host_read():
    assert _run(lambda mint: [("signal", 18), ("facts", 18)]) == (0, "signal")


@pytest.mark.parametrize("bad", [None, -1, 37, 99, True, "18", 18.0])
def test_bad_signal_decimals_fall_through_to_the_facts_then_the_host(bad):
    assert _run(lambda mint: [("signal", bad), ("facts", 18)]) == (0, "facts")
    assert _run(lambda mint: [("signal", bad), ("facts", None)]) == (1, "host")


def test_without_a_hint_the_host_read_is_todays():
    assert _run(None) == (1, "host")


def test_a_raising_hint_falls_through_to_the_host():
    def boom(mint):
        raise KeyError(mint)
    assert _run(boom) == (1, "host")


def test_the_lane_cache_hands_the_facts_then_the_signal_decimals():
    from sniper.services.memecoin_bsc_fast_lane import BscSafetyCache
    cache = BscSafetyCache(lambda *a, **k: (True, "ok"), ttl_s=60, usd=50, quotes={},
                           generation_fn=lambda: 1.0)
    cache.note_candidates([{"mint": MINT.upper().replace("0X", "0x"), "quote": "0x" + "bb" * 20,
                            "pool": "0x" + "cc" * 20, "decimals": 9}], now=1.0)
    assert cache.decimals_hint(MINT) == [("facts", None), ("signal", 9)]
    cache._facts[MINT.lower()] = (18, 10 ** 27)
    assert cache.decimals_hint(MINT) == [("facts", 18), ("signal", 9)]


def test_the_bsc_lane_safety_takes_the_hint(monkeypatch):
    from sniper.execution import evm_token_safety as S
    from sniper.services import memecoin_bsc_fast_lane as lane
    captured = {}
    monkeypatch.setattr(S, "build_evm_token_safety",
                        lambda **kw: captured.update(kw) or (lambda *a, **k: (True, "ok")))
    hint = lambda mint: [("signal", 18)]
    lane.lane_safety_fn("https://lane", None, TAKER, {}, decimals_hint=hint).for_quote(MINT, None)
    assert captured["decimals_hint_fn"] is hint


def test_a_dropped_candidate_takes_its_signal_decimals_with_it():
    """Gamma LOW on 2f8862742: _drop clears the signal's decimals on expiry and on eviction, so a
    later candidate for the mint never sizes its probe from a stale signal."""
    from sniper.services import memecoin_bsc_fast_lane as lane
    cache = lane.BscSafetyCache(lambda *a, **k: (True, "ok"), ttl_s=60, usd=50, quotes={},
                                generation_fn=lambda: 1.0)
    entry = {"mint": MINT, "quote": "0x" + "bb" * 20, "pool": "0x" + "cc" * 20, "decimals": 9}
    cache.note_candidates([entry], now=1.0)
    cache.next_due(1.0 + lane.CANDIDATE_TTL_S + 1.0)                 # expiry
    assert MINT not in cache.signal_decimals and cache.decimals_hint(MINT)[1] == ("signal", None)
    cache.note_candidates([dict(entry, priority=99)], now=10_000.0)
    others = [{"mint": "0x%040x" % i, "quote": entry["quote"], "pool": entry["pool"], "priority": 0}
              for i in range(lane.CANDIDATE_LIMIT)]
    cache.note_candidates(others, now=10_000.0)                       # eviction (lowest priority)
    assert MINT not in cache.signal_decimals


def test_when_both_are_known_the_facts_the_entry_gate_checks_size_the_probe():
    """Facts first (Alpha, Gamma NIT on 2f8862742): the inline verdict is cached per mint, so its
    probe must be sized from the decimals the entry gate's decimals_disagree compares, not the
    signal's when they differ."""
    from sniper.services.memecoin_bsc_fast_lane import BscSafetyCache
    cache = BscSafetyCache(lambda *a, **k: (True, "ok"), ttl_s=60, usd=50, quotes={},
                           generation_fn=lambda: 1.0)
    cache.note_candidates([{"mint": MINT, "quote": "0x" + "bb" * 20, "pool": "0x" + "cc" * 20,
                            "decimals": 9}], now=1.0)
    cache._facts[MINT.lower()] = (18, 10 ** 27)
    assert _run(cache.decimals_hint) == (0, "facts")
