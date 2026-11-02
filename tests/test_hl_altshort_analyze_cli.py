import importlib.util
from pathlib import Path


def _module():
    path = Path("data/research/altshort_study/analyze.py")
    spec = importlib.util.spec_from_file_location("hl_altshort_analyze", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_analysis_paths_are_explicit_and_isolated(tmp_path):
    module = _module()
    args = module.parse_args([
        "--cache-dir", str(tmp_path / "cache.v4-partial"),
        "--output-dir", str(tmp_path / "analysis.v4-partial"),
        "--db-path", str(tmp_path / "intel.sqlite"),
    ])

    assert args.cache_dir == tmp_path / "cache.v4-partial"
    assert args.output_dir == tmp_path / "analysis.v4-partial"
    assert args.db_path == tmp_path / "intel.sqlite"


def test_analysis_provenance_carries_partial_cache_seal(tmp_path):
    module = _module()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "_cache_seal.json").write_text(
        '{"evidence_status":"partial_censored",'
        '"delisted_tail_status":"censored_archive_declined"}\n', encoding="utf-8"
    )

    provenance = module.analysis_provenance(cache)

    assert provenance["evidence_status"] == "partial_censored"
    assert provenance["delisted_tail_status"] == "censored_archive_declined"
    assert len(provenance["cache_seal_sha256"]) == 64


def test_missing_funding_prints_censor_historical_trade():
    module = _module()
    funding = {"ALT": ([3_600_000], [0.0001])}
    assert module.funding_bps_for_hold(funding, "ALT", 0, 7_200_001) is None
    assert module.cost_adjusted_bps(1.0, 0.9, "ALT", 0, 7_200_001, funding)[0] is None


def test_all_censored_trades_keep_stable_empty_output_schema():
    module = _module()

    trades = module.gen_signal_trades({}, {}, 0.30, "runup_72")
    metrics = module.arm_metrics(trades)

    assert {"coin", "entry_t", "window", "net_bps", "funding_bps"} <= set(trades.columns)
    assert {"window", "n", "mean_bps", "total_bps"} <= set(metrics.columns)
    assert trades.empty and metrics.empty
