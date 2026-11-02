#!/usr/bin/env python3
"""Render a read-only funded memecoin forensics report."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.persistence.db_core import get_readonly_connection
from sniper.persistence.db_memecoin import signal_source_sql
from sniper.util.clock import real_utc_now_iso


PREREGISTERED_SINCE = "2026-08-23T00:00:00+00:00"
DISCOVERY_END = "2026-08-28T00:00:00+00:00"
FIXED_CANDIDATES = ("turnover_10_50_v1", "nano_prior_profitable_v1",
                    "robinhood_organic_m5_lt_1000_v1")
SHADOW_STATES = {
    "small_band_v1": ("would_block", "would_allow", "not_applicable", "unknown", "not_recorded"),
    "prior_5_negative_v1": ("would_block", "would_allow", "insufficient_history", "not_applicable", "unknown", "not_recorded"),
}
for _shadow_name in ("source_size_ge_1000_v1", "robinhood_organic_m5_lt_1000_v1",
                     "solana_clip_ge_100_v1", "turnover_10_50_v1", "nano_prior_profitable_v1"):
    SHADOW_STATES[_shadow_name] = SHADOW_STATES["prior_5_negative_v1"]
SOURCE_SIZE_BINS = ((250.0, "<250"), (500.0, "250_to_<500"),
                    (1000.0, "500_to_<1000"), (math.inf, ">=1000"))
EFFECTIVE_MCAP_BINS = ((50_000.0, "<50000"), (250_000.0, "50000_to_<250000"),
                       (1_000_000.0, "250000_to_<1000000"), (math.inf, ">=1000000"))


def _finite(value, *, positive=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0.0):
        return None
    return number


def _aware(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _canonical_since(value):
    parsed = _aware(value)
    if parsed is None:
        raise ValueError("--since must be an aware ISO-8601 timestamp")
    return parsed.isoformat()


def _entry_time(row):
    return _aware(row.get("opened_recorded_at"))


def _outcome(rows):
    pnl = 0.0
    notional = 0.0
    valid_n = 0
    for row in rows:
        row_pnl = _finite(row.get("realized_pnl"))
        row_notional = _finite(row.get("entry_notional"), positive=True)
        if row_pnl is None or row_notional is None:
            continue
        pnl += row_pnl
        notional += row_notional
        valid_n += 1
    return {
        "position_n": len(rows),
        "coin_n": len({(row.get("network_id"), row.get("mint")) for row in rows}),
        "wallet_n": len({(row.get("network_id"), row.get("source_user_id"))
                         for row in rows if row.get("source_user_id")}),
        "outcome_n": valid_n,
        "invalid_outcome_n": len(rows) - valid_n,
        "realized_pnl": pnl,
        "entry_notional": notional,
        "realized_return": (pnl / notional if notional else None),
    }


def _classification_facts(row):
    try:
        payload = json.loads(row.get("classification_json") or "")
        facts = payload.get("facts") or {}
    except (TypeError, ValueError, AttributeError):
        return None, None
    return payload.get("coin_band"), _finite(facts.get("effective_mcap"), positive=True)


def _trace_events(row):
    try:
        payload = json.loads(row.get("trace_json") or "")
        events = payload.get("substage_events")
    except (TypeError, ValueError, AttributeError):
        return []
    return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []


def _trace_shadows(row):
    for event in _trace_events(row):
        if event.get("name") == "funded_entry_shadow_v1":
            return event
    return {"state": "not_recorded"}


def _gate_diagnostics(rows):
    diagnostics = []
    for row in rows:
        events = _trace_events(row)
        premium = [event for event in events if event.get("name") == "execution_premium"]
        chase = [event for event in events if event.get("name") in
                 {"chase_quote_confirmation", "chase_quote_skipped"}]
        diagnostics.append({
            "position_id": int(row["id"]), "network_id": str(row.get("network_id") or ""),
            "source_cohort": row["source_cohort"], "realized_pnl": _finite(row.get("realized_pnl")),
            "execution_premium": premium[-1] if premium else {"state": "not_recorded"},
            "chase": chase[-1] if chase else {"state": "not_recorded"},
        })
    return diagnostics


def _positions(conn, since):
    rows = conn.execute(
        f"SELECT p.*,a.classification_json,a.trace_json,({signal_source_sql('p')}) AS source_cohort "
        "FROM memecoin_positions p "
        "LEFT JOIN memecoin_entry_attempts a ON a.attempt_id=p.attempt_id "
        "WHERE p.execution_mode='real' AND p.is_open=0 "
        "AND p.opened_recorded_at>=? ORDER BY p.opened_recorded_at,p.id",
        (since,),
    ).fetchall()
    positions = []
    for item in rows:
        row = dict(item)
        band, mcap = _classification_facts(row)
        row["coin_band"] = band
        row["effective_mcap"] = mcap
        row["entry_time"] = _entry_time(row)
        row["source_size"] = _finite(row.get("their_size_usd"), positive=True)
        row["is_manual"] = row["source_cohort"] == "manual"
        row["is_automatic"] = row["source_cohort"] == "copy"
        row["shadows"] = _trace_shadows(row)
        positions.append(row)
    return positions


def _chart_rows(conn, positions):
    if not positions:
        return {}
    ids = [int(row["id"]) for row in positions]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT position_id,observed_at,unit_price FROM memecoin_route_series "
        f"WHERE seam='exit_decision' AND position_id IN ({placeholders}) "
        "ORDER BY position_id,observed_at,id",
        ids,
    ).fetchall()
    grouped = defaultdict(list)
    for row in rows:
        grouped[int(row["position_id"])].append(dict(row))
    return grouped


def _charts(conn, positions):
    route_rows = _chart_rows(conn, positions)
    charts = []
    for row in positions:
        entry_price = _finite(row.get("entry_price"), positive=True)
        entry_at = row.get("entry_time")
        close_at = _aware(row.get("closed_recorded_at"))
        points = []
        if entry_price is not None and entry_at is not None and close_at is not None:
            for route in route_rows.get(int(row["id"]), []):
                observed_at = _aware(route.get("observed_at"))
                unit_price = _finite(route.get("unit_price"), positive=True)
                if (observed_at is None or unit_price is None
                        or not entry_at < observed_at <= close_at):
                    continue
                points.append({
                    "elapsed_s": (observed_at - entry_at).total_seconds(),
                    "return_pct": (unit_price / entry_price - 1.0) * 100.0,
                })
        charts.append({
            "position_id": int(row["id"]),
            "network_id": row.get("network_id"),
            "mint": row.get("mint"),
            "ticker": row.get("ticker"),
            "source_handle": row.get("source_handle"),
            "exit_reason": row.get("exit_reason"),
            "realized_return": _outcome([row])["realized_return"],
            "state": "eligible" if len(points) >= 2 else "no_exact_route_series",
            "points": points if len(points) >= 2 else [],
        })
    return charts


def _coin_groups(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(str(row.get("network_id") or ""), str(row.get("mint") or ""))].append(row)
    result = []
    for (network_id, mint), entries in groups.items():
        summary = _outcome(entries)
        result.append({
            "network_id": network_id,
            "mint": mint,
            "anchor": _coin_anchor(network_id, mint),
            "position_ids": [int(row["id"]) for row in entries],
            "summary": summary,
        })
    return sorted(result, key=lambda row: (row["network_id"], row["mint"]))


def _best_two_removed(rows, *, count=2, positive_only=False):
    groups = _coin_groups(rows)
    ranked = sorted(
        groups,
        key=lambda row: (-row["summary"]["realized_pnl"], row["network_id"], row["mint"]),
    )
    removed = [row for row in ranked if not positive_only or row["summary"]["realized_pnl"] > 0][:count]
    removed_ids = {position_id for group in removed for position_id in group["position_ids"]}
    kept = [row for row in rows if int(row["id"]) not in removed_ids]
    return {
        "removed_coin_groups": [
            {"network_id": row["network_id"], "mint": row["mint"]} for row in removed
        ],
        "summary": _outcome(kept),
    }


def _coin_anchor(network_id, mint):
    digest = hashlib.sha256(f"{network_id}\0{mint}".encode()).hexdigest()[:16]
    return f"coin-{digest}"


def _result_counts(rows):
    counts = {"win_n": 0, "loss_n": 0, "flat_n": 0, "unknown_n": 0}
    for row in rows:
        pnl = _finite(row.get("realized_pnl"))
        notional = _finite(row.get("entry_notional"), positive=True)
        if pnl is None or notional is None:
            counts["unknown_n"] += 1
        elif pnl > 0.0:
            counts["win_n"] += 1
        elif pnl < 0.0:
            counts["loss_n"] += 1
        else:
            counts["flat_n"] += 1
    return counts


def _trader_dossiers(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(str(row.get("network_id") or ""), str(row.get("source_user_id") or ""))].append(row)
    dossiers = []
    for (network_id, source_user_id), entries in groups.items():
        coins = sorted({(str(row.get("network_id") or ""), str(row.get("mint") or ""))
                        for row in entries})
        dossiers.append({
            "network_id": network_id,
            "source_user_id": source_user_id,
            "source_handle": next((row.get("source_handle") for row in entries
                                   if row.get("source_handle")), ""),
            "distinct_coin_n": len(coins),
            "summary": _outcome(entries),
            "best_coin_removed": _best_two_removed(entries, count=1, positive_only=True),
            "result_counts": _result_counts(entries),
            "coins": [{
                "network_id": network_id,
                "mint": mint,
                "label": f"{network_id}:{mint}",
                "anchor": _coin_anchor(network_id, mint),
            } for network_id, mint in coins],
        })
    return sorted(dossiers, key=lambda row: (row["network_id"], row["source_user_id"]))


def _chain_concentration(rows):
    chains = defaultdict(list)
    for row in rows:
        chains[str(row.get("network_id") or "")].append(row)
    result = []
    for network_id, entries in sorted(chains.items()):
        traders = sorted(_trader_dossiers(entries),
                         key=lambda row: (-row["summary"]["realized_pnl"], row["source_user_id"]))
        best = next((row for row in traders
                     if row["source_user_id"] and row["summary"]["realized_pnl"] > 0), None)
        source = best["source_user_id"] if best else None
        result.append({
            "network_id": network_id, "summary": _outcome(entries),
            "best_trader_removed": {"source_user_id": source, "summary": _outcome([
                row for row in entries if source is None or row.get("source_user_id") != source])},
            "best_coin_removed": _best_two_removed(entries, count=1, positive_only=True),
        })
    return result


def _candidate_concentration(rows):
    result = {}
    for name in FIXED_CANDIDATES:
        states = sorted(set(SHADOW_STATES[name]) | {_shadow_state(row.get("shadows"), name) for row in rows})
        result[name] = {
            state: _chain_concentration([row for row in rows if _shadow_state(row.get("shadows"), name) == state])
            for state in states
        }
    return result


def _position_dossiers(rows):
    return [{
        "position_id": int(row["id"]),
        "network_id": str(row.get("network_id") or ""),
        "mint": str(row.get("mint") or ""),
        "source_handle": str(row.get("source_handle") or ""),
        "realized_return": _outcome([row])["realized_return"],
        "source_size": row.get("source_size"),
        "effective_mcap": row.get("effective_mcap"),
        "coin_band": row.get("coin_band"),
        "shadows": row.get("shadows") or {"state": "not_recorded"},
    } for row in rows]


def _feature_rows(rows, key):
    return [row for row in rows if _finite(row.get(key), positive=True) is not None]


def _distribution(rows, key, bins):
    values = [_finite(row.get(key), positive=True) for row in rows]
    valid = [value for value in values if value is not None]
    result = [{"label": label, "n": 0} for _ceiling, label in bins]
    for value in valid:
        for index, (ceiling, _label) in enumerate(bins):
            if value < ceiling:
                result[index]["n"] += 1
                break
    return {"coverage_n": len(valid), "unavailable_n": len(rows) - len(valid), "bins": result}


def _feature_comparison(rows, key, floor):
    discovery = []
    holdout = []
    for row in _feature_rows(rows, key):
        entry_at = row.get("entry_time")
        if entry_at is None:
            continue
        target = discovery if entry_at < _aware(DISCOVERY_END) else holdout
        if _finite(row.get(key), positive=True) >= floor:
            target.append(row)
    return {"floor": floor, "discovery": _outcome(discovery), "holdout": _outcome(holdout)}


def _feature_summary(rows):
    automatic = [row for row in rows if row["is_automatic"]]
    return {
        "source_size": {
            "coverage_n": len(_feature_rows(automatic, "source_size")),
            "distribution": _distribution(automatic, "source_size", SOURCE_SIZE_BINS),
            "comparisons": [_feature_comparison(automatic, "source_size", floor)
                            for floor in (250.0, 500.0)],
        },
        "effective_mcap": {
            "coverage_n": len(_feature_rows(automatic, "effective_mcap")),
            "distribution": _distribution(automatic, "effective_mcap", EFFECTIVE_MCAP_BINS),
            "comparisons": [_feature_comparison(automatic, "effective_mcap", 50_000.0)],
        },
    }


def _shadow_summary(rows):
    summary = {}
    names = dict(SHADOW_STATES)
    for row in rows:
        for name, value in (row.get("shadows") or {}).items():
            if isinstance(value, dict) and "state" in value:
                names.setdefault(name, ("not_recorded",))
    for name, known_states in names.items():
        states = {state: {"sources": set(), "coins": set()} for state in known_states}
        for row in rows:
            state = _shadow_state(row.get("shadows"), name)
            states.setdefault(state, {"sources": set(), "coins": set()})
            source = row.get("source_user_id")
            if source:
                states[state]["sources"].add((str(row.get("network_id") or ""), str(source)))
            states[state]["coins"].add((str(row.get("network_id") or ""), str(row.get("mint") or "")))
        summary[name] = {
            state: {"source_n": len(value["sources"]), "coin_n": len(value["coins"])}
            for state, value in states.items()
        }
    return summary


def build_report(conn, since: str) -> dict:
    """Build a report without writing to the ledger."""
    since = _canonical_since(since)
    rows = _positions(conn, since)
    automatic = [row for row in rows if row["is_automatic"]]
    manual = [row for row in rows if row["is_manual"]]
    charts = _charts(conn, rows)
    range_label = "preregistered" if since == PREREGISTERED_SINCE else "exploratory"
    return {
        "as_of": real_utc_now_iso(),
        "range": {"since": since, "label": range_label},
        "summaries": {
            "all_funded": _outcome(rows),
            "automatic": _outcome(automatic),
            "manual": _outcome(manual),
            "unknown_source": _outcome([row for row in rows if row["source_cohort"] == "unknown"]),
        },
        "historical_compatibility": {
            "legacy_nonmanual": _outcome([row for row in rows if row.get("signal_source") != "manual"]),
        },
        "chain_concentration": _chain_concentration(automatic),
        "candidate_concentration": _candidate_concentration(automatic),
        "gate_diagnostics": _gate_diagnostics(rows),
        "coverage": {
            "position_n": len(rows),
            "coin_n": len({(row.get("network_id"), row.get("mint")) for row in rows}),
            "source_n": len({row.get("source_user_id") for row in rows if row.get("source_user_id")}),
            "automatic_source_n": len({row.get("source_user_id") for row in automatic
                                        if row.get("source_user_id")}),
            "eligible_chart_n": sum(chart["state"] == "eligible" for chart in charts),
            "no_exact_route_series_n": sum(chart["state"] != "eligible" for chart in charts),
        },
        "trader_dossiers": _trader_dossiers(automatic),
        "position_dossiers": _position_dossiers(rows),
        "coin_dossiers": _coin_groups(rows),
        "charts": charts,
        "shadow_summary": _shadow_summary(automatic),
        "feature_summary": _feature_summary(rows),
        "best_two_removed": _best_two_removed(automatic),
        "data_gaps": {
            "open_positions_excluded": conn.execute(
                "SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
                "AND opened_recorded_at>=?", (since,),
            ).fetchone()[0],
            "shadow_not_recorded_n": sum(row["shadows"].get("state") == "not_recorded"
                                       for row in automatic),
        },
    }


def _pct(value):
    return "n/a" if value is None else f"{value * 100.0:+.2f}%"


def _money(value):
    return f"${float(value or 0.0):+,.2f}"


def _optional_money(value):
    return "unavailable" if value is None else _money(value)


def _svg(points):
    width, height, pad = 520, 160, 20
    max_x = max(point["elapsed_s"] for point in points) or 1.0
    values = [0.0] + [point["return_pct"] for point in points]
    low, high = min(values), max(values)
    span = high - low or 1.0
    path = " ".join(
        f"{pad + point['elapsed_s'] / max_x * (width - 2 * pad):.2f},"
        f"{height - pad - (point['return_pct'] - low) / span * (height - 2 * pad):.2f}"
        for point in points
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Exact route series">'
        f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" '
        'stroke="#667085"/><polyline fill="none" stroke="#2e90fa" stroke-width="2" '
        f'points="{path}"/></svg>'
    )


def _summary_table(summaries):
    rows = []
    for name, summary in summaries.items():
        rows.append(
            "<tr>"
            f"<td>{html.escape(name)}</td><td>{summary['position_n']}</td>"
            f"<td>{summary['outcome_n']}</td><td>{_money(summary['realized_pnl'])}</td>"
            f"<td>{_pct(summary['realized_return'])}</td></tr>"
        )
    return "<table><tr><th>cohort</th><th>positions</th><th>valid outcomes</th><th>PnL</th><th>return</th></tr>" + "".join(rows) + "</table>"


def _shadow_state(shadows, name):
    value = shadows.get(name) if isinstance(shadows, dict) else None
    return str(value.get("state") or "unknown") if isinstance(value, dict) else "not_recorded"


def _gate_provenance_html(shadows):
    provenance = shadows.get("gate_provenance_v1")
    if not isinstance(provenance, dict):
        return "<p>Recorded gate states: not_recorded</p>"
    states = provenance.get("selection_gate_states")
    state_text = " ".join(f"{name}={state}" for name, state in sorted(states.items())) if isinstance(states, dict) else "not_recorded"
    result = f"<p>Recorded gate states: {html.escape(state_text)}</p>"
    evaluations = provenance.get("gate_evaluations")
    ceiling = evaluations.get("m5_turnover_over_ceiling") if isinstance(evaluations, dict) else None
    if (isinstance(ceiling, dict) and ceiling.get("effective_ratio") == 0
            and _finite(ceiling.get("configured_ratio"), positive=True) is not None):
        result += (
            "<p>Recorded turnover ceiling inactive: "
            f"configured={html.escape(str(ceiling['configured_ratio']))} "
            f"effective={html.escape(str(ceiling['effective_ratio']))}</p>")
    return result


def _coin_dossier_html(report):
    positions = {row["position_id"]: row for row in report["position_dossiers"]}
    output = []
    for coin in report["coin_dossiers"]:
        position_html = []
        for position_id in coin["position_ids"]:
            row = positions[position_id]
            shadow_text = " ".join(
                f"{name}={_shadow_state(row['shadows'], name)}" for name in report["shadow_summary"])
            position_html.append(
                "<li>"
                f"position={position_id} source={html.escape(row['source_handle'])} "
                f"return={_pct(row['realized_return'])} source_size={_optional_money(row['source_size'])} "
                f"effective_mcap={_optional_money(row['effective_mcap'])} coin_band={html.escape(str(row['coin_band'] or 'unavailable'))} "
                f"{html.escape(shadow_text)}"
                f"{_gate_provenance_html(row['shadows'])}"
                "<details><summary>Recorded shadow and gate evaluations</summary><pre>"
                f"{html.escape(json.dumps(row['shadows'], indent=2, sort_keys=True))}</pre></details>"
                "</li>"
            )
        output.append(
            f'<article id="{html.escape(coin["anchor"])}">'
            f"<h3>{html.escape(coin['network_id'])}:{html.escape(coin['mint'])}</h3>"
            f"<p>PnL={_money(coin['summary']['realized_pnl'])} return={_pct(coin['summary']['realized_return'])}</p>"
            "<ul>" + "".join(position_html) + "</ul></article>"
        )
    return "".join(output)


def _feature_html(label, distribution_label, payload):
    comparisons = []
    for item in payload["comparisons"]:
        comparisons.append(
            f"floor={_money(item['floor'])} discovery={_pct(item['discovery']['realized_return'])} "
            f"holdout={_pct(item['holdout']['realized_return'])}"
        )
    distribution = payload["distribution"]
    bins = "; ".join(f"{item['label']}={item['n']}" for item in distribution["bins"])
    return (
        f"<h2>{html.escape(label)}</h2>"
        f"<h3>{html.escape(distribution_label)}</h3>"
        f"<p>coverage={distribution['coverage_n']} unavailable={distribution['unavailable_n']} {html.escape(bins)}</p>"
        f"<p>{' ; '.join(comparisons)}</p>"
    )


def _shadow_summary_html(summary):
    rows = []
    for name, states in summary.items():
        for state, counts in states.items():
            rows.append(
                "<tr>"
                f"<td>{html.escape(name)}</td><td>{html.escape(state)}</td>"
                f"<td>sources={counts['source_n']}</td><td>coins={counts['coin_n']}</td></tr>"
            )
    return (
        "<h2>Shadow state summary</h2>"
        "<table><tr><th>shadow</th><th>state</th><th>sources</th><th>coins</th></tr>"
        + "".join(rows) + "</table>"
    )


def _concentration_html(chains):
    sections = []
    for chain in chains:
        coin = chain["best_coin_removed"]
        trader = chain["best_trader_removed"]
        removed = ", ".join(row["mint"] for row in coin["removed_coin_groups"]) or "none"
        sections.append(
            f"<h3>{html.escape(chain['network_id'])}</h3>"
            f"<p>Best trader removed={html.escape(trader['source_user_id'] or 'none')}; "
            f"best coin removed={html.escape(removed)}</p>"
            + _summary_table({"automatic": chain["summary"],
                              "best positive trader removed": trader["summary"],
                              "best positive coin removed": coin["summary"]}))
    return "<h2>Automatic concentration by chain</h2>" + "".join(sections)


def _candidate_concentration_html(candidates):
    output = []
    for name, states in candidates.items():
        for state, chains in states.items():
            for chain in chains:
                removed = chain["best_trader_removed"]
                remaining = removed["summary"]
                values = [name, chain["network_id"], state, chain["summary"]["position_n"],
                          _money(chain["summary"]["realized_pnl"]), removed["source_user_id"] or "none",
                          remaining["position_n"], remaining["outcome_n"], remaining["wallet_n"],
                          remaining["coin_n"], _money(remaining["entry_notional"]), _money(remaining["realized_pnl"]),
                          _pct(remaining["realized_return"])]
                output.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in values) + "</tr>")
    return (
        "<h2>Candidate wallet concentration</h2>"
        "<p>Each candidate, chain, and recorded state selects its own largest positive wallet contribution. "
        "The subtraction removes every position from that wallet in that group. No positive wallet means no removal. "
        "These are closed-position diagnostics; the day-14 read also requires open and censored outcomes.</p>"
        '<div style="overflow-x:auto" tabindex="0" role="region" aria-label="Candidate wallet concentration">'
        "<table><tr><th>candidate</th><th>chain</th><th>state</th><th>positions</th><th>PnL</th>"
        "<th>removed wallet</th><th>remaining positions</th><th>remaining valid outcomes</th>"
        "<th>remaining wallets</th><th>remaining coins</th><th>remaining notional</th><th>remaining PnL</th><th>remaining return</th></tr>"
        + "".join(output) + "</table></div>")


def _gate_diagnostics_html(rows):
    output = []
    for row in rows:
        premium, chase = row["execution_premium"], row["chase"]
        values = [row["network_id"], row["position_id"], row["source_cohort"],
                  premium.get("reason", "not_recorded"), premium.get("cap", "not_recorded"),
                  premium.get("reference_basis", "not_recorded"),
                  premium.get("reference_price", "not_recorded"),
                  chase.get("reason", chase.get("name", "not_recorded")),
                  chase.get("cap_pct", "not_recorded"), _optional_money(row["realized_pnl"])]
        output.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in values) + "</tr>")
    return (
        "<h2>Historical gate diagnostics</h2>"
        "<p>Each row uses the last recorded gate event. Absent events remain not_recorded. "
        "Current configuration does not supply historical values.</p>"
        "<table><tr><th>chain</th><th>position</th><th>source cohort</th>"
        "<th>execution_premium reason</th><th>cap</th><th>reference basis</th><th>reference price</th>"
        "<th>chase event or reason</th><th>chase cap pct</th><th>PnL</th></tr>"
        + "".join(output) + "</table>")


def render_html(report: dict) -> str:
    """Render a self-contained HTML report."""
    coverage = report["coverage"]
    traders = "".join(
        "<tr>"
        f"<td>{html.escape(row['network_id'])}</td>"
        f"<td>{html.escape(str(row['source_handle'] or row['source_user_id']))}</td>"
        f"<td>{row['summary']['position_n']}</td><td>{row['distinct_coin_n']}</td>"
        f"<td>{_money(row['summary']['realized_pnl'])}</td>"
        f"<td>{_money(row['best_coin_removed']['summary']['realized_pnl'])}</td>"
        f"<td>{_pct(row['summary']['realized_return'])}</td>"
        f"<td>{row['result_counts']['win_n']}W/{row['result_counts']['loss_n']}L/"
        f"{row['result_counts']['flat_n']}F/{row['result_counts']['unknown_n']}U</td>"
        f"<td>{' '.join(f'<a href=\"#{html.escape(coin["anchor"])}\">{html.escape(coin["label"])}</a>' for coin in row['coins'])}</td>"
        "</tr>"
        for row in report["trader_dossiers"]
    )
    chart_html = []
    for chart in report["charts"]:
        title = " ".join(str(value or "") for value in (
            chart["position_id"], chart["source_handle"], chart["exit_reason"],
        ))
        body = _svg(chart["points"]) if chart["state"] == "eligible" else "no_exact_route_series"
        chart_html.append(
            f"<article><h3>{html.escape(title)}</h3><p>return {_pct(chart['realized_return'])}</p>{body}</article>"
        )
    removed = ", ".join(
        f"{row['network_id']}:{row['mint']}" for row in report["best_two_removed"]["removed_coin_groups"]
    ) or "none"
    prefix = """<!doctype html><html><head><meta charset=\"utf-8\"><title>Funded Memecoin Forensics</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem;color:#182230}table{border-collapse:collapse}th,td{border:1px solid #d0d5dd;padding:.35rem .55rem;text-align:left}article{margin:1rem 0;padding:1rem;border:1px solid #d0d5dd}svg{max-width:100%;background:#f9fafb}</style>
