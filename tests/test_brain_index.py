"""Tests for brain_index — repo-local engineering index.

Performance note: `generate_schema_registry()` and especially
`generate_dependency_index()` are O(repo size) each — a single
dependency-index build takes ~3.5s because it AST-walks every Python
source file. Tests below share both via session-scoped fixtures so
the suite runs once per process instead of once per test (~14s →
~3s savings).
"""

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture(scope="session")
def schema_registry():
    """Cached schema registry — shared across every brain_index test in
    the session. Tests must not mutate; `dict.copy()` if you need to."""
    from brain_index import generate_schema_registry
    return generate_schema_registry()


@pytest.fixture(scope="session")
def dependency_index(schema_registry):
    """Cached dependency index — shared across every brain_index test."""
    from brain_index import generate_dependency_index
    return generate_dependency_index(schema_registry)


def test_generate_schema_registry(schema_registry):
    registry = schema_registry

    assert "generated_at" not in registry
    assert "schema_version" in registry
    assert "bootstrap_sources" in registry
    assert "table_count" in registry
    assert "tables" in registry

    tables = registry["tables"]
    for expected in [
        "low_buy_trades",
        "user_analysis",
        "position_signals",
        "paper_trades",
        "market_metadata",
        "followed_wallets",
        "paper_limit_orders",
        "signal_queue",
    ]:
        assert expected in tables, f"Missing table: {expected}"

    cols = {c["name"]: c for c in tables["low_buy_trades"]["columns"]}
    assert "transaction_hash" in cols
    assert cols["transaction_hash"]["pk"] is True
    assert "proxy_wallet" in cols
    assert cols["proxy_wallet"]["type"] == "TEXT"
    assert cols["proxy_wallet"]["notnull"] is True

    # paper_limit_orders — the motivating failure case
    plo_cols = {c["name"]: c for c in tables["paper_limit_orders"]["columns"]}
    assert "order_id" in plo_cols
    assert "strategy_id" in plo_cols
    assert "condition_id" in plo_cols
    assert "bid_price" in plo_cols
    assert "favorite_token_id" in plo_cols
    assert "favorite_token_label" in plo_cols
    assert "tour" in plo_cols
    assert "player1" in plo_cols
    assert "player2" in plo_cols

    assert isinstance(tables["low_buy_trades"]["indexes"], list)
    assert isinstance(tables["low_buy_trades"]["foreign_keys"], list)
    assert "paper_trader" in registry["bootstrap_sources"]
    assert "real_trader" in registry["bootstrap_sources"]


def test_schema_registry_includes_memecoin_x(schema_registry):
    assert "memecoin_x_coin_mentions" in schema_registry["tables"]
    for table in (
        "memecoin_x_signal_runs", "memecoin_x_signal_events",
        "memecoin_x_notification_outbox", "memecoin_x_notification_attempts",
        "memecoin_x_llm_jobs", "memecoin_x_llm_annotations", "memecoin_x_llm_watermarks",
    ):
        assert table in schema_registry["tables"]


def test_schema_registry_includes_isolated_v6_paper_ledger(schema_registry):
    tables = schema_registry["tables"]
    for table in (
        "v6_paper_positions", "v6_paper_decisions", "v6_paper_exit_followup_schedules",
        "v6_paper_exit_followup_results",
    ):
        assert table in tables
    schedule_columns = {
        column["name"] for column in tables["v6_paper_exit_followup_schedules"]["columns"]
    }
    assert {"followup_id", "action_token", "horizon_s", "due_at", "token_qty_atomic"} \
        <= schedule_columns


def test_schema_registry_indexes_supabase_wallet_ledger_tables(schema_registry):
    registry = schema_registry
    tables = registry["tables"]
    for expected in [
        "wallet_analysis_results",
        "wallet_findings",
        "artifact_registry",
        "market_outcome_dim",
    ]:
        assert expected in tables, f"Missing Supabase table: {expected}"

    outcome_cols = {
        c["name"]: c for c in tables["market_outcome_dim"]["columns"]
    }
    assert outcome_cols["token_id"]["pk"] is True
    assert "negative_risk" in outcome_cols
    assert "dim_quality_flags_json" in outcome_cols

    findings_fks = tables["wallet_findings"]["foreign_keys"]
    assert {
        "from_column": "wallet",
        "to_table": "wallet_analysis_results",
        "to_column": "wallet",
    } in findings_fks


