"""Memecoin copy engine (paper). Entry on curated buy; proportional exit-mirror.

Fills are honest (DexScreener mid + modeled slippage). No averaging on repeat buys in v0.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.execution.solana_quote import honest_fill, dexscreener_stats, modeled_fill

logger = logging.getLogger(__name__)


def momentum_a_chain_allowed(network_id):
    """Chains momentum_a may open on.

    Solana has always worked. 4663 is admitted from 2026-07-24 because the tick tape prices it far
    faster than DexScreener — its 15-min hold used to expire before the first quote ever landed,
    giving $0.00 over 138 closes at 0% WR. That un-block shipped only after the pre-registered
    tape-vs-DexScreener cross-check PASSED (n=1768, median rel err 1.78%, p90 12.62%; spec §5.1).
    Every other EVM chain has NO tape, so its quotes are still too slow for a 15-min arm.
    """
    return str(network_id or "").strip().lower() in ("solana", "", "4663")


# Arms that exit on the TRADER's sell (mirror). momentum_a exits only on its own momentum
# read (in the exit sweep), so it is NOT here.
MIRROR_ARMS = (
    "control", "alpha_b", "real_shadow", "scalp_c", "scalp_c2", "trail_d", "peak_lock_e",
    "combo_f",
)


def _log_entry_snapshot(conn, ev, now_iso, action, *, mcap=None, liquidity=None,
                        token_age_s=None, latency=None, market_features=None,
                        executable_price=None):
    """Best-effort log-only snapshot. Never raises into the trade path."""
    try:
        from sniper.services.memecoin_signal import normalize
        from sniper.services.memecoin_episode import episode_facts, resolve_episode
        from sniper.services.memecoin_features import (
            build_snapshot, consensus_n_60m, trader_profile, write_snapshot,
        )
        sig = normalize(ev)
        eid = None
        episode_features = {}
        if action != "skipped_base_asset":
            eid, _ = resolve_episode(
                conn, sig.chain_id, sig.token_address, sig.source_tx, now_iso,
                source_price=sig.source_entry_price, executable_price=executable_price,
            )
            episode_features = episode_facts(
                conn, eid, current_source_price=sig.source_entry_price,
                current_executable_price=executable_price,
            )
        extra = dict(market_features or {})
        extra.update({
            "their_market_cap": getattr(ev, "their_market_cap", None),
            "is_first_buy": bool(getattr(ev, "is_first_buy", False)),
            "lag_s": getattr(ev, "lag_s", None),
            "trader_profile": trader_profile(conn, getattr(ev, "source_user_id", None)),
            **episode_features,
        })
        if (market_features or {}).get("market_cap") is not None:
            extra["dex_market_cap"] = market_features["market_cap"]
        snap = build_snapshot(sig, mcap=mcap, liquidity=liquidity, token_age_s=token_age_s,
                              consensus_n_60m=consensus_n_60m(conn, sig, now_iso),
                              wallet_trust_tier=None, regime_id=None,
                              entry_latency=latency or {}, decided_at=now_iso,
                              extra_features=extra)
        snap["episode_id"] = eid
        snapshot_id = write_snapshot(conn, snap, action=action)
        return {"episode_id": eid, "snapshot_id": snapshot_id}
    except Exception:
        logger.exception("entry snapshot logging failed (execution unaffected)")
        return None


def _qty_of(conn, position_id):
    # Remaining open qty (entered − exited) so a prior partial sell isn't double-exited.
    return m.open_qty(conn, position_id)


def _realized_from_exits(conn, position_id, entry_price):
    """Total realized PnL across all exit fills (partials + final) vs entry price.
    Keys off db_memecoin.EXIT_KINDS (single source of truth) so new exit kinds
    like bracket_exit are counted automatically — no duplicate list to forget."""
    placeholders = ",".join("?" for _ in m.EXIT_KINDS)
    rows = conn.execute(
        f"SELECT price, qty, fill_source, price_attribution FROM memecoin_fills "
        f"WHERE position_id=? AND kind IN ({placeholders})",
        (position_id, *m.EXIT_KINDS)).fetchall()
    # NULL IS THE UNKNOWN, 0 IS A KNOWN ZERO. A leg priced at the entry price because no quote was
    # available is not a measurement, and summing it yields exactly 0.0 — a fabricated breakeven
    # that reads as a real one. When NOTHING about this exit was observed, say so.
    #
    # A PARTIAL SELL KEEPS ITS NUMBER. If any leg carries a real price the realized figure covers
    # what was actually sold, so it stays. Note the residual limitation, which this does not fix:
    # for such a row the UNSOLD remainder is still implicitly valued at zero. That is a smaller
    # version of the same bug and it is left standing deliberately rather than silently widened.
    if not rows:
        return None
    if all(_is_placeholder_leg(r) for r in rows):
        return None
    return sum((float(r[0]) - float(entry_price)) * float(r[1]) for r in rows)


def _is_placeholder_leg(row):
    """Was this exit leg's price invented rather than observed?

    Keyed on the structured fields, never on `price == entry_price`: an exit that genuinely fills
    at the entry price is a real breakeven and must not be erased.
    """
    price_attribution = row[3] if len(row) > 3 else None
    if price_attribution == m.PLACEHOLDER_PRICE_ATTRIBUTION:
        return True
    # Pre-marker rows (closed before 2026-08-29) and the `noquote` path, which always substitutes.
    return (row[2] or "") == "noquote"


def conviction_size(their_size_usd, base_usd, *, enabled=None, pivot=None,
                    min_mult=None, max_mult=None):
    """Scale the paper bet by the copied trade's size (conviction). mult = sqrt(their/pivot)
    clamped to [min_mult, max_mult] — sqrt is gentler than linear (a $3M whale is not 150x
    the edge of a $20k buy). Returns the flat base when disabled or their_size is unknown."""
    import math
    from sniper.settings import memecoin as _cfg
    enabled = _cfg.MEMECOIN_CONVICTION_SIZING if enabled is None else enabled
    pivot = _cfg.MEMECOIN_SIZE_PIVOT_USD if pivot is None else pivot
    min_mult = _cfg.MEMECOIN_SIZE_MIN_MULT if min_mult is None else min_mult
    max_mult = _cfg.MEMECOIN_SIZE_MAX_MULT if max_mult is None else max_mult
    if not enabled or not their_size_usd or their_size_usd <= 0 or pivot <= 0:
        return base_usd
    mult = max(min_mult, min(max_mult, math.sqrt(their_size_usd / pivot)))
    return round(base_usd * mult, 2)


def _call_with_network(fn, mint, network_id, *args, **kwargs):
    try:
        return fn(mint, *args, network_id=network_id, **kwargs)
    except TypeError as exc:
        if "network_id" not in str(exc) or "unexpected keyword" not in str(exc):
            raise
        return fn(mint, *args, **kwargs)


def _entry_stats(mint, network_id, stats_fn):
    """Best-effort signal-time market facts from ONE existing stats call (tests pass None to
    skip). token_age = real-UTC now - DexScreener pairCreatedAt (ms); None when
    unavailable — never fabricate 0. symbol backfills the ticker for symbol-less sources
    (follow_activity swaps carry only addresses)."""
    if stats_fn is None:
        return {}
    try:
        st = _call_with_network(stats_fn, mint, network_id)
    except Exception:
        return {}
    if not st:
        return {}
    st = dict(st)
    age = None
    pca = st.get("pair_created_at")   # ms epoch
    if pca:
        from sniper.util.clock import real_utc_now
        age = max(0.0, real_utc_now() - float(pca) / 1000.0)
    st["token_age_s"] = age
    return st


# PAPER STEP 3 (spec 2026-10-08 paper path without DexScreener): the entry's market facts are the
# funded decision's on the same origin. The one-day provider comparison ended 2026-10-10;
# existing namespaced evidence remains readable without another provider GET per paper entry.
PROVIDER_SHADOW = False
FUNDED_FACT_KEYS = ("effective_mcap", "mcap_basis", "liquidity", "liquidity_source", "pair_age_s",
                    "volume_m5", "volume_h1", "volume_mcap")


def _saved_object(raw):
    try:
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _funded_facts(conn, ev):
    """(facts, ticker) of the latest funded attempt for this exact origin, or (None, None).
    No attempt, or one without a classification, is unknown: never a provider fill-in."""
    try:
        attempt = m.entry_attempt_for_origin(conn, getattr(ev, "fomo_event_id", None))
        raw = (attempt or {}).get("classification_json")
        if not raw:
            return None, None
        cls = json.loads(raw)
        facts = cls.get("facts") if isinstance(cls.get("facts"), dict) else {}
        frozen = {}
        inputs = _saved_object(attempt.get("gate_inputs_json"))
        if inputs is not None:
            frozen["gate_inputs"] = inputs
            if isinstance(inputs.get("onchain_m5"), dict):
                frozen["onchain_m5"] = inputs["onchain_m5"]
        trace = _saved_object(attempt.get("trace_json")) or {}
        events = trace.get("substage_events")
        for event in events if isinstance(events, list) else ():
            if not isinstance(event, dict):
                continue
            if event.get("name") == "volume_gate":
                frozen["volume_gate"] = event
            elif event.get("name") == "onchain_volume_m5" and "onchain_m5" not in frozen:
                frozen["onchain_m5"] = event
        return ({"attempt_id": attempt["attempt_id"], "coin_band": cls.get("coin_band"),
                 **{key: facts.get(key) for key in FUNDED_FACT_KEYS}, **frozen},
                attempt.get("ticker") or None)
    except Exception:
        logger.exception("funded facts unreadable for %s", getattr(ev, "fomo_event_id", None))
        return None, None


def _entry_facts(conn, ev, network_id, stats_fn):
    """The entry's market features: `funded_facts` and, while PROVIDER_SHADOW, `provider_shadow`.
    Never a provider key at the top level."""
    facts, ticker = _funded_facts(conn, ev)
    out = {"funded_facts": facts} if facts else {}
    if PROVIDER_SHADOW:
        shadow = _entry_stats(ev.mint, network_id, stats_fn)
        if shadow:
            out["provider_shadow"] = shadow
    return out, ticker


def _paper_m5_gate(ev, stats, mcap, now_iso):
    """The funded m5 volume gate's inputs for a funded_clone paper entry, recorded on its entry
    snapshot (Gamma via Alpha, 2026-10-07: grade age x mcap x turnover on the operator's m5-floor
    question once the young, under-$250k, below-floor cell has 30 closes). Observational: no new
    provider call, nothing decides on it, and a failure records its error class instead."""
    try:
        if "frozen_volume_gate" in stats:
            return _paper_saved_m5_gate(stats)
        from datetime import datetime
        from sniper.services.memecoin_market_state import _coin_band
        from sniper.services.memecoin_real_runner import m5_floor_evidence
        try:
            signal = datetime.fromisoformat(str(getattr(ev, "created_at", "")).replace("Z", "+00:00"))
            source_age_s = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                            - signal).total_seconds()
        except (TypeError, ValueError):
            source_age_s = None
        return m5_floor_evidence(stats, ev, mcap=mcap, network_id=getattr(ev, "network_id", None),
                                 coin_band=_coin_band(mcap, False), decision_at=now_iso,
                                 source_age_s=source_age_s)
    except Exception as exc:  # noqa: BLE001 - evidence must never cost the paper entry
        logger.warning("paper m5 gate evidence failed for %s", getattr(ev, "mint", None),
                       exc_info=True)
        return {"error": type(exc).__name__}


def _paper_saved_m5_gate(stats):
    """Describe only saved decision-time evidence; legacy gaps remain unknown."""
    from sniper.services.memecoin_real_runner import organic_volume
    gate = stats.get("frozen_volume_gate") or {}
    inputs = stats.get("frozen_gate_inputs") or {}
    own = stats.get("onchain_m5") or {}
    kwargs = gate.get("decision_kwargs") or {}
    mc = _positive(gate.get("mcap", inputs.get("mcap")))
    ratio = inputs.get("ratio_floor")
    if kwargs:
        band = kwargs.get("m5_band_max_mcap") or 0
        ratio = kwargs.get("m5_floor_over_band" if mc and band > 0 and mc > band else "m5_floor")
    provider = gate.get("decided_organic_volume_m5")
    if kwargs:
        provider = organic_volume(kwargs.get("vol_m5"), kwargs.get("signal_usd") or 0,
                                  source_age_s=kwargs.get("source_age_s"),
                                  index_lag_s=kwargs.get("index_lag_s") or 0)
    bound = own.get("usd")
    tested = gate.get("tested_volume_m5")
    if kwargs and ratio is not None and mc and gate.get("reason") != "m5_turnover_over_ceiling":
        if provider is not None and bound is not None:
            tested = max(provider, bound)
        elif provider is None and bound is not None and bound >= mc * ratio:
            tested = bound
        else:
            tested = provider
    allowed = gate.get("allowed")
    allowed = allowed if isinstance(allowed, bool) else None
    return {"evidence_basis": "frozen_funded_attempt", "organic_m5_usd": bound,
            "own_reason": own.get("reason", "frozen_own_evidence_unknown"),
            "own_coverage": own.get("coverage"),
            "own_measurement_basis": own.get("measurement_basis"),
            "m5_floor_undecidable": bool(kwargs.get("m5_floor_undecidable")) if kwargs else None,
            "provider_organic_m5_usd": provider, "tested_m5_usd": tested,
            "tested_ratio": tested / mc if tested is not None and mc else None,
            "m5_floor_ratio": ratio, "m5_floor_usd": mc * ratio if mc and ratio is not None else None,
            "m5_abs_floor_usd": kwargs.get("m5_abs_floor_usd"),
            "pair_age_s": gate.get("pair_age_s", stats.get("pair_age_s")),
            "source_age_s": gate.get("source_age_s"), "mcap_usd": mc,
            "coin_band": stats.get("coin_band"),
            "txns_m5_buys": stats.get("txns_m5_buys", gate.get("txns_m5_buys")),
            "txns_m5_sells": stats.get("txns_m5_sells", gate.get("txns_m5_sells")),
            "txns_m5_total": inputs.get("txns_m5_total", gate.get("txns")),
            "would_allow": allowed,
            "would_reason": gate.get("reason") if allowed is not None else "frozen_decision_unknown"}


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _entry_network(conn, ev, chain_slug=None):
    """Best available paper-entry network without guessing a concrete EVM chain from its address."""
    network_id = getattr(ev, "network_id", None) or None
    if (not network_id or network_id == "evm-unknown") and ev.mint.startswith("0x"):
        if chain_slug:
            from sniper.execution.solana_quote import DEX_CHAIN_TO_FOMO
            network_id = DEX_CHAIN_TO_FOMO.get(chain_slug, network_id)
        if not network_id or network_id == "evm-unknown":
            network_id = m.resolve_network_from_events(conn, ev.mint) or network_id
    return m.normalized_coin_network(network_id, ev.mint)


def _close_instrumentation(conn, pos, now_iso):
    """time_to_peak_s / max_gain_pct from the position's tracked peak (peak is updated for
    ALL arms each exit sweep), so the exit-liquidity meter is real for every arm."""
    from datetime import datetime

    def _p(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    entry = pos["entry_price"] or 0.0
    peak = pos.get("peak_price") or entry
    a, b = _p(pos["opened_at"]), _p(pos.get("peak_ts") or pos["opened_at"])
    tpk = (b - a).total_seconds() if (a and b) else 0.0
    mg = ((peak / entry - 1.0) * 100.0) if entry > 0 else None
    m.set_position_instrumentation(conn, pos["id"], time_to_peak_s=tpk, max_gain_pct=mg)


def _paper_assignments(conn, snapshot_ref, arms, *, entry_mcap, network_id, opened_at,
                       notional_usd=None, source_user_id=None):
    from sniper.services.memecoin_policy import (
        build_paper_cohort, resolve_paper_arm_policy, runtime_release_sha,
    )
    out = {}
    episode_id = (snapshot_ref or {}).get("episode_id")
    for arm in arms:
        existing = m.get_policy_assignment(conn, episode_id, arm) if episode_id else None
        if existing:
            assignment = (existing["policy_id"], existing["policy_version"],
                          existing["parameters_json"])
            record = False
        else:
            assignment = resolve_paper_arm_policy(
                arm, entry_mcap=entry_mcap, network_id=network_id, opened_at=opened_at,
                conn=conn, source_user_id=source_user_id,
            )
            record = True
        out[arm] = {"assignment": assignment, "record": record}
    # code_sha is provenance for memecoin_policy_versions.code_commit, NOT cohort identity.
    code_sha = runtime_release_sha()
    cohort_id, cohort_json = build_paper_cohort(arms, notional_usd=notional_usd)
    return out, {"id": cohort_id, "json": cohort_json, "code_sha": code_sha}


def _open_arm(conn, ev, size_usd, price, source, entry_liq, now_iso, strategy, token_age_s=None,
              entry_mcap=None, ticker_fallback=None, chain_slug=None, snapshot_ref=None,
              policy_assignment=None, cohort_ref=None, entry_provenance=None):
    """Open ONE arm's position at a SHARED, pre-resolved entry fill (dedup per (uid,mint,arm))."""
    network_id = _entry_network(conn, ev, chain_slug)
    # Dedup across ALL network buckets, not just the resolved one: an open row can still be
    # tagged 'evm-unknown' (pre-heal) while this event resolves concrete — a bucket-scoped
    # check would double-open the same coin into the same arm and pollute the A/B.
    if m.open_positions_for_coin(conn, ev.source_user_id, ev.mint, strategy):
        return "skipped_dup"
    if policy_assignment is None or cohort_ref is None:
        resolved, cohort_ref = _paper_assignments(
            conn, snapshot_ref, (strategy,), entry_mcap=entry_mcap,
            network_id=network_id, opened_at=now_iso, notional_usd=size_usd,
            source_user_id=ev.source_user_id,
        )
        policy_assignment = resolved[strategy]
    assigned_policy_id, assigned_policy_version, assigned_policy_json = (
        policy_assignment["assignment"]
    )
    record_assignment = policy_assignment["record"]
    provenance = entry_provenance or {}
    qty = size_usd / price
    # TAKE THE WRITE LOCK BEFORE THE SNAPSHOT (2026-09-05). `SAVEPOINT` on a connection with no
    # transaction begins a DEFERRED one, and `open_position` SELECTs before it INSERTs, which pins
    # a read snapshot. SQLite does NOT call the busy handler when a read transaction upgrades to a
    # write, so any writer committing in between fails us instantly with SQLITE_BUSY_SNAPSHOT
    # (517) or SQLITE_BUSY (5) and the 60s `busy_timeout` never applies. Measured in isolation:
    # 517 raised in 0.000 seconds. On 2026-09-05 that cost 5 of 23 "paper fan-out failed" events
    # their positions; the funded hook runs first, so funded entries were never affected.
    #
    # BEGIN IMMEDIATE takes the write lock up front and DOES wait on the busy handler.
    began = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        began = True
    conn.execute("SAVEPOINT paper_entry")
    try:
        pid = m.open_position(
            conn, source_user_id=ev.source_user_id, source_handle=ev.source_handle,
            mint=ev.mint, ticker=(ev.ticker or ticker_fallback or ""), entry_price=price,
            entry_notional=size_usd,
            entry_fill_source=source, entry_lag_s=ev.lag_s, their_size_usd=ev.their_size_usd,
            entry_liquidity=entry_liq, entry_token_age_s=token_age_s, entry_mcap=entry_mcap,
            opened_at=now_iso,
            signal_source=getattr(ev, "signal_source", "global_feed"), strategy=strategy,
            origin_event_id=getattr(ev, "fomo_event_id", None),
            network_id=network_id, assigned_policy_id=assigned_policy_id,
            assigned_policy_version=assigned_policy_version,
            assigned_policy_json=assigned_policy_json,
            paper_cohort_id=cohort_ref["id"], paper_cohort_json=cohort_ref["json"],
            experiment_status=("excluded_fill" if source == "fomo" else "eligible"),
            their_price=provenance.get("source_price"),
            fill_gap_pct=provenance.get("fill_gap_pct"),
            price_attribution=provenance.get("price_attribution"),
            quote_price=provenance.get("quote_price"),
            quote_observed_at=provenance.get("quote_observed_at"),
            source_drift_pct=provenance.get("source_drift_pct"),
            execution_slippage_pct=provenance.get("execution_slippage_pct"),
            total_copy_drift_pct=provenance.get("total_copy_drift_pct"), commit=False)
        m.insert_fill(
            conn, position_id=pid, kind="entry", price=price, qty=qty,
            notional=size_usd, fill_source=source, fraction=1.0, created_at=now_iso,
            origin_event_id=getattr(ev, "fomo_event_id", None), network_id=network_id,
            signal_source=getattr(ev, "signal_source", "global_feed"),
            source_price=provenance.get("source_price"),
            price_attribution=provenance.get("price_attribution"),
            quote_price=provenance.get("quote_price"),
            quote_observed_at=provenance.get("quote_observed_at"),
            source_drift_pct=provenance.get("source_drift_pct"),
            execution_slippage_pct=provenance.get("execution_slippage_pct"),
            total_copy_drift_pct=provenance.get("total_copy_drift_pct"), commit=False,
        )
        # THE PAPER FILL'S OWN POOL (Gamma 3): what the v1 tick engine binds to, as funded binds
        # to its own receipt. Absent when the entry reused the funded quote or priced off a mid.
        from sniper.services.memecoin_paper_ticks import entry_binding
        binding = entry_binding(network_id, provenance.get("route_binding"),
                                provenance.get("token_decimals"))
        if binding is not None:
            m.record_executed_pool(conn, pid, binding["pool"], "paper_quote_route", commit=False)
            m.set_paper_tick_json(conn, pid, json.dumps(binding, sort_keys=True), commit=False)
    except sqlite3.IntegrityError:
        # lost the check-then-insert race to a concurrent open (main poll vs follow-activity
        # thread); the partial UNIQUE(is_open=1) index rejected us -> treat as a dup, not a crash.
        conn.execute("ROLLBACK TO paper_entry")
        conn.execute("RELEASE paper_entry")
        if began:
            # The pass continues into network calls from here; carrying the write lock out of this
            # function would block every other writer for the length of them.
            conn.rollback()
        return "skipped_dup"
    except Exception:
        conn.execute("ROLLBACK TO paper_entry")
        conn.execute("RELEASE paper_entry")
        if began:
            conn.rollback()
        raise
    conn.execute("RELEASE paper_entry")
    conn.commit()
    if record_assignment and snapshot_ref and snapshot_ref.get("episode_id"):
        try:
            m.record_policy_assignment(
                conn, episode_id=snapshot_ref["episode_id"], arm_id=strategy,
                policy_id=assigned_policy_id, policy_version=assigned_policy_version,
                parameters_json=assigned_policy_json,
                entry_snapshot_id=snapshot_ref.get("snapshot_id"), assigned_ts=now_iso,
                code_commit=cohort_ref.get("code_sha"),
            )
        except Exception:
            logger.exception("policy assignment audit failed position=%s arm=%s", pid, strategy)
    return "opened"


