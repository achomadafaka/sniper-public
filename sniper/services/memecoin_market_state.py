"""Deterministic, local-only classification for funded memecoin entry attempts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import logging
import math
from statistics import median
from typing import Any

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg

logger = logging.getLogger("memecoin")


@dataclass(frozen=True)
class EntryClassification:
    classification_version: int
    network_id: str
    mint: str
    signal_source: str
    decision_at: str
    coin_band: str
    lifecycle: str
    liquidity_class: str
    momentum_phase: str
    market_regime: str
    data_quality: str
    risk_scale: float
    reason_codes: tuple[str, ...]
    facts: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        return value


@dataclass(frozen=True)
class PostPeakConfirmation:
    nominated: bool
    confirmed: bool
    peak_price: float | None
    display_price: float | None
    exact_price: float | None
    exact_giveback_pct: float | None
    reason: str


@dataclass(frozen=True)
class ExecutionPremium:
    allow: bool
    ratio: float | None
    display_price: float | None
    exact_price: float | None
    cap: float | None
    reason: str
    reference_price: float | None
    reference_basis: str | None


# The ratio above which an "execution premium" is a broken quote rather than an expensive route.
# 100x is two orders of magnitude clear of the widest real premium ever observed (9.87x) and two
# orders BELOW the smallest fault observed (4,280x), so the band between them is empty by
# measurement rather than by choice.
_QUOTE_UNIT_FAULT_RATIO = 100.0


def _source_fallback_on():
    """Whether a missing chart price falls back to the trader's price instead of failing open.

    `MEMECOIN_REAL_EXEC_PREMIUM_SOURCE_FALLBACK=0` restores the previous verdict exactly.
    """
    return bool(getattr(cfg, "MEMECOIN_REAL_EXEC_PREMIUM_SOURCE_FALLBACK", False))


def _source_floor_applies(classification):
    """Whether the trader's price may replace a LOWER chart price as the premium reference.

    NANO ONLY ON SOLANA (2026-09-13, operator, position 56875 MPAIR). The floor (262512d2) lets
    the trader's fill stand in for a lagging chart. But the chart lags ~25 s, so it is usually the
    price BEFORE the trader's own buy, and the trader's fill already carries his impact: with the
    floor, a route 1.53x the pre-buy chart read as 1.33x the source and passed a 1.5x cap. Funded
    Solana closes since 08-20 that the floor let through (chart >= 1.5x, floored < 1.5x): micro
    n=25 -$384.95, small n=2 -$74.83, nano n=29 +$16.34. A nano coin can keep running after the
    buy; a bigger one that jumped 50% on one buy mostly gives it back. Other chains keep the floor,
    by measurement: on 4663 the same split is the OPPOSITE (floor-let-through micro n=19 +$355.27;
    premium >= 1.5x n=58 +$289.40 vs < 1.5x n=118 +$11.54), and BSC has n=2 with a chart price.
    MEMECOIN_REAL_EXEC_PREMIUM_SOLANA_FLOOR_NANO_ONLY=0 is the rollback.
    """
    if not getattr(cfg, "MEMECOIN_REAL_EXEC_PREMIUM_SOLANA_FLOOR_NANO_ONLY", True):
        return True
    network = str(classification.get("network_id") or "").lower()
    band = classification.get("coin_band")
    if network != "solana" or not band:
        return True                      # unmeasured chain, or no band: the old behaviour
    return band == "nano"


def execution_premium_decision(
        classification: EntryClassification | dict, *,
        exact_quote_price: float | None, source_price: float | None = None,
        cap_override: float | None = None, post_trade_price: float | None = None,
        onchain: bool = False) -> ExecutionPremium:
    """Compare the route price to the chart price, with the source price as a stale-chart floor.

    The chase cap compares the exact quote to the TRADER'S FILL, so it reads the coin's move and
    our own slippage as one number.
    Measured 2026-08-23 over 211 attempts since 08-01, the two separate cleanly. Median
    display/source (did the coin move) is flat-to-DOWN in every band -- 0.876 / 0.903 / 0.973 for
    <25k / 25-100k / >100k -- while median quote/display (what our route costs) is monotone in
    size: 1.483 / 1.235 / 1.089. So a fixed source-referenced cap on a nano coin fires mostly on
    OUR slippage: 8 of the 9 chase refusals since 08-01 hit coins trading at or BELOW the price
    the trader had just paid. The gate blocked, but not for the reason it printed.

    The premium is the half that predicts the loss. Realized return over 221 filled closes by
    quote/display: <1.15x **+1.07%** (n=96), 1.15-1.5x -2.35% (n=81), 1.5-2.0x -5.38% (n=26),
    >=2.0x **-52.14%, 22% wins (n=18)**. Market move over the same fills orders nothing
    (-5.85% / -9.86% / +3.88% / +6.50%) and if anything leans AGAINST the chase premise -- the
    coins that ran did fine.

    The selected reference keeps the nano premium inside the measurement. No per-band constant is
    needed. The cut sits at the CLIFF (2.0x), not at an optimum. IN-SAMPLE: n=18 decides the top
    cell.

    Fail OPEN on a missing display price or a cap of 0 (chase-guard convention).

    ON-CHAIN (`onchain`, E11 off on the chain; spec 2026-10-08-memecoin-evm-entry-gates-onchain-design.md,
    operator decision (b)). The display price is never read. The reference is the source's
    post-trade price (the pool right after the source's swap) at MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE:
    1.2 = the live 1.5 / the measured median post-trade/display 1.25, so the gate keeps today's
    strictness; the 2026-10-15 read refits it on filled closes. Without a post-trade price the
    missing-display posture below applies unchanged (the source fill at the live cap, or abstain)."""
    cap = _number(cfg.MEMECOIN_REAL_MAX_EXEC_PREMIUM, positive=True)
    if cap is None:
        return ExecutionPremium(True, None, None, None, None, "cap_off", None, None)
    # A per-wallet override replaces the cap only while the gate is on; it never turns it on.
    override = _number(cap_override, positive=True)
    if override is not None:
        cap = override
    if isinstance(classification, EntryClassification):
        value = classification.to_dict()
    elif isinstance(classification, dict):
        value = classification
    else:
        value = {}
    facts = value.get("facts") if isinstance(value.get("facts"), dict) else {}
    display = None if onchain else _number(facts.get("display_price"), positive=True)
    source = _number(source_price, positive=True)
    exact = _number(exact_quote_price, positive=True)
    post = _number(post_trade_price, positive=True) if onchain else None
    post_cap = _number(getattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE", None), positive=True)

    if post is not None and post_cap is not None:
        cap = post_cap
        if exact is None:
            return ExecutionPremium(True, None, None, None, cap, "unpriceable_quote", post,
                                    "post_trade")
        reference, basis = post, "post_trade"
        if source is not None and source > reference and _source_floor_applies(value):
            reference, basis = source, "source"
    elif display is None:
        # NO CHART PRICE IS A THIRD OF FUNDED CLOSES, NOT A CORNER (2026-09-06). Over 30 days it is
        # 289 of 861, net -$293.88, and every one of them was admitted unjudged because this
        # returned `no_display_price` and failed open. Of those, 155 carry a usable TRADER price,
        # and that reference orders their returns cleanly: +7.52% below 1.15x (n=73), then
        # -3.59%, -22.56% and -5.90% above it. A fallback at the same cap refuses 43 of them worth
        # -$348.79.
        #
        # THE DOCSTRING'S OBJECTION TO A SOURCE REFERENCE STILL STANDS WHERE IT WAS MADE. It argues
        # a source-referenced cap fires mostly on OUR slippage for nano coins -- and that is about
        # using source INSTEAD of the chart. This uses it only where the chart is ABSENT and the
        # alternative is no gate at all, and the numbers above are measured on exactly that
        # population.
        if not (_source_fallback_on() and source is not None and exact is not None):
            return ExecutionPremium(True, None, None, exact, cap, "no_display_price", None, None)
        reference, basis = source, "source"
    else:
        if exact is None:
            return ExecutionPremium(True, None, display, None, cap, "unpriceable_quote",
                                    display, "chart")
        # CHART PRESENT: nothing changes. Chart is the reference and the trader price is the floor.
        reference = display
        basis = "chart"
        if source is not None and source > reference and _source_floor_applies(value):
            reference, basis = source, "source"
    ratio = exact / reference
    # A UNIT FAULT IS NOT A PREMIUM. Measured 2026-09-02 over 7 days of 4663 premium refusals:
    # 17 of 20 were plausible route cost (1.97x-9.87x) and THREE were 4,280x, 380,148x and
    # 395,149x. VEX carried `quote_price` 2487.26 against a source price of 0.0065 -- 2487 is the
    # ETH/USD price sitting in a token-price column, the same WETH/USD defect class that produced
    # $3.47M of fake PnL on 2026-07-30. Both outcomes REFUSE, so no funded decision changes here;
    # what changes is that a broken quote stops being reported as a route premium, stops polluting
    # every measurement taken over this column, and becomes separately countable.
    # The bound is deliberately absurd rather than tuned: it must never reclassify a real premium,
    # and the widest genuine one observed is 9.87x.
    if ratio > _QUOTE_UNIT_FAULT_RATIO:
        return ExecutionPremium(
            False, ratio, display, exact, cap, "quote_unit_fault", reference, basis)
    return ExecutionPremium(
        ratio <= cap, ratio, display, exact, cap,
        "ok" if ratio <= cap else "execution_premium_over_cap", reference, basis)


def confirm_post_peak(
        classification: EntryClassification | dict, *,
        exact_quote_price: float | None) -> PostPeakConfirmation:
    """Require the executable whole-clip quote to confirm a nominal post-peak collapse."""
    if isinstance(classification, EntryClassification):
        value = classification.to_dict()
    elif isinstance(classification, dict):
        value = classification
    else:
        value = {}
    facts = value.get("facts") if isinstance(value.get("facts"), dict) else {}
    peak = _number(facts.get("peak_price"), positive=True)
    display = _number(facts.get("display_price"), positive=True)
    runup = _number(facts.get("runup_pct"))
    display_giveback = _number(facts.get("giveback_pct"))
    nominated = (
        value.get("momentum_phase") == "post_peak"
        and peak is not None
        and runup is not None
        and runup >= cfg.MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT
        and display_giveback is not None
        and display_giveback >= cfg.MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT
    )
    if not nominated:
        return PostPeakConfirmation(
            False, False, peak, display, None, None, "not_nominated")
    exact = _number(exact_quote_price, positive=True)
    if exact is None:
        return PostPeakConfirmation(
            True, False, peak, display, None, None, "exact_price_unavailable")
    exact_giveback = (1.0 - exact / peak) * 100.0
    confirmed = exact_giveback >= cfg.MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT
    return PostPeakConfirmation(
        True,
        confirmed,
        peak,
        display,
        exact,
        exact_giveback,
        "post_peak_confirmed" if confirmed else "exact_giveback_below_threshold",
    )


def _liquidity_reason(liquidity_class, source):
    """`liquidity:thin` or `liquidity:thin:pool_store` (F6 recheck, 2026-09-23).

    A depth taken from OUR OWN pool store is the VIRTUAL local liquidity 2*L*sqrt(P) of the current
    tick, not the provider's TVL: measured at a median 0.27 of theirs where both existed, and it
    can sit orders of magnitude ABOVE the actual in-range value on a narrow concentrated position
    (actual 1.999001 against virtual 2,000). The class is still worth having -- the operator asked
    for it on 2026-09-22 precisely because 2,071 of 2,254 funded 4663 attempts read `unknown` --
    but every row it produces must say WHICH number it came from, so a reader can never take a
    virtual figure for measured inventory. The prefix is unchanged, so every existing
    `startswith("liquidity:")` reader keeps working.
    """
    return f"liquidity:{liquidity_class}" + (f":{source}" if source else "")


def finalize_funded_classification(
        classification: EntryClassification, *, base_clip_usd: float) -> EntryClassification:
    """Finalize the only balance-dependent descriptive fact without rereading market state."""
    if not isinstance(classification, EntryClassification):
        raise ValueError("classification must be EntryClassification")
    base = _number(base_clip_usd, positive=True)
    if base is None:
        raise ValueError("base clip must be finite and positive")
    liquidity = _number(classification.facts.get("liquidity"), positive=True)
    if liquidity is None:
        liquidity_class = "unknown"
    elif liquidity < 20.0 * base:
        liquidity_class = "thin"
    elif liquidity < 100.0 * base:
        liquidity_class = "balanced"
    else:
        liquidity_class = "deep"
    source = classification.facts.get("liquidity_source")
    reasons = tuple(
        _liquidity_reason(liquidity_class, source) if reason.startswith("liquidity:") else reason
        for reason in classification.reason_codes
    )
    if not any(reason.startswith("liquidity:") for reason in reasons):
        reasons = (*reasons, _liquidity_reason(liquidity_class, source))
    facts = {**classification.facts, "base_clip_usd": base}
    states = (classification.lifecycle, liquidity_class, classification.momentum_phase,
              classification.market_regime)
    unknowns = sum(value == "unknown" for value in states)
    read_failed = "classification_read_failed" in reasons
    data_quality = (
        "complete" if unknowns == 0
        else "critical_unknown" if read_failed or (
            classification.momentum_phase == "unknown"
            and classification.market_regime == "unknown")
        else "partial"
    )
    return replace(
        classification,
        liquidity_class=liquidity_class,
        data_quality=data_quality,
        reason_codes=reasons,
        facts=facts,
    )


def _number(value, *, positive=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or (positive and value <= 0):
        return None
    return value


def _aware_at(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("decision_at must be an aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("decision_at must be an aware ISO timestamp")
    return parsed.astimezone(timezone.utc)


def _coin_band(effective_mcap, assumed_micro):
    if effective_mcap is None:
        return "assumed_nano" if assumed_micro else "out_of_scope"
    if effective_mcap < 50_000:
        return "nano"
    if effective_mcap < 250_000:
        return "micro"
    if effective_mcap < 1_000_000:
        return "small"
    if effective_mcap < 2_000_000:
        return "mid"
    return "out_of_scope"


def _momentum(conn, *, network_id, mint, decision, current_price):
    window_start = decision - timedelta(seconds=cfg.MEMECOIN_FUNDED_TAPE_WINDOW_S)
    rows = conn.execute(
        "SELECT observed_at,price FROM memecoin_quote_observations_v2 "
        "WHERE network_id=? AND mint=? AND price>0 AND quality IN ('ok','probe_ok') "
        "AND julianday(observed_at)>=julianday(?) AND julianday(observed_at)<=julianday(?) "
        "ORDER BY julianday(observed_at),id",
        (m.normalized_coin_network(network_id, mint), mint, window_start.isoformat(),
         decision.isoformat()),
    ).fetchall()
    observations = []
    for row in rows:
        price = _number(row[1], positive=True)
        try:
            observed = _aware_at(row[0])
        except ValueError:
            continue
        if price is not None:
            observations.append((observed, price))
    current = _number(current_price, positive=True)
    if current is not None and (not observations or observations[-1] != (decision, current)):
        observations.append((decision, current))
    facts = {
        "momentum_observations": len(observations),
        "momentum_span_s": None,
        "first_price": None,
        "peak_price": None,
        "display_price": current if current is not None else (
            observations[-1][1] if observations else None),
        "runup_pct": None,
        "giveback_pct": None,
    }
    phase, shape = _phase([(observed.timestamp(), price) for observed, price in observations])
    facts["momentum_span_s"] = shape["span_s"]
    if shape["first"] is not None:
        facts.update({
            "first_price": shape["first"],
            "peak_price": shape["peak"],
            "display_price": shape["current"],
            "runup_pct": shape["runup"],
            "giveback_pct": shape["giveback"],
        })
    return phase, facts


def _phase(points):
    """(phase, shape) from (epoch seconds, price) points, oldest first. ONE rule for both bases:
    the provider series in `_momentum` and our own tape series in `own_momentum_facts`."""
    shape = dict(span_s=None, first=None, peak=None, current=None, runup=None, giveback=None)
    if len(points) < cfg.MEMECOIN_FUNDED_TAPE_MIN_OBSERVATIONS:
        return "unknown", shape
    span = float(points[-1][0]) - float(points[0][0])
    shape["span_s"] = span
    if span < cfg.MEMECOIN_FUNDED_TAPE_MIN_SPAN_S:
        return "unknown", shape
    first = points[0][1]
    peak = max(price for _, price in points)
    current = points[-1][1]
    runup = (peak / first - 1.0) * 100.0
    giveback = (1.0 - current / peak) * 100.0
    shape.update(first=first, peak=peak, current=current, runup=runup, giveback=giveback)
    if (runup >= cfg.MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT
            and giveback >= cfg.MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT):
        return "post_peak", shape
    near_peak = giveback <= 10.0
    if runup >= 100.0 and near_peak:
        return "extended", shape
    current_gain = (current / first - 1.0) * 100.0
    if current_gain >= 10.0 and near_peak:
        return "climbing", shape
    return "flat", shape


OWN_READ_META = ("pool", "protocol", "quote_token", "quote_usd", "covered_s", "read_at", "read_ms",
                 "wait_would_ms", "includes_own_fill", "carried_n")


def own_momentum_facts(read):
    """E14 shadow facts from one own-tape read (`memecoin_volume_writer.read_momentum`).

    Prices are USD per token, the unit `confirm_post_peak` compares with the exact quote. The
    names are the own basis's own (`own_*`), so no reader takes them for the provider's."""
    read = read if isinstance(read, dict) else {}
    points = list(read.get("points") or ()) if read.get("reason") == "ok" else []
    phase, shape = _phase(points)
    facts = {
        "phase": phase,
        "reason": read.get("reason") or "read_failed",
        "points": len(points),
        "span_s": shape["span_s"],
        "own_first_price": shape["first"],
        "own_peak_price": shape["peak"],
        "own_display_price": shape["current"],
        "own_runup_pct": shape["runup"],
        "own_giveback_pct": shape["giveback"],
    }
    facts.update({key: read.get(key) for key in OWN_READ_META})
    return facts


def _regime(conn, *, decision, consensus_n_60m, momentum_facts):
    bucket_s = cfg.MEMECOIN_FUNDED_REGIME_BUCKET_S
    baseline_buckets = cfg.MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS
    start = decision - timedelta(seconds=bucket_s * (baseline_buckets + 1))
    # INDEX PREFILTER (2026-09-26). julianday() cannot use idx_mc_feed_v2_observed_at, so this read
    # scanned all ~510k rows: 210 ms on EVERY funded Solana entry, the slot lane's largest stage.
    # A plain string bound one calendar day before `start` lets the index prune. It is implied by
    # the exact julianday bound for 'T' and ' ' separators and any UTC offset under a day, so the
    # row set, and therefore the regime, is unchanged; the julianday predicates stay the authority.
    coarse = (start - timedelta(days=1)).date().isoformat()
    rows = conn.execute(
        "SELECT network_id,mint,source_user_id,observed_at "
        "FROM memecoin_feed_observations_v2 WHERE observed_at>=? AND normalized_side='buy' "
        "AND clock_domain='observed_real_utc' AND julianday(observed_at)>=julianday(?) "
        "AND julianday(observed_at)<julianday(?) ORDER BY julianday(observed_at),id",
        (coarse, start.isoformat(), decision.isoformat()),
    ).fetchall()
    current_coins, current_buyers = set(), set()
    completed = [dict(buys=0, coins=set(), buyers=set()) for _ in range(baseline_buckets)]
    latest = None
    for row in rows:
        try:
            observed = _aware_at(row[3])
        except ValueError:
            continue
        age = (decision - observed).total_seconds()
        if age < 0:
            continue
        latest = observed if latest is None or observed > latest else latest
        coin = m.coin_identity(row[0], row[1])
        buyer = str(row[2] or "").strip()
        if age < bucket_s:
            current_coins.add(coin)
            if buyer:
                current_buyers.add(buyer)
            continue
        index = int(age // bucket_s) - 1
        if 0 <= index < baseline_buckets:
            bucket = completed[index]
            bucket["buys"] += 1
            bucket["coins"].add(coin)
            if buyer:
                bucket["buyers"].add(buyer)
    nonempty = [bucket for bucket in completed if bucket["buys"] > 0]
    baseline_buys = sum(bucket["buys"] for bucket in completed)
    coin_med = median([len(bucket["coins"]) for bucket in nonempty]) if nonempty else 0.0
    buyer_med = median([len(bucket["buyers"]) for bucket in nonempty]) if nonempty else 0.0
    latest_age = (decision - latest).total_seconds() if latest is not None else None
    facts = {
        "regime_current_coins": len(current_coins),
        "regime_current_buyers": len(current_buyers),
        "regime_baseline_buys": baseline_buys,
        "regime_nonempty_buckets": len(nonempty),
        "regime_coin_median": float(coin_med),
        "regime_buyer_median": float(buyer_med),
        "regime_latest_age_s": latest_age,
    }
    usable = (
        latest_age is not None
        and latest_age <= cfg.MEMECOIN_FUNDED_REGIME_MAX_STALENESS_S
        and baseline_buys >= cfg.MEMECOIN_FUNDED_REGIME_MIN_BASELINE_BUYS
        and len(nonempty) >= cfg.MEMECOIN_FUNDED_REGIME_MIN_NONEMPTY_BUCKETS
        and coin_med > 0
        and buyer_med > 0
    )
    if not usable:
        return "unknown", facts
    coins = len(current_coins)
    buyers = len(current_buyers)
    if coins >= max(5.0, 1.75 * coin_med) and buyers >= max(3.0, 1.5 * buyer_med):
        return "hot_broad", facts
    runup = _number(momentum_facts.get("runup_pct"))
    if ((_number(consensus_n_60m) or 0) >= 2 or (runup is not None and runup >= 20.0)):
        return "hot_narrow", facts
    if coins <= 0.5 * coin_med and buyers <= 0.5 * buyer_med:
        return "thin", facts
    return "normal", facts


def regime_risk_scale(regime, signal_source):
    """The funded clip scale for a regime: the one formula classify_funded_entry and the slot
    lane's prefetch (market_risk_scale) both use."""
    from sniper.services.memecoin_origin import bypasses_selection
    if bypasses_selection(signal_source) or regime in ("hot_broad", "normal"):
        return 1.0
    return float(cfg.MEMECOIN_FUNDED_REGIME_RISK_SCALE)


def market_risk_scale(conn, *, decision_at, signal_source):
    """The scale classify_funded_entry gives a coin with no consensus and no price history, read
    BEFORE any signal (2026-09-27, the slot lane prefetches its leg for it). The regime is
    market-wide except hot_narrow, which needs a coin; a failed read is `unknown`, as in classify."""
    try:
        regime, _facts = _regime(conn, decision=_aware_at(decision_at), consensus_n_60m=None,
                                 momentum_facts={})
    except Exception:
        regime = "unknown"
    return regime_risk_scale(regime, signal_source)


def lifecycle_for(age_s, runup_pct):
    """The classifier's lifecycle rule, in one place (decision features reuse it)."""
    if age_s is None:
        return "unknown"
    if age_s <= 3600:
        return "fresh"
    if age_s <= 86400:
        return "young"
    if runup_pct is not None and runup_pct >= cfg.MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT:
        return "revival"
    return "mature"


def classify_funded_entry(
    conn,
    *,
    network_id: str,
    mint: str,
    decision_at: str,
    signal_source: str,
    effective_mcap: float | None,
    assumed_micro: bool,
    market_stats: dict,
    base_clip_usd: float | None,
    consensus_n_60m: int | None,
    mcap_basis: str | None = None,
    priced_mcap_shadow: tuple | None = None,
    momentum_own: dict | None = None,
) -> EntryClassification:
    decision = _aware_at(decision_at)
    network = m.normalized_coin_network(network_id, mint)
    mcap = _number(effective_mcap, positive=True)
    band = _coin_band(mcap, bool(assumed_micro))
    stats = market_stats if isinstance(market_stats, dict) else {}
    liquidity = _number(stats.get("liquidity"), positive=True)
    base_clip = _number(base_clip_usd, positive=True)
    age = _number(stats.get("pair_age_s"))
    if age is None:
        age = _number(stats.get("token_age_s"))
    if age is not None and age < 0:
        age = None
    reasons = [f"coin_band:{band}"]
    read_failed = False
    try:
        momentum, momentum_facts = _momentum(
            conn, network_id=network, mint=mint, decision=decision,
            current_price=stats.get("price"),
        )
    except Exception:
        momentum, momentum_facts = "unknown", {
            "momentum_observations": None,
            "momentum_span_s": None,
            "first_price": None,
            "peak_price": None,
            "display_price": _number(stats.get("price"), positive=True),
            "runup_pct": None,
            "giveback_pct": None,
        }
        read_failed = True
    runup = _number(momentum_facts.get("runup_pct"))
    lifecycle = lifecycle_for(age, runup)
    if liquidity is None or base_clip is None:
        liquidity_class = "unknown"
    elif liquidity < 20.0 * base_clip:
        liquidity_class = "thin"
    elif liquidity < 100.0 * base_clip:
        liquidity_class = "balanced"
    else:
        liquidity_class = "deep"
    try:
        regime, regime_facts = _regime(
            conn,
            decision=decision,
            consensus_n_60m=consensus_n_60m,
            momentum_facts=momentum_facts,
        )
    except Exception:
        regime, regime_facts = "unknown", {
            "regime_current_coins": None,
            "regime_current_buyers": None,
            "regime_baseline_buys": None,
            "regime_nonempty_buckets": None,
            "regime_coin_median": None,
            "regime_buyer_median": None,
            "regime_latest_age_s": None,
        }
        read_failed = True
    if read_failed:
        reasons.append("classification_read_failed")
    liquidity_source = stats.get("liquidity_source") or None
    reasons.extend((f"lifecycle:{lifecycle}",
                    _liquidity_reason(liquidity_class, liquidity_source),
                    f"momentum:{momentum}", f"regime:{regime}"))
    unknowns = sum(value == "unknown" for value in (
        lifecycle, liquidity_class, momentum, regime))
    data_quality = (
        "complete" if unknowns == 0
        else "critical_unknown" if read_failed or (momentum == "unknown" and regime == "unknown")
        else "partial"
    )
    risk_scale = regime_risk_scale(regime, signal_source)
    # WINNER RE-ENTRY LADDER (2026-09-28): the k-th re-entry after a win takes the k-th ladder
    # step of the clip through this same scale. An unreadable rank leaves the scale alone and
    # records nothing; the reservation re-reads the rank under its lock and refuses a mismatch.
    reentry = None
    try:
        reentry = m.winner_reentry_step(conn, network, str(mint), decision.isoformat(),
                                        signal_source=signal_source)
    except Exception:
        logger.exception("winner re-entry rank unreadable for %s: unscaled", mint)
    if reentry is not None and reentry["factor"] is not None:
        risk_scale *= reentry["factor"]
    facts = {
        "network_id": network,
        "mint": mint,
        "signal_source": signal_source,
        "effective_mcap": mcap,
        "assumed_micro": bool(assumed_micro),
        "pair_age_s": age,
        "liquidity": liquidity,
        # WHICH NUMBER THIS IS (F6 recheck, 2026-09-23). `pool_store` means the virtual local
        # liquidity of our own cached pool state, not the provider's TVL; absent means the
        # provider answered. `finalize_funded_classification` re-reads it from here, so the class
        # it recomputes carries the same label.
        "liquidity_source": liquidity_source,
        # PROVENANCE (2026-08-12). `liquidity` and `pair_age_s` describe the DEEPEST pair
        # DexScreener knows about this mint. That is not always the pool the swap routes
        # through, and nothing here can tell the difference. KALMAN (funded position 36192)
        # recorded liquidity $22,040.91 and pair_age_s 54,842 -- both exact facts about a
        # $24,508 Raydium pool opened 15.2 h earlier -- while the trade routed a Meteora pool
        # created 100 minutes before it holding $356, and a $36.95 clip round tripped to
        # $17.02. Neither number was wrong. Both described a pool we did not trade.
        #
        # These two fields make that visible without a second request: the batch already
        # returns them. Compare `liquidity_pair_address` against a V4 quote's `route.pool_id`.
        # Read `liquidity_pair_count` above 1 as "this mint has other pools".
        #
        # They are DESCRIPTIVE. `liquidity_class` gates nothing today (`risk_scale` comes from
        # the market regime alone), and the route's real cost is bounded by the entry impact
        # cap and the round-trip cap, which measure the route itself instead of guessing it.
        "liquidity_pair_address": stats.get("pair_address"),
        "liquidity_pair_count": _number(stats.get("n_pairs")),
        # THE REVIVAL GUARD'S OWN INPUTS (2026-08-23). `volume_alive_decision` reads volume_m5 and
        # volume_h1, divides by the market cap, refuses under the turnover floors, and throws all
        # three numbers away. Only the refusal STRING kept the value ("m5 volume $902 under floor
        # $24264"), and a refused coin has no outcome, so the filter's quality could not be measured
        # in either direction: refused entries have the volume and no outcome, filled entries have
        # the outcome and no volume. There was no join. 45 refusals since 2026-08-16 and none since
        # 08-21 -- and no way to ask whether the coins it rejected did worse than the ones it passed.
        #
        # `volume_mcap` is the denominator the GATE uses -- stats["market_cap"], not
        # `effective_mcap` above, which is resolved separately and can differ. Recording it makes
        # each decision exactly reconstructable: turnover = volume_x / volume_mcap.
        #
        # DESCRIPTIVE, like the two pair fields above. The batch already returns these; nothing
        # here gates on them and no threshold moves.
        #
        # COVERAGE. These land only on an attempt that reached a market-data read. Most attempts
        # die before one: `unroutable_chain` and `not_approved` fire first and pass only
        # `effective_mcap=ev.their_market_cap`, with no stats dict at all -- 26 of the first 27
        # payloads carried the fields as None for that reason, and `facts.liquidity` was set on
        # exactly 1. A blank `volume_mcap` is therefore the NORMAL case and not a recording
        # fault; do not read the non-null ratio as one. `coin_band` is derived from
        # `effective_mcap` and says nothing about which gate refused -- read `terminal_status`.
        "volume_m5": _number(stats.get("volume_m5")),
        "volume_h1": _number(stats.get("volume_h1")),
        "volume_mcap": _number(stats.get("market_cap"), positive=True),
        "base_clip_usd": base_clip,
        "consensus_n_60m": consensus_n_60m,
        **momentum_facts,
        # WHICH PRICES THE PHASE CAME FROM (E14, 2026-10-08). Absent on older rows, which are the
        # provider's. The own-tape shadow rides beside it under its own key and decides nothing.
        "momentum_basis": "provider_mid",
        **regime_facts,
    }
    if momentum_own is not None:
        facts["momentum_own"] = own_momentum_facts(momentum_own)
    if mcap_basis is not None:
        # WHICH STAGE PRICED THE BAND (2026-09-27): event | history | dexscreener |
        # onchain_source_fill | dexscreener_fallback | route | blind | evm_source | assumed.
        # Absent on rows written before, and on refusals decided before the stages ran.
        facts["mcap_basis"] = mcap_basis
    if priced_mcap_shadow is not None:
        # THE PRICED SHADOW (523c9968): the mcap at the source's price beside today's, and the band
        # it would give. Read-only here: the band above stays today's (Alpha, exits paper-first).
        priced, priced_basis = priced_mcap_shadow
        facts["priced_mcap"] = priced
        facts["priced_mcap_basis"] = priced_basis
        facts["priced_coin_band"] = _coin_band(_number(priced, positive=True), False)
    if reentry is not None:
        facts["reentry_rank"] = reentry["rank"]
        facts["reentry_factor"] = reentry["factor"]
    return EntryClassification(
        classification_version=cfg.MEMECOIN_FUNDED_CLASSIFICATION_VERSION,
        network_id=network,
        mint=str(mint),
        signal_source=str(signal_source or "unknown"),
        decision_at=decision.isoformat(),
        coin_band=band,
        lifecycle=lifecycle,
        liquidity_class=liquidity_class,
        momentum_phase=momentum,
        market_regime=regime,
        data_quality=data_quality,
        risk_scale=risk_scale,
        reason_codes=tuple(reasons),
        facts=facts,
    )
