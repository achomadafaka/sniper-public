#!/usr/bin/env python3
"""Resume-safe Hyperliquid history cache with exact provider symbol identity.

Public read-only endpoints only. This script does not run as part of a paper or funded service.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time

import sys as _sys, pathlib as _pathlib   # a bare `python3 scripts/research/cache_hl_altshort_history.py` puts this file's
_R = str(_pathlib.Path(__file__).resolve().parents[2])  # own dir on sys.path,
if _R not in _sys.path:                    # never the repo root
    _sys.path.insert(0, _R)
from sniper.hyperlive.altshort_universe import provider_coin_index
from sniper.markets.hyperliquid_api import (
    fetch_candles as _fetch_candles,
    fetch_funding_history as _fetch_funding,
    fetch_provider_marks as _fetch_provider_marks,
)


DEFAULT_START_MS = int(datetime(2026, 1, 27, tzinfo=timezone.utc).timestamp() * 1000)
DEFAULT_END_MS = int(datetime(2026, 7, 26, 15, tzinfo=timezone.utc).timestamp() * 1000)
# Public hourly snapshots can end several completed bars before the requested wall-clock end.
# Six hours accepts the observed provider tail while still rejecting stale partial artifacts.
MAX_TAIL_LAG_MS = 6 * 60 * 60 * 1000
INVALIDATED_REPORTS = {
    "data/research/altshort_study/results/v2/report.json":
        "ec56ff1fa40c6de5d4e377e5ac0690a8c86d852ecd42d26f54fb7be36c849629",
    "data/research/altshort_study/results/v3/report.json":
        "f88138f6b08c3329e363c7c4368a41470d0eeb9f4650137c37fbdbc4a07d0a45",
}


def _write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def _funding_pages(fetch_funding, coin: str, start_ms: int, end_ms: int, sleep) -> list[dict]:
    rows: list[dict] = []
    cursor = int(start_ms)
    for _ in range(30):
        page = fetch_funding(coin, cursor, int(end_ms))
        if not page:
            break
        rows.extend(page)
        if len(page) < 500:
            break
        cursor = int(page[-1]["time"]) + 1
        sleep(0.25)
    return rows


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_meta(path: Path, *, timestamp_field: str) -> dict:
    relative = path.name
    if not path.exists():
        return {"path": relative, "status": "missing", "row_count": 0, "sha256": None,
                "first_ts_ms": None, "last_ts_ms": None}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"path": relative, "status": "invalid", "row_count": 0,
                "sha256": _sha256(path), "first_ts_ms": None, "last_ts_ms": None}
    if not isinstance(rows, list):
        rows = []
    timestamps = []
    for row in rows:
        try:
            timestamps.append(int(row[timestamp_field]))
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "path": relative,
        "status": "present" if rows and timestamps else "empty",
        "row_count": len(rows),
        "sha256": _sha256(path),
        "first_ts_ms": min(timestamps, default=None),
        "last_ts_ms": max(timestamps, default=None),
    }


def _valid_artifact(path: Path, *, timestamp_field: str, start_ms: int, end_ms: int) -> bool:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(rows, list) or not rows:
        return False
    try:
        timestamps = [int(row[timestamp_field]) for row in rows]
    except (KeyError, TypeError, ValueError):
        return False
    return timestamps == sorted(timestamps) and min(timestamps) <= int(end_ms) \
        and max(timestamps) >= int(end_ms) - MAX_TAIL_LAG_MS


def _reuse_exact_file(source_dir: Path | None, target: Path, *, timestamp_field: str,
                      start_ms: int, end_ms: int) -> bool:
    if source_dir is None or target.exists():
        return False
    source = source_dir / target.name
    if not source.is_file() or not _valid_artifact(
        source, timestamp_field=timestamp_field, start_ms=start_ms, end_ms=end_ms
    ):
        return False
    shutil.copyfile(source, target)
    return _sha256(source) == _sha256(target)


def cache_history(
    *, cache_dir: Path, start_ms: int, end_ms: int,
    reuse_cache_dir: Path | None = None,
    fetch_provider_marks=_fetch_provider_marks,
    fetch_candles=_fetch_candles,
    fetch_funding=_fetch_funding,
    sleep=time.sleep,
) -> dict:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if (cache_dir / "_cache_seal.json").exists():
        raise FileExistsError(f"cache is sealed and immutable: {cache_dir}")
    reuse_cache_dir = Path(reuse_cache_dir) if reuse_cache_dir is not None else None
    universe = sorted(fetch_provider_marks())
    provider_coin_index(universe)
    universe_path = cache_dir / "_universe.json"
    _write_json(universe_path, universe)
    _write_json(cache_dir / "_cache_identity.json", {
        "schema_version": 1,
        "source": "metaAndAssetCtxs.universe.name",
        "symbol_identity": "provider_exact_case_sensitive",
        "universe_sha256": hashlib.sha256(universe_path.read_bytes()).hexdigest(),
    })
    candles_written = funding_written = files_reused = 0
    for coin in universe:
        candle_path = cache_dir / f"candles_{coin}.json"
        if candle_path.exists() and not _valid_artifact(
            candle_path, timestamp_field="t", start_ms=start_ms, end_ms=end_ms
        ):
            candle_path.unlink()
        files_reused += int(_reuse_exact_file(
            reuse_cache_dir, candle_path, timestamp_field="t",
            start_ms=start_ms, end_ms=end_ms,
        ))
        if not candle_path.exists():
            candles = fetch_candles(coin, "1h", int(start_ms), int(end_ms))
            if candles:
                _write_json(candle_path, candles)
                candles_written += 1
            sleep(0.25)
        funding_path = cache_dir / f"funding_{coin}.json"
        if funding_path.exists() and not _valid_artifact(
            funding_path, timestamp_field="time", start_ms=start_ms, end_ms=end_ms
        ):
            funding_path.unlink()
        files_reused += int(_reuse_exact_file(
            reuse_cache_dir, funding_path, timestamp_field="time",
            start_ms=start_ms, end_ms=end_ms,
        ))
        if not funding_path.exists():
            funding = _funding_pages(
                fetch_funding, coin, int(start_ms), int(end_ms), sleep
            )
            _write_json(funding_path, funding)
            funding_written += 1
            sleep(0.25)
    symbols = [
        {
            "provider_symbol": coin,
            "candles": _artifact_meta(cache_dir / f"candles_{coin}.json", timestamp_field="t"),
            "funding": _artifact_meta(cache_dir / f"funding_{coin}.json", timestamp_field="time"),
        }
        for coin in universe
    ]
    _write_json(cache_dir / "_cache_seal.json", {
        "schema_version": 2,
        "source": "hyperliquid_public_info",
        "symbol_identity": "provider_exact_case_sensitive",
        "evidence_status": "partial_censored",
        "delisted_tail_status": "censored_archive_declined",
        "study_start_ms": int(start_ms),
        "study_end_ms": int(end_ms),
        "retrieved_ts_ms": int(time.time() * 1000),
        "universe_sha256": hashlib.sha256(universe_path.read_bytes()).hexdigest(),
        "invalidated_reports": INVALIDATED_REPORTS,
        "symbols": symbols,
    })
    return {
        "universe_n": len(universe),
        "candles_written": candles_written,
        "funding_written": funding_written,
        "files_reused": files_reused,
        "evidence_status": "partial_censored",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--reuse-cache-dir", type=Path)
    parser.add_argument("--start-ms", type=int, default=DEFAULT_START_MS)
    parser.add_argument("--end-ms", type=int, default=DEFAULT_END_MS)
    args = parser.parse_args(argv)
    print(json.dumps(cache_history(
        cache_dir=args.cache_dir, reuse_cache_dir=args.reuse_cache_dir,
        start_ms=args.start_ms, end_ms=args.end_ms
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