def paper_chase_cap_pct(entry_mcap, network_id):
    """Paper band-cap approximation, in percent; zero disables this gate.

    Reuses funded settings and the above-nano ceiling, but omits event freshness,
    wallet overrides and 4663 display drift. This value is a paper admission rule,
    not a funded admission verdict.

    WHICH WAY IT DIFFERS FROM FUNDED DEPENDS ON CONFIG, so no fixed direction is claimed. The
    omitted 4663 nano display-mismatch branch uses MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT:
    at the code default of 10 it would make funded STRICTER than this, but the live value on
    2026-09-24 is 0, which switches funded's gate OFF on that branch -- funded is LOOSER there.
    Wallet overrides and the fresh-follow waiver only ever loosen funded. Read the knob from the
    running `.env` before stating which side is stricter; a worktree has none and reads the default.
    """
    from sniper.settings import memecoin as _cfg
    from sniper.services.memecoin_market_state import _coin_band
    from sniper.services.memecoin_real_runner import over_nano_chase_cap
    is_nano = _coin_band(entry_mcap, False) in {"nano", "assumed_nano"}
    base = float(getattr(_cfg, "MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT", 0.0) or 0.0) if is_nano \
        else float(getattr(_cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 0.0) or 0.0)
    return float(over_nano_chase_cap(base, network_id, is_nano) or 0.0)


