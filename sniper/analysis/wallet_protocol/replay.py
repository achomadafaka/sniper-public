"""W5 — replay determinism: serialization helpers.

Two responsibilities:

1. ``serialize_wallet_data`` / ``deserialize_wallet_data`` —
   round-trip ``WalletData`` through a JSON-safe dict so that
   ``scripts/record_fetch_snapshot.py`` can persist live-fetch results and
   ``scripts/run_wallet_protocol.py --from-fixture`` can replay them
   without hitting any live API.

2. ``reports_match`` — compare two protocol-report dicts (or verdict
   summaries) for spec-relevant bit-identity within tolerance, used by
   ``scripts/replay_wallet_run.py``.

Spec ref: docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md
§7.4 (replay determinism). Plan ref: docs/superpowers/plans/2026-04-30-analyzer-completion-phase-1.md W5.

Out of scope: re-running ``analyze_wallet`` from JSONL events (W5 leaves
log-replay to ``replay_wallet_run.py --from-log``, which compares against
the recorded ``analyze_completed`` event payload rather than re-executing
the full analysis).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
from sniper.analysis.wallet_protocol.flipper import PeerBaselineFn, PriceHistoryFn
from sniper.analysis.wallet_protocol.types import WalletData

FIXTURE_SCHEMA_VERSION = 2


# ─────────────────────────────────────────────────────────────────────────────
# WalletData round-trip
# ─────────────────────────────────────────────────────────────────────────────


def serialize_wallet_data(data: WalletData) -> dict[str, Any]:
    """Convert a ``WalletData`` into a JSON-safe dict."""
    if not isinstance(data.replay_surface_meta, dict):
        # Symmetric with deserialize_wallet_data: a v2 fixture must carry
        # a non-null replay_surface_meta. Stamping it with status="skipped"
        # entries IS a valid choice when surfaces weren't captured — but
        # the field itself must be a dict, never None. Refusing to write
        # invalid v2 prevents save→load asymmetry that would corrupt
        # downstream replay state.
        raise ValueError(
            "v2 fixture missing or null 'replay_surface_meta' field "
            "(must be a non-null dict)"
        )
    cl_dict: dict[str, Any] | None = None
    if data.capital_ledger is not None:
        cl_dict = asdict(data.capital_ledger)
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "wallet": data.wallet,
        "fetched_at": int(data.fetched_at),
        "closed_positions": list(data.closed_positions),
        "open_positions": list(data.open_positions),
        "activity_rows": list(data.activity_rows),
        "activity_cap_hit": bool(data.activity_cap_hit),
        "archetype_layer1": data.archetype_layer1,
        "archetype_layer2": data.archetype_layer2,
        "extractor_features_summary": data.extractor_features_summary,
        "execution_profile": data.execution_profile,
        "capital_ledger": cl_dict,
        "portfolio_value_usdc": data.portfolio_value_usdc,
        "fetch_quality_flags": list(data.fetch_quality_flags),
        "subgraph_fallback_used": bool(data.subgraph_fallback_used),
        "walk_full_history": bool(data.walk_full_history),
        "n_unmapped_fills": int(data.n_unmapped_fills),
        # v2 replay surfaces:
        "price_history_lookups": data.price_history_lookups,
        "copy_latency_prices": data.copy_latency_prices,
        "leafs_by_cid": data.leafs_by_cid,
        "nav_lookup": data.nav_lookup,
        "peer_baselines": data.peer_baselines,
        "replay_surface_meta": data.replay_surface_meta,
        "fetch_termination": data.fetch_termination,
        # CTF event streams — persisted literally; None MUST survive the
        # round trip because the surface uses None (fetch failure) vs []
        # (fetch succeeded, no events) to distinguish data_unavailable
        # causes. Do NOT coerce with `or []`.
        "redemptions": data.redemptions,
        "splits": data.splits,
        "merges": data.merges,
    }


def deserialize_wallet_data(payload: dict[str, Any]) -> WalletData:
    """Reconstruct a ``WalletData`` from its JSON-safe dict."""
    schema_version = payload.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or schema_version < 1
        or schema_version > FIXTURE_SCHEMA_VERSION
    ):
        raise ValueError(
            f"unsupported fixture schema_version {schema_version!r}; "
            f"this build supports 1..{FIXTURE_SCHEMA_VERSION}"
        )
    if "wallet" not in payload:
        raise ValueError("fetch snapshot missing 'wallet' field")

    is_v2 = schema_version >= 2
    if is_v2 and not isinstance(payload.get("replay_surface_meta"), dict):
        # REQUIRED means non-null, not just key-present. A missing key OR
        # an explicit null both fail closed.
        raise ValueError(
            "v2 fixture missing or null 'replay_surface_meta' field "
            "(must be a non-null dict)"
        )

    cl_dict = payload.get("capital_ledger")
    cl: CapitalLedger | None = None
    if cl_dict is not None:
        cl = CapitalLedger(**cl_dict)
    return WalletData(
        wallet=str(payload["wallet"]).lower(),
        closed_positions=list(payload.get("closed_positions") or []),
        open_positions=list(payload.get("open_positions") or []),
        activity_rows=list(payload.get("activity_rows") or []),
        activity_cap_hit=bool(payload.get("activity_cap_hit", False)),
        archetype_layer1=payload.get("archetype_layer1"),
        # archetype_layer2 is additive within v2; absent → None for legacy
        # fixtures recorded before the field was introduced.
        archetype_layer2=payload.get("archetype_layer2"),
        # extractor_features_summary is additive within v2 (Edge Surface v1);
        # absent → None for fixtures recorded before the field was introduced.
        extractor_features_summary=payload.get("extractor_features_summary"),
        execution_profile=payload.get("execution_profile"),
        fetched_at=int(payload.get("fetched_at", 0)),
        capital_ledger=cl,
        portfolio_value_usdc=payload.get("portfolio_value_usdc"),
        fetch_quality_flags=list(payload.get("fetch_quality_flags") or []),
        subgraph_fallback_used=bool(payload.get("subgraph_fallback_used", False)),
        # Completeness Surface v1 — additive within v2; pre-feature fixtures
        # default to (False, 0), which is truthful (capture-state was not tracked).
        walk_full_history=bool(payload.get("walk_full_history", False)),
        n_unmapped_fills=int(payload.get("n_unmapped_fills") or 0),
        # v2 replay surfaces — None when v1 (key absent) or when explicitly skipped.
        price_history_lookups=payload.get("price_history_lookups"),
        copy_latency_prices=payload.get("copy_latency_prices"),
        leafs_by_cid=payload.get("leafs_by_cid"),
        nav_lookup=payload.get("nav_lookup"),
        peer_baselines=payload.get("peer_baselines"),
        replay_surface_meta=payload.get("replay_surface_meta"),
        fetch_termination=payload.get("fetch_termination"),
        # CTF event streams — no default so a missing key → None, which is
        # the correct backward-compat signal (pre-surface fixtures have no
        # key; explicit null in the payload deserializes to None, preserving
        # the fetch-failure vs empty-list distinction).
        redemptions=payload.get("redemptions"),
        splits=payload.get("splits"),
        merges=payload.get("merges"),
    )


def save_fetch_snapshot(data: WalletData, path: Path | str) -> Path:
    """Serialize ``data`` to ``path`` (creates parent directories)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(serialize_wallet_data(data), indent=2, default=str) + "\n"
    )
    return out


