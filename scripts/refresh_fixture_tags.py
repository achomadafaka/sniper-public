"""Fixture-refresh utility for wallet dossier market semantics v1.

Reads a WalletData fixture JSON, queries market_tags and tag_nav_category
from a read-only SQLite DB, and writes a tagged copy of the fixture with
`leafs_by_cid` and `nav_lookup` populated.

Spec: docs/superpowers/specs/2026-05-26-wallet-dossier-market-semantics-v1.md §6

Usage:
    python3 scripts/refresh_fixture_tags.py \\
        --fixture <input.json> \\
        --db <sniper_tracker.db> \\
        --out <tagged.json>

Read-only DB access. Does NOT mutate the input fixture file.
Exit non-zero only on input read error, DB connection failure, or output
write failure. Empty results (zero tags found) is exit 0 with a clear log.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sqlite3
import sys
from pathlib import Path

# Ensure the repo root is on sys.path when running as a script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sniper.analysis.wallet_protocol.tag_inference import AMBIGUOUS_TAGS, is_skip_tag

logger = logging.getLogger(__name__)


def _extract_condition_ids(fixture: dict) -> list[str]:
    """Collect unique condition_ids from all position lists in the fixture."""
    seen: set[str] = set()
    order: list[str] = []
    for key in ("closed_positions", "open_positions", "activity_rows", "redemptions"):
        for row in fixture.get(key) or []:
            cid = (row.get("conditionId") or row.get("condition") or "").strip()
            if cid and cid not in seen:
                seen.add(cid)
                order.append(cid)
    return order


def _fetch_market_tags(conn: sqlite3.Connection, condition_ids: list[str]) -> dict[str, list[str]]:
    """Query market_tags for the given condition_ids. Returns cid → [tags]."""
    if not condition_ids:
        return {}
    placeholders = ",".join("?" * len(condition_ids))
    rows = conn.execute(
        f"SELECT condition_id, tag FROM market_tags WHERE condition_id IN ({placeholders})",
        condition_ids,
    ).fetchall()
    result: dict[str, list[str]] = {}
    for cid, tag in rows:
        result.setdefault(cid, []).append(tag)
    # Sort for determinism
    for cid in result:
        result[cid].sort()
    return result


def _fetch_nav_category_map(conn: sqlite3.Connection, tags: set[str]) -> dict[str, str]:
    """Query tag_nav_category for the given tags. Returns tag → nav_category."""
    if not tags:
        return {}
    tag_list = sorted(tags)
    placeholders = ",".join("?" * len(tag_list))
    rows = conn.execute(
        f"SELECT tag, nav_category FROM tag_nav_category WHERE tag IN ({placeholders})",
        tag_list,
    ).fetchall()
    return {tag: nav for tag, nav in rows}


def refresh_fixture_tags(
    fixture_path: Path,
    db_path: Path,
    out_path: Path,
    *,
    apply_skip_list: bool = True,
) -> None:
    """Main logic: read fixture, query DB, write tagged copy.

    Deep-copies the input fixture; never mutates the source.
    """
    # --- read input fixture ---
    try:
        input_bytes = fixture_path.read_bytes()
        fixture = json.loads(input_bytes)
    except Exception:
        logger.exception("Failed to read input fixture: %s", fixture_path)
        sys.exit(1)

    # Deep copy — never mutate source
    tagged = copy.deepcopy(fixture)

    # --- extract condition_ids ---
    condition_ids = _extract_condition_ids(tagged)
    n_conditions_total = len(condition_ids)
    logger.info("n_conditions_total=%d", n_conditions_total)

    # --- query DB ---
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = None
    except Exception:
        logger.exception("Failed to open DB (read-only): %s", db_path)
        sys.exit(1)

    try:
        raw_tags_by_cid = _fetch_market_tags(conn, condition_ids)
    except Exception:
        logger.exception("Failed to query market_tags from %s", db_path)
        conn.close()
        sys.exit(1)

    # Gather all raw tags for nav_category lookup (before skip filter)
    all_raw_tags: set[str] = set()
    for tags in raw_tags_by_cid.values():
        all_raw_tags.update(tags)

    try:
        nav_map = _fetch_nav_category_map(conn, all_raw_tags)
    except Exception:
        logger.exception("Failed to query tag_nav_category from %s", db_path)
        conn.close()
        sys.exit(1)

    conn.close()

    # --- build leafs_by_cid (post-skip filter) ---
    leafs_by_cid: dict[str, list[str]] = {}
    n_conditions_with_leafs = 0
    n_conditions_unmapped = 0
    n_leafs_skipped = 0
    n_leafs_ambiguous = 0

    for cid in condition_ids:
        raw = raw_tags_by_cid.get(cid, [])
        survivors: list[str] = []
        for tag in raw:
            if tag in AMBIGUOUS_TAGS:
                n_leafs_ambiguous += 1
            if apply_skip_list and is_skip_tag(tag):
                n_leafs_skipped += 1
                continue
            survivors.append(tag)
        if survivors:
            leafs_by_cid[cid] = sorted(survivors)
            n_conditions_with_leafs += 1
        elif not raw:
            n_conditions_unmapped += 1

    # --- build nav_lookup (from tag_nav_category rows; excludes skip tags) ---
    # Only include tags that survived the skip filter somewhere in the fixture.
    surviving_tags: set[str] = set()
    for tags in leafs_by_cid.values():
        surviving_tags.update(tags)

    nav_lookup: dict[str, str] = {}
    for tag in sorted(surviving_tags):
        if tag in nav_map:
            nav_lookup[tag] = nav_map[tag]

    n_nav_mappings = len(nav_lookup)

    # Log stats
    logger.info(
        "n_conditions_total=%d n_conditions_with_leafs=%d "
        "n_conditions_unmapped=%d n_leafs_skipped=%d "
        "n_leafs_ambiguous=%d n_nav_mappings=%d",
        n_conditions_total,
        n_conditions_with_leafs,
        n_conditions_unmapped,
        n_leafs_skipped,
        n_leafs_ambiguous,
        n_nav_mappings,
    )

    if n_conditions_with_leafs == 0:
        logger.info("Zero leaf tags found for any condition_id — output will have empty leafs_by_cid")

    # --- write tagged fixture ---
    tagged["leafs_by_cid"] = leafs_by_cid
    tagged["nav_lookup"] = nav_lookup

    try:
        out_bytes = json.dumps(tagged, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        out_path.write_bytes(out_bytes)
    except Exception:
        logger.exception("Failed to write output fixture: %s", out_path)
        sys.exit(1)

    logger.info("Written tagged fixture to %s", out_path)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Refresh fixture leafs_by_cid and nav_lookup from DB (read-only)."
    )
    parser.add_argument("--fixture", required=True, type=Path, help="Input fixture JSON path")
    parser.add_argument("--db", required=True, type=Path, help="SQLite DB path (read-only)")
    parser.add_argument("--out", required=True, type=Path, help="Output tagged fixture JSON path")
    parser.set_defaults(apply_skip_list=True)
    parser.add_argument(
        "--apply-skip-list",
        dest="apply_skip_list",
        action="store_true",
        help="Apply tag_inference.is_skip_tag filter (default: on)",
    )
    parser.add_argument(
        "--no-apply-skip-list",
        dest="apply_skip_list",
        action="store_false",
        help="Do not filter tags with tag_inference.is_skip_tag",
    )
    args = parser.parse_args()

    refresh_fixture_tags(
        fixture_path=args.fixture,
        db_path=args.db,
        out_path=args.out,
        apply_skip_list=args.apply_skip_list,
    )


if __name__ == "__main__":
    main()
