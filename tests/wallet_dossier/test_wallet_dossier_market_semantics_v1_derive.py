"""Tests for §7.4 v1 derive_category — failing until v1 is implemented.

Spec: docs/superpowers/specs/2026-05-26-wallet-dossier-market-semantics-v1.md §3
Tests T1-T7.
"""
from __future__ import annotations

import pytest

from sniper.analysis.wallet_dossier.derivers import derive_category
from sniper.analysis.wallet_protocol.tag_inference import AMBIGUOUS_TAGS, _SKIP_LITERALS, is_skip_tag


# ---------------------------------------------------------------------------
# T1 — tagged path resolves nav category
# ---------------------------------------------------------------------------


def test_T1_tagged_path_resolves_nav_category():
    """Happy path: one cid, one non-ambiguous non-skip leaf, nav_lookup present."""
    cid = "0x" + "a" * 64
    result = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["microstrategy"]},
        nav_lookup={"microstrategy": "crypto"},
    )
    # Spec §3 returns 4-tuple: (category, category_source, category_unavailable_reason, leaf_tags_sorted)
    assert len(result) == 4, "derive_category must return a 4-tuple in v1"
    category, category_source, category_unavailable_reason, leaf_tags_sorted = result
    assert category == "crypto"
    assert category_source == "tag_nav_category"
    assert category_unavailable_reason is None
    assert leaf_tags_sorted == ["microstrategy"]


# ---------------------------------------------------------------------------
# T2a — market_tags_table_empty reason
# ---------------------------------------------------------------------------


def test_T2a_market_tags_table_empty():
    """Empty leafs_by_cid → reason market_tags_table_empty, leaf_tags_json IS NULL."""
    result = derive_category(
        condition_id="0x" + "a" * 64,
        leafs_by_cid={},
        nav_lookup={"crypto": "crypto"},
    )
    assert len(result) == 4
    category, category_source, reason, leaf_tags_sorted = result
    assert category == "data_unavailable"
    assert reason == "market_tags_table_empty"
    assert leaf_tags_sorted is None


def test_T2a_market_tags_table_empty_none_leafs():
    """None leafs_by_cid → same as empty → market_tags_table_empty."""
    result = derive_category(
        condition_id="0x" + "a" * 64,
        leafs_by_cid=None,
        nav_lookup={},
    )
    assert len(result) == 4
    _, _, reason, leaf_tags_sorted = result
    assert reason == "market_tags_table_empty"
    assert leaf_tags_sorted is None


# ---------------------------------------------------------------------------
# T2b — tag_not_found_in_leafs reason
# ---------------------------------------------------------------------------


def test_T2b_tag_not_found_in_leafs():
    """Some other cid is in leafs_by_cid but NOT the target cid."""
    target_cid = "0x" + "a" * 64
    other_cid = "0x" + "b" * 64
    result = derive_category(
        condition_id=target_cid,
        leafs_by_cid={other_cid: ["sports"]},
        nav_lookup={"sports": "sports"},
    )
    assert len(result) == 4
    category, category_source, reason, leaf_tags_sorted = result
    assert category == "data_unavailable"
    assert reason == "tag_not_found_in_leafs"
    assert leaf_tags_sorted is None


# ---------------------------------------------------------------------------
# T3 — multi-leaf alphabetical tie-break
# ---------------------------------------------------------------------------


def test_T3_multi_leaf_alphabetical_tiebreak():
    """Multiple leafs mapping to different nav categories → alphabetical first wins.

    Spec §3: 'category = sorted(navs)[0]'.
    Leafs: microstrategy(→economy), stocks(→economy), soccer(→sports).
    navs = {economy, sports} → sorted → economy wins.
    leaf_tags_json sorted alphabetically: [microstrategy, soccer, stocks].
    """
    cid = "0x" + "a" * 64
    result = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["microstrategy", "stocks", "soccer"]},
        nav_lookup={
            "microstrategy": "economy",
            "stocks": "economy",
            "soccer": "sports",
        },
    )
    assert len(result) == 4
    category, category_source, reason, leaf_tags_sorted = result
    assert category == "economy"
    assert category_source == "tag_nav_category"
    assert reason is None
    assert leaf_tags_sorted == ["microstrategy", "soccer", "stocks"]


# ---------------------------------------------------------------------------
# T4 — all_leafs_skipped
# ---------------------------------------------------------------------------


def test_T4_all_leafs_skipped():
    """All leafs in skip-list → reason all_leafs_skipped, leaf_tags_json IS NULL."""
    # Verify the tags we're using are actually in the skip list
    assert is_skip_tag("hide-from-new"), "hide-from-new must be a skip tag"
    assert is_skip_tag("recurring"), "recurring must be a skip tag"

    cid = "0x" + "a" * 64
    result = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["hide-from-new", "recurring"]},
        nav_lookup={"hide-from-new": "internal", "recurring": "internal"},
    )
    assert len(result) == 4
    category, category_source, reason, leaf_tags_sorted = result
    assert category == "data_unavailable"
    assert reason == "all_leafs_skipped"
    assert leaf_tags_sorted is None


