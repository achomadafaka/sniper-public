"""Skip the WETH unwrap when the wallet's native ETH already covers the buy (2026-09-27).

Spec: docs/superpowers/specs/2026-09-27-memecoin-4663-native-float-design.md. The V4, pons and
launchpad builders attach `WETH.withdraw(amount)` to every WETH-quoted buy on a native-paid pool,
and the executor must see it CONFIRMED before the swap can spend its ETH: p50 668 ms / p90 1.32 s on
84 of 112 funded 4663 fills since 09-13. When MEMECOIN_EVM_NATIVE_FLOAT_CHAINS names the chain and
native >= amount + gas reserve, the unwrap is skipped. The swap transaction is unchanged.

Every unknown keeps the unwrap: an unreadable native balance, an unreadable gas price, a read that
has not finished in time. A read that was wrong-high makes the node refuse the swap for
insufficient funds; `insufficient_native` recognises that refusal so the executor can fall back to
the unwrap once, inside the same entry.
"""
from __future__ import annotations

import threading
import time
from contextvars import ContextVar

# FLAP NO_READ (2026-10-21, Alpha/Gamma): 6 of 17 flap fills kept the unwrap because the probe read
# had not finished ~50 ms after it started (p50 2,070 ms vs 1,211 ms). A fast lane sets this to
# (chain, fn) around its hook call: fn returns its own fresh wallet snapshot {native_wei,
# gas_price_wei, age_ms} or None. Per call and per thread; never module state.
LANE_SNAPSHOT = ContextVar("lane_native_snapshot", default=None)

WITHDRAW_SELECTOR = "0x2e1a7d4d"
# The words a 4663 node answers with, memecoin_submission_attempts 2026-09-04..09-17 (entries,
# exits and cleanups, all `explicit_rejected`): "insufficient funds for gas * price + value:
# address 0x... have N want M".
_INSUFFICIENT = "insufficient funds for gas * price + value"
_GAS_RESERVE_SWAPS = 3                  # an exit must still have gas after this entry
READ_WAIT_S = 1.0                       # how long `covers` waits for the balance read


def unwrap_amount(pre, quote_token):
    """The atomic amount of an exact `WETH.withdraw(amount)` of `quote_token`, else None."""
    try:
        data = str(pre.get("data") or "").lower()
        if (str(pre.get("to") or "").lower() != str(quote_token or "").lower()
                or not data.startswith(WITHDRAW_SELECTOR) or len(data) != 74):
            return None
        value = pre.get("value") or 0
        value = int(value, 16) if isinstance(value, str) else int(value)
        amount = int(data[10:], 16)
    except (AttributeError, TypeError, ValueError):
        return None
    return amount if amount > 0 and value == 0 else None


def float_covers(native_wei, amount_wei, gas_limit, gas_price_wei, low_wei):
    """True only when every input is known and native covers amount + a 3-swap gas reserve + low."""
    try:
        native, amount = int(native_wei), int(amount_wei)
        reserve = int(gas_limit) * int(gas_price_wei) * _GAS_RESERVE_SWAPS + int(low_wei)
    except (TypeError, ValueError):
        return False
    return native > 0 and amount > 0 and native >= amount + reserve


def insufficient_native(detail):
    """True when a node's refusal text says the account could not pay gas * price + value."""
    return bool(detail) and _INSUFFICIENT in str(detail).lower()


