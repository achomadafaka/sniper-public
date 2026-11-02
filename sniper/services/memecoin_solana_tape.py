"""Solana tape for the exit brain: pull-enumerated, vault-keyed, fail-closed (v2 prereg §S12).

For each open real Solana position: resolve its pool ONCE from our own entry fill tx (cached in
`pool_meta`, chain 'solana', pool = the coin-VAULT address), then enumerate NEW signatures on that
vault via `getSignaturesForAddress(until=<cursor>)` and decode each with the vault-keyed
`vault_rows`. Slot->time comes free from each tx's `blockTime` into `block_times`.

Completeness is BY CONSTRUCTION, not by ledger: the cursor advances only after every signature in
the batch is fetched and decoded. Any fetch failure aborts the batch without advancing, so the
tape up to the cursor is always complete; a stalled cursor surfaces as staleness downstream and
the state is simply not trusted. This replaces the subscription-liveness gate a push model would
need — a pull on the chain's own index cannot silently miss events inside its cursed window.

STRICTLY READ-ONLY on `solana_read_rpc()`; the funded ledger connection is read-only (fills for
pool resolution, open positions). The custody RPC var (which arms the real leg) is never read
here — a test asserts its name does not appear in this module at all.
"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sniper.persistence import db_ticks as t
from sniper.util.clock import real_utc_now_iso
from sniper.services.memecoin_solana_amounts import (
    NATIVE_SOL,
    QUOTE_MINTS,
    pump_curve_rows,
    resolve_pool_accounts,
    resolve_pump_curve_accounts,
    vault_rows,
)

logger = logging.getLogger("memecoin")

_SIG_LIMIT = 1000         # Solana RPC maximum; live-verified against the tape provider
_MAX_PAGES = 5            # safety bound only: <=5000 signatures enumerated per vault per pass
_ENUM_FRONTIER = "enum:"
_DECODE_FRONTIER = "decode:"
_FLOOR_CONTRACTION_MIN_S = 1800.0


DYNAMIC_QUOTE_MIN_LIQ_USD = 25_000.0


def _dynamic_quote_ok(qmint):
    """A non-WSOL/USDC quote binds only when the quote token itself has a real USD market."""
    from sniper.execution.solana_quote import dexscreener_stats
    st = dexscreener_stats(str(qmint)) or {}
    try:
        return float(st.get("price") or 0) > 0 and \
            float(st.get("liquidity") or 0) >= DYNAMIC_QUOTE_MIN_LIQ_USD
    except (TypeError, ValueError):
        return False


def _resolve_accounts(tx, mint, *, exclude_owner=None, required_owner=None):
    from sniper.services.memecoin_solana_amounts import resolve_pool_accounts_dynamic
    resolved = resolve_pool_accounts_dynamic(
        tx, mint, exclude_owner=exclude_owner, required_owner=required_owner,
        quote_ok_fn=_dynamic_quote_ok)
    if resolved:
        return resolved
    # The funded fee payer can satisfy the curve checks.
    # Exclude it from both resolver paths.
    curve = resolve_pump_curve_accounts(tx, mint, exclude_owner=exclude_owner)
    if required_owner is not None and curve and str(curve[0]) != str(required_owner):
        return None
    return curve


def _bootstrap_history_s():
    from sniper.services.exit_engine_hot import required_history_s
    from sniper.settings import memecoin as cfg
    required = required_history_s(getattr(cfg, "MEMECOIN_HOT_STRUCTURE_WINDOW_S", None))
    return max(300.0, float(required)) if required is not None else 300.0


def _frontier(mode, signature):
    if mode not in ("enum", "decode"):
        raise ValueError(f"unknown Solana frontier mode: {mode}")
    return (_ENUM_FRONTIER if mode == "enum" else _DECODE_FRONTIER) + str(signature or "unknown")


def _parse_frontier(value):
    """Return (mode, signature). Unprefixed round-3 rows migrate as enumeration frontiers."""
    raw = str(value or "")
    if raw.startswith(_DECODE_FRONTIER):
        return "decode", raw[len(_DECODE_FRONTIER):]
    if raw.startswith(_ENUM_FRONTIER):
        return "enum", raw[len(_ENUM_FRONTIER):]
    return ("enum", raw) if raw else (None, None)


def _write_cursor(tc, vault, last_sig, now_iso, frontier, floor_ts=None):
    tc.execute("INSERT INTO sol_sig_cursor (vault, last_sig, updated_at, frontier_before, floor_ts) "
               "VALUES (?,?,?,?,?) ON CONFLICT(vault) DO UPDATE SET "
               "last_sig=excluded.last_sig, updated_at=excluded.updated_at, "
               "frontier_before=excluded.frontier_before, "
               "floor_ts=COALESCE(excluded.floor_ts, sol_sig_cursor.floor_ts)",
               (vault, last_sig or "", now_iso, frontier, floor_ts))
    tc.commit()


def ensure_sol_cursor(tc):
    tc.execute("CREATE TABLE IF NOT EXISTS sol_sig_cursor ("
               "vault TEXT PRIMARY KEY, last_sig TEXT NOT NULL, updated_at TEXT NOT NULL)")
    # `frontier_before` is the resumable paging marker AND the completeness flag in one column:
    # non-NULL means "a known un-drained region exists between last_sig and this signature".
    # One column, so the flag can never disagree with the marker.
    cols = {r[1] for r in tc.execute("PRAGMA table_info(sol_sig_cursor)")}
    if "frontier_before" not in cols:
        tc.execute("ALTER TABLE sol_sig_cursor ADD COLUMN frontier_before TEXT")
    if "floor_ts" not in cols:
        tc.execute("ALTER TABLE sol_sig_cursor ADD COLUMN floor_ts REAL")
    tc.execute("CREATE TABLE IF NOT EXISTS sol_pool_vaults ("
               "pool TEXT PRIMARY KEY, mint TEXT NOT NULL, coin_vault TEXT NOT NULL, "
               "quote_vault TEXT NOT NULL, quote_mint TEXT NOT NULL, resolved_at TEXT NOT NULL)")
    # THE ACCOUNT THAT OWNS THE POOL VAULTS (2026-09-02), the SPL owner field, not the program id.
    # Some programs share one authority across every pool (108 pools on one address here); pumpswap
    # derives one per pool, so a single-pool value is the common case and this column cannot
    # identify those programs by itself. NULL means the row predates the column, never "no program".
    #
    # CORRECTED 2026-09-22: this comment used to say `fastlane_sol_state` marks exist only when a
    # pool's program emits an update we decode, so state coverage was "a property of this column".
    # That is false. `SolanaSwapMarkCache` keeps one mark per mint, so a swap mark replaces a state
    # mark before either is persisted; coverage measures the sample, not the program.
    _vault_cols = {r[1] for r in tc.execute("PRAGMA table_info(sol_pool_vaults)")}
    if "program" not in _vault_cols:
        tc.execute("ALTER TABLE sol_pool_vaults ADD COLUMN program TEXT")
    # The pool's VENUE program id (2026-10-21), from the bind transaction itself: the instruction of
    # a known AMM/launchpad program that names both vaults. NULL = unknown (or a row bound before
    # the column); the LP rail reads it to admit only proven constant-product venues.
    if "venue_program" not in _vault_cols:
        tc.execute("ALTER TABLE sol_pool_vaults ADD COLUMN venue_program TEXT")
    tc.execute("CREATE TABLE IF NOT EXISTS sol_position_pools ("
               "position_id INTEGER PRIMARY KEY, mint TEXT NOT NULL, pool TEXT NOT NULL, "
               "venue_kind TEXT NOT NULL, resolution_tx TEXT, resolved_at TEXT NOT NULL)")
    tc.commit()


def vault_tape_incomplete(tc, vault):
    """True when this vault has a known hole below the head. Engine-visible completeness."""
    try:
        ensure_sol_cursor(tc)
        r = tc.execute("SELECT frontier_before FROM sol_sig_cursor WHERE vault=?",
                       (str(vault),)).fetchone()
    except Exception:
        return True                       # cannot verify completeness -> assume incomplete
    return bool(r and r[0])


def _seed_tx_via_mint_activity(mint, get_sigs_fn, get_tx_fn, call_fn, *, max_fetch=4):
    """Seed transaction for a position with NO on-chain fill of our own (paper positions).
    getTokenLargestAccounts(mint) -> candidate pool vaults (the AMM vault holds the bag on these
    coins) -> newest txs touching one. Returns the first tx that DECODES to a pool for this mint
    — an irrelevant transfer as the newest tx must not end the search (Codex review 2026-07-26).
    Every step fail-closed: None means unresolved, never guessed. `max_fetch` bounds getTx spend."""
    if call_fn is None or get_sigs_fn is None:
        return None, 0
    try:
        resp = call_fn("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    except Exception:
        return None, 0
    value = (resp or {}).get("value") if isinstance(resp, dict) else None
    candidate_sigs = []
    for acct in (value or [])[:3]:
        addr = acct.get("address") if isinstance(acct, dict) else None
        if not addr:
            continue
        try:
            sigs = get_sigs_fn(addr, {"limit": 5}) or []
        except Exception:
            continue
        candidate_sigs.append([srow.get("signature") for srow in sigs
                               if isinstance(srow, dict) and srow.get("signature")])
    # Round-robin accounts: one noisy largest account cannot consume the entire resolution budget
    # before the actual pool vault is inspected.
    fetched = 0
    depth = 0
    while fetched < max_fetch and any(depth < len(sigs) for sigs in candidate_sigs):
        for sigs in candidate_sigs:
            if fetched >= max_fetch:
                break
            if depth >= len(sigs):
                continue
            sig = sigs[depth]
            try:
                tx = get_tx_fn(sig)
            except Exception:
                fetched += 1
                continue
            fetched += 1
            if tx and _resolve_accounts(tx, mint):
                return tx, fetched
        depth += 1
    return None, fetched


def _seed_tx_via_modeled_route(pos, mint, get_sigs_fn, get_tx_fn, *, max_fetch):
    """Find a validating transaction from the token-facing AMM hop recorded by the paper fill."""
    route = pos.get("route_binding")
    plan = route.get("route_plan") if isinstance(route, dict) else None
    candidates = []
    for hop in plan if isinstance(plan, list) else ():
        swap = hop.get("swapInfo") if isinstance(hop, dict) else None
        if not isinstance(swap, dict) or str(swap.get("outputMint") or "") != mint:
            continue
        account = swap.get("ammKey")
        if account and str(account) not in candidates:
            candidates.append(str(account))
    if not candidates or get_sigs_fn is None or max_fetch <= 0:
        return None, 0, None
    signatures = []
    for account in candidates:
        try:
            rows = get_sigs_fn(account, {"limit": min(5, max_fetch)}) or []
        except Exception:
            continue
        signatures.extend((account, str(row["signature"])) for row in rows
                          if isinstance(row, dict) and row.get("signature"))
    fetched = 0
    for account, signature in signatures:
        if fetched >= max_fetch:
            break
        try:
            tx = get_tx_fn(signature)
        except Exception:
            tx = None
        fetched += 1
        resolved = _resolve_accounts(tx, mint, required_owner=account) if tx else None
        if resolved:
            return tx, fetched, resolved
    return None, fetched, None


def resolve_position_pool(tc, mconn, pos, get_tx_fn, *, get_sigs_fn=None, call_fn=None,
                          return_fetch_count=False, max_fetch=4):
    """Coin-vault address for this position's pool, resolving + caching on first need.

    Resolution source is OUR OWN entry fill tx (`memecoin_fills.tx_sig`), excluding the fee payer
    (us) — the live-found failure mode where our wallet reads as a pool candidate. Paper positions
    have no fill tx; when `get_sigs_fn`/`call_fn` are supplied, a seed tx is found from the mint's
    largest token accounts instead (same decode validation). Returns the vault or None
    (unresolved stays unresolved; never guessed).
    """
    mint = str(pos.get("mint") or "")
    if not mint:
        return (None, 0) if return_fetch_count else None
    ensure_sol_cursor(tc)   # sol_pool_vaults lives here; resolve can run before any tape pass
    position_id = pos.get("id")
    if position_id is not None:
        bound = tc.execute(
            "SELECT pool FROM sol_position_pools WHERE position_id=? AND mint=?",
            (position_id, mint)).fetchone()
        if bound:
            return (bound[0], 0) if return_fetch_count else bound[0]
    fill = mconn.execute(
        "SELECT tx_sig FROM memecoin_fills WHERE position_id=? "
        "AND kind IN ('entry','manual_buy') AND tx_sig IS NOT NULL "
        "ORDER BY id LIMIT 1", (pos.get("id"),)).fetchone()
    payer = None
    fetch_count = 0
    if fill and fill[0]:
        # An entry transaction is stronger than a mint cache. A coin can graduate from its curve to
        # an AMM while a prior position/cache still exists; reusing that old venue tapes the wrong
        # market for the new position.
        tx = get_tx_fn(fill[0])
        fetch_count = 1
        if not tx:
            # Named because this return was silent, and 37 of 46 funded Solana positions that
            # never bound a pool ALL had an entry tx_sig — so the failing branch decides whether
            # the cause is the RPC read or the decode below. BIND_MAX_ATTEMPTS is spent from a 5 s
            # spin loop, so a position can exhaust its attempts in ~25 s against a 171 s median
            # hold, and an unbound position runs on a ~10 s DexScreener mid instead of the ~2.5 s
            # swap lane. Log only; the return is unchanged.
            logger.warning("solana tape: bind failed branch=entry_tx_unreadable position=%s "
                           "mint=%s tx_sig=%s", pos.get("id"), mint, fill[0])
            return (None, fetch_count) if return_fetch_count else None
        payer_keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")) or []
        if payer_keys:
            k = payer_keys[0]
            payer = k.get("pubkey") if isinstance(k, dict) else k
    else:
        # A mint-level cache is not a position identity. The same mint can graduate from a curve
        # to an AMM, so an unbound position must resolve from a transaction or stay unresolved.
        tx, fetch_count, route_resolved = _seed_tx_via_modeled_route(
            pos, mint, get_sigs_fn, get_tx_fn, max_fetch=max_fetch)
        if not tx and fetch_count < max_fetch:
            fallback, fallback_count = _seed_tx_via_mint_activity(
                mint, get_sigs_fn, get_tx_fn, call_fn, max_fetch=max_fetch - fetch_count)
            tx, fetch_count = fallback, fetch_count + fallback_count
            route_resolved = None
        if not tx:
            # The no-fill path: modeled route AND mint-activity fallback both came back empty.
            # Named for the same reason as the entry-tx branch above. Log only.
            logger.warning("solana tape: bind failed branch=no_seed_tx position=%s mint=%s "
                           "fetches=%s/%s", pos.get("id"), mint, fetch_count, max_fetch)
            return (None, fetch_count) if return_fetch_count else None
    resolved = (route_resolved if not fill else None) or _resolve_accounts(
        tx, mint, exclude_owner=payer)
    if not resolved:
        logger.warning("solana tape: pool unresolved for position %s mint=%s", pos.get("id"), mint)
        return (None, fetch_count) if return_fetch_count else None
    _owner, vault, qmint, quote_vault = resolved
    resolved_at = real_utc_now_iso()
    tc.execute("INSERT OR REPLACE INTO pool_meta (chain_id, pool, token0, token1, quote_side, "
               "resolved_at) VALUES ('solana', ?, ?, ?, 1, ?)",
               (vault, mint, qmint, resolved_at))   # corrected clock: host runs +7 h
    from sniper.services.memecoin_solana_amounts import venue_program
    tc.execute("INSERT OR REPLACE INTO sol_pool_vaults(pool,mint,coin_vault,quote_vault,quote_mint,"
               "resolved_at,program,venue_program) VALUES(?,?,?,?,?,?,?,?)",
               (vault, mint, vault, quote_vault, qmint, resolved_at,
                str(_owner) if _owner else None, venue_program(tx, vault, quote_vault)))
    if position_id is not None:
        tc.execute("INSERT OR REPLACE INTO sol_position_pools "
                   "(position_id,mint,pool,venue_kind,resolution_tx,resolved_at) "
                   "VALUES (?,?,?,?,?,?)",
                   (position_id, mint, vault, "pump_curve" if qmint == NATIVE_SOL else "amm",
                    fill[0] if fill and fill[0] else None, resolved_at))
    tc.commit()
    return (vault, fetch_count) if return_fetch_count else vault


def _new_signatures(vault, cursor_sig, get_sigs_fn, *, start_before=None, floor_ts=None):
    """-> (signatures OLDEST-FIRST, reached_cursor) | (None, False) on RPC failure.

    `getSignaturesForAddress` is newest-first and caps at `_SIG_LIMIT`, so a backlog bigger than one
    page must be walked BACKWARD with `before` until it reaches the cursor. `reached_cursor` says
    whether that walk actually got there; the caller must not treat a partial walk as a whole one.

    `start_before` resumes a previous partial walk from where it stopped instead of re-walking from
    the head, so a backlog drains in bounded steps and the RPC work is not repeated.
    """
    out, before = [], start_before
    reached = False
    for _ in range(_MAX_PAGES):
        params = {"limit": _SIG_LIMIT}
        if cursor_sig:
            params["until"] = cursor_sig
        if before:
            params["before"] = before
        page = get_sigs_fn(vault, params)
        if page is None:
            return None, False
        if not page:
            reached = True                       # nothing left between here and the cursor
            break
        if floor_ts is not None:
            times = [r.get("blockTime") for r in page
                     if isinstance(r, dict) and r.get("blockTime") is not None]
            # A NULL block time cannot be classified against the floor. Keep walking and decode it;
            # silently dropping it would create a hole while the cursor reported complete.
            if len(times) == len(page) and times and min(times) < floor_ts:
                out.extend(r for r in page
                           if r.get("blockTime") is not None and r.get("blockTime") >= floor_ts)
                reached = True
                break
        out.extend(page)
        if len(page) < _SIG_LIMIT:
            reached = True                       # a short page IS the cursor
            break
        nxt = page[-1].get("signature")          # oldest of this page; `before` is exclusive
        if not nxt:
            break                                # cannot page further -> NOT reached
        before = nxt
    else:
        # A full final page does not prove that signatures remain. Probe one row below our oldest
        # enumerated signature so an exactly-full remainder can advance instead of oscillating
        # forever between frontier set/clear. The probe is evidence only: its row is left for the
        # next bounded walk and never enlarges this call's decode batch.
        params = {"limit": 1}
        if cursor_sig:
            params["until"] = cursor_sig
        if before:
            params["before"] = before
        probe = get_sigs_fn(vault, params)
        if probe is None:
            return None, False
        reached = (not probe or (floor_ts is not None and probe[0].get("blockTime") is not None
                                 and probe[0]["blockTime"] < floor_ts))
    out.reverse()                                # pages arrive newest-first -> oldest-first
    return out, reached


def tape_pass_for_vault(tc, mint, vault, get_sigs_fn, get_tx_fn, now_iso, *, tx_budget,
                        floor_ts=None, tx_fetch_workers=1, allow_floor_contraction=False):
    """Keep optional exact-volume proof isolated from the existing tape's behavior."""
    token, transactions = None, []
    try:
        from sniper.services import memecoin_solana_volume_evidence as volume
        ensure_sol_cursor(tc)
        row = tc.execute("SELECT floor_ts FROM sol_sig_cursor WHERE vault=?", (vault,)).fetchone()
        stored_floor = row[0] if row else None
        reset = floor_ts is not None and stored_floor is not None and (
            floor_ts < stored_floor or (allow_floor_contraction and
            floor_ts - stored_floor >= max(_FLOOR_CONTRACTION_MIN_S, _bootstrap_history_s())))
        token = volume.begin_pass(tc, mint, vault, now_iso, reset=reset)
    except Exception:
        logger.debug("solana volume: begin failed; coverage unknown", exc_info=True)

    def fetched(signature):
        tx = get_tx_fn(signature)
        transactions.append((signature, tx))
        return tx

    result = -1
    try:
        result = _tape_pass_for_vault(tc, mint, vault, get_sigs_fn, fetched, now_iso,
                                     tx_budget=tx_budget, floor_ts=floor_ts,
                                     tx_fetch_workers=tx_fetch_workers,
                                     allow_floor_contraction=allow_floor_contraction)
        return result
    finally:
        if token is not None:
            try:
                cursor = tc.execute("SELECT frontier_before FROM sol_sig_cursor WHERE vault=?",
                                    (vault,)).fetchone()
                volume.finish_pass(tc, token, transactions=transactions,
                                   complete=result >= 0 and cursor is not None and not cursor[0])
            except Exception:
                logger.debug("solana volume: finish failed; coverage unknown", exc_info=True)


