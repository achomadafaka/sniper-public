"""Fresh-anomaly engine v2 — a separate analyzer lane for thin-history, concentrated,
large-position wallets whose information-advantage SHAPE the shuffle/permutation path
can't certify (it needs sample size).

WHAT CHANGED vs the retired `fresh_specialist.py`
    The old core was `qualifies = large_win_same_area >= 1` plus
    `excess_pp = (win_rate - mean_entry) * 100` — it flagged ordinary variance
    (3 cheap wins out of 12 has P>=3 ~ 0.44 under the binomial null). This version:
    - counts INDEPENDENT event families, not bets (correlated wins collapse);
    - measures surprise with a calibrated Poisson-binomial / anytime-valid e-process
      over those families (display p-value + the decision e-value);
    - shrinks the displayed magnitude toward the population ratio (EB);
    - splits ex-ante (knowable at trade time) from ex-post (post-resolution) evidence
      across five channels A..E;
    - subtracts alternative explanations (single-event, public-truth-stale) before
      raising a flag; keeps the correct hard excludes (structural, churn).

WHAT IT IS / IS NOT
    Pure: classifies a PositionBook + INJECTED feed closures (CLV, funding graph). It
    never fetches, never mutates verdicts, never sizes or advises. WATCH-ONLY: every
    assessment carries the base-rate `sample_warning`. It NEVER uses the word "insider".

A strong fresh flag (`high_surprise`) requires strong evidence in an information-
advantage channel (A outcome-surprise / B timing-CLV / E coordination) AND >= 2 strong
channels total AND no surviving alternative explanation. Concentration (C) + size (D)
alone — the mere conviction shape — never flags.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import stats as ST
from .types import PositionBook, PositionEpisode

DEFAULTS = {
    "thin_history_max_resolved": 40,
    "large_bet_usdc": 5_000.0,
    "very_large_bet_usdc": 25_000.0,
    "min_top_area_share": 0.5,        # Channel C moderate floor
    "strong_top_area_share": 0.7,     # Channel C strong floor
    "churn_sold_frac": 0.5,
    "churn_share_min": 0.7,
    "paired_share_min": 0.5,
    "e_strong": 20.0, "e_moderate": 5.0, "e_weak": 2.0,
    "clv_strong": 0.7, "clv_moderate": 0.55,
    "clv_max_bets": 30,                  # cap CLV price-history calls to the top-N bets by exposure
    "public_truth_stale_dominant_cost_share": 0.50,
}

_SAMPLE_WARNING = (
    "Thin-history high-conviction pattern. This shape has a HIGH false-positive base rate — most "
    "wallets matching it do NOT have durable edge (survivorship). WATCH ONLY; the forward test is "
    "the only verdict. Not a score, not proven edge, not advice."
)

_LVL_ORD = {"none": 0, "weak": 1, "moderate": 2, "strong": 3}


@dataclass
class FreshAssessment:
    assessable: bool = False
    history_thin: bool = True
    n_independent_events: int = 0
    effective_sample_size: float = 0.0
    e_value: float = 1.0
    anytime_p: float = 1.0
    obs_over_expected: float = 0.0
    fixed_p_value: float = 1.0
    shrunk_oe: float = 1.0
    ex_ante_level: str = "none"
    ex_post_level: str = "none"
    coordination_level: str = "none"
    channels: dict = field(default_factory=dict)
    channels_strong: int = 0
    high_surprise: bool = False
    alt_explanation: str | None = None
    excludes: str | None = None
    reasons: list = field(default_factory=list)
    data_gaps: list = field(default_factory=list)
    sample_warning: str = ""


def _area(e: PositionEpisode) -> str:
    if getattr(e, "category", ""):
        return e.category
    if getattr(e, "tags", None):
        return e.tags[0]
    slug = getattr(e, "event_slug", "") or ""
    return slug.rsplit("-", 1)[0] if slug else "unknown"


def _e_level(e_value: float, c: dict) -> str:
    if e_value >= c["e_strong"]:
        return "strong"
    if e_value >= c["e_moderate"]:
        return "moderate"
    if e_value >= c["e_weak"]:
        return "weak"
    return "none"


def assess_fresh_anomaly(book: PositionBook, *, clv_fn=None, funding: dict | None = None,
                         now_ts: float | None = None, cfg: dict | None = None) -> FreshAssessment:
    """Assess a wallet's fresh-anomaly shape. Pure; feeds (clv_fn, funding) are injected."""
    c = {**DEFAULTS, **(cfg or {})}
    episodes = list(book.episodes or [])
    directional = [e for e in episodes if e.is_directional]
    resolved_dir = book.resolved_directional()
    # "thin" is about DIRECTIONAL evidence depth (what the shuffle test needs), not total ledger
    # activity — a wallet with heavy structural side-activity but few directional bets is still thin.
    history_thin = len(resolved_dir) <= c["thin_history_max_resolved"]

    total_cost = sum(e.buy_cost for e in directional)

    # ---- hard excludes (first) ----------------------------------------------------------------
    if not directional or total_cost <= 0:
        return FreshAssessment(assessable=False, history_thin=history_thin,
                               reasons=["no directional bets"], sample_warning=_SAMPLE_WARNING)

    by_cid: dict[str, set] = {}
    for e in directional:
        by_cid.setdefault(e.condition_id, set()).add(e.outcome_index)
    paired_cids = {cid for cid, oidx in by_cid.items() if len(oidx) >= 2}
    paired_cost = sum(e.buy_cost for e in directional if e.condition_id in paired_cids)
    if paired_cost / total_cost >= c["paired_share_min"]:
        return FreshAssessment(assessable=False, history_thin=history_thin, excludes="structural",
                               reasons=[f"holds both outcomes of {len(paired_cids)} condition(s) — "
                                        "hedger/arb, not directional picks"],
                               sample_warning=_SAMPLE_WARNING)

    churn_cost = sum(e.buy_cost for e in directional
                     if e.buy_shares > 0 and (e.sell_shares / e.buy_shares) >= c["churn_sold_frac"])
    if churn_cost / total_cost >= c["churn_share_min"]:
        return FreshAssessment(assessable=False, history_thin=history_thin, excludes="churn_or_arb",
                               reasons=["sells most of what it buys before resolution — trader/flipper, "
                                        "not a conviction specialist"], sample_warning=_SAMPLE_WARNING)

    # ---- concentration (Channel C) ------------------------------------------------------------
    area_cost: dict[str, float] = {}
    for e in directional:
        area_cost[_area(e)] = area_cost.get(_area(e), 0.0) + e.buy_cost
    top_area = max(area_cost, key=area_cost.get)
    top_area_share = area_cost[top_area] / total_cost
    hhi = sum((v / total_cost) ** 2 for v in area_cost.values())

    # ---- event-family clustering (independent units) for Channel A -----------------------------
    fams: dict[str, list[PositionEpisode]] = {}
    for e in resolved_dir:
        fams.setdefault(e.event_slug or e.condition_id, []).append(e)
    fam_trials: list[tuple[float, float]] = []
    fam_weights: list[float] = []
    W = 0.0
    E0 = 0.0
    degenerate_families = 0
    for eps in fams.values():
        w = sum(x.buy_cost for x in eps)
        # A family with ~0 cost basis (e.g. a cost-basis-0 split/convert leg) is not a real
        # informative bet — and a null p~0 would blow up the e-value into a spurious anomaly.
        # Drop it AND disclose, rather than silently skipping.
        if w <= 0:
            degenerate_families += 1
            continue
        p_f = sum(x.buy_cost * x.entry_vwap for x in eps) / w
        if not (0.0 < p_f < 1.0):
            degenerate_families += 1
            continue
        y_frac = sum(x.buy_cost * (1 if x.won else 0) for x in eps) / w
        y_f = y_frac
        fam_trials.append((p_f, y_f))
        fam_weights.append(w)
        W += y_f
        E0 += p_f
    n_indep = len(fam_trials)
    ess = (sum(fam_weights) ** 2 / sum(w * w for w in fam_weights)) if fam_weights else 0.0
    ev = ST.eprocess_value(fam_trials)
    e_value, anytime_p = ev["e_value"], ev["anytime_p"]
    obs_over_expected = (W / E0) if E0 > 0 else 0.0
    fixed_p = ST.poisson_binomial_sf([p for p, _ in fam_trials],
                                     int(math.ceil(W - 1e-12))) if fam_trials else 1.0
    shrunk = ST.eb_shrunk_oe(W, E0)

    # ---- alternative explanations (subtract before flagging) -----------------------------------
    alt: str | None = None
    stale_large_cost = 0.0
    for eps in fams.values():
        for x in eps:
            if (x.buy_cost >= c["large_bet_usdc"] and x.first_buy_ts and x.resolution_ts
                    and x.first_buy_ts > x.resolution_ts):
                stale_large_cost += float(x.buy_cost or 0.0)
    resolved_cost = sum(float(x.buy_cost or 0.0) for x in resolved_dir)
    stale_share = (stale_large_cost / resolved_cost) if resolved_cost > 0 else 0.0
    if stale_large_cost > 0 and stale_share >= c["public_truth_stale_dominant_cost_share"]:
        alt = "public_truth_stale"
    if alt is None and n_indep <= 1:
        alt = "single_event"

    # ---- channel levels -----------------------------------------------------------------------
    channels: dict[str, dict] = {}
    data_gaps: list[str] = [
        "no market-depth feed — size-vs-depth not evaluated",
        "event-level clustering only — cross-event slug correlation not corrected "
        "(e.g. two markets for one election may inflate n_independent_events)",
    ]
    if degenerate_families:
        data_gaps.append(f"{degenerate_families} family(ies) dropped for ~0 entry price "
                         "(cost-basis-0 split/convert legs)")
    if stale_large_cost > 0 and alt != "public_truth_stale":
        data_gaps.append(f"public-truth-stale large bet observed but not dominant "
                         f"({stale_share:.0%} of resolved directional cost)")

    if alt or n_indep < 2:
        channels["A"] = {"level": "none",
                         "reason": f"surprise not assessable ({alt or 'single event'})"}
    else:
        channels["A"] = {"level": _e_level(e_value, c),
                         "reason": f"e-value {e_value:.1f} over {n_indep} independent events "
                                   f"(obs {W:.1f} vs exp {E0:.1f})"}

    c_lvl = ("strong" if top_area_share >= c["strong_top_area_share"]
             else "moderate" if top_area_share >= c["min_top_area_share"]
             else "weak" if top_area_share > 0 else "none")
    channels["C"] = {"level": c_lvl, "reason": f"{top_area_share:.0%} of stake in {top_area}"}

    max_bet = max((e.buy_cost for e in directional), default=0.0)
    d_lvl = ("strong" if max_bet >= c["very_large_bet_usdc"]
             else "moderate" if max_bet >= c["large_bet_usdc"]
             else "weak" if max_bet > 0 else "none")
    channels["D"] = {"level": d_lvl, "reason": f"largest bet ${max_bet:,.0f}"}

    if clv_fn is not None:
        # cap to the top-N resolved bets by exposure — bounds price-history calls for deep wallets
        clv_bets = sorted(resolved_dir, key=lambda e: e.buy_cost, reverse=True)[:int(c["clv_max_bets"])]
        clvs = [(e.buy_cost, 1 if v > 0 else 0) for e in clv_bets
                for v in (clv_fn(e),) if v is not None]
        if clvs:
            wsum = sum(w for w, _ in clvs)
            beat = sum(w * b for w, b in clvs) / wsum if wsum else 0.0
            b_lvl = ("strong" if beat >= c["clv_strong"] else "moderate" if beat >= c["clv_moderate"]
                     else "weak" if beat > 0 else "none")
            channels["B"] = {"level": b_lvl, "reason": f"{beat:.0%} of stake beat the closing line"}
        else:
            channels["B"] = {"level": "none", "reason": "no CLV data for resolved bets"}
            data_gaps.append("CLV unavailable for resolved bets")
    else:
        channels["B"] = {"level": "none", "reason": "no CLV feed"}
        data_gaps.append("no CLV feed")

    if funding is not None:
        linked = funding.get("linked_wallets") or []
        sync = int(funding.get("synchronized_entries") or 0)
        e_lvl = "strong" if (linked and sync) else "moderate" if linked else "none"
        channels["E"] = {"level": e_lvl,
                         "reason": f"{len(linked)} linked wallet(s), {sync} synchronized entries"}
        data_gaps.extend(funding.get("data_gaps") or [])
    else:
        channels["E"] = {"level": "none", "reason": "funding graph not run"}
        data_gaps.append("funding graph unavailable")

    edge_levels = [channels[k]["level"] for k in ("A", "B", "E")]
    high_surprise = ("strong" in edge_levels) and (alt is None)
    channels_strong = sum(1 for k in ("A", "B", "C", "D", "E") if channels[k]["level"] == "strong")
    ex_post_level = channels["A"]["level"]
    ex_ante_level = max((channels[k]["level"] for k in ("B", "C", "D")),
                        key=lambda x: _LVL_ORD.get(x, -1))
    coordination_level = channels["E"]["level"]

    reasons: list[str] = []
    if channels["A"]["level"] in ("moderate", "strong"):
        reasons.append(channels["A"]["reason"])
    reasons.append(f"concentrated: {top_area_share:.0%} of stake in {top_area}")
    if max_bet >= c["large_bet_usdc"]:
        reasons.append(f"conviction scale: largest bet ${max_bet:,.0f}")
    if channels["B"]["level"] in ("moderate", "strong"):
        reasons.append(channels["B"]["reason"])
    if coordination_level != "none":
        reasons.append(channels["E"]["reason"])
    if alt:
        reasons.append(f"alternative explanation present: {alt}")

    return FreshAssessment(
        assessable=True, history_thin=history_thin, n_independent_events=n_indep,
        effective_sample_size=round(ess, 2), e_value=e_value, anytime_p=anytime_p,
        obs_over_expected=round(obs_over_expected, 3), fixed_p_value=fixed_p,
        shrunk_oe=round(shrunk, 3), ex_ante_level=ex_ante_level, ex_post_level=ex_post_level,
        coordination_level=coordination_level, channels=channels, channels_strong=channels_strong,
        high_surprise=high_surprise, alt_explanation=alt, excludes=None, reasons=reasons,
        data_gaps=data_gaps, sample_warning=_SAMPLE_WARNING)
