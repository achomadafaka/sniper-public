"""Phase 2 Task 8 — per-open-position MarketState materialize pass (read-only, log-only).

For each open real position it reads that pool's recorded tape from the ticks DB, windows it on
BLOCK time via `compute_state` (the same code path replay uses), and writes ONE versioned snapshot
to `memecoin_exit_market_state`. It only READS the funded ledger (the open-position set) and never
writes it. Never raises: one bad position never denies the others a snapshot.

The block-time enrichment (fetching missing headers via RPC) is the standalone worker's job and a
STOP condition — here we read whatever `block_times` already holds and let `compute_state` fail
closed (DATA_INSUFFICIENT) on any gap. No live network here.
"""
import logging

from sniper.persistence import db_exit_state as xs
from sniper.persistence import db_ticks as t
from sniper.services.memecoin_exit_brain import (
    CUT, DEFER, DERISK, TAKE_PROFIT, exit_engine)
from sniper.services.memecoin_exit_market_state import _epoch, _iso, compute_state
from sniper.services.memecoin_fastlane import in_scope, open_real_positions
from sniper.services.memecoin_solana_tape import vault_tape_incomplete
from sniper.settings import memecoin as cfg

logger = logging.getLogger("memecoin")

# ponytail: bound the per-pool tape read; compute_state's block-time window trims to freshness.
# Raise only if a pool's fresh window ever exceeds this many recent ticks.
_MAX_TICKS = 5000


def _pool_kind(tc, chain_id, pool):
    """AMM profile from the recorded tick kinds: v2 if it emits Sync / V2 swaps, v3 if only V3
    swaps, sol for the vault-keyed Solana tape. None if the pool has no swaps yet ->
    compute_state emits SOURCE_UNTRUSTED (unknown kind)."""
    kinds = {r[0] for r in tc.execute(
        "SELECT DISTINCT kind FROM pool_ticks WHERE chain_id=? AND pool=?", (str(chain_id), pool))}
    if kinds & {"sol_swap", "sol_sync", "sol_curve_swap", "sol_curve_sync"}:
        return "sol"
    if "v2_swap" in kinds or "v2_sync" in kinds:
        return "v2"
    if "v3_swap" in kinds:
        return "v3"
    return None


def _quote_side(tc, chain_id, pool):
    r = tc.execute("SELECT quote_side FROM pool_meta WHERE chain_id=? AND pool=?",
                   (str(chain_id), pool)).fetchone()
    return r[0] if r else None


def _read_pool_ticks(tc, chain_id, pool):
    rows = tc.execute(
        "SELECT kind, block_no, tx_sig, log_index, maker, amount0, amount1, "
        "tick, tick_lower, tick_upper, liquidity, sqrt_price_x96, block_hash FROM pool_ticks "
        "WHERE chain_id=? AND pool=? AND block_no IS NOT NULL "
        "ORDER BY block_no DESC, log_index DESC LIMIT ?",
        (str(chain_id), pool, _MAX_TICKS)).fetchall()
    return [dict(r) for r in rows]


def route_protocol(chain_id, route_target):
    """Which protocol the position's own fill routed through, or None.

    `quote_route_target` stores the ROUTER for an EVM fill (entry_attribution.py:110). It never
    names the pool — that is why it must not be used as a pool key — but it does name the
    PROTOCOL, which is exactly what an ambiguous mint needs.
    """
    rt = str(route_target or "").lower()
    if not rt:
        return None
    ch = str(chain_id)
    try:
        from sniper.execution.evm_v4_quote import V4
        from sniper.settings import memecoin as _c
        table = {
            str((V4.get(ch) or {}).get("universal_router") or "").lower(): "v4",
            str((_c.MEMECOIN_EVM_V3_ROUTER or {}).get(ch) or "").lower(): "v3",
            str((_c.MEMECOIN_EVM_V2_ROUTER or {}).get(ch) or "").lower(): "v2",
        }
    except Exception:
        logger.exception("route protocol: router config unavailable -> no tie-break")
        return None
    table.pop("", None)
    return table.get(rt)


