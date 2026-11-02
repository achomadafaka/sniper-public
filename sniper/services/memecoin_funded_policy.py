"""Immutable exit-policy selection for new funded memecoin reservations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
from typing import Any

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_market_state import EntryClassification
from sniper.services.memecoin_v5_replay_gate import (
    PolicyRegistry, active_policy_revision, policy_sha256)
from sniper.settings import memecoin as cfg


FUNDED_CONTRACT_VERSION = 1
FUNDED_POLICY_VERSION = 1
# The engine-authoritative envelope (2026-07-24 v3 prereg §5). Selected at reservation time when
# the position's chain is in MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS AND an accepted V5 policy revision
# is selected. The engine version alone is not a policy identity: source metadata also freezes the
# accepted manifest and its canonical hash.
ENGINE_ENVELOPE_POLICY_ID = "engine-envelope-v1"
MANUAL_UNMANAGED_POLICY_ID = "manual-unmanaged-v1"
SOURCE_MIRROR_TP100_POLICY_ID = "source-mirror-tp100-v1"
# The 4663 follow policy identity. Named for what it IS (the Robinhood follow policy), not for the
# take-profit confirmation it used to carry — that override is gone; see the note below.
ROBINHOOD_FOLLOW_POLICY_ID = "follow-30-50-v3"

# REMOVED 2026-09-03 (operator): the Robinhood take-profit confirmation override.
#
# It forced a 3.0s TP hold on 4663 while `.env` said MEMECOIN_REAL_TP_CONFIRM_SECONDS=0, so the
# one knob that documents this behaviour was false for the one chain the behaviour ran on. The
# window is now the env value everywhere, and 0 means the take-profit fires on the touch.
#
# MEASURED before removal, over the 10 funded positions it ever held (08-30 .. 09-03): the exact
# whole-bag quote at the moment of nomination beat the realized outcome by $224.12. It saved two
# positions (AU +$19, ASS +$88 above their TP quote) and cost six, every one of which rode from a
# confirmed take-profit down to `b_stop`: HOUSE -$106, RIZZLER -$70, ROBINCAT -$62, 心怀感恩 -$61,
# DOUGH -$34, Cindy -$16. DOUGH is the shape of the whole set — one sweep at +31.5% with $66.98
# quoted on a $50.94 entry, the next sweep 2.6s later at +13.4%, and a 3.0s window that a 1.6s
# sweep cadence could not satisfy on a coin that volatile.

# TIMECAP LIFT (2026-08-26, operator "entirely replace the timeout"). The 13-minute cap was the
# working staleness exit; the armed volume-death rail took that job at 30s, so the cap goes back to
# being a pure catastrophe backstop at 4 hours.
#
# 14400 IS A JUDGMENT, NOT A MEASUREMENT, and nothing in the ledger can currently support one:
# EVERY funded hold ever observed is right-censored at max_hold_s=780, so the data cannot say what
# a position would have done in minute 14 or hour 3. The number is an operator choice for how long
# a coin may sit quiet-but-not-dead before the backstop takes it. It becomes measurable only after
# this cohort produces holds that run past 780s.
TIMECAP_LIFT_MAX_HOLD_S = 14400.0

# A LIFTED TIMECAP IS A DIFFERENT POLICY, SO IT GETS A DIFFERENT NAME. Writing 14400 under an
# existing policy_id would put two contents under one identity — the same mistake as one column
# carrying two meanings, at the contract layer, where it is worst: every historical comparison
# keyed on policy_id would silently mix cohorts.
#
# Explicit map, not a computed suffix bump. The trailing -vN already encodes each family's OWN
# content history at different numbers (nano/micro are on v2 for the fixed-fraction de-risk while
# small/mid are still v1), so a mechanical "+1" reads as a rule and is really a coincidence.
# `engine-envelope-v1` is deliberately ABSENT: the v5 engine owns its own sell decision under the
# v3 prereg, and its backstop is not this change's to move.
# Policies that ARE the chosen linear rail set, so a per-wallet copy arm must never replace them.
# MATCHED BY FAMILY, NOT BY VERSIONED STRING. A hard-coded ("tp-only-v1", "follow-30-50-v1") tuple
# lived at memecoin_risk.py:441 and would have stopped matching the moment the timecap lift renamed
# those families — silently reinstating a mirror-on-sell copy arm over the exact rails the comment
# there says it exists to protect. Found 2026-08-26 while reviewing an unrelated change; the rename
# was mine. Any future -vN keeps working.
SELF_RAILED_POLICY_FAMILIES = frozenset({"engine-envelope", "tp-only", "follow-30-50", "source-mirror-tp100"})


def policy_family(policy_id) -> str:
    """`follow-30-50-v2` -> `follow-30-50`. Identity without the version."""
    return str(policy_id or "").rsplit("-v", 1)[0]


def owns_its_rails(policy_id) -> bool:
    """True when this policy IS the rail set and a copy arm must not override it."""
    return policy_family(policy_id) in SELF_RAILED_POLICY_FAMILIES


TIMECAP_LIFT_POLICY_IDS = {
    "follow-30-50-v1": "follow-30-50-v2",
    "follow-30-50-v3": "follow-30-50-v4",
    "tp-only-v1": "tp-only-v2",
    "funded-nano-v2": "funded-nano-v3",
    "funded-micro-v2": "funded-micro-v3",
    "funded-small-v1": "funded-small-v2",
    "funded-mid-v1": "funded-mid-v2",
}


@dataclass(frozen=True)
class FundedExitPolicy:
    policy_id: str
    policy_version: int
    coin_band: str
    tp_pct: float
    derisk_pct: float
    derisk_mode: str
    trail_pct: float
    trail_arm_pct: float
    trail_requires_derisk: bool
    stop_pct: float
    catastrophe_pct: float
    stop_debounce_sweeps: int
    max_hold_s: float
    unquotable_exit_s: float
    mirror_on_sell: bool
    # v2 (2026-07-23): FIXED-FRACTION partial de-risk. Declared last WITH a default so every
    # pre-existing direct constructor (tests, tools) stays valid; contracts written before the
    # field parse via the explicit shim in _validated_policy_data.
    derisk_fraction: float = 0.0
    # Decision policy is frozen per funded position. V5-only (2026-07-25): engine-envelope contracts
    # freeze "v5" plus the accepted complete policy revision; non-engine policies parse as None.
    decision_engine_version: str | None = None
    decision_policy_sha256: str | None = None
    decision_policy_manifest: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        # Keep the strict policy object byte-compatible with the previous pinned reader. Engine
        # version is encoded in rollback-safe source metadata by build_funded_contract().
        value = asdict(self)
        for name in (
            "decision_engine_version", "decision_policy_sha256", "decision_policy_manifest",
        ):
            value.pop(name, None)
        return value


def _finite_nonnegative(name, value, *, upper=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (upper is not None and value > upper):
        raise ValueError(f"{name} is outside its valid range")
    return value


def _policy(*, policy_id, coin_band, tp, derisk, trail, stop, trail_arm,
            trail_requires_derisk, derisk_mode=None, derisk_fraction=0.0, mirror_on_sell=True,
            catastrophe=None, max_hold=None, unquotable=None, debounce=None,
            decision_engine_version=None, decision_policy_sha256=None,
            decision_policy_manifest=None):
    # catastrophe/max_hold/unquotable/debounce default to cfg, which is what every BAND policy
    # wants — passing None keeps those callers byte-identical. They are overridable only so a
    # per-wallet copy-arm policy (memecoin_copy_arm) can reproduce a paper arm's actual rails:
    # `scalp_c2` is a pure mirror with NO price floor at all, which cfg's catastrophe would
    # silently add back, and the short arms (momentum_a/peak_lock_e/trail_d) cap hold at minutes
    # or hours rather than the funded default week.
    if catastrophe is None:
        catastrophe = cfg.MEMECOIN_REAL_CATASTROPHE_PCT
    if max_hold is None:
        max_hold = cfg.MEMECOIN_REAL_MAX_HOLD_S
    if unquotable is None:
        unquotable = cfg.MEMECOIN_REAL_UNQUOTABLE_EXIT_S
    if debounce is None:
        debounce = cfg.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS
    # The lift is applied HERE because `_policy` is the one place every family is constructed, so
    # no family can be added later that silently keeps the old cap under a new name.
    if cfg.MEMECOIN_TIMECAP_LIFT_ENABLED and policy_id in TIMECAP_LIFT_POLICY_IDS:
        policy_id = TIMECAP_LIFT_POLICY_IDS[policy_id]
        max_hold = TIMECAP_LIFT_MAX_HOLD_S
    if derisk_mode is None:
        derisk_mode = "recover_entry" if float(derisk) > 0 else "off"
    derisk_fraction = _finite_nonnegative("derisk_fraction", derisk_fraction, upper=1.0)
    if derisk_mode == "fraction":
        if not (0.0 < derisk_fraction < 0.9):
            raise ValueError("fraction de-risk requires 0 < derisk_fraction < 0.9")
        if float(derisk) <= 0:
            raise ValueError("fraction de-risk requires a positive trigger")
    elif derisk_fraction != 0.0:
        raise ValueError("derisk_fraction is only meaningful in fraction mode")
    policy = FundedExitPolicy(
        policy_id=policy_id,
        policy_version=FUNDED_POLICY_VERSION,
        coin_band=coin_band,
        tp_pct=_finite_nonnegative("tp_pct", tp),
        derisk_pct=_finite_nonnegative("derisk_pct", derisk),
        derisk_mode=derisk_mode,
        derisk_fraction=derisk_fraction,
        trail_pct=_finite_nonnegative("trail_pct", trail, upper=100.0),
        trail_arm_pct=_finite_nonnegative("trail_arm_pct", trail_arm),
        trail_requires_derisk=bool(trail_requires_derisk),
        stop_pct=_finite_nonnegative("stop_pct", stop, upper=100.0),
        catastrophe_pct=_finite_nonnegative("catastrophe_pct", catastrophe, upper=100.0),
        stop_debounce_sweeps=debounce,
        max_hold_s=_finite_nonnegative("max_hold_s", max_hold),
        unquotable_exit_s=_finite_nonnegative("unquotable_exit_s", unquotable),
        mirror_on_sell=bool(mirror_on_sell),
        decision_engine_version=decision_engine_version,
        decision_policy_sha256=decision_policy_sha256,
        decision_policy_manifest=decision_policy_manifest,
    )
    if (isinstance(policy.stop_debounce_sweeps, bool)
            or not isinstance(policy.stop_debounce_sweeps, int)
            or policy.stop_debounce_sweeps < 1):
        raise ValueError("stop_debounce_sweeps must be a positive integer")
    _validate_source_mirror_tp100_policy(policy)
    if policy.max_hold_s <= 0 and policy.policy_id not in (
            MANUAL_UNMANAGED_POLICY_ID, SOURCE_MIRROR_TP100_POLICY_ID):
        raise ValueError("max_hold_s must be positive")
    if policy.trail_requires_derisk and policy.derisk_pct <= 0:
        raise ValueError("a post-derisk trail requires a derisk trigger")
    return policy


def source_mirror_tp100_applies(source_user_id, network_id):
    """Immutable exact-UID membership; no database or provider work on entry."""
    pairs = cfg.MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS
    if not pairs or not source_user_id:
        return False
    from sniper.services.memecoin_chains import canonical_chain_id
    return (source_user_id, canonical_chain_id(network_id)) in pairs


def _validate_source_mirror_tp100_policy(policy):
    if policy.policy_id != SOURCE_MIRROR_TP100_POLICY_ID:
        return
    fixed = {"tp_pct": 100.0, "derisk_pct": 0.0, "derisk_fraction": 0.0,
             "trail_pct": 0.0, "trail_arm_pct": 0.0, "stop_pct": 0.0,
             "catastrophe_pct": 0.0, "max_hold_s": 0.0, "unquotable_exit_s": 0.0}
    if (any(isinstance(getattr(policy, key), bool) or getattr(policy, key) != value
            for key, value in fixed.items())
            or policy.derisk_mode != "off" or policy.mirror_on_sell is not True
            or policy.trail_requires_derisk is not False
            or type(policy.policy_version) is not int or policy.policy_version != FUNDED_POLICY_VERSION
            or type(policy.stop_debounce_sweeps) is not int or policy.stop_debounce_sweeps != 1
            or any(getattr(policy, key) is not None for key in (
                "decision_engine_version", "decision_policy_sha256", "decision_policy_manifest"))):
        raise ValueError("source mirror TP100 policy shape is invalid")


def _validate_source_mirror_tp100_provenance(value, network_id, source_user_id=None):
    from sniper.services.memecoin_chains import canonical_chain_id
    if (not isinstance(value, dict)
            or set(value) != {"policy_id", "source_user_id", "network_id"}
            or value.get("policy_id") != SOURCE_MIRROR_TP100_POLICY_ID):
        raise ValueError("source mirror TP100 provenance is invalid")
    uid, network = value["source_user_id"], value["network_id"]
    if (not isinstance(uid, str) or not uid or any(c.isspace() for c in uid)
            or (source_user_id is not None and uid != source_user_id)
            or network in (None, "evm-unknown") or canonical_chain_id(network) != network
            or canonical_chain_id(network_id) != network):
        raise ValueError("source mirror TP100 provenance identity mismatch")


def source_mirror_tp100_policy(*, coin_band):
    band = "nano" if coin_band == "assumed_nano" else coin_band
    if band not in ("nano", "micro", "small", "mid", "out_of_scope"):
        raise ValueError("source mirror TP100 coin band is invalid")
    return _policy(
        policy_id=SOURCE_MIRROR_TP100_POLICY_ID, coin_band=band, tp=100.0,
        derisk=0.0, trail=0.0, trail_arm=0.0, trail_requires_derisk=False, stop=0.0,
        catastrophe=0.0, max_hold=0.0, unquotable=0.0, debounce=1, mirror_on_sell=True)


def band_anchor_trail_pct(coin_band: str) -> float:
    """The v1 anchor trail percent for a coin band — the SAME cfg value the non-envelope policy for
    that band uses as its EXECUTED trail (nano/micro share MEMECOIN_REAL_PARTIAL_TRAIL_PCT, small
    uses MEMECOIN_REAL_TRAIL_PCT, mid uses MEMECOIN_REAL_MID_TRAIL_PCT). One source, two uses: the
    live executed rail below, and — under engine-envelope-v1, where the executed trail is 0/off —
    the real-runner adapter's REFERENCE scale for propose_v5's vol-vs-trail comparison (2026-07-24
    v3 prereg §6). Never a new/fitted constant."""
    band = "nano" if coin_band == "assumed_nano" else coin_band
    if band in ("nano", "micro"):
        return cfg.MEMECOIN_REAL_PARTIAL_TRAIL_PCT
    if band == "small":
        return cfg.MEMECOIN_REAL_TRAIL_PCT
    if band == "mid":
        return cfg.MEMECOIN_REAL_MID_TRAIL_PCT
    raise ValueError(f"coin band is not reservable: {coin_band!r}")


def _engine_envelope_policy(*, coin_band: str, policy_revision=None) -> FundedExitPolicy:
    """engine-envelope-v1 (2026-07-24 prereg §5): tp/derisk/trail/stop all off — the v5 engine
    (memecoin_exit_brain.propose_v5) owns the sell decision, mapped onto existing funded reasons by
    the real-runner adapter. `_policy()` still pulls catastrophe/stop-debounce/max_hold/unquotable
    straight from cfg regardless of the zeros passed here, so those envelope rails stay live and
    absolute exactly like every other funded policy — only the linear tp/derisk/trail/stop rails are
    surrendered. Mirror-on-sell is off: a source wallet's own sell is flow for the engine to read,
    not an instruction (mirror_on_sell is already a per-position frozen-contract field read fresh
    every sweep, so this is not a new mechanism)."""
    band = "nano" if coin_band == "assumed_nano" else coin_band
    if band not in ("nano", "micro", "small", "mid"):
        raise ValueError(f"coin band is not reservable: {coin_band!r}")
    # V5-only (2026-07-25): the decision engine is no longer selectable — every new engine-envelope
    # contract freezes v5. `exit_engine("v5")` is a cheap self-check that the installed engine exists.
    from sniper.services.memecoin_exit_brain import exit_engine
    exit_engine("v5")
    if policy_revision is None:
        policy_revision = active_policy_revision()
    if policy_revision is None:
        raise ValueError("engine envelope requires an accepted active V5 policy revision")
    policy_sha, manifest = policy_revision
    if policy_sha256(manifest) != policy_sha or manifest.get("engine_version") != "v5":
        raise ValueError("engine envelope policy revision is invalid")
    return _policy(
        policy_id=ENGINE_ENVELOPE_POLICY_ID,
        coin_band=band,
        tp=0.0,
        derisk=0.0,
        trail=0.0,
        stop=0.0,
        trail_arm=0.0,
        trail_requires_derisk=False,
        mirror_on_sell=False,
        decision_engine_version="v5",
        decision_policy_sha256=policy_sha,
        decision_policy_manifest=manifest,
    )


def _manual_unmanaged_policy(*, coin_band: str) -> FundedExitPolicy:
    """Manual positions stay open until the operator adds rails or closes them."""
    band = "nano" if coin_band == "assumed_nano" else coin_band
    if band not in ("nano", "micro", "small", "mid", "out_of_scope"):
        raise ValueError(f"coin band is not reservable: {coin_band!r}")
    return _policy(
        policy_id=MANUAL_UNMANAGED_POLICY_ID,
        coin_band=band,
        tp=0.0,
        derisk=0.0,
        derisk_mode="off",
        trail=0.0,
        trail_arm=0.0,
        trail_requires_derisk=False,
        stop=0.0,
        catastrophe=0.0,
        max_hold=0.0,
        unquotable=0.0,
        mirror_on_sell=False,
    )


def follow3050_applies(handle, chain) -> bool:
    """Whether follow-30-50-v1 governs this wallet on this chain. Empty allowlist = never."""
    if not handle:
        return False
    configured = set()
    for entry in cfg.MEMECOIN_FOLLOW3050_WALLETS:
        name, _, net = str(entry).rpartition(":")
        if name and net:
            configured.add(tp_only_wallet_key(name, net))
    return tp_only_wallet_key(handle, chain) in configured


def _follow3050_policy(*, coin_band: str, policy_id="follow-30-50-v1") -> FundedExitPolicy:
    """follow-30-50-v1: a flat take-profit and a flat stop, no trail, no de-risk, no mirror.

    THE NAME IS HISTORICAL, THE KNOBS DECIDE (2026-09-23). The id was minted at +30% / -50%; the
    live values come from MEMECOIN_FOLLOW3050_TP_PCT and MEMECOIN_FOLLOW3050_STOP_PCT and the
    operator retunes them (25 / 35 on 2026-09-23). A replay keyed on the id alone gets the wrong
    answer: read the knobs from the running process, never the name. The id stays so the ledger's
    policy_id history stays comparable; a change of SHAPE (a trail, a de-risk) would need a new id.

    The price confirmation sweep that keeps a wick from selling a healthy coin is NOT in this
    policy. It is `MEMECOIN_REAL_STOP_CONFIRM_SECONDS`, measured against the breach timestamp on
    the position, because a duration cannot be carried in this object: `FundedExitPolicy` rejects
    unknown keys for rollback safety, and the only field available here counts sweeps.

    The catastrophe floor and unquotable timeout stay on cfg defaults. With mirror off and no
    trail, the take-profit, the stop and the catastrophe floor are the only exits.
    """
    band = "nano" if coin_band == "assumed_nano" else coin_band
    # `out_of_scope` is accepted here, unlike every band-parameterised policy, because this one has
    # NO band-dependent value: the take-profit and the stop are flat and the trail and de-risk are
    # zero, so there is nothing to look up per band and nothing to invent.
    # It matters because `_coin_band` hard-codes out_of_scope above $2M, which is a SECOND mcap
    # ceiling independent of MEMECOIN_REAL_MAX_MCAP_USD. With that knob at 0 the band table was
    # still refusing 81% of one armed wallet's buys and 67% of another's (measured 2026-08-13).
    # Admission is still decided by `band_is_reservable` / MEMECOIN_REAL_RESERVABLE_BANDS — this
    # only means that IF the operator admits the band, it has rails to run under.
    if band not in ("nano", "micro", "small", "mid", "out_of_scope"):
        raise ValueError(f"coin band is not reservable: {coin_band!r}")
    return _policy(
        policy_id=policy_id,
        coin_band=band,
        tp=cfg.MEMECOIN_FOLLOW3050_TP_PCT,
        derisk=0.0, trail=0.0, trail_arm=0.0, trail_requires_derisk=False,
        stop=cfg.MEMECOIN_FOLLOW3050_STOP_PCT,
        # debounce stays on the shipped sweep count. A seconds window CANNOT be expressed as a
        # frozen hit count: the runner compares a bare counter with no clock, so 75 "seconds"
        # became 75 DETECTIONS — 75s on the 1s fast lane but 38 MINUTES if the ~31s slow lane
        # decides, on a position with no mirror and no trail. The real window is elapsed time;
        # see stop_breach_confirmed() and MEMECOIN_REAL_STOP_CONFIRM_SECONDS.
        mirror_on_sell=False,
    )


# DEGEN EXIT TRIAL, POLICY V4 (operator YES 2026-10-01; spec
# docs/superpowers/specs/2026-10-01-memecoin-degen-exit-trial-v4-design.md). A NEW policy id: a new
# id parses on a pinned prior release, a new policy key does not; the trial's own record lives in
# contract['source']['exit_trial'], the block that tolerates unknown keys.
EXIT_TRIAL_V4_POLICY_ID = "exit-trial-v4-v1"
EXIT_TRIAL_V4_FAMILY = "exit-trial-v4"


def is_exit_trial_v4(policy) -> bool:
    """True for a frozen policy (or policy id) of the V4 trial family."""
    return policy_family(getattr(policy, "policy_id", policy)) == EXIT_TRIAL_V4_FAMILY


def exit_trial_v4_policy(*, coin_band: str) -> FundedExitPolicy:
    """exit-trial-v4-v1: TP +100, floor (catastrophe) -60, cap 800 s, mirror on (first sell, in the
    two Solana mirror deciders); no stop, no trail, no de-risk. The unquotable timeout and stop
    debounce stay on cfg. No value depends on the band, so every reservable band is accepted."""
    band = "nano" if coin_band == "assumed_nano" else coin_band
    if band not in ("nano", "micro", "small", "mid", "out_of_scope"):
        raise ValueError(f"coin band is not reservable: {coin_band!r}")
    return _policy(
        policy_id=EXIT_TRIAL_V4_POLICY_ID, coin_band=band, tp=cfg.EXIT_TRIAL_V4_TP_PCT,
        derisk=0.0, trail=0.0, trail_arm=0.0, trail_requires_derisk=False, stop=0.0,
        catastrophe=cfg.EXIT_TRIAL_V4_FLOOR_PCT, max_hold=cfg.EXIT_TRIAL_V4_MAX_HOLD_S,
        mirror_on_sell=True)


def _aware(iso):
    from datetime import datetime
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo is not None and t.utcoffset() is not None else None


def exit_trial_v4_applies(*, source_user_id, network_id, decision_at) -> dict | None:
    """The contract's exit_trial record when V4 governs this funded entry, else None: Solana, a
    listed source, decided at or after MEMECOIN_EXIT_TRIAL_V4_FROM. An empty or unparseable cohort
    time (or decision time) is OFF, never on. No I/O: a set lookup and a datetime compare."""
    wallets = cfg.MEMECOIN_EXIT_TRIAL_V4_WALLETS
    if not wallets or not source_user_id or str(source_user_id).lower() not in wallets:
        return None
    if str(network_id or "").lower() != "solana":
        return None
    start, at = _aware(cfg.MEMECOIN_EXIT_TRIAL_V4_FROM), _aware(decision_at)
    if start is None or at is None or at < start:
        return None
    return {"name": "degen-v4", "source_user_id": str(source_user_id),
            "cohort_from": cfg.MEMECOIN_EXIT_TRIAL_V4_FROM}


def tp_only_wallet_key(handle, chain) -> str:
    """The allowlist key for a (wallet, chain) pair. One spelling, used to write and to read."""
    from sniper.services.memecoin_chains import resolve_chain
    return f"{handle}:{resolve_chain(chain) or chain}"


def tp_only_applies(handle, chain) -> bool:
    """Whether tp-only-v1 governs this wallet on this chain. Empty allowlist = never.

    BOTH sides are canonicalised. The chain has two spellings in this system (4663 and robin), and
    matching a raw configured string against a canonical query silently fails closed — the operator
    types one spelling, the policy never applies, and nothing reports it.
    """
    if not handle:
        return False
    configured = set()
    for entry in cfg.MEMECOIN_REAL_TP_ONLY_WALLETS:
        name, _, net = str(entry).rpartition(":")
        if name and net:
            configured.add(tp_only_wallet_key(name, net))
    return tp_only_wallet_key(handle, chain) in configured


def _tp_only_policy(*, coin_band: str) -> FundedExitPolicy:
    """tp-only-v1: a FIXED take-profit, a stop, a hard cap, and NO mirror-on-sell.

    Every other funded policy exits partly by following the source wallet out. This one does not:
    the take-profit, the stop and the cap are the ONLY discretionary exits, which is the whole
    point — it is for a wallet whose entries we want and whose exits we do not.

    That also makes it the policy with the least margin for an exit-engine fault. There is no
    wallet sell to bail out a position if the rails go quiet, so the catastrophe floor, stop
    debounce and unquotable timeout are deliberately left on cfg defaults rather than overridden.

    The parameters are measured, not chosen: see MEMECOIN_REAL_TP_ONLY_* in settings for the
    derivation, its confidence interval, and the reason a LOWER take-profit is not a safer one.
    Applies the same rails to every reservable band — the evidence was not band-split, and
    inventing per-band numbers from n=12 would be fitting.
    """
    band = "nano" if coin_band == "assumed_nano" else coin_band
    if band not in ("nano", "micro", "small", "mid"):
        raise ValueError(f"coin band is not reservable: {coin_band!r}")
    return _policy(
        policy_id="tp-only-v1",
        coin_band=band,
        tp=cfg.MEMECOIN_REAL_TP_ONLY_TP_PCT,
        derisk=0.0,
        trail=0.0,
        trail_arm=0.0,
        trail_requires_derisk=False,
        stop=cfg.MEMECOIN_REAL_TP_ONLY_STOP_PCT,
        max_hold=cfg.MEMECOIN_REAL_TP_ONLY_MAX_HOLD_S,
        mirror_on_sell=False,
    )


def band_is_reservable(coin_band: str, *, signal_source: str | None = None) -> bool:
    """Whether a band may open a NEW funded position — an ADMISSION rule, never an exit rule.

    Exits read the frozen contract via `frozen_policy_from_position`, so a position already open
    in a now-refused band keeps its original rails and closes normally. Do not move this into
    `resolve_funded_policy`: that resolver also builds the policy SHAPE for existing positions.

    `assumed_nano` follows `nano`. A manual operator buy bypasses a configured band floor.
    See `MEMECOIN_REAL_RESERVABLE_BANDS` for the active policy."""
    from sniper.services.memecoin_origin import bypasses_selection
    if bypasses_selection(signal_source):   # manual, or the desk seat (spec 2026-10-21 §1.5)
        return True
    band = "nano" if coin_band == "assumed_nano" else coin_band
    return band in cfg.MEMECOIN_REAL_RESERVABLE_BANDS


def resolve_funded_policy(*, coin_band: str, network_id: str) -> FundedExitPolicy:
    network = m.normalized_coin_network(network_id, "")
    band = "nano" if coin_band == "assumed_nano" else coin_band
    follow_policy_id = (ROBINHOOD_FOLLOW_POLICY_ID if network == "4663"
                        else "follow-30-50-v1")
    if network in cfg.engine_authoritative_chains():
        revision = active_policy_revision()
        if revision is None:
            raise ValueError("engine-authoritative chain has no accepted active V5 policy revision")
        return _engine_envelope_policy(coin_band=coin_band, policy_revision=revision)
    # follow-30-50-v1 for EVERY wallet (2026-08-13 operator), not just the handle allowlist.
    # This is the resolver, so it needs no handle — which is exactly why it belongs here. With
    # MEMECOIN_REAL_ROSTER_GATE_ENABLED=0 an entry can come from a wallet with no memecoin_wallets
    # row at all; the allowlist path reads a handle from that table and would fall through to the
    # band default for every one of them, silently reinstating mirror-on-sell, a trail and a
    # de-risk on the majority of new positions.
    # Applied AFTER the engine check so an engine-authoritative chain keeps its envelope.
    if band == "out_of_scope":
        if (cfg.MEMECOIN_FOLLOW3050_ALL_WALLETS
                or band in cfg.MEMECOIN_REAL_RESERVABLE_BANDS):
            return _follow3050_policy(coin_band=band, policy_id=follow_policy_id)
        raise ValueError(f"coin band is not reservable: {coin_band!r}")
    use_band_policy = band in cfg.MEMECOIN_REAL_BAND_POLICY_BANDS
    if cfg.MEMECOIN_FOLLOW3050_ALL_WALLETS and not use_band_policy:
        return _follow3050_policy(coin_band=coin_band, policy_id=follow_policy_id)
    # tp-only-v1 is NOT applied here. This resolver has no source_user_id, so applying it would
    # make a policy fitted to one wallet global to every wallet on every non-engine chain. It is
    # applied by the contract builder, which knows whose signal it is. See tp_only_wallet_key().
    # v2 (2026-07-23, operator-directed at n=29): the all-or-nothing recover-entry rail at
    # +40/+50 sat above 5 of 10 stopped positions' executable peaks (11984 peaked +48.3% and rode
    # to −66%). First profit rail becomes a FIXED-FRACTION partial at +20% — bank half, ride the
    # rest under the trail. Counterfactual +$85-92 on the 29-close cohort with bounded tail cost:
    # scripts/research/memecoin_partial_derisk_counterfactual.py + the 2026-07-22 proposal doc.
    if coin_band in ("assumed_nano", "nano"):
        return _policy(
            policy_id="funded-nano-v2",
            coin_band="nano",
            tp=0.0,
            derisk=cfg.MEMECOIN_REAL_PARTIAL_DERISK_PCT,
            derisk_mode="fraction",
            derisk_fraction=cfg.MEMECOIN_REAL_PARTIAL_DERISK_FRACTION,
            trail=band_anchor_trail_pct("nano"),
            stop=cfg.MEMECOIN_REAL_NANO_SL_PCT,
            trail_arm=0.0,
            trail_requires_derisk=True,
        )
    if coin_band == "micro":
        return _policy(
            policy_id="funded-micro-v2",
            coin_band="micro",
            tp=0.0,
            derisk=cfg.MEMECOIN_REAL_PARTIAL_DERISK_PCT,
            derisk_mode="fraction",
            derisk_fraction=cfg.MEMECOIN_REAL_PARTIAL_DERISK_FRACTION,
            trail=band_anchor_trail_pct("micro"),
            stop=cfg.MEMECOIN_REAL_MICRO_SL_PCT,
            trail_arm=0.0,
            trail_requires_derisk=True,
        )
    if coin_band == "small":
        return _policy(
            policy_id="funded-small-v1",
            coin_band="small",
            tp=cfg.MEMECOIN_REAL_TP_PCT,
            derisk=cfg.MEMECOIN_REAL_DERISK_PCT,
            trail=band_anchor_trail_pct("small"),
            stop=cfg.real_stop_pct_for_chain(network),
            trail_arm=cfg.MEMECOIN_REAL_TRAIL_ARM_PCT,
            trail_requires_derisk=False,
        )
    if coin_band == "mid":
        return _policy(
            policy_id="funded-mid-v1",
            coin_band="mid",
            tp=0.0,
            derisk=cfg.MEMECOIN_REAL_MID_DERISK_PCT,
            trail=band_anchor_trail_pct("mid"),
            stop=cfg.MEMECOIN_REAL_MID_SL_PCT,
            trail_arm=0.0,
            trail_requires_derisk=True,
        )
    raise ValueError(f"coin band is not reservable: {coin_band!r}")


def engine_policy_runtime_capable(policy: FundedExitPolicy) -> bool:
    """True only while runtime inputs exactly match the position's accepted frozen V5 revision.

    This project keeps one installed engine, not multiple policy implementations. If a deploy or
    config change drifts from an open position's revision, that position loses engine sale authority
    and retains only the immutable envelope rails; it is never silently evaluated under new inputs.
    """
    try:
        active = active_policy_revision()
        if active is None or policy.policy_id != ENGINE_ENVELOPE_POLICY_ID:
            return False
        active_sha, active_manifest = active
        if (policy.decision_policy_sha256 != active_sha
                or policy.decision_policy_manifest != active_manifest):
            return False
        from sniper.services.memecoin_v5_replay_gate import registered_policy_parameters
        return active_manifest.get("registered_parameters") == registered_policy_parameters(cfg)
    except (AttributeError, TypeError, ValueError):
        return False


def _positive_clip(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} clip must be numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} clip must be finite and positive")
    return value


def _floor_for_validation(network_id):
    """The floor the risk context lifted the scaled clip to: the chain's own (operator 2026-10-08)."""
    return cfg.min_trade_floor_usd_for(network_id)