def _mirror_size_holds(conn, ev, pos, now_iso, strategy):
    """True when the source's sell is too small a share of its bag for funded to have mirrored.

    ⚠️ THE PAPER MIRROR WAS UNCONDITIONAL AND FUNDED'S IS NOT (2026-09-24, operator: the paper
    should mimic the funded runner). Since `MEMECOIN_REAL_MIRROR_SIZE_CHAINS` was set to solana on
    2026-09-24T06:46:19Z, funded mirrors only a sell of at least
    `MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION` of the wallet's holding; a smaller sell is a trim and
    the position keeps its rails. Every paper mirror on those chains was therefore an exit funded
    would not have taken.

    Uses funded's `source_sell_share` arithmetic with the paper row's frozen threshold.
    Unknown, inconsistent or failed sizing holds: none proves the trader left.
    The frozen paper mirror flag takes precedence; funded's live size overlay can
    override its frozen flag. These are separate policy contracts, not exit parity.

    ⚠️ THE ANCHOR DIFFERS IN KIND AND IT IS STATED RATHER THAN HIDDEN. Funded measures sells since
    `opened_recorded_at`, the CONFIRMED FILL, because a trader sell landing between decision and
    fill has not sold anything we held yet. A paper position has no fill confirmation, so its
    anchor is `opened_at`, which for paper IS the open — same intent, slightly wider window.
    """
    import json
    from sniper.settings import memecoin as _cfg
    from sniper.services.memecoin_policy import paper_policy_value
    chains = getattr(_cfg, "MEMECOIN_REAL_MIRROR_SIZE_CHAINS", frozenset())
    network = str(pos.get("network_id") or getattr(ev, "network_id", "") or "").strip().lower()
    if network not in chains:
        return False                      # off those chains funded mirrors unconditionally too
    minimum = float(paper_policy_value(pos, "mirror_size_min_fraction", 0.0) or 0.0)
    if minimum <= 0:
        return False
    try:
        from sniper.services.memecoin_real_runner import source_sell_share
        trades = m.source_trades_for_coin(conn, pos.get("source_user_id"), pos.get("mint"),
                                          pos.get("network_id"))
        event_id = getattr(ev, "fomo_event_id", None)
        if event_id and not any(t.get("fomo_event_id") == event_id for t in trades):
            trades.append({"fomo_event_id": event_id, "side": "sell",
                           "their_price": getattr(ev, "their_price", None),
                           "their_size_usd": getattr(ev, "their_size_usd", None),
                           "created_at": getattr(ev, "created_at", None)})
        fraction, facts = source_sell_share(
            trades, opened_at=pos.get("opened_at"),
            lookback_s=float(getattr(_cfg, "MEMECOIN_REAL_MIRROR_SIZE_LOOKBACK_S", 86400.0)))
    except Exception as exc:
        logger.exception("paper mirror size check failed for %s", pos.get("id"))
        fraction, facts = None, {"error_type": type(exc).__name__}
    if fraction is None:
        code = "size_inconsistent" if (facts or {}).get("inconsistent") else "size_unknown"
    elif fraction >= minimum:
        code = "size_mirror"
    else:
        code = "size_hold"
    try:
        # Its own reason: `memecoin_exit_trigger_audit` upserts on (position_id, reason,
        # checked_minute), so sharing funded's `mirror` label would replace one verdict with the
        # other whenever both land in the same minute.
        m.record_exit_trigger_audit(
            conn, position_id=pos["id"], reason="paper_mirror", display_price=None,
            preview_usdc=None, allowed=(code == "size_mirror"),
            detail=json.dumps({"code": code, "arm": strategy, "anchor": "opened_at",
                               "sold_fraction": None if fraction is None else round(fraction, 4),
                               "min_fraction": minimum,
                               "mirror_policy_source": "frozen_paper_policy",
                               "error_type": (facts or {}).get("error_type")}, sort_keys=True),
            checked_at=now_iso, lane="paper_mirror_on_sell")
    except Exception:
        logger.exception("could not record the paper mirror witness for %s", pos.get("id"))
    return code != "size_mirror"


