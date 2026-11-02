"""Quote-asset residue: record it at the fill, sweep it back to USDC through the existing holding sell.

Design: docs/superpowers/specs/2026-09-27-slot-lane-any-venue-design.md section 8.1 (2026-09-28).

WHY. An ExactIn leg (USDC -> quote asset) delivers the quote's outAmount and the venue buy spends
only the leg's minimum, so up to the leg slippage (1.5% of the clip) stays in custody as the quote
asset (ZEC, xStocks, launchpad tokens). ExactOut routes for 12 of 29 census quote mints and none of
the stock tokens, so the answer is ExactIn plus this sweep.

NO NEW SELL PATH. The sweep only ENQUEUES the operator's reviewed holding sell
(memecoin_other_holdings.sell_once, 2026-09-13), which the runner claims only on a FLAT Solana book,
under the wallet lock taken non-blocking, re-reading the protected mints (open positions and
reservations) inside the lock, write-ahead and chain-settled. That sell takes the WHOLE balance, so
the sweep enqueues a mint only when the wallet holds no more of it than the recorded unswept residue
(x ENQUEUE_TOLERANCE, rounding): a quote asset the operator holds on purpose (JUP, Fartcoin, PENGU
and APE are census quote assets) is logged `foreign_balance` and never sold (Alpha, 2026-09-28).
SOL and USDC are never swept.

WHEN IT RUNS. MEMECOIN_QUOTE_RESIDUE_SWEEP (default off) gates the cycle; residue rows exist only
after a filled LIVE venue buy with a non-SOL/USDC quote, and every venue's live list is empty today,
so this is inert until the first exotic-quote live commit wires record_fill_residue at its fill.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

NETWORK = "solana"
NEVER_SWEPT = frozenset({"So11111111111111111111111111111111111111112",
                         "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"})
MIN_SWEEP_USD = 5.0          # fee and priority ~$0.001-0.01, route slippage <= 1%: $5 clears both
AGED_S = 7 * 24 * 3600       # after a week, sweep from $0.50 (issuer pause/delegate risk is carried)
AGED_MIN_USD = 0.50
ENQUEUE_TOLERANCE = 1.02     # the snapshot's balance may exceed the recorded residue by rounding only
SWEEP_EVERY_S = 60.0         # the runner loop ticks every few seconds; the sweep needs a minute
_STATE = {"last_run": None, "foreign_logged": {}}


def residue_from_receipt(meta, owner, quote_mint):
    """The quote asset the fill left with `owner`: its token balance after the transaction minus
    before (atomic), from the receipt's pre/post token balances, never from the quote. None when the
    receipt does not show the owner's account for that mint."""
    def total(rows):
        seen, amount = False, 0
        for row in rows or ():
            if row.get("mint") == quote_mint and row.get("owner") == owner:
                seen = True
                amount += int(((row.get("uiTokenAmount") or {}).get("amount")) or 0)
        return seen, amount
    pre_seen, pre = total((meta or {}).get("preTokenBalances"))
    post_seen, post = total((meta or {}).get("postTokenBalances"))
    if not post_seen:
        return None
    return post - (pre if pre_seen else 0)


def record_fill_residue(conn, *, quote_mint, residue_atomic, quote_usd, decimals, venue, attempt_id,
                        entry_sig, recorded_at):
    """Append one live fill's residue (called only after a filled LIVE exotic-quote venue buy).
    Nothing is recorded for SOL/USDC or a non-positive amount. Returns True when a row was written."""
    from sniper.persistence import db_memecoin as m
    if quote_mint in NEVER_SWEPT or residue_atomic is None or int(residue_atomic) <= 0:
        return False
    usd = (int(residue_atomic) / 10 ** int(decimals) * float(quote_usd)
           if quote_usd is not None and decimals is not None else None)
    m.record_quote_residue(conn, network_id=NETWORK, quote_mint=quote_mint,
                           residue_atomic=int(residue_atomic), residue_usd=usd, venue=venue,
                           attempt_id=attempt_id, entry_sig=entry_sig, recorded_at=recorded_at)
    return True


