"""Solana hot tape — `accountSubscribe` on pool vaults (fastlane spec §2.1). FREE, no API key.

Measured 2026-07-24: `accountSubscribe` over `wss://api.mainnet-beta.solana.com` pushes a
notification every slot, median 404 ms / p90 790 ms, keyless. Every other free endpoint fails it
(dRPC paywalls it; Ankr/1RPC/BlockEden reject the socket; PublicNode ignores it). So this is the
single free push feed, and the always-running 30 s pull enumerator stays as verifier and gap-filler
— push can throttle, pull cannot. WS loss degrades to pull + envelope, never to blindness.

Why vaults and not transactions: a token-account notification carries the balance DIRECTLY, so
per-slot deltas of (coin vault, quote vault) ARE price + flow + depth with **zero getTransaction on
the hot path**. Per-slot netting matches the frozen per-slot VWAP grain (Solana prereg S1) exactly,
so there is no fidelity loss against the registered features.

Sign rule, live-verified (Solana prereg §0.1): opposite-sign coin/quote deltas = a swap; same-sign =
an LP event (`sol_mint` / `sol_burn`, the real depth-pull signal).

STRICTLY READ-ONLY: subscriptions only, on the read endpoint. It never signs, never submits, and
never touches `MEMECOIN_SOLANA_RPC_URL` (the variable that arms the funded Solana leg).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct

logger = logging.getLogger("memecoin")

DEFAULT_WS = "wss://api.mainnet-beta.solana.com"

# PUMP BONDING CURVES (2026-09-27, operator: "get these DexScreener prices out"). A held curve had
# no market price but the DexScreener mid (25-55 s behind). The curve account IS the price:
# virtual_sol / virtual_token. Decoded FAIL-CLOSED: the Anchor discriminator names the account, a
# bool byte and virtual >= real on both sides catch a misaligned layout, and pump's fixed total
# supply (1B tokens at 6 decimals) proves the token decimals from the data itself, so no mint read
# is needed on the stream thread. Anything unproven is no mark, never a guessed price.
CURVE_DISCRIMINATOR = hashlib.sha256(b"account:BondingCurve").digest()[:8]
PUMP_CURVE_TOTAL_SUPPLY = 1_000_000_000 * 10**6
PUMP_TOKEN_DECIMALS = 6


def curve_subscribe_frame(account, req_id):
    """`accountSubscribe` for a program account: base64, because jsonParsed cannot read it."""
    return json.dumps({
        "jsonrpc": "2.0", "id": int(req_id), "method": "accountSubscribe",
        "params": [str(account), {"encoding": "base64", "commitment": "confirmed"}],
    })


def decode_bonding_curve(data):
    """{virtual_token, virtual_sol, real_token, real_sol, complete} from a BondingCurve account,
    or None when any part of it is unproven."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 49:
        return None
    if bytes(data[:8]) != CURVE_DISCRIMINATOR:
        return None
    vt, vs, rt, rs, supply = struct.unpack_from("<5Q", data, 8)
    complete = data[48]
    if complete not in (0, 1) or supply != PUMP_CURVE_TOTAL_SUPPLY:
        return None
    state = {"virtual_token": vt, "virtual_sol": vs, "real_token": rt, "real_sol": rs,
             "complete": bool(complete)}
    if complete:
        # A GRADUATED curve (Alpha's live read of 65924, 2026-09-27) zeroes all four reserves.
        # Discriminator and supply prove the account; `complete` is the fact that matters, and
        # refusing the zeros as "empty" kept the pre-graduation price alive until the epoch clear.
        return state
    if vt <= 0 or vs <= 0 or vt < rt or vs < rs:
        return None
    return state


class CurveTape:
    """One bonding-curve account -> `sol_curve_state` rows. A seed read and the push stream can
    race: older rows and equal-slot seeds are refused; equal-slot pushes can still update it."""

    encoding = "base64"

    def __init__(self, curve, *, mint, chain_id="solana"):
        self.curve, self.mint, self.chain_id = str(curve), mint, chain_id
        self._slot = None

    def on_raw(self, slot, data, *, seed=False):
        if self._slot is not None and (int(slot) < self._slot or (seed and int(slot) == self._slot)):
            return None
        state = decode_bonding_curve(data)
        if state is None:
            return None
        self._slot = int(slot)
        return {"chain_id": self.chain_id, "pool": self.mint, "kind": "sol_curve_state",
                "block_no": int(slot), **state}