def _mirror_close_arm(conn, ev, now_iso, strategy, *, quote_fn=None, pos=None):
    """Mirror-close ONE mirroring arm at an EXECUTABLE whole-bag route quote.

    Fill model v2 (2026-08-05 review finding #2): the old path priced mirror closes off a
    DexScreener mid (modeled slippage) with a `their_price` 'fomo' fallback. On the
    DexScreener-blind chain (4663) that meant EVERY mirror close booked source='fomo', and one
    fomo fill censors its whole origin from the strict forward cohort — so robin copy-arm
    evidence could never accrue, and mirror-heavy arms were priced by a different (more
    optimistic) model than the sweep's route-quoted closes. An unavailable or spike-quarantined
    quote now books a `_noquote` close at entry — which the report censors — never a mid or the
    trader's own price."""
    if pos is None:
        pos = m.get_open_position(
            conn, ev.source_user_id, ev.mint, strategy,
            network_id=getattr(ev, "network_id", None),
        )
    if not pos:
        # Sell events often omit/under-resolve the chain (follow_activity, 4663 gaps) while
        # the entry stored a resolved identity — an exact-bucket miss must not skip the
        # mirror-exit. Fall back to the network-agnostic row; refuse only genuine ambiguity.
        cands = m.open_positions_for_coin(conn, ev.source_user_id, ev.mint, strategy)
        if len(cands) == 1:
            pos = cands[0]
        elif cands:
            logger.warning(
                "mirror-close ambiguous across chains uid=%s mint=%s arm=%s n=%d — skipped",
                ev.source_user_id, ev.mint, strategy, len(cands))
            return "ignored"
        else:
            return "ignored"
    if _mirror_size_holds(conn, ev, pos, now_iso, strategy):
        return "mirror_size_hold"
    if not m.claim_close(conn, pos["id"]):
        return "ignored"
    qty = _qty_of(conn, pos["id"])
    entry = pos.get("entry_price") or 0.0
    reason = "b_mirror" if strategy in ("alpha_b", "real_shadow") else "mirror"
    if qty <= 0:
        # drained by prior partials with no close: finalize from the booked legs, book nothing new.
        _close_instrumentation(conn, pos, now_iso)
        m.finalize_close(conn, pos["id"], now_iso,
                         _realized_from_exits(conn, pos["id"], entry), exit_reason=reason)
        return "exit"
    price, source = (quote_fn or executable_exit_fill)(pos["mint"], pos.get("network_id"), qty)
    if price is not None and price > 0:
        # Bad-tick guard (2026-07-23, Jimothy): a wrong-denominated quote once booked +$6.0M of
        # phantom paper PnL across every arm and poisoned wallet copy-PnL stats (arming
        # evidence). Same rejection rule as the sweep guard, judged only against POST-ENTRY
        # observations (peak / last clean quote; an exact-entry peak is initialization, not an
        # observation). A quarantined quote is treated as UNAVAILABLE — censored, never
        # substituted with a stale mid or the trader's own price.
        import os as _os
        _limit = float(_os.getenv("MEMECOIN_SPIKE_RATIO_LIMIT", "10.0"))
        prior = max((v for v in (pos.get("peak_price"), pos.get("last_quote_price"))
                     if v and v > 0 and v != entry), default=0.0)
        if prior > 0 and price / prior >= _limit:
            logger.warning(
                "mirror bad-tick quarantined mint=%s arm=%s quote=%.6g prior=%.6g (%.0fx)",
                ev.mint, strategy, price, prior, price / prior)
            price, source = None, "mirror_bad_tick_quarantined"
    censor_cause = censor_rail = None
    if price is None or price <= 0:
        censor_cause, censor_rail = source or "nonpositive_quote", reason
        exit_price, fill_source, reason = entry, "noquote", reason + "_noquote"
    else:
        exit_price, fill_source = price, source
    m.insert_fill(conn, position_id=pos["id"], kind="mirror_exit", price=exit_price,
                  qty=qty, notional=exit_price * qty, fill_source=fill_source,
                  fraction=1.0, created_at=now_iso,
                  # entry price substituted for an absent quote: a placeholder, not an observation
                  price_attribution=(m.PLACEHOLDER_PRICE_ATTRIBUTION
                                     if fill_source == "noquote" else None))
    _close_instrumentation(conn, pos, now_iso)
    m.finalize_close(conn, pos["id"], now_iso,
                     _realized_from_exits(conn, pos["id"], entry), exit_reason=reason)
    if censor_cause is not None:
        record_noquote_cause(conn, pos["id"], censor_cause, censor_rail, now_iso)
    return "exit"


# Fill-model identity. This string is hashed into the paper cohort contract
# (memecoin_policy.build_paper_cohort), so changing it forks the cohort automatically and rows
# priced by the two models can never pool into one mean — the whole point of versioning it.
# v2 (2026-08-05): mirror exits moved from DexScreener-mid/fomo pricing onto the same
# executable route quotes the sweep books — v1 rows mixed two exit fill models per arm.
from sniper.execution.solana_real_seams import (       # noqa: E402
    ROLE_PAPER, jupiter_role)

PAPER_FILL_MODEL = "jupiter-executable-route-v2"


def _quote_price_to_usd(price, network_id):
    """Convert a route-quote unit price into USD per token, or (None, reason).

    Chain 4663 quotes in WETH (CHAIN_QUOTE kind='eth'); every other chain quotes in a USD
    stablecoin. Storing the WETH-denominated number alongside USD marks inflated every 4663
    position by the ETH/USD rate — a $100 GME clip booked $202,724 of fake profit on a mirror
    exit because entry was WETH/token and the exit mark was USD/token (~2027x).

    Fails CLOSED: no quote-asset price means no fill, never an unconverted number.
    """
    from sniper.services.memecoin_quotes import CHAIN_QUOTE, quote_usd_price
    quote = CHAIN_QUOTE.get(str(network_id))
    if not quote or quote.get("kind") == "usd":
        return price, None                      # already USD per token
    rate = quote_usd_price(str(network_id))
    if not rate or float(rate) <= 0:
        return None, "quote_asset_price_unavailable"
    return price * float(rate), None


