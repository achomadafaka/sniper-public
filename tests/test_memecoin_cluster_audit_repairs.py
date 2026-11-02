"""Regressions for the eight defects found in the 2026-08-22 cluster-admission audit."""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.services.memecoin_cluster_admission import decide_cluster
from sniper.services.memecoin_cluster_rpc import SYSTEM_PROGRAM_ID, ResearchClusterRpc
from sniper.services.memecoin_cluster_service import ClusterCacheWorker
from sniper.services.memecoin_cluster_rpc import ResearchRpcQuotaError


# --- defect 1: curve and pool owners were never excluded ---

class _FakeRpc(ResearchClusterRpc):
    def __init__(self, largest, owners, programs):
        self._largest, self._owners, self._programs = largest, owners, programs

    def holder_accounts(self, mint):
        return self._largest

    def token_account_owners(self, accounts):
        return {a: self._owners[a] for a in accounts if a in self._owners}

    def owner_programs(self, owners):
        return {o: self._programs.get(o) for o in owners}


def test_program_owned_holder_is_excluded_by_identity_not_balance():
    """The pump bonding curve is a PDA with no funding transfer, so it can never resolve.
    Without identity-based exclusion every pre-migration pump coin blocks forever — the
    curve held 99.2% of one live book, and 77.4% of coins have a top holder past the 50%
    top-funder limit."""
    rpc = _FakeRpc(
        largest=[("ataCURVE", "991735643282127"), ("ataA", "8196544389457")],
        owners={"ataCURVE": "CURVE", "ataA": "WALLET_A"},
        programs={"CURVE": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                  "WALLET_A": SYSTEM_PROGRAM_ID},
    )
    holders = rpc.eligible_holders("MINT")
    curve = next(h for h in holders if h.owner == "CURVE")
    wallet = next(h for h in holders if h.owner == "WALLET_A")
    assert curve.exclusion is not None and curve.exclusion.startswith("program_account:")
    assert wallet.exclusion is None


def test_unreadable_owner_stays_eligible_and_blocks():
    """An owner we cannot identify is NEVER waved through — it stays eligible, fails to
    resolve, and blocks. A balance heuristic is forbidden precisely because the median
    top-holder share is 80%."""
    rpc = _FakeRpc(
        largest=[("ataX", "500")],
        owners={"ataX": "UNKNOWN_BIG"},
        programs={"UNKNOWN_BIG": None},
    )
    assert rpc.eligible_holders("MINT")[0].exclusion is None


# --- defect 5: float thresholds on atomic balances ---

def test_exact_thresholds_use_integer_ratios():
    big = 10 ** 15
    exact25 = decide_cluster(
        [("A", 25 * big), ("B", 25 * big), ("C", 25 * big), ("D", 25 * big)],
        {"A": "F", "B": "F"}, trace_fn=lambda o: (f"X{o}", "resolved"))
    assert exact25.allowed is False and exact25.reason == "cluster_share_high"
    clean = decide_cluster(
        [("A", 10 * big), ("B", 10 * big), ("C", 40 * big), ("D", 40 * big)],
        {}, trace_fn=lambda o: (f"X{o}", "resolved"))
    assert clean.allowed is True


# --- defect 4: quota stranded claims and had no backoff ---

class _Store:
    def __init__(self):
        self.finished = []

    def claim_cache_work(self, limit, now):
        return ["o1", "o2", "o3", "o4"]

    def finish_cache_work(self, owner, state, now):
        self.finished.append((owner, state))

    def put_funder_result(self, *a, **k):
        pass


def test_quota_releases_every_claimed_owner_and_backs_off():
    store = _Store()
    def trace(owner):
        raise ResearchRpcQuotaError("429")
    worker = ClusterCacheWorker(store, trace)
    clock = [0.0]
    counts = worker.cache_pass(limit=4, monotonic=lambda: clock[0])
    assert counts["quota"] == 1
    # every owner in the batch is released back to pending, none stranded
    assert [o for o, state in store.finished] == ["o1", "o2", "o3", "o4"]
    assert all(state == "pending" for _, state in store.finished)
    assert worker._quota_backoff_s > 0
    # the next immediate pass does not re-claim
    again = worker.cache_pass(limit=4, monotonic=lambda: clock[0])
    assert again == {"resolved": 0, "quota": 0} and worker.skipped_for_backoff is True


# --- defect 7: worker state must never fall into the release directory ---

def test_worker_requires_an_explicit_state_root(monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import memecoin_cluster_gate_worker as w
    monkeypatch.delenv("SNIPER_STATE_ROOT", raising=False)
    with pytest.raises(SystemExit):
        w.main([])
    assert "SNIPER_STATE_ROOT is required" in capsys.readouterr().err