def apply_desk_rails(policy: FundedExitPolicy, desk: dict[str, Any]) -> FundedExitPolicy:
    """The desk seat's rails on top of a band policy, with the 'custom' rule the intent queue uses:
    a percentage rail the seat did not name is OFF (0). The catastrophe floor, debounce, max hold and
    unquotable exit stay the band's. Refuses a policy that owns its own sale decision."""
    if policy.policy_id in (ENGINE_ENVELOPE_POLICY_ID, SOURCE_MIRROR_TP100_POLICY_ID,
                            MANUAL_UNMANAGED_POLICY_ID):
        raise ValueError(f"desk rails cannot replace {policy.policy_id}")
    rails = dict((desk or {}).get("rails") or {})
    if not rails.get("sl_pct"):
        raise ValueError("a desk contract needs the seat's sl_pct")
    unknown = set(rails) - {"tp_pct", "sl_pct", "trail_pct", "derisk_pct"}
    if unknown:
        raise ValueError("desk rails carry unsupported field(s): %s" % ", ".join(sorted(unknown)))
    derisk = _finite_nonnegative("derisk_pct", float(rails.get("derisk_pct") or 0.0))
    if derisk > 0 and policy.derisk_mode == "off":
        raise ValueError("desk derisk_pct needs a band policy with a de-risk mode")
    railed = replace(
        policy,
        tp_pct=_finite_nonnegative("tp_pct", float(rails.get("tp_pct") or 0.0)),
        stop_pct=_finite_nonnegative("sl_pct", float(rails["sl_pct"])),
        trail_pct=_finite_nonnegative("trail_pct", float(rails.get("trail_pct") or 0.0)),
        derisk_pct=derisk,
        trail_arm_pct=0.0,
        trail_requires_derisk=derisk > 0,
        **({"max_hold_s": _finite_nonnegative("time_stop_s", float(desk["time_stop_s"]))} if (desk or {}).get("time_stop_s") else {}),
        # No de-risk named = de-risk OFF, mode included: a fraction mode left on with a 0 trigger
        # writes a contract the exit sweep's own reader refuses.
        **({} if derisk > 0 else {"derisk_mode": "off", "derisk_fraction": 0.0}),
    )
    # Prove the exit sweep can read it back BEFORE it is frozen: an unparseable contract leaves a
    # funded position with no rails at all.
    _validated_policy_data(railed.to_dict())
    return railed