def pools_on_route_protocol(tc, chain_id, pools, route_target):
    """Narrow ambiguous pools to the protocol this position actually traded on.

    A mint with two usable pools makes the `len(rows) == 1` rule refuse, and that is the LARGER
    half of the funded gap: over the 4663 funded book, 29% of mints are ambiguous against 11%
    with no pool at all. Completing the pool index makes it worse rather than better — turning
    every V4 row usable moved funded resolution DOWN, because a mint that resolves to one V3 pool
    today gains a second and starts failing closed.

    The position already records which router filled it, so the pool on that protocol is a RECORD
    of what we traded, not a guess. Most-ticks or deepest-liquidity would silently pick a pool we
    never touched, which is worse than refusing.

    Shape separates V4: a V4 pool id is 32 bytes (66 chars); a V2/V3 pool is a 20-byte address
    (42 chars). Shape CANNOT separate V2 from V3, so those come from the swap kind the pool
    emits. Our tape records a V4 swap under `kind='v3_swap'`, so kind alone would not separate V3
    from V4 either — the two tests are complementary and neither works alone.

    Returns the pools unchanged when the route is absent or unrecognised, so the caller's existing
    fail-closed rule still applies. Two pools on the SAME protocol also stay ambiguous: that is an
    honest ambiguity, and inventing a second tie-break to remove it would be the guess this
    avoids.
    """
    proto = route_protocol(chain_id, route_target)
    if proto is None:
        return list(pools)
    if proto == "v4":
        return [p for p in pools if len(str(p)) == 66]
    cands = [p for p in pools if len(str(p)) == 42]
    if len(cands) <= 1:
        return cands
    kind = "v2_swap" if proto == "v2" else "v3_swap"
    return [p for p in cands if tc.execute(
        "SELECT 1 FROM pool_ticks WHERE chain_id=? AND pool=? AND kind=? LIMIT 1",
        (str(chain_id), str(p).lower(), kind)).fetchone()]


def resolve_pool_for_position(tc, chain_id, mint, position_id=None, route_target=None):
    """The ONE position->pool resolver. Never `quote_route_target` — that is the router/settler and
    has no ticks by construction (P1, Codex 2026-07-24: EVM still used it in two call sites after
    the 4663 fix landed in only one).

    Solana: pool id is the coin-VAULT, keyed by token0 = mint, base58 case-sensitive.
    EVM: the mint must match the BASE side (derived from quote_side) on a pool with quote_side and
    both decimals resolved. Ambiguity (>1 usable pool) is narrowed by `route_target` — the router
    that filled THIS position — and still fails closed when that cannot decide it.
    """
    mint = str(mint or "")
    if not mint:
        return None
    if str(chain_id) == "solana":
        if position_id is not None:
            try:
                r = tc.execute(
                    "SELECT pool FROM sol_position_pools WHERE position_id=? AND mint=?",
                    (position_id, mint)).fetchone()
                if r:
                    return str(r[0])
            except Exception:
                pass
        rows = tc.execute("SELECT pool FROM pool_meta WHERE chain_id='solana' AND token0=?",
                          (mint,)).fetchall()
        return str(rows[0][0]) if len(rows) == 1 else None
    rows = tc.execute(
        "SELECT pool FROM pool_meta WHERE chain_id=? AND quote_side IS NOT NULL "
        "AND token0_decimals IS NOT NULL AND token1_decimals IS NOT NULL "
        "AND ((quote_side=0 AND LOWER(token1)=?) OR (quote_side=1 AND LOWER(token0)=?))",
        (str(chain_id), mint.lower(), mint.lower())).fetchall()
    pools = [str(r[0]).lower() for r in rows]
    if len(pools) != 1 and position_id is not None:
        # FILL-ANCHORED FALLBACK. Mint-keyed resolution answers for 1 of 10 recent funded 4663
        # entries: a mint maps to many pools (median 13 among ambiguous ones, max 191) and the
        # exactly-one rule refuses. The entry receipt names the pool the swap actually touched, and
        # the resolver worker persisted it. This is a PURE READ — the network call happened once,
        # off this path, because a transient RPC failure here would return None, read as "no usable
        # pool", and become an exit refusal indistinguishable from a real ambiguity.
        #
        # MONOTONE BY CONSTRUCTION: it is consulted only when the mint-keyed answer is not exactly
        # one pool, so it can only turn "no pool" into "the pool we traded" and can never override
        # a case that resolves today.
        from sniper.services.memecoin_position_pool import get_position_pool
        stored = get_position_pool(tc, position_id, chain_id)
        if stored:
            return stored
    if len(pools) > 1 and route_target:
        # Ambiguous by mint alone. Narrow to the protocol this position's own fill used before
        # refusing — see pools_on_route_protocol.
        pools = pools_on_route_protocol(tc, chain_id, pools, route_target)
    return pools[0] if len(pools) == 1 else None