def executable_entry_fill(mint, network_id, size_usd, *, quote_provenance=None):
    """One EXECUTABLE route-quote fill price for a paper entry, or (None, reason).

    Reuses the V6 quote path (`memecoin_quotes.quote_entry`) rather than re-deriving it:
    it resolves token decimals properly — the lite-api omits them and the old code's 6dp default
    mispriced 9-decimal tokens 1000x — and it raises instead of substituting a non-executable
    price. Optional provenance is from that same response; observed_at is local
    `real_utc_now_iso` completion time, not a provider source timestamp. An unavailable quote is a
    skip, not a guess.
    """
    from sniper.services.memecoin_quotes import quote_entry
    from sniper.settings import memecoin as _cfg
    from types import SimpleNamespace
    try:
        # PAPER KEY, PER CALL. These quotes run INSIDE the funded runner, so a
        # process-level declaration cannot separate them — declaring would hand the
        # funded exit brain the paper key. On 2026-08-29 the pooled budget ran the
        # runner to 40% rate-limited and blinded a funded position's exact previews
        # 48 times in 62 seconds. Scoped at the call, not around the loop.
        with jupiter_role(ROLE_PAPER):
            fill = quote_entry(
                SimpleNamespace(mint=mint, network_id=network_id), float(size_usd),
                solana_rpc_url=_cfg.MEMECOIN_SOLANA_RPC_URL,
                evm_rpc_url=_cfg.evm_rpc_for_chain(network_id) if str(network_id) != "solana" else None,
            )
        price = float(fill.unit_price)
        if price <= 0:
            return None, "nonpositive_quote"
        # Normalise to USD per token so entries can never be compared against USD marks in a
        # different unit (the 4663 WETH-vs-USD mismatch, ~1900x).
        price, reason = _quote_price_to_usd(price, network_id)
        if price and quote_provenance is not None:
            quote_provenance.update(
                observed_at=fill.observed_at, provider=fill.quote_provider,
                route_binding=dict(fill.route_binding), fingerprint=fill.quote_fingerprint,
            )
            # The coin's decimals the quote read, for the paper tick engine's binding only; the
            # caller takes it out before the snapshot so its paper_quote payload is unchanged.
            decimals = getattr(fill, "token_decimals", None)
            if decimals is not None:
                quote_provenance["_token_decimals"] = decimals
        return (price, "route_quote") if price else (None, reason or "nonpositive_quote")
    except Exception as exc:
        # NAME THE CAUSE. `quote_entry` attaches `quote_reason`/`quote_host` when Jupiter answered,
        # so a rate limit and a genuinely unroutable coin stop sharing one label. Anything without
        # a reason stays `quote_unavailable`, which now means "we could not even classify it".
        # THE REASON EXISTED AND A LEVEL FILTER DESTROYED IT. `quote_entry` attaches
        # `quote_reason` on exactly ONE of its raise paths, so 5,002 of 5,003 skips landed in the
        # unclassified bucket — and the exception TYPE, which would have decomposed them, was
        # formatted into the `logger.info` below and then dropped: zero occurrences in 564,097
        # runner log lines. A decomposable failure destroyed at the last step is a different defect
        # from one never captured, and it is fixed by recording the type instead of logging it.
        #
        # FORWARD ONLY from 2026-08-29. The existing 5,002 carry no reason in any column and no
        # surviving log line, so they cannot be decomposed and are NOT attributed. Until this has
        # accrued, the denominator for any quote-failure rate is that whole bucket with unknown
        # composition; a share over the labelled rows alone is a share over n=1.
        reason = (getattr(exc, "quote_reason", None)
                  or "quote_unavailable_%s" % type(exc).__name__)
        host = getattr(exc, "quote_host", None)
        logger.info("paper entry quote unavailable mint=%s net=%s reason=%s host=%s (%s)",
                    mint, network_id, reason, host, type(exc).__name__)
        # THE HOST TRAVELS WITH THE REASON. It was logged and then dropped, so no per-host baseline
        # was possible — the same gap JUPITER_LOAD had to close after the fact, reintroduced. A log
        # line is not a record: `reason@host` reaches the snapshot, where it can be counted.
        return None, ("%s@%s" % (reason, host)) if host else reason


def _exit_token_decimals(mint, network_id):
    """Token decimals for the human->atomic qty conversion, or None (caller fail-closes).

    Solana resolves via getTokenSupply, EVM chains via decimals(). Hardcoding None for every
    non-Solana chain made `executable_exit_fill` return `qty_unresolved` BEFORE it ever reached
    `quote_exit`, which has a complete 4663 sell branch — so chain 4663 never attempted a paper
    exit route quote in its life (868/868 ENTRIES priced route_quote, 0 of 387 full exits did).
    Every close booked a `noquote` leg the report censors, leaving all eight forward arms with
    zero measurable closes on that chain (audit 2026-08-05).
    """
    from sniper.services.memecoin_quotes import erc20_decimals, token_decimals
    from sniper.settings import memecoin as _cfg
    if str(network_id) == "solana":
        return token_decimals(mint, _cfg.MEMECOIN_SOLANA_RPC_URL)
    rpc = _cfg.evm_rpc_for_chain(network_id)
    return erc20_decimals(mint, rpc) if rpc else None


def executable_exit_fill(mint, network_id, qty):
    """EXECUTABLE whole-bag SELL price for `qty` tokens, or (None, reason).

    Exit twin of `executable_entry_fill`. The old path priced every paper exit off the same
    DexScreener mid the entry used, so a dumping coin was sold at a stale HIGHER price and the
    optimism compounded on both legs of the trade. Callers must route an unavailable quote into
    the existing `noquote` handling — which the report already CENSORS — never into a mid.
    """
    from sniper.services.memecoin_quotes import quote_exit
    from sniper.settings import memecoin as _cfg
    try:
        rpc = _cfg.MEMECOIN_SOLANA_RPC_URL
        dec = _exit_token_decimals(mint, network_id)
        position = {"mint": mint, "network_id": network_id, "token_decimals": dec}
        qty_atomic = int(float(qty) * (10 ** int(dec))) if dec is not None else None
        if not qty_atomic or qty_atomic <= 0:
            return None, "qty_unresolved"
        # PAPER KEY, PER CALL. These quotes run INSIDE the funded runner, so a
        # process-level declaration cannot separate them — declaring would hand the
        # funded exit brain the paper key. On 2026-08-29 the pooled budget ran the
        # runner to 40% rate-limited and blinded a funded position's exact previews
        # 48 times in 62 seconds. Scoped at the call, not around the loop.
        with jupiter_role(ROLE_PAPER):
            fill = quote_exit(
                position, qty_atomic, rpc_url=rpc,
                evm_rpc_url=_cfg.evm_rpc_for_chain(network_id) if str(network_id) != "solana" else None,
            )
        price = float(fill.unit_price)
        if price <= 0:
            return None, "nonpositive_quote"
        price, reason = _quote_price_to_usd(price, network_id)
        return (price, "route_quote") if price else (None, reason or "nonpositive_quote")
    except Exception as exc:
        logger.info("paper exit quote unavailable mint=%s net=%s (%s)",
                    mint, network_id, type(exc).__name__)
        # TYPED, like the entry twin at `executable_entry_fill`. An untyped string cannot tell a
        # rate limit from a dead route from a transport failure, and this reason is now persisted.
        # No consumer compares against the old literal: the only `"quote_unavailable"` equality in
        # the tree (line ~784) is on the ENTRY quote's reason, which comes from a different call.
        return None, "exception:%s" % type(exc).__name__


# The audit reason is this writer's ALONE. `memecoin_exit_trigger_audit` upserts on
# (position_id, reason, checked_minute), so sharing a reason with a rail would let one row replace
# the other inside the same minute -- the 2026-09-23 debounce_pending loss, which cost a 65% firing
# rate its evidence.
NOQUOTE_CAUSE_REASON = "paper_noquote_cause"


