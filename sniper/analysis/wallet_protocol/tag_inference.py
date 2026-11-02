"""Multi-grain tag membership for protocol v2 §8 (H5 / FH4).

A position (or flip) belongs to:
- One or more LEAF grains: each tag in `market_tags(condition_id)` after skip-list filter.
- One or more NAV grains: nav_category lookup for each leaf tag via `tag_nav_category`.

A position can belong to multiple nav grains if its tags map to multiple nav categories.
The single BH-FDR correction at branch level (§8.3) handles multiplicity inflation.

Skip list (§4.6): internal tooling / time-series-format / market-mechanic flags. Not
topic tags. Filtered before any inference. Includes literal slugs and the `uptspt-*`
prefix family.

Ambiguous tags (§8.1): mapped to nav_category='other' and surfaced as a data-quality
flag when they appear in the wallet's top tags by position count.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# 13 nav-bar categories per spec §8.1, plus 'other' for unclassified.
NAV_CATEGORIES = (
    "politics", "sports", "crypto", "esports", "iran", "finance",
    "geopolitics", "tech", "culture", "economy", "weather", "mentions",
    "elections", "other",
)

# Literal slugs from §4.6 + data/tag_nav_category_seed.yaml.
_SKIP_LITERALS = frozenset({
    "hide-from-new", "recurring", "all", "up-or-down", "neg-risk",
    "multi-strikes", "hit-price",
    "5m", "15m", "1h", "4h", "today", "weekly", "daily", "monthly",
    "todays-sports",
    "2025-predictions", "best-of-2025",
    "rewards-automation-50-4pt5-50", "rewards-20-4pt5-50",
})

# Prefix family — `uptspt-*` (per spec §4.6 skip list).
_SKIP_PREFIXES = ("uptspt-",)

# Tags flagged as ambiguous nav mapping (§8.1). When any appears in the wallet's
# top-5 tags by position count, raise data-quality flag.
AMBIGUOUS_TAGS = frozenset({"awards", "world", "tech"})


def is_skip_tag(tag: str) -> bool:
    """True if `tag` should be excluded from any tag-grain enumeration."""
    if not tag:
        return True
    if tag in _SKIP_LITERALS:
        return True
    return any(tag.startswith(p) for p in _SKIP_PREFIXES)


@dataclass
class TagMembership:
    """Per-condition_id resolved tag set."""

    condition_id: str
    leaf_tags: list[str]                                   # post-skip-list
    nav_tags: list[str]                                    # mapped from leafs via tag_nav_category
    has_unmapped_leafs: bool                               # True when one or more leaf has no nav mapping


@dataclass
class TagGrainEnumeration:
    """Output of enumerate_tag_grains for a wallet."""

    leaf_grain_members: dict[str, list[int]] = field(default_factory=dict)   # tag → list of obs_indices
    nav_grain_members: dict[str, list[int]] = field(default_factory=dict)    # nav_category → list of obs_indices
    membership_by_obs_index: dict[int, TagMembership] = field(default_factory=dict)
    hierarchy_source: str = "tag_nav_category"             # "tag_nav_category" | "none"
    unmapped_leaf_count: int = 0                           # leafs with no nav mapping
    ambiguous_tag_hits: list[str] = field(default_factory=list)              # ambiguous tags found in observations
    skipped_count: int = 0                                 # tags filtered by skip list


def fetch_tag_lookups_from_db(
    conn: sqlite3.Connection,
    condition_ids: Iterable[str],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Build (condition_id → leaf_tags, leaf_tag → nav_category) maps via db.py.

    Thin wrapper around `db.fetch_market_tags_for_cids` and
    `db.fetch_tag_nav_category_map`. All raw SQL lives in db.py per project
    convention (CLAUDE.md). `tag_nav_category` may be empty (table not seeded
    yet); caller detects via `nav_lookup == {}` → `hierarchy_source='none'`.
    """
    # Imported lazily to avoid circular import (db.py → sniper.* in some paths).
    import db as _db

    leafs_by_cid = _db.fetch_market_tags_for_cids(conn, condition_ids)
    nav_lookup = _db.fetch_tag_nav_category_map(conn)
    return leafs_by_cid, nav_lookup


