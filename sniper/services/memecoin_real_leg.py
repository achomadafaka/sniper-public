"""Priority 3 — real-money copy leg (DISARMED). Wires the tested components into one entry flow:

    arm gate (MEMECOIN_REAL_ENABLED + kill switch)
      -> S1 risk gate + pessimistic reservation (reserve_entry)
      -> pre-entry token-safety gate (freeze/mint authority, token-2022 fee, sell-route)
      -> entry executor (quote -> sign -> write-ahead sig -> send -> confirm -> chain-delta fill)

Tier-0 speed instrumentation: per-stage latencies (gate/safety/exec) are captured on every attempt
so later speed decisions have data, not guesses. `token_safety_fn` and `execute_fn` are injected so
this is unit-tested without live HTTP; at arm-time they are bound to the real Jupiter/RPC/solders
implementations. A rejection or unsafe token never reaches the executor and frees the reservation.

Exit routing is the mirror: the real exit sweep passes each real position through
solana_exit_executor.execute_exit — wired into the runner's real leg at arm-time.
"""
from __future__ import annotations

import logging
import json
import time
import inspect

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from sniper.services.memecoin_arming import real_entries_allowed, real_entry_block_reason
from sniper.services import memecoin_arming as _arming
from sniper.services.memecoin_risk import preflight_entry, reserve_entry, risk_refusal_result

logger = logging.getLogger(__name__)



def losing_reentry_applies(signal_source):
    """False only for a desk entry while MEMECOIN_DESK_LOSING_REENTRY_EXEMPT is on (operator 2026-10-23)."""
    from sniper.services.memecoin_origin import DESK
    return not (signal_source == DESK and bool(getattr(cfg, "MEMECOIN_DESK_LOSING_REENTRY_EXEMPT", True)))


def reentry_cooldown_applies(signal_source):
    """False only for a desk entry while MEMECOIN_DESK_REENTRY_COOLDOWN_EXEMPT is on (operator 2026-10-23)."""
    from sniper.services.memecoin_origin import DESK
    return not (signal_source == DESK and bool(getattr(cfg, "MEMECOIN_DESK_REENTRY_COOLDOWN_EXEMPT", True)))

def _cluster_gate_enabled():
    """Read the funded cluster-gate flag at call time so a restart-free flip is honoured."""
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_REAL_CLUSTER_GATE_ENABLED", False))


def _entry_mint_read_evidence(ev, trace, read):
    """Only the successful safety read bound to this attempt and requested mint."""
    pre = getattr(ev, "entry_prereads", None)
    attempt = getattr(trace, "attempt_id", None)
    if (trace is None or type(attempt) is not str or not attempt
            or getattr(ev, "network_id", None) != "solana"
            or pre is None or getattr(pre, "trace", None) is not trace
            or getattr(pre, "chain", None) != "solana"
            or getattr(pre, "mint", None) != ev.mint
            or getattr(pre, "_mint", None) is not read):
        return None
    try:
        from sniper.execution.solana_wallet import _TOKEN_PROGRAM
        from sniper.execution.solana_token_safety import TOKEN_2022_PROGRAM
        value, failure = read
        data = value["data"]
        parsed = data["parsed"]
        info = parsed["info"]
        decimals = info["decimals"]
        programs = {_TOKEN_PROGRAM: "spl-token", TOKEN_2022_PROGRAM: "spl-token-2022"}
        if (failure is not None or parsed.get("type") != "mint"
                or value.get("owner") not in programs
                or data.get("program") != programs[value["owner"]]
                or info.get("isInitialized") is not True
                or type(decimals) is not int or not 0 <= decimals <= 18):
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return {"network_id": "solana", "mint": ev.mint,
            "attempt_id": attempt, "decimals": decimals}


def network_matches_mint_family(network_id, mint):
    """Concrete Solana/EVM identity must agree with the mint address family."""
    chain = str(network_id or "").strip()
    if chain in ("", "evm-unknown"):
        return True
    is_evm_mint = str(mint or "").startswith("0x")
    return (chain == "solana") == (not is_evm_mint)