def record_noquote_cause(conn, position_id, cause, rail, now_iso):
    """Persist WHY a paper exit was censored, once, beside the close it censored.

    `executable_exit_fill` returns (None, reason) and BOTH censor sites used to discard the reason,
    so six distinct failures -- qty_unresolved, nonpositive_quote, quote_asset_price_unavailable,
    exception:<Type>, the plausibility veto in the sweep and the bad-tick quarantine in the mirror
    -- all read as the single label `<rail>_noquote`. The ledger could not say which, and the two
    silent ones write nothing anywhere: `grep -c "paper exit quote unavailable"` over the whole
    24 MB runner log returns 0, so every censored row measured on 2026-09-24 came from a cause that
    left no trace at all.

    `rail` is the exit reason BEFORE `_noquote` was appended, and it is what keeps an `unquotable`
    censor -- where the rail had already given up before any quote was asked -- from pooling with a
    live TP or stop that simply could not be priced. It is a field rather than part of the cause
    literal because the alternative is the cartesian product of every cause with every rail.

    Best-effort: this is measurement beside a close that has already happened. It never raises into
    the close path.
    """
    try:
        m.record_exit_trigger_audit(
            conn, position_id=position_id, reason=NOQUOTE_CAUSE_REASON,
            display_price=None, preview_usdc=None, allowed=0,
            detail=json.dumps({"cause": cause, "rail": rail}),
            checked_at=now_iso, lane="paper_exit")
    except Exception:
        logger.exception("noquote cause audit failed position=%s cause=%s rail=%s",
                         position_id, cause, rail)


def arm_base_size(arms, their_size_usd, size_usd):
    """The base clip for this fan-out, before any funded clip overrides it.

    `funded_clone` exists to mimic the funded runner, and the funded runner does NOT scale by the
    copied trader's size — it is handed `MEMECOIN_TRADE_SIZE_USD` and re-sizes by bankroll. Keeping
    conviction sizing on that arm would reintroduce a difference the arm exists to remove. Every
    other arm keeps the behaviour it was measured with.
    """
    if set(arms or ()) == {"funded_clone"}:
        return float(size_usd)
    return conviction_size(their_size_usd, size_usd)


