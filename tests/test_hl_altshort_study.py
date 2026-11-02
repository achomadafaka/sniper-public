import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import subprocess

import pytest


def _load_runner():
    spec = importlib.util.spec_from_file_location("hl_altshort_study", "scripts/hl_altshort_study.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hl_altshort_study"] = mod
    spec.loader.exec_module(mod)
    return mod


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path):
    candles = tmp_path / "cache"
    candles.mkdir()
    universe = tmp_path / "universe.json"
    universe.write_text(json.dumps(["AAA", "BBB", "CCC", "DDD"]), encoding="utf-8")
    events = tmp_path / "events.csv"
    with events.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["coin", "entry_t", "exit_t", "window"])
        writer.writeheader()
        writer.writerow({"coin": "AAA", "entry_t": 100, "exit_t": 250, "window": "W2"})
    for coin, prices in {"AAA": [100, 90], "BBB": [100, 95], "CCC": [100, 105]}.items():
        rows = [
            {"t": 0, "o": prices[0], "h": prices[0], "l": prices[0], "c": prices[0]},
            {"t": 200, "o": prices[0], "h": prices[0], "l": prices[0], "c": prices[0]},
            {"t": 300, "o": prices[1], "h": prices[1], "l": prices[1], "c": prices[1]},
        ]
        (candles / f"candles_{coin}.json").write_text(json.dumps(rows), encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "study_id": "fixture",
        "code_sha": "test-sha",
        "events_path": "events.csv",
        "universe_path": "universe.json",
        "candle_dir": "cache",
        "funding_dir": "cache",
        "inputs": [
            {"path": "events.csv", "sha256": _sha(events)},
            {"path": "universe.json", "sha256": _sha(universe)},
            {"path": "cache", "glob": "candles_*.json",
             "sha256_tree": None},
        ],
        "evaluation_windows": ["W2"],
        "window_bounds": {"W2": {"start_ms": 100, "end_ms": 300}},
        "historical_eligibility_lookback_ms": 0,
        "costs": {"round_trip_bps": 20},
        "portfolio": {"slots": 2, "initial_equity": 1000},
        "bootstrap": {"replications": 50, "seed": 7},
        "thresholds": {"min_coverage": 0.8, "min_day_clusters": 20},
        "survivorship_bias": True,
    }
    manifest["inputs"][2]["sha256_tree"] = None  # filled after runner import
    return manifest


def test_manifest_requires_identity_and_pinned_inputs(tmp_path):
    runner = _load_runner()
    with pytest.raises(ValueError, match="study_id"):
        runner.validate_manifest({"schema_version": 2}, input_root=tmp_path,
                                 actual_code_sha="x", dirty=False)

    manifest = _fixture(tmp_path)
    manifest["inputs"][2]["sha256_tree"] = runner.sha256_tree(tmp_path / "cache", "candles_*.json")
    assert runner.validate_manifest(manifest, input_root=tmp_path,
                                    actual_code_sha="test-sha", dirty=False) == []
    manifest["inputs"][0]["sha256"] = "0" * 64
    assert "hash_mismatch:events.csv" in runner.validate_manifest(
        manifest, input_root=tmp_path, actual_code_sha="test-sha", dirty=False
    )


def test_dirty_or_wrong_code_blocks_verdict(tmp_path):
    runner = _load_runner()
    manifest = _fixture(tmp_path)
    manifest["inputs"][2]["sha256_tree"] = runner.sha256_tree(tmp_path / "cache", "candles_*.json")
    blockers = runner.validate_manifest(manifest, input_root=tmp_path,
                                        actual_code_sha="wrong", dirty=True)
    assert blockers == ["code_sha_mismatch", "unversioned_code"]