def _age_s(now_iso, then_iso):
    parse = lambda s: datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return (parse(now_iso) - parse(then_iso)).total_seconds()


def plan_sweep(unswept, balances, *, now_iso, live_intent_mints=frozenset()):
    """One decision per mint with unswept residue. Pure.

    `unswept` is db_memecoin.unswept_quote_residue's shape; `balances` maps mint -> (amount_raw or
    None, snapshot observed_at or None) from the other-holdings snapshot. A decision is
    {"mint", "action": "enqueue"|"skip", "reason", "allow_small", "ids", "residue_atomic",
    "balance_raw", "usd"}."""
    decisions = []
    for mint, agg in sorted(unswept.items(), key=lambda kv: -kv[1]["usd"]):
        balance, observed = balances.get(mint, (None, None))
        d = {"mint": mint, "action": "skip", "reason": None, "allow_small": False, "ids": agg["ids"],
             "residue_atomic": agg["atomic"], "balance_raw": balance, "usd": agg["usd"]}
        decisions.append(d)
        aged = _age_s(now_iso, agg["oldest"]) >= AGED_S
        if mint in NEVER_SWEPT:
            d["reason"] = "never_swept"
        elif mint in live_intent_mints:
            d["reason"] = "sell_in_progress"
        elif agg["usd"] < (AGED_MIN_USD if aged else MIN_SWEEP_USD):
            d["reason"] = "under_threshold"
        elif observed is None or observed < agg["newest"]:
            d["reason"] = "snapshot_older_than_residue"      # the snapshot may not hold the fill yet
        elif balance is None or balance <= 0:
            d["reason"] = "not_held"                         # zero, or a position owns the mint
        elif balance > agg["atomic"] * ENQUEUE_TOLERANCE:
            d["reason"] = "foreign_balance"                  # the operator holds more of it: never sell
        else:
            d.update(action="enqueue", reason="residue", allow_small=aged and agg["usd"] < MIN_SWEEP_USD)
    return decisions


def run_sweep_once(conn, *, now_iso, log=None, clock=time.monotonic):
    """One sweep cycle on the runner loop (no network: two ledger reads, at most one intent write
    per eligible mint). Inert unless MEMECOIN_QUOTE_RESIDUE_SWEEP is on. Returns the decisions."""
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    if not bool(getattr(cfg, "MEMECOIN_QUOTE_RESIDUE_SWEEP", False)):
        return []
    now_mono = clock()
    if _STATE["last_run"] is not None and now_mono - _STATE["last_run"] < SWEEP_EVERY_S:
        return []
    _STATE["last_run"] = now_mono
    unswept = m.unswept_quote_residue(conn, NETWORK)
    if not unswept:
        return []
    live = m.live_holding_sell_mints(conn, NETWORK)
    balances = {mint: m.other_holding_balance(conn, NETWORK, mint) for mint in unswept}
    decisions = plan_sweep(unswept, balances, now_iso=now_iso, live_intent_mints=frozenset(live))
    for d in decisions:
        if d["action"] == "enqueue":
            try:
                intent_id = m.enqueue_holding_sell(conn, NETWORK, d["mint"], now_iso=now_iso,
                                                   allow_small=bool(d["allow_small"]))
            except ValueError as exc:                    # a sell for this mint is already live
                d.update(action="skip", reason="enqueue_refused: %s" % exc)
                continue
            m.mark_quote_residue_swept(conn, d["ids"], intent_id, now_iso=now_iso)
            d["intent_id"] = intent_id
        foreign_key = (d["residue_atomic"], d["balance_raw"])
        if d["reason"] == "foreign_balance":
            if _STATE["foreign_logged"].get(d["mint"]) == foreign_key:
                continue                                 # logged once per change, not every minute
            _STATE["foreign_logged"][d["mint"]] = foreign_key
        if log is not None and (d["action"] == "enqueue" or d["reason"] == "foreign_balance"):
            log("[memecoin] quote residue sweep %s %s: residue %s (~$%.2f) balance %s%s" % (
                d["mint"][:8], d["reason"], d["residue_atomic"], d["usd"], d["balance_raw"],
                " -> intent %s" % d.get("intent_id") if d.get("intent_id") else ""))
    return decisions