def copy_event_to_arms(conn, ev, size_usd, now_iso, *, arms, fill_fn=honest_fill, stats_fn=None,
                       chase_below_pct=None, quote_fn=None, exit_quote_fn=None,
                       funded_quote=None, funded_clip=None):
    """Fan ONE feed event to all arms sharing a SINGLE entry fill + chase decision, so the exit
    policy is the only variable across arms (spec §2) and we make one quote call, not one per arm.
    Returns {arm: outcome}. `chase_below_pct` (opt-in, real path only) also skips when our fill is
    more than that % BELOW the trader's price — a dump-in-progress / falling knife; None (default)
    leaves the paper A/B behavior unchanged (above-guard only)."""
    from sniper.settings.memecoin import MEMECOIN_CHASE_MAX_PCT
    # NO ARMS, NO WORK (2026-08-27, outside review). Every outcome below is a dict comprehension
    # over `arms`, so an empty list already returned {} — but only AFTER buying an executable route
    # quote and writing an action='opened' entry snapshot. With MEMECOIN_ARMS emptied on 08-23 that
    # ran for five days: 1,899 "opened" snapshots against 8 real paper positions, and roughly 380
    # Jupiter route quotes a day (~11% on top of the funded runner's own load) spent by a lane
    # everyone believed dead — including my own provider scoping, which said the paper lane does
    # not consume Jupiter. It does: the STATS call is DexScreener, but the FILL price comes from
    # executable_entry_fill -> the V6 quote path -> the Jupiter lite-api.
    #
    # Returning here is byte-identical for the caller ({} either way) and writes no snapshot. The
    # consumers of memecoin_entry_snapshots look rows up by upstream_event_id rather than filtering
    # on action, so an absent row for an event that produced no position is correct — it is the
    # same state the event would have had before it ever reached this function.
    if not arms:
        return {}
    if ev.side == "buy":
        # PER-ARM CHAIN SCOPE (2026-09-22). Checked HERE, above the quote, for the reason the note
        # above gives: an arm that will not trade this chain must not buy a route quote first. An
        # arm with no knob allows every chain, so an unset environment does not reach this branch.
        # Sells are never scoped: a position this lane already opened must always be closable.
        from sniper.settings.memecoin import paper_arm_allows_chain, paper_arm_samples_event
        # ORIGIN SAMPLER. Same placement and the same reason: a sampled-out event must not buy a
        # quote. Keyed on the origin, so every arm drops the same events and stays comparable.
        if not paper_arm_samples_event(getattr(ev, "fomo_event_id", None)):
            _log_entry_snapshot(conn, ev, now_iso, "skipped_sampled_out")
            return {a: "skipped_sampled_out" for a in arms}
        _chain = getattr(ev, "network_id", None)
        _scoped = [a for a in arms if paper_arm_allows_chain(a, _chain)]
        if not _scoped:
            _log_entry_snapshot(conn, ev, now_iso, "skipped_chain_scope")
            return {a: "skipped_chain_scope" for a in arms}
        if len(_scoped) != len(arms):
            out = {a: "skipped_chain_scope" for a in arms if a not in _scoped}
            out.update(copy_event_to_arms(
                conn, ev, size_usd, now_iso, arms=_scoped, fill_fn=fill_fn, stats_fn=stats_fn,
                chase_below_pct=chase_below_pct, quote_fn=quote_fn, exit_quote_fn=exit_quote_fn,
                funded_quote=funded_quote, funded_clip=funded_clip))
            return out
        # OPEN-COIN CAP (2026-09-23). Same placement and the same reason as the two gates above:
        # an event this lane will not open must not buy a route quote first.
        #
        # It counts COINS, not positions and not arms. `bracket_pass` prices ONE batched map keyed
        # by `coin_identity` over every open position, so a coin already open costs nothing more —
        # and capping a coin we already hold would censor exactly the re-entry origins this arm
        # exists to measure. The blind sub-cap counts non-Solana coins only: Solana rides the
        # batched DexScreener call, a blind-chain coin enters the GeckoTerminal pass instead.
        #
        # A bound that binds is a MEASURED quantity, not a silence, so it lands in the same durable
        # channel the sampler and the chain scope use and the read counts it as not-observed.
        from sniper.settings.memecoin import (MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS,
                                              MEMECOIN_PAPER_ARM_MAX_OPEN_COINS)
        _identity = m.coin_identity(getattr(ev, "network_id", None), ev.mint)
        if MEMECOIN_PAPER_ARM_MAX_OPEN_COINS > 0 or MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS > 0:
            _open = m.open_paper_coin_identities(conn)
            if _identity not in _open:
                if (MEMECOIN_PAPER_ARM_MAX_OPEN_COINS > 0
                        and len(_open) >= MEMECOIN_PAPER_ARM_MAX_OPEN_COINS):
                    _log_entry_snapshot(conn, ev, now_iso, "skipped_open_coin_cap")
                    return {a: "skipped_open_coin_cap" for a in arms}
                if (MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS > 0 and _identity[0] != "solana"
                        and sum(1 for k in _open if k[0] != "solana")
                        >= MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS):
                    _log_entry_snapshot(conn, ev, now_iso, "skipped_open_blind_cap")
                    return {a: "skipped_open_blind_cap" for a in arms}
        # cash/base assets (WETH, cbBTC, USDC…) are routing legs, not memecoin signals — the
        # feed emits "bought WETH" when a trader routes through it; copying one produced
        # unmanageable positions at garbage parsed prices (2026-07-10 cleanup).
        from sniper.analysis.fomo_account.classify import is_cash_asset
        if is_cash_asset(ev.mint):
            _log_entry_snapshot(conn, ev, now_iso, "skipped_base_asset")
            return {a: "skipped_base_asset" for a in arms}
        # A paper position already open in every requested arm cannot use another entry quote.
        # _open_arm keeps its check under insertion for concurrent opens.
        if all(m.open_positions_for_coin(conn, ev.source_user_id, ev.mint, a) for a in arms):
            _log_entry_snapshot(conn, ev, now_iso, "skipped_dup")
            return {a: "skipped_dup" for a in arms}
        network_id = getattr(ev, "network_id", None)
        entry_stats = None
        quote_provenance = {}
        token_decimals = None
        # THE FUNDED EVENT'S OWN QUOTE, AT THE FUNDED INSTANT (2026-09-24, operator: the paper must
        # mimic the funded runner). The fan-out runs AFTER the funded hook and used to buy a second
        # quote seconds later; on MANLETS that was 2.738 s and cost 11.75% — paper entered at
        # 1.02274e-04 against the funded 9.15237e-05 on the same signal. Size cannot explain it and
        # points the other way: paper bought $100 against $175, and a smaller clip prices BETTER on
        # a thin pool.
        #
        # Reused only when the funded path actually bought one, which is the FILLED events —
        # measured 6 of 229 origins since 2026-09-23T20:58Z, because a refusal usually never
        # reaches a quote. The rest buy the arm's own single quote exactly as before, and which one
        # was used is recorded on the row rather than inferred.
        # THE FUNDED CLIP, WHEN THE FUNDED PATH ACTUALLY SPENT ONE. Paper ran flat at
        # MEMECOIN_TRADE_SIZE_USD while funded re-sized by bankroll, so the same signal carried
        # $100 on paper against $175 on solana or $39.81 on 4663 and dollar PnL was never
        # comparable. A refusal never sized, so most events keep the base and say so.
        _clip = _positive((funded_clip or {}).get("usd"))
        size_source = "funded_clip" if _clip else "base_size"
        if _clip:
            size_usd = _clip
        funded_ticker = None
        reused = _positive((funded_quote or {}).get("price"))
        if reused:
            price, source = reused, "funded_quote_reuse"
            entry_stats, funded_ticker = _entry_facts(conn, ev, network_id, stats_fn)
        # The runtime passes honest_fill + dexscreener_stats. Both used to fetch the same pair,
        # serially, on every paper buy. Reuse the one stats response for modeled fill + features.
        elif fill_fn is honest_fill and stats_fn is not None:
            entry_stats, funded_ticker = _entry_facts(conn, ev, network_id, stats_fn)
            if quote_fn is None:
                price, _quote_source = executable_entry_fill(
                    ev.mint, network_id, size_usd, quote_provenance=quote_provenance)
            else:
                price, _quote_source = quote_fn(ev.mint, network_id, size_usd)
            token_decimals = quote_provenance.pop("_token_decimals", None)
            if quote_provenance:
                entry_stats = {**entry_stats, "paper_quote": quote_provenance}
            source = "paper_quote"
            if price is None:
                source = _quote_source
                # The action now carries WHY. `skipped_no_executable_quote` was one bucket over
                # `no_route`, `rate_limited` and `provider_error`, and 95% of curated-time buy
                # events landed in it — read as "unpriceable coins" when it may be substantially
                # our own keyless rate limit. Split BEFORE the paper Jupiter key lands, so the
                # key's effect is attributable instead of assumed.
                action = ("skipped_no_executable_quote"
                          if source in (None, "quote_unavailable")
                          else "skipped_quote_%s" % source)   # source is "reason@host"
                # Fail CLOSED. The old path fell back to a DexScreener mid here, which measured
                # a median -0.35% fill gap against the trader (i.e. paper claimed we filled BETTER
                # than the wallet we copy) while the funded leg measured +7.54% WORSE on the same
                # events. Booking a mid as if it were a fill is what made every paper dollar
                # unachievable; an unpriceable event is now no evidence rather than fake evidence.
                _log_entry_snapshot(conn, ev, now_iso, action, market_features=entry_stats)
                return {a: action for a in arms}
        else:
            price, source = _call_with_network(
                fill_fn, ev.mint, network_id, size_usd, "buy", ev.their_price,
            )
        if not price or price <= 0:
            _log_entry_snapshot(conn, ev, now_iso, "ignored")
            return {a: "ignored" for a in arms}
        chase_features = {"paper_chase_model": "band_cap_approximation_v1",
                          "paper_chase_cap_pct": None, "paper_chase_gap_pct": None}
        tp = ev.their_price
        if tp and tp > 0:
            gap_pct = (price / tp - 1.0) * 100.0
            # Persist the approximation used for this decision on admitted and refused rows.
            _chase_cap = paper_chase_cap_pct(
                _positive(getattr(ev, "their_market_cap", None))
                or _positive(((entry_stats or {}).get("funded_facts") or {}).get("effective_mcap")),
                getattr(ev, "network_id", None))
            chase_features.update(paper_chase_cap_pct=_chase_cap, paper_chase_gap_pct=gap_pct)
            if _chase_cap > 0 and gap_pct > _chase_cap:
                m.set_event_chase(conn, ev.fomo_event_id, price, gap_pct)
                # THE FAMILY NAME IS PRE-REGISTERED AND STAYS (2026-09-24, Alpha's ruling on
                # Codex 44951cd2). `skipped_chase_over_funded_cap` is the family registered in
                # docs/audits/2026-09-23-refusal-family-paper-prereg.md and carried by 41 ledger
                # rows before this change; renaming it would split the series, and every reader
                # keyed on it would go blind without an error. The name overstates the match -- this
                # is paper's band-cap APPROXIMATION of funded's cap, labelled on the row by
                # `paper_chase_model` -- and the rename's premise was a read without `.env`: the
                # code default for MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT is 10, the live value
                # is 0, which turns funded's gate OFF on that branch rather than tightening it.
                _log_entry_snapshot(conn, ev, now_iso, "skipped_chase_over_funded_cap",
                                    executable_price=price,
                                    market_features={**(entry_stats or {}), **chase_features})
                return {a: "skipped_chase_over_funded_cap" for a in arms}
            if chase_below_pct is not None and gap_pct < -abs(chase_below_pct):
                m.set_event_chase(conn, ev.fomo_event_id, price, gap_pct)
                _log_entry_snapshot(conn, ev, now_iso, "skipped_dump",
                                    executable_price=price,
                                    market_features={**(entry_stats or {}), **chase_features})
                return {a: "skipped_dump" for a in arms}    # falling knife: dump already in progress
        if entry_stats is None:
            entry_stats, funded_ticker = _entry_facts(conn, ev, network_id, stats_fn)
        funded = entry_stats.get("funded_facts") or {}
        # NULL, NOT THE FUNDED NUMBER (paper step 3): our liquidity (E6: pool-store virtual depth or
        # the implied quote reserve) and the pair age are not the provider quantities these columns
        # held. Both stay in funded_facts under their own names; an unknown entry_liq keeps the
        # unknown-liquidity fill rule. The network comes from the event, the ticker from the attempt.
        entry_liq = None
        token_age = None
        symbol = funded_ticker
        chain_slug = None
        # signal event mcap -> the funded decision's mcap (its basis in funded_facts) -> median of
        # recent feed readings for this mint, so per-mcap policy/exit analytics have a value.
        entry_mcap = (_positive(getattr(ev, "their_market_cap", None))
                      or _positive(funded.get("effective_mcap"))
                      or _positive(m.resolve_mint_mcap(
                          conn, ev.mint, network_id=getattr(ev, "network_id", None))))
        if "funded_clone" in arms:
            # Saved gate inputs take precedence over the classification's restricted fact list.
            gate_stats = {key: value for key, value in (
                ("volume_m5", funded.get("volume_m5")), ("volume_h1", funded.get("volume_h1")),
                ("market_cap", funded.get("volume_mcap")), ("pair_age_s", funded.get("pair_age_s")))
                if value is not None}
            frozen_inputs = funded.get("gate_inputs") or {}
            gate_stats.update({key: frozen_inputs[key] for key in
                               ("volume_m5", "volume_h1", "txns_m5_buys", "txns_m5_sells")
                               if key in frozen_inputs})
            gate_stats.update(frozen_volume_gate=funded.get("volume_gate"),
                              frozen_gate_inputs=funded.get("gate_inputs"),
                              onchain_m5=funded.get("onchain_m5"), coin_band=funded.get("coin_band"))
            chase_features["paper_m5_gate"] = _paper_m5_gate(ev, gate_stats, entry_mcap, now_iso)
        snapshot_ref = _log_entry_snapshot(
            conn, ev, now_iso, "opened", mcap=entry_mcap, liquidity=entry_liq,
            token_age_s=token_age,
            # The size provenance rides the snapshot rather than a new column: a read has to be able
            # to separate rows sized like funded from rows sized at base, and averaging the two
            # would report a number that is neither.
            market_features={**(entry_stats or {}), **chase_features,
                             "paper_size_source": size_source,
                             "paper_size_usd": float(size_usd)},
            executable_price=price,
        )
        policy_network = _entry_network(conn, ev, chain_slug)
        assignments, cohort_ref = _paper_assignments(
            conn, snapshot_ref, arms, entry_mcap=entry_mcap,
            network_id=policy_network, opened_at=now_iso, notional_usd=size_usd,
            source_user_id=ev.source_user_id,
        )
        source_price = _positive(getattr(ev, "their_price", None))
        # ⚠️ `quote_price` IS THE ROUTE QUOTE ON BOTH SIDES NOW. It used to hold the DexScreener MID
        # here while the funded path held the route quote it signs against, so `fill/quote - 1` was
        # a mid-to-executable spread on one side (paper 11.74% mean) and true execution slippage on
        # the other (funded 0.86%), and the two were compared as if they were the same quantity. On
        # MANLETS the paper "quote" was 7.128e-05, twenty percent BELOW the source's own price,
        # because a stale mid is not a price anyone can trade at.
        #
        # The mid is not lost and needs no column: `_log_entry_snapshot` already stores the whole
        # stats payload, so `memecoin_entry_snapshots.features_json["price"]` is where it lives.
        quote_price = price
        fill_gap_pct = ((price / source_price - 1.0) * 100.0 if source_price else None)
        source_drift_pct = (
            (quote_price / source_price - 1.0) * 100.0
            if source_price and quote_price else None
        )
        # ABSENT, NOT ZERO. Paper never executes, so it has no fill to compare against its quote and
        # cannot measure execution slippage at all. Writing 0.0 would claim a measurement of zero.
        execution_slippage_pct = None
        entry_provenance = {
            "source_price": source_price,
            "quote_price": quote_price,
            "quote_observed_at": ((funded_quote or {}).get("observed_at")
                                  if source == "funded_quote_reuse"
                                  else quote_provenance.get("observed_at")),
            "fill_gap_pct": fill_gap_pct,
            "source_drift_pct": source_drift_pct,
            "execution_slippage_pct": execution_slippage_pct,
            "total_copy_drift_pct": fill_gap_pct,
            "price_attribution": (
                "dexscreener_modeled" if source == "dexscreener"
                else "fomo_unexecutable" if source == "fomo" else str(source or "unknown")
            ),
            # The pool our own paper quote routed through (spec 2026-10-08 paper exit ticks).
            "route_binding": (quote_provenance.get("route_binding")
                              if source == "paper_quote" else None),
            "token_decimals": token_decimals if source == "paper_quote" else None,
        }
        # momentum_a is a 15-min fast-ride (MEMECOIN_A_MAX_HOLD_S): on slow-quoting EVM chains its
        # short hold expires before the first DexScreener quote ever lands -> 100% a_timecap_noquote
        # at $0 on 4663 (77% on evm-unknown), while alpha_b's 12h hold outlasts the delay. Don't
        # open it off Solana; the long-hold / time-capless arms still run on every chain.
        net = str(getattr(ev, "network_id", "") or "").strip().lower()

        def _open(a):
            if a == "momentum_a" and not momentum_a_chain_allowed(net):
                return "skipped_slow_chain"
            return _open_arm(conn, ev, size_usd, price, source, entry_liq, now_iso, a,
                             token_age_s=token_age, entry_mcap=entry_mcap,
                             ticker_fallback=symbol, chain_slug=chain_slug,
                             snapshot_ref=snapshot_ref, policy_assignment=assignments[a],
                             cohort_ref=cohort_ref, entry_provenance=entry_provenance)
        return {a: _open(a) for a in arms}

    # sell: EXECUTABLE whole-bag route quote (fill model v2). One quote per (network, qty) per
    # event — every arm shares the entry fill, so un-partialed arms share one route call and a
    # derisked remainder (different qty) gets its own. momentum_a ignores sells.
    from sniper.services.memecoin_policy import paper_policy_is_valid, paper_policy_value
    exit_quote = exit_quote_fn or executable_exit_fill
    _quote_cache = {}

    def _shared_quote(mint, network_id, qty):
        key = (network_id, qty)
        if key not in _quote_cache:
            _quote_cache[key] = exit_quote(mint, network_id, qty)
        return _quote_cache[key]

    def _mirror(a):
        pos = m.get_open_position(
            conn, ev.source_user_id, ev.mint, a,
            network_id=getattr(ev, "network_id", None),
        )
        if not pos:
            # Same chain-omitted-sell fallback _mirror_close_arm carries: an exact-bucket miss
            # here used to return 'ignored' BEFORE that fallback could ever run, so a sell with
            # no network tag never mirror-closed a concretely-tagged paper position.
            cands = m.open_positions_for_coin(conn, ev.source_user_id, ev.mint, a)
            if len(cands) == 1:
                pos = cands[0]
            else:
                if cands:
                    logger.warning(
                        "mirror-close ambiguous across chains uid=%s mint=%s arm=%s n=%d — skipped",
                        ev.source_user_id, ev.mint, a, len(cands))
                return "ignored"
        if (pos.get("experiment_status") in ("eligible", "excluded_fill")
                and not paper_policy_is_valid(pos)):
            m.set_experiment_status(conn, pos["id"], "invalid_policy")
            pos["experiment_status"] = "invalid_policy"
        if not bool(paper_policy_value(pos, "mirror_on_sell", a in MIRROR_ARMS)):
            return "ignored"
        return _mirror_close_arm(conn, ev, now_iso, a, quote_fn=_shared_quote, pos=pos)

    return {a: _mirror(a) for a in arms}


