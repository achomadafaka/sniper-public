"""Keep native gas alive on a funded EVM chain by UNWRAPPING WETH, not by swapping.

WHY THIS EXISTS. `memecoin_gas_topup.gas_topup_once` is Solana-only in every parameter --
`sol_balance`, a Solana keypair, `solana_funded_rpc_urls`, and a reserve taken against
`pending_totals(conn, "solana")`. There is no EVM branch anywhere in the runner, so chain 4663 has
run on hand-funded gas since it was armed. Measured 2026-08-30: 0.0000925 ETH per funded round trip
against a refusal floor of 0.000350 at two slots, i.e. roughly 19 trades of runway from a standing
start. When it runs dry the EVM entry gate refuses -- correctly and silently -- so the lane simply
stops buying with no alert.

WHY IT IS SAFER THAN ITS SOLANA SIBLING, and the reason it is worth having at all. The Solana
version SWAPS USDC->SOL through Jupiter: a route, a price, slippage, and a provider that has 429'd
us. This unwraps WETH->native ETH via `WETH.withdraw(amount)` -- selector 0x2e1a7d4d, already used
as a pre-transaction on the native buy path and already recognised by the executor. It is 1:1 by
protocol. There is no route to pick wrong, no price to read stale, no slippage, and no counterparty.
The only ways it can lose money are gas for the transaction itself and unwrapping more than we meant.

THE RESERVE IS THE REAL GUARD. WETH on a funded EVM chain IS the trading bankroll, so every wei
unwrapped is bankroll withdrawn. `min_weth_reserve` is subtracted before any amount is considered
and the top-up is skipped rather than shrunk when the reserve would be breached -- a partial top-up
that leaves gas still under the floor spends a transaction to remain refused.

FAIL-SAFE DIRECTIONS, mirroring the Solana version deliberately:
  * an UNREADABLE native balance is NOT low. Unknown must never trigger a spend; the Solana sibling
    returns `sol_unreadable` for the same reason. A transient RPC failure cost us a $497 entry on
    2026-08-30 through the premium gate, and that gate at least refused rather than spent.
  * an unreadable WETH balance is NOT spare. Same rule, other side of the trade.
  * a zero daily cap is unlimited. The target and the WETH reserve limit each unwrap. A positive
    cap remains available for an operator override.
  * the runner holds the same wallet lock used by funded entry and exit, because two concurrent
    unwraps would each read the pre-unwrap balance and both decide to spend.

This module DECIDES and BUILDS. It performs no network I/O of its own: balances arrive as
arguments and the transaction leaves through injected seams, so the whole policy is testable
without a chain and without key material.
"""
from __future__ import annotations

import logging
import os
import time
import traceback

logger = logging.getLogger(__name__)

# WETH.withdraw(uint256) -- unwrap to native, 1:1. Already used by the buy path's pre-transactions
# and matched at evm_swap_executor.py:389, so this introduces no new calldata shape.
WITHDRAW_SELECTOR = "0x2e1a7d4d"
DEPOSIT_SELECTOR = "0xd0e30db0"
# Deliberately generous: an unwrap is a fixed-cost call and under-estimating it strands the
# transaction, which is the one failure that leaves gas LOWER than before.
# ONE LIMIT FOR EVERY FUNDED WETH CALL (2026-09-28). The entry builders signed their unwrap at a
# literal 60,000; reservation 4158 (4663) burned EXACTLY 60,000 x 20,098,463 wei and reverted, i.e.
# the whole limit -- most likely out of gas, because 4663 is Nitro and gasUsed there includes the L1
# data component (the receipt's gasUsed is unverified). 1 tx_failed against 242 accepted 4663 unwrap
# pipelines since 09-01. Unused gas is refunded; the extra admission need is ~90k x gasPrice
# (~1.8e12 wei at 0.02 gwei against ~2.9e15 native). Withdraw and deposit, entry, exit, top-up and
# recovery all take this one value.
WETH_CALL_GAS_LIMIT = 150_000
UNWRAP_GAS_LIMIT = WETH_CALL_GAS_LIMIT
WRAP_GAS_LIMIT = WETH_CALL_GAS_LIMIT


def encode_withdraw(amount_wei: int) -> str:
    """Calldata for `WETH.withdraw(amount)`. Rejects a negative or non-integral amount rather than
    silently encoding a wrapped value -- a bad amount here spends real gas to do the wrong thing."""
    amount = int(amount_wei)
    if amount < 0:
        raise ValueError("withdraw amount must be non-negative")
    return WITHDRAW_SELECTOR + f"{amount:064x}"


def _topup_provably_dead(seams, row):
    """True only when this signed top-up can NEVER mine.

    Two facts, both positive, both required:
      1. the account's MINED nonce is already past this transaction's nonce, so a different
         transaction has consumed it -- two transactions cannot share a nonce; and
      2. `pending` equals `latest`, so the node is holding nothing of ours that could still land.

    Fail-closed throughout. A missing seam, an unreadable nonce or a malformed row proves nothing
    and leaves the row unresolved, which is the behaviour that existed before this check.
    """
    confirmed_fn = seams.get("confirmed_nonce_fn")
    pending_fn = seams.get("pending_nonce_fn")
    if not callable(confirmed_fn) or not callable(pending_fn):
        return False
    try:
        row_nonce = int(row["nonce"])
        confirmed = int(confirmed_fn())
        pending = int(pending_fn())
    except (TypeError, ValueError, KeyError):
        return False
    return confirmed > row_nonce and confirmed == pending


