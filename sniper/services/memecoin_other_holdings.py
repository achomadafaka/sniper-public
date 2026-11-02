"""Other holdings: tokens in the funded Solana wallet that no position owns, and operator sells.

WHY (operator, 2026-09-13). The wallet holds about 30 token accounts and 29 were never positions:
route leftovers, dust from partial fills, and airdrops. Nothing showed them, so a holder reward
or an airdrop could arrive and nobody would know. The operator asked for one section that lists
them and a way to sell what can be sold.

THE RUNNER SIGNS; THE WEB ONLY ASKS. The web writes a sell intent. The runner claims it and sells
under the `sol-<pubkey>` execution lock, taken non-blocking like the rent sweep, so a chore never
makes a live exit wait. A mint that an open position or an unresolved reservation owns is never
listed and never sold: the protected set is read again inside the lock.

The sell reuses the funded exit quote (`exit_quote_fn`). That quote sizes from the exact raw
balance and already carries the Pump.fun native sell, the restricted-payout refusal, and the
packet-size cap.

A SIGNED SELL STAYS OPEN UNTIL THE CHAIN ANSWERS (Codex review, 2026-09-13). A send error, a
pending status, or a not-found status does not prove the transaction will never land. The intent
stays 'submitted' and blocks any other sell of the same mint. resolve_once settles it: confirmed or
failed on chain, or dead by the two facts of sig_provably_dead. Only the receipt measures proceeds.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from sniper.execution.solana_token_safety import TOKEN_2022_PROGRAM
from sniper.services.memecoin_rent_sweep import TOKEN_PROGRAM

TOKEN_PROGRAMS = (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"
SELL_SLIPPAGE_PCT = 10.0


def wallet_tokens(url, owner, *, call_fn=None):
    """{mint: (amount_raw, decimals)} for every non-zero token account of `owner`, both programs.
    Raises on a failed read: a partial list would drop holdings from the snapshot."""
    from sniper.services.memecoin_rent_sweep import rpc_call
    call_fn = call_fn or rpc_call
    out = {}
    for program in TOKEN_PROGRAMS:
        response = call_fn(url, "getTokenAccountsByOwner",
                           [owner, {"programId": program},
                            # CONFIRMED, the level a sell confirms at (2026-09-14, GOOGLx, LINK).
                            # The node default is finalized, ~13 s behind: the rescan after a
                            # sale read the old balance and listed the sold token again.
                            {"encoding": "jsonParsed", "commitment": "confirmed"}])
        if "result" not in response:
            raise RuntimeError("getTokenAccountsByOwner failed: %s" % response.get("error"))
        for account in (response["result"] or {}).get("value") or []:
            try:
                info = account["account"]["data"]["parsed"]["info"]
                raw = int(info["tokenAmount"]["amount"] or 0)
                decimals = int(info["tokenAmount"]["decimals"])
                mint = info["mint"]
            except (KeyError, TypeError, ValueError):
                continue
            if raw > 0:
                prev = out.get(mint, (0, decimals))[0]
                out[mint] = (prev + raw, decimals)
    return out


JUP_TOKEN_SEARCH = "https://lite-api.jup.ag/tokens/v2/search?query="
# ONE ASK PER TOKEN PER 30 MINUTES, misses included. This runs on the runner's main loop and
# shares Jupiter's keyless per-IP limit with funded quotes, so it must not ask every 300 s pass,
# and a slow answer must not hold exits: 3 s timeout, then the snapshot goes on without names.
JUP_FACTS_TTL_S = 1800.0
JUP_FACTS_TIMEOUT_S = 3.0
_FACTS_CACHE = {}          # mint -> (expires_monotonic, facts dict or None)


def jupiter_token_facts(mints, *, get_fn=None, now_fn=None):
    """Token names, reference prices and explicit suspicion flags, or {} on lookup failure.

    WHY (operator, 2026-09-14: "tons of them have no names"). 14 of 24 holdings had no symbol:
    DexScreener lists no pair for tokenized stocks (xStocks, Backpack, Ondo) or bridged majors
    (LINK, ZEC, HYPE), so it returned no name and no price. Jupiter's token list named all 14 and
    priced 12. Cached batched reads also audit named tokens, so a lookalike's provider reference
    price cannot override an explicit suspicion flag. A failure answers {} without stopping
    the balance snapshot."""
    if not mints:
        return {}
    import time
    now = (now_fn or time.monotonic)()
    out, ask = {}, []
    for mt in mints:
        hit = _FACTS_CACHE.get(mt)
        if hit and hit[0] > now:
            if hit[1]:
                out[mt] = hit[1]
        else:
            ask.append(mt)
    if not ask:
        return out
    if get_fn is None:
        import json
        import urllib.request

        def get_fn(url):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=JUP_FACTS_TIMEOUT_S) as resp:
                return json.load(resp)
    for i in range(0, len(ask), 100):
        chunk = ask[i:i + 100]
        try:
            rows = get_fn(JUP_TOKEN_SEARCH + ",".join(chunk)) or []
        except Exception as exc:
            # Not cached: a transport failure says nothing about the token. The next pass asks.
            logger.warning("other holdings: jupiter token names unavailable (%s)", exc)
            return out
        found = {}
        for t in rows:
            if isinstance(t, dict) and t.get("id") in chunk:
                found[t["id"]] = {"symbol": t.get("symbol"), "price": t.get("usdPrice"),
                                  "liquidity": t.get("liquidity")}
                if isinstance(t.get("audit"), dict) and t["audit"].get("isSus") is True:
                    found[t["id"]].update(price=None, untrusted_price=True)
        for mt in chunk:
            _FACTS_CACHE[mt] = (now + JUP_FACTS_TTL_S, found.get(mt))
        out.update(found)
    return out


def snapshot_once(conn, *, rpc_candidates, owner, now_iso, call_fn=None, price_fn=None,
                  token_facts_fn=None):
    """Rewrite the Solana other-holdings snapshot. Returns a tally; never raises."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_rent_sweep import protected_mints
    tally = {"state": None, "rows": 0, "unpriced": 0}
    try:
        protected = protected_mints(conn)
        if protected is None:
            tally["state"] = "protected_unreadable"
            return tally
        held = None
        for url in rpc_candidates:
            if not url:
                continue
            try:
                held = wallet_tokens(url, owner, call_fn=call_fn)
                break
            except Exception:
                continue
        if held is None:
            tally["state"] = "rpc_unreadable"      # keep the old snapshot; never write an empty one
            return tally
        # A token sold in the last 60 s stays off the list even if a lagging node still shows it.
        just_sold = m.recently_sold_holdings(conn, "solana", now_iso=now_iso, within_s=60)
        mints = [mt for mt in held if mt not in protected and mt not in (USDC_MINT, WSOL_MINT)
                 and mt not in just_sold]
        if price_fn is None:
            from sniper.execution.solana_quote import dexscreener_stats_batch_keyed as price_fn
        prices = price_fn([("solana", mt) for mt in mints]) if mints else {}
        # Audit even a named/priced token: a lookalike can carry another asset's reference price.
        facts = (token_facts_fn or jupiter_token_facts)(mints) if mints else {}
        rows = []
        for mt in mints:
            raw, decimals = held[mt]
            amount = raw / (10 ** decimals)
            stats = prices.get(("solana", mt)) or {}
            jup = facts.get(mt) or {}
            price = None if not jup or jup.get("untrusted_price") else stats.get("price") or jup.get("price")
            rows.append({"mint": mt, "symbol": stats.get("symbol") or jup.get("symbol"),
                         "amount": amount,
                         "amount_raw": raw, "decimals": decimals, "price_usd": price,
                         "value_usd": (amount * price) if price else None,
                         "liquidity_usd": stats.get("liquidity") if stats.get("price")
                         else jup.get("liquidity")})
        unpriced = sum(1 for r in rows if r["price_usd"] is None)
        m.replace_other_holdings(conn, "solana", rows, observed_at=now_iso, unpriced_count=unpriced)
        tally.update(state="ok", rows=len(rows), unpriced=unpriced)
        return tally
    except Exception:
        logger.exception("other holdings snapshot failed")
        tally["state"] = "error"
        return tally


