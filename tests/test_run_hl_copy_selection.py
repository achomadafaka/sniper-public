import importlib
import json
import re
from pathlib import Path
import subprocess

import pytest

from sniper.hyperlive import intel_store as IS
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)


@pytest.fixture(autouse=True)
def _no_capture_network(monkeypatch):
    """The runner now fetches L2 + funding for fidelity capture. Stub them so these selection
    tests stay hermetic (capture fails open to None anyway; this just avoids the network)."""
    monkeypatch.setattr("scripts.run_hl_copy.HL.fetch_l2_book", lambda coin: None)
    monkeypatch.setattr("scripts.run_hl_copy.HL.fetch_funding_history", lambda *a, **k: None)


def _seed_proven_follows(conn, wallet="0xproven", *, n=32, net_bps=40.0):
    day = 86_400_000
    coins = ["BTC", "ETH", "SOL", "HYPE", "DOGE", "XRP", "FARTCOIN", "PURR"]
    for i in range(n):
        fid = IS.open_follow(
            conn,
            {"wallet": wallet, "coin": coins[i % len(coins)], "side": "long", "entry_mark": 1.0},
            ts_ms=(i + 1) * day,
        )
        IS.close_follow(conn, fid, exit_mark=1.0, net_bps=net_bps, reason="mirror_exit", ts_ms=(i + 1) * day)


