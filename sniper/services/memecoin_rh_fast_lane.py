"""Robinhood 4663 fast lane, M2: the in-runner lane thread (spec
docs/superpowers/specs/2026-10-01-memecoin-4663-fast-lane-design.md §2.5-§2.9; Alpha's build order
2026-10-01: datagram + runner_shadow, then the cached seams, then live behind the knob).

THE SPLIT (§2.9). The detector process (M0 feed + M1 decide) does every per-block and per-signal
read and decides from memory; for a would-send decision it sends ONE datagram to this thread. The
runner thread sleeps on that socket and runs only per-signal work, so between signals it holds no
GIL at all.

THE DATAGRAM IS AUTHENTICATED BY THE KERNEL, NOT BY ITS CONTENT. An abstract AF_UNIX name has no
file permissions (the on-chain wake treats its datagram as a hint for that reason). This one
carries a decision, so the socket sets SO_PASSCRED and a datagram is heard only when the kernel
says it came from OUR uid and from the detector's pid, read from the detector's own fresh
heartbeat. Everything else is dropped and counted.

MODE runner_shadow (step 1). Per signal: the handoff delay, a NON-BLOCKING try of the per-(wallet,
4663) execution lock every executor holds through quote, send, confirm and delta (busy means the
live lane would fall back, never wait), and the unsigned buy built from the detector's cap. Its
busy window and thread CPU are recorded, which the GIL-overlap read joins against the slot lane's
decisions. It signs nothing, reserves nothing and sends nothing; a test pins that.
"""
from __future__ import annotations

import collections
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import select
import socket
import struct
import threading
import time
from fractions import Fraction
from functools import partial
from sniper.execution.evm_wallet import CHAIN_QUOTE
from sniper.services.memecoin_decision_features import from_cached_stats
from sniper.services.memecoin_source_fill import validate_source_fill
from sniper.services.memecoin_rh_deposit_proof import (
    DEPOSIT_DELIVERY_LANE, cap_bound, lane_initiation, payload_refusal)
from sniper.services.memecoin_evm_v4_events import v4_pool_id
from sniper.settings import memecoin as lane_cfg
from sniper.util.receipt_clock import freeze_receipt_clock


def native_source_price_matches(payload, proof):
    """Bind unsigned native price inputs to the receipt; pure, no provider or ledger work."""
    try:
        post = proof["source_post"]
        key = payload["key"]
        if (key["currency0"] != "0x" + "00" * 20 or key["currency1"] != proof["mint"]
                or post["mint_is_c1"] is not True or v4_pool_id(key) != post["pool"]):
            return False
        sqrt = int(post["sqrt_price_x96"])
        if int(payload["post_sqrt"]) != sqrt:
            return False
        usd = Fraction(str(payload["eth_usd"]))
        if usd <= 0:
            return False
        if proof["payment_token"] == CHAIN_QUOTE[str(payload.get("chain") or "4663")]["token"].lower():
            if proof["payment_decimals"] != 18:
                return False
            src_in = int(proof["payment_atomic"])
        else:
            src_in = int(Fraction(str(proof["paid_usd"])) / usd * 10**18)
        if int(payload["src_in_wei"]) != src_in or src_in <= 0:
            return False
        post_price = Fraction(1 << 192, sqrt**2)
        if payload.get("post_wei_num") is not None or payload.get("post_wei_den") is not None:
            if Fraction(int(payload["post_wei_num"]), int(payload["post_wei_den"])) != post_price:
                return False
        if lane_cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
            return Fraction(int(payload["cap_num"]), int(payload["cap_den"])) == post_price
        terms = []
        if lane_cfg.MEMECOIN_REAL_CHASE_MAX_PCT > 0:
            terms.append(post_price * (1 + Fraction(str(lane_cfg.MEMECOIN_REAL_CHASE_MAX_PCT))/100))
        if lane_cfg.MEMECOIN_REAL_MAX_EXEC_PREMIUM > 0:
            terms.append(Fraction(src_in, int(proof["acquired_atomic"]))
                         * Fraction(str(lane_cfg.MEMECOIN_REAL_MAX_EXEC_PREMIUM)))
        cap = Fraction(int(payload["cap_num"]), int(payload["cap_den"]))
        return bool(terms) and 0 < cap <= min(terms)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return False

logger = logging.getLogger(__name__)

_PREFIX = "\0sniper-rh-fast-lane-"
_REQUIRED = ("tx_hash", "seq", "wallet", "mint", "permit_nonce", "key", "cap_num", "cap_den",
             "clip_wei", "sent_epoch")
_SHARED_REQUIRED = ("tx_hash", "seq", "wallet", "mint", "permit_nonce", "pool", "src_out",
                    "post_ratio", "paid_usd", "arrival_utc", "sent_epoch")
SHARED_ROUTE_KINDS = frozenset({"v3_shared", "v4_shared", "pons_shared", "split_shared"})
_SEEN_MAX = 4096
_DETECTOR_BEAT_MAX_AGE_S = 60.0
_FAILED_BACKOFF_S = 10.0
REVERT_CLASS_WAIT_S = 1.0         # the lane's longest wait for a released revert's class
# A bridged coin whose full path did not quote (Gamma LOW1 on a8d5ba8da): the route, not a blip, so
# the paced host does not redo the whole check every 10 s for as long as the coin is a candidate.
_BRIDGE_LEG_BACKOFF_S = 60.0


def lane_address(root):
    """The lane's abstract socket name for this state root (a test or another checkout never
    reaches production)."""
    return _PREFIX + hashlib.sha256(str(root).encode()).hexdigest()[:12]


def default_address():
    from sniper.util.state_paths import state_root
    return lane_address(state_root())


def lane_payload(rec, out, *, sent_epoch, inputs=None):
    """The datagram for one would-send decision: the signal's identity, the decision the runner
    acts on, and the detector's research-host inputs (wallet snapshot, ETH/USD and its tape stamp),
    so the runner reads nothing before its send. The cap travels as an exact fraction."""
    inputs = inputs or {}
    if out.get("route_kind") in SHARED_ROUTE_KINDS:
        return {k: rec.get(k) for k in ("tx_hash", "seq", "wallet", "mint", "handle",
                                        "source_user_id", "permit_nonce", "arrival_utc")} | {
            "receipt_clock": freeze_receipt_clock(rec.get("receipt_clock")),
            "route_kind": out["route_kind"], "key": None, "pool": out["pool"],
            "src_out": out["src_out"], "post_ratio": out["post_ratio"],
            "source_fill": out.get("source_fill"),
            "paid_usd": out["paid_usd"], "sent_epoch": float(sent_epoch),
            # Speed fix 1: the early worker's sizing (snapshot_quote_balances), nothing else.
            "snapshot": inputs.get("snapshot"), "eth_usd": inputs.get("eth_usd"),
            "eth_usd_at": inputs.get("eth_usd_at")}
    cap = out["cap_price"]
    return {"tx_hash": rec["tx_hash"], "seq": rec["seq"], "wallet": rec["wallet"],
            "mint": rec["mint"], "handle": rec.get("handle"),
            "source_user_id": rec.get("source_user_id"), "permit_nonce": rec["permit_nonce"],
            "arrival_utc": rec.get("arrival_utc"), "pool": out["pool"], "key": out["key"],
            "receipt_clock": freeze_receipt_clock(rec.get("receipt_clock")),
            "route_kind": out.get("route_kind"),
            "cap_num": cap.numerator, "cap_den": cap.denominator, "clip_wei": int(out["clip_wei"]),
            "min_out": int(out["min_out"]), "state_kind": out.get("state_kind"),
            "tape_lag_blocks": out.get("tape_lag_blocks"), "sent_epoch": float(sent_epoch),
            # The live lane's mcap and source price come from the decide's own post-trade state.
            "post_sqrt": out.get("est_post_sqrt"), "src_in_wei": out.get("src_in_wei"),
            "state_liquidity": out.get("state_liquidity"),
            "a_state_liquidity": out.get("a_state_liquidity"),
            "src_out": out.get("src_out"),
            "source_fill": out.get("source_fill"),
            # Option C: the user's own Solana deposit proof, when the receipt has not landed yet.
            "deposit_proof": rec.get("deposit_proof"),
            # What the source paid, in USD (the watcher's size basis for the same buy).
            "paid_usd": out.get("paid_usd"),
            # Routes rev 3 (6.1): the post-trade price in wei per coin atomic for every kind, and
            # what the lane needs to rebuild a WETH or bridged buy at its own clip.
            "post_wei_num": None if out.get("post_wei") is None else out["post_wei"].numerator,
            "post_wei_den": None if out.get("post_wei") is None else out["post_wei"].denominator,
            "in_is_c0": out.get("in_is_c0"), "bridge": out.get("bridge"),
            "a_pool": out.get("bridge_pool"), "a_key": out.get("a_key"), "a_sqrt": out.get("a_sqrt"),
            "x_is_c0": out.get("x_is_c0"),
            # 'source_pool': option C's v4_shared bought through the source's own pool.
            "pool_choice": out.get("pool_choice"),
            "latest_same_pool": out.get("latest_same_pool"),
            "a_latest_same_pool": out.get("a_latest_same_pool"),
            "snapshot": inputs.get("snapshot"), "eth_usd": inputs.get("eth_usd"),
            "eth_usd_at": inputs.get("eth_usd_at")}


