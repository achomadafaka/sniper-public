"""WO-7 — official V5 replay acceptance gate: fill-modeled, OOS-split, evidence-floored.

The single acceptance step and the source of every registered constant. Old-SHA-vs-new-SHA replay
runs in isolated git worktrees (never parallel engine code in the tree); THIS module is the pure
scoring layer both SHAs feed — the fill model, the time-ordered OOS split, the evidence-floor gate,
the declared candidate grid, and the paired-bootstrap acceptance bar. No DB writes, no network.

Verified constraints that shape it (2026-07-25 DB audit, re-confirmed this cycle):
- Each chain is read from its own decoded tape; Solana never falls back to the EVM tape.
- Fill calibration uses exact executable previews paired to confirmed chain fills at the same tick;
  `memecoin_position_marks.executable_exit_value` is a midpoint alias and is never calibration.
- The 4663 tape currently holds 0 decoded v3_burn rows (WO-10 accrues them forward), and the funded
  4663 path is Uniswap-V3 -> the LP-removal-event floor is unmet on history.
Consequence enforced structurally below: until the fill model is calibrated against real closes, the
metric is net_mid_exit_value and is ineligible to arm ANY chain; and a chain whose evidence floors
are unmet cannot pass regardless of the replay delta. This gate MANUFACTURES NOTHING.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --- policy identity + the declared finite candidate grid (before any held-out touch) ------------

POLICY_REVISION = "v5-gate-2026-07-25"

# The finite search space fit on the TRAINING slice and validated ONCE on held-out. Declaring it here
# (before held-out) is what prevents post-hoc tuning. These are CANDIDATES to search, never registered
# answers — no value here is wired into a rule until a held-out run selects it AND floors are met.
@dataclass(frozen=True)
class CandidateGrid:
    lp_minor_fraction: tuple = (0.05, 0.10, 0.20)
    lp_catastrophic_fraction: tuple = (0.40, 0.60, 0.80)
    structure_persistence_window_s: tuple = (60.0, 120.0, 300.0)
    no_progress_window_s: tuple = (300.0, 600.0, 900.0)
    route_gone_streak: tuple = (3, 4, 5)
    sizing_form: tuple = ("continuous", "three_tier")
    policy_revision: str = POLICY_REVISION


CANDIDATE_GRID = CandidateGrid()

# --- per-chain evidence floors (WO-7 revision) ---------------------------------------------------

EVIDENCE_FLOORS = {
    "position_days": 14,          # >=14 held-pool position-days of that chain's own decoded tape
    "closed_overlapping": 20,     # >=20 closed positions overlapping the tape window
    "decoded_lp_removals": 10,    # >=10 decoded LP-removal events for the WO-3 boundary fit
}


def evidence_floors_met(metrics, *, fill_model_calibrated):
    """Return (met, unmet_reasons). A chain passes only when EVERY floor is met AND the fill model is
    calibrated — a mid-only or in-sample green is never deploy evidence."""
    reasons = []
    for key, floor in EVIDENCE_FLOORS.items():
        got = metrics.get(key)
        if got is None or got < floor:
            reasons.append(f"{key}={got} < {floor}")
    if not fill_model_calibrated:
        reasons.append("fill_model_uncalibrated")
    return (not reasons), reasons


# --- fill model (the executor's real slippage ladder + whole-bag impact cap) ----------------------

@dataclass(frozen=True)
class FillModel:
    """Values every simulated exit through the executor's own economics, not the mid. `calibrated`
    is False until the slippage curve is fit AND validated against real closes; while False the
    output is net_mid_exit_value and can never arm a chain."""
    impact_cap_pct: float
    haircut_frac: float                    # p75 realized-slippage proxy (uncalibrated)
    calibrated: bool = False
    slippage_curve: tuple = ()             # calibrated per-size (notional -> impact_frac) points

    @property
    def metric_name(self):
        return "net_fill_modeled_exit_value" if self.calibrated else "net_mid_exit_value"

    @property
    def arm_eligible(self):
        return self.calibrated

    def exit_value(self, mid_notional):
        """Net executable proceeds for selling a whole bag worth `mid_notional` at mid. Applies the
        size-appropriate impact fraction, capped by the whole-bag impact cap. Uncalibrated -> the p75
        haircut (a mid proxy), so the number is honest about being mid-anchored, not executable."""
        if mid_notional is None or mid_notional <= 0:
            return 0.0
        impact = self._impact_frac(mid_notional)
        impact = min(impact, self.impact_cap_pct / 100.0)
        return float(mid_notional) * (1.0 - impact)

    def _impact_frac(self, notional):
        if not self.calibrated or not self.slippage_curve:
            return self.haircut_frac
        # piecewise: the impact of the smallest calibrated size >= notional (fail-safe: the largest)
        for size, frac in sorted(self.slippage_curve):
            if notional <= size:
                return frac
        return self.slippage_curve[-1][1] if self.slippage_curve else self.haircut_frac


def calibrate_fill_model(real_closes, *, impact_cap_pct, haircut_frac, min_closes=20):
    """Fit the fill model's slippage curve from real closes (each: {notional, realized_impact_frac}).
    Returns a CALIBRATED FillModel only with >= min_closes real closes; otherwise an UNCALIBRATED one
    (net_mid_exit_value, ineligible to arm). Manufactures nothing: too few closes -> not calibrated."""
    closes = [c for c in (real_closes or ())
              if c.get("notional") and c.get("realized_impact_frac") is not None]
    if len(closes) < int(min_closes):
        return FillModel(impact_cap_pct=impact_cap_pct, haircut_frac=haircut_frac, calibrated=False)
    curve = _conservative_monotone_curve(
        (float(c["notional"]), float(c["realized_impact_frac"])) for c in closes)
    return FillModel(impact_cap_pct=impact_cap_pct, haircut_frac=haircut_frac,
                     calibrated=True, slippage_curve=curve)


def real_close_samples(mconn, chain):
    """Return exact preview-to-confirmed-fill samples for one chain.

    The executable preview and fill must share the position and exact decision timestamp. Full exits
    match the position's final reason; partials match the `derisk` audit and scale the whole-bag
    preview by the executed fraction. Unsigned/provisional fills and missing previews are excluded.
    Positive impact is adverse slippage; quote improvement is conservatively floored at zero.
    """
    rows = mconn.execute(
        "SELECT f.id AS fill_id, f.kind, f.notional AS actual_notional, f.fraction, "
        "a.preview_usdc FROM memecoin_fills f "
        "JOIN memecoin_positions p ON p.id=f.position_id "
        "JOIN memecoin_exit_trigger_audit a ON a.position_id=f.position_id "
        " AND a.checked_at=f.created_at AND a.allowed=1 "
        " AND ((f.kind='derisk' AND a.reason='derisk') "
        "      OR (f.kind<>'derisk' AND a.reason=p.exit_reason)) "
        "WHERE p.execution_mode='real' AND COALESCE(p.network_id,'solana')=? "
        "AND f.tx_sig IS NOT NULL AND TRIM(f.tx_sig)<>'' "
        "AND f.notional IS NOT NULL AND f.notional>0 "
        "AND a.preview_usdc IS NOT NULL AND a.preview_usdc>0 "
        "ORDER BY f.id", (str(chain),)).fetchall()
    samples = []
    for row in rows:
        kind = row["kind"] if hasattr(row, "keys") else row[1]
        actual = float(row["actual_notional"] if hasattr(row, "keys") else row[2])
        fraction = row["fraction"] if hasattr(row, "keys") else row[3]
        preview = float(row["preview_usdc"] if hasattr(row, "keys") else row[4])
        expected = preview
        if kind == "derisk":
            if fraction is None or not 0 < float(fraction) <= 1:
                continue
            expected *= float(fraction)
        if expected <= 0:
            continue
        samples.append({
            "fill_id": int(row["fill_id"] if hasattr(row, "keys") else row[0]),
            "notional": expected,
            "realized_impact_frac": max(0.0, 1.0 - actual / expected),
        })
    return samples


def _conservative_monotone_curve(points):
    """Collapse raw (notional, realized_impact_frac) closes into a size-bucketed, MONOTONE
    non-decreasing curve: a bigger bag can never be modeled cheaper to exit than a smaller one (a
    conservative floor, since realized impact is a noisy sample). Per size we take the CONSERVATIVE
    (max) observed impact, then enforce a running max across increasing size."""
    by_size = {}
    for notional, frac in points:
        by_size[notional] = max(frac, by_size.get(notional, frac))
    curve, running = [], 0.0
    for size in sorted(by_size):
        running = max(running, by_size[size])          # non-decreasing in size (conservative)
        curve.append((size, running))
    return tuple(curve)


# --- time-ordered OOS split ----------------------------------------------------------------------

def time_ordered_split(episodes, *, train_frac=0.5, time_key="closed_ts"):
    """Split episodes into (train, holdout) by TIME — never randomly. Constants are fit on the earlier
    slice; acceptance is judged only on the later held-out slice."""
    ordered = sorted(episodes or (), key=lambda e: e.get(time_key) or "")
    cut = int(len(ordered) * float(train_frac))
    return ordered[:cut], ordered[cut:]


# --- full-policy replay with independent inventory ----------------------------------------------

@dataclass(frozen=True)
class ReplayInventory:
    remaining_fraction: float = 1.0
    partial_used: bool = False


@dataclass(frozen=True)
class PolicyEpisodeResult:
    episode_id: str
    net_exit_value: float
    proceeds: float
    catastrophic_loss: bool
    actions: tuple


def _replay_policy_episode(episode, *, decide, fill_model, catastrophic_loss_frac):
    from sniper.services.memecoin_exit_brain import CUT, DERISK, TAKE_PROFIT
    episode_id = str(episode.get("episode_id") or "")
    try:
        entry = float(episode.get("entry_notional"))
    except (TypeError, ValueError) as exc:
        raise EvidencePairingError(f"episode {episode_id or '<missing>'}: invalid entry") from exc
    events = list(episode.get("events") or ())
    if not episode_id or entry <= 0 or not events:
        raise EvidencePairingError(f"episode {episode_id or '<missing>'}: missing identity/entry/events")
    inventory = ReplayInventory()
    proceeds = 0.0
    actions = []
    terminal_value = None
    for event in events:
        try:
            whole_value = float(event["whole_position_mid_value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidencePairingError(f"episode {episode_id}: unpriced event") from exc
        if not (whole_value >= 0):
            raise EvidencePairingError(f"episode {episode_id}: negative event value")
        terminal_value = whole_value
        decision = decide(event, inventory)
        action = getattr(decision, "action", None)
        if action == CUT:
            proceeds += fill_model.exit_value(whole_value * inventory.remaining_fraction)
            inventory = ReplayInventory(remaining_fraction=0.0,
                                        partial_used=inventory.partial_used)
            actions.append("CUT")
            break
        if action in (DERISK, TAKE_PROFIT) and not inventory.partial_used:
            try:
                proposed = float(decision.proposed_fraction)
            except (TypeError, ValueError):
                continue
            if not 0 < proposed <= 1:
                continue
            sold = inventory.remaining_fraction * proposed
            proceeds += fill_model.exit_value(whole_value * sold)
            inventory = ReplayInventory(
                remaining_fraction=inventory.remaining_fraction - sold, partial_used=True)
            actions.append(f"{action}:{proposed:.6f}")
    if inventory.remaining_fraction > 0:
        proceeds += fill_model.exit_value(float(terminal_value) * inventory.remaining_fraction)
        actions.append("TERMINAL")
    return PolicyEpisodeResult(
        episode_id=episode_id, net_exit_value=proceeds - entry, proceeds=proceeds,
        catastrophic_loss=proceeds <= entry * (1.0 - float(catastrophic_loss_frac)),
        actions=tuple(actions))


def replay_paired_policies(episodes, *, baseline_decide, candidate_decide, fill_model,
                           catastrophic_loss_frac=0.80):
    """Replay complete baseline/candidate policies over identical episodes with separate inventory.

    ``decide(event, inventory)`` owns the full policy proposal at that event; the harness enforces
    the funded one-partial guard and fill-values every sale. This is the common scorer each isolated
    old/new-SHA worktree can feed. It never opens a DB, tape, or held-out partition itself.
    """
    seen = set()
    baseline_results = {}
    candidate_results = {}
    for episode in episodes or ():
        episode_id = str(episode.get("episode_id") or "")
        if not episode_id or episode_id in seen:
            raise EvidencePairingError(f"duplicate/missing episode id: {episode_id!r}")
        seen.add(episode_id)
        baseline_results[episode_id] = _replay_policy_episode(
            episode, decide=baseline_decide, fill_model=fill_model,
            catastrophic_loss_frac=catastrophic_loss_frac)
        candidate_results[episode_id] = _replay_policy_episode(
            episode, decide=candidate_decide, fill_model=fill_model,
            catastrophic_loss_frac=catastrophic_loss_frac)
    if not seen:
        raise EvidencePairingError("empty replay evidence")
    return {
        "baseline_by_episode": {key: value.net_exit_value
                                for key, value in baseline_results.items()},
        "candidate_by_episode": {key: value.net_exit_value
                                 for key, value in candidate_results.items()},
        "baseline_catastrophic_rate": (
            sum(value.catastrophic_loss for value in baseline_results.values()) / len(seen)),
        "candidate_catastrophic_rate": (
            sum(value.catastrophic_loss for value in candidate_results.values()) / len(seen)),
        "baseline_results": baseline_results,
        "candidate_results": candidate_results,
    }


# --- acceptance bar ------------------------------------------------------------------------------

@dataclass(frozen=True)
class AcceptanceVerdict:
    metric_name: str
    n: int
    paired_mean_diff: float
    bootstrap_lower_bound: float
    catastrophic_regression: bool
    floors_met: bool
    arm_eligible: bool
    reasons: tuple = field(default_factory=tuple)


class EvidencePairingError(ValueError):
    """Raised when baseline/candidate holdout evidence cannot be paired by immutable episode id —
    length, identity, or ordering mismatch. Fail closed: a mispaired comparison is NEVER scored."""


def pair_by_episode(baseline_by_episode, candidate_by_episode):
    """Pair per-episode net values by IMMUTABLE episode id, in a deterministic (sorted-id) order.

    Never a silent positional `zip`: baseline and candidate MUST cover exactly the same episode-id set
    (same episodes replayed through both SHAs), and neither may be empty. Any mismatch raises
    EvidencePairingError so the caller fails closed instead of scoring a truncated/misaligned pair."""
    base = dict(baseline_by_episode or {})
    cand = dict(candidate_by_episode or {})
    if not base or not cand:
        raise EvidencePairingError("empty evidence — cannot pair")
    if set(base) != set(cand):
        missing_b = sorted(set(cand) - set(base))
        missing_c = sorted(set(base) - set(cand))
        raise EvidencePairingError(
            f"episode-id mismatch (only-in-candidate={missing_b[:5]} only-in-baseline={missing_c[:5]})")
    ids = tuple(sorted(base))
    return ids, [float(base[i]) for i in ids], [float(cand[i]) for i in ids]


def acceptance_verdict(baseline_by_episode, candidate_by_episode, *, fill_model, floor_metrics,
                       catastrophic_baseline, catastrophic_candidate, bootstrap_iters=2000):
    """Paired acceptance on the HELD-OUT slice. `*_by_episode` are {immutable_episode_id: net exit
    value} for the SAME episodes replayed through the baseline (previous V5) and candidate (new V5)
    policy SHAs — paired by id (never positional). A chain arms ONLY when, together: the fill model is
    calibrated, all evidence floors are met, the paired bootstrap lower bound on (candidate - baseline)
    is >= 0, and there is no catastrophic-loss-rate regression. A pairing failure fails CLOSED (not
    arm-eligible), never a silently truncated comparison."""
    from sniper.analysis.wallet_protocol.statistics import bootstrap_ci
    floors_met, floor_reasons = evidence_floors_met(
        floor_metrics, fill_model_calibrated=fill_model.calibrated)
    reasons = list(floor_reasons)
    try:
        _ids, base_vals, cand_vals = pair_by_episode(baseline_by_episode, candidate_by_episode)
    except EvidencePairingError as exc:
        return AcceptanceVerdict(
            metric_name=fill_model.metric_name, n=0, paired_mean_diff=0.0,
            bootstrap_lower_bound=0.0, catastrophic_regression=False, floors_met=floors_met,
            arm_eligible=False, reasons=tuple(reasons + [f"evidence_pairing_failed: {exc}"]))
    paired = [c - b for b, c in zip(base_vals, cand_vals)]
    n = len(paired)
    point, lo, _hi = bootstrap_ci(paired, n_iter=int(bootstrap_iters))   # seeded -> deterministic
    catastrophic_regression = float(catastrophic_candidate) > float(catastrophic_baseline)
    if lo < 0:
        reasons.append(f"paired_bootstrap_lb={lo:.4f} < 0")
    if catastrophic_regression:
        reasons.append("catastrophic_loss_rate_regression")
    arm = fill_model.arm_eligible and floors_met and lo >= 0 and not catastrophic_regression
    return AcceptanceVerdict(
        metric_name=fill_model.metric_name, n=n, paired_mean_diff=point, bootstrap_lower_bound=lo,
        catastrophic_regression=catastrophic_regression, floors_met=floors_met,
        arm_eligible=arm, reasons=tuple(reasons))


# --- WO-13: canonical policy manifest + SHA-256 --------------------------------------------------

FILL_MODEL_VERSION = "fillmodel-v1-conservative-monotone"
ENGINE_VERSION = "v5"
POLICY_REGISTRY_FILENAME = "memecoin_v5_policy_registry.jsonl"
ACTIVE_POLICY_FILENAME = "memecoin_v5_active_policy.json"

# Rules that ship DEGENERATE (no registered constant yet) — declared in the manifest so a revision's
# identity records exactly which rules are still in fail-closed mode.
DEGENERATE_RULES = (
    "lp_severity_catastrophic_until_boundaries_registered",
    "sizing_conviction_scaled_until_wo7_form_registered",
    "time_derisk_no_authority_without_persisted_executable_no_progress",
    "partial_lp_escalation_unregistered",
)


def registered_policy_parameters(cfg):
    """Snapshot every live setting/constant that can change a V5 decision.

    This is passed into the immutable manifest before preregistration. Values that are deliberately
    unregistered remain explicit ``None`` and are also named in ``degenerate_rules``; process-env
    drift can therefore never masquerade as the same policy revision.
    """
    from sniper.analysis.exit_flex_v5 import (
        ARRIVAL_HORIZON_UNITS, BAND_TIME_TO_PEAK_S, FUNDED_BAND_PROFILE,
        LP_MATERIAL_DERISK_FRACTION, TARGET_HORIZON_S, TIME_DERISK_FRACTION,
        VOL_HORIZON_UNITS)
    from sniper.services import memecoin_exit_market_state as market_state
    return {
        "band_anchor_trail_pct": {
            "nano": float(cfg.MEMECOIN_REAL_PARTIAL_TRAIL_PCT),
            "micro": float(cfg.MEMECOIN_REAL_PARTIAL_TRAIL_PCT),
            "small": float(cfg.MEMECOIN_REAL_TRAIL_PCT),
            "mid": float(cfg.MEMECOIN_REAL_MID_TRAIL_PCT),
        },
        "band_edge_duration_s": dict(sorted(BAND_TIME_TO_PEAK_S.items())),
        "funded_band_profile": dict(sorted(FUNDED_BAND_PROFILE.items())),
        "partial_sizing": {
            "form": "continuous_strength_ratio", "min_fraction": 0.25,
            "max_fraction": 0.75, "lp_material_fraction": LP_MATERIAL_DERISK_FRACTION,
            "time_fraction": TIME_DERISK_FRACTION, "one_partial_per_position": True,
        },
        "target_horizon_s": TARGET_HORIZON_S,
        "vol_horizon_units": VOL_HORIZON_UNITS,
        "arrival_horizon_units": ARRIVAL_HORIZON_UNITS,
        "unsellable_sweeps": int(cfg.MEMECOIN_REAL_UNSELLABLE_SWEEPS),
        "lp_severity_bounds": dict(sorted((cfg.MEMECOIN_LP_SEVERITY_BOUNDS or {}).items())),
        "lp_route_health_complete_required": True,
        "route_gone_streak": getattr(cfg, "MEMECOIN_ROUTE_GONE_STREAK", None),
        "no_progress_window_s": getattr(cfg, "MEMECOIN_NO_PROGRESS_WINDOW_S", None),
        "structure_persistence_window_s": getattr(
            cfg, "MEMECOIN_STRUCTURE_BREAK_WINDOW_S", None),
        "hot_structure_window_s": getattr(cfg, "MEMECOIN_HOT_STRUCTURE_WINDOW_S", None),
        "feature_version": market_state.FEATURE_VERSION,
        "feature_freshness_s": float(getattr(
            cfg, "MEMECOIN_EXIT_STALE_S", market_state._STALE_S)),
        "min_structure_points": int(getattr(
            cfg, "MEMECOIN_EXIT_MIN_STRUCTURE_POINTS", market_state._MIN_STRUCTURE_POINTS)),
        "pivot_confirm_events": 2,
    }


def policy_manifest(*, baseline_policy_sha, candidate_policy_sha, evidence_cutoff,
                    selected_parameters=None, registered_parameters=None):
    """The COMPLETE immutable identity of a policy revision under test: every candidate constant/grid,
    the evidence floors, the sealed evidence cutoff, the fill-model version, the degenerate rules, and
    the previous-V5 (baseline) vs candidate-V5 SHAs. `policy_sha256` over this is the revision hash a
    position is governed by. Baseline is the PREVIOUS V5 policy — never a retired engine."""
    grid = CANDIDATE_GRID
    selected = dict(sorted((selected_parameters or {}).items()))
    degenerate = list(DEGENERATE_RULES)
    if "partial_lp_escalation" in selected:
        degenerate.remove("partial_lp_escalation_unregistered")
    return {
        "policy_revision": POLICY_REVISION,
        "engine_version": ENGINE_VERSION,
        "baseline_policy_sha": str(baseline_policy_sha),
        "candidate_policy_sha": str(candidate_policy_sha),
        "evidence_cutoff": str(evidence_cutoff),
        "fill_model_version": FILL_MODEL_VERSION,
        "evidence_floors": dict(sorted(EVIDENCE_FLOORS.items())),
        "degenerate_rules": degenerate,
        "partial_lp_escalation_arms": list(PARTIAL_LP_ESCALATION_ARMS),
        "selected_parameters": selected,
        "registered_parameters": dict(sorted((registered_parameters or {}).items())),
        "candidate_grid": {
            "lp_minor_fraction": list(grid.lp_minor_fraction),
            "lp_catastrophic_fraction": list(grid.lp_catastrophic_fraction),
            "structure_persistence_window_s": list(grid.structure_persistence_window_s),
            "no_progress_window_s": list(grid.no_progress_window_s),
            "route_gone_streak": list(grid.route_gone_streak),
            "sizing_form": list(grid.sizing_form),
        },
    }


def policy_sha256(manifest):
    """SHA-256 over the canonical (sorted-key, tight-separator) JSON of the manifest. Deterministic:
    the same manifest always hashes identically; any changed constant changes the hash."""
    import hashlib
    import json
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- WO-13: rejected revisions are immutable + permanently rejected ------------------------------

class RevisionImmutableError(RuntimeError):
    """Raised on any attempt to re-reject / un-reject an already-rejected revision. A rejection is
    permanent — a retry is a NEW preregistration, not a mutation of the old verdict."""


class InvalidPolicyRevision(ValueError):
    """The manifest/window is incomplete and therefore cannot seal a held-out read."""


class EvidenceWindowReuseError(ValueError):
    """A preregistration attempted to reuse evidence from a rejected revision."""


def _windows_overlap(a, b):
    (a0, a1), (b0, b1) = a, b
    return not (str(a1) <= str(b0) or str(b1) <= str(a0))


class PolicyRegistry:
    """Durable append-only preregistration and one-held-out-read ledger.

    JSONL keeps the evidence contract reviewable in git/operator tooling. Every mutation reloads
    under an exclusive file lock before appending, so two operators cannot both consume the same
    revision's one permitted read. No method opens a tape or holdout; it only seals identity/result.
    """

    def __init__(self, path):
        from pathlib import Path
        self.path = Path(path)

    @staticmethod
    def _validate_manifest(manifest):
        required = ("baseline_policy_sha", "candidate_policy_sha", "evidence_cutoff",
                    "candidate_grid", "evidence_floors", "degenerate_rules",
                    "selected_parameters", "registered_parameters")
        missing = [key for key in required if key not in manifest]
        raw = " ".join(str(manifest.get(k, "")) for k in (
            "baseline_policy_sha", "candidate_policy_sha", "evidence_cutoff")).upper()
        if (missing or "PENDING" in raw or "NOT_SET" in raw
                or not manifest.get("selected_parameters")
                or not manifest.get("registered_parameters")):
            raise InvalidPolicyRevision(
                f"policy revision is not fully selected/sealed (missing={missing})")
        partial_arm = manifest["selected_parameters"].get("partial_lp_escalation")
        if partial_arm is not None and partial_arm not in PARTIAL_LP_ESCALATION_ARMS:
            raise InvalidPolicyRevision(f"unknown partial_lp_escalation arm: {partial_arm}")

    @staticmethod
    def _validate_window(window):
        try:
            lo, hi = tuple(window)
        except Exception as exc:
            raise InvalidPolicyRevision("evidence window must be a (start,end) pair") from exc
        if not str(lo) or not str(hi) or str(lo) >= str(hi):
            raise InvalidPolicyRevision("evidence window must be ordered and non-empty")
        return str(lo), str(hi)

    @staticmethod
    def _records(handle):
        import json
        handle.seek(0)
        out = []
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except (TypeError, ValueError) as exc:
                raise InvalidPolicyRevision(f"corrupt registry JSONL at line {line_no}") from exc
        return out

    def _locked(self):
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        return os.fdopen(fd, "r+", encoding="utf-8")

    @staticmethod
    def _append(handle, record):
        import json
        import os
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    def preregister(self, manifest, *, evidence_window, registered_at):
        import fcntl
        self._validate_manifest(manifest)
        window = self._validate_window(evidence_window)
        sha = policy_sha256(manifest)
        with self._locked() as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            records = self._records(handle)
            if any(r.get("policy_sha256") == sha for r in records):
                raise RevisionImmutableError(f"revision {sha} already registered — immutable")
            rejected_shas = {r.get("policy_sha256") for r in records
                             if r.get("event") == "HOLDOUT_RECORDED"
                             and r.get("verdict") == "REJECTED"}
            rejected_windows = [tuple(r["evidence_window"]) for r in records
                                if r.get("event") == "PREREGISTERED"
                                and r.get("policy_sha256") in rejected_shas]
            if any(_windows_overlap(window, old) for old in rejected_windows):
                raise EvidenceWindowReuseError(
                    "evidence window overlaps a permanently rejected revision")
            self._append(handle, {
                "event": "PREREGISTERED", "policy_sha256": sha,
                "manifest": manifest, "evidence_window": list(window),
                "registered_at": str(registered_at),
            })
        return sha

    def record_holdout(self, policy_sha, *, verdict, recorded_at, result):
        import fcntl
        verdict = str(verdict).upper()
        if verdict not in {"ACCEPTED", "REJECTED"}:
            raise ValueError("verdict must be ACCEPTED or REJECTED")
        result = dict(result or {})
        with self._locked() as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            records = self._records(handle)
            registered = any(r.get("event") == "PREREGISTERED"
                             and r.get("policy_sha256") == policy_sha for r in records)
            consumed = any(r.get("event") == "HOLDOUT_RECORDED"
                           and r.get("policy_sha256") == policy_sha for r in records)
            if not registered:
                raise InvalidPolicyRevision(f"revision {policy_sha} is not preregistered")
            if consumed:
                raise RevisionImmutableError(f"revision {policy_sha} already consumed its holdout")
            if verdict == "ACCEPTED" and result.get("arm_eligible") is not True:
                raise InvalidPolicyRevision("ACCEPTED requires result.arm_eligible=true")
            self._append(handle, {
                "event": "HOLDOUT_RECORDED", "policy_sha256": str(policy_sha),
                "verdict": verdict, "recorded_at": str(recorded_at), "result": result,
            })

    def _state(self, policy_sha):
        import fcntl
        with self._locked() as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            records = self._records(handle)
        registered = any(r.get("event") == "PREREGISTERED"
                         and r.get("policy_sha256") == policy_sha for r in records)
        result = next((r for r in records if r.get("event") == "HOLDOUT_RECORDED"
                       and r.get("policy_sha256") == policy_sha), None)
        return registered, result

    def accepted_manifest(self, policy_sha):
        """Return the exact preregistered manifest only after its one holdout read accepted it.

        This is the runtime bridge for WO-13. It is deliberately read-only: a missing registry is
        an absent policy, not a reason to create a file or imply that a revision was registered.
        """
        import fcntl
        try:
            handle = self.path.open("r", encoding="utf-8")
        except OSError:
            return None
        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            records = self._records(handle)
        registered = [r for r in records if r.get("event") == "PREREGISTERED"
                      and r.get("policy_sha256") == policy_sha]
        heldout = [r for r in records if r.get("event") == "HOLDOUT_RECORDED"
                   and r.get("policy_sha256") == policy_sha]
        if (len(registered) != 1 or len(heldout) != 1
                or heldout[0].get("verdict") != "ACCEPTED"
                or (heldout[0].get("result") or {}).get("arm_eligible") is not True):
            return None
        manifest = registered[0].get("manifest")
        try:
            self._validate_manifest(manifest)
        except (TypeError, ValueError):
            return None
        if policy_sha256(manifest) != str(policy_sha):
            return None
        return manifest

    def can_read_holdout(self, policy_sha):
        registered, result = self._state(policy_sha)
        return registered and result is None

    def is_rejected(self, policy_sha):
        _registered, result = self._state(policy_sha)
        return bool(result and result.get("verdict") == "REJECTED")


def active_policy_revision(*, env=None):
    """Load the operator-selected V5 revision only when the durable registry accepted it.

    The pointer contains only the immutable hash. The manifest comes from the append-only registry,
    so editing the pointer cannot manufacture an accepted policy. Missing or malformed state is OFF.
    """
    import json
    from sniper.settings import memecoin as cfg
    try:
        data_dir = cfg.state_root(env) / "data"
        pointer = json.loads((data_dir / ACTIVE_POLICY_FILENAME).read_text(encoding="utf-8"))
        if not isinstance(pointer, dict) or set(pointer) != {"policy_sha256"}:
            return None
        policy_sha = pointer["policy_sha256"]
        if (not isinstance(policy_sha, str) or len(policy_sha) != 64
                or any(ch not in "0123456789abcdef" for ch in policy_sha)):
            return None
        manifest = PolicyRegistry(data_dir / POLICY_REGISTRY_FILENAME).accepted_manifest(policy_sha)
        return (policy_sha, manifest) if manifest is not None else None
    except (OSError, TypeError, ValueError):
        return None


# --- WO-13: first preregistered policy question --------------------------------------------------

# Material LP removal AFTER an earlier partial exit — escalate to a full CUT, or remain suppressed
# (WO5B ships "suppress": one prior partial suppresses further material-LP DERISKs; catastrophic still
# CUTs). Both arms are scored on TRAINING and the winner frozen BEFORE the one held-out read.
PARTIAL_LP_ESCALATION_ARMS = ("escalate_to_cut", "suppress")


def select_partial_lp_arm(train_scores):
    """Freeze the partial-LP-escalation arm on the TRAINING slice only (takes training scores; never
    holdout). Returns the frozen selection + basis. Higher training score wins; ties keep the
    conservative WO5B default ('suppress')."""
    scores = {a: float(train_scores.get(a, float("-inf"))) for a in PARTIAL_LP_ESCALATION_ARMS}
    best = max(PARTIAL_LP_ESCALATION_ARMS,
              key=lambda a: (scores[a], a == "suppress"))   # tie -> suppress (conservative default)
    return {"selected_arm": best, "frozen": True, "basis": "training",
            "train_scores": scores}


# --- floor status reporter (reads the real DBs; no writes; never opens holdout) -------------------

def _lp_kind_for(chain):
    return "v3_burn" if str(chain) == "4663" else ("sol_burn" if str(chain) == "solana" else "v2_burn")


def _pool_tape_window_days(ticks_conn, chain, pool):
    """(min_ts, max_ts, span_days) of the pool's decoded tape from block_times, or None if no tape."""
    try:
        row = ticks_conn.execute(
            "SELECT MIN(b.block_ts), MAX(b.block_ts) FROM pool_ticks p JOIN block_times b "
            "ON b.chain_id=p.chain_id AND b.block_no=p.block_no WHERE p.chain_id=? AND p.pool=?",
            (str(chain), str(pool))).fetchone()
    except Exception:
        return None
    if not row or row[0] is None or row[1] is None:
        return None
    return row[0], row[1]


