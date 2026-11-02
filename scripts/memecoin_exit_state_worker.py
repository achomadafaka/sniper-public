#!/usr/bin/env python3
"""Standalone exit-state materializer process (Phase 2 Task 9) — DEFAULT OFF, log-only.

Deliberately NOT a thread inside `memecoin_runner_loop.py` (settled 2026-07-23): feature/RPC/SQLite
faults must not share the funded runner's failure domain. This process has its own ticks + ledger
connections, imports no funded-rail execution code, and only READS the ledger's open-position set. A
dedicated watchdog supervises it independently of the runner.

Enabling `MEMECOIN_EXIT_STATE_ENABLED` is an operator STOP condition — it turns on block-time RPC
spend. With the flag OFF (default) `main()` returns immediately, opening no connection and making no
network call. The block-time backfill's only live-RPC seam is `get_block_fn`, built from the EXISTING
ingest RPC and invoked ONLY when enabled; if no RPC is configured it stays None and the materializer
runs fail-closed (missing headers -> DATA_INSUFFICIENT).
"""
import argparse
import json
import logging
import os
import sys
import time

# Run as `python3 scripts/memecoin_exit_state_worker.py` (the watchdog's exact command): the script
# dir, not the repo root, is on sys.path, so `import sniper` needs the root added first.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sniper.persistence import db_ticks as t                                          # noqa: E402
from sniper.services.memecoin_exit_materializer import _MAX_TICKS, materialize_open_positions  # noqa: E402
from sniper.services.memecoin_exit_market_state import _iso                            # noqa: E402
from sniper.services.memecoin_fastlane import in_scope, open_real_positions            # noqa: E402
from sniper.util.clock import real_utc_now                                              # noqa: E402

logger = logging.getLogger("memecoin")


def _enabled():
    """Single source of truth for the gate.

    If the var is set anywhere (shell, or .env via load_dotenv) the LIVE env wins — a settings
    attribute baked at import time would predate load_dotenv and read stale. Only when it is set
    nowhere do we fall back to the settings default, so the worker can never disagree with
    `sniper.settings.memecoin` about what "unset" means (it did, silently, until 2026-07-23).
    """
    v = os.getenv("MEMECOIN_EXIT_STATE_ENABLED")
    if v is None:
        from sniper.settings import memecoin as cfg
        return bool(getattr(cfg, "MEMECOIN_EXIT_STATE_ENABLED", False))
    return v.strip().lower() in {"1", "true", "yes"}


def _connect_ticks():
    """Read-only handle (own-DB split, P0-3 2026-07-24): this worker only ever READS pool_ticks /
    pool_meta / the funded runner's free block_times capture from the shared ticks DB. Structurally
    zero write handles on it — a second writer there already caused a WAL-lock incident once."""
    return t.connect_ticks_readonly()


def _connect_exit_state():
    """This worker's OWN file: market-state snapshots, candidate decisions, and its RPC-backfilled
    block_times headers all write here (own-DB split, P0-3 2026-07-24) — never the shared ticks DB."""
    from sniper.persistence import db_exit_state as xs
    return xs.connect_exit_state()


def _connect_sol_tape_readonly():
    """Read the independently-owned Solana tape without becoming its second writer."""
    return t.connect_sol_tape_readonly()


def _connect_ledger():
    # read-only: this worker only READS the funded ledger (the open-position set); the connection
    # itself enforces that it can never write a money-path table.
    from sniper.persistence.db_core import get_readonly_connection
    return get_readonly_connection()


def _rpc_get_block_fn():
    """Header fetch `(chain_id, block_no) -> ts_int|None` over the EXISTING ingest RPC. LIVE RPC —
    only reached once the operator has enabled the worker. Returns None when no RPC is configured, so
    the materializer stays fail-closed rather than inventing a timestamp."""
    try:
        from sniper.execution.evm_wallet import _rpc
        from sniper.settings import memecoin as cfg
    except Exception:
        return None

    def get_block(chain_id, block_no):
        rpc = cfg.evm_rpc_for_chain(str(chain_id))
        if not rpc:
            return None
        resp = _rpc(rpc, "eth_getBlockByNumber", [hex(int(block_no)), False])
        blk = resp.get("result") if isinstance(resp, dict) else None
        if not blk or blk.get("timestamp") is None:
            return None
        return int(blk["timestamp"], 16)

    return get_block