def send_signal(address, payload):
    """One datagram to the lane. Never raises and never blocks: False when nothing is bound, the
    queue is full or anything else fails (today's path still covers the signal)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(json.dumps(payload).encode(), socket.MSG_DONTWAIT, address)
        return True
    except Exception:  # noqa: BLE001 - a signal must never fail the detector
        return False


ROUTE_KINDS = ("v4_native", "v4_weth", "v4_bridge")
# The native lanes that submit through the chain context's shared transport (auto_free) when no
# sender is injected. The BSC lane brings its own sender; 4663 keeps its sequencer fallback.
_SHARED_TRANSPORT_CHAINS = frozenset({4663, 8453})


UNKNOWN_BINDING = object()      # an intent's safety binding that cannot be known at the log's arrival


class BridgeLegFailed(RuntimeError):
    """A bridged coin sold into X, but the full coin -> X -> native path did not quote on the lane's
    host (routes rev 3, Gamma C2): the route's failure, never the coin's verdict."""


def route_kind(payload):
    """A datagram's or registry entry's route kind; one without a kind is v4_native (Gamma C4)."""
    return (payload or {}).get("route_kind") or (payload or {}).get("kind") or "v4_native"


def registry_entry(value):
    """A registry value as {kind, key, pool, bridge, a_key, a_pool}: an older (key, pool) tuple
    and an entry without a kind read as v4_native, unchanged (Gamma C4)."""
    if isinstance(value, dict):
        return {"kind": route_kind(value), "key": value.get("key"), "pool": value.get("pool"),
                "bridge": value.get("bridge"), "a_key": value.get("a_key"), "a_pool": value.get("a_pool")}
    key, pool = value
    return {"kind": "v4_native", "key": key, "pool": pool, "bridge": None, "a_key": None, "a_pool": None}


def _lane_deadline_s():
    """LANE_SWAP_DEADLINE_S (sniper/settings/memecoin.py): every lane buy's deadline, kept inside the
    never-admitted dead proof."""
    from sniper.settings.memecoin import LANE_SWAP_DEADLINE_S
    return LANE_SWAP_DEADLINE_S


def quote_slippage_floor(payload, clip_wei):
    """A nonzero size-aware output bound from state already carried by the watcher.

    Reuses the lane's existing one-range LP/hook estimate, not a market-movement witness.
    Missing state is a mechanical build miss; it never licenses a zero signed minimum.
    """
    from sniper.execution.evm_v4_quote import source_pool_est_out
    try:
        liquidity = payload["state_liquidity"]
        sqrt = payload["post_sqrt"]
        if type(liquidity) is not int or liquidity <= 0 or type(sqrt) is not int or sqrt <= 0:
            raise ValueError("missing state")
        key = payload["key"]
        kind = route_kind(payload)
        if kind == "v4_bridge":
            raise ValueError("first leg has no receipt-bound output state")
        proof = validate_source_fill(payload.get("source_fill"), payload)
        post = (proof or {}).get("source_post") or {}
        orientation = payload.get("in_is_c0", True)
        if type(orientation) is not bool:
            raise ValueError("missing pair orientation")
        currencies = (str(key["currency0"]).lower(), str(key["currency1"]).lower())
        quote = currencies[0 if orientation else 1]
        expected_quote = ("0x" + "00" * 20 if kind == "v4_native" else
                          CHAIN_QUOTE[(proof or {}).get("network_id")]["token"].lower())
        if (proof is None or post.get("venue") != "v4"
                or post.get("pool") != str(payload.get("pool") or "").lower()
                or v4_pool_id(key) != post["pool"]
                or post.get("sqrt_price_x96") != str(sqrt)
                or post.get("liquidity_atomic") != str(liquidity)
                or post.get("mint_is_c1") != orientation
                or currencies[1 if orientation else 0] != proof["mint"]
                or quote != expected_quote):
            raise ValueError("output state is not receipt bound")
        if type(clip_wei) is not int or clip_wei <= 0:
            raise ValueError("invalid clip")
        for leg in (key,):
            hook = str(leg.get("hooks") or "").lower()
            fee = leg["fee"]
            take = lane_cfg.MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS.get(hook, 0)
            if (type(fee) is not int or not 0 <= fee < 0x800000
                    or type(take) is not int or not 0 <= take < 10_000
                    or hook not in ("0x" + "00" * 20, "")
                    and hook not in lane_cfg.MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS):
                raise ValueError("unmodelled fee or hook")
        expected = source_pool_est_out(int(clip_wei), dict(sqrt=sqrt, liquidity=liquidity),
                                       key, orientation)
        keep = 1 - Fraction(str(lane_cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT)) / 100
        if expected is None or expected <= 0 or not 0 < keep <= 1:
            raise ValueError("invalid output or slippage")
        minimum = int(expected * keep)
        if minimum <= 0:
            raise ValueError("zero minimum")
        return minimum
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError) as exc:
        raise ValueError("native output quote unavailable") from exc


def lane_buy_tx(payload, clip_wei, deadline, taker, floor=0, chain="4663"):
    """(the unsigned buy, its minimum output) for the datagram's route at the lane's own clip
    (routes rev 3, 6.2). Every kind pays native as the value. `floor` (coin atomic) is the copy
    price cap's minimum at this clip; the stricter of it and the cap's minimum is signed. Raises
    ValueError, KeyError or TypeError for a datagram that cannot build (the caller's build_refused).
    Movement-only mode replaces both source-relative floors with size-aware independent slippage.
    """
    from sniper.execution import evm_v4_quote as v4
    movement = lane_cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY
    cap = None if movement else Fraction(int(payload["cap_num"]), int(payload["cap_den"]))
    quote_floor = quote_slippage_floor(payload, clip_wei) if movement else None
    kind = route_kind(payload)
    if kind == "v4_native":
        min_out = quote_floor if movement else max(clip_wei * cap.denominator // cap.numerator, int(floor))
        return v4.native_buy_tx(payload["key"], clip_wei, min_out, deadline, taker=taker,
                                chain=chain), min_out
    if kind == "v4_weth":
        if not isinstance(payload.get("in_is_c0"), bool):
            raise ValueError("a WETH route needs in_is_c0")
        min_out = quote_floor if movement else max(clip_wei * cap.denominator // cap.numerator, int(floor))
        return v4.weth_buy_tx(payload["key"], payload["in_is_c0"], clip_wei, min_out, deadline,
                              taker=taker, chain=chain), min_out
    if kind == "v4_bridge":
        from sniper.execution.evm_v4_multihop_tx import native_multihop_buy_tx
        if not (isinstance(payload.get("a_key"), dict) and int(payload.get("a_sqrt") or 0) > 0
                and len(str(payload.get("bridge") or "")) == 42
                and str(payload["bridge"]).lower().startswith("0x")):
            raise ValueError("a bridged route needs bridge, a_key and a_sqrt")
        ak, bk = payload["a_key"], payload["key"]
        x, mint = str(payload["bridge"]).lower(), str(payload["mint"]).lower()
        # Hop A at its MID rate for THIS clip (Gamma M1); the cap is X per coin atomic.
        x_mid = clip_wei * int(payload["a_sqrt"]) ** 2 >> 192
        min_out = quote_floor if movement else max(x_mid * cap.denominator // cap.numerator, int(floor))
        path = [(x, int(ak["fee"]), int(ak["tick_spacing"]), ak["hooks"]),
                (mint, int(bk["fee"]), int(bk["tick_spacing"]), bk["hooks"])]
        return native_multihop_buy_tx(chain, mint, path, clip_wei, min_out, deadline), min_out
    raise ValueError("unknown route kind %r" % kind)


def route_quote(quote_fn, entry, mint, quote_token, amount, *, host, http, slippage_bps=None,
                chain="4663"):
    """The safety builder's sell-quote seam by route kind (routes rev 3, 6.5), every read on `host`
    through `http`. v4_native is today's. A WETH-quoted or bridged coin is quoted out through the
    pool it is sold through (coin -> WETH, or coin -> X: the classification) and the sell is built
    by the leg's own builder over that quote; the safety call's own probe_out carries the amount
    to the bridge's full-path check. The caller looks the registry up by the first argument, so the builder's buy
    probe (quote token first) finds no entry and gets None, as today."""
    from sniper.execution.evm_v4_quote import _quote_single
    entry = registry_entry(entry)
    kind, key, pool = entry["kind"], entry["key"], entry["pool"]
    if kind == "v4_native":
        return quote_fn(mint, quote_token, amount, key=key, pool_id=pool, rpc_url=host, http=http,
                        slippage_bps=slippage_bps)
    sell_token = str(mint).lower()
    if sell_token not in (str(key["currency0"]).lower(), str(key["currency1"]).lower()):
        return None                                  # not this pool's coin: no route, as today
    zf1 = str(key["currency0"]).lower() == sell_token
    out = _quote_single(chain, key, zf1, int(amount), host, http=http)
    if not out:
        return None
    other = str(key["currency1"] if zf1 else key["currency0"]).lower()
    override = {"amount_out": out, "pool_id": pool, "pool_key": key, "zero_for_one": zf1,
                "pool_fee": key["fee"], "quote_currency": other}
    return quote_fn(mint, quote_token, amount, key=key, pool_id=pool, rpc_url=host, http=http,
                    slippage_bps=slippage_bps, quote_override=override)


def quote_path_pinned(currency_in, path, amount_in, host, *, http):
    """One V4Quoter quoteExactInput on `host` itself, never a pool member (Gamma C1: not
    evm_v4_multihop._quote_path, whose read can move to a funded read-pool member). `path` is
    [(currency out of the hop, PoolKey), ...]. The amount out, or None (a revert, an empty answer)."""
    from sniper.execution.evm_v4_multihop import quote_path_data
    from sniper.execution.evm_v4_quote import V4
    from sniper.execution.evm_wallet import _rpc
    answer = _rpc(host, "eth_call", [{"to": V4["4663"]["quoter"],
                                      "data": quote_path_data(currency_in, path, amount_in)},
                                     "latest"], http=http)
    raw = (answer or {}).get("result")
    out = int(raw[2:66], 16) if isinstance(raw, str) and len(raw) >= 66 else 0
    return out or None


class SafetyTimes:
    """Every verdict's wall time by route kind and path (routes rev 3, 6.6; Gamma C3), noted by the
    checking thread and flushed with the minute's counters (never a SQLite write per verdict)."""

    def __init__(self, maxlen=5000):
        self._rows = collections.deque(maxlen=maxlen)

    def note(self, mint, kind, path, wall_ms, outcome):
        from sniper.util.clock import real_utc_now_iso
        self._rows.append((real_utc_now_iso(), mint, kind, path, round(float(wall_ms), 3), outcome))

    def flush(self, db_path):
        import sqlite3
        rows = []
        while self._rows:
            rows.append(self._rows.popleft())
        if not rows:
            return
        db = sqlite3.connect(db_path, timeout=2)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS rh_fast_lane_safety_times (at TEXT NOT NULL, "
                       "mint TEXT, route_kind TEXT, path TEXT, wall_ms REAL, outcome TEXT)")
            db.executemany("INSERT INTO rh_fast_lane_safety_times VALUES (?,?,?,?,?,?)", rows)
            db.commit()
        finally:
            db.close()


def routed_safety(check, registry, host, http, *, path, times, clock=time.perf_counter):
    """`check` (today's token safety over `route_quote`) with the bridge's full-path check after a
    pass and every verdict's wall time noted. A bridged coin that sells into X but whose full path
    does not quote raises BridgeLegFailed: the cache keeps no verdict, inline names the route.

    THE AMOUNT IS THIS CALL'S (review EVM06, 2026-10-08): it comes back in this call's own
    `probe_out`, never a dict shared by every call of the long-lived safety function, so an
    overlapping same-coin read (the busy fallback) cannot take or clear another call's amount."""
    from sniper.execution.evm_token_safety import names_timings
    captures_timings = names_timings(check)
    def safety(mint, usd=None, timings=None):
        mint = str(mint).lower()
        entry = registry.get(mint)
        kind = registry_entry(entry)["kind"] if entry is not None else "unregistered"
        t0, outcome = clock(), "error"
        try:
            deadline = getattr(_INLINE, "deadline", None)
            probe_out = {}
            ok, why = check(mint, usd=usd, probe_out=probe_out,
                            **({"deadline": deadline} if deadline is not None else {}),
                            **({"timings": timings} if captures_timings and timings is not None else {}))
            amount = probe_out.get("amount")
            if ok and kind == "v4_bridge" and not bridge_leg_ok(entry, mint, amount, host, http=http):
                outcome = "bridge_leg_failed"
                raise BridgeLegFailed(mint)
            outcome = "ok" if ok else "unsafe"
            return ok, why
        except BridgeLegFailed:
            raise
        except Exception as exc:
            outcome = "error:%s" % type(exc).__name__
            raise
        finally:
            times.note(mint, kind, path, (clock() - t0) * 1000.0, outcome)
    return safety


def bridge_leg_ok(entry, mint, amount, host, *, http):
    """The bridge's routability (Gamma C2): coin -> X -> native quotes on the lane's host."""
    from sniper.execution.evm_v4_quote import _NATIVE
    entry = registry_entry(entry)
    if not amount or not entry.get("bridge") or not isinstance(entry.get("a_key"), dict):
        return False
    return bool(quote_path_pinned(str(mint).lower(), [(str(entry["bridge"]).lower(), entry["key"]),
                                                      (_NATIVE, entry["a_key"])],
                                  int(amount), host, http=http))


def make_lane_sender(mode, address, *, send_fn=None, clock=time.time):
    """The detector's hook after each M1 decision: a would-send decision becomes one datagram to
    the runner's lane, stamped with the HOST clock. None unless the runner's lane runs
    (runner_shadow or live: in live the datagram is the only way a signal reaches the lane)."""
    if mode not in ("runner_shadow", "live"):
        return None
    send_fn = send_fn or send_signal

    # runner_shadow also hears a decision the DETECTOR's float check stopped (Gamma 2026-10-02: an
    # unfunded float stopped every M1 decision before any datagram). The runner re-checks the float
    # from the same snapshot and labels it float_short+<verdict>; live never hears it.
    heard = ("would_send", "float_short") if mode == "runner_shadow" else ("would_send",)

    def on_decided(rec, out, inputs=None):
        # Routes rev 3 (6.3): every kind reaches the runner; its live lane refuses a kind outside
        # MEMECOIN_RH_FAST_ROUTES, and the runner shadow judges them all.
        if out.get("reason") in heard:
            payload = lane_payload(rec, out, sent_epoch=clock(), inputs=inputs)
            payload["decide_reason"] = out["reason"]
            send_fn(address, payload)
    return on_decided


class LaneSocket:
    """The bound receiving end, with the sender's kernel credentials on every datagram."""

    def __init__(self, address):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            self.sock.bind(address)
        except Exception:
            self.sock.close()
            raise

    def recv(self, timeout):
        """(payload or None, pid, uid), or None when nothing arrived within `timeout`."""
        ready, _w, _x = select.select([self.sock], [], [], timeout)
        if not ready:
            return None
        data, ancillary, _flags, _addr = self.sock.recvmsg(
            65536, socket.CMSG_SPACE(struct.calcsize("3i")))
        pid = uid = None
        for level, kind, blob in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
                pid, uid, _gid = struct.unpack("3i", blob[:struct.calcsize("3i")])
        try:
            payload = json.loads(data)
        except ValueError:
            payload = None
        return payload, pid, uid

    def close(self):
        self.sock.close()


def detector_pid(path, now, max_age_s=_DETECTOR_BEAT_MAX_AGE_S):
    """The detector's pid from its own heartbeat file, or None when the beat is missing, stale or
    carries no pid (then no datagram is heard)."""
    try:
        beat = json.loads(open(path).read())
        if now - float(beat["ts"]) > max_age_s:
            return None
        return int(beat["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def check_sender(payload, pid, uid, *, detector_pid):
    """None when the datagram is the detector's and well formed, else the reason it is dropped.
    Two kinds: a signal (the default) and "candidates", the mints the safety cache should hold."""
    if uid != os.getuid():
        return "foreign_uid"
    if detector_pid is None:
        return "no_detector"
    if pid != detector_pid:
        return "not_the_detector"
    if isinstance(payload, dict) and payload.get("kind") == "candidates":
        entries = payload.get("entries")
        if not isinstance(entries, list) or not all(
                isinstance(e, dict) and isinstance(e.get("mint"), str) for e in entries):
            return "malformed"
        discovery = payload.get("discovery_mints", [])
        if (not isinstance(discovery, list) or len(discovery) > 32
                or not all(_discovery_mint(m) for m in discovery)):
            return "malformed"
        return None
    if not isinstance(payload, dict):
        return "malformed"
    if payload.get("kind") in (FEED_HOLD_KIND, FEED_RELEASE_KIND):
        return _feed_hold_malformed(payload)
    if payload.get("kind") == PREBUY_KIND:
        return _prebuy_malformed(payload)
    required = _SHARED_REQUIRED if route_kind(payload) in SHARED_ROUTE_KINDS else _REQUIRED
    if any(k not in payload for k in required):
        return "malformed"
    if required is _SHARED_REQUIRED:
        try:
            unpriced = route_kind(payload) in {'pons_shared', 'split_shared'}
            if (int(payload["seq"]) <= 0 or int(payload["src_out"]) <= 0
                    or not all(math.isfinite(float(payload[k])) and float(payload[k]) > 0
                               for k in (("paid_usd",) if unpriced else ("post_ratio", "paid_usd")))
                    or unpriced and payload['post_ratio'] is not None
                    or route_kind(payload) == 'split_shared' and payload['pool'] is not None):
                return "malformed"
        except (ValueError, TypeError, OverflowError):
            return "malformed"
    return None


# The 4663 Relay pre-buy's datagram (spec 2026-10-14-rh-relay-prebuy-design.md §2.3): the signal fields
# lane_buy_tx needs, priced at the pool's current tape spot (no source trade exists yet), the deposit's
# USD, and the deposit-plus-request proof. `tx_hash` is the Relay order id, never a source transaction.
PREBUY_KIND = "relay_prebuy_v1"
# D-b (2026-10-16): the detector's hold at the feed's arrival and its release when no signal follows.
FEED_HOLD_KIND, FEED_RELEASE_KIND = "feed_hold_v1", "feed_release_v1"
_HEX = frozenset("0123456789abcdef")


def _feed_hold_malformed(payload):
    tx, mint = str(payload.get("tx_hash") or "").lower(), str(payload.get("mint") or "").lower()
    ok_tx = len(tx) == 66 and tx.startswith("0x") and set(tx[2:]) <= _HEX
    ok_mint = mint == "*" or (len(mint) == 42 and mint.startswith("0x") and set(mint[2:]) <= _HEX)
    return None if ok_tx and ok_mint else "malformed"


def make_feed_hold_sender(mode, address, *, send_fn=None, clock=time.time):
    """The detector's D-b hook: hold(rec) at the feed's arrival, settle(rec, out) after its decision
    (a release unless the decision became the lane's datagram, whose own hold takes over). Live and
    MEMECOIN_RH_FAST_FEED_HOLD only; one datagram each, never blocks or raises (send_signal)."""
    send_fn = send_fn or send_signal

    def on():
        from sniper.settings import memecoin as cfg
        return mode == "live" and cfg.memecoin_rh_fast_feed_hold()

    def send(kind, rec):
        try:
            if not on():
                return False
            return bool(send_fn(address, {"kind": kind, "tx_hash": str(rec.get("tx_hash") or "").lower(),
                                          "mint": str(rec.get("mint") or "*").lower(),
                                          "sent_epoch": clock()}))
        except Exception:
            logger.exception("rh fast: feed hold datagram failed")
            return False

    class FeedHolds:
        @staticmethod
        def hold(rec):
            return send(FEED_HOLD_KIND, rec)

        @staticmethod
        def release(rec):
            return send(FEED_RELEASE_KIND, rec)

        @staticmethod
        def settle(rec, out):
            if (out or {}).get("reason") != "would_send":
                return send(FEED_RELEASE_KIND, rec)
            return False
    return FeedHolds()
_PREBUY_REQUIRED = _REQUIRED + ("route_kind", "spot_wei_num", "spot_wei_den", "paid_usd", "eth_usd",
                                "snapshot", "source_initiation", "prebuy")


def _prebuy_malformed(payload):
    if any(k not in payload for k in _PREBUY_REQUIRED):
        return "malformed"
    try:
        if (route_kind(payload) not in ROUTE_KINDS or int(payload["seq"]) <= 0
                or int(payload["spot_wei_num"]) <= 0 or int(payload["spot_wei_den"]) <= 0
                or not math.isfinite(float(payload["paid_usd"])) or float(payload["paid_usd"]) <= 0
                or not isinstance(payload["source_initiation"], dict)
                or not isinstance(payload["prebuy"], dict)
                or str(payload["prebuy"].get("order_id") or "").lower() != str(payload["tx_hash"]).lower()):
            return "malformed"
    except (ValueError, TypeError, OverflowError):
        return "malformed"
    return None


class ResearchPaused(RuntimeError):
    """The research host answered 429 recently: nothing is sent until the pause ends."""


def _discovery_mint(mint):
    return (isinstance(mint, str) and len(mint) == 42 and mint.startswith("0x")
            and all(c in "0123456789abcdefABCDEF" for c in mint[2:])
            and int(mint[2:], 16) != 0)


def discovery_descriptor(chain="4663"):
    """Immutable configured identity, read from memory; never an active-pool cache."""
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_v4_quote import V4
    entries = (cfg.MEMECOIN_EVM_V4_LAUNCHPADS or {}).get(str(chain), ())
    conf = V4.get(str(chain)) or {}
    return (str(chain), conf.get("pool_manager"), conf.get("quoter"),
            tuple(tuple(sorted(e.items())) for e in entries))


class _DiscoveryCapacityUnavailable(Exception):
    """A local pace grant was unavailable; this is not a provider failure."""


class _DiscoveryReads:
    """One background job's owned HTTP/deadline; no state is patched on other threads."""
    def __init__(self, owner, generation, descriptor, *, allow_native_due=False, mint=None):
        self.owner, self.generation, self.descriptor = owner, generation, descriptor
        self.deadline = owner.paced.clock() + min(60.0, owner.cache.ttl_s)
        self.closed, self.failure = False, None
        self.publication = None
        self.allow_native_due = allow_native_due
        self.mint = mint

    def cheap_admission(self):
        owner = self.owner
        return (not self.closed and self.failure is None
                and owner.paced.clock() < self.deadline and owner.available_fn()
                and not owner.stop_fn() and not owner.paced.paused() and not owner.paced.dead())

    def raise_if_cancelled(self):
        # Generation reads can touch the intent directory. Only this outer job boundary uses
        # them, never the pacer's health/account lock or any entry thread.
        from sniper.execution.evm_rpc_pool import RequestNotSent
        owner = self.owner
        if not self.cheap_admission():
            self.failure = self.failure or (
                "deadline" if owner.paced.clock() >= self.deadline else "unavailable")
        elif (owner.generation_fn() != self.generation
              or discovery_descriptor() != self.descriptor
              or (owner.cache.urgent_due(owner.clock()) if self.allow_native_due
                  else owner.cache.next_due(owner.clock()) is not None)):
            self.failure = "unavailable"
        if self.mint is not None:
            noted = owner._candidates.get(self.mint)
            if noted is None or owner.clock() - noted > 600:
                self.failure = "unavailable"
        if self.failure is not None:
            raise RequestNotSent("RH discovery job " + self.failure)

    def physical_admission(self):
        self.raise_if_cancelled()          # account callbacks run outside their file/health locks
        return True

    def __call__(self, method, url, headers=None, body=None, timeout=10):
        from sniper.execution.evm_rpc_pool import RequestNotSent
        from urllib.parse import urlsplit
        method = str(method).upper()
        host = urlsplit(str(url)).hostname
        if method == "POST":
            if _norm_url(url) != _norm_url(self.owner.url):
                self.failure = "unavailable"
                raise RequestNotSent("RH discovery request outside owned research host")
            try:
                request = json.loads(body)
            except (TypeError, ValueError):
                request = None
            if (not isinstance(request, dict) or request.get("method") not in (
                    "eth_call", "eth_getLogs", "eth_blockNumber", "eth_chainId")):
                self.failure = "unavailable"
                raise RequestNotSent("RH discovery is read only")
        elif method != "GET" or host not in ("api.geckoterminal.com", "api.dexscreener.com"):
            self.failure = "unavailable"
            raise RequestNotSent("RH discovery provider unavailable")
        while True:
            self.raise_if_cancelled()
            remaining = self.deadline - self.owner.paced.clock()
            if remaining <= 0:
                self.failure = "deadline"
                raise RequestNotSent("RH discovery job deadline")
            try:
                reply = self.owner.paced.try_background(
                    method, url, headers=headers, body=body, timeout=min(timeout, remaining),
                    admission_fn=self.cheap_admission, refusal_exc=_DiscoveryCapacityUnavailable,
                    pause_on_429=method == "POST", physical_admission_fn=self.physical_admission)
                break
            except _DiscoveryCapacityUnavailable:
                # Wait only for the configured pace. Busy account ownership cancels immediately.
                self.raise_if_cancelled()
                if self.owner.paced._background_lock.locked() or self.owner.paced._lock.locked():
                    self.failure = "unavailable"
                    raise RequestNotSent("RH discovery account busy")
                remaining = self.deadline - self.owner.paced.clock()
                if remaining <= 0:
                    self.failure = "deadline"
                    raise RequestNotSent("RH discovery job deadline")
                self.owner.paced.sleep(min(0.05, remaining))
            except RequestNotSent:
                self.failure = self.failure or "unavailable"
                raise
            except Exception:
                self.failure = "unavailable"
                raise
        self.raise_if_cancelled()                    # late answers cannot publish anything
        status, answer, _headers = reply
        if status != 200 or not isinstance(answer, dict):
            self.failure = "unavailable"
        elif method == "POST":
            error = answer.get("error")
            # A concrete contract revert is an uninitialized/buy-only pool result, not an
            # infrastructure error. It still cannot supply the complete two-direction proof.
            contract_revert = (isinstance(error, dict) and type(error.get("code")) is int
                               and error["code"] == 3 and isinstance(error.get("message"), str)
                               and bool(error["message"].strip()))
            if (type(answer.get("id")) is not type(request.get("id"))
                    or answer.get("id") != request.get("id") or answer.get("jsonrpc") != "2.0"
                    or "error" in answer and "result" in answer
                    or "error" in answer and not contract_revert
                    or "error" not in answer and "result" not in answer):
                self.failure = "unavailable"
        elif host == "api.geckoterminal.com" and not isinstance(answer.get("data"), list):
            self.failure = "unavailable"
        elif host == "api.dexscreener.com" and not (
                answer.get("pairs") is None or isinstance(answer.get("pairs"), list)):
            self.failure = "unavailable"
        return reply                                # real provider failures keep existing handling

    def get_json(self, url):
        from sniper.execution import geckoterminal_quote as gt, solana_quote as ds
        from sniper.execution.evm_rpc_pool import RequestNotSent
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname
        if host == "api.geckoterminal.com":
            if gt._gt_disabled():
                return None                         # same configured discovery-source exclusion
            cooling = gt.cooldown_active(funded=False)
        else:
            cooling = ds.dexscreener_cooldown_active(funded=False)
        if cooling:
            self.failure = "unavailable"
            raise RequestNotSent("RH discovery metadata research provider cooling down")
        _status, answer, headers = self("GET", url, timeout=10)
        if _status == 429:
            # Metadata providers have their own research cells. Their rejection must never
            # pause the RPC account or the funded metadata path.
            if host == "api.geckoterminal.com":
                gt._start_cooldown(time.monotonic(), headers, gt._cfg_cooldown(None), funded=False)
            else:
                ds._ds_start_cooldown(funded=False, reason="discovery HTTP 429")
        self.raise_if_cancelled()
        return answer

    def verify_pool_key(self, chain, mint, pool_id, key):
        from eth_utils import keccak
        from sniper.execution.evm_v4_quote import _encode_pool_key
        from sniper.execution.evm_rpc_pool import RequestNotSent
        self.raise_if_cancelled()
        expected = "0x" + keccak(bytes.fromhex(_encode_pool_key(key))).hex()
        if (str(chain) != "4663" or expected != str(pool_id).lower()
                or str(mint).lower() not in (str(key.get("currency0", "")).lower(),
                                            str(key.get("currency1", "")).lower())):
            self.failure = "unavailable"
            raise RequestNotSent("RH discovery pool identity mismatch")

    def publish(self, pools, expires_at, proofs):
        from sniper.execution.evm_rpc_pool import RequestNotSent
        self.raise_if_cancelled()
        if not proofs or not pools or expires_at <= self.owner.clock():
            self.failure = "unavailable"
            raise RequestNotSent("RH discovery lacks a complete fresh pool proof")
        self.publication = (expires_at, tuple(p for p, _key in proofs))


class DiscoveryPreparation:
    """Bounded discovery-only work on the existing research safety worker/account.

    Native safety has two turns per shared fairness grant; urgent work cancels that grant.
    The listener only notes candidates; entries never join this work or use it as a safety
    verdict. Readiness is an immutable completion record.
    """
    def __init__(self, *, paced, url, cache, generation_fn, available_fn,
                 clock=time.time, monotonic_ns=time.monotonic_ns, boot_id=None):
        from sniper.util.receipt_clock import clock_domain
        self.paced, self.url, self.cache = paced, url, cache
        self.generation_fn, self.available_fn = generation_fn, available_fn
        self.clock, self.monotonic_ns = clock, monotonic_ns
        self.boot_id = clock_domain() if boot_id is None else boot_id
        self.stop_fn = lambda: False
        self.counts = collections.Counter()
        self._candidates, self._records, self._retry_at = {}, {}, {}
        self._lock, self._job_lock = threading.Lock(), threading.Lock()
        self.last_completion_at = None
        self.last_completion_mint = None
        self._native_completions = 0

    def note_candidates(self, mints, *, now):
        with self._lock:
            for mint in list(mints)[:32]:
                if _discovery_mint(mint):
                    self._candidates[str(mint).lower()] = now
            for mint in [m for m, at in self._candidates.items() if now - at > 600]:
                self._candidates.pop(mint, None)
                self._records.pop(mint, None)
                self._retry_at.pop(mint, None)
            while len(self._candidates) > 32:
                oldest = min(self._candidates, key=self._candidates.get)
                self._candidates.pop(oldest, None)
                self._records.pop(oldest, None)
                self._retry_at.pop(oldest, None)

    def next_due(self, now, *, allow_native_due=False):
        if (self.cache.urgent_due(now) if allow_native_due else self.cache.next_due(now) is not None):
            return None
        with self._lock:
            due = [m for m, noted in self._candidates.items() if now - noted <= 600
                   and now >= self._retry_at.get(m, -1e18)
                   and (m not in self._records or self._records[m]["expires_at"] <= now)]
            return min(due, key=lambda m: (self._retry_at.get(m, -1e18), m)) if due else None

    def native_completed(self):
        self._native_completions = min(2, self._native_completions + 1)

    def next_work(self, now):
        native = self.cache.next_due(now)
        shared = self.next_due(now, allow_native_due=self._native_completions >= 2)
        if shared is not None and (native is None or self._native_completions >= 2):
            return "discovery", shared
        return ("native", native) if native is not None else (None, None)

    def run_one(self, mint, *, allow_native_due=False):
        from sniper.execution import evm_v4_quote as v4
        from sniper.execution.evm_rpc_pool import RequestNotSent
        mint = str(mint).lower()
        if not _discovery_mint(mint) or not self._job_lock.acquire(blocking=False):
            return False
        job = None
        try:
            self._native_completions = 0             # one speculative job per fairness grant
            self.counts["attempted"] += 1
            generation = self.generation_fn()
            if generation is None or generation == GENERATION_INFLIGHT:
                self.counts["unavailable"] += 1
                return False
            job = _DiscoveryReads(self, generation, discovery_descriptor(),
                                  allow_native_due=allow_native_due, mint=mint)
            job.raise_if_cancelled()
            pools = v4._quote_pools_for_mint(
                "4663", mint, rpc_url=self.url, http=job, gt_http=job.get_json,
                ds_http=job.get_json, publication_fn=job.publish)
            job.raise_if_cancelled()
            if job.publication is None:
                raise RequestNotSent("RH discovery did not complete a fresh proof")
            expires_at, proven = job.publication
            published = v4._pool_cache.get(("4663", mint))
            if published is None or published[1] is not pools:
                raise RequestNotSent("RH discovery publication replaced")
            completion_ns, completion_at = self.monotonic_ns(), self.clock()
            with self._lock:
                if mint not in self._candidates:
                    raise RequestNotSent("RH discovery candidate no longer retained")
                self._records[mint] = dict(completion_ns=completion_ns, completion_at=completion_at,
                    expires_at=expires_at, generation=generation, descriptor=job.descriptor,
                    published=published, proof_pools=proven)
                self.last_completion_at = completion_at
                self.last_completion_mint = mint
                self.counts["completed"] += 1
            return True
        except Exception:
            reason = "deadline" if job is not None and job.failure == "deadline" else "unavailable"
            self.counts[reason] += 1
            return False
        finally:
            if job is not None:
                job.closed = True
            retry_at = self.clock() + _FAILED_BACKOFF_S
            with self._lock:
                if mint in self._candidates:
                    self._retry_at[mint] = retry_at
                else:
                    self._retry_at.pop(mint, None)
                    self._records.pop(mint, None)
            self._job_lock.release()

    def readiness_at_ingress(self, mint, receipt_clock, *, generation=None, chain="4663", route=None):
        """A causal label only: memory/clock reads, no lock, DB, RPC, or funded permission."""
        from sniper.execution import evm_v4_quote as v4
        from sniper.util.receipt_clock import freeze_receipt_clock
        if route is not None and route not in SHARED_ROUTE_KINDS:
            return {"state": "unknown", "scope": "discovery_only",
                    "reason": "native_preparation_clock_unavailable"}
        receipt_clock = freeze_receipt_clock(receipt_clock)
        if (receipt_clock is None or not self.boot_id or receipt_clock["boot_id"] != self.boot_id):
            return {"state": "unknown", "scope": "discovery_only",
                    "reason": "original_clock_unavailable"}
        ingress = receipt_clock["monotonic_ns"]
        mint = str(mint).lower()
        record = self._records.get(mint)
        if str(chain) != "4663" or record is None:
            return {"state": "cold", "scope": "discovery_only", "reason": "no_completed_proof"}
        ingress_epoch = record["completion_at"] + (ingress - record["completion_ns"]) / 1e9
        if (record["completion_ns"] > ingress or ingress > self.monotonic_ns()
                or ingress_epoch >= record["expires_at"]
                or discovery_descriptor() != record["descriptor"]
                or v4._pool_cache.get(("4663", mint)) is not record["published"]):
            return {"state": "cold", "scope": "discovery_only", "reason": "proof_not_valid_at_ingress"}
        return {"state": "warm", "scope": "discovery_only",
                "completed_monotonic_ns": record["completion_ns"],
                "expires_at": record["expires_at"], "completion_age_s":
                    (ingress - record["completion_ns"]) / 1e9}

    def status(self):
        from sniper.execution import evm_v4_quote as v4
        now = self.clock()
        with self._lock:
            queued = sum(now - at <= 600 and (
                mint not in self._records or self._records[mint]["expires_at"] <= now)
                for mint, at in self._candidates.items())
            mint = self.last_completion_mint
            record = self._records.get(mint)
            completion = None if record is None else dict(
                scope="discovery_only", chain_id="4663", mint=mint, boot_id=self.boot_id,
                proof_pool_ids=list(record["proof_pools"])[:8],
                completion_monotonic_ns=record["completion_ns"],
                completion_epoch=record["completion_at"], expires_at=record["expires_at"],
                cache_valid=(now < record["expires_at"]
                    and discovery_descriptor() == record["descriptor"]
                    and v4._pool_cache.get(("4663", mint)) is record["published"]))
        return dict(attempted=self.counts["attempted"], completed=self.counts["completed"],
                    unavailable=self.counts["unavailable"], deadline=self.counts["deadline"],
                    last_completion=completion,
                    queue_size=queued, completion_age_s=(None if self.last_completion_at is None
                                                        else now - self.last_completion_at))


class InlineHostDead(RuntimeError):
    """The inline safety host failed `DEAD_AFTER` transport reads in a row: nothing is sent to it
    until its dead mark ends."""


def is_transport_failure(exc):
    """True when no answer came back at all (TLS, connect, reset, timeout); an HTTP status is an
    answer (HTTPError subclasses URLError, which subclasses OSError, so it is excluded first)."""
    import http.client
    import urllib.error
    from sniper.execution.fast_http import FastHttpError
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (OSError, http.client.HTTPException, FastHttpError))


class PacedHttp:
    """The 4663 research host's budget for the safety cache (Alpha: M1 plus the cache at or under
    1 call a second): at most one request every `min_gap_s`, and a 429 pauses every request for
    `pause_s`. Wraps the EVM http seam `(method, url, headers, body, timeout) -> (status, json,
    headers)`; anything else passes through untouched.

    The inline seam also keeps its host's transport health here (Alpha 2026-10-02: the inline
    QuickNode host answered every call with a TLS alert): `DEAD_AFTER` transport failures in a row
    mark the host dead for `DEAD_S`; any answer resets the run."""

    DEAD_AFTER, DEAD_S = 3, 600.0
    QUOTA_PROBE_S = 900.0      # a monthly-RU quota ends on the provider's calendar, not ours: one probe per interval

    def __init__(self, base=None, *, min_gap_s=2.0, pause_s=120.0, clock=time.monotonic,
                 sleep=time.sleep):
        if base is None:
            from sniper.execution.evm_wallet import _http as base
        self.base, self.min_gap_s, self.pause_s = base, min_gap_s, pause_s
        self.clock, self.sleep = clock, sleep
        self.counts = {"calls": 0, "http_429": 0, "transport_errors": 0, "dead_marked": 0, "quota_403": 0}
        self.quota_alert = None
        self.quota_probe = None       # () -> (status, answer): one cheap read, set by the wiring
        self._quota, self._quota_probe_at, self.quota_gen = False, 0.0, 0
        self._lock = threading.Lock()
        self._background_lock = threading.Lock()
        self._last, self._paused_until = None, 0.0
        self._run, self._dead_until = 0, 0.0

    def paused(self):
        return self.clock() < self._paused_until

    @staticmethod
    def is_quota_answer(status, answer):
        """Chainstack answers a spent monthly quota with HTTP 403 and JSON-RPC -32005 (live 2026-10-28)."""
        err = answer.get("error") if isinstance(answer, dict) else None
        return status == 403 and isinstance(err, dict) and (
            err.get("code") == -32005 or "monthly quota" in str(err.get("message") or "").lower())

    def quota_spent(self):
        return self._quota                               # one bool read: no lock on the signal path

    def quota_blocks(self):
        return self._quota

    def quota_probe_due(self):
        """True once per QUOTA_PROBE_S while spent; only the warmer asks, and it owns the probe."""
        with self._lock:
            if self._quota and self.clock() >= self._quota_probe_at:
                self._quota_probe_at = self.clock() + self.QUOTA_PROBE_S
                return True
            return False

    def quota_recover(self):
        """Background only, from its own thread whatever the warmer's mode: one owned probe per interval."""
        if self._quota and self.quota_probe is not None and self.quota_probe_due():
            try:
                gen = self.quota_gen
                self.quota_probe_result(*self.quota_probe(), gen=gen)
            except Exception:
                logger.exception("rh fast lane: inline quota probe failed")

    def _quota_page(self, entered):
        if self.quota_alert is None:
            return
        try:
            self.quota_alert("4663 inline safety host monthly quota SPENT (HTTP 403 / -32005): inline "
                             "checks and the warmer stop; one probe per %.0f min" % (self.QUOTA_PROBE_S / 60)
                             if entered else "4663 inline safety host quota is back: inline checks resume")
        except Exception:
            logger.exception("rh fast lane: inline quota page not sent")

    def note_answer(self, status, answer):
        """A quota answer from ANY read marks the host spent (page once). Only the owned probe
        clears it: a late 200 from a read sent before the mark proves nothing."""
        if not self.is_quota_answer(status, answer):
            return
        with self._lock:
            self.counts["quota_403"] += 1
            entered = not self._quota
            if entered:
                self._quota, self._quota_probe_at = True, self.clock() + self.QUOTA_PROBE_S
                self.quota_gen += 1
        if entered:
            self._quota_page(True)

    def quota_probe_result(self, status, answer, gen=None):
        """The warmer's probe answer: a plain 200 JSON-RPC result clears the state and pages."""
        if self.is_quota_answer(status, answer):
            with self._lock:
                self.counts["quota_403"] += 1
            return
        result = answer.get("result") if isinstance(answer, dict) else None
        if (status == 200 and isinstance(answer, dict) and "error" not in answer and isinstance(result, str)
                and re.fullmatch(r"0x[0-9a-fA-F]+", result)):          # a real block number, nothing else heals
            with self._lock:
                cleared = self._quota and (gen is None or gen == self.quota_gen)   # this latch's own probe only
                if cleared:
                    self._quota = False
            if cleared:
                self._quota_page(False)

    def dead(self):
        return self.clock() < self._dead_until

    def transport(self, ok):
        """Note one read: `ok` False for a transport failure. Returns the failures in a row."""
        with self._lock:
            if ok:
                self._run = 0
                return 0
            self.counts["transport_errors"] += 1
            self._run += 1
            if self._run >= self.DEAD_AFTER and not self.dead():
                self.counts["dead_marked"] += 1
                self._dead_until = self.clock() + self.DEAD_S
            return self._run

    def pause(self):
        """A 429 seen on another path to the same host pauses this budget too."""
        with self._lock:
            self.counts["http_429"] += 1
            self._paused_until = self.clock() + self.pause_s

    def __call__(self, method, url, headers=None, body=None, timeout=10):
        # Serialize background reads without holding the inline health/counter lock over I/O.
        with self._background_lock:
            with self._lock:
                if self.paused():
                    raise ResearchPaused("research host paused after a 429")
                wait = (self.min_gap_s - (self.clock() - self._last)
                        if self._last is not None else 0)
            if wait > 0:
                self.sleep(wait)
            with self._lock:
                if self.paused():
                    raise ResearchPaused("research host paused after a 429")
                self._last = self.clock()
                self.counts["calls"] += 1
            status, body_json, hdrs = self.base(method, url, headers=headers, body=body,
                                                timeout=timeout)
            if status == 429:
                self.pause()
            return status, body_json, hdrs

    def try_background(self, method, url, headers=None, body=None, timeout=10, *,
                       admission_fn, wait_for_pace=False, refusal_exc=ResearchPaused,
                       pause_on_429=True, physical_admission_fn=None):
        """Optional speculative grant on this same account; busy ownership always skips.

        BSC's existing candidate worker opts into waiting for the configured gap outside locks.
        They recheck admission every 50 ms and again at the physical request boundary. The
        default never waits. RH/shared background and inline callers keep their existing seams.
        """
        while True:
            if not admission_fn():
                raise refusal_exc("speculative capacity unavailable")
            if not self._background_lock.acquire(blocking=False):
                raise refusal_exc("background account busy")
            try:
                if not self._lock.acquire(blocking=False):
                    raise refusal_exc("background account busy")
                try:
                    if self.paused() or self.dead() or not admission_fn():
                        raise refusal_exc("speculative capacity unavailable")
                    wait = (self.min_gap_s - (self.clock() - self._last)
                            if self._last is not None else 0)
                    if wait <= 0:
                        self._last = self.clock()
                        self.counts["calls"] += 1
                finally:
                    self._lock.release()
                if wait <= 0:
                    background_call = getattr(self.base, 'background_call', None)
                    send = background_call if callable(background_call) else self.base
                    extra = {'admission_fn': physical_admission_fn or admission_fn} if callable(background_call) else {}
                    status, body_json, hdrs = send(method, url, headers=headers, body=body,
                                                   timeout=timeout, **extra)
                    if status == 429 and pause_on_429:
                        self.pause()
                    return status, body_json, hdrs
            finally:
                self._background_lock.release()
            if not wait_for_pace:
                raise refusal_exc("background account pace unavailable")
            self.sleep(min(wait, 0.05))


class WarmDexHttp:
    """The fast lanes' DexScreener connection, KEPT ALIVE (operator 2026-10-14: "fix the dexscreener
    thing, we need a faster way"). 73851 on 56: the lane's inline market-stats read was most of its
    185 ms setup, because every call opened a new TLS connection on a new thread (cold urllib GET
    41-237 ms from this host; the same GET on a warm pooled connection 14-20 ms). One client per
    process, pinged every MEMECOIN_DEX_WARM_INTERVAL_S so a quiet lane never pays the handshake.
    Same request and JSON; an error status raises urllib's HTTPError so DexBudget's 429 stop and
    the shared breaker keep working unchanged. It is the TRANSPORT inside today's _ds_default_http,
    so the 6 s whole-request deadline (the 09-25 DNS stall) and the 8 research slots still bound it;
    the pool is sized to those 8 slots so an inline read never queues behind the prefetch."""

    PING_URL = "https://api.dexscreener.com/latest/dex/tokens/0x0000000000000000000000000000000000000001"

    def __init__(self, *, client_factory=None, start_warmer=None):
        self._factory = client_factory or self._default_client
        self._start_warmer = start_warmer or self._default_warmer
        self._client = None
        self._lock = threading.Lock()

    @staticmethod
    def _default_client():
        from sniper.execution.fast_http import PooledHttpClient
        from sniper.execution.solana_quote import _DS_TIMEOUT_S
        return PooledHttpClient(max_connections=8, max_keepalive=4, connect_timeout_s=_DS_TIMEOUT_S,
                                read_timeout_s=_DS_TIMEOUT_S, write_timeout_s=_DS_TIMEOUT_S,
                                pool_timeout_s=_DS_TIMEOUT_S,
                                # <=60 s: past Cloudflare's unknown idle close a reused socket can
                                # reset and trip the research breaker (Gamma NIT on 8c825acd3)
                                keepalive_expiry_s=min(3 * cfg_interval(), 60.0))

    @staticmethod
    def _default_warmer(warm_fn, interval_s):
        from sniper.execution.fast_http import start_warming
        start_warming((("dexscreener", warm_fn),), interval_s=interval_s)

    def _get(self):
        with self._lock:
            if self._client is None:
                self._client = self._factory()
                # Started on the first real request, never at construction: a process (or test)
                # that never asks DexScreener never pings it.
                try:
                    self._start_warmer(self._ping, cfg_interval())
                except Exception:      # a warmer that will not start must never fail the read
                    logger.exception("dexscreener warm client: warmer did not start (reads go cold)")
            return self._client

    def _ping(self):
        """Keep the connection warm; never while the research breaker cools (a 429 or a dead
        provider): the ping must not be what keeps DexScreener refusing us."""
        from sniper.execution.solana_quote import dexscreener_cooldown_active
        if not dexscreener_cooldown_active(funded=False):
            self("GET", self.PING_URL)

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        import urllib.error
        from sniper.execution.solana_quote import _UA
        merged = {"user-agent": _UA, "accept": "application/json", **(headers or {})}
        status, payload, date = self._get().request(method, url, headers=merged, body=body,
                                                    timeout=timeout)
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "dexscreener %d" % status, None, None)
        return status, ({} if payload == "" else payload), date   # urllib: empty body -> {}


def cfg_interval():
    """MEMECOIN_DEX_WARM_INTERVAL_S clamped to 1..3600 s (20 when unreadable). Warming off is
    MEMECOIN_DEX_WARM_HTTP=0, not an interval of 0."""
    import math
    from sniper.settings import memecoin as cfg
    try:
        value = float(cfg.MEMECOIN_DEX_WARM_INTERVAL_S)
    except (TypeError, ValueError):
        return 20.0
    return max(1.0, min(3600.0, value)) if math.isfinite(value) else 20.0


_WARM_DEX = None
_WARM_DEX_LOCK = threading.Lock()


def lane_dex_base(dex_base):
    """The base a fast lane's DexBudget uses: an injected one as given; else today's deadline-bound
    research worker sending through the shared warm client (MEMECOIN_DEX_WARM_HTTP, default on);
    else None (today's per-call urllib worker). Called at the lanes' start, never in a builder."""
    global _WARM_DEX
    from functools import partial
    from sniper.execution.solana_quote import _ds_default_http
    from sniper.settings import memecoin as cfg
    if dex_base is not None or not cfg.MEMECOIN_DEX_WARM_HTTP:
        return dex_base
    with _WARM_DEX_LOCK:
        if _WARM_DEX is None:
            _WARM_DEX = WarmDexHttp()
        return partial(_ds_default_http, funded=False, transport=_WARM_DEX)


def lane_facts_once(mint, *, http, host):
    """The 4663 lane's (decimals, supply): one batch [decimals, totalSupply] on `host` (BSC cut 4's
    shared-gate fix, 2026-10-14), paired by id. ANY batch failure, or a decimals answer the batch
    cannot read, is today's exact code: erc20_decimals (None on failure, with its retry), then the
    supply read. A supply that is not a word is None, as today."""
    from sniper.execution import evm_wallet
    try:
        status, answer, _h = http("POST", host, body=json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
             "params": [{"to": mint, "data": "0x313ce567"}, "latest"]},
            {"jsonrpc": "2.0", "id": 2, "method": "eth_call",
             "params": [{"to": mint, "data": "0x18160ddd"}, "latest"]}]))
        ids = [item.get("id") if isinstance(item, dict) else None for item in answer] if isinstance(
            answer, list) else []
        by_id = {item["id"]: item for item in answer} if sorted(ids, key=str) == [1, 2] else {}
        # exactly ids 1 and 2, once each: a repeated, extra or missing id is today's reads (Gamma LOW)
        decimals = int(by_id[1]["result"], 16) if status == 200 and by_id else None
        supply_answer = by_id[2] if decimals is not None else None
    except Exception:
        decimals = supply_answer = None
    def word(answer):
        result = (answer or {}).get("result")
        return isinstance(result, str) and len(result) > 2

    if decimals is None or decimals < 0:
        decimals, supply_answer = evm_wallet.erc20_decimals(mint, host, http=http), None
    if not word(supply_answer):              # today's supply read, with _rpc's own handling
        supply_answer = evm_wallet._rpc(host, "eth_call", [{"to": mint, "data": "0x18160ddd"}, "latest"],
                                        http=http)
    result = (supply_answer or {}).get("result")
    return decimals, (int(result, 16) if word(supply_answer) else None)


class DexBudget:
    """The lane's DexScreener calls (Alpha 2026-10-01): the SAME dexscreener_stats the runner's entry
    hook calls, through today's research-role worker, every request counted. A 429 STOPS every lane
    call for the life of the process: today's shared breaker cools down on it too, and the lane must
    not be what keeps the provider refusing the funded path. Once stopped, no request goes out, so
    the prefetch can never start that cooldown itself."""

    def __init__(self, base=None):
        if base is None:
            from functools import partial
            from sniper.execution.solana_quote import _ds_default_http
            base = partial(_ds_default_http, funded=False)
        self.base = base
        self.counts = collections.Counter()          # calls / http_429
        self.stopped = None                          # why the lane stopped asking, once it has
        self._lock = threading.Lock()

    def http(self, method, url, headers=None, body=None):
        import urllib.error
        with self._lock:
            self.counts["calls"] += 1
        try:
            return self.base(method, url, headers, body)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                with self._lock:
                    self.counts["http_429"] += 1
                    first = self.stopped is None
                    self.stopped = "dexscreener_429"
                if first:
                    logger.warning("rh fast lane: DexScreener answered 429; the lane's market-data "
                                   "calls are stopped until restart (every signal falls back)")
            raise

    def stats(self, mint, network_id="4663"):
        """Today's market stats for `mint` on `network_id` (the 4663 lane's by default), or None:
        stopped, cooling down, unlisted, failed."""
        if self.stopped:
            return None
        from sniper.execution.solana_quote import dexscreener_stats
        return dexscreener_stats(mint, http=self.http, network_id=network_id)



class WarmStatsPacer:
    """One DexScreener stats call per `gap_s` across every lane's safety warmer in this process
    (lambda + Beta, Alpha GO 2026-10-14): the provider's 429 is per IP, stops every lane stats call
    until restart (DexBudget), and the funded breaker shares the provider."""

    def __init__(self, gap_s, clock=time.monotonic):
        self.gap_s, self.clock, self._at, self._lock = float(gap_s), clock, -1e18, threading.Lock()

    def take(self):
        """True, and the slot is spent, when a warm stats call may go out now."""
        with self._lock:
            now = self.clock()
            if now - self._at < self.gap_s:
                return False
            self._at = now
            return True


WARM_STATS = WarmStatsPacer(lane_cfg.MEMECOIN_WARM_STATS_GAP_S)

def background_safety(check, rotation, paced):
    """Provider failures stay absent in the prefetch cache, so a healthy inline host can check.
    A genuine token refusal is still cached. Background only; no entry-path reads or retries."""
    def failures():
        if rotation is None:                      # the inline host's warmer: its pacer alone
            return (paced.counts["http_429"],)
        return (sum(s["quota"] + s["transport"] for s in rotation.stats()[1]),
                paced.counts["http_429"], rotation.provider_errors())

    def checked(mint, usd=None):
        before = failures()
        ok, reason = check(mint, usd=usd)
        incomplete = str(reason or "").startswith((
            "code read failed", "sell-route probe failed", "sell simulation failed",
            "sell_sim_rpc_unknown", "safety_deadline_exceeded", "state_not_ready:"))
        if not ok and (failures() != before or paced.paused() or incomplete):
            raise ResearchPaused("background safety provider unavailable")
        return ok, reason
    return checked


