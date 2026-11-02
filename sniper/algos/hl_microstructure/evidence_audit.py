"""Read-only evidence audit for the Hyperliquid microstructure paper lane."""
from __future__ import annotations

import itertools
import json
import math
import sqlite3
from bisect import bisect_left
from collections import defaultdict


FORWARD_HORIZONS_MS = (5_000, 10_000, 30_000)
WALK_FORWARD_BLOCKS = 3
WALK_FORWARD_EMBARGO_MS = 3_600_000
FDR_ALPHA = 0.1


def build_evidence_audit(conn: sqlite3.Connection) -> dict:
    source_labels = _source_forward_labels(conn)
    return {
        "summary": _summary(conn),
        "source_pairs": _source_pairs(conn),
        "source_counts": _source_counts(conn),
        "exit_reasons": _exit_reasons(conn),
        "regime_buckets": _regime_buckets(conn),
        "expert_predictiveness": _expert_predictiveness(conn),
        "negative_controls": _negative_controls(conn),
        "source_forward_returns": _source_forward_returns(source_labels),
        "multiple_testing": _multiple_testing(source_labels),
        "walk_forward": _walk_forward(source_labels),
        "asset_regime_source_returns": _asset_regime_source_returns(source_labels),
        "data_gaps": _data_gaps(conn),
    }


