#!/usr/bin/env python3
"""Seed only the HL market mark/funding tape from immutable local cache files."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.hyperlive import intel_store as IS  # noqa: E402


HOUR_MS = 3_600_000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(path: Path, pattern: str) -> str:
    digest = hashlib.sha256()
    for child in sorted(path.glob(pattern), key=lambda item: item.name):
        if not child.is_file():
            continue
        digest.update(child.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(child)))
    return digest.hexdigest()


def seed(conn, *, manifest_path: Path, input_root: Path) -> dict:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    source_manifest = manifest.get("source_manifest_path")
    if source_manifest:
        source_path = Path(input_root) / source_manifest
        if (not source_path.exists()
                or sha256_file(source_path) != manifest.get("source_manifest_sha256")):
            raise ValueError("source_manifest_hash_mismatch")
    candle_dir = Path(input_root) / manifest["candle_dir"]
    funding_dir = Path(input_root) / manifest["funding_dir"]
    if sha256_tree(candle_dir, "candles_*.json") != manifest["candle_sha256_tree"]:
        raise ValueError("candle_hash_mismatch")
    if sha256_tree(funding_dir, "funding_*.json") != manifest["funding_sha256_tree"]:
        raise ValueError("funding_hash_mismatch")
    start_ms = int(manifest["start_ms"])
    end_ms = int(manifest["end_ms_exclusive"])
    signals_before = conn.execute(
        "SELECT COUNT(*) FROM hl_altshort_signal_observations"
    ).fetchone()[0]
    output = []
    coins = set()
    for candle_path in sorted(candle_dir.glob("candles_*.json")):
        # Provider casing round-trips verbatim: the cache writes candles_kPEPE.json /
        # funding_kPEPE.json. Uppercasing here re-corrupts the symbol AND silently misses the
        # funding file on a case-sensitive filesystem (the exists() guard swallows it).
        coin = candle_path.stem.removeprefix("candles_")
        funding_path = funding_dir / f"funding_{coin}.json"
        funding = {}
        if funding_path.exists():
            for row in json.loads(funding_path.read_text(encoding="utf-8")):
                try:
                    bucket = int(row["time"]) // HOUR_MS * HOUR_MS
                    funding[bucket] = float(row["rate"])
                except (KeyError, TypeError, ValueError):
                    continue
        for row in json.loads(candle_path.read_text(encoding="utf-8")):
            try:
                ts_ms = int(row["t"])
                if not start_ms <= ts_ms < end_ms:
                    continue
                open_ = float(row["o"])
                high = float(row.get("h", open_))
                low = float(row.get("l", open_))
                close = float(row["c"])
            except (KeyError, TypeError, ValueError):
                continue
            if min(open_, high, low, close) <= 0:
                continue
            output.append((ts_ms, coin, close, open_, high, low, funding.get(ts_ms)))
            coins.add(coin)
    conn.executemany(
        "INSERT OR IGNORE INTO hl_altshort_regime_marks("
        "ts_ms,coin,mark,open_mark,high_mark,low_mark,funding_rate) VALUES (?,?,?,?,?,?,?)",
        output,
    )
    conn.commit()
    signals_after = conn.execute(
        "SELECT COUNT(*) FROM hl_altshort_signal_observations"
    ).fetchone()[0]
    return {
        "coins": len(coins), "rows": len(output),
        "signals_before": signals_before, "signals_after": signals_after,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Seed immutable HL hourly market tape")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--input-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        raise SystemExit("refusing write without --apply")
    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)
    try:
        result = seed(
            conn, manifest_path=Path(args.manifest), input_root=Path(args.input_root)
        )
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