class SafetyCache:
    """Pre-signal token-safety verdicts (spec §2.2; Alpha option 1). A verdict is today's
    token_safety_fn at the lane's clip, kept for `ttl_s` (older is absent: the signal falls back).
    Candidates come from the detector with a priority: 0 for a mint a signal or an armed wallet
    touched, the activity rank otherwise; the most urgent due mint is checked next.

    THE GATES' MARKET DATA (Alpha 2026-10-01). The background worker fetches the coin's market
    stats with `stats_fn` (DexBudget.stats, today's function), kept for the same TTL. They are gate
    inputs only: the hook's volume, sell-share, chase-display, age and band gates read them; the
    lane's min-out, cap, booking and grading never do. Facts and stats publish before safety and
    remain independent of its verdict."""

    def __init__(self, safety_fn, *, ttl_s, usd, facts_fn=None, stats_fn=None, refresh_lead_s=0.0,
                 supply_ttl_s=300.0, clock=time.time, facts_sink=None):
        self.safety_fn, self.ttl_s, self.usd = safety_fn, ttl_s, usd
        # SUPPLY EXPIRES (Codex EVM-07): decimals never change, totalSupply can (a mint, a burn, a
        # rebase), and the lane's mcap is supply x price. A supply older than `supply_ttl_s` is
        # unknown, so the lane falls back; a passing verdict refreshes it once it is past half that.
        self.supply_ttl_s, self.clock = float(supply_ttl_s), clock
        self._facts_at = {}                          # mint -> when its supply was read (cache clock)
        # REFRESH AHEAD (Gamma 2026-10-02): a signalled coin (priority 0) is due `refresh_lead_s`
        # before its verdict goes stale, so a repeat buy finds it fresh even while another verdict
        # is in flight (~16 s each at the research host's 2 s gap).
        self.refresh_lead_s = float(refresh_lead_s)
        self.facts_fn = facts_fn                     # mint -> (decimals, total supply atomic)
        self.facts_sink = facts_sink
        self.stats_fn = stats_fn                     # mint -> market stats dict, or None
        self._facts = {}
        self._stats = {}                             # mint -> (stats, fetched at)
        self._lock = threading.Lock()
        self._priority = {}                          # mint -> (priority, noted_at)
        self._verdicts = {}                          # mint -> (ok, reason, at, cpu_ms)
        self.counts = collections.Counter()          # verdicts_ok / _unsafe / verdict_errors
        self._warmup = {}                            # bounded by candidate lifetime
        self._retry_at = {}                          # mint -> no check before this, after a raise
        # THE SHARED WARM TIERS' EVIDENCE (lambda + Beta, 2026-10-14): signal-time lookups by the
        # mint's warm tier ("<tier>:hit|miss"), and each tier's hit verdict age [sum s, n].
        self.lookups, self.hit_age_s = collections.Counter(), {}
        self._inflight = set()                       # mints a worker is verdicting now (two warmers)
        self._warm_tier = {}                         # mint -> its tier in the detector's last list

    def note_candidates(self, entries, *, now, warm=True):
        """Queue `entries` by priority. `warm` (the detector's candidates) also sets each coin's
        warm tier for the lookup counters; the lane's own signal note (-1, a queue rank) passes
        warm=False so it never relabels the tier the coin was warmed in (Gamma MEDIUM, 9a234644b)."""
        with self._lock:
            for e in entries:
                mint = str(e["mint"]).lower()
                pri = int(e.get("priority", 99))
                old = self._priority.get(mint)
                self._warmup.setdefault(mint, {"candidate_first_at": now})
                self._warmup[mint]["candidate_last_at"] = now
                self._priority[mint] = (min(pri, old[0]) if old and now - old[1] < 60 else pri, now)
                if warm:
                    self._warm_tier[mint] = pri          # the latest list's tier, never a min
            for mint in [m for m, (_p, at) in self._priority.items() if now - at > 600]:
                self._warmup.pop(mint, None)
                self._priority.pop(mint, None)       # a candidate unseen for 10 min is dropped
                self._warm_tier.pop(mint, None)

    def next_due(self, now):
        """The most urgent due mint: by priority, then the LEAST RECENTLY CHECKED within it (never
        checked first). Gamma 2026-10-02: ties used to break by mint address, so with more
        priority-0 coins due than the host can verdict, a high-address coin starved."""
        with self._lock:
            due = [(pri, self._verdicts[mint][2] if mint in self._verdicts else -1e18, mint)
                   for mint, (pri, _at) in self._priority.items()
                   if (mint not in self._verdicts or now - self._verdicts[mint][2] > self.ttl_s - (
                       self.refresh_lead_s if pri <= 0 else 0.0))
                   and now >= self._retry_at.get(mint, -1e18) and mint not in self._inflight]
        return min(due)[2] if due else None

    def urgent_due(self, now):
        """Background-only priority check; original candidate TTL/verdict rules are unchanged."""
        with self._lock:
            return any(pri < 0 and (
                mint not in self._verdicts or now - self._verdicts[mint][2]
                > self.ttl_s - self.refresh_lead_s)
                and now >= self._retry_at.get(mint, -1e18)
                for mint, (pri, _at) in self._priority.items())

    def run_one(self, mint, *, now, safety_fn=None, facts_fn=None, stats=True):
        """One coin's facts, stats (when `stats`) and verdict, on `safety_fn`/`facts_fn` (the inline
        warmer's host) or the cache's own. None when it raised or another worker holds the coin."""
        with self._lock:
            if mint in self._inflight:
                return None
            self._inflight.add(mint)
        try:
            return self._run_one(mint, now=now, safety_fn=safety_fn or self.safety_fn,
                                 facts_fn=facts_fn or self.facts_fn, stats=stats)
        finally:
            with self._lock:
                self._inflight.discard(mint)

    def _run_one(self, mint, *, now, safety_fn, facts_fn, stats):
        c0, started = time.thread_time(), time.monotonic()
        with self._lock:
            if mint in self._warmup:
                self._warmup[mint]["warm_worker_start_at"] = now
        # Publish inputs before the slower verdict, without asserting token safety.
        if facts_fn is not None and self._supply_due(mint, now):
            observed_at = now + time.monotonic() - started
            try:
                self.put_facts(mint, facts_fn(mint), now=observed_at)
            except Exception as exc:
                logger.warning("rh fast safety: facts %s for %s", type(exc).__name__, mint)
        if stats and self.stats_fn is not None:
            observed_at = now + time.monotonic() - started
            try:
                got = self.stats_fn(mint)
            except Exception as exc:
                logger.warning("rh fast safety: stats %s for %s", type(exc).__name__, mint)
                got = None
            with self._lock:
                if isinstance(got, dict) and got:
                    self._stats[mint] = (dict(got), observed_at)
                    self.counts["stats_ok"] += 1
                else:
                    self._stats.pop(mint, None)
                    self.counts["stats_missing"] += 1
        safety_at = now + time.monotonic() - started
        try:
            ok, reason = safety_fn(mint, usd=self.usd)
        except Exception as exc:
            logger.warning("rh fast safety: %s for %s", type(exc).__name__, mint)
            with self._lock:
                if mint in self._warmup:
                    self._warmup[mint].update(
                        warm_worker_end_at=now + time.monotonic() - started,
                        warm_worker_outcome="provider_error:" + type(exc).__name__)
                self._verdicts.pop(mint, None)
                self._retry_at[mint] = now + time.monotonic() - started + (
                    _BRIDGE_LEG_BACKOFF_S if isinstance(exc, BridgeLegFailed) else _FAILED_BACKOFF_S)
                self.counts["verdict_errors"] += 1
            return None
        rec = (bool(ok), reason, safety_at,
               (time.thread_time() - c0) * 1000.0)
        with self._lock:
            if mint in self._warmup:
                self._warmup[mint].update(
                    warm_worker_end_at=now + time.monotonic() - started,
                    warm_worker_outcome="passed" if rec[0] else "unsafe")
            self._verdicts[mint] = rec
            self.counts["verdicts_ok" if rec[0] else "verdicts_unsafe"] += 1
            self.counts["cpu_ms"] += rec[3]
        return rec

    def put(self, mint, *, now, facts=None, stats=None, reason=None):
        """A passing verdict a caller checked itself (the BSC lane's inline check on a cache miss),
        with the facts and stats it READ: cached exactly as run_one would have. `facts` must be a
        fresh chain read, never a cached value handed back, because it is stamped `now`. `reason`
        is that check's own pass basis, kept for the cache hit (Gamma 2026-10-07)."""
        mint = str(mint).lower()
        with self._lock:
            self._verdicts[mint] = (True, reason, now, 0.0)
            self.counts["verdicts_inline"] += 1
            if facts and facts[0] is not None and facts[1]:
                self._facts[mint] = (int(facts[0]), int(facts[1]))
                self._facts_at[mint] = now
            if isinstance(stats, dict) and stats:
                self._stats[mint] = (dict(stats), now)

    def _supply_due(self, mint, now):
        with self._lock:
            at = self._facts_at.get(mint)
            known = mint in self._facts
        return not known or (at is not None and now - at > self.supply_ttl_s / 2)

    def facts(self, mint, now=None):
        """(decimals, supply atomic) while the supply is younger than `supply_ttl_s`, else None:
        an expired supply is unknown, so the live lane falls back (Codex EVM-07). An entry written
        without a time (a test placing it directly) never expires; every writer here stamps one."""
        mint = str(mint).lower()
        now = self.clock() if now is None else now
        with self._lock:
            got, at = self._facts.get(mint), self._facts_at.get(mint)
        if got is None or (at is not None and now - at > self.supply_ttl_s):
            return None
        return got

    def put_facts(self, mint, facts, now=None):
        """Publish chain facts independently of a safety verdict."""
        if facts and facts[0] is not None and facts[1] is not None:
            decimals, supply = facts
            if type(decimals) is int and 0 <= decimals <= 255 and type(supply) is int and supply > 0:
                with self._lock:
                    self._facts[str(mint).lower()] = (decimals, supply)
                    self._facts_at[str(mint).lower()] = self.clock() if now is None else now
                if self.facts_sink is not None:
                    try:
                        self.facts_sink(str(mint).lower(), (decimals, supply))
                    except Exception:
                        logger.exception("rh fast safety: shared facts publish failed for %s", mint)

    def put_market_stats(self, mint, stats, *, now):
        """Publish a market read without claiming that token safety passed."""
        if isinstance(stats, dict) and stats:
            with self._lock:
                self._stats[str(mint).lower()] = (dict(stats), now)

    def stats(self, mint, *, now):
        """(stats copy, fetched at) when fetched within the TTL, else None (the signal falls back)."""
        with self._lock:
            got = self._stats.get(str(mint).lower())
        if got is None or now - got[1] > self.ttl_s:
            return None
        return dict(got[0]), got[1]

    def size(self):
        with self._lock:
            return len(self._priority)

    def cached_answer(self, mint, label):
        """(True, "<label>: <the pass's own reason>") for a cache hit. A PASS RECORDS ITS BASIS
        (Gamma 2026-10-07): a bare label hid which check admitted the coin; no reason is
        `unknown`, never `ok`. Callers have already established a fresh pass via `reason()`; a
        verdict a worker replaced with a refusal (or dropped) since then answers False (Alpha
        2026-10-14: this used to pass whatever the verdict had become)."""
        v = self.verdict(mint)
        if v is None or not v[0]:
            return False, "%s: verdict_changed:%s" % (label, "absent" if v is None else v[1] or "refused")
        return True, "%s: %s" % (label, v[1] or "unknown")

    def verdict(self, mint):
        """The mint's verdict tuple (ok, reason, at, cpu_ms) as one read, or None. Counts nothing:
        the warm-tier lookups stay the native lane's evidence."""
        with self._lock:
            return self._verdicts.get(str(mint).lower())

    def reason(self, mint, *, now, evidence=None):
        """None when a fresh passing verdict exists, else the named fallback."""
        with self._lock:
            v = self._verdicts.get(str(mint).lower())
            if evidence is not None:
                warmup = self._warmup.get(str(mint).lower(), {})
                evidence.update({key: warmup.get(key) for key in (
                    "candidate_first_at", "candidate_last_at", "warm_worker_start_at",
                    "warm_worker_end_at", "warm_worker_outcome")})
                evidence["verdict_at"] = v[2] if v is not None else None
                evidence["lookup_at"] = now
            tier = self._tier(str(mint).lower())
            fresh = v is not None and now - v[2] <= self.ttl_s
            self.lookups["%s:%s" % (tier, "hit" if fresh and v[0] else "miss")] += 1
            if fresh and v[0]:
                age = self.hit_age_s.setdefault(tier, [0.0, 0])
                age[0] += now - v[2]
                age[1] += 1
        if v is None:
            return "no_safety_verdict"
        if now - v[2] > self.ttl_s:
            return "safety_stale"
        return None if v[0] else "unsafe:%s" % (v[1] or "refused")

    def in_flight(self, mint):
        """True while a worker is verdicting `mint` (run_one's claim)."""
        with self._lock:
            return str(mint).lower() in self._inflight

    def _tier(self, mint):
        """The mint's warm tier label (caller holds the lock): -1, 0, 1, activity, or other (not in
        the detector's warm set; the same word as 56's BscSafetyCache)."""
        got = self._warm_tier.get(mint)
        if got is None:
            return "other"
        return str(got) if got <= 1 else "activity"



_QUOTA = re.compile(r"429|quota|rate.?limit|too many requests|limit exceeded", re.I)


class InlineWarmer:
    """The safety cache's second worker, on the inline host while it is idle (spec 2026-10-14
    rh-fast-cached-only rev 2 §3.6; operator via Alpha: 'Go, it's unlimited / plenty'). Idle by
    construction: it reads only while MEMECOIN_RH_FAST_CACHED_ONLY is on, when the fill path makes
    no inline read. The first 429 or quota answer pauses it `pause_s` (counted; alerted once per
    process). A provider failure never caches a verdict (background_safety). Stats only when the
    coin has none, through the process-wide WARM_STATS pacer (DexScreener's 429 is per IP)."""

    def __init__(self, cache, *, safety_fn, facts_fn, host, gap_s, stats_pacer=None, pause_s=600.0,
                 clock=time.monotonic, log=print, alert_fn=None):
        self.cache, self.facts_fn, self.host = cache, facts_fn, host
        self.safety_fn_raw = safety_fn
        self.stats_pacer = stats_pacer or WARM_STATS
        self.gap_s, self.pause_s, self.clock, self.log = float(gap_s), float(pause_s), clock, log
        if alert_fn is None:
            from sniper.services.memecoin_alerts import send_alert

            def alert_fn(text):
                send_alert("rh_fast_lane", text)
        self.alert_fn, self._alerted = alert_fn, False
        self.paused_until, self._quota = 0.0, False
        self.counts = collections.Counter()

    def _safety(self, mint, usd=None):
        try:
            ok, reason = self.safety_fn_raw(mint, usd=usd)
        except Exception as exc:
            self._quota = self._quota or bool(_QUOTA.search(str(exc)))
            raise
        if not ok and _QUOTA.search(str(reason or "")):
            self._quota = True
            raise ResearchPaused("inline host quota: %s" % str(reason)[:80])
        return ok, reason

    def step(self, now):
        """One verdict when due; returns the seconds to wait before the next step."""
        if not lane_cfg.MEMECOIN_RH_FAST_CACHED_ONLY or self.clock() < self.paused_until:
            return 1.0
        if self.host.paused() or self.host.dead():
            return 1.0
        if self.host.quota_spent():
            return 1.0                                  # monthly quota spent: the recovery thread probes
        mint = self.cache.next_due(now)
        if mint is None:
            return 1.0
        if self.cache.in_flight(mint):
            # Another worker claimed it after next_due handed it out (Gamma LOW 3, 96bcf06de): a
            # skip, not an error, and no WARM_STATS slot spent on it.
            self.counts["skipped"] += 1
            return self.gap_s
        stats = self.cache.stats(mint, now=now) is None and self.stats_pacer.take()
        before, self._quota = self.host.counts["http_429"], False
        rec = self.cache.run_one(mint, now=now, safety_fn=background_safety(self._safety, None, self.host),
                                 facts_fn=self.facts_fn, stats=stats)
        if rec is None and self.cache.in_flight(mint):
            self.counts["skipped"] += 1                  # claimed between the check and run_one
        else:
            self.counts["verdicts"] += 1
            self.counts["passes" if rec and rec[0] else "refusals" if rec else "errors"] += 1
        # A 429 or quota answer this step drew pauses the warmer whatever the counting said
        # (Gamma LOW on 4b6814d47: the skip used to return before this check).
        if self.host.counts["http_429"] > before or self._quota:
            self.paused_until = self.clock() + self.pause_s
            self.counts["pauses"] += 1
            why = "429" if self.host.counts["http_429"] > before else "quota answer"
            self.log("[rh-fast-lane] inline warmer paused %.0fs after a %s" % (self.pause_s, why))
            if not self._alerted:
                self._alerted = True
                try:
                    self.alert_fn("4663 inline warmer paused %.0f s after a %s from the inline safety "
                                  "host (counted; later pauses are not alerted)" % (self.pause_s, why))
                except Exception:
                    logger.exception("rh fast lane: inline warmer alert failed")
        return self.gap_s

def clip_wei_for(clip_usd, eth_usd):
    """The clip in wei at the datagram's ETH price, or None when the price is absent."""
    if not eth_usd or eth_usd <= 0:
        return None
    return int(Fraction(str(clip_usd)) / Fraction(str(eth_usd)) * 10 ** 18)


def inputs_reason(payload, *, generation, now_host, clip_wei, gas_limit, low_wei,
                  max_snapshot_age_s, eth_usd_max_age_s, generation_margin_s=0.0, extra_gas_wei=0):
    """None when the datagram's research-host inputs can be acted on with NO read, else the named
    fallback (spec §4, Alpha's invariant 2). Host clock throughout: the snapshot's `read_at`, the
    execution `generation` stamp and `now_host`. A snapshot read at or before the last execution's
    end may predate its spend; a stale or absent price, balance or gas is absent, never zero.
    `extra_gas_wei`: what a lane's own bid (56 crowded gas) pays above the snapshot's price on THIS
    send; the float must also hold it. The reserve for later swaps stays at the snapshot's price."""
    from sniper.execution.evm_native_float import float_covers
    if generation == GENERATION_INFLIGHT:
        return GENERATION_INFLIGHT
    if generation is None:
        return "generation_unknown"
    snap = payload.get("snapshot")
    if not isinstance(snap, dict) or snap.get("read_at") is None:
        return "no_snapshot"
    # The margin (Gamma LOW): the research node follows the sequencer, so a poll that starts just
    # after our own send can still answer the old pending nonce.
    if float(snap["read_at"]) <= float(generation) + float(generation_margin_s):
        return "snapshot_predates_execution"
    if now_host - float(snap["read_at"]) > max_snapshot_age_s:
        return "snapshot_stale"
    if not payload.get("eth_usd") or payload.get("eth_usd_at") is None:
        return "no_eth_usd"
    if now_host - float(payload["eth_usd_at"]) > eth_usd_max_age_s:
        return "eth_usd_stale"
    native = snap.get("native_wei")
    if extra_gas_wei and type(native) is int:
        native -= int(extra_gas_wei)
    if clip_wei is None or not float_covers(native, clip_wei, gas_limit, snap.get("gas_price_wei"),
                                            low_wei):
        return "float_short"
    return None


def bid_extra_wei(snap, gas_limit, bid_wei):
    """Native a bid pays above the snapshot's gas price on one send (never negative; 0 unknown)."""
    try:
        return max(0, int(gas_limit) * (int(bid_wei) - int(snap["gas_price_wei"])))
    except (KeyError, TypeError, ValueError):
        return 0


# A post is open on the chain in some process (EVM-08 intent fence): the lane refuses.
GENERATION_INFLIGHT = "generation_inflight"


def _finite_number(x):
    return type(x) in (int, float) and math.isfinite(x)


def snapshot_quote_balances(payload, quote_usd, *, generation, now_host, max_age_s, margin_s):
    """(quote balance in USD, native balance) from the datagram's wallet snapshot, or None: the
    BSC lane's early_quote_balances contract for a 4663 shared signal (speed fix 1). Speculative
    sizing only, the funded gate still reads live; it never writes `_gate_balance_cache`. A snapshot
    read at or before the wallet's last spend (+ margin), older than `max_age_s`, from the future,
    or with no WETH is no answer."""
    snap = payload.get("snapshot")
    if (not isinstance(snap, dict) or not _finite_number(generation)
            or not _finite_number(snap.get("read_at")) or not _finite_number(quote_usd)
            or quote_usd <= 0):
        return None
    read_at = snap["read_at"]
    weth, native = snap.get("weth_wei"), snap.get("native_wei")
    if (read_at <= generation + margin_s or not 0 <= now_host - read_at <= max_age_s
            or type(weth) is not int or weth <= 0 or type(native) is not int or native < 0):
        return None
    return weth / 10 ** 18 * quote_usd, native / 10 ** 18


SHARED_CACHE_LABEL = "rh_fast_cache_shared"
# The only questions a nominal-clip verdict answers. Anything else (the quote-bound path's
# bought_amount_atomic, Ink's pinned block_tag, a future exact input) goes to the funded check
# (Gamma MEDIUM on 13eaf8cea).
SHARED_CACHE_KWARGS = frozenset({"usd", "deadline", "timings"})
SHARED_CACHE_KINDS = frozenset({"v4_shared", "split_shared"})   # never pons or v3 (no lane verdict)


def shared_cache_refusal(verdict, *, usd, verdict_usd, ttl_s, now):
    """None when the lane's verdict tuple may stand in for the funded token safety at `usd`, else
    why not. Only a pass, 0..ttl_s old, at a clip from a quarter of the verdict's own up to it, and
    not a curve verdict (a curve's exit quote moves with every buy) is reused."""
    if verdict is None:
        return "absent"
    if not verdict[0]:
        return "refused"
    if not 0 <= now - verdict[2] <= ttl_s:
        return "stale"
    if not _finite_number(usd):
        return "no_usd"
    if usd > verdict_usd:
        return "clip_above_verdict"
    if usd < verdict_usd / 4:
        # Alpha 2026-10-14 (Gamma's bound): a fixed per-swap or per-transfer cost the verdict read
        # as ~1% at its own clip reads ~4x+ larger here; the funded check judges this clip itself.
        return "clip_below_quarter"
    if "launchpad_curve" in str(verdict[1] or ""):
        return "curve_verdict"
    return None


def cache_first_safety(funded, cache, *, now_fn):
    """`funded` (the chain's token safety), answered by `cache`'s verdict when the call asks only
    SHARED_CACHE_KWARGS and shared_cache_refusal allows, else called as is. One atomic verdict read decides; the funded signature is kept
    (functools.wraps), so the leg detects and passes the same keyword arguments."""
    import functools

    @functools.wraps(funded)
    def safety(mint, *args, **kwargs):
        if args or not SHARED_CACHE_KWARGS.issuperset(kwargs):
            return funded(mint, *args, **kwargs)
        v = cache.verdict(mint)
        if shared_cache_refusal(v, usd=kwargs.get("usd"), verdict_usd=cache.usd,
                                ttl_s=cache.ttl_s, now=now_fn()) is None:
            return True, "%s: %s" % (SHARED_CACHE_LABEL, v[1] or "unknown")
        return funded(mint, *args, **kwargs)
    return safety


def lane_generation_fn(lock_key, chain, *, lock_dir=None):
    """The wallet's generation as the lane reads it: the later of the last serialized execution and
    the last broadcast on the chain from ANY path; None once any stamp has failed in this process
    (Gamma MEDIUM: an unadvanced generation would make a stale snapshot read as fresh), and
    GENERATION_INFLIGHT while any process has an intent record open on the chain (EVM-08)."""
    from sniper.execution import exec_lock

    def generation():
        if exec_lock.generation_fault():
            return None
        # Intents BEFORE the stamps: a completion stamps .gen and then unlinks its record, so an
        # empty directory read first guarantees the .gen read after it includes that completion.
        pending = exec_lock.intents_outstanding(chain, lock_dir=lock_dir)
        if pending is None:
            return None
        if pending:
            return GENERATION_INFLIGHT
        own = exec_lock.exec_generation(lock_key, lock_dir=lock_dir)
        sent = exec_lock.exec_generation(exec_lock.chain_generation_key(chain), lock_dir=lock_dir)
        if own is None or sent is None:          # unreadable: the read latched the fault
            return None
        return max(own, sent)
    return generation


class Seen:
    """Each source buy is handled once: by tx hash and by (wallet, permit nonce)."""

    def __init__(self):
        self._keys = collections.OrderedDict()

    def first(self, payload):
        keys = (("tx", payload["tx_hash"]), ("permit", payload["wallet"], payload["permit_nonce"]))
        fresh = not any(k in self._keys for k in keys)
        for k in keys:
            self._keys[k] = True
        while len(self._keys) > _SEEN_MAX:
            self._keys.popitem(last=False)
        return fresh


class LaneEvent:
    """The event the funded hook judges for one lane signal (Alpha 2026-10-01: the lane calls the
    SAME gate functions today's funded EVM path calls). It has OnchainEv's shape, the on-chain lane's
    event for the same buy, built from the datagram alone: the watcher's size and price basis (what
    the wallet paid, over what it received), the source's post-trade facts on its pool for the chase
    gates, and the coin's market cap at the tape's post-trade price, which the hook reads as the
    event's own mcap (as it does a J7 event's). `no_revival`: today's lane for the same buy owns the
    revival watch, so a lane refusal never arms a second one."""
    side = "buy"
    signal_source = "rh_fast_lane"
    delivery_lane = "rh_fast_lane"
    ticker = None
    clock_domain = "observed_real_utc"
    source_timestamp_trust = "observed_detection"
    no_revival = True

    def __init__(self, payload, *, decimals, supply, chain="4663", delivery_lane="rh_fast_lane"):
        from sniper.services.memecoin_evm_post_trade import EvmChaseReference
        self.mint = str(payload["mint"]).lower()
        self.receipt_clock = freeze_receipt_clock(payload.get("receipt_clock"))
        self.network_id = chain = str(chain)
        self.delivery_lane = delivery_lane
        self.source_user_id = payload.get("source_user_id")
        self.source_handle = payload.get("handle")
        proof = payload.get("source_fill")
        self.source_wallet = payload.get("wallet")
        self.source_block_number = payload.get("seq")
        self.shared_evm_route = route_kind(payload) in SHARED_ROUTE_KINDS
        # Direct fill provenance already requires the wallet's bound receipt payment.
        # Relay fill economics alone cannot prove that the beneficiary initiated the buy.
        self.source_initiation = (proof.get('source_initiation') or dict(proof, schema_version=1,
            proof_version=proof.get("receipt_proof_version"), beneficiary=proof.get("wallet"),
            payer=proof.get("wallet") if proof.get("receipt_proof_version") == "direct-wallet-paid-v1" else None)
            if isinstance(proof, dict) else lane_initiation(payload))
        if not isinstance(proof, dict) and self.source_initiation is not None:
            # A deposit-proved (receipt-less) buy: its refused row must not make the on-chain lane
            # skip the same buy (memecoin_onchain_lane.already_attempted_from_another_lane).
            self.delivery_lane = DEPOSIT_DELIVERY_LANE
        received = Fraction(int(proof["acquired_atomic"] if proof else payload["src_out"]),
                            10 ** int(proof["token_decimals"] if proof else decimals))
        self.their_size_usd = float(proof["paid_usd"] if proof else payload["paid_usd"])
        self.their_price = float(Fraction(str(self.their_size_usd)) / received)
        if route_kind(payload) in SHARED_ROUTE_KINDS:
            self.their_market_cap = None  # the shared hook resolves mcap with its usual evidence
            # The receipt's pool ratio uses gross pool input/output. Wallet-net USD price can
            # include taxes, route fees or another payment currency; their product has no proven
            # units. Keep the verified wallet average until a quote-currency binding exists.
            post = {"reason": "wallet_net_post_units_unproven"}
            hint = _shared_hint_pool(payload, proof, chain)
            if hint is not None:
                post["hint_pools" if isinstance(hint, tuple) else "hint_pool"] = hint
            self.evm_chase_reference = EvmChaseReference(chain, post=post)
        else:
            self._v4_price(payload, supply, EvmChaseReference)
        self.implied_quote_reserve_usd = self.implied_quote_leg = None
        self.fomo_event_id = "rhfast:%s:%s:%s" % (chain, str(payload["tx_hash"]).lower(), self.mint)
        self.created_at = self.received_at = self.received_at_normalized = payload.get("arrival_utc")
        self.source_evidence = {
            "their_size_usd": self.their_size_usd, "source_user_id": self.source_user_id,
            "network_id": chain, "mint": self.mint, "signature": str(payload["tx_hash"]).lower(),
            "source_timestamp_trust": "observed_detection",
            "valuation_basis": "wallet_net_receipt" if proof else "lane_paid_usd",
            "source_fill": proof, "source_initiation": self.source_initiation}
        if payload.get("preconf") is True:
            # Base 8453 (Alpha ruling 2026-10-14): a Flashblocks-preconfirmed source, graded apart.
            self.source_evidence["source_observation_state"] = "preconfirmed"

    def _v4_price(self, payload, supply, reference_type):
        eth_usd = Fraction(str(payload["eth_usd"]))
        if payload.get("post_wei_num") and payload.get("post_wei_den"):   # routes rev 3: every kind
            post = Fraction(int(payload["post_wei_num"]), int(payload["post_wei_den"]))
        else:                                                   # an older native datagram
            post = Fraction(1 << 192, int(payload["post_sqrt"]) ** 2)  # wei per coin atomic, after
        self.their_market_cap = float(int(supply) * post / 10 ** 18 * eth_usd)
        proof = payload.get("source_fill")
        if proof:
            post_usd = post * 10**int(proof["token_decimals"]) / 10**18 * eth_usd
            ratio = post_usd / Fraction(str(self.their_price))
        else:
            ratio = post / Fraction(int(payload["src_in_wei"]), int(payload["src_out"]))
        self.evm_chase_reference = reference_type(self.network_id, post={
            "ratio": float(ratio), "pool": str(payload.get("pool") or "").lower(), "venue": "v4"})


def _shared_hint_pool(payload, proof, chain):
    """Receipt-bound V4 entry hints; split candidates carry no marginal source price."""
    from sniper.settings import memecoin as cfg
    if str(chain) not in cfg.memecoin_evm_lane_shared_hint_chains():
        return None
    if route_kind(payload) == "split_shared":
        verified = validate_source_fill(proof, payload)
        post = (verified or {}).get("source_post") or {}
        if post.get("reason") == "split_route" and post.get("pools"):
            return tuple(post["pools"])
        return None
    if route_kind(payload) != "v4_shared" or not isinstance(proof, dict):
        return None
    post = proof.get("source_post")
    pool = str(payload.get("pool") or "").lower()
    if (not isinstance(post, dict) or post.get("venue") != "v4"
            or str(post.get("pool") or "").lower() != pool):
        return None
    from sniper.execution.evm_v4_quote import _is_v4_pool_id
    return pool if _is_v4_pool_id(pool) else None


def default_hook_factory(contexts, stats):
    """The runner's funded entry hook, built as memecoin_runner_loop builds it, over the lane's
    contexts and the coin's prefetched market stats. The blind-chain mcap lookup is a network read
    and the lane's event always carries its mcap, so the lane gives it none."""
    from sniper.services.memecoin_real_runner import make_real_hook
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now_iso

    def stats_fn(mint, network_id=None):
        return dict(stats)
    return make_real_hook(None, contexts, now_fn=real_utc_now_iso,
                          trade_size=cfg.MEMECOIN_TRADE_SIZE_USD,
                          max_age_s=cfg.MEMECOIN_REAL_ENTRY_MAX_AGE_S, stats_fn=stats_fn,
                          blind_mcap_fn=lambda chain, mint: None)


@contextlib.contextmanager
def refusal_pages_off():
    """Today's lane pages the same buy's refusal, so the lane's own stays silent. A fill or a failed
    send still pages: the hook's rule for its retry lane, whose switch this is."""
    from sniper.services import memecoin_real_runner as runner
    was = getattr(runner._entry_alert_suppress, "on", False)
    runner._entry_alert_suppress.on = True
    try:
        yield
    finally:
        runner._entry_alert_suppress.on = was


def hook_reason(res):
    res = res or {}
    return "hook:%s:%s" % (res.get("status") or "none", str(res.get("reason") or "")[:80])


def state_class(payload):
    """The decide's state behind a send: `exact` (the source's own tape swap), `fresh` (a pre-state
    inside the decide's lag bound), None otherwise (shared routes, unknown)."""
    return {"tape_post": "exact", "pre": "fresh"}.get((payload or {}).get("state_kind"))


def _never_execute(*_a, **_k):
    raise AssertionError("the shadow hook reached an executor")


