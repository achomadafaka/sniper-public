"""Wallet dossier markdown report (§10).

Generated entirely from the in-memory `DossierTables` plus the raw
ProtocolReport dict (section A pulls scalar headline fields that aren't
stored in the dossier tables). NO free-text claims, NO interpretation
that doesn't trace back to a table query.

Section K (LLM operator summary) is opt-in via `include_llm_summary=True`.
The template is fixed — slot-filling only, no free-form additions.

The output is byte-stable: same inputs produce byte-identical markdown.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sniper.analysis.wallet_dossier.builder import DossierTables

# Phrases that must never appear in dossier-generated markdown (§9 + §11.7).
# Lowercased for case-insensitive matching by the §11.7 test.
VOCAB_BLOCKLIST: tuple[str, ...] = (
    "copy target",
    "copy trade",
    "copy-trade",
    "copytrade",
    "mirror this wallet",
    "follow signal",
    "entry trigger",
    "expected pnl",
    "recommended size",
    "recommended position size",
)


def build_markdown(
    *,
    tables: DossierTables,
    protocol_report: dict[str, Any],
    wallet: str,
    fetched_at_iso: str | None = None,
    include_llm_summary: bool = False,
) -> str:
    """Render the dossier markdown per spec §10.

    Args:
        tables: in-memory DossierTables (from build_tables / build_dossier).
        protocol_report: raw ProtocolReport dict — section A reads headline
            scalars from here that aren't tabled (overall_verdict etc.).
        wallet: wallet address (lower-cased in the title).
        fetched_at_iso: optional ISO date string; default derives from
            tables.dossier_meta if available.
        include_llm_summary: when True, append section K with the fixed
            slot-filling template.

    Returns:
        The complete markdown string, ending with a newline.
    """
    sections: list[str] = []
    sections.append(_header(wallet, tables, fetched_at_iso))
    sections.append(_section_a(protocol_report, tables))
    sections.append(_section_b(tables))
    sections.append(_section_c(tables))
    sections.append(_section_d(tables))
    sections.append(_section_e(tables))
    sections.append(_section_f(tables))
    sections.append(_section_g(tables))
    sections.append(_section_h(tables))
    sections.append(_section_i(tables))
    sections.append(_section_j(tables))
    if include_llm_summary:
        sections.append(_section_k(protocol_report, tables))
    return "\n\n".join(s for s in sections if s) + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta(tables: DossierTables, key: str) -> str | None:
    for r in tables.dossier_meta:
        if r["key"] == key:
            return r["value"]
    return None


def _fmt_float(x: Any, places: int = 4) -> str:
    if x is None or x == "":
        return "—"
    try:
        return f"{float(x):.{places}f}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_int(x: Any) -> str:
    if x is None or x == "":
        return "—"
    try:
        return f"{int(x)}"
    except (TypeError, ValueError):
        return str(x)


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Tiny markdown table renderer. All cells are coerced to str."""
    if not rows:
        body_line = "| " + " | ".join("—" for _ in headers) + " |"
    else:
        body_lines = [
            "| " + " | ".join(str(c) for c in r) + " |"
            for r in rows
        ]
        body_line = "\n".join(body_lines)
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    return f"{head}\n{sep}\n{body_line}"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _header(
    wallet: str,
    tables: DossierTables,
    fetched_at_iso: str | None,
) -> str:
    fetched_at = fetched_at_iso or _meta(tables, "fixture_fetched_at") or "unknown"
    return (
        f"# Wallet Dossier — {wallet.lower()} ({fetched_at})\n\n"
        f"_Schema: {_meta(tables, 'schema_version') or 'unknown'}._  \n"
        f"_This report is read-only intelligence. It does NOT recommend "
        f"copying, mirroring, or trading this wallet._"
    )


