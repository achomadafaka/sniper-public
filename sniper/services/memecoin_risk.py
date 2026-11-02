"""Memecoin real-money risk envelope (S1). Pure check over an injected context + a context
builder that folds confirmed-real and pending reservations into effective values. Delegates the
generic bankroll/concurrency/exposure checks to sniper.risk.policy.evaluate_entry_risk."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import logging
import math
import time

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from sniper.risk.models import RiskContext, RiskDecision
from sniper.risk.policy import evaluate_entry_risk
from sniper.util.clock import real_utc_now_iso

logger = logging.getLogger(__name__)


# Rent-exempt minimum for one SPL token account (Solana protocol constant, 165-byte account).
# The entry swap creates the mint ATA, the WSOL wrap account, and on a multi-hop route an
# intermediate ATA — 3 in the 2026-08-25 trace. Rent is a DEPOSIT, not a fee (reclaim it with
# `scripts/memecoin_reclaim_ata_rent.py`), but the transaction cannot land unless the wallet holds
# it up front.
ATA_RENT_SOL = 0.00203928
ENTRY_ATA_COUNT = 3          # ponytail: worst case seen; a longer route needs more, and refusing
                             # early is the safe direction.


def required_entry_sol(open_chain_positions, gas_entry_sol, gas_exit_sol, gas_retry_factor):
    """SOL an entry must have on hand: entry fee + new-account rent + a full-book exit reserve
    with retry.

    Rent was absent from this number until 2026-08-25. The gate passed at 0.00446 SOL, the swap
    reached instruction 4 and died on `Transfer: insufficient lamports 312142, need 2039280`
    (sig 5G5w4Ynt), and the fee was burned. The GAS-LOW alert reads the same number, so the
    funded lane stayed silent while every entry failed."""
    return (gas_entry_sol
            + ENTRY_ATA_COUNT * ATA_RENT_SOL
            + (open_chain_positions + 1) * gas_exit_sol * gas_retry_factor)


def _reject(reason):
    return RiskDecision(allowed=False, approved_size_usdc=0.0, reason=reason, clamped=False)


def duplicate_refusal(blocker):
    detail = "already held or reserved"
    if blocker:
        detail = ("open funded position" if blocker["kind"] == "position" else
                  "another entry attempt was in progress at decision time")
    return RiskDecision(False, 0.0, f"one position per coin: {detail}", False, blocker)


def risk_refusal_result(decision):
    result = {"status": "rejected", "reason": decision.reason}
    if decision.duplicate_blocker:
        result["duplicate_blocker"] = decision.duplicate_blocker
        result["gate_inputs_json"] = json.dumps({"duplicate_blocker": decision.duplicate_blocker})
    return result


def coin_age_refusal(classification, network_id, source_user_id=None):
    """Reason string when the coin is younger than this chain's floor, or None to admit.

    The age is `facts["pair_age_s"]`, which `classify_funded_entry` has already computed, so this
    costs no provider call. See `min_coin_age_s_for_chain` for the measurement behind the floor.

    AN UNKNOWN AGE ADMITS, deliberately. The reading is missing on about 9% of funded entries and
    those carry a POSITIVE median, so refusing on a missing number would cost more than the floor
    saves. This is a profitability filter and not a safety gate: safety gates fail closed, and
    treating a missing reading as a young coin would be exactly the "a number that is absent is not
    a number that is bad" error.
    """
    floor = cfg.min_coin_age_s_for(network_id, source_user_id)    # per-wallet override first
    if floor <= 0 or classification is None:
        return None
    facts = getattr(classification, "facts", None) or {}
    age = facts.get("pair_age_s")
    if not isinstance(age, (int, float)) or isinstance(age, bool):
        return None
    if float(age) >= floor:
        return None
    # THE MINT'S OWN EVIDENCE, FROM THE CACHE ONLY (2026-09-28, Alpha GO). The pair age is the
    # oldest LISTED pair, which for a graduated launchpad coin is the new pool (2e32783b: 54 s
    # against a 33,193 s old mint). The entry hook warms this before the reservation; here it is a
    # dict read, because this runs inside the reservation transaction. It can only raise the age.
    from sniper.services.memecoin_coin_age import cached_age_s
    mint_age = cached_age_s(network_id, facts.get("mint"))
    if mint_age is not None and mint_age >= floor:
        return None
    return "coin_too_young: %.0fs old, floor %.0fs" % (float(age), floor)


def coin_age_presign_check(network_id, mint, pair_age_s=None, source_user_id=None):
    """(reason or None, trace fields or None) just BEFORE SIGNING, for a coin whose PAIR age was
    unknown (operator yes 2026-09-28; spec 2026-09-28-memecoin-unknown-pair-age-mint-evidence-design.md).

    The 120 s floor admitted an unknown pair age, and DexScreener has not indexed the newest coins, so
    the rule missed exactly the coins it targets: 21 of 22 funded fills under 120 s came in that way
    (-$267.44, 3 of 7 fast crashes; docs/audits/2026-09-28-fast-crash-stops.md). The hook started the
    mint's first-tx lookup at classification WITHOUT waiting; this reads its cache only, no I/O.

    A known pair age (0 included) returns (None, None): the reservation gate already decided. The
    lookup not back, failed or rate-limited: None with resolved=False, admitted as today."""
    floor = cfg.min_coin_age_s_for(network_id, source_user_id)
    if floor <= 0 or (isinstance(pair_age_s, (int, float)) and not isinstance(pair_age_s, bool)):
        return None, None
    from sniper.services.memecoin_coin_age import cached_age_s, LowerBoundEvidence
    mint_age = cached_age_s(network_id, mint)
    fields = {"floor_s": floor, "mint_age_s": mint_age, "resolved": mint_age is not None}
    if isinstance(mint_age, LowerBoundEvidence):
        fields['mint_age_quality'] = 'lower_bound'
        fields['mint_age_lower_bound_s'] = float(mint_age)
        if mint_age < floor:
            # Recent signatures cannot establish that no earlier mint transaction exists.
            fields.update(mint_age_s=None, resolved=False)
            return None, fields
    if mint_age is None or mint_age >= floor:
        return None, fields
    return ("coin_too_young: mint %.0fs old (pair age unknown), floor %.0fs" % (mint_age, floor),
            fields)


class BandRefused(ValueError):
    """The coin band may not open a funded position (the mcap floor / ceiling).

    Carries the resolved classification because the callers ROLL BACK on ValueError, which would
    otherwise discard the very evidence the refusal is about. A floor-refused attempt must keep its
    effective_mcap, or the gate is unauditable exactly where it fires: on 2026-07-27 the first
    production refusal wrote terminal_reason='micro' with classification_json NULL, so nothing
    recorded WHICH mcap produced that band. Callers persist this after their rollback."""

    def __init__(self, coin_band, *, classification_json, classification_hash,
                 classification_version):
        super().__init__(f"coin band is not reservable: {coin_band!r}")
        self.classification_json = classification_json
        self.classification_hash = classification_hash
        self.classification_version = classification_version


def persist_refused_classification(conn, attempt_id, exc):
    """Best-effort re-persist of a BandRefused classification AFTER the caller's rollback.
    Never raises: losing the audit row must not turn a clean reject into an exception."""
    if not isinstance(exc, BandRefused) or not attempt_id:
        return False
    try:
        m.set_entry_attempt_classification(
            conn, attempt_id,
            classification_json=exc.classification_json,
            classification_hash=exc.classification_hash,
            classification_version=exc.classification_version,
            commit=True)
        return True
    except Exception:
        logger.exception("could not persist refused classification for attempt %s", attempt_id)
        return False


def real_trade_size(*, usdc_balance, open_notional_usd, max_concurrent=None, max_clip_usd=None,
                    bankroll_fraction=None, mode=None):
    """The clip for the NEXT real entry. 'fixed' -> MEMECOIN_TRADE_SIZE_USD (original invariant).
    'bankroll' -> max(MEMECOIN_MIN_TRADE_USD, equity / MEMECOIN_MAX_CONCURRENT).
    'fraction' -> max(MEMECOIN_MIN_TRADE_USD, equity * bankroll_fraction) — the operator's
    %-of-bankroll clip, MEMECOIN_BANKROLL_FRACTION unless the caller passes its chain's own
    override. `mode` defaults to MEMECOIN_REAL_SIZING unless the caller passes its chain's own
    override (a chain can run 'fraction' while the global default stays 'bankroll'). equity =
    free USDC + cost basis already deployed (the caller passes VENUE-scoped values) — so profits
    raise the clip. The adaptive clip never exceeds free USDC. Floored to the cent."""
    mode = cfg.MEMECOIN_REAL_SIZING if mode is None else mode
    if mode not in ("bankroll", "fraction"):
        return cfg.MEMECOIN_TRADE_SIZE_USD
    equity = float(usdc_balance) + float(open_notional_usd)
    if mode == "fraction":
        frac = cfg.MEMECOIN_BANKROLL_FRACTION if bankroll_fraction is None else bankroll_fraction
        clip = equity * frac
    else:
        # PER-CHAIN DIVISOR (2026-08-30). `max_concurrent=None` keeps the global value, so every
        # existing caller is byte-identical. A caller that knows its chain passes that chain's
        # limit, which is what lets solana keep divisor 1 (clip = FULL equity) while 4663 runs 2.
        _slots = cfg.MEMECOIN_MAX_CONCURRENT if max_concurrent is None else max_concurrent
        clip = equity / max(1, int(_slots))
    # FLOOR to the cent, never round — the same rule scaled_funded_clip below already applies,
    # for the same reason. At MEMECOIN_MAX_CONCURRENT=1 the bankroll clip IS the whole balance,
    # so this value and evaluate_entry_risk's `bankroll_usdc` read the same number. `round()`
    # lifts a half cent UP, the clip lands one cent above the balance, and the gate refuses with
    # "insufficient bankroll". The refusal repeats for every signal until the balance moves: two
    # funded signals died there on 2026-08-23 with no pending reservation and no open position.
    # The 1e-9 nudge absorbs float noise (x.xx * 100 landing at ...99999) so a clean division
    # keeps its exact cent.
    # PER-ENTRY CEILING, APPLIED HERE SO EVERY CALLER GETS IT (2026-09-04, operator).
    # It first lived only in the EVM entry gate, and the operator's dashboard -- which sizes the
    # "next clip" with its own call to this function -- kept showing the uncapped number ($215
    # against a $150 cap). Two call sites computing the same quantity will diverge; one function
    # that takes the ceiling cannot.
    if max_clip_usd is not None and float(max_clip_usd) > 0:
        clip = min(clip, float(max_clip_usd))
    requested = math.floor(max(cfg.MEMECOIN_MIN_TRADE_USD, clip) * 100 + 1e-9) / 100
    spendable = math.floor(max(0.0, float(usdc_balance)) * 100 + 1e-9) / 100
    return min(requested, spendable)


def manual_percent_size(*, quote_balance, open_notional_usd, pending_notional_usd,
                        global_exposure_usd, max_total_exposure_usd, pct):
    """Resolve one manual percent against selected-chain sizing equity.

    The quote balance still contains reserved cash. Subtract it before the equity sum, then add
    the reserved cost basis. This counts each dollar once. The result cannot exceed spendable
    quote cash or configured global exposure headroom.
    """
    values = {
        "quote_balance": quote_balance,
        "open_notional_usd": open_notional_usd,
        "pending_notional_usd": pending_notional_usd,
        "global_exposure_usd": global_exposure_usd,
        "max_total_exposure_usd": max_total_exposure_usd,
    }
    for name, value in values.items():
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or float(value) < 0):
            raise ValueError(f"{name} must be finite and nonnegative")
    if (isinstance(pct, bool) or not isinstance(pct, (int, float))
            or not math.isfinite(float(pct)) or not 0 < float(pct) <= 1):
        raise ValueError("pct must be finite and in (0,1]")

    pending = float(pending_notional_usd)
    spendable = max(0.0, float(quote_balance) - pending)
    equity = spendable + float(open_notional_usd) + pending
    requested = equity * float(pct)
    cap = float(max_total_exposure_usd)
    exposure_headroom = (max(0.0, cap - float(global_exposure_usd))
                         if cap > 0 else requested)
    return math.floor(min(requested, spendable, exposure_headroom) * 100 + 1e-9) / 100


def solana_clip_ceiling_usd(classification, network_id=None, source_user_id=None):
    """MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO for a Solana entry that qualifies, else None (today's
    ceilings). The network is the classification's, else `network_id`; anything but Solana gets
    None. At MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD 0 every Solana entry qualifies, with or without
    a classification. Above 0 the classification's resolved effective_mcap must be at or above it:
    at the default 50000 that is band micro, small or mid, or out_of_scope resolved (>= $2M).
    Absent is not above: nano, assumed_nano and an unresolved mcap get None. The caller passes the
    result to the builder (`clip_ceiling_usd`) and to entry_cap_usd (`ceiling_usd`); a caller that
    passes nothing sizes as today. WALLETS (operator 2026-10-08 ~14:20Z): while
    MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS is set, only a wallet it names qualifies; no wallet id
    then means no raise (never wider)."""
    from sniper.services.memecoin_wallet_caps import raised_clip_wallet_allowed
    knob = solana_above_nano_ceiling_usd()
    network = (getattr(classification, "network_id", None) if classification is not None
               else network_id)
    if knob is None or network != "solana" or not raised_clip_wallet_allowed(source_user_id):
        return None
    threshold = solana_raised_clip_min_mcap_usd()
    if threshold <= 0:
        return knob
    mcap = (getattr(classification, "facts", None) or {}).get("effective_mcap")
    if (isinstance(mcap, (int, float)) and not isinstance(mcap, bool) and math.isfinite(mcap)
            and mcap > 0 and mcap >= threshold):
        return knob
    return None


def solana_flat_clip_ceiling_usd():
    """The raised ceiling when it applies to EVERY Solana entry (threshold 0 and no wallet list), else
    None. For the callers that size before the coin or the wallet is known: the slot lane's prefetched
    leg (leg_plan), its USDC reservation and wallet cap, the dashboard. With a wallet list set the leg
    stays at today's clip and a listed wallet is raised on the SOL try only."""
    from sniper.services.memecoin_wallet_caps import raised_clip_list_set
    knob = solana_above_nano_ceiling_usd()
    return (knob if knob is not None and solana_raised_clip_min_mcap_usd() <= 0
            and not raised_clip_list_set() else None)