class _LaneChecks:
    """The in-memory checks both modes run before the funded hook. Nothing here reads, writes,
    reserves or signs (spec §4, Alpha's invariants 2 and 4); every refusal is a named fallback."""

    # The chain's own pieces (the Base 8453 lane overrides them; plan 2026-10-14 part A).
    CHAIN_ID = 4663
    DELIVERY_LANE = "rh_fast_lane"
    SAFETY_LABEL = "rh_fast_safety_cache"
    EXACT_STATE_KIND = "tape_post"          # the decide's post-trade tape state (the pre-buy's: its own)

    def _lane_event(self, payload, **kw):
        return LaneEvent(payload, chain=str(self.CHAIN_ID), delivery_lane=self.DELIVERY_LANE, **kw)

    def _precheck(self, payload, rec, host_now, safety_reason, stats, float_short_ok=False):
        rec["route_kind"] = route_kind(payload)
        from sniper.services.memecoin_wallet_caps import entry_cap_usd
        ceiling = entry_cap_usd(payload.get("source_user_id"), str(self.CHAIN_ID),
                                existing_cap=self.clip_usd)
        clip_wei = clip_wei_for(ceiling, payload.get("eth_usd"))
        rec["clip_wei"] = clip_wei
        if clip_wei is None:
            return "no_eth_usd"
        if self.require_exact_state and payload.get("state_kind") != self.EXACT_STATE_KIND:
            return "estimated_state"
        try:
            tx, min_out = lane_buy_tx(payload, clip_wei, int(host_now) + _lane_deadline_s(), self.taker,
                                      chain=str(self.CHAIN_ID))
            rec.update(min_out=min_out, tx_bytes=(len(tx["data"]) - 2) // 2)
        except (ValueError, ZeroDivisionError, KeyError, TypeError):
            return "build_refused"
        reason = inputs_reason(
            payload, generation=self.generation_fn(), now_host=host_now, clip_wei=clip_wei,
            gas_limit=int(tx["gas"]), low_wei=self.low_wei,
            max_snapshot_age_s=self.max_snapshot_age_s, eth_usd_max_age_s=self.eth_usd_max_age_s,
            generation_margin_s=self.generation_margin_s)
        if reason == "float_short" and float_short_ok:
            # runner_shadow only (Alpha 2026-10-02): the float is the live lane's resource, not a
            # gate. The shadow notes it and judges the signal anyway, so an unfunded float never
            # blinds its hook verdicts and staleness checks. The live lane passes no flag.
            rec["float_short"] = 1
            reason = None
        if reason is not None:
            return reason
        if safety_reason is not None:
            return safety_reason
        if not self.facts_fn(payload["mint"]):
            return "no_mcap"
        if not stats:
            return "no_market_stats"
        return self._source_trade_reason(payload, rec)

    def _source_trade_reason(self, payload, rec):
        """None when the datagram carries the source's own trade the event is priced from."""
        if not all(payload.get(k) for k in ("paid_usd", "post_sqrt", "src_in_wei", "src_out")):
            return "no_source_trade"
        if rec["route_kind"] != "v4_native" and not (payload.get("post_wei_num")
                                                     and payload.get("post_wei_den")):
            return "no_source_trade"                 # v1's post formula is native-only
        return None

    def _lane_context(self, payload, execute_fn):
        """The chain's context the hook runs on: today's, with the wallet's balances from the
        datagram's snapshot, the safety cache's verdict (fresh and passing, or the precheck fell
        back) and the lane's executor. Nothing in it reads the network."""
        snap, eth_usd = payload["snapshot"], float(payload["eth_usd"])
        quote_usd = int(snap["weth_wei"]) / 10 ** 18 * eth_usd
        native = int(snap["native_wei"]) / 10 ** 18

        def safety(mint, usd=None):
            return self.cache.cached_answer(mint, self.SAFETY_LABEL)
        return {**self.contexts, str(self.CHAIN_ID): dict(
            self.ctx, usdc_fn=lambda: quote_usd, native_fn=lambda: native, token_safety_fn=safety,
            execute_fn=execute_fn, warm_quote_price_fn=None, head_fn=None)}


class RunnerShadow(_LaneChecks):
    """runner_shadow: what the live lane would decide, with no side effect. The in-memory checks,
    then every gate of the funded hook on a READ-ONLY ledger connection inside one transaction that
    is rolled back (the attempt trace refuses a caller-owned transaction, so no row is written),
    stopped before the leg exactly as a J7 shadow decision is; then a non-blocking try of the wallet
    lock. After each hook verdict a background thread asks DexScreener once more and records whether
    fresh stats would have changed it (Alpha 2026-10-01: the staleness cost, read before live)."""

    def __init__(self, *, lock_key, lock_dir, taker, now_fn, clip_usd=50.0, low_wei=0,
                 generation_fn=lambda: 0.0, host_clock=time.time, max_snapshot_age_s=30.0,
                 eth_usd_max_age_s=120.0, generation_margin_s=0.0, require_exact_state=True,
                 facts_fn=lambda mint: None, ctx=None, contexts=None, ro_connect=None,
                 hook_factory=None, fresh_stats_fn=None, check_sink=None):
        self.lock_key, self.lock_dir, self.taker, self.now_fn = lock_key, lock_dir, taker, now_fn
        self.clip_usd, self.low_wei, self.generation_fn = clip_usd, low_wei, generation_fn
        self.host_clock = host_clock
        self.max_snapshot_age_s, self.eth_usd_max_age_s = max_snapshot_age_s, eth_usd_max_age_s
        self.generation_margin_s, self.require_exact_state = generation_margin_s, require_exact_state
        self.facts_fn, self.ctx, self.contexts = facts_fn, dict(ctx or {}), dict(contexts or {})
        self.ro_connect, self.hook_factory = ro_connect, hook_factory or default_hook_factory
        self.fresh_stats_fn, self.check_sink = fresh_stats_fn, check_sink
        self._ro = None
        self._checks = []

    def handle(self, payload, *, recv_epoch, safety_reason=None, stats=None):
        """`recv_epoch` and the payload's `sent_epoch` are host-clock; `now_fn` is the corrected
        clock the ledger's decision stamps use (the busy window joins them). `safety_reason` is the
        safety cache's answer for the mint (None = a fresh passing verdict); `stats` is the cache's
        (market stats, fetched at), or None."""
        from sniper.execution.exec_lock import wallet_exec_lock
        rec = {"tx_hash": payload["tx_hash"], "mint": payload["mint"], "pool": payload.get("pool"),
               "recv_epoch": recv_epoch, "sent_epoch": payload["sent_epoch"],
               "handoff_ms": (recv_epoch - float(payload["sent_epoch"])) * 1000.0,
               "clip_wei": None, "min_out": None, "tx_bytes": None}
        start, t0, c0 = self.now_fn(), time.perf_counter(), time.thread_time()
        host_now = self.host_clock()
        cached = stats[0] if stats else None
        reason = self._precheck(payload, rec, host_now, safety_reason, cached, float_short_ok=True)
        verdict = None
        if reason is None:
            if self._ro is None:
                self._ro = self.ro_connect()
            verdict = self.verdict(self._ro, payload, cached)
            reason = None if verdict == "pass" else verdict
        if reason is None:
            with wallet_exec_lock(self.lock_key, lock_dir=self.lock_dir, blocking=False) as held:
                reason = "built" if held else "lock_busy"
        lock_busy = int(reason == "lock_busy")
        if rec.pop("float_short", None):
            reason = "float_short+" + reason        # the live lane would have stopped here
        rec.update(reason=reason, lock_busy=lock_busy, busy_start_epoch=start,
                   busy_end_epoch=self.now_fn(), busy_ms=(time.perf_counter() - t0) * 1000.0,
                   cpu_ms=(time.thread_time() - c0) * 1000.0)
        if verdict is not None and self.fresh_stats_fn is not None:
            self._start_fresh_check(payload, verdict, host_now - stats[1])
        return rec

    def verdict(self, conn, payload, stats, facts=None):
        """"pass" when every gate of the funded hook passes on `stats`, else hook:<status>:<why>."""
        from sniper.services.memecoin_real_runner import J7_SHADOW_LANE
        decimals, supply = facts or self.facts_fn(payload["mint"])
        ev = self._lane_event(payload, decimals=decimals, supply=supply)
        ev.delivery_lane = J7_SHADOW_LANE              # the hook stops before the leg
        hook = self.hook_factory(self._lane_context(payload, _never_execute), stats)
        conn.execute("BEGIN")
        try:
            with refusal_pages_off():
                res = hook.raw(conn, ev)
        finally:
            conn.rollback()
        return "pass" if (res or {}).get("status") == "shadow" else hook_reason(res)

    def _start_fresh_check(self, payload, cached_verdict, cached_age_s):
        def check():
            row = {"source_tx": payload["tx_hash"], "mint": payload["mint"],
                   "cached_age_s": cached_age_s, "cached_verdict": cached_verdict,
                   "fresh_verdict": None, "differ": None}
            try:
                fresh = self.fresh_stats_fn(payload["mint"])
                if isinstance(fresh, dict) and fresh:
                    conn = self.ro_connect()
                    try:
                        row["fresh_verdict"] = self.verdict(conn, payload, fresh)
                    finally:
                        conn.close()
                    row["differ"] = int(row["fresh_verdict"] != cached_verdict)
            except Exception:
                logger.exception("rh fast lane: fresh-stats check failed (measurement only)")
            if self.check_sink is not None:
                try:
                    self.check_sink(row)
                except Exception:
                    logger.exception("rh fast lane: fresh-stats check record failed")
        thread = threading.Thread(target=check, name="rh-fast-stats-check", daemon=True)
        self._checks.append(thread)
        thread.start()

    def join_checks(self, timeout=5.0):
        for thread in self._checks:
            thread.join(timeout)


SEQUENCER_URL = "https://sequencer.mainnet.chain.robinhood.com"
# The sequencer's own refusals that prove a transaction was NOT admitted (it answers only after
# sequencing; a precheck error comes back at once). "nonce too low/high" are NOT here: another copy
# of the same bytes, or a held too-high nonce, can still land, so those settle by receipt.
_PROVEN_REJECT = ("insufficient funds", "fee cap", "max fee per gas less than block base fee",
                  "intrinsic gas too low", "invalid sender", "invalid chain id",
                  "transaction type not supported", "exceeds block gas limit", "oversized data")


def classify_send_reply(reply):
    """("accepted", tx hash) | ("rejected", message) | ("nonce", message) | ("ambiguous", why) for
    one sequencer reply (spec 2026-10-01 fast lane; the mapping Alpha accepted 2026-10-01)."""
    if not isinstance(reply, dict):
        return "ambiguous", str(reply)[:120]
    result = reply.get("result")
    if isinstance(result, str) and result.startswith("0x") and len(result) == 66:
        return "accepted", result
    error = reply.get("error")
    message = " ".join(str(error.get("message") if isinstance(error, dict) else error).split())
    lowered = message.lower()
    if "nonce too low" in lowered or "nonce too high" in lowered:
        return "nonce", message[:160]
    if "already known" in lowered or "known transaction" in lowered:
        return "ambiguous", message[:160]
    if any(marker in lowered for marker in _PROVEN_REJECT):
        return "rejected", message[:160]
    return "ambiguous", (message or "no result")[:160]


class SequencerSender:
    """One warm keep-alive HTTPS connection to the Robinhood sequencer (spec §2.6). `send` writes
    the signed bytes and returns its classified reply; the TLS handshake is done ahead by `warm`.
    A transport failure or timeout is ambiguous, and the connection is rebuilt next time. Every
    send stamps the chain's broadcast generation afterwards, as eth_send_raw does."""

    def __init__(self, url=SEQUENCER_URL, *, timeout_s=3.0, connect=None, headers=None):
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        self.host, self.path = parsed.hostname, parsed.path or "/"
        self.timeout_s = timeout_s
        self.headers = dict(headers or {})
        self._connect = connect
        self._conn = None
        self._lock = threading.Lock()

    def _open(self):
        if self._connect is not None:
            return self._connect()
        import http.client
        conn = http.client.HTTPSConnection(self.host, timeout=self.timeout_s)
        conn.connect()
        return conn

    def _post(self, body):
        if self._conn is None:
            self._conn = self._open()
        self._conn.request("POST", self.path, body=body,
                           headers={"Content-Type": "application/json", "Connection": "keep-alive",
                                    **self.headers})
        response = self._conn.getresponse()
        return json.loads(response.read() or b"{}")

    def warm(self):
        """Open (or keep) the connection: one cheap request the sequencer refuses at once."""
        with self._lock:
            try:
                self._post(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "eth_chainId",
                                       "params": []}))
                return True
            except Exception:
                self._close()
                return False

    def send(self, raw_hex):
        from sniper.execution import exec_lock
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
                           "params": [raw_hex]})
        with self._lock:
            try:
                intent = exec_lock.intent_send(raw_hex, "rpc")
            except exec_lock.IntentUnwritable as exc:
                return "rejected", "intent_unwritable: %s" % exc       # nothing was sent
            try:
                reply = self._post(body)
            except Exception as exc:
                self._close()
                reply = "transport: %s" % type(exc).__name__
            finally:
                exec_lock.stamp_send(raw_hex, intent=intent)
        return classify_send_reply(reply)

    def _close(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None


def signed_value(raw_hex):
    """The native value (wei) a signed legacy, type-1 or type-2 transaction pays."""
    import rlp
    raw = bytes.fromhex(str(raw_hex)[2:] if str(raw_hex).startswith("0x") else str(raw_hex))
    if raw and raw[0] >= 0xC0:
        return int.from_bytes(rlp.decode(raw)[4], "big")
    if raw and raw[0] == 2:
        return int.from_bytes(rlp.decode(raw[1:])[6], "big")
    if raw and raw[0] == 1:
        return int.from_bytes(rlp.decode(raw[1:])[5], "big")
    raise ValueError("not a signed EVM transaction")


_WARM_MINT = "0x" + "fa57" * 10                   # a coin no ledger row names


def warm_hook(shadow, *, log):
    """One shadow verdict at lane-thread start, off any signal (Alpha 2026-10-01: the first hook
    call in a process measured 376 ms cold, lazy imports and first page reads; warm, p50 12 ms). It
    runs the funded hook's whole gate stack read-only and rolled back, for a synthetic buy by an
    armed 4663 wallet (with none armed the gates stop at the roster) of a coin no ledger row names.
    Returns (ms, verdict), or (None, None) when anything fails: it never raises into the lane."""
    from sniper.persistence import db_memecoin as m
    from sniper.util.clock import real_utc_now_iso
    try:
        conn = shadow.ro_connect()
        try:
            chain = str(getattr(shadow, "CHAIN_ID", 4663))
            armed = sorted(m.real_approved_user_ids(conn, network_id=chain) or ())
            now = time.time()
            payload = dict(tx_hash="0x" + "0" * 64, mint=_WARM_MINT, pool="0x" + "0" * 64,
                           wallet="0x" + "fa" * 20, seq=1,
                           source_user_id=armed[0] if armed else "rh-fast-warm", handle="warm",
                           arrival_utc=real_utc_now_iso(), sent_epoch=now, eth_usd=2500.0,
                           eth_usd_at=now, post_sqrt=874745060896749537239184449426278,
                           src_in_wei=25854436920000000, src_out=3182688391454788035426447,
                           paid_usd=1500.0, snapshot=dict(weth_wei=0, native_wei=0))
            # Synthetic receipt identity for the existing read-only, rolled-back warm verdict.
            payload["source_fill"] = dict(receipt_proof_version="direct-wallet-paid-v1",
                network_id=chain, tx_hash=payload["tx_hash"], block_number=1,
                block_hash="0x" + "00" * 32, wallet=payload["wallet"], mint=_WARM_MINT,
                source_user_id=payload["source_user_id"], token_decimals=18,
                acquired_atomic=str(payload["src_out"]), paid_usd=payload["paid_usd"])
            stats = dict(price=2.05e-05, volume_m5=50000.0, volume_h1=200000.0,
                         txns_m5_buys=300, txns_m5_sells=100, liquidity=30000.0,
                         pair_created_at=(now - 3 * 3600) * 1000.0)
            t0 = time.perf_counter()
            verdict = shadow.verdict(conn, payload, stats, facts=(18, 10 ** 27))
            ms = (time.perf_counter() - t0) * 1000.0
        finally:
            conn.close()
    except Exception as exc:
        logger.exception("rh fast lane: hook warm failed")
        log("[rh-fast-lane] hook warm failed (%s): the first signal pays the cold call"
            % type(exc).__name__)
        return None, None
    log("[rh-fast-lane] hook warm: %.0f ms (%s)" % (ms, verdict))
    return ms, verdict


_INLINE = threading.local()          # the per-signal deadline of an inline safety check


def inline_deadline(deadline):
    """The inline check's deadline scope, the funded path's shape (memecoin_evm_leg's
    safety_deadline): the safety builder enters it on every worker thread it starts, so a read on
    any of them sees the same deadline."""
    import contextlib

    @contextlib.contextmanager
    def scope():
        prior = getattr(_INLINE, "deadline", None)
        effective = min(prior, deadline) if prior is not None else deadline
        _INLINE.deadline = effective
        try:
            yield lambda: time.monotonic() >= effective
        finally:
            _INLINE.deadline = prior
    return scope()


def lane_inline_http(url, paced, base=None, gate=None):
    """The inline safety check's http seam (Alpha 2026-10-02: a cache miss is checked inline on the
    lane's own safety host, never the funded pool; both lanes): that host only, not queued behind the background
    reads (a signal is waiting), inside the signal's whole-check deadline, and silent while the
    host's budget is paused after a 429 (a 429 here pauses it as well)."""
    import urllib.error

    def http(method, target, headers=None, body=None, timeout=10):
        if target != url:
            raise RuntimeError("fast lane: a read off the lane's own safety host was refused")
        if gate is not None and not gate():
            raise InlineHostDead("inline safety host chain identity not admitted")       # nothing is sent to a host whose chain id is unproven or wrong, whoever calls the closure
        if paced.dead():
            raise InlineHostDead("inline safety host marked dead after transport failures")
        if paced.quota_blocks():
            raise InlineHostDead("inline safety host monthly quota spent (probe pending)")
        if paced.paused():
            raise ResearchPaused("lane host paused after a 429")
        deadline = getattr(_INLINE, "deadline", None)
        cut = False                       # the check's own deadline shortened this read
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                from sniper.execution.evm_token_safety import EvmSafetyDeadlineExceeded
                raise EvmSafetyDeadlineExceeded()
            cut = left < timeout
            timeout = min(timeout, left)
        if base is None:
            from sniper.execution.evm_wallet import _http as call
        else:
            call = base
        with paced._lock:                # every request sent (the inline spend; Gamma LOW 2026-10-02)
            paced.counts["calls"] += 1
        try:
            status, answer, hdrs = call(method, target, headers=headers, body=body, timeout=timeout)
        except urllib.error.HTTPError as exc:
            paced.transport(True)
            if exc.code == 429:
                paced.pause()
            raise
        except Exception as exc:
            if not is_transport_failure(exc):
                raise
            detail = "%s: %s" % (type(exc).__name__, str(exc).replace(url, "<inline host>"))
            if cut and time.monotonic() >= deadline:
                # Gamma MED on 84c020a34: the check reads in parallel, so one slow check would cut
                # several reads at once and mark an alive host dead. Not the host's failure.
                logger.warning("fast lane: inline safety read cut by the check's deadline: %s", detail)
                from sniper.execution.evm_token_safety import EvmSafetyDeadlineExceeded
                raise EvmSafetyDeadlineExceeded() from exc
            run = paced.transport(False)      # never the URL in the log: a key rides in its path
            logger.warning("fast lane: inline safety host transport failure (%d in a row%s): %s",
                           run, ", host dead for %ds" % paced.DEAD_S if paced.dead() else "", detail)
            raise
        paced.transport(True)
        paced.note_answer(status, answer)
        if status == 429:
            paced.pause()
        return status, answer, hdrs
    return http


def lane_warm_http(url, paced, base, gap_s, gate=None):
    """The inline warmer's seam: the inline seam (host check, `paced`'s 429 pause and counters), ONE
    read at a time, `gap_s` apart. Live 2026-10-14T20:11:18Z the unpaced warmer sent 68 reads in
    ~2 s (the slot scan's 12-wide tail) and drew a 429; the Chainstack endpoint holds about 49 reads
    a second (probe 20:14Z: 40/s and a 40-read burst clean, 50 drew 429; the BSC 56 lane, same
    account, drew none). The serial pacer wraps the whole inline seam, so a read queued behind a 429 meets `paced`'s
    pause before it is sent (Gamma LOW on ade03bb98). It never pauses itself (pause_s 0)."""
    return PacedHttp(lane_inline_http(url, paced, base=base, gate=gate), min_gap_s=gap_s, pause_s=0.0)


class InlineHost:
    """One inline safety host of the rotation: its OWN pacer (429 pause, dead mark, quota latch and probe) and its own safety / facts / warm seams. `label` is "host i of n": nothing here ever names the URL."""

    def __init__(self, label, paced, *, safety_fn, facts_fn, warm_safety_fn=None, warm_facts_fn=None, identity_probe=None):
        self.label, self.paced = label, paced
        self.safety_fn, self.facts_fn = safety_fn, facts_fn
        self.warm_safety_fn, self.warm_facts_fn = warm_safety_fn, warm_facts_fn
        self.identity_probe = identity_probe        # () -> (status, answer): ONE eth_chainId, background only
        self.admission, self.admitted_at, self.chain_seen = "pending", 0.0, None       # pending | admitted | refused (answered another chain)

    def healthy(self):
        p = self.paced
        return self.admission == "admitted" and not (p.dead() or p.quota_blocks() or p.paused())

    def state(self):
        p = self.paced
        if self.admission == "refused":
            return "wrong chain (refused)"
        if self.admission != "admitted":
            return "identity pending"
        return "quota spent" if p.quota_blocks() else "dead" if p.dead() else "429 pause" if p.paused() else "healthy"


class _SumCounts:
    """counts[...] summed over the rotation's hosts: the lane and the heartbeat read one host-shaped counter."""

    def __init__(self, hosts):
        self._hosts = hosts

    def __getitem__(self, key):
        return sum(h.paced.counts[key] for h in self._hosts)

    def get(self, key, default=0):
        try:
            return self[key]
        except KeyError:
            return default


class InlineHostSet:
    """Ordered inline safety hosts with rotation (operator "2 with rotation", 2026-10-29: the single Chainstack host spent its monthly quota and every 4663 fast entry was blocked).
    pick() returns the FIRST healthy host in list order (not dead, not quota-spent, not 429-paused): when the active host turns unhealthy the very next check goes to the next healthy one, and a
    recovered earlier host (its own 15-minute quota probe saw a real block number, or its pause ended) takes over again. The check that met the quota answer is NOT retried and nothing is added on the
    happy path: pick() is a few attribute reads, no lock, no I/O. Each rotation, all-hosts-unavailable and the return are logged and alerted (off the calling thread), never with a URL.
    It also presents the PacedHttp read surface the lane, the warmer and the heartbeat use (counts summed, dead() = all dead, paused() = none healthy, quota_spent() = all spent)."""

    ADMIT_RETRY_S, ADMIT_RECHECK_S = 5.0, 600.0
    ADMIT_PROOF_TTL_S = 1800.0  # background-only expiry; transient recheck errors get bounded grace

    def __init__(self, hosts, *, log=print, alert_fn=None, chain_hex="0x1237", clock=time.monotonic):
        if not hosts:
            raise ValueError("an inline host set needs at least one host")
        self.hosts, self.log, self.chain_hex, self.clock = list(hosts), log, str(chain_hex).lower(), clock
        self._settled = False                     # no rotation alert until the first admission pass has run (startup is not an outage)
        if alert_fn is None:
            def alert_fn(text):
                from sniper.services.memecoin_alerts import send_alert
                send_alert("rh_fast_lane", text)
        self.alert_fn = alert_fn
        self.counts = _SumCounts(self.hosts)
        self._lock = threading.Lock()
        self._cur = None
        self.rotations = collections.Counter()

    # ---- selection
    def _first_healthy(self):
        for h in self.hosts:
            if h.healthy():
                return h
        return None

    def pick(self):
        chosen = self._first_healthy()
        if chosen is not self._cur:
            self._note(chosen)
        return chosen

    def _note(self, chosen):
        with self._lock:
            prev = self._cur
            if chosen is prev:
                return
            self._cur = chosen
            n = len(self.hosts)
            if chosen is None:
                self.rotations["all_unavailable"] += 1
                text = ("4663 inline safety hosts: ALL %d unavailable (%s); inline checks fail closed and a cache miss falls back until one recovers"
                        % (n, ", ".join("%s %s" % (h.label, h.state()) for h in self.hosts)))
            elif prev is None:
                self.rotations["recovered"] += 1
                text = "4663 inline safety hosts: %s is back, inline checks resume" % chosen.label
            else:
                self.rotations["rotated"] += 1
                text = "4663 inline safety host rotated: %s (%s) -> %s" % (prev.label, prev.state(), chosen.label)
        self.log("[rh-fast-lane] " + text)
        if not self._settled:
            return
        def send():
            try:
                self.alert_fn(text)
            except Exception:
                logger.exception("rh fast lane: inline rotation alert not sent")
        threading.Thread(target=send, name="rh-fast-inline-rotation-alert", daemon=True).start()

    # ---- dispatch seams (the active host answers each call)
    def safety(self, mint, usd=None, timings=None):
        host = self.pick()
        if host is None:
            raise InlineHostDead("every inline safety host is unavailable (spent, paused or dead)")
        return host.safety_fn(mint, usd=usd, **({"timings": timings} if timings is not None else {}))

    def facts(self, mint):
        host = self.pick()
        if host is None:
            raise InlineHostDead("every inline safety host is unavailable (spent, paused or dead)")
        return host.facts_fn(mint)

    def warm_safety(self, mint, usd=None):
        host = self.pick()
        if host is None or host.warm_safety_fn is None:
            raise ResearchPaused("no healthy inline host for the warmer")
        return host.warm_safety_fn(mint, usd=usd)

    def warm_facts(self, mint):
        host = self.pick()
        if host is None or host.warm_facts_fn is None:
            raise ResearchPaused("no healthy inline host for the warmer")
        return host.warm_facts_fn(mint)

    # ---- identity admission (background thread only: the fill path never makes this call)
    def admit_pass(self):
        """One eth_chainId per host that is pending (or admitted longer than ADMIT_RECHECK_S ago), skipping hosts that are quota-spent, dead or paused. A host is ADMITTED only when it answers the
        lane's chain id; a valid different chain id REFUSES it for good (logged and alerted, named by provider domain, never used); an error, a transport failure or a malformed answer leaves it pending
        (retried next pass). A quota answer latches the host's quota state as any read does. pick() uses admitted, healthy hosts only, so until a host is admitted the set fails closed."""
        now = self.clock()
        for h in self.hosts:
            p = h.paced
            if h.admission == "admitted" and now - h.admitted_at > self.ADMIT_PROOF_TTL_S:
                h.admission = "pending"
                self.rotations["identity_expired"] += 1
                text = "4663 inline safety %s identity proof EXPIRED after 30 min; pending revalidation" % h.label
                self.log("[rh-fast-lane] " + text)
                threading.Thread(target=lambda t=text: self.alert_fn(t), name="rh-fast-inline-rotation-alert", daemon=True).start()
            if h.identity_probe is None or h.admission == "refused" or p.quota_blocks() or p.dead() or p.paused():
                continue
            if h.admission == "admitted" and now - h.admitted_at < self.ADMIT_RECHECK_S:
                continue
            with p._lock:  # background only; bind the proof to this quota/admission generation
                if p.quota_blocks():
                    continue
                generation = p.quota_gen
            try:
                status, answer = h.identity_probe()[:2]
            except Exception:
                logger.exception("rh fast lane: inline identity probe failed")
                continue
            p.note_answer(status, answer)
            result = answer.get("result") if isinstance(answer, dict) else None
            if status != 200 or not isinstance(result, str) or "error" in answer or not re.fullmatch(r"0x[0-9a-fA-F]+", result):
                continue
            with p._lock:  # never let a pre-quota answer publish after revocation/rejoin
                if generation != p.quota_gen or p.quota_blocks():
                    continue
                h.chain_seen = result.lower()
                matched = int(result, 16) == int(self.chain_hex, 16)
                if matched:
                    text = ("inline safety %s admitted: chain id %s" % (h.label, self.chain_hex)
                            if h.admission != "admitted" else None)
                    h.admission, h.admitted_at = "admitted", self.clock()
                else:
                    h.admission = "refused"
                    self.rotations["refused"] += 1
                    text = "4663 inline safety %s REFUSED: it answered chain id %s (expected %s); never used" % (h.label, h.chain_seen, self.chain_hex)
            if text is not None:
                self.log("[rh-fast-lane] " + text)
            if not matched:
                threading.Thread(target=lambda t=text: self.alert_fn(t), name="rh-fast-inline-rotation-alert", daemon=True).start()
        if not self._settled:
            self._settled = True
            self._cur = self._first_healthy()           # the starting host is not a rotation
            if self._cur is None:
                self.log("[rh-fast-lane] inline safety: no host admitted at the first pass; inline checks fail closed until one is")
                if all(h.admission == "refused" for h in self.hosts):
                    text = "4663 inline safety hosts: ALL %d REFUSED (wrong chain); inline checks fail closed" % len(self.hosts)
                    threading.Thread(target=lambda: self.alert_fn(text), name="rh-fast-inline-rotation-alert", daemon=True).start()
            return
        self.pick()

    def beat(self):
        """The heartbeat's view: the active host's 1-based index (None when none is healthy), n, each host's label and state, and the rotation counters. Provider domains only, never a URL."""
        first = self._first_healthy()
        return {"n": len(self.hosts), "active": None if first is None else self.hosts.index(first) + 1,
                "hosts": [{"label": h.label, "state": h.state()} for h in self.hosts], "rotations": dict(self.rotations)}

    # ---- the PacedHttp read surface
    def dead(self):
        return all(h.paced.dead() for h in self.hosts)

    def paused(self):
        return self._first_healthy() is None

    def quota_blocks(self):
        return all(h.paced.quota_blocks() for h in self.hosts)

    def quota_spent(self):
        return all(h.paced.quota_spent() for h in self.hosts)

    def quota_recover(self):
        """Background only: every spent host runs its own one-per-interval probe."""
        for h in self.hosts:
            if h.paced.quota_spent() and h.admission == "admitted":
                h.admission = "pending"  # revoke before recovery can clear the quota latch on another thread
            h.paced.quota_recover()


_STATUS = {"inline": None}      # the process's inline host set (build_lane), read by the runner heartbeat


def status():
    """The runner heartbeat's rh_fast_lane dict (inline host rotation: active host i of n, per-host state, rotation counters), or None while no inline host set exists in this process
    (the heartbeat then has no key). Provider-domain labels only, never a URL. Reads attributes; no I/O."""
    inline = _STATUS.get("inline")
    if inline is None:
        return None
    try:
        return {"inline_hosts": inline.beat()}
    except Exception:
        return None


def host_provider(url):
    """The registrable domain of an RPC URL ("alchemy.com", "p2pify.com"), the only part of it an alert or a log may name: the key rides in the path (or a subdomain), never here. "?" when unreadable."""
    from urllib.parse import urlsplit
    try:
        parts = [x for x in (urlsplit(str(url)).hostname or "").lower().split(".") if x]
    except ValueError:
        return "?"
    return ".".join(parts[-2:]) if len(parts) >= 2 else "?"


def resolve_inline_hosts(env, log):
    """(urls, names): the inline safety hosts to use, in order. MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URLS (comma list) wins over the single MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL (a list of one is
    the rollback). EACH URL passes safety_host_refusal on its own, so the funded custody host, the research keys and any other role's or funded-pool URL are still refused (a refused entry is logged by
    position, never by URL, and the others are kept); duplicates are dropped."""
    listed = str(env.get("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URLS", "") or "").strip()
    if listed:
        own = "MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URLS"
        parts = [x for x in listed.replace(",", " ").split() if x]
        check_env = {k: v for k, v in env.items() if k != "MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL"}      # the single knob is ignored when the list is set
    else:
        own = "MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL"
        raw = str(env.get(own, "") or "").strip()
        parts, check_env = ([raw] if raw else []), env
    urls, seen = [], set()
    for i, part in enumerate(parts, 1):
        refusal = safety_host_refusal(part, check_env, own_name=own)
        if refusal is not None:
            log("[rh-fast-lane] %s entry %d refused (%s): a cache miss falls back for it" % (own, i, refusal)
                if own.endswith("URLS") else "[rh-fast-lane] %s refused (%s): a cache miss falls back" % (own, refusal))
            continue
        key = _norm_url(part)
        if key in seen:
            log("[rh-fast-lane] %s entry %d is a duplicate: dropped" % (own, i))
            continue
        seen.add(key)
        urls.append(part)
    return urls, own


class _SafetyWaitExceeded(Exception):
    """The parallel safety read was still running at the signal's deadline."""


def _await_safety(future, deadline):
    """The parallel read's (ok, why), re-raising whatever the read itself raised (so the lane's
    labels match a sequential read's); _SafetyWaitExceeded when it is still running at `deadline`."""
    try:
        return future.result(timeout=max(0.0, deadline - time.monotonic()))
    except FutureTimeout:
        if future.done():
            raise                        # the read's own TimeoutError, labelled like any error
        raise _SafetyWaitExceeded() from None


def stamp_input_legs(rec):
    """Cut 4's input legs (inline_inputs_path_ms, inline_facts_ms, inline_stats_ms) into the row's
    execution evidence: at the safety step and again at the lane's finish, so a cold signal the hook
    refuses before safety keeps its measured legs (2026-10-14). Absent = that leg was cached or never
    read; never a 0. Measurement only: no gate reads them."""
    legs = {"inline_" + leg: rec[leg] for leg in ("facts_ms", "stats_ms") if leg in rec}
    if "inputs_ms" in rec:
        legs["inline_inputs_path_ms"] = rec["inputs_ms"]
    if legs:
        rec.setdefault("execution_evidence", {}).update(legs)


def receipt_refusal(payload, kind):
    """Why a datagram's receipt proof does not admit it, or None (the lane's checks before the hook,
    in this order). Pure; no provider or ledger work."""
    proof = validate_source_fill(payload.get("source_fill"), payload)
    if proof is None:
        return "source_fill_unproved"
    if (proof["receipt_proof_version"] != "direct-wallet-paid-v1"
            and not isinstance(proof.get("source_initiation"), dict)):
        # Beneficiary-only Relay economics do not prove initiation. An explicit initiation
        # retained here has already passed source_initiation_refusal's current proof flags
        # and exact receipt/datagram identity in validate_source_fill above. Keep unproved
        # envelopes out before an attempt can suppress the independently verified lane.
        return "relay_origin_unproved"
    post = proof.get("source_post") or {}
    if kind == 'split_shared':
        # Canonical validation already binds unique complete V4 pools and their combined
        # output to wallet-net acquisition within 90%. A split has no single pool or price.
        bound = (post.get('reason') == 'split_route' and post.get('venue') == 'v4'
                 and post.get('receipt_routes_complete') is True and bool(post.get('pools'))
                 and payload.get('pool') is None and payload.get('post_ratio') is None)
    elif kind == 'pons_shared':
        got = int(proof['acquired_atomic'])
        bound = (post.get('venue') == 'pons_v2'
                 and post.get('reason') == 'pons_post_state_unavailable'
                 and post.get('receipt_routes_complete') is True
                 and post.get('pool') == str(payload.get('pool') or '').lower()
                 and got <= int(post.get('pool_output_atomic', '0')) <= got * 10 // 9
                 and payload.get('post_ratio') is None)
    else:
        bound = not (post.get("reason") or post.get("pool") != str(payload.get("pool") or "").lower()
            or post.get("venue") != ("v3" if kind == "v3_shared" else "v4")
            or not post.get("sqrt_price_x96"))
    if not bound:
        return "source_post_unbound"
    if (str(payload.get("src_out")) != proof["acquired_atomic"]
            or payload.get("paid_usd") != proof["paid_usd"]):
        return "source_fill_mismatch"
    if proof.get("payment_basis") == "transaction_value_upper_bound":
        return "source_payment_unproved"
    if kind not in SHARED_ROUTE_KINDS and payload.get("eth_usd") is None:
        return "no_eth_usd"
    if kind == "v4_native" and not native_source_price_matches(payload, proof):
        return "source_price_mismatch"
    return None


class LiveLane(_LaneChecks):
    """MEMECOIN_RH_FAST_LANE=live (spec 2026-10-01 fast lane §2.5-§2.7, Alpha's invariants).

    BEFORE THE FUNDED HOOK, in memory only: the pilot's fill count and hourly cap, then the checks
    both modes share (the clip at the datagram's ETH price, the exact-state rule, the unsigned buy,
    the snapshot/price/float checks, the safety verdict, the coin's facts and its market stats).

    THE HOOK (Alpha 2026-10-01, Gamma's BLOCK on d6e567683): the runner's own funded entry hook,
    make_real_hook, judges a LaneEvent over the lane's context. Every gate today's 4663 entry
    passes runs exactly as it does there, by construction: roster and per-chain arming, the mcap
    ceiling and floor, staleness, the source-buy family, the chase gates, the volume family, sell
    share, coin age, the band; then the leg's kill switch, arming, quiet hours and admission, the
    reservation, and the safety verdict. Only then is the lane's executor called.

    THE EXECUTOR (`execute_reserved`), holding the per-(wallet, 4663) execution lock taken WITHOUT
    waiting: post_peak means fallback (no exact quote can confirm it), the clip (the approved size,
    capped at the pilot clip) with the datagram's min-out, the nonce through the shared floor, the
    signature, the write-ahead identity and the configured same-bytes submission. A proven refusal
    releases; shared ambiguity settles by receipt, while the legacy sender reoffers the same bytes.
    Nothing is ever re-signed. THE LOCK IS HELD THROUGH THE RECEIPT, at most `lock_receipt_cap_s` (Alpha on
    Gamma's MEDIUM: 4663 receipts p50 211 ms, p99 2.8 s), so a lane buy never lands inside another
    executor's balance-delta window; past the cap it is released with an alert and the receipt is
    awaited outside it. The fill is booked from its own receipt."""

    def __init__(self, *, ctx, conn, sender, fanout_fn, rpc_read, facts_fn, lock_key, lock_dir,
                 clip_usd, low_wei, generation_fn, host_clock, max_snapshot_age_s,
                 eth_usd_max_age_s, generation_margin_s, pilot_fills, max_sends_per_hour,
                 require_exact_state, lock_receipt_cap_s, contexts=None, hook_factory=None,
                 receipt_timeout_s=30.0, receipt_poll_s=0.25, sleep=time.sleep, fills=0,
                 send_times=(), alert_fn=None, balance_at_fn=None, check_sink=None, cache=None,
                 inline_safety_fn=None, inline_facts_fn=None, stats_fn=None,
                 inline_deadline_s=2.0, inline_host=None, routes=("v4_native",),
                 revert_reason_fn=None):
        self.submission_transport = (ctx.get("submission_transport")
                                     if self.CHAIN_ID in _SHARED_TRANSPORT_CHAINS and sender is None
                                     else None)
        self.ctx, self.conn, self.fanout_fn = ctx, conn, fanout_fn
        self.sender = sender if sender is not None else (
            SequencerSender() if self.CHAIN_ID == 4663 and self.submission_transport is None else None)
        self.revert_reason_fn = revert_reason_fn      # tx hash -> (kind, detail): a mined revert's class
        # ponytail: one custody read at a time per lane; a hung one occupies its daemon thread until
        # its own HTTP timeout, and meanwhile reads either join it or answer None (never a second).
        self._read_lock, self._read_job, self._read_thread = threading.Lock(), None, None
        self.contexts = dict(contexts or {})
        self.taker = ctx["address"]
        self.rpc_read, self.facts_fn = rpc_read, facts_fn
        self.lock_key, self.lock_dir, self.clip_usd, self.low_wei = lock_key, lock_dir, clip_usd, low_wei
        self.generation_fn, self.host_clock = generation_fn, host_clock
        self.max_snapshot_age_s, self.eth_usd_max_age_s = max_snapshot_age_s, eth_usd_max_age_s
        self.generation_margin_s = generation_margin_s
        self.pilot_fills, self.max_sends_per_hour = pilot_fills, max_sends_per_hour
        self.require_exact_state = require_exact_state
        self.hook_factory = hook_factory or default_hook_factory
        self.receipt_timeout_s, self.receipt_poll_s, self.sleep = receipt_timeout_s, receipt_poll_s, sleep
        self.lock_receipt_cap_s = lock_receipt_cap_s
        self.fills = int(fills)
        self.send_times = collections.deque(send_times)
        if alert_fn is None:
            from sniper.services.memecoin_alerts import send_alert

            def alert_fn(text):
                send_alert("rh_fast_lane", text)
        self.alert_fn = alert_fn
        self.balance_at_fn = balance_at_fn            # (mint, block number) -> atomic, or None
        self.check_sink = check_sink                  # where a fill's balance check is recorded
        self._checks = []
        # THE OPERATOR'S STOP (Alpha 2026-10-02, Gamma's pilot stop rule): set from the stop file by
        # a 1 s poller off the fill path; no restart needed, because a restart is refused while the
        # signed or held reservation that fires a hard stop is still open.
        self.stopped = False
        # INLINE ON A CACHE MISS (Alpha 2026-10-02; BSC first, 4663 when its safety host is
        # dedicated): the safety cache, the inline verdict and the coin's facts and stats seams.
        self.cache = cache
        self.inline_safety_fn, self.inline_facts_fn = inline_safety_fn, inline_facts_fn
        from sniper.execution.evm_token_safety import names_timings
        self._timed_inline_safety_fn = inline_safety_fn if names_timings(inline_safety_fn) else None
        self.inline_host = inline_host               # the inline seam's PacedHttp: dead() and counts
        self.routes = frozenset(routes)              # MEMECOIN_RH_FAST_ROUTES (routes rev 3, 6.3)
        self.stats_fn, self.inline_deadline_s = stats_fn, inline_deadline_s
        self.shared_worker, self._pilot_pending = None, 0
        self._inputs_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fast-lane-inputs")
        self._inputs_future = None
        # MEMECOIN_RH_FAST_PARALLEL_SAFETY: one inline safety read at a time, beside the inputs.
        self._safety_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fast-lane-safety")
        self._safety_future = None
        # C1 INTENT WARM (2026-10-16, Alpha; MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS): the watcher's
        # datagram at the source log's arrival starts this coin's inline reads ~70 ms before the
        # signal lands. Lane thread only (warm_intent and the claim run where handle() runs), one
        # coin at a time, rate-capped, its own pool (a signal never waits behind it), never awaited
        # when absent.
        self._intent_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fast-lane-intent")
        self._intent = None
        self._intent_times = collections.deque()
        self.intent_counts = collections.Counter()
        self._intent_expired = collections.OrderedDict()   # mint -> expired unclaimed (trace record)

    def handle(self, payload, *, recv_epoch, safety_reason, stats=None):
        """One signal: the in-memory checks, then the funded hook with the lane's executor."""
        t0, c0, host_now = time.perf_counter(), time.thread_time(), self.host_clock()
        rec = {"tx_hash": None, "source_tx": payload["tx_hash"], "mint": payload["mint"],
               "route_kind": route_kind(payload), "pool_choice": payload.get("pool_choice"),
               "pool": payload.get("pool"), "recv_epoch": recv_epoch,
               "sent_epoch": payload["sent_epoch"],
               "handoff_ms": (recv_epoch - float(payload["sent_epoch"])) * 1000.0,
               "clip_wei": None, "min_out": None, "tx_bytes": None, "lock_busy": 0,
               "busy_start_epoch": host_now, "_perf0": t0, "state_class": state_class(payload)}

        def done(reason, **fields):
            rec.pop("_perf0", None)
            rec.pop("_intent_entry", None)
            rec.pop("_inline_deadline", None)
            rec.pop("_safety_before", None)
            rec.pop("_safety_timings", None)
            if rec.pop("_safety_future", None) is not None:
                rec["parallel_safety"] = "abandoned"      # its result is never read or cached
            rec.update(fields, reason=reason, busy_end_epoch=self.host_clock(),
                       busy_ms=(time.perf_counter() - t0) * 1000.0,
                       cpu_ms=(time.thread_time() - c0) * 1000.0)
            stamp_input_legs(rec)
            return rec

        if self.stopped:
            return done("operator_stop")
        if self.prebuy_inflight_fn is not None and self.prebuy_inflight_fn(payload["mint"]):
            # A Relay pre-buy of this coin is deciding or awaiting its receipt (Alpha, on Gamma's MEDIUM 2):
            # no hook here, so no attempt row; the on-chain lane waits on the pre-buy's row and judges after.
            return done("relay_prebuy_in_flight")
        kind = route_kind(payload)
        # A source-pool buy is option C's cover for v4_shared, so v4_shared's permission gates it,
        # whatever builder (native, WETH, bridge) it takes (Gamma 2026-10-08).
        gate = "v4_shared" if payload.get("pool_choice") == "source_pool" or kind == 'split_shared' else kind
        if gate not in self.routes:                  # routes rev 3 (6.3): MEMECOIN_RH_FAST_ROUTES
            return done("route_off:%s" % gate)
        if self.shared_worker is not None:
            self.shared_worker.drain()
        if self.fills + self._pilot_pending >= self.pilot_fills:
            return done("pilot_complete")
        while self.send_times and host_now - self.send_times[0] > 3600:
            self.send_times.popleft()
        if len(self.send_times) + self._pilot_pending >= self.max_sends_per_hour:
            return done("pilot_rate_cap")
        if payload.get("paid_usd") is None:
            return done("no_source_trade")
        reason = self._initiation(payload, kind, rec)
        if reason is not None:
            return done(reason)
        if kind in SHARED_ROUTE_KINDS:
            if self.shared_worker is not None:
                return done(self.shared_worker.offer(payload, recv_epoch))
            res = self._handle_shared(payload, rec)
            return done(hook_reason(res), pending_id=res.get("pending_id"),
                        position_id=res.get("position_id"))
        cached = stats[0] if stats else None
        # DECISION FEATURES (spec 2026-10-14): the tuple behind `cached`, built at the trace's finish.
        held = {"entry": stats, "read_at": host_now, "source": "lane_cache"}
        reason = self._precheck(payload, rec, host_now, safety_reason, cached)
        rec["safety_path"] = "cache"
        needs_inline = False
        if (reason in ("no_safety_verdict", "safety_stale") and self.inline_safety_fn is not None
                and not self._cached_only()):
            # A cache miss checked inline on the dedicated safety host, after every cheaper check
            # passed -- the hook's network-free absolute source-buy band included -- and a pass is
            # cached and the in-memory checks run again on it.
            reason = self._band_refusal(payload)
            if reason is None and lane_cfg.MEMECOIN_RH_FAST_PARALLEL_SAFETY:
                reason = self._start_safety(str(payload["mint"]).lower(), rec)
            if reason is None:
                reason = self._inline_inputs(str(payload["mint"]).lower(), rec)
            if reason is None:
                needs_inline = True
                read_at = self.host_clock()
                got = self.cache.stats(payload["mint"], now=read_at)
                cached = got[0] if got else None
                held = {"entry": got, "read_at": read_at, "source": "lane_cache"}
                reason = self._precheck(payload, rec, self.host_clock(), None, cached)
        if reason is not None:
            return done(reason)
        facts = self.facts_fn(payload["mint"])
        if not facts:                                # expired since the precheck: unknown supply
            return done("no_mcap")
        decimals, supply = facts
        ev = self._lane_event(payload, decimals=decimals, supply=supply)
        ev.decision_features = partial(from_cached_stats, lane=self.DELIVERY_LANE, held=held,
                                       their_usd=ev.their_size_usd)

        def execute_fn(conn, pending_id, mint, trade_size, now_iso, trace=None, presign_check_fn=None):
            return self.execute_reserved(payload, rec, conn, pending_id, trade_size,
                                         decimals=decimals, trace=trace,
                                         presign_check_fn=presign_check_fn)
        contexts = self._lane_context(payload, execute_fn)
        if needs_inline:
            # The hook's sequential safety path reserves the coin before calling token safety.
            # No execution lock or DB transaction is held during these reads.
            # A J7 twin cannot reserve while this unsigned claim is being checked.
            def inline_safety(mint, usd=None):
                why = self._inline(mint, rec)
                if why is not None:
                    rec["_outcome"] = why
                return why is None, why
            key = str(self.CHAIN_ID)
            contexts[key] = dict(contexts[key], token_safety_fn=inline_safety)
        hook = self.hook_factory(contexts, cached)
        if self.stopped or getattr(self, "_stop_owner", self).stopped:
            return done("operator_stop")
        self._stamp_intent(ev, payload, rec)
        with refusal_pages_off():
            res = hook.attempt(self.conn, ev)
        res = res or {}
        return done(rec.pop("_outcome", None) or hook_reason(res),
                    pending_id=res.get("pending_id"), position_id=res.get("position_id"))

    ORIGIN_PREFIX = "rhfast"
    prebuy_inflight_fn = None          # RelayPrebuyWorker.inflight, MEMECOIN_RH_RELAY_PREBUY on only

    def _cached_only(self):
        """4663 sends only on a cached verdict (spec 2026-10-14 rh-fast-cached-only §3.1): a miss
        keeps the precheck's refusal and the ordinary path judges the buy. Read per signal."""
        return self.CHAIN_ID == 4663 and lane_cfg.MEMECOIN_RH_FAST_CACHED_ONLY

    def _initiation(self, payload, kind, rec):
        """None when the signal's initiation is proved (rec["initiation"] names how), else the
        fallback reason. Option C's deposit proof or the source's own receipt; the Relay pre-buy
        overrides it (its proof is the deposit plus its Relay request)."""
        deposit = (lane_cfg.MEMECOIN_RH_FAST_DEPOSIT_PROOF and kind not in SHARED_ROUTE_KINDS
                   and payload.get("source_fill") is None
                   and isinstance(payload.get("deposit_proof"), dict))
        if deposit:
            # OPTION C (memecoin_rh_deposit_proof): no receipt yet. The user's own signed Solana
            # deposit proves initiation, judged by the hook's own gate here, before any attempt row;
            # the cap may only be the post-trade price x (1 + chase) (operator option 2).
            why = payload_refusal(payload)
            if why is not None:
                return "deposit_proof_refused:%s" % why
            if payload.get("eth_usd") is None:
                return "no_eth_usd"
            if not cap_bound(payload):
                return "deposit_cap_unbound"
            rec["initiation"] = "deposit:%s" % payload["deposit_proof"].get("origin_tx")
            return None
        reason = receipt_refusal(payload, kind)
        if reason is not None:
            return reason
        rec["initiation"] = "receipt"
        return None

    def _handle_shared(self, payload, rec):
        """Keep the original funded safety, quoter and executor, bounded by the pilot clip."""
        from sniper.services.memecoin_real_runner import make_real_hook
        from sniper.settings import memecoin as cfg
        from sniper.util.clock import real_utc_now_iso
        from sniper.services.memecoin_bsc_fast_lane import policy_decision_for
        # The on-chain lane's policy verdict on this exact source buy is final (memecoin_onchain_lane.
        # ONCHAIN_POLICY_FINAL): a shared route sends only after its receipt, by when that lane may
        # have judged it. One dict read in this process.
        decided = policy_decision_for(tx_hash=payload.get('tx_hash'), mint=payload.get('mint'),
                                      source_user_id=None)
        if decided is not None:
            return {'status': 'rejected', 'reason': 'onchain_decided: ' + decided}
        proof = validate_source_fill(payload.get('source_fill'), payload)
        if proof is None:
            return {'status': 'rejected', 'reason': 'source_fill_unproved'}
        if (route_kind(payload) == 'split_shared'
                and not (proof.get('source_post') or {}).get('pools')):
            return {'status': 'rejected', 'reason': 'source_split_unproved'}
        # Shared events already carry authenticated token decimals. Supply is unused by
        # LaneEvent's shared branch; the original funded hook owns mcap resolution.
        held = {}                     # DECISION FEATURES: what the hook's own stats read returned

        def stats_fn(mint, network_id=None):
            now = self.host_clock()
            got = self.cache.stats(mint, now=now)
            if got is not None:
                held.update(entry=got, read_at=now, source="lane_cache")
                return got[0]
            started = self.host_clock()
            stats = self.stats_fn(mint) if self.stats_fn is not None else None
            self.cache.put_market_stats(mint, stats, now=started)
            held.update(entry=(stats, started) if stats else None, read_at=started,
                        source="inline_read")
            return stats
        contexts = dict(self.contexts)
        ctx = dict(self.ctx, entry_size_cap_usd=self.clip_usd)
        # SPEED FIX 1 (operator 2026-10-14 via Alpha), read per signal, on this signal's copy only.
        if lane_cfg.MEMECOIN_RH_FAST_SHARED_EARLY_SNAPSHOT:
            ctx["early_quote_balances_fn"] = lambda quote_usd: snapshot_quote_balances(
                payload, quote_usd, generation=self.generation_fn(), now_host=self.host_clock(),
                max_age_s=self.max_snapshot_age_s, margin_s=self.generation_margin_s)
        if (lane_cfg.MEMECOIN_RH_FAST_SHARED_CACHED_SAFETY
                and route_kind(payload) in SHARED_CACHE_KINDS and callable(ctx.get("token_safety_fn"))):
            ctx["token_safety_fn"] = cache_first_safety(ctx["token_safety_fn"], self.cache,
                                                        now_fn=self.host_clock)
        contexts[str(self.CHAIN_ID)] = ctx
        factory = getattr(self, "shared_hook_factory", None)
        hook = (factory(contexts, stats_fn) if factory else make_real_hook(
            None, contexts, now_fn=real_utc_now_iso, trade_size=cfg.MEMECOIN_TRADE_SIZE_USD,
            max_age_s=cfg.MEMECOIN_REAL_ENTRY_MAX_AGE_S, stats_fn=stats_fn))
        ev = self._lane_event(payload, decimals=proof['token_decimals'], supply=None)
        ev.decision_features = partial(from_cached_stats, lane=self.DELIVERY_LANE, held=held,
                                       their_usd=ev.their_size_usd)
        preparation = getattr(self, "preparation", None)
        ev.source_evidence["entry_preparation"] = (
            preparation.readiness_at_ingress(ev.mint, ev.receipt_clock, route=route_kind(payload))
            if preparation is not None else {"state": "unknown", "scope": "discovery_only",
                                            "reason": "preparation_disabled"})
        if self.stopped or getattr(self, "_stop_owner", self).stopped:
            return {"status": "skipped", "reason": "operator_stop"}
        with refusal_pages_off():
            res = hook(self.conn, ev) or {}
        rec["tx_hash"] = res.get("tx_hash") or res.get("tx_sig")
        if res.get("status") == "filled":
            self.fills += 1
        if res.get("tx_sig") or res.get("tx_hash") or res.get("status") == "filled":
            self.send_times.append(self.host_clock())
        return res

    def _band_refusal(self, payload):
        """"source_band:<reason>" when the hook's ABSOLUTE source-buy band would refuse this signal,
        else None. Run on a cache miss BEFORE the inline check (Alpha/Gamma 2026-10-02: degen_ak
        09:21:22Z spent 610 ms and ~10 inline calls on a buy the floor then refused), lane-only:
        the runner's gate order is unchanged.

        THE HOOK'S OWN ARITHMETIC, through source_buy_family_reason with the mcap legs off: the
        floor judges the trader's net (the same prior-net read the hook makes, keyed on the
        lane's origin so the J7 twin of this buy is not "earlier"), the ceiling the single buy,
        the boundary tolerance as configured. A refusal here is one the hook makes too: its ratio
        leg runs first and can only refuse, and the band then judges the same amount. A wallet or
        chain exemption of source_buy_floor skips this (the hook would waive it).

        Moved ONLY this: the ratio floor and the mcap ceiling need the coin's supply, the volume and
        market gates its stats, and token safety the verdict -- on a miss all three come from the
        inline read, so those gates stay where they are. Any error returns None: the inline check
        and the hook decide, as before."""
        from sniper.persistence import db_memecoin as m
        from sniper.services.memecoin_real_runner import source_buy_family_reason
        from sniper.settings import memecoin as cfg
        try:
            their = float(payload["paid_usd"])
            if not math.isfinite(their) or their < 0:
                return None                  # the hook reads this as unknown, which passes the band
            abs_min = cfg.min_source_buy_usd_for(str(self.CHAIN_ID), payload.get("source_user_id"))
            abs_max = max(0.0, float(getattr(cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 0.0) or 0.0))
            if not (abs_min > their or 0 < abs_max < their):
                return None                                  # clears the band on the single buy
            chain, mint = str(self.CHAIN_ID), str(payload["mint"]).lower()
            uid = payload.get("source_user_id")
            if "source_buy_floor" in m.entry_gate_exemptions(self.conn, uid, chain):
                return None
            prior = None
            window = float(getattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 0) or 0)
            if abs_min > their and window > 0:
                prior = m.source_prior_net_buy_usd(
                    self.conn, source_user_id=uid, network_id=chain, mint=mint,
                    before_iso=payload.get("arrival_utc"), window_s=window,
                    exclude_event_id="%s:%s:%s:%s" % (self.ORIGIN_PREFIX, chain,
                                                      str(payload["tx_hash"]).lower(), mint),
                    judged_usd=their)
            reason, _info = source_buy_family_reason(
                their_usd=their, source_mcap=None, prior_net_usd=prior, ratio=0.0,
                unknown_mcap_floor=0.0, abs_min=abs_min, abs_max=abs_max, family_active=False)
        except Exception:
            logger.exception("fast lane: pre-inline source band check failed (inline decides)")
            return None
        return None if reason is None else "source_band:" + reason

    def _inline_inputs(self, mint, rec):
        """Read the existing gate inputs before admission, without asserting token safety."""
        rec["safety_path"] = "inline"
        if self.inline_host is not None and self.inline_host.dead():
            return "inline_host_dead"
        t0 = time.perf_counter()
        deadline = rec.setdefault("_inline_deadline", time.monotonic() + float(self.inline_deadline_s))
        entry = self._claim_intent(mint, rec) or {}
        stats_future = None
        def read_stats():
            started, t_read = self.host_clock(), time.perf_counter()
            with inline_deadline(deadline):
                try:
                    stats = self.stats_fn(mint)
                except Exception:
                    stats = None
            return stats, started, (time.perf_counter() - t_read) * 1000.0
        def start_stats():
            # Only the lane thread schedules reads. A timed-out provider may still
            # own the worker: never queue behind it or bypass it with warm facts.
            if self._inputs_future is not None and not self._inputs_future.done():
                return None
            future = self._inputs_pool.submit(read_stats)
            self._inputs_future = future
            return future
        try:
            with inline_deadline(deadline):
                # These are independent required reads. One bounded worker; only the lane
                # publishes their results, and neither read grants a token-safety verdict.
                if (self.stats_fn is not None
                        and self.cache.stats(mint, now=self.host_clock()) is None):
                    stats_future = entry.get("stats") or start_stats()
                    if stats_future is None:
                        return "inline_market_busy"
                if self.cache.facts(mint) is None:
                    t_facts = time.perf_counter()
                    try:
                        if "facts" in entry:          # the intent's read, started at log arrival
                            facts = entry["facts"].result(timeout=max(0., deadline - time.monotonic()))
                        else:
                            facts = self.inline_facts_fn(mint)
                        self.cache.put_facts(mint, facts)
                    except FutureTimeout:
                        return "inline_facts_error:TimeoutError"
                    except Exception as exc:
                        return "inline_facts_error:%s" % type(exc).__name__
                    finally:                 # cut 4: which leg is the long pole (absent = not read)
                        rec["facts_ms"] = (time.perf_counter() - t_facts) * 1000.0
                if self.cache.stats(mint, now=self.host_clock()) is None and self.stats_fn is not None:
                    # Previously warm stats can expire while facts are being read.
                    if stats_future is None:
                        stats_future = start_stats()
                        if stats_future is None:
                            return "inline_market_busy"
                    t_stats = time.perf_counter()
                    try:
                        stats, started, rec["stats_ms"] = stats_future.result(
                            timeout=max(0., deadline - time.monotonic()))
                    except FutureTimeout:
                        return "inline_unsafe:safety_deadline_exceeded"
                    if stats_future is entry.get("stats"):
                        # cut 4's leg is the signal's own wait; the intent's read time is evidence
                        rec["execution_evidence"]["intent"]["stats_read_ms"] = rec["stats_ms"]
                        rec["stats_ms"] = (time.perf_counter() - t_stats) * 1000.0
                    self.cache.put_market_stats(mint, stats, now=started)
            return None
        finally:
            if stats_future is not None:
                stats_future.cancel()
            rec["inputs_ms"] = (time.perf_counter() - t0) * 1000.0

    INTENT_TTL_S = 5.0
    INTENT_PER_MINUTE = 30

    def warm_intent(self, mint, pair=None):
        """C1: start a coin's inline reads (market stats, facts, the parallel safety read) at the
        source log's arrival, before the receipt decode. Best-effort and bounded: one coin at a time
        (another coin's intent is skipped while one is open), INTENT_PER_MINUTE starts, nothing read
        that the cache already holds, nothing cached here. The signal claims the reads in
        _start_safety/_inline_inputs; one no signal claims within INTENT_TTL_S expires (counted, the
        "no buy" waste). Returns what happened; never raises."""
        try:
            return self._warm_intent(str(mint).lower(), pair)
        except Exception:
            logger.exception("fast lane: intent warm failed (the signal reads as before)")
            self.intent_counts["error"] += 1
            return "error"

    def _warm_intent(self, mint, pair=None):
        now = time.monotonic()
        self._expire_intent(now)
        if self._intent is not None:
            why = "duplicate" if self._intent[0] == mint else "busy"
            self.intent_counts[why] += 1
            return why
        while self._intent_times and now - self._intent_times[0] >= 60.0:
            self._intent_times.popleft()
        if len(self._intent_times) >= self.INTENT_PER_MINUTE:
            self.intent_counts["rate_capped"] += 1
            return "rate_capped"
        host = self.inline_host
        if host is not None and host.dead():
            self.intent_counts["host_dead"] += 1
            return "host_dead"
        deadline = now + float(self.inline_deadline_s)
        entry = {"at": now}
        if self.stats_fn is not None and self.cache.stats(mint, now=self.host_clock()) is None:
            entry["stats"] = self._intent_pool.submit(self._intent_stats, mint, deadline)
        if self.inline_facts_fn is not None and self.cache.facts(mint) is None:
            entry["facts"] = self._intent_pool.submit(self._intent_facts, mint, deadline)
        binding = (self._intent_binding(mint, pair) if self._parallel_safety_on()
                   and self.inline_safety_fn is not None and self.cache.verdict(mint) is None else None)
        if binding is UNKNOWN_BINDING:
            # The quote the signal will bind is not known at the log's arrival: a read now could not
            # be adopted and would only queue on the inline host beside the facts read (e54dddf6).
            self.intent_counts["safety_quote_unknown"] += 1
        elif (self._parallel_safety_on() and self.inline_safety_fn is not None
                and self.cache.verdict(mint) is None):
            timings = ({} if self.inline_safety_fn is getattr(self, "_timed_inline_safety_fn", None)
                       else None)
            entry.update(safety_before=host.counts["transport_errors"] if host is not None else 0,
                         safety_timings=timings, safety_binding=binding)
            entry["safety"] = self._intent_pool.submit(self._intent_safety, mint, self.cache.usd,
                                                       timings, deadline)
        if not any(leg in entry for leg in ("stats", "facts", "safety")):
            self.intent_counts["cached"] += 1
            return "cached"
        self._intent = (mint, entry)
        self._intent_times.append(now)
        self.intent_counts["started"] += 1
        return "started"

    def _safety_binding(self, mint):
        """What the inline safety read binds besides the mint and the clip (the 56 lane: the coin's
        quote). An intent's safety read is adopted only when the signal's binding is the same."""
        return None

    def _intent_binding(self, mint, pair):
        """The binding an intent's safety read starts under, or UNKNOWN_BINDING when the signal's cannot
        be known at the log's arrival (then no intent safety read starts). `pair` is the source Transfer's
        sender. Here (4663): the same binding as the signal's."""
        return self._safety_binding(mint)

    def _parallel_safety_on(self):
        """Whether a cold signal starts its safety read beside the inputs (4663's knob here; the 56
        lane overrides with its own), so the intent starts that read only where the signal would."""
        return lane_cfg.MEMECOIN_RH_FAST_PARALLEL_SAFETY

    def _intent_stats(self, mint, deadline):
        started, t0 = self.host_clock(), time.perf_counter()
        with inline_deadline(deadline):
            try:
                stats = self.stats_fn(mint)
            except Exception:
                stats = None                   # read_stats' rule: an unread market is unknown
        return stats, started, (time.perf_counter() - t0) * 1000.0

    def _intent_facts(self, mint, deadline):
        with inline_deadline(deadline):
            return self.inline_facts_fn(mint)

    def _intent_safety(self, mint, usd, timings, deadline):
        with inline_deadline(deadline):
            return self.inline_safety_fn(mint, usd=usd,
                                         **({"timings": timings} if timings is not None else {}))

    def _expire_intent(self, now):
        if self._intent is not None and now - self._intent[1]["at"] > self.INTENT_TTL_S:
            self._intent_expired.pop(self._intent[0], None)
            self._intent_expired[self._intent[0]] = now
            while len(self._intent_expired) > 64:
                self._intent_expired.popitem(last=False)
            self._intent = None
            self.intent_counts["expired_unused"] += 1

    def _stamp_intent(self, ev, payload, rec):
        """C1 per-attempt record (2026-10-17, Alpha), on listed intent chains: the hook marks it as the
        attempt trace starts. handoff_to_decision_ms runs from the watcher's send to this hook call
        (host clock on both). Never raises: a record never decides."""
        try:
            if str(self.CHAIN_ID) in lane_cfg.memecoin_evm_lane_intent_warm_chains():
                ev.pre_trace_substages = [("evm.intent_warm", {
                    "intent": self._intent_outcome(str(payload["mint"]).lower(), rec),
                    "handoff_to_decision_ms":
                        (self.host_clock() - float(payload["sent_epoch"])) * 1000.0})]
        except Exception:
            logger.exception("fast lane: intent trace record failed")
        return ev

    def _intent_outcome(self, mint, rec):
        """This signal's intent, for its attempt trace: claimed (its reads were adopted), open_unclaimed
        (an intent is open but the signal needed none of its reads), expired_unused (it expired before
        the signal came) or none."""
        self._expire_intent(time.monotonic())
        if rec.get("_intent_entry") is not None:
            return "claimed"
        if self._intent is not None and self._intent[0] == mint:
            return "open_unclaimed"
        return "expired_unused" if self._intent_expired.pop(mint, None) is not None else "none"

    def _claim_intent(self, mint, rec):
        """The open intent for `mint`, claimed once per signal (kept on rec), or None."""
        if "_intent_entry" in rec:
            return rec["_intent_entry"]
        now, entry = time.monotonic(), None
        self._expire_intent(now)
        if self._intent is not None and self._intent[0] == mint:
            entry, self._intent = self._intent[1], None
            self.intent_counts["claimed"] += 1
            rec.setdefault("execution_evidence", {})["intent"] = {
                "age_ms": round((now - entry["at"]) * 1000.0, 3),
                "legs": sorted(leg for leg in ("stats", "facts", "safety") if leg in entry)}
        rec["_intent_entry"] = entry
        return entry

    def _start_safety(self, mint, rec):
        """Start the inline safety read beside the inline inputs (Alpha GO (c), 2026-10-08).
        Always None: this never refuses a signal.

        The worker ONLY reads: it enters the signal's deadline scope and calls the inline safety
        function, nothing else. The cache, the labels and the verdict belong to the lane thread,
        which reads the result inside the hook after the reservation (`_inline`), or never: an
        unread result is abandoned and nothing from it is cached.

        BUSY (Gamma LOW, Alpha 2026-10-08): while an earlier (abandoned or overrun) read is still
        running, this signal starts no read and its hook checks it sequentially on the lane
        thread, exactly as with the knob off, so an abandoned read never refuses a later signal.
        That sequential check can overlap the running read; each binds its own sell amount
        (routed_safety's per-call probe_out, review EVM06), so the overlap cannot cross amounts."""
        entry = self._claim_intent(mint, rec)
        if entry is not None and "safety" in entry:
            if entry["safety_binding"] == self._safety_binding(mint):
                # The intent's read, started at the log's arrival: same function, same sell amount,
                # same binding (Gamma B1 on 316bd94e0: a coin's quote noted only by this signal).
                rec.setdefault("_inline_deadline", time.monotonic() + float(self.inline_deadline_s))
                rec.update(_safety_before=entry["safety_before"],
                           _safety_timings=entry["safety_timings"],
                           _safety_future=entry["safety"], parallel_safety="intent")
                return None
            self.intent_counts["safety_quote_mismatch"] += 1    # the signal reads its own below
        if self._safety_future is not None and not self._safety_future.done():
            rec["parallel_safety"] = "busy"
            return None
        host = self.inline_host
        if host is not None and host.dead():
            return None
        deadline = rec.setdefault("_inline_deadline", time.monotonic() + float(self.inline_deadline_s))
        rec["_safety_before"] = host.counts["transport_errors"] if host is not None else 0
        usd = self.cache.usd
        timings = {} if self.inline_safety_fn is getattr(self, "_timed_inline_safety_fn", None) else None
        rec["_safety_timings"] = timings

        def read():
            with inline_deadline(deadline):
                return self.inline_safety_fn(mint, usd=usd,
                    **({"timings": timings} if timings is not None else {}))
        self._safety_future = rec["_safety_future"] = self._safety_pool.submit(read)
        rec["parallel_safety"] = "started"
        return None

    def _inline(self, mint, rec):
        """A cache miss checked inline on the lane's own host: today's safety function, then the
        coin's chain facts and market stats when not cached. None on a pass (cached for the next
        signal), else the fallback. A failure, a timeout or a refusal is never cached: the
        background loop checks the coin again. A dead host falls back at once, unmeasured; a
        refusal during which the host failed a read is the host's, never the coin's (live
        2026-10-02: a TLS alert read as 'inline_unsafe:code read failed')."""
        rec["safety_path"] = "inline"
        host = self.inline_host
        future = rec.pop("_safety_future", None)
        timings = (rec.pop("_safety_timings", None) if future is not None else {}
                   if self.inline_safety_fn is getattr(self, "_timed_inline_safety_fn", None) else None)
        if future is not None:
            # "intent_used": the intent's read, adopted (bsc_lane_live then tells adoption apart).
            rec["parallel_safety"] = "intent_used" if rec.get("parallel_safety") == "intent" else "used"
        if host is not None and host.dead():
            return "inline_host_dead"
        before = (rec.pop("_safety_before", 0) if future is not None
                  else host.counts["transport_errors"] if host is not None else 0)
        t0 = time.perf_counter()
        deadline = rec.get("_inline_deadline", time.monotonic() + float(self.inline_deadline_s))
        scope = inline_deadline(deadline)
        expired = scope.__enter__()
        try:
            if expired():
                return "inline_unsafe:safety_deadline_exceeded"
            try:
                ok, why = (self.inline_safety_fn(mint, usd=self.cache.usd,
                               **({"timings": timings} if timings is not None else {})) if future is None
                           else _await_safety(future, deadline))
            except _SafetyWaitExceeded:
                return "inline_unsafe:safety_deadline_exceeded"
            except BridgeLegFailed:
                # The route's, never the coin's (Gamma C2); the host's when it failed a read during
                # this check (Gamma LOW3 on a8d5ba8da).
                if host is not None and host.counts["transport_errors"] != before:
                    return "inline_host_error"
                return "bridge_leg_failed"
            except Exception as exc:
                why, ok = "inline_safety_error:%s" % type(exc).__name__, None
            # ponytail: a concurrent inline check's failure can relabel this one; labels only
            if not ok and host is not None and host.counts["transport_errors"] != before:
                return "inline_host_error"
            if ok is None:
                return why
            if not ok:
                return "inline_unsafe:%s" % (why or "refused")
            # A reused supply keeps its original observation time (Codex night review, EVM-07):
            # only a fresh read is handed to put(), which stamps it now.
            fresh = None
            if not self.cache.facts(mint):
                try:
                    fresh = self.inline_facts_fn(mint)
                except Exception as exc:
                    return "inline_facts_error:%s" % type(exc).__name__
            got = self.cache.stats(mint, now=self.host_clock())
            stats = got[0] if got else None
            if not stats and self.stats_fn is not None:
                try:
                    stats = self.stats_fn(mint)
                except Exception:
                    stats = None                      # absent: no_market_stats falls back below
            self.cache.put(mint, now=self.host_clock(), facts=fresh, stats=None if got else stats,
                           reason=why)
            return None
        finally:
            scope.__exit__(None, None, None)
            rec["safety_ms"] = (time.perf_counter() - t0) * 1000.0
            rec.setdefault("execution_evidence", {}).update(
                inline_safety_path_ms=rec["safety_ms"], reservation_to_verdict_wait_ms=None)
            stamp_input_legs(rec)
            if timings is not None and (future is None or future.done()):
                rec["execution_evidence"]["token_safety_timings"] = timings.copy()

    # ------------------------------------------------------------------ the lane's executor

    def execute_reserved(self, payload, rec, conn, pending_id, trade_size, *, decimals, trace=None,
                         presign_check_fn=None):
        """The leg's execute_fn for a reservation the hook made: a result dict the hook finishes
        its trace with. `rec` carries the lane's own outcome and timings."""
        from sniper.execution.exec_lock import touch_generation, wallet_exec_lock
        from sniper.persistence import db_memecoin as m

        def abort(outcome, why):
            m.release_pending(conn, pending_id, self.RELEASE_PREFIX + outcome)
            rec["_outcome"] = outcome
            return {"status": "aborted", "reason": why, "pending_id": pending_id}

        pending = m.pending_row(conn, pending_id)
        movement = lane_cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY
        if movement:
            from sniper.execution.entry_attribution import market_movement_check
            market = market_movement_check(cap_pct=lane_cfg.copy_premium_cap(
                payload.get("source_user_id"), str(self.CHAIN_ID))[0])
            market.update(unknown_reason="current_pre_copy_spot_unproved", unknown_action="proceed")
            rec.setdefault("execution_evidence", {})["copy_market_movement"] = market
            if trace is not None:
                try:
                    trace.mark_substage("copy_market_movement", **market)
                except Exception:
                    logger.warning("native market-movement trace unavailable", exc_info=True)
        if self._post_peak_nominated(conn, pending):
            return abort("post_peak", "post_peak: the lane has no exact quote to confirm it")
        clip_usd = min(float(trade_size), float(self.clip_usd))
        clip_wei = clip_wei_for(clip_usd, self._quote_asset_usd(payload))
        if str(rec.get("initiation") or "").startswith("deposit:"):
            # OPERATOR OPTION 2 (Alpha 2026-10-08): a deposit-proved buy has no receipt, so the
            # reservation's source average is our estimate, and an estimated source carries no
            # premium term. On the first option-C send the copy-premium floor over it bound tighter
            # than the cap (6.09e23 vs 5.80e23); that send reverted on price either way (received
            # 5.29e23, Gamma). The signed minimum is the decide's cap alone, which cap_bound held
            # to post x (1 + chase) in handle().
            floor, why = 0, None
        else:
            floor, why = self._copy_floor(payload, pending, clip_wei, decimals)
        if why is not None:
            return abort("copy_signed_floor", why)
        try:
            tx, min_out = self._buy_tx(payload, clip_wei, int(self.host_clock()) + _lane_deadline_s(),
                                      floor=floor)
        except (ValueError, KeyError, TypeError, ZeroDivisionError):
            if not movement:
                raise
            return abort("build_refused", "native output quote unavailable")
        if min_out <= 0:                                # no slippage bound is never a buy
            return abort("build_refused", "minimum output is zero at the approved clip")
        # A lane's own gas bid (56 crowded gas), decided before the lock; None signs today's price.
        bid = self._gas_bid(payload, rec, trace, clip_wei, int(tx["gas"]))
        if bid is not None:
            rec["gas_bid_wei"] = int(bid)
            rec["gas_bid_extra_wei"] = bid_extra_wei(payload["snapshot"], int(tx["gas"]), int(bid))
        evidence = rec.setdefault("execution_evidence", {})
        evidence.update(signed_floors=self._floor_evidence(payload, clip_wei, floor, min_out),
                        quote_price=None, source_reference="source_post_price_and_wallet_average",
                        floor_reference={"cap": "source_post_or_average_cap", "copy": "wallet_average",
                                         "slippage": "source_post_pool_estimate"},
                        public_transaction={"chain": str(self.CHAIN_ID), "pool": payload.get("pool"),
                                            "to": tx.get("to"), "value": str(tx.get("value")),
                                            "calldata": str(tx.get("data") or "")[:32768],
                                            "calldata_truncated": len(str(tx.get("data") or "")) > 32768})
        if movement:
            evidence.update(source_reference="intervening_market_unknown",
                            floor_reference={"slippage": "size_aware_pool_output_estimate"})
        latest = payload.get("latest_same_pool")
        evidence["latest_same_pool"] = dict(latest) if isinstance(latest, dict) else None
        evidence["latest_same_pool_age_s_at_build"] = None
        if isinstance(latest, dict):
            try:
                age = self.host_clock() - float(latest["observed"])
                if math.isfinite(age) and age >= 0:
                    evidence["latest_same_pool_age_s_at_build"] = age
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        rec.update(clip_wei=clip_wei, min_out=min_out, copy_floor=floor,
                   tx_bytes=(len(tx["data"]) - 2) // 2)
        t_lock = time.perf_counter()
        receipt, tx_hash = None, None
        observer = (getattr(self, "ctx", None) or {}).get("bsc_execution_observer") if str(self.CHAIN_ID) == "4663" else None
        receipt_observation = {} if observer is not None else None
        with wallet_exec_lock(self.lock_key, lock_dir=self.lock_dir, blocking=False) as held:
            if not held:
                rec["lock_busy"] = 1
                return abort("lock_busy", "wallet execution lock busy: today's path covers it")
            unchanged = False
            try:
                # Admission precedes this lock; another wallet actor may have sent meanwhile.
                reason = None
                if m.unresolved_holding_sell_count(conn, str(self.CHAIN_ID)):
                    reason = "holding_unresolved"
                elif m.has_unresolved_evm_gas_topup(conn, str(self.CHAIN_ID)):
                    reason = "gas_unresolved"
                if reason:
                    m.release_unsigned_blocked_entry(conn, pending_id, reason)
                    rec["_outcome"] = reason
                    unchanged = True
                    return {"status": "deferred", "reason": reason,
                            "pending_id": pending_id}
                # The hook and inline safety may outlive the snapshot or another execution.
                # Revalidate under the existing lock before claiming a nonce or signing.
                reason = inputs_reason(
                    dict(payload, eth_usd=self._quote_asset_usd(payload),
                         eth_usd_at=payload.get("eth_usd_at", payload.get("bnb_usd_at"))),
                    generation=self.generation_fn(), now_host=self.host_clock(),
                    clip_wei=clip_wei, gas_limit=int(tx["gas"]), low_wei=self.low_wei,
                    max_snapshot_age_s=self.max_snapshot_age_s,
                    eth_usd_max_age_s=self.eth_usd_max_age_s,
                    generation_margin_s=self.generation_margin_s,
                    extra_gas_wei=rec.get("gas_bid_extra_wei", 0))
                if reason is not None:
                    result = abort(reason, reason)
                    unchanged = True  # no nonce claim, signing or send reached
                    return result
                # The leg's pre-sign check (Base 8453: a preconfirmed source proved a phantom refuses,
                # the ordinary path's rule). One local read; 4663 passes none.
                reason = presign_check_fn() if presign_check_fn is not None else None
                if reason is not None:
                    result = abort(reason, reason)
                    unchanged = True
                    return result
                sent = self._send(payload, rec, conn, pending_id, tx, clip_wei, trace)
                if isinstance(sent, dict):
                    return sent
                tx_hash = sent
                receipt = self._receipt(tx_hash, self.lock_receipt_cap_s,
                    **({"receipt_observation": receipt_observation} if observer is not None else {}))
            finally:
                rec["lock_ms"] = (time.perf_counter() - t_lock) * 1000.0
                if not unchanged:
                    try:
                        touch_generation(self.lock_key, lock_dir=self.lock_dir)
                    except Exception as exc:
                        from sniper.execution import exec_lock
                        exec_lock.latch_generation_fault("lane stamp %s: %s: %s" % (
                            self.lock_key, type(exc).__name__, exc))
        if receipt is None:
            self._alert("lane receipt not seen within %.1fs of the send: wallet lock released, "
                        "still waiting (tx %s)" % (self.lock_receipt_cap_s, tx_hash))
            receipt = self._receipt(tx_hash, self.receipt_timeout_s,
                **({"receipt_observation": receipt_observation} if observer is not None else {}))
        if observer is not None and receipt_observation:
            try:
                observer.observed(getattr(trace, "attempt_id", None) or rec.get("attempt_id"),
                    tx_hash, receipt, observed_ns=receipt_observation["monotonic_ns"],
                    boot_id=observer.boot_id)
            except Exception:
                logger.exception("rh fast lane: receipt observation publication failed")
                try:
                    observer.record_gap(dict(reason="confirmation_publication_failed",
                        attempt_id=getattr(trace, "attempt_id", None) or rec.get("attempt_id")))
                except Exception:
                    logger.exception("rh fast lane: receipt publication gap failed")
        return self._settle(payload, rec, conn, pending_id, tx_hash, decimals, receipt, trace)

    # ---- the chain's own pieces (the BSC 56 lane overrides them; spec 2026-10-02 BSC lane) ----

    CHAIN_ID = 4663
    BACKEND = "rh_fast_sequencer"
    RELEASE_PREFIX = "rh_fast_"

    def _quote_asset_usd(self, payload):
        return payload["eth_usd"]

    def _buy_tx(self, payload, clip_wei, deadline, floor=0):
        """(the unsigned buy, its minimum output) at the datagram's cap, by route kind."""
        return lane_buy_tx(payload, clip_wei, deadline, self.taker, floor=floor, chain=str(self.CHAIN_ID))

    def _floor_evidence(self, payload, clip_wei, floor, min_out):
        if lane_cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
            return {"slippage": str(min_out), "copy": "0",
                    "signed_minimum": str(min_out), "binding": ["slippage"],
                    "policy": "quote_slippage"}
        amount = clip_wei
        if route_kind(payload) == "v4_bridge":
            amount = clip_wei * int(payload["a_sqrt"]) ** 2 >> 192
        cap = amount * int(payload["cap_den"]) // int(payload["cap_num"])
        floors = {"cap": cap, "copy": int(floor)}
        return {**{k: str(v) for k, v in floors.items()}, "signed_minimum": str(min_out),
                "binding": [k for k, v in floors.items() if v == min_out]}

    def _expected_out(self, payload, clip_wei):
        """The coin a `clip_wei` buy delivers at the source's post state, when the lane knows it:
        a V4 pool's output needs its liquidity, which the datagram does not carry."""
        return None

    def _copy_floor(self, payload, pending, clip_wei, decimals):
        """(the signed-minimum floor, refusal or None) from MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT over
        the reservation's wallet-net source average at the final clip (Codex EVM-01): the bound the
        ordinary executor's copy_minimum_refusal enforces. Arithmetic only."""
        if lane_cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
            return 0, None
        from sniper.execution.entry_attribution import copy_cap_min_out
        cap = lane_cfg.copy_premium_cap(payload.get("source_user_id"), str(self.CHAIN_ID))[0]
        floor = copy_cap_min_out(clip_wei, self._quote_asset_usd(payload),
                                 (pending or {}).get("source_price"), decimals, cap)
        if floor is None:
            return 0, "copy price cap %.1f%%: source average or quote price unavailable" % cap
        expected = self._expected_out(payload, clip_wei) if floor else None
        if expected is not None and expected < floor:
            return 0, "copy price: expected output %d is under the %.1f%% cap's minimum %d" % (
                expected, cap, floor)
        return floor, None

    def _gas_price(self, snap):
        return int(int(snap["gas_price_wei"]) * float(self.ctx["gasprice_buffer"]))

    def _gas_bid(self, payload, rec, trace, clip_wei, gas_limit):
        """A lane's own gasPrice for this send, or None for today's (_gas_price). 56 overrides it."""
        return None

    def _book(self, conn, pending, receipt, tx_seen, block, payload, decimals):
        from sniper.execution.evm_swap_executor import recover_confirmed_lane_entry
        return recover_confirmed_lane_entry(conn, pending, receipt, tx_seen, block,
                                            owner=self.ctx["address"],
                                            decimals_fn=lambda _mint: int(decimals),
                                            network_id=str(self.CHAIN_ID))

    def _book_unbound(self, conn, pending, receipt, tx_seen, block, decimals):
        """The fallback for a canonical buy the strict receipt rule refused: this transaction's own
        receipt net, whatever sent the coin (Codex EVM-03: never a block's balance delta)."""
        from sniper.execution.evm_swap_executor import recover_confirmed_lane_entry
        return recover_confirmed_lane_entry(conn, pending, receipt, tx_seen, block,
                                            owner=self.ctx["address"],
                                            decimals_fn=lambda _mint: int(decimals),
                                            network_id=str(self.CHAIN_ID), bound=False)

    def _receipt_amounts(self, receipt, tx_seen, payload):
        from sniper.execution.evm_swap_executor import lane_receipt_amounts
        from sniper.execution.evm_v4_quote import V4
        return lane_receipt_amounts(receipt, tx_seen, self.ctx["address"], payload["mint"],
                                    pool_manager=V4[str(self.CHAIN_ID)]["pool_manager"])

    def _post_peak_nominated(self, conn, pending):
        """The reservation's frozen classification nominates post_peak (Alpha invariant 3): today's
        executor needs an exact quote to confirm or clear it, and the lane has none."""
        from sniper.persistence import db_memecoin as m
        from sniper.services.memecoin_market_state import confirm_post_peak
        contract = m.validated_pending_funded_contract(conn, pending)
        if not contract:
            return False
        classification = json.loads(contract["funded_contract_json"]).get("classification")
        return confirm_post_peak(classification or {}, exact_quote_price=None).nominated

    def _send(self, payload, rec, conn, pending_id, tx, clip_wei, trace):
        """Under the lock: nonce, signature, write-ahead identity, one send. Returns the tx hash
        when the bytes may have landed, else the final result (reservation and nonce given back).
        Shared signed owners survive unknown delivery, including failures before provider I/O."""
        from sniper.execution.submission_transport import sha256_hex
        from sniper.persistence import db_memecoin as m
        transport = getattr(self, "submission_transport", None)
        snap = payload["snapshot"]
        nonce = self.ctx["lane_claim_nonce_fn"](snap.get("nonce"))
        if nonce is None:
            m.release_pending(conn, pending_id, self.RELEASE_PREFIX + "nonce_in_flight")
            rec["_outcome"] = "nonce_in_flight"
            return {"status": "aborted", "reason": "nonce_in_flight", "pending_id": pending_id}
        broadcast, bound = False, False

        def give_back(outcome, why, status="aborted", **fields):
            self.ctx["lane_release_nonce_fn"](nonce)
            m.release_pending(conn, pending_id, self.RELEASE_PREFIX + outcome.split(":")[0])
            rec["_outcome"] = outcome
            return {"status": status, "reason": why, "pending_id": pending_id, **fields}
        try:
            signed = self.ctx["sign_tx_fn"]({"to": tx["to"], "data": tx["data"], "value": clip_wei,
                                             "gas": int(tx["gas"]),
                                             "gasPrice": rec.get("gas_bid_wei") or self._gas_price(snap),
                                             "nonce": nonce, "chainId": self.CHAIN_ID})
            if not signed:
                return give_back("sign_failed", "sign failed")
            raw, tx_hash = signed
            rec["tx_hash"] = tx_hash
            if not m.bind_entry_submission(
                    conn, pending_id, tx_identity=tx_hash, signed_payload_b64=raw,
                    signed_payload_sha256=sha256_hex(raw), blockhash=None, last_valid_height=None,
                    nonce=nonce, backend=self.BACKEND, bound_at=_utc_now_iso(),
                    evm_quote_asset_usd=float(self._quote_asset_usd(payload))):
                if transport is not None:
                    rec["_outcome"] = "binding_conflict"
                    return {"status": "unknown", "reason": "submission binding conflict",
                            "pending_id": pending_id, "tx_hash": tx_hash}
                return give_back("binding_conflict", "submission binding conflict")
            bound = True
            request, submitted = None, None
            if transport is not None:
                from sniper.execution.submission_transport import AttemptState, build_request
                from sniper.execution.submission_wiring import (
                    _record_fn, evm_raw_chain_id, evm_tx_identity, guarded_submit,
                )
                try:
                    identity = evm_tx_identity(raw)
                    if identity != tx_hash or evm_raw_chain_id(raw) != self.CHAIN_ID:
                        raise ValueError("signed identity or chain mismatch")
                except (TypeError, ValueError):
                    # The immutable signed owner survives even malformed signer output.
                    rec["_outcome"] = "signed_identity_invalid"
                    return {"status": "unknown", "reason": "signed identity or chain mismatch",
                            "pending_id": pending_id, "tx_hash": tx_hash}
                request = build_request(
                    chain_id=str(self.CHAIN_ID), action="entry", tx_identity=identity,
                    signed_payload=raw, nonce=nonce, owner_kind="pending", owner_id=str(pending_id),
                    created_at=_utc_now_iso())
                record = _record_fn(
                    conn, chain_id=str(self.CHAIN_ID), action="entry", owner_kind="pending",
                    owner_id=str(pending_id), identity=identity,
                    sha256=request.signed_payload_sha256, now_fn=_utc_now_iso, async_write=True)
            t_send = time.perf_counter()
            t_send_ns = time.monotonic_ns()
            broadcast = True
            observer = self.ctx.get("bsc_execution_observer")
            if observer is not None:
                try:
                    observer.dispatch(getattr(trace, "attempt_id", None) or rec.get("attempt_id"),
                                      tx_hash, payload.get("receipt_clock"), route_lane="native",
                                      venue=payload.get("route_kind", "v2_native"),
                                      quote_asset=payload.get("quote"), ready_at_original_ingress=(
                                          (rec.get("execution_evidence") or {}).get("safety_warmup")
                                          or {}).get("ready_at_original_ingress"))
                except Exception:
                    pass
            if trace is not None:
                try:
                    trace.mark_substage("evm.sender_dispatch", attempt_no=1, tx_sig=tx_hash)
                except Exception:
                    pass
            if request is not None:
                submitted = guarded_submit(transport, request, record_fn=record)
                kind = ("accepted" if submitted.state is AttemptState.ACCEPTED else
                        "rejected" if submitted.state is AttemptState.EXPLICIT_REJECTED else "ambiguous")
                detail = submitted.message
            else:
                kind, detail = self._post(raw, payload)
                rec.setdefault("execution_evidence", {}).update(self._send_evidence(payload))
        except Exception:
            if not broadcast and not (bound and transport is not None):
                try:
                    give_back("error", "raised before the send")
                except Exception:
                    logger.exception("rh fast lane: give-back after a raise failed")
            raise
        rec["send_ms"] = (time.perf_counter() - t_send) * 1000.0
        rec["decide_to_send_ms"] = (t_send - rec.get("_perf0", t_send)) * 1000.0
        self.send_times.append(self.host_clock())
        rec["send_reply"] = kind
        # Every send's own clock (spec 2026-10-14 rh-fast-cached-only §3.3): feed frame to bytes on
        # the wire on the boot monotonic clock (None when absent or from another boot), and who took it.
        from sniper.util.receipt_clock import elapsed_receipt_ms
        rec["feed_to_send_ms"] = elapsed_receipt_ms(payload.get("receipt_clock"), t_send_ns)
        rec["accepted_by"] = submitted.accepted_by if submitted is not None else None
        if trace is not None:
            try:
                trace.mark_substage("rh_fast_lane_send", reply=kind, send_ms=rec["send_ms"],
                                    decide_to_send_ms=rec["decide_to_send_ms"], attempt_no=1, tx_sig=tx_hash,
                                    **({"accepted_by": submitted.accepted_by,
                                        "provider_states": {a.provider_id: a.state.value
                                                            for a in submitted.attempts}}
                                       if submitted is not None else {}))
            except Exception:
                pass                                   # a trace mark never decides an entry
        if kind == "rejected":                         # proven not admitted: the nonce is free
            return give_back("rejected:%s" % str(detail)[:80], "rejected: %s" % detail,
                             tx_hash=tx_hash)
        if kind == "ambiguous" and transport is None and self._fanout_allowed(payload):
            try:
                self.fanout_fn(raw)                    # the SAME bytes through today's send
            except Exception:
                logger.exception("rh fast lane: fan-out of an ambiguous send failed")
        return tx_hash

    def _post(self, raw, payload):
        """The signed bytes to the lane's sender (the BSC lane adds its bundle's expiry)."""
        return self.sender.send(raw)

    def _send_evidence(self, payload):
        """Send-set facts for the attempt's evidence (the BSC lane names its members)."""
        return {}

    def _fanout_allowed(self, payload):
        """Whether an ambiguous send is re-offered through fanout_fn (the BSC cap floor says no)."""
        return True

    def _settle(self, payload, rec, conn, pending_id, tx_hash, decimals, receipt, trace):
        """The booking, outside the lock: the receipt, the revert, the receipt booking or the
        balance-delta fallback, and the fill check."""
        from sniper.persistence import db_memecoin as m

        def result(outcome, status, why, **fields):
            rec["_outcome"] = outcome
            return {"status": status, "reason": why, "pending_id": pending_id, "tx_hash": tx_hash,
                    **fields}
        if receipt is None:
            return result("submitted_unknown", "unknown",
                          "submitted_unknown: no receipt within %.0fs" % self.receipt_timeout_s)
        from sniper.execution.evm_swap_executor import _receipt_is_canonical, receipt_status
        state = receipt_status(receipt) if _names(receipt, tx_hash) else None
        if _names(receipt, tx_hash):
            logs = receipt.get("logs")
            rec.setdefault("execution_evidence", {})["public_receipt"] = {
                **{key: receipt.get(key) for key in ("transactionHash", "blockHash", "blockNumber",
                                                   "transactionIndex", "status", "gasUsed")},
                "logs": logs[:128] if isinstance(logs, list) else None,
                "logs_truncated": len(logs) > 128 if isinstance(logs, list) else None}
        if state is None:
            # Codex EVM-04: a partial, malformed or foreign receipt proves nothing. The signed bytes
            # may still land, so the reservation and its binding stay for the stale sweep.
            self._alert("lane receipt for tx %s has no recognized status: kept bound" % tx_hash)
            return result("receipt_unknown", "unknown", "receipt without a recognized status")
        try:
            number = int(str(receipt["blockNumber"]), 0)
            rec["blocks_behind"] = number - int(payload["seq"])
            rec["held"] = int(rec["send_ms"] > 120.0)
            # Gamma's pilot stop rule H4 reads the block around our fill: where our buy sits in it.
            rec["our_block"] = number
            if receipt.get("transactionIndex") is not None:
                rec["our_tx_index"] = int(str(receipt["transactionIndex"]), 0)
        except (KeyError, TypeError, ValueError, OverflowError):
            self._alert("lane receipt for tx %s has malformed block metadata: kept bound" % tx_hash)
            return result("receipt_unknown", "unknown", "receipt with malformed block metadata")
        mint = str(payload["mint"]).lower()
        tx_seen = self.rpc_read("eth_getTransactionByHash", [tx_hash])
        block = self.rpc_read("eth_getBlockByNumber", [receipt["blockNumber"], False])
        if state == 0:
            # Only our own bound transaction's status 0, in its canonical block, gives the
            # reservation back; its class is the replay's, never assumed min-out.
            try:
                canonical = bool(tx_seen and block and _receipt_is_canonical(
                    m.pending_row(conn, pending_id), receipt, tx_seen, block,
                    self.ctx["address"], status=0))
            except (KeyError, TypeError, ValueError, OverflowError):
                canonical = False
            if not canonical:
                self._alert("lane receipt status 0 for tx %s is not proven canonical: kept bound"
                            % tx_hash)
                return result("receipt_unknown", "unknown", "failed receipt not proven canonical")
            if not m.reset_entry_submission_after_failed_receipt(
                    conn, pending_id, tx_hash, release_reason=self.RELEASE_PREFIX + "reverted"):
                self._alert("lane failed receipt for tx %s has a submission reset conflict: kept bound"
                            % tx_hash)
                return result("receipt_unknown", "unknown", "submission reset conflict")
        if trace is not None:
            # Both receipt statuses landed; record the already-held block before any replay.
            stamp = None
            try:
                candidate = int(str((block or {}).get("timestamp")), 0)
                if candidate > 0:
                    stamp = candidate
            except (AttributeError, TypeError, ValueError, OverflowError):
                pass
            try:
                trace.mark_substage("rh_fast_lane_receipt", block=number, block_time=stamp,
                                    attempt_no=1, tx_sig=tx_hash)
            except Exception:
                pass
        if state == 0:
            kind = self._revert_kind(tx_hash, rec)
            return result("reverted", "failed",
                          "reverted_min_out" if kind == "min_out" else "reverted_unknown",
                          revert_kind=kind)
        booked, outcome = None, "filled"
        if tx_seen and block:
            booked = self._book(conn, m.pending_row(conn, pending_id), receipt, tx_seen, block,
                                payload, decimals)
        if booked:
            amounts = self._receipt_amounts(receipt, tx_seen, payload)
        elif tx_seen and block:
            # Gamma MEDIUM: a confirmed buy the strict receipt rule refused is a real holding. Its
            # own receipt's net books it; a block's balance delta never does (Codex EVM-03: it
            # carries every other transaction in the block).
            from sniper.execution.evm_swap_executor import lane_receipt_amounts
            booked = self._book_unbound(conn, m.pending_row(conn, pending_id), receipt, tx_seen,
                                        block, decimals)
            if booked:
                outcome = "filled_from_receipt_net"
                amounts = lane_receipt_amounts(receipt, tx_seen, self.ctx["address"], mint,
                                               pool_manager=None)
                self._alert("booked from tx %s's own receipt net (%s atomic): the strict receipt "
                            "rule refused its coin's source" % (tx_hash, amounts[0]))
        if not booked:
            self._alert("CONFIRMED lane buy NOT booked, held for reconcile: pending=%s tx=%s "
                        "mint=%s" % (pending_id, tx_hash, mint))
            return result("booking_held", "unknown", "confirmed but not booked: held for reconcile")
        observer = self.ctx.get("bsc_execution_observer")
        if observer is not None:
            try:
                observer.booked(getattr(trace, "attempt_id", None) or rec.get("attempt_id"),
                                tx_hash, receipt, booked["position_id"])
            except Exception:
                pass
        rec["got_atomic"], rec["paid_wei"] = amounts            # the fill price, exact (H1, E1)
        self._start_check(tx_hash, booked["position_id"], mint, number, amounts[0])
        self._start_preapproval(conn, booked["position_id"], payload)
        self.fills += 1
        return result(outcome, "filled", outcome, position_id=booked["position_id"],
                      notional=booked.get("notional"))

    def _start_preapproval(self, conn, position_id, payload):
        """The token pre-approval the ordinary V4 entry starts through booked_fn (spec 2026-09-30
        4663 approve at entry), for a lane fill (Gamma 2026-10-08: pos 71780's stop paid approve_n=1
        / 450 ms). After the booking commit, on its own daemon thread, never failing the fill.
        Same knobs: MEMECOIN_EVM_PREAPPROVE_CHAINS and the chain's approval pipeline (the shared
        nonce floor). The lane's own buys are V4 swaps, so it names the route protocol."""
        chain = str(self.CHAIN_ID)
        fn = self.ctx.get("preapprove_position_fn")
        try:
            # The ordinary entry's rule (evm_real_seams booked_fn): a pre-approval chain whose
            # signers share the nonce floor, pipelined (4663) or receipt-first (8453, 1).
            if (fn is None or route_kind(payload) not in ROUTE_KINDS
                    or chain not in lane_cfg.memecoin_evm_preapprove_chains()
                    or not (lane_cfg.memecoin_evm_approval_pipeline(chain) or chain in ("1", "8453"))):
                return
            db_path = conn.execute("PRAGMA database_list").fetchone()[2]
            if not db_path:
                return                    # in-memory ledger: nothing another connection can see

            def run():
                try:
                    fn(db_path, position_id, route_protocol="uniswap_v4")
                except Exception:
                    logger.exception("fast lane: pre-approval failed chain=%s pos=%s", chain,
                                     position_id)
            threading.Thread(target=run, name="rh-lane-preapprove-%s" % position_id,
                             daemon=True).start()
        except Exception:
            logger.exception("fast lane: pre-approval not started for pos=%s", position_id)

    def _revert_kind(self, tx_hash, rec=None):
        """A released revert's class from the replay, waited for at most REVERT_CLASS_WAIT_S (Gamma
        LOW: the replay is 3+ custody-host calls at a 10 s timeout, and the lane thread must not sit
        on them). Past the wait the label is unknown and the worker logs the class when it lands."""
        if self.revert_reason_fn is None:
            return "unknown"
        box, gave_up = {}, threading.Event()

        def classify():
            try:
                box["kind"], box["detail"] = self.revert_reason_fn(tx_hash) or ("unknown", None)
            except Exception:
                box["kind"] = "unknown"
            if gave_up.is_set():
                logger.warning("rh fast lane: revert class of %s landed late: %s", tx_hash, box["kind"])
        # ponytail: one daemon per revert; a hung one ends with its HTTP timeouts (reverts are rare).
        worker = threading.Thread(target=classify, name="rh-fast-revert-class", daemon=True)
        worker.start()
        worker.join(REVERT_CLASS_WAIT_S)
        gave_up.set()
        if rec is not None:
            # The worker owns box only; a late replay never mutates a returned record.
            detail = box.get("detail")
            basis = "unknown"
            if isinstance(detail, str) and detail.startswith("replay_basis="):
                basis = detail.split(";", 1)[0].split("=", 1)[1]
            rec.setdefault("execution_evidence", {}).update(replay_basis=basis, revert_detail=detail)
        return box.get("kind", "unknown")

    def _alert(self, text):
        try:
            self.alert_fn(text)
        except Exception:
            logger.exception("rh fast lane: alert failed: %s", text)

    def _delta(self, mint, number):
        """The coin's balance change in block `number` (block-pinned), or None when unreadable."""
        if self.balance_at_fn is None:
            return None
        try:
            after, before = self.balance_at_fn(mint, number), self.balance_at_fn(mint, number - 1)
        except Exception:
            return None
        return None if after is None or before is None else int(after) - int(before)

    def _start_check(self, tx_hash, position_id, mint, number, receipt_got):
        """Alpha invariant 1, OFF the fill path: after the booking, compare the receipt's quantity
        with the block-pinned balance delta; a disagreement (a rebasing coin, or another transfer in
        the same block) is recorded and alerted, never silently resolved. It holds nothing: the
        position stands as its receipt booked it."""
        def check():
            delta = self._delta(mint, number)
            agree = delta is not None and delta == receipt_got
            row = {"tx_hash": tx_hash, "position_id": position_id, "receipt_got": receipt_got,
                   "delta_got": delta, "agree": agree}
            if not agree:
                self._alert("receipt and balance delta disagree on position %s (receipt %s, delta "
                            "%s): booked from the receipt; reconcile by hand if the wallet "
                            "disagrees" % (position_id, receipt_got, delta))
            if self.check_sink is not None:
                try:
                    self.check_sink(row)
                except Exception:
                    logger.exception("rh fast lane: fill check record failed")
        thread = threading.Thread(target=check, name="rh-fast-fill-check", daemon=True)
        self._checks.append(thread)
        thread.start()

    def join_checks(self, timeout=5.0):
        for thread in self._checks:
            thread.join(timeout)

    def _bounded_read(self, method, params, deadline):
        """One custody read answered by `deadline` or abandoned (Codex EVM-06: the synchronous read,
        not just the poll loop, outlived the lock cap; its HTTP timeout is 10 s and the pool may hedge
        or fall back past that). The read runs on one daemon thread (Gamma: a non-daemon worker
        would hold a clean runner stop until its HTTP timeout). While an abandoned read is still
        out, a poll for the same request joins it and any other request gets None, so timeouts
        never multiply threads; an answer that lands late is used by the next poll at most."""
        key = (method, json.dumps(params, sort_keys=True))
        with self._read_lock:
            job = self._read_job
            if (job is not None and job[0] == key and job[1]["done"].is_set()
                    and job[1]["value"] is not None):
                # THE LATE ANSWER (Codex EVM-02): consumed once, never re-asked. A finished
                # foreign or empty job is replaced below.
                self._read_job = None
                return job[1]["value"]
            if job is None or job[1]["done"].is_set():
                box = {"done": threading.Event(), "value": None}

                def run():
                    try:
                        box["value"] = self.rpc_read(method, params)
                    except Exception:
                        box["value"] = None
                    finally:
                        box["done"].set()
                self._read_thread = threading.Thread(target=run, name="lane-custody-read",
                                                     daemon=True)
                job = (key, box)
                self._read_job = job
                self._read_thread.start()
            elif job[0] != key:
                return None
        if not job[1]["done"].wait(max(0.0, deadline - time.monotonic())):
            return None
        with self._read_lock:
            if self._read_job is job:
                self._read_job = None
        return job[1]["value"]

    def _receipt(self, tx_hash, timeout_s, *, receipt_observation=None):
        """Our transaction's receipt with a recognized status, or None by the deadline: a partial or
        foreign reply is read past, never returned (Codex EVM-04). Every read is bounded by the same
        deadline (Codex EVM-06)."""
        from sniper.execution.evm_swap_executor import receipt_status
        deadline = time.monotonic() + timeout_s
        while True:
            receipt = self._bounded_read("eth_getTransactionReceipt", [tx_hash], deadline)
            if receipt and _names(receipt, tx_hash) and receipt_status(receipt) is not None:
                if receipt_observation is not None and receipt_status(receipt) == 1:
                    receipt_observation.setdefault("monotonic_ns", time.monotonic_ns())
                return receipt
            if time.monotonic() >= deadline:
                return None
            self.sleep(self.receipt_poll_s)


def _revert_reason(tx_hash, rpc_url):
    """A mined revert's (kind, detail) by the funded executor's own replay on the custody host."""
    from sniper.execution.evm_real_seams import evm_revert_reason
    return evm_revert_reason(tx_hash, rpc_url)


def _names(receipt, tx_hash):
    """The receipt is for `tx_hash` (a provider answer for another transaction proves nothing)."""
    return str((receipt or {}).get("transactionHash") or "").lower() == str(tx_hash).lower()


def _utc_now_iso():
    from sniper.util.clock import real_utc_now_iso
    return real_utc_now_iso()


_EVENTS = """
CREATE TABLE IF NOT EXISTS rh_fast_lane_events (
    id INTEGER PRIMARY KEY, tx_hash TEXT, mode TEXT, mint TEXT, pool TEXT, reason TEXT,
    sent_epoch REAL, recv_epoch REAL, handoff_ms REAL, busy_start_epoch REAL, busy_end_epoch REAL,
    busy_ms REAL, cpu_ms REAL, lock_busy INTEGER, clip_wei TEXT, min_out TEXT, tx_bytes INTEGER,
    written_at TEXT, safety_path TEXT, safety_ms REAL);
CREATE TABLE IF NOT EXISTS rh_fast_lane_drop_counts (
    at TEXT NOT NULL, reason TEXT NOT NULL, n INTEGER NOT NULL);
"""


def write_record(db_path, rec):
    """One lane event into the lane's own SQLite file (never the funded ledger)."""
    import sqlite3
    from sniper.util.clock import real_utc_now_iso
    db = sqlite3.connect(db_path, timeout=2)
    try:
        db.executescript(_EVENTS)
        # Cache or inline per signal (Gamma's cache-miss read, 2026-10-02): a lane db written
        # before these columns gets them on its first write; NULL when the signal never reached
        # the safety step.
        have = {r[1] for r in db.execute("PRAGMA table_info(rh_fast_lane_events)")}
        for name, kind in (("safety_path", "TEXT"), ("safety_ms", "REAL"), ("route_kind", "TEXT"),
                           ("parallel_safety", "TEXT"), ("pool_choice", "TEXT")):
            if name not in have:
                db.execute("ALTER TABLE rh_fast_lane_events ADD COLUMN %s %s" % (name, kind))
        db.execute(
            "INSERT INTO rh_fast_lane_events (tx_hash, mode, mint, pool, reason, sent_epoch, "
            "recv_epoch, handoff_ms, busy_start_epoch, busy_end_epoch, busy_ms, cpu_ms, lock_busy, "
            "clip_wei, min_out, tx_bytes, written_at, safety_path, safety_ms, route_kind, "
            "parallel_safety, pool_choice) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["tx_hash"], rec.get("mode"), rec["mint"], rec.get("pool"), rec["reason"],
             rec["sent_epoch"], rec["recv_epoch"], rec["handoff_ms"], rec["busy_start_epoch"],
             rec["busy_end_epoch"], rec["busy_ms"], rec["cpu_ms"], rec["lock_busy"],
             str(rec["clip_wei"]), None if rec["min_out"] is None else str(rec["min_out"]),
             rec["tx_bytes"], real_utc_now_iso(), rec.get("safety_path"), rec.get("safety_ms"),
             rec.get("route_kind"), rec.get("parallel_safety"), rec.get("pool_choice")))
        db.commit()
    finally:
        db.close()