def _contract(pos, pool, pool_kind, quote_side, tape_incomplete=False, *, now_ts=None):
    held_s = None
    opened_ts = _epoch(pos.get("opened_at"))
    if now_ts is not None and opened_ts is not None:
        held_s = max(0.0, float(now_ts) - opened_ts)
    trail_pct = coin_band = None
    decision_engine_version = "v5"   # v5-only: the sole installed engine (v2/v3 retired)
    from sniper.services.memecoin_funded_policy import (
        ENGINE_ENVELOPE_POLICY_ID, band_anchor_trail_pct, frozen_policy_from_position)
    # Invalid immutable metadata must fail this position. Relabeling it as legacy v3 would produce
    # plausible but false one-row evidence; materialize_open_positions owns the per-position skip.
    frozen = frozen_policy_from_position(pos)
    if frozen is not None:
        coin_band = frozen.coin_band
        if frozen.policy_id == ENGINE_ENVELOPE_POLICY_ID:
            trail_pct = band_anchor_trail_pct(coin_band)
            decision_engine_version = getattr(frozen, "decision_engine_version", "v5")
        else:
            trail_pct = frozen.trail_pct
    from sniper.analysis.exit_controller import controller_state_from_row
    return {
        "chain_id": str(pos.get("network_id") or ""), "mint": str(pos.get("mint") or ""),
        "derisked_at": pos.get("derisked_at"),   # one-shot DERISK guard (prereg §2.3)
        "held_s": held_s, "exact_quote_misses": pos.get("exact_quote_misses") or 0,
        "trail_pct": trail_pct, "coin_band": coin_band,
        "decision_engine_version": decision_engine_version,
        "tape_incomplete": tape_incomplete,       # engine-visible completeness (Codex 2026-07-24)
        "pool": pool, "pool_kind": pool_kind, "quote_side": quote_side,
        "position_id": pos.get("id"), "entry_price": pos.get("entry_price"),
        "executable_peak": pos.get("executable_peak_price"),
        "entry_contract_version": pos.get("funded_contract_version"),
        # WO-5B: carry the WO-5A controller state so propose_v5's route/no-progress feed reads real
        # hysteresis. All NULL/0 until the reducer is wired live -> conservative (route_health False).
        "controller_state": controller_state_from_row(pos),
    }


