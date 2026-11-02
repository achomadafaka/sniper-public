"""Item 7a — Real EVM entry executor (S2-EVM, DISARMED build). Pure orchestration over injected seams
so every money-safety branch is unit-tested without a live endpoint. Mirrors the Solana executor with
two EVM-specific steps:

  1. quote (0x Swap v2, USDC -> token).
  2. record pre-balances from CHAIN.
  3. EXACT-AMOUNT approve() of the router's spender for exactly the sell amount if the quote reports an
     allowance issue — never an unlimited approval.
  4. sign the swap; write-ahead the tx hash AND nonce onto the reservation (set_pending_broadcast)
     BEFORE broadcasting — the nonce is the EVM no-double-submit key.
  5. send once; confirm by receipt.
  6. confirmed -> price the fill from the on-chain ERC-20 balance DELTA (never the quote), open a
     execution_mode='real' position tagged with the chain, record the fill with its tx hash, confirm
     the reservation. failed -> release. unknown/timeout -> LEAVE the reservation, open nothing, and
     NEVER resubmit (the tx may still land; the reused nonce would be rejected anyway).

NOT integration-tested. A live smoke test (item 11) MUST precede any arm.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import threading
import time

from sniper.execution.entry_attribution import (
    balance_parts, bind_quote_metadata, buy_drift, copy_minimum_refusal, positive_price, quote_age_ms, quote_fields,
    trace_mark, trace_mark_submission, trace_now,
)
from sniper.persistence import db_memecoin as m
from sniper.execution.submission_transport import sha256_hex


def receipt_status(receipt):
    """1 or 0 for a receipt's recognized status, else None (missing, malformed or unknown): a
    receipt without a recognized status is never a revert (Codex EVM-04)."""
    value = str((receipt or {}).get("status")).lower()
    return 1 if value in ("0x1", "1") else 0 if value in ("0x0", "0") else None


def _receipt_is_canonical(pending, receipt, transaction, block, owner, *, status=1):
    """The receipt, transaction and block name the pending's own signed swap, in one block, sent
    by `owner`, with `status` (1 by default) and every log from that transaction and block."""
    def word(value):
        return int(str(value), 0)
    tx_hash = pending["tx_sig"]
    return not (
        not tx_hash or not pending.get("signed_payload_sha256")
        or receipt_status(receipt) != status
        or any(str(item).lower() != str(tx_hash).lower() for item in (
            receipt["transactionHash"], transaction["hash"]))
        or any(str(item).lower() != str(block["hash"]).lower() for item in (
            receipt["blockHash"], transaction["blockHash"]))
        or word(receipt["blockNumber"]) != word(block["number"])
        or word(transaction["blockNumber"]) != word(block["number"])
        or str(receipt["from"]).lower() != str(owner).lower()
        or str(transaction["from"]).lower() != str(owner).lower()
        or not isinstance(receipt["logs"], list)
        or any(str(log["transactionHash"]).lower() != str(tx_hash).lower()
               or str(log["blockHash"]).lower() != str(block["hash"]).lower()
               for log in receipt["logs"]))


def _book_receipt_entry(conn, pending, *, network_id, opened_at, got, spent, label, network_fee_wei=None):
    """Position + entry fill + confirm_pending in ONE transaction, from receipt-grade amounts.
    None when the reservation is no longer this bound, reserved swap; raises if it was booked.
    `network_fee_wei`: the receipt's own fee (_receipt_fee_paid), written on the fill; None = unknown."""
    tx_hash, price = pending["tx_sig"], float(pending["evm_quote_asset_usd"])
    contract = m.validated_pending_funded_contract(conn, pending)
    their_price = positive_price(pending.get("source_price"))
    fill_price = spent / got
    drift = buy_drift(their_price, None, fill_price)
    if conn.in_transaction:
        raise RuntimeError("%s entry recovery needs a clean transaction" % label)
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = m.pending_row(conn, pending["id"])
        if (not current or current["status"] != "reserved" or current["tx_sig"] != tx_hash
                or current["evm_quote_asset_usd"] != price):
            conn.rollback()
            return None
        # By attempt (indexed); a scan by tx_sig costs about 18 ms inside this write transaction
        # and remains only for a reservation without an attempt id.
        if (m.entry_fill_for_attempt(conn, pending.get("attempt_id"), tx_hash)
                if pending.get("attempt_id") else
                conn.execute("SELECT 1 FROM memecoin_fills WHERE kind='entry' AND tx_sig=?",
                             (tx_hash,)).fetchone()):
            raise RuntimeError("%s entry already has a fill" % label)
        position_id = m.open_position(
            conn, source_user_id=pending.get("source_user_id"), source_handle="(real)",
            mint=pending["mint"], ticker="", entry_price=fill_price, entry_notional=spent,
            entry_fill_source="chain", entry_lag_s=m.resolve_source_lag_s(
                conn, pending.get("origin_event_id")),
            their_size_usd=m.resolve_source_size_usd(
                conn, pending.get("origin_event_id"), network_id=network_id,
                mint=pending["mint"], source_user_id=pending.get("source_user_id")),
            entry_liquidity=pending.get("source_liquidity"),
            entry_token_age_s=pending.get("source_token_age_s"),
            entry_mcap=positive_price(pending.get("source_market_cap")),
            opened_at=opened_at, strategy=pending.get("strategy") or "alpha_b",
            execution_mode="real", network_id=network_id,
            signal_source=pending.get("signal_source") or "global_feed",
            origin_event_id=pending.get("origin_event_id"), their_price=their_price,
            fill_gap_pct=drift["total_copy_drift_pct"],
            attempt_id=pending.get("attempt_id"),
            price_attribution="exact_event" if their_price else "unavailable",
            cluster_decision_id=pending.get("cluster_decision_id"), **contract, **drift,
            commit=False)
        m.insert_fill(
            conn, position_id=position_id, kind="entry", price=fill_price, qty=got,
            notional=spent, fill_source="chain", fraction=1.0, created_at=opened_at,
            tx_sig=tx_hash, attempt_id=pending.get("attempt_id"),
            origin_event_id=pending.get("origin_event_id"), network_id=network_id,
            signal_source=pending.get("signal_source") or "global_feed",
            source_price=their_price,
            price_attribution="exact_event" if their_price else "unavailable",
            network_fee_wei=network_fee_wei, **drift, commit=False)
        if not m.confirm_pending(conn, pending["id"], commit=False):
            raise RuntimeError("%s entry recovery lost reservation" % label)
        conn.commit()
        try:
            from sniper.services.memecoin_hot_supervisor import request_rebuild
            request_rebuild()
        except Exception:
            pass
        return {"status": "filled", "position_id": position_id}
    except Exception:
        conn.rollback()
        raise


def _opened_at(block):
    return datetime.fromtimestamp(int(str(block["timestamp"]), 0), tz=timezone.utc).isoformat()


def recover_confirmed_ink_entry(conn, pending, receipt, transaction, block, *, owner,
                                weth, decimals_fn):
    """Book an Ink or Ethereum buy from its canonical transaction and exact receipt flows."""
    try:
        price = float(pending["evm_quote_asset_usd"])
        if (str(pending.get("network_id")) not in {"1", "57073"} or not math.isfinite(price) or price <= 0
                or not _receipt_is_canonical(pending, receipt, transaction, block, owner)):
            return None
        opened_at = _opened_at(block)
        from sniper.services.memecoin_evm_onchain_watch import wallet_paid_token_flows
        acquired, paid_atomic = wallet_paid_token_flows(receipt, owner, weth, transaction)
        if str(pending["network_id"]) == "1":
            from sniper.execution.evm_v4_quote import V4
            from sniper.settings import memecoin as cfg
            from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC
            routers = {V4["1"]["universal_router"].lower(), cfg.MEMECOIN_EVM_V3_ROUTER["1"].lower(),
                       cfg.MEMECOIN_EVM_V2_ROUTER["1"].lower()}
            if str(transaction["to"]).lower() not in routers:
                return None
            # Ethereum routes through a router while a pool sends the coin. Counterparty
            # equality cannot link those legs; the bound canonical swap and net owner flows can.
            net = {}
            for log in receipt["logs"]:
                topics = [str(t).lower() for t in log.get("topics") or ()]
                if len(topics) != 3 or topics[0] != TRANSFER_TOPIC:
                    continue
                sender, recipient = "0x" + topics[1][-40:], "0x" + topics[2][-40:]
                token = str(log["address"]).lower()
                amount = int(str(log["data"]), 0)
                delta = (amount if recipient == owner.lower() else 0) - (
                    amount if sender == owner.lower() else 0)
                net[token] = net.get(token, 0) + delta
            acquired = {t: n for t, n in net.items() if t != weth.lower() and n > 0}
            paid_atomic = max(0, int(str(transaction["value"]), 0)) + max(0, -net.get(weth.lower(), 0))
        mint = str(pending["mint"]).lower()
        if len(acquired) != 1 or mint not in acquired or paid_atomic <= 0:
            return None
        decimals = int(decimals_fn(mint))
        if not 0 <= decimals <= 36:
            return None
        got = acquired[mint] / 10 ** decimals
        spent = paid_atomic / 10 ** 18 * price
        if (not math.isfinite(got) or got <= 0 or not math.isfinite(spent)
                or spent <= 0 or spent > float(pending["reserved_notional_usd"]) + 0.01):
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return _book_receipt_entry(conn, pending, network_id=str(pending["network_id"]),
                               opened_at=opened_at, got=got, spent=spent, label="EVM",
                               network_fee_wei=_receipt_fee_paid(receipt)[0])


def recover_confirmed_receipt_entry(conn, pending, receipt, transaction, block, *, owner,
                                    quote_token, quote_decimals, decimals_fn):
    """Book an ordinary EVM buy that confirmed but was not booked in its own call (a lagging receipt,
    a restart) from its canonical receipt: got is the owner's NET coin, paid the transaction's native
    value, else the owner's NET quote-token outflow, at the write-ahead's quote USD. Never a wallet
    balance delta (Alpha 2026-10-06, after Codex EVM-03). None when anything is unproven."""
    from sniper.execution.evm_real_seams import receipt_token_net
    try:
        price = float(pending["evm_quote_asset_usd"])
        if (not math.isfinite(price) or price <= 0
                or not _receipt_is_canonical(pending, receipt, transaction, block, owner)):
            return None
        mint = str(pending["mint"]).lower()
        got_atomic = receipt_token_net(receipt, mint, owner)
        value = int(str(transaction.get("value") or "0x0"), 0)
        paid = value if value > 0 else -receipt_token_net(receipt, quote_token, owner)
        decimals = int(decimals_fn(mint))
        if got_atomic <= 0 or paid <= 0 or not 0 <= decimals <= 36:
            return None
        got = got_atomic / 10 ** decimals
        spent = paid / 10 ** int(quote_decimals) * price
        if (not math.isfinite(got) or got <= 0 or not math.isfinite(spent)
                or spent <= 0 or spent > float(pending["reserved_notional_usd"]) + 0.01):
            return None
        opened_at = _opened_at(block)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return _book_receipt_entry(conn, pending, network_id=str(pending["network_id"]),
                               opened_at=opened_at, got=got, spent=spent, label="EVM receipt",
                               network_fee_wei=_receipt_fee_paid(receipt)[0])


def lane_receipt_amounts(receipt, transaction, owner, mint, *, pool_manager):
    """(coin atomic the owner netted, native wei paid) of a fast-lane buy from its own receipt, or
    None. The lane's buy pays native as the transaction's value and the coin comes straight from
    `pool_manager` to us (4663: V4 TAKEs from the PoolManager; 56: the coin's V2 pair swaps to
    us), so exactly one token may arrive, it must be `mint`, and every unit of it must come from
    that source (spec 2026-10-01 fast lane §2.7). Coin the owner sends on in the SAME transaction
    (a fee debited after the credit, a burn) is subtracted: the booking is the wallet's net, never
    its gross inflow (Codex EVM-02). Any other token leaving the owner refuses: the native value
    would not be the whole cost. A self transfer moves nothing.

    `pool_manager=None` is the UNBOUND reading (Codex EVM-03), for a canonical buy the strict rule
    refused: the owner's net coin in THIS transaction's own receipt whatever sent it (a router
    forwarding the pool's output), with another token arriving ignored (it came free, so the whole
    value stays the coin's cost). Still transaction-attributed, never a block's balance delta."""
    from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC
    owner, mint = str(owner).lower()[2:], str(mint).lower()
    source = None if pool_manager is None else str(pool_manager).lower()[2:]
    paid = int(str(transaction.get("value") or "0x0"), 0)
    got, sent, other = 0, 0, False
    for log in receipt.get("logs") or []:
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if len(topics) != 3 or topics[0] != TRANSFER_TOPIC:
            continue
        sender, recipient = topics[1][-40:], topics[2][-40:]
        if owner not in (sender, recipient) or sender == recipient:
            continue
        token = str(log.get("address") or "").lower()
        amount = int(str(log.get("data") or "0x0"), 16)
        if recipient == owner and token != mint:
            other = other or source is not None
        elif token != mint or (recipient == owner and source is not None and sender != source):
            other = True
        elif recipient == owner:
            got += amount
        else:
            sent += amount
    return None if other or got - sent <= 0 or paid <= 0 else (got - sent, paid)


def recover_confirmed_lane_entry(conn, pending, receipt, transaction, block, *, owner, decimals_fn,
                                 network_id="4663", source=None, bound=True):
    """Book one fast-lane buy from its canonical receipt, never a balance delta: paid is the
    transaction's native value at the write-ahead's native price, got the coin `source` sent us
    (the chain's V4 PoolManager by default, 4663 or Base 8453; the BSC lane's coin pair on 56), net
    of what we sent on.
    `bound=False` books the receipt's unbound net instead (lane_receipt_amounts). Same
    one-transaction booking and double-booking guard as the Ink recovery."""
    from sniper.execution.evm_v4_quote import V4
    try:
        price = float(pending["evm_quote_asset_usd"])
        if (str(pending.get("network_id")) != str(network_id) or not math.isfinite(price)
                or price <= 0 or bound and not source and str(network_id) not in ("4663", "8453")
                or not _receipt_is_canonical(pending, receipt, transaction, block, owner)):
            return None
        amounts = lane_receipt_amounts(
            receipt, transaction, owner, pending["mint"],
            pool_manager=(source or V4[str(network_id)]["pool_manager"]) if bound else None)
        if amounts is None:
            return None
        decimals = int(decimals_fn(str(pending["mint"]).lower()))
        if not 0 <= decimals <= 36:
            return None
        got = amounts[0] / 10 ** decimals
        spent = amounts[1] / 10 ** 18 * price
        if (not math.isfinite(got) or got <= 0 or not math.isfinite(spent)
                or spent <= 0 or spent > float(pending["reserved_notional_usd"]) + 0.01):
            return None
        opened_at = _opened_at(block)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return _book_receipt_entry(conn, pending, network_id=str(network_id), opened_at=opened_at,
                               got=got, spent=spent, label="%s fast-lane" % network_id,
                               network_fee_wei=_receipt_fee_paid(receipt)[0])


def _receipt_fee_paid(receipt):
    """(wei, basis) a confirmed transaction paid in native fees, from its own receipt:
    gasUsed * effectiveGasPrice plus the OP-stack l1Fee when the receipt carries one. (None,
    'unknown') when the gas fields are missing or malformed: no guess (EVM-01, X402ID)."""
    if not isinstance(receipt, dict):
        return None, "unknown"
    try:
        paid = int(str(receipt["gasUsed"]), 16) * int(str(receipt["effectiveGasPrice"]), 16)
        l1 = receipt.get("l1Fee")
        if l1 is None:
            return paid, "receipt_no_l1fee"
        return paid + int(str(l1), 16), "receipt"
    except (KeyError, TypeError, ValueError):
        return None, "unknown"


def _trace_substage(trace, name, **metadata):
    """Best-effort trace detail; instrumentation can never change execution."""
    if trace is None:
        return
    try:
        marker = getattr(trace, "mark_substage", None)
        if marker is not None:
            marker(name, **metadata)
    except Exception:
        pass


