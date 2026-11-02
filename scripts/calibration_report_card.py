#!/usr/bin/env python3
"""Extract the exact audit fields from a wallet protocol replay JSON.

Single source of truth for v3 calibration wallet review prose. Prints
everything an analyst review must cite — no interpretation, no rounding
beyond display, no narrative.

Usage:
    python3 scripts/calibration_report_card.py <wallet_addr>

Reads the fixture at tests/calibration/fixtures/<wallet>_2026-05-13.json,
runs the protocol under harness flags, and prints the audit-ready card with
the pinning header (fixture path / analyzer commit / replay command /
replay timestamp / dirty-tree status) required by the calibration review
discipline.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

def _detect_repo_root() -> Path:
    """Walk up from this file until we find a `tests/calibration/fixtures` dir.

    Avoids hardcoding /home/bigpapi/Desktop/Sniper so the card works in
    worktrees, CI, and forks. The fallback is git-rev-parse if the directory
    layout shifts.
    """
    here = Path(__file__).resolve()
    for ancestor in [here.parent, *here.parents]:
        if (ancestor / "tests" / "calibration" / "fixtures").is_dir():
            return ancestor
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "-C", str(here.parent), "rev-parse", "--show-toplevel"],
            text=True,
        ).strip()
        if out:
            return Path(out)
    except Exception:
        pass
    raise RuntimeError(
        "Could not detect repo root from script location or git; "
        "expected tests/calibration/fixtures/ in an ancestor directory."
    )


REPO_ROOT = _detect_repo_root()
FIXTURE_DIR = REPO_ROOT / "tests" / "calibration" / "fixtures"
FIXTURE_DATE = "2026-05-13"
HARNESS_ARGS = [
    "--no-tags", "--no-peers", "--no-exec", "--no-history", "--no-archetype",
    "--seed", "1", "--n-iter", "2000", "--json", "--no-persist",
]


def _git_rev() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception as e:
        return f"<unavailable: {e}>"


def _git_dirty() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain"], text=True
        )
        if out.strip():
            n_lines = len(out.strip().splitlines())
            return f"DIRTY ({n_lines} tracked changes/untracked)"
        return "clean"
    except Exception as e:
        return f"<unavailable: {e}>"


def _replay(wallet: str, fixture: Path) -> dict:
    sys.path.insert(0, str(REPO_ROOT))
    import scripts.run_wallet_protocol as mod
    argv = [wallet, "--from-fixture", str(fixture)] + HARNESS_ARGS
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(argv)
    if rc != 0:
        raise RuntimeError(f"run_wallet_protocol returned {rc} for {wallet}")
    return json.loads(buf.getvalue().strip())


def _ts(epoch: int | None) -> str:
    if epoch is None:
        return "<none>"
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _money(v):
    return f"${v:>14,.2f}" if isinstance(v, (int, float)) else str(v)


def _pct(v, *, digits: int = 2) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    return f"{v * 100:.{digits}f}%"


def _print_help() -> None:
    print(__doc__ or "calibration_report_card.py — extract audit-ready fields")
    print()
    print("usage:")
    print(f"  calibration_report_card.py <wallet>")
    print(f"  calibration_report_card.py --fixture <path>")
    print(f"  calibration_report_card.py -h|--help")
    print()
    print("flags:")
    print("  <wallet>            wallet address; fixture resolved as")
    print(f"                      {FIXTURE_DIR.relative_to(REPO_ROOT)}/<wallet>_{FIXTURE_DATE}.json")
    print("  --fixture <path>    explicit fixture path (overrides date convention;")
    print("                      useful for staged .recapture.json files)")
    print("  -h, --help          show this help")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        _print_help()
        return 0
    if argv[0] == "--fixture":
        if len(argv) != 2:
            print("--fixture requires a path argument", file=sys.stderr)
            return 2
        fixture = Path(argv[1]).resolve()
        # Infer wallet from fixture filename: <wallet>_<date>.json or
        # <wallet>_<date>.recapture.json. The wallet is the leading
        # 0x-prefixed hex segment up to the first underscore.
        stem = fixture.name
        wallet = stem.split("_")[0].lower()
    else:
        if len(argv) != 1:
            print("usage: calibration_report_card.py <wallet> | --fixture <path> | -h", file=sys.stderr)
            return 2
        wallet = argv[0].lower()
        fixture = FIXTURE_DIR / f"{wallet}_{FIXTURE_DATE}.json"
    if not fixture.exists():
        print(f"FIXTURE NOT FOUND: {fixture}", file=sys.stderr)
        return 1
    fixture_bytes = fixture.stat().st_size
    fixture_mtime = dt.datetime.fromtimestamp(
        fixture.stat().st_mtime, tz=dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Read fixture schema_version from the fixture payload (NOT the
    # ProtocolReport — those are different concepts).
    try:
        fixture_schema_version = json.loads(fixture.read_text()).get("schema_version", "<missing>")
    except Exception as e:
        fixture_schema_version = f"<unreadable: {e}>"
    replay_ts = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = _replay(wallet, fixture)

    # Pinning header
    print("=" * 78)
    print(f"CALIBRATION REPORT CARD — {wallet}")
    print("=" * 78)
    print(f"fixture_path:      {fixture.relative_to(REPO_ROOT)}")
    print(f"fixture_bytes:     {fixture_bytes:,}")
    print(f"fixture_captured:  {fixture_mtime}")
    print(f"analyzer_commit:   {_git_rev()}")
    print(f"working_tree:      {_git_dirty()}")
    print(f"replay_ts:         {replay_ts}")
    print(f"replay_argv:       run_wallet_protocol.py {wallet} --from-fixture <fixture> {' '.join(HARNESS_ARGS)}")
    print(f"protocol_version:  {r.get('protocol_version')}")
    print(f"fixture_schema_version: {fixture_schema_version}")
    print()

    # Verdict block
    print("--- VERDICT ---")
    print(f"  overall_verdict:    {r['overall_verdict']!r}")
    print(f"  primary_edge_type:  {r['primary_edge_type']!r}")
    print(f"  copyability_note:   {r['copyability_note']!r}  (analyzer field; NOT a deployable copy signal)")
    print(f"  gate_result:        {r['gate_result']!r}  gate_reason={r.get('gate_reason')!r}")
    _fr = r.get('flip_rate')
    print(f"  trading_mode:       {r.get('trading_mode')!r}  flip_rate={_fr:.4f}" if isinstance(_fr, (int, float)) else f"  trading_mode:       {r.get('trading_mode')!r}  flip_rate={_fr!r}")
    print(f"  archetype_layer1:   {r['archetype_layer1']!r}")
    print(f"  archetype_layer2:   {r['archetype_layer2']!r}")
    print()

    # Operator triage context (G3)
    print("--- TRIAGE CONTEXT ---")
    triage = r.get("triage_context")
    if isinstance(triage, dict) and triage:
        for k in sorted(triage):
            print(f"  {k}: {triage[k]}")
    else:
        print("  <none>")
    print()

    # Counts
    print("--- COUNTS ---")
    print(f"  n_closed_positions:    {r['n_closed_positions']:,}")
    print(f"  n_held_to_resolution:  {r['n_held_to_resolution']:,}")
    print(f"  n_open_positions:      {r['n_open_positions']}")
    ob = r["open_position_breakdown"]
    print(f"  open in_money:   {ob['n_open_in_money']}  ${ob['in_money_capital_usdc']:,.2f}")
    print(f"  open underwater: {ob['n_open_underwater']}  ${ob.get('underwater_capital_usdc',0):,.2f}")
    print(f"  open dead:       {ob['n_open_dead']}  ${ob['dead_capital_usdc']:,.2f}")
    print(f"  activity_rows_fetched: {r['activity_rows_fetched']:,}")
    print(f"  walk_full_history:     {r['walk_full_history']}")
    print(f"  activity_cap_hit:      {r['activity_cap_hit']}")
    print(f"  subgraph_fallback_used: {r['subgraph_fallback_used']}")
    print(f"  n_unmapped_fills:      {r['n_unmapped_fills']:,}")
    print()

    # PnL
    print("--- PNL ---")
    print(f"  realized_pnl:    {_money(r['realized_pnl'])}")
    print(f"  unrealized_pnl:  {_money(r['unrealized_pnl'])}")
    print(f"  combined_pnl:    {_money(r['combined_pnl'])}")
    print(f"  net_pnl:         {_money(r['net_pnl'])}  (legacy = realized mirror)")
    print(f"  realized_roi:    {r['realized_roi'] if r['realized_roi'] is None else f'{r['realized_roi']*100:.4f}%'}")
    print(f"  unrealized_roi:  {r['unrealized_roi'] if r['unrealized_roi'] is None else f'{r['unrealized_roi']*100:.4f}%'}")
    print(f"  combined_roi:    {r['combined_roi'] if r['combined_roi'] is None else f'{r['combined_roi']*100:.4f}%'}")
    print(f"  dw_roi_point:    {r['dw_roi_point']*100:.4f}%  CI=[{r['dw_roi_ci_lower']*100:.4f}%, {r['dw_roi_ci_upper']*100:.4f}%]")
    print(f"  ew_roi_point:    {r['ew_roi_point']*100:.4f}%")
    print()

    # Time coverage
    print("--- TIME COVERAGE ---")
    tc = r["time_coverage"]
    print(f"  first_position_ts:  {tc['first_position_ts']}  ({_ts(tc['first_position_ts'])})")
    print(f"  last_position_ts:   {tc['last_position_ts']}  ({_ts(tc['last_position_ts'])})")
    print(f"  first_fill_ts:      {tc['first_fill_ts']}  ({_ts(tc['first_fill_ts'])})")
    print(f"  last_fill_ts:       {tc['last_fill_ts']}  ({_ts(tc['last_fill_ts'])})")
    print(f"  duration_days:      {tc['duration_days']}")
    print(f"  n_positions_per_month: {tc['n_positions_per_month']:.4f}")
    print(f"  fetch_termination:  {tc.get('fetch_termination', {})}")
    print()

    # Hypothesis branches
    print("--- HYPOTHESIS BRANCHES ---")
    for branch_name in ("held_analyst_branch", "flipper_branch"):
        b = r.get(branch_name)
        if not isinstance(b, dict):
            print(f"  {branch_name}: not present (verdict={r.get('overall_verdict')!r})")
            continue
        print(f"  {branch_name}: active={b.get('is_active')}  skip_reason={b.get('skip_reason')!r}")
        print(f"    n_hypotheses={len(b.get('hypotheses',[]))}  n_tag_grains={len(b.get('tag_grains',[]))}")
        for h in b.get('hypotheses', []):
            verd = h.get("verdict")
            name = h.get("name") or h.get("hypothesis_id") or "?"
            p = h.get("p_value")
            es = h.get("effect_size")
            line = f"      [{verd}] {name}"
            if p is not None:
                line += f"  p={p:.4f}"
            if es is not None:
                line += f"  effect={es:.4f}"
            print(line)
    print()

    # Data quality flags (EXACT, ordered, count cited)
    flags = r.get("data_quality_flags") or []
    print(f"--- DATA QUALITY FLAGS (n={len(flags)}) ---")
    for f in flags:
        print(f"  - {f}")
    print()

    # Applicable limitations
    lims = r.get("applicable_limitations") or []
    print(f"--- APPLICABLE LIMITATIONS (n={len(lims)}) ---")
    for lim in lims:
        print(f"  - {lim}")
    print()

    # Capital ledger
    print("--- CAPITAL LEDGER ---")
    cl = r.get("capital_ledger") or {}
    print(f"  n_splits:      {cl.get('n_splits',0):,}")
    print(f"  n_merges:      {cl.get('n_merges',0):,}")
    print(f"  n_redemptions: {cl.get('n_redemptions',0):,}")
    print(f"  total_deposits_usdc:    ${cl.get('total_deposits_usdc',0):,.2f}")
    print(f"  total_withdrawals_usdc: ${cl.get('total_withdrawals_usdc',0):,.2f}")
    print(f"  net_capital_funded_usdc: ${cl.get('net_capital_funded_usdc',0):,.2f}")
    print(f"  collateral_summary: {cl.get('collateral_summary', {})}")
    print()

    # Outcome side (totals + attribution + tag coverage)
    print("--- OUTCOME SIDE ---")
    osb = r["outcome_side_breakdown"]
    for side in ("yes", "no", "other"):
        d = osb.get(side, {})
        print(
            f"  {side.upper()}: n={d.get('n',0)}  "
            f"WR={_pct(d.get('win_rate'))}  "
            f"PnL=${d.get('total_pnl',0):,.2f}  "
            f"share={_pct(d.get('share_of_closed'))}"
        )
        attr = d.get("by_attribution") or {}
        for k, v in attr.items():
            print(f"     {k}: n={v.get('n',0)} pnl=${v.get('total_pnl',0):,.2f}")
        if d.get("top_markets_by_pnl"):
            print(f"    top_markets_by_pnl:")
            for m in d["top_markets_by_pnl"][:5]:
                print(f"      ${m['pnl']:>14,.0f}  {m['title'][:70]}  outcome={m.get('outcome')}")
    if osb.get("caveat"):
        print(f"  caveat: {osb['caveat']}")
    print()

    # Category clusters (G6)
    print("--- CATEGORY CLUSTERS ---")
    clusters = osb.get("category_clusters") or []
    if clusters:
        for c in clusters[:10]:
            pnl = c.get("pnl", 0.0)
            total = c.get("total_pnl", 0.0)
            share = (abs(pnl) / max(abs(total), 1.0)) if isinstance(pnl, (int, float)) else None
            print(
                f"  {c.get('category')}: n={c.get('n',0)} "
                f"pnl=${pnl:,.2f} share={_pct(share)}"
            )
    else:
        print("  <none>")
    print()

    # Timing surface
    print("--- TIMING SURFACE ---")
    ts = r["timing_surface"]
    for k, v in ts.items():
        print(f"  {k}: {v}")
    print()

    # Exit attribution coverage
    print("--- EXIT ATTRIBUTION COVERAGE ---")
    ea = r["exit_attribution"]
    for k, v in (ea.get("coverage") or {}).items():
        print(f"  {k}: {v}")
    print()

    # Position builder surface
    print("--- POSITION BUILDER SURFACE ---")
    pb = r.get("position_building_surface", {})
    sq = pb.get("source_quality_distribution", {})
    iq = pb.get("identity_quality_distribution", {})
    rq = pb.get("row_quality_summary", {})
    print(f"  source_quality_distribution: {sq}")
    print(f"  identity_quality_distribution: {iq}")
    print(f"  row_quality_summary: {rq}")
    print()

    # Extractor features
    print("--- EXTRACTOR FEATURES ---")
    for k, v in r["extractor_features_summary"].items():
        print(f"  {k}: {v}")
    print()

    # Data completeness
    print("--- DATA COMPLETENESS ---")
    dc = r.get("data_completeness", {})
    for k, v in dc.items():
        print(f"  {k}: {v}")
    print()

    print("=" * 78)
    print("END CARD")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
