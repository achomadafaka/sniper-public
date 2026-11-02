"""Resolve one mint's pool and draw its tape. The single implementation, shared by every caller.

EXTRACTED from scripts/memecoin_pool_prewarm.py 2026-08-26 so the signal-time resolver could reuse
it rather than fork it. The behaviour is unchanged and the OUTPUT ROWS ARE IDENTICAL — same
`resolve_position_pool` call, same synthetic no-fill position, same tape draw with the same floor —
which is the point: every existing consumer of `sol_pool_vaults` and `pool_meta` lights up with no
change to any of them.

WHY THIS EXISTS AT ALL. Mint -> pool resolution has been a CONSEQUENCE of holding a coin rather
than a precondition of entering one. Measured 2026-08-26: the pool was resolved before the position
opened on 0 of 99 funded entries, `observe_pre_entry_flow` reached 5 of 2,019 attempts (0.25%), the
volume-death shadow is blind on 31% of holds, and the sell-share gate's volume-share evidence lane
almost never fires. Three consumers, one missing input.

WHAT A SIGNAL-TIME RESOLVER CAN AND CANNOT WIN, stated honestly rather than discovered later:

  - A funded entry completes in about 1.8s. A resolution costs roughly 1-3s of RPC. So on the FIRST
    signal for a brand-new mint the entry usually still wins the race, and every consumer keeps
    failing open exactly as it does today. That case is not fixed here.
  - Every SUBSEQUENT signal on the same mint reads a resolved pool.
  - The revival watch's 30-second window is long enough for resolution to complete INSIDE it, so a
    clearance can carry real buy/sell direction instead of unknown.
  - An open position gets tape coverage from seconds after it opens rather than not at all, which
    is the shadow's missing 31%.

So: repeat-signal coverage high, first-signal coverage partial. Anyone quoting a single number for
"coverage" is quoting the wrong thing.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def resolve_one_mint(tc, mconn, mint, *, first_seen, get_tx, get_sigs, call, max_fetch,
                     tape_budget=0, now_iso=None, floor_fn=None,
                     resolve_fn=None, tape_fn=None):
    """Resolve one mint's vault and optionally draw its tape. Returns a per-mint stats dict.

    RESOLVING A POOL IS ONLY HALF THE JOB. `solana_tape_pass` taps vaults reached from OPEN
    POSITIONS, so a pool resolved here would carry pool_meta and still have zero ticks — the flow
    read would find the pool, see an empty window, return None and fail open, which is
    indistinguishable from having no lane at all. The tape draw is what actually puts flow in front
    of a consumer.
    """
    from sniper.util.clock import real_utc_now_iso
    # INJECTABLE, defaulting to the real seams. Callers pass their own module-level names so a
    # test that patches them in the CALLER's namespace still takes effect — extracting this body
    # out of the prewarm script must not silently move the patch surface out from under its tests.
    if resolve_fn is None or tape_fn is None:
        from sniper.services.memecoin_solana_tape import (
            resolve_position_pool as _rp, tape_pass_for_vault as _tp)
        resolve_fn = resolve_fn or _rp
        tape_fn = tape_fn or _tp

    out = {"resolved": 0, "unresolved": 0, "errors": 0, "rpc_calls": 0,
           "taped": 0, "tape_calls": 0, "vault": None}
    try:
        # A synthetic position with id=None is the documented no-fill path: the fill lookup misses,
        # the modeled-route seed returns empty without a route_binding, and
        # _seed_tx_via_mint_activity runs off the bare mint. pool_meta and sol_pool_vaults are
        # mint-keyed so they are written; the sol_position_pools bind is skipped because
        # `position_id is not None` guards it.
        vault, spent = resolve_fn(
            tc, mconn, {"mint": mint, "id": None}, get_tx,
            get_sigs_fn=get_sigs, call_fn=call,
            return_fetch_count=True, max_fetch=max_fetch)
        out["rpc_calls"] += int(spent or 0)
        if not vault:
            out["unresolved"] = 1            # unresolved stays unresolved; never guessed
            return out
        out["resolved"] = 1
        out["vault"] = vault
        if tape_budget > 0:
            # floor_ts IS LOAD-BEARING AND `None` IS THE WRONG VALUE. A fresh vault gets a cursor
            # with last_sig='', so with no floor the pass tries to drain the vault's ENTIRE history
            # before recording anything: measured on a live pool it returned n=0 and wrote 0 ticks
            # across four consecutive passes, advancing the frontier each time and never
            # converging. The floor terminates that walk.
            floor_ts = floor_fn(first_seen) if floor_fn else None
            n = tape_fn(
                tc, mint, vault, get_sigs, get_tx, now_iso or real_utc_now_iso(),
                tx_budget=tape_budget, floor_ts=floor_ts)
            if n and n > 0:
                out["tape_calls"] += int(n)
                out["taped"] = 1
    except Exception:
        out["errors"] = 1
        logger.exception("pool resolve failed for mint=%s (skipped)", mint)
    return out