def subscribe_frame(vault, req_id):
    """`accountSubscribe` request for one token account, jsonParsed so the balance is readable."""
    return json.dumps({
        "jsonrpc": "2.0", "id": int(req_id), "method": "accountSubscribe",
        "params": [str(vault), {"encoding": "jsonParsed", "commitment": "confirmed"}],
    })


def parse_frame(msg, sub_to_vault):
    """Parse one WS frame into an ack or a token balance tuple.

    Pure, so the socket loop stays a dumb transport and every rule here is testable offline.
    Fail-closed: any frame we cannot read completely yields None rather than a guessed balance.
    """
    if not isinstance(msg, dict):
        return None
    if "result" in msg and "id" in msg and not isinstance(msg.get("result"), dict):
        return ("ack", msg["id"], msg["result"])
    params = msg.get("params") or {}
    result = params.get("result") or {}
    sub = params.get("subscription")
    vault = sub_to_vault.get(sub)
    if vault is None:
        return None
    slot = (result.get("context") or {}).get("slot")
    data = (result.get("value") or {}).get("data") if isinstance(result.get("value"), dict) else None
    if isinstance(data, list):
        # A base64 program account (a pump curve): hand the raw bytes to its tape.
        if len(data) != 2 or data[1] != "base64" or slot is None:
            return None
        try:
            return ("raw", vault, int(slot), base64.b64decode(data[0], validate=True))
        except (TypeError, ValueError):
            return None
    try:
        info = (((result.get("value") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        token_amount = info.get("tokenAmount") or {}
        amount = int(token_amount.get("amount"))
        raw_decimals = token_amount.get("decimals")
        decimals = (
            raw_decimals
            if isinstance(raw_decimals, int) and not isinstance(raw_decimals, bool)
            else None
        )
    except (TypeError, ValueError, AttributeError):
        return None
    if slot is None:
        return None
    return ("balance", vault, int(slot), amount, decimals)


class VaultTape:
    """Per-slot vault balances -> pool_ticks-shaped rows, using the frozen sign rule.

    Holds the last seen balance per vault and emits a row when BOTH vaults have moved within the
    same slot (a complete two-sided picture). A slot where only one side moved emits `sol_sync`
    only: the reserve level is captured, directional flow is never fabricated.
    """

    def __init__(self, coin_vault, quote_vault, *, mint, chain_id="solana"):
        self.coin_vault, self.quote_vault = str(coin_vault), str(quote_vault)
        self.mint, self.chain_id = mint, chain_id
        self._last = {}            # vault -> last balance
        self._slot = {}            # slot -> {vault: (pre, post)}
        # SAME-SLOT PROVENANCE (Codex review 2026-10-21): slot -> {vault: balance seen FOR that
        # slot}. A complete row's balance0/balance1 come from here, never from `_last`, which a
        # later slot's notification for one vault may already have overwritten (interleaved
        # arrival coin@100, coin@101, quote@100 used to pair coin@101 with quote@100).
        self._slot_bal = {}
        # WITHIN-SLOT AMBIGUITY (Codex review 2026-10-21): one slot can carry several states per
        # vault, and write order cannot bind a coin write to the quote write of the same
        # transaction. A second write to either vault in a slot makes the slot ambiguous: sync rows
        # only. These unsigned rows are price evidence only; LP authority needs tx identity (_tx).
        self._writes = {}          # slot -> {vault: writes seen}
        # TX-BOUND AUTHORITY (Codex P1 on 56a8c4676): per-account ordering cannot pair writes across
        # transactions, and a void after publication cannot recall an exit already nominated. A write
        # that names its transaction pairs only with the other vault's write from THE SAME
        # transaction: that pair is one transaction's post-state, so it is authoritative at once.
        self._tx = {}              # (slot, tx_sig) -> {vault: (balance, delta)}
        self._decimals = {}        # vault -> token decimals

    def on_balance(self, vault, slot, amount, decimals=None, *, tx_sig=None, tx_bound=False):
        """Record a balance and return a row when the slot is complete, else None.

        `tx_bound` (the gRPC account lane, which delivers every write): a complete row needs both
        vaults written by one named transaction; a write without a signature is never paired."""
        vault = str(vault)
        if vault not in (self.coin_vault, self.quote_vault):
            return None
        if isinstance(decimals, int) and not isinstance(decimals, bool):
            self._decimals[vault] = decimals
        pre = self._last.get(vault)
        self._last[vault] = amount
        slot = int(slot)
        if tx_bound:
            return self._on_tx_balance(vault, slot, amount, pre, tx_sig)
        writes = self._writes.setdefault(slot, {})
        writes[vault] = writes.get(vault, 0) + 1
        self._prune_slots(slot)
        if pre is None:
            return None                      # first sighting establishes the baseline, no delta yet
        if writes[vault] > 1:
            # Ambiguous slot: nothing more from it may pair (price rows); reserves only.
            self._slot.pop(slot, None)
            self._slot_bal.pop(slot, None)
            return self._sync_row(slot)
        slot_map = self._slot.setdefault(slot, {})
        slot_map[vault] = amount - pre
        self._slot_bal.setdefault(slot, {})[vault] = amount
        if len(slot_map) < 2:
            return self._sync_row(slot)          # one side only -> reserves, no flow
        row = self._row(slot, slot_map)
        self._slot.pop(slot, None)
        self._slot_bal.pop(slot, None)
        return row

    def _on_tx_balance(self, vault, slot, amount, pre, tx_sig):
        for key in [k for k in self._tx if k[0] < slot - 64]:
            del self._tx[key]
        if pre is None:
            return None                      # first sighting establishes the baseline, no delta yet
        if not tx_sig:
            return self._sync_row(slot)      # unnamed write: reserves only, never a pair
        legs = self._tx.setdefault((slot, str(tx_sig)), {})
        legs[vault] = (amount, amount - pre)
        if len(legs) < 2:
            return self._sync_row(slot)
        del self._tx[(slot, str(tx_sig))]
        (coin_bal, coin_d), (quote_bal, quote_d) = legs[self.coin_vault], legs[self.quote_vault]
        if coin_d == 0 or quote_d == 0:
            return self._sync_row(slot)
        kind = (("sol_mint" if coin_d > 0 else "sol_burn") if (coin_d > 0) == (quote_d > 0)
                else "sol_swap")
        return {"chain_id": self.chain_id, "pool": self.mint, "kind": kind, "block_no": slot,
                "tx_sig": str(tx_sig), "log_index": 0, "maker": None,
                "amount0": str(coin_d), "amount1": str(quote_d),
                "decimals0": self._decimals.get(self.coin_vault),
                "decimals1": self._decimals.get(self.quote_vault),
                "balance0": str(coin_bal), "balance1": str(quote_bal), "observed_at": None}

    def _prune_slots(self, newest, keep=64):
        """Per-slot state only for the recent slots (bounded memory). A notification for a pruned
        slot starts that slot over, so it can only ever produce a one-sided sync row."""
        if len(self._writes) > keep:
            for old in [s for s in self._writes if s < newest - keep]:
                self._writes.pop(old, None)
                self._slot_bal.pop(old, None)
                self._slot.pop(old, None)

    def seed_balance(self, vault, slot, amount, decimals=None):
        """A vault's balance read right after its subscription was acknowledged (2026-09-27,
        operator: "fix the reconnect"). It becomes the BASELINE only when the vault has none: a
        notification that arrived first is newer evidence and wins. Without it, each new epoch's
        first change per vault was only a baseline, so the first trade after every 30 s rollover
        emitted no row.

        Returns a `sol_seed` row once BOTH vaults have a baseline, else None. That row is the
        reserves the new subscription started from. The cache uses it for display only: it is not a
        reserve observation for any exit rail and not a price."""
        vault = str(vault)
        if vault not in (self.coin_vault, self.quote_vault) or vault in self._last:
            return None
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return None
        if amount < 0:
            return None
        if isinstance(decimals, int) and not isinstance(decimals, bool):
            self._decimals[vault] = decimals
        self._last[vault] = amount
        if self.coin_vault not in self._last or self.quote_vault not in self._last:
            return None
        row = self._sync_row(int(slot))
        row["kind"] = "sol_seed"
        return row

    def _sync_row(self, slot):
        return {"chain_id": self.chain_id, "pool": self.mint, "kind": "sol_sync", "block_no": slot,
                "tx_sig": None, "log_index": 0, "maker": None,
                "amount0": str(self._last.get(self.coin_vault, 0)),
                "amount1": str(self._last.get(self.quote_vault, 0)),
                "decimals0": self._decimals.get(self.coin_vault),
                "decimals1": self._decimals.get(self.quote_vault),
                "observed_at": None}

    def _row(self, slot, deltas):
        coin_d = deltas.get(self.coin_vault, 0)
        quote_d = deltas.get(self.quote_vault, 0)
        both = self._slot_bal.get(slot, {})
        if coin_d == 0 or quote_d == 0 or self.coin_vault not in both or self.quote_vault not in both:
            return self._sync_row(slot)          # never pair with a balance from another slot
        # LIVE-VERIFIED sign rule: opposite = swap, same = LP add/remove
        if (coin_d > 0) == (quote_d > 0):
            kind = "sol_mint" if coin_d > 0 else "sol_burn"
        else:
            kind = "sol_swap"
        return {"chain_id": self.chain_id, "pool": self.mint, "kind": kind, "block_no": slot,
                "tx_sig": None, "log_index": 0, "maker": None,
                "amount0": str(coin_d), "amount1": str(quote_d),
                "decimals0": self._decimals.get(self.coin_vault),
                "decimals1": self._decimals.get(self.quote_vault),
                # The reserves AFTER this slot, both captured FOR this slot (same-slot provenance).
                # Read by display (the quiet-pool carry) and by the LP rail. Extra keys; every
                # consumer reads rows by name.
                "balance0": str(both[self.coin_vault]),
                "balance1": str(both[self.quote_vault]),
                "observed_at": None}


async def run_vault_stream(vaults, on_row, *, ws_url=None, connect=None, stop=None,
                           refresh_fn=None, refresh_s=2.0, seed_fn=None, vault_seed_fn=None):
    """Subscribe to `vaults` ({vault: VaultTape}) and push rows into `on_row`. Read-only.

    `connect` is injected in tests so no socket is opened. Any error tears down and returns — the
    caller's supervisor reconnects, and the 30 s pull tape covers the gap. A gap is honest; an
    invented tick is not.

    `refresh_fn` closes the join gap for a position that opens MID-EPOCH. Without it a new position
    waits out the whole epoch before it is subscribed — measured 18.2 / 21.4 / 27.9 s on 2026-08-12,
    which is the most volatile part of a position's life. `accountSubscribe` is per-account and the
    socket accepts one at any time, so a new vault joins the LIVE connection: no reconnect, and no
    `cache.invalidate()` (shortening the epoch instead would wipe the mark cache every few seconds).

    `refresh_fn` is called ON THE EVENT-LOOP THREAD, deliberately. `asyncio.run` runs the loop in the
    worker thread that opened the SQLite handles, and those are check_same_thread=True, so handing
    the refresh to a worker thread would raise. It may therefore block frame reading for the length
    of one bounded read — still far below the epoch it replaces.

    `seed_fn(account) -> (slot, raw bytes) | None` reads a curve account once AFTER its subscription
    is acknowledged: `accountSubscribe` pushes only on change, so a quiet curve would otherwise have
    no price at all. It runs on a thread, never on this loop; a failed seed is simply no row.

    `vault_seed_fn(vault) -> (slot, amount, decimals) | None` does the same for an SPL vault
    (2026-09-27): its balance becomes the tape's baseline unless a notification got there first, so
    the first trade of every epoch is a complete swap instead of an invisible baseline.
    """
    import asyncio
    import inspect

    import websockets                                          # already a dependency
    ws_url = ws_url or DEFAULT_WS
    connect = connect or websockets.connect
    sub_to_vault, req_to_vault = {}, {}
    seeds = set()

    async def _emit(row):
        result = on_row(row)
        if inspect.isawaitable(result):
            await result

    async def _seed(vault):
        try:
            got = await asyncio.to_thread(seed_fn, vault)
            if not got:
                return
            slot, raw = got
            tape = vaults.get(vault)
            row = tape.on_raw(slot, raw, seed=True) if tape is not None else None
        except Exception:
            logger.debug("curve seed failed for %s", vault, exc_info=True)
            return
        if row is not None:
            await _emit(row)

    async def _seed_vault(vault):
        try:
            got = await asyncio.to_thread(vault_seed_fn, vault)
            if not got:
                return
            slot, amount, decimals = got
            tape = vaults.get(vault)
            # Applied on THIS loop thread, the thread on_balance runs on: no race with a frame.
            row = tape.seed_balance(vault, slot, amount, decimals) if tape is not None else None
        except Exception:
            logger.debug("vault seed failed for %s", vault, exc_info=True)
            return
        if row is not None:
            await _emit(row)

    def _acked(req_id, sub):
        vault = req_to_vault.get(req_id)
        if vault is None:
            return
        sub_to_vault[sub] = vault
        tape = vaults.get(vault)
        if seed_fn is not None and hasattr(tape, "on_raw"):
            task = asyncio.ensure_future(_seed(vault))
            seeds.add(task)
            task.add_done_callback(seeds.discard)
        elif vault_seed_fn is not None and hasattr(tape, "seed_balance"):
            task = asyncio.ensure_future(_seed_vault(vault))
            seeds.add(task)
            task.add_done_callback(seeds.discard)

    async with connect(ws_url) as ws:
        next_id = 0

        async def _subscribe(vault):
            nonlocal next_id
            next_id += 1
            req_to_vault[next_id] = vault
            frame = (curve_subscribe_frame if getattr(vaults.get(vault), "encoding", None) == "base64"
                     else subscribe_frame)
            await ws.send(frame(vault, next_id))

        for vault in list(vaults):
            await _subscribe(vault)

        async def _read():
            async for raw in ws:
                if stop is not None and stop():
                    return
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                parsed = parse_frame(msg, sub_to_vault)
                if parsed is None:
                    # may be an ack whose subscription id we still need to map
                    if isinstance(msg, dict) and "result" in msg and "id" in msg:
                        _acked(msg["id"], msg["result"])
                    continue
                kind = parsed[0]
                if kind == "ack":
                    _acked(parsed[1], parsed[2])
                    continue
                if kind == "raw":
                    _, vault, slot, raw = parsed
                    tape = vaults.get(vault)
                    row = tape.on_raw(slot, raw) if hasattr(tape, "on_raw") else None
                    if row is not None:
                        await _emit(row)
                    continue
                _, vault, slot, amount, decimals = parsed
                tape = vaults.get(vault)
                if tape is None:
                    continue
                row = tape.on_balance(vault, slot, amount, decimals=decimals)
                if row is not None:
                    result = on_row(row)
                    # Production hot-exit corroboration is async so blocking read-RPC work cannot
                    # starve the pull-fallback task. Synchronous callbacks remain supported.
                    if inspect.isawaitable(result):
                        await result

        if refresh_fn is None:
            await _read()
            if seeds:
                await asyncio.gather(*seeds, return_exceptions=True)
            return

        async def _join_new_vaults():
            while True:
                await asyncio.sleep(refresh_s)
                try:
                    fresh = refresh_fn() or {}
                except Exception:
                    continue                  # a failed refresh never tears down a healthy stream
                for vault, tape in fresh.items():
                    if vault in vaults:
                        continue
                    vaults[vault] = tape      # the reader consults this same dict
                    await _subscribe(vault)

        reader = asyncio.ensure_future(_read())
        joiner = asyncio.ensure_future(_join_new_vaults())
        try:
            done, _pending = await asyncio.wait(
                [reader, joiner], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()                 # surface a reader failure to the supervisor
        finally:
            for task in (reader, joiner):
                task.cancel()
