"""The current exit rails, expressed as data. resolve_policy() is a pure, versioned decision function.
v1 returns today's fixed rails for every snapshot — the adaptive branches arrive in SP#3. This object
is the ONE definition of rails that the runner AND the replay harness both consume (no drift)."""
from __future__ import annotations
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from sniper.settings import memecoin as S


@dataclass(frozen=True)
class ExitPolicy:
    tp_pct: float
    stop_pct: float                 # negative
    stop_debounce_sweeps: int
    derisk_trigger_pct: float | None
    derisk_fraction: float
    trail_mode: str
    max_hold_s: float
    mirror_sell_mode: str


# Launch guesses for a forward PAPER experiment, not fitted production parameters.
# (mcap floor, policy-id band, TP %, stop %, max hold seconds)
_SCALP_BANDS = (
    (30_000_000.0, "ge30m", 12.0, -20.0, 2 * 3600.0),
    (5_000_000.0, "5m_30m", 25.0, -30.0, 6 * 3600.0),
    (1_000_000.0, "1m_5m", 40.0, -40.0, 12 * 3600.0),
    (0.0, "lt1m", 60.0, -50.0, float("inf")),
)


def _positive_finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 and math.isfinite(value) else None


def _scalp_policy(entry_mcap):
    """Resolve the experimental paper rail. Unknown stays unknown and gets the fixed fallback."""
    mcap = _positive_finite(entry_mcap)
    if mcap is None:
        return ExitPolicy(
            tp_pct=float(S.MEMECOIN_TAKE_PROFIT_PCT),
            stop_pct=-abs(float(S.MEMECOIN_STOP_LOSS_PCT)),
            stop_debounce_sweeps=1, derisk_trigger_pct=None, derisk_fraction=0.0,
            trail_mode="off", max_hold_s=float("inf"), mirror_sell_mode="on",
        ), "scalp_c_fallback_v1"
    for floor, band, tp, stop, hold in _SCALP_BANDS:
        if mcap >= floor:
            return ExitPolicy(
                tp_pct=tp, stop_pct=stop, stop_debounce_sweeps=1,
                derisk_trigger_pct=None, derisk_fraction=0.0, trail_mode="off",
                max_hold_s=hold, mirror_sell_mode="on",
            ), f"scalp_c_{band}_v1"
    raise AssertionError("scalp bands must end at floor zero")


# scalp_c2 — the CORRECTED conditioned arm. scalp_c (v1) confounded three changes and its tight stop
# + hard time cap buried the signal (-$921 vs control +$1,739 on the same entries; the timecap alone
# force-closed coins control rode back to positive). scalp_c2 changes ONLY the take-profit — small
# for big caps (which reach +10% ~19% of the time and NEVER +40/+60), riding for sub-$1M (the +100%
# tail) — and otherwise copies CONTROL exactly: wide -60% stop, NO time cap, mirror the trader's sell
# (the only rail that actually made money). If it beats control, conditioning the TP is real edge; if
# not, the fixed rail wins and per-token TP chasing stops. Bands: (mcap_floor, band, tp_pct).
_SCALP_C2_TP = (
    (30_000_000.0, "ge30m", 10.0),
    (5_000_000.0, "5m_30m", 20.0),
    (1_000_000.0, "1m_5m", 35.0),
    (0.0, "lt1m", float(S.MEMECOIN_TAKE_PROFIT_PCT)),   # ride, exactly like control
)


def _scalp_c2_policy(entry_mcap):
    """Conditioned TP + control's structure. Unknown mcap -> the fixed control rail (never a scalp:
    treating 'unknown' as 'big' is the bug class that dropped sub-$1M winners on feed gaps)."""
    stop = -abs(float(S.MEMECOIN_STOP_LOSS_PCT))       # wide, same as control
    mcap = _positive_finite(entry_mcap)
    if mcap is None:
        return ExitPolicy(
            tp_pct=float(S.MEMECOIN_TAKE_PROFIT_PCT), stop_pct=stop, stop_debounce_sweeps=1,
            derisk_trigger_pct=None, derisk_fraction=0.0, trail_mode="off",
            max_hold_s=float("inf"), mirror_sell_mode="on"), "scalp_c2_fallback_v1"
    for floor, band, tp in _SCALP_C2_TP:
        if mcap >= floor:
            return ExitPolicy(
                tp_pct=tp, stop_pct=stop, stop_debounce_sweeps=1, derisk_trigger_pct=None,
                derisk_fraction=0.0, trail_mode="off", max_hold_s=float("inf"),
                mirror_sell_mode="on"), f"scalp_c2_{band}_v1"
    raise AssertionError("scalp_c2 bands must end at floor zero")