def materialize_open_positions(mconn, tc, now_ts, *, limit=None, sol_tc=None, xc=None):
    """One pass: write a MarketState snapshot for every open real position. Returns snapshots written.

    `mconn` = funded ledger (READ ONLY — the open-position set). `tc` = ticks DB (read-only EVM
    tape). `sol_tc` = the Solana tape's OWN connection (own-DB split, 2026-07-24): the Solana writer
    no longer writes pool_ticks/block_times/pool_meta into `tc`, so Solana positions are read from
    `sol_tc` when given. `sol_tc=None` falls back to `tc` (pre-split callers / tests that seed Solana
    rows directly into `tc` keep working unchanged). `xc` = the exit-state DB (own-DB split, P0-3
    2026-07-24): the snapshot write and its EVM `block_times` read both prefer it, falling back to
    `tc` when `xc` is None (single-db callers/tests keep working unchanged) — `xc=tc` is exactly the
    pre-split shape. `memecoin_exit_market_state` is single-writer (this worker only).

    Out-of-scope chains or a position with no tracked pool still get an honest SOURCE_UNTRUSTED
    snapshot; a missing block header yields DATA_INSUFFICIENT (fail closed)."""
    xc = xc if xc is not None else tc
    xs.ensure_exit_state_tables(xc)
    now_iso = _iso(now_ts)
    written = 0
    for pos in open_real_positions(mconn, limit=limit):
        try:
            chain_id = str(pos.get("network_id") or "")
            active_tc = sol_tc if (chain_id == "solana" and sol_tc is not None) else tc
            if chain_id == "solana":
                # Solana pool id = the coin-VAULT address, resolved by the tape worker from our
                # own entry tx into pool_meta (token0 = mint). quote_route_target is the Jupiter
                # route target there, not the pool — the exact join mistake 4663 already taught.
                pool = resolve_pool_for_position(
                    active_tc, chain_id, pos.get("mint"), position_id=pos.get("id"))
            else:
                pool = resolve_pool_for_position(
                    active_tc, chain_id, pos.get("mint"),
                    route_target=pos.get("quote_route_target"))
            if pool and (chain_id == "solana" or in_scope(chain_id)):
                swaps = _read_pool_ticks(active_tc, chain_id, pool)
                blocks = {s["block_no"] for s in swaps if s.get("block_no") is not None}
                # Solana's own block_times live wherever its writer put them (active_tc, own-DB
                # split) — no exit-state merge concept there. EVM headers read BOTH stores: the
                # funded runner's free ingest-time capture in `tc`, plus the worker's own
                # RPC-backfilled copy in `xc`.
                block_times = (t.block_ts_map(active_tc, chain_id, blocks) if chain_id == "solana"
                              else xs.merged_block_ts_map(active_tc, xc, chain_id, blocks))
                # Solana enumeration can leave a known hole below the head; the engine
                # must see that as data, not only in a log. The cursor (and its completeness
                # flag) live wherever the Solana writer wrote them — `active_tc`, own-DB split.
                incomplete = (vault_tape_incomplete(active_tc, pool) if chain_id == "solana"
                              else False)
                contract = _contract(pos, pool, _pool_kind(active_tc, chain_id, pool),
                                     _quote_side(active_tc, chain_id, pool), incomplete,
                                     now_ts=now_ts)
            else:
                swaps, block_times = [], {}     # unknown chain / unresolved pool -> untrusted
                contract = _contract(pos, pool, None, None, now_ts=now_ts)
            state = compute_state(swaps, block_times, contract, cfg=None, now_ts=now_ts)
            written += xs.write_market_state(xc, state, computed_at=now_iso)
            state_row = xc.execute(
                "SELECT id FROM memecoin_exit_market_state WHERE position_id=? AND computed_at=?",
                (pos.get("id"), now_iso)).fetchone()
            context_swaps = []
            for swap in swaps:
                block_ts = _epoch(block_times.get(swap.get("block_no")))
                if block_ts is not None and block_ts <= float(now_ts):
                    context_swaps.append({**swap, "block_ts": block_ts})
            from sniper.analysis.market_breadth import tape_market_breadth
            regime = tape_market_breadth(active_tc, chain_id, as_of=now_iso)
            # Creation-history identity is not present in pool_ticks.  Keep the field explicit and
            # unavailable; a swap maker must never be promoted to "deployer" by inference.
            wallet_activity = {
                "available": False, "active": None, "wallets": [],
                "data_gap": "creation_history_identity_unavailable",
            }
            try:
                from sniper.services.memecoin_holder_evidence import future_context_at
                future_context = future_context_at(
                    mconn, pos, decision_at=now_iso,
                    max_age_s=cfg.MEMECOIN_HOLDER_EVIDENCE_MAX_AGE_S)
            except Exception:
                # Context collection has zero authority and cannot deny the core state/decision row.
                future_context = {
                    "observation_only": True, "status": "blocked_missing_feed",
                    "reason": "holder_history_unavailable",
                }
            record_shadow_decision(
                xc, pos, state, now_iso=now_iso, mid=state.nominal_price,
                contract=contract, state_id=state_row[0] if state_row else None,
                swaps=context_swaps, regime=regime, deployer_activity=wallet_activity,
                future_context=future_context, as_of_ts=now_ts)
        except Exception:
            logger.exception("exit materializer: position %s failed (skipped)", pos.get("id"))
            continue
    return written


