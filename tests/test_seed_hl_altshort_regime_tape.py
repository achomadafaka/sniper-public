import importlib.util
import json
import sys

from sniper.hyperlive import intel_store as IS


def _seeder():
    spec = importlib.util.spec_from_file_location(
        "seed_hl_altshort_regime_tape", "scripts/seed_hl_altshort_regime_tape.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_seed_is_hash_pinned_and_writes_market_tape_only(tmp_path):
    seeder = _seeder()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "candles_AAA.json").write_text(json.dumps([
        {"t": 0, "o": 100, "h": 110, "l": 90, "c": 105},
        {"t": 3_600_000, "o": 105, "h": 108, "l": 100, "c": 102},
    ]), encoding="utf-8")
    (cache / "funding_AAA.json").write_text(json.dumps([
        {"time": 100, "rate": 0.0001}, {"time": 3_600_100, "rate": 0.0002},
    ]), encoding="utf-8")
    manifest = {
        "candle_dir": "cache", "funding_dir": "cache", "start_ms": 0,
        "end_ms_exclusive": 7_200_000,
        "candle_sha256_tree": seeder.sha256_tree(cache, "candles_*.json"),
        "funding_sha256_tree": seeder.sha256_tree(cache, "funding_*.json"),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)

    result = seeder.seed(conn, manifest_path=path, input_root=tmp_path)

    assert result == {"coins": 1, "rows": 2, "signals_before": 0, "signals_after": 0}
    rows = conn.execute("SELECT * FROM hl_altshort_regime_marks ORDER BY ts_ms").fetchall()
    assert rows[0]["open_mark"] == 100
    assert rows[0]["high_mark"] == 110
    assert rows[0]["low_mark"] == 90
    assert rows[0]["mark"] == 105
    assert rows[0]["funding_rate"] == 0.0001


def test_seed_rejects_hash_mismatch(tmp_path):
    seeder = _seeder()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "candles_AAA.json").write_text("[]", encoding="utf-8")
    manifest = {
        "candle_dir": "cache", "funding_dir": "cache", "start_ms": 0,
        "end_ms_exclusive": 1, "candle_sha256_tree": "wrong",
        "funding_sha256_tree": seeder.sha256_tree(cache, "funding_*.json"),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)

    try:
        seeder.seed(conn, manifest_path=path, input_root=tmp_path)
        assert False, "expected hash mismatch"
    except ValueError as exc:
        assert "candle_hash_mismatch" in str(exc)


def test_seed_preserves_provider_symbol_case_and_loads_matching_funding(tmp_path):
    seeder = _seeder()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "candles_kPEPE.json").write_text(json.dumps([
        {"t": 0, "o": 1, "h": 2, "l": 0.5, "c": 1.5},
    ]), encoding="utf-8")
    (cache / "funding_kPEPE.json").write_text(json.dumps([
        {"time": 100, "rate": 0.0007},
    ]), encoding="utf-8")
    manifest = {
        "candle_dir": "cache", "funding_dir": "cache", "start_ms": 0,
        "end_ms_exclusive": 3_600_000,
        "candle_sha256_tree": seeder.sha256_tree(cache, "candles_*.json"),
        "funding_sha256_tree": seeder.sha256_tree(cache, "funding_*.json"),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)

    seeder.seed(conn, manifest_path=path, input_root=tmp_path)

    row = conn.execute("SELECT coin, funding_rate FROM hl_altshort_regime_marks").fetchone()
    assert row["coin"] == "kPEPE"
    assert row["funding_rate"] == 0.0007