def _record_sold(conn, iid, sig, now_iso, receipt_proceeds_fn, quote, tally):
    """Mark a confirmed sell. Only the receipt measures proceeds; a quote is not money received."""
    from sniper.persistence import db_memecoin as m
    usd = None
    if receipt_proceeds_fn is not None:
        try:
            usd = receipt_proceeds_fn(sig)
        except Exception:
            logger.exception("holding sell: receipt read failed sig=%s", sig)
    reason = None
    if usd is None:
        quoted = int((quote or {}).get("outAmount") or 0) / 1e6
        reason = ("proceeds not read from the receipt" if not quoted
                  else "proceeds not read from the receipt; the quote was $%.2f" % quoted)
    m.resolve_holding_sell(conn, iid, "sold", now_iso=now_iso, sig=sig, reason=reason,
                           usd_received=None if usd is None else round(float(usd), 6))
    tally.update(state="sold", usd_received=usd)
    logger.warning("holding sell CONFIRMED intent=%s usd=%s sig=%s", iid, usd, sig)
    return tally


def resolve_once(conn, *, pubkey, rpc_url, now_iso, confirm_once_fn, height_fn,
                 receipt_proceeds_fn=None, balance_fn=None):
    """Settle signed sells whose result was unknown. Returns {intent_id: new state}; never raises.

    'failed' on NEGATIVE evidence needs both facts of sig_provably_dead: the chain is past the
    signature's last valid block height, and the raw balance is still the full signed amount.
    Anything less leaves the intent 'submitted'.
    """
    from sniper.persistence import db_memecoin as m
    out = {}
    try:
        rows = m.unresolved_holding_sells(conn, "solana")
    except Exception:
        logger.exception("holding sell resolve: could not read unresolved intents")
        return out
    if balance_fn is None:
        from sniper.execution.solana_wallet import token_balance_raw

        def balance_fn(mint):
            return token_balance_raw(pubkey, mint, rpc_url)
    for row in rows:
        iid, sig = row["intent_id"], row["sig"]
        try:
            status = confirm_once_fn(sig)
            if status == "confirmed":
                _record_sold(conn, iid, sig, now_iso, receipt_proceeds_fn, None, {})
                out[iid] = "sold"
                continue
            if status == "failed":
                m.resolve_holding_sell(conn, iid, "failed", now_iso=now_iso,
                                       reason="the sell landed and failed on chain")
                out[iid] = "failed"
                continue
            last_valid, snapshot = row["last_valid_height"], row["balance_raw"]
            if last_valid is None or not snapshot:
                continue                                   # no proof possible; stay unresolved
            height = height_fn()
            if not isinstance(height, (int, float)) or height <= last_valid:
                continue                                   # still inside the window
            balance = balance_fn(row["mint"])
            if isinstance(balance, int) and balance >= int(snapshot):
                m.resolve_holding_sell(conn, iid, "failed", now_iso=now_iso,
                                       reason="the sell expired and never landed")
                out[iid] = "failed"
        except Exception:
            logger.exception("holding sell resolve failed intent=%s", iid)
    return out