def _v4_ranking_fields(quote):
    """E10 phase 1 (2026-09-29): the V4 discovery ranking the won quote carries, and the pool it
    was won on. Present only when the quote carries a ranking, so every other trace is unchanged."""
    ranking = quote.get("_sniper_v4_ranking") if isinstance(quote, dict) else None
    if ranking is None:
        return {}
    route = quote.get("route") if isinstance(quote.get("route"), dict) else {}
    return {"v4_ranking": ranking,
            "won_pool": route.get("pool_id") if route.get("protocol") == "uniswap_v4" else None}


_RECEIPT_VS_DELTA_SLOT = threading.BoundedSemaphore(1)
RECEIPT_VS_DELTA_DROPPED = [0]


def receipt_vs_delta_later(token_fn, mint, entry_tok_pre, got, position_id, tx_hash):
    """DIAGNOSTIC ONLY, after the booking and OFF the wallet lock (Alpha amendment 2026-10-06; Gamma
    LOW: a read inside execute_entry_evm held the per-wallet lock 50-135 ms longer). One daemon reads
    the token balance and logs the old balance window beside the receipt's figure, so how often the
    window was wrong is measured. Never an input. One read outstanding at a time: one that cannot
    start, or fails, is dropped and counted (RECEIPT_VS_DELTA_DROPPED). WARNING, because the runner
    configures no logging and Python's last-resort handler drops INFO (Gamma on 0ea5eb364)."""
    import logging
    log = logging.getLogger(__name__)
    if entry_tok_pre is None or not _RECEIPT_VS_DELTA_SLOT.acquire(blocking=False):
        RECEIPT_VS_DELTA_DROPPED[0] += 1
        log.warning("evm.receipt_vs_delta dropped pos=%s tx=%s dropped_total=%d", position_id,
                 tx_hash, RECEIPT_VS_DELTA_DROPPED[0])
        return None

    def run():
        try:
            tok_post, _ = balance_parts(token_fn(mint))
            delta = tok_post - entry_tok_pre
            log.warning("evm.receipt_vs_delta pos=%s tx=%s receipt_got=%r delta_got=%r agree=%s",
                     position_id, tx_hash, got, delta,
                     abs(delta - got) <= max(abs(got) * 1e-9, 1e-12))
        except Exception:
            RECEIPT_VS_DELTA_DROPPED[0] += 1
            log.warning("evm.receipt_vs_delta dropped pos=%s tx=%s dropped_total=%d", position_id,
                     tx_hash, RECEIPT_VS_DELTA_DROPPED[0])
        finally:
            _RECEIPT_VS_DELTA_SLOT.release()
    worker = threading.Thread(target=run, name="evm-receipt-vs-delta", daemon=True)
    worker.start()
    return worker


def _signed_value_usd(tx, sell_decimals, sell_token_usd):
    """The native value of the swap we signed, in USD, or None when it pays no native."""
    try:
        value = tx.get("value") or 0
        value = int(value, 16) if isinstance(value, str) else int(value)
        usd = value / (10 ** int(sell_decimals)) * float(sell_token_usd or 0.0)
    except (TypeError, ValueError, AttributeError):
        return None
    return usd if usd > 0 and math.isfinite(usd) else None


def _read_balances(usdc_fn, token_fn, mint):
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        usdc_future = executor.submit(usdc_fn)
        token_future = executor.submit(token_fn, mint)
        return usdc_future.result(), token_future.result()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _quote_raw(usdc_fn):
    """The raw quote-token word behind the last `usdc_fn()` read, or None when unknown.

    None (no reader, a failed read, anything but an int) means UNKNOWN, never a zero balance: the
    unwrap guard below only refuses on a balance it actually read."""
    reader = getattr(usdc_fn, "last_raw", None)
    if not callable(reader):
        return None
    try:
        raw = reader()
    except Exception:
        return None
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _preflight_max_quote_age_ms():
    """Defined in settings, not a getattr fallback -- an undefined name would be inert in .env."""
    from sniper.settings import memecoin as cfg
    try:
        return float(cfg.MEMECOIN_EVM_PREFLIGHT_MAX_QUOTE_AGE_MS)
    except Exception:
        return 750.0


def _requote_matches(fresh, sell_atomic, approved_spender):
    """Post-approval re-quote acceptance: the fresh quote must sell EXACTLY the amount that was
    just exact-approved and route through the SAME spender (a venue-rung flip mid-entry changes
    the router → the approval would not cover it). Anything else is a fail-closed abort."""
    if not fresh:
        return False
    try:
        if int(fresh.get("sellAmount") or 0) != int(sell_atomic):
            return False
    except (TypeError, ValueError):
        return False
    allow = (fresh.get("issues") or {}).get("allowance") or {}
    spender = allow.get("spender") or (fresh.get("transaction") or {}).get("to")
    return bool(spender) and str(spender).lower() == str(approved_spender).lower()