class Drops:
    """Dropped datagrams, counted in memory and flushed together (Gamma LOW: the abstract name is
    reachable by any local process, so a drop must not cost the runner a SQLite write)."""

    def __init__(self):
        self.counts = collections.Counter()

    def add(self, reason):
        self.counts[reason] += 1

    def flush(self, db_path):
        if not self.counts:
            return
        import sqlite3
        from sniper.util.clock import real_utc_now_iso
        counts, self.counts = self.counts, collections.Counter()
        db = sqlite3.connect(db_path, timeout=2)
        try:
            db.executescript(_EVENTS)
            now = real_utc_now_iso()
            db.executemany("INSERT INTO rh_fast_lane_drop_counts VALUES (?,?,?)",
                           [(now, reason, n) for reason, n in sorted(counts.items())])
            db.commit()
        finally:
            db.close()


def bind_or_log(address, state, log):
    """The bound socket, or None while another process holds the name (a deploy overlap); the
    first refusal is logged once (Gamma LOW), and binding is retried by the caller."""
    try:
        sock = LaneSocket(address)
    except OSError as exc:
        if not state.get("logged"):
            log("[rh-fast-lane] lane socket name in use (%s); retrying every 1 s" % exc)
            state["logged"] = True
        return None
    state["logged"] = False
    return sock