def _section_a(report: dict[str, Any], tables: DossierTables) -> str:
    fields = [
        "overall_verdict",
        "gate_result",
        "primary_edge_type",
        "copyability_note",
        "archetype_layer1",
        "trading_mode",
        "flip_rate",
        "net_pnl",
        "n_closed_positions",
    ]
    rows = [[f, str(report.get(f) if report.get(f) is not None else "—")] for f in fields]
    body = _md_table(["field", "value"], rows)
    note = (
        "_`copyability_note` is a legacy mirror field on ProtocolReport. "
        "The dossier does not interpret it and never generates new "
        "copyability language._"
    )
    return f"## A. Top-level (read-only mirror)\n\n{body}\n\n{note}"


def _section_b(tables: DossierTables) -> str:
    if not tables.hypothesis_results:
        body = _md_table(
            ["branch", "name", "verdict", "n", "p_value", "effect_size"],
            [],
        )
        return f"## B. Hypothesis tally (read-only mirror)\n\n{body}\n\n_No branches present on the ProtocolReport._"
    rows = [
        [
            r["branch"], r["name"], r["verdict"],
            _fmt_int(r["n"]),
            _fmt_float(r["p_value"]),
            _fmt_float(r["effect_size"]),
        ]
        for r in sorted(
            tables.hypothesis_results, key=lambda r: (r["branch"], r["name"])
        )
    ]
    body = _md_table(
        ["branch", "name", "verdict", "n", "p_value", "effect_size"],
        rows,
    )
    return f"## B. Hypothesis tally (read-only mirror)\n\n{body}"


def _section_c(tables: DossierTables) -> str:
    if not tables.surfaces:
        body = _md_table(["surface_name", "data_state", "key_metrics"], [])
        return f"## C. Surfaces (read-only mirror)\n\n{body}\n\n_No surfaces populated on the ProtocolReport._"
    rows = []
    for r in sorted(tables.surfaces, key=lambda r: r["surface_name"]):
        km = r["key_metrics"] or ""
        # Truncate for readability; the full JSON is in the SQLite.
        if len(km) > 160:
            km = km[:157] + "…"
        rows.append([r["surface_name"], r["data_state"] or "—", km])
    body = _md_table(["surface_name", "data_state", "key_metrics"], rows)
    return f"## C. Surfaces (read-only mirror)\n\n{body}"


def _section_d(tables: DossierTables) -> str:
    pnl_by_bucket: dict[str, dict[str, float]] = {}
    bucket_by_pid = {r["position_id"]: r["accounting_bucket"] for r in tables.diagnostic_accounting}
    for p in tables.positions:
        bucket = bucket_by_pid.get(p["position_id"]) or "OTHER"
        cell = pnl_by_bucket.setdefault(bucket, {"n_positions": 0, "n_episodes": 0, "pnl": 0.0})
        cell["n_positions"] += 1
        cell["pnl"] += float(p.get("realized_pnl") or 0.0)
    n_eps_by_pid: dict[str, int] = Counter(ep["position_id"] for ep in tables.position_exit_episodes)
    for p in tables.positions:
        bucket = bucket_by_pid.get(p["position_id"]) or "OTHER"
        pnl_by_bucket[bucket]["n_episodes"] += n_eps_by_pid.get(p["position_id"], 0)
    rows = [
        [
            b,
            _fmt_int(c["n_positions"]),
            _fmt_int(c["n_episodes"]),
            _fmt_float(c["pnl"], places=2),
        ]
        for b, c in sorted(pnl_by_bucket.items())
    ]
    body = _md_table(
        ["accounting_bucket", "n_positions", "n_episodes", "pnl"], rows,
    )
    note = (
        "_DIAGNOSTIC ONLY — not a strategy thesis source. YES/NO/OTHER "
        "outcome labels are accounting state, not market structure._"
    )
    return f"## D. Diagnostic accounting (YES/NO/OTHER)\n\n{body}\n\n{note}"