def _effective_chase_cap(base_value, wallet_value, expires_at, quote_observed_at):
    """Use a scoped wallet cap only on an exact quote inside its source-event window."""
    try:
        base = float(base_value)
    except (TypeError, ValueError):
        base = 0.0
    if not math.isfinite(base) or base <= 0:
        return 0.0
    try:
        wallet = float(wallet_value)
    except (TypeError, ValueError):
        return base
    if not math.isfinite(wallet) or wallet <= base:
        return base
    try:
        deadline = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(str(quote_observed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return base
    if (deadline.tzinfo is None or deadline.utcoffset() is None
            or observed.tzinfo is None or observed.utcoffset() is None):
        return base
    return wallet if observed <= deadline else base


def _quote_over_source_refusal(network_id, quote_price, source_price):
    """A refusal reason when our quote is at or above the chain's quote-over-source cap, else None.
    See settings.quote_over_source_max_pct_for. Missing prices pass (the chase and premium gates
    keep their own fail-closed rules)."""
    from sniper.settings.memecoin import quote_over_source_max_pct_for
    cap = quote_over_source_max_pct_for(network_id)
    quote, source = positive_price(quote_price), positive_price(source_price)
    if cap is None or quote is None or source is None:
        return None
    pct = (quote / source - 1.0) * 100.0
    if pct >= cap:
        return "our quote is %+.0f%% above source price (cap %.0f%%)" % (pct, cap)
    return None


def _chase_gap(premium, source_price, partial_drift):
    """How far the COIN has moved since the copied trader filled. Returns (pct, basis, display).

    THE CHASE GATE MEASURED THE WRONG QUANTITY UNTIL 2026-09-02. `source_drift_pct` is
    quote/source, and our quote carries our own route markup, so the gate reported the coin's
    movement PLUS our markup -- and the markup dominates by 3-10x. All eight 4663 chase refusals
    of 2026-09-02:

        coin      coin moved (display/source)   our markup (quote/display)   what the gate saw
        BTCB2            -15.9%                        +107.4%                    +74.4%
        gubby            -37.0%                        +138.4%                    +50.2%
        FOX              -13.6%                         +70.0%                    +46.9%
        r/ape            -52.7%                        +168.3%                    +27.0%

    THE COIN FELL IN SEVEN OF EIGHT. Every one was refused for chasing a pump that never happened.
    FOX is the clearest: refused at +47%, and three minutes later a second FOX signal died with
    `entry_simulation_reverted` -- the route was not executable at all, which is what the +70%
    markup was really reporting.

    NOT A LOOSENING. quote/display keeps being checked by the gate that exists for it:
    `_execution_premium` decides on the same quote a few lines earlier. Until now BOTH gates
    measured the route markup and NEITHER measured the coin. `_execution_premium`'s own docstring
    already recorded which half predicts the loss -- the premium does, over 221 filled closes; the
    chase (display/source) does not.

    THE FALLBACK IS DELIBERATELY THE OLD BEHAVIOUR. `display_price` is absent on ~43% of 4663
    attempts (young coins DexScreener has not indexed) and the premium gate abstains on exactly
    those rows. Failing open there would remove BOTH gates at once from nearly half the book.
    Where we cannot see the market we keep the contaminated measure rather than nothing, and the
    basis is returned so a refusal can name which number decided it.
    """
    display = positive_price(getattr(premium, "display_price", None))
    source = positive_price(source_price)
    if display is not None and source is not None:
        return (display / source - 1.0) * 100.0, "market", display
    return (partial_drift or {}).get("source_drift_pct"), "quote_fallback", display


# HOOKERS (2026-09-30, attempt 92da4a9a): an aggregator's quote carries no block. Kyber priced the
# pool about 20 blocks stale while the V4 rung, which reads the pool at the latest block, was cut by
# the ladder; the stale quote alone refused an entry the pool would have filled inside the cap.
_AGGREGATOR_RUNGS = frozenset({"kyber", "relay"})
# The source-route rungs (evm_source_route, 2026-10-15): the pool the source itself used.
from sniper.execution.evm_source_route import SOURCE_FAMILY as _SOURCE_FAMILY  # noqa: E402
from sniper.execution.evm_source_route import SOURCE_RUNGS as _SOURCE_RUNG_NAMES  # noqa: E402
_SOURCE_RUNGS = frozenset(_SOURCE_RUNG_NAMES)
_ONCHAIN_RUNGS = frozenset({"v4", "v4_multihop", "v3", "v3_bridge", "v2", "mixed_v3_v2", "pons_v2",
                            "launchpad_curve", "flap_curve", "pancake_infinity"}) | _SOURCE_RUNGS
_LADDER_CUTS = frozenset({"ladder_grace", "ladder_deadline"})
_STALE_RECHECK = object()               # _price_gates: "ask the ladder once more", nothing released


def stale_aggregator_cut_rungs(quote):
    """The on-chain rungs the ladder cut before they answered, when an aggregator won; else ().
    Pure: reads the winner name and the rung records the ladder already attached to the quote."""
    if not isinstance(quote, dict) or quote.get("_sniper_winner") not in _AGGREGATOR_RUNGS:
        return ()
    return tuple(r.get("rung") for r in (quote.get("_sniper_rungs") or ())
                 if isinstance(r, dict) and r.get("rung") in _ONCHAIN_RUNGS
                 and r.get("blind_reason") in _LADDER_CUTS)


def source_route_won(quote):
    """True when a source-route rung won the ladder (Alpha 2026-10-15): with 4663's grace at 0 the
    first hit wins, so a price refusal of the source pool must re-ask today's rungs, never refuse
    while a cheaper route was cut."""
    return isinstance(quote, dict) and quote.get("_sniper_winner") in _SOURCE_RUNGS


def _execution_premium(contract_fields, exact_price, trace, *, attempt_no, revision,
                       source_price=None, tag=None, post_trade_price=None, network_id=None):
    """Our EVM route price against the chart and source-price floor. See `execution_premium_decision`
    for why the premium (quote/display) is the half that predicts the loss and the chase
    (display/source) is not: realized return over 221 filled closes is +1.07% below 1.15x and
    -52.14% at 22% wins above 2.0x (n=18).

    THE CALL SITE DID NOT EXIST ON THE EVM LEG UNTIL 2026-08-30. `execution_premium_decision` was
    reachable only from `solana_swap_executor`, so every funded EVM entry paid whatever the route
    asked. It was invisible rather than permissive-by-choice: while 4663 had no FOMO_TO_DEX entry
    the classification carried `display_price=None`, so even a wired gate could not have decided.
    40b2f7a9 restored the input; this restores the call.

    UNITS. `quote_fields(quote)["quote_price"]` is `quote_unit_price`, which is USD per bought
    token and already multiplies by `_sniper_sell_token_usd` -- so on 4663 the WETH sell leg is
    converted before it gets here. `display_price` is USD per token from DexScreener. Both sides
    are USD and no conversion belongs at this call site. Passing a raw `buyAmount` here would read
    as a ~2,478x premium and refuse everything, which is the 2026-07-30 WETH-vs-USD defect that
    produced $3.47M of fictional PnL -- loud rather than silent, but wrong.

    A missing display price has no premium verdict. The executor then applies the remaining exact
    quote, route, impact, and chase gates. An unpriceable route still fails closed.

    `cap_off` still allows -- a cap of 0 is the operator turning the gate off, not missing data.

    E11 ON-CHAIN: with `E11:<network_id>` in MEMECOIN_DEXSCREENER_OFF the reference is the source's
    `post_trade_price` (see execution_premium_decision). On EVERY attempt the trace also carries the
    shadow verdicts the 2026-10-15 refit reads (quote/post-trade at 1.2x and 1.6x, quote/display at
    the live cap while the display price is in hand). Pure: no I/O.
    """
    if not contract_fields:
        return None
    from sniper.services.memecoin_market_state import execution_premium_decision
    from sniper.settings import memecoin as cfg
    contract = json.loads(contract_fields["funded_contract_json"])
    market_only = getattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)
    onchain = network_id is not None and cfg.dexscreener_off("E11", network_id)
    result = execution_premium_decision(
        contract.get("classification"), exact_quote_price=exact_price,
        source_price=None if market_only else source_price,
        post_trade_price=None if market_only else post_trade_price, onchain=onchain)
    _trace_substage(
        trace, "execution_premium", attempt_no=attempt_no, revision=revision,
        allow=result.allow, ratio=result.ratio, display_price=result.display_price,
        source_price=source_price, reference_price=result.reference_price,
        reference_basis=result.reference_basis, exact_price=result.exact_price,
        cap=result.cap, reason=result.reason, onchain=onchain,
        **_premium_shadows(contract.get("classification"), exact_price, post_trade_price,
                           source_price=source_price),
        **(tag or {}))
    return result


SHADOW_POST_TRADE_ALT_CAP = 1.6     # the figure first relayed; shadowed for the 2026-10-15 refit


def _premium_shadows(classification, exact_price, post_trade_price, *, source_price=None):
    """Frozen 1.2/1.6 measurements and separately named live-cap verdicts. Pure, no I/O."""
    from sniper.services.memecoin_market_state import execution_premium_decision
    out = {"shadow_post_trade_ratio": None, "shadow_post_trade_allow_1_2": None,
           "shadow_post_trade_allow_1_6": None, "shadow_display_ratio": None,
           "shadow_display_allow": None, "shadow_display_cap": None,
           "shadow_display_reason": None, "shadow_live_post_trade_allow": None,
           "shadow_live_post_trade_cap": None, "shadow_live_post_trade_reason": None}

    def positive(value):
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (TypeError, ValueError, OverflowError):
            return None

    exact = positive(exact_price)
    if exact is None:
        return out
    post = positive(post_trade_price)
    if post is not None:
        ratio = exact / post
        if math.isfinite(ratio):
            live = execution_premium_decision(classification, exact_quote_price=exact,
                                             source_price=source_price, post_trade_price=post,
                                             onchain=True)
            out.update(shadow_post_trade_ratio=ratio, shadow_post_trade_allow_1_2=ratio <= 1.2,
                       shadow_post_trade_allow_1_6=ratio <= SHADOW_POST_TRADE_ALT_CAP,
                       shadow_live_post_trade_allow=live.allow, shadow_live_post_trade_cap=live.cap,
                       shadow_live_post_trade_reason=live.reason)
    value = classification if isinstance(classification, dict) else {}
    facts = value.get("facts") if isinstance(value.get("facts"), dict) else {}
    display = positive(facts.get("display_price"))
    if display is not None:
        ratio = exact / display
        if math.isfinite(ratio):
            # The display-only counterfactual uses the shared helper's cap-off/unit semantics.
            live = execution_premium_decision(classification, exact_quote_price=exact)
            out.update(shadow_display_ratio=ratio, shadow_display_allow=live.allow,
                       shadow_display_cap=live.cap, shadow_display_reason=live.reason)
    return out


def _premium_refusal(premium):
    """The refusal this premium result implies on an EVM leg, or None to proceed.

    Returns (reason_string, release_tag). The ratio form matches the Solana string verbatim so
    one operator alert and one `memecoin_refusal_capture` pattern cover both chains.
    """
    if premium is None or premium.reason == "cap_off":
        return None
    if premium.reason == "quote_unit_fault":
        # A UNIT FAULT CARRIES A RATIO, so it must be caught BEFORE the ratio branch below or it
        # prints as a route premium -- which is exactly how VEX was reported as "395149.46x the
        # source price" when the real fault was an ETH/USD value in a token-price column. Still
        # refuses: an unusable quote must never fund a transaction. Only the name changes, so the
        # operator alert and the measurements stop counting a broken quote as an expensive route.
        return ("quote price failed its unit sanity check (%.0fx the %s) — refusing, the quote "
                "is unusable rather than expensive"
                % (premium.ratio,
                   "source price" if premium.reference_basis == "source" else "chart mid"),
                "quote_unit_fault")
    if premium.ratio is not None:
        if premium.allow:
            return None
        basis = {"source": "source price", "post_trade": "post-trade price"}.get(
            premium.reference_basis, "chart mid")
        return ("route quote is %.2fx the %s (cap %.2fx)"
                % (premium.ratio, basis, premium.cap), "execution_premium")
    if premium.reason == "no_display_price":
        return None
    # The route price is unavailable. Do not send a funded EVM transaction without it.
    return ("execution premium undecidable on a funded EVM chain (%s)" % premium.reason,
            "execution_premium_blind")


def _dip_route(quote):
    """The route that priced a premium refusal, as plain data for the dip watch, or None.

    `{rung, sell_atomic, buy_atomic, pool_id, legs_path}`: the winning rung, the exact input and
    output of this quote, a V4 single hop's pool and a V4 multihop's pools. The watch re-quotes
    that exact pool or path at that input and reads the premium off the output ratio, with no
    pool discovery. No callable: the result dict is stored and serialized.
    """
    try:
        route = quote.get("route") or {}
        # THE PIN (2026-10-08, design 92f34c9b7): what a display-feed chain's watch needs to re-quote
        # this exact v2 path, v3 path or mixed hop with no pair discovery. Plain route keys only.
        pin = {k: route[k] for k in ("router", "path", "tokens", "fees", "base_token", "v3_tokens",
                                     "v3_fees") if route.get(k) is not None}
        fot = quote.get("_fot_exit")
        if pin and isinstance(fot, dict) and fot.get("cut_bps") is not None:
            pin["cut_bps"] = int(fot["cut_bps"])
        # A source-route winner is re-quoted by its family's pin (same pool or path, same builder).
        winner = quote.get("_sniper_winner")
        return json.loads(json.dumps({
            "rung": _SOURCE_FAMILY.get(winner, winner),
            "sell_atomic": str(int(quote["sellAmount"])),
            "buy_atomic": str(int(quote["buyAmount"])),
            "pool_id": route.get("pool_id"),
            "legs_path": route.get("legs_path"),
            # The signed minimum: k = buy/min maps the copy floor onto the watch's quote basis.
            **({"min_atomic": str(int(quote["minBuyAmount"]))}
               if quote.get("minBuyAmount") is not None else {}),
            **({"pin": pin} if pin else {}),
        }))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _post_peak_confirmation(contract_fields, exact_price, trace, *, attempt_no, revision):
    if not contract_fields:
        return None
    from sniper.services.memecoin_market_state import confirm_post_peak
    contract = json.loads(contract_fields["funded_contract_json"])
    result = confirm_post_peak(
        contract.get("classification"), exact_quote_price=exact_price)
    marker = getattr(trace, "mark_substage", None) if trace is not None else None
    if marker is not None:
        marker(
            "post_peak_confirmation",
            attempt_no=attempt_no,
            revision=revision,
            nominated=result.nominated,
            confirmed=result.confirmed,
            reason=result.reason,
            peak_price=result.peak_price,
            display_price=result.display_price,
            exact_price=result.exact_price,
            exact_giveback_pct=result.exact_giveback_pct,
        )
    return result


_OVER_BUDGET_CODES = ("route_over_budget", "route_over_budget_marginal")


def _round_trip_budget(chain):
    from sniper.execution.evm_real_seams import _entry_tp_pct
    from sniper.execution.evm_v4_multihop import round_trip_budget_pct
    return round_trip_budget_pct(_entry_tp_pct(chain), chain=chain)


def _declined_round_trip_pct(miss):
    """The refusing rung's measured round-trip cost (percent, positive), or None."""
    for rung in miss.get("rungs") or ():
        reason = str((rung or {}).get("reason") or "") if isinstance(rung, dict) else ""
        if reason.startswith("round_trip_over_budget"):
            try:
                return abs(float(reason.rsplit(":", 1)[1]))
            except (IndexError, ValueError):
                return None
    return None


def _evm_roundtrip_resize(call_quote, mint, usd_amount, miss, chain, trace):
    """SHRINK INSTEAD OF REFUSE on EVM (operator 2026-10-14; Solana's rule since 2026-09-28).
    (quote, new_usd) from ONE re-quote at a smaller clip, or None to let the refusal stand.
    Only the part of the round trip our size causes can be bought down: a $PROBE_USD probe prices
    the fixed part (fees, transfer tax); at or over TARGET x budget it is a trap at any size. The
    budget is the gate's own number, never widened; the re-quote's own ladder judges it again."""
    from sniper.settings import memecoin as cfg
    if (not getattr(cfg, "MEMECOIN_EVM_ENTRY_ROUNDTRIP_RESIZE", False)
            or miss.get("code") not in _OVER_BUDGET_CODES):
        return None
    cost, cap = _declined_round_trip_pct(miss), _round_trip_budget(chain)
    note = {"cost_pct": cost, "cap_pct": cap, "clip_usd": float(usd_amount)}

    def done(outcome, **extra):
        _trace_substage(trace, "evm.roundtrip_resize", outcome=outcome, **note, **extra)

    if cost is None or not cap:
        return done("unreadable")
    target = cap * float(cfg.MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_TARGET_FRACTION)
    note["target_pct"] = target
    try:
        probe = call_quote(float(cfg.MEMECOIN_ENTRY_ROUNDTRIP_PROBE_USD))
    except Exception as exc:
        return done("probe_failed", error=type(exc).__name__)
    pct = ((probe.get("route") or {}).get("round_trip_pct") if isinstance(probe, dict) else None)
    try:
        fixed = abs(float(pct))
    except (TypeError, ValueError):
        return done("probe_unpriced")          # the probe itself did not price inside the budget
    note["probe_pct"] = fixed
    if fixed >= target or cost <= fixed:
        return done("fixed_cost")
    # ponytail: linear size cost (constant-product pool or curve, small impact); the re-quote's
    # own ladder measures the real round trip at the new clip and refuses again if it is wrong.
    new_usd = round(float(usd_amount) * (target - fixed) / (cost - fixed), 2)
    floor = max(float(usd_amount) * float(cfg.MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_MIN_FRACTION),
                float(cfg.MEMECOIN_MIN_TRADE_USD))
    note["resized_usd"] = new_usd
    if new_usd < floor or new_usd >= float(usd_amount):
        return done("below_floor", floor_usd=floor)
    try:
        quote = call_quote(new_usd)
    except Exception as exc:
        return done("requote_failed", error=type(exc).__name__)
    if not quote:
        return done("requote_declined")
    done("resized")
    import logging
    # WARNING, not info: a changed clip is a money event (runner root logger is WARNING).
    logging.getLogger(__name__).warning(
        "evm roundtrip resize chain=%s mint=%s clip_usd=%.2f resized_usd=%.2f cost_pct=%.2f "
        "probe_pct=%.2f cap_pct=%.2f", chain, mint, float(usd_amount), new_usd, cost, fixed, cap)
    return quote, new_usd


def _quote_miss(quote_miss_fn):
    """Attribute an empty quote. `{}` when the caller supplies no attribution seam.

    A bare "no quote" cannot separate "this coin has no executable route" from "the route ladder
    could not read" (4663 V4 pool discovery under a GeckoTerminal 429, 2026-08-07). Both released
    the reservation with the same string, so the refusals were unauditable after the fact."""
    if quote_miss_fn is None:
        return {}
    try:
        miss = quote_miss_fn() or {}
    except Exception:
        return {}
    return miss if isinstance(miss, dict) and miss.get("code") else {}


def execute_entry_evm(conn, *, pending_id, mint, network_id, usd_amount, strategy, source_user_id,
                      now_iso, quote_fn, approve_fn, sign_fn, send_fn, confirm_fn, usdc_fn, token_fn,
                      sleep_fn=None, pending=None, trace=None, sell_decimals=6,
                      sell_token_usd=1.0, revert_reason_fn=None, safety_gate_fn=None,
                      quote_miss_fn=None, receipt_token_fn=None, receipt_spent_fn=None,
                      lifi_entry_fn=None,
                      retry_slippage_bps=None, cleanup_native_fn=None, chase_max_pct=None,
                      native_pre_pipeline_fn=None, wallet_chase_cap_pct=None,
                      wallet_chase_cap_expires_at=None, entry_preflight_fn=None, quote_safety_fn=None,
                      presign_check_fn=None, gas_guard_fn=None,
                      require_receipt_amounts=False,
                      trace_attempt_base=1, receipt_fn=None, native_float_fn=None,
                      release_nonce_fn=None, chase_reference=None, booked_fn=None,
                      native_visible_fn=None, native_balance_fn=None, dip_cap=None,
                      sender_dispatch_fn=None, first_balances_fn=None, source_block=None):
    """Execute one real EVM entry against a reservation. Returns {status: filled|aborted|failed|
    unknown|anomaly, ...}. See module docstring for the safety contract.

    Bounded min-out retry (2026-07-17): a mined-REVERTED swap whose revert `revert_reason_fn`
    positively classifies as a router min-out ('Too little received' / V2 INSUFFICIENT_OUTPUT_AMOUNT
    / V4TooLittleReceived) is QUOTE MOVEMENT — the pool moved between quote and landing — and is
    retried EXACTLY ONCE with a fresh quote. The reservation is kept through the retry and released
    on the second failure. Unknown reverts never retry (fail-closed)."""
    pending = pending or m.pending_row(conn, pending_id) or {}
    contract_fields = m.validated_pending_funded_contract(conn, pending)
    pending_network = str(pending.get("network_id") or "")
    if pending_network not in ("", str(network_id)):
        m.release_pending(conn, pending_id, "network_mismatch")
        return {"status": "aborted", "reason": "pending network mismatch"}
    provenance = {
        "attempt_id": pending.get("attempt_id"),
        "origin_event_id": pending.get("origin_event_id"),
        "network_id": str(network_id),
        "signal_source": pending.get("signal_source"),
    }
    pending_source_price = positive_price(pending.get("source_price"))
    from sniper.settings import memecoin as cfg
    market_only = getattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)

    _MAX_SWAP_ATTEMPTS = 2                       # 1 send + at most 1 min-out retry, never more
    confirmed_unwraps = set()                    # retry-local only; never cached across entries
    safety_checked = False                       # parallel safety joins ONCE, before 1st mutation
    gas_guard_checked = False                    # likewise: asked once, before the 1st mutation
    gas_guard_class = None                       # the route class it priced (re-asked if it changes)
    preflight_unknown_retry_used = False
    entry_usdc_pre = entry_tok_pre = None
    # NATIVE FLOAT (spec 2026-09-27): `native_float_fn(quote)` returns a probe, or None when the chain
    # is not opted in. `float_off` turns it off for the rest of THIS entry after one fallback.
    float_off = False
    float_used = False

    def finish_with_cleanup(reason, result):
        if not confirmed_unwraps or cleanup_native_fn is None:
            m.release_pending(conn, pending_id, reason)
            return result
        amount = sum(int(key[1][-64:], 16) for key in confirmed_unwraps)
        try:
            cleanup_status = cleanup_native_fn(amount)
        except Exception:
            cleanup_status = "unknown"
        result["cleanup_status"] = cleanup_status
        if cleanup_status != "confirmed":
            m.mark_pending_recovery_status(conn, pending_id, "native_cleanup_" + cleanup_status)
        # The entry has a terminal failure here. The cleanup only wraps the prior native WETH
        # unwrap, so it cannot create a coin position or spend the reserved entry notional.
        # Keep its identity for audit, but never let it hold the entry slot.
        m.release_pending(conn, pending_id, reason)
        return result

    def entry_preflight(quote, attempt_no):
        if quote_safety_fn is not None:
            try:
                safe, reason = quote_safety_fn(quote)
            except Exception:
                safe, reason = False, "safety_exception: exact quote check failed"
            if not safe:
                return reason or "safety_exception: exact quote check refused"
        if entry_preflight_fn is None:
            return None
        try:
            verdict = entry_preflight_fn(quote)
        except Exception:
            verdict = (False, "entry_simulation_rpc_unknown: exact route simulation unavailable")
        if verdict is None:
            return None
        try:
            safe, reason = verdict
        except (TypeError, ValueError):
            safe, reason = False, "entry_simulation_malformed: exact route simulation unavailable"
        _trace_substage(trace, "evm.entry_preflight",
                        outcome="passed" if safe else "refused", reason=reason,
                        attempt_no=attempt_no)
        return None if safe else reason

    partial_attribution = "exact_event" if pending_source_price is not None else None

    def _chase_reference(quote):
        """(price, basis, note) the chase gates compare a quote with (spec
        2026-09-28-memecoin-evm-chase-post-trade-reference-design.md): the source's post-trade
        price when `chase_reference` has one for the pool THIS quote trades, else the reservation's
        average fill. It peeks at a J7 receipt read and never waits on it. The premium, drift
        columns and fill attribution keep the average."""
        if market_only:
            from sniper.execution.entry_attribution import market_movement_check
            movement = market_movement_check(cap_pct=cfg.copy_premium_cap(source_user_id, network_id)[0])
            _trace_substage(trace, "copy_market_movement", **movement)
            return None, "intervening_market_unknown", "pre_copy_marginal_unavailable"
        if chase_reference is None:
            return pending_source_price, "avg_fill", None
        try:
            from sniper.services.memecoin_evm_post_trade import route_pool
            return chase_reference.resolve(
                pending_source_price, route_pool(network_id, (quote or {}).get("route")))
        except Exception:
            return pending_source_price, "avg_fill", "reference_error"

    def _with_buy_tax(quote, attempt_no):
        """The quote with its signed minimum lowered by the coin's measured buy tax, else the quote
        unchanged (spec 2026-09-30-memecoin-evm-tax-aware-entry-min-out-design.md; Alpha: bound
        to the pool). Applies only when: the chain is in MEMECOIN_EVM_TAX_AWARE_MIN_OUT_CHAINS; the
        source's receipt decode, already in hand on the event (`chase_reference`, never waited
        on), carries `buy_tax`; and this quote is a V2 fee-on-transfer buy out of THAT pair. Any
        other rung keeps its minimum: an aggregator may price the tax, and a pool the coin does not
        tax would only hand the difference to a searcher. No read, no re-quote."""
        from sniper.settings import memecoin as cfg
        if (chase_reference is None or not isinstance(quote, dict) 
                or str(network_id) not in cfg.MEMECOIN_EVM_TAX_AWARE_MIN_OUT_CHAINS):
            return quote
        try:
            post = chase_reference.payload()
            tax = float(post["buy_tax"]) if isinstance(post, dict) and "buy_tax" in post else None
        except (TypeError, ValueError, AttributeError):
            tax = None
        if tax is None or not math.isfinite(tax) or tax <= 0:
            return quote
        from sniper.execution.evm_v2_exit_quote import with_lower_min_out
        from sniper.services.memecoin_evm_post_trade import route_pool
        ours = route_pool(network_id, quote.get("route"))
        pool = str(post.get("pool") or "").lower()
        tax_bps = int(round(min(tax, float(cfg.MEMECOIN_EVM_BUY_TAX_CAP_PCT) / 100.0) * 10_000))
        outcome, lowered = "pool_mismatch", None
        if ours and pool and str(ours).lower() == pool and tax_bps > 0:
            try:
                new_min = int(quote["minBuyAmount"]) - int(quote["buyAmount"]) * tax_bps // 10_000
            except (KeyError, TypeError, ValueError):
                new_min = 0
            lowered = with_lower_min_out(quote, new_min)
            outcome = "applied" if lowered is not None else "not_fee_on_transfer_v2"
        _trace_substage(trace, "evm.buy_tax_min_out", outcome=outcome, attempt_no=attempt_no,
                        buy_tax=tax, tax_bps=tax_bps, pool=pool or None, route_pool=ours,
                        min_before=quote.get("minBuyAmount"),
                        min_after=(lowered or {}).get("minBuyAmount"))
        return lowered if lowered is not None else quote

    def _reference_active(basis, note):
        # A reference that is off for this chain resolves to ("avg_fill", None): then every string
        # and trace substage is today's byte for byte (Beta F1).
        return chase_reference is not None and (note is not None or basis != "avg_fill")

    def _copy_ceiling(quote):
        from sniper.settings import memecoin as cfg
        if market_only:
            reason = None
            # Unknown movement does not remove the quote's signed slippage boundary.
            try:
                minimum, expected = int(quote["minBuyAmount"]), int(quote["buyAmount"])
                if not 0 < minimum <= expected:
                    reason = "copy price minimum unavailable"
            except (KeyError, TypeError, ValueError, OverflowError):
                reason = "copy price minimum unavailable"
            if (quote.get("route") or {}).get("protocol") == "relay_same_chain":
                from sniper.execution.evm_relay_quote import relay_output_minimum
                if relay_output_minimum(quote) is None:
                    reason = "copy price signed minimum unproved for Relay route"
            if reason is not None:
                return finish_with_cleanup("copy_signed_floor", {
                    "status": "skipped_chase", "reason": reason, **provenance,
                    **quote_fields(quote),
                    **buy_drift(pending_source_price, quote_fields(quote)["quote_price"], None)})
            return None
        if dip_cap is not None:
            # DIP MAX PREMIUM (2026-10-09): a dip decision's SIGNED minimum must price at most the
            # row's reference x (1 + max), so the dip never fills above it; unknown refuses.
            dip_max = float(dip_cap["max_pct"])
            why = (copy_minimum_refusal(quote, float(dip_cap["reference_price"]), dip_max)
                   if dip_max > 0 else "no headroom (cap %.1f%%)" % dip_max)
            if why is not None:
                return finish_with_cleanup("dip_cap", {
                    "status": "skipped_chase", "reason": "dip cap: " + why, "dip_cap_pct": dip_max,
                    **provenance, **quote_fields(quote),
                    **buy_drift(pending_source_price, quote_fields(quote)["quote_price"], None)})
        reason = copy_minimum_refusal(quote, pending_source_price,
                                      cfg.copy_premium_cap(source_user_id, network_id)[0])
        if reason is not None:
            return finish_with_cleanup('copy_signed_floor', {
                'status': 'skipped_chase', 'reason': reason, **provenance,
                # the dip watch's tick 0 and its k (2026-10-08, SOCKET 8346cfef): this refusal is
                # queued for a dip like the execution premium's
                'dip_route': _dip_route(quote),
                **quote_fields(quote), **buy_drift(pending_source_price, quote_fields(quote)['quote_price'], None)})
        return None

    def _reference_suffix(basis, note):
        return " [reference %s: %s]" % (basis, note) if _reference_active(basis, note) else ""

    def _price_gates(quote, attempt_no, quote_observed_at, revision, allow_recheck=False,
                     tag=None):
        """The pure price gates on one quote. Returns (partial_drift, refusal_result or None).

        `allow_recheck` (the first ladder only): a PRICE refusal decided by a stale-capable
        aggregator quote, while an on-chain rung was cut, returns `_STALE_RECHECK` instead of
        refusing. Nothing is released; the caller asks the ladder once more and gates that quote.
        `tag` is added to the premium and chase events: the recheck pass repeats both inside one
        attempt, and readers that take the first or the last need to tell them apart.

        RUN ON EVERY QUOTE THAT COULD BE SIGNED, not only the first (E1, 2026-09-23). The winner
        refresh replaces the quote AFTER these gates decide, and `_requote_matches` proves only
        that the exact approval still covers the swap -- it says nothing about what the swap now
        costs. A $10 clip that quoted 10 tokens and refreshed to 5 reached signing, while the same
        5-token quote arriving first was refused `skipped_chase` by the chase cap.

        Re-running is STRICTLY TIGHTENING: the initial decision still stands, so a refresh can
        only ever add a refusal. Nothing here does I/O -- `funded_contract_json` is already in
        memory and both decisions are pure -- so a replay costs one JSON parse each (measured p50
        0.014 ms, p90 0.020 ms on a real 2,496-byte contract).

        The chase substage is emitted on the INITIAL pass only, exactly as the post-approval
        requote block re-checks the chase inline without a second line:
        `scripts/research/chase_cap_prospective.py` reads the FIRST chase event, and the trace
        sequence is asserted verbatim in tests.
        """
        partial_drift = buy_drift(pending_source_price, quote_fields(quote)["quote_price"], None)
        # The chase reference first (a pure peek at the source receipt read, never a wait): E11
        # on-chain prices the premium against the same post-trade price (spec 2026-10-08).
        ref_price, ref_basis, ref_note = _chase_reference(quote)
        # EXECUTION PREMIUM. Before any wallet mutation (approval / pre-tx / signing), same
        # position in the sequence as post-peak confirmation: both read the quote we just took
        # and both must decide while the only thing spent is a read.
        premium = _execution_premium(
            contract_fields, quote_fields(quote)["quote_price"], trace,
            attempt_no=attempt_no, revision=revision, source_price=pending_source_price,
            tag=tag, network_id=network_id,
            post_trade_price=(ref_price if str(ref_basis).startswith("post_trade") else None),
        )
        premium_refusal = _premium_refusal(premium)
        recheck = bool(allow_recheck and (stale_aggregator_cut_rungs(quote)
                                          or source_route_won(quote)))
        if premium_refusal is not None:
            reason, release_tag = premium_refusal
            if recheck and release_tag == "execution_premium":
                return partial_drift, _STALE_RECHECK
            return partial_drift, finish_with_cleanup(release_tag, {
                "status": "skipped_chase", "reason": reason, **provenance,
                "display_price": premium.display_price,
                "exact_price": premium.exact_price,
                # The dip watch restates a post-trade cap on the source basis (memecoin_dip_retry).
                "premium_reference_price": premium.reference_price,
                "premium_reference_basis": premium.reference_basis,
                "price_attribution": partial_attribution,
                "dip_route": _dip_route(quote),
                **quote_fields(quote), **partial_drift,
            })
        chase_cap = _effective_chase_cap(
            chase_max_pct, wallet_chase_cap_pct, wallet_chase_cap_expires_at,
            quote_observed_at)
        ref_drift = (partial_drift if ref_basis == "avg_fill" else
                     buy_drift(ref_price, quote_fields(quote)["quote_price"], None))
        # See `_chase_gap`: this asks whether the COIN ran, not whether OUR ROUTE is expensive.
        quote_chase_pct, chase_basis, display_price = _chase_gap(premium, ref_price, ref_drift)
        if revision == "initial":
            _trace_substage(
                trace, "chase_quote_confirmation", gap_pct=quote_chase_pct,
                cap_pct=chase_cap, base_cap_pct=chase_max_pct,
                wallet_cap_pct=wallet_chase_cap_pct,
                wallet_cap_expires_at=wallet_chase_cap_expires_at,
                source_price=pending_source_price,
                quote_price=quote_fields(quote)["quote_price"],
                display_price=display_price, chase_basis=chase_basis,
                **({"reference_price": ref_price, "reference_basis": ref_basis,
                    "reference_note": ref_note} if _reference_active(ref_basis, ref_note) else {}),
                **(tag or {}),
            )
            from sniper.settings import memecoin as _cfg_o
            # The band arrives already lifted by the runner, which leaves the cap WITHOUT the
            # override on the trace (Gamma 663c9882); the band cap in force is recorded beside it.
            override = _cfg_o.copy_cap_override_fields(
                source_user_id, network_id, lane="evm",
                price=quote_fields(quote)["quote_price"], average=pending_source_price,
                post=ref_price if ref_basis != "avg_fill" else None,
                band_pct=getattr(trace, "copy_cap_band_without_override", chase_max_pct),
                band_used_pct=chase_max_pct)
            if override is not None:
                _trace_substage(trace, "copy_cap_override", **override, chase_cap_used=chase_cap,
                                **(tag or {}))
        if chase_cap > 0 and quote_chase_pct is not None and quote_chase_pct > chase_cap:
            if recheck:
                return partial_drift, _STALE_RECHECK
            return partial_drift, finish_with_cleanup("chase_quote", {
                "status": "skipped_chase",
                "reason": ("coin is %+.0f%% above source price (cap %.0f%%, basis %s)"
                           % (quote_chase_pct, chase_cap, chase_basis)
                           + _reference_suffix(ref_basis, ref_note)),
                "quote_chase_pct": quote_chase_pct,
                "chase_basis": chase_basis,
                "price_attribution": partial_attribution,
                **provenance, **quote_fields(quote), **partial_drift,
            })
        _over = _quote_over_source_refusal(
            network_id, quote_fields(quote)["quote_price"], ref_price)
        if _over is not None:
            if recheck:
                return partial_drift, _STALE_RECHECK
            return partial_drift, finish_with_cleanup("quote_over_source", {
                "status": "skipped_chase", "reason": _over + _reference_suffix(ref_basis, ref_note),
                "price_attribution": partial_attribution,
                **provenance, **quote_fields(quote), **partial_drift,
            })
        post_peak = _post_peak_confirmation(
            contract_fields, quote_fields(quote)["quote_price"], trace,
            attempt_no=attempt_no, revision=revision,
        )
        if post_peak is not None and post_peak.confirmed:
            return partial_drift, finish_with_cleanup("post_peak_confirmed", {
                "status": "aborted", "reason": "post_peak_confirmed", **provenance,
                "peak_price": post_peak.peak_price,
                "display_price": post_peak.display_price,
                "exact_price": post_peak.exact_price,
                "exact_giveback_pct": post_peak.exact_giveback_pct,
                "price_attribution": partial_attribution,
                **quote_fields(quote), **partial_drift,
            })
        return partial_drift, None

    def _retry_on_winner(requote, slip, want, spender, attempt_no):
        """The min-out retry's fresh quote FROM THE WINNING RUNG, or None to run the ladder.

        A min-out revert means the minimum was computed on state the pool has left, so the retry
        needs a fresh quote at a wider slippage -- not a fresh VENUE. Re-running the whole ladder
        spends the 1.8-3.0 s that made the quote stale in the first place, which is the staleness
        the retry exists to escape.

        Returns None on anything unexpected -- no answer, a different spender, a different size --
        and the caller then runs the ladder exactly as it did before. This can only ever save a
        ladder call; it can never be the reason an entry fails.
        """
        fresh = None
        try:
            fresh = requote(slippage_bps=slip) if slip is not None else requote()
        except Exception:
            fresh = None
        ok = bool(fresh) and _requote_matches(fresh, want, spender)
        _trace_substage(trace, "evm.min_out_retry_winner",
                        outcome="reused" if ok else "ladder",
                        reason="ok" if ok else ("requote_none" if not fresh else
                                                "requote_mismatch"),
                        attempt_no=attempt_no, slippage_bps=slip)
        return fresh if ok else None

    def _source_block():
        """The source trade's block: the event's own, else the J7 lane's receipt read when it has
        already finished (a peek, never a wait). None when unknown -- absent, never 0."""
        if type(source_block) is int and source_block > 0:
            return source_block
        try:
            post = chase_reference.payload() if chase_reference is not None else None
            seen = post.get("source_receipt_observation") if isinstance(post, dict) else None
            block = (seen.get("source_block_number")
                     if isinstance(seen, dict) and seen.get("network_id") == str(network_id) else None)
        except Exception:
            return None
        return block if type(block) is int and block > 0 else None

    def _state_block(quote):
        """The block a quote's state was read at, when its rung pins one (the Pons counter cache)."""
        route = quote.get("route") if isinstance(quote, dict) else None
        block = route.get("state_block") if isinstance(route, dict) else None
        return block if type(block) is int and block > 0 else None

    def _pre_source(quote):
        """(state block, source block) when this quote was priced BEFORE the source's trade, else
        None. Its price cannot include the source's own buy, so the minimum is too high and the
        preflight reverts (CLAWDMKT 2026-10-16: -2.8% against the source's average at quote time,
        +25.5% on the re-quote). Kill knob MEMECOIN_EVM_REFRESH_PRE_SOURCE_STATE."""
        from sniper.settings import memecoin as cfg
        state = _state_block(quote)
        if state is None or not cfg.memecoin_evm_refresh_pre_source_state():
            return None
        source = _source_block()
        return (state, source) if source is not None and state < source else None

    def _refresh_stale_winner(quote, attempt_no, token_decimals):
        """Re-ask the WINNING rung when this quote is older than the preflight tolerates.

        THE OTHER HALF OF THE 'row' REVERT (2026-09-22). 0xb30fcc1d passed its preflight at
        21:40:21.35 and the chain reverted the transaction two seconds later, 115,763 gas for
        nothing. Routing reads to the freshest pool member fixed the node's share of that
        staleness; this fixes ours, because the ladder takes 1.8-3.0 s and the minimum is computed
        at the top of it.

        ONE RUNG, NOT THE LADDER: `_requote` re-asks only the rung that won, so a refresh costs a
        single quote rather than the seconds that made the quote stale in the first place.

        A FAILED REFRESH PROCEEDS ON THE ORIGINAL (operator, 2026-09-22). One immediate second
        attempt, then carry on and let the preflight decide: it now reads the freshest member, so a
        route that is truly gone reverts there and the fallback takes the next alternate with no
        gas spent. Refusing here would add a refusal with no evidence yet of how often a winner
        stops quoting for 750 ms -- the trace below is what produces that evidence.

        A MISMATCHED REFRESH IS DISCARDED, NOT USED. `_requote_matches` is the same check the
        post-approval refresh uses: the fresh quote must sell exactly the same amount and route
        through the same spender, because a refresh that changed venue would invalidate the
        approval this entry is about to rely on.
        """
        if not isinstance(quote, dict):
            return quote
        requote = quote.get("_requote")
        # THE AGE COMES FROM THE PROCESS-LOCAL CLOCK, NOT THE PERSISTED INSTANT (E2, 2026-09-23).
        # `_sniper_quote_observed_at` is an ISO UTC string for the chase window and attribution;
        # `float()` on it raised, and a capacity-reused quote was therefore never refreshed. A
        # quote with no monotonic stamp has no age WE can compute, so it proceeds unrefreshed --
        # absent, not zero.
        observed = quote.get("_sniper_quote_monotonic_at")
        if not callable(requote) or observed is None:
            _trace_substage(trace, "evm.winner_refresh", outcome="skipped", reason="no_requote",
                            attempt_no=attempt_no, refreshed=False)
            return quote
        try:
            age_ms = max(0.0, (time.perf_counter() - float(observed)) * 1000.0)
        except (TypeError, ValueError):
            return quote
        # A QUOTE PRICED BEFORE THE SOURCE'S TRADE IS STALE AT ANY AGE (D-c, 2026-10-16): the same
        # one-rung re-ask, no other read. A replacement still before the source is used anyway and
        # named (`still_before_source`): it is the newer read, and the preflight decides, as for
        # every refresh (operator 2026-09-22).
        pre_source = _pre_source(quote)
        trigger = {} if pre_source is None else {
            "trigger": "state_before_source", "state_block": pre_source[0],
            "source_block": pre_source[1]}
        if age_ms < _preflight_max_quote_age_ms() and pre_source is None:
            _trace_substage(trace, "evm.winner_refresh", outcome="fresh", reason="fresh",
                            attempt_no=attempt_no, refreshed=False, age_ms=round(age_ms, 1))
            return quote
        fresh = None
        for _ in range(2):                    # one immediate second call before giving up
            try:
                fresh = requote()
            except Exception:
                fresh = None
            if fresh:
                break
        if not fresh:
            _trace_substage(trace, "evm.winner_refresh", outcome="unavailable",
                            reason="requote_none", attempt_no=attempt_no, refreshed=False,
                            age_ms=round(age_ms, 1), **trigger)
            return quote
        allow = (quote.get("issues") or {}).get("allowance") or {}
        spender = allow.get("spender") or (quote.get("transaction") or {}).get("to")
        try:
            want = int(quote.get("sellAmount") or 0)
        except (TypeError, ValueError):
            want = 0
        if not spender or not want or not _requote_matches(fresh, want, spender):
            _trace_substage(trace, "evm.winner_refresh", outcome="mismatch",
                            reason="requote_mismatch", attempt_no=attempt_no, refreshed=False,
                            age_ms=round(age_ms, 1), **trigger)
            return quote
        bound = bind_quote_metadata(
            fresh, network_id=network_id, mint=mint, quote_observed_at=trace_now(trace) or now_iso,
            sell_decimals=sell_decimals, buy_decimals=token_decimals,
            sell_token_usd=sell_token_usd,
        )
        alternates = quote.get("_alternates")
        if alternates:
            # THE RUNNER-UPS SURVIVE THE REFRESH (E3, 2026-09-23). `_requote` re-asks ONE rung, so
            # `evm_real_seams` builds no `_alternates` for the replacement -- that list only exists
            # when a second rung completed. Dropping the original's disabled the MUSEFUND/AGRIPPA
            # fall-through for exactly the entries that took a refresh: the replacement reverts in
            # preflight and a known in-budget runner-up sits unused. They are the same quotes at
            # the same age they would have had if the winner had not been refreshed, which is the
            # path that already uses them, and the adopted one still takes the full gate pass,
            # its own preflight and the fallback cap.
            bound["_alternates"] = alternates
        if trigger:
            trigger["fresh_state_block"] = _state_block(fresh)
            trigger["still_before_source"] = _pre_source(fresh) is not None
        _trace_substage(trace, "evm.winner_refresh", outcome="refreshed", reason="ok",
                        attempt_no=attempt_no, refreshed=True, age_ms=round(age_ms, 1),
                        rung=quote.get("_sniper_winner"), **trigger)
        return bound

    attempt = 0
    fallback_quote = None            # the next ladder hit, taken when the winner's preflight reverted
    fallback_from = None
    fallbacks_used = 0               # a fallback swaps the quote; it never spends a swap attempt
    retry_requote = None             # the winner's refresh, carried into a min-out retry
    retry_spender = None
    retry_want = 0
    stale_recheck_used = False       # ONE extra ladder per entry, and only on a would-be refusal
    stale_original = None            # the refused aggregator quote, kept if the recheck answers nothing
    recheck_exclude = ()             # rungs the recheck must not ask (a refused source-route winner)
    source_refused = ()              # ...and the post-approval requote of the route that replaced it
    roundtrip_resized = False        # ONE shrink-instead-of-refuse per entry (_evm_roundtrip_resize)
    # BALANCE READS BESIDE THE LADDER (spec 2026-10-01 EVM entry speed C3, default off). The same
    # two reads `_read_balances` makes, started here instead of after the ladder, and taken once at
    # the first read site below: before the price gates and the first wallet mutation, as today.
    # This runs under the caller's per-(wallet, chain) lock (exec_lock.serialize_executor), so no
    # exit can move the balances between the read and the swap. A read that raises raises at the
    # same site. Later passes (a recheck or a min-out retry) read fresh, as today.
    early_balances = None
    from sniper.settings import memecoin as _cfg
    if (first_balances_fn is None
            and str(network_id) in _cfg.memecoin_evm_balance_beside_ladder_chains()):
        _pool = ThreadPoolExecutor(max_workers=2)
        early_balances = (_pool.submit(usdc_fn), _pool.submit(token_fn, mint))
        _pool.shutdown(wait=False)
    while attempt < _MAX_SWAP_ATTEMPTS:
        attempt_no = int(trace_attempt_base) + attempt
        # 1. quote (USDC -> token), FRESH on every attempt — a min-out retry with the stale quote
        #    would just revert again; the fresh quote re-baselines amountOutMinimum on the moved pool.
        slip = (retry_slippage_bps if (
            attempt or (str(network_id) == "4663" and safety_gate_fn is not None)) else None)
        if fallback_quote is not None:
            # PREFLIGHT FALLBACK (2026-09-22, MUSEFUND / AGRIPPA): the ladder's winner reverted in
            # simulation while another rung had an in-budget quote. That quote goes through every
            # gate below exactly as a fresh one would; only the ladder call is skipped.
            quote, fallback_quote = fallback_quote, None
        elif retry_requote is not None and (fresh := _retry_on_winner(
                retry_requote, slip, retry_want, retry_spender, attempt_no)) is not None:
            # MIN-OUT RETRY RE-ASKS THE WINNER (2026-09-22). Re-running the whole ladder to
            # re-baseline one minimum costs the 1.8-3.0 s that made the quote stale, which is the
            # staleness the retry exists to escape. Same rung, new slippage. If the winner will not
            # answer or answers a different venue, `_retry_on_winner` returns None and the ladder
            # below runs exactly as before.
            quote, retry_requote = fresh, None
        elif recheck_exclude:
            # A SOURCE-ROUTE WINNER FAILED A PRICE GATE (2026-10-15): today's rungs alone answer
            # the recheck. A seam without `exclude_rungs` answers nothing, so the source quote is
            # gated again below and its refusal stands -- never a fill at the refused price.
            try:
                quote = quote_fn(mint, usd_amount, slippage_bps=slip, exclude_rungs=recheck_exclude)
            except TypeError as exc:
                if "exclude_rungs" not in str(exc):
                    raise
                quote = None
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "source-route recheck quote raised; the original refusal stands "
                    "attempt_id=%s pending_id=%s chain=%s mint=%s",
                    provenance.get("attempt_id"), pending_id, network_id, mint)
                quote = None
            recheck_exclude = ()
        else:
            try:
                if slip is None:
                    quote = quote_fn(mint, usd_amount)
                else:
                    try:
                        quote = quote_fn(mint, usd_amount, slippage_bps=slip)
                    except TypeError as exc:
                        if "slippage_bps" not in str(exc):
                            raise
                        quote = quote_fn(mint, usd_amount)
            except Exception:
                # A raise on the RECHECK ladder is "no answer": the first quote already earned a
                # refusal and keeps it. On any other ladder the raise propagates exactly as before.
                if stale_original is None:
                    raise
                import logging
                logging.getLogger(__name__).exception(
                    "stale-aggregator recheck quote raised; the original refusal stands "
                    "attempt_id=%s pending_id=%s chain=%s mint=%s",
                    provenance.get("attempt_id"), pending_id, network_id, mint)
                quote = None
        retry_requote = None
        # The recheck pass repeats quote_initial, the quote mark, balance_pre and the price-gate
        # events inside the same attempt_no; the second set carries this tag. Empty otherwise.
        recheck_tag = {"stale_recheck": True} if stale_original is not None else {}
        if stale_original is not None:
            # The recheck ladder. No answer is not "no quote": the original quote is gated again
            # (recheck now off) and refuses exactly as it would have, never a miss or a LI.FI entry.
            _trace_substage(trace, "evm.stale_aggregator_recheck", attempt_no=attempt_no,
                            outcome="requoted" if quote else "kept_original",
                            winner=(quote.get("_sniper_winner") if isinstance(quote, dict) else None))
            quote, stale_original = (quote or stale_original), None
        _trace_substage(trace, "evm.quote_initial",
                        duration_ms=(quote.get("_sniper_quote_duration_ms")
                                     if isinstance(quote, dict) else None),
                        outcome="available" if quote else "unavailable", attempt_no=attempt_no,
                        reused_capacity_quote=bool(
                            isinstance(quote, dict) and quote.get("_sniper_capacity_quote")),
                        rungs=(quote.get("_sniper_rungs")
                               if isinstance(quote, dict) else None),
                        **_v4_ranking_fields(quote), **recheck_tag)
        recorded_quote_at = trace_mark_submission(
            trace, "quote", attempt_no,
            outcome="available" if quote else "unavailable", revision="initial", **recheck_tag,
        ) or trace_now(trace) or now_iso
        quote_observed_at = (
            quote.get("_sniper_quote_observed_at")
            if isinstance(quote, dict) else None) or recorded_quote_at
        if not quote:
            miss = _quote_miss(quote_miss_fn)
            _trace_substage(trace, "evm.quote_miss", attempt_no=attempt_no,
                            code=miss.get("code"), detail=miss.get("detail"),
                            rungs=miss.get("rungs"))
            if attempt == 0 and not roundtrip_resized and miss.get("code") in _OVER_BUDGET_CODES:
                roundtrip_resized = True
                resized = _evm_roundtrip_resize(
                    (lambda usd: quote_fn(mint, usd) if slip is None
                     else quote_fn(mint, usd, slippage_bps=slip)),
                    mint, usd_amount, miss, str(network_id), trace)
                if resized is not None:
                    quote, usd_amount = resized
                    quote_observed_at = ((quote.get("_sniper_quote_observed_at")
                                          if isinstance(quote, dict) else None)
                                         or trace_now(trace) or now_iso)
        if not quote:
            if attempt == 0:
                code = miss.get("code")
                if code == "no_route" and lifi_entry_fn is not None and (market_only or dip_cap is None):
                    try:
                        lifi_result = lifi_entry_fn(miss)
                    except Exception:
                        lifi_result = {
                            "status": "unknown", "reason": "lifi_internal_error",
                            "lifi_failure_type": "internal_error",
                        }
                    if not isinstance(lifi_result, dict):
                        lifi_result = {
                            "status": "unknown", "reason": "lifi_invalid_result",
                            "lifi_failure_type": "internal_error",
                        }
                    _trace_substage(
                        trace, "lifi.entry", outcome=lifi_result.get("status"),
                        direct_miss_code=code,
                        lifi_failure_type=lifi_result.get("lifi_failure_type"),
                    )
                    return {
                        **lifi_result,
                        "direct_quote_miss_code": code,
                        **provenance,
                    }
                m.release_pending(conn, pending_id, "quote_" + code if code
                                  else "quote_unavailable")
                reason = "no quote (%s: %s)" % (code, miss["detail"]) if code else "no quote"
                return {"status": "aborted", "reason": reason,
                        "quote_miss_code": code, **provenance}
            # first attempt REVERTED min-out and the retry could not obtain a fresh quote
            return finish_with_cleanup("requote_unavailable", {
                "status": "failed", "reason": "reverted_min_out",
                "revert_detail": "no fresh quote for the min-out retry",
                "quote_miss_code": miss.get("code"), **provenance})
        quote = bind_quote_metadata(
            quote, network_id=network_id, mint=mint, quote_observed_at=quote_observed_at,
            sell_decimals=sell_decimals, sell_token_usd=sell_token_usd,
        )
        try:
            sell_atomic = int(quote.get("sellAmount") or 0)
        except (TypeError, ValueError):
            sell_atomic = 0

        # 2. Read balances on each attempt for current decimals. Keep the first balances for the
        #    final fill delta because a confirmed WETH unwrap survives a failed native swap.
        float_probe = None
        if native_float_fn is not None and not float_off and quote.get("pre_transactions"):
            try:   # starts the native read on its own thread, beside the balance reads below
                float_probe = native_float_fn(quote)
            except Exception:
                float_probe = None
        beside = {}
        if first_balances_fn is not None:
            first, first_balances_fn = first_balances_fn, None
            usdc_pre, token_snapshot = first()
            beside = {"beside_ladder": True}
        elif early_balances is not None:
            early, early_balances = early_balances, None
            usdc_pre, token_snapshot = early[0].result(), early[1].result()
            beside = {"beside_ladder": True}
        else:
            usdc_pre, token_snapshot = _read_balances(usdc_fn, token_fn, mint)
        quote_raw_pre = _quote_raw(usdc_fn)
        tok_pre, token_decimals = balance_parts(token_snapshot)
        if entry_usdc_pre is None:
            entry_usdc_pre, entry_tok_pre = usdc_pre, tok_pre
        _trace_substage(trace, "evm.balance_pre", attempt_no=attempt_no, **recheck_tag, **beside)
        if isinstance(token_snapshot, tuple) and token_decimals is None:
            _trace_substage(trace, "evm.token_decimals", attempt_no=attempt_no,
                            outcome="unavailable")
            m.release_pending(conn, pending_id, "token_decimals_unavailable")
            return {"status": "aborted", "reason": "token decimals unavailable",
                    **provenance}
        quote = bind_quote_metadata(
            quote, network_id=network_id, mint=mint, quote_observed_at=quote_observed_at,
            sell_decimals=sell_decimals, buy_decimals=token_decimals,
            sell_token_usd=sell_token_usd,
        )
        partial_drift, refusal = _price_gates(
            quote, attempt_no, quote_observed_at, "initial",
            allow_recheck=not stale_recheck_used and attempt == 0, tag=recheck_tag)
        if refusal is _STALE_RECHECK:
            stale_recheck_used, stale_original = True, quote
            if source_route_won(quote):
                recheck_exclude = source_refused = tuple(sorted(_SOURCE_RUNGS))
            _trace_substage(trace, "evm.stale_aggregator_refusal", attempt_no=attempt_no,
                            winner=quote.get("_sniper_winner"),
                            cut_rungs=list(stale_aggregator_cut_rungs(quote)))
            continue                     # same attempt: the loop asks the ladder again
        if refusal is not None:
            if attempt == 0:
                # This initial quote's price gates precede EVERY wallet mutation. In particular,
                # the unsent ordinary-lane refusal must not invalidate a parallel fast-lane buy.
                # attempt > 0 has already mined a swap; even a price refusal then must stamp.
                from sniper.execution.exec_lock import UnchangedWalletResult
                return UnchangedWalletResult(refusal)
            return refusal

        # 2b. parallel token-safety join: the verdict was computed concurrently with the quote
        # (read-only either way); it MUST be checked before the first wallet mutation (approval /
        # pre-tx / signing). Joined once — a min-out retry re-runs mutations, not safety.
        if safety_gate_fn is not None and not safety_checked:
            safe, safety_reason = safety_gate_fn()
            safety_checked = True
            if not safe:
                m.release_pending(conn, pending_id, "token_safety")
                return {"status": "unsafe_gate", "reason": safety_reason, **provenance,
                        "price_attribution": partial_attribution,
                        **quote_fields(quote), **partial_drift}

        # 2c. GAS GUARD (chain 1, eth-mainnet spec §4.3): the last read-only gate before the first
        # wallet mutation. None on every other chain. A refusal has spent nothing but reads.
        if gas_guard_fn is not None and not gas_guard_checked:
            gas_guard_checked = True
            # A tagged guard fills a fresh dict with its numbers for the trace (Alpha 2026-10-07: a
            # pass recorded nothing); any other guard is called exactly as before.
            gas_facts = {}
            if getattr(gas_guard_fn, "takes_route", False):
                # THE ROUTE IT PRICES IS THIS QUOTE'S (2026-10-07): units per route class.
                gas_refusal = gas_guard_fn(usd_amount, facts=gas_facts,
                                           route=(quote or {}).get("route"))
                gas_guard_class = gas_facts.get("route_class")
            elif getattr(gas_guard_fn, "fills_facts", False):
                gas_refusal = gas_guard_fn(usd_amount, facts=gas_facts)
            else:
                gas_refusal = gas_guard_fn(usd_amount)
            _trace_substage(trace, "evm.gas_guard", outcome="refused" if gas_refusal else "passed",
                            reason=gas_refusal, attempt_no=attempt_no, **gas_facts)
            if gas_refusal:
                m.release_pending(conn, pending_id, "gas_guard")
                return {"status": "aborted", "reason": gas_refusal, **provenance,
                        "price_attribution": partial_attribution,
                        **quote_fields(quote), **partial_drift}

        refreshed = _refresh_stale_winner(quote, attempt_no, token_decimals)
        if refreshed is not quote:
            # THE REPLACEMENT IS A DIFFERENT PRICE, so it gets the same gates and the same release
            # tags (E1). `_refresh_stale_winner` returns the ORIGINAL object on a requote that
            # raised, answered nothing, or answered a different venue or size, so every failure
            # mode of the refresh keeps the single initial decision untouched.
            quote = refreshed
            # THE REPLACEMENT'S OWN INSTANT COMES WITH IT (E1 recheck, 2026-09-23).
            # `_refresh_stale_winner` binds the new observation time, and re-running the gates on
            # the ORIGINAL one let a replacement inherit a wallet chase allowance that had already
            # expired: the window is judged by `_effective_chase_cap` from this exact variable,
            # which also feeds the trace's quote age and the final attribution bind. Adopting it
            # can only move the cap DOWN to the base value, never up, so this is tightening only.
            # The post-approval requote below has always done the same (`quote_observed_at =
            # fresh_observed_at`). Falling back to the previous instant keeps today's behaviour if
            # the bind ever produces no stamp -- absent, never None.
            quote_observed_at = quote.get("_sniper_quote_observed_at") or quote_observed_at
            partial_drift, refusal = _price_gates(
                quote, attempt_no, quote_observed_at, "refresh")
            if refusal is not None:
                return refusal
        quote = _with_buy_tax(quote, attempt_no)
        refusal = _copy_ceiling(quote)
        if refusal is not None:
            return refusal
        preflight_reason = entry_preflight(quote, attempt_no)
        if preflight_reason is not None:
            alternates = quote.get("_alternates") if isinstance(quote, dict) else None
            # Both a proven revert and an UNPROVABLE simulation (a node that answered junk, the
            # 2026-09-22 MUSEFUND/AGRIPPA class once _is_revert_evidence stopped reading 0x0 as
            # proof) leave the winner unsigned; a runner-up with its own passing preflight is fine.
            if (preflight_reason.startswith(("entry_simulation_reverted:",
                                             "entry_simulation_rpc_unknown:")) and alternates
                    and fallbacks_used < 3):
                # THE NEXT HIT, NOT A REFUSAL. The winner's route does not execute; a rung that
                # also quoted inside budget gets its turn, with its own preflight and gates. The
                # remaining runner-ups ride along so a second revert falls through again.
                nxt = alternates[0]
                rest = list(alternates[1:])
                fallback_quote = dict(nxt.get("quote") or {})
                if rest:
                    fallback_quote["_alternates"] = rest
                else:
                    fallback_quote.pop("_alternates", None)
                fallback_from = quote.get("_sniper_winner") or "winner"
                _trace_substage(trace, "evm.entry_preflight_fallback", outcome="fallback",
                                reason=preflight_reason, attempt_no=attempt_no,
                                fallback_to=nxt.get("rung"), fallback_from=fallback_from)
                fallbacks_used += 1
                continue
            if (not preflight_unknown_retry_used and attempt == 0
                    and preflight_reason.startswith("entry_simulation_reverted:")):
                preflight_unknown_retry_used = True
                _trace_substage(trace, "evm.entry_preflight_retry",
                                outcome="retry", reason=preflight_reason)
                continue
            if (preflight_reason.startswith("entry_simulation_min_out")
                    and attempt + 1 < _MAX_SWAP_ATTEMPTS):
                # Carry the winner so the retry re-asks IT at the wider slippage instead of
                # re-running the ladder. The spender and size it must still match are taken from
                # the quote that just failed, not from the fresh one.
                if isinstance(quote, dict) and callable(quote.get("_requote")):
                    allow = (quote.get("issues") or {}).get("allowance") or {}
                    retry_spender = (allow.get("spender")
                                     or (quote.get("transaction") or {}).get("to"))
                    try:
                        retry_want = int(quote.get("sellAmount") or 0)
                    except (TypeError, ValueError):
                        retry_want = 0
                    retry_requote = quote["_requote"] if (retry_spender and retry_want) else None
                attempt += 1
                continue
            m.release_pending(conn, pending_id, "entry_preflight")
            return {"status": "unsafe_gate", "reason": preflight_reason, **provenance,
                    "price_attribution": partial_attribution,
                    **quote_fields(quote), **partial_drift}

        # 2c. IN-SWAP UNWRAP (2026-09-28, spec 4663 V4 in-swap unwrap). Say which shape this buy
        # took. The router pulls sellAmount of WETH through Permit2 inside the swap, and more than
        # the wallet holds reverts on chain and burns the gas: the pre-transaction unwrap's guard
        # and facts, run here because the approval below is the first wallet mutation.
        swap_route = quote.get("route") or {}
        if swap_route.get("in_swap_unwrap") or swap_route.get("in_swap_unwrap_fallback"):
            _trace_substage(trace, "evm.v4_in_swap_unwrap", attempt_no=attempt_no,
                            outcome=("applied" if swap_route.get("in_swap_unwrap") else
                                     "fallback_%s" % swap_route["in_swap_unwrap_fallback"]))
        if swap_route.get("in_swap_unwrap"):
            over = quote_raw_pre is not None and sell_atomic > quote_raw_pre
            _trace_substage(trace, "evm.unwrap_facts", outcome="refused" if over else "ok",
                            attempt_no=attempt_no, in_swap=True, unwrap_amount_wei=sell_atomic,
                            weth_balance_wei=quote_raw_pre, quote_price_usd=sell_token_usd)
            if over:
                return finish_with_cleanup("unwrap_exceeds_quote_balance", {
                    "status": "aborted", "reason": "unwrap exceeds quote balance",
                    **provenance, "price_attribution": partial_attribution,
                    **quote_fields(quote), **partial_drift})

        # 3. exact-amount approval if the quote flags an allowance issue
        if presign_check_fn is not None:
            reason = presign_check_fn()
            if reason:
                m.release_pending(conn, pending_id, "coin_age_presign")
                return {"status": "unsafe_gate", "reason": reason, **provenance}
        allow = (quote.get("issues") or {}).get("allowance")
        if allow and allow.get("spender"):
            approved_spender = allow["spender"]
            approved = approve_fn(approved_spender, sell_atomic)
            # A STANDING allowance (56, 2026-10-07) sent nothing and waited for nothing: like a
            # pipelined approval there is no stale quote to revise.
            standing = isinstance(approved, dict) and bool(approved.get("standing"))
            pipelined = standing or (isinstance(approved, dict) and bool(approved.get("pipelined")))
            _trace_substage(trace, "evm.approval",
                            outcome=("standing" if standing else "broadcast" if pipelined else
                                     "confirmed" if approved else "failed"),
                            attempt_no=attempt_no)
            if not approved:
                m.release_pending(conn, pending_id, "approve_failed")
                return {"status": "aborted", "reason": "approve failed", **provenance,
                        "price_attribution": partial_attribution,
                        **quote_fields(quote), **partial_drift}
            # Pipelined approval (in flight, not confirmed): there was no wait for the quote to go
            # stale during, so the initial quote + post-peak check (already done, before this first
            # mutation) remain the funded-contract gate — no post-approval revision exists. An
            # approval that somehow reverts surfaces as the swap's allowance revert and is handled
            # by the mined-REVERTED path below (fail-closed, reservation released).
            if not pipelined:
                # 3a. the approval took real seconds to confirm; on a violently-moving pool the
                # original min-out is stale by the time the swap signs. Refresh the quote and
                # require the SAME sell amount + SAME spender (else the exact approval doesn't
                # cover it) — any mismatch aborts fail-closed. The swap signs the FRESH transaction.
                # WINNER ONLY WHEN FLAGGED (2026-09-28, MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE,
                # default off): `quote_fn` re-runs the whole ladder with its post-hit grace; the
                # winner's own `_requote` asks one rung for the SAME amount_atomic -- the amount
                # just approved. A None, an error or a mismatch falls back to the ladder below, so
                # the flag can only remove the wait, never a route today's path would find.
                from sniper.settings import memecoin as cfg
                fresh, requote_path = None, "ladder"
                winner_requote = quote.get("_requote") if isinstance(quote, dict) else None
                if (callable(winner_requote)
                        and bool(getattr(cfg, "MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE", False))):
                    try:
                        fresh = winner_requote(slippage_bps=slip)
                    except Exception:
                        fresh = None
                    if fresh and _requote_matches(fresh, sell_atomic, approved_spender):
                        requote_path = "winner"
                    else:
                        fresh = None
                if fresh is None and source_refused:
                    # The source pool was refused on price this attempt: its requote must not
                    # bring it back. A seam without the keyword answers nothing (fail-closed).
                    try:
                        fresh = quote_fn(mint, usd_amount, slippage_bps=slip,
                                         exclude_rungs=source_refused)
                    except TypeError as exc:
                        if "exclude_rungs" not in str(exc):
                            raise
                        fresh = None
                elif fresh is None:
                    if slip is None:
                        fresh = quote_fn(mint, usd_amount)
                    else:
                        try:
                            fresh = quote_fn(mint, usd_amount, slippage_bps=slip)
                        except TypeError as exc:
                            if "slippage_bps" not in str(exc):
                                raise
                            fresh = quote_fn(mint, usd_amount)
                _trace_substage(trace, "evm.quote_post_approval",
                                outcome="available" if fresh else "unavailable",
                                attempt_no=attempt_no, path=requote_path,
                                rung=(fresh or {}).get("_sniper_winner")
                                if isinstance(fresh, dict) else None)
                fresh_observed_at = trace_mark_submission(
                    trace, "quote", attempt_no,
                    outcome="available" if fresh else "unavailable", revision="post_approval",
                ) or trace_now(trace) or now_iso
                if not _requote_matches(fresh, sell_atomic, approved_spender):
                    m.release_pending(conn, pending_id, "post_approve_quote_mismatch")
                    return {"status": "aborted", "reason": "post-approve quote mismatch",
                            **provenance, "price_attribution": partial_attribution,
                            **quote_fields(quote), **partial_drift}
                quote = bind_quote_metadata(
                    fresh, network_id=network_id, mint=mint, quote_observed_at=fresh_observed_at,
                    sell_decimals=sell_decimals, buy_decimals=token_decimals,
                    sell_token_usd=sell_token_usd,
                )
                quote_observed_at = fresh_observed_at
                partial_drift = buy_drift(pending_source_price,
                                          quote_fields(quote)["quote_price"], None)
                chase_cap = _effective_chase_cap(
                    chase_max_pct, wallet_chase_cap_pct, wallet_chase_cap_expires_at,
                    quote_observed_at)
                ref_price, ref_basis, ref_note = _chase_reference(quote)
                if _reference_active(ref_basis, ref_note):
                    _trace_substage(trace, "chase_requote_reference", attempt_no=attempt_no,
                                    source_price=pending_source_price, reference_price=ref_price,
                                    reference_basis=ref_basis, reference_note=ref_note)
                quote_chase_pct = (partial_drift if ref_basis == "avg_fill" else buy_drift(
                    ref_price, quote_fields(quote)["quote_price"], None)).get("source_drift_pct")
                if chase_cap > 0 and quote_chase_pct is not None and quote_chase_pct > chase_cap:
                    m.release_pending(conn, pending_id, "chase_quote")
                    return {
                        "status": "skipped_chase",
                        "reason": "exact quote is %+.0f%% above source price (cap %.0f%%)"
                                  % (quote_chase_pct, chase_cap) + _reference_suffix(ref_basis, ref_note),
                        "quote_chase_pct": quote_chase_pct,
                        "price_attribution": partial_attribution,
                        **provenance, **quote_fields(quote), **partial_drift,
                    }
                _over = _quote_over_source_refusal(
                    network_id, quote_fields(quote)["quote_price"], ref_price)
                if _over is not None:
                    m.release_pending(conn, pending_id, "quote_over_source")
                    return {"status": "skipped_chase",
                            "reason": _over + _reference_suffix(ref_basis, ref_note),
                            "price_attribution": partial_attribution,
                            **provenance, **quote_fields(quote), **partial_drift}
                # Re-check on the REVISED quote. The approval already landed, but the swap has
                # not been signed, and a post-approval requote can move the price -- a gate that
                # only decided on the initial quote would let the revision through unpriced.
                premium = _execution_premium(
                    contract_fields, quote_fields(quote)["quote_price"], trace,
                    attempt_no=attempt_no, revision="post_approval",
                    source_price=pending_source_price, network_id=network_id,
                    post_trade_price=(ref_price if str(ref_basis).startswith("post_trade")
                                      else None),
                )
                premium_refusal = _premium_refusal(premium)
                if premium_refusal is not None:
                    reason, release_tag = premium_refusal
                    m.release_pending(conn, pending_id, release_tag)
                    return {
                        "status": "skipped_chase", "reason": reason, **provenance,
                        "display_price": premium.display_price,
                        "exact_price": premium.exact_price,
                        # The dip watch restates a post-trade cap on the source basis (memecoin_dip_retry).
                        "premium_reference_price": premium.reference_price,
                        "premium_reference_basis": premium.reference_basis,
                        "price_attribution": partial_attribution,
                        "dip_route": _dip_route(quote),
                        **quote_fields(quote), **partial_drift,
                    }
                post_peak = _post_peak_confirmation(
                    contract_fields, quote_fields(quote)["quote_price"], trace,
                    attempt_no=attempt_no, revision="post_approval",
                )
                if post_peak is not None and post_peak.confirmed:
                    m.release_pending(conn, pending_id, "post_peak_confirmed")
                    return {
                        "status": "aborted", "reason": "post_peak_confirmed", **provenance,
                        "peak_price": post_peak.peak_price,
                        "display_price": post_peak.display_price,
                        "exact_price": post_peak.exact_price,
                        "exact_giveback_pct": post_peak.exact_giveback_pct,
                        "price_attribution": partial_attribution,
                        **quote_fields(quote), **partial_drift,
                    }
                quote = _with_buy_tax(quote, attempt_no)
                refusal = _copy_ceiling(quote)
                if refusal is not None:
                    return refusal
                preflight_reason = entry_preflight(quote, attempt_no)
                if preflight_reason is not None:
                    m.release_pending(conn, pending_id, "entry_preflight")
                    return {
                        "status": "unsafe_gate", "reason": preflight_reason, **provenance,
                        "price_attribution": partial_attribution,
                        **quote_fields(quote), **partial_drift,
                    }

        # A WINNER OF ANOTHER CLASS IS RE-PRICED BEFORE IT IS SIGNED (2026-10-07). The guard ran
        # once, on the initial quote; the post-approval requote, a stale-winner refresh or a
        # min-out retry can land on another route, and a hooked multihop round trip costs 2.2x a
        # hookless one. A refusal here costs at most an approval already sent.
        if (gas_guard_fn is not None and gas_guard_checked
                and getattr(gas_guard_fn, "takes_route", False)):
            from sniper.services.memecoin_evm_risk import route_gas_class
            now_class = route_gas_class((quote or {}).get("route"))
            if now_class != gas_guard_class:
                gas_facts = {}
                gas_refusal = gas_guard_fn(usd_amount, facts=gas_facts,
                                           route=(quote or {}).get("route"))
                _trace_substage(trace, "evm.gas_guard",
                                outcome="refused" if gas_refusal else "passed",
                                reason=gas_refusal, attempt_no=attempt_no,
                                revision="class_change", prior_class=gas_guard_class, **gas_facts)
                gas_guard_class = gas_facts.get("route_class")
                if gas_refusal:
                    m.release_pending(conn, pending_id, "gas_guard")
                    return {"status": "aborted", "reason": gas_refusal, **provenance,
                            "price_attribution": partial_attribution,
                            **quote_fields(quote), **partial_drift}

        tx = quote.get("transaction")
        if not tx:
            m.release_pending(conn, pending_id, "no_tx")
            return {"status": "aborted", "reason": "no swap tx", **provenance,
                    "price_attribution": partial_attribution,
                    **quote_fields(quote), **partial_drift}

        # 3b. Pre-transactions: 4663 pipelines an accepted exact WETH unwrap with its nonce+1 swap.
        # Other pre-transactions stay receipt-first. A retry reuses a confirmed unwrap only.
        pre_failed = None
        replace_unbound_submission = None
        float_used = False
        waited_unwrap = False
        for pre in quote.get("pre_transactions") or ():
            data = str(pre.get("data") or "").lower()
            unwrap_key = None
            if data.startswith("0x2e1a7d4d"):
                unwrap_key = (
                    str(pre.get("to") or "").lower(), data,
                    str(pre.get("value") or "0x0").lower(),
                )
            if unwrap_key is not None and unwrap_key in confirmed_unwraps:
                continue   # the failed swap reverted its value transfer; native remains for retry
            if unwrap_key is not None and float_probe is not None:
                try:
                    covered = bool(float_probe.covers(pre, tx))
                except Exception:
                    covered = False
                _trace_substage(trace, "evm.native_float",
                                outcome="unwrap_skipped" if covered else "unwrap_kept",
                                attempt_no=attempt_no,
                                **getattr(float_probe, "decision_facts", {}))
                if covered:
                    float_used = True
                    continue   # the swap below spends native the wallet already holds
                try:
                    part = float_probe.shortfall(pre, tx) if hasattr(float_probe, "shortfall") else None
                except Exception:
                    part = None
                if part is not None:
                    # PARTIAL UNWRAP (chain-1 unstall, 2026-10-08): spare native pays the rest, so
                    # only the shortfall leaves WETH; the swap's value is unchanged. The guard
                    # below checks the shortfall against WETH, and a failure re-wraps exactly it.
                    pre = dict(pre, data="0x2e1a7d4d%064x" % part)
                    data = pre["data"]
                    unwrap_key = (str(pre.get("to") or "").lower(), data,
                                  str(pre.get("value") or "0x0").lower())
                    float_used = True              # WETH moved by the shortfall only: value is the spend
                    _trace_substage(trace, "evm.native_float", outcome="unwrap_partial",
                                    attempt_no=attempt_no,
                                    **{**getattr(float_probe, "decision_facts", {}),
                                       "shortfall_wei": part})
            if unwrap_key is not None:
                # UNWRAP GUARD AND FACTS (2026-09-28). WETH9 withdraw reverts when the amount
                # exceeds the balance, and a revert burns gas and loses the entry, so an unwrap
                # above the WETH just read is refused before anything is signed. An unknown
                # balance proceeds as before. The facts go on the trace whatever the outcome, so
                # the next failed unwrap names its own cause (reservation 4158 could not).
                try:
                    unwrap_wei = int(data[10:], 16)
                except ValueError:
                    unwrap_wei = None
                try:
                    unwrap_gas = int(str(pre.get("gas") or "0"), 0)
                except ValueError:
                    unwrap_gas = None
                over = (unwrap_wei is not None and quote_raw_pre is not None
                        and unwrap_wei > quote_raw_pre)
                _trace_substage(trace, "evm.unwrap_facts", outcome="refused" if over else "ok",
                                attempt_no=attempt_no, unwrap_amount_wei=unwrap_wei,
                                weth_balance_wei=quote_raw_pre, quote_price_usd=sell_token_usd,
                                unwrap_gas_limit=unwrap_gas,
                                **(getattr(float_probe, "decision_facts", {})
                                   if float_probe is not None else {}))
                if over:
                    return finish_with_cleanup("unwrap_exceeds_quote_balance", {
                        "status": "aborted", "reason": "unwrap exceeds quote balance",
                        **provenance, "price_attribution": partial_attribution,
                        **quote_fields(quote), **partial_drift})
            if unwrap_key is not None and native_pre_pipeline_fn is not None:
                try:
                    pre_result = native_pre_pipeline_fn(pre)
                except Exception:
                    pre_result = {"status": "unknown"}
                if pre_result is not None:
                    pre_result = pre_result if isinstance(pre_result, dict) else {"status": "unknown"}
                    pre_status = str(pre_result.get("status") or "unknown").lower()
                    if pre_status == "accepted":
                        # `accepted` from this seam means the unwrap CONFIRMED, not merely that a
                        # node took it. Nonce order sequences execution once both are mined; it
                        # does not make an unmined unwrap's ETH spendable by the nonce+1 swap, and
                        # the node rejects that swap outright (attempt 7204b30c, 2026-09-03).
                        # Keep it in the cleanup set because a later swap failure can leave native ETH.
                        confirmed_unwraps.add(unwrap_key)
                        replace_unbound_submission = pre_result.get("tx_hash")
                        _trace_substage(trace, "evm.pre_transaction_pipeline", outcome="accepted",
                                        attempt_no=attempt_no,
                                        tx_hash=pre_result.get("tx_hash"),
                                        nonce=pre_result.get("nonce"),
                                        gas_price_wei=pre_result.get("gas_price_wei"),
                                        gas_limit=pre_result.get("gas_limit"))
                        continue
                    _trace_substage(trace, "evm.pre_transaction_pipeline", outcome=pre_status,
                                    attempt_no=attempt_no,
                                    tx_hash=pre_result.get("tx_hash"),
                                    nonce=pre_result.get("nonce"),
                                    gas_price_wei=pre_result.get("gas_price_wei"),
                                    gas_limit=pre_result.get("gas_limit"))
                    if pre_status == "reverted":
                        # MINED AND REVERTED (receipt 0x0): nothing was unwrapped and the swap was
                        # never sent, so the slot is free now -- not ambiguous for 10 minutes
                        # until the stale pass reads the same receipt (reservation 4158).
                        return finish_with_cleanup("pre_transaction_reverted", {
                            "status": "aborted", "reason": "pre-transaction reverted (unwrap)",
                            "tx_hash": pre_result.get("tx_hash"), **provenance,
                            "price_attribution": partial_attribution,
                            **quote_fields(quote), **partial_drift})
                    # ⚠️ `not_visible` IS AMBIGUOUS, NOT FAILED (2026-09-23, LINTCHA). The receipt
                    # already confirmed, so the unwrap HAPPENED; only one pool member had yet to
                    # report the balance. Calling that "pre-tx failed (unwrap)" asserts a failure
                    # that did not occur -- the chain says `success`, block 70750681 -- and it is
                    # the same class the submission doctrine already names `submitted_unknown`:
                    # unseen is never proof of absent. All 5 pre-transactions this system called
                    # ambiguous or failed in the 30 days to 2026-09-23 succeeded on chain.
                    #
                    # The entry still stops, because the swap cannot be admitted against a balance
                    # the validating node has not committed. What changes is that the row no longer
                    # claims a failure, and the re-wrap below still runs.
                    if pre_status == "not_visible":
                        # THE RECEIPT CONFIRMED, so the ETH exists and MUST be re-wrapped even
                        # though the swap never went out -- `finish_with_cleanup` is what does
                        # that, and returning past it would strand the native balance (the
                        # behaviour this branch kept before it stopped claiming a failure).
                        confirmed_unwraps.add(unwrap_key)
                        m.mark_pending_recovery_status(conn, pending_id,
                                                       "native_pre_transaction_ambiguous")
                        return finish_with_cleanup("pipeline_not_visible", {
                            "status": "unknown", "reason": "native pre-transaction ambiguous",
                            "tx_hash": pre_result.get("tx_hash"), **provenance,
                            "price_attribution": partial_attribution,
                            **quote_fields(quote), **partial_drift})
                    if pre_status in ("unknown", "ambiguous"):
                        # No receipt, so the ETH is NOT known to exist and must not be re-wrapped.
                        m.mark_pending_recovery_status(conn, pending_id,
                                                       "native_pre_transaction_ambiguous")
                        return {"status": "unknown", "reason": "native pre-transaction ambiguous",
                                "tx_hash": pre_result.get("tx_hash"), **provenance,
                                "price_attribution": partial_attribution,
                                **quote_fields(quote), **partial_drift}
                    pre_failed = "pipeline_" + pre_status
                    break
            # The pre-unwrap native, read once before the unwrap goes out (Gamma LOW on 883012fdb):
            # the swap needs it PLUS the unwrap, so a wallet already holding about the value cannot
            # pass the visibility check on a node that has not seen the unwrap. Unreadable: the
            # unwrap alone, as the 4663 pipeline checks.
            native_before = None
            if unwrap_key is not None and native_visible_fn is not None and native_balance_fn is not None:
                try:
                    native_before = native_balance_fn()
                    native_before = int(native_before) if native_before is not None else None
                except Exception:
                    native_before = None
            pre_signed = sign_fn(pre)
            if not pre_signed:
                return finish_with_cleanup("pre_tx_sign_failed", {
                        "status": "aborted", "reason": "pre-tx sign failed", **provenance,
                        "price_attribution": partial_attribution,
                        **quote_fields(quote), **partial_drift})
            pre_raw, pre_hash, _pre_nonce = pre_signed
            pre_send = send_fn(pre_raw)
            if pre_send is False:
                pre_failed = "broadcast_rejected"
                break
            pre_state = confirm_fn(pre_hash)    # ReceiptState: carries the receipt it read
            if pre_state != "confirmed":
                pre_failed = "confirmation_unknown"
                break
            if unwrap_key is not None:
                confirmed_unwraps.add(unwrap_key)
                waited_unwrap = True
                # A CONFIRMED UNWRAP THE SWAP'S NODE CANNOT SEE YET (8453 PICNIC af22bb097bbb,
                # 2026-10-06): a flashblocks preconfirmation receipt arrived in 340 ms, the swap went
                # to custody, which still held the pre-unwrap ETH (Base seals every 2 s), and was
                # rejected for insufficient funds; 3 of 3 8453 unwrap entries since 09-15. The swap
                # waits until every healthy pool member shows the ETH (the 4663 pipeline's check),
                # or stops ambiguous and the landed ETH is re-wrapped. Only after an unwrap.
                if native_visible_fn is not None and unwrap_wei:
                    started = time.perf_counter()
                    need_wei = unwrap_wei + (native_before or 0)
                    # THE UNWRAP PAID GAS (EVM-01, X402ID 2026-10-07): a settled balance is
                    # before + unwrap - fee, so before + unwrap could never be met (custody sat
                    # exactly 324,503,662,207 wei short for 3 s). The fee comes from the receipt
                    # the confirmation already holds: no read. Unknown fee keeps the full need
                    # (ambiguous, re-wrapped); a fee not below the unwrap would erase the baseline.
                    # This unwrap is the only pre-transaction any builder emits, and the check runs
                    # before a later one could be sent, so no other fee lands inside this window.
                    paid_wei, fee_basis = _receipt_fee_paid(getattr(pre_state, "receipt", None))
                    if native_before is not None and paid_wei is not None and paid_wei < unwrap_wei:
                        need_wei -= paid_wei
                    try:
                        visible = bool(native_visible_fn(need_wei))
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception(
                            "native visibility check failed; the swap goes as before")
                        visible = True
                    _trace_substage(trace, "evm.native_visible", attempt_no=attempt_no,
                                    visible=visible, need_wei=need_wei,
                                    native_before_wei=native_before, fee_paid_wei=paid_wei,
                                    fee_basis=fee_basis,
                                    wait_ms=round((time.perf_counter() - started) * 1000.0, 1))
                    if not visible:
                        m.mark_pending_recovery_status(conn, pending_id,
                                                       "native_pre_transaction_ambiguous")
                        return finish_with_cleanup("pre_tx_not_visible", {
                            "status": "unknown", "reason": "native pre-transaction ambiguous",
                            "tx_hash": pre_hash, **provenance,
                            "price_attribution": partial_attribution,
                            **quote_fields(quote), **partial_drift})
        _trace_substage(trace, "evm.pre_transactions",
                        count=len(quote.get("pre_transactions") or ()),
                        outcome="failed" if pre_failed else "complete", attempt_no=attempt_no)
        if pre_failed:
            return finish_with_cleanup("pre_tx_failed", {
                    "status": "aborted", "reason": "pre-tx failed (unwrap)", **provenance,
                    "price_attribution": partial_attribution,
                    **quote_fields(quote), **partial_drift})

        if str(network_id) == "8453" and waited_unwrap:
            # The unwrap aged the quote after preflight (BASEDOG 73395). Re-ask only the winner.
            refreshed = _refresh_stale_winner(quote, attempt_no, token_decimals)
            if refreshed is not quote:
                quote = refreshed
                quote_observed_at = quote.get("_sniper_quote_observed_at") or quote_observed_at
                partial_drift, refusal = _price_gates(
                    quote, attempt_no, quote_observed_at, "post_unwrap")
                if refusal is not None:
                    return refusal
                quote = _with_buy_tax(quote, attempt_no)
                refusal = _copy_ceiling(quote)
                if refusal is not None:
                    return refusal
                preflight_reason = entry_preflight(quote, attempt_no)
                if preflight_reason is not None:
                    return finish_with_cleanup("post_unwrap_preflight", {
                        "status": "unsafe_gate", "reason": preflight_reason, **provenance,
                        "price_attribution": partial_attribution,
                        **quote_fields(quote), **partial_drift})
                tx = quote.get("transaction")
                if not tx:
                    return finish_with_cleanup("post_unwrap_no_tx", {
                        "status": "aborted", "reason": "no swap tx", **provenance})

        # 4. sign; write-ahead hash + nonce BEFORE broadcast
        if presign_check_fn is not None:
            reason = presign_check_fn()
            if reason:
                return finish_with_cleanup("coin_age_presign", {
                    "status": "unsafe_gate", "reason": reason, **provenance,
                    "price_attribution": partial_attribution,
                    **quote_fields(quote), **partial_drift})
        signed = sign_fn(tx)
        _trace_substage(trace, "evm.swap_signing",
                        outcome="signed" if signed else "failed", attempt_no=attempt_no)
        trace_mark_submission(
            trace, "signing", attempt_no,
            outcome="signed" if signed else "failed",
        )
        if not signed:
            return finish_with_cleanup("sign_failed", {
                    "status": "aborted", "reason": "sign failed", **provenance,
                    "price_attribution": partial_attribution,
                    **quote_fields(quote), **partial_drift})
        raw, tx_hash, nonce = signed
        if not m.bind_entry_submission(
                conn, pending_id, tx_identity=tx_hash, signed_payload_b64=raw,
                signed_payload_sha256=sha256_hex(raw), blockhash=None,
                last_valid_height=None, nonce=nonce,
                backend=getattr(send_fn, "submission_backend", "evm"), bound_at=now_iso,
                replace_unbound=replace_unbound_submission,
                evm_quote_asset_usd=sell_token_usd if require_receipt_amounts else None):
            return {"status": "unknown", "reason": "submission binding conflict",
                    "tx_hash": tx_hash, **provenance,
                    "price_attribution": partial_attribution,
                    **quote_fields(quote), **partial_drift}

        # 5. send once, confirm
        # DISCARD ANY EARLIER REJECTION BEFORE THE SEND (E8, 2026-09-23). Clearing inside
        # `eth_send_raw` is not enough, because on the funded default path the send never reaches
        # it: a submission transport returns False from `aggregate_to_seam` having never touched
        # the marker, so a message from a PREVIOUS entry on this worker thread was still standing
        # and got attributed to this one. Emptying the slot here means anything present afterwards
        # was written during THIS send, with no tx identity to plumb through the seam. The send is
        # synchronous on this thread, and a transport's own workers write their own thread's slot,
        # never ours.
        from sniper.execution.evm_real_seams import take_send_rejection
        take_send_rejection()
        if sender_dispatch_fn is not None:
            try:
                sender_dispatch_fn(tx_hash, quote)
            except Exception:
                pass                      # bounded measurement publication never decides a send
        if str(network_id) == "56":
            _trace_substage(trace, "evm.sender_dispatch", attempt_no=attempt_no, tx_sig=tx_hash)
        send_result = send_fn(raw)
        _trace_substage(trace, "evm.broadcast",
                        outcome=("rejected" if send_result is False else
                                 "accepted" if send_result else "ambiguous"),
                        attempt_no=attempt_no)
        if send_result is False:
            # THE NODE'S OWN WORDS, NOT A BARE CLASS (2026-09-17, PAWSINU 0xbcc948e7). A bare
            # `broadcast_rejected` cannot say whether the nonce, the gas or the balance refused the
            # swap, and those have different repairs.
            # Taken, never peeked: the slot was emptied immediately before the send, so a message
            # here belongs to it. None means the transport rejected without the node's words, and
            # the reason stays the bare class -- absent rather than wrong.
            detail = take_send_rejection()
            if float_used and not float_off:
                from sniper.execution.evm_native_float import insufficient_native
                if insufficient_native(detail):
                    # THE FLOAT WAS SHORT AFTER ALL (a wrong-high native read). The node refused, so
                    # nothing was admitted: give the nonce back, clear the binding, and run this
                    # entry once more on today's path, which unwraps first.
                    if release_nonce_fn is not None:
                        try:
                            release_nonce_fn(nonce)
                        except Exception:
                            pass
                    if not m.reset_entry_submission_after_failed_receipt(conn, pending_id, tx_hash):
                        return {"status": "unknown", "reason": "submission reset conflict",
                                "tx_hash": tx_hash, **provenance,
                                "price_attribution": partial_attribution,
                                **quote_fields(quote), **partial_drift}
                    float_off = True
                    _trace_substage(trace, "evm.native_float", outcome="fallback_unwrap",
                                    attempt_no=attempt_no)
                    continue
            return finish_with_cleanup("broadcast_rejected", {
                    "status": "failed",
                    "reason": ("broadcast_rejected: %s" % detail) if detail else "broadcast_rejected",
                    "tx_hash": tx_hash,
                    **provenance, "price_attribution": partial_attribution,
                    **quote_fields(quote), **partial_drift})
        trace_mark_submission(
            trace, "transaction_submitted", attempt_no,
            outcome="accepted" if send_result else "ambiguous",
        )
        status = confirm_fn(tx_hash)
        # Fill timing (2026-10-02): the block the receipt already carried (ReceiptState), else None.
        block = getattr(status, "block_number", None)
        _trace_substage(trace, "evm.confirmation", outcome=status, attempt_no=attempt_no,
                        block=block, tx_sig=tx_hash)
        confirmation_observed_at = None
        if status in ("confirmed", "failed"):
            confirmation_observed_at = trace_now(trace)
            confirmation_observed_at = trace_mark_submission(
                trace, "chain_confirmation", attempt_no, outcome=status, block=block,
                tx_sig=tx_hash,
            ) or confirmation_observed_at
        quote_age = quote_age_ms(
            trace, status, quote_observed_at=quote_observed_at,
            confirmation_observed_at=confirmation_observed_at,
        )

        if status == "failed":
            kind, revert_detail = "unknown", None
            if revert_reason_fn is not None:
                try:
                    kind, revert_detail = revert_reason_fn(tx_hash) or ("unknown", None)
                except Exception:
                    kind, revert_detail = "unknown", None
            _trace_substage(trace, "evm.revert_reason", attempt_no=attempt_no,
                            kind=kind, detail=revert_detail)
            if kind == "min_out" and attempt + 1 < _MAX_SWAP_ATTEMPTS:
                # QUOTE MOVEMENT, proven: retry once with a fresh quote. Reservation KEPT.
                if not m.reset_entry_submission_after_failed_receipt(
                        conn, pending_id, tx_hash):
                    return {"status": "unknown", "reason": "submission reset conflict",
                            "tx_hash": tx_hash, **provenance,
                            "price_attribution": partial_attribution,
                            **quote_fields(quote), **partial_drift}
                # A mined min-out uses the same winner retry as a simulated min-out. The failed
                # receipt and binding reset above are mandatory before another signature.
                if callable(quote.get("_requote")):
                    allow = (quote.get("issues") or {}).get("allowance") or {}
                    retry_spender = allow.get("spender") or (quote.get("transaction") or {}).get("to")
                    try:
                        retry_want = int(quote.get("sellAmount") or 0)
                    except (TypeError, ValueError):
                        retry_want = 0
                    retry_requote = quote["_requote"] if retry_spender and retry_want else None
                attempt += 1
                continue
            reason = "reverted_min_out" if kind == "min_out" else "reverted_unknown"
            return finish_with_cleanup(reason, {
                    "status": "failed", "reason": reason, "revert_detail": revert_detail,
                    "tx_hash": tx_hash, "quote_age_ms": quote_age,
                    **provenance, "price_attribution": partial_attribution,
                    **quote_fields(quote), **partial_drift})

        if status != "confirmed":
            # unknown / timeout: the tx may still land -> keep the reservation, open nothing,
            # NEVER resubmit (the nonce is spent; a resubmit reuses it and is rejected).
            return {"status": "unknown", "tx_hash": tx_hash, **provenance,
                    "price_attribution": partial_attribution,
                    **quote_fields(quote), **partial_drift}
        # 6. fill truth from the on-chain balance delta (never the quote). Re-read before
        # believing a zero delta — a real $5 Solana fill was booked 'anomaly' this exact way
        # (2026-07-10); an orphaned EVM entry would sit un-managed by the exit sweep.
        if sleep_fn is None:
            sleep_fn = time.sleep      # module-level import; a local one here shadowed the whole
                                       # enclosing scope and broke every earlier use of `time`
        # THE FLOAT PAID IN NATIVE, so WETH did not move: the spend is the swap's own `value` at the
        # quote's price, exact and read from nothing.
        native_spent = None
        if float_used:
            try:
                value = tx.get("value") or 0
                value = int(value, 16) if isinstance(value, str) else int(value)
                native_spent = value / (10 ** int(sell_decimals)) * float(sell_token_usd or 0.0)
            except (TypeError, ValueError):
                native_spent = None
        if require_receipt_amounts:
            # THE SWAP'S OWN RECEIPT (Alpha 2026-10-06, after Codex EVM-03): no wallet balance
            # window, which carried every other transfer that landed while we quoted, signed, sent
            # and confirmed. A native payment is the value of the transaction we signed, exact,
            # whether the float or a confirmed unwrap funded it (the unwrap converts, it costs nothing).
            spent = native_spent or _signed_value_usd(tx, sell_decimals, sell_token_usd) or 0.0
            got = 0.0
            quantity_source = "receipt_required"
        else:
            usdc_post, token_snapshot = _read_balances(usdc_fn, token_fn, mint)
            tok_post, post_decimals = balance_parts(token_snapshot)
            # A failed native-pool attempt can leave its WETH unwrap confirmed. The retry starts with
            # less WETH, so only the balance from before the first attempt gives the full entry cost.
            spent = native_spent if native_spent else entry_usdc_pre - usdc_post
            token_decimals = token_decimals if token_decimals is not None else post_decimals
            got = tok_post - entry_tok_pre
            quantity_source = "native_value" if native_spent else "balance_delta"

        def recover_receipt_amounts():
            nonlocal spent, got, token_decimals, quantity_source
            if got <= 0 and receipt_token_fn is not None:
                try:
                    receipt_value = receipt_token_fn(tx_hash, mint)
                    if receipt_value is not None:
                        receipt_got, receipt_decimals = balance_parts(receipt_value)
                        if receipt_got > 0:
                            got = receipt_got
                            token_decimals = (receipt_decimals if require_receipt_amounts
                                              else token_decimals if token_decimals is not None
                                              else receipt_decimals)
                            quantity_source = "receipt"
                except Exception:
                    pass
            if spent <= 0 and receipt_spent_fn is not None and tx_hash:
                try:
                    recovered = receipt_spent_fn(tx_hash)
                except Exception:
                    recovered = None
                if recovered and float(recovered) > 0:
                    # Receipt spend is in quote-token units; the booked spend is USD.
                    spent = float(recovered) * float(sell_token_usd or 1.0)
                    quantity_source += "+receipt_spend"

        # A confirmed receipt can book the fill now; a lagging balance must not leave
        # the bought bag without exit rails for three one-second sleeps.
        recover_receipt_amounts()
        for _ in range(3):
            if spent > 0 and got > 0:
                break
            sleep_fn(1.0)
            if require_receipt_amounts:      # a node a block behind: the same receipt, read again
                recover_receipt_amounts()
                continue
            usdc_post, token_snapshot = _read_balances(usdc_fn, token_fn, mint)
            tok_post, post_decimals = balance_parts(token_snapshot)
            if spent <= 0:
                spent = native_spent if native_spent else entry_usdc_pre - usdc_post
            if got <= 0:
                got = tok_post - entry_tok_pre
            token_decimals = token_decimals if token_decimals is not None else post_decimals
        if spent <= 0 or got <= 0:
            recover_receipt_amounts()  # preserve the late-receipt fallback after bounded reads
        _trace_substage(trace, "evm.balance_delta",
                        outcome="positive" if spent > 0 and got > 0 else "missing",
                        quantity_source=quantity_source,
                        attempt_no=attempt_no)
        trace_mark(trace, "balance_delta")
        quote = bind_quote_metadata(
            quote, network_id=network_id, mint=mint, quote_observed_at=quote_observed_at,
            sell_decimals=sell_decimals, buy_decimals=token_decimals,
            sell_token_usd=sell_token_usd,
        )
        if spent <= 0 or got <= 0:
            return {"status": "anomaly", "tx_hash": tx_hash, "spent": spent, "got": got,
                    "quote_age_ms": quote_age, **provenance,
                    "price_attribution": partial_attribution,
                    **quote_fields(quote), **buy_drift(
                        pending_source_price, quote_fields(quote)["quote_price"], None)}
        fill_price = spent / got
        # capture signal-time market cap (median of recent feed readings) so real EVM positions carry
        # entry_mcap like paper arms — was 100% NULL on real, blinding per-mcap exit/policy analytics.
        # + execution-quality: how far our on-chain fill drifted from the trader's price we copied.
        their_px = positive_price(pending.get("source_price"))
        if their_px is not None:
            price_attribution = "exact_event"
        else:
            their_px = positive_price(m.resolve_mint_their_price(
                conn, mint, network_id=network_id, source_user_id=source_user_id))
            price_attribution = "legacy_source_chain" if their_px is not None else "unavailable"
        quote_data = quote_fields(quote)
        drift = buy_drift(their_px, quote_data["quote_price"], fill_price)
        gap_pct = drift["total_copy_drift_pct"]
        entry_mcap = positive_price(pending.get("source_market_cap"))
        if entry_mcap is None:
            entry_mcap = m.resolve_mint_mcap(conn, mint, network_id=network_id,
                                             price=fill_price)
        # ONE TRANSACTION: position, entry fill and the reservation's confirmation (Beta,
        # 2026-09-29, Ink Task 8 crash matrix). They were three commits. A process that died after
        # the position committed left a position with no fill while the reservation still held
        # the swap's hash, and on Ink the restart's receipt recovery then tried to book the same
        # swap again (the open-position unique index refused it, and the row went orphaned).
        # Nothing slow runs inside: the rebuild request and the descriptive executed-pool receipt
        # read (up to the hedged 12 s budget) moved after the commit.
        if conn.in_transaction:
            conn.commit()              # earlier writes of this attempt, as open_position used to
        conn.execute("BEGIN IMMEDIATE")
        try:
            # BOOKED ALREADY? (2026-09-30, Ink double booking). The receipt recovery
            # (`recover_confirmed_ink_entry`) may book this swap while this thread is still in its
            # confirmation wait or its balance reads. If that position has since CLOSED, the
            # open-position unique index no longer stops a second position for the same swap, and
            # a failed `confirm_pending` below is only a warning: the ledger would hold a position
            # with no bag. One indexed lookup inside this transaction; the swap is already mined.
            booked = m.entry_fill_for_attempt(conn, pending.get("attempt_id"), tx_hash)
            if booked is not None:
                conn.rollback()
                import logging
                logging.getLogger(__name__).warning(
                    "entry already booked from its receipt: reservation=%s position=%s tx=%s; "
                    "not booked a second time", pending_id, booked["position_id"], tx_hash)
                return {"status": "filled", "position_id": booked["position_id"],
                        "fill_id": booked["id"], "fill_price": booked["price"],
                        "notional": booked["notional"], "tx_hash": tx_hash,
                        "booked_by": "receipt_recovery", "source_price": their_px,
                        "price_attribution": price_attribution, "quote_age_ms": quote_age,
                        **provenance, **quote_data, **drift}
            pid = m.open_position(
                conn, source_user_id=source_user_id, source_handle="(real)", mint=mint, ticker="",
                entry_price=fill_price, entry_notional=spent, entry_fill_source="chain",
                # Our read lag for the source event, not a literal -1.0 — see resolve_source_lag_s.
                entry_lag_s=m.resolve_source_lag_s(conn, pending.get("origin_event_id")),
                # The SOURCE trader's size, not a literal 0.0 — see resolve_source_size_usd.
                their_size_usd=m.resolve_source_size_usd(
                    conn, pending.get("origin_event_id"), network_id=pending.get("network_id"),
                    mint=pending.get("mint"), source_user_id=pending.get("source_user_id")),
                entry_liquidity=pending.get("source_liquidity"),
                entry_token_age_s=pending.get("source_token_age_s"),
                opened_at=now_iso, strategy=strategy,
                execution_mode="real", network_id=network_id,
                signal_source=pending.get("signal_source") or "global_feed",
                origin_event_id=pending.get("origin_event_id"), entry_mcap=entry_mcap,
                their_price=their_px, fill_gap_pct=gap_pct, attempt_id=pending.get("attempt_id"),
                price_attribution=price_attribution, quote_age_ms=quote_age,
                evm_route_json=(json.dumps(quote.get("route"), sort_keys=True, separators=(",", ":"))
                                if isinstance(quote.get("route"), dict) else None),
                **contract_fields, **quote_data, **drift,
                commit=False)
            fill_id = m.insert_fill(
                conn, position_id=pid, kind="entry", price=fill_price, qty=got,
                notional=spent, fill_source="chain", fraction=1.0, created_at=now_iso,
                tx_sig=tx_hash, source_price=their_px, price_attribution=price_attribution,
                quote_age_ms=quote_age, **provenance, **quote_data, **drift, commit=False)
            if not m.confirm_pending(conn, pending_id, commit=False):
                import logging
                logging.getLogger(__name__).warning(
                    "entry fill booked but reservation %s was not 'reserved'", pending_id)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        try:
            # A funded position just opened. The hot lane's book is fixed for a 30-second epoch, so
            # without this the position is unpriced -- and unprotected by every rail -- for up to
            # 30s. Measured: open to first mark p50 11.4s, p90 28.0s on 4663, only 12% inside 5s.
            # Setting a flag; the lane ends its own epoch. Never blocks or fails an entry.
            from sniper.services.memecoin_hot_supervisor import request_rebuild
            request_rebuild()
        except Exception:
            pass
        if booked_fn is not None:
            # TOKEN PRE-APPROVAL (spec 2026-09-30 4663 approve at entry). Starts a daemon thread and
            # returns; the entry never joins it. After the commit, so the thread's own connection
            # sees the booked position, and never able to fail the fill.
            try:
                booked_fn(pid)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("pre-approval not started for position %s", pid)
        if require_receipt_amounts:
            receipt_vs_delta_later(token_fn, mint, entry_tok_pre, got, pid, tx_hash)
        # THE POOL THE FILL ACTUALLY TOUCHED (Beta 76ea7130, wired 2026-09-22). Read from our own
        # receipt (the pool that sent most of the coin on a split route, Alpha 2026-10-17; else the
        # last Swap), stamped write-once on the position. Never an entry, size or sign input, but the
        # tick stream marks on it and volume_death counts it as an own pool; a failure here never
        # touches the fill. AFTER request_rebuild:
        # the receipt read can take up to the hedged budget (12 s) on a lagging node, and the
        # rebuild request is what ends the window in which the new position has no rail.
        if receipt_fn is not None and tx_hash:
            try:
                from sniper.services.memecoin_executed_pool import executed_pool_from_receipt
                pool, source, hops = executed_pool_from_receipt(network_id, receipt_fn(tx_hash), mint=mint)
                if pool:
                    m.record_executed_pool(conn, pid, pool, source)
                _trace_substage(trace, "evm.executed_pool", pool=pool, source=source, hops=hops)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("executed pool not recorded for position %s", pid)
        _trace_substage(trace, "evm.persistence", outcome="complete", attempt_no=attempt_no)
        trace_mark(trace, "persistence")
        return {"status": "filled", "position_id": pid, "fill_id": fill_id,
                "fill_price": fill_price, "notional": spent, "tx_hash": tx_hash,
                "source_price": their_px, "price_attribution": price_attribution,
                "quote_age_ms": quote_age, **provenance, **quote_data, **drift}
