#!/usr/bin/env python3
"""
UI Live Audit Runner
====================
Non-mutating audit tool that fetches all API endpoints listed in the manifest,
checks for data consistency across APIs, and generates a report.

Usage:
    python3 scripts/ui_live_audit.py                     # full audit
    python3 scripts/ui_live_audit.py --tab operator      # single tab
    python3 scripts/ui_live_audit.py --tab operator --dry-run  # parse + validate only
    python3 scripts/ui_live_audit.py --base-url http://localhost:8080

Output:
    output/ui-audits/<timestamp>/report.md
    output/ui-audits/<timestamp>/discrepancies.json
    output/ui-audits/<timestamp>/api_snapshots/<endpoint_id>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Resolve project root so we can find the manifest regardless of cwd
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
MANIFEST_PATH = PROJECT_ROOT / "config" / "ui_live_audit_manifest.yaml"

# ---------------------------------------------------------------------------
# Classification constants (must match manifest)
# ---------------------------------------------------------------------------
UI_RENDER_BUG = "UI_RENDER_BUG"
API_CONTRACT_BUG = "API_CONTRACT_BUG"
SOURCE_DATA_BUG = "SOURCE_DATA_BUG"
EXPLAINABLE_FORMATTING_ONLY = "EXPLAINABLE_FORMATTING_ONLY"
STALE_DATA_BLOCKER = "STALE_DATA_BLOCKER"

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ---------------------------------------------------------------------------
# Manifest loader
# ---------------------------------------------------------------------------
def load_manifest(path: Path) -> Dict[str, Any]:
    """Load the YAML manifest. Requires PyYAML."""
    try:
        import yaml
    except ImportError:
        raise SystemExit("PyYAML is required: pip install pyyaml")
    with open(path) as f:
        return yaml.safe_load(f)


def validate_manifest(manifest: Dict[str, Any]) -> List[str]:
    """Return a list of validation errors (empty = valid)."""
    errors: List[str] = []
    if "tabs" not in manifest:
        errors.append("Manifest missing 'tabs' key")
        return errors

    for tab_name, tab in manifest["tabs"].items():
        if "endpoints" not in tab:
            errors.append(f"Tab '{tab_name}' missing 'endpoints'")
            continue
        for ep in tab["endpoints"]:
            if "id" not in ep:
                errors.append(f"Tab '{tab_name}': endpoint missing 'id'")
            if "path" not in ep:
                eid = ep.get("id", "?")
                errors.append(f"Tab '{tab_name}': endpoint '{eid}' missing 'path'")
            if "required_fields" not in ep:
                eid = ep.get("id", "?")
                errors.append(f"Tab '{tab_name}': endpoint '{eid}' missing 'required_fields'")
    return errors


# ---------------------------------------------------------------------------
# API fetcher
# ---------------------------------------------------------------------------
def fetch_endpoint(
    base_url: str,
    endpoint: Dict[str, Any],
    api_key: Optional[str] = None,
    timeout: int = 30,
) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
    """
    Fetch a single API endpoint. Returns (status_code, json_body, error_msg).
    Non-mutating: only GET requests.
    """
    import urllib.request
    import urllib.error
    import urllib.parse

    path = endpoint["path"]
    method = endpoint.get("method", "GET").upper()
    params = endpoint.get("params", {})

    url = base_url.rstrip("/") + path
    if params and method == "GET":
        url += "?" + urllib.parse.urlencode(params)

    headers = {"Accept": "application/json"}
    if endpoint.get("auth") and api_key:
        headers["X-API-Key"] = api_key

    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return resp.status, data, None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        return e.code, None, f"HTTP {e.code}: {body[:200]}"
    except urllib.error.URLError as e:
        return 0, None, f"Connection error: {e.reason}"
    except json.JSONDecodeError as e:
        return 200, None, f"JSON decode error: {e}"
    except Exception as e:
        return 0, None, f"Unexpected error: {e}"


# ---------------------------------------------------------------------------
# Value extractors for normalization rules
# ---------------------------------------------------------------------------
def _resolve_value(key: str, snapshots: Dict[str, Dict[str, Any]]) -> Any:
    """
    Resolve a dotted path like 'system_stats.health_ok' against collected snapshots.
    Also handles special functions:
        len(endpoint.field)           -> length of a list
        count_open(endpoint.items)    -> count items with status=='open'
        count_active(endpoint.items)  -> count items with status=='active'
        sum_pnl(endpoint.items)       -> sum of pnl_usdc for closed items
    """
    for func_name, func in [
        ("len(", lambda lst: len(lst) if isinstance(lst, list) else 0),
        ("count_open(", lambda lst: sum(
            1 for item in (lst if isinstance(lst, list) else [])
            if isinstance(item, dict) and item.get("status") == "open"
        )),
        ("count_active(", lambda lst: sum(
            1 for item in (lst if isinstance(lst, list) else [])
            if isinstance(item, dict) and item.get("status") == "active"
        )),
        ("sum_pnl(", lambda lst: round(sum(
            float(item.get("pnl_usdc") or 0)
            for item in (lst if isinstance(lst, list) else [])
            if isinstance(item, dict) and item.get("status") not in (None, "open")
        ), 2)),
    ]:
        if key.startswith(func_name) and key.endswith(")"):
            inner = key[len(func_name):-1]
            inner_val = _resolve_value(inner, snapshots)
            return func(inner_val)

    # Dotted path: endpoint_id.field.subfield...
    parts = key.split(".", 1)
    if len(parts) < 2:
        return None
    endpoint_id, field_path = parts
    data = snapshots.get(endpoint_id)
    if data is None:
        return None

    for part in field_path.split("."):
        if isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def _check_rule(
    rule: Dict[str, Any],
    snapshots: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Evaluate a single normalization rule. Returns a discrepancy dict or None."""
    comparison = rule.get("comparison", "equals")
    severity = rule.get("severity", "medium")
    classification = rule.get("classification", API_CONTRACT_BUG)
    name = rule.get("name", "unnamed rule")

    source_a_key = rule.get("source_a", "")
    source_b_key = rule.get("source_b", "")

    val_a = _resolve_value(source_a_key, snapshots) if source_a_key else None

    if comparison == "equals":
        val_b = _resolve_value(source_b_key, snapshots) if source_b_key else None
        if val_a is None and val_b is None:
            return None
        if val_a != val_b:
            return {
                "rule": name, "comparison": comparison,
                "expected": val_b, "actual": val_a,
                "severity": severity, "classification": classification,
            }

    elif comparison == "approx_equals":
        val_b = _resolve_value(source_b_key, snapshots) if source_b_key else None
        tolerance = float(rule.get("tolerance", 0.01))
        if val_a is None or val_b is None:
            return None
        try:
            if abs(float(val_a) - float(val_b)) > tolerance:
                return {
                    "rule": name, "comparison": comparison,
                    "expected": val_b, "actual": val_a, "tolerance": tolerance,
                    "severity": severity, "classification": classification,
                }
        except (TypeError, ValueError):
            return {
                "rule": name, "comparison": comparison,
                "expected": val_b, "actual": val_a,
                "severity": severity, "classification": classification,
                "note": "Could not convert to float for comparison",
            }

    elif comparison == "less_than":
        threshold = float(rule.get("threshold", 0))
        if val_a is None:
            return {
                "rule": name, "comparison": comparison,
                "expected": f"< {threshold}", "actual": None,
                "severity": severity, "classification": classification,
                "note": "Value not available",
            }
        try:
            if float(val_a) >= threshold:
                return {
                    "rule": name, "comparison": comparison,
                    "expected": f"< {threshold}", "actual": float(val_a),
                    "severity": severity, "classification": classification,
                }
        except (TypeError, ValueError):
            pass

    elif comparison == "non_empty_list":
        if not isinstance(val_a, list) or len(val_a) == 0:
            return {
                "rule": name, "comparison": comparison,
                "expected": "non-empty list",
                "actual": type(val_a).__name__ if val_a is not None else "None",
                "severity": severity, "classification": classification,
            }

    elif comparison == "items_have_fields":
        required_fields = rule.get("required_item_fields", [])
        if not isinstance(val_a, list):
            return {
                "rule": name, "comparison": comparison,
                "expected": f"list of items with {required_fields}",
                "actual": type(val_a).__name__,
                "severity": severity, "classification": classification,
            }
        missing_samples = []
        for i, item in enumerate(val_a[:10]):
            if not isinstance(item, dict):
                missing_samples.append({"index": i, "type": type(item).__name__})
                continue
            missing = [f for f in required_fields if f not in item]
            if missing:
                missing_samples.append({"index": i, "missing_fields": missing})
        if missing_samples:
            return {
                "rule": name, "comparison": comparison,
                "expected": f"all items have {required_fields}",
                "actual": f"{len(missing_samples)} items with missing fields",
                "samples": missing_samples[:5],
                "severity": severity, "classification": classification,
            }

    elif comparison == "equals_true":
        if val_a is not True:
            return {
                "rule": name, "comparison": comparison,
                "expected": True, "actual": val_a,
                "severity": severity, "classification": classification,
            }

    return None