def load_fetch_snapshot(path: Path | str) -> WalletData:
    """Load a fetch snapshot back into a ``WalletData``."""
    payload = json.loads(Path(path).read_text())
    return deserialize_wallet_data(payload)


# ─────────────────────────────────────────────────────────────────────────────
# Verdict comparison for replay
# ─────────────────────────────────────────────────────────────────────────────


# Spec-relevant decision fields. Replay treats divergence on any of these as
# a verdict-identity failure. Float fields use ``tol``; everything else is
# compared by ``==``.
_VERDICT_FIELDS = (
    "overall_verdict",
    "primary_edge_type",
    "gate_result",
    "trading_mode",
    "n_closed_positions",
    "n_held_to_resolution",
    "n_open_positions",
    "n_open_dead",
    "net_pnl",
    "dw_roi_point",
    "dw_roi_ci_lower",
    "dw_roi_ci_upper",
    "ew_roi_point",
)

_HYPOTHESIS_NUMERIC_FIELDS = (
    "test_statistic",
    "p_value_raw",
    "p_value_bh_adjusted",
    "ci_lower",
    "ci_upper",
)


def _floats_equal(a: Any, b: Any, *, tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _compare_field(
    diffs: list[str], path: str, a: Any, b: Any, *, tol: float
) -> None:
    if isinstance(a, float) or isinstance(b, float):
        if not _floats_equal(a, b, tol=tol):
            diffs.append(f"{path}: {a!r} != {b!r} (delta>{tol})")
        return
    if a != b:
        diffs.append(f"{path}: {a!r} != {b!r}")


def _compare_branch_hypotheses(
    diffs: list[str],
    branch_name: str,
    a_branch: dict[str, Any] | None,
    b_branch: dict[str, Any] | None,
    *,
    tol: float,
) -> None:
    if a_branch is None and b_branch is None:
        return
    if a_branch is None or b_branch is None:
        diffs.append(f"{branch_name}: present={a_branch is not None}/{b_branch is not None}")
        return

    for collection_key in ("hypotheses", "tag_grains"):
        a_list = a_branch.get(collection_key) or []
        b_list = b_branch.get(collection_key) or []
        if len(a_list) != len(b_list):
            diffs.append(
                f"{branch_name}.{collection_key}: len {len(a_list)} != {len(b_list)}"
            )
            continue
        for a_item, b_item in zip(a_list, b_list):
            ident = a_item.get("name") or a_item.get("grain") or "?"
            _compare_field(
                diffs,
                f"{branch_name}.{collection_key}.{ident}.verdict",
                a_item.get("verdict"),
                b_item.get("verdict"),
                tol=tol,
            )
            for fld in _HYPOTHESIS_NUMERIC_FIELDS:
                _compare_field(
                    diffs,
                    f"{branch_name}.{collection_key}.{ident}.{fld}",
                    a_item.get(fld),
                    b_item.get(fld),
                    tol=tol,
                )


def reports_match(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    tol: float = 1e-9,
) -> tuple[bool, list[str]]:
    """Compare two protocol-report dicts (or report-equivalent verdict
    summaries) for bit-identity within tolerance.

    Returns ``(matched, diffs)``. ``matched`` is True iff ``diffs`` is empty.

    Tolerated divergence: float fields within ``tol``. Strings, ints, and
    ``None`` must match exactly.

    Comparison surface: top-level decision fields plus per-hypothesis numeric
    fields under ``held_analyst_branch`` and ``flipper_branch``. Detail dicts
    are not compared field-by-field (they include implementation-specific
    bootstrap diagnostics whose stability is checked separately by stress
    tests).
    """
    diffs: list[str] = []
    for fld in _VERDICT_FIELDS:
        if fld in a or fld in b:
            _compare_field(diffs, fld, a.get(fld), b.get(fld), tol=tol)

    _compare_branch_hypotheses(
        diffs,
        "held_analyst_branch",
        a.get("held_analyst_branch"),
        b.get("held_analyst_branch"),
        tol=tol,
    )
    _compare_branch_hypotheses(
        diffs,
        "flipper_branch",
        a.get("flipper_branch"),
        b.get("flipper_branch"),
        tol=tol,
    )

    return (not diffs, diffs)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture closure builder
# ─────────────────────────────────────────────────────────────────────────────


def build_fixture_closures(
    data: WalletData,
) -> tuple[PriceHistoryFn | None, PeerBaselineFn | None]:
    """Construct fixture-backed (price_history_fn, peer_baseline_fn) closures.

    Rule (spec 2026-05-01-fixture-v2-schema.md, "Empty != skipped"):
    * None field -> closure is None.
    * Dict field (including {}) -> closure exists, returns stored value or
      None for unknown keys.
    """
    price_fn: PriceHistoryFn | None = None
    peer_fn: PeerBaselineFn | None = None

    if data.price_history_lookups is not None:
        lookups = data.price_history_lookups

        def _price_history_fn(asset_id: str, ts: int) -> float | None:
            return lookups.get(f"{asset_id}|{int(ts)}")

        price_fn = _price_history_fn

    if data.peer_baselines is not None:
        baselines = data.peer_baselines

        def _peer_baseline_fn(grain: str, tag: str, wallet: str):
            # `wallet` arg matches the PeerBaselineFn contract; baselines are
            # keyed by (grain, tag) only — self-exclusion was applied at capture.
            value = baselines.get(f"{grain}|{tag}")
            if value is None:
                return None
            mean_wr, peer_count = value[0], value[1]
            return (float(mean_wr), int(peer_count))

        peer_fn = _peer_baseline_fn

    return price_fn, peer_fn