def _overlap_days(a0, a1, b0, b1):
    """Days of overlap between ISO-time intervals [a0,a1] and [b0,b1] (0 if disjoint/unparseable)."""
    from datetime import datetime
    def _p(x):
        try:
            return datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    a0, a1, b0, b1 = _p(a0), _p(a1), _p(b0), _p(b1)
    if None in (a0, a1, b0, b1):
        return 0.0
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0.0, (hi - lo).total_seconds() / 86400.0)


def chain_floor_status(mconn, ticks_conn, chain, *, fill_model_calibrated=False):
    """Honest per-chain floor readout from the live DBs. Never fabricates a pass, never opens holdout.

    - `decoded_lp_removals`: forward decoded LP-removal events on this chain's tape.
    - `closed_overlapping`: closed real positions whose lifetime overlaps their OWN held-pool tape
      coverage (NOT every closed position on the chain).
    - `position_days`: sum of those per-position lifetime∩held-pool-tape overlaps, in days (never None).
    """
    from sniper.services.memecoin_exit_materializer import resolve_pool_for_position

    def _count(conn, sql, args=()):
        try:
            return conn.execute(sql, args).fetchone()[0]
        except Exception:
            return None

    lp = _count(ticks_conn, "SELECT COUNT(*) FROM pool_ticks WHERE chain_id=? AND kind=?",
                (str(chain), _lp_kind_for(chain)))
    tape_rows = _count(ticks_conn, "SELECT COUNT(*) FROM pool_ticks WHERE chain_id=?", (str(chain),))
    try:
        position_rows = mconn.execute(
            "SELECT mint, opened_at, closed_at, is_open FROM memecoin_positions "
            "WHERE execution_mode='real' AND COALESCE(network_id,'solana')=?",
            (str(chain),)).fetchall()
    except Exception:
        position_rows = []
    overlapping, position_days = 0, 0.0
    for r in position_rows:
        mint = r["mint"] if hasattr(r, "keys") else r[0]
        opened = r["opened_at"] if hasattr(r, "keys") else r[1]
        closed_at = r["closed_at"] if hasattr(r, "keys") else r[2]
        is_open = bool(r["is_open"] if hasattr(r, "keys") else r[3])
        try:
            pool = resolve_pool_for_position(ticks_conn, str(chain), mint)
        except Exception:
            pool = None
        if not pool:
            continue
        window = _pool_tape_window_days(ticks_conn, chain, pool)
        if window is None:
            continue
        end = window[1] if is_open or not closed_at else closed_at
        days = _overlap_days(opened, end, window[0], window[1])
        if days > 0.0:
            if not is_open and closed_at:
                overlapping += 1
            position_days += days
    metrics = {
        "position_days": round(position_days, 3),      # from held-pool tape overlap; never None
        "closed_overlapping": overlapping,             # only closes overlapping their held-pool tape
        "decoded_lp_removals": lp,
        "tape_rows": tape_rows,
    }
    met, reasons = evidence_floors_met(
        metrics, fill_model_calibrated=bool(fill_model_calibrated))
    return {"chain": str(chain), "metrics": metrics, "floors_met": met, "reasons": reasons}