</head><body>"""
    body = (
        "<h1>Funded Memecoin Forensics</h1>"
        f"<p>as_of={html.escape(report['as_of'])} since={html.escape(report['range']['since'])} "
        f"range={html.escape(report['range']['label'])}</p>"
        f"<p>positions={coverage['position_n']} coins={coverage['coin_n']} sources={coverage['source_n']} "
        f"exact_charts={coverage['eligible_chart_n']}</p>"
        + "<h2>Outcome summaries</h2>" + _summary_table(report["summaries"])
        + "<p>Automatic means copy in the frozen funded contract. Manual and unknown contracts stay separate.</p>"
        + _concentration_html(report["chain_concentration"])
        + _candidate_concentration_html(report["candidate_concentration"])
        + "<h2>Automatic traders</h2><table><tr><th>chain</th><th>trader</th><th>positions</th><th>coins</th><th>PnL</th><th>Best positive coin removed: PnL</th><th>return</th><th>results</th><th>coin links</th></tr>"
        + traders + "</table>"
        + "<h2>Best two automatic coin groups removed</h2>"
        f"<p>removed={html.escape(removed)} return={_pct(report['best_two_removed']['summary']['realized_return'])}</p>"
        + "<h2>Historical compatibility</h2>"
        "<p>legacy_nonmanual uses the historical position source label. It does not prove copy identity. "
        "The combined best-two result is a historical diagnostic across chains.</p>"
        + _summary_table(report["historical_compatibility"])
        + _gate_diagnostics_html(report["gate_diagnostics"])
        + "<h2>Coin dossiers</h2>" + _coin_dossier_html(report)
        + _shadow_summary_html(report["shadow_summary"])
        + _feature_html("Source-size evidence", "Source-size distribution", report["feature_summary"]["source_size"])
        + _feature_html("Effective-market-cap evidence", "Effective-market-cap distribution", report["feature_summary"]["effective_mcap"])
        + "<h2>Exact route charts</h2>" + "".join(chart_html)
        + "<h2>Data gaps</h2>"
        f"<p>open_positions_excluded={report['data_gaps']['open_positions_excluded']} "
        f"shadow_not_recorded={report['data_gaps']['shadow_not_recorded_n']}</p>"
        + "</body></html>"
    )
    return prefix + body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="SQLite ledger path")
    parser.add_argument("--since", required=True, help="aware ISO-8601 cohort cutoff")
    parser.add_argument("--output", required=True, help="output HTML path")
    args = parser.parse_args(argv)
    try:
        since = _canonical_since(args.since)
    except ValueError as exc:
        parser.error(str(exc))
    conn = get_readonly_connection(args.db)
    try:
        report = build_report(conn, since)
    finally:
        conn.close()
    Path(args.output).write_text(render_html(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