def test_schema_registry_table_count(schema_registry):
    assert schema_registry["table_count"] >= 35


def test_generate_dependency_index(dependency_index):
    deps = dependency_index

    assert "generated_at" not in deps
    assert "file_count" in deps
    assert "files" in deps
    assert "table_readers" in deps
    assert "table_writers" in deps

    assert "db.py" in deps["files"]
    db_entry = deps["files"]["db.py"]
    total_refs = len(db_entry["reads_tables"]) + len(db_entry["writes_tables"])
    assert total_refs > 10, f"db.py only references {total_refs} tables"

    # Representative active app file with multiple table refs. Previously
    # paper_trader.py at root; archived 2026-05-27 (legacy-pipeline-2026-04-30)
    # so it no longer appears in the active index. db_followed is the cleanest
    # replacement: 27 table refs in current code, table set unambiguously
    # owned by the active sync/followed wallet path.
    assert "sniper/persistence/db_followed.py" in deps["files"]
    dbf = deps["files"]["sniper/persistence/db_followed.py"]
    dbf_all_tables = set(dbf["reads_tables"]) | set(dbf["writes_tables"])
    assert "followed_positions" in dbf_all_tables

    assert len(deps["table_readers"]) > 0
    assert len(deps["table_writers"]) > 0


def test_dependency_index_skips_tests_and_venv(dependency_index):
    deps = dependency_index

    for file_path in deps["files"]:
        assert not file_path.startswith("tests/"), f"Test file leaked: {file_path}"
        assert ".venv/" not in file_path, f"Venv file leaked: {file_path}"
        assert "__pycache__" not in file_path, f"Cache file leaked: {file_path}"

def test_dependency_index_ignores_docstring_table_mentions(dependency_index):
    deps = dependency_index

    readers = deps["table_readers"].get("paper_limit_orders", [])
    assert "stink_bid_runner.py" not in readers


def test_pattern_registry_valid():
    pattern_file = REPO_ROOT / "index" / "pattern_registry.yaml"
    assert pattern_file.exists(), "pattern_registry.yaml not found"

    with open(pattern_file) as f:
        data = yaml.safe_load(f)

    assert "patterns" in data
    patterns = data["patterns"]
    assert len(patterns) >= 5

    for p in patterns:
        assert "name" in p, f"Pattern missing 'name': {p}"
        assert "description" in p, f"Pattern '{p.get('name')}' missing 'description'"
        assert "files" in p, f"Pattern '{p['name']}' missing 'files'"
        assert "example" in p, f"Pattern '{p['name']}' missing 'example'"
        assert len(p["files"]) >= 1, f"Pattern '{p['name']}' has empty files list"

    names = {p["name"] for p in patterns}
    for expected in ["strategy_plugin", "flask_blueprint", "db_module"]:
        assert expected in names, f"Missing expected pattern: {expected}"


def test_format_schema_table(schema_registry):
    from brain_index import format_schema_table

    output = format_schema_table(schema_registry, "low_buy_trades")

    assert "low_buy_trades" in output
    assert "transaction_hash" in output
    assert "TEXT" in output
    assert "PK" in output
    assert "proxy_wallet" in output


def test_format_schema_table_expression_index(schema_registry):
    # memecoin_positions has idx_mc_pos_open_uniq_v3 keyed on
    # COALESCE(execution_mode,'paper') — PRAGMA index_info reports NULL
    # for expression keys, which used to crash the formatter. (v2 was the
    # pre-mode shape; it is dropped in ensure_memecoin_tables — db_memecoin.py:620.)
    from brain_index import format_schema_table

    output = format_schema_table(schema_registry, "memecoin_positions")

    assert "memecoin_positions" in output
    assert "idx_mc_pos_open_uniq_v3" in output


def test_format_schema_table_tolerates_none_index_column():
    # Committed registries may still carry a raw NULL for an expression
    # index key; the formatter must render it, not TypeError.
    from brain_index import format_schema_table

    registry = {
        "tables": {
            "t": {
                "columns": [
                    {"name": "a", "type": "TEXT", "notnull": False,
                     "pk": False, "default": None},
                ],
                "indexes": [
                    {"name": "idx_t_expr", "unique": True,
                     "columns": ["a", None]},
                ],
                "foreign_keys": [],
            }
        }
    }

    output = format_schema_table(registry, "t")
    assert "idx_t_expr" in output
    assert "<expr>" in output