_LIVE = """
CREATE TABLE IF NOT EXISTS rh_fast_lane_live (
    id INTEGER PRIMARY KEY, source_tx TEXT, our_tx TEXT, mint TEXT, reason TEXT, pending_id INTEGER,
    position_id INTEGER, blocks_behind INTEGER, send_ms REAL, decide_to_send_ms REAL,
    send_reply TEXT, held INTEGER, lock_ms REAL, our_block INTEGER, our_tx_index INTEGER,
    paid_wei TEXT, got_atomic TEXT, safety_path TEXT, safety_ms REAL, written_at TEXT);
CREATE TABLE IF NOT EXISTS rh_fast_lane_fill_checks (
    id INTEGER PRIMARY KEY, our_tx TEXT, position_id INTEGER, receipt_got TEXT, delta_got TEXT,
    agree INTEGER, written_at TEXT);
"""


LIVE_COLUMNS = (("copy_floor", "TEXT"), ("initiation", "TEXT"), ("execution_evidence_json", "TEXT"),
                ("feed_to_send_ms", "REAL"), ("accepted_by", "TEXT"), ("state_class", "TEXT"))


def send_counters(db, *, window="-1 day"):
    """The last day's sends by state class (sends, reverted) and feed-to-send p50/p90 over the sends
    that carried a same-boot receipt clock (spec 2026-10-14 rh-fast-cached-only §3.3). Heartbeat
    only; never on the fill path."""
    rows = db.execute("SELECT state_class, reason, feed_to_send_ms FROM rh_fast_lane_live "
                      "WHERE send_reply IS NOT NULL AND written_at >= strftime('%Y-%m-%dT%H:%M:%S', "
                      "'now', ?)", (window,)).fetchall()
    by = {}
    for cls, reason, _ms in rows:
        slot = by.setdefault(cls or "unknown", {"sends": 0, "reverted": 0})
        slot["sends"] += 1
        slot["reverted"] += int(reason == "reverted")
    ms = sorted(m for _c, _r, m in rows if m is not None)

    def pick(f):
        return ms[min(len(ms) - 1, int(len(ms) * f))] if ms else None
    return {"by_class": by, "feed_to_send_ms": {"n": len(ms), "p50": pick(.5), "p90": pick(.9)}}