class NativeFloatProbe:
    """Read native on a worker; a caller may consume only an already-completed read."""

    def __init__(self, read_fn, *, quote_token, gas_price_fn, low_wei, partial=False,
                 reserve_floor_wei=None, reserve_floor_fn=None, wait_for_read=True, book_facts=None,
                 snapshot_fn=None):
        self._quote_token, self._gas_price_fn, self._low_wei = quote_token, gas_price_fn, low_wei
        self._partial = bool(partial)          # MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS (2026-10-08)
        self._wait_for_read = bool(wait_for_read)
        # A spendable chain requires its sizing reserve; BSC's plain float supplies a book floor too.
        self._floor = reserve_floor_wei
        self._reserve_floor_fn = reserve_floor_fn
        self._floor_done = reserve_floor_fn is None
        self._snapshot_fn = snapshot_fn       # consulted only while the read is still pending
        self._read_at = time.monotonic()
        self._read_ms = None
        self._native = None
        self._used_native = None
        self._decision_facts = {}
        self._book_facts = book_facts if book_facts is not None else {}
        self._thread = threading.Thread(target=self._read, args=(read_fn,),
                                        name="native-float-read", daemon=True)
        self._thread.start()

    def _read(self, read_fn):
        # The local book floor first (milliseconds), so a pending native RPC can still be replaced
        # by a lane snapshot with the full-book reserve known. A failed floor leaves native unknown.
        try:
            if self._reserve_floor_fn is not None:
                self._floor = max(int(self._floor or 0), int(self._reserve_floor_fn()))
                self._floor_done = True
            self._native = read_fn()
        except Exception:
            self._native = None
        finally:
            self._read_ms = (time.monotonic() - self._read_at) * 1000.0

    def _snapshot(self):
        """The lane's fresh snapshot when the floor is known and every field is a positive int."""
        if self._snapshot_fn is None or not self._floor_done:
            return None
        try:
            snap = self._snapshot_fn()
        except Exception:
            return None
        if not isinstance(snap, dict) or any(
                type(snap.get(k)) is not int or snap[k] <= 0 for k in ("native_wei", "gas_price_wei")):
            return None
        return snap

    @property
    def decision_facts(self):
        """Only inputs consumed by the lane, never a later worker completion."""
        return dict(self._decision_facts)

    def _inputs(self, pre, tx):
        """(amount, gas_limit, gas_price) when the read finished and the swap spends exactly what the
        unwrap would free, else None."""
        self._decision_facts = dict(native_balance_wei=None, swap_amount_wei=None,
                                    swap_gas_limit=None, gas_price_wei=None, book_floor_wei=None,
                                    required_native_keep_wei=None, shortfall_wei=None,
                                    computed_shortfall_wei=None, native_source=None,
                                    native_read_ms=None, native_read_elapsed_ms=None,
                                    snapshot_age_ms=None)
        self._used_native = None
        amount = unwrap_amount(pre, self._quote_token)
        if amount is None:
            return None
        if self._wait_for_read:
            self._thread.join(READ_WAIT_S)
        elif time.monotonic() - self._read_at >= READ_WAIT_S:
            return None
        self._decision_facts.update(
            native_read_ms=self._read_ms,
            native_read_elapsed_ms=(time.monotonic() - self._read_at) * 1000.0)
        snap = None
        if self._thread.is_alive():
            snap = self._snapshot()
            if snap is None:
                return None
        try:
            gas_price = self._gas_price_fn()
            if gas_price is None and snap is not None:
                gas_price = snap["gas_price_wei"]
            value = tx.get("value") or 0
            value = int(value, 16) if isinstance(value, str) else int(value)
            gas_limit = tx.get("gas") or 0
            gas_limit = int(gas_limit, 16) if isinstance(gas_limit, str) and gas_limit.startswith(
                "0x") else int(gas_limit)
        except Exception:
            return None
        if value != amount:                 # the swap must spend exactly what the unwrap freed
            return None
        self._used_native = snap["native_wei"] if snap is not None else self._native
        self._decision_facts.update(native_source="lane_snapshot" if snap is not None else "probe",
                                    snapshot_age_ms=snap.get("age_ms") if snap is not None else None)
        self._decision_facts.update(native_balance_wei=self._used_native, swap_amount_wei=amount,
                                    swap_gas_limit=gas_limit, gas_price_wei=gas_price,
                                    book_floor_wei=self._floor, **self._book_facts)
        return amount, gas_limit, gas_price

    def _low(self, gas_limit, gas_price):
        """Raise the gas keep to a supplied book floor. A partial probe needs a known floor."""
        if not self._partial and self._floor is None:
            return self._low_wei
        if self._floor is None:
            return None
        try:
            float_keep = int(gas_limit) * int(gas_price) * _GAS_RESERVE_SWAPS + int(self._low_wei)
            return int(self._low_wei) + max(0, int(self._floor) - float_keep)
        except (TypeError, ValueError):
            return None

    def covers(self, pre, tx):
        got = self._inputs(pre, tx)
        if got is None:
            return False
        amount, gas_limit, gas_price = got
        low = self._low(gas_limit, gas_price)
        if low is None:
            return False
        try:
            self._decision_facts["required_native_keep_wei"] = (
                int(gas_limit) * int(gas_price) * _GAS_RESERVE_SWAPS + int(low))
        except (TypeError, ValueError):
            pass
        return float_covers(self._used_native, amount, gas_limit, gas_price, low)

    def shortfall(self, pre, tx):
        """PARTIAL UNWRAP (chain-1 unstall, 2026-10-08): the wei to unwrap when the spare native
        (above the same 3-swap gas reserve + low that `covers` keeps) pays part of the swap, else
        None (today's full unwrap). Only on a probe built with partial=True; every unknown is None."""
        if not self._partial:
            return None
        got = self._inputs(pre, tx)
        if got is None:
            return None
        amount, gas_limit, gas_price = got
        low = self._low(gas_limit, gas_price)
        if low is None:
            return None
        try:
            spare = int(self._used_native) - int(gas_limit) * int(gas_price) * _GAS_RESERVE_SWAPS - int(low)
        except (TypeError, ValueError):
            return None
        self._decision_facts["required_native_keep_wei"] = int(self._used_native) - spare
        short = amount - spare
        self._decision_facts["computed_shortfall_wei"] = short
        self._decision_facts["shortfall_wei"] = short if spare > 0 and 0 < short < amount else None
        return self._decision_facts["shortfall_wei"]
