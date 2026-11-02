from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts.research import hl_rollover_backtest as runner


POLICY = {
    "policy_version": "rollover-v1",
    "excluded_coins": ["BTC", "ETH", "SOL"],
    "min_history_hours": 720,
    "min_history_coverage": 0.8,
    "return_percentile_min": 0.95,
    "runup_percentile_min": 0.95,
    "rollover_6h_bps_max": 0.0,
    "max_entries_per_24h": 5,
    "take_profit_bps": 200.0,
    "cap_hours": 96,
    "fee_bps": 9.0,
    "slippage_each_bps": 15.0,
    "min_benchmark_coverage": 0.8,
    "bootstrap_replications": 5000,
    "bootstrap_seed": 20260727,
}
SELECTIVE_PREREG = {
    "prereg_version": "selective-rollover-forward-v1",
    "status": "frozen_forward_only",
    "base_policy_sha256": "322225bdc0bc7d30e744f6b3c75e636e46209e66a03d072b60871d61ccf5c0c6",
    "historical_data_cutoff_ts_ms": 1785067200000,
    "forward_start_ts_ms": 1785070800000,
    "rule": {
        "return_1h_bps_gt": -60.0,
        "return_12h_bps_lte": 350.0,
        "volume_ratio_24h_lte": 1.2,
    },
    "nomination": {
        "max_per_rolling_24h": 5,
        "replacement": "forbidden",
        "rejection_state": "shadow_until_200bps_short_profit_or_96h_cap",
    },
    "exit": {
        "take_profit_bps": 200.0,
        "cap_hours": 96,
        "stop_loss_bps": None,
        "sample_clock": "observed_marks_max_300_seconds",
    },
    "forward_evidence": {
        "min_closed_events": 200,
        "min_distinct_episodes": 2,
        "episode_separation_no_nomination_hours": 168,
        "min_closed_per_episode": 30,
        "positive_residual_each_episode": True,
        "leave_one_episode_out_residual_positive": True,
        "residual_day_block_ci_low_gt_bps": 0.0,
    },
    "coverage": {
        "historical_universe_min": 0.95,
        "decision_feed_min": 0.90,
        "required_decision_feeds": [
            "open_interest",
            "liquidation_volume",
            "spot_perp_basis",
            "orderbook_replenishment",
        ],
    },
    "risk": {
        "status": "required_not_defined",
        "required_metrics": [
            "expected_shortfall_95_bps",
            "maximum_drawdown_bps",
            "worst_loss_bps",
            "stop_through_p99_bps",
            "liquidation_distance_min_bps",
            "concurrent_exposure_peak",
        ],
    },
    "promotion_authority": "none_research_review_only",
}
HOUR_MS = 3_600_000


def _candles(*, coin_n=21, hours=830, decision=721):
    times = np.arange(hours, dtype=np.int64) * HOUR_MS
    candles = {}
    for index in range(coin_n):
        coin = f"C{index:03d}"
        close = np.full(hours, 100.0)
        open_ = close.copy()
        high = close.copy()
        low = close.copy()
        volume = np.full(hours, 1000.0)
        if coin == "C000":
            close[decision - 6:decision] = 103.0
            close[decision] = 102.5
            open_[decision + 1] = close[decision]
            close[decision + 1] = 101.5
            close[decision + 2] = 99.0
            high = np.maximum(open_, close)
            low = np.minimum(open_, close)
        candles[coin] = pd.DataFrame(
            {"t": times, "o": open_, "h": high, "l": low, "c": close, "v": volume}
        )
    return candles


def _funding(candles, rate=0.00001):
    return {
        coin: pd.DataFrame({"time": frame["t"] + 85, "rate": rate})
        for coin, frame in candles.items()
    }