def resolve_policy(snapshot: dict, *, arm: str):
    if arm == "scalp_c":
        return _scalp_policy((snapshot or {}).get("entry_mcap"))
    if arm == "scalp_c2":
        return _scalp_c2_policy((snapshot or {}).get("entry_mcap"))
    if arm in ("alpha_b", "real_shadow"):
        pol = ExitPolicy(
            tp_pct=float(S.MEMECOIN_REAL_TP_PCT), stop_pct=-abs(float(S.MEMECOIN_B_STOP_PCT)),
            stop_debounce_sweeps=int(S.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS),
            derisk_trigger_pct=(float(S.MEMECOIN_REAL_DERISK_PCT) or None),
            derisk_fraction=0.5, trail_mode="off",
            max_hold_s=float(getattr(S, "MEMECOIN_B_MAX_HOLD_S", 43200)), mirror_sell_mode="on")
        return pol, "fixed_real_v1"
    pol = ExitPolicy(
        tp_pct=float(S.MEMECOIN_TAKE_PROFIT_PCT), stop_pct=-abs(float(S.MEMECOIN_STOP_LOSS_PCT)),
        stop_debounce_sweeps=1, derisk_trigger_pct=None, derisk_fraction=0.0, trail_mode="off",
        max_hold_s=float("inf"), mirror_sell_mode="off")
    return pol, "fixed_control_v1"


def _validated_policy_data(policy: ExitPolicy) -> dict:
    data = asdict(policy)
    for field in ("tp_pct", "stop_pct", "derisk_fraction"):
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)):
            raise ValueError(f"{field} must be finite")
        data[field] = float(value)
    if data["tp_pct"] < 0:
        raise ValueError("tp_pct must be nonnegative")
    if data["stop_pct"] >= 0:
        raise ValueError("stop_pct must be negative")
    if not 0.0 <= data["derisk_fraction"] <= 1.0:
        raise ValueError("derisk_fraction must be between zero and one")
    debounce = data["stop_debounce_sweeps"]
    if isinstance(debounce, bool) or not isinstance(debounce, int) or debounce < 1:
        raise ValueError("stop_debounce_sweeps must be a positive integer")
    trigger = data["derisk_trigger_pct"]
    if trigger is not None:
        if isinstance(trigger, bool) or not isinstance(trigger, (int, float)) \
                or not math.isfinite(float(trigger)) or float(trigger) <= 0:
            raise ValueError("derisk_trigger_pct must be positive finite or null")
        data["derisk_trigger_pct"] = float(trigger)
    hold = data["max_hold_s"]
    if hold is None or (isinstance(hold, (int, float)) and math.isinf(float(hold))):
        data["max_hold_s"] = None
    elif (isinstance(hold, bool) or not isinstance(hold, (int, float))
          or not math.isfinite(float(hold)) or float(hold) <= 0):
        raise ValueError("max_hold_s must be positive finite or null")
    else:
        data["max_hold_s"] = float(hold)
    if data["trail_mode"] not in ("off", "on"):
        raise ValueError("trail_mode must be 'off' or 'on'")
    if data["mirror_sell_mode"] not in ("off", "on"):
        raise ValueError("mirror_sell_mode must be 'off' or 'on'")
    return data