# ---------------------------------------------------------------------------
# Field presence checker
# ---------------------------------------------------------------------------
def check_required_fields(
    endpoint_id: str,
    data: Dict[str, Any],
    required_fields: List[str],
    failure_severity: str,
) -> List[Dict[str, Any]]:
    """Check that all required fields exist in the response."""
    discrepancies = []
    for field in required_fields:
        if field not in data:
            discrepancies.append({
                "rule": f"{endpoint_id} missing required field '{field}'",
                "comparison": "field_present",
                "expected": f"field '{field}' in response",
                "actual": "missing",
                "severity": failure_severity,
                "classification": API_CONTRACT_BUG,
            })
    return discrepancies


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------
def generate_markdown_report(
    tab_results: Dict[str, Dict[str, Any]],
    all_discrepancies: List[Dict[str, Any]],
    run_ts: str,
    base_url: str,
    dry_run: bool = False,
) -> str:
    """Generate a human-readable Markdown audit report."""
    lines: List[str] = []
    lines.append("# UI Live Audit Report")
    lines.append("")
    lines.append(f"**Timestamp:** {run_ts}")
    lines.append(f"**Base URL:** {base_url}")
    lines.append(f"**Mode:** {'DRY RUN (no API calls)' if dry_run else 'LIVE'}")
    lines.append(f"**Total discrepancies:** {len(all_discrepancies)}")
    lines.append("")

    # Summary by severity
    by_severity: Dict[str, int] = {}
    for d in all_discrepancies:
        sev = d.get("severity", "unknown")
        by_severity[sev] = by_severity.get(sev, 0) + 1

    if by_severity:
        lines.append("## Summary by Severity")
        lines.append("")
        lines.append("| Severity | Count |")
        lines.append("|----------|-------|")
        for sev in sorted(by_severity, key=lambda s: SEVERITY_ORDER.get(s, 99)):
            lines.append(f"| {sev} | {by_severity[sev]} |")
        lines.append("")

    # Summary by classification
    by_class: Dict[str, int] = {}
    for d in all_discrepancies:
        cls = d.get("classification", "unknown")
        by_class[cls] = by_class.get(cls, 0) + 1

    if by_class:
        lines.append("## Summary by Classification")
        lines.append("")
        lines.append("| Classification | Count |")
        lines.append("|----------------|-------|")
        for cls in sorted(by_class):
            lines.append(f"| {cls} | {by_class[cls]} |")
        lines.append("")

    # Per-tab details
    for tab_name, result in sorted(tab_results.items()):
        lines.append(f"## Tab: {tab_name}")
        lines.append("")

        endpoints = result.get("endpoints", {})
        for ep_id, ep_result in endpoints.items():
            status = ep_result.get("status_code", "N/A")
            error = ep_result.get("error")
            icon = "PASS" if not error and status == 200 else "FAIL"
            lines.append(f"### [{icon}] {ep_id} ({ep_result.get('path', '?')})")
            lines.append(f"- Status: {status}")
            if error:
                lines.append(f"- Error: {error}")
            else:
                field_count = ep_result.get("field_count", "?")
                lines.append(f"- Fields returned: {field_count}")
            lines.append("")

        tab_discrepancies = [
            d for d in all_discrepancies if d.get("tab") == tab_name
        ]
        if tab_discrepancies:
            lines.append(f"### Discrepancies ({len(tab_discrepancies)})")
            lines.append("")
            for d in tab_discrepancies:
                sev = d.get("severity", "?")
                cls = d.get("classification", "?")
                rule_name = d.get("rule", "?")
                lines.append(f"- **[{sev}] [{cls}]** {rule_name}")
                lines.append(f"  - Expected: `{d.get('expected')}`")
                lines.append(f"  - Actual: `{d.get('actual')}`")
                if d.get("note"):
                    lines.append(f"  - Note: {d['note']}")
                lines.append("")
        else:
            lines.append("No discrepancies found.")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Snapshot saver