# --- residue credits (operator order 2026-10-07 ~15:30Z via Alpha) ---------------------------------
#
# WHY. A native pump exit (pump_native_exit.build_native_exit) sizes its quote -> USDC tail to the
# curve leg's MINIMUM, so a fill better than the minimum leaves the quote asset in the wallet. The
# exit books only the USDC it received; the leftover was sold later through Other Holdings and
# never reached the position. Zphinx (71502): booked +$4.61, true ~+$49.99.
#
# TWO PASSES on the holdings worker thread (off the funded loop), each gated by
# MEMECOIN_QUOTE_RESIDUE_CREDITS and bounded by MEMECOIN_QUOTE_RESIDUE_CREDITS_SINCE:
#   read_exit_residue_once  one receipt per real Solana exit fill, read once: every mint other than
#                           the coin, USDC and wSOL that the exit left becomes a residue row bound to
#                           the position (none = a known zero; unreadable = unknown, never zero).
#   credit_sells_once       a sold holding sell's proceeds, split pro rata by atomic amount across
#                           the residue rows it consumed, into those positions' realized_pnl
#                           (append-only memecoin_quote_residue_credits). The share of the balance
#                           no row explains (airdrops, older dust) is recorded and never booked.

SKIPPED_MINTS = NEVER_SWEPT
READS_PER_PASS = 5
_SELL_RECEIPTS = {}          # sig -> receipt: a confirmed sell's receipt never changes


def _valid_token_balances(tx):
    """Complete unsigned atomic snapshots, with each account's identity stable across sides."""
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        if not isinstance(keys, list) or not keys:
            return False
        before, mint_decimals, key_indexes = {}, {}, {}
        for field in ("preTokenBalances", "postTokenBalances"):
            entries = tx["meta"][field]
            if not isinstance(entries, list):
                return False
            accounts = {}
            for row in entries:
                index, mint, owner = row["accountIndex"], row["mint"], row["owner"]
                amount, decimals = row["uiTokenAmount"]["amount"], row["uiTokenAmount"]["decimals"]
                if (type(index) is not int or not 0 <= index < len(keys) or index in accounts
                        or not isinstance(mint, str) or not mint or not isinstance(owner, str) or not owner
                        or type(decimals) is not int or not 0 <= decimals <= 255
                        or (type(amount) is not int and (not isinstance(amount, str)
                            or not amount.isascii() or not amount.isdecimal()))
                        or not 0 <= int(amount) < 2 ** 64):
                    return False
                key = keys[index]
                pubkey = key.get("pubkey") if isinstance(key, dict) else key
                identity = mint, owner, decimals
                if (not isinstance(pubkey, str) or not pubkey or key_indexes.setdefault(pubkey, index) != index
                        or (index in before and before[index] != identity)
                        or mint_decimals.setdefault(mint, decimals) != decimals):
                    return False
                accounts[index] = identity
            before = accounts
        return True
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, AttributeError):
        return False


def _owner_deltas(meta, owner):
    """{mint: (post - pre atomic, decimals, owner token account indexes)} for `owner`."""
    out = {}
    for rows, sign in (((meta or {}).get("preTokenBalances"), -1),
                       ((meta or {}).get("postTokenBalances"), 1)):
        for row in rows or ():
            if row.get("owner") != owner:
                continue
            amount = (row.get("uiTokenAmount") or {})
            delta, decimals, idx = out.get(row.get("mint"), (0, None, set()))
            idx.add(row.get("accountIndex"))
            out[row.get("mint")] = (delta + sign * int(amount.get("amount") or 0),
                                    amount.get("decimals", decimals), idx)
    return out