def route_real_entry(ev, evm_allowlist, resolve_fn=None, *, resolved_network_id=None):
    """Venue router. A base58 mint -> 'solana'; an 0x (EVM) mint -> 'evm:<chain>' if the chain is
    allowlisted, else 'drop'. Mint prefix identifies the venue family, while a concrete network is
    authoritative; disagreement fails closed. A Solana buy still routes when the feed omits
    networkId, and an 0x mint can NEVER be handed to the Solana executor.

    When the feed omits the chain entirely ('' / legacy 'evm-unknown' — ~27% of curated EVM buys),
    `resolve_fn(mint)` recovers it from DexScreener so allowlisted-chain tokens still trade. A chain
    the feed NAMED is authoritative and never second-guessed (a resolver mistake must not smuggle a
    held-out-chain token past the allowlist)."""
    mint = getattr(ev, "mint", "") or ""
    chain = (str(resolved_network_id).strip() if resolved_network_id is not None
             else (getattr(ev, "network_id", "") or "").strip())
    if resolved_network_id is None and chain in ("", "evm-unknown") and resolve_fn is not None:
        try:
            chain = resolve_fn(mint) or chain
        except Exception:
            pass   # resolver failure -> unrouted -> drop (fail-closed)
    if not network_matches_mint_family(chain, mint):
        return "drop"
    if not mint.startswith("0x"):
        return "solana"
    if chain in evm_allowlist:
        return "evm:" + chain
    return "drop"


def _trace_mark(trace, stage):
    if trace is None:
        return
    try:
        trace.mark(stage)
    except Exception:
        pass


def _probe(trace, name):
    """A measurement-only substage (2026-09-28): splits the Solana reservation stage, which had no
    mark between the leg's start and the reservation. Never raises into an entry."""
    marker = getattr(trace, "mark_substage", None) if trace is not None else None
    if marker is None:
        return
    try:
        marker(name)
    except Exception:
        pass


def _executor_supports_kwarg(execute_fn, name):
    try:
        params = inspect.signature(execute_fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind == inspect.Parameter.VAR_KEYWORD for p in params)


def desk_prequote(ev, execute_fn):
    """THE DESK PRE-QUOTE (memecoin_desk_prequote): the quote kept fresh while JUDGE decided, for a desk
    entry only, or None. The executor's take() still checks clip and age; None quotes as before."""
    desk = getattr(ev, "desk", None)
    if not isinstance(desk, dict) or not _executor_supports_kwarg(execute_fn, "early_quote"):
        return None
    from sniper.services.memecoin_desk_prequote import held_for
    return held_for(desk.get("ticket_id"), getattr(ev, "mint", None))


def desk_price_cap(ev, execute_fn):
    """(refusal, cap) for a desk entry (S6, operator 2026-10-22): its verdict price cap from the frozen
    desk metadata, or a refusal when it is missing, malformed or the executor cannot enforce it. A desk
    entry is never executed uncapped. Non-desk entries: (None, None)."""
    desk = getattr(ev, "desk", None)
    if desk is None:
        return None, None
    from sniper.services.memecoin_origin import validate_verdict_price
    cap = desk.get("verdict_price") if isinstance(desk, dict) else None
    if cap is None:
        return "desk_verdict_price_missing", None
    if not validate_verdict_price(cap)[0]:
        return "desk_verdict_price_invalid", None
    if not _executor_supports_kwarg(execute_fn, "desk_price_cap"):
        return "desk_verdict_price_unsupported", None
    return None, cap


def dip_cap_refusal(ev, execute_fn):
    """'dip_cap_unsupported' when a dip decision (ev.dip_cap, memecoin_dip_retry) would reach an
    executor that cannot enforce it, else None. Fail closed: such a dip never fills at the gate cap
    (Alpha, B, 2026-10-09)."""
    if getattr(ev, "dip_cap", None) is None or _executor_supports_kwarg(execute_fn, "dip_cap"):
        return None
    return "dip_cap_unsupported"


def _coin_age_presign_fn(ev, classification, trace):
    """The executor's pre-sign coin-age check (spec 2026-09-28-memecoin-unknown-pair-age-mint-
    evidence-design.md), or None where it does not apply: the knob off, not Solana, or a KNOWN pair
    age (the reservation gate judged that one). Calling it reads a cache and marks the trace."""
    from sniper.services.memecoin_coin_age import unknown_pair_check_enabled
    network = str(getattr(ev, "network_id", None) or "solana")
    facts = (getattr(classification, "facts", None) or {}) if classification is not None else {}
    age = facts.get("pair_age_s")
    if (network not in {"solana", "57073"} or not unknown_pair_check_enabled()
            or (isinstance(age, (int, float)) and not isinstance(age, bool))):
        return None
    mint = getattr(ev, "mint", None)

    def check():
        from sniper.services.memecoin_risk import coin_age_presign_check
        reason, fields = coin_age_presign_check(network, mint, age,
                                                source_user_id=getattr(ev, "source_user_id", None))
        if fields is not None and trace is not None:
            try:
                trace.mark_substage("coin_age_presign", **fields)
            except Exception:
                logger.exception("coin age presign trace failed for %s", mint)
        return reason
    return check


