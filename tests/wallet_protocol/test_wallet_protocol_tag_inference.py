"""Tests for §8 multi-grain tag inference helper."""

import sqlite3

from sniper.analysis.wallet_protocol.tag_inference import (
    AMBIGUOUS_TAGS,
    NAV_CATEGORIES,
    enumerate_tag_grains,
    fetch_tag_lookups_from_db,
    is_skip_tag,
    resolve_membership,
)


# ----------------------------------------------------------------------------
# Skip-list filter
# ----------------------------------------------------------------------------


def test_skip_tag_literal_filtered():
    assert is_skip_tag("hide-from-new")
    assert is_skip_tag("recurring")
    assert is_skip_tag("5m")
    assert is_skip_tag("4h")
    assert is_skip_tag("multi-strikes")
    assert is_skip_tag("hit-price")


def test_skip_tag_uptspt_prefix_filtered():
    """The `uptspt-*` prefix family is filtered per §4.6."""
    assert is_skip_tag("uptspt-tournament")
    assert is_skip_tag("uptspt-foo")


def test_topic_tag_not_skipped():
    assert not is_skip_tag("iran")
    assert not is_skip_tag("nba")
    assert not is_skip_tag("bitcoin")
    assert not is_skip_tag("geopolitics")


def test_empty_tag_treated_as_skip():
    assert is_skip_tag("")


# ----------------------------------------------------------------------------
# Membership resolution
# ----------------------------------------------------------------------------


def test_resolve_basic_leaf_and_nav():
    nav_lookup = {"iran": "geopolitics", "nuclear-weapons": "geopolitics"}
    m = resolve_membership(
        "0xCID", ["iran", "nuclear-weapons", "geopolitics"], nav_lookup,
    )
    assert m.condition_id == "0xCID"
    assert set(m.leaf_tags) == {"iran", "nuclear-weapons", "geopolitics"}
    # geopolitics is also a nav slug — it is not in nav_lookup, so it would be
    # unmapped here. That's expected: the nav slug is recovered from leaf-tag
    # mappings to it. With only iran/nuclear-weapons mapped, nav={geopolitics}.
    assert m.nav_tags == ["geopolitics"]
    assert m.has_unmapped_leafs  # 'geopolitics' leaf has no entry in nav_lookup


def test_resolve_dedups_nav_grains():
    """Position with two leafs both mapping to crypto → nav grain crypto only once."""
    nav_lookup = {"bitcoin": "crypto", "ethereum": "crypto"}
    m = resolve_membership("c1", ["bitcoin", "ethereum"], nav_lookup)
    assert m.nav_tags == ["crypto"]


def test_resolve_filters_skip_tags_from_leafs():
    nav_lookup = {"nba": "sports"}
    m = resolve_membership("c1", ["nba", "5m", "recurring"], nav_lookup)
    assert m.leaf_tags == ["nba"]
    assert m.nav_tags == ["sports"]


def test_resolve_ambiguous_tag_routes_to_other():
    """Ambiguous tags map to 'other' nav so they don't pollute real nav grains."""
    nav_lookup = {"crypto": "crypto"}
    m = resolve_membership("c1", ["crypto", "world"], nav_lookup)
    assert "world" in m.leaf_tags  # leaf still preserved for data-quality flag
    assert "crypto" in m.nav_tags
    assert "other" in m.nav_tags  # ambiguous routed to other


def test_resolve_unmapped_leaf_does_not_block_others():
    """One unmapped leaf marks `has_unmapped_leafs` but other leafs still map."""
    nav_lookup = {"nba": "sports"}
    m = resolve_membership("c1", ["nba", "obscure-new-tag"], nav_lookup)
    assert m.nav_tags == ["sports"]
    assert m.has_unmapped_leafs


def test_resolve_with_empty_nav_lookup():
    """When tag_nav_category not seeded, nav_tags is empty."""
    m = resolve_membership("c1", ["iran", "geopolitics"], {})
    assert "iran" in m.leaf_tags and "geopolitics" in m.leaf_tags
    assert m.nav_tags == []
    assert m.has_unmapped_leafs


# ----------------------------------------------------------------------------
# Wallet-level enumeration
# ----------------------------------------------------------------------------


def test_enumerate_basic_two_positions():
    """Two positions, one in iran/geopolitics, one in nba/sports."""
    obs = [
        {"conditionId": "cidA"},
        {"conditionId": "cidB"},
    ]
    leafs_by_cid = {
        "cidA": ["iran", "nuclear-weapons"],
        "cidB": ["nba"],
    }
    nav_lookup = {
        "iran": "geopolitics",
        "nuclear-weapons": "geopolitics",
        "nba": "sports",
    }
    e = enumerate_tag_grains(obs, leafs_by_cid, nav_lookup)
    assert e.hierarchy_source == "tag_nav_category"
    assert sorted(e.leaf_grain_members.keys()) == ["iran", "nba", "nuclear-weapons"]
    assert e.leaf_grain_members["iran"] == [0]
    assert e.leaf_grain_members["nba"] == [1]
    assert e.nav_grain_members["geopolitics"] == [0]
    assert e.nav_grain_members["sports"] == [1]