def solana_raised_clip_min_mcap_usd():
    """MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD; a negative or non-finite value reads as 50000."""
    value = getattr(cfg, "MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD", 50_000.0)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        return 50_000.0
    return float(value)


def solana_above_nano_ceiling_usd():
    """The configured above-nano ceiling, or None when the knob is 0 or not a finite positive
    number. For a caller that sizes before the band is known (the early quote's alternate)."""
    knob = getattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO", 0.0)
    if (isinstance(knob, bool) or not isinstance(knob, (int, float))
            or not math.isfinite(knob) or knob <= 0):
        return None
    return float(knob)


def scaled_funded_clip(*, base_clip_usd, risk_scale, floor_usd):
    for name, value in (("base_clip_usd", base_clip_usd), ("floor_usd", floor_usd)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if (isinstance(risk_scale, bool) or not isinstance(risk_scale, (int, float))
            or not math.isfinite(float(risk_scale)) or not 0 < float(risk_scale) <= 1):
        raise ValueError("risk_scale must be finite and in (0,1]")
    base = float(base_clip_usd)
    scaled = min(base, max(float(floor_usd), base * float(risk_scale)))
    # FLOOR to the cent, never round. prepare_funded_contract rejects an approved clip above
    # `max(base * scale, MIN_TRADE_FLOOR_USD)` with only 1e-9 of tolerance, so rounding UP put the
    # clip over its own risk ceiling and the contract refused the entry outright. That turned a
    # regime scale-down into a veto, which the risk-only regime rule forbids. It stayed hidden
    # while the clip was a flat $50 (50 * 0.75 = 37.50 needs no rounding) and appeared the moment
    # bankroll sizing produced a clip whose scaled value did not land on a cent (36.93 -> 27.6975).
    # The 1e-9 nudge absorbs float noise (x.xx * 100 landing at ...99999) without ever admitting a
    # real excess, since the validator itself allows the same 1e-9.
    return math.floor(scaled * 100 + 1e-9) / 100


@dataclass(frozen=True)
class MemecoinRiskCtx:
    usdc_balance: float
    sol_balance: float | None
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
    gas_entry_sol: float
    gas_exit_sol: float
    gas_retry_factor: float
    base_trade_size_usd: float | None = None
    # Per-chain slot cap and the per-chain AUTOMATIC count it is compared against. Defaults of 0
    # mean "no per-chain limit configured", so an unbuilt/legacy context behaves exactly as before.
    chain_max_concurrent: int = 0
    chain_automatic_count: int = 0
    chain_concurrent: int = 0
    chain_exposure_usd: float = 0.0
    chain_pending_count: int = 0
    cooldown_coin_ids: frozenset = frozenset()   # real closes inside the re-entry cooldown window
    losing_coin_ids: frozenset = frozenset()     # coins whose most recent real close LOST
    # {family: {coin: close}}: the closes the caller's cross-wallet scope lifted (evidence only).
    reentry_lifted: dict | None = None
    drawdown_pause_until: str | None = None      # ISO end of an active drawdown pause, else None
    # OPERATOR OVERRIDE (2026-09-22, operator order). Set ONLY by a leg resolving ev.override on a
    # manual entry -- see ALLOWED_POLICY_PARAMS in tests/test_memecoin_manual_gate_bypass.py. Skips
    # the daily-loss latch, one-per-coin, the chain/portfolio slot caps, and the exposure-cap
    # evaluation in memecoin_entry_allowed below. Does NOT touch real_enabled, kill_tripped, the
    # trade-size invariant, the min-trade-floor (already a manual-exempt SELECTION gate), or the
    # SOL gas/balance reads -- those stay unconditional for every entry, override or not.
    override_risk_caps: bool = False
    duplicate_blockers: dict = field(default_factory=dict)
    effective_pending_usdc: float | None = None
    pending_sol_lamports: int = 0
    funding_asset: str = "usdc"
    funding_amount_atomic: int | None = None
    funding_overhead_atomic: int = 0
    funding_sol_usd: float | None = None
    funding_available_usd: float | None = None


def snapshot_funding_values(evidence):
    """Legacy IDs or coordinator funding evidence: (IDs, unreserved SOL fee/rent/tips)."""
    if isinstance(evidence, dict):
        if set(evidence) != {"pending_ids", "extra_sol_atomic"}:
            raise ValueError("invalid snapshot funding evidence")
        extra = evidence["extra_sol_atomic"]
        if type(extra) is not int or extra < 0:
            raise ValueError("invalid snapshot SOL overhead")
        return evidence["pending_ids"], extra
    return evidence, 0


def entry_sol_spend_budget(ctx):
    """Current-message SOL allowance from the already serialized cash/book snapshot.

    The message itself must fit principal, exact fee/tip and the existing rent bound in this
    allowance. Do not subtract the estimated entry gas again or reserve a global maximum bid.
    """
    from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
    atomic = Decimal(1_000_000_000)
    balance = int((Decimal(str(ctx.sol_balance)) * atomic).to_integral_value(rounding=ROUND_FLOOR))
    exits = int((Decimal(ctx.chain_concurrent + 1) * Decimal(str(ctx.gas_exit_sol))
                 * Decimal(str(ctx.gas_retry_factor)) * atomic).to_integral_value(rounding=ROUND_CEILING))
    return balance - ctx.pending_sol_lamports - exits


def build_memecoin_risk_context(
        conn, *, usdc_balance, sol_balance, day, kill_tripped=False, risk_scale=1.0,
        size_override=None, include_reentry_policy=True, include_losing_reentry=True,
        include_reentry_cooldown=True,
        enforce_concurrency=True, enforce_min_floor=True, override_risk_caps=False,
        funding_asset="usdc", funding_sol_usd=None, sol_buffer_cap_usd=0.0,
        funding_overhead_atomic=0, size_cap_usd=None, reentry_closes_by=None,
        clip_ceiling_usd=None, snapshot_debits=(), copy_slots=True):
    """`copy_slots=False` (a desk entry): the automatic copy slot caps do not gate it; the desk has its
    own cap inside the reservation (_DESK_CAP_SQL). Exposure, floor and every other gate still bind."""
    if funding_asset not in ("usdc", "sol"):
        raise ValueError("unsupported entry funding asset")
    if funding_asset == "sol" and (funding_sol_usd is None or
                                   not math.isfinite(float(funding_sol_usd)) or
                                   float(funding_sol_usd) <= 0 or
                                   not math.isfinite(float(sol_buffer_cap_usd)) or
                                   float(sol_buffer_cap_usd) <= 0 or
                                   not isinstance(funding_overhead_atomic, int) or
                                   funding_overhead_atomic < 0):
        raise ValueError("SOL funding needs a current price and a positive buffer cap")
    # Portfolio slots/exposure are global. Sizing, USDC encumbrance, and SOL exit gas remain scoped
    # to Solana so EVM capital cannot inflate the clip or the SOL reserve.
    snap = m.real_open_snapshot(conn, network="solana")
    p_count, p_notional = m.pending_totals(conn, network="solana")
    snapshot_debits, extra_sol = snapshot_funding_values(snapshot_debits)
    p_usdc, p_sol = m.pending_funding_totals(conn, "solana", snapshot_debits=snapshot_debits)
    p_sol += extra_sol
    global_book = m.funded_book_cost_snapshot(conn)
    automatic_count = m.automatic_funded_count(conn)
    # MARK, NOT COST (2026-09-06) — see `real_open_snapshot` and the EVM twin. The exposure cap
    # below is deliberately still at cost basis.
    sol_trade_usd = 0.0
    if funding_asset == "sol" and sol_balance is not None:
        gas_floor = required_entry_sol(snap["count"] + p_count, cfg.MEMECOIN_GAS_ENTRY_SOL,
                                       cfg.MEMECOIN_GAS_EXIT_SOL, cfg.MEMECOIN_GAS_RETRY_FACTOR)
        sol_trade_usd = min(float(sol_buffer_cap_usd), max(
            0.0, float(sol_balance) - (p_sol + funding_overhead_atomic) / 1e9
            - gas_floor) * float(funding_sol_usd))
    free_cash = (None if usdc_balance is None else
                 max(0.0, float(usdc_balance) - p_usdc) + sol_trade_usd)
    sizing_equity = (None if free_cash is None else
                     free_cash + snap["mark_notional"] + p_notional)
    chain_slots = cfg.max_concurrent_for("solana", equity=sizing_equity)
    chain_automatic = m.automatic_funded_count(conn, network="solana")
    # A manual entry names its own clip; every other path takes the configured sizing mode. The
    # The override feeds the gate input. Bankroll and exposure still judge an oversized request.
    # Unknown balance -> no sizing. The gate refuses with an honest reason before size matters;
    # this guard only keeps the equity arithmetic from crashing on None.
    # ABOVE NANO (2026-10-08): `clip_ceiling_usd` (solana_clip_ceiling_usd) raises the Solana
    # ceiling and the global hard cap below; it never lowers either and never enables a disabled one.
    solana_max = float(getattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD", 0.0) or 0.0)
    if clip_ceiling_usd and solana_max > 0:
        solana_max = max(solana_max, float(clip_ceiling_usd))
    base_trade_size = (float(size_override) if size_override
                       else 0.0 if free_cash is None
                       else real_trade_size(
        usdc_balance=free_cash,
        open_notional_usd=snap["mark_notional"] + p_notional,
        max_concurrent=chain_slots,
        # PER-ENTRY CEILING, Solana (2026-09-08). The EVM gate has passed one since 2026-09-04;
        # this side passed nothing, so bankroll sizing here had a floor and no ceiling.
        max_clip_usd=solana_max))
    trade_size = scaled_funded_clip(
        base_clip_usd=base_trade_size,
        risk_scale=risk_scale,
        floor_usd=cfg.MEMECOIN_MIN_TRADE_FLOOR_USD,
    )
    # A CALLER'S HARD CAP (the trial clip, 2026-10-01; the EVM twin's Pons cap). Last and only
    # downward, before the SOL funding amount is derived from the clip.
    if size_cap_usd is not None and float(size_cap_usd) > 0:
        trade_size = min(trade_size, float(size_cap_usd))
    from sniper.services.memecoin_wallet_caps import global_cap_usd
    hard_cap = global_cap_usd()
    if hard_cap is not None and clip_ceiling_usd:
        hard_cap = max(hard_cap, float(clip_ceiling_usd))
    if hard_cap is not None:
        trade_size = min(trade_size, hard_cap)
    funding_lamports = (int(math.floor(trade_size / float(funding_sol_usd) * 1e9))
                        if funding_asset == "sol" and trade_size > 0 else None)
    blockers = {}
    held = frozenset(m.real_open_coin_ids(conn, blockers=blockers)
                     | m.pending_open_coin_ids(conn, blockers=blockers))
    lifted = {"cooldown": {}, "losing": {}}       # filled by the two re-entry reads below
    return MemecoinRiskCtx(
        # None preserved on BOTH balances (2026-08-26): float(None) here either crashed or, one
        # layer down, a collapsed-to-zero read refused as "insufficient bankroll" — a confident
        # wrong reason. Unknown is not zero, and unknown is not low.
        usdc_balance=(float(usdc_balance) if usdc_balance is not None else None),
        sol_balance=(float(sol_balance) if sol_balance is not None else None),
        effective_concurrent=automatic_count,
        effective_exposure_usd=global_book["notional"],
        effective_pending_notional_usd=p_notional,
        effective_pending_usdc=p_usdc,
        pending_sol_lamports=p_sol,
        funding_asset=funding_asset,
        funding_amount_atomic=funding_lamports,
        funding_overhead_atomic=(funding_overhead_atomic if funding_asset == "sol" else 0),
        funding_sol_usd=(float(funding_sol_usd) if funding_asset == "sol" else None),
        funding_available_usd=(sol_trade_usd if funding_asset == "sol" else None),
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
        real_enabled=cfg.MEMECOIN_REAL_ENABLED, kill_tripped=bool(kill_tripped),
        trade_size_usd=trade_size,
        base_trade_size_usd=base_trade_size,
        # The GLOBAL slot cap. Per-chain limits must never become a way past the portfolio
        # total, so this reads its own knob (which falls back to the global value when unset).
        max_concurrent=(cfg.MEMECOIN_MAX_CONCURRENT_TOTAL if enforce_concurrency and copy_slots
                        else automatic_count + 1),
        chain_max_concurrent=(chain_slots if enforce_concurrency and copy_slots else chain_automatic + 1),
        chain_automatic_count=chain_automatic,
        max_total_exposure_usd=cfg.MEMECOIN_MAX_TOTAL_EXPOSURE_USD,
        # A SELECTION gate by the operator's 2026-08-27 doctrine: it judges whether the clip is
        # worth taking, not whether the capital is safe. A $4.18 buy risks $4.18. Its sibling
        # "route capacity is below the trade floor" was already manual-exempt; this one escaped
        # only because it lives here rather than in real_entry_hook, where the bypass test can see
        # it. Live 2026-09-03: two manual buys refused at 13% of equity = $4.18 against a $5 floor.
        min_trade_floor_usd=(cfg.MEMECOIN_MIN_TRADE_FLOOR_USD if enforce_min_floor else 0.0),
        gas_entry_sol=cfg.MEMECOIN_GAS_ENTRY_SOL, gas_exit_sol=cfg.MEMECOIN_GAS_EXIT_SOL,
        gas_retry_factor=cfg.MEMECOIN_GAS_RETRY_FACTOR,
        chain_concurrent=snap["count"] + p_count,
        chain_exposure_usd=snap["notional"] + p_notional,
        chain_pending_count=p_count,
        # The two re-entry sets are POLICY, not safety (their gate comments say so: the 07-09
        # re-entry study and the losing-re-entry operator intent). The CALLER decides whether the
        # policy applies — a manual leg passes False because the operator's deliberate re-buy is
        # the policy exception by definition. The risk module stays blind to WHO asked; it only
        # receives the policy sets. One-per-coin, bankroll, gas, kill, daily all stay unconditional.
        # TWO FAMILIES, TWO SWITCHES (2026-08-28). They travel together for a manual entry, which
        # passes include_reentry_policy=False and empties both — but a per-wallet exemption names
        # ONE family, so excusing the cooldown must not silently excuse losing-re-entry as well.
        # `include_losing_reentry` defaults True, so every existing caller is unchanged.
        # `reentry_closes_by` (2026-10-08) narrows both sets to the closes of one wallet, the
        # caller's cross-wallet re-entry scope; None (default) keeps every close. Evaluated here,
        # so the reservation applies it to the closes it reads under its own lock.
        cooldown_coin_ids=(m.recently_closed_real_coin_ids(
                               conn, real_utc_now_iso(), cfg.MEMECOIN_REAL_REENTRY_COOLDOWN_S,
                               closes_by=reentry_closes_by, lifted=lifted["cooldown"])
                           if include_reentry_policy and include_reentry_cooldown else frozenset()),
        losing_coin_ids=(m.losing_real_coin_ids(
                             conn, since_iso=cfg.losing_reentry_since_iso(real_utc_now_iso()),
                             closes_by=reentry_closes_by, lifted=lifted["losing"])
                         if (include_reentry_policy and include_losing_reentry
                             and cfg.MEMECOIN_REAL_BLOCK_LOSING_REENTRY)
                         else frozenset()),
        reentry_lifted=lifted,
        override_risk_caps=bool(override_risk_caps))


def memecoin_entry_allowed(ctx, *, mint, trade_size):
    """Fail-closed entry gate (spec §4). First failure wins. SOL reserve is layered in Task 7."""
    if not ctx.real_enabled:
        return _reject("real trading disabled")
    if ctx.kill_tripped:
        return _reject("kill switch tripped")
    if ctx.daily_tripped and not ctx.override_risk_caps:
        return _reject("daily loss limit reached (latched)")
    if ctx.drawdown_pause_until:
        return _reject("drawdown pause: today's realized PnL fell $%.0f below the day's high; "
                       "automatic entries resume %s UTC"
                       % (float(cfg.MEMECOIN_DRAWDOWN_PAUSE_USD), ctx.drawdown_pause_until[11:19]))
    if ctx.usdc_balance is None:
        # BEFORE any size-derived check: unknown balance zeroes the sizing, so the invariant and
        # min-floor rejects below would fire first and report confidently wrong reasons. Refuse
        # honestly and retryably — the money may be exactly where the ledger says, and the next
        # signal re-reads. "insufficient bankroll" for a failed read sends the operator chasing a
        # balance problem that does not exist (live, 2026-08-26 14:57Z, $108 in the wallet).
        return _reject("USDC balance unavailable")
    if float(trade_size) != ctx.trade_size_usd:
        return _reject("trade size must equal approved size (invariant)")
    # DUPLICATE BEFORE THE FLOOR (2026-09-30, attempt 822b4ac1): another lane's reservation for this
    # coin is subtracted from spendable, so a duplicate sizes to $0 and used to be reported as
    # "below min-trade floor". Both refuse; only the reason changes. Policy notes stay below.
    if (cfg.MEMECOIN_REAL_ONE_POSITION_PER_COIN and not ctx.override_risk_caps
            and m.coin_identity("solana", mint) in ctx.held_coin_ids):
        return duplicate_refusal(ctx.duplicate_blockers.get(m.coin_identity("solana", mint)))
    if trade_size < ctx.min_trade_floor_usd:
        # Say the NUMBERS. "-EV at this clip" alone reads as a verdict on the coin, so the
        # operator cannot tell a small clip from a nearly-empty wallet and goes looking for a
        # bug in the gate (live, 2026-09-03).
        return _reject("below min-trade floor: $%.2f clip < $%.2f floor (-EV at this clip)"
                       % (float(trade_size), float(ctx.min_trade_floor_usd)))
    # One-per-coin is a POLICY choice (the 2026-07-09 re-entry study), so it is switchable. The
    # INSERT-level dedup race guard in reserve_entry is not, and stays on either way: that one
    # prevents two concurrent reservations double-spending into the same coin, which is a
    # correctness property, not a preference.
    # Turning this off lets several funded positions share a mint. The linear rails are per
    # position id and are unaffected. `real_mirror_on_sell` is NOT: it refuses to close when a
    # (uid, mint) has more than one open position, so duplicates would be unclosable by mirror.
    # follow-30-50-v1 sets mirror_on_sell=False, so that path is already inert for the armed
    # roster — re-enabling mirror with this off would strand duplicates.
    # (The one-per-coin check itself now runs above the min-trade floor; see there.)
    # PER-CHAIN SLOT CAP, checked before the shared policy's GLOBAL cap so the refusal names which
    # limit bound. Both used to write the bare string "max concurrent positions reached", and
    # telling a chain-local cap from the portfolio one then needed a network_id group-by — which
    # is how four 4663 refusals from 2026-07-18 were read as current starvation six weeks later.
    if (not ctx.override_risk_caps and ctx.chain_max_concurrent > 0
            and ctx.chain_automatic_count >= ctx.chain_max_concurrent):
        return _reject("solana max concurrent positions reached (%d)" % ctx.chain_max_concurrent)
    if m.coin_identity("solana", mint) in ctx.cooldown_coin_ids:
        return _reject("re-entry cooldown: a real position on this coin closed within the window")
    # Do not re-fight a coin that already beat us. Unlike the cooldown above this is
    # loss-conditional and does not expire: it lifts only if that coin's most recent real close
    # stops being a loss, which cannot happen while the block holds -- so in practice it is
    # permanent per coin, which is the operator's intent (KIRK, -$39.51 across two entries).
    if m.coin_identity("solana", mint) in ctx.losing_coin_ids:
        return _reject("losing re-entry: our last real position on this coin lost")
    # SOL full-book exit reserve: after this entry there are (chain_concurrent + 1) Solana
    # positions; guarantee gas to exit all of those with retry. Other chains use other gas assets.
    required_sol = required_entry_sol(ctx.chain_concurrent, ctx.gas_entry_sol,
                                      ctx.gas_exit_sol, ctx.gas_retry_factor)
    if ctx.sol_balance is None:
        return _reject("SOL balance unavailable")
    if ctx.sol_balance - ctx.pending_sol_lamports / 1e9 < required_sol:
        return _reject("insufficient SOL for entry + full-book exit reserve")
    if ctx.funding_asset == "sol" and (
            ctx.funding_amount_atomic is None or ctx.funding_amount_atomic <= 0
            or ctx.funding_available_usd is None
            or ctx.funding_available_usd + 1e-9 < float(trade_size)):
        return _reject("SOL buffer insufficient for approved clip")
    # delegate concurrency / total-exposure / effective-USDC to the shared evaluator, using
    # EFFECTIVE (confirmed+reserved) inputs. daily-loss + market checks are neutralized: the
    # latch above is the sole daily gate; there is no market concept for memecoins.
    # OVERRIDE feeds the evaluator permissive CAPS, never a fabricated bankroll/exposure READING
    # -- bankroll_usdc and total_exposure_usdc stay the real numbers, so a genuinely-empty wallet
    # still refuses. Only the artificial ceilings relax.
    exposure_cap = (1e12 if ctx.override_risk_caps
                    else ctx.max_total_exposure_usd if ctx.max_total_exposure_usd > 0 else 1e12)
    rc = RiskContext(
        bankroll_usdc=(ctx.funding_available_usd if ctx.funding_asset == "sol" else
                       ctx.usdc_balance - (ctx.effective_pending_usdc
                                           if ctx.effective_pending_usdc is not None
                                           else ctx.effective_pending_notional_usd)),
        total_exposure_usdc=ctx.effective_exposure_usd,
        market_exposure_usdc=ctx.effective_exposure_usd,
        concurrent_positions=ctx.effective_concurrent,
        daily_realized_pnl_usdc=0.0,
        max_total_exposure_usdc=exposure_cap,
        max_market_exposure_usdc=exposure_cap,                 # == total: market clamp never fires
        max_concurrent_positions=(1_000_000 if ctx.override_risk_caps else ctx.max_concurrent),
        daily_loss_limit_usdc=1e12,                            # inert; latch owns daily
        paper_only=not ctx.real_enabled, real_trading_enabled=ctx.real_enabled,
        venue="solana_memecoin")
    decision = evaluate_entry_risk(rc, float(trade_size), condition_id=mint, execution_mode="real")
    if decision.allowed and decision.clamped:
        # a clamped approval would make the reservation notional != the actual spend — the
        # write-ahead ledger's core invariant. Reject instead; the next entry re-sizes cleanly.
        return _reject("size clamped by risk policy (reservation!=spend)")
    return decision


def winner_reentry_refusal(conn, classification, *, network_id, mint, signal_source=None):
    """The winner re-entry ladder, re-read UNDER the reservation lock (spec 2026-09-28).

    Classification sized the clip from the rank it saw. A held position on the coin can close in
    between, so the rank is read again here and a mismatch refuses rather than reserving one
    ladder step too big. Returns the refusal reason, or None to proceed."""
    step = m.winner_reentry_step(
        conn, network_id, mint, real_utc_now_iso(),
        signal_source=signal_source or getattr(classification, "signal_source", None))
    sealed = (getattr(classification, "facts", None) or {}).get("reentry_rank")
    if step is None:
        return (None if sealed is None else
                "winner re-entry ladder: rank changed since classification (%s -> 0)" % sealed)
    if step["factor"] is None:
        return ("winner re-entry ladder exhausted: %d re-entries in the window (max %d)"
                % (step["rank"], len(cfg.winner_reentry_ladder() or ())))
    if sealed != step["rank"]:
        return ("winner re-entry ladder: rank changed since classification (%s -> %d)"
                % (sealed, step["rank"]))
    return None


def reserve_entry(conn, *, mint, source_user_id, strategy, day, usdc_balance, sol_balance,
                  now_iso, kill_tripped=False, origin_event_id=None, attempt_id=None,
                  signal_source=None, source_price=None, source_market_cap=None,
                  observed_at=None, clock_domain=None, classification=None,
                  cluster_decision_id=None, size_override=None, include_reentry_policy=True,
                  include_losing_reentry=True, include_reentry_cooldown=True, enforce_concurrency=True,
                  funded_policy=None,
                  enforce_min_floor=True, override_risk_caps=False,
                  funding_asset="usdc", funding_sol_usd=None, sol_buffer_cap_usd=0.0,
                  funding_overhead_atomic=0, trial=None, size_cap_usd=None,
                  reentry_closes_by=None, clip_ceiling_usd=None, snapshot_debits=(),
                  snapshot_debits_fn=None, entry_tip_lamports=None, timing=None,
                  entry_sol_budget=None, desk=None):
    """Atomic armed-entry gate: latch the daily breaker, evaluate the envelope on effective
    values, and — only if allowed — reserve the coin (sig-less) in one immediate transaction.
    BEGIN IMMEDIATE protects capacity across different coins; the partial UNIQUE network/mint index
    protects coin dedup. The caller (S2) then set_pending_sig(...) BEFORE broadcasting, and confirm/release on
    the outcome. Optional timing is collected in memory for the caller's existing trace;
    lock_ms measures BEGIN execution, not independently observed SQLite lock contention."""
    # Serialize the capacity snapshot + reservation across processes. Mint uniqueness only protects
    # two callers racing on the SAME coin; different mints could otherwise both consume the final
    # concurrency/bankroll slot after reading the same stale snapshot.
    # A supplied value is the already assigned arm, including a known zero. Ordinary callers
    # retain their existing arm selection. Never infer a slot arm from the all-lanes flag.
    if entry_sol_budget is not None:
        entry_sol_budget.clear()  # a refused re-attempt must not reuse an older reservation's budget
    if entry_tip_lamports is not None and (type(entry_tip_lamports) is not int or entry_tip_lamports < 0):
        return _reject("entry tip liability unavailable"), None
    if size_cap_usd is not None:
        try:
            value = float(size_cap_usd)
            if not math.isfinite(value) or value <= 0:
                return _reject("entry size cap unavailable"), None
        except (ValueError, TypeError, OverflowError):
            return _reject("entry size cap unavailable"), None
    from sniper.services.memecoin_wallet_caps import entry_cap_usd
    size_cap_usd = entry_cap_usd(source_user_id, "solana", existing_cap=size_cap_usd,
                                 ceiling_usd=clip_ceiling_usd)
    phase_started = time.perf_counter() if timing is not None else 0.0
    conn.execute("BEGIN IMMEDIATE")
    try:
        if timing is not None:
            phase_done = time.perf_counter()
            timing["lock_ms"] = (phase_done - phase_started) * 1000
            phase_started = phase_done
        m.check_and_latch_daily_loss(
            conn, day, cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD, commit=False)
        # TRIAL FIRST (trial spec 5.2): the clip below comes only from a marker validated here,
        # so an exempt entry (manual, override) is never sized to a trial clip.
        refusal, trial = trial_refusal(
            conn, source_user_id=source_user_id, network_id="solana", trial=trial,
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
            if snapshot_debits_fn is not None:
                if not callable(snapshot_debits_fn) or snapshot_debits:
                    raise ValueError("invalid snapshot debit callback")
                # Settlement cannot cross this capacity transaction. Resolve history here,
                # after serialization, rather than beside the earlier RPC balance read.
                snapshot_debits = snapshot_debits_fn()
            ctx = build_memecoin_risk_context(
                conn, usdc_balance=usdc_balance, sol_balance=sol_balance,
                day=day, kill_tripped=kill_tripped, risk_scale=risk_scale,
                size_override=size_override, include_reentry_policy=include_reentry_policy,
                include_losing_reentry=include_losing_reentry, include_reentry_cooldown=include_reentry_cooldown,
                reentry_closes_by=reentry_closes_by, clip_ceiling_usd=clip_ceiling_usd,
                enforce_concurrency=enforce_concurrency, copy_slots=desk is None,
                enforce_min_floor=enforce_min_floor,
                override_risk_caps=override_risk_caps, funding_asset=funding_asset,
                funding_sol_usd=funding_sol_usd, sol_buffer_cap_usd=sol_buffer_cap_usd,
                funding_overhead_atomic=funding_overhead_atomic,
                snapshot_debits=snapshot_debits,
                size_cap_usd=(min(size_cap_usd, float(trial["clip_usd"])) if trial and size_cap_usd is not None
                              else (trial or {}).get("clip_usd", size_cap_usd)))
        except (AttributeError, ValueError) as exc:
            conn.rollback()
            m.check_and_latch_daily_loss(
                conn, day, cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD, commit=True)
            return _reject(f"invalid_funded_contract: {exc}"), None
        if timing is not None:
            phase_done = time.perf_counter()
            timing["risk_ms"] = (phase_done - phase_started) * 1000
            phase_started = phase_done
        # A Nozomi arm-B attempt (MEMECOIN_NOZOMI_ALL_LANES) also pays its tip from this SOL.
        from sniper.execution.solana_sender_ab import nozomi_gas_extra_sol
        tip_atomic = (entry_tip_lamports if entry_tip_lamports is not None else
                      round(nozomi_gas_extra_sol(attempt_id, source_user_id) * 1e9))
        if funding_asset == "sol" and entry_tip_lamports is not None and funding_overhead_atomic < tip_atomic:
            conn.rollback()
            return _reject("native funding missing frozen entry tip"), None
        if tip_atomic and (funding_asset != "sol" or entry_tip_lamports is None):
            import dataclasses
            ctx = dataclasses.replace(ctx, gas_entry_sol=ctx.gas_entry_sol + tip_atomic / 1e9)
        decision = memecoin_entry_allowed(ctx, mint=mint, trade_size=ctx.trade_size_usd)
        if not decision.allowed:
            conn.commit()  # preserve a daily-loss latch created inside this transaction
            return decision, None
        sol_spend_budget = entry_sol_spend_budget(ctx) if entry_sol_budget is not None else None
        # NOT tied to include_reentry_policy (Lambda F2, 2026-09-28): a cooldown-exempt wallet
        # passes False there, and classification has already scaled its clip, so skipping this
        # would let an exhausted or malformed ladder re-enter it at full size. Manual entries are
        # excluded inside winner_reentry_step; an operator override skips it like every cap.
        ladder_refusal = (None if override_risk_caps else
                          winner_reentry_refusal(conn, classification, network_id="solana",
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
                    network_id="solana",
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
                # AFTER the rollback: a band refusal keeps its classification so the floor stays
                # auditable (which mcap produced which band), not just a reason string.
                persist_refused_classification(conn, attempt_id, exc)
                m.check_and_latch_daily_loss(
                    conn, day, cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD, commit=True)
                return _reject(f"invalid_funded_contract: {exc}"), None
        # entry-time liquidity/age are read from the classifier's own facts, already computed by
        # classify_funded_entry above (no extra provider call) — see facts["liquidity"] /
        # facts["pair_age_s"] in memecoin_market_state.py. Absent classification/facts -> NULL,
        # never 0 (0 would read as a measured zero, not a missing reading).
        facts = classification.facts if classification is not None else {}
        too_young = coin_age_refusal(classification, "solana", source_user_id)
        if too_young:                       # refuse BEFORE reserving, so no capital is held
            conn.rollback()
            return _reject(too_young), None
        if timing is not None:
            phase_done = time.perf_counter()
            timing["policy_ms"] = (phase_done - phase_started) * 1000
            phase_started = phase_done
        pid = m.reserve_pending(
            conn, mint=mint, source_user_id=source_user_id, strategy=strategy,
            notional=ctx.trade_size_usd, reserved_at=now_iso, network_id="solana",
            origin_event_id=origin_event_id, attempt_id=attempt_id,
            signal_source=signal_source, source_price=source_price,
            source_market_cap=source_market_cap,
            source_liquidity=facts.get("liquidity"), source_token_age_s=facts.get("pair_age_s"),
            observed_at=observed_at,
            clock_domain=clock_domain, cluster_decision_id=cluster_decision_id,
            funding_asset=("sol" if funding_asset == "sol" else
                           "usdc" if entry_tip_lamports is not None or tip_atomic else None),
            funding_amount_atomic=ctx.funding_amount_atomic,
            funding_overhead_atomic=(ctx.funding_overhead_atomic if funding_asset == "sol" else
                                     tip_atomic if entry_tip_lamports is not None or tip_atomic else None),
            funding_sol_usd=ctx.funding_sol_usd,
            commit=False, **contract_fields)
        if timing is not None:
            phase_done = time.perf_counter()
            timing["insert_ms"] = (phase_done - phase_started) * 1000
            phase_started = phase_done
        # The refusal label is read INSIDE the reservation transaction, before its commit, so it
        # names the gate the INSERT actually hit (Codex review); a copy refusal returns at once.
        lost = (m.lost_reservation_reason(conn, network_id="solana", mint=mint,
                                          signal_source=signal_source, at_iso=now_iso,
                                          funded_contract_json=contract_fields.get("funded_contract_json"))
                if pid is None else None)
        conn.commit()
        if timing is not None:
            timing["commit_ms"] = (time.perf_counter() - phase_started) * 1000
        if pid is None:   # lost the coin-dedup race between check and INSERT, or a desk gate
            return _reject(lost), None
        if entry_sol_budget is not None:
            entry_sol_budget.update(pending_id=pid, available_lamports=sol_spend_budget)
        return with_reentry_lift(decision, ctx, "solana", mint), pid
    except Exception:
        conn.rollback()
        raise


def with_reentry_lift(decision, ctx, network_id, mint):
    """The allowed decision, carrying {family: blocking close} for each re-entry family the
    caller's cross-wallet scope lifted on this coin (the attempt trace records it), or unchanged."""
    coin = m.coin_identity(network_id, mint)
    lift = {family: closes[coin] for family, closes in (ctx.reentry_lifted or {}).items()
            if coin in closes}
    return replace(decision, reentry_lift=lift) if lift else decision


def trial_refusal(conn, *, source_user_id, network_id, trial, classification, enforce=True,
                  override_risk_caps=False):
    """The trial checks inside a reservation's BEGIN IMMEDIATE (trial spec 5.2). Returns (refusal
    reason or None, the marker to freeze or None). The marker is rebuilt from the trial ROW, never
    taken from the caller. `enforce` is the caller's enforce_concurrency: an entry exempt from the
    slot caps, or an operator override, is not a trial entry.

    One read on the open-trial index for every automatic reservation, so ANY path that cannot carry
    the trial clip and marker (the slot lane, a future lane) is refused for a trial wallet instead of
    buying at the full clip."""
    from sniper.services import memecoin_trial as trials
    if not enforce or override_risk_caps or not source_user_id:
        return None, None
    row = m.open_trial(conn, source_user_id, network_id)
    asked = (trial or {}).get("trial_id")
    if row is None:
        return (("trial_ended: trial #%s is no longer open" % asked) if trial else None), None
    if row["stopped_at"]:
        return "trial_stopped: trial #%d stopped at %s" % (row["id"], row["stopped_at"]), None
    if trial is None:
        return ("%s: open trial #%d on %s; this path cannot carry the trial clip and marker"
                % (trials.TRIAL_UNMARKED, row["id"], row["chain"])), None
    if asked is None or int(asked) != int(row["id"]):
        return "trial_ended: trial #%s is not the open trial #%d" % (asked, row["id"]), None
    if classification is None:
        return "trial_no_contract: a trial entry needs a funded contract for its marker", None
    open_n, cap = m.trial_book_count(conn), int(cfg.MEMECOIN_REAL_TRIAL_MAX_CONCURRENT)
    if open_n >= cap:
        return ("trial_concurrency: %d trial positions and reservations open, cap %d"
                % (open_n, cap)), None
    held = int(row["positions"]) + m.trial_reserved_count(conn, row["id"])
    two_of_three = row["rule"] == trials.TP_TWO_OF_THREE_RULE
    limit = 3 if two_of_three else trials.MAX_POSITIONS
    if held >= limit:
        return "trial_full: trial #%d holds %d of %d positions" % (
            row["id"], held, limit), None
    if two_of_three:
        closes = trials.tp_two_of_three_closes(m.trial_closes_linked(conn, row))
        if trials.tp_two_of_three_verdict(closes) is not None:
            return "trial_verdict_pending: trial #%d awaits its terminal grade" % row["id"], None
        # A partial sale or unknown close cannot free the next sequential admission. The held
        # count includes a signed/unsigned reservation until it is explicitly resolved.
        if held > len(closes):
            return "trial_outcome_pending: trial #%d awaits its held outcome" % row["id"], None
    return None, trials.marker(row)


def preflight_entry(
        conn, *, mint, day, usdc_balance, sol_balance, kill_tripped=False,
        classification=None, include_reentry_policy=True, include_losing_reentry=True,
        include_reentry_cooldown=True,
        enforce_concurrency=True, enforce_min_floor=True, override_risk_caps=False,
        reentry_closes_by=None, clip_ceiling_usd=None, copy_slots=True):
    """Read the funded risk state without a reservation or a daily-loss latch write.
    copy_slots must match reserve_entry's (False for a desk entry, 1a): both hops agree.

    include_reentry_policy must match what the caller will pass to reserve_entry — the 08-27
    manual re-buy fix missed this hop on its first pass: the leg runs preflight BEFORE the
    reservation and returns on refusal, so a fixed reserve_entry alone never ran. Same rule for
    override_risk_caps (2026-09-22): both hops must agree, or a preflight refusal on a stale
    reading blocks an override entry the reservation itself would have allowed."""
    risk_scale = classification.risk_scale if classification is not None else 1.0
    ctx = build_memecoin_risk_context(
        conn,
        usdc_balance=usdc_balance,
        sol_balance=sol_balance,
        day=day,
        kill_tripped=kill_tripped,
        risk_scale=risk_scale,
        include_reentry_policy=include_reentry_policy,
        include_losing_reentry=include_losing_reentry, include_reentry_cooldown=include_reentry_cooldown,
        enforce_concurrency=enforce_concurrency, copy_slots=copy_slots,
        enforce_min_floor=enforce_min_floor,
        override_risk_caps=override_risk_caps,
        reentry_closes_by=reentry_closes_by,
        clip_ceiling_usd=clip_ceiling_usd,
    )
    return memecoin_entry_allowed(ctx, mint=mint, trade_size=ctx.trade_size_usd)


def _select_copy_arm_for_contract(conn, *, source_user_id, network_id, engine_authoritative):
    """Resolve the source wallet's copy arm, or None to keep the band policy.

    Fails SOFT by design. A selection error must never block a funded entry that every admission
    gate already approved — the position simply opens on the band rails it would have had before
    this feature existed. It is logged with context rather than swallowed, because a selection that
    is silently never applied is indistinguishable from a feature that was never deployed.
    """
    from sniper.services.memecoin_copy_arm import copy_arm_enabled, select_copy_arm

    if engine_authoritative or not source_user_id or not copy_arm_enabled():
        return None
    try:
        from sniper.services.memecoin_chains import resolve_chain
        chain = resolve_chain(network_id)
        if chain is None:
            return None
        return select_copy_arm(conn, source_user_id, chain=chain)
    except Exception:
        logger.exception("copy-arm selection failed wallet=%s network=%s; using band policy",
                         source_user_id, network_id)
        return None


def apply_wallet_exit_override(conn, policy, *, source_user_id, network_id):
    """(policy with the operator's per-wallet TP/stop, provenance) or (policy, None).

    LAST in the chain on purpose: the operator's per-(wallet, chain) choice in the UI wins over the
    band default, the .env wallet lists and a copy arm. Never over the engine envelope, whose sale
    decision belongs to the V5 engine. A read failure keeps the base rails and is logged: default
    rails, never no rails. One indexed lookup, no network."""
    from sniper.services.memecoin_funded_policy import (
        ENGINE_ENVELOPE_POLICY_ID, SOURCE_MIRROR_TP100_POLICY_ID)
    if policy.policy_id in (ENGINE_ENVELOPE_POLICY_ID, SOURCE_MIRROR_TP100_POLICY_ID) or not source_user_id:
        return policy, None
    try:
        override = m.wallet_exit_override(conn, source_user_id, network_id=network_id)
    except Exception:
        logger.exception("wallet exit override unreadable wallet=%s network=%s; base rails",
                         source_user_id, network_id)
        return policy, None
    if override is None:
        return policy, None
    changes = {k: override[k] for k in ("tp_pct", "stop_pct") if override[k] is not None}
    provenance = {"id": override["id"], "tp_pct": override["tp_pct"],
                  "stop_pct": override["stop_pct"], "base_policy_id": policy.policy_id,
                  "base_tp_pct": policy.tp_pct, "base_stop_pct": policy.stop_pct,
                  "set_by": override["actor"], "set_at": override["set_at"]}
    return replace(policy, **changes), provenance


def funded_policy_for_origin(conn, *, coin_band, network_id, source_user_id,
                             entry_mcap=None, apply_wallet_override=True, provenance=None):
    """The exit rails a funded entry from this origin would be frozen under, and its copy arm.

    ONE function, because two callers need the same answer: the funded contract builder, and the
    paper baseline arm that exists to be its control. A control that restates these rules instead
    of calling them measures the difference between two transcriptions the first time either
    moves, and reports it as a difference between paper and funded.

    The order is load-bearing and is the order the funded path has always used:

    1. the band and chain default, which may be an engine envelope;
    2. the per-wallet override, which needs a handle and therefore cannot live in the resolver;
    3. the per-wallet copy arm, suppressed when the policy owns its own rails;
    4. the operator's per-(wallet, chain) exit override from the UI (apply_wallet_exit_override).

    Returns the policy and the copy arm, because the contract records both and a control that
    took the policy without the arm would not be running the same rails. A caller that freezes a
    contract passes a `provenance` dict; step 4 fills provenance['exit_override'] from the SAME
    read that changed the policy, so the contract cannot describe a value it did not apply.
    """
    from sniper.services.memecoin_funded_policy import (
        ENGINE_ENVELOPE_POLICY_ID, resolve_funded_policy, tp_only_applies, _tp_only_policy,
        follow3050_applies, _follow3050_policy, owns_its_rails,
        SOURCE_MIRROR_TP100_POLICY_ID, source_mirror_tp100_applies, source_mirror_tp100_policy)

    if source_mirror_tp100_applies(source_user_id, network_id):
        from sniper.services.memecoin_chains import canonical_chain_id
        if provenance is not None:
            provenance["source_mirror_tp100"] = {
                "policy_id": SOURCE_MIRROR_TP100_POLICY_ID, "source_user_id": source_user_id,
                "network_id": canonical_chain_id(network_id)}
        return source_mirror_tp100_policy(coin_band=coin_band), None

    policy = resolve_funded_policy(coin_band=coin_band, network_id=network_id)
    # PER-WALLET copy arm (2026-08-04): the source wallet's best paper arm supplies the exit rails
    # instead of the band default. Applied AFTER the band floor, so selection can never admit a
    # coin admission refused. Never overrides an engine-authoritative envelope — that chain has
    # surrendered its linear rails to the V5 engine, and a copy arm is a linear rail set.
    # tp-only-v1 is applied HERE, not in the resolver, because only this scope knows WHOSE signal
    # this is. It governs a named (wallet, chain) pair and nothing else.
    _handle = conn.execute(
        "SELECT handle FROM memecoin_wallets WHERE source_user_id=?", (source_user_id,)).fetchone()
    if policy.policy_id != ENGINE_ENVELOPE_POLICY_ID and _handle:
        if follow3050_applies(_handle[0], network_id):
            policy = _follow3050_policy(coin_band=coin_band)
        elif tp_only_applies(_handle[0], network_id):
            policy = _tp_only_policy(coin_band=coin_band)
    # tp-only-v1 suppresses the copy arm for the same reason an engine envelope does: it IS the
    # chosen linear rail set, deliberately without mirror-on-sell, and a copy arm would silently
    # replace it with an arm that mirrors the wallet out — the exact behaviour it exists to avoid.
    # BY FAMILY, NOT BY VERSIONED STRING (2026-08-26). This was a hard-coded
    # ("tp-only-v1", "follow-30-50-v1") tuple, so the timecap lift's rename to -v2 would have made
    # it stop matching and silently reinstated a mirroring copy arm over the rails the comment
    # above says it exists to protect. `owns_its_rails` matches the family, so any future version
    # keeps working.
    copy_arm = _select_copy_arm_for_contract(
        conn, source_user_id=source_user_id, network_id=network_id,
        engine_authoritative=owns_its_rails(policy.policy_id))
    if copy_arm is not None and copy_arm.get("arm"):
        from sniper.services.memecoin_copy_arm import funded_policy_for_arm
        policy = funded_policy_for_arm(
            copy_arm["arm"], coin_band=coin_band, network_id=network_id, entry_mcap=entry_mcap)
    if apply_wallet_override:
        policy, override = apply_wallet_exit_override(
            conn, policy, source_user_id=source_user_id, network_id=network_id)
        if override is not None and provenance is not None:
            provenance["exit_override"] = override
    return policy, copy_arm


def prepare_funded_contract(
        conn, *, classification, network_id, mint, attempt_id, origin_event_id,
        base_clip_usd, approved_clip_usd, source_user_id=None, funded_policy=None, trial=None,
        desk=None):
    from sniper.services.memecoin_funded_policy import (
        band_is_reservable, build_funded_contract, canonical_funded_contract)
    from sniper.services.memecoin_market_state import (
        EntryClassification, finalize_funded_classification)

    if not isinstance(classification, EntryClassification):
        raise ValueError("classification must be EntryClassification")
    if not attempt_id:
        raise ValueError("contract reservation requires attempt_id")
    expected_identity = m.coin_identity(network_id, mint)
    actual_identity = m.coin_identity(classification.network_id, classification.mint)
    if actual_identity != expected_identity:
        raise ValueError("classification identity does not match reservation")
    final = finalize_funded_classification(
        classification, base_clip_usd=base_clip_usd)
    classification_json, classification_hash = m.canonical_funded_json(final.to_dict())
    # mcap FLOOR (2026-07-27). One gate at the admission seam BOTH chains route through, so a
    # per-chain copy can't drift out of sync. BandRefused carries the classification because the
    # callers roll back on ValueError — without it the refused row loses its effective_mcap.
    if not band_is_reservable(final.coin_band, signal_source=final.signal_source):
        raise BandRefused(final.coin_band,
                          classification_json=classification_json,
                          classification_hash=classification_hash,
                          classification_version=final.classification_version)
    provenance = {}
    exit_trial = None
    if funded_policy is not None:
        policy, copy_arm = funded_policy, None     # manual: the operator named the rails
    else:
        policy, copy_arm = funded_policy_for_origin(
            conn, coin_band=final.coin_band, network_id=expected_identity[0],
            source_user_id=source_user_id, entry_mcap=final.facts.get("effective_mcap"),
            provenance=provenance)
        # DEGEN EXIT TRIAL V4: replaces whatever the origin resolved (band, follow-30-50, copy arm,
        # the UI TP/stop override); the contract records none of them, because none was applied.
        from sniper.services.memecoin_funded_policy import (
            SOURCE_MIRROR_TP100_POLICY_ID, exit_trial_v4_applies, exit_trial_v4_policy)
        if policy.policy_id != SOURCE_MIRROR_TP100_POLICY_ID:
            exit_trial = exit_trial_v4_applies(source_user_id=source_user_id,
                                             network_id=expected_identity[0], decision_at=final.decision_at)
        if exit_trial is not None:
            override = provenance.pop("exit_override", None)
            exit_trial.update(replaced_policy_id=policy.policy_id,
                              replaced_copy_arm=bool((copy_arm or {}).get("arm")),
                              replaced_override_id=(override or {}).get("id"))
            policy, copy_arm = exit_trial_v4_policy(coin_band=final.coin_band), None
    if desk is not None:
        # The desk seat's bounds-checked rails replace the numbers of the band policy, in `policy`
        # (same schema, so a pinned prior release runs them); who decided and what it replaced
        # lives in source['desk']. Frozen and hashed here, before any signing.
        from sniper.services.memecoin_funded_policy import apply_desk_rails
        if funded_policy is not None or exit_trial is not None:
            raise ValueError("a desk entry cannot carry another policy owner")
        policy, desk = apply_desk_rails(policy, desk), dict(desk, base_policy_id=policy.policy_id)
        copy_arm = None
    contract = build_funded_contract(
        classification=final,
        base_clip_usd=base_clip_usd,
        approved_clip_usd=approved_clip_usd,
        policy=policy,
        origin_event_id=origin_event_id,
        copy_arm=copy_arm,
        exit_override=provenance.get("exit_override"),
        exit_trial=exit_trial,
        trial=None if provenance.get("source_mirror_tp100") else trial,
        source_mirror_tp100=provenance.get("source_mirror_tp100"),
        desk=desk,
    )
    contract_json, contract_hash = canonical_funded_contract(contract)
    m.set_entry_attempt_classification(
        conn,
        attempt_id,
        classification_json=classification_json,
        classification_hash=classification_hash,
        classification_version=final.classification_version,
        commit=False,
    )
    m.set_entry_attempt_funded_contract(
        conn,
        attempt_id,
        funded_contract_json=contract_json,
        funded_contract_hash=contract_hash,
        funded_contract_version=contract["contract_version"],
        commit=False,
    )
    return {
        "funded_contract_version": contract["contract_version"],
        "funded_contract_hash": contract_hash,
        "funded_contract_json": contract_json,
    }
