"""
Tests for the UI live audit manifest and runner.

Covers:
  1. Manifest structure validity (YAML loads, tabs present, field schemas)
  2. Route existence (every manifest endpoint path is registered in Flask)
  3. Normalization rule consistency (no orphan endpoint_id references)
  4. Value resolver logic (_resolve_value, _check_rule)
"""

import os
import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts.ui_live_audit import (
    _check_rule,
    _resolve_value,
    load_manifest,
    validate_manifest,
    MANIFEST_PATH,
)

# ---------------------------------------------------------------------------
# Allowed values
# ---------------------------------------------------------------------------

EXPECTED_TABS = {"operator", "signals", "positions", "wallets", "strategies", "intel"}

REQUIRED_ENDPOINT_FIELDS = {"id", "path", "method", "required_fields", "failure_severity"}

REQUIRED_RULE_FIELDS = {"name", "comparison", "severity", "classification"}

ALLOWED_CLASSIFICATIONS = {
    "UI_RENDER_BUG",
    "API_CONTRACT_BUG",
    "SOURCE_DATA_BUG",
    "EXPLAINABLE_FORMATTING_ONLY",
    "STALE_DATA_BLOCKER",
    "VPS_HEALTH_DEGRADED",
}

ALLOWED_SEVERITIES = {"critical", "high", "medium", "low"}

ALLOWED_COMPARISONS = {
    "equals",
    "equals_true",
    "approx_equals",
    "less_than",
    "non_empty_list",
    "items_have_fields",
}


# ---------------------------------------------------------------------------
# Fixture: load manifest once
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def manifest():
    return load_manifest(MANIFEST_PATH)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Manifest structure validity
# ═══════════════════════════════════════════════════════════════════════════


def test_manifest_loads_without_error(manifest):
    """Manifest YAML parses and returns a dict."""
    assert isinstance(manifest, dict)


def test_manifest_has_version(manifest):
    assert "version" in manifest


def test_manifest_has_classifications(manifest):
    assert "classifications" in manifest
    for cls in manifest["classifications"]:
        assert cls in ALLOWED_CLASSIFICATIONS, f"Unknown classification: {cls}"


def test_manifest_has_all_six_tabs(manifest):
    tabs = set(manifest.get("tabs", {}).keys())
    assert tabs == EXPECTED_TABS, f"Missing tabs: {EXPECTED_TABS - tabs}, extra: {tabs - EXPECTED_TABS}"


def test_validate_manifest_returns_no_errors(manifest):
    errors = validate_manifest(manifest)
    assert errors == [], f"Validation errors: {errors}"


def test_every_endpoint_has_required_schema_fields(manifest):
    """Every endpoint in every tab must have id, path, method, required_fields, failure_severity."""
    for tab_name, tab in manifest["tabs"].items():
        for ep in tab.get("endpoints", []):
            for field in REQUIRED_ENDPOINT_FIELDS:
                assert field in ep, (
                    f"Tab '{tab_name}', endpoint '{ep.get('id', '?')}' missing '{field}'"
                )


def test_endpoint_failure_severity_is_valid(manifest):
    for tab_name, tab in manifest["tabs"].items():
        for ep in tab.get("endpoints", []):
            sev = ep["failure_severity"]
            assert sev in ALLOWED_SEVERITIES, (
                f"Tab '{tab_name}', endpoint '{ep['id']}': invalid severity '{sev}'"
            )


def test_every_normalization_rule_has_required_fields(manifest):
    """Every normalization rule must have name, comparison, severity, classification."""
    for tab_name, tab in manifest["tabs"].items():
        for rule in tab.get("normalization_rules", []):
            for field in REQUIRED_RULE_FIELDS:
                assert field in rule, (
                    f"Tab '{tab_name}', rule '{rule.get('name', '?')}' missing '{field}'"
                )


def test_normalization_rule_classifications_valid(manifest):
    for tab_name, tab in manifest["tabs"].items():
        for rule in tab.get("normalization_rules", []):
            cls = rule["classification"]
            assert cls in ALLOWED_CLASSIFICATIONS, (
                f"Tab '{tab_name}', rule '{rule['name']}': unknown classification '{cls}'"
            )


