"""Slice 6 Task 7 — `MaterializedWalletData` fixture exporter / loader.

JSON envelope shape:

```
{
  "wallet_data": <dataclasses.asdict(WalletData)>,
  "window": {
      "window_id": ...,
      "capture_run_ids": [...],
      "start_ts": ...,
      "end_ts": ...,
      "source_versions": [[k, v], ...]
  },
  "replay_surface_meta": {...},
  "manifest_hashes": {...},
  "row_counts": {...},
  "price_bars": {token_id: [{"ts", "price", "source_version"}, ...]}
}
```

The loader rebuilds `WalletData`, the `AnalysisWindow`, and a fresh
`price_history_fn` closure over the persisted `price_bars` (mirroring
`make_ledger_price_history_fn`'s "first bar with ts >= target inside a
forward window" rule). `peer_baseline_fn` / `leafs_by_cid` /
`nav_lookup` stay `None` per Slice 5's design.

The round-trip contract is byte-equal `ProtocolReport` (compared via
`dataclasses.asdict`). The exporter is idempotent on identical content
— the file is rewritten only when its sha256 changes.

No live APIs. No `requests`, `httpx`, `bot.supabase_helper`, or
`urllib.request` imports. The polymarket-client surface stays out of
this module too — the exporter consumes only ledger-derived state.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from sniper.analysis.wallet_protocol.ledger.materialize import (
    MaterializedWalletData,
)
from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow
from sniper.analysis.wallet_protocol.types import WalletData


_PRICE_FORWARD_WINDOW_FALLBACK = 60 * 60  # mirrors live adapter floor for 60-min fidelity


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


def export_calibration_fixture(
    bundle: MaterializedWalletData,
    output_path: Path,
) -> Path:
    """Serialise `bundle` to `output_path` and return the same path.

    Idempotent on content: identical sha256 over the JSON bytes is a
    no-op (mtime preserved). Different content rewrites the file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = _bundle_to_payload(bundle)
    serialized = _stable_json(payload)

    if output_path.exists():
        existing = output_path.read_bytes()
        if hashlib.sha256(existing).hexdigest() == hashlib.sha256(
            serialized
        ).hexdigest():
            return output_path

    output_path.write_bytes(serialized)
    return output_path


def _bundle_to_payload(bundle: MaterializedWalletData) -> dict[str, Any]:
    return {
        "wallet_data": dataclasses.asdict(bundle.wallet_data),
        "window": _window_to_payload(bundle.window),
        "replay_surface_meta": _normalize_jsonable(bundle.replay_surface_meta),
        "manifest_hashes": _normalize_jsonable(bundle.manifest_hashes),
        "row_counts": _normalize_jsonable(bundle.row_counts),
        "price_bars": _price_bars_payload(bundle),
    }


def _window_to_payload(window: AnalysisWindow) -> dict[str, Any]:
    return {
        "window_id": window.window_id,
        "capture_run_ids": list(window.capture_run_ids),
        "start_ts": window.start_ts,
        "end_ts": window.end_ts,
        "source_versions": [list(pair) for pair in window.source_versions],
    }