def _section_e(tables: DossierTables) -> str:
    def _grouped(key: str) -> list[list[str]]:
        agg: dict[str, dict[str, float]] = {}
        for p in tables.positions:
            k = p.get(key) or "unknown"
            cell = agg.setdefault(k, {"n": 0, "pnl": 0.0})
            cell["n"] += 1
            cell["pnl"] += float(p.get("realized_pnl") or 0.0)
        return [
            [k, _fmt_int(c["n"]), _fmt_float(c["pnl"], places=2)]
            for k, c in sorted(agg.items())
        ]

    parts = []
    parts.append(
        "### market_type\n\n"
        + _md_table(["market_type", "n", "pnl"], _grouped("market_type"))
    )
    parts.append(
        "### entry_price_bucket\n\n"
        + _md_table(["entry_price_bucket", "n", "pnl"], _grouped("entry_price_bucket"))
    )
    parts.append(
        "### outcome_role\n\n"
        + _md_table(["outcome_role", "n", "pnl"], _grouped("outcome_role"))
    )
    # Local data-gaps mirror (§E says "explicit list of what's NULL and why").
    gap_rows = [
        [g["gap_name"], g["severity"], _fmt_int(g.get("affected_grain_count"))]
        for g in sorted(tables.data_gaps, key=lambda g: g["gap_name"])
    ]
    parts.append(
        "### data_gaps (positions affected)\n\n"
        + _md_table(["gap_name", "severity", "affected_grain_count"], gap_rows)
    )
    return "## E. Market semantics (per position)\n\n" + "\n\n".join(parts)


def _section_f(tables: DossierTables) -> str:
    class_counts: dict[str, dict[str, float]] = {}
    for ep in tables.position_exit_episodes:
        k = ep.get("counterfactual_class") or "insufficient_data"
        cell = class_counts.setdefault(k, {"n": 0, "pnl": 0.0})
        cell["n"] += 1
        cell["pnl"] += float(ep.get("episode_pnl") or 0.0)
    total_n = sum(c["n"] for c in class_counts.values()) or 1
    class_rows = [
        [
            k,
            _fmt_int(c["n"]),
            _fmt_float(c["pnl"], places=2),
            _fmt_float(c["n"] / total_n, places=4),
        ]
        for k, c in sorted(class_counts.items())
    ]
    class_table = _md_table(
        ["counterfactual_class", "n_episodes", "pnl", "share_of_episodes"],
        class_rows,
    )

    # Named win-rate metrics with denominators (per spec §F bullet 2).
    # Aggregate across grains, weighted by episode counts.
    held_won = sum(1 for ep in tables.position_exit_episodes if ep.get("counterfactual_class") == "held_won")
    held_lost = sum(1 for ep in tables.position_exit_episodes if ep.get("counterfactual_class") == "held_lost")
    preres_classes = (
        "profitable_exit_later_won", "profitable_exit_later_lost",
        "loss_exit_later_won", "loss_exit_later_lost",
    )
    preres_n = sum(
        1 for ep in tables.position_exit_episodes
        if ep.get("counterfactual_class") in preres_classes
    )
    prof_n = sum(
        1 for ep in tables.position_exit_episodes
        if ep.get("counterfactual_class") in ("profitable_exit_later_won", "profitable_exit_later_lost")
    )
    loss_n = sum(
        1 for ep in tables.position_exit_episodes
        if ep.get("counterfactual_class") in ("loss_exit_later_won", "loss_exit_later_lost")
    )
    outcome_wr = (held_won / (held_won + held_lost)) if (held_won + held_lost) else None
    trade_wr = (prof_n / preres_n) if preres_n else None
    sold_winners_early = (
        sum(1 for ep in tables.position_exit_episodes if ep.get("counterfactual_class") == "profitable_exit_later_won")
        / prof_n
    ) if prof_n else None
    sold_winner_at_loss = (
        sum(1 for ep in tables.position_exit_episodes if ep.get("counterfactual_class") == "loss_exit_later_won")
        / loss_n
    ) if loss_n else None
    loss_cut_correctness = (
        sum(1 for ep in tables.position_exit_episodes if ep.get("counterfactual_class") == "loss_exit_later_lost")
        / loss_n
    ) if loss_n else None
    named_metrics = _md_table(
        ["metric", "value", "denominator"],
        [
            ["outcome_win_rate_held", _fmt_float(outcome_wr), _fmt_int(held_won + held_lost)],
            ["trade_win_rate_preres", _fmt_float(trade_wr), _fmt_int(preres_n)],
            ["sold_winners_early_share", _fmt_float(sold_winners_early), _fmt_int(prof_n)],
            ["sold_winner_at_loss_share", _fmt_float(sold_winner_at_loss), _fmt_int(loss_n)],
            ["loss_cut_correctness_rate", _fmt_float(loss_cut_correctness), _fmt_int(loss_n)],
        ],
    )
    return (
        "## F. Result semantics (the 10-class taxonomy)\n\n"
        + class_table
        + "\n\n### Named win-rate metrics (denominator explicit)\n\n"
        + named_metrics
    )