def on_copy_event(conn, ev, size_usd, now_iso, fill_fn=honest_fill, stats_fn=None,
                  strategy="control", exit_quote_fn=None):
    """Single-arm wrapper over copy_event_to_arms (back-compat for tests + one-arm callers)."""
    return copy_event_to_arms(conn, ev, size_usd, now_iso, arms=[strategy],
                              fill_fn=fill_fn, stats_fn=stats_fn,
                              exit_quote_fn=exit_quote_fn)[strategy]


def partial_exit(conn, position_id, fraction, now_iso, price_fn=dexscreener_stats):
    """Manual partial mirror-exit: sell `fraction` of remaining qty at a live fill,
    realize that slice, keep the rest open. fraction>=1 (or dust remainder) fully closes."""
    pos = m.get_position(conn, position_id)
    if not pos or not pos.get("is_open"):
        return None
    fraction = max(0.0, min(1.0, float(fraction)))
    if fraction <= 0:
        return None
    # ATOMIC: claim the position (is_open 1->0) BEFORE reading remaining/booking, so a
    # concurrent bracket/mirror close or a second partial can't also book against the same
    # qty (cross-process double-sell / negative-qty / zombie). Loser of the flip returns None.
    if not m.claim_close(conn, position_id):
        return None
    remaining = m.open_qty(conn, position_id)
    if remaining <= 0:
        # already drained by prior partials without a close -> finalize the zombie (stays closed)
        m.finalize_close(conn, position_id, now_iso,
                         _realized_from_exits(conn, position_id, pos["entry_price"]),
                         exit_reason="partial")
        return {"realized_delta": 0.0, "remaining": 0.0, "closed": True, "price": None}
    st = _call_with_network(price_fn, pos["mint"], pos.get("network_id")) or {}
    mid = st.get("price")
    if not mid or mid <= 0:
        m.reopen_position(conn, position_id)   # can't price -> undo the claim, book nothing
        return None
    sell_qty = remaining * fraction
    left = remaining - sell_qty
    closed = fraction >= 1.0 or left <= 1e-9 * max(1.0, remaining)
    from sniper.execution.solana_quote import modeled_fill
    price = modeled_fill(mid, mid * sell_qty, st.get("liquidity"), "sell")
    realized_delta = (price - pos["entry_price"]) * sell_qty
    m.insert_fill(conn, position_id=position_id, kind="partial_exit", price=price,
                  qty=sell_qty, notional=price * sell_qty, fill_source="dexscreener",
                  fraction=fraction, created_at=now_iso)
    if closed:
        m.finalize_close(conn, position_id, now_iso,
                         _realized_from_exits(conn, position_id, pos["entry_price"]),
                         exit_reason="partial")
    else:
        m.reopen_position(conn, position_id)   # remainder stays open (undo the claim)
    return {"realized_delta": realized_delta, "remaining": left, "closed": closed, "price": price}


def manual_buy(conn, source_user_id, mint, ticker, size_usd, now_iso, fill_fn=honest_fill,
               network_id=None):
    """Open a paper position by hand at a live fill (kind manual_buy). No-op if one is open.

    honest_fill resolves dexscreener -> fomo fallback internally, so no separate quote
    pre-fetch is needed here (keeps this pure/testable)."""
    if m.open_positions_for_coin(conn, source_user_id, mint):
        return None
    price, source = _call_with_network(
        fill_fn, mint, network_id, size_usd, "buy", None,
    )
    if not price or price <= 0:
        return None
    qty = size_usd / price
    pid = m.open_position(
        conn, source_user_id=source_user_id, source_handle="(manual)", mint=mint,
        ticker=ticker, entry_price=price, entry_notional=size_usd,
        entry_fill_source=source, entry_lag_s=-1.0, their_size_usd=0.0, opened_at=now_iso,
        network_id=network_id, experiment_status="manual")
    m.insert_fill(conn, position_id=pid, kind="manual_buy", price=price, qty=qty,
                  notional=size_usd, fill_source=source, fraction=1.0, created_at=now_iso)
    return pid