def _executor_supports_trace(execute_fn):
    return _executor_supports_kwarg(execute_fn, "trace")


def _crowd_presign_fn(ev, prior, *, now_iso, started, clock):
    """No I/O at signing: pin the corrected-UTC expiry to this attempt's monotonic clock."""
    from datetime import datetime
    import math
    from sniper.services.memecoin_origin import desk_lane_open
    try:
        now_iso, started = getattr(ev, "_crowd_clock_anchor", (now_iso, started))
        raw = ev.desk["admission_proof"]["entry_deadline_utc"]
        deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        at = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        stop = started + (deadline - at).total_seconds()
        if not math.isfinite(stop):
            raise ValueError("deadline")
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return lambda: "crowd_metadata_invalid"

    def check():
        try:
            if not desk_lane_open(ev.desk):
                return "desk_disabled"
            tick = clock()
            if not math.isfinite(tick) or tick >= stop:
                return "crowd_entry_expired"
            return prior() if prior is not None else None
        except Exception:
            return "crowd_presign_unknown"
    check.require_final_check = True
    return check


def _known_group_presign_fn(conn, ev, prior, *, now_iso, started, clock):
    """Original deadline plus current durable invalidation; no provider work at signing."""
    from datetime import datetime, timedelta
    from sniper.services.memecoin_known_group_canary import _admission_refusal
    anchor = datetime.fromisoformat(now_iso)

    def check():
        try:
            elapsed = clock() - started
            if elapsed < 0:
                return 'known_group_clock_regressed'
            at = (anchor + timedelta(seconds=elapsed)).isoformat()
            refusal = _admission_refusal(conn, ev.desk, mint=ev.mint, now_iso=at)
            return refusal or (prior() if prior is not None else None)
        except Exception:
            logger.exception('known-group pre-sign proof unavailable')
            return 'known_group_presign_unknown'
    check.require_final_check = True
    return check


def _entry_provenance(ev, trace):
    normalized = getattr(ev, "received_at_normalized", None)
    clock_domain = str(getattr(ev, "clock_domain", None) or "").strip() or None
    if normalized:
        observed_at = normalized
        clock_domain = "observed_real_utc"
    elif clock_domain == "observed_real_utc":
        observed_at = getattr(ev, "observed_at", None) or getattr(ev, "received_at", None)
    else:
        observed_at = None
    return {
        "origin_event_id": getattr(ev, "fomo_event_id", None),
        "attempt_id": getattr(trace, "attempt_id", None),
        "signal_source": getattr(ev, "signal_source", None),
        "source_price": getattr(ev, "their_price", None),
        "source_market_cap": getattr(ev, "their_market_cap", None),
        "observed_at": observed_at,
        "clock_domain": clock_domain,
    }


