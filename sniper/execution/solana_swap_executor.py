"""Real Solana entry executor (S2, DISARMED build).

`execute_entry` is pure orchestration over injected seams (quote / sign / send / confirm / balance
reads) so every money-safety branch is unit-tested without a live endpoint. The safety contract:

  1. quote first; ABORT if price impact exceeds the hard cap or the planned sell route is absent.
  2. record pre-balances from CHAIN.
  3. sign; bind the signed identity before broadcast (full payload+validity on the shared backend,
     signature-only on the explicit legacy seam)
     (the write-ahead protocol that makes a crashed reservation reconcilable).
  4. send exactly once; confirm by signature poll.
  5. confirmed -> price the fill from the on-chain balance DELTA (never the quote), open a
     execution_mode='real' position + record the fill with its tx_sig, and confirm the reservation.
     failed -> release the reservation. unknown/timeout -> LEAVE the reservation, open nothing,
     and NEVER resubmit (the tx may still land; resubmitting is the double-spend).

NOT integration-tested. A live smoke test (item 22) MUST precede any arm. Requires solders for the
real sign/send helpers (not used by execute_entry's injected tests).
"""
from __future__ import annotations

import base64
import json
import logging
import math
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from sniper.execution.entry_attribution import (
    balance_parts, bind_quote_metadata, buy_drift, positive_price, quote_age_ms, quote_fields,
    quote_unit_price, trace_mark, trace_mark_submission,
)
from sniper.execution.solana_wallet import USDC_MINT

SOL_MINT = "So11111111111111111111111111111111111111112"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
# The two assets a Solana funded exit can settle through. A pump.fun curve quoted in anything else
# has no sell Jupiter can build today (see execute_entry 1b').
_SETTLEMENT_MINTS = frozenset({SOL_MINT, USDC_MINT})


def signed_priority_fee_lamports(signed_b64):
    """Requested priority fee from the signed compute budget; None when proof is unavailable."""
    try:
        from solders.transaction import VersionedTransaction
        message = VersionedTransaction.from_bytes(base64.b64decode(signed_b64, validate=True)).message
        limit = price = None
        for ix in message.instructions:
            if str(message.account_keys[ix.program_id_index]) != COMPUTE_BUDGET_PROGRAM:
                continue
            data = bytes(ix.data)
            if len(data) == 5 and data[0] == 2:
                if limit is not None:
                    return None
                limit = int.from_bytes(data[1:], "little")
            elif len(data) == 9 and data[0] == 3:
                if price is not None:
                    return None
                price = int.from_bytes(data[1:], "little")
        if limit is None or price is None:
            return None
        return (limit * price + 999_999) // 1_000_000
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def _oversize_retry_exclusions(quote):
    """The venue of the route's FIRST hop, for the oversize retry to exclude, or ().

    WHY (2026-09-14, "human" ref 7b9a3454). Every oversized buy with a recorded route (4 of 4 since
    09-12) paid USDC -> SOL through HumidiFi, whose accounts are not in Jupiter's lookup tables:
    1,473 bytes against the 1,232 cap. `maxAccounts=48` alone kept HumidiFi (1,442 bytes) and on
    live rebuilds often steered the route INTO it. The same buys with HumidiFi excluded built at
    684-1,149 bytes on 6 live coins for 0.1% fewer tokens or less. The first hop leaves USDC, the
    deepest market on the chain, so it always has a substitute. The last hop is the coin's own
    pool and is never excluded; a first hop that shares its label is kept too.
    """
    legs = [(leg or {}).get("swapInfo") or {} for leg in (quote or {}).get("routePlan") or ()]
    if len(legs) < 2:
        return ()
    first, last = str(legs[0].get("label") or ""), str(legs[-1].get("label") or "")
    return (first,) if first and first != last else ()


def unbuildable_curve_quote_asset(quote, is_restricted=None):
    """The output mint of a Pump.fun hop that pays out something Jupiter cannot then handle, or
    None. A missing or non-Jupiter-shaped quote (the Raydium venue fallback carries no routePlan)
    answers None, which keeps every other gate exactly as it was — this refuses one named shape,
    not the unknown.

    NARROWED 2026-09-12, then again 2026-09-13 (operator: "fix it", twice). The payout asset's
    RESTRICTING Token-2022 extensions are the discriminator. fries paid MCDx (permanentDelegate,
    defaultAccountState, pausableConfig, confidentialTransferMint, scaledUiAmountConfig, plus a
    transferHook) and Jupiter failed at the ATA create for it. FSTR paid Fartcoin (classic SPL) and
    {CAT} paid PUMP (Token-2022 with only a transferHook); for both, Jupiter built the sell for a
    real holder and simulateTransaction succeeded. `is_restricted(mint)` answers True / False /
    None; only a proven unrestricted mint (False) passes. Without the reader, or when it cannot
    read, the payout is refused as before.
    """
    if not isinstance(quote, dict):
        return None
    for leg in quote.get("routePlan") or ():
        info = (leg or {}).get("swapInfo") or {}
        if str(info.get("label") or "") != "Pump.fun":
            continue
        out = str(info.get("outputMint") or "")
        if out and out not in _SETTLEMENT_MINTS:
            if is_restricted is not None:
                try:
                    classic = is_restricted(out) is False
                except Exception:
                    classic = False
                if classic:
                    continue
            return out
    return None
from sniper.persistence import db_memecoin as m


def recover_confirmed_entry(conn, pending, receipt, *, owner, now_iso):
    """Create the missing funded position from one confirmed Solana transaction receipt."""
    if str(pending.get("network_id") or "solana") != "solana" or not pending.get("attempt_id"):
        return None
    if pending.get("funding_asset") == "sol":
        try:
            sol_usd = float(pending["funding_sol_usd"])
            maximum_lamports = int(pending["funding_amount_atomic"])
            if not math.isfinite(sol_usd) or sol_usd <= 0 or maximum_lamports <= 0:
                return None
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        proof = receipt_sol_entry_fill(
            receipt, signature=pending.get("tx_sig"), owner=owner, mint=pending.get("mint"),
            maximum_lamports=maximum_lamports)
        spent = (proof["sol_spent_lamports"] / 1e9 * sol_usd) if proof else None
        fill = ((spent, proof["got"], proof["decimals"])
                if spent is not None and spent <= float(pending["reserved_notional_usd"]) + 0.01
                else None)
    elif pending.get("funding_asset") in (None, "usdc"):
        fill = receipt_entry_fill(
            receipt, signature=pending.get("tx_sig"), owner=owner, mint=pending.get("mint"),
            maximum=pending.get("reserved_notional_usd"))
    else:
        return None
    if fill is None:
        return None
    spent, got, _decimals = fill
    try:
        opened_at = datetime.fromtimestamp(
            int(receipt["blockTime"]), tz=timezone.utc).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None

    contract_fields = m.validated_pending_funded_contract(conn, pending)
    their_price = positive_price(pending.get("source_price"))
    if their_price is None:
        their_price = positive_price(m.resolve_mint_their_price(
            conn, pending["mint"], network_id="solana",
            source_user_id=pending.get("source_user_id")))
    attribution = "exact_event" if their_price is not None else "unavailable"
    fill_price = spent / got
    drift = buy_drift(their_price, None, fill_price)
    entry_mcap = positive_price(pending.get("source_market_cap"))
    if entry_mcap is None:
        # PRICED AT OUR FILL (2026-09-18). A median of the window's readings records the
        # coin at an earlier price, and band policy reads this number.
        entry_mcap = m.resolve_mint_mcap(conn, pending["mint"], network_id="solana",
                                         price=fill_price)

    if conn.in_transaction:
        raise RuntimeError("confirmed entry recovery needs a clean transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = m.pending_row(conn, pending["id"])
        if (not current or current.get("status") != "reserved"
                or current.get("tx_sig") != pending.get("tx_sig")):
            conn.rollback()
            return None
        positions = conn.execute(
            "SELECT * FROM memecoin_positions WHERE execution_mode='real' AND attempt_id=?",
            (pending["attempt_id"],),
        ).fetchall()
        if len(positions) > 1:
            raise RuntimeError("confirmed entry recovery found duplicate positions")
        if positions:
            position_id = positions[0]["id"]
        else:
            # The attempt's own handle (73884 read '(real)' after its anomaly was recovered).
            position_id = m.open_position(
                conn, source_user_id=pending.get("source_user_id"),
                source_handle=m.entry_attempt_source_handle(conn, pending["attempt_id"]) or "(real)",
                mint=pending["mint"], ticker="", entry_price=fill_price,
                entry_notional=spent, entry_fill_source="chain",
                entry_lag_s=m.resolve_source_lag_s(conn, pending.get("origin_event_id")),
                their_size_usd=m.resolve_source_size_usd(
                    conn, pending.get("origin_event_id"), network_id=pending.get("network_id"),
                    mint=pending.get("mint"), source_user_id=pending.get("source_user_id")),
                entry_liquidity=pending.get("source_liquidity"),
                entry_token_age_s=pending.get("source_token_age_s"),
                entry_mcap=entry_mcap, opened_at=opened_at,
                signal_source=pending.get("signal_source") or "global_feed",
                strategy=pending.get("strategy") or "alpha_b",
                origin_event_id=pending.get("origin_event_id"), execution_mode="real",
                network_id="solana", their_price=their_price,
                fill_gap_pct=drift["total_copy_drift_pct"], attempt_id=pending["attempt_id"],
                price_attribution=attribution, cluster_decision_id=pending.get("cluster_decision_id"),
                **contract_fields, **drift, commit=False)
            try:
                # A funded position just opened. The hot lane's book is fixed for a 30-second epoch, so
                # without this the position is unpriced -- and unprotected by every rail -- for up to
                # 30s. Measured: open to first mark p50 11.4s, p90 28.0s on 4663, only 12% inside 5s.
                # Setting a flag; the lane ends its own epoch. Never blocks or fails an entry.
                from sniper.services.memecoin_hot_supervisor import request_rebuild
                request_rebuild()
            except Exception:
                pass
        fills = conn.execute(
            "SELECT * FROM memecoin_fills WHERE kind='entry' AND attempt_id=?",
            (pending["attempt_id"],),
        ).fetchall()
        if len(fills) > 1:
            raise RuntimeError("confirmed entry recovery found duplicate fills")
        if not fills:
            m.insert_fill(
                conn, position_id=position_id, kind="entry", price=fill_price, qty=got,
                notional=spent, fill_source="chain", fraction=1.0, created_at=opened_at,
                tx_sig=pending.get("tx_sig"), attempt_id=pending["attempt_id"],
                **({"funding_asset": "sol", "funding_spent_atomic": proof["sol_spent_lamports"],
                    "funding_sol_usd": sol_usd, "network_fee_lamports": proof["fee_lamports"],
                    "priority_fee_lamports": signed_priority_fee_lamports(
                        pending.get("signed_payload_b64")),
                    "rent_lamports": proof["rent_lamports"], "returned_rent_lamports": 0}
                   if pending.get("funding_asset") == "sol" else {}),
                origin_event_id=pending.get("origin_event_id"), network_id="solana",
                signal_source=pending.get("signal_source") or "global_feed",
                source_price=their_price, price_attribution=attribution, **drift, commit=False)
        if not m.confirm_pending(conn, pending["id"], commit=False):
            raise RuntimeError("confirmed entry recovery lost the reservation")
        m.link_entry_attempt_position(conn, pending["attempt_id"], position_id)
        conn.commit()
        return position_id
    except Exception:
        conn.rollback()
        raise


def _token_snapshot_parts(value):
    atomic = getattr(value, "atomic", None)
    decimals = getattr(value, "decimals", None)
    if atomic is not None:
        human = getattr(value, "human", 0.0)
        return float(human), decimals, int(atomic)
    human, decimals = balance_parts(value)
    return human, decimals, None


def _token_delta(pre, post):
    pre_human, pre_decimals, pre_atomic = pre
    post_human, post_decimals, post_atomic = post
    if pre_atomic is None or post_atomic is None:
        if (pre_decimals is not None and post_decimals is not None
                and pre_decimals != post_decimals):
            return None, None
        return post_human - pre_human, pre_decimals or post_decimals
    if pre_decimals is None and pre_atomic != 0:
        return None, None
    if post_decimals is None and post_atomic != 0:
        return None, None
    if (pre_decimals is not None and post_decimals is not None
            and pre_decimals != post_decimals):
        return None, None
    decimals = pre_decimals if pre_decimals is not None else post_decimals
    if decimals is None:
        return None, None
    return (post_atomic - pre_atomic) / (10 ** decimals), decimals


CURVE_ROUTE_REFUSAL = "route_via_completed_curve"


def route_legs(quote):
    """[(label, ammKey)] for a Jupiter quote, or [] for a venue that carries no routePlan."""
    out = []
    if not isinstance(quote, dict):
        return out
    for leg in quote.get("routePlan") or ():
        info = (leg or {}).get("swapInfo") or {}
        out.append((str(info.get("label") or ""), str(info.get("ammKey") or "")))
    return out


def completed_curve_in_route(quote, curve_state_fn):
    """The ammKey of a DRAINED pump.fun bonding curve this route runs through, or None.

    WHY THE LABEL IS NOT ENOUGH. Jupiter labels the bonding curve `Pump.fun` and the
    post-graduation pool `Pump.fun Amm`, so the label finds the CANDIDATE without an RPC call --
    but a live, un-graduated curve is a perfectly good venue and routes through it every day. Only
    the curve's own state separates "this coin has not graduated yet" from "this coin graduated
    and the corpse is all Jupiter can still see". So exactly one account is read, and only after
    the impact cap has already refused, so a successful entry never pays for it.

    Fail-closed: no reader, an unreadable account, or any exception answers None, which leaves the
    ordinary impact refusal exactly as it was. This names one shape; it never invents one.
    """
    if curve_state_fn is None:
        return None
    from sniper.settings import memecoin as cfg
    if not getattr(cfg, "MEMECOIN_SOL_CURVE_GRADUATION_RERESOLVE", False):
        return None
    floor = float(getattr(cfg, "MEMECOIN_SOL_CURVE_DRAINED_SOL", 0.5) or 0.5)
    for label, amm_key in route_legs(quote):
        if label != "Pump.fun" or not amm_key:      # "Pump.fun Amm" is the graduated pool
            continue
        try:
            state = curve_state_fn(amm_key)
        except Exception:
            return None
        if not isinstance(state, dict):
            return None
        if state.get("complete") is True:
            return amm_key
        sol = state.get("sol")
        if sol is not None and float(sol) < floor:
            return amm_key
    return None


def _post_peak_confirmation(contract_fields, exact_price, trace):
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
            nominated=result.nominated,
            confirmed=result.confirmed,
            reason=result.reason,
            peak_price=result.peak_price,
            display_price=result.display_price,
            exact_price=result.exact_price,
            exact_giveback_pct=result.exact_giveback_pct,
        )
    return result