def _transfers_out(tx, accounts):
    """Atomic amount moved OUT of `accounts` by parsed SPL transfers (top-level and inner)."""
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    ixs = list(msg.get("instructions") or ())
    for group in ((tx or {}).get("meta") or {}).get("innerInstructions") or ():
        ixs += list(group.get("instructions") or ())
    total = 0
    for ix in ixs:
        parsed = ix.get("parsed") if isinstance(ix, dict) else None
        if not isinstance(parsed, dict) or parsed.get("type") not in ("transfer", "transferChecked"):
            continue
        info = parsed.get("info") or {}
        if info.get("source") in accounts:
            total += int(info.get("amount") or (info.get("tokenAmount") or {}).get("amount") or 0)
    return total


def exit_residue_from_receipt(tx, owner, coin_mint):
    """What one successful exit left with `owner`: {"rows": [{"mint", "atomic", "decimals", "usd"}],
    "negative": [mint]}. None when the receipt is missing, failed or has no balances (unknown).
    usd prices the row at the exit's own tail rate (USDC received / quote transferred out); None
    when no transfer out is visible. A negative delta (the exit spent a balance it did not create)
    is flagged and never recorded."""
    meta = (tx or {}).get("meta")
    if not isinstance(meta, dict) or meta.get("err") is not None:
        return None
    if not _valid_token_balances(tx):
        return None
    deltas = _owner_deltas(meta, owner)
    usdc = deltas.get("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", (0, 6, set()))[0] / 1e6
    keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")) or []
    pubkey = lambda k: k.get("pubkey") if isinstance(k, dict) else k
    rows, negative = [], []
    for mint, (delta, decimals, idx) in sorted(deltas.items()):
        if mint == coin_mint or mint in SKIPPED_MINTS or delta == 0:
            continue
        if delta < 0:
            negative.append(mint)
            continue
        accounts = {pubkey(keys[i]) for i in idx if isinstance(i, int) and i < len(keys)}
        out = _transfers_out(tx, accounts)
        usd = (delta * usdc / out) if out > 0 and usdc > 0 else None
        rows.append({"mint": mint, "atomic": int(delta), "decimals": decimals, "usd": usd})
    return {"rows": rows, "negative": negative}


def plan_sell_credits(owners, *, consumed_atomic, usd_received):
    """Split one sell pro rata by atomic amount. Pure. The denominator is the larger of what the
    sell consumed and what the rows explain: a balance above the rows is foreign (recorded, never
    booked); a balance below them (`short`) splits the proceeds over the rows so the credits never
    exceed the money received."""
    total = sum(int(o["atomic"]) for o in owners)
    consumed = int(consumed_atomic)
    denom = max(consumed, total)
    usd = float(usd_received)
    credits = [{**o, "usd": (int(o["atomic"]) / denom * usd) if denom > 0 else 0.0} for o in owners]
    foreign = max(0, consumed - total)
    return {"credits": credits, "foreign_atomic": foreign,
            "foreign_usd": (foreign / consumed * usd) if consumed > 0 else 0.0,
            "short": total > consumed}


def _credits_on():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_QUOTE_RESIDUE_CREDITS", False))


def _since():
    from sniper.settings import memecoin as cfg
    return str(getattr(cfg, "MEMECOIN_QUOTE_RESIDUE_CREDITS_SINCE", "") or "2026-09-09T00:00:00+00:00")


def _bound_receipt(tx, sig, owner):
    """The requested successful landed transaction, signed by the funded fee payer."""
    try:
        transaction, meta, slot = tx["transaction"], tx["meta"], tx["slot"]
        payer = transaction["message"]["accountKeys"][0]
        return bool(sig and owner and transaction["signatures"][0] == sig
                    and isinstance(payer, dict) and payer.get("pubkey") == owner
                    and payer.get("signer") is True and isinstance(meta, dict)
                    and "err" in meta and meta["err"] is None and type(slot) is int and slot > 0)
    except (KeyError, IndexError, TypeError):
        return False