def real_entry(conn, ev, *, day, usdc_balance, sol_balance, now_iso, trade_size,
               token_safety_fn, execute_fn, kill_path=None, clock=None, alert_fn=None,
               evm_allowlist=None, evm_entry_fn=None, resolve_fn=None,
               impact_cap_pct=None, chase_max_pct=None, resolved_identity=None, trace=None,
               wallet_chase_cap_pct=None, wallet_chase_cap_expires_at=None,
               classification=None, capacity_clip=None, cluster_decide_fn=None,
               cluster_block_price_fn=None, size_cap_usd=None, trial=None,
               snapshot_debits_fn=None):
    """Attempt one real entry for a curated buy event. Routes by venue first (network router), then
    on the Solana path: arm-gate -> S1 reserve -> token-safety -> executor. Returns the executor
    result (+ 'timing'), or {status: disarmed|dropped_network|evm_unarmed|rejected|unsafe}. DISARMED
    unless MEMECOIN_REAL_ENABLED and no kill switch. `evm_entry_fn` dispatches allowlisted EVM tokens
    once the EVM leg exists (None until then -> passed + counted, never sent to the Solana executor)."""
    from sniper.settings.memecoin import MEMECOIN_REAL_POLICY, MEMECOIN_EVM_ALLOWLIST
    clock = clock or time.perf_counter
    t0 = clock()

    if not real_entries_allowed(kill_path):
        # `real_entries_allowed` REMAINS the gate -- it is the seam the funded-leg tests
        # monkeypatch, and replacing it here silently bypassed their arrangement in 7 of
        # them. `real_entry_block_reason` is consulted only for the LABEL, which reaches
        # terminal_reason via _trace_terminal so the tape says which stand-down this was.
        return {"status": "disarmed",
                "reason": real_entry_block_reason(kill_path) or "unknown"}
    # QUIET WINDOW (2026-09-11): automatic entries stand down inside the operator's UTC window.
    # A manual buy is the operator's own hand and passes. Exits never come through here.
    from sniper.services.memecoin_origin import bypasses_selection, enforces_capacity
    _apply_quiet_hours = enforces_capacity(getattr(ev, "signal_source", None))
    if _apply_quiet_hours and _arming.in_quiet_hours():
        return {"status": "disarmed", "reason": "quiet_hours"}

    # never real-buy a cash/base asset (WETH, cbBTC, USDC…) — a routing leg the feed mislabels
    # as a buy would pass token safety (it IS sellable) and burn a slot on a non-signal.
    from sniper.analysis.fomo_account.classify import is_cash_asset
    if is_cash_asset(ev.mint):
        return {"status": "dropped_base_asset"}

    if resolved_identity is None and resolve_fn is None:
        from sniper.execution.solana_quote import resolve_evm_chain as resolve_fn
    route = route_real_entry(
        ev, MEMECOIN_EVM_ALLOWLIST if evm_allowlist is None else evm_allowlist,
        resolve_fn=resolve_fn,
        resolved_network_id=(resolved_identity.network_id if resolved_identity is not None else None))
    if route == "drop":
        network_id = (resolved_identity.network_id if resolved_identity is not None
                      else getattr(ev, "network_id", "") or "evm-unknown")
        m.record_network_drop(conn, network_id,
                              ev.mint, "excluded", now_iso)
        return {"status": "dropped_network", "network": network_id}
    route_network = route.split(":", 1)[1] if route.startswith("evm:") else "solana"
    if conn is not None and m.unresolved_signed_pending_count(conn, route_network):
        return {
            "status": "rejected",
            "reason": "unresolved signed transaction on %s" % route_network,
        }
    if route.startswith("evm:"):
        if route_network in ("57073", "1"):
            reason = cfg.chain_capability_reason(route_network)
            if reason:
                return {"status": "rejected", "reason": reason}
        if route_network == "57073" and _cluster_gate_enabled():
            # The current cluster producer has no verified Ink owner graph. Its enabled policy
            # cannot be bypassed by this early EVM dispatch.
            return {"status": "rejected", "reason": "cluster_evidence_unavailable_57073"}
        if evm_entry_fn is None:                        # EVM leg not built yet -> pass (counted)
            network_id = (resolved_identity.network_id if resolved_identity is not None
                          else getattr(ev, "network_id", ""))
            m.record_network_drop(conn, network_id, ev.mint, "evm_unarmed", now_iso)
            return {"status": "evm_unarmed", "route": route}
        return evm_entry_fn(conn, ev, route=route, day=day, usdc_balance=usdc_balance,
                            sol_balance=sol_balance, now_iso=now_iso, trade_size=trade_size,
                            token_safety_fn=token_safety_fn, kill_path=kill_path, clock=clock,
                            alert_fn=alert_fn, trace=trace, classification=classification,
                            impact_cap_pct=impact_cap_pct, chase_max_pct=chase_max_pct,
                            wallet_chase_cap_pct=wallet_chase_cap_pct,
                            wallet_chase_cap_expires_at=wallet_chase_cap_expires_at,
                            capacity_clip=capacity_clip, size_cap_usd=size_cap_usd,
                            trial=trial)

    # PER-WALLET RE-ENTRY EXEMPTIONS (2026-08-28). Resolved HERE because the leg owns policy and
    # the risk modules must stay blind to who asked — they receive finished sets, exactly as they
    # do for the manual split. Two families, two switches: excusing the cooldown must not silently
    # excuse losing-re-entry. Fail-closed to "apply everything" if the lookup raises.
    _probe(trace, "probe_leg_start")
    try:
        # Only the Solana path reaches here (EVM returned above), so the chain is Solana.
        _wallet_exempt = m.entry_gate_exemptions(conn, getattr(ev, "source_user_id", None), "solana")
    except Exception:
        _wallet_exempt = frozenset()
    # A desk entry keeps every capacity gate (spec desk-execution-plumbing §1.5).
    _enforce_concurrency = enforces_capacity(getattr(ev, "signal_source", None))
    # Same manual condition, separate name: the min-trade floor is a SELECTION gate and a manual
    # buy is the operator's own judgement of whether the clip is worth taking.
    _enforce_min_floor = _enforce_concurrency
    _apply_cooldown = (_enforce_concurrency
                       and "reentry_cooldown" not in _wallet_exempt)
    _apply_losing = ("losing_reentry" not in _wallet_exempt
                     and losing_reentry_applies(getattr(ev, "signal_source", None)))
    # DESK RE-ENTRY EXEMPT (operator 2026-10-23, two orders): a desk entry no longer waits out
    # MEMECOIN_REAL_REENTRY_COOLDOWN_S (24 h) after a close of the same coin, and is no longer refused by
    # the losing re-entry block. One-per-coin and every capacity/safety gate still apply. Kill knobs,
    # each restoring its own gate for the desk: MEMECOIN_DESK_REENTRY_COOLDOWN_EXEMPT=0,
    # MEMECOIN_DESK_LOSING_REENTRY_EXEMPT=0.
    _apply_reentry_cooldown = reentry_cooldown_applies(getattr(ev, "signal_source", None))
    # CROSS-WALLET RE-ENTRY (operator 2026-10-08): a full-cap wallet is cooled, and blocked as a
    # losing re-entry, only by its OWN closes; other wallets' and manual closes do not block it.
    # The risk builders receive the scope, never the list. Both hops get it.
    from sniper.services.memecoin_wallet_caps import cross_wallet_reentry_closes_by
    _reentry_closes_by = cross_wallet_reentry_closes_by(getattr(ev, "source_user_id", None), "solana")
    # ABOVE NANO (operator 2026-10-08): the band's clip ceiling, from this entry's classification.
    from sniper.services.memecoin_risk import solana_clip_ceiling_usd
    _clip_ceiling = solana_clip_ceiling_usd(classification, "solana", getattr(ev, "source_user_id", None))
    # OPERATOR OVERRIDE (2026-09-22, operator order): "Enter anyway" on a refused/failed card.
    # Named separately from every manual switch above -- override is stronger than plain manual,
    # so it must never be inferred from signal_source alone. Only ev.override, set nowhere but the
    # override intent path, can turn this on. See memecoin_risk.py's override_risk_caps for the
    # exact list of what this does and does not skip.
    _override_risk = bool(getattr(ev, "override", False))
    # Read the existing risk state before the cluster request. The reservation repeats this check
    # under BEGIN IMMEDIATE after an allow.
    _probe(trace, "probe_pre_preflight")
    if classification is not None:
        preflight = preflight_entry(
            conn,
            mint=ev.mint,
            day=day,
            usdc_balance=usdc_balance,
            sol_balance=sol_balance,
            kill_tripped=False,
            classification=classification,
            include_reentry_policy=_apply_cooldown,
            include_losing_reentry=_apply_losing,
            include_reentry_cooldown=_apply_reentry_cooldown,
            reentry_closes_by=_reentry_closes_by,
            clip_ceiling_usd=_clip_ceiling,
            enforce_concurrency=_enforce_concurrency,
            enforce_min_floor=_enforce_min_floor,
            override_risk_caps=_override_risk,
            copy_slots=getattr(ev, "desk", None) is None,
        )
        if not preflight.allowed:
            return risk_refusal_result(preflight)

    cluster_decision_id = None
    # A configured-on gate with no injected client is a WIRING ERROR, not a pass. Without
    # this, `cluster_decide_fn=None` silently skips admission while the operator view still
    # shows the gate enabled — the worst possible state, because it looks protected and is
    # not. Fail closed and name the fault.
    if _cluster_gate_enabled() and cluster_decide_fn is None:
        return {"status": "rejected", "reason": "cluster_client_missing"}
    if cluster_decide_fn is not None:
        cluster_started = clock()
        cluster = cluster_decide_fn(
            ev.mint,
            source_path=getattr(ev, "signal_source", None) or "global_feed",
        )
        cluster_ms = (clock() - cluster_started) * 1000.0
        cluster_decision_id = cluster.decision_id
        attempt_id = getattr(trace, "attempt_id", None)
        if attempt_id:
            m.set_entry_attempt_cluster_decision(
                conn,
                attempt_id,
                decision_id=cluster.decision_id,
                reason=cluster.reason,
                metrics_json=json.dumps(
                    cluster.metrics, sort_keys=True, separators=(",", ":")
                ),
                completed_at=cluster.completed_at,
                duration_ms=cluster_ms,
            )
        _trace_mark(trace, "cluster")
        if not cluster.allowed:
            if attempt_id:
                ref_price = None
                if cluster_block_price_fn is not None:
                    try:
                        ref_price = cluster_block_price_fn(ev.mint)
                    except Exception:
                        logger.exception(
                            "cluster block reference read failed mint=%s", ev.mint
                        )
                from sniper.services.memecoin_capture_v2 import record_cluster_block_outcome
                record_cluster_block_outcome(
                    conn,
                    attempt_id=attempt_id,
                    mint=ev.mint,
                    observed_at=cluster.completed_at,
                    ref_price=ref_price,
                    source_user_id=getattr(ev, "source_user_id", None),
                    handle=getattr(ev, "source_handle", None),
                )
            return {
                "status": "rejected",
                "reason": cluster.reason,
                "cluster_decision_id": cluster.decision_id,
            }

    # route == 'solana': the existing Solana real-entry flow.
    # A MANUAL entry names its own clip (the operator picked the coin and the size); every
    # automated path keeps the configured sizing mode. Scoped by signal_source so a copy signal
    # can never carry an operator size, and the override still faces the whole risk gate.
    size_override = (trade_size
                     if bypasses_selection(getattr(ev, "signal_source", None)) else None)
    # A manual entry is the operator's deliberate pick, so the re-entry POLICY sets (cooldown,
    # losing-re-entry — both selection judgments per the 07-09 study) do not apply to it. Live
    # 2026-08-27 12:56Z: the operator's re-buy after a fresh close was refused twice by the
    # cooldown. One-per-coin and every safety gate still judge the entry.
    funded_policy = None
    if getattr(ev, "signal_source", None) == "manual" and classification is not None:
        from sniper.services.memecoin_funded_policy import _manual_unmanaged_policy
        funded_policy = _manual_unmanaged_policy(coin_band=classification.coin_band)
    _probe(trace, "probe_pre_reserve")
    decision, pid = reserve_entry(
        conn, mint=ev.mint, source_user_id=ev.source_user_id, strategy=MEMECOIN_REAL_POLICY,
        day=day, usdc_balance=usdc_balance, sol_balance=sol_balance, now_iso=now_iso,
        kill_tripped=False, classification=classification,
        cluster_decision_id=cluster_decision_id, size_override=size_override,
        include_reentry_policy=_apply_cooldown,
        include_losing_reentry=_apply_losing,
        include_reentry_cooldown=_apply_reentry_cooldown,
        reentry_closes_by=_reentry_closes_by,
        clip_ceiling_usd=_clip_ceiling,
        enforce_concurrency=_enforce_concurrency,
        enforce_min_floor=_enforce_min_floor,
        override_risk_caps=_override_risk,
        funded_policy=funded_policy, trial=trial, size_cap_usd=size_cap_usd,
        snapshot_debits_fn=snapshot_debits_fn, desk=getattr(ev, "desk", None),
        **_entry_provenance(ev, trace))
    t_gate = clock()
    _trace_mark(trace, "reservation")
    if not decision.allowed:
        return risk_refusal_result(decision)
    from sniper.services.memecoin_wallet_caps import mark_reentry_lift
    mark_reentry_lift(trace, decision, _reentry_closes_by)
    # spend EXACTLY what was reserved — under bankroll sizing the gate computes the clip, so the
    # caller's static trade_size is only a fixed-mode fallback (reservation==spend invariant).
    trade_size = decision.approved_size_usdc or trade_size

    # TOKEN SAFETY. Operator-switchable as of 2026-08-13, default ON. When it is off the check is
    # not merely ignored, it is NOT CALLED — that is the latency the operator asked for, and a
    # result nobody reads is worse than no result.
    #
    # WARNING, stated plainly because the log line is the only remaining trace: with this off a
    # funded entry can buy a token it cannot sell back (honeypot, or a Token-2022 transfer fee that
    # taxes the exit leg). That is an UNRECOVERABLE loss of the clip, not a drawdown an exit rail
    # can cut. No downstream rail substitutes for it: the stop, the take-profit and the catastrophe
    # floor all presuppose a sellable bag.
    marker = getattr(trace, "mark_substage", None) if trace is not None else None
    _safety_warning = None
    _mint_evidence = None
    if cfg.MEMECOIN_REAL_TOKEN_SAFETY_ENABLED:
        # The band may have read this mint already (MEMECOIN_SOLANA_BAND_MCAP_BASIS=onchain): the
        # gate judges that read instead of a second one. Without it the call is exactly as before.
        _pre_read = getattr(ev, "mint_account_read", None)
        safe, reason = (token_safety_fn(ev.mint, pre_read=_pre_read) if _pre_read is not None
                        else token_safety_fn(ev.mint))
        t_safety = clock()
        if marker is not None:
            marker("token_safety", allowed=bool(safe), reason=str(reason))
        _trace_mark(trace, "safety_checks")
        if safe:
            _mint_evidence = _entry_mint_read_evidence(ev, trace, _pre_read)
        if not safe:
            # OPERATOR OVERRIDE (2026-09-22, operator order). The check still ran and the verdict
            # is kept, not overwritten -- it just no longer refuses. "run the safety check anyway
            # ... never as a block" (the operator's own words, relayed by Alpha).
            if _override_risk:
                _safety_warning = reason
            else:
                m.release_pending(conn, pid, "token_safety")
                if alert_fn:
                    alert_fn(ev.mint, reason)
                return {"status": "unsafe", "reason": reason, "pending_id": pid}
    else:
        logger.warning(
            "real entry: TOKEN SAFETY BYPASSED by MEMECOIN_REAL_TOKEN_SAFETY_ENABLED=0 "
            "mint=%s size=%s — an unsellable token will not be refused", ev.mint, trade_size)
        t_safety = clock()
        # The substage still reports, so a bypassed entry is visible in the trace and not merely
        # absent from it. An absent substage is indistinguishable from a trace that never ran.
        if marker is not None:
            marker("token_safety", allowed=True, reason="bypassed_by_config")
        _trace_mark(trace, "safety_checks")

    execute_kwargs = {"impact_cap_pct": impact_cap_pct}
    if _mint_evidence is not None and _executor_supports_kwarg(execute_fn, "mint_read_evidence"):
        execute_kwargs["mint_read_evidence"] = _mint_evidence
    # Pass the chase limit only when the executor accepts it.
    if chase_max_pct is not None and _executor_supports_kwarg(execute_fn, "chase_max_pct"):
        execute_kwargs["chase_max_pct"] = chase_max_pct
    if trace is not None and _executor_supports_trace(execute_fn):
        execute_kwargs["trace"] = trace
    # THE CHASE REFERENCE (2026-09-28): the on-chain lane's post-trade source price, when the event
    # carries one; the executor otherwise keeps the reservation's average-fill source price.
    _presign = _coin_age_presign_fn(ev, classification, trace)
    if (getattr(ev, "signal_source", None) == "desk"
            and (getattr(ev, "desk", None) or {}).get("cohort_id") == "large-buyer-crowd-v1"):
        if not _executor_supports_kwarg(execute_fn, "presign_refusal_fn"):
            m.release_pending(conn, pid, "crowd_deadline_unsupported")
            return {"status": "rejected", "reason": "crowd_deadline_unsupported", "pending_id": pid}
        _presign = _crowd_presign_fn(ev, _presign, now_iso=now_iso, started=t0, clock=clock)
    if (getattr(ev, 'signal_source', None) == 'desk'
            and (getattr(ev, 'desk', None) or {}).get('cohort_id') == 'known-group-launch-v1'):
        if not _executor_supports_kwarg(execute_fn, 'presign_refusal_fn'):
            m.release_pending(conn, pid, 'known_group_deadline_unsupported')
            return {'status':'rejected','reason':'known_group_deadline_unsupported','pending_id':pid}
        _presign = _known_group_presign_fn(conn, ev, _presign, now_iso=now_iso, started=t0, clock=clock)
        refusal = _presign()
        if refusal:
            m.release_pending(conn, pid, refusal)
            return {'status':'rejected','reason':refusal,'pending_id':pid}
    if _presign is not None and _executor_supports_kwarg(execute_fn, "presign_refusal_fn"):
        execute_kwargs["presign_refusal_fn"] = _presign
    _ref = getattr(ev, "chase_reference_price", None)
    if _ref and _executor_supports_kwarg(execute_fn, "chase_reference_price"):
        execute_kwargs["chase_reference_price"] = _ref
        _note = getattr(ev, "chase_reference_note", None)     # a routed leg's stated looseness
        if _note and _executor_supports_kwarg(execute_fn, "chase_reference_note"):
            execute_kwargs["chase_reference_note"] = _note
    # THE EARLY QUOTE (memecoin_early_quote), only this attempt's: the executor takes it for the
    # approved clip or discards it and quotes as before.
    _early = getattr(ev, "entry_early_quote", None)
    if (_early is not None and trace is not None and getattr(_early, "trace", None) is trace
            and _executor_supports_kwarg(execute_fn, "early_quote")):
        execute_kwargs["early_quote"] = _early
    elif (_held := desk_prequote(ev, execute_fn)) is not None:
        execute_kwargs["early_quote"] = _held
    _desk_refused, _desk_cap = desk_price_cap(ev, execute_fn)
    if _desk_refused is not None:
        m.release_pending(conn, pid, _desk_refused)
        return {"status": "rejected", "reason": _desk_refused, "pending_id": pid}
    if _desk_cap is not None:
        execute_kwargs["desk_price_cap"] = _desk_cap
    _dip_refused = dip_cap_refusal(ev, execute_fn)
    if _dip_refused is not None:
        m.release_pending(conn, pid, _dip_refused)
        return {"status": "rejected", "reason": _dip_refused, "pending_id": pid}
    if getattr(ev, "dip_cap", None) is not None:
        execute_kwargs["dip_cap"] = ev.dip_cap
    result = execute_fn(conn, pid, ev.mint, trade_size, now_iso, **execute_kwargs)
    t_exec = clock()

    timing = {
        "gate_ms": (t_gate - t0) * 1000.0,
        "safety_ms": (t_safety - t_gate) * 1000.0,
        "exec_ms": (t_exec - t_safety) * 1000.0,
        "total_ms": (t_exec - t0) * 1000.0,
    }
    try:
        pending = m.pending_row(conn, pid) or {}
        m.record_exec_latency(conn, mint=ev.mint, pending_id=pid,
                              status=result.get("status"), sig=result.get("sig"),
                              timing=timing, at=now_iso,
                              impact_pct=result.get("impact_pct"), network_id="solana",
                              attempt_id=pending.get("attempt_id"),
                              origin_event_id=pending.get("origin_event_id"),
                              signal_source=pending.get("signal_source"),
                              source_price=(result.get("source_price")
                                            if result.get("source_price") is not None
                                            else pending.get("source_price")),
                              price_attribution=result.get("price_attribution"),
                              quote_price=result.get("quote_price"),
                              quote_observed_at=result.get("quote_observed_at"),
                              quote_age_ms=result.get("quote_age_ms"),
                              quote_id=result.get("quote_id"),
                              quote_route_target=result.get("quote_route_target"),
                              fill_price=result.get("fill_price"),
                              source_drift_pct=result.get("source_drift_pct"),
                              execution_slippage_pct=result.get("execution_slippage_pct"),
                              total_copy_drift_pct=result.get("total_copy_drift_pct"))
    except Exception:
        pass   # instrumentation is best-effort; never break the trade path
    if result.get("status") == "filled" and result.get("position_id"):
        if _safety_warning:
            result = {**result, "token_safety_warning": _safety_warning}
        if _override_risk:
            try:
                m.set_experiment_status(conn, result["position_id"], "manual_override")
            except Exception:
                logger.exception("manual_override marker failed to write pos=%s",
                                 result.get("position_id"))
    return {**result, "pending_id": pid, "timing": timing}