def _open_pools(mconn, tc):
    """Deduped `(chain_id, pool)` for in-scope open real positions with a tracked pool."""
    out = set()
    for pos in open_real_positions(mconn):
        chain = str(pos.get("network_id") or "")
        # NOT quote_route_target as a POOL KEY — the router/settler has no ticks (P1, Codex
        # 2026-07-24). It is still the right tie-break for an AMBIGUOUS EVM mint, because it names
        # the PROTOCOL the fill used; resolve_pool_for_position only consults it when the mint has
        # more than one usable pool and would otherwise fail closed. Passing it here matters
        # because this set decides which pools get exit state written at all: without the
        # tie-break an ambiguous mint resolves to None, the pool never enters `out`, and the pull
        # materializer that CAN now price it never sees a tape for it.
        # Solana keeps position_id and never the route target — there it is the Jupiter route, not
        # a pool, which is the exact join mistake 4663 already taught us.
        from sniper.services.memecoin_exit_materializer import resolve_pool_for_position
        if chain == "solana":
            pool = resolve_pool_for_position(tc, chain, pos.get("mint"),
                                             position_id=pos.get("id"))
        else:
            pool = resolve_pool_for_position(tc, chain, pos.get("mint"),
                                             route_target=pos.get("quote_route_target"))
        if pool and in_scope(chain):
            out.add((chain, pool))
    return out


def _missing_blocks(tc, xc, chain_id, pool):
    """Block_nos for this pool with no header in EITHER store yet, NEWEST FIRST and bounded to the
    recent window.

    Own-DB split (P0-3, 2026-07-24): a block already free-captured by the funded runner's own
    getLogs blockTimestamp (in `tc`) needs no RPC spend here — checking both stores before deciding
    a block is "missing" is a spend saving, not just a correctness nicety.

    Newest-first is load-bearing: the fresh state needs the current window's headers, and an ancient
    block whose header the RPC no longer serves must never starve the recent ones (the old oldest-first
    scan retried the same unavailable [1,2] every pass and never reached newer, available blocks). The
    recent-window LIMIT keeps a permanently-unavailable ancient block from consuming the budget forever.
    """
    from sniper.persistence import db_exit_state as xs
    blocks = [r[0] for r in tc.execute(
        "SELECT DISTINCT block_no FROM pool_ticks WHERE chain_id=? AND pool=? AND block_no IS NOT NULL "
        "ORDER BY block_no DESC LIMIT ?", (str(chain_id), pool, _MAX_TICKS))]
    have = xs.merged_block_ts_map(tc, xc, chain_id, blocks)
    return [b for b in blocks if b not in have]     # already newest-first


def enrich_block_times(mconn, tc, xc, get_block_fn, now_iso, *, cap):
    """Backfill the headers the open-position windows need — bounded (<= `cap` total across pools),
    cached forever, fail-closed. Written into `xc` (the exit-state DB's OWN block_times copy, own-DB
    split P0-3 2026-07-24) — never `tc`, the funded runner's shared, read-only-to-this-worker ticks
    DB. `get_block_fn(chain_id, block_no) -> ts_int|None` is the live-RPC seam. Returns headers
    written."""
    written = 0
    budget = cap
    for chain_id, pool in _open_pools(mconn, tc):
        if budget <= 0:
            break
        missing = _missing_blocks(tc, xc, chain_id, pool)[:budget]
        if not missing:
            continue
        written += t.backfill_block_times(
            xc, chain_id, missing, lambda b: get_block_fn(chain_id, b), now_iso)
        budget -= len(missing)
    return written


def run_pass(mconn, tc, xc, now_ts, *, get_block_fn=None, cap=64, sol_tc=None):
    """One enrichment (only if a live RPC seam is supplied) + one materialize pass.

    `xc` = the exit-state DB's OWN connection (own-DB split, P0-3 2026-07-24): block-time backfill,
    market-state snapshots, and candidate decisions all target it — this worker ends every pass with
    zero write handles on `tc`, the funded runner's shared ticks DB (read-only here).

    `sol_tc` is read-only. RPC enumeration/decoding belongs to the dedicated tape worker, so a slow
    provider can never delay this materialization pass.
    """
    if get_block_fn is not None:
        try:
            enrich_block_times(mconn, tc, xc, get_block_fn, _iso(now_ts), cap=cap)
        except Exception:
            logger.exception("exit-state worker: block-time enrichment failed (continuing fail-closed)")
    if sol_tc is not None:
        return materialize_open_positions(mconn, tc, now_ts, sol_tc=sol_tc, xc=xc)
    return materialize_open_positions(mconn, tc, now_ts, xc=xc)