def test_normalization_rule_severities_valid(manifest):
    for tab_name, tab in manifest["tabs"].items():
        for rule in tab.get("normalization_rules", []):
            sev = rule["severity"]
            assert sev in ALLOWED_SEVERITIES, (
                f"Tab '{tab_name}', rule '{rule['name']}': invalid severity '{sev}'"
            )


def test_normalization_rule_comparisons_valid(manifest):
    for tab_name, tab in manifest["tabs"].items():
        for rule in tab.get("normalization_rules", []):
            cmp = rule["comparison"]
            assert cmp in ALLOWED_COMPARISONS, (
                f"Tab '{tab_name}', rule '{rule['name']}': unknown comparison '{cmp}'"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Route existence — every manifest path exists in Flask url_map
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def flask_routes():
    """Collect all registered Flask route rule strings."""
    # web_server.py does heavy work at import (DB init, etc.).  We only need
    # the url_map, so we import under controlled conditions.
    os.environ.setdefault("FLASK_HOST", "127.0.0.1")
    os.environ.setdefault("FLASK_PORT", "5000")
    os.environ.setdefault("DATABASE_PATH", ":memory:")

    from sniper.web.server import app
    # Return the set of rule strings (e.g. "/api/system-stats")
    return {rule.rule for rule in app.url_map.iter_rules()}


def test_all_manifest_endpoints_have_flask_routes(manifest, flask_routes):
    """Every endpoint path listed in the manifest must be a registered Flask route."""
    missing = []
    for tab_name, tab in manifest["tabs"].items():
        for ep in tab.get("endpoints", []):
            path = ep["path"]
            if path not in flask_routes:
                missing.append(f"{tab_name}/{ep['id']}: {path}")
    assert missing == [], (
        f"Manifest endpoints with no matching Flask route:\n  " +
        "\n  ".join(missing)
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. Normalization rule consistency — no orphan endpoint_id references
# ═══════════════════════════════════════════════════════════════════════════


def _extract_endpoint_ref(expr: str) -> str | None:
    """Extract the endpoint_id from a source expression like 'system_stats.health_ok'
    or 'len(open_position_signals.items)'."""
    # Strip function wrappers: len(...), count_open(...), etc.
    inner = expr
    for prefix in ("len(", "count_open(", "count_active(", "sum_pnl("):
        if inner.startswith(prefix) and inner.endswith(")"):
            inner = inner[len(prefix):-1]
            break
    parts = inner.split(".", 1)
    return parts[0] if parts[0] else None


def test_normalization_rules_reference_existing_endpoints(manifest):
    """Every source_a / source_b must reference an endpoint_id that exists
    in the same tab or in the tab named by cross_tab."""
    tabs = manifest["tabs"]

    # Build a map: endpoint_id -> set of tab_names where it lives
    ep_id_to_tabs: dict[str, set[str]] = {}
    for tab_name, tab in tabs.items():
        for ep in tab.get("endpoints", []):
            ep_id_to_tabs.setdefault(ep["id"], set()).add(tab_name)

    orphans = []
    for tab_name, tab in tabs.items():
        for rule in tab.get("normalization_rules", []):
            allowed_tabs = {tab_name}
            if "cross_tab" in rule:
                allowed_tabs.add(rule["cross_tab"])

            for source_key in ("source_a", "source_b"):
                expr = rule.get(source_key, "")
                if not expr:
                    continue
                ref = _extract_endpoint_ref(expr)
                if ref is None:
                    continue
                ref_tabs = ep_id_to_tabs.get(ref, set())
                if not ref_tabs & allowed_tabs:
                    orphans.append(
                        f"Tab '{tab_name}', rule '{rule['name']}': "
                        f"'{source_key}' references endpoint '{ref}' "
                        f"not found in tabs {allowed_tabs}"
                    )

    assert orphans == [], (
        "Orphan endpoint references in normalization rules:\n  " +
        "\n  ".join(orphans)
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Value resolver logic — _resolve_value and _check_rule
# ═══════════════════════════════════════════════════════════════════════════


# -- _resolve_value --


def test_resolve_value_dotted_path():
    snapshots = {"system_stats": {"health_ok": True, "nested": {"deep": 42}}}
    assert _resolve_value("system_stats.health_ok", snapshots) is True
    assert _resolve_value("system_stats.nested.deep", snapshots) == 42


def test_resolve_value_missing_endpoint_returns_none():
    assert _resolve_value("nonexistent.field", {}) is None


def test_resolve_value_missing_field_returns_none():
    snapshots = {"ep": {"a": 1}}
    assert _resolve_value("ep.b", snapshots) is None


def test_resolve_value_single_part_returns_none():
    snapshots = {"ep": {"a": 1}}
    assert _resolve_value("ep", snapshots) is None


def test_resolve_value_len():
    snapshots = {"signals": {"items": [1, 2, 3]}}
    assert _resolve_value("len(signals.items)", snapshots) == 3


def test_resolve_value_len_non_list_returns_zero():
    snapshots = {"signals": {"items": "not a list"}}
    assert _resolve_value("len(signals.items)", snapshots) == 0


def test_resolve_value_count_open():
    items = [
        {"status": "open"},
        {"status": "closed"},
        {"status": "open"},
        {"status": "resolved"},
    ]
    snapshots = {"positions": {"items": items}}
    assert _resolve_value("count_open(positions.items)", snapshots) == 2


def test_resolve_value_count_open_empty():
    snapshots = {"positions": {"items": []}}
    assert _resolve_value("count_open(positions.items)", snapshots) == 0


def test_resolve_value_count_active():
    items = [
        {"status": "active"},
        {"status": "paused"},
        {"status": "active"},
        {"status": "stopped"},
        {"status": "active"},
    ]
    snapshots = {"strats": {"items": items}}
    assert _resolve_value("count_active(strats.items)", snapshots) == 3


def test_resolve_value_sum_pnl():
    items = [
        {"status": "closed", "pnl_usdc": 10.5},
        {"status": "open", "pnl_usdc": 100.0},  # open -> excluded
        {"status": "resolution_win", "pnl_usdc": 20.3},
        {"status": "closed", "pnl_usdc": -5.0},
    ]
    snapshots = {"pos": {"items": items}}
    # closed + resolution_win + closed = 10.5 + 20.3 + (-5.0) = 25.8
    assert _resolve_value("sum_pnl(pos.items)", snapshots) == 25.8


def test_resolve_value_sum_pnl_excludes_open_and_none():
    items = [
        {"status": "open", "pnl_usdc": 999},
        {"status": None, "pnl_usdc": 999},
        {"status": "closed", "pnl_usdc": 7.0},
    ]
    snapshots = {"pos": {"items": items}}
    # Only the closed item counts
    assert _resolve_value("sum_pnl(pos.items)", snapshots) == 7.0


# -- _check_rule: equals --


def test_check_rule_equals_pass():
    rule = {
        "name": "test",
        "source_a": "ep.count",
        "source_b": "ep2.count",
        "comparison": "equals",
        "severity": "medium",
        "classification": "API_CONTRACT_BUG",
    }
    snapshots = {"ep": {"count": 5}, "ep2": {"count": 5}}
    assert _check_rule(rule, snapshots) is None


def test_check_rule_equals_fail():
    rule = {
        "name": "test",
        "source_a": "ep.count",
        "source_b": "ep2.count",
        "comparison": "equals",
        "severity": "medium",
        "classification": "API_CONTRACT_BUG",
    }
    snapshots = {"ep": {"count": 5}, "ep2": {"count": 10}}
    result = _check_rule(rule, snapshots)
    assert result is not None
    assert result["actual"] == 5
    assert result["expected"] == 10


def test_check_rule_equals_both_none_is_pass():
    """When both values are None (missing), equals returns no discrepancy."""
    rule = {
        "name": "test",
        "source_a": "missing.field",
        "source_b": "also_missing.field",
        "comparison": "equals",
        "severity": "medium",
        "classification": "API_CONTRACT_BUG",
    }
    assert _check_rule(rule, {}) is None


# -- _check_rule: approx_equals --


def test_check_rule_approx_equals_within_tolerance():
    rule = {
        "name": "approx test",
        "source_a": "a.val",
        "source_b": "b.val",
        "comparison": "approx_equals",
        "tolerance": 1.0,
        "severity": "high",
        "classification": "SOURCE_DATA_BUG",
    }
    snapshots = {"a": {"val": 100.0}, "b": {"val": 100.5}}
    assert _check_rule(rule, snapshots) is None


def test_check_rule_approx_equals_outside_tolerance():
    rule = {
        "name": "approx test",
        "source_a": "a.val",
        "source_b": "b.val",
        "comparison": "approx_equals",
        "tolerance": 1.0,
        "severity": "high",
        "classification": "SOURCE_DATA_BUG",
    }
    snapshots = {"a": {"val": 100.0}, "b": {"val": 105.0}}
    result = _check_rule(rule, snapshots)
    assert result is not None
    assert result["severity"] == "high"


def test_check_rule_approx_equals_none_is_pass():
    """When either value is None, approx_equals returns no discrepancy."""
    rule = {
        "name": "approx test",
        "source_a": "a.val",
        "source_b": "missing.val",
        "comparison": "approx_equals",
        "tolerance": 1.0,
        "severity": "high",
        "classification": "SOURCE_DATA_BUG",
    }
    snapshots = {"a": {"val": 100.0}}
    assert _check_rule(rule, snapshots) is None


# -- _check_rule: less_than --


def test_check_rule_less_than_pass():
    rule = {
        "name": "lag check",
        "source_a": "health.lag",
        "comparison": "less_than",
        "threshold": 30.0,
        "severity": "critical",
        "classification": "STALE_DATA_BLOCKER",
    }
    snapshots = {"health": {"lag": 5.0}}
    assert _check_rule(rule, snapshots) is None


def test_check_rule_less_than_fail():
    rule = {
        "name": "lag check",
        "source_a": "health.lag",
        "comparison": "less_than",
        "threshold": 30.0,
        "severity": "critical",
        "classification": "STALE_DATA_BLOCKER",
    }
    snapshots = {"health": {"lag": 45.0}}
    result = _check_rule(rule, snapshots)
    assert result is not None
    assert result["severity"] == "critical"
    assert result["classification"] == "STALE_DATA_BLOCKER"


def test_check_rule_less_than_none_value():
    rule = {
        "name": "lag check",
        "source_a": "missing.field",
        "comparison": "less_than",
        "threshold": 30.0,
        "severity": "critical",
        "classification": "STALE_DATA_BLOCKER",
    }
    result = _check_rule(rule, {})
    assert result is not None
    assert result["actual"] is None
    assert "not available" in result.get("note", "")


# -- _check_rule: non_empty_list --


def test_check_rule_non_empty_list_pass():
    rule = {
        "name": "events check",
        "source_a": "ctx.events",
        "comparison": "non_empty_list",
        "severity": "medium",
        "classification": "STALE_DATA_BLOCKER",
    }
    snapshots = {"ctx": {"events": [{"id": 1}]}}
    assert _check_rule(rule, snapshots) is None


def test_check_rule_non_empty_list_fail_empty():
    rule = {
        "name": "events check",
        "source_a": "ctx.events",
        "comparison": "non_empty_list",
        "severity": "medium",
        "classification": "STALE_DATA_BLOCKER",
    }
    snapshots = {"ctx": {"events": []}}
    result = _check_rule(rule, snapshots)
    assert result is not None


def test_check_rule_non_empty_list_fail_not_list():
    rule = {
        "name": "events check",
        "source_a": "ctx.events",
        "comparison": "non_empty_list",
        "severity": "medium",
        "classification": "STALE_DATA_BLOCKER",
    }
    snapshots = {"ctx": {"events": "not a list"}}
    result = _check_rule(rule, snapshots)
    assert result is not None


# -- _check_rule: items_have_fields --


def test_check_rule_items_have_fields_pass():
    rule = {
        "name": "wallet fields",
        "source_a": "lb.items",
        "comparison": "items_have_fields",
        "required_item_fields": ["wallet", "score"],
        "severity": "high",
        "classification": "API_CONTRACT_BUG",
    }
    snapshots = {"lb": {"items": [
        {"wallet": "0x1", "score": 80},
        {"wallet": "0x2", "score": 60},
    ]}}
    assert _check_rule(rule, snapshots) is None


def test_check_rule_items_have_fields_fail_missing():
    rule = {
        "name": "wallet fields",
        "source_a": "lb.items",
        "comparison": "items_have_fields",
        "required_item_fields": ["wallet", "score"],
        "severity": "high",
        "classification": "API_CONTRACT_BUG",
    }
    snapshots = {"lb": {"items": [
        {"wallet": "0x1"},  # missing score
        {"wallet": "0x2", "score": 60},
    ]}}
    result = _check_rule(rule, snapshots)
    assert result is not None
    assert "missing" in result["actual"].lower()


def test_check_rule_items_have_fields_fail_not_list():
    rule = {
        "name": "wallet fields",
        "source_a": "lb.items",
        "comparison": "items_have_fields",
        "required_item_fields": ["wallet"],
        "severity": "high",
        "classification": "API_CONTRACT_BUG",
    }
    snapshots = {"lb": {"items": "not a list"}}
    result = _check_rule(rule, snapshots)
    assert result is not None


def test_check_rule_items_have_fields_fail_non_dict_items():
    rule = {
        "name": "wallet fields",
        "source_a": "lb.items",
        "comparison": "items_have_fields",
        "required_item_fields": ["wallet"],
        "severity": "high",
        "classification": "API_CONTRACT_BUG",
    }
    snapshots = {"lb": {"items": ["string_item"]}}
    result = _check_rule(rule, snapshots)
    assert result is not None


# -- _check_rule with len() in source_b --


def test_check_rule_equals_with_len_function():
    """Tests count == len(items) pattern used across many tabs."""
    rule = {
        "name": "count vs len",
        "source_a": "signals.count",
        "source_b": "len(signals.items)",
        "comparison": "equals",
        "severity": "medium",
        "classification": "API_CONTRACT_BUG",
    }
    snapshots = {"signals": {"count": 3, "items": [1, 2, 3]}}
    assert _check_rule(rule, snapshots) is None

    # Mismatch
    snapshots_bad = {"signals": {"count": 5, "items": [1, 2, 3]}}
    result = _check_rule(rule, snapshots_bad)
    assert result is not None
    assert result["actual"] == 5
    assert result["expected"] == 3


# -- _check_rule: equals_true --
# This is the regression test for the March 27 2026 incident where
# ops/health returned ok=false with HTTP 200 and the audit passed silently.


def test_check_rule_equals_true_passes_when_true():
    """equals_true: no discrepancy when field is boolean True."""
    rule = {
        "name": "ops_health.ok must be true",
        "source_a": "ops_health.ok",
        "comparison": "equals_true",
        "severity": "critical",
        "classification": "VPS_HEALTH_DEGRADED",
    }
    snapshots = {"ops_health": {"ok": True, "issues": [], "wal_mb": 50}}
    assert _check_rule(rule, snapshots) is None


def test_check_rule_equals_true_fails_when_false():
    """equals_true: discrepancy when field is boolean False (the March 27 incident)."""
    rule = {
        "name": "ops_health.ok must be true",
        "source_a": "ops_health.ok",
        "comparison": "equals_true",
        "severity": "critical",
        "classification": "VPS_HEALTH_DEGRADED",
    }
    snapshots = {"ops_health": {"ok": False, "issues": ["wal_bloat:207.9MB"], "wal_mb": 207.9}}
    result = _check_rule(rule, snapshots)
    assert result is not None
    assert result["actual"] is False
    assert result["expected"] is True
    assert result["severity"] == "critical"
    assert result["classification"] == "VPS_HEALTH_DEGRADED"


def test_check_rule_equals_true_fails_when_none():
    """equals_true: discrepancy when field is missing/None."""
    rule = {
        "name": "ops_health.ok must be true",
        "source_a": "ops_health.ok",
        "comparison": "equals_true",
        "severity": "critical",
        "classification": "VPS_HEALTH_DEGRADED",
    }
    snapshots = {"ops_health": {}}  # ok field absent
    result = _check_rule(rule, snapshots)
    assert result is not None
    assert result["actual"] is None