def serialize_policy(policy: ExitPolicy) -> str:
    """Canonical strict JSON. A null max hold means no time cap."""
    return json.dumps(_validated_policy_data(policy), sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def deserialize_policy(payload: str) -> ExitPolicy:
    try:
        data = json.loads(payload)
        if isinstance(data, dict) and "exit_policy" in data:
            data = data["exit_policy"]
        if not isinstance(data, dict) or set(data) != set(ExitPolicy.__dataclass_fields__):
            raise ValueError("field set mismatch")
        hold = data.get("max_hold_s")
        candidate = ExitPolicy(**{**data, "max_hold_s": float("inf") if hold is None else hold})
        _validated_policy_data(candidate)
        return candidate
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid policy JSON: {exc}") from exc


def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _paper_policy_assignment(policy_id: str, payload: dict) -> tuple[str, int, str]:
    """Return a content-addressed immutable paper policy identity."""
    canonical = _canonical_json(payload)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"{policy_id}@{digest}", 1, canonical


def runtime_release_sha() -> str:
    """Pinned release identity without a subprocess or mutable checkout lookup."""
    for parent in Path(__file__).resolve().parents:
        if parent.parent.name == "releases" and re.fullmatch(r"[0-9a-f]{40}", parent.name):
            return parent.name
    try:
        from sniper.util.state_paths import state_path
        value = json.loads(state_path("data", "memecoin_deploy.json").read_text()).get("sha")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value):
            return value
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return "unversioned"


def _paper_common_params(arm=None) -> dict:
    from sniper.services.memecoin_paper_ticks import engine_params
    return {
        # THE EXIT ENGINE, frozen at open (spec 2026-10-08 paper exit ticks): present only when
        # this arm opens under onchain_ticks_v1, so a v0 assignment's bytes and hash are unchanged.
        **engine_params(arm),
        "paper_max_hold_s": float(S.MEMECOIN_PAPER_MAX_HOLD_D) * 86400.0,
        "paper_unquotable_s": float(S.MEMECOIN_PAPER_UNQUOTABLE_S),
        # FROZEN AT OPEN, the way the funded contract freezes it (2026-09-24). A funded position
        # carries its own `voldeath_exit_s` in its contract, so an env edit cannot change the exit
        # rule of a position already open — `frozen_voldeath_exit_s` reads it back. A paper row has
        # no funded contract, so without this it would read the live env at every sweep and a knob
        # move mid-hold would judge it under a rule it was never opened under.
        "voldeath_exit_s": float(getattr(S, "MEMECOIN_VOLDEATH_EXIT_S", 0.0) or 0.0),
        # The size-aware mirror's threshold, frozen for the same reason: funded requires a
        # source sell of at least this fraction of the wallet's bag before it mirrors, and a
        # knob move must not rewrite the exit rule of a position already open.
        "mirror_size_min_fraction": float(
            getattr(S, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.0) or 0.0),
    }