def main(*, iterations=None):
    """Run the materialize loop while enabled. `iterations` bounds the loop for tests; None = forever."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_REPO_ROOT, ".env"))   # so os.getenv sees the operator's flag; no-op if absent
    if not _enabled():
        logger.info("exit-state materializer OFF (MEMECOIN_EXIT_STATE_ENABLED unset) — exiting")
        return 0
    interval = float(os.getenv("MEMECOIN_EXIT_STATE_INTERVAL_S", "30") or 30)
    cap = int(os.getenv("MEMECOIN_EXIT_BLOCKTIME_MAX_PER_TICK", "64") or 64)
    tc = _connect_ticks()
    xc = _connect_exit_state()
    mconn = _connect_ledger()
    get_block_fn = _rpc_get_block_fn()
    sol_tc = None
    try:
        sol_tc = _connect_sol_tape_readonly()
    except Exception:
        logger.info("exit-state worker: Solana tape absent (Solana state stays fail-closed)")
    passes = 0
    while True:
        try:
            # CORRECTED clock, never time.time(): now_ts is compared against CHAIN block_ts
            # (real UTC). This host runs ~+7 h fast (NTP inactive), so the raw clock made every
            # feature look 7 h stale -> fail-closed -> the engine could never act.
            now = real_utc_now()
            if sol_tc is None:
                try:
                    sol_tc = _connect_sol_tape_readonly()
                except Exception:
                    pass
            run_pass(mconn, tc, xc, now, get_block_fn=get_block_fn, cap=cap,
                     sol_tc=sol_tc)
        except Exception:
            logger.exception("exit-state materializer pass failed (continuing)")
        passes += 1
        if iterations is not None and passes >= iterations:
            break
        time.sleep(interval)
    return passes


def _smoke_no_rpc(*, ready_file, hold_s):
    """Open the worker's real connection set, run one network-free pass, then stay alive briefly.

    This is only a deployment-smoke seam.  It deliberately bypasses the operator enable flag, never
    constructs RPC functions, and records the resolved database paths so the parent can verify the
    live process' file-descriptor access modes through ``/proc``.
    """
    tc = _connect_ticks()
    xc = _connect_exit_state()
    mconn = _connect_ledger()
    try:
        sol_tc = _connect_sol_tape_readonly()
    except Exception:
        sol_tc = None
    conns = {"ticks": tc, "exit_state": xc, "ledger": mconn}
    if sol_tc is not None:
        conns["sol_tape"] = sol_tc
    try:
        run_pass(mconn, tc, xc, real_utc_now(), get_block_fn=None, sol_tc=sol_tc)
        paths = {}
        for name, conn in conns.items():
            row = conn.execute("PRAGMA database_list").fetchone()
            paths[name] = os.path.realpath(row[2]) if row and row[2] else ""
        tmp = f"{ready_file}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "paths": paths}, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, ready_file)
        time.sleep(max(0.0, float(hold_s)))
        return 0
    finally:
        for conn in conns.values():
            try:
                conn.close()
            except Exception:
                pass


def _cli():
    ap = argparse.ArgumentParser(description="Standalone memecoin exit-state worker")
    ap.add_argument("--smoke-no-rpc", action="store_true",
                    help="open real worker connections and hold for deployment smoke inspection")
    ap.add_argument("--smoke-ready-file")
    ap.add_argument("--smoke-hold-s", type=float, default=10.0)
    args = ap.parse_args()
    if args.smoke_no_rpc:
        if not args.smoke_ready_file:
            ap.error("--smoke-no-rpc requires --smoke-ready-file")
        return _smoke_no_rpc(ready_file=args.smoke_ready_file, hold_s=args.smoke_hold_s)
    if args.smoke_ready_file:
        ap.error("--smoke-ready-file requires --smoke-no-rpc")
    return main()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(_cli())
