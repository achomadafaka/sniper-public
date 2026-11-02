from __future__ import annotations

import sqlite3
from dataclasses import replace
import json
import socket
import threading
from urllib.error import HTTPError

import pytest


class QuotaHttp:
    def __call__(self, _request, timeout):
        raise HTTPError("https://research.invalid", 429, "quota", {}, None)


def test_research_rpc_429_has_typed_quota_result() -> None:
    from sniper.services.memecoin_cluster_rpc import ResearchClusterRpc, ResearchRpcQuotaError

    rpc = ResearchClusterRpc("https://research.invalid/?api-key=research", http=QuotaHttp())
    with pytest.raises(ResearchRpcQuotaError):
        rpc.call("getSignaturesForAddress", ["OWNER", {"limit": 1000}])


def test_research_rpc_requires_the_dedicated_url(monkeypatch) -> None:
    from sniper.services.memecoin_cluster_rpc import ResearchClusterRpc

    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_URL", "https://custody.invalid")
    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_POOL_URLS", "https://pool.invalid")
    with pytest.raises(ValueError):
        ResearchClusterRpc("")


def test_holder_eligibility_keeps_creator_and_unknown_rank_one() -> None:
    from sniper.services.memecoin_cluster_rpc import ResearchClusterRpc

    rpc = ResearchClusterRpc("https://research.invalid/?api-key=research")
    rpc.holder_accounts = lambda _mint: [("ATA-A", "60"), ("ATA-B", "40")]
    rpc.token_account_owners = lambda _accounts: {"ATA-A": "CREATOR", "ATA-B": "B"}
    # Owner-program reads raise on failure since d87abea8c's review; stub them (system wallets).
    rpc.owner_programs = lambda owners: {o: "11111111111111111111111111111111" for o in owners}
    holders = rpc.eligible_holders("MINT")
    assert [(row.owner, row.balance_atomic) for row in holders] == [("CREATOR", 60), ("B", 40)]


def test_store_keeps_atomic_holder_balances_as_text(tmp_path) -> None:
    from sniper.services.memecoin_cluster_store import ClusterStore

    store = ClusterStore(tmp_path / "cluster.db")
    snapshot_id = store.create_snapshot("MINT", "2026-08-22T00:00:00Z")
    store.add_holder(snapshot_id, "ATA", "OWNER", "18446744073709551615", None)
    assert store.holders(snapshot_id)[0]["balance_atomic"] == "18446744073709551615"


def test_decision_rows_are_immutable_at_the_database_layer(tmp_path) -> None:
    from sniper.services.memecoin_cluster_admission import ClusterDecision
    from sniper.services.memecoin_cluster_store import ClusterStore

    store = ClusterStore(tmp_path / "cluster.db")
    decision = ClusterDecision(
        decision_id="decision-1",
        allowed=False,
        reason="cluster_share_high",
        policy_id="solana-funded-cluster-admission-v3",
        snapshot_at="2026-08-22T00:00:00Z",
        completed_at="2026-08-22T00:00:01Z",
        metrics={"attempted_supply": 100},
    )
    store.insert_decision(decision, None, "global_feed")
    changed = replace(decision, reason="allowed", allowed=True)
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_decision(changed, None, "global_feed")


def test_unresolved_cache_work_is_newest_first(tmp_path) -> None:
    from sniper.services.memecoin_cluster_store import ClusterStore

    store = ClusterStore(tmp_path / "cluster.db")
    store.enqueue_unresolved("OLD", "2026-08-22T00:00:00Z", "pending")
    store.enqueue_unresolved("NEW", "2026-08-22T00:00:01Z", "pending")
    assert store.claim_cache_work(2, "2026-08-22T00:00:02Z") == ["NEW", "OLD"]


def test_client_timeout_returns_store_unavailable_block() -> None:
    from sniper.services.memecoin_cluster_service import ClusterGateClient

    decision = ClusterGateClient("/missing/cluster.sock", timeout_s=0.01).decide(
        "MINT", source_path="global_feed"
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_store_unavailable"


def _one_reply(path, reply: dict) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        ready.set()
        client, _ = server.accept()
        client.recv(16_384)
        client.sendall(json.dumps(reply).encode() + b"\n")
        client.close()
        server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(1), "test cluster socket did not start"
    return thread


def _allow_reply(snapshot_at: str) -> dict:
    return {
        "allowed": True,
        "completed_at": "2026-08-22T00:00:00Z",
        "decision_id": "decision-1",
        "metrics": {"coverage": 1.0},
        "policy_id": "solana-funded-cluster-admission-v3",
        "reason": "allowed",
        "snapshot_at": snapshot_at,
    }


def test_client_accepts_exactly_five_second_allow(tmp_path) -> None:
    from sniper.services.memecoin_cluster_service import ClusterGateClient

    path = tmp_path / "cluster.sock"
    server = _one_reply(path, _allow_reply("2026-08-21T23:59:48Z"))
    decision = ClusterGateClient(
        path, now_fn=lambda: "2026-08-22T00:00:05Z"
    ).decide("MINT", source_path="global_feed")
    server.join(1)
    assert decision.allowed is True


def test_client_rejects_allow_older_than_five_seconds(tmp_path) -> None:
    from sniper.services.memecoin_cluster_service import ClusterGateClient

    path = tmp_path / "cluster.sock"
    server = _one_reply(path, _allow_reply("2026-08-21T23:59:48Z"))
    decision = ClusterGateClient(
        path, now_fn=lambda: "2026-08-22T00:00:05.001Z"
    ).decide("MINT", source_path="global_feed")
    server.join(1)
    assert decision.allowed is False
    assert decision.reason == "cluster_decision_stale"


def test_allowed_decision_rejects_snapshot_older_than_17_seconds(tmp_path) -> None:
    from sniper.services.memecoin_cluster_service import ClusterGateClient

    path = tmp_path / "cluster.sock"
    server = _one_reply(path, _allow_reply("2026-08-21T23:59:42.999Z"))
    decision = ClusterGateClient(
        path, now_fn=lambda: "2026-08-22T00:00:00Z"
    ).decide("MINT", source_path="global_feed")
    server.join(1)
    assert decision.allowed is False
    assert decision.reason == "cluster_decision_stale"


def test_cache_pass_stops_on_first_quota_result() -> None:
    from sniper.services.memecoin_cluster_service import ClusterCacheWorker
    from sniper.services.memecoin_cluster_rpc import ResearchRpcQuotaError

    calls: list[str] = []

    def trace(owner: str):
        calls.append(owner)
        if owner == "B":
            raise ResearchRpcQuotaError("quota")
        return f"F-{owner}", "resolved"

    worker = ClusterCacheWorker.for_test(queue=["A", "B", "C"], trace_fn=trace)
    assert worker.cache_pass() == {"resolved": 1, "quota": 1}
    assert calls == ["A", "B"]