def _profile_matched_assignment(conn, *, entry_mcap, network_id, source_user_id, opened_at):
    """The hypothesis arm: a take-profit fitted from this wallet's own prior route-space peaks.

    Registered rule: docs/superpowers/specs/2026-08-29-profile-matched-v1-fitting-addendum.md

    TWO AUTHORITIES, AND THE BOUNDARY IS THE WHOLE CONTRACT (A5). The frozen artifact owns the
    FITTED knob — the take-profit, and nothing else in v1. The funded resolver owns every
    INHERITED one — the stop above all, resolved LIVE at decision time and never read from the
    file. Freezing the stop would reintroduce exactly the staleness `funded_clone` exists to
    prevent: the baseline would track funded while the treatment drifted away from it, and the
    contrast would slowly become a comparison of two different stops.

    `stop_at_fit` rides along for audit only. A divergence from the live stop is recorded on the
    assignment and does NOT block, because a drifting stop is a fact the study's readers need and
    suppressing it would hide a change in the comparison itself.

    NO PROFILE MEANS funded_clone, NOT THE BAND DEFAULT (A7). Under the live all-wallets regime the
    band default is unreachable on funded, so falling back to it would put this arm on rails no
    funded position runs — and the study would measure a configuration difference, which is the
    error the parent document was written to remove.
    """
    from sniper.services.memecoin_profile_fit import profile_for

    art = profile_for(source_user_id, network_id, opened_at=opened_at)
    if art is None:
        policy_id, version, payload = _funded_clone_assignment(
            conn, entry_mcap=entry_mcap, network_id=network_id, source_user_id=source_user_id)
        body = json.loads(payload)
        body["strategy"] = "profile_matched_v1"
        body["profile"] = {"fallback": True,
                           "reason": "no fitted profile for this (wallet, chain)"}
        return _paper_policy_assignment("profile-matched-v1-fallback", body)

    # The inherited rails come from the SAME resolver the funded builder uses, so the treatment
    # differs from the baseline in exactly one place: the take-profit.
    from sniper.services.memecoin_market_state import _coin_band
    from sniper.services.memecoin_risk import funded_policy_for_origin
    policy, copy_arm = funded_policy_for_origin(
        conn, coin_band=_coin_band(entry_mcap, False), network_id=network_id,
        source_user_id=source_user_id, entry_mcap=entry_mcap)
    inherited = policy.to_dict()
    fitted = dict(inherited)
    fitted["tp_pct"] = float(art["tp_pct"])
    return _paper_policy_assignment("profile-matched-v1", {
        "schema_version": 1, "strategy": "profile_matched_v1",
        "exit_policy": fitted,
        "profile": {
            "fallback": False,
            "fit_window_end": art["fit_window_end"],
            "peak_space": art["peak_space"],
            "n_observed": art["n_observed"], "n_censored": art["n_censored"],
            "shrinkage_weight": art["shrinkage_weight"],
            "shrinkage_ratio": art["shrinkage_ratio"],
            "dataset_sha256": art["dataset_sha256"],
            # AUDIT ONLY. Recorded, compared, never applied.
            "stop_at_fit": art.get("stop_at_fit"),
            "stop_live": inherited.get("stop_pct"),
            "stop_diverged": (art.get("stop_at_fit") is not None
                              and abs(float(art["stop_at_fit"])
                                      - float(inherited.get("stop_pct") or 0.0)) > 1e-9),
        },
        "params": {**_paper_common_params("profile_matched_v1"),
                   "mirror_on_sell": policy.mirror_on_sell},
    })


def _funded_clone_assignment(conn, *, entry_mcap, network_id, source_user_id):
    """Freeze, for a paper position, the rails a FUNDED entry from this origin would carry.

    THE POINT OF THE ARM. Every legacy paper arm differs from funded on every rail — the control
    arm runs TP 500 / stop 60 / mirror-on-sell against funded's 30 / 50 / no mirror. A baseline
    carrying those rails measures the POLICY DIFFERENCE between paper and funded and reports it
    as a translation gap. This arm removes that term by running the funded rails on paper, so a
    remaining difference is execution and selection rather than configuration.

    IT RESOLVES, IT DOES NOT RESTATE. `funded_policy_for_origin` is the same function the funded
    contract builder calls, in the same order, with the same per-wallet override and the same
    copy arm. A knob moved in settings moves both sides together. Copying the numbers here would
    have made the control drift the first time anyone changed one, and drift silently, because
    nothing compares two transcriptions.

    THE BAND IS DERIVED THE FUNDED WAY. `_coin_band` is imported rather than reimplemented for
    the same reason: its thresholds are the ones admission uses, and a second copy of 50k/250k/1M
    is a second thing to forget.

    Falls back to the band default when there is no connection or no wallet — a scout event has
    no `memecoin_wallets` row, and the funded path treats that the same way.
    """
    from sniper.services.memecoin_market_state import _coin_band
    from sniper.services.memecoin_risk import funded_policy_for_origin

    band = _coin_band(entry_mcap, False)
    policy, copy_arm = funded_policy_for_origin(
        conn, coin_band=band, network_id=network_id, source_user_id=source_user_id,
        entry_mcap=entry_mcap)
    # `mirror_on_sell` comes off the resolved policy, never a literal. follow-30-50-v1 turns it
    # off and the band defaults leave it on, so hard-coding either value here would silently
    # break the clone for every origin governed by the other.
    return _paper_policy_assignment(policy.policy_id, {
        "schema_version": 1, "strategy": "funded_clone",
        "exit_policy": policy.to_dict(),
        "copy_arm": (copy_arm or {}).get("arm"),
        "params": {**_paper_common_params("funded_clone"), "mirror_on_sell": policy.mirror_on_sell},
    })


