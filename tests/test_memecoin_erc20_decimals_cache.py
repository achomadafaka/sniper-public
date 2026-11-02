"""ERC-20 decimals: retried once, never guessed, and the fail-closed gate untouched.

2026-08-30T17:23. Signal GBA on 4663 quoted successfully in 2483ms, then the balance read came
back with decimals=None, so `quote_unit_price` could not value our own quote and the
execution-premium gate refused `unpriceable_quote`. That refusal is CORRECT and stays. The defect
is that we asked the network for an IMMUTABLE CONSTANT on the funded critical path and threw away
a recoverable entry when it did not answer — a direct call returned 18 moments later.

The funded path is already cached: `memecoin_evm_leg` builds a process-lifetime BoundedLru per
(chain, token) at startup and threads it through as `decimals_fn`. So the incident needed a COLD
miss, and a cold miss is exactly when a cache cannot help. Hence a retry, not another cache.
"""
import pytest

from sniper.execution import evm_wallet as W


def _calls(seq):
    """An `_eth_call` stand-in yielding `seq` in order; an Exception entry raises."""
    box = {"n": 0}

    def call(_token, _sel, _rpc, http=None):
        value = seq[min(box["n"], len(seq) - 1)]
        box["n"] += 1
        if isinstance(value, Exception):
            raise value
        return value

    call.count = lambda: box["n"]
    return call


def test_one_transient_no_longer_costs_the_read(monkeypatch):
    """The incident in one line. Mutation check: set attempts to 1 and this goes red."""
    call = _calls([RuntimeError("429"), "0x12"])
    monkeypatch.setattr(W, "_eth_call", call)
    monkeypatch.setattr(W._time, "sleep", lambda _s: None)
    assert W.erc20_decimals("0xTok", "https://rpc") == 18
    assert call.count() == 2


def test_a_total_failure_still_fails_closed(monkeypatch):
    """The gate that refused this entry was right. A retry must not become a way to admit a quote
    we cannot value — it only removes the SINGLE-transient case."""
    call = _calls([RuntimeError("down"), RuntimeError("down"), "0x12"])
    monkeypatch.setattr(W, "_eth_call", call)
    monkeypatch.setattr(W._time, "sleep", lambda _s: None)
    assert W.erc20_decimals("0xTok", "https://rpc") is None
    assert call.count() == 2, "retried more than the bounded number of attempts"


def test_retries_are_bounded_and_configurable(monkeypatch):
    call = _calls([RuntimeError("x")])
    monkeypatch.setattr(W, "_eth_call", call)
    monkeypatch.setattr(W._time, "sleep", lambda _s: None)
    assert W.erc20_decimals("0xTok", "https://rpc", retries=0) is None
    assert call.count() == 1, "retries=0 must mean exactly one attempt"


def test_a_nonsense_reading_is_unknown_not_a_number(monkeypatch):
    monkeypatch.setattr(W, "_eth_call", _calls(["not-hex", "not-hex"]))
    monkeypatch.setattr(W._time, "sleep", lambda _s: None)
    assert W.erc20_decimals("0xTok", "https://rpc") is None


def test_zero_decimals_is_a_real_answer_not_a_miss(monkeypatch):
    """0 is valid for some tokens. Treating it as falsy would retry a good read and then, on a
    truthiness test, discard it. Mutation check: `if value:` instead of `is not None` -> red."""
    call = _calls(["0x00"])
    monkeypatch.setattr(W, "_eth_call", call)
    monkeypatch.setattr(W._time, "sleep", lambda _s: None)
    assert W.erc20_decimals("0xTok", "https://rpc") == 0
    assert call.count() == 1


def test_no_module_level_cache_was_introduced():
    """A second cache in this library leaked across tests and broke three unrelated request-count
    assertions. The funded path's cache lives in `memecoin_evm_leg` where it can be scoped to a
    context; a bounded global with no reset seam is the shared-state hazard this codebase has
    been bitten by twice today."""
    assert not hasattr(W, "_decimals_cache")