def read_exit_residue_once(conn, *, receipt_fn, owner, now_iso, limit=READS_PER_PASS, log=None,
                           force=False):
    """Read up to `limit` unsettled exit receipts and record what each left. Inert with the flag
    off unless `force` (the backfill script). Returns [(sig, state, n_rows)]."""
    from sniper.persistence import db_memecoin as m
    if not (force or _credits_on()):
        return []
    done = []
    for fill in m.unread_exit_fills(conn, since_iso=_since(), limit=limit):
        try:
            tx = receipt_fn(fill["sig"])
        except Exception:
            logger.exception("quote residue: exit receipt read failed sig=%s", fill["sig"])
            tx = None
        out = (exit_residue_from_receipt(tx, owner, fill["mint"])
               if _bound_receipt(tx, fill["sig"], owner) else None)
        state = m.record_exit_residue_read(conn, fill, rows=None if out is None else out["rows"],
                                           negative=None if out is None else out["negative"],
                                           now_iso=now_iso)
        n = len(out["rows"]) if out else 0
        done.append((fill["sig"], state, n))
        if state == "unreadable":
            # Once per signature: the read reaches 'unreadable' exactly once. Later sells are not
            # held by it; the residue it may have left is unknown and is never booked.
            from sniper.services import memecoin_alerts as alerts
            alerts.send_alert("QUOTE-RESIDUE", "exit receipt %s (position %s) unreadable after %d tries: "
                              "its quote residue is UNKNOWN and will not be credited"
                              % (fill["sig"], fill["position_id"], m.QUOTE_RESIDUE_READ_MAX_TRIES),
                              source_id="quote-residue-unreadable:%s" % fill["sig"])
        if log is not None and (n or state == "unreadable" or (out and out["negative"])):
            log("[memecoin] quote residue exit %s pos=%s %s rows=%d%s" % (
                fill["sig"][:10], fill["position_id"], state, n,
                " NEGATIVE %s" % ",".join(x[:8] for x in out["negative"]) if out and out["negative"] else ""))
    return done


def _iso(block_time):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(block_time), timezone.utc).isoformat()


def _sell_receipt(sig, receipt_fn, owner):
    """A sold sell's receipt, cached once read (a confirmed receipt never changes); None unread."""
    tx = _SELL_RECEIPTS.get(sig)
    if tx is None:
        try:
            tx = receipt_fn(sig)
        except Exception:
            logger.exception("quote residue: sell receipt read failed sig=%s", sig)
            tx = None
    if not _bound_receipt(tx, sig, owner) or _chain_key(tx) is None or not _valid_token_balances(tx):
        _SELL_RECEIPTS.pop(sig, None)
        return None
    if len(_SELL_RECEIPTS) < 256:
        _SELL_RECEIPTS[sig] = tx
    return tx


def _chain_key(tx):
    """(slot, blockTime) of a landed transaction: its place on the chain timeline. None = unknown."""
    try:
        slot, block_time = tx["slot"], tx["blockTime"]
        if type(slot) is not int or slot <= 0 or type(block_time) is not int or block_time <= 0:
            return None
        _iso(block_time)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    return slot, block_time


def _previous_landing(conn, sell, tx, receipt_fn, owner):
    """The lower bound (ISO) of the residue `sell` consumed: when the sold sell of this mint just
    before it landed on chain, '' if none (review C06, 2026-10-08). A sell's local `resolved_at` is
    its RECOVERY time, so using it as the bound hid residue that arrived after a late-recovered sell
    landed. Order is slot first; one slot cannot order two sells ('sell_order_ambiguous'). An
    other sell whose receipt is unreadable is placed only when its local resolution (always at or
    after its landing) precedes this landing: it then bounds at that resolution, conservatively
    (residue in between stays uncredited, never misowned); otherwise 'sell_order_unknown'.
    A slot tie with different blockTimes only exists in synthetic receipts; blockTime decides it."""
    from sniper.persistence import db_memecoin as m
    mine = _chain_key(tx)
    if mine is None:
        return "sell_order_unknown"
    landed, bound = _iso(mine[1]), ""
    for sig, resolved_at in m.other_sold_holding_sells(conn, sell["mint"], intent_id=sell["intent_id"]):
        key = _chain_key(_sell_receipt(sig, receipt_fn, owner))
        if key is None:
            if not resolved_at or resolved_at >= landed:
                return "sell_order_unknown"
            bound = max(bound, resolved_at)
        elif key == mine:
            return "sell_order_ambiguous"
        elif key[0] < mine[0] or (key[0] == mine[0] and key[1] < mine[1]):
            bound = max(bound, _iso(key[1]))
    return bound


