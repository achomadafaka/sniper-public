"""The Solana exact-exit preview answers within a wall-clock deadline, or says it could not.

2026-09-27 12:57:17-12:57:56Z (position 66015): the fast lane held the funded exit lock ~39 s while
the funded pool failed every endpoint; one preview balance read took 12.5 s. The preview is the
first step of every exact exit quote, it runs while the lock is held, and nothing bounded it as a
whole. A preview past MEMECOIN_SOLANA_PREVIEW_DEADLINE_S now returns an INFRA failure
(PROVIDER_TIMEOUT: the rail holds, the ops streak moves, unsellable misses never do) and frees the
lock. One preview per position may be in flight; a call while it still runs answers at once.

Same fixture style as test_memecoin_exit_preview_balance_failover.py.
"""
import threading
import time

import pytest
from solders.keypair import Keypair

from sniper.analysis.memecoin_quote_failures import QuoteFailure, is_infra
from sniper.execution import solana_real_seams as S
from sniper.settings import memecoin as cfg


def _executor():
    return S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://custody",
                                        http=lambda *_a, **_k: (200, {}, None),
                                        usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)


@pytest.fixture()
def seams(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_PREVIEW_DEADLINE_S", 0.3, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_READ_RPC_URL", "", raising=False)
    monkeypatch.setattr(S, "_sleep", lambda _s: None)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *a, **k: {"outAmount": "5000000"})
    release = threading.Event()
    state = {"delay": 0.0, "calls": 0}

    def balance(pubkey, mint, url, http=None, ata_fallback=False):
        state["calls"] += 1
        if state["delay"]:
            release.wait(state["delay"])
        return 777
    monkeypatch.setattr(S, "token_balance_raw", balance)
    yield state, release
    release.set()


def test_a_hung_preview_returns_an_infra_timeout_within_the_deadline(seams):
    state, _release = seams
    state["delay"] = 10.0
    t0 = time.monotonic()
    res = _executor()["preview_exit_fn"]({"id": 66015, "mint": "Mint1"})
    assert time.monotonic() - t0 < 0.3 + 0.3
    assert res.preview is None and res.failure is QuoteFailure.PROVIDER_TIMEOUT
    assert is_infra(res.failure)                       # the ops streak, never unsellable misses
    assert res.provider_id == "solana:preview_deadline"


def test_a_second_call_while_the_first_still_hangs_answers_at_once_without_a_new_thread(seams):
    state, _release = seams
    state["delay"] = 10.0
    ex = _executor()
    ex["preview_exit_fn"]({"id": 7, "mint": "Mint1"})
    calls, threads = state["calls"], threading.active_count()
    t0 = time.monotonic()
    res = ex["preview_exit_fn"]({"id": 7, "mint": "Mint1"})
    assert time.monotonic() - t0 < 0.1
    assert res.failure is QuoteFailure.PROVIDER_TIMEOUT
    assert state["calls"] == calls and threading.active_count() == threads


def test_another_position_is_not_blocked_by_a_hung_one(seams):
    state, _release = seams
    state["delay"] = 10.0
    ex = _executor()
    ex["preview_exit_fn"]({"id": 1, "mint": "Mint1"})
    state["delay"] = 0.0
    res = ex["preview_exit_fn"]({"id": 2, "mint": "Mint2"})
    assert res.failure is None and res.preview is not None


def test_a_fast_preview_is_returned_unchanged(seams):
    res = _executor()["preview_exit_fn"]({"id": 3, "mint": "Mint1"})
    assert res.failure is None and res.preview is not None
    assert res.provider_id == "solana:exact_quote"


def test_a_body_exception_still_reaches_the_caller(seams, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("body failed")
    monkeypatch.setattr(S, "token_balance_raw", boom)
    with pytest.raises(RuntimeError, match="body failed"):
        _executor()["preview_exit_fn"]({"id": 4, "mint": "Mint1"})


def test_deadline_zero_runs_the_body_inline(seams, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_PREVIEW_DEADLINE_S", 0.0)
    seen = []
    monkeypatch.setattr(S, "token_balance_raw",
                        lambda *a, **k: seen.append(threading.current_thread().name) or 777)
    _executor()["preview_exit_fn"]({"id": 5, "mint": "Mint1"})
    assert seen == [threading.current_thread().name]


def test_the_setting_defaults_to_three_seconds():
    assert cfg.MEMECOIN_SOLANA_PREVIEW_DEADLINE_S == 3.0
