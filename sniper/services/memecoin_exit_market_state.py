"""Assemble a versioned MarketState for one open position from the recorded swap tape.

Glue over the pure `sniper.analysis.exit_features` library and the `MarketState` value type. This
is the SAME `compute_state` used by the live materializer and by deterministic replay (the "one
code path" refusal). It is pure: it takes already-fetched swaps + a block->time map + a frozen
position contract, and returns a MarketState. No DB, no network here.

State precedence (spec §4): SOURCE_UNTRUSTED > DATA_INSUFFICIENT > DATA_STALE > WARMUP > TRENDING.
The finer lifecycle (WEAKENING / BREAK_*) is the decision engine's job; compute_state reports data
READINESS and the raw features, with TRENDING as the baseline "actionable, data present" state.

The thresholds below are PROVISIONAL defaults, overridable via cfg, and are frozen for real reads
in the pre-registration file (docs/superpowers/plans/2026-07-23-memecoin-real-exit-brain-prereg.md)
before any Gate-1 outcome is read.

Spec: docs/superpowers/specs/2026-07-23-memecoin-real-exit-brain-design.md.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sniper.analysis.exit_features import (
    arrival_acceleration, arrival_rate_window, confirmed_pivots, exec_price_path_per_block,
    in_window, largest_lp_removal_fraction, pool_flow, profile_capability, realized_vol,
    v3_largest_lp_removal_fraction,
)
from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
FEATURE_VERSION = "v4"  # v4 adds transaction-ordered V3 active-liquidity reconstruction

# provisional (see prereg)
_PIVOT_CONFIRM = 2
_VOL_WINDOW_S = 300.0
_ARRIVAL_WINDOW_S = 60.0
_DUST_ATOMIC = 0
_MIN_STRUCTURE_POINTS = 5
_STALE_S = 180.0


def _cfg(cfg, name, default):
    v = getattr(cfg, name, None)          # getattr(None, ...) is None, so a null cfg needs no branch
    return default if v is None else v


def _engine_version(contract):
    explicit = contract.get("engine_version")
    if explicit:
        return str(explicit)
    # Compatibility default for the funded materializer. V6 always passes its own identity, so
    # importing the feature extractor cannot pull the retired candidate policy into V6.
    from sniper.services.memecoin_exit_brain import ENGINE_VERSION_V5
    return ENGINE_VERSION_V5


def __getattr__(name):
    """Preserve the funded module constant without loading V5 when V6 imports feature code."""
    if name == "ENGINE_VERSION":
        from sniper.services.memecoin_exit_brain import ENGINE_VERSION_V5
        return ENGINE_VERSION_V5
    raise AttributeError(name)


def _epoch(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _iso(ep):
    try:
        return datetime.fromtimestamp(float(ep), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def compute_state(swaps, block_times, contract, cfg=None, *, now_ts) -> MarketState:
    chain_id = str(contract.get("chain_id") or "")
    cap = profile_capability(
        chain_id, contract.get("pool_kind"),
        v3_depth_verified=contract.get("v3_depth_verified"))
    base = dict(
        chain_id=chain_id, mint=str(contract.get("mint") or ""), pool=contract.get("pool"),
        position_id=contract.get("position_id"),
        entry_contract_version=contract.get("entry_contract_version"),
        engine_version=_engine_version(contract), feature_version=FEATURE_VERSION,
        required_families=tuple(sorted(cap.required)))

    # profile can't honestly support required features at all (Solana / unknown chain / unknown kind)
    if cap.forced_legacy:
        # ponytail: Solana observation arrival deferred. Solana rows carry no amounts and their slots
        # are absent from the EVM-only block_times map, so a trustworthy arrival RATE needs a slot->time
        # source that does not exist yet; the only clock is ingest time, which §3.5 bans for feature
        # windows. Add it when a Solana time source lands (or an operator-approved ingest-time exception).
        return MarketState(**base, state=StateLabel.SOURCE_UNTRUSTED)

    quote_side = contract.get("quote_side")
    if quote_side is None:
        return MarketState(**base, state=StateLabel.DATA_INSUFFICIENT, computed_time=_iso(now_ts))

    # The TAPE ITSELF declares a known hole below the head (Solana enumeration bound). An operator
    # log is not engine-visible, so incompleteness has to arrive here as data or the engine stays
    # candidate_capable over a tape it knows is missing trades (P1, Codex 2026-07-24). Same rule as
    # the missing-block_ts branch below: an incomplete required window fails closed.
    if contract.get("tape_incomplete"):
        return MarketState(**base, state=StateLabel.DATA_INSUFFICIENT, computed_time=_iso(now_ts))

    # Attach TRUE event time. A block with no timestamp makes the required window INCOMPLETE, so we
    # fail closed (spec §3.5) instead of silently dropping the event and acting on the remainder.
    timed, complete = [], True
    for s in swaps:
        bn = s.get("block_no")
        ep = _epoch(block_times.get(bn)) if bn is not None else None
        if ep is None:
            complete = False
            continue
        s2 = dict(s)
        s2["block_ts"] = ep
        timed.append(s2)
    if swaps and not complete:
        return MarketState(**base, state=StateLabel.DATA_INSUFFICIENT, computed_time=_iso(now_ts))
    if not timed:
        return MarketState(**base, state=StateLabel.DATA_INSUFFICIENT, computed_time=_iso(now_ts))

    latest_ep = max(s["block_ts"] for s in timed)
    watermark_raw = contract.get("capture_watermark_at")
    watermark_ep = _epoch(watermark_raw) if watermark_raw is not None else None
    if watermark_raw is not None and watermark_ep is None:
        return MarketState(**base, state=StateLabel.DATA_INSUFFICIENT,
                           computed_time=_iso(now_ts))
    window_end_ep = max(latest_ep, watermark_ep or latest_ep)
    freshness = float(now_ts) - float(window_end_ep)
    coverage = 1.0   # every event carried a resolvable block_ts (checked above)

    # event time AHEAD of the compute clock -> corrupt time; never act on it (fail closed)
    if freshness < 0:
        return MarketState(**base, state=StateLabel.DATA_INSUFFICIENT,
                           source_event_time=_iso(window_end_ep), computed_time=_iso(now_ts))

    if freshness > _cfg(cfg, "MEMECOIN_EXIT_STALE_S", _STALE_S):
        return MarketState(**base, state=StateLabel.DATA_STALE, freshness_state="stale",
                           age_s=freshness, source_event_time=_iso(window_end_ep),
                           computed_time=_iso(now_ts))

    swaps_only = [s for s in timed if s.get("kind") in (
        "v2_swap", "v3_swap", "sol_swap", "sol_curve_swap")]
    latest_swap = max(
        swaps_only,
        key=lambda s: (float(s.get("block_ts") or 0.0), int(s.get("block_no") or -1),
                       int(s.get("log_index") or 0), str(s.get("tx_sig") or "")),
        default=None)
    if latest_swap is not None:
        from sniper.analysis.exit_controller import event_identity
        latest_event_id = event_identity(
            chain_id, contract.get("pool"), latest_swap.get("tx_sig"),
            latest_swap.get("log_index"))
        try:
            latest_event_block_no = int(latest_swap.get("block_no"))
            latest_event_log_index = int(latest_swap.get("log_index") or 0)
        except (TypeError, ValueError):
            latest_event_block_no = latest_event_log_index = None
        latest_event_time = _iso(latest_swap.get("block_ts"))
    else:
        latest_event_id = None
        latest_event_time = None
        latest_event_block_no = latest_event_log_index = None
    # size-weighted per-block execution price (§3.3): removes intra-block print impact and the equal
    # timestamps that make Δt features degenerate.
    per_block = exec_price_path_per_block(swaps_only, quote_side=quote_side)
    price_path = [(ts, px) for ts, px, _n, _b in per_block]

    structure_warmup = len(price_path) < int(
        _cfg(cfg, "MEMECOIN_EXIT_MIN_STRUCTURE_POINTS", _MIN_STRUCTURE_POINTS))

    # --- features (data present, fresh, complete window) ---
    pivots = tuple(confirmed_pivots(price_path, confirm=int(_cfg(cfg, "MEMECOIN_EXIT_PIVOT_CONFIRM", _PIVOT_CONFIRM))))
    vol_window_s = float(_cfg(cfg, "MEMECOIN_EXIT_VOL_WINDOW_S", _VOL_WINDOW_S))
    vol_path = price_path
    if watermark_ep is not None and price_path and window_end_ep > price_path[-1][0]:
        cutoff = window_end_ep - vol_window_s
        before = [point for point in price_path if point[0] <= cutoff]
        recent = [point for point in price_path if point[0] > cutoff]
        anchor_price = before[-1][1] if before else price_path[0][1]
        vol_path = [(cutoff, anchor_price), *recent, (window_end_ep, price_path[-1][1])]
    rv = realized_vol(vol_path, window_s=vol_window_s)
    dust = int(_cfg(cfg, "MEMECOIN_EXIT_DUST_ATOMIC", _DUST_ATOMIC))
    win_s = float(_cfg(cfg, "MEMECOIN_EXIT_ARRIVAL_WINDOW_S", _ARRIVAL_WINDOW_S))
    # Flow shares arrival's window (P1, Codex 2026-07-24). Scanning the whole buffer let one
    # old positive swap hold the state trending while the CURRENT window was negative.
    flow_events = in_window(swaps_only, window_s=win_s, window_end_ts=window_end_ep)
    flow = pool_flow(flow_events, quote_side=quote_side, dust_atomic=dust)

    # fixed-window arrival rate + acceleration over two adjacent closed windows ending at the
    # provider-checked head when present. No events in a complete interval is known zero activity.
    rate_now = arrival_rate_window(swaps_only, quote_side=quote_side, window_s=win_s,
                                   window_end_ts=window_end_ep, dust_atomic=dust)
    rate_prev = arrival_rate_window(swaps_only, quote_side=quote_side, window_s=win_s,
                                    window_end_ts=window_end_ep - win_s, dust_atomic=dust)
    arr_accel = arrival_acceleration(rate_now, rate_prev)

    # depth (V2 + Solana): last sync quote reserve + its delta between syncs.
    # depth_pull_flag is REAL: reserve direction alone cannot distinguish LP removal from an
    # ordinary buy (a sync accompanies every swap), but a decoded `v2_burn`/`sol_burn` in the
    # window IS an LP exit, unambiguously (sol sign rule live-verified 2026-07-24 against
    # RemoveLiquidityByRange2 logs). True = LP removed here; False = depth trusted and no burn
    # seen; None = depth not trusted at all (V3 / no sync), i.e. unknown rather than "no pull".
    reserve_base = reserve = reserve_delta = None
    depth_pull = None
    fresh_depth_override = None
    is_v3 = str(contract.get("pool_kind") or "").lower() in ("v3", "v3_swap")
    event_order = lambda s: (float(s.get("block_ts") or 0.0),
                             int(s.get("block_no") or -1), int(s.get("log_index") or 0))
    # Curve account lamports include rent and are not an exact executable-depth reserve. Keep
    # sol_curve_sync for observation, but never promote it into the trusted depth family.
    syncs = sorted([s for s in timed if s.get("kind") in ("v2_sync", "sol_sync")],
                   key=event_order)
    # WO-10 V3 depth: the in-range-liquidity series is the v3_swap `liquidity` word (the analog of a
    # V2/SOL sync reserve) and the LP-pull signal is a decoded v3_burn. Only reached when the chain's
    # V3 decoder is live-verified (cap.depth_trusted for V3 is gated on MEMECOIN_V3_DEPTH_VERIFIED_CHAINS).
    v3_liq = sorted([s for s in timed if s.get("kind") == "v3_swap"
                     and s.get("liquidity") is not None and s.get("tick") is not None],
                    key=event_order) if is_v3 else []
    v3_removed_fraction = None
    if cap.depth_trusted and is_v3 and v3_liq:
        reserve = v3_liq[-1].get("liquidity")             # in-range liquidity as the depth magnitude
        v3_window = in_window(timed, window_s=win_s, window_end_ts=window_end_ep)
        v3_burns = [s for s in v3_window if s.get("kind") == "v3_burn"]
        fresh_depth_override = max(
            [v3_liq[-1]["block_ts"], *(s["block_ts"] for s in v3_burns)])
        if v3_burns:
            v3_removed_fraction = v3_largest_lp_removal_fraction(v3_window)
            # None is unmeasurable and must fail closed as a pull. Zero means every decoded burn
            # was outside the current executable range and therefore removed no active depth.
            depth_pull = v3_removed_fraction is None or v3_removed_fraction > 0.0
        else:
            depth_pull = False
    elif cap.depth_trusted and syncs:
        reserve_base = syncs[-1].get(f"amount{1 - int(quote_side)}")
        reserve = syncs[-1].get(f"amount{quote_side}")
        # windowed too: an old burn must not pin depth_pull true forever (P1, Codex 2026-07-24)
        depth_pull = any(s.get("kind") in ("v2_burn", "sol_burn")
                         for s in in_window(timed, window_s=win_s,
                                            window_end_ts=window_end_ep))
        if len(syncs) >= 2:
            try:
                reserve_delta = str(int(syncs[-1].get(f"amount{quote_side}"))
                                    - int(syncs[-2].get(f"amount{quote_side}")))
            except (TypeError, ValueError):
                reserve_delta = None

    lows = [p.price for p in pivots if p.kind == "low"]
    highs = [p.price for p in pivots if p.kind == "high"]
    prior_low = lows[-1] if lows else None
    prior_high = highs[-1] if highs else None
    higher_low_state = (lows[-1] > lows[-2]) if len(lows) >= 2 else None
    lower_high_state = (highs[-1] < highs[-2]) if len(highs) >= 2 else None
    nominal_peak = max((px for _ts, px in price_path), default=None)

    # per-FAMILY freshness (age of THAT family's newest input), not one global timestamp
    newest_swaps = max((s["block_ts"] for s in swaps_only), default=None)
    current_proof_ep = watermark_ep if watermark_ep is not None else newest_swaps
    fresh_flow = (float(now_ts) - float(current_proof_ep)) \
        if current_proof_ep is not None else None
    if fresh_depth_override is not None:                   # WO-10: V3 depth freshness = latest v3_swap
        depth_proof_ep = watermark_ep if watermark_ep is not None else fresh_depth_override
        fresh_depth = float(now_ts) - float(depth_proof_ep)
    else:
        depth_source_ep = syncs[-1]["block_ts"] if syncs else None
        depth_proof_ep = watermark_ep if watermark_ep is not None and depth_source_ep is not None \
            else depth_source_ep
        fresh_depth = (float(now_ts) - float(depth_proof_ep)) \
            if depth_proof_ep is not None else None

    window = (_iso(min(s["block_ts"] for s in timed)), _iso(window_end_ep))
    dust_share = (flow.rejected_dust_n / len(swaps_only)) if swaps_only else None

    # Family quality applies the stale bound to EACH family's OWN freshness. The overall stale gate
    # above only catches the newest event across all families; a fresh Sync must NOT let stale swaps
    # (or vice-versa) ride through as trusted flow/vol.
    stale_bound = float(_cfg(cfg, "MEMECOIN_EXIT_STALE_S", _STALE_S))

    def _quality(supported, fresh_s):
        if not supported or fresh_s is None:
            return "untrusted"
        return "trusted" if fresh_s <= stale_bound else "degraded"

    price_q = _quality(True, fresh_flow)
    flow_q = _quality(cap.flow_trusted, fresh_flow)
    vol_q = _quality(cap.vol_trusted and rv is not None, fresh_flow)
    depth_q = _quality(cap.depth_trusted and reserve is not None, fresh_depth)
    prov = {
        "price": Provenance("pool_ticks_exec_price", window, fresh_flow, coverage, price_q,
                            FEATURE_VERSION, price_kind="block_vwap_exec_atomic"),
        "flow": Provenance("pool_ticks", window, fresh_flow, coverage, flow_q, FEATURE_VERSION),
        "vol": Provenance("pool_ticks_exec_price", window, fresh_flow, coverage, vol_q,
                          FEATURE_VERSION, price_kind="block_vwap_exec_atomic"),
        "depth": Provenance(("sol_sync" if chain_id in ("solana", "sol")
                             else "v3_swap" if is_v3 else "v2_sync"),
                            window, fresh_depth, coverage, depth_q, FEATURE_VERSION),
    }

    # WO-3: each burn is paired to its same-transaction post reserve. An unrelated later sync can
    # never become the denominator; any unmeasurable burn stays None -> catastrophic at the brain.
    liq_removed_fraction = None
    if is_v3 and v3_removed_fraction is not None:
        liq_removed_fraction = v3_removed_fraction
    elif depth_pull and reserve is not None:
        liq_removed_fraction = largest_lp_removal_fraction(
            in_window(timed, window_s=win_s, window_end_ts=window_end_ep),
            quote_side=quote_side)

    common = dict(
        **base,
        liquidity_removed_fraction=liq_removed_fraction,
        latest_event_id=latest_event_id,
        latest_event_time=latest_event_time,
        latest_event_block_no=latest_event_block_no,
        latest_event_log_index=latest_event_log_index,
        source_event_time=_iso(window_end_ep), computed_time=_iso(now_ts), age_s=freshness,
        freshness_state="fresh",
        nominal_price=(price_path[-1][1] if price_path else None),
        executable_price=None,   # executable stays None (no fast lane)
        entry_price=contract.get("entry_price"), nominal_peak=nominal_peak,
        executable_peak=contract.get("executable_peak"),
        drawdown_from_exec_peak=None, drawdown_velocity=None,     # no executable series -> None (§3.4)
        realized_vol=rv, confirmed_pivots=pivots, prior_low=prior_low, prior_high=prior_high,
        higher_low_state=higher_low_state, lower_high_state=lower_high_state,
        buy_n=flow.buy_n, sell_n=flow.sell_n, buy_vol_quote=flow.buy_vol_quote,
        sell_vol_quote=flow.sell_vol_quote, net_flow_quote=flow.net_flow_quote,
        arrival_rate=rate_now, arrival_accel=arr_accel,
        wash_filter=flow.wash_filter, rejected_dust_share=dust_share,
        reserve_base=reserve_base, reserve_quote=reserve, reserve_delta=reserve_delta,
        depth_pull_flag=depth_pull,
        top_actor_concentration=None, provenance=prov)

    # WO-1 per-rule evidence contracts: an untrusted family no longer collapses the whole state to
    # DATA_INSUFFICIENT (that veto blocked the LP CUT during quiet-tape rugs — the missed-rug bug,
    # pull-lane collapse point). The label stays actionable; per-family trust rides in provenance and
    # the engine's per-rule candidate_capable(subset) lets ONLY fresh-family rules act. Profile-level
    # candidate_capable() (no arg) still fails when any required family is untrusted — unchanged.
    return MarketState(**common, state=(StateLabel.WARMUP if structure_warmup
                                        else StateLabel.TRENDING))
