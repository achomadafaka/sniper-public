"""Item 6 — EVM real-money risk venue (S1-EVM).

Same envelope as the Solana S1 but with a native-gas (ETH/BNB) reserve that includes approval gas.
The automatic concurrency cap and funded exposure are global.
The bankroll, pending USDC, and gas stay chain-local.
Generic bankroll/concurrency/exposure checks delegate to sniper.risk.policy.evaluate_entry_risk;
reservations are tagged with the chain (network_id) for exit routing."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math

from sniper.execution.evm_wallet import CHAIN_QUOTE, native_is_quote
from sniper.persistence import db_memecoin as m
from sniper.risk.models import RiskContext, RiskDecision
from sniper.risk.policy import evaluate_entry_risk
from sniper.services.memecoin_risk import (
    coin_age_refusal, persist_refused_classification, prepare_funded_contract, real_trade_size,
    scaled_funded_clip, duplicate_refusal, trial_refusal, winner_reentry_refusal,
    with_reentry_lift)
from sniper.settings import memecoin as cfg
from sniper.util.clock import real_utc_now_iso


def _reject(reason):
    return RiskDecision(allowed=False, approved_size_usdc=0.0, reason=reason, clamped=False)


@dataclass(frozen=True)
class MemecoinEvmRiskCtx:
    usdc_balance: float
    native_balance: float | None      # None = the balance READ failed (unknown), not a low balance
    effective_concurrent: int
    effective_exposure_usd: float
    effective_pending_notional_usd: float
    held_coin_ids: frozenset
    daily_tripped: bool
    real_enabled: bool
    kill_tripped: bool
    trade_size_usd: float
    max_concurrent: int
    max_total_exposure_usd: float
    min_trade_floor_usd: float
    gas_entry_native: float
    gas_approval_native: float
    gas_exit_native: float
    gas_retry_factor: float
    base_trade_size_usd: float | None = None
    chain_max_concurrent: int = 0
    chain_automatic_count: int = 0
    chain_concurrent: int = 0
    chain_exposure_usd: float = 0.0
    pending_count: int = 0            # reservations inside effective_concurrent (for diagnostics)
    network_id: str = "evm-unknown"
    cooldown_coin_ids: frozenset = frozenset()   # real closes inside the re-entry cooldown window
    losing_coin_ids: frozenset = frozenset()
    reentry_lifted: dict | None = None           # see memecoin_risk.MemecoinRiskCtx
    drawdown_pause_until: str | None = None      # ISO end of an active drawdown pause, else None
    canary_stop_reason: str | None = None        # a chain's canary stop has tripped (automatic only)
    gas_reserve_unset: tuple = ()                # per-chain gas amounts this chain must set, and has not
    # Set only on a chain where native_is_quote() -- Arc today. 0.0 everywhere else.
    native_quote_gas_reserve_usd: float = 0.0
    native_quote_gas_deficit_usd: float = 0.0    # > 0: balance can never cover the reserve at any clip
    gas_reserve_min_native: float = 0.0
    # OPERATOR OVERRIDE (2026-09-22, operator order). Mirrors memecoin_risk.py's field of the same
    # name exactly -- see its comment for what this does and does not skip.
    override_risk_caps: bool = False
    duplicate_blockers: dict = field(default_factory=dict)
    native_sizing_equity_usd: float = 0.0
    # MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS: usdc_balance already holds the spare native (2026-10-08).
    native_spendable: bool = False


def _gas_reserve(*, gas_entry_native, gas_approval_native, gas_exit_native, gas_retry_factor,
                 chain_concurrent):
    """One entry plus gas to exit every position in the chain book, with retry headroom."""
    per_exit = gas_exit_native + gas_approval_native
    return (gas_entry_native + gas_approval_native
            + (chain_concurrent + 1) * per_exit * gas_retry_factor)


def required_native_gas(ctx):
    """The native reserve for one EVM entry and all exits in its chain book."""
    return max(ctx.gas_reserve_min_native, _gas_reserve(
        gas_entry_native=ctx.gas_entry_native, gas_approval_native=ctx.gas_approval_native,
        gas_exit_native=ctx.gas_exit_native, gas_retry_factor=ctx.gas_retry_factor,
        chain_concurrent=ctx.chain_concurrent))


def required_entry_native(conn, network_id, *, own_pending_id=None, facts=None):
    """required_native_gas for one more entry on `network_id` right now, from the same inputs
    build_evm_risk_context gives the gate: the chain's gas knobs, its floor, and the open plus
    pending count. An exact unsigned own_pending_id is represented by the prospective entry,
    not counted again as pending. Optional facts retain the counts already read for telemetry.
    For the GAS-LOW page, the default still includes one additional future entry."""
    from types import SimpleNamespace
    snap = m.real_open_snapshot(conn, network=network_id)
    p_count, _p_notional = m.pending_totals(
        conn, network=network_id, own_pending_id=own_pending_id)
    required = required_native_gas(SimpleNamespace(
        gas_reserve_min_native=cfg.gas_reserve_min_native_for(network_id),
        gas_entry_native=cfg.gas_entry_native_for(network_id),
        gas_approval_native=cfg.gas_approval_native_for(network_id),
        gas_exit_native=cfg.gas_exit_native_for(network_id),
        gas_retry_factor=cfg.MEMECOIN_GAS_RETRY_FACTOR,
        chain_concurrent=snap["count"] + p_count))
    if facts is not None:
        facts.update(book_open_count=snap["count"], book_pending_count=p_count,
                     book_count=snap["count"] + p_count, book_reserve_native=required)
    return required


def canary_stop_reason(conn, network_id):
    """A reason string while a chain's canary stop is tripped, else None (eth-mainnet spec §7).

    From the Nth automatic real close on the chain, entries stop while the cumulative GROSS return
    on the deployed clip is below the threshold. It is read from the ledger on every reservation,
    so it needs no latch and no state of its own: with entries stopped the number can only move
    when an open position closes. A SELECTION stop, like the drawdown pause: a manual entry is the
    operator's own decision and is not refused by it.
    """
    stop = cfg.evm_canary_stop_for(network_id)
    if stop is None:
        return None
    from_close, threshold_pct = stop
    closes, pnl, deployed = m.evm_canary_book(conn, network_id)
    if closes < from_close or deployed <= 0:
        return None
    return_pct = pnl / deployed * 100.0
    if return_pct >= threshold_pct:
        return None
    return ("chain %s canary stop: %.1f%% gross on $%.2f deployed over %d closes is below %.0f%% "
            "(checked from close %d); automatic entries are off while it stays below"
            % (network_id, return_pct, deployed, closes, threshold_pct, from_close))


_V2V3_PROTOCOLS = frozenset({"uniswap_v2", "uniswap_v3", "pancake_v3"})


def route_gas_class(route):
    """The chain-1 gas guard's class for the route the entry quote CHOSE, or None when unknown:
    V2V3, V4, V4_HOOKED (the v4 builder's own `hooked` flag), V4_MULTIHOP or V4_MULTIHOP_HOOKED (any
    leg of the route's own legs_path, else its bridge_key/coin_key, carries a hook). Read from the
    route only, never a pool lookup; a multihop route without its legs is unknown, never assumed
    hookless."""
    if not isinstance(route, dict):
        return None
    protocol = str(route.get("protocol") or "").lower()
    if protocol in _V2V3_PROTOCOLS:
        return "V2V3"
    if protocol == "uniswap_v4":
        return "V4_HOOKED" if route.get("hooked") else "V4"
    if protocol != "uniswap_v4_multihop":
        return None
    legs = route.get("legs_path")
    keys = ([(leg or {}).get("key") for leg in legs] if isinstance(legs, list) and legs
            else [route.get("bridge_key"), route.get("coin_key")])
    hooks = []
    for key in keys:
        if not isinstance(key, dict) or not isinstance(key.get("hooks"), str):
            return None
        hooks.append(int(key["hooks"], 16) if key["hooks"].startswith("0x") else None)
    if any(h is None for h in hooks):
        return None
    return "V4_MULTIHOP_HOOKED" if any(hooks) else "V4_MULTIHOP"


def evm_gas_guard_refusal(chain, *, gas_price_wei, clip_usd, native_usd, facts=None,
                          route=None):
    """A reason string when an ENTRY must not pay this gas, else None (eth-mainnet spec §4.3).

    Only on `cfg.evm_gas_guard_chains()`; every other chain returns None unasked. Exits never
    call it: the alternative to an expensive exit is a bag we cannot sell.
      1. the gwei CEILING, a spike guard (0 = off). It is checked first: the cost cap usually
         binds well below it, and a spike should be named as one;
      2. the COST CAP: round-trip gas (units x this price x native USD) as a share of the clip.
    EVERY UNKNOWN REFUSES: an unreadable gas price is never "0 gwei", unmeasured gas units are
    never 0, and an unknown native price or clip cannot be compared with a cap.

    `facts`, a caller-owned dict, receives what was computed (gwei, ceiling_gwei, units,
    native_usd, clip_usd, cost_usd, share_pct, cap_pct) for the entry trace, on a pass and on a
    refusal (Alpha 2026-10-07: a pass recorded nothing). A value never computed stays out, never 0.
    """
    facts = {} if facts is None else facts
    chain = str(chain)
    if chain not in cfg.evm_gas_guard_chains():
        return None
    if isinstance(gas_price_wei, bool) or not isinstance(gas_price_wei, int) or gas_price_wei <= 0:
        return "gas guard: gas price unreadable"
    gwei = gas_price_wei / 1e9
    ceiling = float(cfg.evm_max_gas_gwei_for(chain) or 0.0)
    facts.update(gwei=gwei, ceiling_gwei=ceiling)
    if ceiling > 0 and gwei > ceiling:
        return "gas guard: gas %.3f gwei is over the %.1f gwei entry ceiling" % (gwei, ceiling)
    # THE ROUTE'S OWN CLASS (2026-10-07): a multihop round trip with a hooked leg is 1.05M units,
    # 1.7x the flat knob. `route` is the chosen quote's route; an unknown class refuses.
    route_class = route_gas_class(route)
    if route_class is None:
        return "gas guard: route class unknown (%s)" % (
            (route or {}).get("protocol") if isinstance(route, dict) else "no route")
    facts["route_class"] = route_class
    units = cfg.evm_roundtrip_gas_units_for(chain, route_class=route_class)
    if units is None:
        return ("gas guard: round-trip gas units are not set (MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_%s_%s"
                "%s); the fork rehearsal measures them" % (
                    chain, route_class,
                    "" if route_class in cfg._ROUNDTRIP_GAS_REQUIRED_CLASSES
                    else " or MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_%s" % chain))
    facts["units"] = units
    try:
        native, clip = float(native_usd), float(clip_usd)
    except (TypeError, ValueError):
        native = clip = 0.0
    if not native > 0:
        return "gas guard: native USD price unknown"
    facts["native_usd"] = native
    if not clip > 0:
        return "gas guard: clip unknown"
    facts["clip_usd"] = clip
    cost = units * gas_price_wei / 1e18 * native
    cap = float(cfg.evm_roundtrip_cost_cap_pct_for(chain))
    share = cost / clip * 100.0
    facts.update(cost_usd=cost, share_pct=share, cap_pct=cap)
    if share > cap:
        return ("gas guard: round trip gas $%.2f is %.1f%% of the $%.2f clip (cap %.1f%%; %d units "
                "at %.3f gwei)" % (cost, share, clip, cap, units, gwei))
    return None


def build_evm_risk_context(conn, *, usdc_balance, native_balance, day, kill_tripped=False,
                           network_id=None, risk_scale=1.0, include_reentry_policy=True,
                           enforce_concurrency=True, size_override=None, size_cap_usd=None,
                           override_risk_caps=False, native_quote_price_usd=None,
                           reentry_closes_by=None):
    # Portfolio slots/exposure are global. Sizing, pending USDC encumbrance, and native exit gas
    # remain local to this concrete chain.
    snap = m.real_open_snapshot(conn, network=network_id) if network_id else m.real_open_snapshot(conn)
    p_count, p_notional = (m.pending_totals(conn, network=network_id) if network_id
                           else m.pending_totals(conn))
    blockers = {}
    held = frozenset(m.real_open_coin_ids(conn, blockers=blockers)
                     | m.pending_open_coin_ids(conn, blockers=blockers))
    global_book = m.funded_book_cost_snapshot(conn)
    automatic_count = m.automatic_funded_count(conn)
    chain_automatic = (m.automatic_funded_count(conn, network=network_id) if network_id
                       else automatic_count)
    gas_entry = cfg.gas_entry_native_for(network_id)
    gas_approval = cfg.gas_approval_native_for(network_id)
    gas_exit = cfg.gas_exit_native_for(network_id)
    # NATIVE-TOKEN-COLLAPSE RESERVE (2026-09-16, Arc build). On every chain up to now, native gas
    # (ETH/BNB) and the USDC/WETH quote balance are separate holdings, so sizing off the full quote
    # balance and separately checking native gas in evm_entry_allowed below is safe. Arc's native
    # gas-payment asset IS its USDC quote token -- same balance -- so sizing the clip off the FULL
    # balance would spend the same USDC the post-hoc gas check assumes is still there. Computed
    # BEFORE `spendable_usdc` (not just clamped onto the end) so EVERY consumer of the spendable
    # balance -- the equity-tier slot decision below, the sizing formula in every mode including
    # 'bankroll', and the clip itself -- sees the same net-of-reserve number. `real_trade_size`'s
    # OWN spendable clamp then can never exceed it either, so nothing downstream can drift from it.
    gas_floor = cfg.gas_reserve_min_native_for(network_id)
    gas_reserve = (max(gas_floor, _gas_reserve(
        gas_entry_native=gas_entry, gas_approval_native=gas_approval, gas_exit_native=gas_exit,
        gas_retry_factor=cfg.MEMECOIN_GAS_RETRY_FACTOR, chain_concurrent=snap["count"] + p_count))
        if native_is_quote(network_id) else 0.0)
    native_equity = 0.0
    if (not native_is_quote(network_id)
            and CHAIN_QUOTE.get(str(network_id), {}).get("kind") == "eth"
            and not isinstance(native_quote_price_usd, bool)
            and not isinstance(native_balance, bool)):
        try:
            price, native = float(native_quote_price_usd), float(native_balance)
            reserve = max(gas_floor, _gas_reserve(
                gas_entry_native=gas_entry, gas_approval_native=gas_approval,
                gas_exit_native=gas_exit, gas_retry_factor=cfg.MEMECOIN_GAS_RETRY_FACTOR,
                chain_concurrent=snap["count"] + p_count))
            value = max(0.0, native - reserve) * price
            if math.isfinite(price) and price > 0 and math.isfinite(native) and math.isfinite(value):
                native_equity = value
        except (TypeError, ValueError, OverflowError):
            pass  # unknown native or price adds nothing; the gas gate still owns unknown gas
    # CHAIN-1 UNSTALL (Alpha 2026-10-08): on a chain in MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS the spare
    # native above the full-book gas reserve is spendable cash, not only equity: an expired swap
    # leaves its unwrapped WETH as native. Pending notional comes off the COMBINED pool once, so an
    # in-flight unwrap moving WETH to native never counts twice. Unknown native or price adds 0.
    native_spendable = (native_equity > 0
                        and str(network_id) in cfg.memecoin_evm_native_spendable_chains())
    if native_spendable:
        usdc_balance = float(usdc_balance) + native_equity
    _pre_reserve_spendable = max(0.0, float(usdc_balance) - p_notional)
    gas_deficit = max(0.0, gas_reserve - _pre_reserve_spendable)
    spendable_usdc = max(0.0, _pre_reserve_spendable - gas_reserve)
    # PER-CHAIN slots, keyed on the RAW network_id ("4663"), not the normalized coin network —
    # the config knob is MEMECOIN_MAX_CONCURRENT_<CHAIN> and the chain id is what the operator sets.
    # SIZING EQUITY VALUES OPEN POSITIONS AT THEIR LAST FRESH EXECUTABLE MARK (2026-09-06), not at
    # cost. A bag that has gone to nothing kept its full purchase price here and inflated every
    # later clip on the chain: LARP cost $75.22 against a $5.08 mark and made the 4663 clip $90.37
    # where honest equity gives $65.30. Stale, paused or unreadable counts as ZERO, so missing data
    # can only shrink a clip. `effective_exposure_usd` below is deliberately still at cost.
    # Spare native is part of this chain's equity, even while staged for its fast lane.
    # It is NOT wrapped cash: the existing spendable clamp and bankroll gate stay unchanged.
    unspent_native_equity = 0.0 if native_spendable else native_equity   # never counted twice
    sizing_equity = spendable_usdc + snap["mark_notional"] + p_notional + unspent_native_equity
    chain_slots = cfg.max_concurrent_for(network_id, equity=sizing_equity)
    # Slot-aware: a chain with MEMECOIN_EVM_TIER_CLIP_USD_<chain> caps the clip per ACTIVE tier.
    _chain_max_clip = float(cfg.evm_max_clip_usd_for(network_id, slots=chain_slots) or 0.0)
    # A FIXED PER-CHAIN CLIP (chain 1: $20, operator 2026-09-30) replaces the sizing mode. It is
    # not clamped to the balance: a balance under the clip refuses below ("insufficient bankroll")
    # rather than quietly trading a smaller size than the operator set.
    _fixed_clip = float(cfg.evm_fixed_clip_usd_for(network_id) or 0.0)
    base_trade_size = (float(size_override) if size_override is not None
                       else _fixed_clip if _fixed_clip > 0 else real_trade_size(
        usdc_balance=spendable_usdc,
        open_notional_usd=snap["mark_notional"] + p_notional + unspent_native_equity,
        max_concurrent=chain_slots,
        max_clip_usd=_chain_max_clip,
        bankroll_fraction=cfg.bankroll_fraction_for(network_id),
        mode=cfg.real_sizing_for(network_id)))
    trade_size = scaled_funded_clip(
        base_clip_usd=base_trade_size,
        risk_scale=risk_scale,
        floor_usd=cfg.min_trade_floor_usd_for(network_id),
    )
    # PER-ENTRY CEILING (2026-09-04, operator; per-chain override 2026-09-16). Bankroll sizing is
    # `equity / slots` with a floor and no ceiling, so a growing book grows the clip without
    # bound. Applied AFTER the risk scale so it caps what actually gets risked rather than an
    # input to it, and only ever downward -- a smaller computed clip is untouched. The
    # spendable-balance clamp inside `real_trade_size` and the route-capacity reduction
    # downstream both still apply.
    # The risk scale can only shrink a clip, never grow it past the ceiling already applied in
    # `real_trade_size`, so no second clamp is needed here.
    if _chain_max_clip > 0:
        trade_size = min(trade_size, _chain_max_clip)
    # A CALLER'S HARD CAP (2026-09-13, Codex P1): the Pons creator-tax test allows $50 a trade.
    # Applied last and only downward, so a smaller size from the normal rules stands. It must be
    # here, inside the reservation, because the funded contract is sealed from this size.
    if size_cap_usd is not None and float(size_cap_usd) > 0:
        trade_size = min(trade_size, float(size_cap_usd))
    from sniper.services.memecoin_wallet_caps import global_cap_usd
    hard_cap = global_cap_usd()
    if hard_cap is not None:
        trade_size = min(trade_size, hard_cap)
    # THE GAS SET-ASIDE BACKSTOP: `spendable_usdc` is already net of the reserve, and every
    # sizing mode's OWN clamp keeps the clip inside it -- EXCEPT 'fixed', which returns a flat
    # MEMECOIN_TRADE_SIZE_USD irrespective of the balance argument. This is what actually enforces
    # the reserve for 'fixed' mode; for 'bankroll'/'fraction' it is a defensive no-op (the clip is
    # already <= spendable_usdc, which is already net of the reserve).
    if gas_reserve > 0:
        trade_size = min(trade_size, spendable_usdc)
    lifted = {"cooldown": {}, "losing": {}}       # filled by the two re-entry reads below
    return MemecoinEvmRiskCtx(
        usdc_balance=float(usdc_balance),
        native_balance=(None if native_balance is None else float(native_balance)),
        effective_concurrent=automatic_count,
        pending_count=p_count,
        effective_exposure_usd=global_book["notional"],
        effective_pending_notional_usd=p_notional,
        held_coin_ids=held,
        duplicate_blockers=blockers,
        # A DISABLED BREAKER MUST ALSO RELEASE ITS OWN LATCH (2026-09-18). The trip is a row in
        # `memecoin_daily_loss_trips` and this read does not consult the limit, so setting the
        # limit to 0 while today was already latched left entries blocked by a breaker the
        # operator had just switched off — a switch that does not switch anything.
        daily_tripped=(float(getattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0) or 0) > 0
                       and m.is_daily_loss_tripped(conn, day)),
        # A SELECTION pause, so a manual buy (enforce_concurrency=False) is exempt, like the re-entry sets.
        drawdown_pause_until=(m.drawdown_pause_until(
                                  conn, day, cfg.MEMECOIN_DRAWDOWN_PAUSE_USD,
                                  cfg.MEMECOIN_DRAWDOWN_PAUSE_S, real_utc_now_iso())
                              if enforce_concurrency else None),
        canary_stop_reason=(canary_stop_reason(conn, network_id) if enforce_concurrency else None),
        gas_reserve_unset=tuple(cfg.evm_gas_natives_unset(network_id)),
        real_enabled=cfg.MEMECOIN_REAL_ENABLED, kill_tripped=bool(kill_tripped),
        trade_size_usd=trade_size,
        base_trade_size_usd=base_trade_size,
        # GLOBAL portfolio cap, its own knob. Per-chain limits must not be a way past it.
        max_concurrent=(cfg.MEMECOIN_MAX_CONCURRENT_TOTAL if enforce_concurrency
                        else automatic_count + 1),
        chain_max_concurrent=(chain_slots if enforce_concurrency else chain_automatic + 1),
        chain_automatic_count=chain_automatic,
        max_total_exposure_usd=cfg.MEMECOIN_MAX_TOTAL_EXPOSURE_USD,
        min_trade_floor_usd=cfg.min_trade_floor_usd_for(network_id),
        gas_entry_native=gas_entry,
        gas_approval_native=gas_approval,
        gas_exit_native=gas_exit,
        gas_retry_factor=cfg.MEMECOIN_GAS_RETRY_FACTOR,
        chain_concurrent=snap["count"] + p_count,
        chain_exposure_usd=snap["notional"] + p_notional,
        network_id=m.normalized_coin_network(network_id, "0x"),
        gas_reserve_min_native=gas_floor,
        native_quote_gas_reserve_usd=gas_reserve,
        native_quote_gas_deficit_usd=gas_deficit,
        native_sizing_equity_usd=native_equity,
        native_spendable=native_spendable,
        # POLICY set, caller-controlled — see build_memecoin_risk_context. A manual leg passes
        # False; the module never learns who asked. `reentry_closes_by` is the caller's
        # cross-wallet re-entry scope (same as the Solana builder).
        cooldown_coin_ids=(m.recently_closed_real_coin_ids(
                               conn, real_utc_now_iso(), cfg.MEMECOIN_REAL_REENTRY_COOLDOWN_S,
                               closes_by=reentry_closes_by, lifted=lifted["cooldown"])
                           if include_reentry_policy else frozenset()),
        losing_coin_ids=(m.losing_real_coin_ids(
                             conn, since_iso=cfg.losing_reentry_since_iso(real_utc_now_iso()),
                             closes_by=reentry_closes_by, lifted=lifted["losing"])
                         if (network_id == "57073" and include_reentry_policy
                             and cfg.MEMECOIN_REAL_BLOCK_LOSING_REENTRY) else frozenset()),
        reentry_lifted=lifted,
        override_risk_caps=bool(override_risk_caps))


def evm_entry_allowed(ctx, *, mint, trade_size):
    """Fail-closed EVM entry gate. First failure wins. Mirrors the Solana gate with a native-gas
    (incl. approval) full-book exit reserve in place of the SOL reserve."""
    if not ctx.real_enabled:
        return _reject("real trading disabled")
    if ctx.kill_tripped:
        return _reject("kill switch tripped")
    if (str(ctx.network_id).strip(), str(mint or "").strip().lower()) in cfg.MEMECOIN_REAL_BLOCKED_TOKENS:
        return _reject("blocked_token: verified liquidity removal")
    if ctx.daily_tripped and not ctx.override_risk_caps:
        return _reject("daily loss limit reached (latched)")
    if ctx.drawdown_pause_until:
        return _reject("drawdown pause: today's realized PnL fell $%.0f below the day's high; "
                       "automatic entries resume %s UTC"
                       % (float(cfg.MEMECOIN_DRAWDOWN_PAUSE_USD), ctx.drawdown_pause_until[11:19]))
    if ctx.gas_reserve_unset:
        # No override: this is not a selection gate. Without the reserve amounts the gate below
        # would judge the gas balance against another chain's numbers.
        return _reject("chain %s gas reserve is not set: %s (absent is not zero; the fork rehearsal "
                       "measures them)" % (ctx.network_id, ", ".join(ctx.gas_reserve_unset)))
    if ctx.canary_stop_reason and not ctx.override_risk_caps:
        return _reject(ctx.canary_stop_reason)
    if float(trade_size) != ctx.trade_size_usd:
        return _reject("trade size must equal approved size (invariant)")
    # DUPLICATE BEFORE ANY SIZE-DERIVED CHECK (2026-09-30, attempt 822b4ac1). A reservation for the
    # same coin by another lane is subtracted from spendable, so this attempt sizes to $0 and the
    # floor check below fired first: "below min-trade floor" for what was a duplicate (25 of 85
    # floor refusals 09-16..09-30). Both refuse; only the reason changes.
    if not ctx.override_risk_caps and m.coin_identity(ctx.network_id, mint) in ctx.held_coin_ids:
        return duplicate_refusal(ctx.duplicate_blockers.get(m.coin_identity(ctx.network_id, mint)))
    # Named separately from the floor check below: on a native_is_quote chain (Arc), a balance
    # under the gas reserve also sizes to $0, and "below min-trade floor" would misreport a
    # gas-reserve shortfall as a plain underfunded clip.
    if ctx.native_quote_gas_deficit_usd > 0:
        return _reject(
            "insufficient quote balance for chain's native-is-quote gas reserve "
            f"(short ${ctx.native_quote_gas_deficit_usd:.6f} of the "
            f"${ctx.native_quote_gas_reserve_usd:.6f} reserve)")
    if trade_size < ctx.min_trade_floor_usd:
        # Say the NUMBERS, as the Solana gate has since 2026-09-03. SPRAG 2aa7475f (4663,
        # 2026-09-22): the wallet held $0.46 of USDG and the alert read as a verdict on the coin.
        # Name the real bankroll asset (WETH on 4663, not USDC) and what in-flight entries hold.
        from sniper.execution.evm_wallet import quote_symbol
        return _reject("below min-trade floor: $%.2f clip < $%.2f floor, %s balance $%.2f, "
                       "$%.2f reserved by in-flight entries (-EV at this clip)"
                       % (float(trade_size), float(ctx.min_trade_floor_usd),
                          (quote_symbol(ctx.network_id) or "quote")
                          + ("+spare native" if ctx.native_spendable else ""),
                          float(ctx.usdc_balance or 0.0),
                          float(ctx.effective_pending_notional_usd or 0.0)))
    if m.coin_identity(ctx.network_id, mint) in ctx.cooldown_coin_ids:
        return _reject("re-entry cooldown: a real position on this coin closed within the window")
    if m.coin_identity(ctx.network_id, mint) in ctx.losing_coin_ids:
        return _reject("losing re-entry: our last real position on this coin lost")
    # PER-CHAIN SLOT CAP — named, so a chain-local refusal is distinguishable from the portfolio
    # one in the ledger without a network_id group-by.
    if (not ctx.override_risk_caps and ctx.chain_max_concurrent > 0
            and ctx.chain_automatic_count >= ctx.chain_max_concurrent):
        return _reject("%s max concurrent positions reached (%d)"
                       % (ctx.network_id or "evm", ctx.chain_max_concurrent))
    # native-gas full-book exit reserve: this entry (swap + approval) PLUS gas to exit ALL
    # (chain_N + 1) positions — each exit = swap + approval — with retry headroom.
    if ctx.native_balance is None:
        return _reject("native balance read failed (rpc) — refusing fail-closed, balance unknown")
    required_native = required_native_gas(ctx)
    if ctx.native_balance < required_native:
        return _reject("insufficient native gas for entry + approval + full-book exit reserve "
                       f"(have={ctx.native_balance:.6f}, need={required_native:.6f}, "
                       f"concurrent={ctx.chain_concurrent}, pending={ctx.pending_count})")
    # OVERRIDE feeds the evaluator permissive CAPS, never a fabricated bankroll/exposure READING --
    # see memecoin_risk.py's Solana twin for why.
    exposure_cap = (1e12 if ctx.override_risk_caps
                    else ctx.max_total_exposure_usd if ctx.max_total_exposure_usd > 0 else 1e12)
    rc = RiskContext(
        bankroll_usdc=ctx.usdc_balance - ctx.effective_pending_notional_usd,
        total_exposure_usdc=ctx.effective_exposure_usd,
        market_exposure_usdc=ctx.effective_exposure_usd,
        concurrent_positions=ctx.effective_concurrent,
        daily_realized_pnl_usdc=0.0,
        max_total_exposure_usdc=exposure_cap,
        max_market_exposure_usdc=exposure_cap,
        max_concurrent_positions=(1_000_000 if ctx.override_risk_caps else ctx.max_concurrent),
        daily_loss_limit_usdc=1e12,                       # inert; the latch owns daily
        paper_only=not ctx.real_enabled, real_trading_enabled=ctx.real_enabled,
        venue="evm_memecoin")
    decision = evaluate_entry_risk(rc, float(trade_size), condition_id=mint, execution_mode="real")
    if decision.allowed and decision.clamped:
        # clamped approval would break reservation==spend; reject, next entry re-sizes cleanly.
        return _reject("size clamped by risk policy (reservation!=spend)")
    return decision


def _early_clip_snap(ctx, decision, mint, early_clip_usd, facts):
    """(ctx, decision), with the trade size lowered to the early quote's clip when it sits at most
    MEMECOIN_EVM_EARLY_CLIP_SNAP_PCT below the fresh one and the gate admits it; else unchanged.

    The early clip is taken VERBATIM (take() matches it by float equality). Down only, so the
    snapped size is under every cap and approved <= base x risk_scale still holds; the gate runs
    again on it (the min-trade floor), and a refusal there keeps the fresh decision, so the snap
    never refuses an entry. Pure: no I/O inside the reservation's write lock."""
    if early_clip_usd is None:
        return ctx, decision
    fresh, pct, reason = ctx.trade_size_usd, cfg.MEMECOIN_EVM_EARLY_CLIP_SNAP_PCT, None
    valid = (isinstance(early_clip_usd, (int, float)) and not isinstance(early_clip_usd, bool)
             and math.isfinite(early_clip_usd) and early_clip_usd > 0)
    if not valid:
        reason = "early_clip_invalid"
    elif pct <= 0:
        reason = "off"
    elif early_clip_usd == fresh:
        reason = "equal"
    elif early_clip_usd > fresh:
        reason = "early_above_fresh"     # an up-snap would break approved <= base x risk_scale
    elif fresh - early_clip_usd > fresh * pct / 100.0 + 1e-9:
        reason = "outside_tolerance"
    else:
        snapped_ctx = replace(ctx, trade_size_usd=early_clip_usd)
        snapped = evm_entry_allowed(snapped_ctx, mint=mint, trade_size=early_clip_usd)
        if snapped.allowed:
            ctx, decision = snapped_ctx, snapped
        else:
            reason = "gate: %s" % snapped.reason
    if facts is not None:
        facts.update(early_clip_usd=early_clip_usd if valid else None, fresh_clip_usd=fresh,
                     approved_clip_usd=ctx.trade_size_usd, snapped=reason is None, reason=reason,
                     snap_pct=pct)
    return ctx, decision


def _trial_capped(size_cap_usd, trial):
    """The caller's cap, lowered to the trial clip when this is a trial entry."""
    clip = (trial or {}).get("clip_usd")
    if not clip:
        return size_cap_usd
    return float(clip) if size_cap_usd is None else min(float(size_cap_usd), float(clip))


def reserve_entry_evm(conn, *, mint, source_user_id, strategy, network_id, day, usdc_balance,
                      native_balance, now_iso, kill_tripped=False, origin_event_id=None,
                      attempt_id=None, signal_source=None, source_price=None,
                      source_market_cap=None, observed_at=None, clock_domain=None,
                      classification=None, include_reentry_policy=True,
                      enforce_concurrency=True, size_override=None, funded_policy=None,
                      size_cap_usd=None, override_risk_caps=False, trial=None,
                      native_quote_price_usd=None, reentry_closes_by=None, early_clip_usd=None,
                      snap_facts=None, desk=None):
    """Atomic armed EVM entry gate: latch the GLOBAL daily breaker, evaluate this chain's envelope on
    effective values, and — only if allowed — reserve the coin (sig-less, tagged with network_id)
    in one commit. A lost coin-dedup race returns (reject, None).
    `early_clip_usd`: the early EVM quote's clip, for the down-only snap (`_early_clip_snap`);
    `snap_facts`, a dict the snap fills with what it decided, for the attempt trace."""
    network = m.normalized_coin_network(network_id, mint)
    if network == "evm-unknown":
        return _reject("unresolved EVM network: refusing reservation"), None
    # A CALLER CAP THAT IS NOT A FINITE POSITIVE NUMBER REFUSES (Alpha 2026-10-07), the same check
    # reserve_entry runs on Solana. entry_cap_usd refuses it only while wallet caps are on; off,
    # build_evm_risk_context skipped a NaN or <= 0 cap and the entry was uncapped.
    if size_cap_usd is not None:
        try:
            value = float(size_cap_usd)
            if not math.isfinite(value) or value <= 0:
                return _reject("entry size cap unavailable"), None
        except (ValueError, TypeError, OverflowError):
            return _reject("entry size cap unavailable"), None
    from sniper.services.memecoin_wallet_caps import entry_cap_usd
    try:
        size_cap_usd = entry_cap_usd(source_user_id, network, existing_cap=size_cap_usd)
    except (ValueError, TypeError, OverflowError):
        return _reject("entry size cap unavailable"), None
    conn.execute("BEGIN IMMEDIATE")
    try:
        m.check_and_latch_daily_loss(
            conn, day, cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD, commit=False)
        # TRIAL FIRST (trial spec 5.2): the cap below comes only from a marker validated here.
        refusal, trial = trial_refusal(
            conn, source_user_id=source_user_id, network_id=network, trial=trial,
            classification=classification, enforce=enforce_concurrency,
            override_risk_caps=override_risk_caps)
        if refusal:
            conn.commit()
            return _reject(refusal), None
        try:
            if (classification is not None
                    and classification.classification_version
                    != cfg.MEMECOIN_FUNDED_CLASSIFICATION_VERSION):
                raise ValueError("unsupported classification version")
            risk_scale = classification.risk_scale if classification is not None else 1.0
            ctx = build_evm_risk_context(
                conn, usdc_balance=usdc_balance, native_balance=native_balance,
                day=day, kill_tripped=kill_tripped, network_id=network, risk_scale=risk_scale,
                include_reentry_policy=include_reentry_policy,
                enforce_concurrency=enforce_concurrency, size_override=size_override,
                size_cap_usd=_trial_capped(size_cap_usd, trial),
                override_risk_caps=override_risk_caps,
                native_quote_price_usd=native_quote_price_usd,
                reentry_closes_by=reentry_closes_by)
        except (AttributeError, ValueError) as exc:
            conn.rollback()
            m.check_and_latch_daily_loss(
                conn, day, cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD, commit=True)
            return _reject(f"invalid_funded_contract: {exc}"), None
        decision = evm_entry_allowed(ctx, mint=mint, trade_size=ctx.trade_size_usd)
        if not decision.allowed:
            conn.commit()
            return decision, None
        ctx, decision = _early_clip_snap(ctx, decision, mint, early_clip_usd, snap_facts)
        # NOT tied to include_reentry_policy (Lambda F2, 2026-09-28): a cooldown-exempt wallet
        # passes False there, and classification has already scaled its clip, so skipping this
        # would let an exhausted or malformed ladder re-enter it at full size. Manual entries are
        # excluded inside winner_reentry_step; an operator override skips it like every cap.
        ladder_refusal = (None if override_risk_caps else
                          winner_reentry_refusal(conn, classification, network_id=network,
                                                 mint=mint, signal_source=signal_source))
        if ladder_refusal:
            conn.commit()
            return _reject(ladder_refusal), None
        contract_fields = {}
        if classification is not None:
            try:
                contract_fields = prepare_funded_contract(
                    conn,
                    classification=classification,
                    network_id=network,
                    mint=mint,
                    attempt_id=attempt_id,
                    origin_event_id=origin_event_id,
                    base_clip_usd=ctx.base_trade_size_usd,
                    approved_clip_usd=ctx.trade_size_usd,
                    source_user_id=source_user_id,
                    funded_policy=funded_policy,
                    trial=trial,
                    desk=desk,
                )
            except ValueError as exc:
                conn.rollback()
                # Same lane-for-lane treatment as Solana: keep the refused classification so the
                # floor is auditable on EVERY funded chain, not just the one that was debugged.
                persist_refused_classification(conn, attempt_id, exc)
                m.check_and_latch_daily_loss(
                    conn, day, cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD, commit=True)
                return _reject(f"invalid_funded_contract: {exc}"), None
        # Same lane-for-lane treatment as Solana: read liquidity/age off the classifier's own
        # facts (already computed by classify_funded_entry, no extra provider call). Absent
        # classification/facts -> NULL, never 0.
        facts = classification.facts if classification is not None else {}
        too_young = coin_age_refusal(classification, network, source_user_id)
        if too_young:                       # refuse BEFORE reserving, so no capital is held
            conn.rollback()
            return _reject(too_young), None
        pid = m.reserve_pending(
            conn, mint=mint, source_user_id=source_user_id, strategy=strategy,
            notional=ctx.trade_size_usd, reserved_at=now_iso, network_id=network,
            origin_event_id=origin_event_id, attempt_id=attempt_id,
            signal_source=signal_source, source_price=source_price,
            source_market_cap=source_market_cap,
            source_liquidity=facts.get("liquidity"), source_token_age_s=facts.get("pair_age_s"),
            observed_at=observed_at,
            clock_domain=clock_domain, commit=False, **contract_fields)
        # Read inside the reservation transaction, before its commit (see memecoin_risk).
        lost = (m.lost_reservation_reason(conn, network_id=network, mint=mint,
                                          signal_source=signal_source, at_iso=now_iso,
                                          funded_contract_json=contract_fields.get("funded_contract_json"))
                if pid is None else None)
        conn.commit()
        if pid is None:
            return _reject(lost), None
        return with_reentry_lift(decision, ctx, network, mint), pid
    except Exception:
        conn.rollback()
        raise