def test_enumerate_position_in_multiple_nav_grains():
    """A multi-tagged position contributes to multiple nav grains independently."""
    obs = [{"conditionId": "cid1"}]
    leafs_by_cid = {"cid1": ["bitcoin", "election", "trump"]}
    nav_lookup = {"bitcoin": "crypto", "election": "elections", "trump": "politics"}
    e = enumerate_tag_grains(obs, leafs_by_cid, nav_lookup)
    assert e.nav_grain_members["crypto"] == [0]
    assert e.nav_grain_members["elections"] == [0]
    assert e.nav_grain_members["politics"] == [0]


def test_enumerate_hierarchy_source_none_when_lookup_empty():
    obs = [{"conditionId": "c1"}]
    e = enumerate_tag_grains(obs, {"c1": ["iran"]}, {})
    assert e.hierarchy_source == "none"
    assert e.nav_grain_members == {}
    assert e.leaf_grain_members == {"iran": [0]}


def test_enumerate_records_skipped_count():
    """Skip-list tags are tallied for diagnostics, not enumerated."""
    obs = [{"conditionId": "c1"}]
    leafs_by_cid = {"c1": ["nba", "5m", "recurring", "hide-from-new"]}
    e = enumerate_tag_grains(obs, leafs_by_cid, {"nba": "sports"})
    assert e.skipped_count == 3
    assert "5m" not in e.leaf_grain_members
    assert "nba" in e.leaf_grain_members


def test_enumerate_records_ambiguous_hits():
    obs = [{"conditionId": "c1"}, {"conditionId": "c2"}]
    leafs_by_cid = {"c1": ["world", "iran"], "c2": ["awards"]}
    nav_lookup = {"iran": "geopolitics"}
    e = enumerate_tag_grains(obs, leafs_by_cid, nav_lookup)
    assert "world" in e.ambiguous_tag_hits
    assert "awards" in e.ambiguous_tag_hits
    assert e.ambiguous_tag_hits == sorted(e.ambiguous_tag_hits)


def test_enumerate_handles_missing_cid_gracefully():
    """Observations without conditionId are skipped silently."""
    obs = [{"conditionId": "c1"}, {}, {"foo": "bar"}]
    e = enumerate_tag_grains(obs, {"c1": ["nba"]}, {"nba": "sports"})
    assert e.leaf_grain_members == {"nba": [0]}


def test_enumerate_handles_unknown_cid():
    """Position whose condition_id has no row in market_tags → no grain membership."""
    obs = [{"conditionId": "cid_unknown"}]
    e = enumerate_tag_grains(obs, {}, {"foo": "bar"})
    assert e.leaf_grain_members == {}
    assert e.nav_grain_members == {}
    # Membership dict still has the entry with empty leaf/nav lists
    assert obs[0]["conditionId"] in {m.condition_id for m in e.membership_by_obs_index.values()}


def test_nav_categories_constant_matches_spec():
    """13 nav-bar categories + 'other'."""
    assert "other" in NAV_CATEGORIES
    assert len(NAV_CATEGORIES) == 14
    for required in (
        "politics", "sports", "crypto", "esports", "iran", "finance",
        "geopolitics", "tech", "culture", "economy", "weather", "mentions",
        "elections",
    ):
        assert required in NAV_CATEGORIES


def test_ambiguous_constant_matches_spec():
    """awards/world/tech are flagged ambiguous per §8.1."""
    assert AMBIGUOUS_TAGS == frozenset({"awards", "world", "tech"})


# ----------------------------------------------------------------------------
# DB lookup wrapper
# ----------------------------------------------------------------------------


def test_fetch_tag_lookups_from_db_empty_table():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE market_tags (
            condition_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            tag_label TEXT,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (condition_id, tag)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE tag_nav_category (
            tag TEXT PRIMARY KEY,
            nav_category TEXT NOT NULL,
            confidence TEXT NOT NULL DEFAULT 'manual',
            updated_at INTEGER NOT NULL
        )
        """
    )
    leafs_by_cid, nav_lookup = fetch_tag_lookups_from_db(conn, ["0xAAA"])
    assert leafs_by_cid == {}
    assert nav_lookup == {}


def test_fetch_tag_lookups_from_db_full_and_partial_rows():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE market_tags (
            condition_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            tag_label TEXT,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (condition_id, tag)
        );
        CREATE TABLE tag_nav_category (
            tag TEXT PRIMARY KEY,
            nav_category TEXT NOT NULL,
            confidence TEXT NOT NULL DEFAULT 'manual',
            updated_at INTEGER NOT NULL
        );
        INSERT INTO market_tags (condition_id, tag) VALUES ('0xaaa', 'politics');
        INSERT INTO market_tags (condition_id, tag) VALUES ('0xaaa', 'elections');
        INSERT INTO tag_nav_category (tag, nav_category, updated_at) VALUES ('politics', 'politics', 1);
        """
    )
    leafs_by_cid, nav_lookup = fetch_tag_lookups_from_db(conn, ["0xAAA", "0xMISSING"])
    assert leafs_by_cid == {"0xaaa": ["elections", "politics"]}
    assert nav_lookup == {"politics": "politics"}


def test_fetch_tag_lookups_from_db_missing_nav_table_degrades_to_empty_lookup():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE market_tags (
            condition_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            tag_label TEXT,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (condition_id, tag)
        )
        """
    )
    conn.execute("INSERT INTO market_tags (condition_id, tag) VALUES ('0xaaa', 'politics')")
    leafs_by_cid, nav_lookup = fetch_tag_lookups_from_db(conn, ["0xAAA"])
    assert leafs_by_cid == {"0xaaa": ["politics"]}
    assert nav_lookup == {}
