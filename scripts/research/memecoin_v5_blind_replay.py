#!/usr/bin/env python3
"""Read-only V5 replay over the recorded 4663 tape — prereg:
docs/superpowers/plans/2026-07-24-memecoin-v3-blind-replay-prereg.md.

One code path: the PRODUCTION compute_state + selected proposer walk the tape block-by-block with no
lookahead. Fills at the NEXT block's VWAP with 0/10/20% haircuts. Required block timestamps must be
prefetched by the standalone header fetcher; this process cannot write either database.
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
from dotenv import load_dotenv                                   # noqa: E402
load_dotenv(os.path.join(_ROOT, ".env"))

from sniper.analysis.exit_features import exec_price_path_per_block          # noqa: E402
from sniper.analysis.exit_flex_v5 import market_cap_band                     # noqa: E402
from sniper.persistence import db_ticks as t                                  # noqa: E402
from sniper.services.memecoin_exit_brain import (                             # noqa: E402
    CUT, DERISK, RIDE, TAKE_PROFIT, exit_engine)
from sniper.services.memecoin_exit_market_state import compute_state          # noqa: E402
from sniper.services.memecoin_funded_policy import (                          # noqa: E402
    ENGINE_ENVELOPE_POLICY_ID, band_anchor_trail_pct, frozen_policy_from_position)

ARMS = ("control", "real_shadow")
MAX_HOLD_S = 12 * 3600.0
CATASTROPHE = 0.80
HAIRCUTS = (0.0, 0.10, 0.20)
DERISK_FRACTION = 0.5


def _ep(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _pool_for_mint(tc, mint, allow_multi=False, return_fidelity=False, entry_pool=None):
    """allow_multi=False keeps the pre-registered replay cohort (exactly-one-pool mints only).
    allow_multi=True resolves multi-pool mints to the pool with the most tick blocks (dominant
    venue). `entry_pool_exact` is reserved for a pool identity frozen at entry; merely finding one
    mapped pool is not proof it was the executable entry venue."""
    rows = tc.execute(
        "SELECT pool, quote_side FROM pool_meta WHERE chain_id='4663' AND quote_side IS NOT NULL "
        "AND ((quote_side=0 AND LOWER(token1)=?) OR (quote_side=1 AND LOWER(token0)=?))",
        (mint, mint)).fetchall()
    entry_matches = [r for r in rows
                     if entry_pool and str(r[0]).lower() == str(entry_pool).lower()]
    if len(entry_matches) == 1:
        out = (str(entry_matches[0][0]).lower(), int(entry_matches[0][1]), "entry_pool_exact")
    elif len(rows) == 1:
        pool = str(rows[0][0]).lower()
        out = (pool, int(rows[0][1]), "single_mapped_pool")
    elif len(rows) > 1 and allow_multi:
        best = max(rows, key=lambda r: tc.execute(
            "SELECT COUNT(DISTINCT block_no) FROM pool_ticks WHERE chain_id='4663' AND pool=?",
            (str(r[0]).lower(),)).fetchone()[0])
        out = (str(best[0]).lower(), int(best[1]), "dominant_proxy")
    else:
        out = (None, None, None)
    return out if return_fidelity else out[:2]


def _policy_fields(pos):
    """Use the position's immutable funded reference when present. The 30% fallback is only for
    legacy rows whose historical policy was never frozen into the ledger."""
    policy = frozen_policy_from_position(pos)
    if policy is None:
        return 30.0, market_cap_band(pos.get("entry_mcap"))
    trail_pct = (band_anchor_trail_pct(policy.coin_band)
                 if policy.policy_id == ENGINE_ENVELOPE_POLICY_ID else policy.trail_pct)
    return float(trail_pct), policy.coin_band


def replay_position(tc, rpc_call, pos, fetch_headers=False, *, engine_version="v5"):
    """-> dict(result) or dict(skip=reason)."""
    if fetch_headers:
        return {"skip": "header_fetch_disabled_readonly"}
    mint = str(pos["mint"] or "").lower()
    pool, qs = _pool_for_mint(tc, mint)
    if not pool:
        return {"skip": "pool_unresolved"}
    t0 = _ep(pos["opened_at"])
    entry = float(pos["entry_price"] or 0)
    notional = float(pos["entry_notional"] or 0)
    if not t0 or entry <= 0 or notional <= 0:
        return {"skip": "bad_entry_fields"}
    rows = [dict(r) for r in tc.execute(
        "SELECT kind, block_no, maker, amount0, amount1 FROM pool_ticks "
        "WHERE chain_id='4663' AND pool=? AND block_no IS NOT NULL ORDER BY block_no",
        (pool,))]
    if not rows:
        return {"skip": "no_tape"}
    all_blocks = sorted({r["block_no"] for r in rows})
    bt = t.block_ts_map(tc, "4663", all_blocks)
    timed = []
    for r in rows:
        ep = _ep(bt.get(r["block_no"]))
        if ep is not None:
            timed.append({**r, "block_ts": ep})
    window = [r for r in timed if t0 <= r["block_ts"] <= t0 + MAX_HOLD_S]
    if len({r["block_no"] for r in window if r["kind"] in ("v2_swap", "v3_swap")}) < 5:
        return {"skip": "too_few_swap_blocks"}

    trail_pct, coin_band = _policy_fields(pos)
    contract = {"chain_id": "4663", "mint": mint, "pool": pool,
                "pool_kind": "v2" if any(r["kind"] in ("v2_swap", "v2_sync") for r in window) else "v3",
                "quote_side": qs, "position_id": pos["id"], "entry_price": entry,
                "trail_pct": trail_pct, "exact_quote_misses": 0, "coin_band": coin_band}
    decision_engine_version, proposer = exit_engine(engine_version)
    blocks = sorted({r["block_no"] for r in window})
    # Key on BLOCK IDENTITY, not block_ts. 4663 makes ~10 blocks/second and block_times are
    # second-resolution, so a {ts: px} dict kept ONE block per second and silently discarded the
    # other ~9 — the replay was running on a per-second tape it called per-block (P1, Codex
    # 2026-07-24). `path` is already ordered by (block_ts, block_no) by the feature library.
    path = exec_price_path_per_block(
        [r for r in window if r["kind"] in ("v2_swap", "v3_swap")], quote_side=qs)
    if not path:
        return {"skip": "no_priced_blocks"}
    vwap = {blk: px for _ts, px, _n, blk in path}      # block_no -> price
    bt_of = {blk: ts for ts, _px, _n, blk in path}     # block_no -> block_ts
    bts = [blk for _ts, _px, _n, blk in path]          # priced blocks, chronological
    scale = entry / vwap[bts[0]]           # anchor atomic ratio to our entry price

    # PRODUCTION CADENCE (2026-07-24): decide every 15 s on the last <=5000 ticks — exactly the
    # live worker's sweep + read bound. Per-block decisions are 150x slower AND unfaithful: with
    # ~10 blocks/s the live engine never sees per-block granularity between sweeps.
    grid = []
    nxt_t = bt_of[bts[0]]
    for blk in bts:
        if bt_of[blk] >= nxt_t:
            grid.append(blk)
            nxt_t = bt_of[blk] + 30.0      # MEMECOIN_EXIT_STATE_INTERVAL_S — production sweep
    qty = 1.0                              # fraction of the bag still held
    pnl_units = 0.0                        # in entry-price units per 1.0 bag
    actions = []
    partial_fractions = []
    exited = censored = False
    bts_set = set(grid)
    window.sort(key=lambda r: (r["block_ts"], r["block_no"]))
    ptr = 0
    derisked_at = None                     # one-shot DERISK guard, exactly as the ledger supplies it
    for i, blk in enumerate(bts):
        bt_i = bt_of[blk]
        if blk not in bts_set:
            continue
        while ptr < len(window) and (window[ptr]["block_ts"], window[ptr]["block_no"]) <= (bt_i, blk):
            ptr += 1
        so_far = window[max(0, ptr - 5000):ptr]
        st = compute_state(so_far, {r["block_no"]: bt[r["block_no"]] for r in so_far},
                           contract, now_ts=bt_i)
        d = proposer({**contract, "held_s": bt_i - t0, "derisked_at": derisked_at}, st)
        nxt = bts[i + 1] if i + 1 < len(bts) else None
        px_now = vwap[blk] * scale
        # envelope: catastrophe on DEFER too
        if px_now <= entry * (1 - CATASTROPHE) and nxt:
            pnl_units += qty * (vwap[nxt] * scale - entry)
            actions.append("ENV_CATASTROPHE"); qty = 0.0; exited = True; break
        if d.action == CUT and nxt:
            pnl_units += qty * (vwap[nxt] * scale - entry)
            actions.append("CUT:" + ",".join(d.reason_codes[:3])); qty = 0.0; exited = True; break
        # Production routes BOTH partial actions through the same CAS claim. Once either action
        # settles, later partial proposals cannot claim and therefore cannot sell again.
        if (d.action in (DERISK, TAKE_PROFIT) and derisked_at is None
                and nxt and qty > 1e-9):
            decision_fraction = float(d.proposed_fraction or DERISK_FRACTION)
            f = decision_fraction * qty
            pnl_units += f * (vwap[nxt] * scale - entry)
            qty -= f
            partial_fractions.append(decision_fraction)
            # Stamp the shared partial-exit claim exactly as the executor does. Without it replay
            # can halve the bag repeatedly even though the funded adapter refuses every later claim.
            derisked_at = bt_i
            actions.append(f"{d.action}@{bt_i - t0:.0f}s")
        elif d.action == RIDE:
            actions.append("RIDE") if (not actions or actions[-1] != "RIDE") else None
    if qty > 1e-9:                          # timecap or tape end
        last_px = vwap[bts[-1]] * scale
        pnl_units += qty * (last_px - entry)
        censored = (bt_of[bts[-1]] - t0) < MAX_HOLD_S - 600
        actions.append("TIMECAP" if not censored else "CENSORED_TAPE_END")
    out = {"pos_id": pos["id"], "arm": pos["strategy"], "actual": float(pos["realized_pnl"] or 0),
           "actions": actions[:6], "censored": censored, "exited": exited,
           "engine_version": decision_engine_version, "partial_fractions": partial_fractions}
    gross = pnl_units / entry * notional
    proceeds = max(0.0, notional + gross)          # what the sells actually returned
    for h in HAIRCUTS:
        out[f"{engine_version.lower()}_pnl_h{int(h*100)}"] = gross - h * proceeds
    return out


def _walk_worker(job):
    """Read-only worker: opens its own ro ticks connection; NEVER writes (headers were
    pre-fetched serially in main, so the parallel phase cannot contend with the live tape)."""
    tc = t.connect_ticks_readonly()
    try:
        pos, engine_version = job if isinstance(job, tuple) else (job, "v5")
        return replay_position(tc, None, pos, fetch_headers=False, engine_version=engine_version)
    except Exception as e:
        return {"skip": f"worker_error:{type(e).__name__}"}
    finally:
        tc.close()


def main():
    from multiprocessing import Pool

    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.state_paths import state_root
    tc = t.connect_ticks_readonly()
    m = get_readonly_connection(str(state_root() / "sniper_tracker.db"))
    w0 = tc.execute("SELECT MIN(observed_at) FROM pool_ticks WHERE chain_id='4663'").fetchone()[0]
    for arm in ARMS:
        poss = [dict(r) for r in m.execute(
            "SELECT id, mint, strategy, opened_at, entry_price, entry_notional, realized_pnl,entry_mcap "
            "      ,funded_contract_version,funded_contract_hash,funded_contract_json "
            "FROM memecoin_positions WHERE network_id='4663' AND is_open=0 AND strategy=? "
            "AND opened_at >= ? ORDER BY opened_at", (arm, w0))]
        # Header fetching is the standalone fetcher's job (memecoin_replay_header_fetch.py, run
        # first, resumable, hard socket timeouts). The in-walk pre-pass hung for 23 min at 0% CPU
        # retrying permanently-unserved headers on timeout-less sockets — deleted. Rows whose
        # blocks stay headerless drop fail-closed in replay_position.
        for engine_version in ("v5",):
            res, skips = [], {}
            with Pool(24) as workers:
                for r in workers.map(_walk_worker, [(p, engine_version) for p in poss]):
                    if "skip" in r:
                        skips[r["skip"]] = skips.get(r["skip"], 0) + 1
                    else:
                        res.append(r)
            n = len(res)
            print(f"\n=== arm={arm} engine={engine_version} eligible={len(poss)} "
                  f"replayed={n} skips={skips}")
            if not n:
                continue
            actual = sum(r["actual"] for r in res)
            for h in (0, 10, 20):
                pnls = sorted(r[f"{engine_version}_pnl_h{h}"] for r in res)
                tot = sum(pnls)
                print(f"  haircut {h:2d}%: {engine_version} total ${tot:+.2f}  "
                      f"vs actual ${actual:+.2f} drop-top1 ${tot - pnls[-1]:+.2f} "
                      f"drop-top2 ${tot - sum(pnls[-2:]):+.2f}")
            from collections import Counter
            first = Counter(next((a.split(":")[0].split("@")[0] for a in r["actions"]
                                  if not a.startswith("RIDE")), "NONE") for r in res)
            print(f"  first non-ride action: {dict(first)}")
            print(f"  censored: {sum(1 for r in res if r['censored'])}  "
                  f"engine-exited: {sum(1 for r in res if r['exited'])}")
            bleed = [r for r in res if not r["exited"]]
            if bleed:
                print(f"  no-cut envelope/timecap cohort: n={len(bleed)} "
                      f"{engine_version}_h10 "
                      f"${sum(r[f'{engine_version}_pnl_h10'] for r in bleed):+.2f} "
                      f"actual ${sum(r['actual'] for r in bleed):+.2f}")



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