def _write_fixture_cache(root: Path) -> Path:
    root.mkdir()
    candles = _candles()
    funding = _funding(candles)
    (root / "_universe.json").write_text(
        json.dumps(sorted(candles)) + "\n", encoding="utf-8"
    )
    (root / "_cache_identity.json").write_text(json.dumps({
        "schema_version": 1,
        "source": "metaAndAssetCtxs.universe.name",
        "symbol_identity": "provider_exact_case_sensitive",
        "universe_sha256": runner._sha256(root / "_universe.json"),
    }) + "\n", encoding="utf-8")
    for coin, frame in candles.items():
        (root / f"candles_{coin}.json").write_text(
            json.dumps(frame.to_dict("records"), separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (root / f"funding_{coin}.json").write_text(
            json.dumps(funding[coin].to_dict("records"), separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    return root


def _reseal_cache_identity(root: Path) -> None:
    identity_path = root / "_cache_identity.json"
    identity = json.loads(identity_path.read_text())
    identity["universe_sha256"] = runner._sha256(root / "_universe.json")
    identity_path.write_text(json.dumps(identity) + "\n")


def _write_policy(path: Path) -> Path:
    path.write_text(json.dumps(POLICY) + "\n", encoding="utf-8")
    return path


def test_runner_writes_deterministic_cache_only_report(tmp_path, monkeypatch):
    cache = _write_fixture_cache(tmp_path / "cache")
    policy = _write_policy(tmp_path / "policy.json")
    output = tmp_path / "result"
    monkeypatch.setattr(
        runner,
        "research_git_state",
        lambda: {"code_sha": "a" * 40, "dirty": False, "dirty_paths": []},
    )

    result = runner.run(
        cache_dir=cache,
        policy_path=policy,
        output_dir=output,
    )

    assert result["report"]["overall"]["n"] == 1
    assert result["report"]["provenance_valid"] is True
    assert result["report"]["older_history"] == "blocked_paid_archive"
    assert result["report"]["verdict"]["status"] == "candidate_reject"
    assert "rollover_incremental_value" in result["report"]["verdict"]["failed"]
    hypothesis = result["report"]["exploratory_next_hypothesis"]
    assert hypothesis["status"] == "posthoc_not_validated"
    assert hypothesis["inference_status"] == "invalid_post_selection"
    assert hypothesis["headline_allowed"] is False
    assert hypothesis["execution_after_nomination"]["overall"]["n"] == 1
    assert hypothesis["replacement_control"]["n"] == 1
    assert hypothesis["forward_prereg"]["status"] == "blocked_prereg_only"
    assert hypothesis["forward_prereg"]["eligible_for_review"] is False
    assert "risk_envelope_undefined" in hypothesis["forward_prereg"]["blockers"]
    assert "independent_forward_sample_missing" in hypothesis["forward_prereg"]["blockers"]
    assert len(hypothesis["exit_sensitivity"]) == 9
    assert hypothesis["exit_sensitivity"][0]["cap_hours"] == 12
    assert hypothesis["exit_sensitivity"][0]["stop_loss_bps"] is None
    assert hypothesis["rule"] == {
        "return_1h_bps_gt": -60.0,
        "return_12h_bps_lte": 350.0,
        "volume_ratio_24h_lte": 1.2,
    }
    assert sorted(path.name for path in output.iterdir()) == [
        "events.csv",
        "feature_falsification.csv",
        "periods.csv",
        "regimes.csv",
        "report.json",
        "run_meta.json",
    ]
    meta = json.loads((output / "run_meta.json").read_text())
    assert meta["policy_sha256"] == "322225bdc0bc7d30e744f6b3c75e636e46209e66a03d072b60871d61ccf5c0c6"
    assert meta["input_hashes"]["_universe.json"]
    assert meta["network_used"] is False
    assert meta["selective_prereg_sha256"]
    assert b"\r\n" not in (output / "events.csv").read_bytes()


def test_selective_prereg_v1_rejects_mutation(tmp_path):
    path = tmp_path / "selective.json"
    path.write_text(json.dumps(SELECTIVE_PREREG) + "\n")

    loaded, digest = runner._load_selective_prereg(path)

    assert loaded == SELECTIVE_PREREG
    assert len(digest) == 64
    changed = {**SELECTIVE_PREREG, "forward_start_ts_ms": 1785074400000}
    path.write_text(json.dumps(changed) + "\n")
    with pytest.raises(ValueError, match="frozen selective prereg"):
        runner._load_selective_prereg(path)


def test_runner_refuses_output_collision_and_dirty_research_code(tmp_path, monkeypatch):
    cache = _write_fixture_cache(tmp_path / "cache")
    policy = _write_policy(tmp_path / "policy.json")
    output = tmp_path / "result"
    output.mkdir()
    monkeypatch.setattr(
        runner,
        "research_git_state",
        lambda: {"code_sha": "b" * 40, "dirty": True, "dirty_paths": ["runner.py"]},
    )

    with pytest.raises(FileExistsError):
        runner.run(cache_dir=cache, policy_path=policy, output_dir=output)

    with pytest.raises(ValueError, match="dirty research code"):
        runner.run(
            cache_dir=cache,
            policy_path=policy,
            output_dir=tmp_path / "clean-output",
        )


def test_runner_records_missing_coin_files_instead_of_silently_dropping(tmp_path, monkeypatch):
    cache = _write_fixture_cache(tmp_path / "cache")
    universe = json.loads((cache / "_universe.json").read_text())
    universe.append("MISSING")
    (cache / "_universe.json").write_text(json.dumps(universe) + "\n")
    _reseal_cache_identity(cache)
    policy = _write_policy(tmp_path / "policy.json")
    monkeypatch.setattr(
        runner,
        "research_git_state",
        lambda: {"code_sha": "c" * 40, "dirty": False, "dirty_paths": []},
    )

    result = runner.run(
        cache_dir=cache,
        policy_path=policy,
        output_dir=tmp_path / "result",
    )

    assert result["run_meta"]["missing_candle_coins"] == ["MISSING"]
    assert "MISSING" not in result["run_meta"]["loaded_coins"]


def test_cache_and_frames_preserve_provider_case_for_rebased_tokens(tmp_path):
    cache = _write_fixture_cache(tmp_path / "cache")
    universe = json.loads((cache / "_universe.json").read_text())
    universe[universe.index("C000")] = "kPEPE"
    (cache / "_universe.json").write_text(json.dumps(universe) + "\n")
    _reseal_cache_identity(cache)
    (cache / "candles_C000.json").rename(cache / "candles_kPEPE.json")
    (cache / "funding_C000.json").rename(cache / "funding_kPEPE.json")
    policy = runner._load_policy(_write_policy(tmp_path / "policy.json"))

    candles, funding, meta = runner._load_cache(cache, policy)
    frames = runner.build_causal_frames(candles, policy)

    assert "kPEPE" in candles and "KPEPE" not in candles
    assert "kPEPE" in funding and "kPEPE" in frames.coins
    assert "kPEPE" in meta["loaded_coins"]


def test_cache_rejects_case_colliding_provider_symbols(tmp_path):
    cache = _write_fixture_cache(tmp_path / "cache")
    universe = json.loads((cache / "_universe.json").read_text())
    universe.extend(["kPEPE", "KPEPE"])
    (cache / "_universe.json").write_text(json.dumps(universe) + "\n")
    _reseal_cache_identity(cache)
    policy = runner._load_policy(_write_policy(tmp_path / "policy.json"))

    with pytest.raises(ValueError, match="ambiguous provider coin identity: KPEPE"):
        runner._load_cache(cache, policy)


def test_v2_cache_requires_sealed_provider_identity_manifest(tmp_path):
    cache = _write_fixture_cache(tmp_path / "cache")
    policy = runner._load_policy(_write_policy(tmp_path / "policy.json"))
    (cache / "_cache_identity.json").unlink()

    with pytest.raises(ValueError, match="missing provider cache identity"):
        runner._load_cache(cache, policy, require_provider_identity=True)

    identity = {
        "schema_version": 1,
        "source": "metaAndAssetCtxs.universe.name",
        "symbol_identity": "provider_exact_case_sensitive",
        "universe_sha256": runner._sha256(cache / "_universe.json"),
    }
    (cache / "_cache_identity.json").write_text(json.dumps(identity) + "\n")
    _candles_loaded, _funding_loaded, meta = runner._load_cache(
        cache, policy, require_provider_identity=True
    )
    assert meta["provider_symbol_identity_valid"] is True

    identity["universe_sha256"] = "0" * 64
    (cache / "_cache_identity.json").write_text(json.dumps(identity) + "\n")
    with pytest.raises(ValueError, match="provider cache universe hash mismatch"):
        runner._load_cache(cache, policy, require_provider_identity=True)


def test_selective_prereg_v2_replaces_impossible_historical_percentage_gate():
    prereg, digest = runner._load_selective_prereg(
        Path("data/research/hl_rollover_backtest/selective_prereg.v2.json")
    )

    assert len(digest) == 64
    assert prereg["prereg_version"] == "selective-rollover-forward-v2"
    assert "historical_universe_min" not in prereg["coverage"]
    assert prereg["coverage"]["forward_tradeable_universe_min"] == 0.95
    assert prereg["historical_tail"]["status"] == "unbounded_required_blocker"
    assert prereg["decision_features"]["required_proxies"] == [
        "impact_spread_depth_proxy",
        "oi_collapse_adverse_move_proxy",
    ]
    assert prereg["decision_features"]["unavailable_direct_feeds"] == [
        "liquidation_volume",
        "orderbook_replenishment",
    ]


def test_v2_forward_gate_blocks_on_tail_and_missing_satisfiable_tape_features():
    prereg, digest = runner._load_selective_prereg(
        Path("data/research/hl_rollover_backtest/selective_prereg.v2.json")
    )
    report = runner._forward_prereg_report(
        prereg,
        digest,
        {"historical_universe_coverage": 192 / 229, "max_candle_ts_ms": 1},
        [],
    )

    assert "historical_delisted_tail_unbounded" in report["blockers"]
    assert "historical_survivorship_unbounded" not in report["blockers"]
    assert "decision_feature_missing:impact_spread_depth_proxy" in report["blockers"]
    assert "decision_feed_missing:liquidation_volume" not in report["blockers"]


def test_sealed_v1_report_has_machine_readable_invalidation():
    path = Path("data/research/hl_rollover_backtest/INVALIDATION.v1.json")
    invalidation = json.loads(path.read_text())
    report_path = Path(invalidation["invalidates"]["report_path"])

    assert invalidation["status"] == "invalidated_provider_symbol_identity"
    assert invalidation["invalidates"]["report_sha256"] == runner._sha256(report_path)
    assert invalidation["coverage"] == {
        "loaded_before_fix_n": 185,
        "provider_case_recoverable_n": 7,
        "provider_case_recovered_target_n": 192,
        "official_endpoint_delisted_unrecoverable_n": 37,
        "tradeable_nonmajor_universe_n": 229,
    }
    assert invalidation["rerun_policy"] == "new_output_only_never_overwrite_v1"


def test_runner_refuses_post_freeze_candles(tmp_path, monkeypatch):
    cache = _write_fixture_cache(tmp_path / "cache")
    candle_path = cache / "candles_C000.json"
    rows = json.loads(candle_path.read_text())
    rows[-1]["t"] = SELECTIVE_PREREG["forward_start_ts_ms"]
    candle_path.write_text(json.dumps(rows) + "\n")
    policy = _write_policy(tmp_path / "policy.json")
    monkeypatch.setattr(
        runner,
        "research_git_state",
        lambda: {"code_sha": "d" * 40, "dirty": False, "dirty_paths": []},
    )

    with pytest.raises(ValueError, match="post-freeze candle"):
        runner.run(
            cache_dir=cache,
            policy_path=policy,
            output_dir=tmp_path / "result",
        )