def resolve_paper_arm_policy(arm: str, *, entry_mcap=None, network_id=None,
                             opened_at=None, conn=None,
                             source_user_id=None) -> tuple[str, int, str]:
    """Freeze exactly the settings consumed by one legacy paper arm."""
    if arm == "profile_matched_v1":
        return _profile_matched_assignment(
            conn, entry_mcap=entry_mcap, network_id=network_id,
            source_user_id=source_user_id, opened_at=opened_at)
    if arm == "funded_clone":
        return _funded_clone_assignment(
            conn, entry_mcap=entry_mcap, network_id=network_id, source_user_id=source_user_id)
    if arm in ("scalp_c", "scalp_c2"):
        resolved, policy_id = resolve_policy({"entry_mcap": entry_mcap}, arm=arm)
        return _paper_policy_assignment(policy_id, {
            "schema_version": 1, "strategy": arm,
            "exit_policy": json.loads(serialize_policy(resolved)),
            "params": {**_paper_common_params(), "mirror_on_sell": True},
        })
    common = _paper_common_params()
    params = {
        "control": {
            **common, "stop_pct": float(S.MEMECOIN_STOP_LOSS_PCT),
            "tp_pct": float(S.MEMECOIN_TAKE_PROFIT_PCT), "mirror_on_sell": True,
        },
        "momentum_a": {
            **common, "rollover_sweeps": int(S.MEMECOIN_A_ROLLOVER_SWEEPS),
            "trail_pct": float(S.MEMECOIN_A_TRAIL_PCT),
            "stop_pct": float(S.MEMECOIN_A_STOP_PCT),
            "max_hold_s": float(S.MEMECOIN_A_MAX_HOLD_S), "mirror_on_sell": False,
        },
        "alpha_b": {
            **common, "stop_pct": float(S.MEMECOIN_B_STOP_PCT),
            "max_hold_s": float(S.MEMECOIN_B_MAX_HOLD_S), "mirror_on_sell": True,
        },
        "trail_d": {
            **common, "activate_pct": float(S.MEMECOIN_TRAIL_D_ACTIVATE_PCT),
            "trail_pct": float(S.MEMECOIN_TRAIL_D_TRAIL_PCT),
            "stop_pct": float(S.MEMECOIN_TRAIL_D_STOP_PCT),
            "max_hold_s": float(S.MEMECOIN_TRAIL_D_MAX_HOLD_S), "mirror_on_sell": True,
        },
        "peak_lock_e": {
            **common, "activate_pct": float(S.MEMECOIN_PEAK_E_ACTIVATE_PCT),
            "stall_sweeps": int(S.MEMECOIN_PEAK_E_STALL_SWEEPS),
            "stop_pct": float(S.MEMECOIN_PEAK_E_STOP_PCT),
            "max_hold_s": float(S.MEMECOIN_PEAK_E_MAX_HOLD_S), "mirror_on_sell": True,
        },
        "combo_f": {
            **common, "trigger_pct": float(S.MEMECOIN_COMBO_F_TRIGGER_PCT),
            "fraction": float(S.MEMECOIN_COMBO_F_FRACTION),
            "trail_pct": float(S.MEMECOIN_COMBO_F_TRAIL_PCT),
            "stop_pct": float(S.MEMECOIN_COMBO_F_STOP_PCT),
            "max_hold_s": float(S.MEMECOIN_COMBO_F_MAX_HOLD_S), "mirror_on_sell": True,
        },
        "real_shadow": {
            **common, "tp_pct": float(S.MEMECOIN_REAL_TP_PCT),
            "derisk_pct": float(S.MEMECOIN_REAL_DERISK_PCT),
            "trail_pct": float(S.MEMECOIN_REAL_TRAIL_PCT),
            "trail_arm_pct": float(S.MEMECOIN_REAL_TRAIL_ARM_PCT),
            "stop_pct": float(S.real_stop_pct_for_chain(network_id)),
            "catastrophe_pct": float(S.MEMECOIN_REAL_CATASTROPHE_PCT),
            "stop_debounce_sweeps": int(S.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS),
            "max_hold_s": float(S.MEMECOIN_REAL_MAX_HOLD_S),
            "unquotable_exit_s": float(S.MEMECOIN_REAL_UNQUOTABLE_EXIT_S),
            "mirror_on_sell": True,
        },
    }.get(arm)
    if params is None:
        raise ValueError(f"unknown paper arm: {arm!r}")
    policy_id = f"{arm}-paper-v1"
    return _paper_policy_assignment(policy_id, {
        "schema_version": 1, "strategy": arm, "params": params,
    })