def _section_g(tables: DossierTables) -> str:
    interpretable_quality = {"interpretable", "interpretable_evidence_only"}
    grain_quality: dict[str, str] = {r["grain_id"]: r["grain_quality"] for r in tables.result_semantics}
    interpretable_ids = {gid for gid, q in grain_quality.items() if q in interpretable_quality}
    rows = []
    for me in sorted(tables.mechanism_evidence, key=lambda r: (r["grain_id"], r["mechanism"])):
        if me["grain_id"] not in interpretable_ids:
            continue
        if me["mechanism"] == "underpowered":
            continue
        gk = next(
            (r["grain_key"] for r in tables.result_semantics if r["grain_id"] == me["grain_id"]),
            "{}",
        )
        gk_short = gk
        if len(gk_short) > 80:
            gk_short = gk_short[:77] + "…"
        candidate = me["candidate_thesis_evidence"] or me["not_promoted_reason"] or "—"
        rows.append([
            gk_short, _fmt_int(me["sample_size"]), me["mechanism"],
            me["evidence_strength"], candidate,
        ])
    body = _md_table(
        ["grain_key", "n", "mechanism", "evidence_strength", "candidate_or_reason"],
        rows,
    )
    note = (
        "_Grains with `grain_quality = interpretable_evidence_only` carry "
        "`metric_supported_but_not_promotable`: math pattern exists, "
        "category gap prevents promotion to candidate_thesis_evidence._"
    )
    return f"## G. Mechanism evidence by grain (interpretable only)\n\n{body}\n\n{note}"


def _section_h(tables: DossierTables) -> str:
    diagnostic_quality = {"diagnostic_only", "not_interpretable_at_this_grain", "underpowered"}
    grain_quality = {r["grain_id"]: r for r in tables.result_semantics}
    rows = []
    for gid, rs in sorted(grain_quality.items()):
        q = rs["grain_quality"]
        if q not in diagnostic_quality:
            continue
        reasons = rs["grain_quality_reasons"] or "[]"
        gk_short = rs["grain_key"]
        if len(gk_short) > 80:
            gk_short = gk_short[:77] + "…"
        rows.append([gk_short, q, _fmt_int(rs["n_positions"]), reasons])
    body = _md_table(
        ["grain_key", "quality", "n_positions", "gate_failures"],
        rows,
    )
    return f"## H. Diagnostic-only grains\n\n{body}"


def _section_i(tables: DossierTables) -> str:
    by_source: Counter[str] = Counter(r["source"] for r in tables.data_lineage)
    total = sum(by_source.values()) or 1
    rows = [
        [src, _fmt_int(n), _fmt_float(n / total, places=4)]
        for src, n in sorted(by_source.items())
    ]
    body = _md_table(["source", "n_claims", "share"], rows)
    return f"## I. Data lineage summary\n\n{body}"


def _section_j(tables: DossierTables) -> str:
    rows = [
        [
            g["gap_name"],
            (g.get("affected_columns") or "[]"),
            g["severity"],
            g.get("unblocking_spec") or "—",
        ]
        for g in sorted(tables.data_gaps, key=lambda g: g["gap_name"])
    ]
    body = _md_table(
        ["gap_name", "affected_columns", "severity", "unblocking_spec"],
        rows,
    )
    return f"## J. Data gaps\n\n{body}"