def test_runtime_clock_uses_corrected_utc(monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    monkeypatch.setattr(mod, "real_utc_now", lambda: 1234.567)

    assert mod._now_ms() == 1_234_567


def test_copy_cycle_interval_is_start_to_start_not_sleep_after_work():
    mod = importlib.import_module("scripts.run_hl_copy")
    assert mod._cycle_wait_seconds(30, 7.5) == pytest.approx(22.5)
    assert mod._cycle_wait_seconds(30, 31) == 0


def test_marks_preserve_provider_symbol_case_and_funding_identity(monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    payload = [
        {"universe": [{"name": "kPEPE"}, {"name": "KAS"},
                      {"name": "OLD", "isDelisted": True}]},
        [{"markPx": "0.01", "funding": "0.0001"},
         {"markPx": "0.02", "funding": "0.0002"},
         {"markPx": "3", "funding": "0.1"}],
    ]
    monkeypatch.setattr(mod.HL, "_post_info", lambda _payload: payload)

    marks = mod._marks()

    assert marks == {"kPEPE": 0.01, "KAS": 0.02}
    assert mod._LATEST_FUNDING_RATES == {"kPEPE": 0.0001, "KAS": 0.0002}


def test_arm_target_stays_in_frozen_measurement_cohort_after_mean_turns_negative(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xfrozen", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xfrozen", active=True, copy_tier="watch_fallback", ts_ms=1)
    monkeypatch.setattr(mod, "_marks", lambda: {"BTC": 100.0})
    holdings = []
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", lambda _wallet: {"assetPositions": holdings})
    monkeypatch.setattr(mod, "_arm_entry_snapshot", lambda *_a, **_k: ({}, {}))
    monkeypatch.setattr(mod.IS, "record_altshort_observation", lambda *_a, **_k: None)
    arm = {"arm_id": "frozen-v1", "targets_json": json.dumps(["0xfrozen"])}

    mod._cycle(conn, now_ms=999_000, arm=arm)
    holdings.append({"position": {"coin": "BTC", "szi": "1", "entryPx": "100"}})

    assert mod._cycle(conn, now_ms=1_000_000, arm=arm) == (1, 0)


def test_mark_lookup_matches_legacy_case_but_returns_provider_symbol():
    mod = importlib.import_module("scripts.run_hl_copy")

    assert mod._provider_mark({"kPEPE": 0.01, "KAS": 0.02}, "KPEPE") == (
        "kPEPE", 0.01
    )
    assert mod._provider_mark({"kPEPE": 0.01, "KPEPE": 0.02}, "kpepe") is None


def test_copy_cycle_skips_active_watch_target_without_forward_proof(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, ts_ms=1)
    calls = {"clearinghouse": 0}

    monkeypatch.setattr(mod, "_marks", lambda: {"BTC": 100.0})

    def fetch_state(wallet):
        calls["clearinghouse"] += 1
        return {"assetPositions": [{"position": {"coin": "BTC", "szi": "1", "entryPx": "100"}}]}

    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", fetch_state)

    opened, closed = mod._cycle(conn, now_ms=1_000_000)

    assert (opened, closed) == (0, 0)
    assert calls["clearinghouse"] == 0


def test_copy_cycle_processes_watch_fallback_target_when_no_proven_exists(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback", ts_ms=1)

    monkeypatch.setattr(mod, "_marks", lambda: {"BTC": 100.0})
    holdings = []
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state",
                        lambda wallet: {"assetPositions": holdings})

    mod._cycle(conn, now_ms=999_000)                                     # baseline: flat
    holdings.append({"position": {"coin": "BTC", "szi": "1", "entryPx": "100"}})   # new entry
    opened, closed = mod._cycle(conn, now_ms=1_000_000)

    assert (opened, closed) == (1, 0)
    opened_copy = IS.get_open_copies(conn, target="0xwatch")[0]
    assert opened_copy["coin"] == "BTC"
    assert opened_copy["copy_tier"] == "watch_fallback"


def test_copy_cycle_closes_existing_copy_when_target_loses_proof(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, ts_ms=1)
    IS.open_copy(conn, "0xwatch", "BTC", "long", 100.0, ts_ms=1)
    calls = {"clearinghouse": 0}

    monkeypatch.setattr(mod, "_marks", lambda: {"BTC": 101.0})

    def fetch_state(wallet):
        calls["clearinghouse"] += 1
        return {"assetPositions": []}

    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", fetch_state)

    opened, closed = mod._cycle(conn, now_ms=1_000_000)

    assert (opened, closed) == (0, 1)
    assert calls["clearinghouse"] == 1
    assert IS.get_open_copies(conn, target="0xwatch") == []


def test_copy_cycle_does_not_false_close_deactivated_target_still_holding(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xoff", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xoff", active=False, ts_ms=1)
    IS.open_copy(conn, "0xoff", "BTC", "long", 100.0, ts_ms=1)
    calls = {"clearinghouse": 0}

    monkeypatch.setattr(mod, "_marks", lambda: {"BTC": 101.0})

    def fetch_state(wallet):
        calls["clearinghouse"] += 1
        return {"assetPositions": [{"position": {"coin": "BTC", "szi": "1", "entryPx": "100"}}]}

    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", fetch_state)

    opened, closed = mod._cycle(conn, now_ms=1_000_000)

    assert (opened, closed) == (0, 0)
    assert calls["clearinghouse"] == 1
    assert len(IS.get_open_copies(conn, target="0xoff")) == 1


def test_copy_cycle_closes_existing_copy_after_target_deactivated(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xoff", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xoff", active=False, ts_ms=1)
    IS.open_copy(conn, "0xoff", "BTC", "long", 100.0, ts_ms=1)
    calls = {"clearinghouse": 0}

    monkeypatch.setattr(mod, "_marks", lambda: {"BTC": 99.0})

    def fetch_state(wallet):
        calls["clearinghouse"] += 1
        return {"assetPositions": []}

    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", fetch_state)

    opened, closed = mod._cycle(conn, now_ms=1_000_000)

    assert (opened, closed) == (0, 1)
    assert calls["clearinghouse"] == 1
    assert IS.get_open_copies(conn, target="0xoff") == []


def test_copy_cycle_throttles_between_target_fetches(tmp_path, monkeypatch):
    # With many copy targets, a fetch_delay spreads the per-target clearinghouse fetches so the
    # mirror does not burst the HL API. Two eligible targets -> two fetches -> one delay between.
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    for w in ("0xaaa", "0xbbb"):
        IS.upsert_screened(conn, w, {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
        IS.upsert_tracked(conn, w, {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
        IS.set_copy_target(conn, w, active=True, copy_tier="watch_fallback", ts_ms=1)
    sleeps = []

    monkeypatch.setattr(mod, "_marks", lambda: {"BTC": 100.0})
    holdings = []
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state",
                        lambda wallet: {"assetPositions": holdings})
    patch_local(monkeypatch, "sleep", lambda s: sleeps.append(s))

    mod._cycle(conn, now_ms=999_000, fetch_delay=0.3)                    # baseline (both flat)
    holdings.append({"position": {"coin": "BTC", "szi": "1", "entryPx": "100"}})
    sleeps.clear()
    opened, closed = mod._cycle(conn, now_ms=1_000_000, fetch_delay=0.3)  # both open BTC (new)

    assert opened == 2
    assert sleeps == [0.3]


def test_arm_cycle_skips_legacy_book_until_slow_sweep(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    for wallet in ("0xarm", "0xlegacy"):
        IS.upsert_screened(conn, wallet, {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
        IS.upsert_tracked(conn, wallet, {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
        IS.set_copy_target(conn, wallet, active=True, copy_tier="watch_fallback", ts_ms=1)
    IS.open_copy(conn, "0xlegacy", "BTC", "long", 100.0, ts_ms=1)
    arm = {"targets_json": json.dumps(["0xarm"])}
    fetched = []
    monkeypatch.setattr(mod, "_marks", lambda: {"BTC": 100.0})
    monkeypatch.setattr(
        mod.HL, "fetch_clearinghouse_state",
        lambda wallet: fetched.append(wallet) or {"assetPositions": []},
    )

    mod._cycle(conn, now_ms=1_000_000, arm=arm, include_legacy=False)
    assert fetched == ["0xarm"]
    assert mod._LAST_CYCLE_STATS == {
        "targets_high": 1, "targets_legacy": 0, "targets_total": 1,
    }

    fetched.clear()
    mod._cycle(conn, now_ms=1_001_000, arm=arm, include_legacy=True)
    assert set(fetched) == {"0xarm", "0xlegacy"}
    assert mod._LAST_CYCLE_STATS["targets_legacy"] == 1


def test_copy_cycle_processes_proven_active_target(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xproven", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    _seed_proven_follows(conn, "0xproven")
    IS.set_copy_target(conn, "0xproven", active=True, ts_ms=1)

    monkeypatch.setattr(mod, "_marks", lambda: {"BTC": 100.0})
    holdings = []
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state",
                        lambda wallet: {"assetPositions": holdings})

    mod._cycle(conn, now_ms=40 * 86_400_000 - 60_000)                   # baseline: flat
    holdings.append({"position": {"coin": "BTC", "szi": "1", "entryPx": "100"}})   # new entry
    opened, closed = mod._cycle(conn, now_ms=40 * 86_400_000)

    assert (opened, closed) == (1, 0)
    opened_copy = IS.get_open_copies(conn, target="0xproven")[0]
    assert opened_copy["coin"] == "BTC"
    assert opened_copy["copy_tier"] == "proven"


def test_altshort_arm_records_entry_regime_benchmark_marks_and_close(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback", ts_ms=1)
    arm = IS.create_altshort_arm(
        conn,
        {"arm_id": "forward-v1", "policy": {"alt_short_only": True, "cost_bps": 39},
         "targets": ["0xwatch"], "taxonomy_version": "altshort-core-v1",
         "benchmark_version": "loo-equal-weight-v1", "regime_version": "causal-hourly-v1"},
        activated_ts_ms=1_000_000, code_sha="abc", created_ts_ms=999_000,
    )
    marks = {"AAA": 100.0, "BBB": 100.0, "BTC": 100.0}
    monkeypatch.setattr(mod, "_marks", lambda: dict(marks))
    monkeypatch.setattr(
        mod, "_funding", lambda coin, start_ms, end_ms: [{"time": 7_200_000, "rate": 0.0001}]
    )
    holdings = []
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", lambda wallet: {"assetPositions": holdings})
    policy = mod.CF.EntryPolicy(alt_short_only=True)

    mod._cycle(conn, now_ms=1_800_000, policy=policy, arm=arm)  # first-sight flat baseline
    holdings.append({"position": {"coin": "AAA", "szi": "-1", "entryPx": "100"}})
    marks.update({"AAA": 90.0, "BBB": 95.0})
    opened, closed = mod._cycle(conn, now_ms=3_700_000, policy=policy, arm=arm)
    assert (opened, closed) == (1, 0)
    obs = IS.list_altshort_observations(conn, arm_id="forward-v1")[0]
    assert obs["coin"] == "AAA" and obs["detect_ts_ms"] == 3_700_000
    regime = json.loads(obs["regime_json"])
    assert regime["decision_ts_ms"] == 3_700_000
    assert regime["label"] == "unknown"  # not enough trailing history yet
    assert json.loads(obs["benchmark_entry_json"]) == {"BBB": 95.0}

    marks.update({"AAA": 80.0, "BBB": 90.0})
    IS.record_altshort_regime_marks(
        conn, marks, funding_rates={"AAA": 0.0001, "BBB": 0.0001}, ts_ms=7_200_000
    )
    holdings.clear()
    opened, closed = mod._cycle(conn, now_ms=7_300_000, policy=policy, arm=arm)
    assert (opened, closed) == (0, 1)
    obs = IS.list_altshort_observations(conn, arm_id="forward-v1")[0]
    assert obs["status"] == "closed" and obs["latest_mark"] == 80.0
    assert obs["wallet_net_bps"] == pytest.approx(
        (90 - 80) / 90 * 10_000 - 39 + 1, abs=0.01
    )
    benchmark = json.loads(obs["benchmark_exit_json"])
    assert benchmark["covered_n"] == 1 and benchmark["coverage"] == 1.0
    assert benchmark["funding_coverage"] == 1.0
    assert benchmark["basket_net_bps"] == pytest.approx(
        (95 - 90) / 95 * 10_000 - 39 + 1
    )


def test_arm_manifest_activation_is_pinned_and_replay_safe(tmp_path):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    manifest = {
        "arm_id": "forward-v1", "code_sha": "abc",
        "policy": {"alt_short_only": True, "cost_bps": 39},
        "targets": ["0xabc"], "taxonomy_version": "altshort-core-v1",
        "benchmark_version": "loo-equal-weight-v1", "regime_version": "causal-hourly-v1",
    }
    path = tmp_path / "arm.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    arm = mod._load_or_create_arm(conn, path, code_sha="abc", dirty=False, now_ms=1000)
    replay = mod._load_or_create_arm(conn, path, code_sha="abc", dirty=False, now_ms=9999)
    assert arm["activated_ts_ms"] == replay["activated_ts_ms"] == 1000
    with pytest.raises(ValueError, match="code_sha_mismatch"):
        mod._load_or_create_arm(conn, path, code_sha="wrong", dirty=False, now_ms=1000)
    with pytest.raises(ValueError, match="dirty_tree"):
        mod._load_or_create_arm(conn, path, code_sha="abc", dirty=True, now_ms=1000)


def test_code_only_arm_version_reuses_semantic_evidence_anchor(tmp_path):
    mod = importlib.import_module("scripts.run_hl_copy")
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    manifest = {
        "arm_id": "forward-v1", "code_sha": "abc",
        "policy": {"alt_short_only": True, "cost_bps": 39},
        "targets": ["0xabc"], "taxonomy_version": "altshort-core-v1",
        "benchmark_version": "loo-equal-weight-v1", "regime_version": "causal-hourly-v1",
    }
    path = tmp_path / "arm.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    first = mod._load_or_create_arm(
        conn, path, code_sha="abc", dirty=False, now_ms=1000
    )
    manifest.update({"arm_id": "forward-v2", "code_sha": "def"})
    path.write_text(json.dumps(manifest), encoding="utf-8")

    second = mod._load_or_create_arm(
        conn, path, code_sha="def", dirty=False, now_ms=9999
    )

    assert first["arm_id"] == second["arm_id"] == "forward-v1"
    assert second["deployment_arm_id"] == "forward-v2"
    assert second["activated_ts_ms"] == 1000
    assert IS.compatible_altshort_arm_ids(conn, arm_id="forward-v1") == ["forward-v1"]
    heartbeat = mod._heartbeat_payload(
        now_ms=10_000, code_sha="def", dirty=False, arm=second,
        policy=mod.CF.EntryPolicy(alt_short_only=True), summary={"open": 0, "closed": 0},
    )
    assert heartbeat["deployment_arm_id"] == "forward-v2"
    assert heartbeat["evidence_arm_id"] == "forward-v1"
    assert heartbeat["depth_capture_format_version"] == 2
    assert heartbeat["depth_capture_scope"] == "provider_returned_snapshot"


def test_copy_runtime_code_pin_allows_evidence_only_commits(tmp_path):
    mod = importlib.import_module("scripts.run_hl_copy")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    code = tmp_path / "runner.py"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "runner.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "runtime"], cwd=tmp_path, check=True)
    pinned = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                            text=True, capture_output=True).stdout.strip()
    (tmp_path / "evidence.md").write_text("result\n", encoding="utf-8")
    subprocess.run(["git", "add", "evidence.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "evidence"], cwd=tmp_path, check=True)
    assert mod._git_state(root=tmp_path, pinned_code_sha=pinned,
                          code_paths=["runner.py"]) == (pinned, False)
    (tmp_path / "runtime.sqlite-wal").write_text("operational state\n", encoding="utf-8")
    assert mod._git_state(root=tmp_path, pinned_code_sha=pinned,
                          code_paths=["runner.py"]) == (pinned, False)
    (tmp_path / "evidence.md").write_text("unrelated tracked work\n", encoding="utf-8")
    assert mod._git_state(root=tmp_path, pinned_code_sha=pinned,
                          code_paths=["runner.py"]) == (pinned, False)
    code.write_text("VALUE = 2\n", encoding="utf-8")
    actual, dirty = mod._git_state(root=tmp_path, pinned_code_sha=pinned,
                                   code_paths=["runner.py"])
    assert actual != pinned and dirty is True


def test_provider_identity_manifest_starts_new_semantic_lineage():
    manifest = json.loads(Path(
        "data/research/altshort_study/forward_arm.v4.json"
    ).read_text())

    assert manifest["code_sha"] == "5eac493cfc81fdb2d29d02892b0825855f492029"
    assert manifest["taxonomy_version"] == (
        "altshort-exclude-btc-eth-sol-provider-exact-v2"
    )
    assert manifest["rollover_from"] == "wallet-copy-altshort-forward-v3-20260727"
    assert manifest["promotion_status"] == (
        "blocked_invalidated_historical_and_zero_forward_closes"
    )


def test_provider_identity_runtime_rollover_preserves_semantics():
    v4 = json.loads(Path(
        "data/research/altshort_study/forward_arm.v4.json"
    ).read_text())
    v5 = json.loads(Path(
        "data/research/altshort_study/forward_arm.v5.json"
    ).read_text())

    assert v5["code_sha"] == "4063ece062fd50dda4ea21f03cee053ab7e3b23c"
    assert v5["rollover_from"] == v4["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
    ):
        assert v5[field] == v4[field]


def test_runtime_hardening_rollover_preserves_evidence_semantics():
    v5 = json.loads(Path(
        "data/research/altshort_study/forward_arm.v5.json"
    ).read_text())
    v6 = json.loads(Path(
        "data/research/altshort_study/forward_arm.v6.json"
    ).read_text())

    assert v6["code_sha"] == "9a7878104e524e5f04dd67b381523eaed59410e3"
    assert v6["rollover_from"] == v5["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
    ):
        assert v6[field] == v5[field]


def test_audit_remediation_rollover_preserves_frozen_evidence_semantics():
    v6 = json.loads(Path("data/research/altshort_study/forward_arm.v6.json").read_text())
    v7 = json.loads(Path("data/research/altshort_study/forward_arm.v7.json").read_text())
    assert v7["code_sha"] == "560ced1a1e41427e8c21659ad18a5cb07800b07d"
    assert v7["rollover_from"] == v6["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
        "promotion_status", "retrospective", "survivorship_bias",
    ):
        assert v7[field] == v6[field]


def test_evidence_closure_rollover_preserves_frozen_copy_semantics():
    v7 = json.loads(Path("data/research/altshort_study/forward_arm.v7.json").read_text())
    v8 = json.loads(Path("data/research/altshort_study/forward_arm.v8.json").read_text())
    assert v8["code_sha"] == "795a4936103a1e7630ecd715ee908d9bdcfa2b8a"
    assert v8["rollover_from"] == v7["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
        "promotion_status", "retrospective", "survivorship_bias",
    ):
        assert v8[field] == v7[field]
def test_rate_limit_rollover_preserves_frozen_copy_semantics():
    v8 = json.loads(Path("data/research/altshort_study/forward_arm.v8.json").read_text())
    v9 = json.loads(Path("data/research/altshort_study/forward_arm.v9.json").read_text())
    assert v9["code_sha"] == "52c1941f61109e7c70b05232e38863e090342ffb"
    assert v9["rollover_from"] == v8["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
        "promotion_status", "retrospective", "survivorship_bias",
    ):
        assert v9[field] == v8[field]


def test_funding_time_rollover_preserves_frozen_copy_semantics():
    v9 = json.loads(Path("data/research/altshort_study/forward_arm.v9.json").read_text())
    v10 = json.loads(Path("data/research/altshort_study/forward_arm.v10.json").read_text())
    assert v10["code_sha"] == "25f50584c287a036d082373697ad652d43014630"
    assert v10["rollover_from"] == v9["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
        "promotion_status", "retrospective", "survivorship_bias",
    ):
        assert v10[field] == v9[field]


def test_partial_report_rollover_preserves_frozen_copy_semantics():
    v10 = json.loads(Path("data/research/altshort_study/forward_arm.v10.json").read_text())
    v11 = json.loads(Path("data/research/altshort_study/forward_arm.v11.json").read_text())
    assert v11["code_sha"] == "725ee380fcfb72e7637fd950bf3330f233d4dc6a"
    assert v11["rollover_from"] == v10["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
        "promotion_status", "retrospective", "survivorship_bias",
    ):
        assert v11[field] == v10[field]


def test_quiet_websocket_rollover_preserves_frozen_copy_semantics():
    v11 = json.loads(Path("data/research/altshort_study/forward_arm.v11.json").read_text())
    v12 = json.loads(Path("data/research/altshort_study/forward_arm.v12.json").read_text())
    assert v12["code_sha"] == "72f6b24f239b45d825ff7f25fbc5c171d0aaa6fd"
    assert v12["rollover_from"] == v11["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
        "promotion_status", "retrospective", "survivorship_bias",
    ):
        assert v12[field] == v11[field]


def test_operator_evidence_rollover_preserves_frozen_copy_semantics():
    mod = importlib.import_module("scripts.run_hl_copy")
    v13 = json.loads(Path("data/research/altshort_study/forward_arm.v13.json").read_text())
    v14 = json.loads(Path("data/research/altshort_study/forward_arm.v14.json").read_text())
    assert v14["code_sha"] == "c150f5f03f3fb0f614c80ec8dbb9230c44bdfd40"
    assert v14["rollover_from"] == v13["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
        "promotion_status", "retrospective", "survivorship_bias",
    ):
        assert v14[field] == v13[field]


FROZEN_COPY_PATHS = ["scripts/run_hl_copy.py", "sniper/hyperlive",
                     "sniper/markets/hyperliquid_api.py"]


def test_forward_vs_promotion_rollover_preserves_frozen_copy_semantics():
    """v15. `fe9831d8` split forward-test collection status from promotion status in
    edge_desk (operator display), which is inside the frozen copy paths, WITHOUT minting a
    rollover — so the arm would have refused to start on the next HL runtime deploy with
    code_sha_mismatch. v15 carries every copy-semantic field from v14 verbatim."""
    v14 = json.loads(Path("data/research/altshort_study/forward_arm.v14.json").read_text())
    v15 = json.loads(Path("data/research/altshort_study/forward_arm.v15.json").read_text())
    assert v15["code_sha"] == "fe9831d8581b515dcff1f9238fa3e456a8d981ae"
    assert v15["rollover_from"] == v14["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
        "promotion_status", "retrospective", "survivorship_bias",
    ):
        assert v15[field] == v14[field]
    # Still blocked — a rollover carries the cohort forward, it never promotes it.
    assert v15["promotion_status"].startswith("blocked_")


def test_active_arm_manifest_pins_the_current_frozen_code():
    """THE guard this whole lineage exists for: the manifest the watchdog actually runs must
    resolve to its own pin against today's tree. When it does not, the frozen code changed
    without a rollover and the arm will refuse to start."""
    mod = importlib.import_module("scripts.run_hl_copy")
    watchdog = Path("scripts/hl_copy_watchdog.sh").read_text()
    active = re.search(r"forward_arm\.(v\d+)\.json", watchdog).group(1)
    manifest = json.loads(
        Path(f"data/research/altshort_study/forward_arm.{active}.json").read_text())
    assert mod._git_state(
        pinned_code_sha=manifest["code_sha"], code_paths=FROZEN_COPY_PATHS,
    ) == (manifest["code_sha"], False), (
        f"{active} pins {manifest['code_sha'][:8]} but the frozen copy paths have moved — "
        "mint a rollover (carrying copy semantics verbatim) instead of repinning in place."
    )


def test_single_instance_lock_rollover_preserves_frozen_copy_semantics():
    """v16. `run_hl_copy.py` gained a process-lifetime single-instance lock (a893488d) — inside
    the frozen copy paths, so it needs a rollover like every other edit there. Startup safety
    only; every copy-semantic field carries verbatim from v15."""
    v15 = json.loads(Path("data/research/altshort_study/forward_arm.v15.json").read_text())
    v16 = json.loads(Path("data/research/altshort_study/forward_arm.v16.json").read_text())
    assert v16["code_sha"] == "a893488d9fa98d14ca05b50d0d59bfbe19c1f325"
    assert v16["rollover_from"] == v15["arm_id"]
    for field in (
        "policy", "targets", "taxonomy_version", "benchmark_version", "regime_version",
        "promotion_status", "retrospective", "survivorship_bias",
    ):
        assert v16[field] == v15[field]
    assert v16["promotion_status"].startswith("blocked_")
