"""A funded position whose vol_death rail cannot prove silence for too long pages once.

PAIDCATS 65854 (2026-09-26) sat on onchain_pending / onchain_unavailable for four minutes with a 30 s
threshold and nothing paged; 65860 held on a false route_changed every minute. A 'pending' that
never ends is not a healthy state (Codex ctb audit, P0 item 5).
"""
import threading
import time

import pytest

from sniper.services import memecoin_alerts as A


@pytest.fixture(autouse=True)
def _clean():
    A._vd_blind_state.clear()
    yield
    A._vd_blind_state.clear()


def _note(pid, why, now, sent, page_after_s=120.0):
    return A.note_voldeath_coverage(pid, why, mint="MintX", handle="h", ticker="T",
                                    now_mono=now, page_after_s=page_after_s,
                                    send_fn=sent.append)


def test_a_blind_episode_pages_once_after_the_threshold_then_recovers():
    sent = []
    assert _note(1, "onchain_pending", 1000.0, sent) is None
    assert _note(1, "onchain_unavailable:identity_mismatch", 1119.0, sent) is None
    assert sent == []
    assert _note(1, "onchain_unavailable:provider", 1120.0, sent) == "blind"
    assert len(sent) == 1 and "VOLDEATH-BLIND" in sent[0] and "120s" in sent[0]
    assert "onchain_unavailable:provider" in sent[0] and "T (h)" in sent[0]
    assert _note(1, "route_unknown_entry", 1300.0, sent) is None      # still one page
    assert _note(1, "onchain_moving", 1310.0, sent) == "recovered"    # evidence again
    assert len(sent) == 2 and "RECOVERED" in sent[1]
    assert _note(1, "onchain_pending", 2000.0, sent) is None          # a new episode
    assert _note(1, "onchain_pending", 2121.0, sent) == "blind"
    assert len(sent) == 3


def test_by_design_holds_and_fires_are_not_blind():
    sent = []
    for why in ("state_uncovered_frozen", "state_moving", "route_changed", "measured",
                "measured_onchain_still", "exact_quote_moving"):
        assert _note(2, why, 1000.0, sent) is None
        assert _note(2, why, 5000.0, sent) is None
    assert sent == [] and 2 not in A._vd_blind_state


def test_a_short_blip_that_recovers_never_pages():
    sent = []
    _note(3, "onchain_pending", 1000.0, sent)
    assert _note(3, "onchain_moving", 1050.0, sent) is None           # never paged: no recovery
    _note(3, "onchain_pending", 1100.0, sent)
    assert _note(3, "onchain_pending", 1200.0, sent) is None          # the clock restarted
    assert sent == []


def test_the_knob_at_zero_never_pages():
    sent = []
    _note(4, "onchain_pending", 1000.0, sent, page_after_s=0)
    assert _note(4, "onchain_pending", 9000.0, sent, page_after_s=0) is None
    assert sent == []


class _SlowState(dict):
    """Yields right after the `paged` check reads, before the write that follows it: the window
    a busy box opens for free, and the one a missing lock would double-page through."""
    def __getitem__(self, key):
        value = dict.__getitem__(self, key)
        if key == "paged":
            time.sleep(0.01)
        return value


class _SlowDict(dict):
    def __setitem__(self, key, value):
        dict.__setitem__(self, key, _SlowState(value))


def test_two_lanes_cannot_double_page(monkeypatch):
    # The fast lane and the slow sweep both run the decision; the GIL alone hides a missing lock.
    sent = []
    monkeypatch.setattr(A, "_vd_blind_state", _SlowDict())
    _note(5, "onchain_pending", 1000.0, sent)
    threads = [threading.Thread(target=_note, args=(5, "onchain_pending", 1200.0, sent))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(sent) == 1


def test_entries_unseen_for_an_hour_are_pruned():
    sent = []
    _note(6, "onchain_pending", 1000.0, sent)
    _note(7, "onchain_pending", 1000.0 + 3601.0, sent)
    assert 6 not in A._vd_blind_state and 7 in A._vd_blind_state


def test_a_send_failure_never_raises():
    def boom(_msg):
        raise RuntimeError("telegram down")
    A.note_voldeath_coverage(8, "onchain_pending", now_mono=0.0, page_after_s=1.0, send_fn=boom)
    assert A.note_voldeath_coverage(8, "onchain_pending", now_mono=5.0, page_after_s=1.0,
                                    send_fn=boom) == "blind"


def test_the_runner_notes_every_vol_death_verdict():
    # Structural on purpose: the call must sit on the verdict the decision returned, for EVERY
    # verdict (a non-blind one is what ends an episode), not only inside the HELD branch.
    import ast
    import inspect
    from sniper.services import memecoin_real_runner as R
    tree = ast.parse(inspect.getsource(R))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "note_voldeath_coverage"]
    assert len(calls) == 1, "expected exactly one call site in the runner"
    call = calls[0]
    assert isinstance(call.args[1], ast.Name) and call.args[1].id == "_vd_why"
    decision = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", "") == "armed_exit_decision"]
    assert decision and decision[0].lineno < call.lineno