def build_funded_contract(
    *,
    classification: EntryClassification,
    base_clip_usd: float,
    approved_clip_usd: float,
    policy: FundedExitPolicy,
    origin_event_id: str | None,
    copy_arm: dict[str, Any] | None = None,
    voldeath_exit_s: float | None = None,
    exit_override: dict[str, Any] | None = None,
    exit_trial: dict[str, Any] | None = None,
    trial: dict[str, Any] | None = None,
    source_mirror_tp100: dict[str, Any] | None = None,
    desk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(classification, EntryClassification):
        raise ValueError("classification must be EntryClassification")
    _validate_source_mirror_tp100_policy(policy)
    source_profile = policy.policy_id == SOURCE_MIRROR_TP100_POLICY_ID
    if source_profile:
        _validate_source_mirror_tp100_provenance(source_mirror_tp100, classification.network_id)
        if any(value is not None for value in (copy_arm, exit_override, exit_trial, trial)):
            raise ValueError("source mirror TP100 cannot retain another policy owner")
        voldeath_exit_s = 0.0
    elif source_mirror_tp100 is not None:
        raise ValueError("source mirror TP100 provenance requires its policy")
    # Frozen only for the lifted-timecap cohort. Before the lift the rail is env-scoped exactly as
    # it shipped, so contracts stay byte-identical and the interim cohort keeps its env fallback.
    if voldeath_exit_s is None and cfg.MEMECOIN_TIMECAP_LIFT_ENABLED:
        voldeath_exit_s = cfg.MEMECOIN_VOLDEATH_EXIT_S
    voldeath_exit_s = (_finite_nonnegative("voldeath_exit_s", float(voldeath_exit_s))
                       if voldeath_exit_s else 0.0)
    base = _positive_clip("base", base_clip_usd)
    approved = _positive_clip("approved", approved_clip_usd)
    if approved > base:
        raise ValueError("approved clip cannot exceed base clip")
    facts_network = classification.facts.get("network_id")
    facts_mint = classification.facts.get("mint")
    if facts_network is not None and m.normalized_coin_network(facts_network, classification.mint) \
            != m.normalized_coin_network(classification.network_id, classification.mint):
        raise ValueError("classification identity network mismatch")
    if facts_mint is not None and str(facts_mint) != classification.mint:
        raise ValueError("classification identity mint mismatch")
    compatible_band = (
        "nano" if classification.coin_band == "assumed_nano" else classification.coin_band)
    if policy.coin_band != compatible_band:
        raise ValueError("classification band does not match funded policy")
    if (compatible_band == "out_of_scope"
            and "out_of_scope" not in cfg.MEMECOIN_REAL_RESERVABLE_BANDS):
        # The last-line invariant mirrors the admission knob instead of hardcoding the band:
        # with the knob admitting out_of_scope (operator, no-mcap-ceiling regime 2026-08-19),
        # a hardcoded raise here refused every admitted large/unknown-cap entry AFTER
        # band_is_reservable and the follow3050 policy had both accepted it.
        raise ValueError("out_of_scope classification is not reservable")
    scale = _finite_nonnegative("risk_scale", classification.risk_scale, upper=1.0)
    if scale <= 0:
        raise ValueError("risk_scale must be positive")
    if approved > max(base * scale, _floor_for_validation(classification.network_id)) + 1e-9:
        raise ValueError("approved clip exceeds the classification risk scale")
    # AN AUTOMATIC ENTRY MUST CARRY AT LEAST ONE DOWNSIDE RAIL.
    #
    # 56 funded positions opened on 2026-08-14 and 2026-08-17 with stop_pct=0, trail_pct=0 and
    # (for 28 of them) catastrophe_pct=0 — nothing could close a loser. 54 were `follow-30-50-v1`,
    # a policy whose NAME says stop 50, so the policy definition was never the fault: the same id
    # has produced 163 correctly-railed contracts since. It was intermittent within those two days
    # (67%, 75%), i.e. a value sometimes absent at freeze time. Realized -$270, eight positions
    # past -80%, and 45 of 56 exited `manual` — the operator was the stop loss.
    #
    # `_validated_policy_data` could not catch it: it checks each rail with `_finite_nonnegative`,
    # so 0.0 is a legal value for every rail independently and an all-zero contract validates
    # clean. The missing invariant is the CONJUNCTION, and it has to live here.
    #
    # WHY WRITE-TIME AND NOT IN THE VALIDATOR. The validator also runs on READ. Adding it there
    # would make 28 stored contracts unparseable, and the comment above the coin_band check
    # records what that costs: an unparseable contract makes `frozen_policy_from_position` raise,
    # the sweep logs and continues, and the position rides with NO rails — the same outcome by the
    # other route (out_of_scope, found live 2026-08-26 on position 44706). Refusing to CREATE one
    # is safe; refusing to READ one strands the history that documents the bug.
    #
    # MANUAL IS EXEMPT BY DESIGN, not by oversight. `_manual_unmanaged_policy` sets every rail to
    # zero on purpose — "manual positions stay open until the operator adds rails or closes them"
    # — and a manual intent with rails_mode='custom' may deliberately arm none of them. A manual
    # entry is a discretionary pick and the operator owns the exit.
    #
    # The condition is the conjunction of all three. `stop_pct == 0` alone would reject the 15
    # legitimate trail-only positions (stop=0, trail=30) that are net positive.
    if policy.policy_id not in (MANUAL_UNMANAGED_POLICY_ID, SOURCE_MIRROR_TP100_POLICY_ID) and not any(
            float(getattr(policy, rail, 0.0) or 0.0) > 0.0
            for rail in ("stop_pct", "trail_pct", "catastrophe_pct")):
        raise ValueError("funded contract has no downside rail (stop, trail and catastrophe "
                         "are all zero on a non-manual policy)")
    policy_data = policy.to_dict()
    trail_activation = (
        {"mode": "after_derisk", "value": policy.derisk_pct}
        if policy.trail_requires_derisk
        else {"mode": "peak_gain_pct", "value": policy.trail_arm_pct}
    )
    # Intent/de-risk labels must match the ACTUAL mechanism (2026-07-23 review P2). v2 fraction
    # mode sells `derisk_fraction` at the +`derisk_pct` TRIGGER — that banks a fraction of value,
    # NOT principal recovery, and `derisk_pct` is the trigger, not a recovery percent. Only
    # recover_entry mode actually recovers principal. Mislabeling froze a false claim into
    # immutable contracts.
    expectation = {
        "trail_activation": trail_activation,
        "trail_giveback_pct": policy.trail_pct or None,
        "normal_stop_pct": policy.stop_pct or None,
        "catastrophe_stop_pct": policy.catastrophe_pct or None,
        "max_hold_s": policy.max_hold_s,
        "unquotable_exit_s": policy.unquotable_exit_s,
    }
    if source_profile:
        expectation["intent"] = "source_mirror_or_take_profit"
    elif policy.policy_id == MANUAL_UNMANAGED_POLICY_ID:
        expectation["intent"] = "manual_only"
    elif policy.derisk_mode == "fraction" and policy.derisk_fraction and policy.derisk_pct > 0:
        expectation["intent"] = "bank_partial_then_capture_trend"
        expectation["partial_bank_fraction"] = policy.derisk_fraction
        expectation["partial_trigger_pct"] = policy.derisk_pct
        expectation["principal_recovery_mode"] = "fraction"
    elif policy.derisk_pct > 0:
        expectation["intent"] = "recover_principal_then_capture_trend"
        expectation["principal_recovery_pct"] = policy.derisk_pct
        expectation["principal_recovery_mode"] = policy.derisk_mode
    else:
        expectation["intent"] = "capture_trend"
    return {
        "contract_version": FUNDED_CONTRACT_VERSION,
        "engine_authoritative": policy.policy_id == ENGINE_ENVELOPE_POLICY_ID,
        "identity": {
            "network_id": m.normalized_coin_network(
                classification.network_id, classification.mint),
            "mint": classification.mint,
        },
        "source": {
            "signal_source": classification.signal_source,
            "decision_at": classification.decision_at,
            "origin_event_id": origin_event_id,
            # Per-wallet copy arm lives in `source`, NOT in `policy`. `_validated_policy_data`
            # rejects any key it does not know, so an added policy field would make every PINNED
            # PRIOR RELEASE raise on contracts written by this one — i.e. a rollback would leave
            # open funded positions whose exit sweep cannot parse its own policy. `source` is
            # already tolerant of unknown keys (the engine trio is conditional there for the same
            # reason), so a rolled-back runner ignores this and runs the frozen rails as written.
            **({"copy_arm": copy_arm} if copy_arm else {}),
            # The operator's per-wallet TP/stop override (2026-09-27): the overridden NUMBERS are
            # in `policy` (same schema, so a pinned prior release still runs them); who set it and
            # what it replaced lives here, in the unknown-key-tolerant block, for the same reason
            # as copy_arm above.
            **({"exit_override": exit_override} if exit_override else {}),
            **({"source_mirror_tp100": dict(source_mirror_tp100)} if source_profile else {}),
            # THE DESK SEAT (2026-10-21): ticket, the rails it chose, and the band policy they
            # replaced. Unknown-key-tolerant block, same reason as copy_arm above.
            **({"desk": dict(desk)} if desk else {}),
            # The Degen exit trial's record (V4): who, the stamped cohort start, and what it
            # replaced. Same unknown-key-tolerant block, same reason.
            **({"exit_trial": exit_trial} if exit_trial else {}),
            # THE TRIAL MARKER (trial spec section 3): {trial_id, rule, clip_usd} on trial positions
            # only. In `source` for the copy_arm reason above: a pinned prior release ignores it and
            # runs the frozen rails. A non-trial contract stays byte-identical.
            **({"trial": dict(trial)} if trial else {}),
            # FROZEN SILENCE THRESHOLD (2026-08-26). The armed volume-death rail read its 60s from
            # the environment, so an env edit changed the exit rule of positions ALREADY OPEN —
            # exactly what a frozen contract exists to prevent. Freezing it here puts it inside the
            # hashed immutable bytes.
            #
            # IN `source`, NOT `policy`, and that is the whole reason it is here:
            # `_validated_policy_data` rejects any key it does not know, so a new policy field
            # would make every PINNED PRIOR RELEASE raise on contracts written by this one — a
            # rollback would leave open funded positions whose exit sweep cannot read its own
            # rails, across all three funded readers. Measured against release 72322a40: the new
            # policy_id parses, a policy_version bump does not, an added policy key does not.
            # `source` is already tolerant (copy_arm and the engine trio are here for this reason),
            # so a rolled-back runner ignores this key and falls back to the env value — i.e. to
            # today's live behaviour rather than to an outage. The named trade is that this field
            # gets the looser validation of `source`.
            #
            # ONLY THE THRESHOLD IS FROZEN, NEVER WHETHER THE RAIL RUNS. MEMECOIN_VOLDEATH_ARMED
            # stays a live kill switch: an operator must be able to stop a rail on open positions.
            # Freezing "armed" would make a position opened under the rail impossible to disarm.
            **({"voldeath_exit_s": voldeath_exit_s} if voldeath_exit_s else {}),
            **({
                "decision_engine_version": policy.decision_engine_version,
                "decision_policy_sha256": policy.decision_policy_sha256,
                "decision_policy_manifest": policy.decision_policy_manifest,
            } if policy.policy_id == ENGINE_ENVELOPE_POLICY_ID else {}),
        },
        "classification": classification.to_dict(),
        "sizing": {
            "base_clip_usd": base,
            "risk_scale": scale,
            "approved_clip_usd": approved,
        },
        "policy": policy_data,
        "expectation": expectation,
        "invalidations": (["mirror_sell", "take_profit", "manual_override", "source_integrity"]
                          if source_profile else
                          ["manual_override"] if policy.policy_id == MANUAL_UNMANAGED_POLICY_ID else [
            "post_peak_confirmed",
            "mirror_sell",
            "stop",
            "catastrophe",
            "trail",
            "timecap",
            "unquotable",
            # A stall-armed copy arm can close this position on a rule none of the other entries
            # describe. The contract states what may close it, so the rail has to appear here or
            # the contract understates its own exits.
            *(["peak_stall"] if (copy_arm or {}).get("stall") else []),
            "manual_override",
        ]),
    }


def canonical_funded_contract(contract):
    return m.canonical_funded_json(contract)


def _validated_policy_data(value):
    if not isinstance(value, dict):
        raise ValueError("funded contract policy is missing")
    expected = set(FundedExitPolicy.__dataclass_fields__) - {
        "decision_engine_version", "decision_policy_sha256", "decision_policy_manifest"}
    # Back-compat: contracts predating partial de-risk can lack derisk_fraction. Decision version
    # deliberately does not live in this strict object; the prior pinned reader rejects additions.
    missing = expected - set(value)
    if set(value) - expected or not missing <= {"derisk_fraction"}:
        raise ValueError("funded contract policy fields do not match runtime schema")
    if missing:
        value = dict(value)
        value.setdefault("derisk_fraction", 0.0)
    try:
        policy = FundedExitPolicy(**value)
    except TypeError as exc:
        raise ValueError("funded contract policy is invalid") from exc
    if (isinstance(policy.policy_version, bool) or not isinstance(policy.policy_version, int)
            or policy.policy_version != FUNDED_POLICY_VERSION):
        raise ValueError("funded contract policy version is unsupported")
    if not isinstance(policy.policy_id, str) or not policy.policy_id.strip():
        raise ValueError("funded contract policy id is invalid")
    # "out_of_scope" IS A VALID BAND THE WRITER WRITES (4 real contracts carry it): a coin whose
    # mcap falls outside every band range is admitted under follow-30-50 per doctrine. Rejecting it
    # here made every such position UNPARSEABLE BY ITS OWN EXIT SWEEP — frozen_policy_from_position
    # raised, the sweep logged and continued, and the position rode with NO rails at all. All three
    # closed out_of_scope positions in history closed `manual`: the operator was the stop loss.
    # Found live 2026-08-26 (position 44703, 18 min past its frozen 780s timecap, operator report
    # "TP and SL aren't working"). The validator's job is the writer's actual domain, not the
    # subset that felt canonical.
    if policy.coin_band not in ("nano", "micro", "small", "mid", "out_of_scope"):
        raise ValueError("funded contract policy coin band is invalid")
    for name in (
        "tp_pct", "derisk_pct", "trail_pct", "trail_arm_pct", "stop_pct",
        "catastrophe_pct", "max_hold_s", "unquotable_exit_s",
    ):
        upper = 100.0 if name in ("trail_pct", "stop_pct", "catastrophe_pct") else None
        _finite_nonnegative(name, getattr(policy, name), upper=upper)
    _validate_source_mirror_tp100_policy(policy)
    if policy.max_hold_s <= 0 and policy.policy_id not in (
            MANUAL_UNMANAGED_POLICY_ID, SOURCE_MIRROR_TP100_POLICY_ID):
        raise ValueError("funded contract max_hold_s must be positive")
    if (isinstance(policy.stop_debounce_sweeps, bool)
            or not isinstance(policy.stop_debounce_sweeps, int)
            or policy.stop_debounce_sweeps < 1):
        raise ValueError("funded contract stop debounce is invalid")
    if not isinstance(policy.trail_requires_derisk, bool):
        raise ValueError("funded contract trail activation is invalid")
    if not isinstance(policy.mirror_on_sell, bool):
        raise ValueError("funded contract mirror policy is invalid")
    if policy.derisk_mode not in ("off", "recover_entry", "fraction"):
        raise ValueError("funded contract derisk mode is invalid")
    _finite_nonnegative("derisk_fraction", policy.derisk_fraction, upper=1.0)
    if policy.derisk_mode == "fraction":
        if not (0.0 < policy.derisk_fraction < 0.9):
            raise ValueError("funded contract fraction de-risk is out of bounds")
        if policy.derisk_pct <= 0:
            raise ValueError("funded contract fraction de-risk has no trigger")
    elif policy.derisk_fraction != 0.0:
        raise ValueError("funded contract derisk_fraction is only valid in fraction mode")
    if policy.trail_requires_derisk and policy.derisk_pct <= 0:
        raise ValueError("funded contract post-derisk trail has no derisk threshold")
    return policy


def _verified_contract(pos: dict) -> dict | None:
    """The position's authenticated contract dict, or None when it has none (legacy row).

    One place decides whether a contract's bytes are genuine, so the policy and anything else read
    out of it (the copy arm and its stall rule) can never come from differently-trusted bytes.
    """
    keys = ("funded_contract_version", "funded_contract_hash", "funded_contract_json")
    values = tuple(pos.get(key) for key in keys)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("funded contract fields are incomplete")
    version, expected_hash, payload_json = values
    if (isinstance(version, bool) or not isinstance(version, int)
            or version != FUNDED_CONTRACT_VERSION):
        raise ValueError("funded contract version is unsupported")
    try:
        contract = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("funded contract JSON is invalid") from exc
    canonical, actual_hash = m.canonical_funded_json(contract)
    if canonical != payload_json or actual_hash != str(expected_hash).lower():
        raise ValueError("funded contract hash does not match canonical JSON")
    if contract.get("contract_version") != version:
        raise ValueError("funded contract version does not match JSON")
    return contract


def copy_arm_from_position(pos: dict) -> dict | None:
    """The frozen per-wallet copy-arm selection, or None. Authenticated like the policy."""
    try:
        contract = _verified_contract(pos)
    except ValueError:
        return None
    if not contract:
        return None
    source = contract.get("source")
    arm = source.get("copy_arm") if isinstance(source, dict) else None
    return arm if isinstance(arm, dict) else None


def frozen_voldeath_exit_s(pos: dict) -> float | None:
    """The position's own frozen silence threshold, or None when it has none.

    None means LEGACY, never malformed: contracts written before the timecap lift simply do not
    carry the key, and the caller falls back to the env value — which is the behaviour those
    positions were opened under. A contract that cannot be authenticated also reads None rather
    than raising, matching `copy_arm_from_position`: this is a threshold lookup on the exit path,
    and a parse fault here must not be able to stop an exit sweep. The authoritative parse still
    happens in `frozen_policy_from_position`, which does raise.
    """
    try:
        contract = _verified_contract(pos)
    except ValueError:
        return None
    if not contract:
        return None
    source = contract.get("source")
    value = source.get("voldeath_exit_s") if isinstance(source, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def voldeath_exit_s(pos: dict, env_exit_s: float) -> float:
    """The armed volume-death silence threshold for one position: its own frozen value wins, and the
    env value is only the FALLBACK for a legacy (pre-lift) contract that carries none. An env edit
    therefore never changes the exit rule of a position already open."""
    frozen = frozen_voldeath_exit_s(pos)
    return frozen if frozen is not None else env_exit_s


CONTRACT_UNPARSEABLE = object()


def frozen_policy_or_unparseable(pos):
    """Return a distinct result for an invalid frozen contract."""
    try:
        return frozen_policy_from_position(pos)
    except ValueError:
        return CONTRACT_UNPARSEABLE


def frozen_policy_from_position(pos: dict) -> FundedExitPolicy | None:
    """Parse and validate a position's immutable policy; all-null means legacy."""
    contract = _verified_contract(pos)
    if contract is None:
        return None
    identity = contract.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("funded contract identity is missing")
    contract_network = m.normalized_coin_network(identity.get("network_id"), identity.get("mint"))
    position_network = m.normalized_coin_network(pos.get("network_id"), pos.get("mint"))
    if contract_network != position_network or str(identity.get("mint")) != str(pos.get("mint")):
        raise ValueError("funded contract identity does not match position")
    policy = _validated_policy_data(contract.get("policy"))
    source = contract.get("source")
    if policy.policy_id == SOURCE_MIRROR_TP100_POLICY_ID:
        if (contract.get("engine_authoritative") is not False
                or not isinstance(source, dict) or not pos.get("source_user_id")
                or any(key in source for key in (
                    "copy_arm", "exit_override", "exit_trial", "trial", "voldeath_exit_s",
                    "decision_engine_version", "decision_policy_sha256", "decision_policy_manifest"))):
            raise ValueError("source mirror TP100 cannot retain another policy owner")
        _validate_source_mirror_tp100_provenance(
            source.get("source_mirror_tp100"), position_network, pos["source_user_id"])
    elif isinstance(source, dict) and "source_mirror_tp100" in source:
        raise ValueError("source mirror TP100 provenance requires its policy")
    selected = source.get("decision_engine_version") if isinstance(source, dict) else None
    selected_policy_sha = source.get("decision_policy_sha256") if isinstance(source, dict) else None
    selected_manifest = source.get("decision_policy_manifest") if isinstance(source, dict) else None
    if policy.policy_id == ENGINE_ENVELOPE_POLICY_ID:
        # V5-only (2026-07-25): refuse to open an engine-envelope contract whose frozen decision
        # version is not v5. Never silently upgrade a retired (v2/v3) or absent version to v5.
        if selected != "v5":
            raise ValueError(
                "engine funded contract decision version is not v5 (v2/v3 retired); "
                f"got {selected!r}")
        # Contracts predating WO-13 remain parseable for recovery, but their absent policy identity
        # gives them no engine sale authority. New contracts always carry both fields.
        if selected_policy_sha is None and selected_manifest is None:
            return replace(policy, decision_engine_version="v5")
        if selected_policy_sha is None or not isinstance(selected_manifest, dict):
            raise ValueError("engine funded contract policy identity is incomplete")
        try:
            PolicyRegistry._validate_manifest(selected_manifest)
        except (TypeError, ValueError) as exc:
            raise ValueError("engine funded contract policy manifest is invalid") from exc
        if selected_manifest.get("engine_version") != "v5":
            raise ValueError("engine funded contract policy manifest is not v5")
        if policy_sha256(selected_manifest) != selected_policy_sha:
            raise ValueError("engine funded contract policy hash does not match manifest")
        return replace(
            policy, decision_engine_version="v5",
            decision_policy_sha256=selected_policy_sha,
            decision_policy_manifest=selected_manifest)
    if selected is not None or selected_policy_sha is not None or selected_manifest is not None:
        raise ValueError("non-engine funded contract cannot select a decision engine")
    return policy


def is_source_mirror_tp100_position(pos):
    """Only the authenticated frozen contract grants profile authority, never current config."""
    if not isinstance(pos, dict):
        return False
    try:
        policy = frozen_policy_from_position(pos)
    except ValueError:
        return False
    return policy is not None and policy.policy_id == SOURCE_MIRROR_TP100_POLICY_ID


def effective_funded_policy(
        pos: dict, *, override: dict | None) -> tuple[FundedExitPolicy | None, dict]:
    """Return the immutable policy and an in-memory operator overlay."""
    original = frozen_policy_from_position(pos)
    if original is None:
        return None, {}
    effective = original.to_dict()
    ov = override or {}
    bare_trail = (ov.get("trail_pct") is not None and ov.get("tp_pct") is None
                  and ov.get("tp_target_mcap") is None)
    if ov.get("tp_pct") is not None:
        effective["tp_pct"] = float(ov["tp_pct"])
    elif bare_trail:
        effective["tp_pct"] = 0.0
    if ov.get("sl_pct") is not None:
        effective["stop_pct"] = abs(float(ov["sl_pct"]))
    if ov.get("trail_pct") is not None:
        effective["trail_pct"] = float(ov["trail_pct"])
    if ov.get("derisk_pct") is not None:
        effective["derisk_pct"] = float(ov["derisk_pct"])
    elif ov.get("trail_pct") is not None:
        effective["derisk_pct"] = 0.0
    if ov.get("trail_pct") is not None:
        effective["trail_requires_derisk"] = effective["derisk_pct"] > 0
        effective["trail_arm_pct"] = 0.0
    effective["tp_target_mcap"] = ov.get("tp_target_mcap")
    effective["sl_target_mcap"] = ov.get("sl_target_mcap")
    return original, effective