def solana_book_count(conn):
    """Open funded Solana positions plus unresolved Solana reservations, or None if unreadable."""
    try:
        return int(conn.execute(
            "SELECT (SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
            "AND is_open=1 AND network_id='solana') + (SELECT COUNT(*) FROM "
            "memecoin_pending_entries WHERE status='reserved' "
            "AND COALESCE(network_id,'solana')='solana')").fetchone()[0])
    except Exception:
        logger.exception("holding sell: could not read the funded Solana book")
        return None


def sell_once(conn, *, keypair, pubkey, rpc_url, now_iso, exit_quote_fn, min_sell_usd,
              receipt_proceeds_fn=None, lock_dir=None, build_fn=None, sign_fn=None,
              send_fn=None, confirm_fn=None):
    """Claim one queued sell intent and sell the whole balance of its mint for USDC.
    Returns a tally; never raises. Sells nothing when the wallet lock is busy."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_rent_sweep import protected_mints
    tally = {"state": None, "intent_id": None, "sig": None}
    try:
        from sniper.execution.exec_lock import wallet_exec_lock
        with wallet_exec_lock("sol-%s" % pubkey, blocking=False, lock_dir=lock_dir) as held:
            if not held:
                tally["state"] = "wallet_busy"      # the intent stays queued for the next pass
                return tally
            if m.unresolved_withdraws(conn, "solana"):
                tally["state"] = "withdrawal_unresolved"
                return tally
            # THE FUNDED BOOK GOES FIRST (2026-09-14, operator: "make sure everything stays optimal
            # for the runner and the funded path"). The sell holds this lock through its confirm,
            # about 1-2 s and up to 40 s, and a live entry or exit on this wallet waits for it.
            # So an operator sell waits, still queued and unclaimed, until no Solana position is
            # open and no reservation is unresolved. Read inside the lock: no entry is mid-flight.
            book = solana_book_count(conn)
            if book is None or book > 0:
                tally["state"] = "book_busy" if book else "book_unreadable"
                return tally
            intent = m.claim_holding_sell(conn, "solana", now_iso=now_iso)
            if intent is None:
                tally["state"] = "idle"
                return tally
            iid, mint = intent["intent_id"], intent["mint"]
            tally["intent_id"] = iid

            def fail(reason):
                m.resolve_holding_sell(conn, iid, "failed", now_iso=now_iso, reason=reason)
                tally["state"] = "failed"
                tally["reason"] = reason
                return tally

            protected = protected_mints(conn)
            if protected is None:
                return fail("could not read the open positions; sold nothing")
            if mint in protected:
                return fail("an open position owns this token; use its card")

            from sniper.execution.solana_real_seams import (
                jupiter_swap_tx, sign_swap_tx, rpc_send, rpc_confirm, _clamp_exit_fee)
            from sniper.execution.solana_exit_executor import _oversized, _exit_retry_accounts
            build_fn = build_fn or jupiter_swap_tx
            sign_fn = sign_fn or sign_swap_tx
            send_fn = send_fn or rpc_send
            confirm_fn = confirm_fn or rpc_confirm

            quote = exit_quote_fn(mint, None, SELL_SLIPPAGE_PCT)
            signed = None
            for attempt in (0, 1):
                if not quote:
                    return fail("no route sells this token")
                out_usd = int(quote.get("otherAmountThreshold", quote.get("outAmount")) or 0) / 1e6
                if out_usd <= 0:
                    return fail("the route has no positive minimum output")
                if not intent.get("allow_small") and out_usd < float(min_sell_usd):
                    return fail("a sell returns $%.2f, under the $%.2f minimum"
                                % (out_usd, float(min_sell_usd)))
                tx_b64 = build_fn(quote, pubkey, return_metadata=True)
                tx_b64 = _clamp_exit_fee(tx_b64)[0]        # exit fee clamp; never refuses
                signed = sign_fn(tx_b64, keypair) if tx_b64 else None
                if not signed:
                    return fail("the route did not build")
                cap = _exit_retry_accounts()
                if attempt == 0 and cap and _oversized(signed[0]):
                    try:
                        quote = exit_quote_fn(mint, None, SELL_SLIPPAGE_PCT, max_accounts=cap)
                    except TypeError:
                        quote = None
                    continue
                break
            if _oversized(signed[0]):
                return fail("every route is too large for one transaction")
            signed_b64, sig = signed[0], signed[1]
            validity = signed[2] if len(signed) > 2 and isinstance(signed[2], dict) else {}

            # WRITE-AHEAD: the sig and the two expiry facts are on the intent before the
            # broadcast. The quote sells the exact raw balance, so inAmount is that balance.
            m.resolve_holding_sell(conn, iid, "submitted", now_iso=now_iso, sig=sig,
                                   last_valid_height=validity.get("last_valid_height"),
                                   balance_raw=int(quote.get("inAmount") or 0) or None)
            tally["sig"] = sig
            # A send that reports no signature can still be in flight, so the result always
            # comes from the chain, never from the send.
            send_fn(signed_b64, rpc_url)
            status = confirm_fn(sig, rpc_url)
            if status == "confirmed":
                return _record_sold(conn, iid, sig, now_iso, receipt_proceeds_fn, quote, tally)
            if status == "failed":
                return fail("the sell landed and failed on chain")
            # pending / not_found / unavailable: the result is unknown. The intent stays
            # 'submitted', no other sell of this mint can start, and resolve_once settles it.
            tally["state"] = "unresolved"
            logger.warning("holding sell UNRESOLVED status=%s mint=%s sig=%s", status, mint, sig)
            return tally
    except Exception:
        logger.exception("holding sell pass failed")
        if tally.get("intent_id") and tally.get("state") is None:
            try:
                m.resolve_holding_sell(conn, tally["intent_id"],
                                       "failed" if not tally.get("sig") else "submitted",
                                       now_iso=now_iso, reason="internal error; check the runner log")
            except Exception:
                logger.exception("holding sell: could not record the failure")
        tally["state"] = "error"
        return tally


# --- off the funded loop (2026-09-27, Alpha GO C1) -------------------------------------------------
#
# The snapshot, the operator sell and the resolve ran inline in the funded main loop (maint
# "other_holdings", worst sample 6.2 s since 09-24; the snapshot's Jupiter token-name urlopen has a 3 s
# socket timeout that does not bound DNS). A copy of the proven memecoin_evm_holdings.start_pass: one
# worker thread per pass with its own connection, single-flight, and the main loop only computes the
# due flags. The sell code and its NON-BLOCKING "sol-<pubkey>" wallet lock are unchanged.
# MEMECOIN_SOLANA_HOLDINGS_THREAD=0 runs everything inline, as before.

import os as _os
import time as _time

# Written by the main loop (thread, last_sell_try, pass_started_*) and by the worker (the rest):
# disjoint keys, single-item assignments. Telemetry and single-flight only.
_WORKER = {"thread": None, "last_sell_try": 0.0, "pass_started_at": None, "last_pass_s": None,
           "last_snapshot_at": None, "failures": 0}
SELL_RETRY_S = 15.0        # a sell waiting for a quiet wallet re-checks at most this often
QUIET_STATES = ("wallet_busy", "idle", "book_busy")


def thread_enabled(env=None):
    env = _os.environ if env is None else env
    return str(env.get("MEMECOIN_SOLANA_HOLDINGS_THREAD", "1")).strip().lower() not in {
        "0", "false", "no", "off"}


def _residue_passes(conn, receipt_fn, pubkey, now_fn, log_fn):
    """QUOTE-RESIDUE CREDITS (operator 2026-10-07): record what exits left, then credit the holding
    sells that sold it, on the snapshot cadence. Flag off: one attribute read. Each pass has its own
    try so a failure never skips the other or the snapshot."""
    from sniper.settings import memecoin as cfg
    if not getattr(cfg, "MEMECOIN_QUOTE_RESIDUE_CREDITS", False):
        return
    from sniper.services import memecoin_quote_residue as qr
    for name in ("read_exit_residue_once", "credit_sells_once"):
        try:
            getattr(qr, name)(conn, receipt_fn=receipt_fn, owner=pubkey, now_iso=now_fn(), log=log_fn)
        except Exception:
            logger.exception("quote residue %s failed", name)


def start_pass(ctx, *, keypair, pubkey, rpc_url, rpc_candidates, snapshot_due, sell_due,
               resolve_due, min_sell_usd, now_fn, log_fn=print, connect=None):
    """Run one sell / resolve / snapshot pass on a worker thread with its own connection.
    Returns True when a pass started; False while the previous one still runs or there is no work.
    A sell or resolve that did something is followed by a snapshot in the same pass (the inline
    code rescanned on the next cycle)."""
    import threading
    running = _WORKER["thread"]
    if running is not None and running.is_alive():
        return False
    can_sell = bool(sell_due and keypair is not None and ctx.get("exit_quote_fn"))
    if can_sell and _time.monotonic() - _WORKER["last_sell_try"] < SELL_RETRY_S:
        can_sell = False
    can_resolve = bool(resolve_due and ctx.get("confirm_once_fn") and ctx.get("block_height_fn"))
    if not (snapshot_due or can_sell or can_resolve):
        return False
    if can_sell:
        _WORKER["last_sell_try"] = _time.monotonic()

    def run():
        started, conn = _time.monotonic(), None
        try:
            from sniper.persistence.db_core import get_connection
            conn = (connect or get_connection)()
            rescan = bool(snapshot_due)
            if can_sell:
                t = sell_once(conn, keypair=keypair, pubkey=pubkey, rpc_url=rpc_url,
                              now_iso=now_fn(), exit_quote_fn=ctx["exit_quote_fn"],
                              min_sell_usd=min_sell_usd,
                              receipt_proceeds_fn=ctx.get("receipt_proceeds_fn"),
                              send_fn=ctx.get('rpc_send_fn'), confirm_fn=ctx.get('rpc_confirm_fn'))
                if t.get("state") not in QUIET_STATES:
                    log_fn("[memecoin] other holdings sell: %s" % t)
                    rescan = True
            if can_resolve:
                r = resolve_once(conn, pubkey=pubkey, rpc_url=rpc_url, now_iso=now_fn(),
                                 confirm_once_fn=ctx["confirm_once_fn"],
                                 height_fn=ctx["block_height_fn"],
                                 receipt_proceeds_fn=ctx.get("receipt_proceeds_fn"))
                if r:
                    log_fn("[memecoin] other holdings resolved: %s" % r)
                    rescan = True
            if snapshot_due and ctx.get("receipt_fn") is not None:
                _residue_passes(conn, ctx["receipt_fn"], pubkey, now_fn, log_fn)
            if rescan:
                t = snapshot_once(conn, rpc_candidates=rpc_candidates, owner=pubkey,
                                  now_iso=now_fn(), call_fn=ctx.get('rpc_call_fn'))
                if t.get("state") == "ok":
                    _WORKER["last_snapshot_at"] = now_fn()
                else:
                    log_fn("[memecoin] other holdings scan: %s" % t)
        except Exception:
            _WORKER["failures"] += 1
            logger.exception("Solana other holdings pass failed")
        finally:
            _WORKER["last_pass_s"] = round(_time.monotonic() - started, 2)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    _WORKER["pass_started_at"] = now_fn()
    thread = threading.Thread(target=run, name="memecoin-solana-holdings", daemon=True)
    _WORKER["thread"] = thread
    thread.start()
    return True


def worker_status():
    """For the heartbeat. A hung pass reads alive=True with an old pass_started_at."""
    t = _WORKER["thread"]
    return {"alive": bool(t is not None and t.is_alive()),
            "pass_started_at": _WORKER["pass_started_at"], "last_pass_s": _WORKER["last_pass_s"],
            "last_snapshot_at": _WORKER["last_snapshot_at"], "failures": _WORKER["failures"]}