def _price_bars_payload(
    bundle: MaterializedWalletData,
) -> dict[str, list[dict[str, Any]]]:
    """Capture the bar surface in JSON form.

    Preferred path: `bundle.get_price_bars(token_id)` (the diagnostics
    callable Slice 5 / Task 6 publishes) returns the actual bar list
    with their captured `ts` / `price` / `source_version`. Use it when
    available; the loader can rebuild a faithful closure from the bars.

    Fallback path: `wallet_data.price_history_lookups` carries a
    "latest bar per token within window" map (no bar ts). Synthesize
    one bar per token at `window.end_ts` so the loader still has data
    for in-window probes near the window tail.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    tokens = list((bundle.wallet_data.price_history_lookups or {}).keys())

    if bundle.get_price_bars is not None and tokens:
        for token_id in tokens:
            result = bundle.get_price_bars(token_id)
            for bar in result.bars:
                out.setdefault(token_id, []).append(
                    {
                        "ts": int(bar.ts),
                        "price": float(bar.price),
                        "source_version": str(bar.source_version),
                    }
                )

    if not out:
        end_ts = bundle.window.end_ts
        for token_id, price in (bundle.wallet_data.price_history_lookups or {}).items():
            if price is None:
                continue
            out.setdefault(str(token_id), []).append(
                {
                    "ts": int(end_ts),
                    "price": float(price),
                    "source_version": _source_version_or_default(bundle),
                }
            )
    return out


def _source_version_or_default(bundle: MaterializedWalletData) -> str:
    sv = dict(bundle.window.source_versions)
    return sv.get("price_v") or "unknown"


def _normalize_jsonable(value: Any) -> Any:
    """Best-effort normalisation: tuples → lists, sets → sorted lists.

    Called on `replay_surface_meta` / `manifest_hashes` / `row_counts`
    so the on-disk JSON is stable across runs.
    """
    if isinstance(value, dict):
        return {k: _normalize_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(_normalize_jsonable(v) for v in value)
    return value


def _stable_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_calibration_fixture(path: Path) -> MaterializedWalletData:
    """Read the envelope at `path` and return a usable bundle."""
    payload = json.loads(Path(path).read_text())
    wallet_data = _wallet_data_from_payload(payload["wallet_data"])
    window = _window_from_payload(payload["window"])
    bars = payload.get("price_bars") or {}
    price_history_fn = _make_price_history_fn(bars)
    return MaterializedWalletData(
        wallet_data=wallet_data,
        price_history_fn=price_history_fn,
        peer_baseline_fn=None,
        leafs_by_cid=None,
        nav_lookup=None,
        window=window,
        replay_surface_meta=dict(payload.get("replay_surface_meta") or {}),
        manifest_hashes=dict(payload.get("manifest_hashes") or {}),
        row_counts=dict(payload.get("row_counts") or {}),
    )


def _wallet_data_from_payload(payload: Mapping[str, Any]) -> WalletData:
    fields = {f.name for f in dataclasses.fields(WalletData)}
    accepted = {k: v for k, v in payload.items() if k in fields}
    return WalletData(**accepted)


def _window_from_payload(payload: Mapping[str, Any]) -> AnalysisWindow:
    source_versions = tuple(
        tuple(pair) for pair in payload.get("source_versions", [])
    )
    return AnalysisWindow(
        window_id=payload["window_id"],
        capture_run_ids=tuple(payload["capture_run_ids"]),
        start_ts=int(payload["start_ts"]),
        end_ts=int(payload["end_ts"]),
        source_versions=source_versions,
    )


def _make_price_history_fn(
    bars_payload: Mapping[str, Any],
):
    """Build a closure that mirrors `make_ledger_price_history_fn`.

    Returns the price of the first bar with `ts >= target` inside the
    forward window; None when no usable bar exists.
    """
    bars_by_token: dict[str, list[dict[str, Any]]] = {
        str(token_id): sorted(
            (
                {
                    "ts": int(bar["ts"]),
                    "price": float(bar["price"]),
                }
                for bar in bars
                if bar.get("ts") is not None and bar.get("price") is not None
            ),
            key=lambda b: b["ts"],
        )
        for token_id, bars in bars_payload.items()
    }
    forward_window = _PRICE_FORWARD_WINDOW_FALLBACK

    def _phf(token_id: str, ts: int) -> float | None:
        bars = bars_by_token.get(str(token_id))
        if not bars:
            return None
        target = int(ts)
        upper = target + forward_window
        for bar in bars:
            if bar["ts"] < target:
                continue
            if bar["ts"] > upper:
                return None
            return bar["price"]
        return None

    return _phf


__all__ = [
    "export_calibration_fixture",
    "load_calibration_fixture",
]