def credit_sells_once(conn, *, receipt_fn, owner, now_iso, log=None, force=False, apply=True):
    """Credit every sold holding sell that consumed recorded residue. The sell's own receipt gives
    its landing time and the exact balance it consumed. A sell waits (no credit) while any exit
    before it has unknown residue. `apply=False` plans without writing (the backfill dry run).
    Returns one dict per sell considered."""
    from sniper.persistence import db_memecoin as m
    if not (force or _credits_on()):
        return []
    since, out = _since(), []
    for sell in m.quote_residue_sell_candidates(conn, since_iso=since):
        rec = {"intent_id": sell["intent_id"], "mint": sell["mint"], "state": None}
        out.append(rec)
        if m.exit_reads_unsettled_before(conn, since_iso=since, before_iso=sell["resolved_at"]):
            rec["state"] = "waiting_for_exit_reads"            # landed <= resolved_at: no read needed
            continue
        tx = _sell_receipt(sell["sig"], receipt_fn, owner)
        meta = (tx or {}).get("meta")
        if not isinstance(meta, dict) or meta.get("err") is not None or not (tx or {}).get("blockTime"):
            rec["state"] = "sell_receipt_unreadable"
            continue
        consumed = -_owner_deltas(meta, owner).get(sell["mint"], (0, None, set()))[0]
        if consumed <= 0:
            rec["state"] = "sell_consumed_nothing"
            continue
        landed = _iso(tx["blockTime"])
        if m.exit_reads_unsettled_before(conn, since_iso=since, before_iso=landed):
            rec["state"] = "waiting_for_exit_reads"
            continue
        after = _previous_landing(conn, sell, tx, receipt_fn, owner)
        if after in ("sell_order_unknown", "sell_order_ambiguous"):
            rec["state"] = after
            continue
        # WHOLE-SECOND EDGES (Gamma, C06): blockTime is whole seconds and residue time is local and
        # fractional, so a residue in the previous sell's landing second or this sell's cannot be
        # placed on either side of it. It waits, named, rather than going to a guessed owner.
        edges = {after[:19], landed[:19]} - {""}
        near = m.quote_residue_owners(conn, sell["mint"], before_iso=_iso(int(tx["blockTime"]) + 1),
                                      intent_id=sell["intent_id"], after_iso=after[:19])
        if any(o["position_id"] is not None and str(o["source_at"])[:19] in edges for o in near):
            rec["state"] = "residue_order_ambiguous"
            continue
        owners = [o for o in m.quote_residue_owners(conn, sell["mint"], before_iso=landed,
                                                     intent_id=sell["intent_id"], after_iso=after)
                  if o["position_id"] is not None]
        if not owners:
            rec["state"] = "no_owner"
            continue
        plan = plan_sell_credits(owners, consumed_atomic=consumed, usd_received=sell["usd_received"])
        rec.update(state="planned", consumed=consumed, plan=plan)
        todo = [c for c in plan["credits"] if c["credit_id"] is None and c["priced"]]
        if apply and todo:
            rec["booked"] = m.apply_quote_residue_credits(
                conn, intent_id=sell["intent_id"], mint=sell["mint"], credits=todo,
                consumed_atomic=consumed, foreign_atomic=plan["foreign_atomic"],
                foreign_usd=plan["foreign_usd"], short=plan["short"], now_iso=now_iso)
            rec["state"] = "credited"
            if log is not None:
                log("[memecoin] quote residue credit intent=%s mint=%s consumed=%d usd=%.6f -> %s%s" % (
                    sell["intent_id"][:8], sell["mint"][:8], consumed, float(sell["usd_received"]),
                    ", ".join("pos %s +$%.4f" % (c["position_id"], c["usd"]) for c in todo),
                    " foreign $%.4f" % plan["foreign_usd"] if plan["foreign_atomic"] else ""))
    return out