def write_live_record(db_path, rec):
    """The live lane's own outcome for one signal (the shared event row carries the timings)."""
    import sqlite3
    from sniper.util.clock import real_utc_now_iso
    db = sqlite3.connect(db_path, timeout=2)
    try:
        db.executescript(_LIVE)
        add_missing_columns(db, "rh_fast_lane_live", LIVE_COLUMNS)
        db.execute("INSERT INTO rh_fast_lane_live (source_tx, our_tx, mint, reason, pending_id, "
                   "position_id, blocks_behind, send_ms, decide_to_send_ms, send_reply, held, "
                   "lock_ms, our_block, our_tx_index, paid_wei, got_atomic, safety_path, safety_ms, "
                   "copy_floor, initiation, written_at, execution_evidence_json, feed_to_send_ms, "
                   "accepted_by, state_class) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rec.get("source_tx"), rec.get("tx_hash"), rec.get("mint"), rec.get("reason"),
                    rec.get("pending_id"), rec.get("position_id"), rec.get("blocks_behind"),
                    rec.get("send_ms"), rec.get("decide_to_send_ms"), rec.get("send_reply"),
                    rec.get("held"), rec.get("lock_ms"), rec.get("our_block"),
                    rec.get("our_tx_index"),
                    None if rec.get("paid_wei") is None else str(rec["paid_wei"]),
                    None if rec.get("got_atomic") is None else str(rec["got_atomic"]),
                    rec.get("safety_path"), rec.get("safety_ms"),
                    None if rec.get("copy_floor") is None else str(rec["copy_floor"]),
                    rec.get("initiation"), real_utc_now_iso(),
                    json.dumps(rec.get("execution_evidence"), sort_keys=True),
                    rec.get("feed_to_send_ms"), rec.get("accepted_by"), rec.get("state_class")))
        db.commit()
    finally:
        db.close()


def fill_check_sink(db_path):
    def sink(row):
        import sqlite3
        from sniper.util.clock import real_utc_now_iso
        db = sqlite3.connect(db_path, timeout=2)
        try:
            db.executescript(_LIVE)
            db.execute("INSERT INTO rh_fast_lane_fill_checks (our_tx, position_id, receipt_got, "
                       "delta_got, agree, written_at) VALUES (?,?,?,?,?,?)",
                       (row["tx_hash"], row["position_id"], str(row["receipt_got"]),
                        None if row["delta_got"] is None else str(row["delta_got"]),
                        int(bool(row["agree"])), real_utc_now_iso()))
            db.commit()
        finally:
            db.close()
    return sink


_STATS_CHECKS = """
CREATE TABLE IF NOT EXISTS rh_fast_lane_stats_checks (
    id INTEGER PRIMARY KEY, source_tx TEXT, mint TEXT, cached_age_s REAL, cached_verdict TEXT,
    fresh_verdict TEXT, differ INTEGER, written_at TEXT);
CREATE TABLE IF NOT EXISTS rh_fast_lane_dex (
    at TEXT NOT NULL, calls INTEGER, http_429 INTEGER, stopped TEXT, stats_ok INTEGER,
    stats_missing INTEGER);
"""


def stats_check_sink(db_path):
    """Where runner_shadow records each fresh-stats re-judgement (Alpha: Gamma reads the
    disagreement rate before live). A fresh GET that returned nothing has no verdict and no
    `differ`: absent, never a match."""
    def sink(row):
        import sqlite3
        from sniper.util.clock import real_utc_now_iso
        db = sqlite3.connect(db_path, timeout=2)
        try:
            db.executescript(_STATS_CHECKS)
            db.execute("INSERT INTO rh_fast_lane_stats_checks (source_tx, mint, cached_age_s, "
                       "cached_verdict, fresh_verdict, differ, written_at) VALUES (?,?,?,?,?,?,?)",
                       (row["source_tx"], row["mint"], row["cached_age_s"], row["cached_verdict"],
                        row["fresh_verdict"], row["differ"], real_utc_now_iso()))
            db.commit()
        finally:
            db.close()
    return sink


def write_dex_counts(db_path, dex, cache):
    """The lane's DexScreener budget, once a minute (cumulative; counters restart with the process)."""
    import sqlite3
    from sniper.util.clock import real_utc_now_iso
    db = sqlite3.connect(db_path, timeout=2)
    try:
        db.executescript(_STATS_CHECKS)
        db.execute("INSERT INTO rh_fast_lane_dex VALUES (?,?,?,?,?,?)", (
            real_utc_now_iso(), dex.counts["calls"], dex.counts["http_429"], dex.stopped,
            cache.counts["stats_ok"], cache.counts["stats_missing"]))
        db.commit()
    finally:
        db.close()


_STATE = """
CREATE TABLE IF NOT EXISTS rh_fast_lane_state (
    at TEXT NOT NULL, generation_fault INTEGER NOT NULL, reason TEXT);
"""


def write_lane_state(db_path):
    """Whether the lane can act at all, once a minute: a latched generation fault keeps it off until
    a restart (every signal falls back with generation_unknown), and the row says why."""
    import sqlite3
    from sniper.execution import exec_lock
    from sniper.util.clock import real_utc_now_iso
    db = sqlite3.connect(db_path, timeout=2)
    try:
        db.executescript(_STATE)
        db.execute("INSERT INTO rh_fast_lane_state VALUES (?,?,?)", (
            real_utc_now_iso(), int(exec_lock.generation_fault()),
            exec_lock.generation_fault_reason()))
        db.commit()
    finally:
        db.close()


class LatchWatch:
    """The lane's one loud line when the generation fault latches (Alpha 2026-10-01): a runner log
    line and one alert, the first time the lane sees it. `check()` is True only that once."""

    def __init__(self, *, log, alert_fn=None):
        self.log, self.alert_fn, self.seen = log, alert_fn, False

    def check(self):
        from sniper.execution import exec_lock
        if self.seen or not exec_lock.generation_fault():
            return False
        self.seen = True
        text = ("4663 fast lane OFF until restart: generation stamp fault (%s); every signal falls "
                "back to today's path" % exec_lock.generation_fault_reason())
        self.log("[rh-fast-lane] " + text)
        if self.alert_fn is not None:
            try:
                self.alert_fn(text)
            except Exception:
                logger.exception("rh fast lane: latch alert failed")
        return True


_BEAT = """
CREATE TABLE IF NOT EXISTS rh_fast_lane_heartbeat (
    at TEXT NOT NULL, candidates_msgs INTEGER, signals INTEGER, cache_size INTEGER,
    verdicts_ok INTEGER, verdicts_unsafe INTEGER, verdict_errors INTEGER, safety_cpu_ms REAL,
    research_calls INTEGER, research_429 INTEGER,
    inline_calls INTEGER, inline_429 INTEGER, research_slot INTEGER, research_slots TEXT,
    inline_transport_errors INTEGER, inline_dead_marked INTEGER, inline_dead INTEGER);
"""


def rotation_columns(rotation):
    """`(active slot, JSON [{slot, calls, quota, spent}])` for a heartbeat, (None, None) without a
    rotation or a research key. Slot numbers and counts only, never a URL (a key rides in it)."""
    if rotation is None or not rotation.slots:
        return None, None
    active, slots = rotation.stats()
    return active, json.dumps(slots, sort_keys=True)


def add_missing_columns(db, table, columns):
    """ALTER TABLE ADD COLUMN for each `(name, type)` an older db lacks (one writer per db)."""
    have = {r[1] for r in db.execute("PRAGMA table_info(%s)" % table)}
    for name, kind in columns:
        if name not in have:
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, kind))


def _send_counters_json(db):
    """send_counters for the heartbeat, NULL while the lane db has no live table yet."""
    import sqlite3
    try:
        return json.dumps(send_counters(db), sort_keys=True)
    except sqlite3.OperationalError:   # no rh_fast_lane_live yet (a shadow lane), or an older one
        return None


def write_heartbeat(db_path, dispatch, cache, research, inline=None, rotation=None):
    """The lane's cumulative counters, once a minute (counters restart with the process). `inline`
    is the inline host's pacer: its calls and 429s sit beside the research pacer's, NULL when there
    is no inline host (unknown, never a 0). `rotation` adds the research key's active slot and
    per-slot calls and quota answers."""
    import sqlite3
    from sniper.util.clock import real_utc_now_iso
    db = sqlite3.connect(db_path, timeout=2)
    try:
        db.executescript(_BEAT)
        add_missing_columns(db, "rh_fast_lane_heartbeat", (     # a lane db written before them
            ("inline_calls", "INTEGER"), ("inline_429", "INTEGER"),
            ("research_slot", "INTEGER"), ("research_slots", "TEXT"),
            ("inline_transport_errors", "INTEGER"), ("inline_dead_marked", "INTEGER"),
            ("inline_dead", "INTEGER"), ("inline_quota", "INTEGER"),
            ("discovery_attempted", "INTEGER"), ("discovery_completed", "INTEGER"),
            ("discovery_unavailable", "INTEGER"), ("discovery_deadline", "INTEGER"),
            ("discovery_queue_size", "INTEGER"), ("discovery_completion_age_s", "REAL"),
            ("discovery_last_completion", "TEXT"), ("prebuy_counters", "TEXT"),
            ("warm_tiers", "TEXT"), ("send_counters", "TEXT"), ("inline_warm", "TEXT"),
            ("lane_holds", "TEXT"), ("inline_hosts", "TEXT")))
        prebuy = getattr(dispatch, "prebuy", None)       # the Relay pre-buy worker, knob on only
        if prebuy is not None:
            try:
                prebuy.flush_notes(db_path)
            except Exception:
                logger.exception("relay prebuy: source fill notes not written")
        slot, slots = rotation_columns(rotation)
        preparation = getattr(dispatch, "preparation", None)
        status = preparation.status() if preparation is not None else {}
        db.execute("INSERT INTO rh_fast_lane_heartbeat (at, candidates_msgs, signals, cache_size, "
                   "verdicts_ok, verdicts_unsafe, verdict_errors, safety_cpu_ms, research_calls, "
                   "research_429, inline_calls, inline_429, research_slot, research_slots, "
                   "inline_transport_errors, inline_dead_marked, inline_dead, inline_quota, "
                   "discovery_attempted, discovery_completed, discovery_unavailable, "
                   "discovery_deadline, discovery_queue_size, discovery_completion_age_s, "
                   "discovery_last_completion, prebuy_counters, warm_tiers, send_counters, inline_warm, "
                   "lane_holds, inline_hosts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            real_utc_now_iso(), dispatch.counts["candidates_msgs"], dispatch.counts["signals"],
            cache.size(), cache.counts["verdicts_ok"], cache.counts["verdicts_unsafe"],
            cache.counts["verdict_errors"], round(cache.counts["cpu_ms"], 3),
            research.counts["calls"], research.counts["http_429"],
            None if inline is None else inline.counts["calls"],
            None if inline is None else inline.counts["http_429"], slot, slots,
            None if inline is None else inline.counts["transport_errors"],
            None if inline is None else inline.counts["dead_marked"],
            None if inline is None else int(inline.dead()),
            None if inline is None else int(inline.quota_spent()),
            status.get("attempted"), status.get("completed"), status.get("unavailable"),
            status.get("deadline"), status.get("queue_size"), status.get("completion_age_s"),
            json.dumps(status["last_completion"]) if status.get("last_completion") else None,
            None if prebuy is None else json.dumps(dict(prebuy.beat(), prebuys=dispatch.counts["prebuys"]),
                                                   sort_keys=True),
            None if getattr(cache, "lookups", None) is None else json.dumps(
                {"lookups": dict(cache.lookups), "hit_age_s": cache.hit_age_s}, sort_keys=True),
            _send_counters_json(db),
            None if getattr(dispatch, "inline_warm", None) is None else json.dumps(
                dict(dispatch.inline_warm.counts, paused_until=dispatch.inline_warm.paused_until),
                sort_keys=True),
            json.dumps(dict(LANE_HOLDS.beat(), feed_holds=dispatch.counts.get("feed_holds", 0),
                            feed_releases=dispatch.counts.get("feed_releases", 0)), sort_keys=True),
            json.dumps(inline.beat(), sort_keys=True) if hasattr(inline, "beat") else None))
        db.commit()
    finally:
        db.close()


def note_prebuy_source(live, payload, rec):
    """A 4663 lane verdict on a source buy, for the Relay pre-buy's source-fill counters (no-op with the
    knob off, a queued or in-flight hand-off, or no recent pre-buy of the coin). In memory only."""
    if (not (lane_cfg.MEMECOIN_RH_RELAY_PREBUY and live.CHAIN_ID == 4663
             or lane_cfg.MEMECOIN_BSC_RELAY_PREBUY and live.CHAIN_ID == 56)
            or rec.get("reason") in ("relay_prebuy_in_flight", "shared_queued")):
        return
    try:
        from sniper.services.memecoin_rh_relay_prebuy import note_source_fill
        note_source_fill(str(live.CHAIN_ID), payload["mint"], payload.get("source_user_id"), payload["tx_hash"],
                         live.DELIVERY_LANE, rec.get("reason"))
    except Exception:
        logger.exception("relay prebuy: source fill note failed")


class SharedRouteWorker:
    """One shared route attempt off the native listener, with its own connection and lane-local state.

    The listener alone updates pilot counters. A queued/running attempt reserves one pilot
    slot until its completion is consumed; the worker never mutates those shared counters.
    """
    def __init__(self, live, *, connect, db_path):
        import queue
        self.live, self.connect, self.db_path = live, connect, db_path
        self.jobs, self.completed = queue.SimpleQueue(), collections.deque()
        self.thread = threading.Thread(target=self._run, name="rh-fast-shared", daemon=True)
        self.closed = False

    def start(self):
        self.thread.start()

    def offer(self, payload, recv_epoch):
        if self.closed or not self.thread.is_alive():
            return "shared_worker_unavailable"
        if self.live._pilot_pending:
            return "shared_worker_busy"
        self.live._pilot_pending += 1
        self.jobs.put((dict(payload), recv_epoch))
        return "shared_queued"

    def drain(self):
        # deque append/popleft are thread-safe; an empty worker adds no queue lock to V4 fills.
        while self.completed:
            fills, sends = self.completed.popleft()
            self.live.fills += fills
            self.live.send_times.extend(sends)
            self.live._pilot_pending -= 1

    def close(self):
        if not self.closed:
            self.closed = True
            self.jobs.put(None)
        if self.thread.ident is not None:          # never started (a test): nothing to join
            self.thread.join(timeout=3)

    def _run(self):
        import copy
        worker = copy.copy(self.live)
        worker.shared_worker, worker._pilot_pending = None, 0
        worker._stop_owner = self.live
        conn = None
        try:
            while (job := self.jobs.get()) is not None:
                payload, recv_epoch = job
                worker.fills, worker.send_times = 0, collections.deque()
                worker.stopped = self.live.stopped
                worker.routes = self.live.routes
                try:
                    if conn is None:
                        conn = self.connect()
                        conn.execute("PRAGMA busy_timeout=200")
                    worker.conn = conn
                    rec = worker.handle(payload, recv_epoch=recv_epoch, safety_reason=None)
                    note_prebuy_source(worker, payload, rec)
                    write_record(self.db_path, dict(rec, mode="live", tx_hash=payload["tx_hash"]))
                    write_live_record(self.db_path, rec)
                except Exception:
                    # Never release a claim here: the funded engine owns signed/unknown truth.
                    logger.exception("rh fast shared route: attempt failed for %s", payload.get("tx_hash"))
                finally:
                    self.completed.append((worker.fills, tuple(worker.send_times)))
                    LANE_HOLDS.release(payload.get("tx_hash"), payload.get("mint"))
        finally:
            if conn is not None:
                conn.close()


class LaneDispatch:
    """One heard datagram: a candidates list feeds the safety cache; a signal is deduplicated,
    queues its mint first for a verdict, and is handled with the cache's current answer."""

    def __init__(self, *, shadow, cache, registry, seen, drops, db_path, beat, host_clock=time.time,
                 live=None, preparation=None):
        self.shadow, self.cache, self.registry, self.seen = shadow, cache, registry, seen
        self.live = live                             # a LiveLane in MEMECOIN_RH_FAST_LANE=live
        self.drops, self.db_path, self.beat, self.host_clock = drops, db_path, beat, host_clock
        self.counts = collections.Counter()          # candidates_msgs / signals
        self.preparation, self.preparation_busy = preparation, False
        self.prebuy = None                           # RelayPrebuyWorker, MEMECOIN_RH_RELAY_PREBUY on

    def on_datagram(self, payload, pid, uid, recv_epoch):
        if self.preparation is None or isinstance(payload, dict) and payload.get("kind") == "candidates":
            return self._on_datagram(payload, pid, uid, recv_epoch)
        self.preparation_busy = True                 # listener-local; no lock or entry I/O
        try:
            return self._on_datagram(payload, pid, uid, recv_epoch)
        finally:
            self.preparation_busy = False

    def _on_feed_hold(self, payload):
        """D-b: the detector holds (tx, mint) at the feed's arrival so the on-chain lane waits for this
        lane's verdict; its release ends a hold no signal followed. Live and the knob only; a dict
        write under LANE_HOLDS' lock, no I/O."""
        if self.live is None or not _feed_hold_on():
            self.drops.add("feed_hold_off")
            return None
        tx, mint = payload["tx_hash"], payload["mint"]
        if payload["kind"] == FEED_HOLD_KIND:
            LANE_HOLDS.hold(tx, mint, source="feed")
            self.counts["feed_holds"] += 1
            return "feed_hold"
        LANE_HOLDS.release(tx, mint)
        self.counts["feed_releases"] += 1
        return "feed_release"

    def _on_datagram(self, payload, pid, uid, recv_epoch):
        why = check_sender(payload, pid, uid, detector_pid=detector_pid(self.beat, recv_epoch))
        if why is not None:
            self.drops.add(why)
            return None
        if payload.get("kind") == "candidates":
            for e in payload["entries"]:
                if isinstance(e.get("key"), dict) and e.get("pool"):
                    self.registry[str(e["mint"]).lower()] = registry_entry(e)
            self.cache.note_candidates(payload["entries"], now=self.host_clock())
            if self.preparation is not None:
                self.preparation.note_candidates(payload.get("discovery_mints", []),
                                                  now=self.host_clock())
            self.counts["candidates_msgs"] += 1
            return "candidates"
        if payload.get("kind") in (FEED_HOLD_KIND, FEED_RELEASE_KIND):
            return self._on_feed_hold(payload)
        if payload.get("kind") == PREBUY_KIND:
            # Its own worker thread decides it: the listener only queues (no hold, no read).
            if self.prebuy is None:
                self.drops.add("prebuy_off")
                return None
            if not self.seen.first(payload):
                self.drops.add("duplicate")
                return None
            self.counts["prebuys"] += 1
            return self.prebuy.offer(payload, recv_epoch)
        if not self.seen.first(payload):
            self.drops.add("duplicate")
            return None
        self.counts["signals"] += 1
        if self.live is None:
            return self._on_signal(payload, recv_epoch)
        LANE_HOLDS.hold(payload["tx_hash"], payload["mint"])
        queued = False
        try:
            rec = self._on_signal(payload, recv_epoch)
            # A queued shared route decides on the shared worker, which releases the hold.
            queued = isinstance(rec, dict) and rec.get("reason") == "shared_queued"
            return rec
        finally:
            if not queued:
                LANE_HOLDS.release(payload["tx_hash"], payload["mint"])

    def _on_signal(self, payload, recv_epoch):
        if route_kind(payload) in SHARED_ROUTE_KINDS:
            if self.live is None:
                self.drops.add("shared_shadow_unsupported")
                return None
            rec = self.live.handle(payload, recv_epoch=recv_epoch, safety_reason=None)
            note_prebuy_source(self.live, payload, rec)
            write_record(self.db_path, dict(rec, mode="live", tx_hash=payload["tx_hash"]))
            write_live_record(self.db_path, rec)
            return rec
        mint = str(payload["mint"]).lower()
        self.registry[mint] = registry_entry({
            "kind": route_kind(payload), "key": payload["key"], "pool": payload.get("pool"),
            "bridge": payload.get("bridge"), "a_key": payload.get("a_key"),
            "a_pool": payload.get("a_pool")})
        # -1: the lane's own signal outranks the detector's priority 0 (any coin a signal OR an
        # armed wallet touched in the last hour), and keeps that rank across its re-lists.
        self.cache.note_candidates([{"mint": mint, "priority": -1}], now=self.host_clock(), warm=False)
        safety_evidence = {}
        safety = self.cache.reason(mint, now=self.host_clock(), evidence=safety_evidence)
        safety_evidence["cache_miss_reason"] = safety
        stats = self.cache.stats(mint, now=self.host_clock())
        if self.live is not None:
            rec = self.live.handle(payload, recv_epoch=recv_epoch, safety_reason=safety,
                                   stats=stats)
            rec.setdefault("execution_evidence", {})["safety_warmup"] = safety_evidence
            note_prebuy_source(self.live, payload, rec)
            write_record(self.db_path, dict(rec, mode="live", tx_hash=payload["tx_hash"]))
            write_live_record(self.db_path, rec)
            return rec
        rec = self.shadow.handle(payload, recv_epoch=recv_epoch, safety_reason=safety, stats=stats)
        write_record(self.db_path, dict(rec, mode="runner_shadow"))
        return rec