def _section_k(report: dict[str, Any], tables: DossierTables) -> str:
    """Operator-requested LLM summary — fixed template, slot-filling only.

    This section is rendered deterministically from table queries; no LLM
    call happens here. The slot output is what an LLM would emit verbatim
    if invoked with the template; we render it directly so the report is
    self-contained and byte-stable.
    """
    held_supported = [
        r["name"] for r in tables.hypothesis_results
        if r["branch"] == "held_analyst" and r["verdict"] == "supported"
    ]
    flipper_supported = [
        r["name"] for r in tables.hypothesis_results
        if r["branch"] == "flipper" and r["verdict"] == "supported"
    ]
    class_counts: Counter[str] = Counter(
        ep.get("counterfactual_class") or "insufficient_data"
        for ep in tables.position_exit_episodes
    )
    grain_rows = []
    by_grain = {r["grain_id"]: r for r in tables.result_semantics}
    interpretable_quality = {"interpretable", "interpretable_evidence_only"}
    for me in tables.mechanism_evidence:
        if me["evidence_strength"] != "metric_supported":
            continue
        rs = by_grain.get(me["grain_id"])
        if rs is None or rs["grain_quality"] not in interpretable_quality:
            continue
        promotable = "YES" if me["candidate_thesis_evidence"] else "NO"
        reason = me["not_promoted_reason"] if promotable == "NO" else ""
        grain_rows.append(
            f"  Grain: {rs['grain_key']}\n"
            f"  Mechanism: {me['mechanism']} ({me['evidence_strength']})\n"
            f"  Promotable: {promotable}{f' ({reason})' if reason else ''}"
        )
    gap_lines = [
        f"  - {g['gap_name']} ({g['severity']})"
        for g in sorted(tables.data_gaps, key=lambda g: g["gap_name"])
    ]
    held_lost = class_counts.get("held_lost", 0)
    return (
        "## K. Operator-requested LLM Summary\n\n"
        "This summary is filled from queries against sections A–J. It introduces "
        "no claims absent from those sections.\n\n"
        "[1] Top-level verdict from §A.\n"
        f"    Verdict: {report.get('overall_verdict') or '—'}\n"
        f"    Edge type: {report.get('primary_edge_type') or '—'}\n"
        f"    Net PnL: {report.get('net_pnl') if report.get('net_pnl') is not None else '—'}\n\n"
        "[2] Hypotheses supported from §B.\n"
        f"    Held: {', '.join(held_supported) if held_supported else 'none'}\n"
        f"    Flipper: {', '.join(flipper_supported) if flipper_supported else 'none'}\n\n"
        "[3] Counterfactual class distribution from §F.\n"
        f"    Held won: {class_counts.get('held_won', 0)}, "
        f"Held lost: {held_lost}\n"
        f"    Profitable exit later won: {class_counts.get('profitable_exit_later_won', 0)}, "
        f"Profitable exit later lost: {class_counts.get('profitable_exit_later_lost', 0)}\n"
        f"    Loss exit later won: {class_counts.get('loss_exit_later_won', 0)}, "
        f"Loss exit later lost: {class_counts.get('loss_exit_later_lost', 0)}\n"
        f"    Market still open / unresolved / unknown: "
        f"{class_counts.get('market_still_open', 0) + class_counts.get('unresolved', 0) + class_counts.get('unknown', 0)}\n\n"
        "[4] Metric-supported mechanism evidence by grain from §G.\n"
        + ("\n".join(grain_rows) if grain_rows else "    none\n")
        + "\n\n[5] Data gaps from §J.\n"
        + ("\n".join(gap_lines) if gap_lines else "    none\n")
        + "\n\n[6] What this report DOES NOT say.\n"
        "    - It does not recommend a strategy for this wallet.\n"
        "    - It does not score a thesis as ready for paper or live trading.\n"
        "    - It does not infer category from market titles.\n"
        "    - It does not aggregate broad buckets (YES/NO/OTHER) into theses."
    )


__all__ = ["build_markdown", "VOCAB_BLOCKLIST"]