def _tape_pass_for_vault(tc, mint, vault, get_sigs_fn, get_tx_fn, now_iso, *, tx_budget,
                        floor_ts=None, tx_fetch_workers=1, allow_floor_contraction=False):
    """Enumerate + decode NEW signatures for one vault. Returns tx fetched, or -1 on abort.

    Completeness contract: everything at or below `last_sig` has been DECODED. The cursor therefore
    advances only over signatures we actually fetched, contiguously, starting from the cursor.

    When the enumeration bound is hit before reaching the cursor there is a hole. The old code
    logged it and advanced anyway, which put the un-enumerated remainder permanently BELOW the
    cursor — unreachable, because `until=cursor` only returns newer. Now the hole is *drained*:
    `frontier_before` records how far down the walk got, the next pass resumes from there, and the
    cursor does not move until the walk reaches it. While `frontier_before` is set the vault reads
    as incomplete and `compute_state` fails closed (P1, Codex 2026-07-24).
    """
    if isinstance(tx_fetch_workers, bool) or not isinstance(tx_fetch_workers, int) \
            or tx_fetch_workers < 1:
        raise ValueError("tx_fetch_workers must be a positive integer")
    ensure_sol_cursor(tc)
    cur = tc.execute("SELECT last_sig, frontier_before, floor_ts FROM sol_sig_cursor WHERE vault=?",
                     (vault,)).fetchone()
    last_sig = cur[0] if cur else None
    frontier = cur[1] if cur else None
    stored_floor = cur[2] if cur else None
    if (floor_ts is not None and stored_floor is not None and floor_ts < stored_floor):
        # A newly relevant older position widens the completeness region. The forward cursor cannot
        # fetch below its original floor, so restart enumeration from head; tick dedup makes the
        # re-decode idempotent and the cursor stays incomplete until the wider floor is reached.
        last_sig = None
        frontier = None
        stored_floor = floor_ts
        _write_cursor(tc, vault, None, now_iso, _frontier("decode", "floor-expanded"), floor_ts)
    elif allow_floor_contraction and floor_ts is not None and stored_floor is not None \
            and floor_ts - stored_floor >= max(
                _FLOOR_CONTRACTION_MIN_S, _bootstrap_history_s()):
        # The caller supplied every active consumer for this vault. Old incomplete work below
        # their earliest required history is no longer evidence-bearing.
        last_sig = None
        frontier = None
        stored_floor = floor_ts
        _write_cursor(tc, vault, None, now_iso, None, floor_ts)
    effective_floor = stored_floor if stored_floor is not None else floor_ts
    if not last_sig and floor_ts is not None and stored_floor is None:
        # Migrate an abandoned genesis walk to the explicit evidence floor. No decoded cursor is
        # discarded; only the old paging frontier is reset and re-enumerated from the head.
        frontier = None
        _write_cursor(tc, vault, None, now_iso, None, floor_ts)
    frontier_mode, frontier_sig = _parse_frontier(frontier)

    # An enumeration marker is an RPC paging bound. A decode marker is only a completeness marker:
    # using it as the exclusive `before` bound hides that signature and every newer one.
    start_before = frontier_sig if frontier_mode == "enum" else None
    sigs, reached = _new_signatures(vault, last_sig, get_sigs_fn, start_before=start_before,
                                    floor_ts=effective_floor)
    if sigs is None:
        _write_cursor(tc, vault, last_sig, now_iso,
                      frontier or _frontier("decode", "rpc-error"))
        return -1                                  # enumeration itself failed -> nothing to trust

    if not reached:
        # Still short of the cursor. Record how far down we got and decode NOTHING: ticks are only
        # ever written for a region contiguous with the cursor, so the tape can never contain an
        # island floating above a hole.
        oldest = sigs[0].get("signature") if sigs else frontier_sig
        encoded = _frontier("enum", oldest or "rpc-bound")
        if encoded != frontier:
            _write_cursor(tc, vault, last_sig, now_iso, encoded)
        logger.warning("solana tape: %s incomplete — enumeration bound reached before the cursor; "
                       "draining toward it (frontier=%s). Vault reads INCOMPLETE until closed.",
                       str(vault)[:12], str(oldest)[:24])
        return 0

    known_excluded_head = bool(frontier and frontier_mode == "enum")
    enumerated_sigs = sigs
    sigs = [s for s in enumerated_sigs if not s.get("err")]   # already oldest-first
    if not sigs and enumerated_sigs:
        # getSignaturesForAddress already proves these transactions finalized with errors. They
        # cannot mutate the vault, so no getTransaction decode is needed; nevertheless the durable
        # cursor must cross them or an all-error page becomes an infinite recovery loop. Preserve
        # the excluded head as a decode marker so completeness cannot clear early.
        newest_noop = enumerated_sigs[-1].get("signature")
        if not newest_noop:
            return -1
        next_frontier = (_frontier("decode", frontier_sig) if known_excluded_head else None)
        _write_cursor(tc, vault, newest_noop, now_iso, next_frontier, effective_floor)
        return 0
    if not sigs and known_excluded_head:
        # The bounded drain found nothing BETWEEN the cursor and the frontier — but the frontier
        # signature ITSELF is excluded by the `before` bound and is still un-decoded, as is
        # anything newer. Clearing here read the vault COMPLETE for exactly one pass while known
        # signatures remained, and materialization ran inside that window (P0-B, Codex round 2:
        # pass2 incomplete=False remaining=1). Re-enumerate UNRESTRICTED in this same pass and
        # fall through to decode; the decode path's own truncation logic is the only thing that
        # may clear the frontier (full decode -> None) — completeness holds continuously.
        sigs, reached = _new_signatures(vault, last_sig, get_sigs_fn, start_before=None,
                                        floor_ts=effective_floor)
        if sigs is None:
            return -1
        sigs = [s for s in sigs if not s.get("err")]
        if not reached and sigs:
            # deep backlog even unrestricted: same not-reached handling as above, frontier moves
            oldest = sigs[0].get("signature")
            if oldest:
                _write_cursor(tc, vault, last_sig, now_iso, _frontier("enum", oldest))
            return 0
        known_excluded_head = False       # unrestricted re-walk now includes the old frontier/head
    if not sigs:
        # An unrestricted empty walk proves a decode backlog is drained. An enumeration marker is
        # handled by the unrestricted branch above, so clearing here is always evidence-backed.
        # Refresh updated_at even when the head signature is unchanged: this is the durable proof
        # that a reconnect catch-up checked through the provider head and found no missed event.
        _write_cursor(tc, vault, last_sig, now_iso, None, effective_floor)
        return 0

    if tx_budget <= 0:
        # Enumeration may consume the final pass budget (for example pool resolution at the end of
        # a global allowance). Preserve the existing decoded cursor; writing `last=None` below
        # would rewind it to the empty-string bootstrap marker.
        _write_cursor(tc, vault, last_sig, now_iso, _frontier("decode", sigs[-1]["signature"]))
        return 0

    # P0-4 (Codex 2026-07-24): `sigs` here is the WHOLE enumerated batch (reached=True — the walk
    # itself found no gap). Truncating it to `tx_budget` for decode is a SEPARATE, decode-spend
    # limit, not an enumeration hole — but the old code advanced the cursor over only the decoded
    # prefix and left `frontier_before` untouched (NULL on a vault's first pass), so a genuinely
    # incomplete tape (real, already-enumerated, un-decoded signatures beyond the budget) read as
    # COMPLETE. Recording the newest un-decoded signature as the frontier makes that visible; a
    # later pass that re-enumerates unrestricted (`before=None`, once frontier clears) always
    # rediscovers it exactly like the page-bound-hit path already does — never lost, only delayed.
    truncated = len(sigs) > tx_budget
    # A reached walk that started BELOW an enumeration marker still excluded that marker and the
    # known head above it. Even when this bottom segment fits the budget, decode mode must survive
    # so the next pass re-enumerates unrestricted before completeness can clear.
    more_known = truncated or known_excluded_head
    frontier_hint = sigs[-1]["signature"] if truncated else frontier_sig
    new_frontier = _frontier("decode", frontier_hint) if more_known else None
    sigs = sigs[:tx_budget]                        # oldest first, so the cursor never jumps a gap
    if tx_fetch_workers == 1:
        transactions = []
        for signature_row in sigs:
            transaction = get_tx_fn(signature_row["signature"])
            transactions.append(transaction)
            if not transaction or not transaction.get("meta"):
                break
    else:
        with ThreadPoolExecutor(max_workers=min(tx_fetch_workers, len(sigs))) as executor:
            transactions = list(executor.map(
                get_tx_fn, (s["signature"] for s in sigs)))
    failure_index = next((index for index, tx in enumerate(transactions)
                          if not tx or not tx.get("meta")), None)
    batch_failed = failure_index is not None
    if failure_index == 0:
        failed = sigs[0]
        _write_cursor(tc, vault, last_sig, now_iso,
                      new_frontier or _frontier("decode", failed["signature"]))
        logger.warning("solana tape: tx fetch failed sig=%s (cursor held)",
                       str(failed.get("signature"))[:24])
        return -1
    if failure_index is not None:
        failed = sigs[failure_index]
        new_frontier = new_frontier or _frontier("decode", failed["signature"])
        sigs = sigs[:failure_index]
        transactions = transactions[:failure_index]
        logger.warning("solana tape: tx fetch failed sig=%s; committing %s decoded contiguous "
                       "predecessors and keeping vault incomplete",
                       str(failed.get("signature"))[:24], len(sigs))

    rows, times, last = [], [], None
    pool_meta = tc.execute(
        "SELECT quote_mint, quote_vault FROM sol_pool_vaults WHERE pool=?", (vault,)).fetchone()
    for s, tx in zip(sigs, transactions):
        if pool_meta and pool_meta[0] == NATIVE_SOL:
            rows.extend(pump_curve_rows(
                tx, mint, vault, pool_meta[1], slot=s.get("slot"), signature=s["signature"],
                observed_at=now_iso))
        else:
            # THE BOUND QUOTE MINT, NOT THE STATIC PAIR (2026-09-22, QQQ position 63509).
            # `vault_rows` defaults to QUOTE_MINTS=(WSOL, USDC) and matches the quote leg by MINT,
            # so a pool quoted in anything else finds no quote leg: it emits no `sol_swap` at all
            # and a `sol_sync` whose amount1 is None, which `_record_state` then refuses. Pool
            # 2Qrpq...9mxk (quote Xs8S1uUs...) held ZERO pool_ticks for its entire life while the
            # signature cursor was enumerating its vault.
            #
            # The resolver already accepts exotic quotes — that is the 2026-08-14 PEECOIN/GLDx
            # dynamic pass, and it is what bound this pool — so the two halves of the lane
            # disagreed: binding succeeded and emission produced nothing. `pool_meta[0]` IS the
            # quote mint the binding resolved, so this passes it through rather than re-deriving.
            # THE BOUND QUOTE VAULT ADDRESS, when the binding stored one (2026-09-23). The mint
            # alone cannot price a pool whose quote vault sits under a different PDA; 60 of 60
            # pools still emitting null-quote syncs after the 2026-09-22 mint fix had this address
            # stored and unused. `pool_meta[1]` is that address, already read for the curve branch.
            rows.extend(vault_rows(tx, mint, vault, slot=s.get("slot"), signature=s["signature"],
                                   observed_at=now_iso,
                                   quote_mints=((pool_meta[0],) if pool_meta and pool_meta[0]
                                                else QUOTE_MINTS),
                                   quote_vault=(pool_meta[1] if pool_meta else None)))
        bt = tx.get("blockTime") or s.get("blockTime")
        if bt and s.get("slot") is not None:
            times.append((int(s["slot"]), int(bt)))
        last = s["signature"]
    t.insert_ticks(tc, rows)
    if times:
        got = dict(times)
        t.backfill_block_times(tc, "solana", list(got), lambda b: got.get(b), now_iso)
    # Cursor advances over the decoded, cursor-contiguous run. `frontier_before` now tracks THIS
    # pass's truncation state explicitly (None clears a fully-drained prior hole too).
    if truncated:
        logger.warning("solana tape: %s budget truncation left signatures undecoded beyond %s "
                       "(frontier=%s) — vault reads INCOMPLETE until a later pass drains it.",
                       str(vault)[:12], str(last)[:24], str(new_frontier)[:24])
    _write_cursor(tc, vault, last, now_iso, new_frontier)
    return len(sigs)