def floor_report(mconn, ticks_conn, *, chains=("solana", "4663"), calibration_samples=None):
    """Read-only cadence report across chains. NEVER opens holdout data or mutates policy. Emits
    CODE_COMPLETE_EVIDENCE_BLOCKED unless EVERY chain meets EVERY floor (fill model additionally
    calibrated). A green report is a signal to the operator, not an automatic holdout read."""
    calibration_samples = calibration_samples or {}
    per_chain = {}
    for chain in chains:
        conn = ticks_conn.get(chain) if isinstance(ticks_conn, dict) else ticks_conn
        n_samples = int(calibration_samples.get(chain, 0))
        status = chain_floor_status(
            mconn, conn, chain, fill_model_calibrated=n_samples >= 20)
        status["calibration_sample_count"] = n_samples
        status["fill_model_calibrated"] = n_samples >= 20
        per_chain[chain] = status
    all_met = bool(per_chain) and all(s["floors_met"] for s in per_chain.values())
    return {
        "verdict": "FLOORS_MET_AWAITING_OPERATOR" if all_met else "CODE_COMPLETE_EVIDENCE_BLOCKED",
        "holdout_read": False,          # this reporter NEVER opens holdout
        "policy_mutated": False,        # and NEVER mutates policy
        "chains": per_chain,
    }