def format_evidence_audit_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        "# Hyperliquid Microstructure Evidence Audit",
        "",
        f"- Closed fusion positions: {s['closed_positions']}",
        f"- Realized PnL: {_usd(s['realized_pnl'])}",
        f"- Win rate: {'n/a' if s['win_rate'] is None else f'{100 * s['win_rate']:.1f}%'}",
        "- Source label population: entered-only source votes; this is not all source opportunities.",
        "- Net return cost model: mid return minus taker round trip and entry spread estimate.",
        "- Inference: screening only; overlapping horizons and adjacent entries are not IID proof of edge.",
        "",
        "## Source Pairs",
    ]
    lines.extend(_table_lines(report["source_pairs"], ["pair", "closed", "pnl", "win_rate"], empty="No source-pair entries."))
    lines.extend(["", "## Source Counts"])
    lines.extend(_table_lines(report["source_counts"].values(), ["aligned_sources", "closed", "pnl", "win_rate"], empty="No source-count entries."))
    lines.extend(["", "## Exit Reasons"])
    lines.extend(_table_lines(report["exit_reasons"], ["reason", "closed", "pnl", "avg_hold_sec"], empty="No closed exits."))
    lines.extend(["", "## Regime Buckets"])
    lines.extend(_table_lines(report["regime_buckets"], ["bucket", "closed", "pnl", "win_rate"], empty="No entry-feature regimes."))
    lines.extend(["", "## Expert Predictiveness"])
    lines.extend(_table_lines(report["expert_predictiveness"], ["expert_id", "family", "horizon_ms", "settled", "accuracy", "avg_signed_bps"], empty="No settled expert predictions."))
    lines.extend(["", "## Negative Controls"])
    lines.extend(_table_lines(
        report["negative_controls"],
        ["control", "horizon_ms", "observations", "directional_accuracy", "net_win_rate", "avg_net_bps"],
        empty="No negative-control labels.",
    ))
    lines.extend(["", "## Source Forward Returns"])
    lines.extend(_table_lines(
        report["source_forward_returns"],
        ["source", "horizon_ms", "observations", "directional_accuracy", "net_win_rate", "avg_signed_bps", "avg_net_bps"],
        empty="No source-vote forward labels.",
    ))
    lines.extend(["", "## Multiple Testing"])
    lines.append(
        f"- Method: {report['multiple_testing']['fdr_method']}, "
        f"alpha={report['multiple_testing']['fdr_alpha']} "
        f"({report['multiple_testing']['inference_mode'].replace('_', ' ')})"
    )
    lines.extend(_table_lines(
        report["multiple_testing"]["tests"],
        ["source", "horizon_ms", "observations", "effective_observations", "avg_net_bps", "p_value", "q_value", "screening_pass"],
        empty="No multiple-testing rows.",
    ))
    lines.extend(["", "## Walk Forward Persistence Screen"])
    lines.extend(_table_lines(
        report["walk_forward"],
        ["source", "horizon_ms", "block", "train_observations", "test_observations", "train_avg_net_bps", "test_avg_net_bps"],
        empty="No walk-forward rows.",
    ))
    lines.extend(["", "## Asset/Regime Source Returns"])
    lines.extend(_table_lines(
        report["asset_regime_source_returns"],
        ["asset", "regime", "source", "horizon_ms", "observations", "avg_net_bps", "net_win_rate"],
        empty="No asset/regime source rows.",
    ))
    lines.extend(["", "## Data Gaps"])
    for key, value in report["data_gaps"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def _summary(conn: sqlite3.Connection) -> dict:
    row = _one_row(conn, """
        SELECT COUNT(*) AS closed,
               COALESCE(SUM(realized_pnl), 0) AS pnl,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins
        FROM hlm_positions
        WHERE policy_id='fusion_radar' AND state='closed'
    """)
    closed = int(row["closed"] or 0)
    wins = int(row["wins"] or 0)
    return {
        "closed_positions": closed,
        "realized_pnl": _round_money(row["pnl"] or 0.0),
        "win_rate": (wins / closed) if closed else None,
        "source_label_population": "entered_only_source_votes",
        "net_cost_model": "mid_return_minus_taker_round_trip_and_entry_spread",
        "inference_mode": "screening_only_overlapping_entry_labels",
    }


def _source_pairs(conn: sqlite3.Connection) -> list[dict]:
    stats = _Stats()
    for row in _position_rows(conn):
        aligned = _aligned_sources(row)
        if len(aligned) < 2:
            continue
        for left, right in itertools.combinations(sorted(aligned), 2):
            stats.add(f"{left}+{right}", row["realized_pnl"])
    return stats.rows(label="pair")


def _source_counts(conn: sqlite3.Connection) -> dict[int, dict]:
    stats = _Stats()
    for row in _position_rows(conn):
        stats.add(len(_aligned_sources(row)), row["realized_pnl"])
    return {int(r["aligned_sources"]): r for r in stats.rows(label="aligned_sources")}


def _exit_reasons(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT close_reason AS reason,
               COUNT(*) AS closed,
               COALESCE(SUM(realized_pnl), 0) AS pnl,
               AVG(realized_pnl) AS avg_pnl,
               AVG((closed_ts_ms - opened_ts_ms) / 1000.0) AS avg_hold_sec
        FROM hlm_positions
        WHERE policy_id='fusion_radar' AND state='closed'
        GROUP BY close_reason
        ORDER BY closed DESC, pnl ASC, reason
    """).fetchall()
    return [
        {
            "reason": r["reason"] or "unknown",
            "closed": int(r["closed"] or 0),
            "pnl": _round_money(r["pnl"] or 0.0),
            "avg_pnl": _round_money(r["avg_pnl"] or 0.0),
            "avg_hold_sec": round(float(r["avg_hold_sec"] or 0.0), 2),
        }
        for r in rows
    ]


def _regime_buckets(conn: sqlite3.Connection) -> list[dict]:
    stats = _Stats()
    rows = conn.execute("""
        SELECT p.realized_pnl, fs.features_json
        FROM hlm_positions p
        JOIN hlm_decisions d ON d.position_id = p.id AND d.status='entered'
        JOIN hlm_feature_snapshots fs ON fs.id = d.feature_snapshot_id
        WHERE p.policy_id='fusion_radar' AND p.state='closed'
    """).fetchall()
    for row in rows:
        features = _json_dict(row["features_json"])
        stats.add(_regime_bucket(features), row["realized_pnl"])
    return stats.rows(label="bucket")


def _expert_predictiveness(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT expert_id,
               family,
               horizon_ms,
               COUNT(*) AS settled,
               AVG(result) AS accuracy,
               AVG(CASE WHEN side=label_side THEN ABS(realized_return) ELSE -ABS(realized_return) END) AS avg_signed_return
        FROM hlm_expert_predictions
        WHERE result IS NOT NULL
        GROUP BY expert_id, family, horizon_ms
        ORDER BY settled DESC, avg_signed_return DESC, expert_id
        LIMIT 20
    """).fetchall()
    return [
        {
            "expert_id": r["expert_id"],
            "family": r["family"],
            "horizon_ms": int(r["horizon_ms"] or 0),
            "settled": int(r["settled"] or 0),
            "accuracy": round(float(r["accuracy"] or 0.0), 4),
            "avg_signed_return": float(r["avg_signed_return"] or 0.0),
            "avg_signed_bps": round(10000 * float(r["avg_signed_return"] or 0.0), 4),
        }
        for r in rows
    ]


def _source_forward_labels(conn: sqlite3.Connection) -> list[dict]:
    snapshots = _snapshot_index(conn)
    labels = []
    for obs in _entry_vote_observations(conn):
        entry_mid = _float(obs["entry_features"].get("mid"))
        if entry_mid is None or entry_mid <= 0:
            continue
        for horizon_ms in FORWARD_HORIZONS_MS:
            future = snapshots.future_mid(obs["asset"], obs["decision_ts_ms"] + horizon_ms)
            if future is None:
                continue
            signed = _signed_return(entry_mid, future["mid"], obs["vote_side"])
            if signed is None:
                continue
            spread_bps = _float(obs["entry_features"].get("spread_bps"))
            cost = _round_trip_cost(obs["fee_rate"], spread_bps)
            labels.append({
                "asset": obs["asset"],
                "source": obs["source"],
                "horizon_ms": horizon_ms,
                "decision_ts_ms": obs["decision_ts_ms"],
                "regime": obs["regime"],
                "signed_return": signed,
                "net_return": signed - cost,
                "cost": cost,
                "spread_bps": spread_bps,
            })
    return labels


def _source_forward_returns(labels: list[dict]) -> list[dict]:
    stats = _ForwardStats()
    for row in labels:
        stats.add_net((row["source"], row["horizon_ms"]), row["signed_return"], row["net_return"])
    return stats.rows(labels=("source", "horizon_ms"))


def _asset_regime_source_returns(labels: list[dict]) -> list[dict]:
    stats = _ForwardStats()
    for row in labels:
        stats.add_net(
            (row["asset"], row["regime"], row["source"], row["horizon_ms"]),
            row["signed_return"],
            row["net_return"],
        )
    rows = stats.rows(labels=("asset", "regime", "source", "horizon_ms"))
    rows.sort(key=lambda r: (-int(r["observations"]), str(r["asset"]), str(r["regime"]), str(r["source"]), int(r["horizon_ms"])))
    return rows


def _multiple_testing(labels: list[dict]) -> dict:
    grouped = defaultdict(list)
    for row in labels:
        grouped[(row["source"], row["horizon_ms"])].append(row)
    tests = []
    for (source, horizon_ms), rows in grouped.items():
        n = len(rows)
        mean = sum(r["net_return"] for r in rows) / n if n else 0.0
        # Overlapping forward windows are not independent: two entries on the same
        # asset closer than one horizon share most of their look-ahead path. Test the
        # cluster means so the p-value reflects effective sample size, not raw count.
        clusters = _cluster_means(rows, horizon_ms)
        p_value = _one_sided_mean_p_value(clusters)
        tests.append({
            "source": source,
            "horizon_ms": horizon_ms,
            "observations": n,
            "effective_observations": len(clusters),
            "avg_net_bps": round(10000 * mean, 4),
            "p_value": p_value,
        })
    tests = _apply_bh_fdr(tests)
    tests.sort(key=lambda r: (r["q_value"], -r["avg_net_bps"], str(r["source"]), int(r["horizon_ms"])))
    return {
        "fdr_method": "benjamini_hochberg",
        "fdr_alpha": FDR_ALPHA,
        "inference_mode": "screening_only_overlapping_entry_labels",
        "tests": tests,
    }


def _walk_forward(labels: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for row in labels:
        grouped[(row["source"], row["horizon_ms"])].append(row)
    out = []
    for (source, horizon_ms), rows in grouped.items():
        rows = sorted(rows, key=lambda r: (int(r["decision_ts_ms"]), str(r["asset"])))
        blocks = _chronological_blocks(rows, WALK_FORWARD_BLOCKS)
        for block_index, test_rows in enumerate(blocks):
            if block_index == 0 or not test_rows:
                continue
            block_start = min(int(r["decision_ts_ms"]) for r in test_rows)
            train_cutoff = block_start - WALK_FORWARD_EMBARGO_MS
            train_rows = [r for r in rows if int(r["decision_ts_ms"]) < train_cutoff]
            if not train_rows:
                continue
            train_avg = _avg_net_bps(train_rows)
            test_avg = _avg_net_bps(test_rows)
            out.append({
                "source": source,
                "horizon_ms": horizon_ms,
                "block": block_index,
                "embargo_ms": WALK_FORWARD_EMBARGO_MS,
                "train_observations": len(train_rows),
                "test_observations": len(test_rows),
                "train_avg_net_bps": train_avg,
                "test_avg_net_bps": test_avg,
                "train_positive": train_avg > 0,
                "test_positive": test_avg > 0,
            })
    out.sort(key=lambda r: (str(r["source"]), int(r["horizon_ms"]), int(r["block"])))
    return out


def _negative_controls(conn: sqlite3.Connection) -> list[dict]:
    snapshots = _snapshot_index(conn)
    observations = _entry_vote_observations(conn)
    stats = _ForwardStats()
    assets = snapshots.assets()

    for idx, obs in enumerate(observations):
        entry_mid = _float(obs["entry_features"].get("mid"))
        if entry_mid is None or entry_mid <= 0:
            continue
        entry_spread_bps = _float(obs["entry_features"].get("spread_bps"))
        cost = _round_trip_cost(obs["fee_rate"], entry_spread_bps)
        for horizon_ms in FORWARD_HORIZONS_MS:
            future = snapshots.future_mid(obs["asset"], obs["decision_ts_ms"] + horizon_ms)
            if future is None:
                continue

            shuffled_side = _opposite_side(obs["vote_side"]) if idx % 2 == 0 else obs["vote_side"]
            signed = _signed_return(entry_mid, future["mid"], shuffled_side)
            if signed is not None:
                stats.add(("shuffled_signs", horizon_ms), signed, cost)

            shifted = snapshots.session_placebo_entry(
                obs["asset"],
                obs["decision_ts_ms"],
                seed=idx,
                min_gap_ms=max(60_000, 2 * horizon_ms),
            )
            if shifted is not None:
                shifted_future = snapshots.future_mid(obs["asset"], shifted["ts"] + horizon_ms)
                shifted_signed = _signed_return(shifted["mid"], shifted_future["mid"], obs["vote_side"]) if shifted_future else None
                if shifted_signed is not None:
                    shifted_spread_bps = _float(shifted["features"].get("spread_bps"))
                    shifted_cost = _round_trip_cost(obs["fee_rate"], shifted_spread_bps)
                    stats.add(("random_entry_timestamps", horizon_ms), shifted_signed, shifted_cost)

            lagged = snapshots.at_or_before(obs["asset"], obs["decision_ts_ms"] - 60_000)
            lagged_features = lagged["features"] if lagged else obs["entry_features"]
            lagged_side = _irrelevant_feature_side(lagged_features)
            if lagged_side is not None:
                lagged_signed = _signed_return(entry_mid, future["mid"], lagged_side)
                if lagged_signed is not None:
                    stats.add(("lagged_irrelevant_feature", horizon_ms), lagged_signed, cost)

            other_asset = _next_asset(assets, obs["asset"])
            if other_asset:
                other_entry = snapshots.future_mid(other_asset, obs["decision_ts_ms"])
                other_future = snapshots.future_mid(other_asset, obs["decision_ts_ms"] + horizon_ms)
                other_signed = _signed_return(other_entry["mid"], other_future["mid"], obs["vote_side"]) if other_entry and other_future else None
                if other_signed is not None:
                    stats.add(("random_asset_assignment", horizon_ms), other_signed, cost)

    rows = stats.rows(labels=("control", "horizon_ms"))
    rows.sort(key=lambda r: (int(r["horizon_ms"]), str(r["control"])))
    return rows


def _data_gaps(conn: sqlite3.Connection) -> dict[str, str]:
    cols = _table_columns(conn, "hlm_positions")
    return {
        "trade_ticks": "available_in_hlm_raw_events",
        "l2_depth": "available_in_hlm_raw_events",
        "entry_feature_snapshots": "available_via_hlm_decisions_feature_snapshot_id",
        "source_votes": "entry_only_in_hlm_positions" if "source_votes_json" in cols else "missing",
        "mfe_mae": "not_persisted_reconstructable_from_snapshots",
        "per_decision_vote_detail": "missing_for_skips_and_holds",
        "timestamp_clock": "exchange_ts_ms_and_receive_ts_ms_both_present_do_not_mix_without_normalization",
    }


def _position_rows(conn: sqlite3.Connection):
    return conn.execute("""
        SELECT id, side, realized_pnl, source_votes_json
        FROM hlm_positions
        WHERE policy_id='fusion_radar' AND state='closed'
    """).fetchall()


def _entry_vote_observations(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT p.id AS position_id,
               p.asset,
               p.source_votes_json,
               d.decision_ts_ms,
               d.fee_rate,
               fs.features_json
        FROM hlm_positions p
        JOIN hlm_decisions d ON d.position_id = p.id
             AND d.status='entered'
             AND d.action='enter'
        JOIN hlm_feature_snapshots fs ON fs.id = d.feature_snapshot_id
        WHERE p.policy_id='fusion_radar'
        ORDER BY d.decision_ts_ms, p.id
    """).fetchall()
    out = []
    for row in rows:
        votes = _json_dict(row["source_votes_json"])
        features = _json_dict(row["features_json"])
        for source, payload in votes.items():
            if not isinstance(payload, dict):
                continue
            side = payload.get("side")
            if side not in {"long", "short"}:
                continue
            out.append({
                "position_id": int(row["position_id"]),
                "asset": row["asset"],
                "decision_ts_ms": int(row["decision_ts_ms"]),
                "fee_rate": float(row["fee_rate"] or 0.0),
                "entry_features": features,
                "regime": _regime_bucket(features),
                "source": str(source),
                "vote_side": side,
            })
    return out


def _aligned_sources(row) -> list[str]:
    side = row["side"]
    votes = _json_dict(row["source_votes_json"])
    out = []
    for family, payload in votes.items():
        if isinstance(payload, dict) and payload.get("side") == side:
            out.append(str(family))
    return out


def _regime_bucket(features: dict) -> str:
    spread = _float(features.get("spread_bps"))
    bid_depth = _float(features.get("bid_depth_5"))
    ask_depth = _float(features.get("ask_depth_5"))
    tight = spread is not None and spread <= 12.0
    deep = bid_depth is not None and ask_depth is not None and min(bid_depth, ask_depth) >= 10_000.0
    return ("tight" if tight else "wide") + "_" + ("deep" if deep else "thin")


class _Stats:
    def __init__(self):
        self._data = defaultdict(lambda: {"closed": 0, "wins": 0, "pnl": 0.0})

    def add(self, key, pnl) -> None:
        value = float(pnl or 0.0)
        item = self._data[key]
        item["closed"] += 1
        item["wins"] += 1 if value > 0 else 0
        item["pnl"] += value

    def rows(self, *, label: str) -> list[dict]:
        out = []
        for key, item in self._data.items():
            closed = int(item["closed"])
            row = {
                label: key,
                "closed": closed,
                "pnl": _round_money(item["pnl"]),
                "win_rate": (item["wins"] / closed) if closed else None,
            }
            out.append(row)
        out.sort(key=lambda r: (-int(r["closed"]), float(r["pnl"]), str(r[label])))
        return out


class _ForwardStats:
    def __init__(self):
        self._data = defaultdict(lambda: {"n": 0, "correct": 0, "net_wins": 0, "signed_sum": 0.0, "net_sum": 0.0})

    def add(self, key, signed_return: float, cost: float) -> None:
        signed = float(signed_return)
        net = signed - float(cost or 0.0)
        self.add_net(key, signed, net)

    def add_net(self, key, signed_return: float, net_return: float) -> None:
        signed = float(signed_return)
        net = float(net_return)
        item = self._data[key]
        item["n"] += 1
        item["correct"] += 1 if signed > 0 else 0
        item["net_wins"] += 1 if net > 0 else 0
        item["signed_sum"] += signed
        item["net_sum"] += net

    def rows(self, *, labels: tuple[str, ...]) -> list[dict]:
        out = []
        for key, item in self._data.items():
            values = key if isinstance(key, tuple) else (key,)
            n = int(item["n"])
            if n <= 0:
                continue
            row = {
                "observations": n,
                "directional_accuracy": round(item["correct"] / n, 4),
                "net_win_rate": round(item["net_wins"] / n, 4),
                "avg_signed_bps": round(10000 * item["signed_sum"] / n, 4),
                "avg_net_bps": round(10000 * item["net_sum"] / n, 4),
            }
            for label, value in zip(labels, values):
                row[label] = value
            out.append(row)
        out.sort(key=lambda r: (-int(r["observations"]), tuple(_sort_value(r.get(label)) for label in labels)))
        return out


class _SnapshotIndex:
    def __init__(self, by_asset: dict[str, list[dict]]):
        self._by_asset = by_asset
        self._times = {asset: [r["ts"] for r in rows] for asset, rows in by_asset.items()}

    def assets(self) -> list[str]:
        return sorted(self._by_asset)

    def future_mid(self, asset: str, ts_ms: int) -> dict | None:
        rows = self._by_asset.get(asset) or []
        times = self._times.get(asset) or []
        idx = bisect_left(times, int(ts_ms))
        if idx >= len(rows):
            return None
        return rows[idx]

    def at_or_before(self, asset: str, ts_ms: int) -> dict | None:
        rows = self._by_asset.get(asset) or []
        times = self._times.get(asset) or []
        idx = bisect_left(times, int(ts_ms))
        if idx < len(times) and times[idx] == int(ts_ms):
            return rows[idx]
        idx -= 1
        if idx < 0:
            return None
        return rows[idx]

    def shifted_entry(self, asset: str, ts_ms: int, *, offset: int) -> dict | None:
        rows = self._by_asset.get(asset) or []
        times = self._times.get(asset) or []
        if not rows:
            return None
        idx = bisect_left(times, int(ts_ms))
        if idx >= len(rows):
            idx = len(rows) - 1
        return rows[(idx + int(offset)) % len(rows)]

    def session_placebo_entry(self, asset: str, ts_ms: int, *, seed: int, min_gap_ms: int) -> dict | None:
        return _session_placebo_entry(
            self._by_asset.get(asset) or [],
            ts_ms,
            seed=seed,
            min_gap_ms=min_gap_ms,
        )


def _snapshot_index(conn: sqlite3.Connection) -> _SnapshotIndex:
    rows = conn.execute("""
        SELECT asset, decision_ts_ms, features_json
        FROM hlm_feature_snapshots
        ORDER BY asset, decision_ts_ms, id
    """).fetchall()
    by_asset = defaultdict(list)
    seen = set()
    for row in rows:
        asset = row["asset"]
        ts = int(row["decision_ts_ms"])
        key = (asset, ts)
        if key in seen:
            continue
        seen.add(key)
        features = _json_dict(row["features_json"])
        mid = _float(features.get("mid"))
        if mid is None or mid <= 0:
            continue
        by_asset[asset].append({"asset": asset, "ts": ts, "mid": mid, "features": features})
    return _SnapshotIndex(dict(by_asset))


def _table_lines(rows, columns: list[str], *, empty: str) -> list[str]:
    rows = list(rows or [])
    if not rows:
        return [f"- {empty}"]
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for row in rows[:20]:
        values = []
        for col in columns:
            value = row.get(col)
            if isinstance(value, float):
                value = f"{value:.4f}"
            values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return lines


def _one_row(conn: sqlite3.Connection, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise sqlite3.Error("query returned no row")
    return row


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _json_dict(raw) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signed_return(entry_mid: float, future_mid: float, side: str) -> float | None:
    entry = _float(entry_mid)
    future = _float(future_mid)
    if entry is None or future is None or entry <= 0:
        return None
    realized = (future - entry) / entry
    if side == "long":
        return realized
    if side == "short":
        return -realized
    return None


def _round_trip_cost(fee_rate: float, spread_bps: float | None = None) -> float:
    spread_cost = max(0.0, float(spread_bps or 0.0)) / 10_000.0
    return 2.0 * float(fee_rate or 0.0) + spread_cost


def _opposite_side(side: str) -> str:
    return "short" if side == "long" else "long"


def _irrelevant_feature_side(features: dict) -> str | None:
    for key in ("book_imbalance_5", "flow_imbalance_5s", "microprice_minus_mid", "return_30s"):
        value = _float((features or {}).get(key))
        if value is None or abs(value) <= 1e-12:
            continue
        return "long" if value > 0 else "short"
    return None


def _session_placebo_entry(rows: list[dict], ts_ms: int, *, seed: int, min_gap_ms: int) -> dict | None:
    if not rows:
        return None
    ts = int(ts_ms)
    min_gap = max(0, int(min_gap_ms))
    candidates = [r for r in rows if abs(int(r["ts"]) - ts) >= min_gap]
    if not candidates:
        candidates = list(rows)
    idx = (int(seed) * 1_103_515_245 + 12_345) % len(candidates)
    return candidates[idx]


def _next_asset(assets: list[str], asset: str) -> str | None:
    if len(assets) < 2:
        return None
    try:
        idx = assets.index(asset)
    except ValueError:
        return assets[0]
    return assets[(idx + 1) % len(assets)]


def _cluster_means(rows: list[dict], horizon_ms: int) -> list[float]:
    # Collapse observations that share a (asset, non-overlapping time block) into one
    # cluster mean. Block width = one horizon, so entries whose forward windows overlap
    # count once. Returns the list of cluster-mean net returns used for inference.
    width = max(1, int(horizon_ms))
    buckets = defaultdict(list)
    for r in rows:
        block = int(r["decision_ts_ms"]) // width
        buckets[(r.get("asset"), block)].append(float(r["net_return"]))
    return [sum(v) / len(v) for v in buckets.values() if v]


def _one_sided_mean_p_value(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 1.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    if variance <= 0:
        return 0.0 if mean > 0 else 1.0
    standard_error = math.sqrt(variance / n)
    if standard_error <= 0:
        return 0.0 if mean > 0 else 1.0
    # Student-t, not normal: with the small effective samples this lane produces, the
    # normal approximation is anticonservative (p too small -> false screening passes).
    t = mean / standard_error
    return round(_student_t_sf(t, n - 1), 6)


def _student_t_sf(t: float, df: int) -> float:
    """Upper-tail survival P(T >= t) for a Student-t with ``df`` degrees of freedom."""
    if df <= 0:
        return 1.0
    half = _betai(df / 2.0, 0.5, df / (df + t * t))  # = P(|T| >= |t|)
    return 0.5 * half if t >= 0 else 1.0 - 0.5 * half


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) (Numerical Recipes)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float) -> float:
    max_iter = 300
    eps = 3.0e-12
    fpmin = 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _apply_bh_fdr(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: (float(r["p_value"]), str(r["source"]), int(r["horizon_ms"])))
    m = len(ordered)
    running = 1.0
    q_values = {}
    for rank, row in reversed(list(enumerate(ordered, start=1))):
        q = min(running, float(row["p_value"]) * m / rank)
        running = q
        q_values[(row["source"], row["horizon_ms"])] = round(min(q, 1.0), 6)
    out = []
    for row in rows:
        item = dict(row)
        q_value = q_values[(row["source"], row["horizon_ms"])]
        item["q_value"] = q_value
        item["screening_pass"] = bool(q_value <= FDR_ALPHA and float(item["avg_net_bps"]) > 0)
        item["significant"] = False
        item["significance_note"] = "screening_only_overlapping_entry_labels"
        item["p_value"] = round(float(item["p_value"]), 6)
        out.append(item)
    return out


def _chronological_blocks(rows: list[dict], block_count: int) -> list[list[dict]]:
    if not rows:
        return []
    count = min(int(block_count), len(rows))
    size = math.ceil(len(rows) / count)
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def _avg_net_bps(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    return round(10000 * sum(float(r["net_return"]) for r in rows) / len(rows), 4)


def _sort_value(value):
    if isinstance(value, int):
        return (0, value)
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value))


def _round_money(value) -> float:
    return round(float(value or 0.0), 4)


def _usd(value) -> str:
    n = float(value or 0.0)
    return ("+" if n > 0 else "") + f"${n:.2f}"