def _trace_substage(trace, name, **metadata):
    marker = getattr(trace, "mark_substage", None) if trace is not None else None
    if marker is None:
        return
    try:
        marker(name, **metadata)
    except Exception:
        pass


def _timed(trace, name, call, *args, **kwargs):
    """Run `call`, record how long it took as its own substage mark, return what it returned.

    WHY THIS EXISTS. `signing_ms` is not a sub-measure of the entry: measured over 277 solana fills
    in the 14 days to 2026-09-24, quote_completed_at -> signing_completed_at is p50 405 ms / p90
    915 ms and `signing_ms` on the same rows is p50 405 / p90 915 -- the same window. Everything
    inside it was UNATTRIBUTED, because the three substages that fire there (roundtrip_cap 0.05 ms,
    post_peak_confirmation 0.03 ms, chase_quote_confirmation 0.02 ms) are decision marks, not
    timers. No amount of re-reading the traces could split that 405 ms.

    TIMING ONLY. The call is invoked exactly as before, its result is returned untouched, and an
    exception propagates after the mark is written -- a call that raised is still a call that spent
    time, and dropping its duration would bias the very number this exists to measure.
    `mark_substage` takes an explicit `duration_ms`, so the measured value is recorded rather than
    the gap-since-last-mark it would otherwise infer.
    """
    started = time.monotonic()
    try:
        return call(*args, **kwargs)
    finally:
        _trace_substage(trace, name, duration_ms=(time.monotonic() - started) * 1000.0)


def _read_pre_balances(trace, usdc_fn, token_fn, mint):
    """(usdc_fn(), token_fn(mint)) run concurrently; see execute_entry step 2."""
    from sniper.execution.evm_swap_executor import _read_balances
    times = {}

    def timed(name, call):
        def run(*args):
            started = time.monotonic()
            try:
                return call(*args)
            finally:
                times[name] = (time.monotonic() - started) * 1000.0
        return run

    started = time.monotonic()
    try:
        return _read_balances(timed("timing_usdc_read", usdc_fn),
                              timed("timing_token_read", token_fn), mint)
    finally:
        for name in ("timing_usdc_read", "timing_token_read"):
            if name in times:
                _trace_substage(trace, name, duration_ms=times[name])
        _trace_substage(trace, "timing_balance_reads",
                        duration_ms=(time.monotonic() - started) * 1000.0)


class _PreBalanceReads:
    """usdc_fn() and token_fn(mint) started at the top of execute_entry, joined at its step 2.

    WHY (2026-09-27, Alpha GO "L2"): the two reads ran after every price gate and cost p50 112.7 /
    p90 138.7 ms (n=8 first attempts) on top of the forward quote, the reverse quote and the
    round-trip check, which together take about as long. Started with the quote, they are mostly
    hidden (modelled p50 109.8 ms saved).

    STILL LIVE READS. They are the fill-delta baseline, so they must start inside the wallet lock
    (serialize_executor holds it around the whole execute_fn), where no actor of ours can move the
    wallet between this baseline and our fill. Never start them earlier than execute_entry.

    Two DAEMON threads per attempt and never more. An attempt that aborts before the join simply
    drops this object: nothing waits on the threads, and a read that fails later is never raised.
    At the join a failed read raises on the caller exactly as the old helper did, usdc first.
    EntryTrace.mark_substage is not thread-safe, so the workers only time and the marks are written
    by the caller after the join, under the old names, plus timing_balance_wait: how long the entry
    actually blocked on the reads."""

    NAMES = ("timing_usdc_read", "timing_token_read")

    def __init__(self, usdc_fn, token_fn, mint):
        import threading
        self._started = time.monotonic()
        self._results = {}
        self._threads = [
            threading.Thread(target=self._run, args=(self.NAMES[0], usdc_fn), daemon=True,
                             name="entry-pre-read-usdc"),
            threading.Thread(target=self._run, args=(self.NAMES[1], token_fn, mint), daemon=True,
                             name="entry-pre-read-token"),
        ]
        for thread in self._threads:
            thread.start()

    def _run(self, name, call, *args):
        started = time.monotonic()
        value = error = None
        try:
            value = call(*args)
        except BaseException as exc:                 # re-raised on the caller at join()
            error = exc
        self._results[name] = (value, error, started, time.monotonic())

    def token_decimals_if_done(self):
        """The token read's decimals when it already finished cleanly, else None. Never waits: the
        caller holds the wallet lock, so a wait here would delay the next entry on this wallet."""
        got = self._results.get(self.NAMES[1])
        if not got or got[1] is not None:
            return None
        try:
            decimals = _token_snapshot_parts(got[0])[1]
            return int(decimals) if decimals is not None and 0 <= int(decimals) <= 30 else None
        except (TypeError, ValueError):
            return None

    def join(self, trace):
        waiting = time.monotonic()
        for thread in self._threads:
            thread.join()
        waited_ms = (time.monotonic() - waiting) * 1000.0
        for name in self.NAMES:
            _value, _error, started, done = self._results[name]
            _trace_substage(trace, name, duration_ms=(done - started) * 1000.0)
        last = max(self._results[name][3] for name in self.NAMES)
        _trace_substage(trace, "timing_balance_reads",
                        duration_ms=(last - self._started) * 1000.0)
        _trace_substage(trace, "timing_balance_wait", duration_ms=waited_ms)
        for name in self.NAMES:
            if self._results[name][1] is not None:
                raise self._results[name][1]
        return tuple(self._results[name][0] for name in self.NAMES)


def _payload_bytes(signed_b64):
    """Serialized size of a signed transaction, or None when it cannot be decoded. A size we
    cannot read must never look like a size under the limit, so the caller treats None as
    'do not retry' and leaves the send seam's own guard to decide."""
    try:
        return len(base64.b64decode(signed_b64))
    except Exception:
        return None


from time import monotonic as _receipt_clock   # execute_entry imports `time` locally

ENTRY_RECEIPT_READS = 4             # a confirmed entry can lag getTransaction at 'confirmed' by a beat
ENTRY_RECEIPT_READ_SLEEP_S = 0.25


def _verified_receipt_fill(receipt_fill_fn, sig, mint, maximum):
    """Return a finite receipt fill within the reserved spend, or None."""
    if receipt_fill_fn is None:
        return None
    try:
        spent, got, decimals = receipt_fill_fn(sig, mint, maximum)
        spent = float(spent)
        got = float(got)
        decimals = int(decimals)
    except (TypeError, ValueError, OverflowError, ArithmeticError):
        return None
    except Exception:
        # A read that raised saw nothing (2026-09-27, Alpha GO F1): unreadable, so the confirmed buy
        # stays reserved for recovery instead of escaping the executor. Stamped with its traceback.
        logger.warning("%s entry receipt read raised sig=%s; treated as unreadable",
                       datetime.now(timezone.utc).isoformat(), sig, exc_info=True)
        return None
    if (not math.isfinite(spent) or not math.isfinite(got)
            or spent <= 0 or got <= 0 or spent > float(maximum)
            or decimals < 0 or decimals > 255):
        return None
    return spent, got, decimals


def receipt_entry_fill(tx, *, signature, owner, mint, maximum):
    """Return a verified Solana entry receipt fill, or None on any mismatch."""
    try:
        transaction = tx["transaction"]
        message = transaction["message"]
        account = message["accountKeys"][0]
        if (not isinstance(account, dict) or account.get("pubkey") != owner
                or account.get("signer") is not True
                or not signature or transaction.get("signatures", [None])[0] != signature):
            return None
        meta = tx["meta"]
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None
    return receipt_entry_balance_delta(meta, owner=owner, mint=mint, maximum=maximum)


def receipt_sol_entry_fill(tx, *, signature, owner, mint, maximum_lamports, tip_lamports=0):
    """Native-funded token cost from one signed receipt, excluding network fee, new ATA rent and the
    Sender A/B tip (`tip_lamports`, the one transfer the pre-sign guard proved the message carries):
    a curve buy spends its exact lamports, so a tip left in would read as spend over the maximum."""
    try:
        transaction = tx["transaction"]
        keys = transaction["message"]["accountKeys"]
        if (not keys or not isinstance(keys[0], dict) or keys[0].get("pubkey") != owner
                or keys[0].get("signer") is not True
                or signature not in transaction["signatures"]):
            return None
        meta = tx["meta"]
        if meta["err"] is not None:
            return None
        fee = meta["fee"]
        pre, post = meta["preBalances"], meta["postBalances"]
        before, after = meta["preTokenBalances"], meta["postTokenBalances"]
        maximum = int(maximum_lamports)
        if (isinstance(fee, bool) or not isinstance(fee, int) or fee < 0
                or maximum <= 0 or len(pre) != len(post) or len(pre) < len(keys)
                or not isinstance(before, list) or not isinstance(after, list)):
            return None
        def target_entries(entries):
            found = {}
            for entry in entries:
                if entry.get("mint") != mint or entry.get("owner") != owner:
                    continue
                index = entry["accountIndex"]
                amount = entry["uiTokenAmount"]["amount"]
                places = entry["uiTokenAmount"]["decimals"]
                if (isinstance(index, bool) or not isinstance(index, int)
                        or index < 0 or index >= len(pre) or index in found
                        or isinstance(amount, bool) or isinstance(places, bool)):
                    raise ValueError("invalid token balance")
                amount, places = int(amount), int(places)
                if amount < 0 or places < 0 or places > 255:
                    raise ValueError("invalid token amount")
                found[index] = (amount, places)
            return found
        old, new = target_entries(before), target_entries(after)
        places = {p for _a, p in (*old.values(), *new.values())}
        if len(places) != 1:
            return None
        decimals = places.pop()
        got_atomic = sum(a for a, _p in new.values()) - sum(a for a, _p in old.values())
        before_owned = {entry["accountIndex"] for entry in before
                        if entry.get("owner") == owner}
        rent = 0
        for entry in after:
            if entry.get("owner") != owner:
                continue
            index = entry["accountIndex"]
            if index in before_owned:
                continue
            if int(pre[index]) != 0 or entry.get("mint") not in (mint, SOL_MINT):
                return None
            if entry.get("mint") == SOL_MINT and int(entry["uiTokenAmount"]["amount"]) != 0:
                return None
            rent += int(post[index])
        tip = int(tip_lamports or 0)
        spent = int(pre[0]) - int(post[0]) - fee - rent - tip
        if got_atomic <= 0 or rent < 0 or tip < 0 or spent <= 0 or spent > maximum:
            return None
        return {"sol_spent_lamports": spent, "got": got_atomic / 10 ** decimals,
                "decimals": decimals, "fee_lamports": fee, "rent_lamports": rent,
                **({"tip_lamports": tip} if tip else {})}
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, AttributeError):
        return None


