"""Tests for scripts/refresh_fixture_tags.py — T13.

Spec: docs/superpowers/specs/2026-05-26-wallet-dossier-market-semantics-v1.md §6

The script is a new file — it does not exist yet. These tests are RED until
the script is implemented.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "refresh_fixture_tags.py"

# skip-list tags from tag_inference
from sniper.analysis.wallet_protocol.tag_inference import is_skip_tag


def _build_test_db(path: Path) -> None:
    """Create a tiny in-memory-style SQLite with market_tags + tag_nav_category rows."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_tags (
            condition_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (condition_id, tag)
        );
        CREATE TABLE IF NOT EXISTS tag_nav_category (
            tag TEXT PRIMARY KEY,
            nav_category TEXT NOT NULL
        );
    """)
    # Insert test tags
    conn.executemany(
        "INSERT OR IGNORE INTO market_tags VALUES (?, ?)",
        [
            ("0xaaa", "microstrategy"),
            ("0xaaa", "crypto"),
            ("0xaaa", "hide-from-new"),   # skip tag — must be filtered out
            ("0xbbb", "sports"),
            ("0xbbb", "soccer"),
            ("0xddd", "awards"),          # ambiguous tag — must be counted
        ],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO tag_nav_category VALUES (?, ?)",
        [
            ("microstrategy", "crypto"),
            ("crypto", "crypto"),
            ("sports", "sports"),
            ("soccer", "sports"),
            ("awards", "other"),
        ],
    )
    conn.commit()
    conn.close()


def _make_input_fixture(cids: list[str]) -> dict:
    """Minimal fixture referencing the given condition IDs in closed_positions."""
    positions = [
        {
            "conditionId": cid,
            "asset": f"asset_{i}",
            "outcome": "Yes",
            "avgPrice": 0.40,
            "totalBought": 100.0,
            "realizedPnl": 60.0,
        }
        for i, cid in enumerate(cids)
    ]
    return {
        "wallet": "0xdeadbeef" + "0" * 32,
        "fetched_at": 1700000000,
        "schema_version": 2,
        "closed_positions": positions,
        "open_positions": [],
        "activity_rows": [],
        "leafs_by_cid": {},
        "nav_lookup": {},
    }


# ---------------------------------------------------------------------------
# T13 — refresh_fixture_tags.py smoke
# ---------------------------------------------------------------------------


def test_T13_script_file_exists():
    """scripts/refresh_fixture_tags.py must exist (new file)."""
    assert SCRIPT_PATH.exists(), (
        f"scripts/refresh_fixture_tags.py not found at {SCRIPT_PATH}; "
        "must be created as part of spec §6 implementation"
    )


def test_T13_leafs_by_cid_populated_for_known_cids(tmp_path: Path):
    """Output fixture has leafs_by_cid populated for cids that had tag rows."""
    db_path = tmp_path / "test.db"
    _build_test_db(db_path)

    input_fixture = _make_input_fixture(["0xaaa", "0xbbb", "0xccc"])
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(input_fixture))
    output_path = tmp_path / "output.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--fixture", str(input_path),
            "--db", str(db_path),
            "--out", str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"

    output = json.loads(output_path.read_text())
    leafs = output.get("leafs_by_cid", {})

    # 0xaaa has tags — must appear
    assert "0xaaa" in leafs, f"0xaaa missing from leafs_by_cid={leafs}"
    # 0xbbb has tags — must appear
    assert "0xbbb" in leafs, f"0xbbb missing from leafs_by_cid={leafs}"
    # 0xccc has no tags — must not appear (or appear empty)
    # (acceptable: absent OR empty list, not a populated entry)


def test_T13_skip_tags_filtered_out_of_leafs_by_cid(tmp_path: Path):
    """leafs_by_cid must NOT contain skip-list tags (is_skip_tag filtered)."""
    db_path = tmp_path / "test.db"
    _build_test_db(db_path)

    input_fixture = _make_input_fixture(["0xaaa"])
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(input_fixture))
    output_path = tmp_path / "output.json"

    subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--fixture", str(input_path), "--db", str(db_path), "--out", str(output_path)],
        capture_output=True, text=True, check=True,
        timeout=60,
    )

    output = json.loads(output_path.read_text())
    leafs = output.get("leafs_by_cid", {})

    # 0xaaa has "hide-from-new" in DB — must be filtered
    tags_for_aaa = leafs.get("0xaaa", [])
    for tag in tags_for_aaa:
        assert not is_skip_tag(tag), (
            f"Skip tag {tag!r} must not appear in leafs_by_cid after filter"
        )
    # microstrategy and crypto are not skip tags — they should survive
    assert "hide-from-new" not in tags_for_aaa


def test_T13_no_apply_skip_list_keeps_skip_tags(tmp_path: Path):
    """--no-apply-skip-list is a real switch, not a no-op."""
    db_path = tmp_path / "test.db"
    _build_test_db(db_path)

    input_fixture = _make_input_fixture(["0xaaa"])
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(input_fixture))
    output_path = tmp_path / "output.json"

    subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--fixture", str(input_path), "--db", str(db_path),
         "--out", str(output_path), "--no-apply-skip-list"],
        capture_output=True, text=True, check=True,
        timeout=60,
    )

    output = json.loads(output_path.read_text())
    assert "hide-from-new" in output.get("leafs_by_cid", {}).get("0xaaa", [])


def test_T13_ambiguous_leafs_are_counted_in_logs(tmp_path: Path):
    """n_leafs_ambiguous reports ambiguous tags seen in fixture conditions."""
    db_path = tmp_path / "test.db"
    _build_test_db(db_path)

    input_fixture = _make_input_fixture(["0xddd"])
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(input_fixture))
    output_path = tmp_path / "output.json"

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--fixture", str(input_path), "--db", str(db_path), "--out", str(output_path)],
        capture_output=True, text=True, check=True,
        timeout=60,
    )

    assert "n_leafs_ambiguous=1" in result.stderr


def test_T13_nav_lookup_populated_from_tag_nav_category(tmp_path: Path):
    """nav_lookup populated from tag_nav_category rows for tags in the fixture."""
    db_path = tmp_path / "test.db"
    _build_test_db(db_path)

    input_fixture = _make_input_fixture(["0xaaa", "0xbbb"])
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(input_fixture))
    output_path = tmp_path / "output.json"

    subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--fixture", str(input_path), "--db", str(db_path), "--out", str(output_path)],
        capture_output=True, text=True, check=True,
        timeout=60,
    )

    output = json.loads(output_path.read_text())
    nav = output.get("nav_lookup", {})

    # microstrategy → crypto, sports → sports (from tag_nav_category)
    assert "microstrategy" in nav, f"microstrategy missing from nav_lookup={nav}"
    assert nav["microstrategy"] == "crypto"
    assert "sports" in nav
    assert nav["sports"] == "sports"


def test_T13_input_fixture_not_mutated(tmp_path: Path):
    """Input fixture bytes are identical before and after running the script."""
    db_path = tmp_path / "test.db"
    _build_test_db(db_path)

    input_fixture = _make_input_fixture(["0xaaa"])
    input_path = tmp_path / "input.json"
    input_bytes_before = json.dumps(input_fixture).encode("utf-8")
    input_path.write_bytes(input_bytes_before)
    output_path = tmp_path / "output.json"

    subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--fixture", str(input_path), "--db", str(db_path), "--out", str(output_path)],
        capture_output=True, text=True, check=True,
        timeout=60,
    )

    input_bytes_after = input_path.read_bytes()
    assert input_bytes_before == input_bytes_after, (
        "Input fixture must not be mutated by refresh_fixture_tags.py"
    )


def test_T13_deterministic_output(tmp_path: Path):
    """Running with same inputs twice produces byte-identical output (deterministic)."""
    db_path = tmp_path / "test.db"
    _build_test_db(db_path)

    input_fixture = _make_input_fixture(["0xaaa", "0xbbb"])
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(input_fixture))

    out1 = tmp_path / "out1.json"
    out2 = tmp_path / "out2.json"

    subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--fixture", str(input_path), "--db", str(db_path), "--out", str(out1)],
        capture_output=True, text=True, check=True,
        timeout=60,
    )
    subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--fixture", str(input_path), "--db", str(db_path), "--out", str(out2)],
        capture_output=True, text=True, check=True,
        timeout=60,
    )

    assert out1.read_bytes() == out2.read_bytes(), (
        "refresh_fixture_tags.py must produce byte-identical output for identical inputs"
    )