def record_shadow_decision(tc, pos, state, *, now_iso, mid, contract=None,
                           override=None, current_mcap=None, state_id=None, swaps=None,
                           regime=None, deployer_activity=None, future_context=None, as_of_ts=None):
    """Log candidate-vs-legacy for one position at one instant. **ZERO AUTHORITY.**

    `swaps` (WO-3, optional): the coin's recorded tape. When given, its longer-horizon episodic
    memory (`episode_context`) is logged alongside the decision as CONTEXT — the proposer never
    receives it, so no rule can consume it without a new prereg. Omitted -> the field is simply null.

    `regime` (WO-4, optional): the market-breadth context dict (market_breadth.tape_market_breadth).
    Logged on the decision row as CONTEXT only, on the same zero-authority footing as `swaps` —
    the proposer never receives it either.

    Zero authority is STRUCTURAL, not a promise: this function takes no funded-ledger connection at
    all. `tc` is the ticks DB and is the only handle it holds, so it cannot write a money-path table
    even by mistake — adding one would require visibly changing this signature. `pos` is a plain dict
    already read by the caller.

    Reads the legacy action from the PRODUCTION rail function (`alpha_b_real_exit_reason`, the same
    one the runner uses, so the two can never drift) and the selected candidate proposer, then writes
    ONE row to `memecoin_exit_candidate_decisions`. It never nominates/sizes/signs/approves/broadcasts,
    and never raises — one bad position must not deny the others a decision row. Returns rows written.

    `divergent` = the two engines said different things about acting at this instant:
    `(candidate would act) XOR (legacy would act)`. `executable_confirmed` and `censored` are always
    0 here: no executable lane exists yet, and censoring is decided at read time, not guessed now.
    """
    try:
        from sniper.services.memecoin_real_runner import alpha_b_real_exit_reason
        legacy = alpha_b_real_exit_reason(pos, mid, now_iso, override=override,
                                          current_mcap=current_mcap)
        selection = (contract or {}).get("decision_engine_version") or "v5"
        engine_version, proposer = exit_engine(selection)
        decision = proposer(contract or {}, state, cfg)
        candidate_acts = decision.action in (CUT, DERISK, TAKE_PROFIT)
        divergent = candidate_acts != (legacy is not None)
        episode = None
        if swaps is not None:
            from sniper.analysis.episode_context import episode_context
            episode = episode_context(swaps, quote_side=(contract or {}).get("quote_side"),
                                      as_of_ts=as_of_ts)
        return xs.write_candidate_decision(tc, {
            "position_id": pos.get("id"),
            "decided_at": now_iso,
            "engine_version": engine_version,
            "feature_version": state.feature_version,
            "state_id": state_id,
            "candidate_action": decision.action,
            "proposed_fraction": decision.proposed_fraction,
            "reason_codes": list(decision.reason_codes),
            "legacy_action": legacy,
            "divergent": divergent,
            "executable_confirmed": False,
            "censored": False,
            "input_block_ts": state.source_event_time,
            "no_action_reason": decision.no_action_reason,
            "episode_context": episode,      # WO-3 shadow context; never read by a rule
            "regime": regime,                # WO-4 shadow context; never read by a rule
            "deployer_activity": deployer_activity,   # WO-6 shadow context; never read by a rule
            "future_context": future_context,         # WO-16 observation-only point-in-time facts
            # v5_shadow retired (v5-only 2026-07-25): column preserved, written null.
        })
    except Exception:
        logger.exception("exit shadow: position %s failed (no decision recorded)", pos.get("id"))
        return 0