_PAPER_REQUIRED_PARAMS = {
    "control": {"paper_max_hold_s", "paper_unquotable_s", "stop_pct", "tp_pct",
                "mirror_on_sell"},
    "momentum_a": {"paper_max_hold_s", "paper_unquotable_s", "rollover_sweeps",
                   "trail_pct", "stop_pct", "max_hold_s", "mirror_on_sell"},
    "alpha_b": {"paper_max_hold_s", "paper_unquotable_s", "stop_pct", "max_hold_s",
                "mirror_on_sell"},
    "trail_d": {"paper_max_hold_s", "paper_unquotable_s", "activate_pct", "trail_pct",
                "stop_pct", "max_hold_s", "mirror_on_sell"},
    "peak_lock_e": {"paper_max_hold_s", "paper_unquotable_s", "activate_pct",
                    "stall_sweeps", "stop_pct", "max_hold_s", "mirror_on_sell"},
    "combo_f": {"paper_max_hold_s", "paper_unquotable_s", "trigger_pct", "fraction",
                "trail_pct", "stop_pct", "max_hold_s", "mirror_on_sell"},
    "real_shadow": {"paper_max_hold_s", "paper_unquotable_s", "tp_pct", "derisk_pct",
                    "trail_pct", "trail_arm_pct", "stop_pct", "catastrophe_pct",
                    "stop_debounce_sweeps", "max_hold_s", "unquotable_exit_s",
                    "mirror_on_sell"},
    "funded_clone": {"paper_max_hold_s", "paper_unquotable_s", "mirror_on_sell"},
    "profile_matched_v1": {"paper_max_hold_s", "paper_unquotable_s", "mirror_on_sell"},
    "scalp_c": {"paper_max_hold_s", "paper_unquotable_s", "mirror_on_sell"},
    "scalp_c2": {"paper_max_hold_s", "paper_unquotable_s", "mirror_on_sell"},
}