def _feed_hold_on():
    from sniper.settings import memecoin as cfg
    return cfg.memecoin_rh_fast_feed_hold()


class LaneHolds:
    """(tx, mint) signals the LIVE lane is deciding, so the runner's slower on-chain lane waits for
    its verdict (2026-10-14, 4663 fast-lane misses C, 73809). The on-chain lane began its own attempt
    at .852, 8 ms after this lane had the datagram (.844) and before the lane's hook refused it over
    the ceiling from verified supply (.980); the on-chain gate then priced the coin from 1B-implied
    feed readings ($7,980) and filled. Held from datagram receipt until the lane's verdict, never
    longer than `bound_s`. No I/O: a lock and a dict on this thread. A process restart drops every
    hold: the on-chain lane then behaves as it did before (fail-open)."""

    def __init__(self, bound_s=1.0, clock=time.monotonic, cap=256, wake_fn=None):
        self.bound_s, self.clock, self.cap = float(bound_s), clock, int(cap)
        self._until, self._waited, self._lock = {}, set(), threading.Lock()
        self._wake_fn = wake_fn
        # D-b measurement: holds by source, holds that ran out while the on-chain lane waited, and
        # the last feed holds (tx, mint, host epoch) for the race read against on-chain routes.
        self.counts = collections.Counter()
        self.recent = collections.deque(maxlen=64)
        # How each hold ended, read once by the on-chain lane for its attempt trace (2026-10-17).
        self._ended = collections.OrderedDict()

    def _end(self, key, outcome):
        self._ended.pop(key, None)
        self._ended[key] = outcome
        while len(self._ended) > self.cap:
            self._ended.popitem(last=False)

    def ended(self, tx, mint):
        """How the hold on (tx, mint), else on (tx, "*"), ended: released, expired_waited,
        expired_waited_tx_only or expired; None when no ended hold is known. Read once."""
        with self._lock:
            got = None
            for key in (self._key(tx, mint), self._key(tx, "*")):
                outcome = self._ended.pop(key, None)
                got = got or outcome
            return got

    @staticmethod
    def _key(tx, mint):
        return str(tx).lower(), str(mint).lower()

    def hold(self, tx, mint, *, source="signal"):
        """Hold (tx, mint). A mint of "*" holds every coin of the transaction (the detector's feed
        hold when the calldata names no mint)."""
        key, until = self._key(tx, mint), self.clock() + self.bound_s
        with self._lock:
            self.counts["hold_" + source] += 1
            if source == "feed":
                self.recent.append((key[0], key[1], time.time()))
            self._until.pop(key, None)
            self._until[key] = until
            while len(self._until) > self.cap:
                old = next(iter(self._until))
                del self._until[old]
                self._waited.discard(old)

    def release(self, tx, mint):
        """The lane's verdict: end the hold, and wake the on-chain lane only if it waited on it.

        Also ends the transaction's "*" feed hold (Gamma B1, 2026-10-16): a feed with no mint holds
        (tx, "*"), and the lane's datagram for (tx, mint) takes it over, so the verdict on that
        datagram must end both. Otherwise "*" kept the on-chain lane waiting to the bound after the
        lane had refused."""
        keys = {self._key(tx, mint), self._key(tx, "*")}
        with self._lock:
            waited = False
            for key in keys:
                if self._until.pop(key, None) is not None:
                    self._end(key, "released")
                waited = waited or key in self._waited
                self._waited.discard(key)
        if waited:
            try:
                (self._wake_fn or _wake_onchain_lane)()
            except Exception:
                logger.exception("rh fast: on-chain lane wake failed")

    def holds(self, tx, mint):
        """True while the lane is deciding this signal (or holds its whole transaction) and the
        bound has not passed."""
        held = False
        with self._lock:
            for key in (self._key(tx, mint), self._key(tx, "*")):
                until = self._until.get(key)
                if until is None:
                    continue
                if self.clock() >= until:
                    del self._until[key]
                    outcome = "expired"
                    if key in self._waited:
                        self.counts["expired_waited"] += 1
                        outcome = "expired_waited"
                        if key[1] == "*":
                            self.counts["expired_waited_tx_only"] += 1
                            outcome = "expired_waited_tx_only"
                    self._end(key, outcome)
                    self._waited.discard(key)
                    continue
                self._waited.add(key)
                held = True
        return held

    def beat(self):
        with self._lock:
            return dict(self.counts, recent=[list(r) for r in self.recent])


def _wake_onchain_lane():
    from sniper.services.memecoin_onchain_wake import default_address, send_wake
    send_wake(default_address())


LANE_HOLDS = LaneHolds()


def stop_file_path():
    """The operator's stop file for the live 4663 lane (present = every signal falls back)."""
    from sniper.util.state_paths import state_path
    return str(state_path("data", "memecoin_rh_fast_lane.stop"))


def _norm_url(url):
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(str(url or "").strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))


def safety_host_refusal(url, env, own_name="MEMECOIN_RH_FAST_SAFETY_RPC_URL"):
    """None when `url` (the value of env `own_name`: the cache host, or the inline host
    MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL) may be a 4663 lane safety host, else why not (Gamma MEDIUM on
    220471669): it must not be any other environment URL (the funded custody RPC, the research key,
    another role's key), a funded EVM read-pool member (evm_wallet._rpc expands an exact member to
    the whole funded pool), or a free-submission endpoint. Matched on the FULL URL: the research key
    and the operator's second key are both QuickNode, so a domain match would refuse the intended
    key. Never a reason that echoes the URL (a key rides in its path)."""
    from sniper.execution.free_endpoint_registry import free_endpoints
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.settings import memecoin as cfg
    want = _norm_url(url)
    if not url or not want.startswith("https://"):
        return "not an https URL"
    for name, value in env.items():
        if name == own_name:
            continue
        for part in str(value or "").replace(",", " ").split():
            if "://" in part and _norm_url(part) == want:
                return "it is also %s" % name
    for pools in (getattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {}) or {},
                  getattr(cfg, "MEMECOIN_EVM_ARCHIVE_RPC_POOL_URLS", {}) or {}):
        for chain, urls in pools.items():
            if any(_norm_url(u) == want for u in urls):
                return "it is a funded chain-%s read-pool member" % chain
    for chain in ("solana", *CHAIN_QUOTE):
        if any(_norm_url(e.url) == want for e in free_endpoints(chain)):
            return "it is a free-submission endpoint"
    return None


def apply_stop_file(live, path, log):
    """Set the live lane's `stopped` from the operator's stop file (scripts/memecoin_fast_lane_stop.sh)
    and log a change once; the lane's own check is one attribute read on its thread."""
    stopped = os.path.exists(path)
    if stopped != live.stopped:
        live.stopped = stopped
        log("[rh-fast-lane] operator stop file %s: the live lane is %s" % (
            "present" if stopped else "removed", "STOPPED" if stopped else "running"))
    return stopped


def _seed_pilot(conn, signal_source="rh_fast_lane", network_id=None):
    """(lane fills so far, host epochs of lane sends in the last hour) from the ledger, so a
    restart never resets the pilot's count or its hourly cap. With `network_id`, one chain's only:
    the 4663 and Base 8453 lanes share the signal source (plan 2026-10-14 part A)."""
    from datetime import datetime
    chain_sql, args = ("", ()) if network_id is None else (" AND network_id=?", (str(network_id),))
    fills = conn.execute("SELECT count(*) FROM memecoin_positions WHERE signal_source=? "
                         "AND execution_mode='real'" + chain_sql, (signal_source,) + args).fetchone()[0]
    since = time.time() - 3600
    # Canonical reverts clear the mutable binding, but remain signed send attempts. Count the
    # earliest attempt for each identity across providers/reoffers, with legacy native bindings
    # only where that reservation/identity has no durable audit yet. All attempt states count
    # conservatively, as the native lane counts a returned send attempt even when not accepted.
    audit_chain_sql = "" if network_id is None else " AND a.chain_id=? AND p.network_id=?"
    audit_args = () if network_id is None else (str(network_id), str(network_id))
    rows = conn.execute(
        "SELECT a.chain_id, a.tx_identity, a.created_at FROM memecoin_submission_attempts a "
        "JOIN memecoin_pending_entries p ON a.owner_kind='pending' AND a.owner_id=CAST(p.id AS TEXT) "
        "AND a.chain_id=p.network_id "
        "WHERE a.action='entry' AND p.signal_source=?" + audit_chain_sql +
        " UNION ALL SELECT network_id, tx_sig, submission_bound_at FROM memecoin_pending_entries p "
        "WHERE signal_source=? AND submission_bound_at IS NOT NULL AND tx_sig IS NOT NULL" + chain_sql +
        " AND NOT EXISTS (SELECT 1 FROM memecoin_submission_attempts a WHERE a.owner_kind='pending' "
        "AND a.owner_id=CAST(p.id AS TEXT) AND a.action='entry' AND a.chain_id=p.network_id "
        "AND a.tx_identity=p.tx_sig COLLATE NOCASE)",
        (signal_source,) + audit_args + (signal_source,) + args)
    sends = {}
    for chain, identity, bound in rows:
        if not identity:
            continue
        try:
            stamp = datetime.fromisoformat(str(bound).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("missing timezone")
            at = stamp.timestamp()
        except (ValueError, OverflowError, OSError) as exc:
            # A broken cap history cannot restart as an empty budget. The caller leaves lane off.
            raise ValueError("unreadable pilot send timestamp") from exc
        key = (chain, str(identity).lower())  # EVM identity casing does not create another send
        sends[key] = min(at, sends.get(key, at))
    return int(fills), sorted(at for at in sends.values() if at >= since)


def build_lane(contexts, *, log, mode, root=None, connect=None, sender=None, ro_connect=None,
               dex_base=None):
    """The lane's production objects for `mode` (runner_shadow | live), or None when off or there
    is no 4663 context. Shared by start_lane and the production-entry tests: nothing here starts
    a thread or opens a network connection. `contexts` are the runner's own: the hook routes the
    lane's event through them, with the 4663 one replaced per signal by the lane's."""
    from sniper.execution.evm_token_safety import build_evm_token_safety
    from sniper.execution.exec_lock import evm_exec_lock_key
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now
    from sniper.util.state_paths import state_path, state_root
    if mode not in ("runner_shadow", "live"):
        return None
    ctx = (contexts or {}).get("4663")
    address = (ctx or {}).get("address") if isinstance(ctx, dict) else None
    if not address or not callable((ctx or {}).get("v4_direct_quote_fn")):
        log("[rh-fast-lane] %s set but no 4663 context: lane off" % mode)
        return None
    root = state_root() if root is None else root
    db_path = str(state_path("data", "memecoin_rh_fast_lane.db"))
    # The SAME key and the same default lock dir memecoin_evm_leg's executors use on 4663.
    key = evm_exec_lock_key("4663", address)
    common = dict(clip_usd=cfg.MEMECOIN_RH_FAST_LANE_PILOT_CLIP_USD,
                  low_wei=cfg.MEMECOIN_EVM_GAS_TOPUP_LOW_WEI,
                  generation_fn=lane_generation_fn(key, "4663"),
                  max_snapshot_age_s=cfg.MEMECOIN_RH_FAST_LANE_SNAPSHOT_MAX_AGE_S,
                  eth_usd_max_age_s=cfg.MEMECOIN_RH_FAST_LANE_ETH_USD_MAX_AGE_S,
                  generation_margin_s=cfg.MEMECOIN_RH_FAST_LANE_GENERATION_MARGIN_S)
    registry = {}                     # mint -> registry_entry (route-shaped), from the detector
    times = SafetyTimes()
    url = cfg.memecoin_rh_fast_safety_rpc_url()
    own = os.getenv("MEMECOIN_RH_FAST_SAFETY_RPC_URL", "").strip()
    refusal = safety_host_refusal(own, os.environ) if own else None
    if refusal is not None:
        # Never another role's or a funded budget: the cache stays on the research key, and a
        # miss falls back (no inline).
        log("[rh-fast-lane] MEMECOIN_RH_FAST_SAFETY_RPC_URL refused (%s): the safety cache stays on "
            "the research host and a cache miss falls back" % refusal)
        url = os.getenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "").strip()
    # The research key rotates across its numbered spare slots on a quota answer (operator
    # 2026-10-02); a cache host of the operator's own (MEMECOIN_RH_FAST_SAFETY_RPC_URL) is not a
    # research slot and passes straight through. The inline host never goes through it.
    from sniper.execution.research_rotation_4663 import Rotation
    rotation = Rotation(log=log, drpc_fallback=True)
    paced = PacedHttp(rotation.default_seam(), min_gap_s=cfg.MEMECOIN_RH_FAST_SAFETY_MIN_GAP_S,
                      pause_s=120.0)

    def facts(mint, http=paced, host=None):
        # Decimals and supply, once per coin, on the cache's host or the inline host (never a funded one).
        return lane_facts_once(mint, http=http, host=host or url)

    def safety_on(http, host, *, path, **deadline):
        def quote(mint, quote_token, amount, slippage_bps=None, **_ignored):
            entry = registry.get(str(mint).lower())
            if entry is None:
                return None
            return route_quote(ctx["v4_direct_quote_fn"], entry, mint, quote_token, amount,
                               host=host, http=http, slippage_bps=slippage_bps)
        check = build_evm_token_safety(
            chain_id="4663", rpc_url=host, taker=address, quote_fn=quote, http=http,
            sell_slippage_bps=int(round(cfg.MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT * 100)),
            sell_route_label="rh_fast_v4_direct", **deadline)
        return routed_safety(check, registry, host, http, path=path, times=times)
    safety_fn = background_safety(safety_on(paced, url, path="cache"), rotation, paced) if url else None
    # INLINE ON A MISS (Alpha 2026-10-02), only on the operator's inline host (operator 08:14Z: the
    # free key does inline checks only): the same verdict and the coin's facts through an unqueued
    # seam inside a whole-check deadline (every worker thread too). Its own PacedHttp holds only
    # the 429 pause, so an inline 429 never pauses the cache and a research 429 never pauses inline.
    inline, inline_paced, inline_warm = None, None, None
    inline_urls, inline_own = resolve_inline_hosts(os.environ, log)
    inline_url = inline_urls[0] if inline_urls else ""
    if inline_urls:
        from sniper.execution.fast_http import PooledHttpClient
        from sniper.execution.submission_backends import register_client
        probe_body = '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}'
        chain_body = '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}'

        def make_host(index, host_url):
            client = register_client(PooledHttpClient())
            paced_host = PacedHttp(min_gap_s=0.0, pause_s=120.0)
            paced_host.quota_probe = lambda: client.request("POST", host_url, body=probe_body, timeout=3.0)[:2]
            identity_probe = lambda: client.request("POST", host_url, body=chain_body, timeout=3.0)[:2]
            holder = {}
            gate = lambda: holder["host"].admission == "admitted"          # no read leaves for a host whose chain id is not proven (admission is the background pass)
            http = lane_inline_http(host_url, paced_host, base=client.request, gate=gate)
            warm_safety = warm_facts = None
            if cfg.MEMECOIN_RH_FAST_INLINE_WARM:
                warm_http = lane_warm_http(host_url, paced_host, client.request, cfg.MEMECOIN_RH_FAST_INLINE_WARM_CALL_GAP_S, gate=gate)
                warm_safety = safety_on(warm_http, host_url, path="inline_warm")
                warm_facts = lambda mint, _h=warm_http, _u=host_url: facts(mint, http=_h, host=_u)
            host = InlineHost("host %d of %d (%s)" % (index, len(inline_urls), host_provider(host_url)), paced_host,
                              safety_fn=safety_on(http, host_url, path="inline", safety_deadline_scope=inline_deadline,
                                                  safety_deadline_s=cfg.MEMECOIN_RH_FAST_INLINE_SAFETY_DEADLINE_S),
                              facts_fn=lambda mint, _h=http, _u=host_url: facts(mint, http=_h, host=_u),
                              warm_safety_fn=warm_safety, warm_facts_fn=warm_facts, identity_probe=identity_probe)
            holder["host"] = host
            return host
        # INLINE ON A MISS (Alpha 2026-10-02), only on the operator's inline hosts: the same verdict and the coin's facts through an unqueued
        # seam inside a whole-check deadline. Each host has its own PacedHttp (429 pause, dead mark, quota latch and probe), so an inline 429 never pauses the
        # cache, a research 429 never pauses inline, and one host's quota never stops another (rotation, operator 2026-10-29).
        inline_paced = InlineHostSet([make_host(i, u) for i, u in enumerate(inline_urls, 1)], log=log)
        _STATUS["inline"] = inline_paced
        inline = dict(inline_safety_fn=inline_paced.safety, inline_facts_fn=inline_paced.facts,
                      inline_deadline_s=cfg.MEMECOIN_RH_FAST_INLINE_SAFETY_DEADLINE_S, inline_host=inline_paced)
        if cfg.MEMECOIN_RH_FAST_INLINE_WARM:
            # The idle active inline host warms the cache (rev 2 §3.6); its cache is set below.
            inline_warm = InlineWarmer(None, safety_fn=inline_paced.warm_safety, facts_fn=inline_paced.warm_facts,
                                       host=inline_paced, gap_s=cfg.MEMECOIN_RH_FAST_INLINE_WARM_GAP_S, log=log)
    dex = DexBudget(dex_base)
    cache = SafetyCache(safety_fn or (lambda mint, usd=None: (False, "no_research_host")),
                        ttl_s=cfg.MEMECOIN_RH_FAST_SAFETY_TTL_S,
                        usd=cfg.MEMECOIN_RH_FAST_LANE_PILOT_CLIP_USD, facts_fn=facts if url else None,
                        stats_fn=dex.stats, refresh_lead_s=cfg.MEMECOIN_RH_FAST_SAFETY_REFRESH_LEAD_S,
                        supply_ttl_s=cfg.MEMECOIN_FAST_LANE_SUPPLY_TTL_S,
                        facts_sink=ctx.get("put_token_facts_fn"))
    if inline_warm is not None:
        inline_warm.cache = cache
    common.update(facts_fn=cache.facts, ctx=ctx, contexts=contexts,
                  require_exact_state=cfg.MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE)
    if ro_connect is None:
        from sniper.persistence.db_core import get_readonly_connection
        ro_connect = get_readonly_connection
    shadow = RunnerShadow(lock_key=key, lock_dir=None, taker=address, now_fn=real_utc_now,
                          ro_connect=ro_connect, fresh_stats_fn=dex.stats,
                          check_sink=stats_check_sink(db_path), **common)
    live = None
    if mode == "live":
        from sniper.execution.evm_wallet import _rpc
        from sniper.persistence.db_core import get_connection
        conn = (connect or get_connection)()
        conn.execute("PRAGMA busy_timeout=200")
        fills, sends = _seed_pilot(conn, network_id="4663")
        rpc_url = ctx.get("rpc_url")

        def rpc_read(method, params):
            return (_rpc(rpc_url, method, params) or {}).get("result")

        def balance_at(mint, block):
            data = "0x70a08231" + str(address)[2:].lower().rjust(64, "0")
            for _ in range(10):                 # the custody node can trail the head by a few blocks
                result = rpc_read("eth_call", [{"to": mint, "data": data}, hex(int(block))])
                if isinstance(result, str) and len(result) > 2:
                    return int(result, 16)
                time.sleep(0.5)
            return None
        reoffer = ctx.get("reoffer_send_fn")
        live_kwargs = dict(
            conn=conn, sender=sender,
            fanout_fn=(lambda raw: reoffer(str(raw).encode("ascii"))) if callable(reoffer) else (
                lambda raw: None),
            rpc_read=rpc_read, lock_key=key, lock_dir=None, host_clock=time.time,
            pilot_fills=cfg.MEMECOIN_RH_FAST_LANE_PILOT_FILLS,
            max_sends_per_hour=cfg.MEMECOIN_RH_FAST_LANE_MAX_SENDS_PER_HOUR,
            fills=fills, send_times=sends, balance_at_fn=balance_at,
            lock_receipt_cap_s=cfg.MEMECOIN_RH_FAST_LANE_LOCK_RECEIPT_CAP_S,
            check_sink=fill_check_sink(db_path), cache=cache, routes=cfg.MEMECOIN_RH_FAST_ROUTES,
            stats_fn=lambda mint: dex.stats(mint), **(inline or {}), **common,
            revert_reason_fn=lambda h: _revert_reason(h, rpc_url))
        live = LiveLane(**live_kwargs)
        live.shared_connect = connect or get_connection
        log("[rh-fast-lane] live route kinds: %s" % (",".join(sorted(live.routes)) or "none"))
        log("[rh-fast-lane] native submission: %s providers=%s" % (
            ctx.get("submission_backend", "shared") if live.submission_transport is not None else (
                "injected" if sender is not None else "sequencer"),
            ",".join(p.provider_id for p in live.submission_transport._providers)
            if live.submission_transport is not None else "legacy"))
    dispatch = LaneDispatch(shadow=shadow, cache=cache, registry=registry, seen=Seen(),
                            drops=Drops(), db_path=db_path,
                            beat=str(state_path("data", "memecoin_rh_feed_shadow.heartbeat")),
                            live=live)
    preparation = None
    if cfg.MEMECOIN_RH_PRESIGNAL_DISCOVERY_ENABLED and url:
        preparation = DiscoveryPreparation(
            paced=paced, url=url, cache=cache, generation_fn=common["generation_fn"],
            available_fn=lambda: not dispatch.preparation_busy and (
                live is None or not live.stopped and not live._pilot_pending))
        dispatch.preparation = preparation
    if live is not None:
        live.preparation = preparation
    prebuy = None
    if live is not None and cfg.MEMECOIN_RH_RELAY_PREBUY:
        from sniper.services.memecoin_rh_relay_prebuy import build_prebuy_worker
        prebuy = build_prebuy_worker(live, live_kwargs, connect=live.shared_connect, db_path=db_path,
                                     registry=registry, cache=cache, log=log)
        dispatch.prebuy = prebuy
    return {"dispatch": dispatch, "cache": cache, "safety_fn": safety_fn, "paced": paced, "prebuy": prebuy,
            "dex": dex, "address_name": lane_address(root), "db_path": db_path, "live": live,
            "url": url, "inline_url": inline_url if inline else "", "inline_paced": inline_paced,
            "rotation": rotation, "times": times, "preparation": preparation, "inline_warm": inline_warm}


# THE LANE'S STOP (2026-10-08, deploy e2f5ae905 refused): start_lane's loops ran `while True` with no
# way out, so a test that started the lane left four threads for the rest of its xdist worker, and
# their safety and keep-alive calls landed in a later test's network guard. The runner never stops the
# lane (the process exit does); stop_lane is for tests and an orderly shutdown. Each start_lane owns
# one stop event, which its loops check once per pass and wait on instead of sleeping. stop_lane sets
# it and NEVER clears it, so a thread that outlives the join still ends, and a later start_lane runs
# on a fresh event (Gamma LOW on c7f4626c3).
_LANE_RUNS = []             # (stop event, threads, shared worker or None), one per start_lane


def stop_lane(timeout_s=5.0):
    """Stop every thread start_lane started in this process and close its shared worker; returns
    the names of threads still alive after `timeout_s`. A thread that never started (a test patched
    Thread.start) is dropped, never joined (Gamma REFUSE on c7f4626c3)."""
    runs, _LANE_RUNS[:] = list(_LANE_RUNS), []
    for stop, _threads, worker in runs:
        stop.set()
        if worker is not None:
            try:
                worker.close()
            except Exception:
                logger.exception("rh fast lane: shared worker close failed")
    deadline = time.monotonic() + float(timeout_s)
    alive = []
    for _stop, threads, _worker in runs:
        for thread in threads:
            if thread.ident is None:
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                alive.append(thread.name)
    return alive


def start_lane(contexts, *, log, root=None):
    """Start the lane threads when MEMECOIN_RH_FAST_LANE is runner_shadow or live and a 4663 context
    exists: the datagram listener, the safety cache (today's token_safety_fn on the research host
    through a V4-direct quote; Alpha option 1) and, live, the sequencer connection's keep-alive.
    Returns the listener thread, or None. Never raises into the runner."""
    from sniper.settings import memecoin as cfg
    try:
        mode = cfg.memecoin_rh_fast_lane_mode()
        lane = build_lane(contexts, log=log, mode=mode, root=root, dex_base=lane_dex_base(None))
        if lane is None:
            return None
    except Exception:
        logger.exception("rh fast lane: start failed (lane off)")
        return None
    stop, worker, threads = threading.Event(), None, []
    if lane.get("preparation") is not None:
        lane["preparation"].stop_fn = stop.is_set
    _LANE_RUNS.append((stop, threads, None))
    if lane["live"] is not None:
        # Gamma LOW: a stop file present at start stops the lane before its first datagram, not after
        # the first poll.
        apply_stop_file(lane["live"], stop_file_path(), log)
        if lane["live"].routes & SHARED_ROUTE_KINDS:
            worker = SharedRouteWorker(lane["live"], connect=lane["live"].shared_connect,
                                       db_path=lane["db_path"])
            lane["live"].shared_worker = worker
            _LANE_RUNS[-1] = (stop, threads, worker)
            worker.start()
        if lane.get("prebuy") is not None:
            _LANE_RUNS.append((stop, [], lane["prebuy"]))     # stop_lane closes it
            lane["prebuy"].start()
    dispatch, cache, safety_fn, paced = lane["dispatch"], lane["cache"], lane["safety_fn"], lane["paced"]
    db_path, address_name = lane["db_path"], lane["address_name"]
    if not lane["url"]:
        log("[rh-fast-lane] no research host for the safety cache: every signal falls back")

    def latch_alert(text):
        from sniper.services.memecoin_alerts import send_alert
        send_alert("rh_fast_lane", text)
    latch = LatchWatch(log=log, alert_fn=latch_alert)

    def run():
        warm_hook(dispatch.shadow, log=log)             # off any signal: the first one runs warm
        sock, bind_state, flushed = None, {}, time.monotonic()
        while not stop.is_set():
            try:
                latch.check()                           # one loud line the first time it latches
                if time.monotonic() - flushed >= 60:
                    flushed = time.monotonic()
                    dispatch.drops.flush(db_path)
                    lane["times"].flush(db_path)
                    write_heartbeat(db_path, dispatch, cache, paced, lane["inline_paced"],
                                    lane["rotation"])
                    write_dex_counts(db_path, lane["dex"], cache)
                    write_lane_state(db_path)
                if sock is None:
                    sock = bind_or_log(address_name, bind_state, log)
                    if sock is None:          # a deploy overlap still holds the name: retry
                        stop.wait(1.0)
                        continue
                got = sock.recv(1.0)
                if got is None:
                    continue
                # HOST clock on both ends (detector heartbeat ts, sent_epoch): a same-host delay.
                # The busy window uses the corrected clock instead, to join ledger decision stamps.
                payload, pid, uid = got
                dispatch.on_datagram(payload, pid, uid, time.time())
            except Exception:
                logger.exception("rh fast lane: signal handling failed (lane keeps running)")
                stop.wait(0.1)
        if sock is not None:
            sock.close()                                # the abstract name is free for a restart now

    def safety_loop():
        # The research host's budget is PacedHttp's; a 429 pauses it and the verdicts age out.
        while not stop.is_set():
            try:
                preparation = lane.get("preparation")
                if preparation is not None:
                    kind, mint = preparation.next_work(time.time())
                    if kind == "discovery":
                        preparation.run_one(mint, allow_native_due=True)
                        continue
                else:
                    mint = cache.next_due(time.time())
                if mint is None or safety_fn is None:
                    stop.wait(1.0)
                    continue
                result = cache.run_one(mint, now=time.time())
                if result is not None and preparation is not None:
                    preparation.native_completed()
            except Exception:
                logger.exception("rh fast safety: verdict loop failed (keeps running)")
                stop.wait(1.0)

    def inline_admission_loop():
        # Chain-identity admission of every inline host (eth_chainId == 4663's id) OFF the fill path: at start, on every rejoin, every 10 minutes. A host serves only once admitted.
        while not stop.is_set():
            try:
                lane["inline_paced"].admit_pass()
            except Exception:
                logger.exception("rh fast safety: inline admission pass failed (keeps running)")
            stop.wait(lane["inline_paced"].ADMIT_RETRY_S)

    def quota_recovery_loop():
        # The inline host's monthly-RU recovery, independent of the warmer's modes.
        while not stop.is_set():
            lane["inline_paced"].quota_recover()
            stop.wait(5.0)

    def inline_warm_loop():
        # The idle inline host's verdicts (rev 2 §3.6): its own pace, pauses and counters.
        warm = lane["inline_warm"]
        while not stop.is_set():
            try:
                wait = warm.step(time.time())
            except Exception:
                logger.exception("rh fast lane: inline warmer step failed (keeps running)")
                wait = 1.0
            stop.wait(wait)

    def stop_watch():
        # Live only: the operator's stop file, read once a second off the fill path.
        path = stop_file_path()
        while not stop.is_set():
            try:
                apply_stop_file(lane["live"], path, log)
            except Exception:
                logger.exception("rh fast lane: stop-file check failed")
            stop.wait(1.0)

    def keepalive():
        # Live only: keep the sequencer's TLS connection warm (one cheap refused request a while).
        while not stop.is_set():
            try:
                lane["live"].sender.warm()
            except Exception:
                logger.exception("rh fast lane: sequencer keep-alive failed")
            stop.wait(45.0)

    threads += [threading.Thread(target=run, name="rh-fast-lane", daemon=True),
                threading.Thread(target=safety_loop, name="rh-fast-safety", daemon=True)]
    if lane.get("inline_paced") is not None:
        threads.append(threading.Thread(target=quota_recovery_loop, name="rh-fast-quota-recovery", daemon=True))
        threads.append(threading.Thread(target=inline_admission_loop, name="rh-fast-inline-admission", daemon=True))
    if lane.get("inline_warm") is not None:
        dispatch.inline_warm = lane["inline_warm"]           # its counters ride the heartbeat
        threads.append(threading.Thread(target=inline_warm_loop, name="rh-fast-inline-warm", daemon=True))
    if lane["live"] is not None:
        if getattr(lane["live"], "submission_transport", None) is None:
            threads.append(threading.Thread(target=keepalive, name="rh-fast-sequencer", daemon=True))
        threads.append(threading.Thread(target=stop_watch, name="rh-fast-stop", daemon=True))
    for thread in threads:
        thread.start()
    log("[rh-fast-lane] %s started%s; safety cache on the research host" % (
        mode, ": LIVE, $%.0f pilot clip" % cfg.MEMECOIN_RH_FAST_LANE_PILOT_CLIP_USD
        if lane["live"] is not None else ": no signing, no reservation, no send"))
    return threads[0]