def resolve_membership(
    condition_id: str,
    leaf_tags: Sequence[str],
    nav_lookup: Mapping[str, str],
    ambiguous_remap_to_other: bool = True,
) -> TagMembership:
    """Resolve one condition_id's leaf + nav membership after skip filter.

    Ambiguous tags map to 'other' (per spec §8.1 — operator must resolve before
    they enter nav-grain inference). The ambiguous tag itself is still returned
    in `leaf_tags` so the operator's data-quality flag can fire.
    """
    leafs: list[str] = []
    navs: list[str] = []
    has_unmapped = False
    for raw in leaf_tags:
        if is_skip_tag(raw):
            continue
        leafs.append(raw)
        if raw in AMBIGUOUS_TAGS:
            if ambiguous_remap_to_other:
                navs.append("other")
            continue
        nav = nav_lookup.get(raw)
        if nav is None:
            has_unmapped = True
            continue
        navs.append(nav)
    # Dedup nav (a position belongs to a nav grain at most once)
    nav_unique = list(dict.fromkeys(navs))
    return TagMembership(
        condition_id=condition_id,
        leaf_tags=leafs,
        nav_tags=nav_unique,
        has_unmapped_leafs=has_unmapped,
    )


def enumerate_tag_grains(
    observations: Sequence[Mapping],
    leafs_by_cid: Mapping[str, Sequence[str]],
    nav_lookup: Mapping[str, str],
) -> TagGrainEnumeration:
    """Build per-grain index lists for a wallet's positions or flips.

    Args:
        observations: list of dicts each with `conditionId`. Order is preserved
            in the output index lists; caller maps back via `obs_index`.
        leafs_by_cid: condition_id → list of leaf tags from market_tags.
        nav_lookup: leaf_tag → nav_category from tag_nav_category. May be empty
            (table not yet seeded), in which case hierarchy_source='none' and
            no nav grains populate.

    Returns:
        TagGrainEnumeration with two grain dicts and per-observation membership.
        Caller filters grains by their own `n >= threshold` rule (10 for H5/FH4).
    """
    enumeration = TagGrainEnumeration(
        hierarchy_source="tag_nav_category" if nav_lookup else "none",
    )
    skip_tally = 0
    ambiguous_set: set[str] = set()

    for obs_idx, obs in enumerate(observations):
        cid = obs.get("conditionId") or obs.get("condition_id") or ""
        if not cid:
            continue
        raw_leafs = leafs_by_cid.get(cid, [])
        # Tally skipped tags for diagnostics
        skip_tally += sum(1 for t in raw_leafs if is_skip_tag(t))
        # Detect ambiguous tags hitting this wallet
        for t in raw_leafs:
            if t in AMBIGUOUS_TAGS:
                ambiguous_set.add(t)
        membership = resolve_membership(cid, raw_leafs, nav_lookup)
        enumeration.membership_by_obs_index[obs_idx] = membership
        if membership.has_unmapped_leafs:
            enumeration.unmapped_leaf_count += 1
        for tag in membership.leaf_tags:
            enumeration.leaf_grain_members.setdefault(tag, []).append(obs_idx)
        for nav in membership.nav_tags:
            enumeration.nav_grain_members.setdefault(nav, []).append(obs_idx)

    enumeration.skipped_count = skip_tally
    enumeration.ambiguous_tag_hits = sorted(ambiguous_set)
    return enumeration


__all__ = [
    "NAV_CATEGORIES",
    "AMBIGUOUS_TAGS",
    "TagMembership",
    "TagGrainEnumeration",
    "is_skip_tag",
    "resolve_membership",
    "enumerate_tag_grains",
    "fetch_tag_lookups_from_db",
]
