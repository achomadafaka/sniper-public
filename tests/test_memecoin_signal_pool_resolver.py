"""The signal-time resolver must close the coverage gap without becoming an RPC burst.

It exists because mint->pool resolution was a consequence of holding (0/99 pre-entry), leaving
three observation lanes failing open. It runs on the research key beside a funded runner, so every
bound is a safety property, not tidiness.
"""
from __future__ import annotations

import pathlib

import pytest

from sniper.services import memecoin_signal_pool_resolver as spr
from sniper.services import memecoin_pool_resolve as pr

LOOP = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py"


@pytest.fixture(autouse=True)
def _clean():
    spr.reset_state()
    yield
    spr.reset_state()


# ------------------------------------------------------------------ enqueue

def test_a_signal_enqueues_its_mint():
    assert spr.enqueue("MINT", now=0.0) is True
    assert spr.snapshot()["pending"] == 1 and spr.snapshot()["enqueued"] == 1


def test_a_hot_mint_is_resolved_once_per_ttl_not_once_per_signal():
    assert spr.enqueue("MINT", now=0.0, ttl_s=900.0) is True
    for t in (1.0, 60.0, 899.0):
        assert spr.enqueue("MINT", now=t, ttl_s=900.0) is False
    assert spr.snapshot()["deduped"] == 3
    # Drain it: while a mint is still PENDING, refusing to re-queue is correct regardless of TTL —
    # the TTL governs re-resolution, not duplicate queue entries.
    assert spr.take_next(1.0) is not None
    assert spr.enqueue("MINT", now=899.0, ttl_s=900.0) is False, "TTL still holds after draining"
    assert spr.enqueue("MINT", now=901.0, ttl_s=900.0) is True


def test_a_mint_that_already_has_a_pool_is_not_queued():
    assert spr.enqueue("MINT", now=0.0, has_pool_fn=lambda _m: True) is False
    assert spr.snapshot()["pending"] == 0


def test_an_unreadable_pool_check_still_enqueues():
    """A broken lookup is not a reason to skip work — worst case we resolve something twice."""
    def boom(_m):
        raise RuntimeError("tape gone")
    assert spr.enqueue("MINT", now=0.0, has_pool_fn=boom) is True


def test_an_empty_mint_is_ignored():
    assert spr.enqueue(None, now=0.0) is False
    assert spr.enqueue("", now=0.0) is False


# ------------------------------------------------------------------ bounds

def test_the_queue_drops_the_OLDEST_when_full_and_counts_it():
    """Oldest first: it has waited longest and its signal is stalest."""
    for i in range(4):
        spr.enqueue(f"M{i}", now=float(i), max_pending=4)
    assert spr.enqueue("NEW", now=10.0, max_pending=4) is True
    pending = [m for m, _fs in spr._PENDING]
    assert "M0" not in pending and "NEW" in pending
    assert len(pending) == 4 and spr.snapshot()["dropped_full"] == 1


def test_a_signal_burst_cannot_become_an_rpc_burst():
    for i in range(30):
        spr.enqueue(f"M{i}", now=0.0, max_pending=64)
    taken = [spr.take_next(0.0, max_per_min=10) for _ in range(15)]
    assert sum(1 for x in taken if x is not None) == 10, "the rate cap is the bound"
    assert spr.snapshot()["dropped_rate"] >= 1


def test_the_rate_window_rolls_forward():
    for i in range(20):
        spr.enqueue(f"M{i}", now=0.0, max_pending=64)
    for _ in range(10):
        spr.take_next(0.0, max_per_min=10)
    assert spr.take_next(30.0, max_per_min=10) is None      # still inside the minute
    assert spr.take_next(61.0, max_per_min=10) is not None   # window rolled


def test_an_empty_queue_yields_nothing_without_consuming_rate():
    assert spr.take_next(0.0, max_per_min=10) is None
    assert spr.snapshot()["dropped_rate"] == 0


def test_the_dedup_memory_is_bounded():
    for i in range(50):
        spr.enqueue(f"M{i}", now=float(i), ttl_s=1.0, max_pending=64)
    assert len(spr._SEEN) < 50, "old dedup entries must be pruned, not accumulated forever"


# ------------------------------------------------------------------ outcomes

@pytest.mark.parametrize("stats,expect", [
    ({"resolved": 1}, "resolved"),
    ({"unresolved": 1}, "unresolved"),
    ({"errors": 1}, "failed"),
])
def test_each_outcome_lands_in_its_own_counter(stats, expect):
    spr.record_outcome(stats)
    assert spr.snapshot()[expect] == 1


def test_a_resolver_failure_writes_nothing_and_never_raises():
    """resolve_one_mint swallows per-mint failures; a dead RPC must not kill the worker."""
    def boom(*_a, **_k):
        raise RuntimeError("rpc down")
    out = pr.resolve_one_mint(None, None, "MINT", first_seen=None, get_tx=None, get_sigs=None,
                              call=None, max_fetch=4, resolve_fn=boom, tape_fn=boom)
    assert out["errors"] == 1 and out["resolved"] == 0 and out["vault"] is None