def plan_gas_topup(*, native_balance_wei, weth_balance_wei, low_water_wei, target_wei,
                   min_weth_reserve_wei, daily_cap_wei, spent_today_wei,
                   open_position_count=0):
    """Decide whether to unwrap, and how much. Pure: no I/O, no clock, no chain.

    Returns {"act": bool, "state": str, "amount_wei": int}. `state` is the reason in every case,
    including the acting one, so a caller can record WHY it did nothing -- the Solana sibling's
    `tally["state"]` serves the same purpose and it is what makes a quiet top-up auditable.
    """
    out = {"act": False, "state": "", "amount_wei": 0}

    # UNKNOWN IS NOT LOW. Both directions, before anything else.
    if native_balance_wei is None:
        out["state"] = "native_unreadable"
        return out
    if weth_balance_wei is None:
        out["state"] = "weth_unreadable"
        return out

    native = int(native_balance_wei)
    weth = int(weth_balance_wei)
    low = int(low_water_wei)
    target = int(target_wei)
    reserve = int(min_weth_reserve_wei)
    cap = int(daily_cap_wei)
    spent = int(spent_today_wei)

    if low <= 0 or target <= 0:
        out["state"] = "disabled"          # an unset low-water mark means "off", never "always"
        return out
    if target <= low:
        # A target at or below the trigger tops up into the refusing state and would re-fire every
        # pass. Refuse the configuration rather than act on it.
        out["state"] = "target_not_above_low_water"
        return out
    if native >= low:
        out["state"] = "above_low_water"
        return out

    want = target - native
    if want <= 0:
        out["state"] = "above_low_water"
        return out

    spare = weth - reserve
    if spare <= 0:
        # THE RESERVE YIELDS WHILE A POSITION IS TRAPPED (2026-09-02).
        #
        # The refusal below is correct for ENTRIES and exactly wrong for EXITS. With an open
        # funded position the gas is not funding a trade we can no longer afford — it is the only
        # way to get the money OUT. A reserve that strands the capital it exists to protect is
        # inverted, and it self-locks: no gas -> cannot sell -> cannot recover WETH -> cannot
        # refill gas, tightening every time a sell reverts, because a revert still burns gas.
        #
        # Live on 4663 this evening: native fell to 0.00051674 against a 0.002 low-water mark and
        # the top-up logged `weth_below_reserve` 83 times, once every 15s, while three funded bags
        # sat un-exitable. RIZZLER's reverted sell alone burned 739,626 gas.
        #
        # The release is BOUNDED THREE WAYS and is not an override:
        #   * only while `open_position_count > 0` — with a flat book the original reasoning holds
        #     in full and the reserve is absolute;
        #   * only up to `want` (target - native), the same amount a normal top-up would take, so
        #     it can never drain more than the healthy path would;
        #   * only up to the WETH actually held.
        if open_position_count > 0 and weth > 0:
            release = min(want, weth)
            if release > 0:
                out["act"] = True
                out["state"] = "reserve_released_for_exit"
                out["amount_wei"] = int(release)
                return out
        # Bankroll is at or under its floor and nothing is trapped behind it. Gas stays low and
        # entries keep refusing, which is the correct outcome: an unwrap here converts the last of
        # the bankroll into gas for trades it can no longer fund.
        out["state"] = "weth_below_reserve"
        return out
    if want > spare:
        # Deliberately NOT clamped to `spare`. A partial top-up that leaves native under the floor
        # spends a transaction and stays refused; better to hold and surface the state.
        out["state"] = "insufficient_weth_for_full_topup"
        return out

    if cap > 0:
        remaining = cap - spent
        if want > remaining:
            out["state"] = "daily_cap_reached"
            return out

    out["act"] = True
    out["state"] = "topup"
    out["amount_wei"] = int(want)
    return out


def plan_gas_reclaim(*, native_balance_wei, target_wei, gas_price_wei,
                     gas_limit=WRAP_GAS_LIMIT, floor_gas=0):
    """Wrap native cash above the target without crossing the target during this transaction.
    `floor_gas` keeps at least that much gas at this gas price when it exceeds the target (the flap
    Portal exit on 56: gas_pass_bounds). 0 = the target alone, as before."""
    out = {"act": False, "state": "", "amount_wei": 0}
    if native_balance_wei is None:
        out["state"] = "native_unreadable"
        return out
    if gas_price_wei is None:
        out["state"] = "gas_price_unreadable"
        return out
    native = int(native_balance_wei)
    target = int(target_wei)
    gas_price = int(gas_price_wei)
    if target <= 0 or gas_price <= 0:
        out["state"] = "disabled"
        return out
    amount = native - max(target, int(floor_gas) * gas_price) - int(gas_limit) * gas_price
    if amount <= 0:
        out["state"] = "at_or_under_target"
        return out
    out.update({"act": True, "state": "reclaim", "amount_wei": amount})
    return out


# --- the pass on chains other than 4663 (2026-09-30, operator via Alpha; BINF 6fc71d90) -----------
#
# A flap Portal exit on 56 pays NATIVE BNB (the Portal unwraps the pool's WBNB) while entries spend
# WBNB, and until now this pass ran on 4663 only: 67604 and 67623 left 0.293 BNB native and every
# BSC clip sized to $0 (0.19 BNB the same way on 09-28). The global MEMECOIN_EVM_GAS_TOPUP_* knobs
# are 4663's ETH values, so another chain never inherits them: its bounds come from its OWN gas
# settings, the same numbers the entry gate and the manual wrap script use.

HEADROOM_ROUND_TRIPS = 3
LEGACY_GAS_PASS_CHAIN = "4663"      # the chain the global MEMECOIN_EVM_GAS_TOPUP_* values are for
APPROVE_GAS = 100_000               # build_real_evm_executor's approve_gas


def chain_gas_need_native(chain, cfg):
    """(book, headroom, terms) in native units from the runner's own gas settings for `chain`:
    the gate's `_gas_reserve` at the fullest book an entry can still open into (max_concurrent - 1
    open), and HEADROOM_ROUND_TRIPS round trips of entry + approval + exit + approval, both at the
    retry factor."""
    from sniper.services.memecoin_evm_risk import _gas_reserve
    key = str(chain)
    entry = float(cfg.gas_entry_native_for(key))
    approval = float(cfg.gas_approval_native_for(key))
    exit_gas = float(cfg.gas_exit_native_for(key))
    retry = float(cfg.MEMECOIN_GAS_RETRY_FACTOR)
    max_open = int(cfg.max_concurrent_for(key))
    book = _gas_reserve(gas_entry_native=entry, gas_approval_native=approval,
                        gas_exit_native=exit_gas, gas_retry_factor=retry,
                        chain_concurrent=max(0, max_open - 1))
    headroom = HEADROOM_ROUND_TRIPS * (entry + approval + exit_gas + approval) * retry
    terms = {"entry": entry, "approval": approval, "exit": exit_gas, "retry_factor": retry,
             "max_concurrent": max_open, "book_need": book, "headroom": headroom,
             "headroom_round_trips": HEADROOM_ROUND_TRIPS}
    return book, headroom, terms


