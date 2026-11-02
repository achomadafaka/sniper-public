"""§12 — Markdown report formatter for ProtocolReport.

Produces the fixed-section template defined in spec §12. Sections that don't
apply (e.g., flipper branch on a held-only wallet) are explicitly marked
"N/A — [reason]" rather than omitted, so report parsing is uniform.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    ProtocolReport,
    StationarityResult,
    TagGrainResult,
)


def _fmt(v, decimals: int = 4) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{decimals}f}"
    return str(v)


def _fmt_pct(v, decimals: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{decimals}f}%"


def _fmt_pp(v, decimals: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:+.{decimals}f}pp"


def _fmt_ts(unix_ts: int) -> str:
    if not unix_ts:
        return "—"
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _verdict_label(verdict: str, underpowered_reason: str | None) -> str:
    """Render verdict with reason-band when underpowered (I2 closure)."""
    if verdict == "underpowered" and underpowered_reason:
        return f"**{verdict} ({underpowered_reason})**"
    return f"**{verdict}**"


def _hypothesis_block(
    name: str,
    h: HypothesisResult | None,
    *,
    drift_companion: dict | None = None,
) -> list[str]:
    if h is None:
        return [f"#### {name} — N/A (hypothesis not run)"]
    label = _verdict_label(h.verdict, h.underpowered_reason)
    out = [f"#### {h.name} — verdict: {label} (n={h.n})"]
    if h.test_statistic is not None:
        out.append(f"- test_statistic: {_fmt(h.test_statistic)}")
    if h.ci_lower is not None and h.ci_upper is not None:
        out.append(f"- 95% CI: [{_fmt(h.ci_lower)}, {_fmt(h.ci_upper)}]")
    if h.p_value_raw is not None:
        out.append(f"- raw p: {_fmt(h.p_value_raw)}")
    if h.p_value_bh_adjusted is not None:
        out.append(f"- BH-adjusted p: {_fmt(h.p_value_bh_adjusted)}")
    for k, v in (h.details or {}).items():
        out.append(f"- {k}: {_fmt(v)}")
    for note in (h.notes or []):
        out.append(f"- note: {note}")
    # Drift companion lines for H4 only — display-only summaries of the
    # other entry-timing horizons. NEVER feeds verdict logic.
    if drift_companion:
        for horizon_key in ("drift_1h", "drift_24h"):
            summary = drift_companion.get(horizon_key)
            if isinstance(summary, dict):
                out.append(
                    f"- {horizon_key}_summary: mean={_fmt(summary.get('mean'))}, "
                    f"median={_fmt(summary.get('median'))}, "
                    f"n={summary.get('n', 0)}"
                )
    return out


def _stationarity_block(s: StationarityResult | None) -> list[str]:
    if s is None:
        return ["#### Stationarity (CUSUM) — N/A"]
    out = [
        f"#### Stationarity (CUSUM)",
        f"- cusum_status: {s.cusum_status}",
        f"- h_used: {_fmt(s.h_used)}",
        f"- k: {_fmt(s.k)}",
        f"- signaled: {'yes' if s.signaled else 'no'}",
    ]
    if s.signaled:
        out.append(f"- signal_index: {s.signal_index}")
        out.append(f"- pre_regime_n: {s.pre_regime_n}")
        out.append(f"- post_regime_n: {s.post_regime_n}")
        out.append(f"- inference_basis: {s.inference_basis}")
    return out


def _tag_grain_block(grains: list[TagGrainResult]) -> list[str]:
    if not grains:
        return ["#### Tag grains: none enumerated (missing tag_nav_category lookup or no peer baseline)"]
    out = ["#### Tag grains (multi-grain BH pool)"]
    for g in grains:
        verdict_label = g.verdict
        if g.verdict == "underpowered" and g.underpowered_reason:
            verdict_label = f"{g.verdict} ({g.underpowered_reason})"
        out.append(
            f"- {g.grain}:{g.tag} | n={g.n_at_grain} | peer_count={g.peer_count} "
            f"| confidence={g.confidence_flag} | verdict={verdict_label}"
        )
        if g.metric_value is not None:
            out.append(
                f"  - metric={_fmt(g.metric_value)}, baseline={_fmt(g.pop_baseline)}, "
                f"excess={_fmt_pp(g.excess_pp)}, CI=[{_fmt(g.ci_lower)}, {_fmt(g.ci_upper)}]"
            )
        if g.p_value_bh_adjusted is not None:
            out.append(f"  - BH-adjusted p: {_fmt(g.p_value_bh_adjusted)}")
    return out


def _fmt_money(v) -> str:
    if v is None:
        return "—"
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _grain_financial_line(g: TagGrainResult) -> str | None:
    """Render one descriptive grain line for the Edge surface block.

    Returns None when no financials were attached (e.g. empty grain).
    """
    if g.grain_total_pnl is None and g.grain_win_rate is None:
        return None
    return (
        f"- {g.grain}:{g.tag} | n={g.n_at_grain} "
        f"| WR={_fmt_pct(g.grain_win_rate)} "
        f"| ROI={_fmt_pct(g.grain_roi)} "
        f"| PnL={_fmt_money(g.grain_total_pnl)} "
        f"| avg_win={_fmt_money(g.grain_avg_win)} "
        f"| avg_loss={_fmt_money(g.grain_avg_loss)} "
        f"| max_loss={_fmt_money(g.grain_max_realized_loss)}"
    )


def _grain_financial_lines(branch: BranchReport | None) -> list[str]:
    if branch is None or not branch.is_active or not branch.tag_grains:
        return []
    out: list[str] = []
    # Sort by absolute total PnL desc so the strongest signal lands first.
    grains_sorted = sorted(
        branch.tag_grains,
        key=lambda g: abs(g.grain_total_pnl or 0.0),
        reverse=True,
    )
    for g in grains_sorted:
        line = _grain_financial_line(g)
        if line is not None:
            out.append(line)
    return out


def _short_cid(cid: str | None) -> str:
    if not cid:
        return "—"
    s = str(cid)
    return s[:10] + "…" if len(s) > 12 else s


def _top_markets_lines(markets: list[dict], indent: str) -> list[str]:
    out: list[str] = []
    for i, m in enumerate(markets, 1):
        title = (m.get("title") or m.get("event_slug") or _short_cid(m.get("condition_id")))
        outcome = m.get("outcome") or "—"
        pnl = _fmt_money(m.get("pnl"))
        bought = _fmt_money(m.get("total_bought"))
        out.append(
            f"{indent}{i}. \"{title}\" [{outcome}] "
            f"→ PnL={pnl}, totalBought={bought}"
        )
    return out


def _bucket_top_markets_block(bucket: dict, indent: str = "  ") -> list[str]:
    markets = bucket.get("top_markets_by_pnl") or []
    if not markets:
        return []
    out = [f"{indent}- Top markets by realized P&L:"]
    out.extend(_top_markets_lines(markets, indent + "    "))
    return out


def _other_bucket_detail_lines(bucket: dict) -> list[str]:
    """Render OTHER-bucket-specific detail beneath the OTHER summary line."""
    out: list[str] = []
    out.append(
        "  - Definition: closed positions whose outcome label is not in "
        "{Yes, No, Long, Short}. Includes multi-outcome categorical markets "
        "(candidate names, team names), descriptive binary labels (Over/Under), "
        "esports rosters, etc."
    )
    labels = bucket.get("outcome_labels") or []
    if labels:
        out.append("  - Top outcome labels (by count):")
        for entry in labels:
            out.append(
                f"      - {entry['label']!r} (n={entry['n']}, "
                f"PnL={_fmt_money(entry.get('total_pnl'))})"
            )
    pnl_markets = bucket.get("top_markets_by_pnl") or []
    if pnl_markets:
        out.append("  - Top markets by realized P&L:")
        out.extend(_top_markets_lines(pnl_markets, "      "))
    vol_markets = bucket.get("top_markets_by_volume") or []
    if vol_markets:
        out.append("  - Top markets by total bought (volume proxy):")
        out.extend(_top_markets_lines(vol_markets, "      "))
    cov = bucket.get("tag_coverage") or {}
    if cov:
        if cov.get("with_leaf_tags") is None:
            out.append(
                f"  - Tag coverage: not available on this run "
                f"({cov.get('note', 'tag lookup not provided')})"
            )
        else:
            with_leafs = cov.get("with_leaf_tags", 0)
            without_leafs = cov.get("without_leaf_tags", 0)
            total = cov.get("total", with_leafs + without_leafs)
            line = (
                f"  - Tag coverage: with_leaf_tags={with_leafs}/{total}, "
                f"without_leaf_tags={without_leafs}/{total}"
            )
            if cov.get("with_nav_mapping") is not None:
                line += (
                    f"; with_nav_mapping={cov.get('with_nav_mapping')}/{total}, "
                    f"without_nav_mapping={cov.get('without_nav_mapping')}/{total}"
                )
            out.append(line)
    nav = bucket.get("top_nav_categories") or []
    if nav:
        out.append("  - Top nav categories:")
        for entry in nav:
            out.append(f"      - {entry['category']} (n={entry['n']})")
    return out


def _bucket_attribution_lines(bucket: dict, indent: str = "  ") -> list[str]:
    """Render the per-bucket exit-attribution slice when populated."""
    attr = bucket.get("by_attribution")
    if not isinstance(attr, dict):
        return []
    held_w = attr.get("held_to_resolution_win", {"n": 0, "total_pnl": 0.0})
    held_l = attr.get("held_to_resolution_loss", {"n": 0, "total_pnl": 0.0})
    pre_w = attr.get("pre_resolution_profitable_exit", {"n": 0, "total_pnl": 0.0})
    pre_l = attr.get("pre_resolution_loss_cut", {"n": 0, "total_pnl": 0.0})
    unk = attr.get("unresolved_or_unknown", {"n": 0, "total_pnl": 0.0})
    insuf = attr.get("insufficient_data", {"n": 0, "total_pnl": 0.0})

    held_n = held_w["n"] + held_l["n"]
    held_pnl = float(held_w["total_pnl"]) + float(held_l["total_pnl"])
    pre_n = pre_w["n"] + pre_l["n"]
    pre_pnl = float(pre_w["total_pnl"]) + float(pre_l["total_pnl"])

    def _wr(wins_n: int, total_n: int) -> str:
        if total_n <= 0:
            return "—"
        return _fmt_pct(wins_n / total_n)

    out = [f"{indent}- Exit attribution:"]
    out.append(
        f"{indent}    Held to resolution:    n={held_n}, "
        f"win={held_w['n']} (WR_resolution={_wr(held_w['n'], held_n)}), "
        f"PnL={_fmt_money(held_pnl)}"
    )
    out.append(
        f"{indent}    Pre-resolution exit:   n={pre_n}, "
        f"win={pre_w['n']} (WR_exit={_wr(pre_w['n'], pre_n)}), "
        f"PnL={_fmt_money(pre_pnl)}"
    )
    if unk["n"]:
        out.append(
            f"{indent}    Unresolved/unknown:    n={unk['n']}, "
            f"PnL={_fmt_money(unk['total_pnl'])}"
        )
    if insuf["n"]:
        out.append(
            f"{indent}    Insufficient data:     n={insuf['n']}, "
            f"PnL={_fmt_money(insuf['total_pnl'])}"
        )
    return out


def _data_completeness_block(report: ProtocolReport) -> list[str]:
    """Completeness Surface v1 — capture-state + attribution-coverage block.

    Rendered near the top of every report so the reader can see how much of
    the wallet's history was actually inspected before reading any
    performance numbers. Display-only; NEVER feeds verdict logic.

    Two sub-sections plus tiered caveats:

      ### Activity capture
        - activity_rows_fetched (with CAP HIT marker)
        - closed_positions_fetched (with cap marker when truncated)
        - walk_full_history (enabled / disabled)
        - subgraph_fallback_used (true / false)
        - n_unmapped_fills (typed counterpart to ACTIVITY_SUBGRAPH_UNMAPPED)

      ### Attribution coverage
        - n_closed_positions
        - n_outside_activity_window (n + share)
        - n_insufficient_data (n + share)
        - n_attributed (n + share) — sample-size floor for timing
          classification (includes unresolved_or_unknown).
        - n_with_resolved_attribution (n + share) — R10 Option A;
          rendered only when it differs from n_attributed. Held +
          pre-res only; excludes unresolved_or_unknown and
          insufficient_data.

      ### Caveats (rendered conditionally)
        - ACTIVITY_TRUNCATED: cap hit AND not walk_full_history AND not
          subgraph_fallback_used → SELL absence is unreliable.
        - CLOSED_POSITIONS_TRUNCATED: /closed-positions terminated
          abnormally → PnL / hypothesis denominators may be incomplete.
        - SUBGRAPH_UNMAPPED_FILLS: n_unmapped_fills > 0.
        - ATTRIBUTION_COVERAGE_LOW: R10 Option A — fires when
          n_with_resolved_attribution / n_total < 0.50. Uses the
          reliability signal, not the sample-size floor, so an
          unresolved-heavy wallet still surfaces the coverage warning
          even though Timing Surface classifies it as `unclear`
          (per spec §5).
        - LIMITATION (always rendered): partial-fill / multi-SELL not yet
          modeled.
    """
    # Prefer the typed `data_completeness` field when populated (analyze_wallet
    # has run); otherwise fall back to deriving the same numbers inline from
    # capture-state flags + exit_attribution. The fallback path preserves
    # byte-identical Markdown for legacy callers that construct ProtocolReport
    # objects by hand without invoking analyze_wallet.
    dc = report.data_completeness if isinstance(report.data_completeness, dict) else None
    if dc is not None:
        ac = dc.get("activity_capture") or {}
        cov = dc.get("attribution_coverage") or {}
        cpc = dc.get("closed_positions_capture") or {}
        # G4 — tag_lookup_state is a top-level data_completeness key (not nested).
        tag_lookup_state = dc.get("tag_lookup_state")
        activity_rows_fetched = int(ac.get("activity_rows_fetched", report.activity_rows_fetched) or 0)
        activity_cap_hit = bool(ac.get("activity_cap_hit", report.activity_cap_hit))
        closed_positions_fetched = int(cpc.get("closed_positions_fetched", report.n_closed_positions) or 0)
        closed_positions_cap_hit = bool(cpc.get("closed_positions_cap_hit", False))
        walk_full_history = bool(ac.get("walk_full_history", report.walk_full_history))
        subgraph_fallback_used = bool(ac.get("subgraph_fallback_used", report.subgraph_fallback_used))
        n_unmapped_fills = int(ac.get("n_unmapped_fills", report.n_unmapped_fills) or 0)
        n_total = int(cov.get("n_total", 0) or 0)
        n_outside = int(cov.get("n_outside_window", 0) or 0)
        insuf_n = int(cov.get("n_insufficient_data", 0) or 0)
        n_attributed = int(cov.get("n_attributed", max(n_total - insuf_n, 0)) or 0)
        n_with_resolved = int(cov.get("n_with_resolved_attribution", 0) or 0)
        history_full = bool(cov.get("history_assumed_full", True))
    else:
        activity_rows_fetched = report.activity_rows_fetched
        activity_cap_hit = report.activity_cap_hit
        closed_positions_fetched = None
        closed_positions_cap_hit = False
        walk_full_history = report.walk_full_history
        subgraph_fallback_used = report.subgraph_fallback_used
        n_unmapped_fills = report.n_unmapped_fills
        tag_lookup_state = None
        ea = report.exit_attribution if isinstance(report.exit_attribution, dict) else None
        coverage = (ea or {}).get("coverage") or {}
        summary = (ea or {}).get("summary") or {}
        n_total = int(coverage.get("n_total", 0) or 0)
        n_outside = int(coverage.get("n_outside_window", 0) or 0)
        insuf_n = int((summary.get("insufficient_data") or {}).get("n", 0) or 0)
        # n_attributed = Timing Surface's sample-size floor (preserved for
        # backward-compat in the rendered "Attribution coverage" section).
        # Includes unresolved_or_unknown.
        n_attributed = max(n_total - insuf_n, 0)
        # R10 Option A: reliability signal — held_n + preres_n. Excludes
        # unresolved_or_unknown AND insufficient_data. This drives the
        # ATTRIBUTION_COVERAGE_LOW caveat trigger below.
        n_with_resolved = int(coverage.get("n_with_resolved_attribution", 0) or 0)
        history_full = bool(coverage.get("history_assumed_full", True))

    out = ["## Data completeness", "### Activity capture"]
    cap_marker = " (CAP HIT)" if activity_cap_hit else ""
    out.append(f"- activity_rows_fetched: {activity_rows_fetched}{cap_marker}")
    if closed_positions_fetched is not None:
        closed_marker = (
            "CAP HIT — closed history may be truncated"
            if closed_positions_cap_hit
            else "capture complete"
        )
        out.append(
            f"- closed_positions_fetched: "
            f"{closed_positions_fetched} ({closed_marker})"
        )
    out.append(
        f"- walk_full_history: "
        f"{'enabled (full /activity walk)' if walk_full_history else 'disabled (default REST window only)'}"
    )
    out.append(
        f"- subgraph_fallback_used: "
        f"{'true (Goldsky-backfilled)' if subgraph_fallback_used else 'false'}"
    )
    out.append(f"- n_unmapped_fills: {n_unmapped_fills}")
    if tag_lookup_state is not None:
        # G4 — render tag-lookup outcome alongside other capture-state lines.
        # Detail clause helps the operator read intent at a glance.
        _tls_detail = {
            "ok": "lookup ran, tags found",
            "empty": "lookup ran, no tags found for any condition",
            "unavailable": "lookup failed — see TAG_LOOKUP_UNAVAILABLE caveat",
            "skipped": "--no-tags requested",
        }.get(tag_lookup_state, "")
        if _tls_detail:
            out.append(f"- tag_lookup_state: {tag_lookup_state} ({_tls_detail})")
        else:
            out.append(f"- tag_lookup_state: {tag_lookup_state}")

    out.append("### Attribution coverage")
    if n_total <= 0:
        out.append("- n_closed_positions: 0 (no closed positions to attribute)")
    else:
        out.append(f"- n_closed_positions: {n_total}")
        out.append(
            f"- n_outside_activity_window: {n_outside} "
            f"({_fmt_pct(n_outside / n_total) if n_total else '—'})"
        )
        out.append(
            f"- n_insufficient_data: {insuf_n} "
            f"({_fmt_pct(insuf_n / n_total) if n_total else '—'})"
        )
        out.append(
            f"- n_attributed: {n_attributed} "
            f"({_fmt_pct(n_attributed / n_total) if n_total else '—'}) "
            "(sample-size floor for timing classification)"
        )
        # R10 Option A: only render the resolved-attribution coverage line
        # when it differs from n_attributed (i.e. there are unresolved
        # positions). Keeps the section compact on clean captures.
        if n_with_resolved != n_attributed:
            out.append(
                f"- n_with_resolved_attribution: {n_with_resolved} "
                f"({_fmt_pct(n_with_resolved / n_total) if n_total else '—'}) "
                "(held-to-resolution + pre-resolution exits; excludes "
                "unresolved_or_unknown and insufficient_data)"
            )

    capture_state_caveats: list[str] = []
    if activity_cap_hit and not walk_full_history and not subgraph_fallback_used:
        capture_state_caveats.append(
            "ACTIVITY_TRUNCATED: /activity hit the 3000-row cap and was not "
            "backfilled (walk_full_history disabled, subgraph fallback not used). "
            "SELL absence cannot be distinguished from missed-by-cap SELL events; "
            "positions older than the truncation window are reported as "
            "`insufficient_data`."
        )
    if closed_positions_cap_hit:
        capture_state_caveats.append(
            "CLOSED_POSITIONS_TRUNCATED: /closed-positions terminated before "
            "confirming the full closed-position history. PnL and hypothesis "
            "denominators may be incomplete."
        )
    if tag_lookup_state == "unavailable":
        capture_state_caveats.append(
            "TAG_LOOKUP_UNAVAILABLE: tag-grain lookups could not run "
            "(missing market_tags table or other DB error). Category and "
            "specialist extraction is degraded for this report."
        )
    if n_unmapped_fills > 0:
        capture_state_caveats.append(
            f"SUBGRAPH_UNMAPPED_FILLS: {n_unmapped_fills} subgraph fills "
            "could not be normalized (no condition_id mapping). Real fills may "
            "be missing from the activity view; treat exit attribution on "
            "affected positions with caution."
        )
    # R10 Option A: caveat fires from the reliability signal
    # (n_with_resolved_attribution / n_total), not the sample-size floor
    # (n_attributed / n_total). This means a wallet dominated by
    # unresolved_or_unknown — which Timing Surface deliberately classifies
    # as `unclear`, not `underpowered` — still surfaces the coverage warning
    # to operators / W2 copyability consumers.
    if n_total > 0 and (n_with_resolved / n_total) < 0.50:
        coverage_pct = _fmt_pct(n_with_resolved / n_total)
        capture_state_caveats.append(
            f"ATTRIBUTION_COVERAGE_LOW: only {n_with_resolved}/{n_total} "
            f"({coverage_pct}) of closed positions have reliable exit "
            "attribution. Pre-resolution vs held-to-resolution splits below "
            "are recent-window-only and may not represent the wallet's "
            "lifetime behaviour."
        )
    # The partial-fill / multi-SELL limitation is a property of the
    # attribution model itself (not of any single run), so it's surfaced
    # whenever a closed-position universe exists. Tracked separately from
    # capture-state caveats so the "no warnings fired" line still renders
    # on a clean capture.
    limitation_line: str | None = None
    if n_total > 0:
        limitation_line = (
            "LIMITATION: exit attribution treats each position as a single "
            "BUY → optional SELL → resolution flow. Partial fills and "
            "multi-SELL exit ladders are not yet modelled — a position with "
            "one captured SELL is classified `pre_resolution_*` even if a "
            "tail remained held to resolution."
        )

    if capture_state_caveats or limitation_line:
        out.append("### Caveats")
        if capture_state_caveats:
            for c in capture_state_caveats:
                out.append(f"- {c}")
        elif n_total > 0 and history_full:
            out.append(
                "- None — capture window covers the closed-position universe."
            )
        if limitation_line:
            out.append(f"- {limitation_line}")
    return out


def _outcome_side_lines(report: ProtocolReport) -> list[str]:
    """Render the YES / NO / OTHER bucket breakdown when populated."""
    osb = report.outcome_side_breakdown
    if not osb:
        return []
    out: list[str] = []
    for key in ("yes", "no", "other"):
        bucket = osb.get(key)
        if not bucket:
            continue
        share = bucket.get("share_of_closed")
        share_str = (
            f", {share*100:.1f}% of closed" if isinstance(share, (int, float)) and share > 0
            else ""
        )
        out.append(
            f"- {key.upper()}: n={bucket.get('n', 0)}{share_str} "
            f"| WR={_fmt_pct(bucket.get('win_rate'))} "
            f"| ROI={_fmt_pct(bucket.get('roi'))} "
            f"| PnL={_fmt_money(bucket.get('total_pnl'))}"
        )
        if key == "other":
            out.extend(_other_bucket_detail_lines(bucket))
        else:
            # YES / NO get a compact top-markets sub-bullet (no full detail).
            out.extend(_bucket_top_markets_block(bucket))
        # Exit attribution slice (rendered for every bucket when present).
        out.extend(_bucket_attribution_lines(bucket))
    caveat = osb.get("caveat")
    if caveat:
        out.append(f"- CAVEAT: {caveat}")
    out.append(
        "- Note: realizedPnl is net of resolution proceeds vs entry "
        "notional; not adjusted for slippage on copy execution. "
        "WR_resolution measures prediction skill; WR_exit measures "
        "pre-resolution trading skill. Coverage / caveats rendered above "
        "in `## Data completeness`."
    )
    return out


def _wallet_shape_lines(report: ProtocolReport) -> list[str]:
    """Render the extractor features summary when populated."""
    feats = report.extractor_features_summary
    if not feats:
        return []
    out: list[str] = []
    for key in (
        "usdc_size_cv",
        "top_condition_fill_share",
        "redeem_ratio",
        "domain_concentration",
        "pnl_cv_abs",
    ):
        if key in feats:
            out.append(f"- {key}: {_fmt(feats[key])}")
    return out


def _edge_surface_block(report: ProtocolReport) -> list[str]:
    """Edge Surface v1 — descriptive performance surfaces. Display-only.

    Compose three sub-sections when their inputs are populated:
        - Per-grain financials (held / flipper)
        - Outcome-side breakdown (YES / NO / OTHER)
        - Wallet shape (extractor features)
    Section returns empty when none of the sub-sections has content.
    """
    held_grain_lines = _grain_financial_lines(report.held_analyst_branch)
    flip_grain_lines = _grain_financial_lines(report.flipper_branch)
    outcome_lines = _outcome_side_lines(report)
    shape_lines = _wallet_shape_lines(report)
    # G6 — category cluster summary (audit 2026-05-10). Rendered when the
    # producer emitted `category_clusters` (requires leafs_by_cid + nav_lookup
    # at analyze_wallet time AND at least one nav-mapped position).
    cluster_lines = _category_cluster_lines(report)

    if not (held_grain_lines or flip_grain_lines or outcome_lines or shape_lines or cluster_lines):
        return []

    out: list[str] = ["## Edge surface"]
    if held_grain_lines:
        out.append("### Per-grain financials (held)")
        out.extend(held_grain_lines)
    if flip_grain_lines:
        out.append("### Per-grain financials (flipper)")
        out.extend(flip_grain_lines)
    if outcome_lines:
        out.append("### Outcome-side breakdown")
        out.extend(outcome_lines)
    if cluster_lines:
        out.append("### Category clusters")
        out.extend(cluster_lines)
    if shape_lines:
        out.append("### Wallet shape (extractor features)")
        out.extend(shape_lines)
    return out


def _category_cluster_lines(report: ProtocolReport) -> list[str]:
    """G6 render: one bullet per cluster with category / n / pnl / share.

    First cluster (sort-first) gets a `(primary)` annotation. Share is
    cluster.pnl / cluster.total_pnl; when total_pnl == 0 we render
    `share=—` to avoid implying a numeric share for zero-PnL wallets.
    Returns [] when outcome_side_breakdown has no `category_clusters` key.
    """
    osb = report.outcome_side_breakdown
    if not isinstance(osb, dict):
        return []
    clusters = osb.get("category_clusters") or []
    if not clusters:
        return []
    out: list[str] = []
    for idx, cluster in enumerate(clusters[:5]):
        cat = cluster.get("category", "?")
        n = int(cluster.get("n") or 0)
        pnl = float(cluster.get("pnl") or 0.0)
        total = float(cluster.get("total_pnl") or 0.0)
        if total > 0:
            share_str = f"share={pnl / total * 100:+.1f}%"
        else:
            share_str = "share=—"
        primary = " (primary)" if idx == 0 else ""
        out.append(
            f"- {cat}: n={n}, pnl={_fmt_money(pnl)}, {share_str}{primary}"
        )
    return out


def _classification_interpretation(ts: dict) -> str:
    """Return the plain-language interpretation paragraph for a
    classification. Allowed-phrasing-only — see spec §8.7 + audit
    forbidden-phrase list."""
    cls = ts["classification"]
    held = ts["held_to_resolution"]
    pre  = ts["pre_resolution"]
    n_attr = ts["n_attributed"]

    if cls == "holding_driven":
        return (
            f"This wallet's gains came predominantly from holding positions "
            f"through resolution. Of {n_attr} attributed positions, "
            f"{held['n']} were held-to-resolution and {held['wins']}/{held['n']} "
            f"resolved in the wallet's favor "
            f"(WR={_fmt_pct(held['win_rate'])}, "
            f"avg captured travel={_fmt_pct(held['avg_captured_travel'])}). "
            f"Pre-resolution exits contributed "
            f"{_fmt_money(pre['total_pnl'])} over {pre['n']} trades."
        )
    if cls == "trading_driven":
        return (
            f"This wallet's gains came predominantly from pre-resolution exits. "
            f"Of {n_attr} attributed positions, {pre['n']} closed via SELL "
            f"before resolution; {pre['wins']}/{pre['n']} of those exited "
            f"profitably (WR={_fmt_pct(pre['win_rate'])}, "
            f"avg observed exit move (winners)="
            f"{_fmt_pct(pre.get('avg_sw_sell_move_winners'))}). "
            f"Held-to-resolution positions contributed "
            f"{_fmt_money(held['total_pnl'])} over {held['n']} trades."
        )
    if cls == "mixed":
        return (
            f"Both holding and pre-resolution trading contributed to this "
            f"wallet's results. Held-to-resolution: {held['n']} trades, "
            f"WR={_fmt_pct(held['win_rate'])}, "
            f"PnL={_fmt_money(held['total_pnl'])}. "
            f"Pre-resolution exits: {pre['n']} trades, "
            f"WR={_fmt_pct(pre['win_rate'])}, "
            f"PnL={_fmt_money(pre['total_pnl'])}. "
            f"Neither pattern dominated."
        )
    if cls == "unclear":
        return (
            f"Neither holding nor pre-resolution trading clearly explains "
            f"this wallet's results. Held-to-resolution and pre-resolution "
            f"buckets each have material n but no single bucket exceeds "
            f"the share/PnL thresholds for a confident label."
        )
    if cls == "underpowered":
        if ts.get("coverage_downgraded"):
            return (
                "Timing evidence is limited — Completeness caveats fired "
                "(capture truncated, unmapped fills, or attribution coverage "
                "<50%). Captured-travel numbers below are still reported but "
                "should not drive copy decisions. See `## Data completeness`."
            )
        return (
            f"Timing evidence is limited — {ts['classification_reason']}. "
            f"Captured-travel numbers below are reported but should not "
            f"drive copy decisions."
        )
    return ""


def _timing_surface_block(report: ProtocolReport) -> list[str]:
    """Timing Surface v1 — captured-travel-only classification block.

    Renders nothing when report.timing_surface is None (no closed
    positions). NEVER feeds verdict, gate, or primary_edge_type.

    See spec §8 (commit d598cf2, r2).
    """
    ts = report.timing_surface
    if not isinstance(ts, dict):
        return []

    out = ["## Timing surface", "", f"Classification: {ts['classification']}"]
    interpretation = _classification_interpretation(ts)
    if interpretation:
        out.append(interpretation)

    out.append("")

    held = ts["held_to_resolution"]
    if held["n"] > 0:
        out.append(
            f"- Held to resolution: n={held['n']} ({held['wins']} win / "
            f"{held['losses']} loss, WR={_fmt_pct(held['win_rate'])}, "
            f"avg captured travel="
            f"{_fmt_pct(held['avg_captured_travel'])}), "
            f"PnL={_fmt_money(held['total_pnl'])}"
        )

    pre = ts["pre_resolution"]
    if pre["n"] > 0:
        out.append(
            f"- Pre-resolution exit: n={pre['n']} ({pre['wins']} win / "
            f"{pre['losses']} loss, WR={_fmt_pct(pre['win_rate'])}, "
            f"avg observed exit move (winners)="
            f"{_fmt_pct(pre.get('avg_sw_sell_move_winners'))}), "
            f"PnL={_fmt_money(pre['total_pnl'])}"
        )

    # Confidence + coverage caveats.
    if ts["confidence_band"] != "high" or ts["coverage_caveats"]:
        out.append(f"- Confidence: {ts['confidence_band']}")
    if ts["coverage_caveats"]:
        out.append(
            f"- Coverage caveats: {', '.join(ts['coverage_caveats'])} "
            f"(see `## Data completeness`)"
        )

    # NOTE: render-language guard — do not include any Task 7 forbidden
    # repricing phrase. The note below describes the v1 limitation in
    # allowed phrasing only.
    out.append(
        "- Note: timing surface describes captured travel only. Strict "
        "market-repricing timing analysis requires external price "
        "history and is not implemented in v1 "
        "(`future_repricing_available=false`)."
    )
    return out


def _position_building_block(report: ProtocolReport) -> list[str]:
    """Position Building Surface v1 (W2) — compact summary section.

    Renders the wallet-level summary fields per spec §5.4:
      - dominant_accumulation_pattern
      - dominant_limitation
      - observation_window_coverage
      - high_confidence_position_count
      - low_confidence_position_count

    Display-only. NEVER feeds verdict, gate, primary_edge_type, or
    copyability_note. No triggers, no recommendations, no copy/trigger
    language. Caveat-aware: when `position_building_surface` is None
    (compute returned None because no usable activity rows and no closed
    positions), the section prints a clean "unavailable" line rather than
    silently omitting — keeps the output structure uniform across wallets.

    See docs/superpowers/specs/2026-05-10-wallet-position-builder-design.md.
    """
    pbs = report.position_building_surface
    if not isinstance(pbs, dict):
        return [
            "## Position building",
            "- Position building surface unavailable — no usable activity rows or closed positions captured.",
        ]
    summary = pbs.get("wallet_summary") or {}
    out: list[str] = ["## Position building"]
    n_positions = summary.get("n_positions")
    n_reconstructed = summary.get("n_reconstructed_positions")
    if isinstance(n_positions, int) and isinstance(n_reconstructed, int):
        out.append(
            f"- n_positions: {n_positions} "
            f"(reconstructed={n_reconstructed})"
        )
    dominant_pattern = summary.get("dominant_accumulation_pattern")
    out.append(
        "- dominant_accumulation_pattern: "
        f"{dominant_pattern if dominant_pattern else 'unavailable'}"
    )
    dominant_limitation = summary.get("dominant_limitation")
    out.append(
        "- dominant_limitation: "
        f"{dominant_limitation if dominant_limitation else 'none observed'}"
    )
    coverage = summary.get("observation_window_coverage")
    if isinstance(coverage, (int, float)):
        out.append(
            f"- observation_window_coverage: {_fmt_pct(coverage)} "
            "(share of positions with good or partial observation quality)"
        )
    else:
        out.append(
            "- observation_window_coverage: — "
            "(no positions to evaluate)"
        )
    high_n = summary.get("high_confidence_position_count")
    low_n = summary.get("low_confidence_position_count")
    if isinstance(high_n, int):
        out.append(
            f"- high_confidence_position_count: {high_n} "
            "(observation_window_quality == 'good')"
        )
    if isinstance(low_n, int):
        out.append(
            f"- low_confidence_position_count: {low_n} "
            "(observation_window_quality in 'poor' or 'unavailable')"
        )
    out.append(
        "- Note: position-building summary describes how the wallet built "
        "its observed positions; observation-quality language flags capture "
        "limitations rather than overclaiming reconstruction certainty. "
        "Display-only — no triggers, no entry recommendations."
    )
    return out


def _liquidity_surface_block(report: ProtocolReport) -> list[str]:
    """Liquidity Surface v1 — proxy-only friction context.

    Display-only. NEVER feeds verdict, gate, primary_edge_type,
    copyability_note, or data_quality_flags. Renders degradation states inside
    this section only.
    """
    surface = report.liquidity_surface
    if not isinstance(surface, dict):
        return []
    summary = surface.get("summary") or {}
    degradation = surface.get("degradation") or {}
    out = ["## Liquidity surface"]
    out.append(f"- status: {surface.get('status', 'data_unavailable')}")
    out.append(f"- fidelity_tier: {surface.get('fidelity_tier', 'unknown')}")
    out.append(
        "- historical_depth_available: "
        f"{bool(surface.get('historical_depth_available', False))}"
    )
    out.append(
        f"- markets: {summary.get('n_markets', 0)}, "
        f"trade_rows: {summary.get('n_trade_rows', 0)}, "
        f"modeled_trade_rows: {summary.get('n_modeled_trade_rows', 0)}"
    )
    out.append(
        "- weighted_execution_cost_bps: "
        f"{_fmt(summary.get('weighted_execution_cost_bps'))}"
    )
    states = degradation.get("states") or []
    reasons = degradation.get("reasons") or []
    if states:
        out.append(f"- degradation_states: {', '.join(str(s) for s in states)}")
    if reasons:
        out.append(f"- caveats: {', '.join(str(r) for r in reasons)}")
    observations = surface.get("market_observations") or []
    if observations:
        out.append("- top_markets:")
        for market in observations[:5]:
            tokens = market.get("tokens") or []
            token_bits = []
            for token in tokens[:3]:
                slip = token.get("modeled_slippage") or {}
                token_bits.append(
                    f"{token.get('outcome', 'unknown')} "
                    f"{token.get('liquidity_bucket', 'unknown')} "
                    f"cost_bps={_fmt(slip.get('weighted_cost_bps'))}"
                )
            out.append(
                f"  - {market.get('condition_id', 'unknown')}: "
                f"pnl={_fmt_money(market.get('realized_pnl'))}; "
                + "; ".join(token_bits)
            )
    out.append(
        "- Note: liquidity surface is display-only and proxy-only. It does "
        "not reconstruct historical order-book depth and does not change "
        "verdict, gate, edge type, copyability note, or quality flags."
    )
    return out


def _capital_risk_block(report: ProtocolReport) -> list[str]:
    """Capital & Risk surface — capital base, drawdown, recycle, return.

    Display-only. NEVER feeds verdict, gate, primary_edge_type,
    copyability_note, or data_quality_flags.
    """
    surface = report.capital_risk_surface
    if not isinstance(surface, dict):
        return []
    out = ["## Capital & risk"]
    out.append(f"- data_state: {surface.get('data_state', 'data_unavailable')}")
    out.append(
        "- peak_concurrent_deployed: "
        f"{_fmt_money(surface.get('peak_concurrent_deployed_usdc'))}  "
        "(capital base — peak USDC committed at once)"
    )
    out.append(
        "- cumulative_deployed: "
        f"{_fmt_money(surface.get('cumulative_deployed_usdc'))}  "
        f"(recycle_factor: {_fmt(surface.get('recycle_factor'))}x)"
    )
    out.append(
        "- max_drawdown: "
        f"{_fmt_money(surface.get('max_drawdown_usdc'))}  "
        f"(pct_of_peak: {_fmt(surface.get('max_drawdown_pct_of_peak'))})"
    )
    span = surface.get("lifespan_days")
    out.append(
        "- return_on_peak_deployed: "
        f"{_fmt(surface.get('return_on_peak_deployed'))}"
        + (f"  over {_fmt(span)} days" if span is not None else "")
    )
    pc = surface.get("pnl_concentration") or {}
    if pc:
        out.append(
            f"- pnl_concentration: {pc.get('bucket', 'unknown')} "
            f"(top5_share={_fmt(pc.get('top5_share_of_gross_profit'))}, "
            f"gini={_fmt(pc.get('gini'))}, "
            f"n_winning={pc.get('n_winning_positions', 0)})"
        )
    caveats = surface.get("caveats") or []
    if caveats:
        out.append(f"- caveats: {'; '.join(str(c) for c in caveats)}")
    out.append(
        "- Note: capital & risk surface is display-only — it does not change "
        "verdict, gate, edge type, copyability note, or quality flags. "
        "peak_concurrent_deployed estimates the capital base: same-second "
        "event ordering can slightly overstate it, while true bankroll is "
        ">= the peak (idle USDC is not counted)."
    )
    return out


def _sizing_outcome_block(report: ProtocolReport) -> list[str]:
    """Sizing & Outcome surface — does entry sizing associate with outcome
    quality. Display-only. NEVER feeds verdict, gate, primary_edge_type,
    copyability_note, or data_quality_flags. Reports association, not skill.
    """
    surface = report.sizing_outcome_surface
    if not isinstance(surface, dict):
        return []
    out = ["## Sizing & outcome"]
    out.append(f"- data_state: {surface.get('data_state', 'data_unavailable')}")
    out.append(f"- association: {surface.get('association', 'inconclusive')}")
    out.append(
        f"- global_rho: {_fmt(surface.get('global_rho'))} "
        f"(p={_fmt(surface.get('global_rho_p'))}, "
        f"n={surface.get('n_datapoints', 0)})"
    )
    out.append(
        "- stratified_rho: "
        f"{_fmt(surface.get('stratified_rho'))}  "
        "(category-weighted — the honest headline)"
    )
    out.append(
        "- leave_one_out_rho: "
        f"{_fmt(surface.get('leave_one_out_rho'))}  "
        f"(robustness_holds: {surface.get('robustness_holds')})"
    )
    out.append(
        "- late_conviction_ratio: "
        f"{_fmt(surface.get('late_conviction_ratio'))}  "
        f"(added_after_entry: {_fmt_money(surface.get('added_after_entry_usdc'))})"
    )
    buckets = surface.get("size_buckets") or []
    if buckets:
        out.append("- entry-size quartiles (low→high):")
        for b in buckets:
            out.append(
                f"  - Q{b.get('quartile')}: n={b.get('n')}, "
                f"mean_entry={_fmt_money(b.get('mean_entry_size_usdc'))}, "
                f"WR={_fmt_pct(b.get('win_rate'))}, "
                f"mean_roi={_fmt_pct(b.get('mean_roi'))}"
            )
    by_cat = surface.get("by_category") or []
    if by_cat:
        out.append(
            "- by_category: "
            + ", ".join(f"{c.get('category')} (n={c.get('n')}, "
                        f"rho={_fmt(c.get('rho'))})" for c in by_cat)
        )
    caveats = surface.get("caveats") or []
    if caveats:
        out.append(f"- caveats: {'; '.join(str(c) for c in caveats)}")
    out.append(
        "- Note: sizing & outcome surface is display-only — it does not "
        "change verdict, gate, edge type, copyability note, or quality "
        "flags. It reports an ASSOCIATION between entry size and outcome, "
        "never 'skill'; only thesis-specific simulation can promote it."
    )
    return out


def _null_baseline_block(report: ProtocolReport) -> list[str]:
    """Null-Strategy Baseline surface — did the wallet's side selection beat
    the chalk. Display-only. NEVER feeds verdict, gate, primary_edge_type,
    copyability_note, or data_quality_flags.
    """
    surface = report.null_baseline_surface
    if not isinstance(surface, dict):
        return []
    out = ["## Null-strategy baseline"]
    out.append(f"- data_state: {surface.get('data_state', 'data_unavailable')}")
    out.append(f"- comparison: {surface.get('comparison', 'inconclusive')}")
    out.append(
        f"- eligible: {surface.get('n_eligible', 0)} / "
        f"{surface.get('n_total_closed', 0)} closed "
        f"(coverage {_fmt(surface.get('coverage'))})"
    )
    out.append(
        "- wallet_ew_roi: "
        f"{_fmt_pct(surface.get('wallet_ew_roi'))}  "
        "(the wallet's chosen side, hold-to-resolution, flat-staked)"
    )
    out.append(
        f"- favorite_ew_roi: {_fmt_pct(surface.get('favorite_ew_roi'))}  "
        f"underdog_ew_roi: {_fmt_pct(surface.get('underdog_ew_roi'))}  "
        f"random_ew_roi: {_fmt_pct(surface.get('random_ew_roi'))}"
    )
    out.append(
        "- edge_vs_favorite: "
        f"{_fmt_pct(surface.get('edge_vs_favorite'))}  "
        "(wallet minus always-favorite — the headline)"
    )
    out.append(
        f"- favorite_hit_rate: {_fmt_pct(surface.get('favorite_hit_rate'))}  "
        f"(wallet on favorite: {surface.get('n_on_favorite', 0)}, "
        f"on underdog: {surface.get('n_on_underdog', 0)})"
    )
    caveats = surface.get("caveats") or []
    if caveats:
        out.append(f"- caveats: {'; '.join(str(c) for c in caveats)}")
    out.append(
        "- Note: null-strategy baseline is display-only — it does not change "
        "verdict, gate, edge type, copyability note, or quality flags. It "
        "measures SIDE SELECTION only (not market selection or sizing), "
        "scoring the wallet's chosen side hold-to-resolution; the "
        "favorite/underdog split assumes a binary market whose two sides "
        "sum to ~1. It reports whether the wallet beat the chalk, never "
        "'skill'."
    )
    return out


def _survivorship_block(report: ProtocolReport) -> list[str]:
    """Survivorship surface — flags winner-ranked discovery. Display-only.
    NEVER feeds verdict, gate, primary_edge_type, copyability_note, or
    data_quality_flags.
    """
    surface = report.survivorship_surface
    if not isinstance(surface, dict):
        return []
    out = ["## Survivorship"]
    out.append(f"- discovery_source: {surface.get('discovery_source')}")
    sampled = surface.get("survivor_sampled")
    out.append(
        "- survivor_sampled: "
        + ("yes — winner-ranked discovery" if sampled is True
           else "unknown")
    )
    caveat = surface.get("caveat")
    if caveat:
        out.append(f"- caveat: {caveat}")
    note = surface.get("note")
    if note:
        out.append(f"- Note: {note}")
    return out


def _position_reconciliation_block(report: ProtocolReport) -> list[str]:
    """Position Reconciliation surface — closed-positions vs Redemption events.

    Display-only. NEVER feeds verdict, gate, primary_edge_type,
    copyability_note, or data_quality_flags. When downstream_lower_bound=True,
    the caller inserts this block BEFORE the quantitative surfaces so the
    reader sees the caveat first.
    """
    surface = report.position_reconciliation_surface
    if not isinstance(surface, dict):
        return []
    out = ["## Position reconciliation"]
    out.append(f"- data_state: {surface.get('data_state', 'data_unavailable')}")

    # Row-vs-condition distinction: closed_positions_count may exceed
    # closed_condition_count when the API returns multiple rows per condition.
    closed_rows = surface.get("closed_positions_count")
    closed_conds = surface.get("closed_condition_count")
    if closed_rows is not None:
        annotation = (
            f"  (row count; {closed_conds} unique conditions)"
            if closed_conds is not None and closed_rows != closed_conds
            else ""
        )
        out.append(f"- closed_positions_count: {closed_rows}{annotation}")
    if closed_conds is not None:
        out.append(f"- closed_condition_count: {closed_conds}  (unique conditionIds used in set arithmetic)")

    redeemed = surface.get("redeemed_condition_count")
    if redeemed is not None:
        out.append(f"- redeemed_condition_count: {redeemed}")

    not_in_closed = surface.get("redeemed_not_in_closed_count")
    if not_in_closed is not None:
        out.append(f"- redeemed_not_in_closed_count: {not_in_closed}")

    ratio = surface.get("undercount_ratio")
    if ratio is not None:
        out.append(f"- undercount_ratio: {ratio:.4f}  ({ratio * 100:.1f}% of redeemed conditions missing from /closed-positions)")

    ob_traced = surface.get("redeemed_orderbook_traced_count")
    if ob_traced is not None:
        out.append(f"- redeemed_orderbook_traced_count: {ob_traced}")

    untraced = surface.get("untraced_redemption_count")
    if untraced is not None:
        rails = surface.get("rails_observed")
        aux_unknown = (
            isinstance(rails, dict)
            and (rails.get("split") is None or rails.get("merge") is None)
        )
        if aux_unknown:
            out.append(
                f"- untraced_redemption_count: {untraced}  "
                "(upper bound; auxiliary acquisition rail unavailable)"
            )
        else:
            out.append(
                f"- untraced_redemption_count: {untraced}  "
                "(no acquisition rail observed in any source)"
            )

    gap_share = surface.get("acquisition_rail_gap_share")
    if gap_share is not None:
        out.append(f"- acquisition_rail_gap_share: {gap_share:.4f}")

    rails = surface.get("rails_observed")
    if isinstance(rails, dict):
        out.append(
            f"- rails_observed: orderbook={rails.get('orderbook', 0)}, "
            f"redemption={rails.get('redemption', 0)}, "
            f"split={rails.get('split', 0)}, "
            f"merge={rails.get('merge', 0)}"
        )

    css = surface.get("condition_set_summary")
    if isinstance(css, dict):
        out.append(
            f"- condition_set_summary: closed_only={css.get('closed_only', 0)}, "
            f"redeemed_only={css.get('redeemed_only', 0)}, "
            f"both={css.get('both', 0)}"
        )

    note = surface.get("note")
    if note:
        out.append(f"- note: {note}")

    caveats = surface.get("caveats") or []
    if caveats:
        out.append(f"- caveats: {'; '.join(str(c) for c in caveats)}")

    return out


def _copy_latency_block(report: ProtocolReport) -> list[str]:
    """Copy-Latency Decay surface — how much entry edge survives copy
    latency. Display-only. NEVER feeds verdict, gate, primary_edge_type,
    copyability_note, or data_quality_flags.
    """
    surface = report.copy_latency_surface
    if not isinstance(surface, dict):
        return []
    out = ["## Copy-latency decay"]
    out.append(f"- data_state: {surface.get('data_state', 'data_unavailable')}")
    out.append(f"- copyability: {surface.get('copyability', 'inconclusive')}")
    out.append(
        f"- eligible: {surface.get('n_eligible', 0)} / "
        f"{surface.get('n_total_closed', 0)} closed positions"
    )
    for h in surface.get("horizons") or []:
        out.append(
            f"  - +{h.get('horizon_min')}m: n={h.get('n')}, "
            f"mean_drift={_fmt(h.get('mean_drift'))}, "
            f"entry_roi={_fmt_pct(h.get('entry_ew_roi'))}, "
            f"copier_roi={_fmt_pct(h.get('copier_ew_roi'))}, "
            f"decay={_fmt_pct(h.get('mean_decay'))}, "
            f"edge_retained={_fmt(h.get('edge_retained'))}"
        )
    caveats = surface.get("caveats") or []
    if caveats:
        out.append(f"- caveats: {'; '.join(str(c) for c in caveats)}")
    out.append(
        "- Note: copy-latency decay is display-only — it does not change "
        "verdict, gate, edge type, copyability note, or quality flags. It "
        "measures post-entry price drift against a copier; it is NOT a "
        "trade recommendation. The surface is dormant — `data_unavailable` "
        "— until a capture is run with the post-entry price grid enabled."
    )
    return out


def _branch_block(
    branch: BranchReport | None,
    label: str,
    *,
    drift_companion: dict | None = None,
) -> list[str]:
    if branch is None:
        return [f"### [{label.upper()}] — N/A (branch not constructed)"]
    out = [f"### [{label.upper()}] BRANCH"]
    if not branch.is_active:
        out.append(f"- N/A — {branch.skip_reason or 'branch suppressed'}")
        return out
    for h in branch.hypotheses:
        # H4 owns the entry-timing horizon set; pass companion drift summaries
        # so the report can show drift_1h / drift_24h alongside H4's drift_6h.
        companion = drift_companion if h.name == "H4" else None
        out.extend(_hypothesis_block(h.name, h, drift_companion=companion))
    out.extend(_tag_grain_block(branch.tag_grains))
    out.extend(_stationarity_block(branch.stationarity))
    if branch.time_split:
        out.append("#### Time-split")
        for hyp_name, ts in branch.time_split.items():
            out.append(
                f"- {hyp_name}: n_total={ts.n_total} n_train={ts.n_train} n_test={ts.n_test} "
                f"train_p={_fmt(ts.train_p)} test_p={_fmt(ts.test_p)} status={ts.holdout_status}"
            )
    if branch.independence_check is not None:
        ic = branch.independence_check
        out.append(
            f"#### Independence check (eventSlug clustering): "
            f"clusters={ic.n_clusters}, fraction_in_3plus={_fmt(ic.fraction_in_clusters_size_3plus)}, "
            f"flag={ic.flag}"
        )
    return out


def _triage_context_block(ctx: dict) -> list[str]:
    """G3 render: `## Triage context` block, one bullet per key:value.

    `fetched_at` integer values render as ISO-8601 UTC; everything else
    is printed as-is (verbatim passthrough — operator-supplied dict).
    Empty dict renders the header plus a `- (no entries)` placeholder so
    the field's presence is visible.
    """
    out: list[str] = ["## Triage context"]
    if not ctx:
        out.append("- (no entries)")
        return out
    for key, value in ctx.items():
        rendered: str
        if key == "fetched_at" and isinstance(value, int):
            rendered = _fmt_ts(value)
        else:
            rendered = str(value)
        out.append(f"- {key}: {rendered}")
    return out


def render_report(report: ProtocolReport) -> str:
    """Return a Markdown-formatted report per §12 template."""
    lines: list[str] = []
    lines.append(f"## Wallet Report: {report.wallet}")
    lines.append(f"**Protocol version:** {report.protocol_version}")
    lines.append(f"**Analysis date:** {_fmt_ts(report.analyzed_at)}")
    lines.append(f"**n_closed_positions:** {report.n_closed_positions}")
    lines.append(f"**n_held_to_resolution:** {report.n_held_to_resolution}")
    lines.append(f"**n_open_positions:** {report.n_open_positions}")
    lines.append(f"**n_open_dead:** {report.n_open_dead}")
    lines.append(f"**net_pnl:** ${_fmt(report.net_pnl, decimals=2)}")
    cap_marker = " (CAP HIT)" if report.activity_cap_hit else ""
    lines.append(f"**activity_rows_fetched:** {report.activity_rows_fetched}{cap_marker}")
    lines.append(f"**flip_rate:** {_fmt(report.flip_rate)}")
    lines.append(f"**trading_mode:** {report.trading_mode}")
    lines.append(f"**archetype (Layer 1):** {report.archetype_layer1 or '—'}")
    lines.append(f"**archetype (Layer 2):** {report.archetype_layer2 or '—'}")

    # G3 — operator-supplied triage context. Rendered immediately after the
    # headline so the reader sees WHY this wallet was analyzed (leaderboard
    # rank, period, etc.) before reading any numbers. Verbatim key:value
    # passthrough; analyzer never validates the dict shape.
    if report.triage_context is not None:
        lines.append("")
        lines.extend(_triage_context_block(report.triage_context))

    # Completeness Surface v1 — capture-state + attribution-coverage + tiered
    # caveats. Always rendered (even when there are no closed positions) so
    # the reader can see how much of the wallet's history was actually fetched
    # before reading any performance numbers. Display-only.
    lines.append("")
    lines.extend(_data_completeness_block(report))

    # I1 — Capital efficiency section. Surfaced before Stage 1 so the
    # structural finding (DW vs EW divergence) is visible whether or not
    # the wallet passes the gate. Section omitted when no closed position
    # has a valid cost basis (compute_dw_ew_roi returned None).
    if report.dw_roi_point is not None and report.ew_roi_point is not None:
        lines.append("")
        lines.append("## Capital efficiency")
        dw_pct = report.dw_roi_point * 100.0
        dw_lo = (report.dw_roi_ci_lower or 0.0) * 100.0
        dw_hi = (report.dw_roi_ci_upper or 0.0) * 100.0
        ew_pct = report.ew_roi_point * 100.0
        lines.append(
            f"- Dollar-weighted ROI: {dw_pct:+.2f}% [{dw_lo:+.2f}%, {dw_hi:+.2f}%] "
            f"(95% CI, ratio bootstrap)"
        )
        lines.append(f"- Equal-weighted ROI: {ew_pct:+.2f}% (per-position mean)")
        # Surface a divergence note inline when the upstream flag fired.
        div_flag = next(
            (f for f in report.data_quality_flags if f.startswith("DW_EW_DIVERGENCE")),
            None,
        )
        if div_flag is not None:
            lines.append(
                f"- DW vs EW divergence flagged — wallet's biggest positions "
                f"performed differently from the average position."
            )
            lines.append(f"  - {div_flag}")

    # C1 — Capital ledger section (rendered when capital_ledger is present).
    if report.capital_ledger is not None:
        cl = report.capital_ledger
        lines.append("")
        lines.append("## Capital ledger (onchain SPLIT/MERGE/REDEMPTION)")
        lines.append(f"- net_capital_funded: ${cl.net_capital_funded_usdc:,.2f} USDC")
        lines.append(f"- total_deposits: ${cl.total_deposits_usdc:,.2f} (via SPLIT events)")
        lines.append(f"- total_withdrawals: ${cl.total_withdrawals_usdc:,.2f} (via MERGE events)")
        lines.append(f"- total_redemption_payout: ${cl.total_redemption_payout_usdc:,.2f} (settlement)")
        lines.append(
            f"- n_splits: {cl.n_splits}, n_merges: {cl.n_merges}, "
            f"n_redemptions: {cl.n_redemptions}"
        )
        if getattr(cl, "collateral_summary", None):
            lines.append("- collateral_tokens:")
            for token, summary in sorted(cl.collateral_summary.items()):
                lines.append(
                    f"  - {token}: events={int(summary.get('n_events', 0))}, "
                    f"deposits=${summary.get('deposits_usdc', 0.0):,.2f}, "
                    f"withdrawals=${summary.get('withdrawals_usdc', 0.0):,.2f}, "
                    f"redemptions=${summary.get('redemption_payout_usdc', 0.0):,.2f}"
                )
        if cl.first_event_ts:
            lines.append(f"- first_event: {_fmt_ts(cl.first_event_ts)}")
        if cl.last_event_ts:
            lines.append(f"- last_event: {_fmt_ts(cl.last_event_ts)}")
        if cl.net_capital_funded_usdc != 0 and report.net_pnl is not None:
            pnl_to_net_ctf_flow = report.net_pnl / cl.net_capital_funded_usdc
            flow_sign = (
                "net_funded"
                if cl.net_capital_funded_usdc > 0
                else "net_withdrawn"
            )
            lines.append(
                f"- realized_pnl_to_net_ctf_flow "
                f"(diagnostic, not ROI; {flow_sign}): "
                f"{pnl_to_net_ctf_flow*100:+.2f}%"
            )
        tie_flag = next(
            (
                f for f in report.data_quality_flags
                if f.startswith("ACCOUNTING_TIE")
                or f.startswith("PORTFOLIO_VALUE_OPEN_MARK")
            ),
            None,
        )
        if tie_flag is not None:
            lines.append(f"- {tie_flag}")

    # Execution quality — display-only summaries from execution_profile.
    # NEVER feeds verdict, gate, or primary_edge_type. Section is omitted
    # when execution_quality_summary is None or empty.
    eq = report.execution_quality_summary
    if eq:
        lines.append("")
        lines.append("## Execution quality")
        for key, label in (
            ("skew_vs_preceding_bar", "skew_vs_preceding_bar"),
            ("skew_vs_tape_5min", "skew_vs_tape_5min"),
            ("drift_1h", "drift_1h"),
            ("drift_6h", "drift_6h"),
            ("drift_24h", "drift_24h"),
        ):
            summary = eq.get(key)
            if isinstance(summary, dict):
                lines.append(
                    f"- {label}: mean={_fmt(summary.get('mean'))}, "
                    f"median={_fmt(summary.get('median'))}, "
                    f"n={summary.get('n', 0)}"
                )

    # Edge Surface v1 — descriptive performance surfaces. Display-only.
    # NEVER feeds verdict, gate, or primary_edge_type. The section is
    # rendered when at least one of its inputs is present.
    edge_lines = _edge_surface_block(report)
    if edge_lines:
        lines.append("")
        lines.extend(edge_lines)

    # Timing Surface v1 — captured-travel-only classification block.
    # NEVER feeds verdict; rendered between Edge surface and Stage 1.
    timing_lines = _timing_surface_block(report)
    if timing_lines:
        lines.append("")
        lines.extend(timing_lines)

    # Position Building Surface v1 (W2) — compact summary block. Display-only.
    # NEVER feeds verdict, gate, primary_edge_type, or copyability_note.
    pb_lines = _position_building_block(report)
    if pb_lines:
        lines.append("")
        lines.extend(pb_lines)

    # Liquidity Surface v1 — display-only proxy friction context.
    # NEVER feeds verdict, gate, primary_edge_type, copyability_note, or flags.
    liq_lines = _liquidity_surface_block(report)
    if liq_lines:
        lines.append("")
        lines.extend(liq_lines)

    # Position Reconciliation surface — when downstream_lower_bound=True the
    # block renders BEFORE the quantitative surfaces so the reader sees the
    # undercount caveat first. When False, it renders after copy_latency.
    # NEVER feeds verdict, gate, primary_edge_type, copyability_note, or flags.
    recon_lines = _position_reconciliation_block(report)
    _recon_lower_bound = (
        isinstance(report.position_reconciliation_surface, dict)
        and report.position_reconciliation_surface.get("downstream_lower_bound") is True
    )
    if recon_lines and _recon_lower_bound:
        lines.append("")
        lines.extend(recon_lines)

    # Capital & Risk surface — display-only capital base / drawdown context.
    # NEVER feeds verdict, gate, primary_edge_type, copyability_note, or flags.
    cap_lines = _capital_risk_block(report)
    if cap_lines:
        lines.append("")
        lines.extend(cap_lines)

    # Sizing & Outcome surface — display-only size↔outcome association.
    # NEVER feeds verdict, gate, primary_edge_type, copyability_note, or flags.
    sizing_lines = _sizing_outcome_block(report)
    if sizing_lines:
        lines.append("")
        lines.extend(sizing_lines)

    # Null-Strategy Baseline surface — display-only beat-the-chalk context.
    # NEVER feeds verdict, gate, primary_edge_type, copyability_note, or flags.
    null_lines = _null_baseline_block(report)
    if null_lines:
        lines.append("")
        lines.extend(null_lines)

    # Survivorship surface — display-only discovery-bias disclosure.
    # NEVER feeds verdict, gate, primary_edge_type, copyability_note, or flags.
    survivorship_lines = _survivorship_block(report)
    if survivorship_lines:
        lines.append("")
        lines.extend(survivorship_lines)

    # Copy-Latency Decay surface — display-only edge-survival context.
    # NEVER feeds verdict, gate, primary_edge_type, copyability_note, or flags.
    copy_latency_lines = _copy_latency_block(report)
    if copy_latency_lines:
        lines.append("")
        lines.extend(copy_latency_lines)

    # Position Reconciliation — when downstream_lower_bound=False, renders
    # here (alongside the other display-only surfaces).
    if recon_lines and not _recon_lower_bound:
        lines.append("")
        lines.extend(recon_lines)

    lines.append("")
    lines.append("### Stage 1 result")
    lines.append(f"- Gate result: **{report.gate_result.upper()}**")
    if report.gate_reason:
        lines.append(f"- Reason: {report.gate_reason}")
    if report.data_quality_flags:
        lines.append("- Data quality flags:")
        for f in report.data_quality_flags:
            lines.append(f"  - {f}")
    else:
        lines.append("- Data quality flags: None")

    if report.gate_result != "pass":
        lines.append("")
        lines.append("### Overall verdict")
        lines.append(f"- **{report.overall_verdict}** (rejected at Stage 1)")
        return "\n".join(lines)

    drift_companion = report.execution_quality_summary or None
    lines.append("")
    lines.extend(
        _branch_block(
            report.held_analyst_branch, "held-analyst",
            drift_companion=drift_companion,
        )
    )
    lines.append("")
    lines.extend(
        _branch_block(
            report.flipper_branch, "flipper",
            drift_companion=drift_companion,
        )
    )

    lines.append("")
    lines.append("### Overall verdict")
    lines.append(f"- **{report.overall_verdict}**")
    if report.primary_edge_type:
        lines.append(f"- primary_edge_type: {report.primary_edge_type}")
    if report.copyability_note:
        lines.append(f"- copyability: {report.copyability_note}")

    if report.applicable_limitations:
        lines.append("")
        lines.append("### Applicable limitations")
        for lim in report.applicable_limitations:
            lines.append(f"- {lim}")

    if report.open_questions:
        lines.append("")
        lines.append("### Open questions")
        for q in report.open_questions:
            lines.append(f"- {q}")

    return "\n".join(lines)


__all__ = ["render_report"]