def test_schema_registry_index_columns_never_none(schema_registry):
    # Generator invariant: expression index keys are stored as "<expr>",
    # never NULL, so every registry consumer can join/format safely.
    for name, table in schema_registry["tables"].items():
        for idx in table["indexes"]:
            for col in idx["columns"]:
                assert isinstance(col, str), (
                    f"{name}.{idx['name']} has non-string index column {col!r}"
                )


def test_precommit_index_regeneration_refuses_unstaged_python_changes():
    """Generated indexes must describe the staged commit, not unrelated working-tree WIP."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "pre-commit-schema-check.sh").read_text()
    guard = source.index("UNSTAGED_PY")
    generate = source.index("python3 scripts/brain_index.py generate")

    assert "git diff --name-only --diff-filter=ACM" in source
    assert "generate --core-only" in source
    assert guard < generate


def test_format_schema_table_missing(schema_registry):
    from brain_index import format_schema_table

    output = format_schema_table(schema_registry, "nonexistent_table")
    assert "not found" in output.lower()


def test_format_schema_list(schema_registry):
    from brain_index import format_schema_list

    output = format_schema_list(schema_registry)

    assert "low_buy_trades" in output
    assert "paper_limit_orders" in output
    assert "col" in output.lower()


def test_format_deps_file(dependency_index):
    from brain_index import format_deps_file

    # paper_trader.py archived 2026-05-27; switched to active db_followed.py
    # (27 table refs, in-active code) as the representative file under test.
    output = format_deps_file(dependency_index, "sniper/persistence/db_followed.py")

    assert "sniper/persistence/db_followed.py" in output
    assert "followed_positions" in output


def test_format_deps_table(dependency_index):
    from brain_index import format_deps_table

    output = format_deps_table(dependency_index, "low_buy_trades")

    assert "low_buy_trades" in output
    assert "db.py" in output


def test_format_pattern():
    from brain_index import format_pattern

    pattern_file = REPO_ROOT / "index" / "pattern_registry.yaml"
    with open(pattern_file) as f:
        data = yaml.safe_load(f)

    output = format_pattern(data, "strategy_plugin")
    assert "strategy_plugin" in output
    assert "StrategyPlugin" in output
    assert "strategies/base.py" in output


def test_format_pattern_missing():
    from brain_index import format_pattern

    pattern_file = REPO_ROOT / "index" / "pattern_registry.yaml"
    with open(pattern_file) as f:
        data = yaml.safe_load(f)

    output = format_pattern(data, "nonexistent_pattern")
    assert "not found" in output.lower()


def test_pattern_registry_strategy_plugin_matches_repo_contract():
    pattern_file = REPO_ROOT / "index" / "pattern_registry.yaml"
    with open(pattern_file) as f:
        data = yaml.safe_load(f)

    strategy_pattern = next(p for p in data["patterns"] if p["name"] == "strategy_plugin")
    example = strategy_pattern["example"]
    notes = strategy_pattern["notes"]

    assert "def discover(" in example
    assert "def enter(" in example
    assert "def manage(" in example
    assert "def exit(" in example
    assert "evaluate_signal" not in example
    assert "manage_position" not in example
    assert "_PLUGINS" in notes


def test_pattern_registry_feed_adapter_matches_repo_contract():
    pattern_file = REPO_ROOT / "index" / "pattern_registry.yaml"
    with open(pattern_file) as f:
        data = yaml.safe_load(f)

    feed_pattern = next(p for p in data["patterns"] if p["name"] == "feed_adapter")
    example = feed_pattern["example"]
    notes = feed_pattern["notes"]

    assert "def tick(" in example
    assert "_ADAPTER_CLASSES" in example
    assert "fetch_snapshot" not in example
    assert "FEED_ADAPTERS" not in notes


def test_pattern_registry_blueprint_notes_do_not_overclaim():
    pattern_file = REPO_ROOT / "index" / "pattern_registry.yaml"
    with open(pattern_file) as f:
        data = yaml.safe_load(f)

    blueprint_pattern = next(p for p in data["patterns"] if p["name"] == "flask_blueprint")
    notes = blueprint_pattern["notes"]

    assert "no raw SQL in routes" not in notes
    assert "create_api_" in blueprint_pattern["example"]