def solana_tape_pass(mconn, tc, get_sigs_fn, get_tx_fn, now_iso, *, tx_budget=40,
                     include_paper=None, call_fn=None, extra_positions=(),
                     positions_override=None, tx_fetch_workers=1,
                     deadline_monotonic=None, clock=None,
                     allow_floor_contraction=False):
    """One pass over open Solana positions. Budget is TOTAL getTransaction spend.

    Real positions first (unchanged priority), then — when MEMECOIN_SOL_TAPE_INCLUDE_PAPER —
    open PAPER positions, deduped after route resolution by venue. One mint can have both a live
    curve and a graduated AMM, so mint-level dedup would silently discard evidence. Paper coverage
    is the winner-regime evidence the V6 prereg is blocked on.

    ``extra_positions`` are additional capture targets (``{id, mint, network_id, opened_at}``) from
    a separate book — the V6 live-paper DB, whose positions do not live in ``memecoin_positions``.
    Empty (the default) is byte-identical to before. Vault-level dedup collapses same-mint overlap
    with the real/paper set, so passing a coin already taped costs nothing extra.

    ``positions_override`` runs a dedicated pass over exactly those positions. It is used by the
    V6 worker so a large funded book cannot consume every transaction slot before V6 evidence is
    captured; the normal funded pass still receives its independent configured budget.

    ``deadline_monotonic`` is an optional cooperative wall-clock slice checked between positions
    and vaults. It never interrupts an in-flight RPC or a vault transaction; the next pass resumes
    from durable cursors."""
    clock = clock or time.monotonic
    if deadline_monotonic is not None:
        deadline_monotonic = float(deadline_monotonic)
        if not math.isfinite(deadline_monotonic):
            raise ValueError("deadline_monotonic must be finite or null")

    def deadline_reached():
        return deadline_monotonic is not None and clock() >= deadline_monotonic

    dedicated_positions = positions_override is not None
    funded_position_count = 0
    if dedicated_positions:
        positions = [dict(p) for p in positions_override]
    else:
        from sniper.services.memecoin_fastlane import open_real_positions
        if include_paper is None:
            from sniper.settings.memecoin import MEMECOIN_SOL_TAPE_INCLUDE_PAPER
            include_paper = MEMECOIN_SOL_TAPE_INCLUDE_PAPER
        positions = [p for p in open_real_positions(mconn)
                     if str(p.get("network_id") or "") in ("solana", "")]
        funded_position_count = len(positions)
        if include_paper:
            try:
                positions += [dict(r) for r in mconn.execute(
                    "SELECT id, mint, network_id, opened_at FROM memecoin_positions WHERE is_open=1 "
                    "AND network_id='solana' AND execution_mode='paper' ORDER BY opened_at")]
            except Exception:
                logger.exception("solana tape: paper-position query failed (real-only pass)")
        if extra_positions:
            positions += [dict(p) for p in extra_positions]
    spent = 0
    jobs = {}
    funded_vaults = set()
    history_s = _bootstrap_history_s()
    try:
        pass_epoch = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00")).timestamp()
        rolling_floor_ts = pass_epoch - history_s
    except (TypeError, ValueError):
        rolling_floor_ts = None
    for position_index, pos in enumerate(positions):
        if deadline_reached():
            break
        mint = str(pos.get("mint") or "")
        if not mint:
            continue
        if spent >= tx_budget:
            break
        try:
            opened = datetime.fromisoformat(str(pos.get("opened_at")).replace("Z", "+00:00"))
            floor_ts = opened.timestamp() - history_s
            if rolling_floor_ts is not None:
                floor_ts = max(floor_ts, rolling_floor_ts)
        except (TypeError, ValueError):
            logger.warning("solana tape: position %s has no valid opened_at; skipped fail-closed",
                           pos.get("id"))
            continue
        try:
            remaining_positions = len(positions) - position_index
            resolution_budget = tx_budget - spent
            if dedicated_positions:
                resolution_budget = max(1, resolution_budget // remaining_positions)
            elif jobs:
                # Cached funded vaults must advance even while a large paper cohort still needs
                # pool discovery. Reserve half the pass for actual tape decoding; max_fetch=0
                # still permits later cached paper positions to join the deduplicated job set.
                resolution_budget = max(
                    0, resolution_budget - max(1, tx_budget // 2))
            vault, resolution_spend = resolve_position_pool(
                tc, mconn, pos, get_tx_fn, get_sigs_fn=get_sigs_fn, call_fn=call_fn,
                return_fetch_count=True, max_fetch=resolution_budget)
            spent += resolution_spend
            if not vault:
                continue
            prior = jobs.get(vault)
            if prior is None:
                jobs[vault] = {"mint": mint, "floor_ts": floor_ts}
            elif floor_ts is not None:
                prior["floor_ts"] = (floor_ts if prior["floor_ts"] is None
                                     else min(prior["floor_ts"], floor_ts))
            if not dedicated_positions and position_index < funded_position_count:
                funded_vaults.add(vault)
        except Exception:
            logger.exception("solana tape: position %s failed (skipped)", pos.get("id"))
    # Deduplicate by resolved VENUE, not mint. One mint can have a live curve plus a graduated AMM;
    # collapsing those would tape the wrong venue. Shared venues use the earliest position floor.
    job_items = tuple(jobs.items())
    if dedicated_positions:
        def _has_backlog(item):
            try:
                row = tc.execute(
                    "SELECT frontier_before FROM sol_sig_cursor WHERE vault=?", (item[0],)
                ).fetchone()
            except Exception:
                return True
            return row is None or bool(row[0])

        # Cheap complete-head checks go first. Their unused shares roll forward, so the known
        # backlog receives the entire remainder without starving quiet V6 vaults.
        job_items = tuple(sorted(job_items, key=_has_backlog))
    for job_index, (vault, job) in enumerate(job_items):
        if spent >= tx_budget or (deadline_reached() and vault not in funded_vaults):
            break
        try:
            job_budget = tx_budget - spent
            if dedicated_positions:
                job_budget = max(1, job_budget // (len(job_items) - job_index))
            n = tape_pass_for_vault(tc, job["mint"], vault, get_sigs_fn, get_tx_fn,
                                    now_iso, tx_budget=job_budget,
                                    floor_ts=job["floor_ts"],
                                    tx_fetch_workers=tx_fetch_workers,
                                    allow_floor_contraction=allow_floor_contraction)
            if n > 0:
                spent += n
        except Exception:
            logger.exception("solana tape: vault %s failed (skipped)", str(vault)[:16])
    return spent


def get_signatures_fn(url=None, *, call=None):
    """Build `(address, params) -> [sig rows] | None` on the READ-ONLY endpoint."""
    if call is None:
        from sniper.services.memecoin_solana_blocktime import _rpc_call
        from sniper.settings.memecoin import solana_read_rpc
        url = url or solana_read_rpc()
        if not url:
            return None

        def call(method, params):
            return _rpc_call(url, method, params, timeout=15)

    def get_sigs(address, params):
        try:
            return call("getSignaturesForAddress", [address, dict(params)])
        except Exception:
            logger.exception("solana getSignaturesForAddress failed addr=%s", address)
            return None

    return get_sigs


# --- Pre-entry flow read (2026-08-25) ------------------------------------------------
# The funded volume gate needs flow for the coin IN FRONT OF US, at decision time. The vendor
# volume fields answer that on 20.1% of attempts that reach the gate, and the coins they cover are
# the established ones -- the wrong population. This reads the LOCAL tape instead: no vendor, no
# network call, and it counts DISTINCT MAKERS and detects self-trading, which a vendor volume
# number cannot.
#
# Reads only. Returns atomic quote units, so every RATIO built from these is decimal-free and needs
# no USD conversion. `reserve_quote` and `gross_quote` share units by construction.
#
# Solana pool_meta carries quote_side=1 on all 220 rows (the sol worker's vault path resolves it),
# so amount1 is the quote leg. Do NOT infer orientation from magnitude -- that is the
# self-valued-liquidity bug (finding-memecoin-self-valued-liquidity-2026-08-20).
SOL_SWAP_KINDS = ("sol_swap", "sol_curve_swap")
SOL_SYNC_KINDS = ("sol_sync", "sol_curve_sync")


def pool_flow_window(tc, pool, *, since_iso, until_iso=None):
    """Flow features for ONE pool over a window, from the local tape. Pure read.

    Returns a dict, or None when the pool has no swap in the window -- None means UNKNOWN, never
    'no flow'. The caller must fail OPEN on None (chase-guard convention).

    Every count is over the requested window. `reserve_quote` is the newest sync at or before
    `until_iso`, which may predate the window when a pool is quiet; `reserve_age_s` reports that
    so a caller can refuse a stale depth rather than silently trust it."""
    until_iso = until_iso or "9999"
    rows = tc.execute(
        "SELECT amount1, maker, observed_at FROM pool_ticks "
        "WHERE chain_id='solana' AND pool=? AND kind IN (?,?) "
        "AND observed_at>=? AND observed_at<=?",
        (pool, SOL_SWAP_KINDS[0], SOL_SWAP_KINDS[1], since_iso, until_iso)).fetchall()
    if not rows:
        return None
    buy_n = sell_n = 0
    gross = 0
    net = 0
    makers = set()
    for amount1, maker, _ts in rows:
        try:
            q = int(amount1)
        except (TypeError, ValueError):
            continue                       # undecodable leg is not evidence either way
        if maker:
            makers.add(maker)
        gross += abs(q)
        net += q
        # SIGN, VERIFIED AGAINST OUR OWN TRADES (2026-08-26). The previous comment here claimed
        # q<0 meant "someone bought the coin" and counted it as sell_n — INVERTED. Checked against
        # funded fills whose direction is known with certainty: our BUYS land on q>0 -> buy_n,
        # 32 of 32 legs. The field names are trader-side and correct; the old comment was not.
        # CAVEAT for anyone widening the scope: on a MULTI-HOP route the per-leg sign follows each
        # POOL, not the trade (one of our exit legs reads positive). This function is scoped to one
        # pool, which is what keeps the reading correct.
        if q < 0:
            sell_n += 1                    # quote leaves the pool: someone SOLD the coin
        elif q > 0:
            buy_n += 1                     # quote enters the pool: someone BOUGHT the coin
    # ONE QUERY PER KIND, not `kind IN (...)`. With IN, SQLite drops to
    # idx_pool_ticks_chain_time, which filters chain and time but NOT pool -- so it walks every
    # pool in the range. `kind=?` keeps it on idx_pool_ticks_pool_kind_time. Verified by
    # EXPLAIN QUERY PLAN; this is finding-sqlite-unindexed-filter-scans-the-tape-2026-08-23, where
    # the predicate missing from the SEARCH line IS the scan.
    res = None
    for kind in SOL_SYNC_KINDS:
        row = tc.execute(
            "SELECT amount1, observed_at FROM pool_ticks "
            "WHERE chain_id='solana' AND pool=? AND kind=? AND observed_at<=? "
            "ORDER BY observed_at DESC LIMIT 1",
            (pool, kind, until_iso)).fetchone()
        if row and (res is None or (row[1] or "") > (res[1] or "")):
            res = row
    reserve_quote = None
    reserve_at = None
    if res:
        try:
            reserve_quote = abs(int(res[0]))
            reserve_at = res[1]
        except (TypeError, ValueError):
            reserve_quote = None
    return {
        "pool": pool,
        "swaps_n": len(rows),
        "makers_n": len(makers),
        "buy_n": buy_n,
        "sell_n": sell_n,
        "gross_quote": gross,
        "net_quote": net,
        "reserve_quote": reserve_quote,
        "reserve_at": reserve_at,
        "window_start": since_iso,
    }


def flow_observation(features, *, clip_quote=None):
    """Turn raw window features into the two SCALE-FREE quantities the gate will judge on.

    Both reference the pool's own depth, so neither carries a dollar constant -- the same
    self-scaling property that makes execution_premium_decision work without a per-band table.

      turnover   = gross_quote / reserve_quote   how hard this pool is being traded right now
      clip_share = clip_quote  / gross_quote     how much of the real flow WE would be

    OBSERVATION ONLY. This returns numbers; it decides nothing. No threshold exists yet and one
    must not be invented from a distribution -- the precedent that works here
    (execution_premium_decision) took its 2.0x from a realised-outcome cliff, and we have zero
    pre-entry flow observations so far."""
    if not features:
        return None
    out = dict(features)
    reserve = features.get("reserve_quote") or 0
    gross = features.get("gross_quote") or 0
    out["turnover"] = (gross / reserve) if reserve > 0 else None
    out["clip_share"] = (clip_quote / gross) if (clip_quote and gross > 0) else None
    swaps = features.get("swaps_n") or 0
    makers = features.get("makers_n") or 0
    # A low distinct-maker count against many swaps is one entity trading with itself. A vendor
    # volume number cannot express this; it counts every leg as real demand.
    out["maker_ratio"] = (makers / swaps) if swaps > 0 else None
    return out


# Observation counters. A swallowed exception with no counter makes a permanently-dead observer
# look exactly like a quiet market: this hook was live on the funded path and had recorded ZERO in
# its entire life, and nothing anywhere said so. Same family as a resolve counter that reports
# success against an empty tape, and a feature job running green on the wrong chain.
# `logger.exception` is still wrong ON THIS PATH -- a funded entry must not be slowed or spammed by
# an observer -- so the compromise is: swallow the exception, COUNT the outcome, and emit one line
# per _OBSERVE_LOG_EVERY calls carrying the breakdown.
OBSERVE_COUNTS = {"calls": 0, "written": 0, "no_pool": 0, "no_swaps": 0, "error": 0}
_OBSERVE_LOG_EVERY = 50


def observe_counts_snapshot():
    """Copy of the observation counters, for a heartbeat or a health surface."""
    return dict(OBSERVE_COUNTS)


def _observe_miss(reason):
    OBSERVE_COUNTS[reason] = OBSERVE_COUNTS.get(reason, 0) + 1
    _maybe_log_observe()
    return None


def _maybe_log_observe():
    c = OBSERVE_COUNTS
    if c["calls"] and c["calls"] % _OBSERVE_LOG_EVERY == 0:
        logger.info("pre-entry flow: calls=%d written=%d no_pool=%d no_swaps=%d error=%d",
                    c["calls"], c["written"], c["no_pool"], c["no_swaps"], c["error"])


def observe_pre_entry_flow(mint, *, window_s=None, clip_quote=None, now_iso=None,
                           tape_path=None, connect=None):
    """Best-effort pre-entry flow for one mint. Returns the observation dict, or None.

    OBSERVATION ONLY. Nothing decides on this. It exists so that a week from now the recorded flow
    can be correlated against realized PnL and a threshold derived from OUTCOMES -- the way the
    2.0x execution-premium cap was derived from a realized -52.14% cliff, rather than from the
    shape of a predictor.

    Every failure returns None. This runs on the funded entry path, so it must not raise, must not
    block, and must not hold a writable handle on the tape."""
    OBSERVE_COUNTS["calls"] += 1
    if window_s is None:
        # Must be >= the prewarm lane's cron cadence, or a pre-warmed pool ages out of the window
        # between refreshes and every read returns no_swaps -- which is exactly how this hook came
        # to record zero for its first hours. A WIDER window is also cheaper: it buys the same
        # coverage at a slower refresh, and a longer sample is a steadier turnover figure, not a
        # worse one.
        try:
            import sniper.settings.memecoin as _cfg
            window_s = float(getattr(_cfg, "MEMECOIN_PRE_ENTRY_FLOW_WINDOW_S", 0) or 0) or 1800.0
        except Exception:
            window_s = 1800.0
    try:
        import sqlite3
        from sniper.util.clock import real_utc_now_iso
        if connect is None:
            if tape_path is None:
                from sniper.util.state_paths import state_path
                tape_path = str(state_path("data", "memecoin_sol_tape.db"))
            conn = sqlite3.connect("file:%s?mode=ro" % tape_path, uri=True, timeout=0.25)
        else:
            conn = connect()
        try:
            row = conn.execute(
                "SELECT pool FROM sol_pool_vaults WHERE mint=? ORDER BY resolved_at DESC LIMIT 1",
                (mint,)).fetchone()
            if not row:
                return _observe_miss("no_pool")   # never resolved -> UNKNOWN, not "no flow"
            import datetime as _dt
            until = now_iso or real_utc_now_iso()
            base = _dt.datetime.fromisoformat(str(until).replace("Z", "+00:00"))
            since = (base - _dt.timedelta(seconds=float(window_s))).isoformat()
            feats = pool_flow_window(conn, row[0], since_iso=since, until_iso=until)
            if not feats:
                # The pool is known but the tape has no swap inside the window. Usually a STALE
                # tape rather than a quiet coin: the prewarm lane was one-shot until dcd49320, so
                # a pool drew ticks once and then aged out of a 300s window within minutes.
                return _observe_miss("no_swaps")
            out = flow_observation(feats, clip_quote=clip_quote)
            OBSERVE_COUNTS["written"] += 1
            _maybe_log_observe()
            return out
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return _observe_miss("error")            # never raises on the funded path; always counted