def receipt_exit_proceeds(tx, *, signature, owner):
    """Return the exact owner USDC increase from one successful signed transaction."""
    try:
        transaction = tx["transaction"]
        account = transaction["message"]["accountKeys"][0]
        if (not isinstance(account, dict) or account.get("pubkey") != owner
                or account.get("signer") is not True
                or signature not in transaction.get("signatures", ())):
            return None
        meta = tx["meta"]
        if not isinstance(meta, dict) or "err" not in meta or meta["err"] is not None:
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None

    delta = 0
    for entries, sign in ((meta.get("preTokenBalances"), -1),
                          (meta.get("postTokenBalances"), 1)):
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            if entry.get("mint") != USDC_MINT or entry.get("owner") != owner:
                continue
            value = entry.get("uiTokenAmount")
            if not isinstance(value, dict) or value.get("decimals") != 6:
                return None
            try:
                amount = int(value.get("amount"))
            except (TypeError, ValueError, OverflowError):
                return None
            if amount < 0:
                return None
            delta += sign * amount
    return delta / 1_000_000.0 if delta > 0 else None


def receipt_exit_fill(tx, *, signature, owner, mint):
    """Exact USDC proceeds and sold mint atoms from the requested signed receipt."""
    try:
        transaction = tx['transaction']
        keys = transaction['message']['accountKeys']
        if (not signature or not owner or not mint or transaction['signatures'][0] != signature or mint == USDC_MINT
                or not isinstance(keys[0], dict) or keys[0].get('pubkey') != owner
                or keys[0].get('signer') is not True):
            return None
        snapshots = []
        for field in ('preTokenBalances', 'postTokenBalances'):
            entries = tx['meta'][field]
            if not isinstance(entries, list):
                return None
            balances = {}
            for value in entries:
                index, amount = value['accountIndex'], value['uiTokenAmount']['amount']
                decimals = value['uiTokenAmount']['decimals']
                if (type(index) is not int or not 0 <= index < len(keys) or index in balances
                        or not isinstance(amount, (str, int)) or isinstance(amount, bool)
                        or type(decimals) is not int
                        or not 0 <= decimals <= 255):
                    return None
                amount = int(amount)
                if not 0 <= amount < 2 ** 64:
                    return None
                balances[index] = (value.get('mint'), value.get('owner'), amount, decimals)
            snapshots.append(balances)
        before, after = snapshots
        for index in before.keys() & after.keys():
            if before[index][:2] != after[index][:2] or before[index][3] != after[index][3]:
                return None
        old = [value for value in before.values() if value[:2] == (mint, owner)]
        new = [value for value in after.values() if value[:2] == (mint, owner)]
        decimals = {value[3] for value in old + new}
        sold_atomic = sum(value[2] for value in old) - sum(value[2] for value in new)
        received = receipt_exit_proceeds(tx, signature=signature, owner=owner)
        if sold_atomic <= 0 or len(decimals) != 1 or received is None:
            return None
        return {'received': received, 'sold_atomic': sold_atomic, 'decimals': decimals.pop()}
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, AttributeError):
        return None


def receipt_entry_balance_delta(meta, *, owner, mint, maximum):
    """Return the owner token deltas from a confirmed Solana transaction receipt."""
    try:
        if not isinstance(meta, dict) or "err" not in meta or meta["err"] is not None:
            return None
        maximum_atomic = int(round(float(maximum) * 1_000_000))
        if maximum_atomic <= 0 or not math.isfinite(float(maximum)) or mint == USDC_MINT:
            return None
    except (TypeError, ValueError, OverflowError):
        return None

    totals = {}
    decimals = {}
    for entries, sign in ((meta.get("preTokenBalances"), -1),
                          (meta.get("postTokenBalances"), 1)):
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            token = entry.get("mint")
            if token not in (USDC_MINT, mint) or entry.get("owner") != owner:
                continue
            value = entry.get("uiTokenAmount")
            if not isinstance(value, dict):
                return None
            amount, places = value.get("amount"), value.get("decimals")
            if isinstance(amount, bool) or isinstance(places, bool):
                return None
            try:
                amount, places = int(amount), int(places)
            except (TypeError, ValueError, OverflowError):
                return None
            if amount < 0 or places < 0 or places > 255:
                return None
            if token == USDC_MINT and places != 6:
                return None
            if token in decimals and decimals[token] != places:
                return None
            decimals[token] = places
            totals[token] = totals.get(token, 0) + sign * amount

    spent_atomic = -totals.get(USDC_MINT, 0)
    got_atomic = totals.get(mint, 0)
    token_decimals = decimals.get(mint)
    if (spent_atomic <= 0 or got_atomic <= 0 or spent_atomic > maximum_atomic
            or token_decimals is None):
        return None
    return (spent_atomic / 1_000_000.0,
            got_atomic / (10 ** token_decimals), token_decimals)


def _impact_cause(quote, pending, decimals):
    """The honest cause of an impact refusal (operator 2026-10-02, option (a)): how far our quoted
    price sits above the source's own average fill (`ran_pct`). Display and evidence only: the gate
    still reads Jupiter's priceImpactPct. In memory only: the quote's exact atomic amounts, the
    reservation and the overlapped token read when it already finished. Any unknown is None, never
    0: a source fill that is absent, zero or negative gives no ratio. No DexScreener depth here
    (operator rule: never for fill questions); our measured size cost is memecoin_impact_shadow's."""
    source = positive_price((pending or {}).get("source_price"))
    ours = quote_unit_price(quote, buy_decimals=decimals) if decimals is not None else None
    ran = (ours / source - 1.0) * 100.0 if ours and source else None
    return {"source_price": source, "quote_price": ours, "buy_decimals": decimals, "ran_pct": ran}


def _route_hops(quote):
    """[{label, amm_key, in_mint, out_mint, in_amount, out_amount}] from a Jupiter routePlan."""
    hops = []
    for step in (quote or {}).get("routePlan") or []:
        info = (step or {}).get("swapInfo") or {}
        hops.append({"label": info.get("label"), "amm_key": info.get("ammKey"),
                     "in_mint": info.get("inputMint"), "out_mint": info.get("outputMint"),
                     "in_amount": info.get("inAmount"), "out_amount": info.get("outAmount"),
                     "percent": (step or {}).get("percent")})
    return hops


def _impact_shadow(impact_shadow_fn, pending, *, mint, usd_amount, quote, impact_pct, cap, hops,
                   refused_at, quote_observed_at, gate="impact_cap", entry_rt_cost_pct=None,
                   rt_cap=None):
    """Hand an impact or round-trip refusal to the shadow (2026-09-28): non-blocking, and a
    failure here can never change the refusal. The shadow measures our own round trip at our clip
    off this thread; a round-trip refusal also carries the entry's own measurement."""
    if impact_shadow_fn is None:
        return
    try:
        from sniper.settings.memecoin import entry_roundtrip_cap_pct
        pending = pending or {}
        impact_shadow_fn({
            "attempt_id": pending.get("attempt_id"), "origin_event_id": pending.get("origin_event_id"),
            "network_id": "solana", "mint": mint, "clip_usd": float(usd_amount),
            "buy_out_atomic": str(quote.get("outAmount")), "jupiter_impact_pct": impact_pct,
            "impact_cap_pct": cap,
            "roundtrip_cap_pct": (rt_cap if rt_cap is not None
                                  else entry_roundtrip_cap_pct(cap, _contract_tp_pct(pending))),
            "refused_at": refused_at, "quote_observed_at": quote_observed_at, "hops": hops,
            "gate": gate, "entry_rt_cost_pct": entry_rt_cost_pct})
    except Exception:
        logger.warning("impact shadow hand-off failed mint=%s", mint, exc_info=True)


def _contract_tp_pct(pending):
    """The take-profit this reservation's frozen contract commits to, or None.

    None is the honest answer for every failure — no pending row, no contract, malformed JSON, a
    missing policy — and the caller treats None as "use the absolute cap". A fabricated TP here
    would silently widen or narrow a funded entry gate.
    """
    try:
        raw = (pending or {}).get("funded_contract_json")
        if not raw:
            return None
        policy = (json.loads(raw) or {}).get("policy") or {}
        tp = policy.get("tp_pct")
        return None if tp is None else float(tp)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _planned_route_cause(planned_atomic, reverse_quote, raised, miss_fn):
    """`(cause, detail)` for a planned-quantity sell proof that failed, or `(None, None)`.

    Local only: it reads the answer in hand and the seam's own record of the calls it made. An
    empty answer with no typed record is `unattributed`, never `no_route`: unknown is not market."""
    if planned_atomic <= 0:
        return "planned_quantity_missing", None
    if raised is not None:
        return "reverse_quote_raised", ("%s: %s" % (type(raised).__name__, raised))[:160]
    if reverse_quote is None:
        miss = None
        if miss_fn is not None:
            try:
                miss = miss_fn()
            except Exception:
                logger.debug("reverse-quote miss accessor failed", exc_info=True)
        if isinstance(miss, dict) and miss.get("code"):
            return str(miss["code"]), miss.get("detail")
        return "unattributed", None
    if not isinstance(reverse_quote, dict):
        return "quote_malformed", type(reverse_quote).__name__
    values = {}
    for key in ("inAmount", "outAmount", "otherAmountThreshold"):
        if reverse_quote.get(key) is None:
            return "quote_malformed", "missing %s" % key
        try:
            values[key] = int(reverse_quote[key])
        except (TypeError, ValueError):
            return "quote_malformed", "unparseable %s" % key
    if values["inAmount"] != planned_atomic:
        return "quantity_mismatch", "in=%d planned=%d" % (values["inAmount"], planned_atomic)
    if values["outAmount"] <= 0 or values["otherAmountThreshold"] <= 0:
        return "zero_output", "out=%d min_out=%d" % (values["outAmount"],
                                                     values["otherAmountThreshold"])
    return None, None


def chase_reference(source_price, chase_reference_price):
    """(price, basis) the exact-quote chase compares our quote with: the source's post-trade price
    when the caller supplies a positive one (the on-chain lane under
    MEMECOIN_REAL_CHASE_REFERENCE=post_trade), else the pending row's source price, the average
    fill (spec 2026-09-28-memecoin-chase-post-trade-reference-design.md)."""
    post = positive_price(chase_reference_price)
    if post is not None:
        return post, "post_trade"
    return positive_price(source_price), "avg_fill"