# ---------------------------------------------------------------------------
# T5 — unmapped leaf preserved
# ---------------------------------------------------------------------------


def test_T5_no_nav_mapping_for_any_leaf():
    """Surviving leaf tag not in nav_lookup → reason no_nav_mapping_for_any_leaf,
    but leaf_tags_json IS preserved (the leaf survived; nav lookup failed).

    Setup: nav_lookup is NON-EMPTY (so nav_lookup_missing_from_fixture is NOT
    triggered by the §3 nav-lookup-empty guard), but it contains no entry for
    the wallet's specific leaf. This is the T5 invariant: survivors exist but
    none map.
    """
    cid = "0x" + "a" * 64
    result = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["unknown-new-tag"]},
        # Non-empty nav_lookup that does NOT contain the wallet's leaf.
        # If this were {}, the earlier guard would give nav_lookup_missing_from_fixture.
        nav_lookup={"other-known-tag": "sports"},
    )
    assert len(result) == 4
    category, category_source, reason, leaf_tags_sorted = result
    assert category == "data_unavailable"
    assert reason == "no_nav_mapping_for_any_leaf"
    # leaf_tags_sorted is preserved despite no nav mapping
    assert leaf_tags_sorted == ["unknown-new-tag"]


def test_T5_no_nav_mapping_edge_case_partial_map():
    """Only some leafs are in nav_lookup; the unmapped ones still survive
    skip filter but contribute no nav. If ALL surviving leaves have no nav entry,
    we still get no_nav_mapping_for_any_leaf.

    Setup: nav_lookup is NON-EMPTY (so nav_lookup_missing_from_fixture is NOT
    triggered by the §3 nav-lookup-empty guard), but none of the wallet's
    specific leaves appear in it. This is the T5 invariant: survivors exist
    but none map.
    """
    cid = "0x" + "a" * 64
    result = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["brand-new-tag", "also-unknown"]},
        # Non-empty nav_lookup that does NOT contain either of the wallet's leaves.
        # If this were {}, the earlier guard would give nav_lookup_missing_from_fixture.
        nav_lookup={"other-known-tag": "sports"},
    )
    assert len(result) == 4
    _, _, reason, leaf_tags_sorted = result
    assert reason == "no_nav_mapping_for_any_leaf"
    assert sorted(leaf_tags_sorted) == ["also-unknown", "brand-new-tag"]


# ---------------------------------------------------------------------------
# T6 — ambiguous-only pending
# ---------------------------------------------------------------------------


def test_T6_ambiguous_only_pending():
    """All leafs are in AMBIGUOUS_TAGS → reason ambiguous_tag_pending_operator_decision,
    NOT routed to 'other'."""
    # Verify 'awards' is an ambiguous tag
    assert "awards" in AMBIGUOUS_TAGS

    cid = "0x" + "a" * 64
    result = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["awards"]},
        nav_lookup={"awards": "other"},
    )
    assert len(result) == 4
    category, category_source, reason, leaf_tags_sorted = result
    assert category == "data_unavailable"
    assert category_source == "data_unavailable"
    assert reason == "ambiguous_tag_pending_operator_decision"
    assert leaf_tags_sorted is None
    # Hard rule: must NOT route to "other"
    assert category != "other"


def test_T6_ambiguous_tags_plural():
    """Multiple ambiguous tags — all still ambiguous-only."""
    assert "world" in AMBIGUOUS_TAGS
    assert "tech" in AMBIGUOUS_TAGS

    cid = "0x" + "c" * 64
    result = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["awards", "world", "tech"]},
        nav_lookup={"awards": "other", "world": "other", "tech": "other"},
    )
    assert len(result) == 4
    _, _, reason, _ = result
    assert reason == "ambiguous_tag_pending_operator_decision"


# ---------------------------------------------------------------------------
# T7 — nav_lookup missing from fixture (deterministic refusal)
# ---------------------------------------------------------------------------


def test_T7_nav_lookup_missing_refuses_without_db_query():
    """Leafs present but nav_lookup is empty {} →
    reason nav_lookup_missing_from_fixture, leaf_tags_json preserved.
    The test asserts outcome only (no mock-spy), per spec §7 note."""
    cid = "0x" + "a" * 64
    result = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["microstrategy"]},
        nav_lookup={},  # empty = missing from fixture, per spec §3
    )
    assert len(result) == 4
    category, category_source, reason, leaf_tags_sorted = result
    assert category == "data_unavailable"
    assert reason == "nav_lookup_missing_from_fixture"
    # leaf_tags_sorted preserved
    assert leaf_tags_sorted == ["microstrategy"]


def test_T7_nav_lookup_none_refuses():
    """nav_lookup=None also treated as missing from fixture."""
    cid = "0x" + "a" * 64
    result = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["microstrategy"]},
        nav_lookup=None,
    )
    assert len(result) == 4
    _, _, reason, leaf_tags_sorted = result
    assert reason == "nav_lookup_missing_from_fixture"
    assert leaf_tags_sorted == ["microstrategy"]