def test_fixture_run_is_deterministic_and_uses_next_bar(tmp_path):
    runner = _load_runner()
    manifest = _fixture(tmp_path)
    manifest["inputs"][2]["sha256_tree"] = runner.sha256_tree(tmp_path / "cache", "candles_*.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "out"
    first = runner.run_study(manifest_path, input_root=tmp_path, output_dir=out,
                             actual_code_sha="test-sha", dirty=False)
    first_bytes = {path.name: path.read_bytes() for path in out.iterdir()}
    second = runner.run_study(manifest_path, input_root=tmp_path, output_dir=out,
                              actual_code_sha="test-sha", dirty=False)
    second_bytes = {path.name: path.read_bytes() for path in out.iterdir()}
    assert first == second
    assert first_bytes == second_bytes
    assert first["events"]["valid_n"] == 1
    assert first["events"]["mean_signal_net_bps"] == pytest.approx(980.0)
    assert first["events"]["mean_basket_net_bps"] == pytest.approx(-20.0)
    assert first["events"]["mean_residual_bps"] == pytest.approx(1000.0)
    assert first["passive_alt_beta"]["valid"] is True
    assert "equity_curve" not in first["passive_alt_beta"]
    assert "equity_curve" not in first["portfolio"]
    assert "accepted_ids" not in first["portfolio"]
    assert first["verdict"]["status"] == "paper_continue"


def test_study_loads_and_emits_exact_provider_symbol_case(tmp_path):
    runner = _load_runner()
    manifest = _fixture(tmp_path)
    universe_path = tmp_path / "universe.json"
    universe = json.loads(universe_path.read_text())
    universe[universe.index("AAA")] = "kPEPE"
    universe_path.write_text(json.dumps(universe), encoding="utf-8")
    (tmp_path / "cache" / "candles_AAA.json").rename(
        tmp_path / "cache" / "candles_kPEPE.json"
    )
    events_path = tmp_path / "events.csv"
    events = events_path.read_text(encoding="utf-8").replace("AAA,", "kPEPE,")
    events_path.write_text(events, encoding="utf-8")
    manifest["inputs"][0]["sha256"] = _sha(events_path)
    manifest["inputs"][1]["sha256"] = _sha(universe_path)
    manifest["inputs"][2]["sha256_tree"] = runner.sha256_tree(
        tmp_path / "cache", "candles_*.json"
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    runner.run_study(
        manifest_path, input_root=tmp_path, output_dir=tmp_path / "out",
        actual_code_sha="test-sha", dirty=False,
    )

    rows = list(csv.DictReader((tmp_path / "out" / "events.csv").open()))
    assert rows[0]["coin"] == "kPEPE"


def test_partial_censored_cache_seal_can_never_emit_edge_verdict(tmp_path):
    runner = _load_runner()
    manifest = _fixture(tmp_path)
    manifest["inputs"][2]["sha256_tree"] = runner.sha256_tree(
        tmp_path / "cache", "candles_*.json"
    )
    seal = tmp_path / "cache" / "_cache_seal.json"
    universe_path = tmp_path / "universe.json"
    symbols = []
    for coin in json.loads(universe_path.read_text()):
        row = {"provider_symbol": coin}
        for kind in ("candles", "funding"):
            path = tmp_path / "cache" / f"{kind}_{coin}.json"
            row[kind] = {
                "path": path.name,
                "status": "present" if path.exists() else "missing",
                "sha256": _sha(path) if path.exists() else None,
            }
        symbols.append(row)
    seal.write_text(json.dumps({
        "schema_version": 2,
        "evidence_status": "partial_censored",
        "delisted_tail_status": "censored_archive_declined",
        "universe_sha256": _sha(universe_path),
        "symbols": symbols,
    }), encoding="utf-8")
    manifest["cache_seal_path"] = "cache/_cache_seal.json"
    manifest["inputs"].append({"path": manifest["cache_seal_path"], "sha256": _sha(seal)})
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = runner.run_study(
        manifest_path, input_root=tmp_path, output_dir=tmp_path / "out",
        actual_code_sha="test-sha", dirty=False,
    )

    assert report["evidence_status"] == "partial_censored"
    assert report["verdict"]["status"] == "partial_censored"
    assert report["verdict"]["blocked"] == ["censored_delisted_tail"]
    assert report["provenance"]["cache_seal_sha256"] == _sha(seal)


def test_cache_seal_detects_artifact_tampering_even_if_manifest_tree_is_rehashed(tmp_path):
    runner = _load_runner()
    manifest = _fixture(tmp_path)
    universe_path = tmp_path / "universe.json"
    symbols = []
    for coin in json.loads(universe_path.read_text()):
        row = {"provider_symbol": coin}
        for kind in ("candles", "funding"):
            path = tmp_path / "cache" / f"{kind}_{coin}.json"
            row[kind] = {"path": path.name, "status": "present" if path.exists() else "missing",
                         "sha256": _sha(path) if path.exists() else None}
        symbols.append(row)
    seal = {"schema_version": 2, "universe_sha256": _sha(universe_path), "symbols": symbols}
    target = tmp_path / "cache" / "candles_AAA.json"
    target.write_text("[]", encoding="utf-8")
    assert "cache_seal_hash_mismatch:AAA:candles" in runner.validate_cache_seal(
        seal, universe_path=universe_path, cache_dir=tmp_path / "cache"
    )


def test_sealed_altshort_reports_have_machine_readable_symbol_invalidation():
    invalidation = json.loads(Path(
        "data/research/altshort_study/INVALIDATION.symbol-identity.json"
    ).read_text())

    assert invalidation["status"] == "invalidated_provider_symbol_identity"
    for item in invalidation["invalidates"]:
        assert item["report_sha256"] == _sha(Path(item["report_path"]))
    assert invalidation["affected_provider_symbols"] == [
        "kBONK", "kDOGS", "kFLOKI", "kLUNC", "kNEIRO", "kPEPE", "kSHIB"
    ]


def test_runner_has_no_network_client():
    source = Path("scripts/hl_altshort_study.py").read_text(encoding="utf-8")
    assert "urllib" not in source
    assert "requests" not in source


def test_study_pin_covers_every_imported_altshort_mechanics_module():
    runner = _load_runner()
    assert "sniper/hyperlive/altshort_evidence.py" in runner.STUDY_CODE_PATHS
    assert "sniper/hyperlive/altshort_signal_paper.py" in runner.STUDY_CODE_PATHS
    assert "sniper/hyperlive/altshort_universe.py" in runner.STUDY_CODE_PATHS


def test_pinned_code_sha_survives_docs_only_commit(tmp_path):
    runner = _load_runner()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    code = tmp_path / "code.py"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "code.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "code"], cwd=tmp_path, check=True)
    pinned = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                            text=True, capture_output=True).stdout.strip()
    (tmp_path / "notes.md").write_text("docs\n", encoding="utf-8")
    subprocess.run(["git", "add", "notes.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "docs"], cwd=tmp_path, check=True)
    assert runner._git_state(tmp_path, pinned_code_sha=pinned, code_paths=["code.py"]) == (
        pinned, False
    )
    (tmp_path / "cache.json").write_text("{}\n", encoding="utf-8")
    assert runner._git_state(tmp_path, pinned_code_sha=pinned, code_paths=["code.py"]) == (
        pinned, False
    )
    code.write_text("VALUE = 2\n", encoding="utf-8")
    actual, dirty = runner._git_state(tmp_path, pinned_code_sha=pinned, code_paths=["code.py"])
    assert actual != pinned and dirty is True


def test_pinned_code_sha_ignores_unrelated_tracked_dirty_file(tmp_path):
    runner = _load_runner()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    code = tmp_path / "code.py"
    service = tmp_path / "service.state"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    service.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "code.py", "service.state"], cwd=tmp_path, check=True)
    subprocess.run([
        "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
        "commit", "-qm", "baseline",
    ], cwd=tmp_path, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
        text=True, capture_output=True,
    ).stdout.strip()

    service.write_text("runtime update\n", encoding="utf-8")

    assert runner._git_state(
        tmp_path, pinned_code_sha=pinned, code_paths=["code.py"]
    ) == (pinned, False)


def test_hourly_close_bridge_uses_observable_exit_and_matched_basket_clock():
    runner = _load_runner()
    hour = 3_600_000
    bars = {
        "AAA": [
            {"ts_ms": hour, "open": 100, "high": 100, "low": 100, "close": 100},
            {"ts_ms": 2 * hour, "open": 90, "high": 90, "low": 84, "close": 84},
        ],
        "BBB": [
            {"ts_ms": hour, "open": 100, "high": 100, "low": 100, "close": 100},
            {"ts_ms": 2 * hour, "open": 96, "high": 96, "low": 95, "close": 95},
        ],
    }
    result = runner.hourly_close_bridge_event(
        {"coin": "AAA", "decision_ts_ms": 0}, bars,
        universe=["AAA", "BBB"], funding_by_coin={},
        costs=runner.CostModel(round_trip_bps=39), min_coverage=1.0,
    )
    assert result["valid"] is True
    assert result["exit_reason"] == "take_profit"
    assert result["exit_observation_ts_ms"] == 2 * hour
    assert result["signal_net_bps"] == pytest.approx(1500 - 39)
    assert result["basket_net_bps"] == pytest.approx(500 - 39)
    assert result["residual_bps"] == pytest.approx(1000)


def test_hourly_close_bridge_stop_precedes_cap_and_cap_uses_close():
    runner = _load_runner()
    hour = 3_600_000
    stop_bars = {
        coin: [
            {"ts_ms": hour, "open": 100, "high": 100, "low": 100, "close": 100},
            {"ts_ms": 2 * hour, "open": 110, "high": close, "low": close, "close": close},
        ]
        for coin, close in {"AAA": 121, "BBB": 105}.items()
    }
    stopped = runner.hourly_close_bridge_event(
        {"coin": "AAA", "decision_ts_ms": 0}, stop_bars,
        universe=["AAA", "BBB"], funding_by_coin={},
        costs=runner.CostModel(round_trip_bps=39), min_coverage=1.0,
    )
    assert stopped["exit_reason"] == "stop"
    assert stopped["signal_net_bps"] == pytest.approx(-2000 - 39)

    cap_bars = {}
    for coin in ("AAA", "BBB"):
        cap_bars[coin] = [
            {"ts_ms": offset * hour, "open": 100, "high": 100, "low": 100,
             "close": 95 if offset == 9 else 100}
            for offset in range(1, 10)
        ]
    capped = runner.hourly_close_bridge_event(
        {"coin": "AAA", "decision_ts_ms": 0}, cap_bars,
        universe=["AAA", "BBB"], funding_by_coin={},
        costs=runner.CostModel(round_trip_bps=39), min_coverage=1.0,
    )
    assert capped["exit_reason"] == "time_cap"
    assert capped["exit_observation_ts_ms"] == 9 * hour
    assert capped["signal_net_bps"] == pytest.approx(500 - 39)