def cap_entry_quote_floor(quote, reference_price, cap_pct, decimals):
    """Bind the approved source-price cap into both vendors' ExactIn build inputs."""
    from sniper.execution.pump_curve_buy import source_price_floor_atomic
    expected, old_floor = int(quote["outAmount"]), int(quote["otherAmountThreshold"])
    floor = max(old_floor, source_price_floor_atomic(
        int(quote["inAmount"]), reference_price, cap_pct, base_decimals=decimals))
    if floor > expected:
        raise ValueError("source-price floor exceeds expected output")
    # Vendors encode slippage bps, so round down the tolerance and keep its floor consistent.
    bps = (expected - floor) * 10_000 // expected
    bps = min(bps, int(quote.get("slippageBps", bps)))
    if not 0 <= bps <= 10_000:
        raise ValueError("invalid vendor slippage bps")
    floor = max(floor, expected * (10_000 - bps) // 10_000)
    capped = {**quote, "otherAmountThreshold": str(floor), "slippageBps": bps}
    if quote.get("_venue") == "raydium":
        response = quote["_raydium_response"]
        capped["_raydium_response"] = {**response, "data": {
            **response["data"], "otherAmountThreshold": str(floor), "slippageBps": bps}}
    return capped


def watched_gate(chase_ref, chase_cap, source_average, hard_cap):
    """{gate_reference_price, gate_cap_pct, gate_basis} of the LOWER price ceiling among the chase
    cap (over the chase reference) and the hard copy cap (over the source's average fill), or {}
    when neither is set (SINS 54M6qVo9, 2026-10-07). Both references are fixed for a signal, so a
    dip back under the lower ceiling passes both gates when the decision re-runs."""
    gates = []
    for ref, cap, basis in ((chase_ref, chase_cap, "source price"),
                            (source_average, hard_cap, "source average fill")):
        try:
            ref, cap = float(ref), float(cap)
        except (TypeError, ValueError):
            continue
        if math.isfinite(ref) and ref > 0 and math.isfinite(cap) and cap > 0:
            gates.append((ref * (1 + cap / 100.0), ref, cap, basis))
    if not gates:
        return {}
    _ceiling, ref, cap, basis = min(gates)
    return {"gate_reference_price": ref, "gate_cap_pct": cap, "gate_basis": basis}


def gate_fields(quote_price, gate):
    """The watched gate plus our exact quote's premium over its reference, or {} when either is
    unknown: nothing is guessed."""
    try:
        premium = (float(quote_price) / gate["gate_reference_price"] - 1.0) * 100.0
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return {}
    return {**gate, "gate_premium_pct": premium} if math.isfinite(premium) else {}


def _floor_refusal(reason, quote_price, gate):
    """A signed-floor refusal that says what it measured (SINS 54M6qVo9, 2026-10-07): the watched
    gate's premium and cap in the reason, and its gate_* fields."""
    fields = gate_fields(quote_price, gate)
    if not fields:
        return {"reason": reason}
    return {"reason": "%s: exact quote is %+.0f%% above %s (cap %.0f%%)"
                      % (reason, fields["gate_premium_pct"], fields["gate_basis"],
                         fields["gate_cap_pct"]), **fields}


def _requote_fresh(call, pending, trace, kind, *, agreed):
    """An EARLY quote would refuse this entry at the chase or post-peak check (Alpha doctrine,
    2026-09-28): a refusal must be confirmed by a FRESH exact quote, so a stale reading never kills
    a live buy. The whole entry re-runs once from the quote step on a fresh quote, with every gate,
    and still holds the reservation; the refusal stands only if the fresh quote confirms it. Counted
    per kind with whether the fresh quote agreed."""
    # `fresh_requote` makes this the ONLY re-run: a fresh run never re-quotes again, whatever refuses.
    result = execute_entry(**{**call, "early_quote": None, "pending": pending,
                              "fresh_requote": True})
    same = bool(agreed(result)) if isinstance(result, dict) else False
    try:
        from sniper.services.memecoin_early_quote import note_refusal_requote
        note_refusal_requote(kind, same)
        _trace_substage(trace, "early_quote_refusal_requoted", kind=kind, agreed=same,
                        fresh_status=(result or {}).get("status"))
    except Exception:
        logger.warning("early quote re-quote not recorded kind=%s", kind, exc_info=True)
    return result


def _probe_roundtrip_pct(quote_fn, reverse_quote_fn, mint, probe_usd):
    """The FIXED part of a round trip: buy `probe_usd` and sell its expected output back, priced the
    way the cap prices the clip. At a dollar our own impact is ~0, so this is route fees, transfer
    tax and spread. Raises on any missing or malformed quote."""
    buy = quote_fn(mint, probe_usd)
    expected, planned = int(buy["outAmount"]), int(buy["otherAmountThreshold"])
    if expected <= 0 or planned <= 0:
        raise ValueError("probe buy quoted no tokens")
    sell = reverse_quote_fn(mint, planned)
    back = (int(sell["outAmount"]) / 1_000_000.0) * (expected / planned)
    return (1.0 - back / float(probe_usd)) * 100.0


def _roundtrip_resize(call, pending, trace, *, mint, usd_amount, rt_cost_pct, rt_cap):
    """SHRINK INSTEAD OF REFUSE (2026-09-28, operator GO; MEMECOIN_ENTRY_ROUNDTRIP_RESIZE). Returns
    the result of ONE re-run at a smaller clip, or None to let the refusal stand. Only the part of the
    round trip that our size causes can be bought down; the fixed part is a trap at any size."""
    from sniper.settings import memecoin as cfg
    if not cfg.MEMECOIN_ENTRY_ROUNDTRIP_RESIZE or call.get("roundtrip_resized"):
        return None
    target = rt_cap * float(cfg.MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_TARGET_FRACTION)
    note = {"cost_pct": rt_cost_pct, "cap_pct": rt_cap, "target_pct": target,
            "clip_usd": float(usd_amount)}
    try:
        probe = _timed(trace, "timing_roundtrip_probe", _probe_roundtrip_pct, call["quote_fn"],
                       call["reverse_quote_fn"], mint, float(cfg.MEMECOIN_ENTRY_ROUNDTRIP_PROBE_USD))
    except Exception as exc:
        # A probe that could not run is not a probe that said "fixed cost": the refusal stands.
        _trace_substage(trace, "roundtrip_resize", outcome="probe_failed",
                        error=type(exc).__name__, **note)
        return None
    fixed = max(0.0, probe) if math.isfinite(probe) else float("inf")
    note["probe_pct"] = probe
    if fixed >= target or rt_cost_pct <= fixed:
        _trace_substage(trace, "roundtrip_resize", outcome="fixed_cost", **note)
        return None
    # ponytail: linear size cost (CP pool, small impact); the re-run's own quotes judge the result.
    new_usd = round(float(usd_amount) * (target - fixed) / (rt_cost_pct - fixed), 2)
    floor = max(float(usd_amount) * float(cfg.MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_MIN_FRACTION),
                float(cfg.MEMECOIN_MIN_TRADE_USD))
    note["resized_usd"] = new_usd
    if new_usd < floor or new_usd >= float(usd_amount):
        _trace_substage(trace, "roundtrip_resize", outcome="below_floor", floor_usd=floor, **note)
        return None
    result = execute_entry(**{**call, "usd_amount": new_usd, "early_quote": None,
                              "pending": pending, "fresh_requote": True,
                              "roundtrip_resized": True})
    status = (result or {}).get("status") if isinstance(result, dict) else None
    _trace_substage(trace, "roundtrip_resize", outcome="resized", fresh_status=status, **note)
    # WARNING, not info: the runner root logger is WARNING, and a changed clip is a money event.
    logger.warning("roundtrip resize mint=%s clip_usd=%.2f resized_usd=%.2f cost_pct=%.2f probe_pct=%.2f "
                "cap_pct=%.1f status=%s", mint, float(usd_amount), new_usd, rt_cost_pct, probe,
                rt_cap, status)
    return result


def _receipt_mint_decimals(evidence, pending, mint, trace, receipt_first, receipt_fill_fn):
    """Receipt-only accounting needs mint decimals, not an unused wallet-delta baseline."""
    if receipt_first is not True or not callable(receipt_fill_fn) or not isinstance(evidence, dict):
        return None
    attempt = evidence.get("attempt_id")
    decimals = evidence.get("decimals")
    if (evidence.get("network_id") != "solana" or pending.get("network_id") != "solana"
            or evidence.get("mint") != mint or pending.get("mint") != mint
            or type(attempt) is not str or not attempt or pending.get("attempt_id") != attempt
            or getattr(trace, "attempt_id", None) != attempt
            or type(decimals) is not int or not 0 <= decimals <= 18):
        return None
    return decimals


def execute_entry(conn, *, pending_id, mint, usd_amount, strategy, source_user_id, now_iso,
                  curve_state_fn=None,
                  quote_fn, reverse_quote_fn, sign_fn, send_fn, confirm_fn, usdc_fn, token_fn,
                  sleep_fn=None, impact_cap_pct=None, chase_max_pct=None, pending=None,
                  trace=None, cluster_decision_id=None, max_accounts=None, receipt_fill_fn=None,
                  roundtrip_measure_fn=None, quote_asset_restricted_fn=None,
                  native_exit_available_fn=None, exclude_dexes=None, quote_miss_fn=None,
                  native_shadow_fn=None, early_quote=None, impact_shadow_fn=None,
                  fresh_requote=False, roundtrip_resized=False, chase_reference_price=None,
                  chase_reference_note=None, presign_refusal_fn=None, dip_cap=None,
                  receipt_first=False, mint_read_evidence=None, desk_price_cap=None):
    """Execute one real entry against a reservation.

    `desk_price_cap` (memecoin_desk.verdict_price) is carried by a desk entry only (S6): the quote must
    be at or under its cap_price_usd, and that bound is signed into the minimum, so a desk fill never
    lands above the verdict price x (1 + cap_pct). It runs whatever MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY says.

    `dip_cap` ({reference_price, max_pct, ...}) is carried by a dip retry's decision only
    (memecoin_dip_retry.dip_decision_cap): the fresh quote must be at or under reference x (1 + max),
    and that bound is also signed into the minimum, so the dip never fills above it.

    The status includes filled, skipped_chase, failed, unknown, and anomaly.
    `impact_cap_pct` sets the price-impact limit. `chase_max_pct` sets the expected quote limit.
    `receipt_first` requires signature-bound receipt truth after confirmation; overlapping
    wallet balances never substitute. The legacy injected default preserves delta accounting.
    """
    # The call as made, for the one fresh re-run an early-quote refusal gets (_requote_fresh).
    _call = dict(locals())
    from sniper.settings.memecoin import (
        MEMECOIN_ENTRY_IMPACT_CAP_PCT, entry_roundtrip_cap_pct,
    )
    cap = MEMECOIN_ENTRY_IMPACT_CAP_PCT if impact_cap_pct is None else float(impact_cap_pct)
    pending = pending or m.pending_row(conn, pending_id) or {}
    cluster_decision_id = cluster_decision_id or pending.get("cluster_decision_id")
    contract_fields = m.validated_pending_funded_contract(conn, pending)
    pending_network = str(pending.get("network_id") or "")
    if pending_network not in ("", "solana"):
        m.release_pending(conn, pending_id, "network_mismatch")
        return {"status": "aborted", "reason": "pending network mismatch"}
    provenance = {
        "attempt_id": pending.get("attempt_id"),
        "origin_event_id": pending.get("origin_event_id"),
        "network_id": "solana",
        "signal_source": pending.get("signal_source"),
    }
    # USDC-FUNDED RESERVATIONS ONLY (2026-09-27, operator GO). This executor buys with USDC and
    # books the fill as a USDC balance delta. Native-SOL funding (cbf7680b) is executed only by the
    # slot lane's execute_prebuilt_entry, which proves the fill from the receipt's lamports. Any
    # other funding reaching here is released before a quote, a read, a signature or a send.
    if pending.get("funding_asset") not in (None, "usdc"):
        m.release_pending(conn, pending_id, "general_path_funding_unsupported")
        return {"status": "aborted", "reason": "general_path_funding_unsupported",
                "funding_asset": pending.get("funding_asset"), **provenance}
    # The fill-delta baseline starts NOW, beside the quote, and is joined at step 2. We are inside
    # the wallet lock here; see _PreBalanceReads for why it must never start earlier.
    from sniper.settings.memecoin import MEMECOIN_ENTRY_OVERLAP_PRE_READS
    known_decimals = _receipt_mint_decimals(
        mint_read_evidence, pending, mint, trace, receipt_first, receipt_fill_fn)
    pre_reads = (_PreBalanceReads(usdc_fn, token_fn, mint)
                 if known_decimals is None and MEMECOIN_ENTRY_OVERLAP_PRE_READS else None)
    if known_decimals is not None:
        _trace_substage(trace, "receipt_first_mint_read", used=True, baseline_reads=False,
                        decimals=known_decimals)
    # THE EARLY QUOTE (2026-09-28, Alpha GO option 4; memecoin_early_quote): the same quote call,
    # started at decision start for the clip the risk envelope would approve. `take` returns it
    # only for THIS clip and while it is fresh; otherwise None and the quote below runs as before.
    # The oversize retry (max_accounts) never takes it. Every gate below judges whichever it gets.
    early = None
    if early_quote is not None and not max_accounts:
        try:
            early = early_quote.take(usd_amount)
        except Exception:
            logger.warning("early entry quote take failed mint=%s", mint, exc_info=True)
            early = None
    # THE NATIVE QUOTE, SHADOW ONLY (2026-09-28, design section 12 B1): read on its own thread
    # beside the Jupiter quote, handed that quote below, logged once, never used here. A used
    # early quote had its shadow started beside it, so none starts here.
    native_shadow = None
    if native_shadow_fn is not None and early is None:
        try:
            native_shadow = native_shadow_fn(mint, usd_amount, pending)
        except Exception:
            logger.warning("native quote shadow did not start mint=%s", mint, exc_info=True)

    # 1. quote + hard slippage/impact cap (abort, don't chase)
    # The oversize retry re-enters this function, so the repeatable submission stages carry an
    # attempt number. Without it `mark()` keeps attempt 1's values and the thrown-away build is
    # what the p50/p95 stage columns describe, while `transaction_submitted` silently absorbs the
    # whole retry -- fast at quote/sign, slow at submit, and wrong in both directions.
    attempt_no = 2 if max_accounts else 1
    if max_accounts:
        retry_kwargs = {"max_accounts": max_accounts}
        if exclude_dexes:
            retry_kwargs["exclude_dexes"] = tuple(exclude_dexes)
        try:
            quote = quote_fn(mint, usd_amount, **retry_kwargs)
        except TypeError as exc:
            # ONLY the seam that predates the retry. A TypeError raised inside quote_fn for any
            # other reason must propagate: swallowing it would silently re-quote uncapped and turn
            # the retry into a no-op that still looks like it ran.
            if not any(key in str(exc) for key in retry_kwargs):
                raise
            quote = quote_fn(mint, usd_amount)
    else:
        quote = early if early is not None else quote_fn(mint, usd_amount)
    quote_observed_at = trace_mark_submission(trace, "quote", attempt_no) or now_iso
    if early is not None:
        # Attribution keeps the time the quote was actually taken, on the trace's clock.
        quote_observed_at = getattr(early_quote, "observed_at", None) or quote_observed_at
    if native_shadow is not None:
        try:
            native_shadow.offer_jupiter(quote)
        except Exception:
            logger.warning("native quote shadow offer failed mint=%s", mint, exc_info=True)
    # WHICH FALLBACK BRANCH FIRED, and Jupiter's own impact, from the tag `_quote_with_fallback`
    # already attached -- in-memory only, no call. Without it the Raydium fallback's thin branch
    # could not be audited: 0 of 840 entry traces carried `priceImpactPct` on 2026-09-24.
    _fallback = quote.get("_sniper_fallback") if isinstance(quote, dict) else None
    if isinstance(_fallback, dict):
        _trace_substage(trace, "quote_fallback", **_fallback)
    if not quote:
        m.release_pending(conn, pending_id, "quote_unavailable")
        # The miss names its class, as the EVM executor's does. The release above is unchanged.
        from sniper.execution.evm_swap_executor import _quote_miss
        miss = _quote_miss(quote_miss_fn)
        if not miss:
            return {"status": "aborted", "reason": "no quote", **provenance}
        return {"status": "aborted", "reason": "no quote (%s: %s)" % (miss["code"], miss.get("detail")),
                "quote_miss_code": miss["code"], **provenance}
    quote = bind_quote_metadata(
        quote, network_id="solana", mint=mint, quote_observed_at=quote_observed_at,
        sell_decimals=6,
    )
    try:
        impact = abs(float(quote.get("priceImpactPct") or 0.0))
        impact_valid = True
    except (TypeError, ValueError):
        impact = 1.0
        impact_valid = False
    impact_pct = impact * 100.0
    impact_sentinel = impact_valid and impact_pct >= 99.9
    admitted_on_ran = False
    if impact_pct > cap and not impact_sentinel:
        legs = route_legs(quote)
        # THE ROUTE IS THE EVIDENCE AND IT WAS NEVER KEPT (2026-09-23). GIGACAT's refusal took
        # three hours to attribute because nothing recorded WHICH pools priced it. The labels and
        # ammKeys are on the quote already; carrying them costs nothing and makes the next one
        # readable without a replay.
        # Per-hop amounts too (2026-09-28): the BYTE refusal could not be decomposed by hop
        # because only labels and keys were kept. Already on the quote; no call.
        hops = _route_hops(quote)
        try:                                 # it must never strand the reservation
            cause = _impact_cause(quote, pending, known_decimals if known_decimals is not None
                                  else pre_reads.token_decimals_if_done()
                                  if pre_reads is not None else None)
        except Exception:
            logger.warning("impact cause not computed", exc_info=True)
            cause = {}
        # THE HONEST GATE (operator yes 2026-10-02 ~14:00Z, after PACKZ ref 298875ad: our price
        # +11.7% over the source's fill was refused on Jupiter's 15.6% against a 12% cap). Jupiter's
        # number prices the source's own push against the pre-buy price; the cap's question is how
        # far above the SOURCE'S FILL we would pay. When that is known (source fill and decimals in
        # memory), it decides: at or under the same cap, the entry goes on. Unknown keeps Jupiter's
        # refusal. It only ever lifts a Jupiter refusal, never adds one. A drained pump.fun curve
        # prices our clip far above the source's fill, so it still lands in the refusal below.
        # ponytail: decimals come from the overlapped token read only when it already finished; a
        # bounded wait here (refusal-path only) if buy_decimals=None shows up often.
        ran = cause.get("ran_pct")
        admitted_on_ran = isinstance(ran, float) and math.isfinite(ran) and ran <= cap
        _trace_substage(trace, "impact_cap_route", impact_pct=impact_pct, cap=cap,
                        labels=[a for a, _b in legs], amm_keys=[b for _a, b in legs], hops=hops,
                        gate_basis="ran" if isinstance(ran, float) else "jupiter",
                        admitted_on_ran=admitted_on_ran, **cause)
    if impact_pct > cap and not impact_sentinel and not admitted_on_ran:
        _impact_shadow(impact_shadow_fn, pending, mint=mint, usd_amount=usd_amount, quote=quote,
                       impact_pct=impact_pct, cap=cap, hops=hops, refused_at=now_iso,
                       quote_observed_at=quote_observed_at)
        drained = completed_curve_in_route(quote, curve_state_fn)
        if drained is not None:
            m.release_pending(conn, pending_id, CURVE_ROUTE_REFUSAL)
            return {"status": "aborted", "impact_pct": impact_pct, "curve_amm_key": drained,
                    "route_labels": [a for a, _b in legs],
                    **provenance, **quote_fields(quote),
                    "reason": "%s: %.2f%% impact priced through a drained pump.fun curve %s"
                              % (CURVE_ROUTE_REFUSAL, impact_pct, drained[:12])}
        m.release_pending(conn, pending_id, "slippage_cap")
        # carry the NUMBER (calibration data): 3/3 launch-day aborts said only "over cap" —
        # impossible to tune the cap without knowing how far over the quotes actually were.
        return {"status": "aborted", "impact_pct": impact_pct,
                **provenance, **quote_fields(quote),
                "reason": "price impact %.2f%% over %.1f%% cap" % (impact * 100.0, cap)}

    try:
        planned_atomic = int(quote["otherAmountThreshold"])
    except (KeyError, TypeError, ValueError):
        planned_atomic = 0
    reverse_raised = None
    try:
        reverse_quote = (_timed(trace, "timing_reverse_quote", reverse_quote_fn, mint,
                                planned_atomic) if planned_atomic > 0 else None)
    except Exception as exc:
        reverse_quote, reverse_raised = None, exc
    # WHICH FALLBACK BRANCH THE SELL PROOF TOOK (2026-10-02, Alpha): its own Raydium second
    # opinion runs serially when the sell route looks thin; the tag says whether it ran and how long
    # it blocked (raydium_wait_ms). In-memory only, from the seam's tag; no call.
    _rev_fallback = reverse_quote.get("_sniper_fallback") if isinstance(reverse_quote, dict) else None
    if isinstance(_rev_fallback, dict):
        _trace_substage(trace, "reverse_quote_fallback", **_rev_fallback)
    cause, detail = _planned_route_cause(planned_atomic, reverse_quote, reverse_raised,
                                         getattr(reverse_quote_fn, "miss", None))
    reverse_valid = cause is None
    # WHY THE SELL PROOF FAILED (2026-09-27, Codex refusal-26ebbe40 item 2). The refusal text and
    # the released reason stay as they were (the retry family matches them); the cause rides here.
    _trace_substage(
        trace, "planned_exit_route", amount_atomic=planned_atomic, available=reverse_valid,
        **({} if reverse_valid else {"cause": cause, "detail": detail}))
    if not reverse_valid:
        m.release_pending(conn, pending_id, "sell_route_unavailable")
        return {"status": "route_failed", "reason": "no planned-quantity sell route",
                **provenance}
    # 1b'. A SELL ROUTE THAT EXISTS IS NOT A SELL THAT CAN BE BUILT (2026-09-09, fries 55293).
    # pump.fun now launches curves quoted in tokenized stocks (fries: McDonald's xStock, MCDx, a
    # Token-2022 mint with permanent delegate / pausable / confidential-transfer extensions).
    # Jupiter QUOTES the sell happily and its pump.fun adapter then dies building it, at an ATA
    # create for the quote asset: `InstructionError[2, IllegalOwner]` — plain /swap,
    # useSharedAccounts=false, single-hop to the quote asset, all identical in simulation. 23 of
    # the last 25 transactions on that curve were other people failing the same way. The BUY leg
    # builds fine, so the trap is asymmetric: 15 exit attempts (TP at +78%, stop, two manual
    # closes) failed while the bag went from +78% to -83%. The reverse quote is already in hand,
    # so this costs no request: refuse when any Pump.fun hop pays out something other than SOL or
    # USDC. Every earlier curve entry that sold fine (Replay, UHI, INVESTOR, Hook, FUZED — all
    # Token-2022 too) paid SOL, so this is the discriminator, not the token program.
    _unbuildable = unbuildable_curve_quote_asset(reverse_quote, quote_asset_restricted_fn)
    # THE NATIVE SELL CAN SELL IT (2026-09-13, DELULU ref f4d86a45, operator: "fix this"). The
    # refusal above predates the native pump.fun sell_v2 exit (548d1006), which sold fries' MCDx
    # curve live on 2026-09-09 20:50:56Z. The exit tries it first for any non-settlement payout,
    # so a curve whose native sell BUILDS at the planned size is exitable: refuse only when it
    # does not. The check builds without simulating (we hold no tokens yet to simulate a sell).
    if _unbuildable and native_exit_available_fn is not None:
        # A CHECK THAT COULD NOT RUN IS NOT A CHECK THAT SAID NO (2026-09-18). This caught the
        # exception and recorded the same `available: False` as a native sell that genuinely does
        # not build, so a refusal and a broken escape hatch were indistinguishable in the trace.
        # The behaviour stays fail-closed — refusing an entry we may not be able to exit is the
        # right direction — but the reason is now recorded, because "the native exit does not fit
        # a packet" and "the builder raised" ask for completely different repairs.
        _native_reason = "built"
        try:
            _answer = native_exit_available_fn(mint, planned_atomic)
            # (ok, reason) since 2026-09-18; a bare bool is still accepted. NEVER `bool(answer)` on
            # the tuple form — a 2-tuple is always truthy, so "too_big" would read as available and
            # this guard would admit exactly the trap it exists to catch.
            if isinstance(_answer, tuple):
                _native_ok, _native_reason = bool(_answer[0]), str(_answer[1])
            else:
                _native_ok = bool(_answer)
                _native_reason = "built" if _native_ok else "did_not_build_or_fit"
        except Exception as exc:
            _native_ok = False
            _native_reason = "check_failed:%s" % type(exc).__name__
            logger.exception("native exit availability check failed for %s; refusing fail-closed",
                             mint)
        _trace_substage(trace, "native_exit_available", payout_mint=_unbuildable,
                        available=_native_ok, reason=_native_reason)
        if _native_ok:
            _unbuildable = None
    if _unbuildable:
        m.release_pending(conn, pending_id, "curve_quote_asset")
        return {"status": "aborted", "impact_pct": impact_pct,
                **provenance, **quote_fields(quote),
                "reason": ("exit route pays %s at a Pump.fun hop; Jupiter cannot build that sell "
                           "(exit not verified)%s"
                           % (_unbuildable[:8],
                              "" if native_exit_available_fn is None
                              else " [native exit %s]" % _native_reason))}

    # 1c. ROUND-TRIP cap. The impact cap bounds the BUY and the check above proves a sell route
    # EXISTS. Neither bounds what that sell RETURNS, and `MEMECOIN_EXIT_SLIPPAGE_LADDER` widens
    # tolerance 3 -> 10 -> 25% until the sell lands, so a one-sided pool was uncapped in the only
    # direction that empties the bag. KALMAN (position 36192, 2026-08-12) bought inside its 15%
    # nano cap and sold the whole $36.95 bag back for $17.02 in 86 s.
    #
    # The reverse quote is ALREADY fetched above, so this costs no request. It prices
    # `planned_atomic` (the signer's minimum bound), so scale its rate onto the EXPECTED quantity
    # -- otherwise the 8% tx-slippage gap between the two would read as 8 points of round-trip
    # cost that no one pays.
    roundtrip_cost_pct = None
    try:
        expected_atomic = int(quote["outAmount"])
        usd_back = (int(reverse_quote["outAmount"]) / 1_000_000.0) * (
            expected_atomic / planned_atomic)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        usd_back = None
    # THE POSITION'S OWN TP, read from the reservation's FROZEN funded contract rather than
    # re-resolved. The contract is immutable for the life of the reservation, so the budget the gate
    # spends is the budget the position will actually be managed under — a re-resolution could drift
    # between admission and management. Absent or malformed, `entry_roundtrip_cap_pct` falls back to
    # the absolute knob, which is exactly today's behaviour.
    rt_cap = entry_roundtrip_cap_pct(cap, _contract_tp_pct(pending))
    if usd_back is not None and usd_amount > 0:
        roundtrip_cost_pct = (1.0 - usd_back / float(usd_amount)) * 100.0
        _trace_substage(
            trace, "roundtrip_cap", cost_pct=roundtrip_cost_pct, cap_pct=rt_cap,
            basis="independent_pre_entry_quotes", scaled_sell_usd=usd_back,
            buy_expected_atomic=str(expected_atomic), sell_input_atomic=str(planned_atomic),
            sell_expected_atomic=str(reverse_quote["outAmount"]),
            buy_context_slot=quote.get("contextSlot"),
            sell_context_slot=reverse_quote.get("contextSlot"),
            buy_route_plan=quote.get("routePlan"), sell_route_plan=reverse_quote.get("routePlan"),
        )
    if impact_sentinel and (
            roundtrip_cost_pct is None or not math.isfinite(roundtrip_cost_pct)):
        m.release_pending(conn, pending_id, "roundtrip_cap")
        return {
            "status": "aborted", "impact_pct": impact_pct,
            "roundtrip_cost_pct": roundtrip_cost_pct,
            **provenance, **quote_fields(quote),
            "reason": "round trip unavailable for impact sentinel",
        }
    used = False
    if (roundtrip_measure_fn is not None and roundtrip_cost_pct is not None
            and roundtrip_cost_pct > rt_cap):
        # Independent quotes omit the buy's reserve changes. Confirm a refusal in one bank.
        measurement = None
        try:
            measurement = _timed(trace, "timing_roundtrip_measure", roundtrip_measure_fn,
                                 quote, reverse_quote)
            if (isinstance(measurement, dict) and measurement.get("status") == "measured"
                    and measurement.get("version") == "solana_sequential_roundtrip_v1"):
                spent = measurement.get("buy_spent_atomic")
                returned = measurement.get("sell_received_atomic")
                if not (isinstance(spent, str) and spent.isdigit()
                        and isinstance(returned, str) and returned.isdigit()):
                    raise ValueError("invalid simulated amounts")
                spent, returned = int(spent), int(returned)
                if spent != int(round(float(usd_amount) * 1_000_000)) or spent <= 0 or returned <= 0:
                    raise ValueError("simulation amount mismatch")
                measured_cost = (1.0 - returned / spent) * 100.0
                if not math.isfinite(measured_cost):
                    raise ValueError("invalid simulated cost")
                roundtrip_cost_pct, usd_back, used = measured_cost, returned / 1_000_000.0, True
        except Exception:
            measurement = {"status": "unknown", "reason": "measurement_failed"}
        _trace_substage(trace, "roundtrip_sequential", evidence=measurement, used=used,
                        cost_pct=roundtrip_cost_pct, cap_pct=rt_cap)
    if roundtrip_cost_pct is not None and roundtrip_cost_pct > rt_cap:
        # ONE BAD QUOTE MUST NOT KILL A LIVE BUY (2026-09-28, SHORK 7fa44410). A refusal the
        # one-bank simulation measured is real evidence; one it could not measure rests on two
        # independent quotes, so it is confirmed by a fresh run first (MEMECOIN_ENTRY_ROUNDTRIP_REQUOTE).
        from sniper.settings.memecoin import MEMECOIN_ENTRY_ROUNDTRIP_REQUOTE
        if MEMECOIN_ENTRY_ROUNDTRIP_REQUOTE and not used and not fresh_requote:
            _trace_substage(trace, "roundtrip_requote", cost_pct=roundtrip_cost_pct,
                            cap_pct=rt_cap)
            return _requote_fresh(
                _call, pending, trace, "roundtrip",
                agreed=lambda r: str(r.get("reason") or "").startswith("round trip "))
        resized = _roundtrip_resize(_call, pending, trace, mint=mint, usd_amount=usd_amount,
                                    rt_cost_pct=roundtrip_cost_pct, rt_cap=rt_cap)
        if resized is not None:
            return resized
        m.release_pending(conn, pending_id, "roundtrip_cap")
        _impact_shadow(impact_shadow_fn, pending, mint=mint, usd_amount=usd_amount, quote=quote,
                       impact_pct=impact_pct, cap=cap, hops=_route_hops(quote), refused_at=now_iso,
                       quote_observed_at=quote_observed_at, gate="roundtrip_cap",
                       entry_rt_cost_pct=roundtrip_cost_pct, rt_cap=rt_cap)
        return {
            "status": "aborted", "impact_pct": impact_pct,
            "roundtrip_cost_pct": roundtrip_cost_pct,
            **provenance, **quote_fields(quote),
            "reason": "round trip %.2f%% over %.1f%% cap (sell back $%.2f of $%.2f)"
                      % (roundtrip_cost_pct, rt_cap, usd_back, float(usd_amount)),
        }

    # 2. pre-balances from chain (for the fill delta), READ AT THE SAME TIME (2026-09-27, Alpha GO;
    # docs/audits/2026-09-27-lemon-dougherty-s7.md). The two reads are independent and were
    # serial: usdc p50 66 ms + token p50 179 ms, sum p50 244 / p90 318 ms over 93 first attempts.
    # Same helper as the EVM executor. A read that raises still raises here, on this thread. The
    # workers only TIME: EntryTrace.mark_substage is not thread-safe (it rewrites its last-mark
    # clock and reads a per-thread CPU clock), so the marks are written here after both return,
    # under the old names and in the old order, plus the combined wall time.
    # Since L2 (2026-09-27) they started at the top of this function and are only JOINED here;
    # every use of their result, the token decimals the chase gate prices with included, is below.
    usdc_pre = token_pre = None
    token_decimals = known_decimals
    if known_decimals is None:
        usdc_pre, token_raw = (pre_reads.join(trace) if pre_reads is not None
                              else _read_pre_balances(trace, usdc_fn, token_fn, mint))
        token_pre = _token_snapshot_parts(token_raw)
        _tok_pre, token_decimals, _pre_atomic = token_pre
    quote = bind_quote_metadata(
        quote, network_id="solana", mint=mint, quote_observed_at=quote_observed_at,
        sell_decimals=6, buy_decimals=token_decimals,
    )
    pending_source_price = positive_price(pending.get("source_price"))
    partial_drift = buy_drift(pending_source_price, quote_fields(quote)["quote_price"], None)
    partial_attribution = "exact_event" if pending_source_price is not None else None
    # Apply the current chase limit to the expected route quote before the sign step.
    # Missing source or quote data keeps the current fail-open policy.
    chase_cap = None if chase_max_pct is None else float(chase_max_pct)
    # PER-WALLET CHASE CAP, re-homed 2026-09-22. This knob was
    # MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET, a per-wallet cap on the RETIRED chart-mid premium
    # gate. It is stored as a price RATIO (2.0 = "pay up to twice the trader's price") and the
    # chase gate speaks PERCENT ABOVE SOURCE, so the mapping is (ratio - 1) * 100: 2.0 -> 100%,
    # against the roster default MEMECOIN_REAL_CHASE_MAX_PCT of 30%. That conversion already lived
    # here to lift the chase cap alongside the premium gate; with the premium gate gone it is the
    # knob's only effect, which is why it stays a LIFT (max) — the operator set it to let one
    # wallet pay MORE than the roster, never less.
    #
    # NOTHING IS CONFIGURED TODAY: the .env line is commented out and the parsed dict is empty, so
    # every wallet takes the roster default. This preserves the capability, not a live value.
    from sniper.settings import memecoin as _cfg_w
    if chase_cap is not None:
        chase_cap = _cfg_w.solana_wallet_chase_cap_pct(source_user_id, chase_cap)
    quote_px = positive_price(quote_fields(quote)["quote_price"])
    # THE CHART-MID EXECUTION-PREMIUM GATE WAS RETIRED HERE ON 2026-09-22 (operator, via Alpha).
    #
    # It compared our route quote to a DexScreener mid that lags 25-55 s, so on a moving coin it
    # measured the COIN'S MOVE and reported it as our execution premium. Measured over the class
    # since 09-15: 27 refusals, all Solana, 8 within 1.3x of the trader's own fill, and TWO (QEPE
    # 0.893x, POOL 0.954x) where our quote was CHEAPER than the trader's fill and was still refused
    # for a 1.5x premium. ⠁⠏⠑ 51a5a953 at 18:04:52Z is the case that reopened it: refused at "1.54x
    # the chart mid" on a 9.78% round trip.
    #
    # The obvious repair — measure the premium from the route's own priceImpactPct instead — is a
    # DEAD GATE by construction, and that is why the gate is gone rather than re-based:
    # MEMECOIN_ENTRY_IMPACT_CAP_PCT already aborts the entry above 5% impact, so anything reaching
    # this point is at most 1 + 0.05/(1-0.05) = 1.0526x against a 1.50x cap. A cap its input cannot
    # reach refuses nothing.
    #
    # SO EXECUTION COST IS ALREADY GATED, more strictly, by MEMECOIN_ENTRY_IMPACT_CAP_PCT, and the
    # source-referenced chase check below owns the "did the coin run away from us" question. Two
    # gates, one job each, and nothing reads a lagging mid.
    # (2026-10-02: the impact cap now lets an entry through when our price over the source's fill
    # is within the cap even if Jupiter's impact is not, so impact here is no longer bounded by the
    # cap; the round-trip cap and the chase check still run on every such entry.)
    #
    # `route_over_mid` stays in the refusal-reason classifier: 27 historical rows carry that
    # wording and the ledger must keep classifying them.
    # SUPERSEDE REVERTED 2026-08-25. Shipping this as "premium REPLACES chase" was wrong: the two
    # measure different axes and neither dominates. Premium is route-vs-CHART (is our execution
    # sane); chase is route-vs-TRADER (are we buying his exit liquidity). I argued the source
    # reference "predicts nothing" on the grounds that the coin's move at signal time is
    # flat-to-down -- but measured over 334 fills, route-vs-trader above 15% is materially worse
    # (mean -7.5% vs -3.4%, median -3.8% vs +0.3%, 45% vs 51% wins) and it survives a within-wallet
    # control 10 wallets to 3.
    #
    # Cost of the supersede while it was live: 44 fills bypassed the chase cap, 6 of them above the
    # cap they would actually have faced (30% fresh-follow / 15% otherwise), net -$28.46 -- GRILA
    # -21.29 at +49.7% drift, CALL -10.14 at +43.8%, CAVIAR -8.50 at +92.3%.
    #
    # BOTH gates now run. A trade must satisfy both, which is the only safe reading when two guards
    # cover different failure modes.
    chase_ref, chase_ref_basis = chase_reference(pending_source_price, chase_reference_price)
    from sniper.settings import memecoin as cfg
    hard_cap = cfg.copy_premium_cap(source_user_id, "solana")[0]
    if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
        from sniper.execution.entry_attribution import market_movement_check
        caps = [value for value in (chase_cap, hard_cap) if value and value > 0]
        _trace_substage(trace, "copy_market_movement", **market_movement_check(
            cap_pct=min(caps) if caps else 0))
        # Vendor amounts do not prove marginal spot. Keep their slippage minimum unchanged.
        chase_ref, chase_ref_basis, hard_cap = None, "intervening_market", 0
    gate = watched_gate(chase_ref, chase_cap, pending_source_price, hard_cap)
    override = cfg.copy_cap_override_fields(
        source_user_id, "solana", lane="onchain", price=quote_px, average=pending_source_price,
        post=chase_ref if chase_ref_basis != "avg_fill" else None,
        band_pct=getattr(trace, "copy_cap_band_without_override",
                         None if chase_max_pct is None else float(chase_max_pct)),
        band_used_pct=chase_cap)
    if override is not None:
        _trace_substage(trace, "copy_cap_override", **override)
    if not (chase_cap and chase_cap > 0 and chase_ref and quote_px):
        # Say WHY the gate stood down. Its silence was the whole defect: an unpriceable quote
        # skipped it on every first buy for weeks, and an absent substage was indistinguishable
        # from a substage that ran and passed. `entry_token_snapshot` closes the common cause;
        # a failed getTokenSupply read can still land here, and now it is countable.
        _trace_substage(
            trace, "chase_quote_skipped",
            reason=("cap_off" if not (chase_cap and chase_cap > 0)
                    else "no_source_price" if not chase_ref else "unpriceable_quote"),
            cap_pct=chase_cap, source_price=pending_source_price, quote_price=quote_px,
            buy_decimals=token_decimals, reference_price=chase_ref, reference_basis=chase_ref_basis,
            reference_note=chase_reference_note)
    else:
        quote_chase_pct = (quote_px / chase_ref - 1.0) * 100.0
        _trace_substage(trace, "chase_quote_confirmation", gap_pct=quote_chase_pct,
                        cap_pct=chase_cap, source_price=pending_source_price,
                        quote_price=quote_px, reference_price=chase_ref,
                        reference_basis=chase_ref_basis, reference_note=chase_reference_note)
        if quote_chase_pct > chase_cap:
            if early is not None:
                return _requote_fresh(_call, pending, trace, "chase",
                                      agreed=lambda r: r.get("status") == "skipped_chase")
            m.release_pending(conn, pending_id, "chase_quote")
            return {
                "status": "skipped_chase",
                "reason": "exact quote is %+.0f%% above source price (cap %.0f%%)"
                          % (quote_chase_pct, chase_cap),
                "quote_chase_pct": quote_chase_pct, **gate_fields(quote_px, gate),
                **provenance, "price_attribution": partial_attribution,
                **quote_fields(quote), **partial_drift,
            }
        try:
            quote = cap_entry_quote_floor(quote, chase_ref, chase_cap, token_decimals)
        except (KeyError, TypeError, ValueError, OverflowError):
            m.release_pending(conn, pending_id, "chase_signed_floor")
            return {"status": "skipped_chase", **_floor_refusal(
                        "source-price minimum cannot be built", quote_px, gate),
                    **provenance, **quote_fields(quote), **partial_drift}
        _trace_substage(trace, "chase_signed_floor", reference_price=chase_ref,
                        reference_basis=chase_ref_basis, cap_pct=chase_cap,
                        min_tokens_atomic=str(quote["otherAmountThreshold"]),
                        slippage_bps=quote.get("slippageBps"), buy_decimals=token_decimals)
    if hard_cap > 0 and pending_source_price:
        try:
            quote = cap_entry_quote_floor(quote, pending_source_price, hard_cap, token_decimals)
        except (KeyError, TypeError, ValueError, OverflowError):
            m.release_pending(conn, pending_id, 'copy_signed_floor')
            return {'status': 'skipped_chase', **_floor_refusal(
                        'copy price minimum cannot be built', quote_px, gate),
                    **provenance, **quote_fields(quote), **partial_drift}
        _trace_substage(trace, 'copy_signed_floor', reference_price=pending_source_price,
                        cap_pct=hard_cap, min_tokens_atomic=str(quote['otherAmountThreshold']))
    if dip_cap is not None and not cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
        # DIP MAX PREMIUM (2026-10-09): a dip decision fills at most reference x (1 + max) on the
        # row's own reference. An unknown quote price, or a floor the quote cannot meet, refuses.
        dip_ref, dip_max = float(dip_cap["reference_price"]), float(dip_cap["max_pct"])
        dip_pct = (quote_px / dip_ref - 1.0) * 100.0 if quote_px and dip_ref > 0 else None
        if dip_pct is None or dip_pct > dip_max:
            m.release_pending(conn, pending_id, "dip_cap")
            return {"status": "skipped_chase",
                    "reason": ("dip cap: exact quote is %s over the dip reference (cap %.1f%%)"
                               % ("unknown" if dip_pct is None else "%+.1f%%" % dip_pct, dip_max)),
                    "dip_cap_pct": dip_max, **provenance, **quote_fields(quote), **partial_drift}
        try:
            quote = cap_entry_quote_floor(quote, dip_ref, dip_max, token_decimals)
        except (KeyError, TypeError, ValueError, OverflowError):
            m.release_pending(conn, pending_id, "dip_cap")
            return {"status": "skipped_chase",
                    "reason": "dip cap: the minimum at %.1f%% over the dip reference cannot be built"
                              % dip_max, "dip_cap_pct": dip_max,
                    **provenance, **quote_fields(quote), **partial_drift}
        _trace_substage(trace, "dip_cap_floor", reference_price=dip_ref, cap_pct=dip_max,
                        knob_pct=dip_cap.get("knob_pct"), gate_cap_pct=dip_cap.get("gate_cap_pct"),
                        quote_premium_pct=dip_pct, min_tokens_atomic=str(quote["otherAmountThreshold"]))
    if desk_price_cap is not None:
        # DESK VERDICT PRICE CAP (S6, operator 2026-10-22: "make sure it just won't happen").
        d_ref, d_cap = float(desk_price_cap["ref_price_usd"]), float(desk_price_cap["cap_pct"])
        d_pct = (quote_px / d_ref - 1.0) * 100.0 if quote_px and d_ref > 0 else None
        _trace_substage(trace, "desk_verdict_price", reference_price=d_ref, cap_pct=d_cap,
                        tolerance_pct=desk_price_cap.get("tolerance_pct"), fee_pct=desk_price_cap.get("fee_pct"),
                        impact_pct=desk_price_cap.get("impact_pct"), quote_price=quote_px,
                        quote_vs_verdict_pct=d_pct, snapshot_at=desk_price_cap.get("snapshot_at"))
        # LIMIT ARM (operator 2026-10-23, choice B): arithmetic on the quote and ref already in scope. In the LIMIT arm an entry quote above
        # the sheet's as-of price (net of the modelled fee and impact) is NOT bought: a typed refusal carries the level and the desk turns it
        # into a 60 s "at or below the sheet price" standing order. Checked first, before the cap, and never requoted. Unknown quote = unchanged.
        arm_net = (d_pct - float(desk_price_cap["fee_pct"]) - float(desk_price_cap["impact_pct"])) if d_pct is not None else None
        if desk_price_cap.get("limit_arm") is not None:
            _trace_substage(trace, "desk_limit_arm", arm=desk_price_cap["limit_arm"], net_drift_pct=arm_net, quote_vs_verdict_pct=d_pct,
                            watch=bool(desk_price_cap["limit_arm"] == 1 and arm_net is not None and arm_net > 0))
            if desk_price_cap["limit_arm"] == 1 and arm_net is not None and arm_net > 0:
                m.release_pending(conn, pending_id, "desk_limit_watch")
                return {"status": "skipped_chase", "refusal_code": "desk_limit_watch",
                        "reason": "desk limit watch: quote is %+.2f%% over the sheet price net of fee and impact" % arm_net,
                        "desk_limit_watch": {"level_price_usd": d_ref, "net_drift_pct": arm_net, "quote_vs_verdict_pct": d_pct,
                                             "quote_price": quote_px},
                        **provenance, **quote_fields(quote), **partial_drift}
        # FILL DRIFT (operator 2026-10-23): arithmetic on the quote and ref already in scope, no call, no wait. The move
        # net of the modelled fee and impact over K x the coin's noise is refused here; the desk re-judges with it.
        drift_limit = desk_price_cap.get("drift_limit_pct")
        drift_pct = (d_pct - float(desk_price_cap["fee_pct"]) - float(desk_price_cap["impact_pct"])) if d_pct is not None else None
        if drift_limit is not None:
            _trace_substage(trace, "desk_drift", drift_pct=drift_pct, drift_limit_pct=drift_limit, drift_k=desk_price_cap.get("drift_k"),
                            noise_pct=desk_price_cap.get("noise_pct"), basis=desk_price_cap.get("basis"),
                            enforce=desk_price_cap.get("drift_enforce"), quote_vs_verdict_pct=d_pct)
            if desk_price_cap.get("drift_enforce") and drift_pct is not None and drift_pct > drift_limit:
                m.release_pending(conn, pending_id, "desk_drift")
                return {"status": "skipped_chase",
                        "reason": ("desk drift: quote is %+.2f%% over the verdict price net of fee and impact (limit %.2f%% = %.1f x noise %.3f%%)"
                                   % (drift_pct, drift_limit, desk_price_cap["drift_k"], desk_price_cap["noise_pct"])),
                        "desk_drift_pct": drift_pct, "desk_drift_limit_pct": drift_limit, "quote_vs_verdict_pct": d_pct,
                        **provenance, **quote_fields(quote), **partial_drift}
        # NO HEADROOM, NO SEND (operator 2026-10-23, T/ACC 16469fe1): a quote 0.19% under the cap
        # on a coin climbing 0.5%/s landed past the floor (Jupiter 0x1771) and paid the fee for a
        # sure failure. Under the minimum headroom refuses like an over-cap quote.
        headroom = cfg.MEMECOIN_DESK_CAP_MIN_HEADROOM_PCT
        if d_pct is None or d_pct > d_cap - headroom:
            if early is not None and d_pct is not None:
                return _requote_fresh(_call, pending, trace, "desk_price",
                                      agreed=lambda r: r.get("status") == "skipped_chase")
            m.release_pending(conn, pending_id, "desk_verdict_price")
            return {"status": "skipped_chase",
                    "reason": ("desk verdict price: exact quote is %s over the verdict price (cap %.2f%%%s)"
                               % ("unknown" if d_pct is None else "%+.2f%%" % d_pct, d_cap,
                                  ", min headroom %.2f%%" % headroom
                                  if d_pct is not None and d_pct <= d_cap else "")),
                    "desk_cap_pct": d_cap, "quote_vs_verdict_pct": d_pct,
                    **provenance, **quote_fields(quote), **partial_drift}
        try:
            quote = cap_entry_quote_floor(quote, d_ref, d_cap, token_decimals)
        except (KeyError, TypeError, ValueError, OverflowError):
            m.release_pending(conn, pending_id, "desk_verdict_price")
            return {"status": "skipped_chase",
                    "reason": "desk verdict price: the minimum at %.2f%% over the verdict price cannot be built"
                              % d_cap, "desk_cap_pct": d_cap,
                    **provenance, **quote_fields(quote), **partial_drift}
        _trace_substage(trace, "desk_verdict_floor", reference_price=d_ref, cap_pct=d_cap,
                        min_tokens_atomic=str(quote["otherAmountThreshold"]))
    post_peak = _post_peak_confirmation(
        contract_fields, quote_fields(quote)["quote_price"], trace)
    if post_peak is not None and post_peak.confirmed:
        if early is not None:
            return _requote_fresh(_call, pending, trace, "post_peak",
                                  agreed=lambda r: r.get("reason") == "post_peak_confirmed")
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

    # THE UNKNOWN-PAIR-AGE COIN CHECK (operator yes 2026-09-28; spec 2026-09-28-memecoin-unknown-
    # pair-age-mint-evidence-design.md). The mint's first-tx lookup started at classification and ran
    # beside the quote; the check reads its cache (no I/O). None admits, including a lookup that is
    # not back yet. A refusal is the reservation gate's family, `coin_too_young`, and never retried.
    if presign_refusal_fn is not None:
        try:
            too_young = presign_refusal_fn()
        except Exception:
            logger.exception("coin age presign check failed for %s; admitting", mint)
            too_young = None
        if too_young:
            m.release_pending(conn, pending_id, "coin_age")
            return {"status": "rejected", "reason": too_young, **provenance,
                    "price_attribution": partial_attribution, **quote_fields(quote), **partial_drift}

    # 3. sign; write-ahead the signature BEFORE broadcast
    signed = sign_fn(quote)
    # THE SPLIT COMES BACK OFF THE SEAM, not from a wrapper here: the swap BUILD is a Jupiter round
    # trip and the signature is local CPU, and only `_sign` can see the boundary between them. It
    # is consumed per thread and popped, so a stale split from an earlier attempt cannot be
    # attributed to this one; a seam that does not carry it simply records nothing.
    try:
        _split = getattr(sign_fn, "sign_timing", None)
        _split = _split() if callable(_split) else None
        if isinstance(_split, dict):
            for _name, _key in (("timing_swap_build", "build_ms"), ("timing_local_sign", "sign_ms"),
                                ("timing_sign_combined", "combined_ms")):
                if _split.get(_key) is not None:
                    _trace_substage(trace, _name, duration_ms=float(_split[_key]),
                                    builder=_split.get("builder"))
    except Exception:
        # Optional telemetry must not interrupt signed-identity binding or submission -- and must
        # not vanish either. Exception path only; the normal path pays nothing for this line.
        logger.debug("entry sign-timing telemetry failed; submission continues", exc_info=True)
    trace_mark_submission(trace, "signing", attempt_no)
    if not signed:
        m.release_pending(conn, pending_id, "sign_failed")
        # A build the venue refused is not a signing fault; say which one it was (2026-09-11).
        why = getattr(sign_fn, "refusal_reason", None)
        why = why() if callable(why) else None
        return {"status": "aborted", "reason": f"route build refused: {why}" if why else "sign failed",
                **provenance,
                "price_attribution": partial_attribution, **quote_fields(quote), **partial_drift}
    signed_tx, sig = signed[:2]
    validity = signed[2] if len(signed) > 2 and isinstance(signed[2], dict) else None

    # OVERSIZE RETRY. A Solana packet is capped at 1232 bytes; a larger signed swap can never land,
    # and the send seam refuses it. Re-quote ONCE with a capped account count and re-enter, which
    # re-runs every price gate on the new quote instead of splicing it past them.
    #
    # This must happen BEFORE the write-ahead. `bind_entry_submission` refuses a second, different
    # payload for the same reservation, and `set_pending_sig` commits the legacy seam's signature.
    # Here nothing is persisted and nothing was broadcast, so the discarded signature never existed
    # outside this frame and the reservation stays clean for the retry's own single binding.
    from sniper.settings.memecoin import MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS
    from sniper.execution.submission_wiring import SOLANA_PACKET_LIMIT
    retry_accounts = int(MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS or 0)
    signed_bytes = _payload_bytes(signed_tx)
    if (signed_bytes is not None and signed_bytes > SOLANA_PACKET_LIMIT
            and max_accounts is None and retry_accounts > 0):
        retry_exclusions = _oversize_retry_exclusions(quote)
        _trace_substage(trace, "oversize_retry", pre_bytes=signed_bytes,
                        limit=SOLANA_PACKET_LIMIT, max_accounts=retry_accounts,
                        exclude_dexes=list(retry_exclusions))
        result = execute_entry(
            conn, pending_id=pending_id, mint=mint, usd_amount=usd_amount, strategy=strategy,
            source_user_id=source_user_id, now_iso=now_iso, quote_fn=quote_fn,
            reverse_quote_fn=reverse_quote_fn, sign_fn=sign_fn, send_fn=send_fn,
            confirm_fn=confirm_fn, usdc_fn=usdc_fn, token_fn=token_fn, sleep_fn=sleep_fn,
            impact_cap_pct=impact_cap_pct, chase_max_pct=chase_max_pct, pending=pending,
            trace=trace, cluster_decision_id=cluster_decision_id, max_accounts=retry_accounts,
            receipt_fill_fn=receipt_fill_fn, roundtrip_measure_fn=roundtrip_measure_fn,
            quote_asset_restricted_fn=quote_asset_restricted_fn,
            native_exit_available_fn=native_exit_available_fn, exclude_dexes=retry_exclusions,
            quote_miss_fn=quote_miss_fn, chase_reference_price=chase_reference_price,
            chase_reference_note=chase_reference_note, presign_refusal_fn=presign_refusal_fn,
            dip_cap=dip_cap, receipt_first=receipt_first, mint_read_evidence=mint_read_evidence,
            desk_price_cap=desk_price_cap)
        if isinstance(result, dict):
            result.setdefault("oversize_retry_pre_bytes", signed_bytes)
        return result
    if max_accounts is not None:
        # The canary needs to tell "maxAccounts shrank the route" from "the route happened to be
        # smaller this time". Route size is time-varying, so record BOTH ends of the retry.
        from sniper.execution.submission_wiring import SOLANA_PACKET_LIMIT
        _trace_substage(trace, "oversize_retry_rebuild", post_bytes=signed_bytes,
                        limit=SOLANA_PACKET_LIMIT, max_accounts=max_accounts,
                        fits=(signed_bytes is not None and signed_bytes <= SOLANA_PACKET_LIMIT))

    # The shared adapter commits the stronger immutable payload+identity+validity binding itself.
    # The legacy seam retains its signature-only write-ahead contract.
    if not getattr(send_fn, "persists_identity", False):
        m.set_pending_sig(conn, pending_id, sig, now_iso, signed_payload_b64=signed_tx)   # <-- before send_fn

    # 4. send once, confirm
    if getattr(send_fn, "accepts_validity", False):
        send_result = send_fn(signed_tx, validity=validity)
    else:
        send_result = send_fn(signed_tx)
    if send_result is False:
        m.release_pending(conn, pending_id, "broadcast_rejected")
        return {"status": "failed", "reason": "broadcast_rejected", "sig": sig,
                **provenance, "price_attribution": partial_attribution,
                **quote_fields(quote), **partial_drift}
    if send_result:
        trace_mark_submission(trace, "transaction_submitted", attempt_no)
    status = confirm_fn(sig)
    if status in ("confirmed", "failed"):
        # Fill timing (2026-10-02): the slot rpc_confirm's ConfirmState already carries, else None.
        trace_mark_submission(trace, "chain_confirmation", attempt_no,
                              landing_slot=getattr(status, "landing_slot", None))
    quote_age = quote_age_ms(trace, status)

    if status == "confirmed":
        # 5. fill truth from the on-chain balance delta (never the quote). A just-confirmed tx can
        # lag the balance surface (or a fail-closed read returns 0.0 on an RPC blip) -> bounded
        # re-read before believing a zero delta; a real $5 fill was booked 'anomaly' 2026-07-10.
        if sleep_fn is None:
            import time
            sleep_fn = time.sleep
        # THE DELTA IS BOUNDED BY THE RESERVATION (2026-09-27, Alpha GO F2). usdc_fn fails CLOSED to
        # 0.0, so a failed post-send read made the delta usdc_pre - 0, the whole wallet, booked as
        # the fill. An ExactIn buy spends exactly the clip (entry_notional == reserved on 1,166 of
        # 1,166 funded Solana fills to 2026-09-27), so a delta above it is not our fill: re-read,
        # then the receipt, which is bounded by the reservation. A post read that RAISES is the
        # same unknown, never a crash after a confirmed send.
        reserved = float(pending.get("reserved_notional_usd") or usd_amount)
        spend_bound = reserved * 1.01

        def _post_delta():
            try:
                delta_spent = usdc_pre - usdc_fn()
                delta_got, delta_decimals = _token_delta(
                    token_pre, _token_snapshot_parts(token_fn(mint)))
            except Exception:
                logger.warning("%s entry post-send balance read raised sig=%s; re-reading",
                               datetime.now(timezone.utc).isoformat(), sig, exc_info=True)
                return None, None, None
            return delta_spent, delta_got, delta_decimals

        def _plausible(fill_spent, fill_got):
            return (fill_spent is not None and 0 < fill_spent <= spend_bound
                    and fill_got is not None and fill_got > 0)

        spent = got = token_decimals = None
        quantity_source = 'receipt_unreadable' if receipt_first else 'balance_delta'
        if not receipt_first:
            spent, got, token_decimals = _post_delta()
            for _ in range(3):
                if _plausible(spent, got):
                    break
                sleep_fn(1.0)
                spent, got, token_decimals = _post_delta()
        receipt_reads = receipt_wait_ms = 0
        if receipt_first or not _plausible(spent, got):
            # BOUNDED RE-READ (operator yes 2026-10-23): a receipt-first entry read the receipt ONCE, 17-69 ms after the
            # confirm, while getTransaction at 'confirmed' often had not served it yet: 6 of 7 desk fills (and ~10% of
            # copy fills) went anomaly/receipt_unreadable and were booked ~5.5-6 s later by the recovery worker. Now up to
            # ENTRY_RECEIPT_READS reads ENTRY_RECEIPT_READ_SLEEP_S apart (<= 0.75 s of waiting), the slot executor's
            # shape. After the send and confirm: nothing before signing or sending changes. All misses = today's path.
            reads = ENTRY_RECEIPT_READS if receipt_first else 1
            started = _receipt_clock()
            receipt_fill = None
            for attempt in range(reads):
                receipt_reads = attempt + 1
                receipt_fill = _verified_receipt_fill(receipt_fill_fn, sig, mint, reserved)
                if receipt_fill is not None or attempt + 1 >= reads:
                    break
                sleep_fn(ENTRY_RECEIPT_READ_SLEEP_S)
            receipt_wait_ms = round((_receipt_clock() - started) * 1000, 1)
            if receipt_fill is not None:
                spent, got, token_decimals = receipt_fill
                quantity_source = "receipt"
            elif receipt_first:
                served = getattr(receipt_fill_fn, "last_served", None)
                logger.warning("%s entry receipt unreadable after %d reads (%.0f ms) sig=%s cause=%s",
                               datetime.now(timezone.utc).isoformat(), receipt_reads, receipt_wait_ms, sig,
                               "not_served" if served is False else "unparseable" if served is True else "unknown")
        _trace_substage(trace, "entry_receipt_recovery", outcome=quantity_source, reads=receipt_reads,
                        wait_ms=receipt_wait_ms)
        trace_mark(trace, "balance_delta")
        quote = bind_quote_metadata(
            quote, network_id="solana", mint=mint, quote_observed_at=quote_observed_at,
            sell_decimals=6, buy_decimals=token_decimals,
        )
        if not _plausible(spent, got):
            # confirmed but no tokens / no spend => anomaly; leave reserved + surface, do not open
            return {"status": "anomaly", "sig": sig, "spent": spent, "got": got,
                    **({'reason': 'receipt_unreadable'} if receipt_first else {}),
                    "quote_age_ms": quote_age, **provenance,
                    "price_attribution": partial_attribution,
                    **quote_fields(quote), **buy_drift(
                        pending_source_price, quote_fields(quote)["quote_price"], None)}
        fill_price = spent / got
        # capture the signal-time market cap (median of recent feed readings for this mint) so real
        # positions carry entry_mcap like paper arms do — it was 100% NULL on real, blinding the
        # per-mcap exit-target/policy analytics. Resolved from the feed, not the fill.
        # + execution-quality: how far our on-chain fill drifted from the trader's price we copied.
        their_px = positive_price(pending.get("source_price"))
        if their_px is not None:
            price_attribution = "exact_event"
        else:
            their_px = positive_price(m.resolve_mint_their_price(
                conn, mint, network_id="solana", source_user_id=source_user_id))
            price_attribution = "legacy_source_chain" if their_px is not None else "unavailable"
        quote_data = quote_fields(quote)
        drift = buy_drift(their_px, quote_data["quote_price"], fill_price)
        gap_pct = drift["total_copy_drift_pct"]
        entry_mcap = positive_price(pending.get("source_market_cap"))
        if entry_mcap is None:
            entry_mcap = m.resolve_mint_mcap(conn, mint, network_id="solana",
                                             price=fill_price)
        pid, fill_id = m.finalize_pending_entry(
            conn, pending_id=pending_id,
            position_kwargs=dict(
            source_user_id=source_user_id, source_handle="(real)", mint=mint, ticker="",
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
            execution_mode="real", network_id="solana",
            signal_source=pending.get("signal_source") or "global_feed",
            origin_event_id=pending.get("origin_event_id"), entry_mcap=entry_mcap,
            their_price=their_px, fill_gap_pct=gap_pct, attempt_id=pending.get("attempt_id"),
            price_attribution=price_attribution, quote_age_ms=quote_age,
            cluster_decision_id=cluster_decision_id,
            **contract_fields, **quote_data, **drift),
            fill_kwargs=dict(kind="entry", price=fill_price, qty=got,
            notional=spent, fill_source="chain", fraction=1.0, created_at=now_iso,
            tx_sig=sig, source_price=their_px, price_attribution=price_attribution,
            quote_age_ms=quote_age, **provenance, **quote_data, **drift))
        trace_mark(trace, "persistence")
        # notional = the TRUE on-chain USDC spend — the ENTRY alert prints this, never the
        # static config clip (a $50.69 fill was alerted as "$10" before this field existed).
        # impact_pct on FILLS too: the cap calibrates from the distribution of impacts we
        # accepted vs aborted, not from aborts alone.
        return {"status": "filled", "position_id": pid, "fill_id": fill_id,
                "fill_price": fill_price, "notional": spent, "impact_pct": impact_pct,
                "roundtrip_cost_pct": roundtrip_cost_pct,
                "sig": sig, "source_price": their_px, "price_attribution": price_attribution,
                "quote_age_ms": quote_age, **provenance, **quote_data, **drift}

    if status == "failed":
        # KEEP THE CHAIN'S OWN WORDS. This used to release with a bare "tx_failed" and return a
        # reason of literally "failed", so `terminal_reason` said nothing and diagnosing one revert
        # cost six manual getTransaction calls (sunnydog06 2026-08-24: Meteora DBC rejected the
        # route with AnchorError ConstraintMintTokenProgram). `status` is a ConfirmState carrying
        # err / program_error / deterministic; plain strings from older seams still work.
        reason = getattr(status, "reason_code", None) or "tx_failed"
        slot_guard = getattr(status, "slot_guard", None)
        if slot_guard:
            _trace_substage(trace, "slot_guard_result", **slot_guard)
        m.release_pending(conn, pending_id, reason[:64])
        return {"status": "failed", "reason": reason, "sig": sig, "quote_age_ms": quote_age,
                "chain_err": getattr(status, "err", None),
                "program_error": getattr(status, "program_error", None),
                "failed_program": getattr(status, "failed_program", None),
                **({"slot_guard": slot_guard} if slot_guard else {}),
                # `deterministic_rejection` was returned here from 2026-08-24 to 2026-09-08 with
                # the comment "the retry lane reads this". It never did — nothing in sniper/ or
                # scripts/ ever read it, and it was not a persisted column either. Removed rather
                # than left as a field a future reader would reasonably trust.
                #
                # Nothing is lost. `reason` already answers the same question: the `chain_error:`
                # prefix means a program executed and answered, and since 2026-09-08 it carries
                # that program's own words, which is what the retry lane actually matches on.
                **provenance, "price_attribution": partial_attribution,
                **quote_fields(quote), **partial_drift}

    # unknown / timeout: the tx may still land -> keep the reservation, open nothing, NEVER resubmit.
    return {"status": "unknown", "sig": sig, **provenance,
            "price_attribution": partial_attribution, **quote_fields(quote), **partial_drift}