def paper_policy_is_valid(position: dict) -> bool:
    """Return whether a forward row contains the complete frozen policy for its arm."""
    try:
        data = json.loads((position or {}).get("assigned_policy_json") or "")
        strategy = str((position or {}).get("strategy") or "")
        if not isinstance(data, dict):
            return False
        params = data.get("params")
        required = _PAPER_REQUIRED_PARAMS[strategy]
        if not isinstance(params, dict) or not required <= set(params):
            return False
        if not isinstance(params.get("mirror_on_sell"), bool):
            return False
        for name in required - {"mirror_on_sell"}:
            value = params.get(name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            if not math.isfinite(float(value)):
                return False
        if strategy in ("scalp_c", "scalp_c2"):
            deserialize_policy(_canonical_json({"exit_policy": data.get("exit_policy")}))
        return True
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def paper_policy_value(position: dict, name: str, fallback):
    """Read a frozen new-cohort parameter; legacy or malformed rows retain old behavior."""
    if (position or {}).get("experiment_status") not in ("eligible", "excluded_fill",
                                                          "operator_modified"):
        return fallback
    try:
        data = json.loads(position.get("assigned_policy_json") or "")
        params = data.get("params", data)
        if name in params:
            return params[name]
        exit_policy = data.get("exit_policy", {})
        return exit_policy[name] if name in exit_policy else fallback
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return fallback


# Chains the paper lab trades. Part of real_shadow's rule identity: adding a chain changes the
# stop rule, which is a real new cohort. Mirrors CHAIN_IDS in memecoin_paper_report.
_PAPER_RULE_CHAINS = ("1", "4663", "56", "8453", "solana")


def _rule_number(value):
    """Canonical JSON forbids inf/nan; a null hold means 'no time cap', as in serialize_policy."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(float(value)) else None
    return value


def paper_arm_rule_identity(arm: str) -> tuple[str, str]:
    """Event-INDEPENDENT identity of one arm's rule: (rule_id, rule_sha256).

    Cohort identity must not vary with the coin. scalp_c/scalp_c2 resolve a per-mcap band and
    real_shadow a per-chain stop, so hashing the RESOLVED policy minted a fresh cohort for every
    (mcap band x chain) pair — 17 concurrent cohorts inside 15 hours — and the report, which keeps
    exactly one cohort, kept landing on a bucket holding a single event. Hash the RULE instead. The
    resolved instance is unchanged: it still lands in the row's assigned_policy_json and still
    drives the exit.
    """
    if arm in ("scalp_c", "scalp_c2"):
        bands = _SCALP_BANDS if arm == "scalp_c" else _SCALP_C2_TP
        fallback, fallback_id = resolve_policy({"entry_mcap": None}, arm=arm)
        payload = {
            "schema_version": 1, "strategy": arm, "rule": "mcap_banded",
            "bands": [[_rule_number(field) for field in band] for band in bands],
            "fallback_policy_id": fallback_id,
            "fallback_exit_policy": json.loads(serialize_policy(fallback)),
            "params": {**_paper_common_params(), "mirror_on_sell": True},
        }
    elif arm == "profile_matched_v1":
        # The RULE is "the frozen artifact's take-profit over funded's inherited rails", which is
        # stable even though the take-profit it yields is per-wallet. Resolving here would put
        # per-wallet parameters into COHORT identity and split one arm into a cohort per wallet.
        payload = {
            "schema_version": 1, "strategy": arm, "rule": "profile_matched_v1",
            "spec": "docs/superpowers/specs/2026-08-29-profile-matched-v1-fitting-addendum.md",
            "fitted": ["tp_pct"], "inherited_live": ["stop_pct", "derisk", "trail"],
            "params": {**_paper_common_params()},
        }
    elif arm == "funded_clone":
        # The rule is "whatever the funded contract builder would freeze for this origin", and
        # that is stable even though the assignment it produces is not. Resolving it here would
        # need an origin this function does not have, and would put per-origin parameters into
        # COHORT identity — splitting one arm into a different cohort per band and per wallet,
        # which is the opposite of what a cohort is for.
        payload = {
            "schema_version": 1, "strategy": arm, "rule": "funded_contract_clone",
            "resolver": "sniper.services.memecoin_risk.funded_policy_for_origin",
            "params": {**_paper_common_params()},
        }
    else:
        payload = json.loads(resolve_paper_arm_policy(arm)[2])
        if arm == "real_shadow":
            params = dict(payload["params"])
            params.pop("stop_pct", None)
            params["stop_pct_by_chain"] = {
                chain: float(S.real_stop_pct_for_chain(chain)) for chain in _PAPER_RULE_CHAINS
            }
            payload["params"] = params
    canonical = _canonical_json(payload)
    return f"{arm}-rule-v1", hashlib.sha256(canonical.encode()).hexdigest()


def build_paper_cohort(arms, notional_usd=None) -> tuple[str, str]:
    """Identity of a comparable A/B regime. Depends only on the arms' rules and the bet size."""
    ordered = [
        {"strategy": arm, "policy_id": rule_id, "policy_version": 1, "policy_sha256": rule_sha}
        for arm, (rule_id, rule_sha) in (
            (arm, paper_arm_rule_identity(arm)) for arm in sorted(dict.fromkeys(arms))
        )
    ]
    # notional is cohort identity, not metadata: every arm on an event shares one entry fill and
    # one bet, so the paired delta is only equal-capital while the bet is constant. Conviction
    # sizing (MEMECOIN_CONVICTION_SIZING) varies it per event and would otherwise pool $100 and
    # $300 events into one dollar mean under the same cohort id. Changing it starts a new cohort.
    #
    # The release sha is deliberately NOT identity. It changes on every deploy (61 web releases
    # to date) for reasons unrelated to the rails, so it reset the cohort long before the 30-cluster
    # evidence floor could ever be reached — the experiment was structurally undecidable. Every
    # parameter that decides a paper exit is pinned by the per-arm rule hashes above, and the
    # release that opened each row is already persisted in memecoin_policy_versions.code_commit.
    # fill_model is cohort identity, not a label. Rows priced off a DexScreener mid + a modeled
    # impact floor measured a -0.35% median fill gap against the copied trader while the funded leg
    # measured +7.54% WORSE on the same events — an ~8pt bias, largest during the launch pump we
    # actually trade. Those rows must never pool with executable-route-priced ones.
    from sniper.services.memecoin_copy_trader import PAPER_FILL_MODEL
    contract = _canonical_json({
        "schema_version": 3, "engine": "legacy-multi-arm-paper",
        "fill_model": PAPER_FILL_MODEL,
        "notional_usd": (round(float(notional_usd), 2) if notional_usd is not None else None),
        "arms": ordered,
    })
    return hashlib.sha256(contract.encode()).hexdigest(), contract


def control_reason(policy: ExitPolicy, pnl_pct: float) -> str | None:
    """Reproduces control_exit_reason() from memecoin_brackets.py against the policy object."""
    if pnl_pct >= policy.tp_pct:
        return "take_profit"
    if pnl_pct <= policy.stop_pct:
        return "stop_loss"
    return None


def policy_exit_reason(policy: ExitPolicy, pnl_pct: float, held_s: float) -> str | None:
    """Generic full-exit rails used by frozen paper policies.

    Favorable/adverse price rails take precedence over a simultaneous time cap, matching the
    existing control sweep's price-first behavior.
    """
    if policy.tp_pct > 0 and pnl_pct >= policy.tp_pct:
        return "take_profit"
    if pnl_pct <= policy.stop_pct:
        return "stop"
    if math.isfinite(policy.max_hold_s) and held_s > policy.max_hold_s:
        return "timecap"
    return None


def apply_overrides(policy: ExitPolicy, override: dict | None, *, entry_price,
                    entry_mcap=None, current_mcap=None) -> ExitPolicy:
    """Fold a per-position exit override (from db_memecoin.effective_exit_override) into an ExitPolicy
    — the ONE place override → rails happens, so the sweep and the replay harness stay on one code
    path. TP/SL given as % override the policy directly; given as a target MARKET CAP they resolve to
    an effective % via the entry mcap→price scaling (mcap ∝ price for a fixed supply). Pure; a None
    override returns the policy unchanged. Authority (who may loosen vs tighten) is enforced at the
    write/API layer, not here — an operator override may widen risk, a template may not."""
    if not override:
        return policy
    tp = override.get("tp_pct")
    if tp is None and override.get("tp_target_mcap") and entry_mcap:
        tp = (override["tp_target_mcap"] / entry_mcap - 1.0) * 100.0
    sl = override.get("sl_pct")
    if sl is None and override.get("sl_target_mcap") and entry_mcap:
        sl = (override["sl_target_mcap"] / entry_mcap - 1.0) * 100.0
    changes: dict = {}
    if tp is not None:
        changes["tp_pct"] = float(tp)
    if sl is not None:
        changes["stop_pct"] = float(sl)
    if override.get("trail_pct") is not None:
        changes["trail_mode"] = "on"
    return replace(policy, **changes) if changes else policy


def choose_entry_template(mcap):
    """The system's per-coin exit template by entry market cap (v1: mcap only). Returns
    (name, override_dict) or None (=> default rails). SCALP big-caps — a small quick TP + tight stop
    instead of refusing them or handing them moonshot rails; micro/mid keep the default rails (they
    can 10x, the defaults already ride). Only consulted when MEMECOIN_AUTO_TEMPLATES='on'. Params are
    launch guesses from settings — SP#3 recalibrates them by paired forward A/B."""
    if mcap is None:
        return None
    if mcap >= S.MEMECOIN_SCALP_MCAP_USD:
        return ("scalp", {"tp_pct": S.MEMECOIN_SCALP_TP_PCT, "sl_pct": -abs(S.MEMECOIN_SCALP_STOP_PCT)})
    return None
