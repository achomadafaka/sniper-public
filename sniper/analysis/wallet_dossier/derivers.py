"""Deterministic derivation rules for wallet-dossier v0.

Pure functions. No external calls. Each rule returns BOTH the derived
value AND the metadata needed to write a `data_lineage` row.

Implemented rules:
  §7.1  market_type           binary | multi_outcome | unknown
  §7.2  entry_price_bucket    p_lt_05 | p_05_25 | p_25_50 | p_50_75 | p_gte_75
  §7.3  outcome_role          binary_yes | binary_no | multi_outcome_selected | unknown
  §7.4  category              always "data_unavailable" in v0
  §7.5  selected-token resolution and exit mode
  §7.6  counterfactual class and episode extraction
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal

MarketType = Literal["binary", "multi_outcome", "unknown"]
OutcomeRole = Literal[
    "binary_yes", "binary_no", "binary_named_outcome",
    "multi_outcome_selected", "unknown",
]
FavoriteStatus = Literal["favorite", "underdog", "longshot", "unknown"]
EntryPriceBucket = Literal[
    "p_lt_05", "p_05_25", "p_25_50", "p_50_75", "p_gte_75"
]

_YES_LABELS = {"yes", "true"}
_NO_LABELS = {"no", "false"}


@dataclass(frozen=True)
class DerivedValue:
    """Pair of (value, source_detail) for lineage tracking."""

    value: str | int | None
    source_detail: str
    derivation_rule: str  # which §7 rule


# ---- §7.1 market_type ------------------------------------------------------


def derive_market_type(
    condition_id: str,
    *,
    closed_positions: list[dict[str, Any]],
    activity_rows: list[dict[str, Any]],
) -> tuple[MarketType, str, int | None]:
    """Return (market_type, source, n_outcomes_in_market).

    market_type:
      "binary"        if exactly 2 distinct asset_ids observed for condition_id
      "multi_outcome" if >2
      "unknown"       if <2 or no observations
    """
    cid_norm = condition_id.lower()
    asset_ids: set[str] = set()
    observed_asset_ids: set[str] = set()
    used_opposite_asset = False

    for p in closed_positions or []:
        if (p.get("conditionId") or "").lower() == cid_norm:
            aid = p.get("asset")
            if aid:
                aid_str = str(aid)
                asset_ids.add(aid_str)
                observed_asset_ids.add(aid_str)
            opposite = p.get("oppositeAsset")
            if opposite and str(opposite) != "0":
                opposite_str = str(opposite)
                if opposite_str not in asset_ids:
                    used_opposite_asset = True
                asset_ids.add(opposite_str)

    for r in activity_rows or []:
        if (r.get("conditionId") or "").lower() != cid_norm:
            continue
        # /activity rows record fills with the wallet's selected token.
        # We treat any non-null token reference as an outcome we've seen.
        for key in ("asset", "assetId", "makerAssetId", "takerAssetId"):
            aid = r.get(key)
            if aid and str(aid) != "0":
                aid_str = str(aid)
                asset_ids.add(aid_str)
                observed_asset_ids.add(aid_str)

    n_outcomes = len(asset_ids) if asset_ids else None
    if n_outcomes is None:
        return ("unknown", "no_observations", None)
    if n_outcomes == 1:
        # Wallet only ever traded one outcome of this condition. We
        # genuinely don't know if the market is binary or multi-outcome.
        # Per §7.1 this is the ambiguous case — flag honestly.
        return ("unknown", "outcome_token_count_ambiguous", 1)
    if n_outcomes == 2:
        if used_opposite_asset and len(observed_asset_ids) == 1:
            return ("binary", "closed_position_opposite_asset", n_outcomes)
        return ("binary", "outcome_token_count", n_outcomes)
    return ("multi_outcome", "outcome_token_count", n_outcomes)


# ---- §7.2 entry_price_bucket -----------------------------------------------


def derive_entry_price_bucket(entry_price: float | None) -> EntryPriceBucket:
    """Bin entry_price into one of 5 stable buckets (§7.2)."""
    if entry_price is None:
        # Defensive: caller should already have clamped/validated, but a
        # missing price degrades to the lowest bucket-as-default would be
        # wrong. v0 raises so the caller decides how to handle.
        raise ValueError("derive_entry_price_bucket requires non-null entry_price")
    p = max(0.0, min(1.0, float(entry_price)))
    if p < 0.05:
        return "p_lt_05"
    if p < 0.25:
        return "p_05_25"
    if p < 0.50:
        return "p_25_50"
    if p < 0.75:
        return "p_50_75"
    return "p_gte_75"


# ---- §7.3 outcome_role -----------------------------------------------------


def derive_outcome_role(
    *,
    market_type: MarketType,
    outcome_label: str | None,
) -> OutcomeRole:
    """Classify outcome_role per §7.3.

    v3 (binary outcome rank): binary conditions with non-yes/no labels
    (e.g. team names, candidate names) are now classified as
    `binary_named_outcome` instead of falling to `unknown`. This is
    NOT an accounting bucket — it lives in the structural grain.

    Hard rule: this does NOT encode favorite/longshot. That's
    `favorite_status` (see derive_favorite_status_and_rank).
    """
    label_raw = (outcome_label or "").strip()
    label_lc = label_raw.lower()
    if market_type == "binary":
        if label_lc in _YES_LABELS:
            return "binary_yes"
        if label_lc in _NO_LABELS:
            return "binary_no"
        if label_raw:
            # v3: named binary outcome (Trump, Canucks, etc.)
            return "binary_named_outcome"
        return "unknown"
    if market_type == "multi_outcome":
        return "multi_outcome_selected"
    return "unknown"


def derive_favorite_status_and_rank(
    *,
    market_type: MarketType,
    entry_price: float | None,
) -> tuple[FavoriteStatus, int | None, str]:
    """Return (favorite_status, outcome_role_rank, source).

    v3 (binary outcome rank). Only binary conditions are eligible.

    Source values:
      "binary_entry_price_complement" — derived from entry_price for binary mkt
      "entry_price_missing"           — entry_price is None/missing
      "sibling_token_prices_at_entry_not_fetched" — non-binary or non-derivable

    Per spec §2.2:
      entry_price >= 0.50  → favorite, rank=1
      0.25 <= entry < 0.50 → underdog, rank=2
      entry_price < 0.25   → longshot, rank=2
    """
    if entry_price is None:
        return ("unknown", None, "entry_price_missing")
    if market_type != "binary":
        return ("unknown", None, "sibling_token_prices_at_entry_not_fetched")
    p = max(0.0, min(1.0, float(entry_price)))
    if p >= 0.50:
        return ("favorite", 1, "binary_entry_price_complement")
    if p < 0.25:
        return ("longshot", 2, "binary_entry_price_complement")
    return ("underdog", 2, "binary_entry_price_complement")


# ---- §7.4 category (v1: nav-category resolution) ---------------------------

from sniper.analysis.wallet_protocol.tag_inference import AMBIGUOUS_TAGS, is_skip_tag


def derive_category(
    *,
    condition_id: str,
    leafs_by_cid: dict[str, list[str]] | None,
    nav_lookup: dict[str, str] | None = None,
) -> tuple[str, str, str | None, list[str] | None]:
    """Return (category, category_source, category_unavailable_reason, leaf_tags_sorted).

    Spec §7.4 v1 nav-category resolution. Filter order:
      1. ambiguous filter — drop tags in AMBIGUOUS_TAGS
      2. skip-list filter — apply tag_inference.is_skip_tag
      3. nav mapping — leaf → nav via nav_lookup
      4. tie-break — alphabetical first on the resulting unique nav set
    """
    cid_lc = condition_id.lower()

    # --- presence check ---
    if not leafs_by_cid:
        return ("data_unavailable", "data_unavailable",
                "market_tags_table_empty", None)

    # Case-insensitive key lookup: find the original dict key that matches cid_lc
    matching_key = next((k for k in leafs_by_cid if k.lower() == cid_lc), None)
    if matching_key is None:
        return ("data_unavailable", "data_unavailable",
                "tag_not_found_in_leafs", None)

    raw_leafs = leafs_by_cid[matching_key]

    # --- 1. ambiguous-first filter ---
    # AMBIGUOUS_TAGS is imported from tag_inference (frozenset; v0 contains
    # {"awards", "world", "tech"}). We do NOT call resolve_membership with
    # ambiguous_remap_to_other=True — dossier semantics refuse to claim
    # rather than route to "other".
    only_ambiguous = (
        len(raw_leafs) > 0
        and all(t in AMBIGUOUS_TAGS for t in raw_leafs)
    )
    if only_ambiguous:
        return ("data_unavailable", "data_unavailable",
                "ambiguous_tag_pending_operator_decision", None)

    non_ambiguous = [t for t in raw_leafs if t not in AMBIGUOUS_TAGS]

    # --- 2. skip-list filter ---
    surviving = [t for t in non_ambiguous if not is_skip_tag(t)]
    if not surviving:
        return ("data_unavailable", "data_unavailable",
                "all_leafs_skipped", None)

    leaf_tags_sorted = sorted(surviving)  # alphabetical, byte-stable

    # --- 3. nav lookup ---
    if not nav_lookup:
        # Fixture-deterministic refusal: no DB fallback.
        return ("data_unavailable", "data_unavailable",
                "nav_lookup_missing_from_fixture", leaf_tags_sorted)

    navs = {nav_lookup[t] for t in surviving if t in nav_lookup}
    if not navs:
        return ("data_unavailable", "data_unavailable",
                "no_nav_mapping_for_any_leaf", leaf_tags_sorted)

    # --- 4. tie-break (alphabetical; arbitrary-deterministic, NOT semantic priority) ---
    category = sorted(navs)[0]
    return (category, "tag_nav_category", None, leaf_tags_sorted)


# ---- §6.4b diagnostic_accounting (accounting-only bucket) ------------------

# This is NOT a §7 rule — it's the YES/NO/OTHER accounting bucket that
# lives in its own table per §6.4b. The string-match classifier is
# allowed here ONLY because §6.4b is diagnostic-only and physically
# separated from the strategy-grain joins (§9 hard rule).

DIAGNOSTIC_ACCOUNTING_CLASSIFIER_VERSION = "v0_string_match"


def classify_accounting_bucket(outcome_label: str | None) -> str:
    """Return YES / NO / OTHER for the diagnostic_accounting table.

    Spec §6.4b. This bucket NEVER participates in mechanism_evidence
    grain keys; that physical separation is the safety guarantee.
    """
    label_lc = (outcome_label or "").strip().lower()
    if label_lc in _YES_LABELS:
        return "YES"
    if label_lc in _NO_LABELS:
        return "NO"
    return "OTHER"


# ---- §7.5 selected_token_resolution ---------------------------------------

# v0 feature flag for rule 2 (curPrice-based resolution). DEFAULT OFF.
# Per spec §7.5 + Acceptance #10: the dossier produces NO rows with
# resolution_source == "closed_position_curPrice" until §11.5 empirical
# verification passes on all 10 v4 calibration wallets.
RESOLVE_BY_CURPRICE_ENABLED = False

ResolutionValue = Literal["won", "lost", "still_open", "unknown"]
ResolutionConfidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class ResolutionResult:
    value: ResolutionValue
    source: str
    confidence: ResolutionConfidence
    derivation_detail: str  # human-readable explanation for lineage


def _outcome_index_to_index_set(outcome_index: int | None) -> str | None:
    """CTF: outcome at index N has indexSet [2^N]."""
    if outcome_index is None or outcome_index < 0:
        return None
    return str(1 << int(outcome_index))


def _payout_int(redemption: dict[str, Any]) -> int:
    raw = redemption.get("payout")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def derive_selected_token_resolution(
    *,
    closed_position: dict[str, Any],
    redemptions: list[dict[str, Any]] | None,
    open_positions: list[dict[str, Any]] | None,
    binary_market_n_outcomes: int | None,
    wallet_holds_other_outcome_for_condition: bool = False,
    enable_curprice_rule: bool = RESOLVE_BY_CURPRICE_ENABLED,
) -> ResolutionResult:
    """Per spec §7.5. Returns (value, source, confidence, derivation_detail).

    Resolution uses (condition_id, asset_id, outcome_index) — NEVER
    condition_id alone (codex constraint). Multi-outcome markets in
    particular cannot be resolved by condition alone.

    Args:
        wallet_holds_other_outcome_for_condition: True if the wallet has
            a closed_position row for the same condition_id but a
            different asset_id. Empirically (SwissMiss v4) Polymarket's
            redemption helper emits paired-indexSet redemptions even when
            the wallet only held one side; in that case payout sign is
            still definitive *for that side*. When the wallet held BOTH
            sides, a paired payout can't be attributed to a specific
            asset_id from this record alone.

    Rule evaluation order:
      1. WalletData.redemptions match on (condition_id, our indexSet),
         with paired-redemption disambiguation when the wallet held only
         one side.
      2. [FEATURE FLAG OFF in v0] WalletData.closed_positions[i].curPrice
      3. Sibling-redemption inference (conservative; binary markets only)
      4. Fallback: still_open if open position exists, else unknown
    """
    condition_id = (closed_position.get("conditionId") or "").lower()
    asset_id = str(closed_position.get("asset") or "")
    outcome_index = closed_position.get("outcomeIndex")
    our_index_set = _outcome_index_to_index_set(outcome_index)
    opposite_asset = str(closed_position.get("oppositeAsset") or "") or None

    redemptions = redemptions or []
    open_positions = open_positions or []

    # ---- Rule 1: redemption matching by indexSet ----
    if our_index_set is not None and redemptions:
        matching = [
            r for r in redemptions
            if (r.get("condition") or "").lower() == condition_id
            and our_index_set in (r.get("indexSets") or [])
        ]
        if matching:
            # Take the latest by timestamp.
            latest = max(matching, key=lambda r: int(r.get("timestamp") or 0))
            payout = _payout_int(latest)
            sets = list(latest.get("indexSets") or [])
            sole_index = sets == [our_index_set]
            if sole_index:
                if payout > 0:
                    return ResolutionResult(
                        value="won",
                        source="redemption_event_solo",
                        confidence="high",
                        derivation_detail=(
                            f"solo redemption of indexSet={our_index_set} "
                            f"with payout={payout}>0 → outcome won"
                        ),
                    )
                return ResolutionResult(
                    value="lost",
                    source="redemption_event_solo",
                    confidence="high",
                    derivation_detail=(
                        f"solo redemption of indexSet={our_index_set} "
                        f"with payout=0 → outcome lost"
                    ),
                )
            # Paired/mixed indexSet. If the wallet only held this side
            # of the condition, the payout sign is still definitive — the
            # sibling token contributed 0 to the payout (we didn't have any).
            # If the wallet held BOTH sides, the payout combines them and
            # we can't attribute it. Polymarket's redemption helper emits
            # paired-indexSet redemptions even for single-side holders
            # (empirically: 894/894 of SwissMiss v4 redemptions).
            if not wallet_holds_other_outcome_for_condition:
                if payout > 0:
                    return ResolutionResult(
                        value="won",
                        source="redemption_event_paired_single_side_holder",
                        confidence="high",
                        derivation_detail=(
                            f"paired redemption indexSets={sets} payout={payout}>0 "
                            f"and wallet held only one side → our outcome won"
                        ),
                    )
                return ResolutionResult(
                    value="lost",
                    source="redemption_event_paired_single_side_holder",
                    confidence="high",
                    derivation_detail=(
                        f"paired redemption indexSets={sets} payout=0 "
                        f"and wallet held only one side → our outcome lost"
                    ),
                )
            # Wallet held both sides — payout cannot be attributed.
            return ResolutionResult(
                value="unknown",
                source="redemption_event_paired_both_sides_held",
                confidence="low",
                derivation_detail=(
                    f"paired redemption indexSets={sets} payout={payout} "
                    f"and wallet held BOTH sides — cannot attribute payout "
                    f"to a specific asset_id from this record alone"
                ),
            )

    # ---- Rule 2: curPrice (FLAG OFF in v0) ----
    if enable_curprice_rule:
        # Intentionally not implemented in v0. Enabling this rule
        # requires §11.5 empirical verification.
        raise NotImplementedError(
            "§7.5 rule 2 (curPrice resolution) requires §11.5 verification "
            "before enabling; v0 ships with flag OFF"
        )

    # ---- Rule 3: sibling-redemption inference (conservative) ----
    # Per spec, fires ONLY when:
    #   (a) market is provably single-winner / exhaustive (binary, ours+sibling both known)
    #   (b) sibling's solo redemption payout > 0
    #   (c) we hold the OTHER side
    #   (d) no redemption record exists for our own asset
    if (
        binary_market_n_outcomes == 2
        and opposite_asset
        and redemptions
    ):
        # No redemption for our asset (else rule 1 would have matched)
        # — already implied since we reached rule 3.
        # Look for a SOLO sibling redemption matching the opposite token.
        # The sibling's indexSet is the complement: if ours is "1", sibling is "2"; if "2", sibling "1".
        if our_index_set in ("1", "2"):
            sibling_index_set = "2" if our_index_set == "1" else "1"
            sibling_matches = [
                r for r in redemptions
                if (r.get("condition") or "").lower() == condition_id
                and list(r.get("indexSets") or []) == [sibling_index_set]
            ]
            if sibling_matches:
                latest = max(sibling_matches, key=lambda r: int(r.get("timestamp") or 0))
                if _payout_int(latest) > 0:
                    return ResolutionResult(
                        value="lost",
                        source="sibling_redemption_inferred_exhaustive",
                        confidence="medium",
                        derivation_detail=(
                            f"binary market; sibling indexSet={sibling_index_set} "
                            f"redeemed solo with payout>0; ours was the losing side"
                        ),
                    )

    # ---- Rule 4: fallback ----
    # Token-wide check: an open row for the SAME asset_id means we still hold
    # THIS side. An open row for the sibling asset (same condition, different
    # asset_id) means the wallet pivoted to the other side — our closed token
    # is NOT still open. Codex 2026-05-27 audit finding.
    asset_id_norm = str(asset_id) if asset_id else None
    has_open_position = False
    if asset_id_norm:
        has_open_position = any(
            (p.get("conditionId") or "").lower() == condition_id
            and str(p.get("asset") or "") == asset_id_norm
            for p in open_positions
        )
    if has_open_position:
        return ResolutionResult(
            value="still_open",
            source="open_position_exists",
            confidence="medium",
            derivation_detail="same (condition_id, asset_id) present in open_positions",
        )
    return ResolutionResult(
        value="unknown",
        source="no_resolution_signal",
        confidence="low",
        derivation_detail=(
            "no redemption for our indexSet, no open position, "
            "rule 2 disabled, rule 3 did not apply"
        ),
    )


# ---- exit_mode (follows from §7.5 resolution) -----------------------------

ExitMode = Literal["held", "pre_resolution", "still_open", "unresolved", "unknown"]


def derive_exit_mode(
    *,
    resolution: ResolutionResult,
    activity_rows: list[dict[str, Any]] | None,
    asset_id: str,
) -> ExitMode:
    """Classify how the position exited.

    - `held`: a redemption matched our token (we held to resolution).
    - `pre_resolution`: no redemption for our token, but SELL fills exist
      in activity_rows for our asset_id (we sold before resolution).
    - `still_open`: rule 4 said the position is still open.
    - `unresolved`: no signal AND market hasn't resolved (rule 4 unknown
      but no SELL fills).
    - `unknown`: degenerate / insufficient data.
    """
    if resolution.value in ("won", "lost") and resolution.source in (
        "redemption_event_solo",
        "redemption_event_mixed_ambiguous",
        "redemption_event_paired_single_side_holder",
        "sibling_redemption_inferred_exhaustive",
    ):
        # The position was held until the market resolved (we ended up
        # in a redemption event). For mixed/inferred, we held but the
        # outcome is uncertain — exit_mode is still "held".
        # Paired-single-side: Polymarket's redemption helper emits paired
        # indexSets even when the wallet only held one side; payout sign
        # is still definitive (see §7.5 rule 1, derivers.py:300).
        return "held"
    if resolution.value == "still_open":
        return "still_open"

    # No redemption match. Check for SELL activity on our asset_id.
    sell_count = 0
    for r in activity_rows or []:
        if (
            r.get("type") == "TRADE"
            and (r.get("side") or "").upper() == "SELL"
            and str(r.get("asset") or "") == asset_id
        ):
            sell_count += 1
    if sell_count > 0:
        return "pre_resolution"
    # No redemption, no sells, no open position → unresolved or unknown.
    if resolution.value == "unknown":
        return "unresolved"
    return "unknown"


# ---- entry/exit timestamps from activity_rows -----------------------------


# ---- §7.6 episode extraction + counterfactual classification --------------

CounterfactualClass = Literal[
    "held_won",
    "held_lost",
    "profitable_exit_later_won",
    "profitable_exit_later_lost",
    "loss_exit_later_won",
    "loss_exit_later_lost",
    "market_still_open",
    "unresolved",
    "insufficient_data",
    "unknown",
]

EpisodeType = Literal["partial_sell", "full_sell", "held_to_resolution", "redemption"]
TradeResult = Literal["win", "loss", "flat"]


@dataclass(frozen=True)
class ExitEpisode:
    """Per-episode truth (spec §6.2). Position-level rollup is downstream."""

    episode_index: int
    episode_type: EpisodeType
    timestamp: int
    size_exited: float
    price_exit: float
    price_entry_for_episode: float
    episode_pnl: float
    trade_result: TradeResult
    selected_token_resolution_at_episode_close: str
    counterfactual_class: CounterfactualClass
    counterfactual_source: str
    counterfactual_confidence: ResolutionConfidence


def _classify_counterfactual(
    *,
    episode_type: EpisodeType,
    trade_result: TradeResult,
    resolution_value: str,
) -> tuple[CounterfactualClass, str, ResolutionConfidence]:
    """Per spec §7.6 table.

    Returns (class, source, confidence).

    source ∈ {episode_is_resolution, eventual_resolution_observed,
              eventual_resolution_unknown}
    """
    if episode_type in ("held_to_resolution", "redemption"):
        if resolution_value == "won":
            return ("held_won", "episode_is_resolution", "high")
        if resolution_value == "lost":
            return ("held_lost", "episode_is_resolution", "high")
        if resolution_value == "still_open":
            # Inconsistent: held-resolution episode but market still open.
            # Caller shouldn't construct this; defensive.
            return ("market_still_open", "eventual_resolution_unknown", "low")
        return ("unknown", "eventual_resolution_unknown", "low")

    # episode_type in ("partial_sell", "full_sell") — pre-resolution exit
    if resolution_value == "still_open":
        return ("market_still_open", "eventual_resolution_observed", "high")
    if resolution_value == "unknown":
        return ("unresolved", "eventual_resolution_unknown", "low")
    if trade_result == "flat":
        return ("insufficient_data", "eventual_resolution_observed", "low")

    if trade_result == "win" and resolution_value == "won":
        return ("profitable_exit_later_won", "eventual_resolution_observed", "high")
    if trade_result == "win" and resolution_value == "lost":
        return ("profitable_exit_later_lost", "eventual_resolution_observed", "high")
    if trade_result == "loss" and resolution_value == "won":
        return ("loss_exit_later_won", "eventual_resolution_observed", "high")
    if trade_result == "loss" and resolution_value == "lost":
        return ("loss_exit_later_lost", "eventual_resolution_observed", "high")

    return ("unknown", "eventual_resolution_unknown", "low")


def extract_episodes_for_position(
    *,
    asset_id: str,
    avg_entry_price: float | None,
    activity_rows: list[dict[str, Any]] | None,
    selected_token_resolution: str,
    resolution_source: str,
    redemption_timestamp: int | None,
) -> list[ExitEpisode]:
    """Build per-episode rows for one position. Spec §6.2 + §7.6.

    Args:
        asset_id: the position's token id
        avg_entry_price: position-level avgPrice (proxy for episode entry
            since we don't do FIFO matching in v0)
        activity_rows: all activity rows for the wallet
        selected_token_resolution: from §7.5 — "won" / "lost" / "still_open" / "unknown"
        resolution_source: the §7.5 source label, used to decide whether
            to emit a final held_to_resolution episode
        redemption_timestamp: timestamp of the redemption event when
            applicable; None otherwise

    Returns episodes in chronological order. Empty list if there are no
    SELLs AND the position isn't redeemed.
    """
    if avg_entry_price is None:
        return []  # cannot compute episode_pnl without entry price proxy

    activity_rows = activity_rows or []
    # Filter to TRADE BUY/SELL for our asset_id, in timestamp order.
    sells: list[dict[str, Any]] = []
    bought_total: float = 0.0
    sold_total: float = 0.0
    for r in activity_rows:
        if str(r.get("asset") or "") != asset_id:
            continue
        if r.get("type") != "TRADE":
            continue
        side = (r.get("side") or "").upper()
        size = r.get("size")
        try:
            size_f = float(size) if size is not None else 0.0
        except (TypeError, ValueError):
            size_f = 0.0
        if side == "BUY":
            bought_total += size_f
        elif side == "SELL":
            sells.append(r)
            sold_total += size_f

    sells.sort(key=lambda r: int(r.get("timestamp") or 0))

    episodes: list[ExitEpisode] = []

    is_redemption_resolved = (
        selected_token_resolution in ("won", "lost")
        and resolution_source.startswith("redemption_event")
    )

    # Pre-resolution SELL episodes
    cum_sold = 0.0
    for sell in sells:
        try:
            price = max(0.0, min(1.0, float(sell.get("price"))))
        except (TypeError, ValueError):
            continue
        try:
            size = float(sell.get("size") or 0.0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        try:
            ts = int(sell.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        cum_sold += size
        # full_sell if cumulative sold equals bought total (regardless of
        # whether the position later showed up in a redemption).
        is_full = abs(cum_sold - bought_total) < 1e-9
        ep_type: EpisodeType = "full_sell" if is_full else "partial_sell"
        pnl = (price - avg_entry_price) * size
        trade_result: TradeResult = (
            "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
        )
        klass, c_source, c_conf = _classify_counterfactual(
            episode_type=ep_type,
            trade_result=trade_result,
            resolution_value=selected_token_resolution,
        )
        episodes.append(ExitEpisode(
            episode_index=len(episodes),
            episode_type=ep_type,
            timestamp=ts,
            size_exited=size,
            price_exit=price,
            price_entry_for_episode=avg_entry_price,
            episode_pnl=pnl,
            trade_result=trade_result,
            selected_token_resolution_at_episode_close=selected_token_resolution,
            counterfactual_class=klass,
            counterfactual_source=c_source,
            counterfactual_confidence=c_conf,
        ))

    # Held-to-resolution episode (when the position resolved via redemption
    # AND there are remaining shares not sold pre-resolution)
    remaining = max(0.0, bought_total - sold_total)
    if is_redemption_resolved and remaining > 0:
        price_exit = 1.0 if selected_token_resolution == "won" else 0.0
        ts = int(redemption_timestamp or 0)
        pnl = (price_exit - avg_entry_price) * remaining
        trade_result: TradeResult = (
            "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
        )
        klass, c_source, c_conf = _classify_counterfactual(
            episode_type="held_to_resolution",
            trade_result=trade_result,
            resolution_value=selected_token_resolution,
        )
        episodes.append(ExitEpisode(
            episode_index=len(episodes),
            episode_type="held_to_resolution",
            timestamp=ts,
            size_exited=remaining,
            price_exit=price_exit,
            price_entry_for_episode=avg_entry_price,
            episode_pnl=pnl,
            trade_result=trade_result,
            selected_token_resolution_at_episode_close=selected_token_resolution,
            counterfactual_class=klass,
            counterfactual_source=c_source,
            counterfactual_confidence=c_conf,
        ))

    return episodes


DominantClassResult = tuple[str, dict[str, float]]


def derive_dominant_counterfactual_class(
    episodes: list[ExitEpisode],
) -> DominantClassResult:
    """Spec §6.3: pick the dominant class by SIZE (not count). If no
    single class >50% of total size, return 'mixed_episodes'.

    Returns (dominant_class_or_mixed, share_by_class).
    """
    if not episodes:
        return ("unknown", {})
    total = sum(e.size_exited for e in episodes)
    if total <= 0:
        return ("unknown", {})
    by_class: dict[str, float] = {}
    for e in episodes:
        by_class[e.counterfactual_class] = by_class.get(e.counterfactual_class, 0.0) + e.size_exited
    share = {k: v / total for k, v in by_class.items()}
    # Pick majority class
    for klass, s in share.items():
        if s > 0.5:
            return (klass, share)
    return ("mixed_episodes", share)


def derive_entry_exit_ts(
    *,
    activity_rows: list[dict[str, Any]] | None,
    asset_id: str,
    condition_id: str,
    redemption_timestamp: int | None = None,
) -> tuple[int | None, int | None]:
    """Derive (entry_ts, exit_ts) from BUY/SELL fills for this asset.

    entry_ts = earliest BUY timestamp on this asset_id.
    exit_ts  = latest SELL or redemption timestamp.

    Returns (None, None) when activity_rows is empty for this asset.
    """
    entry_ts: int | None = None
    exit_ts: int | None = None
    for r in activity_rows or []:
        if str(r.get("asset") or "") != asset_id:
            continue
        ts = r.get("timestamp")
        try:
            ts_int = int(ts) if ts is not None else None
        except (TypeError, ValueError):
            ts_int = None
        if ts_int is None:
            continue
        side = (r.get("side") or "").upper()
        rtype = (r.get("type") or "").upper()
        if rtype == "TRADE" and side == "BUY":
            if entry_ts is None or ts_int < entry_ts:
                entry_ts = ts_int
        if rtype == "TRADE" and side == "SELL":
            if exit_ts is None or ts_int > exit_ts:
                exit_ts = ts_int
    if redemption_timestamp is not None:
        try:
            redemption_ts = int(redemption_timestamp)
        except (TypeError, ValueError):
            redemption_ts = None
        if redemption_ts is not None and (exit_ts is None or redemption_ts > exit_ts):
            exit_ts = redemption_ts
    return entry_ts, exit_ts