def gas_pass_bounds(chain, cfg=None):
    """run_evm_gas_topup_pass bounds for a chain other than 4663, and the base bounds of 4663's
    half-liquid float cap (MEMECOIN_EVM_FLOAT_LIQUID_CAP_CHAINS, 2026-10-15), whose own pass keeps
    the legacy low/target/reserve and takes only these as `balance_bound_base`.

    low    = the gate's book need, never under the chain's MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE_<id>:
             under it an entry refuses for gas, so the pass unwraps up to the target;
    target = low + the round-trip headroom: native above it (plus the wrap's own gas) is wrapped
             back into the quote token;
    reclaim floor (56, flap curve on) = the flap Portal swap + an approve + an unwrap, at the gas
             price the reclaim reads times MEMECOIN_EVM_GASPRICE_BUFFER (the executor's signing
             price): a gas spike can never wrap away the gas a flap exit needs;
    no WETH reserve and no daily cap: the target alone bounds each unwrap (4663's 0.010 ETH reserve
    is an ETH number and is not borrowed).
    """
    if cfg is None:
        from sniper.settings import memecoin as cfg
    key = str(chain)
    book, headroom, _terms = chain_gas_need_native(key, cfg)
    low = max(float(cfg.gas_reserve_min_native_for(key)), book)
    floor_gas = 0
    if key == "56" and bool(getattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", False)):
        floor_gas = int((int(cfg.MEMECOIN_EVM_FLAP_PORTAL_GAS) + APPROVE_GAS + WETH_CALL_GAS_LIMIT)
                        * float(cfg.MEMECOIN_EVM_GASPRICE_BUFFER))
    return {"low_water_wei": int(low * 10 ** 18), "target_wei": int((low + headroom) * 10 ** 18),
            "min_weth_reserve_wei": 0, "daily_cap_wei": 0, "reclaim_floor_gas": floor_gas,
            "hold_under_reservation": True,
            "hold_until_receipt_s": float(cfg.MEMECOIN_EVM_GAS_TOPUP_HOLD_S)}


def _atomic_balance_wei(value):
    """Known nonnegative atomic balance; never truncate floats or accept booleans as money."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def balance_bound_float(bounds, *, base_bounds, native_balance_wei, weth_balance_wei):
    """Maintenance-only cap: native-spendable cash, or base gas plus half liquid capital.

    Unknown balances abstain. Native-spendable cash retains its WETH reserve; entry gas floors
    stay separate. Ordinary floats scale refill hysteresis with liquidity, never protected gas.
    Reclaim's live gas-price floor may retain more native than this liquidity cap.
    """
    native, weth = _atomic_balance_wei(native_balance_wei), _atomic_balance_wei(weth_balance_wei)
    if native is None or weth is None:
        return None
    low, target = int(bounds["low_water_wei"]), int(bounds["target_wei"])
    if base_bounds.get("native_spendable"):
        reserve = int(base_bounds["min_weth_reserve_wei"])
        if 0 < weth <= reserve:
            return dict(bounds)  # Keep the existing open-book exit rescue decision.
        spare = max(0, weth - reserve)
        capped = min(target, native + spare)
        return dict(bounds, target_wei=capped, low_water_wei=min(low, max(0, capped - 1)))
    base_low, base_target = int(base_bounds["low_water_wei"]), int(base_bounds["target_wei"])
    if target <= base_target:
        return dict(bounds)
    capped = min(target, base_target + max(0, native + weth - base_target) // 2)
    if capped == target:
        return dict(bounds)
    refill = base_low + max(0, low - base_low) * (capped - base_target) // (target - base_target)
    return dict(bounds, target_wei=capped, low_water_wei=max(base_low, min(refill, capped - 1)))


_AGREE_TOLERANCE = 0.01          # 1% -- gas spend between reads is far below this
_AGREE_READS = 3


def agreeing_native_read(read_fn, *, reads=_AGREE_READS, tolerance=_AGREE_TOLERANCE, log=None,
                         deadline_s=None):
    """Return the native balance only when `reads` consecutive reads agree, else None.

    PHANTOM BALANCE (2026-09-05). Thirteen times today the top-up planner acted on a native read
    of exactly 2501595900762000 wei -- a value the chain never held (the real balance at each
    of those blocks was 0.0056-0.0060 ETH). Each phantom unwrapped 0.0035 ETH of WETH, and the
    reclaim sixteen seconds later, reading correctly, wrapped it back: two transactions and
    two fees for nothing, thirteen times. One hedged read is not evidence about money; three
    reads that agree are. The reads go through the same pool so a stale member can still be
    among them, but a phantom that appears in one read out of ~150 cannot appear in three
    consecutive ones. When they disagree, every raw value is logged so the next phantom is
    attributable, and the pass does nothing -- the next pass, fifteen seconds later, tries again.
    """
    from sniper.execution.evm_wallet import last_answer
    import time as _time
    if deadline_s is None:
        from sniper.settings import memecoin as _cfg
        deadline_s = float(getattr(_cfg, "MEMECOIN_EVM_GAS_TOPUP_READ_BUDGET_S", 8.0) or 0.0)
    started = _time.monotonic()
    values, who = [], []
    for i in range(max(1, int(reads))):
        # THE PASS RUNS ON THE MAIN LOOP. One hedged read waits up to _HEDGE_TOTAL_S (12s) when
        # every pool member times out, and three of them held the loop for 36s; with the reclaim
        # branch reading too, the watchdog saw a 60-70s silence and restarted the runner twice
        # (2026-09-05 15:36 and 15:48, both while two of three members were quarantined). A
        # balance we cannot read inside the budget is unreadable for this pass: return None, act
        # on nothing, and let the next pass in 15s try again with a healthier pool.
        if deadline_s > 0 and i and (_time.monotonic() - started) > deadline_s:
            if log is not None:
                try:
                    log("EVM gas top-up: native reads exceeded %.1fs budget after %d read(s) -- "
                        "not acting on this pass", deadline_s, i)
                except Exception:
                    pass
            return None
        try:
            v = read_fn()
        except Exception:
            v = None
        if v is None:
            return None
        values.append(int(v))
        target, raw = last_answer()
        who.append((target.split("/")[2] if isinstance(target, str) and "//" in target else target, raw))
    lo, hi = min(values), max(values)
    if hi > 0 and (hi - lo) > hi * tolerance:
        if log is not None:
            try:
                log("EVM gas top-up: native reads DISAGREE %s -- not acting on this pass", values)
                log("EVM gas top-up: native reads by member %s", who)
            except Exception:
                pass
        return None
    return values[-1]


def evm_gas_topup_once(*, chain_id, address, weth_token, rpc_url,
                       native_balance_fn, weth_balance_fn,
                       nonce_fn, gas_price_fn, sign_fn, send_fn, confirm_fn,
                       low_water_wei, target_wei, min_weth_reserve_wei,
                       daily_cap_wei, spent_today_wei, gas_price_buffer=1.5,
                       open_position_count=0,
                       record_submission_fn=None, finish_fn=None, alert_fn=None,
                       claim_nonce_fn=None, release_nonce_fn=None):
    """Unwrap WETH to native gas on one funded EVM chain, at most once per call.

    Every seam is injected, so this runs end to end in a test with no chain and no key. Returns the
    plan dict with `tx_hash` and `status` added when it acted.
    """
    plan = {"act": False, "state": "unknown", "amount_wei": 0, "chain_id": str(chain_id)}
    native = native_balance_fn()
    weth = weth_balance_fn()
    decided = plan_gas_topup(
        native_balance_wei=native, weth_balance_wei=weth,
        low_water_wei=low_water_wei, target_wei=target_wei,
        min_weth_reserve_wei=min_weth_reserve_wei,
        daily_cap_wei=daily_cap_wei, spent_today_wei=spent_today_wei,
        open_position_count=open_position_count)
    plan.update(decided)
    plan["native_before_wei"] = native
    if not decided["act"]:
        return plan

    nonce = nonce_fn()
    gas_price = gas_price_fn()
    if nonce is None or gas_price is None:
        plan["act"] = False
        plan["state"] = "nonce_or_gasprice_unreadable"
        return plan

    if claim_nonce_fn:
        nonce = claim_nonce_fn(nonce)
        if nonce is None:
            plan.update(act=False, state="nonce_unreadable")
            return plan
    tx = {"to": weth_token,
          "data": encode_withdraw(decided["amount_wei"]),
          "value": 0,
          "gas": UNWRAP_GAS_LIMIT,
          "gasPrice": int(float(gas_price) * float(gas_price_buffer)),
          "nonce": int(nonce),
          "chainId": int(chain_id)}
    try:
        signed = sign_fn(tx)
    except Exception:
        if claim_nonce_fn and release_nonce_fn:
            release_nonce_fn(nonce)
        raise
    if not (isinstance(signed, tuple) and len(signed) == 2 and signed[0] and signed[1]):
        plan["act"] = False
        plan["state"] = "sign_failed"
        if claim_nonce_fn and release_nonce_fn:
            release_nonce_fn(nonce)
        return plan
    raw, tx_hash = signed
    plan.update({"tx_hash": str(tx_hash), "nonce": int(nonce)})
    if record_submission_fn:
        try:
            record_submission_fn(plan)
        except Exception:
            plan["act"] = False
            plan["state"] = "journal_failed"
            if claim_nonce_fn and release_nonce_fn:
                release_nonce_fn(nonce)
            return plan

    accepted = send_fn(raw)
    if accepted is None:
        plan.update({"status": "submitted_unknown", "state": "send_unconfirmed"})
    elif accepted is False:
        if claim_nonce_fn and release_nonce_fn:
            release_nonce_fn(nonce)
        plan.update({"status": "not_sent", "state": "send_rejected"})
    elif str(accepted).lower() not in {str(tx_hash).lower(), "already_known"}:
        plan.update({"status": "submitted_unknown", "state": "send_hash_mismatch"})
    else:
        ok = confirm_fn(tx_hash) if confirm_fn else None
        if ok is True or ok == "confirmed":
            plan.update({"status": "confirmed", "state": "topup"})
        elif ok is False or ok == "failed":
            plan.update({"status": "failed", "state": "failed"})
        else:
            plan.update({"status": "submitted_unknown", "state": "submitted_unknown"})
    if finish_fn:
        try:
            finish_fn(plan)
        except Exception:
            pass
    if alert_fn and plan.get("status") != "confirmed":
        try:
            alert_fn(plan)
        except Exception:
            pass
    return plan


def evm_gas_reclaim_once(*, chain_id, address, weth_token, rpc_url,
                         native_balance_fn, nonce_fn, gas_price_fn, sign_fn, send_fn, confirm_fn,
                         target_wei, record_submission_fn=None, finish_fn=None, alert_fn=None,
                         floor_gas=0, claim_nonce_fn=None, release_nonce_fn=None):
    """Wrap surplus native ETH back into WETH cash, at most once per call."""
    plan = {"act": False, "state": "unknown", "amount_wei": 0,
            "chain_id": str(chain_id), "direction": "wrap"}
    native = native_balance_fn()
    gas_price = gas_price_fn()
    decided = plan_gas_reclaim(
        native_balance_wei=native, target_wei=target_wei, gas_price_wei=gas_price,
        floor_gas=floor_gas)
    plan.update(decided)
    plan["native_before_wei"] = native
    if not decided["act"]:
        return plan
    nonce = nonce_fn()
    if nonce is None:
        plan["act"] = False
        plan["state"] = "nonce_unreadable"
        return plan
    if claim_nonce_fn:
        nonce = claim_nonce_fn(nonce)
        if nonce is None:
            plan.update(act=False, state="nonce_unreadable")
            return plan
    tx = {"to": weth_token, "data": DEPOSIT_SELECTOR, "value": decided["amount_wei"],
          "gas": WRAP_GAS_LIMIT, "gasPrice": int(gas_price), "nonce": int(nonce),
          "chainId": int(chain_id)}
    try:
        signed = sign_fn(tx)
    except Exception:
        if claim_nonce_fn and release_nonce_fn:
            release_nonce_fn(nonce)
        raise
    if not (isinstance(signed, tuple) and len(signed) == 2 and signed[0] and signed[1]):
        plan["act"] = False
        plan["state"] = "sign_failed"
        if claim_nonce_fn and release_nonce_fn:
            release_nonce_fn(nonce)
        return plan
    raw, tx_hash = signed
    plan.update({"tx_hash": str(tx_hash), "nonce": int(nonce)})
    if record_submission_fn:
        try:
            record_submission_fn(plan)
        except Exception:
            plan["act"] = False
            plan["state"] = "journal_failed"
            if claim_nonce_fn and release_nonce_fn:
                release_nonce_fn(nonce)
            return plan
    accepted = send_fn(raw)
    if accepted is None:
        plan.update({"status": "submitted_unknown", "state": "send_unconfirmed"})
    elif accepted is False:
        if claim_nonce_fn and release_nonce_fn:
            release_nonce_fn(nonce)
        plan.update({"status": "not_sent", "state": "send_rejected"})
    elif str(accepted).lower() not in {str(tx_hash).lower(), "already_known"}:
        plan.update({"status": "submitted_unknown", "state": "send_hash_mismatch"})
    else:
        ok = confirm_fn(tx_hash) if confirm_fn else None
        if ok is True or ok == "confirmed":
            plan.update({"status": "confirmed", "state": "reclaim"})
        elif ok is False or ok == "failed":
            plan.update({"status": "failed", "state": "failed"})
        else:
            plan.update({"status": "submitted_unknown", "state": "submitted_unknown"})
    if finish_fn:
        try:
            finish_fn(plan)
        except Exception:
            pass
    if alert_fn and plan.get("status") != "confirmed":
        try:
            alert_fn(plan)
        except Exception:
            pass
    return plan


def run_evm_gas_topup_pass(conn, *, context, now_iso, low_water_wei, target_wei,
                           min_weth_reserve_wei, daily_cap_wei, lock_dir=None,
                           reclaim_floor_gas=0, hold_under_reservation=False,
                           hold_until_receipt_s=0.0, balance_bound_base=None):
    """Run one non-blocking, chain-scoped EVM gas maintenance pass.

    A submitted transaction retains its WETH amount until a receipt proves a terminal state. This
    pass checks that receipt first. It never signs a replacement for an unknown transaction.

    The last three arguments come from gas_pass_bounds for a chain other than 4663 (2026-09-30);
    their defaults are 4663's pass, unchanged. `hold_under_reservation` (Alpha's chain-1 rule,
    beta/eth-mainnet 808b0bc37, now 56's too): while ANY reservation on the chain is unresolved the
    pass does nothing at all, before any read and again under the lock. An unwrap or a re-wrap moves
    the WETH and native balances an in-flight entry is sized and proved against, and a flap entry's
    confirmed unwrap is native that entry still needs. `hold_until_receipt_s` keeps the wallet lock
    until our receipt or the time budget expires. An unknown transaction remains in the durable
    gas ledger; funded entries and exits check that debt under the same lock before executing.
    `balance_bound_base` optionally caps an added native float against this pass's liquid balances,
    before deciding whether to try and again from both fresh reads under the custody lock.
    """
    from sniper.execution.exec_lock import wallet_exec_lock
    from sniper.persistence import db_memecoin as m

    chain_id = str((context or {}).get("chain") or "")
    if hold_under_reservation and chain_id:
        held = _reservation_hold(conn, chain_id)
        if held:
            return held
    address = str((context or {}).get("address") or "")
    seams = (context or {}).get("gas_topup") or {}
    required = (
        "native_balance_raw_fn", "weth_balance_raw_fn", "nonce_fn", "gas_price_fn",
        "sign_fn", "send_fn", "confirm_once_fn", "weth_token", "rpc_url",
    )
    if not chain_id or not address or any(not callable(seams.get(name)) for name in required[:7]):
        return {"state": "context_unavailable", "chain_id": chain_id}
    _native_memo = {}

    def _guarded_native():
        # One read series per pass. The top-up branch and the reclaim branch both ask, and they
        # decide on disjoint bands of the same balance, so the second series bought nothing but a
        # second helping of the 12s-per-read wait that stalled the main loop (2026-09-05).
        if "v" not in _native_memo:
            read = seams["native_balance_raw_fn"]
            if balance_bound_base is not None:
                read = lambda: _atomic_balance_wei(seams["native_balance_raw_fn"]())
            _native_memo["v"] = agreeing_native_read(read, log=logger.warning)
        return _native_memo["v"]

    if not seams.get("weth_token") or not seams.get("rpc_url"):
        return {"state": "context_unavailable", "chain_id": chain_id}

    unresolved = m.unresolved_evm_gas_topups(conn, chain_id)
    if unresolved:
        unresolved_row = unresolved[0]
        try:
            receipt_state = seams["confirm_once_fn"](unresolved_row["tx_hash"])
        except Exception:
            receipt_state = None
        if receipt_state == "confirmed":
            m.finish_evm_gas_topup(conn, row_id=unresolved_row["id"], status="confirmed")
            return {"state": "recovered_confirmed", "chain_id": chain_id}
        if receipt_state == "failed":
            m.finish_evm_gas_topup(conn, row_id=unresolved_row["id"], status="failed")
            return {"state": "recovered_failed", "chain_id": chain_id}
        if receipt_state == "not_found" and _topup_provably_dead(seams, unresolved_row):
            # A POSITIVE "no such transaction" IS NOT PROOF OF DEATH ON ITS OWN -- the transaction
            # may simply not have propagated to the node we asked. It BECOMES proof once the
            # account has moved past this nonce, because two transactions cannot share one.
            #
            # Only `confirmed` and `failed` used to clear this branch, and `eth_receipt_state`
            # returns neither for a transaction that was never accepted. Row 42 (chain 4663,
            # 2026-09-04) sat `submitted_unknown` at nonce 1432 while the account stood at 1435 --
            # unmineable, unreadable, and blocking EVERY gas top-up on the chain for as long as it
            # stayed. Gas is what an exit spends, so a permanently blocked top-up strands money
            # exactly when a position needs to close.
            m.finish_evm_gas_topup(conn, row_id=unresolved_row["id"], status="failed")
            return {"state": "recovered_never_admitted", "chain_id": chain_id,
                    "tx_hash": unresolved_row["tx_hash"]}
        return {"state": "unresolved", "chain_id": chain_id,
                "tx_hash": unresolved_row["tx_hash"]}

    day = str(now_iso)[:10]

    def _record_submission(plan):
        plan["record_id"] = m.record_evm_gas_topup_submission(
            conn, now_iso=now_iso, day=day, chain_id=chain_id,
            native_amount_atomic=str(int(plan["amount_wei"])),
            native_before_atomic=str(int(plan["native_before_wei"])),
            tx_hash=plan["tx_hash"], nonce=plan["nonce"],
            direction=plan.get("direction", "unwrap"),
        )

    def _finish(plan):
        record_id = plan.get("record_id")
        if record_id is not None:
            m.finish_evm_gas_topup(conn, row_id=record_id, status=plan["status"])

    def _open_count():
        # OPEN FUNDED POSITIONS ON THIS CHAIN. Fail-safe to 0: an unreadable ledger must leave
        # the reserve absolute, never release it on a guess.
        try:
            return int(m.open_real_position_count(conn, chain_id)) \
                if hasattr(m, "open_real_position_count") else int(conn.execute(
                    "SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                    "AND is_open=1 AND network_id=?", (str(chain_id),)).fetchone()[0])
        except Exception:
            logger.exception("gas top-up: open-position read failed chain=%s", chain_id)
            return 0

    strict = (hold_under_reservation or hold_until_receipt_s > 0 or reclaim_floor_gas > 0
              or balance_bound_base is not None)
    if short_lock_enabled() or strict:
        return _short_lock_pass(
            conn, chain_id=chain_id, address=address, seams=seams, day=day,
            guarded_native=_guarded_native, open_count_fn=_open_count,
            record_submission_fn=_record_submission, finish_fn=_finish,
            low_water_wei=low_water_wei, target_wei=target_wei,
            min_weth_reserve_wei=min_weth_reserve_wei, daily_cap_wei=daily_cap_wei,
            lock_dir=lock_dir, reclaim_floor_gas=reclaim_floor_gas,
            hold_under_reservation=hold_under_reservation,
            hold_until_receipt_s=hold_until_receipt_s, balance_bound_base=balance_bound_base)

    with wallet_exec_lock("evm-%s-%s" % (chain_id, address), lock_dir=lock_dir,
                          blocking=False) as held:
        if not held:
            return {"state": "wallet_busy", "chain_id": chain_id}
        if m.unresolved_holding_sell_count(conn, chain_id):
            return {"state": "holding_unresolved", "chain_id": chain_id}
        if m.has_unresolved_real_exit(conn, chain_id):
            return {"state": "exit_unresolved", "chain_id": chain_id}
        if m.has_unresolved_evm_gas_topup(conn, chain_id):
            return {"state": "unresolved", "chain_id": chain_id}
        spent_today = m.evm_gas_topup_spent_today(conn, chain_id, day)
        open_count = _open_count()
        result = evm_gas_topup_once(
            open_position_count=open_count,
            chain_id=chain_id, address=address, weth_token=seams["weth_token"],
            rpc_url=seams["rpc_url"], native_balance_fn=_guarded_native,
            weth_balance_fn=seams["weth_balance_raw_fn"], nonce_fn=seams["nonce_fn"],
            gas_price_fn=seams["gas_price_fn"], sign_fn=seams["sign_fn"],
            send_fn=seams["send_fn"], confirm_fn=seams["confirm_once_fn"],
            low_water_wei=low_water_wei, target_wei=target_wei,
            min_weth_reserve_wei=min_weth_reserve_wei, daily_cap_wei=daily_cap_wei,
            spent_today_wei=spent_today, gas_price_buffer=1.0,
            record_submission_fn=_record_submission, finish_fn=_finish,
            claim_nonce_fn=seams.get("claim_nonce_fn"),
            release_nonce_fn=seams.get("release_nonce_fn"),
        )
        if result.get("state") != "above_low_water":
            return result
        return evm_gas_reclaim_once(
            chain_id=chain_id, address=address, weth_token=seams["weth_token"],
            rpc_url=seams["rpc_url"], native_balance_fn=_guarded_native,
            nonce_fn=seams["nonce_fn"], gas_price_fn=seams["gas_price_fn"],
            sign_fn=seams["sign_fn"], send_fn=seams["send_fn"],
            confirm_fn=seams["confirm_once_fn"], target_wei=target_wei,
            record_submission_fn=_record_submission, finish_fn=_finish,
            claim_nonce_fn=seams.get("claim_nonce_fn"),
            release_nonce_fn=seams.get("release_nonce_fn"),
        )


# --- its own thread (2026-09-27, Alpha GO B1) ------------------------------------------------------

def thread_enabled(env=None):
    """MEMECOIN_EVM_GAS_TOPUP_THREAD=0 runs the pass inline in the maintenance loop, as before."""
    env = os.environ if env is None else env
    return str(env.get("MEMECOIN_EVM_GAS_TOPUP_THREAD", "1")).strip().lower() not in {
        "0", "false", "no", "off"}


QUIET_STATES = ("above_low_water", "wallet_busy", None)


class EvmGasTopupWorker:
    """The EVM gas top-up pass on its own thread and its own ledger connection.

    WHY (2026-09-27): the pass ran inline in the funded main loop and took 58.8 s and 40.6 s at
    12:35Z (hedged 12 s reads against a failing endpoint set; its 8 s read budget is checked only
    between reads). A slow pass now costs this thread, never the loop's exits or entries. A pass is
    never abandoned: the thread runs each one to completion. A kill mid-send is covered by the
    write-ahead row and the unresolved-row recovery at the start of the next pass, as before.
    """

    def __init__(self, *, pass_fn, connect, now_iso, period_s, log=print, sleep=time.sleep,
                 clock=time.monotonic):
        self._pass_fn, self._connect, self._now_iso = pass_fn, connect, now_iso
        self.period_s = max(1.0, float(period_s))
        self._log, self._sleep, self._clock = log, sleep, clock
        self.passes = self.failures = 0
        self.last_pass_at = self.last_pass_s = self.last_state = self.last_error = None

    def run_once(self, conn):
        started = self._clock()
        try:
            result = self._pass_fn(conn, self._now_iso()) or {}
            self.last_state = result.get("state")
            if result.get("state") not in QUIET_STATES:
                self._log("[memecoin] EVM gas top-up: %s" % result)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            self.failures += 1
            self.last_error = type(exc).__name__
            self._log("[err] EVM gas top-up failed:\n" + traceback.format_exc())
        else:
            self.passes += 1
        self.last_pass_at = self._now_iso()
        self.last_pass_s = round(self._clock() - started, 2)
        return self._clock() - started

    def connect_with_retry(self):
        """The ledger connection, retried every period until it opens. A failure is logged once and
        counted; the thread never dies on it."""
        logged = False
        while True:
            try:
                return self._connect()
            except Exception:
                self.failures += 1
                self.last_error = "connect_failed"
                if not logged:
                    self._log("[err] EVM gas top-up: ledger connect failed; retrying every %.0fs:\n%s"
                              % (self.period_s, traceback.format_exc()))
                    logged = True
                self._sleep(self.period_s)

    def run_forever(self):
        conn = self.connect_with_retry()
        while True:
            try:
                took = self.run_once(conn)
            except Exception:                   # a fault outside the pass must not end the thread
                self._log("[err] EVM gas top-up loop:\n" + traceback.format_exc())
                took = 0.0
            self._sleep(max(1.0, self.period_s - took))

    def status(self):
        return {"last_pass_at": self.last_pass_at, "last_pass_s": self.last_pass_s,
                "last_state": self.last_state, "passes": self.passes, "failures": self.failures,
                "last_error": self.last_error}


# --- the wallet lock held only to decide, sign and send (2026-09-27, Alpha GO B2) --------------------
#
# Funded 4663 entries and exits take the SAME flock ("evm-<chain>-<addr>") BLOCKING, through
# serialize_executor. The pass used to take it and THEN read native (up to three hedged reads, 12 s
# each) and WETH (12 s), so a slow read made a funded exit wait behind it. Now:
#   outside the lock  the agreed native read, a WETH read and (for a reclaim) a gas price decide only
#                     WHETHER to try;
#   inside the lock   native is read once more and must agree with the outside value, WETH is read
#                     again, and those two decide the AMOUNT; the nonce and the gas price are read
#                     here too; each read has an INSIDE_READ_DEADLINE_S wall-clock deadline;
#   after release     the single-shot receipt check settles the write-ahead row, as the in-lock
#                     check did. An unknown receipt leaves the row for the next pass, as before.
# MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK=0 restores the old lock scope.

INSIDE_READ_DEADLINE_S = 2.0


def short_lock_enabled(env=None):
    env = os.environ if env is None else env
    return str(env.get("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", "1")).strip().lower() not in {
        "0", "false", "no", "off"}


def _read_within(read_fn, deadline_s):
    """One read held to a wall-clock deadline (urllib's timeout does not bound DNS). None past it or
    on an error; a late read finishes on its own thread and is discarded."""
    import threading
    box = {}

    def run():
        try:
            box["v"] = read_fn()
        except Exception:
            box["v"] = None
    worker = threading.Thread(target=run, name="evm-gas-topup-read", daemon=True)
    worker.start()
    worker.join(deadline_s)
    return None if worker.is_alive() else box.get("v")


def _read_balances_within(native_fn, weth_fn, deadline_s):
    """Two independent balance reads share one custody-lock deadline; late answers stay unknown."""
    import threading
    deadline = time.monotonic() + float(deadline_s)
    values, completed, workers = [None, None], [None, None], []

    def read(index, fn):
        try:
            values[index] = fn()
        except Exception:
            values[index] = None
        completed[index] = time.monotonic()

    for index, fn in enumerate((native_fn, weth_fn)):
        worker = threading.Thread(target=read, args=(index, fn),
                                  name="evm-gas-balance-read", daemon=True)
        try:
            worker.start()
        except RuntimeError:
            continue
        workers.append(worker)
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    return tuple(value if at is not None and at <= deadline else None
                 for value, at in zip(values, completed))


def _agrees(a, b, tolerance=_AGREE_TOLERANCE):
    hi = max(int(a), int(b))
    return hi <= 0 or abs(int(a) - int(b)) <= hi * tolerance


def _settle_under_lock(conn, seams, result, mode, budget_s, *, poll_s=0.25, clock=None,
                       sleep=None):
    """Poll our own transaction's receipt while the wallet lock is still held, up to `budget_s`.
    A confirmed or failed receipt settles the write-ahead row here; past the budget the row stays
    submitted_unknown and the after-release check or the next pass settles it, as before. An
    ambiguous send (no answer, or a different hash) is polled too: it may still land."""
    from sniper.persistence import db_memecoin as m
    if result.get("status") != "submitted_unknown":
        return
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline = clock() + float(budget_s)
    while True:
        receipt = _read_within(lambda: seams["confirm_once_fn"](result["tx_hash"]),
                               max(0.1, min(INSIDE_READ_DEADLINE_S, deadline - clock())))
        status = ("confirmed" if receipt is True or receipt == "confirmed"
                  else "failed" if receipt is False or receipt == "failed" else None)
        if status is not None:
            if result.get("record_id") is not None:
                m.finish_evm_gas_topup(conn, row_id=result["record_id"], status=status)
            result.update(status=status, state=(mode if status == "confirmed" else "failed"))
            return
        if clock() + poll_s >= deadline:
            return
        sleep(poll_s)


def _reservation_hold(conn, chain_id):
    """A do-nothing result while the chain has an unsettled funded reservation, else None. An
    unreadable ledger holds too: 'we could not look' is not 'nothing is in flight'."""
    from sniper.persistence import db_memecoin as m
    try:
        in_flight = int(m.pending_totals(conn, network=str(chain_id))[0])
    except Exception:
        logger.exception("gas top-up: reservation read failed chain=%s", chain_id)
        return {"state": "reservation_read_failed", "chain_id": str(chain_id)}
    if in_flight:
        return {"state": "reservation_in_flight", "chain_id": str(chain_id),
                "reservations": in_flight}
    return None


def _short_lock_pass(conn, *, chain_id, address, seams, day, guarded_native, open_count_fn,
                     record_submission_fn, finish_fn, low_water_wei, target_wei,
                     min_weth_reserve_wei, daily_cap_wei, lock_dir, reclaim_floor_gas=0,
                     hold_under_reservation=False, hold_until_receipt_s=0.0,
                     balance_bound_base=None):
    from sniper.execution.exec_lock import wallet_exec_lock
    from sniper.persistence import db_memecoin as m

    # OUTSIDE THE LOCK: whether to try. Nothing read here sizes the spend.
    native_out = guarded_native()
    weth_out = seams["weth_balance_raw_fn"]()
    configured = {"low_water_wei": low_water_wei, "target_wei": target_wei}
    outside_bounds = configured
    if balance_bound_base is not None:
        outside_bounds = balance_bound_float(configured, base_bounds=balance_bound_base,
                                             native_balance_wei=native_out,
                                             weth_balance_wei=weth_out)
        if outside_bounds is None:
            return {"act": False, "state": "liquid_balance_unreadable", "chain_id": chain_id}
    gate = plan_gas_topup(
        native_balance_wei=native_out, weth_balance_wei=weth_out,
        **outside_bounds,
        min_weth_reserve_wei=min_weth_reserve_wei, daily_cap_wei=daily_cap_wei,
        spent_today_wei=m.evm_gas_topup_spent_today(conn, chain_id, day),
        open_position_count=open_count_fn())
    if gate["act"]:
        mode = "topup"
    elif gate["state"] != "above_low_water":
        # weth_wei rides along for the GAS-LOW page (real_runner.check_evm_gas_alert): no new read.
        return {**gate, "chain_id": str(chain_id), "native_before_wei": native_out,
                "weth_wei": weth_out}
    else:
        rgate = plan_gas_reclaim(native_balance_wei=native_out,
                                 target_wei=outside_bounds["target_wei"],
                                 gas_price_wei=seams["gas_price_fn"](),
                                 floor_gas=reclaim_floor_gas)
        if not rgate["act"]:
            return {**rgate, "chain_id": str(chain_id), "native_before_wei": native_out,
                    "weth_wei": weth_out}
        mode = "reclaim"

    within = INSIDE_READ_DEADLINE_S
    with wallet_exec_lock("evm-%s-%s" % (chain_id, address), lock_dir=lock_dir,
                          blocking=False) as held:
        if not held:
            return {"state": "wallet_busy", "chain_id": chain_id}
        locked_at = time.monotonic()
        if m.unresolved_holding_sell_count(conn, chain_id):
            return {"state": "holding_unresolved", "chain_id": chain_id}
        if m.has_unresolved_real_exit(conn, chain_id):
            return {"state": "exit_unresolved", "chain_id": chain_id}
        if m.has_unresolved_evm_gas_topup(conn, chain_id):
            return {"state": "unresolved", "chain_id": chain_id}
        held = _reservation_hold(conn, chain_id) if hold_under_reservation else None
        if held:
            return held
        weth_in = None
        if balance_bound_base is not None:
            native_in, weth_in = _read_balances_within(
                seams["native_balance_raw_fn"], seams["weth_balance_raw_fn"], within)
            native_in = _atomic_balance_wei(native_in)
        else:
            native_in = _read_within(seams["native_balance_raw_fn"], within)
        if native_in is None or native_out is None or not _agrees(native_in, native_out):
            return {"act": False, "state": "native_moved_or_unreadable_under_lock",
                    "chain_id": str(chain_id), "native_before_wei": native_out,
                    "native_under_lock_wei": native_in}
        if balance_bound_base is not None:
            # Reclaim also needs fresh wrapped cash: a changed balance changes its native cap.
            inside_bounds = balance_bound_float(configured, base_bounds=balance_bound_base,
                                                native_balance_wei=native_in,
                                                weth_balance_wei=weth_in)
            if inside_bounds is None:
                return {"act": False, "state": "liquid_balance_unreadable", "chain_id": chain_id}
            low_water_wei, target_wei = inside_bounds["low_water_wei"], inside_bounds["target_wei"]
            inside_gate = plan_gas_topup(
                native_balance_wei=native_in, weth_balance_wei=weth_in,
                low_water_wei=low_water_wei, target_wei=target_wei,
                min_weth_reserve_wei=min_weth_reserve_wei, daily_cap_wei=daily_cap_wei,
                spent_today_wei=m.evm_gas_topup_spent_today(conn, chain_id, day),
                open_position_count=open_count_fn())
            if inside_gate["act"]:
                mode = "topup"
            elif inside_gate["state"] == "above_low_water":
                mode = "reclaim"
            else:
                return {**inside_gate, "chain_id": chain_id}
        common = dict(
            chain_id=chain_id, address=address, weth_token=seams["weth_token"],
            rpc_url=seams["rpc_url"], native_balance_fn=lambda: native_in,
            nonce_fn=lambda: _read_within(seams["nonce_fn"], within),
            gas_price_fn=lambda: _read_within(seams["gas_price_fn"], within),
            sign_fn=seams["sign_fn"], send_fn=seams["send_fn"], confirm_fn=None,
            target_wei=target_wei, record_submission_fn=record_submission_fn,
            finish_fn=finish_fn, claim_nonce_fn=seams.get("claim_nonce_fn"),
            release_nonce_fn=seams.get("release_nonce_fn"))
        if mode == "topup":
            if (balance_bound_base is not None and balance_bound_base.get("native_spendable")
                    and inside_gate["state"] != "reserve_released_for_exit"):
                gas_price = _atomic_balance_wei(_read_within(seams["gas_price_fn"], within))
                if gas_price is None or gas_price <= 0:
                    return {"act": False, "state": "gas_price_unreadable", "chain_id": chain_id}
                # A conservative maximum fee must neither exceed the cash prepared nor its gas.
                fee = UNWRAP_GAS_LIMIT * gas_price
                if inside_gate["amount_wei"] <= fee:
                    return {"act": False, "state": "topup_not_economic", "chain_id": chain_id}
                if native_in < fee:
                    return {"act": False, "state": "topup_gas_short", "chain_id": chain_id}
                common["gas_price_fn"] = lambda: gas_price
            if balance_bound_base is None:
                weth_in = _read_within(seams["weth_balance_raw_fn"], within)
            result = evm_gas_topup_once(
                weth_balance_fn=lambda: weth_in, low_water_wei=low_water_wei,
                min_weth_reserve_wei=min_weth_reserve_wei, daily_cap_wei=daily_cap_wei,
                spent_today_wei=m.evm_gas_topup_spent_today(conn, chain_id, day),
                open_position_count=open_count_fn(), gas_price_buffer=1.0, **common)
        else:
            result = evm_gas_reclaim_once(floor_gas=reclaim_floor_gas, **common)
        if hold_until_receipt_s > 0:
            _settle_under_lock(conn, seams, result, mode, hold_until_receipt_s)
            result["lock_held_ms"] = round((time.monotonic() - locked_at) * 1000.0, 1)

    # AFTER RELEASE: one receipt read settles the write-ahead row, as the in-lock check did.
    if result.get("status") != "submitted_unknown" or result.get("state") != "submitted_unknown":
        return result
    try:
        receipt = seams["confirm_once_fn"](result["tx_hash"])
    except Exception:
        receipt = None
    status = ("confirmed" if receipt is True or receipt == "confirmed"
              else "failed" if receipt is False or receipt == "failed" else None)
    if status is None:
        return result                        # stays submitted_unknown; the next pass settles it
    if result.get("record_id") is not None:
        m.finish_evm_gas_topup(conn, row_id=result["record_id"], status=status)
    result.update(status=status, state=(mode if status == "confirmed" else "failed"))
    return result