# ---------------------------------------------------------------------------
# Mixed-case condition_id normalisation (spec §3 cid_lc = condition_id.lower())
# ---------------------------------------------------------------------------


def test_mixed_case_condition_key_normalizes_correctly_upper_cid():
    """spec §3: condition_id lookup is case-insensitive on both sides.

    Fixture key is lowercase; wallet's condition_id has UPPERCASE hex.
    The lookup cid_lc = condition_id.lower() must find the lowercase key,
    so the row resolves to a real category (not data_unavailable).
    """
    cid_upper = "0xABCDEF" + "0" * 58  # uppercase hex
    cid_lower = cid_upper.lower()       # what leafs_by_cid stores

    result = derive_category(
        condition_id=cid_upper,
        leafs_by_cid={cid_lower: ["microstrategy"]},
        nav_lookup={"microstrategy": "crypto"},
    )
    assert len(result) == 4
    category, category_source, reason, leaf_tags_sorted = result
    # Must resolve — not fall through to tag_not_found_in_leafs
    assert category == "crypto", (
        "uppercase condition_id must resolve against lowercase key in leafs_by_cid"
    )
    assert category_source == "tag_nav_category"
    assert reason is None
    assert leaf_tags_sorted == ["microstrategy"]


def test_mixed_case_condition_key_normalizes_correctly_upper_key():
    """spec §3: condition_id lookup is case-insensitive on both sides.

    Fixture key is UPPERCASE; wallet's condition_id is lowercase.
    The set comprehension {k.lower() for k in leafs_by_cid} must match
    cid_lc, so the row resolves to a real category (not data_unavailable).
    """
    cid_lower = "0xabcdef" + "0" * 58  # lowercase condition_id
    cid_upper = cid_lower.upper()       # what leafs_by_cid stores (UPPERCASE key)

    result = derive_category(
        condition_id=cid_lower,
        leafs_by_cid={cid_upper: ["soccer"]},
        nav_lookup={"soccer": "sports"},
    )
    assert len(result) == 4
    category, category_source, reason, leaf_tags_sorted = result
    # Must resolve — not fall through to tag_not_found_in_leafs
    assert category == "sports", (
        "lowercase condition_id must resolve against UPPERCASE key in leafs_by_cid"
    )
    assert category_source == "tag_nav_category"
    assert reason is None
    assert leaf_tags_sorted == ["soccer"]


# ---------------------------------------------------------------------------
# Spec vocabulary completeness: all 6 unavailable reasons are reachable
# ---------------------------------------------------------------------------


def test_all_6_unavailable_reasons_are_covered_by_spec():
    """Regression guard: the 6 reason values defined in spec §3.2 are exercised
    by tests T2a-T7 above. This test just asserts the expected set exists."""
    expected_reasons = {
        "market_tags_table_empty",
        "tag_not_found_in_leafs",
        "nav_lookup_missing_from_fixture",
        "all_leafs_skipped",
        "no_nav_mapping_for_any_leaf",
        "ambiguous_tag_pending_operator_decision",
    }
    # Verify each reason by calling derive_category with the triggering inputs
    cid = "0x" + "a" * 64

    # 1. market_tags_table_empty
    _, _, r1, _ = derive_category(condition_id=cid, leafs_by_cid={}, nav_lookup={})
    assert r1 == "market_tags_table_empty"

    # 2. tag_not_found_in_leafs
    _, _, r2, _ = derive_category(
        condition_id=cid,
        leafs_by_cid={"0x" + "b" * 64: ["sports"]},
        nav_lookup={},
    )
    assert r2 == "tag_not_found_in_leafs"

    # 3. ambiguous_tag_pending_operator_decision
    _, _, r3, _ = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["awards"]},
        nav_lookup={},
    )
    assert r3 == "ambiguous_tag_pending_operator_decision"

    # 4. all_leafs_skipped
    _, _, r4, _ = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["hide-from-new"]},
        nav_lookup={},
    )
    assert r4 == "all_leafs_skipped"

    # 5. nav_lookup_missing_from_fixture (surviving leaf, empty nav_lookup)
    _, _, r5, _ = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["microstrategy"]},
        nav_lookup={},
    )
    assert r5 == "nav_lookup_missing_from_fixture"

    # 6. no_nav_mapping_for_any_leaf (nav_lookup present but leaf not in it)
    # Spec: nav_lookup present (non-empty dict not matching our leaf) → no_nav_mapping
    _, _, r6, _ = derive_category(
        condition_id=cid,
        leafs_by_cid={cid: ["unknown-tag"]},
        nav_lookup={"other-tag": "sports"},  # present but our leaf not in it
    )
    assert r6 == "no_nav_mapping_for_any_leaf"

    found = {r1, r2, r3, r4, r5, r6}
    assert found == expected_reasons