# ---------------------------------------------------------------------------
def save_snapshots(
    output_dir: Path,
    global_snapshots: Dict[str, Dict[str, Any]],
) -> None:
    """Save raw JSON snapshots for each endpoint."""
    snap_dir = output_dir / "api_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    for ep_id, data in global_snapshots.items():
        fpath = snap_dir / f"{ep_id}.json"
        with open(fpath, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  Saved: {fpath}")


# ---------------------------------------------------------------------------
# Core audit engine
# ---------------------------------------------------------------------------
def run_audit(
    manifest: Dict[str, Any],
    tab_filter: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 30,
    output_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Run the full audit. Fetches endpoints, checks field presence and
    normalization rules, optionally saves snapshots.
    Returns (tab_results, all_discrepancies).
    """
    base = base_url or manifest.get("base_url", "http://localhost:8080")
    tabs = manifest.get("tabs", {})
    all_discrepancies: List[Dict[str, Any]] = []
    tab_results: Dict[str, Dict[str, Any]] = {}
    global_snapshots: Dict[str, Dict[str, Any]] = {}

    if tab_filter and tab_filter not in tabs:
        print(f"[error] Tab '{tab_filter}' not found. Available: {', '.join(tabs.keys())}")
        sys.exit(1)

    tabs_to_audit = {tab_filter: tabs[tab_filter]} if tab_filter else tabs

    for tab_name, tab_config in tabs_to_audit.items():
        print(f"\n--- Auditing tab: {tab_name} ---")
        endpoints = tab_config.get("endpoints", [])
        tab_result: Dict[str, Any] = {"endpoints": {}}

        for ep in endpoints:
            ep_id = ep["id"]
            ep_path = ep["path"]
            print(f"  [{ep_id}] {ep_path} ...", end=" ")

            status_code, data, error = fetch_endpoint(
                base, ep, api_key=api_key, timeout=timeout
            )

            if error:
                print(f"FAIL ({error})")
                tab_result["endpoints"][ep_id] = {
                    "path": ep_path,
                    "status_code": status_code,
                    "error": error,
                }
                all_discrepancies.append({
                    "tab": tab_name,
                    "rule": f"{ep_id} fetch failed",
                    "comparison": "http_success",
                    "expected": 200,
                    "actual": status_code,
                    "severity": ep.get("failure_severity", "high"),
                    "classification": API_CONTRACT_BUG,
                    "error": error,
                })
                continue

            print(f"OK ({status_code})")
            global_snapshots[ep_id] = data
            tab_result["endpoints"][ep_id] = {
                "path": ep_path,
                "status_code": status_code,
                "field_count": len(data) if isinstance(data, dict) else "N/A",
            }

            # Check required fields
            field_issues = check_required_fields(
                ep_id, data,
                ep.get("required_fields", []),
                ep.get("failure_severity", "high"),
            )
            for issue in field_issues:
                issue["tab"] = tab_name
            all_discrepancies.extend(field_issues)

        # Run normalization rules
        rules = tab_config.get("normalization_rules", [])
        for rule in rules:
            discrepancy = _check_rule(rule, global_snapshots)
            if discrepancy:
                discrepancy["tab"] = tab_name
                all_discrepancies.append(discrepancy)

        tab_results[tab_name] = tab_result

    # Save snapshots if output_dir given
    if output_dir and global_snapshots:
        print("\nSaving API snapshots...")
        save_snapshots(output_dir, global_snapshots)

    # Sort by severity
    all_discrepancies.sort(
        key=lambda d: SEVERITY_ORDER.get(d.get("severity", "low"), 99)
    )

    return tab_results, all_discrepancies


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="UI Live Audit Runner - non-mutating API consistency checker"
    )
    parser.add_argument(
        "--tab", type=str, default=None,
        help="Audit a single tab (operator, signals, positions, wallets, strategies, intel)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate manifest and show plan without making API calls",
    )
    parser.add_argument(
        "--base-url", type=str, default=None,
        help="Override the base URL from manifest",
    )
    parser.add_argument(
        "--api-key", type=str, default=None,
        help="API key for authenticated endpoints (reads SNIPER_API_KEY env if not set)",
    )
    parser.add_argument(
        "--timeout", type=int, default=30,
        help="HTTP request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Override output directory (default: output/ui-audits/<timestamp>)",
    )
    args = parser.parse_args()

    # ---- Load & validate manifest ----
    print(f"Loading manifest: {MANIFEST_PATH}")
    manifest = load_manifest(MANIFEST_PATH)
    errors = validate_manifest(manifest)
    if errors:
        print("\nManifest validation errors:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

    tab_count = len(manifest.get("tabs", {}))
    ep_count = sum(
        len(t.get("endpoints", []))
        for t in manifest.get("tabs", {}).values()
    )
    print(f"Manifest valid: {tab_count} tabs, {ep_count} endpoints")

    api_key = args.api_key or os.environ.get("SNIPER_API_KEY", "")
    base_url = args.base_url or manifest.get("base_url", "http://localhost:8080")

    # ---- Dry-run mode ----
    if args.dry_run:
        print(f"\n=== DRY RUN MODE ===")
        print(f"Base URL: {base_url}")
        print(f"Tab filter: {args.tab or 'all'}")
        print(f"API key: {'set' if api_key else 'not set'}")
        print()

        tabs = manifest.get("tabs", {})
        if args.tab and args.tab not in tabs:
            print(f"[error] Tab '{args.tab}' not found. Available: {', '.join(tabs.keys())}")
            sys.exit(1)

        target_tabs = {args.tab: tabs[args.tab]} if args.tab else tabs

        for tab_name, tab_config in target_tabs.items():
            print(f"Tab: {tab_name}")
            for ep in tab_config.get("endpoints", []):
                auth_note = " [AUTH]" if ep.get("auth") else ""
                print(f"  -> {ep.get('method', 'GET')} {ep['path']}{auth_note}")
                print(f"     Required fields: {', '.join(ep.get('required_fields', []))}")
                print(f"     Failure severity: {ep.get('failure_severity', 'medium')}")
            rules = tab_config.get("normalization_rules", [])
            if rules:
                print(f"  Normalization rules ({len(rules)}):")
                for rule in rules:
                    cross = f" [cross-tab: {rule['cross_tab']}]" if rule.get("cross_tab") else ""
                    print(f"    - {rule['name']}{cross}")
            print()

        print("=== DRY RUN COMPLETE (no API calls made) ===")
        return

    # ---- Live audit ----
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = PROJECT_ROOT / "output" / "ui-audits" / run_ts
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nStarting live audit against {base_url}")

    tab_results, all_discrepancies = run_audit(
        manifest,
        tab_filter=args.tab,
        api_key=api_key,
        base_url=base_url,
        timeout=args.timeout,
        output_dir=output_dir,
    )

    # Generate Markdown report
    report_md = generate_markdown_report(
        tab_results, all_discrepancies, run_ts, base_url
    )
    report_path = output_dir / "report.md"
    with open(report_path, "w") as f:
        f.write(report_md)
    print(f"\nReport saved: {report_path}")

    # Save discrepancy ledger
    ledger_path = output_dir / "discrepancies.json"
    with open(ledger_path, "w") as f:
        json.dump({
            "run_ts": run_ts,
            "base_url": base_url,
            "tab_filter": args.tab,
            "total": len(all_discrepancies),
            "discrepancies": all_discrepancies,
        }, f, indent=2, default=str)
    print(f"Discrepancy ledger saved: {ledger_path}")

    # ---- Summary ----
    print(f"\n{'='*60}")
    print("AUDIT COMPLETE")
    print(f"{'='*60}")
    print(f"Tabs audited: {len(tab_results)}")
    print(f"Total discrepancies: {len(all_discrepancies)}")
    if all_discrepancies:
        by_sev: Dict[str, int] = {}
        for d in all_discrepancies:
            s = d.get("severity", "unknown")
            by_sev[s] = by_sev.get(s, 0) + 1
        for sev in sorted(by_sev, key=lambda s: SEVERITY_ORDER.get(s, 99)):
            print(f"  {sev}: {by_sev[sev]}")
    else:
        print("  All checks passed.")
    print(f"{'='*60}")

    # Non-zero exit if critical/high discrepancies
    critical_count = sum(
        1 for d in all_discrepancies
        if d.get("severity") in ("critical", "high")
    )
    if critical_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
