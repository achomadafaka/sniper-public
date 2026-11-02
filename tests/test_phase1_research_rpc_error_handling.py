"""Codex audit #9 (35948d32..d7ef0f2c): scripts/research/phase1_evm_resolver.py:133 and
phase1_holder_check.py:83.

Two bugs in the shared `_rpc` / bisect pattern both files use:

1. `_rpc` returned the bare string "failed" when every retry raised a transport exception. Every
   caller reads the error as `err.get("message", "")`, which raises AttributeError on a string --
   an ordinary network blip crashed the whole resolver instead of falling into its None/"unknown"
   branch.

2. When a retryable error triggers a bisect, the two halves were combined with `left or right`
   (holder check, boolean) or `(left or []) + (right or [])` (log resolver, list). Both treat a
   FAILED half (None/unknown) exactly like an EMPTY/False half (successfully read, nothing found)
   -- so a window that lost half its coverage could still report a confident negative ("no
   outbound transfer" / "these are all the logs") built from only the half that answered. A
   negative conclusion needs both halves read successfully; a positive hit in either half is still
   real evidence regardless of the other half's fate.
"""
import time
import urllib.request

import scripts.research.phase1_evm_resolver as resolver
import scripts.research.phase1_holder_check as holder


def _always_raises(*_a, **_k):
    raise TimeoutError("no route to host")


def test_rpc_exhaustion_returns_a_dict_error_not_a_bare_string(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(urllib.request, "urlopen", _always_raises)

    for mod in (resolver, holder):
        result, err = mod._rpc("eth_getLogs", [{}])
        assert result is None
        assert hasattr(err, "get"), (
            "%s._rpc returned %r on exhaustion -- every caller does err.get('message', ''), "
            "which raises AttributeError on a bare string" % (mod.__name__, err))
        assert err.get("message")


def test_a_real_transport_exhaustion_does_not_crash_get_logs_windowed(monkeypatch):
    """The regression itself, not just the return shape: before the fix this raised
    AttributeError instead of returning None."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(urllib.request, "urlopen", _always_raises)

    assert resolver.get_logs_windowed("0xmint", 100, 200) is None


def test_a_real_transport_exhaustion_does_not_crash_outbound_transfer_exists(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(urllib.request, "urlopen", _always_raises)

    assert holder.outbound_transfer_exists("0xmint", "0xsender", 100, 200) is None


def test_a_failed_half_of_a_bisected_log_window_is_unknown_not_dropped(monkeypatch):
    """Left half never resolves (fails retryably all the way to max bisect depth); right half
    genuinely has one log. The combined answer must be None (we do not have full coverage),
    never just the right half's logs presented as though they were the complete window."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    def fake_rpc(method, params, tries=4):
        rng = params[0]
        lo = int(rng["fromBlock"], 16)
        if lo <= 150:
            return None, {"message": "exceeds limit"}     # the top call and the whole left branch
        return [{"blockNumber": hex(160)}], None            # the right half has a real log

    monkeypatch.setattr(resolver, "_rpc", fake_rpc)

    result = resolver.get_logs_windowed("0xmint", 100, 200)

    assert result is None, (
        "a bisected window with one half unread must report unknown coverage, not the "
        "successfully-read half's logs as though nothing was missing: got %r" % (result,))


def test_a_failed_half_of_a_bisected_holder_check_is_unknown_not_a_negative(monkeypatch):
    """Left half never resolves; right half genuinely finds no outbound transfer. The combined
    answer must be None (unknown), never a confident 'still holds' built on half the window."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    def fake_rpc(method, params, tries=4):
        rng = params[0]
        lo = int(rng["fromBlock"], 16)
        if lo <= 150:
            return None, {"message": "exceeds limit"}       # the top call and the whole left branch
        return [], None                                     # the right half is a real, clean miss

    monkeypatch.setattr(holder, "_rpc", fake_rpc)

    result = holder.outbound_transfer_exists("0xmint", "0xsender", 100, 200)

    assert result is None, (
        "a bisected window with one half unread must report unknown, not a negative built "
        "from only the half that answered: got %r" % (result,))


def test_a_hit_in_either_half_still_counts_despite_the_other_half_failing(monkeypatch):
    """The fix must not overcorrect: a real positive from one half is still real evidence even
    when the sibling half never resolved."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    def fake_rpc(method, params, tries=4):
        rng = params[0]
        lo = int(rng["fromBlock"], 16)
        if lo <= 150:
            return None, {"message": "exceeds limit"}        # left never resolves
        return [{"x": 1}], None                              # right DOES find a transfer

    monkeypatch.setattr(holder, "_rpc", fake_rpc)

    assert holder.outbound_transfer_exists("0xmint", "0xsender", 100, 200) is True