def test_an_unresolvable_mint_is_never_guessed():
    out = pr.resolve_one_mint(None, None, "MINT", first_seen=None, get_tx=None, get_sigs=None,
                              call=None, max_fetch=4,
                              resolve_fn=lambda *a, **k: (None, 2), tape_fn=None)
    assert out["unresolved"] == 1 and out["vault"] is None and out["rpc_calls"] == 2


def test_a_resolved_pool_gets_its_tape_drawn():
    """Resolving alone leaves a pool with meta and zero ticks — indistinguishable from no lane."""
    drew = {}
    out = pr.resolve_one_mint(
        None, None, "MINT", first_seen="2026-08-26T12:00:00+00:00", get_tx=None, get_sigs=None,
        call=None, max_fetch=4, tape_budget=4,
        resolve_fn=lambda *a, **k: ("VAULT", 1),
        tape_fn=lambda *a, **k: drew.setdefault("n", 3) and 3,
        floor_fn=lambda fs: "floor-from-" + str(fs))
    assert out["resolved"] == 1 and out["taped"] == 1 and out["tape_calls"] == 3


def test_the_floor_is_passed_to_the_tape_draw():
    """floor_ts=None makes the pass try to drain the vault's entire history and never converge."""
    seen = {}

    def tape(tc, mint, vault, gs, gt, now, *, tx_budget, floor_ts):
        seen["floor"] = floor_ts
        return 1
    pr.resolve_one_mint(None, None, "MINT", first_seen="FS", get_tx=None, get_sigs=None,
                        call=None, max_fetch=4, tape_budget=4,
                        resolve_fn=lambda *a, **k: ("VAULT", 1), tape_fn=tape,
                        floor_fn=lambda fs: "FLOOR:" + fs)
    assert seen["floor"] == "FLOOR:FS"


# ------------------------------------------------------------------ isolation

def test_the_service_never_imports_the_entry_path():
    src = pathlib.Path(spr.__file__).read_text()
    for forbidden in ("real_entry_hook", "execute_fn", "reserve_entry", "wallet_exec_lock",
                      "MEMECOIN_SOLANA_RPC_POOL_URLS", "MEMECOIN_SOLANA_RPC_URL"):
        assert forbidden not in src, f"the resolver must not reference {forbidden}"


def test_the_worker_uses_the_RESEARCH_key_only():
    body = LOOP.read_text()
    start = body.index("def _signal_pool_resolver_worker")
    block = body[start:start + 2400]
    assert "MEMECOIN_RESEARCH_HELIUS_RPC_URL" in block
    for forbidden in ("MEMECOIN_SOLANA_RPC_URL", "solana_funded_rpc_urls", "CAPTURE_"):
        assert forbidden not in block, f"resolver must not touch {forbidden}"


def test_the_snapshot_carries_what_an_operator_needs():
    spr.enqueue("A", now=0.0)
    spr.record_outcome({"resolved": 1})
    snap = spr.snapshot()
    for key in ("enqueued", "resolved", "unresolved", "failed", "dropped_full", "dropped_rate",
                "deduped", "pending"):
        assert key in snap


def test_the_heartbeat_field_survives_to_the_written_file(tmp_path):
    """The assembly is a whitelist; an unnamed field never reaches the file."""
    from sniper.services import memecoin_runner as run
    path = str(tmp_path / "hb.json")
    run.write_heartbeat({"events": 1, "opened": 0}, auth_ok=True, path=path,
                        pool_resolver=spr.snapshot())
    got = run.read_heartbeat(path)
    assert "pool_resolver" in got and "pending" in got["pool_resolver"]


# ------------------------------------------------------------------ funded Jupiter load

def test_the_jupiter_counter_reaches_the_heartbeat_file(tmp_path):
    """Gates enabling any second Jupiter consumer on this IP, so it has to actually be visible."""
    from sniper.services import memecoin_runner as run
    from sniper.execution.solana_real_seams import jupiter_load_snapshot
    path = str(tmp_path / "hb.json")
    run.write_heartbeat({"events": 1, "opened": 0}, auth_ok=True, path=path,
                        jupiter_load=jupiter_load_snapshot())
    got = run.read_heartbeat(path)
    assert "jupiter_load" in got
    for key in ("quotes", "rate_limited", "errors"):
        assert key in got["jupiter_load"]


def test_counting_can_never_refuse_a_funded_quote(monkeypatch):
    """Fail-soft: a broken counter must not become a quote failure."""
    from sniper.execution import solana_real_seams as seams

    class Hostile(dict):
        def __setitem__(self, *_a):
            raise RuntimeError("counter exploded")
        def __getitem__(self, k):
            raise RuntimeError("counter exploded")
    monkeypatch.setattr(seams, "JUPITER_LOAD", Hostile())
    monkeypatch.setattr(seams, "_jup_quote_status", lambda *a, **k: ({"outAmount": "1"}, 200))
    assert seams._quote_with_fallback("A", "B", 1, 50, None) is not None
