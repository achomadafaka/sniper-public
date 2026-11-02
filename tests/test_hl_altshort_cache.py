import importlib.util
import hashlib
import json
from pathlib import Path
import sys


def _runner():
    spec = importlib.util.spec_from_file_location(
        "cache_hl_altshort_history", "scripts/research/cache_hl_altshort_history.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cache_uses_exact_provider_symbol_for_requests_and_file_identity(tmp_path):
    runner = _runner()
    calls = []

    def candles(coin, interval, start_ms, end_ms):
        calls.append(("candles", coin, interval, start_ms, end_ms))
        return [{"t": start_ms, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]

    def funding(coin, start_ms, end_ms):
        calls.append(("funding", coin, start_ms, end_ms))
        return [{"time": start_ms, "rate": 0.0001}]

    result = runner.cache_history(
        cache_dir=tmp_path,
        start_ms=100,
        end_ms=200,
        fetch_provider_marks=lambda: {"kPEPE": 1.0, "KAS": 2.0},
        fetch_candles=candles,
        fetch_funding=funding,
        sleep=lambda _seconds: None,
    )

    assert json.loads((tmp_path / "_universe.json").read_text()) == ["KAS", "kPEPE"]
    identity = json.loads((tmp_path / "_cache_identity.json").read_text())
    assert identity == {
        "schema_version": 1,
        "source": "metaAndAssetCtxs.universe.name",
        "symbol_identity": "provider_exact_case_sensitive",
        "universe_sha256": hashlib.sha256(
            (tmp_path / "_universe.json").read_bytes()
        ).hexdigest(),
    }
    assert (tmp_path / "candles_kPEPE.json").is_file()
    assert (tmp_path / "funding_kPEPE.json").is_file()
    assert not (tmp_path / "candles_KPEPE.json").exists()
    assert ("candles", "kPEPE", "1h", 100, 200) in calls
    assert ("funding", "kPEPE", 100, 200) in calls
    assert result == {
        "universe_n": 2, "candles_written": 2, "funding_written": 2,
        "files_reused": 0, "evidence_status": "partial_censored",
    }


def test_cache_resume_refetches_invalid_existing_provider_cased_files(tmp_path):
    runner = _runner()
    (tmp_path / "candles_kPEPE.json").write_text("[]\n")
    (tmp_path / "funding_kPEPE.json").write_text("[]\n")

    result = runner.cache_history(
        cache_dir=tmp_path,
        start_ms=100,
        end_ms=200,
        fetch_provider_marks=lambda: {"kPEPE": 1.0},
        fetch_candles=lambda *_a: [{"t": 200, "o": 1, "h": 1, "l": 1, "c": 1}],
        fetch_funding=lambda *_a: [{"time": 200, "rate": 0.1}],
        sleep=lambda _seconds: None,
    )

    assert result == {
        "universe_n": 1, "candles_written": 1, "funding_written": 1,
        "files_reused": 0, "evidence_status": "partial_censored",
    }


def test_sealed_cache_refuses_mutation(tmp_path):
    runner = _runner()
    (tmp_path / "_cache_seal.json").write_text("{}\n")
    try:
        runner.cache_history(cache_dir=tmp_path, start_ms=100, end_ms=200,
                             fetch_provider_marks=lambda: {"KAS": 1}, sleep=lambda _: None)
    except FileExistsError as exc:
        assert "sealed" in str(exc)
    else:
        raise AssertionError("sealed cache was mutated")


def test_cache_resume_refetches_artifact_that_does_not_reach_study_tail(tmp_path):
    runner = _runner()
    (tmp_path / "candles_KAS.json").write_text('[{"t":100}]\n')
    (tmp_path / "funding_KAS.json").write_text('[{"time":100}]\n')

    result = runner.cache_history(
        cache_dir=tmp_path,
        start_ms=100,
        end_ms=runner.MAX_TAIL_LAG_MS + 200,
        fetch_provider_marks=lambda: {"KAS": 1.0},
        fetch_candles=lambda *_a: [{"t": runner.MAX_TAIL_LAG_MS + 200}],
        fetch_funding=lambda *_a: [{"time": runner.MAX_TAIL_LAG_MS + 200}],
        sleep=lambda _seconds: None,
    )

    assert result["candles_written"] == 1
    assert result["funding_written"] == 1


def test_cache_accepts_provider_tail_within_six_hour_snapshot_lag(tmp_path):
    runner = _runner()
    end_ms = 1_000_000_000
    artifact = tmp_path / "candles_KAS.json"
    artifact.write_text(json.dumps([{"t": end_ms - 3 * 60 * 60 * 1000}]) + "\n")

    assert runner._valid_artifact(
        artifact, timestamp_field="t", start_ms=100, end_ms=end_ms
    )


def test_cache_seal_is_partial_censored_and_hashes_every_provider_exact_file(tmp_path):
    runner = _runner()

    result = runner.cache_history(
        cache_dir=tmp_path,
        start_ms=100,
        end_ms=200,
        fetch_provider_marks=lambda: {"KAS": 2.0, "kPEPE": 1.0},
        fetch_candles=lambda coin, *_a: [
            {"t": 100, "o": 1, "h": 2, "l": 1, "c": 2, "v": 3}
        ],
        fetch_funding=lambda coin, *_a: [{"time": 150, "rate": 0.0001}],
        sleep=lambda _seconds: None,
    )

    seal = json.loads((tmp_path / "_cache_seal.json").read_text())
    assert seal["schema_version"] == 2
    assert seal["evidence_status"] == "partial_censored"
    assert seal["delisted_tail_status"] == "censored_archive_declined"
    assert seal["study_start_ms"] == 100
    assert seal["study_end_ms"] == 200
    assert [row["provider_symbol"] for row in seal["symbols"]] == ["KAS", "kPEPE"]
    for row in seal["symbols"]:
        assert row["candles"]["sha256"] == hashlib.sha256(
            (tmp_path / row["candles"]["path"]).read_bytes()
        ).hexdigest()
        assert row["funding"]["sha256"] == hashlib.sha256(
            (tmp_path / row["funding"]["path"]).read_bytes()
        ).hexdigest()
        assert row["candles"]["row_count"] == 1
        assert row["funding"]["row_count"] == 1
    assert result["evidence_status"] == "partial_censored"


def test_cache_reuses_only_byte_identical_provider_symbols(tmp_path):
    runner = _runner()
    legacy = tmp_path / "legacy"
    target = tmp_path / "target"
    legacy.mkdir()
    (legacy / "candles_KAS.json").write_text('[{"t":100,"o":1,"h":1,"l":1,"c":1}]\n')
    (legacy / "funding_KAS.json").write_text('[{"time":150,"rate":0.1}]\n')
    (legacy / "candles_KPEPE.json").write_text('[{"t":100,"o":9,"h":9,"l":9,"c":9}]\n')
    (legacy / "funding_KPEPE.json").write_text('[{"time":150,"rate":0.9}]\n')
    calls = []

    result = runner.cache_history(
        cache_dir=target,
        reuse_cache_dir=legacy,
        start_ms=100,
        end_ms=200,
        fetch_provider_marks=lambda: {"KAS": 2.0, "kPEPE": 1.0},
        fetch_candles=lambda coin, *_a: calls.append(("candles", coin)) or [
            {"t": 100, "o": 1, "h": 1, "l": 1, "c": 1}
        ],
        fetch_funding=lambda coin, *_a: calls.append(("funding", coin)) or [
            {"time": 150, "rate": 0.0001}
        ],
        sleep=lambda _seconds: None,
    )

    assert json.loads((target / "candles_KAS.json").read_text())[0]["c"] == 1
    assert ("candles", "KAS") not in calls
    assert ("funding", "KAS") not in calls
    assert ("candles", "kPEPE") in calls
    assert ("funding", "kPEPE") in calls
    assert not (target / "candles_KPEPE.json").exists()
    assert result["files_reused"] == 2
