"""Robinhood 4663 fast lane, M1: the decide path in shadow (spec
docs/superpowers/specs/2026-10-01-memecoin-4663-fast-lane-design.md §2.3-§2.5, §6 M1).

SIGN-LESS AND OUTSIDE THE RUNNER (Alpha 2026-10-01). It runs in the M0 shadow process, holds no
key, reserves nothing and never touches a funded read host: its reads come from the snapshot or the
research host. It builds the unsigned buy the lane would send, and records why it would not.

The buy is a Universal Router V4 swap paid in native ETH (`msg.value`), so it needs no approval and
no Permit2. Funded 4663 entries since 09-13 (n=134): uniswap_v4 hooked 57, plain 3, multihop 33,
relay 16, kyber 11, pons 5, curve 4, direct V3 0. So v1 builds single-hop native V4 only; everything
else is a fallback reason. The encoders are the live-proven ones in `evm_v4_quote`, unchanged.
"""
from __future__ import annotations

import logging
from fractions import Fraction

from sniper.execution import evm_v4_quote as v4
from sniper.execution.evm_v4_quote import native_buy_tx  # noqa: F401 - M1/M2 build the same bytes
from sniper.execution.evm_wallet import CHAIN_QUOTE
from sniper.execution.evm_v4_quote import (
    swap_native_in, swap_one_for_zero, _lp_pips, source_pool_est_out)

logger = logging.getLogger(__name__)

def price_cap(*, post_sqrt, src_in, src_out, chase_pct, premium):
    """The highest average price (wei per token atomic) the buy may pay: the tighter of the
    source's post-trade price x (1 + chase) and the source's average price x the execution premium
    (spec §2.4). A term set to 0 is off; with both off there is no cap, which is None, never 0."""
    terms = []
    if chase_pct and chase_pct > 0:
        terms.append(Fraction(1 << 192, int(post_sqrt) ** 2) * (1 + Fraction(str(chase_pct)) / 100))
    if premium and premium > 0 and src_out:
        terms.append(Fraction(int(src_in), int(src_out)) * Fraction(str(premium)))
    return min(terms) if terms else None


def min_out_at(clip_wei, cap):
    """Token atomic units the clip must buy at the cap price, rounded down; None without a cap."""
    return None if cap is None else int(clip_wei) * cap.denominator // cap.numerator


_ACTIVITY_S = 300.0
_DEPTH = 16


class TapeState:
    """The swap tape held in memory: the last `_DEPTH` swaps of every pool and each pool's swap
    times over `_ACTIVITY_S`. Fed by one tailing thread (`ingest`); read by the decide thread.
    `meta_fn(pool)` is the tape's pool_meta + onchain_pool_protocols row as a dict, or None while
    the pool is unresolved (it is asked again on its next swap)."""

    def __init__(self, meta_fn):
        import collections
        import threading
        self._meta_fn = meta_fn
        self._lock = threading.Lock()
        self._rows = collections.defaultdict(lambda: collections.deque(maxlen=_DEPTH))
        self._times = collections.defaultdict(collections.deque)
        self._meta = {}
        self._pools = collections.defaultdict(set)              # mint -> pools
        self._by_token = collections.defaultdict(set)           # either side's token -> pools (R2/R3)
        self._source_fills = collections.OrderedDict()
        self.ingested = 0
        self.head = None                                        # newest block ingested, any pool

    def ingest(self, rows):
        for row in rows:
            pool = row["pool"]
            meta = None if pool in self._meta else self._meta_fn(pool)    # SQL outside the lock
            if pool not in self._meta and not meta:
                continue
            with self._lock:
                if meta:
                    self._meta[pool] = meta
                    self._pools[str(meta["mint"]).lower()].add(pool)
                    self._by_token[str(meta["token0"]).lower()].add(pool)
                    self._by_token[str(meta["token1"]).lower()].add(pool)
                self._rows[pool].append(row)
                self._times[pool].append(row["observed"])
                self.ingested += 1
                if self.head is None or row["block_no"] > self.head:
                    self.head = row["block_no"]

    def learn(self, pools):
        """Apply a mint's pools read by the learn thread (spec routes R1): [(pool, meta, newest swap
        row)]. Under the same lock as `ingest`. A learned row is marked `learned` (age-bounded at
        decide time, Gamma M2) and never replaces a newer row the tail delivered; it is no activity."""
        with self._lock:
            for pool, meta, row in pools:
                if pool not in self._meta:
                    self._meta[pool] = meta
                self._pools[str(meta["mint"]).lower()].add(pool)
                self._by_token[str(meta["token0"]).lower()].add(pool)
                self._by_token[str(meta["token1"]).lower()].add(pool)
                rows = self._rows[pool]
                if not rows or rows[-1]["block_no"] < row["block_no"]:
                    rows.append(dict(row, learned=True))

    def _recent(self, pool, now):
        times = self._times[pool]
        while times and times[0] < now - _ACTIVITY_S:
            times.popleft()
        return len(times)

    def route(self, mint, now, *, source_sig=None, require_source_fill=False):
        """Native route or refusal; source_sig also returns a bound shared hint under this lock."""
        best = None
        with self._lock:
            pools = self._pools.get(str(mint).lower())
            if not pools:
                return ((None, "no_pool", self._source_swap(source_sig, require_source_fill=require_source_fill)) if source_sig is not None
                        else (None, "no_pool"))
            for pool in pools:
                m = self._meta[pool]
                if m["protocol"] != "v4" or str(m["token0"]).lower() != v4._NATIVE:
                    continue
                n = self._recent(pool, now)
                if best is None or n > best[0]:
                    best = (n, pool, m)
            source = (self._source_swap(source_sig, native_pool=best[1] if best else None,
                                       require_source_fill=require_source_fill)
                      if source_sig is not None else None)
        if best is None:
            return ((None, "no_native_v4_pool", source) if source_sig is not None
                    else (None, "no_native_v4_pool"))
        _n, pool, m = best
        key = dict(currency0=v4._NATIVE, currency1=str(m["token1"]).lower(), fee=int(m["fee"]),
                   tick_spacing=int(m["tick_spacing"]), hooks=str(m["hooks"]).lower())
        return (pool, key, source) if source_sig is not None else (pool, key)

    @staticmethod
    def _key(meta):
        return dict(currency0=str(meta["token0"]).lower(), currency1=str(meta["token1"]).lower(),
                    fee=int(meta["fee"]), tick_spacing=int(meta["tick_spacing"]),
                    hooks=str(meta["hooks"]).lower())

    def weth_route(self, mint, now):
        """{pool, key, in_is_c0} of the mint's busiest WETH-quoted V4 pool with state, else None (R3)."""
        mint, weth = str(mint).lower(), CHAIN_QUOTE["4663"]["token"].lower()
        best = None
        with self._lock:
            for pool in self._by_token.get(mint, ()):
                m = self._meta[pool]
                if m["protocol"] != "v4" or {m["token0"], m["token1"]} != {weth, mint} or not self._rows.get(pool):
                    continue
                n = self._recent(pool, now)
                if best is None or n > best[0]:
                    best = (n, pool, m)
        if best is None:
            return None
        _n, pool, m = best
        return {"pool": pool, "key": self._key(m), "in_is_c0": m["token0"] == weth}

    def bridge_route(self, mint, now):
        """Native -> X -> coin (R2): the coin's busiest V4 pool B against a bridge token X (not native,
        not WETH) that has a native/X V4 pool A, A the busiest such pool; both with state. None if no
        pair exists. {bridge, a_pool, a_key, b_pool, b_key, x_is_c0}."""
        mint, weth = str(mint).lower(), CHAIN_QUOTE["4663"]["token"].lower()
        best = None
        with self._lock:
            for b in self._by_token.get(mint, ()):
                mb = self._meta[b]
                if mb["protocol"] != "v4" or not self._rows.get(b):
                    continue
                x = mb["token1"] if mb["token0"] == mint else mb["token0"]
                if x in (v4._NATIVE, weth, mint):
                    continue
                a = self._a_pool(x, now)
                if a is None:
                    continue
                n = self._recent(b, now)
                if best is None or n > best[0]:
                    best = (n, b, a, x)
            if best is None:
                return None
            _n, b, a, x = best
            mb, ma = self._meta[b], self._meta[a]
        return {"bridge": x, "a_pool": a, "a_key": self._key(ma), "b_pool": b, "b_key": self._key(mb),
                "x_is_c0": mb["token0"] == x}

    def _a_pool(self, x, now):
        """The busiest native/X V4 pool with state (a bridge's first leg), else None. Caller holds
        the lock."""
        best = None
        for a in self._by_token.get(x, ()):
            ma = self._meta[a]
            if (ma["protocol"] != "v4" or ma["token0"] != v4._NATIVE or ma["token1"] != x
                    or not self._rows.get(a)):
                continue
            n = self._recent(a, now)
            if best is None or n > best[0]:
                best = (n, a)
        return None if best is None else best[1]

    def source_pool_route(self, pool, mint, now):
        """The lane's route through the SOURCE's own V4 pool `pool` (option C's v4_shared, Alpha GO
        2026-10-08), by that pool's quote side: native -> {kind v4_native, pool, key, in_is_c0},
        WETH -> {kind v4_weth, ...}, another token X -> {kind v4_bridge, bridge, a_pool, a_key,
        b_pool, b_key, x_is_c0} with A the busiest native/X V4 pool with state. Else {reason}."""
        mint, weth = str(mint).lower(), CHAIN_QUOTE["4663"]["token"].lower()
        with self._lock:
            meta = self._meta.get(pool)
            if meta is None or meta["protocol"] != "v4" or mint not in (meta["token0"], meta["token1"]):
                return {"reason": "source_pool_unbound"}
            key = self._key(meta)
            x = meta["token1"] if meta["token0"] == mint else meta["token0"]
            if x == v4._NATIVE:
                return {"kind": "v4_native", "pool": pool, "key": key, "in_is_c0": True}
            if x == weth:
                return {"kind": "v4_weth", "pool": pool, "key": key, "in_is_c0": meta["token0"] == weth}
            a = self._a_pool(x, now)
            if a is None:
                return {"reason": "deposit_bridge_no_a_pool"}
            return {"kind": "v4_bridge", "bridge": x, "a_pool": a, "a_key": self._key(self._meta[a]),
                    "b_pool": pool, "b_key": key, "x_is_c0": meta["token0"] == x}

    def state(self, pool, block, tx, *, diagnostics=None):
        """("tape_post", the source's own swap) when the tape already has it, else ("pre", the
        pool's last swap in a block before the source's), else (None, "no_state")."""
        with self._lock:
            rows = list(self._rows.get(pool, ()))
            meta = self._meta.get(pool)
            if diagnostics is not None:
                latest = max(rows, key=lambda r: (r["block_no"], r["log_index"]), default=None)
                reason = None
                if latest is not None:
                    if latest.get("pool") != pool:
                        reason = "wrong_pool"
                    elif str(latest.get("network_id", "4663")) != "4663":
                        reason = "wrong_chain"
                    elif latest.get("removed"):
                        reason = "removed_state"
                    elif not latest.get("sqrt") or not latest.get("liquidity"):
                        reason = "missing_liquidity_or_price"
                diagnostics["latest_same_pool"] = (None if latest is None else {
                    "basis": "background_same_pool_observation", "pool": pool, "chain": "4663",
                    "unknown_reason": reason, "canonicality": "unverified_background",
                    "estimate_coverage": "tick_crossing_and_hook_effects_unverified", "quote_price": None,
                    **{k: latest.get(k) for k in ("block_no", "log_index", "tx_sig", "sqrt", "liquidity", "observed")}})
        own = [row for row in rows if row["tx_sig"] == tx]
        if own:
            merged = (self_swap_source(_by_log(own), meta, meta.get("mint"))
                      if len(own) > 1 and meta else None)
            return "tape_post", merged or own[0]
        before = [r for r in rows if r["block_no"] < block]
        if not before:
            return None, "no_state"
        return "pre", max(before, key=lambda r: (r["block_no"], r["log_index"]))

    def source_blocker(self, sig):
        """Why an exact-source re-ask is still waiting, read from memory under the tape lock (no
        I/O): proof_absent, pool_not_in_tape:<venue>, post_<reason> or resolved_at_deadline (the
        source resolved after the last ready() pass but by the deadline check: ready() tests the
        deadline before readiness; Gamma LOW-2 on 11830c4a4)."""
        with self._lock:
            proof = self._source_fills.get(self._fill_key(sig))
            if proof is None:
                return "proof_absent"
            post = proof.get("source_post") or {}
            if post.get("reason"):
                return "post_%s" % post["reason"]
            if self._meta.get(post.get("pool")) is None:
                return "pool_not_in_tape:%s" % (post.get("venue") or "unknown")
            return "resolved_at_deadline"

    def source_swap(self, sig, *, require_source_fill=False):
        """An exact source swap for the shared executor; never estimate an unknown route."""
        with self._lock:
            return self._source_swap(sig, require_source_fill=require_source_fill)

    @staticmethod
    def _fill_key(sig):
        return (str(sig.get("tx_hash") or "").lower(), str(sig.get("wallet") or "").lower(),
                sig.get("source_user_id"), str(sig.get("mint") or "").lower(),
                int(sig.get("seq", sig.get("block_number", 0))))

    def learn_source_fills(self, proofs):
        from sniper.services.memecoin_source_fill import validate_source_fill
        valid = []
        for proof in proofs:
            if not isinstance(proof, dict):
                continue
            parsed = validate_source_fill(proof, proof)
            if parsed is not None and parsed["network_id"] == "4663":
                valid.append((self._fill_key(parsed), parsed))
        with self._lock:
            for key, proof in valid:
                self._source_fills[key] = proof
                self._source_fills.move_to_end(key)
            while len(self._source_fills) > 4096:
                self._source_fills.popitem(last=False)

    def _source_swap(self, sig, native_pool=None, require_source_fill=False):
        # Caller holds the existing tape lock. Native decisions share route's acquisition.
        mint, tx, block = str(sig["mint"]).lower(), str(sig["tx_hash"]).lower(), int(sig["seq"])
        proof = self._source_fills.get(self._fill_key(sig))
        if proof is not None:
            if proof.get("payment_basis") == "transaction_value_upper_bound":
                return {"reason": "source_payment_unproved"}
            post = proof.get("source_post") or {}
            if post.get('reason') == 'split_route' and post.get('pools'):
                return dict(pool=None, src_out=int(proof['acquired_atomic']), post_ratio=None,
                            state_kind='receipt_split', state_block=block,
                            route_kind='split_shared', key=None, source_fill=proof)
            if post.get('venue') == 'pons_v2' and post.get('reason') == 'pons_post_state_unavailable':
                got, output = int(proof['acquired_atomic']), int(post['pool_output_atomic'])
                if not post.get('receipt_routes_complete') or not got <= output <= got * 10 // 9:
                    return {'reason': 'source_post_unbound'}
                return dict(pool=post['pool'], src_out=got, post_ratio=None,
                            state_kind='receipt_leg', state_block=block, route_kind='pons_shared',
                            key=None, source_fill=proof)
            if post.get("reason"):
                # The receipt proof's own reason is the cause; recorded (Alpha P1, 2026-10-14).
                return {"reason": "source_swap_ambiguous", "source_post_reason": str(post["reason"])}
            pool = post.get("pool")
            meta = self._meta.get(pool)
            if meta is None:
                return {"reason": "source_swap_pending"}
            if (post.get("venue") != meta["protocol"] or meta["protocol"] not in {"v3", "v4"}
                    or mint not in {meta["token0"], meta["token1"]}
                    or not post.get("sqrt_price_x96")
                    or post.get("mint_is_c1") != (meta["token1"] == mint)):
                return {"reason": "source_post_unbound"}
            kind = ("v4_native" if meta["protocol"] == "v4" and pool == native_pool
                    else meta["protocol"] + "_shared")
            return dict(pool=pool, src_out=int(proof["acquired_atomic"]),
                        post_ratio=post["ratio"], state_kind="tape_post", state_block=block,
                        route_kind=kind, key=None, source_fill=proof,
                        source_sqrt=int(post["sqrt_price_x96"]))
        hits = {}
        for pool in self._by_token.get(mint, ()):
            meta = self._meta[pool]
            if meta["protocol"] not in {"v3", "v4"}:
                continue
            rows = [row for row in self._rows[pool]
                    if row["block_no"] == block and str(row["tx_sig"]).lower() == tx]
            if len(rows) > 1:
                merged = self_swap_source(_by_log(rows), meta, mint)
                rows = [merged] if merged else rows
            for row in rows:
                mint_is_c1 = meta["token1"] == mint
                paid = int(row["amount0"] if mint_is_c1 else row["amount1"])
                got = -int(row["amount1"] if mint_is_c1 else row["amount0"])
                if paid <= 0 or got <= 0 or int(row["sqrt"]) <= 0:
                    return {"reason": "source_swap_ambiguous", "source_post_reason": "tape_swap_shape"}
                ident = (pool, int(row["log_index"]))
                hit = (int(row["log_index"]), pool, got, paid, row["sqrt"],
                       mint_is_c1, meta["protocol"])
                if ident in hits and hits[ident] != hit:
                    return {"reason": "source_swap_ambiguous", "source_post_reason": "tape_hit_conflict"}
                hits[ident] = hit
        if not hits:
            return None
        if len(hits) != 1:
            # A SPLIT BINDS ONLY THROUGH THE RECEIPT PROOF (2026-10-14, 4663 fast-lane misses S3).
            # Reached only before the watcher's proof is learned: its bound split (pools) landed
            # 75-156 ms after these tape rows on 13 signals since 10-10, all refused here first.
            # Wait for it like a single-leg fill; a proof without pools still ends ambiguous.
            return {"reason": "source_split_pending"}
        _log, pool, got, paid, sqrt, mint_is_c1, protocol = next(iter(hits.values()))
        if require_source_fill:
            return {"reason": "source_fill_pending"}
        ratio = coin_price(sqrt, mint_is_c1) / Fraction(paid, got)
        route_kind = ("v4_native" if protocol == "v4" and pool == native_pool
                      else protocol + "_shared")
        return dict(pool=pool, src_out=got, post_ratio=float(ratio), state_kind="tape_post",
                    state_block=int(sig["seq"]), route_kind=route_kind, key=None,
                    **({"source_fill": proof, "source_sqrt": sqrt} if proof is not None else {}))

    def busiest(self, now):
        """Mints by swaps over `_ACTIVITY_S`, busiest first (the candidate set's tail)."""
        with self._lock:
            counts = {m: sum(self._recent(p, now) for p in pools) for m, pools in self._pools.items()}
        return [m for m, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if n]

    def rank(self, mint, now):
        """1-based rank of the mint by swaps over `_ACTIVITY_S` (measurement only, off the decide
        timing): the candidate-set flag of spec §2.2."""
        with self._lock:
            counts = {m: sum(self._recent(p, now) for p in pools) for m, pools in self._pools.items()}
        n = counts.get(str(mint).lower(), 0)
        return 1 + sum(1 for c in counts.values() if c > n) if n else None


def _by_log(rows):
    """One row per log index, in log order (the tail and the learn thread can both hold a row)."""
    return [row for _log, row in sorted({int(r["log_index"]): r for r in rows}.items())]


def self_swap_source(rows, meta, mint):
    """The ONE source row of a listed hook's self-swap shape, else None (callers then refuse as before).

    `rows`: one transaction's rows on one pool, in log order. The shape (MEMECOIN_4663_SELF_SWAP_HOOKS):
    the pool's hook is listed, the FIRST row buys `mint` from the pool, and every later row has a sender
    and they all share ONE sender that is not the first row's. The source is the first row's amounts
    with the LAST row's sqrt and liquidity: the price the next buyer faces after the hook's own swaps."""
    from sniper.settings import memecoin as cfg
    if (len(rows) < 2 or not meta or str(meta.get("hooks") or "").lower()
            not in cfg.MEMECOIN_4663_SELF_SWAP_HOOKS or mint not in (meta["token0"], meta["token1"])):
        return None
    first, later = rows[0], rows[1:]
    makers = {row.get("maker") for row in later}
    if None in makers or len(makers) != 1 or not first.get("maker") or first["maker"] in makers:
        return None
    c1 = meta["token1"] == mint
    paid = int(first["amount0"] if c1 else first["amount1"])
    got = -int(first["amount1"] if c1 else first["amount0"])
    if paid <= 0 or got <= 0 or int(first["sqrt"]) <= 0 or int(rows[-1]["sqrt"]) <= 0:
        return None
    return dict(first, sqrt=rows[-1]["sqrt"], liquidity=rows[-1]["liquidity"])


def _deposit_reask(rec):
    """True when a re-ask needs only the source's TAPE swap: the signal carries a matched deposit
    proof (option C) and exact state is not required, the same condition under which handle_signal
    lets decide() estimate (Gamma 2026-10-08, pos 71906: such a re-ask waited 2 s for the receipt
    the deposit had already replaced and expired proof_absent). A shared route that went out
    without its receipt (`needs_source_fill`, `_source_send`) waits for the receipt instead."""
    if not rec.get("deposit_proof") or rec.get("needs_source_fill"):
        return False
    from sniper.settings import memecoin as cfg
    return not cfg.MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE


class PendingSourceSwaps:
    """Decide-thread only: one bounded re-ask when the exact source reaches the tape.

    No provider reads or sleeps; checks use the existing tape lock. Ordinary lanes continue.

    `hint_fn(rec)` (memecoin_receipt_hint, MEMECOIN_RH_FAST_RECEIPT_FETCH): once per deposit-matched
    re-ask still waiting for the source's receipt proof `hint_after_s` after it was queued, a
    non-blocking datagram asks the watcher to read that receipt now. It never decides or waits here.
    """
    def __init__(self, wait_s=2.0, limit=100, hint_fn=None, hint_after_s=0.4):
        self.wait_s, self.limit, self._pending = wait_s, limit, {}
        self._expired = []      # (rec, enqueued, blocker): written by the decide loop, never here
        self.hint_fn, self.hint_after_s, self._hinted = hint_fn, hint_after_s, set()

    def __bool__(self):
        return bool(self._pending)

    def offer(self, rec, *, enqueued, now):
        key = (str(rec["tx_hash"]).lower(), str(rec["mint"]).lower())
        if key in self._pending or len(self._pending) >= self.limit:
            return False
        self._pending[key] = (rec, enqueued, now + self.wait_s)
        return True

    def ready(self, tape, *, now):
        ready = []
        for key, (rec, enqueued, deadline) in list(self._pending.items()):
            if now >= deadline:
                # NOT SILENT (2026-10-06): 4 late 4663 fills read only their first "pending"; the
                # expiry names its blocker so the fill-timing attribution can say why.
                self._pending.pop(key)
                self._hinted.discard(key)
                self._expired.append((rec, enqueued, (
                    "tape_swap_absent" if _deposit_reask(rec)
                    and tape.source_swap(rec) is None else tape.source_blocker(rec))))
            else:
                source = tape.source_swap(rec, require_source_fill=not _deposit_reask(rec))
                if source is None or source.get("reason") in {"source_fill_pending", "source_swap_pending",
                                                               "source_split_pending"}:
                    self._hint(key, rec, tape, now=now, queued=deadline - self.wait_s)
                    continue
                self._pending.pop(key)
                self._hinted.discard(key)
                ready.append((rec, enqueued))
        return ready

    def _hint(self, key, rec, tape, *, now, queued):
        if (self.hint_fn is None or key in self._hinted or now < queued + self.hint_after_s
                or not rec.get("deposit_proof") or _deposit_reask(rec)
                or tape.source_blocker(rec) != "proof_absent"):
            return
        self._hinted.add(key)
        try:
            self.hint_fn(rec)
        except Exception:
            logger.exception("rh fast: receipt hint failed for %s", rec.get("tx_hash"))

    def pop_expired(self):
        expired, self._expired = self._expired, []
        return expired


def service_pending_sources(pending, *, tape, live, db_path, now_fn, on_decided, now, deposits=None,
                            confirm=None, settle=None):
    """The decide loop's step after a signal: re-decide each re-ask whose exact source arrived, then
    write one source_pending_expired row per expiry. Decide thread, outside decide() and the tape
    lock. Returns how many re-asks were decided."""
    decided = 0
    for ready, enqueued in pending.ready(tape, now=now):
        out = None
        try:
            out = handle_signal(ready, enqueued, tape=tape, live=live, db_path=db_path,
                                now_fn=now_fn, on_decided=on_decided, deposits=deposits, confirm=confirm)
            decided += 1
        except Exception:
            logger.exception("rh fast: source re-ask failed for %s", ready.get("tx_hash"))
        if settle is not None:
            settle(ready, out)                   # D-b: a re-ask that sent nothing ends its feed hold
    for expired, enqueued, blocker in pending.pop_expired():
        if settle is not None:
            settle(expired, None)
        try:
            record_pending_expired(expired, enqueued, blocker, db_path=db_path)
        except Exception:
            logger.exception("rh fast: expiry row failed for %s", expired.get("tx_hash"))
    return decided


def lane_route(tape, mint, now):
    """The route the lane would buy `mint` through, in decide's order (native, WETH, bridge), as
    the runner's registry holds it (routes rev 3, 6.4): {kind, pool, key} with `pool`/`key` the
    pool the coin is sold through, plus {bridge, a_pool, a_key} for a bridge. None without one."""
    pool, key = tape.route(mint, now)
    if pool is not None:
        return {"kind": "v4_native", "pool": pool, "key": key}
    weth = tape.weth_route(mint, now)
    if weth is not None:
        return {"kind": "v4_weth", "pool": weth["pool"], "key": weth["key"]}
    bridge = tape.bridge_route(mint, now)
    if bridge is not None:
        return {"kind": "v4_bridge", "pool": bridge["b_pool"], "key": bridge["b_key"],
                "bridge": bridge["bridge"], "a_pool": bridge["a_pool"], "a_key": bridge["a_key"]}
    return None


def make_candidates(tape, now, *, signal_mints, armed_mints, k, recent_mints=()):
    """The mints the runner's safety cache should hold (spec §2.2), each with its route
    (`lane_route`), in the shared warm tiers (lambda + Beta, Alpha GO 2026-10-14): priority 0 for a
    mint a signal or an armed wallet touched in the last hour, 1 for one touched (decided on or
    sold) within the chain's warm window, then the `k` busiest routed mints at 10 + activity rank.
    The cache works the lowest priority first, so activity waits for every due tier-0/1 coin. A
    mint with no route is left out: the lane could not buy it."""
    entries, listed = [], set()
    for priority, mints in ((0, set(signal_mints) | set(armed_mints)), (1, set(recent_mints))):
        for mint in sorted(mints - listed):
            route = lane_route(tape, mint, now)
            if route is not None:
                entries.append(dict(route, mint=mint, priority=priority))
                listed.add(mint)
    rank = 0
    for mint in tape.busiest(now):
        if rank >= k:
            break
        if mint in listed:
            continue
        route = lane_route(tape, mint, now)
        if route is None:
            continue
        rank += 1
        entries.append(dict(route, mint=mint, priority=10 + rank))
    return entries


def make_discovery_mints(tape, now, *, signal_mints, armed_mints, k):
    """Tape-observed discovery candidates, bounded by the existing top-K and envelope cap.

    This list requests discovery work only. It never creates a native route or safety entry.
    """
    busiest = tape.busiest(now)
    touched = set(signal_mints) | set(armed_mints)
    ordered = sorted(touched.intersection(busiest)) + [m for m in busiest if m not in touched]
    out = []
    for mint in ordered:
        if len(out) >= min(32, max(0, int(k))):
            break
        # A known native pool does not exclude Pons/V3/split shared source demand.
        out.append(mint)
    return out


def _fallback(reason, **fields):
    return dict(fields, reason=reason, tx=None)


def _source_send(rec, source):
    """The source's own route, sent: a SHARED route only with its receipt. The lane's shared
    executor refuses one without it, and on the deposit path decide() is handed the source before
    the receipt (4663, 10-07..10-14: 11 armed deposit-proved v3/v4_shared signals sent early, every
    one refused source_fill_unproved). Without the receipt the signal re-asks; `needs_source_fill`
    makes that re-ask wait for the receipt, not the tape swap (`_deposit_reask`)."""
    from sniper.services.memecoin_rh_fast_lane import SHARED_ROUTE_KINDS
    if source.get("route_kind") in SHARED_ROUTE_KINDS and not source.get("source_fill"):
        return _fallback("source_fill_pending", **rec, needs_source_fill=True)
    return dict(rec, **source, reason="would_send")


def _state_checks(rec, tape, pool, sig, max_lag_blocks, prefix=""):
    """(kind, row) of `pool` for this signal, or (None, fallback reason). Records the state fields
    under `prefix` (the bridge's first leg uses "a_"). Both tape lag and pre-state age are bounded:
    an unrelated old pool cannot stand in for the wallet's source swap (SHROOM, 2026-10-04)."""
    diagnostic = {}
    kind, row = tape.state(pool, int(sig["seq"]), sig["tx_hash"], diagnostics=diagnostic)
    latest = diagnostic.get("latest_same_pool")
    if latest is not None and int(sig["seq"]) - latest["block_no"] > max_lag_blocks:
        latest["unknown_reason"] = latest.get("unknown_reason") or "stale_state"
    rec[prefix + "latest_same_pool"] = latest
    if kind is None:
        return None, row
    age, lag = int(sig["seq"]) - row["block_no"], int(sig["seq"]) - (tape.head or 0)
    rec.update({prefix + "state_kind": kind, prefix + "state_block": row["block_no"],
                prefix + "state_age_blocks": age, "tape_lag_blocks": lag})
    if not prefix:
        # The state this decision judged, on refusals too: the tape keeps about an hour, so a
        # state_stale verdict is gradable later only from its own row (Alpha P1, 2026-10-14).
        rec.update(state_sqrt=row.get("sqrt"), state_liquidity=row.get("liquidity"))
    if kind == "pre" and lag > max_lag_blocks:
        return None, "tape_behind"
    if kind == "pre" and row.get("learned") and age > max_lag_blocks:
        return None, "learned_stale"
    if kind == "pre" and age > max_lag_blocks:
        return None, "state_stale"
    return kind, row


def _hop(kind, row, src_in, in_is_c0, lp):
    """(post sqrt, source input, coin out) of the source's swap in a coin's pool: its own amounts when
    the tape holds it (exact), else `src_in` net of the LP fee applied to the pre-state. None for an
    empty pool. `in_is_c0`: the source pays currency0 (zeroForOne)."""
    if kind == "tape_post":
        a0, a1 = int(row["amount0"]), int(row["amount1"])
        return int(row["sqrt"]), (a0 if in_is_c0 else a1), (-a1 if in_is_c0 else -a0)
    net = int(src_in) * (10 ** 6 - lp) // 10 ** 6
    est = (swap_native_in if in_is_c0 else swap_one_for_zero)(row["sqrt"], row["liquidity"], net)
    if est is None:
        return None
    post, out = est
    return post, int(src_in), out


def coin_price(post_sqrt, in_is_c0):
    """The coin's price in the paying currency's atomic units per coin atomic, after the swap."""
    sq = int(post_sqrt) ** 2
    return Fraction(1 << 192, sq) if in_is_c0 else Fraction(sq, 1 << 192)


def cap_price(post_price, src_in, src_out, *, chase_pct, premium):
    """price_cap for any pair orientation: the tighter of post-trade price x (1 + chase) and the
    source's average x the execution premium. None (never 0) with both terms off."""
    from sniper.settings import memecoin as cfg
    if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
        return post_price  # Wire metadata only; the runner signs an independent output floor.
    terms = []
    if chase_pct and chase_pct > 0:
        terms.append(post_price * (1 + Fraction(str(chase_pct)) / 100))
    if premium and premium > 0 and src_out:
        terms.append(Fraction(int(src_in), int(src_out)) * Fraction(str(premium)))
    return min(terms) if terms else None


def decide(sig, *, tape, eth_usd, clip_usd, chase_pct, premium, fee_pct, max_lag_blocks, float_wei,
           gas_price_wei, low_wei, decimals, taker, now, allow_estimated_source=False, fresh_read=None):
    """What the lane would do with one M0 signal, from memory only (no I/O) except `fresh_read`'s
    one bounded read of a stale bridge leg (bridge_state_reader). Returns a record whose
    `reason` is "would_send" with the unsigned `tx`, or the named fallback (spec §4).

    Routes (spec 2026-10-02 routes rev 2), the first that exists: a native-ETH V4 pool (v1), a
    WETH-quoted V4 pool (R3), native -> bridge X -> coin over two V4 pools (R2)."""
    from sniper.execution.evm_native_float import float_covers
    rec = {}
    if not eth_usd or eth_usd <= 0:
        return _fallback("no_eth_usd")
    pay = str(sig["payment_token"]).lower()
    dec = decimals.get(pay)
    if dec is None:
        return _fallback("payment_decimals_unknown")
    eth_usd_f = Fraction(str(eth_usd))
    keep = 1 - Fraction(str(fee_pct)) / 100
    paid = Fraction(int(sig["paid"]), 10 ** int(dec))                    # in payment units
    src_in = paid * keep * 10 ** 18
    paid_in_eth = pay == CHAIN_QUOTE["4663"]["token"].lower()
    if not paid_in_eth:
        src_in /= eth_usd_f
    # What the wallet paid in USD, gross of fees: the on-chain watcher's size basis for this buy.
    rec["paid_usd"] = float(paid * eth_usd_f if paid_in_eth else paid)
    rec["src_in_wei"] = int(src_in)
    rec["clip_wei"] = clip_wei = int(Fraction(str(clip_usd)) / eth_usd_f * 10 ** 18)
    from sniper.settings.memecoin import LANE_SWAP_DEADLINE_S
    deadline = int(now) + LANE_SWAP_DEADLINE_S
    pool, key, source = tape.route(sig["mint"], now, source_sig=sig,
                                   require_source_fill=not allow_estimated_source)
    if source is not None and source.get("reason"):
        if source.get("source_post_reason"):
            rec["source_post_reason"] = source["source_post_reason"]
        return _fallback(source["reason"], **rec)
    if source is None and not allow_estimated_source:
        return _fallback("source_swap_pending", **rec)
    if source is not None and source.get("source_fill"):
        rec["source_fill"] = proof = source["source_fill"]
        rec["paid_usd"] = proof["paid_usd"]
        if proof["payment_token"] == CHAIN_QUOTE["4663"]["token"].lower():
            if proof["payment_decimals"] != 18:
                return _fallback("source_payment_unproved", **rec)
            rec["src_in_wei"] = int(proof["payment_atomic"])
        else:
            rec["src_in_wei"] = int(Fraction(str(proof["paid_usd"])) / eth_usd_f * 10**18)
    if allow_estimated_source and not (source is not None and source.get("source_fill")):
        # OPERATOR OPTION 2 (2026-10-07): without the receipt the source's average price is our own
        # estimate, so the cap is the post-trade price x (1 + chase) alone.
        premium = 0
    via_source = None
    if (allow_estimated_source and source is not None and not source.get("source_fill")
            and source["route_kind"] == "v4_shared"):
        from sniper.settings import memecoin as cfg
        if cfg.MEMECOIN_RH_FAST_DEPOSIT_SOURCE_POOL:
            # OPTION C's v4_shared (Alpha GO 2026-10-08, pos 71908): the shared executor below needs
            # the receipt this deposit replaced, so buy through the source's OWN pool instead, at its
            # exact post-swap state off the tape (B's tape_post), the cap post x (1 + chase).
            via_source = tape.source_pool_route(source["pool"], sig["mint"], now)
            if via_source.get("reason"):
                return _fallback(via_source["reason"], **rec)
            rec["pool_choice"] = "source_pool"
    if via_source is None and source is not None and (
            source["route_kind"] in {"v3_shared", "pons_shared", "split_shared"}
            or source["route_kind"] == "v4_shared" and source.get("source_fill")
            or pool is not None and source["pool"] != pool):
        # Preserve the real source output and pool. A source buy through another route never
        # pushes this native pool. Non-native V4 uses the shared executor's exact quotes;
        # bridge estimates cannot replace the proved wallet-net acquisition.
        return _source_send(rec, source)
    if via_source is not None:
        route = via_source["kind"]
        if route == "v4_bridge":
            bridge = via_source
        else:
            pool, key, in_is_c0 = via_source["pool"], via_source["key"], via_source["in_is_c0"]
    elif pool is None:
        route = None
        weth = tape.weth_route(sig["mint"], now)
        if weth is not None:
            route, pool, key = "v4_weth", weth["pool"], weth["key"]
            in_is_c0 = weth["in_is_c0"]
        else:
            bridge = tape.bridge_route(sig["mint"], now)
            if bridge is None:
                if source is not None:
                    return _source_send(rec, source)
                return _fallback(key, **rec)
            route = "v4_bridge"
    else:
        route, in_is_c0 = "v4_native", True
    selected_pool = bridge["b_pool"] if route == "v4_bridge" else pool
    if source is not None and source["pool"] != selected_pool:
        return _source_send(rec, source)
    rec["route_kind"] = route

    if route == "v4_bridge":
        a_pool, b_pool, x = bridge["a_pool"], bridge["b_pool"], bridge["bridge"]
        rec.update(pool=b_pool, key=bridge["b_key"], bridge=x, bridge_pool=a_pool,
                   hooked=v4._pool_is_hooked(bridge["b_key"]))
        from sniper.settings import memecoin as cfg
        if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
            # The runner cannot build an independent output floor for the bridge's first leg.
            return _fallback("bridge_output_state_unbound", **rec)
        a_kind, a_row = _state_checks(rec, tape, a_pool, sig, max_lag_blocks, prefix="a_")
        if (a_kind is None and a_row in ("state_stale", "learned_stale") and fresh_read is not None
                and not sig.get("bridge_fresh_read_tried")
                and _state_checks({}, tape, b_pool, sig, max_lag_blocks)[0] is not None):
            # BRIDGE FRESH READ (Alpha GO 2026-10-16): only leg A is stale, the tape is current and
            # the coin's pool passes, so leg A's state is read once per signal (a re-ask does not
            # read again) at the signal's block or the newer tape head. It is post-source when the
            # source swapped leg A in that block: the state our buy meets. No answer: today's refusal.
            sig["bridge_fresh_read_tried"] = True
            block = max(int(sig["seq"]), tape.head or 0)
            fresh, rec["a_fresh_read"], rec["a_fresh_read_ms"] = fresh_read(a_pool, block)
            if fresh is not None:
                a_kind, a_row = "fresh_read", fresh
                rec.update(a_state_kind="fresh_read", a_state_block=block,
                           a_state_age_blocks=int(sig["seq"]) - block)
        if a_kind is None:
            return _fallback("bridge_" + a_row, **rec)
        kind, row = _state_checks(rec, tape, b_pool, sig, max_lag_blocks)
        if kind is None:
            return _fallback(row, **rec)
        if rec.get("pool_choice") and kind != "tape_post":
            return _fallback("source_pool_not_tape_post", **rec)
        # Hop A at its MID rate (Gamma M1): min_out = X / cap, so a LOW X loosens the native price
        # cap; the mid is at least what the fill gets, so hop A can only tighten it.
        a_sq = int(a_row["sqrt"]) ** 2
        rec["x_mid"] = x_mid = clip_wei * a_sq >> 192
        rec.update(a_key=bridge["a_key"], a_sqrt=int(a_row["sqrt"]),
                   a_state_liquidity=int(a_row["liquidity"]), x_is_c0=bridge["x_is_c0"])
        src_in_x = (int(sig["paid"]) * keep) if pay == x else Fraction(rec["src_in_wei"]) * Fraction(a_sq, 1 << 192)
        hop = _hop(kind, row, int(src_in_x), bridge["x_is_c0"], _lp_pips(bridge["b_key"]))
        if hop is None:
            return _fallback("no_liquidity", **rec)
        post, src_in_x, src_out = hop
        rec.update(est_post_sqrt=post, src_out=src_out, src_in_x=src_in_x, state_liquidity=int(row["liquidity"]),
                   post_wei=coin_price(post, bridge["x_is_c0"]) * Fraction(1 << 192, a_sq))
        cap = cap_price(coin_price(post, bridge["x_is_c0"]), src_in_x, src_out, chase_pct=chase_pct,
                        premium=premium)
        if cap is None:
            return _fallback("no_price_cap", **rec)
        rec["cap_price"] = cap
        rec["min_out"] = min_out = x_mid * cap.denominator // cap.numerator
        from sniper.execution.evm_v4_multihop_tx import native_multihop_buy_tx
        ak, bk = bridge["a_key"], bridge["b_key"]
        if rec.get("pool_choice"):
            # A's impact and B's hook take come out of the chase headroom (min_out is at A's mid):
            # an estimate under min_out would only revert on-chain, so it is refused here.
            rec["est_out"] = est = source_pool_est_out(clip_wei, row, bk, bridge["x_is_c0"], a_row, ak)
            from sniper.settings import memecoin as cfg
            if not cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY and (est is None or est < min_out):
                return _fallback("source_pool_impact_over_cap", **rec)
        try:
            tx = native_multihop_buy_tx("4663", str(sig["mint"]).lower(),
                                        [(x, int(ak["fee"]), int(ak["tick_spacing"]), ak["hooks"]),
                                         (str(sig["mint"]).lower(), int(bk["fee"]), int(bk["tick_spacing"]),
                                          bk["hooks"])], clip_wei, min_out, deadline)
        except ValueError:
            return _fallback("build_refused", **rec)
    else:
        rec.update(pool=pool, key=key, hooked=v4._pool_is_hooked(key), in_is_c0=in_is_c0)
        kind, row = _state_checks(rec, tape, pool, sig, max_lag_blocks)
        if kind is None:
            return _fallback(row, **rec)
        if rec.get("pool_choice") and kind != "tape_post":
            return _fallback("source_pool_not_tape_post", **rec)
        rec["state_liquidity"] = int(row["liquidity"])
        rec["lp_fee_pips"] = lp = _lp_pips(key)
        hop = _hop(kind, row, rec["src_in_wei"], in_is_c0, lp)
        if hop is None:
            return _fallback("no_liquidity", **rec)
        post, src_in_wei, src_out = hop
        if source is not None and source.get("source_fill"):
            post, src_in_wei, src_out = source["source_sqrt"], rec["src_in_wei"], source["src_out"]
            from sniper.settings import memecoin as cfg
            if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
                bound_liquidity = (source["source_fill"].get("source_post") or {}).get("liquidity_atomic")
                rec["state_liquidity"] = int(bound_liquidity) if bound_liquidity is not None else None
        elif kind == "tape_post":
            rec["src_in_wei"] = src_in_wei
        rec.update(est_post_sqrt=post, src_out=src_out, post_wei=coin_price(post, in_is_c0))
        cap = cap_price(rec["post_wei"], src_in_wei, src_out, chase_pct=chase_pct, premium=premium)
        if cap is None:
            return _fallback("no_price_cap", **rec)
        rec["cap_price"] = cap
        rec["min_out"] = min_out = min_out_at(clip_wei, cap)
        if rec.get("pool_choice"):
            rec["est_out"] = est = source_pool_est_out(clip_wei, row, key, in_is_c0)
            from sniper.settings import memecoin as cfg
            if not cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY and (est is None or est < min_out):
                return _fallback("source_pool_impact_over_cap", **rec)
        try:
            tx = (native_buy_tx(key, clip_wei, min_out, deadline, taker=taker) if route == "v4_native"
                  else v4.weth_buy_tx(key, in_is_c0, clip_wei, min_out, deadline, taker=taker))
        except ValueError:
            return _fallback("build_refused", **rec)
    if float_wei is None or gas_price_wei is None:
        return _fallback("float_unknown", **rec)
    if not float_covers(float_wei, clip_wei, int(tx["gas"]), gas_price_wei, low_wei):
        return _fallback("float_short", **rec)
    return dict(rec, reason="would_send", tx=tx)


# ---- wiring in the M0 shadow process (start_decide); every read is the tape or the research host ----

def source_fills_once(conn, tape, cursor, *, limit=256):
    """Indexed bounded ledger page, called only by the dedicated read-only proof worker."""
    import json
    rows = conn.execute("SELECT id,source_valuation_json FROM onchain_wallet_detections "
                        "WHERE network_id='4663' AND id>? ORDER BY id LIMIT ?",
                        (cursor, limit)).fetchall()
    proofs = []
    for rid, raw in rows:
        cursor = rid
        try:
            parsed = json.loads(raw or "{}")
            if isinstance(parsed, dict) and isinstance(parsed.get("source_fill"), dict):
                proofs.append(parsed["source_fill"])
        except (ValueError, TypeError):
            continue
    tape.learn_source_fills(proofs)
    return cursor

_TAPE_COLS = ("id, pool, block_no, log_index, tx_sig, amount0, amount1, liquidity, sqrt_price_x96, "
              "observed_at, maker")


def open_tape(path):
    """A read-only, autocommit tape handle: no read transaction outlives a query, so it never pins
    the tape's WAL (the get_readonly_connection lesson)."""
    import sqlite3
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1, isolation_level=None,
                           check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=250")
    return conn


def _quote_sides():
    from sniper.settings import memecoin as cfg
    return frozenset({v4._NATIVE, CHAIN_QUOTE["4663"]["token"].lower(),
                      *(t.lower() for t in cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES.get("4663", ()))})


_QUOTE_SIDES = _quote_sides()


def meta_reader(conn, key_fn=None):
    """pool -> {protocol, mint, token0, token1, fee, tick_spacing, hooks}, or None while unresolved.

    THE MINT COMES FROM THE PAIR, NOT FROM onchain_pool_protocols: that table holds one row per
    SIDE of a V4 pool (66,321 of 314,422 4663 v4 rows name native 0x0), so taking its first row
    filed most native pools under 0x0 (found 2026-10-01). A native pool's coin is token1.

    THE TAPE'S KEY IS OFTEN BLANK (Gamma 2026-10-01): 72,482 of 105,560 native 4663 pools in
    pool_meta have fee, tick_spacing or hooks NULL, so their coins had no route. When the tape's
    row is incomplete or absent, `key_fn(pool)` gives the PoolManager-verified PoolKey from the
    ledger's memecoin_evm_v4_pool_keys (ledger_key_reader), and the pool is V4 by that key. A
    complete tape row is used as it is, with no ledger read."""
    def read(pool):
        row = conn.execute(
            "SELECT (SELECT p.protocol FROM onchain_pool_protocols p WHERE p.chain_id = m.chain_id "
            "AND p.pool = m.pool LIMIT 1), m.token0, m.token1, m.fee, m.tick_spacing, m.hooks "
            "FROM pool_meta m WHERE m.chain_id = '4663' AND m.pool = ?", (pool,)).fetchone()
        is_v3 = row is not None and row[0] == "v3" and all(v is not None for v in row[:3])
        if not is_v3 and (not row or any(v is None for v in row)) and key_fn is not None:
            key = key_fn(pool)
            if key:
                row = ((row[0] if row else None) or "v4", key["currency0"], key["currency1"],
                       key["fee"], key["tick_spacing"], key["hooks"])
        if not row or (not is_v3 and any(v is None for v in row)):
            return None
        protocol, token0, token1, fee, spacing, hooks = row
        token0, token1 = str(token0).lower(), str(token1).lower()
        # The coin is the side that is not a quote asset (native, WETH, a USD stable; Gamma LOW).
        mint = token0 if token1 in _QUOTE_SIDES and token0 not in _QUOTE_SIDES else token1
        return {"protocol": protocol, "mint": mint, "token0": token0, "token1": token1,
                "fee": fee, "tick_spacing": spacing, "hooks": hooks}
    return read


_KEY_FAILURES_TO_PAUSE = 5
_KEY_PAUSE_S = 60.0


def ledger_key_reader(ledger_path, *, clock=None):
    """pool -> the PoolManager-verified 4663 PoolKey from memecoin_evm_v4_pool_keys, or None: one
    read-only autocommit handle (it never pins the ledger's WAL and can never write), a primary-key
    lookup per pool the tape left unresolved; a key is immutable, so a hit is kept.

    A FAILED READ IS NOT A MISS (Gamma LOW on c1363cab3). A miss (no row) is a working read; a
    raise counts toward a pause: after `_KEY_FAILURES_TO_PAUSE` in a row the reader stops asking
    for `_KEY_PAUSE_S` (tape keys only meanwhile) and logs ONE warning, so a broken ledger never
    logs at tape rate. `read.conn` is the handle; `read.counts` has hits, misses, errors, skipped.
    Called from the tape thread only."""
    import collections
    import time
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS
    clock = clock or time.monotonic
    conn = get_readonly_connection(str(ledger_path))
    conn.execute("PRAGMA busy_timeout=250")
    manager = V4_POOL_MANAGERS["4663"]
    hits, state, counts = {}, {"streak": 0, "paused_until": 0.0}, collections.Counter()

    def read(pool):
        pool = str(pool).lower()
        if pool in hits:
            return hits[pool]
        now = clock()
        if now < state["paused_until"]:
            counts["skipped"] += 1
            return None
        try:
            key = m.get_evm_v4_pool_key(conn, chain_id="4663", pool_id=pool, pool_manager=manager)
        except Exception as exc:
            counts["errors"] += 1
            state["streak"] += 1
            logger.debug("rh fast: ledger PoolKey read failed for %s: %s", pool, exc)
            if state["streak"] >= _KEY_FAILURES_TO_PAUSE:
                state["streak"], state["paused_until"] = 0, now + _KEY_PAUSE_S
                logger.warning("rh fast: %d ledger PoolKey reads failed in a row (%s: %s); tape "
                               "keys only for %.0f s", _KEY_FAILURES_TO_PAUSE, type(exc).__name__,
                               exc, _KEY_PAUSE_S)
            return None
        state["streak"] = 0
        counts["hits" if key else "misses"] += 1
        if key:
            hits[pool] = key
        return key
    read.conn, read.counts = conn, counts
    return read


def _epoch(iso):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _maker(value):
    """The V4 Swap sender, lowercased; None when the row has none (an older tape row)."""
    return str(value).lower() if value else None


def tail_once(conn, tape, cursor, limit=5000):
    """Ingest the 4663 swaps after `cursor` (pool_ticks.id); returns the new cursor."""
    # NOT INDEXED: left to itself SQLite takes the (chain_id, ...) index and sorts every 4663 row on
    # each poll (8.5% of a core, measured); the rowid range after the cursor is the cheap walk.
    rows = conn.execute("SELECT %s FROM pool_ticks NOT INDEXED WHERE id > ? AND chain_id = '4663' "
                        "AND kind = 'v3_swap' ORDER BY id LIMIT ?" % _TAPE_COLS,
                        (cursor, limit)).fetchall()
    good = []
    for rid, pool, block, log, tx, a0, a1, liq, sqrt, observed, maker in rows:
        try:
            good.append(dict(pool=pool, block_no=int(block), log_index=int(log or 0), tx_sig=tx,
                             amount0=int(a0), amount1=int(a1), liquidity=int(liq), sqrt=int(sqrt),
                             observed=_epoch(observed), maker=_maker(maker)))
        except (TypeError, ValueError):
            continue                                   # a row without depth fields: no state
    tape.ingest(good)
    return rows[-1][0] if rows else cursor


def poll_snapshot(rpc, own, weth, *, clock=None):
    """Our 4663 wallet as the research host sees it: native and WETH balances, the pending nonce
    and the gas price, stamped with the HOST time the poll STARTED (M2 step 2: the runner's lane
    reads nothing before its send, and trusts this only if no execution finished after
    `read_at`). Returns (snapshot, None) or (None, kind); a failure is absent, never zero, and
    "rate_limit" makes the caller back off."""
    import re
    import time
    if not own:
        return None, "no_address"
    read_at = (clock or time.time)()
    calls = (("native_wei", "eth_getBalance", [own, "latest"]),
             ("weth_wei", "eth_call", [{"to": weth, "data": "0x70a08231"
                                        + str(own)[2:].lower().rjust(64, "0")}, "latest"]),
             ("nonce", "eth_getTransactionCount", [own, "pending"]),
             ("gas_price_wei", "eth_gasPrice", []))
    snap = {}
    batch = getattr(rpc, "batch", None)
    for offset in range(0, len(calls), 3):  # dRPC free batches permit at most three items
        chunk = calls[offset:offset + 3]
        replies = (batch([(m, p) for _n, m, p in chunk]) if batch is not None
                   else (rpc(m, p) for _n, m, p in chunk))
        for (name, _method, _params), (result, error) in zip(chunk, replies):
            if error is not None or not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", result):
                return None, ("rate_limit" if isinstance(error, dict)
                              and (error.get("http") == 429 or error.get("code") == 15) else "error")
            snap[name] = int(result, 16)
    if len(snap) != len(calls):
        return None, "error"
    return dict(snap, read_at=read_at), None


_DECISIONS = """
CREATE TABLE IF NOT EXISTS rh_fast_decisions (
    id INTEGER PRIMARY KEY, tx_hash TEXT NOT NULL, seq INTEGER, wallet TEXT, handle TEXT, mint TEXT,
    arrival_utc TEXT, decided_at TEXT, reason TEXT, pool TEXT, hooked INTEGER, state_kind TEXT,
    state_block INTEGER, state_age_blocks INTEGER, tape_lag_blocks INTEGER, payment_token TEXT, paid TEXT, src_in_wei TEXT,
    src_out TEXT, est_post_sqrt TEXT, cap_price REAL, chase_pct REAL, premium REAL, fee_pct REAL,
    clip_usd REAL, clip_wei TEXT, min_out TEXT, eth_usd REAL, float_wei TEXT, gas_price_wei TEXT,
    tx_bytes INTEGER, handoff_ms REAL, decide_ms REAL, activity_rank INTEGER, lp_fee_pips INTEGER,
    state_liquidity TEXT, true_checked INTEGER NOT NULL DEFAULT 0, true_pool TEXT, true_block INTEGER,
    true_post_sqrt TEXT, true_in_wei TEXT, true_out TEXT, true_liquidity TEXT);
CREATE INDEX IF NOT EXISTS idx_rh_fast_decisions_tx ON rh_fast_decisions(tx_hash);
CREATE TABLE IF NOT EXISTS rh_fast_heartbeat (
    at TEXT NOT NULL, rpc_calls INTEGER, rpc_errors INTEGER, rpc_429 INTEGER, tape_rows INTEGER,
    tape_lag_s REAL, pools INTEGER, decisions INTEGER, threads_alive INTEGER,
    research_slot INTEGER, research_slots TEXT);
"""


def _s(v):
    return None if v is None else str(v)


_LEARN_REASONS = ("no_pool", "no_native_v4_pool", "source_swap_pending", "source_fill_pending")
_SOURCE_REASK_REASONS = (*_LEARN_REASONS, "no_state", "state_stale", "learned_stale", "tape_behind",
                         "bridge_no_state", "bridge_state_stale", "bridge_learned_stale", "bridge_tape_behind",
                         "source_swap_pending", "source_split_pending")
# Option C: the fill's order id, the deposit match (memecoin_rh_deposit_proof.DepositProofs.take:
# matched or why not; NULL while MEMECOIN_RH_FAST_DEPOSIT_PROOF is off), the fill-minus-deposit
# arrival margin on the host clock, and the proving deposit's signature.
_DEPOSIT_COLUMNS = (("order_id", "TEXT"), ("deposit_status", "TEXT"), ("deposit_margin_ms", "REAL"),
                    ("deposit_origin_tx", "TEXT"))
# Routes rev 2: which route each decision took, the bridge leg and its mid conversion (M1's record).
_ROUTE_COLUMNS = (("route_kind", "TEXT"), ("bridge", "TEXT"), ("bridge_pool", "TEXT"), ("x_mid", "TEXT"),
                  ("a_state_kind", "TEXT"), ("a_state_age_blocks", "INTEGER"))
# Option C's v4_shared: 'source_pool' when the buy goes through the source's own pool, and the
# estimated coin out (net of LP fees and the hook's take) its min_out was checked against.
_SOURCE_POOL_COLUMNS = (("pool_choice", "TEXT"), ("est_out", "TEXT"))
# Alpha P1 (2026-10-14): the pool state judged (refusals too) and an ambiguous source's cause.
_TRACE_COLUMNS = (("state_sqrt", "TEXT"), ("source_post_reason", "TEXT"))
# Bridge fresh read (2026-10-16): the read's outcome ("ok" or why not) and its wall time; NULL = no read.
_FRESH_READ_COLUMNS = (("a_fresh_read", "TEXT"), ("a_fresh_read_ms", "REAL"))


def learn_pools(conn, meta_fn, mint):
    """[(pool, meta, newest swap row)] of every pool of `mint` the tape knows (routes spec R1): the
    pool list from pool_meta (both token indexes), the meta through `meta_fn` (meta_reader, with the
    ledger key fallback), the newest v3_swap row by the (chain_id, pool, block_no) index. A pool with
    no meta or no recorded swap is left out: no state, no route. Local reads only, never RPC."""
    mint = str(mint).lower()
    pools = [r[0] for r in conn.execute(
        "SELECT pool FROM pool_meta WHERE chain_id = '4663' AND token0 = ? UNION "
        "SELECT pool FROM pool_meta WHERE chain_id = '4663' AND token1 = ?", (mint, mint))]
    out = []
    for pool in sorted(pools):
        meta = meta_fn(pool)
        if not meta:
            continue
        row = conn.execute("SELECT %s FROM pool_ticks WHERE chain_id = '4663' AND pool = ? AND "
                           "kind = 'v3_swap' ORDER BY block_no DESC, log_index DESC LIMIT 1"
                           % _TAPE_COLS, (pool,)).fetchone()
        if not row:
            continue
        _rid, _pool, block, log, tx, a0, a1, liq, sqrt, observed, maker = row
        try:
            out.append((pool, meta, dict(pool=pool, block_no=int(block), log_index=int(log or 0),
                                         tx_sig=tx, amount0=int(a0), amount1=int(a1),
                                         liquidity=int(liq), sqrt=int(sqrt), observed=_epoch(observed),
                                         maker=_maker(maker))))
        except (TypeError, ValueError):
            continue
    return out


# Live inputs the decide does not read; they ride the datagram to the runner's lane (M2 step 2).
_LANE_ONLY = ("snapshot", "eth_usd_at")


def handle_signal(rec, enqueued_perf, *, tape, live, db_path, now_fn, on_decided=None, learn=None,
                  deposits=None, confirm=None):
    """Decide one signal (timed), hand it to `on_decided` (M2: the datagram to the runner's lane),
    then rank and write it (untimed). Returns the decision.

    `deposits` (option C, MEMECOIN_RH_FAST_DEPOSIT_PROOF): the in-memory DepositProofs. A signal
    whose order id binds a deposit proof is decided without waiting for its receipt when
    MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE is off; every other signal is decided as before.
    `confirm(proof, rec)` queues a sent proof's post-hoc confirmation."""
    import sqlite3
    import time
    from sniper.util.clock import real_utc_now_iso
    t0 = time.perf_counter()
    params = dict(live)
    params["now"] = now_fn()
    from sniper.services.memecoin_wallet_caps import entry_cap_usd
    params["clip_usd"] = entry_cap_usd(rec.get("source_user_id"), "4663",
                                        existing_cap=params["clip_usd"])
    if deposits is not None:
        if "deposit_status" not in rec:          # a re-ask keeps its first answer: single use
            proof, status, margin_ms = deposits.take(rec, decimals=params.get("decimals") or {},
                                                     now=time.time())
            rec.update(deposit_proof=proof, deposit_status=status, deposit_margin_ms=margin_ms)
        from sniper.settings import memecoin as cfg
        params["allow_estimated_source"] = (bool(rec.get("deposit_proof"))
                                            and not cfg.MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE)
    try:
        out = decide(rec, tape=tape, **{k: v for k, v in params.items() if k not in _LANE_ONLY})
    except Exception:
        logger.exception("rh fast decide failed for %s", rec.get("tx_hash"))
        out = _fallback("decide_error")
    t1 = time.perf_counter()
    out.update(decide_ms=(t1 - t0) * 1000.0, handoff_ms=(t0 - enqueued_perf) * 1000.0)
    if out.get("needs_source_fill"):
        rec["needs_source_fill"] = True          # its re-ask waits for the receipt (_source_send)
    if on_decided is not None:
        try:
            on_decided(rec, out, params)
        except Exception:
            logger.exception("rh fast: lane hand-off failed for %s", rec.get("tx_hash"))
    if confirm is not None and out.get("reason") == "would_send" and rec.get("deposit_proof"):
        try:
            confirm(rec["deposit_proof"], rec)
        except Exception:
            logger.exception("rh fast: deposit confirmation queue failed for %s", rec.get("tx_hash"))
    if learn is not None and out.get("reason") in _LEARN_REASONS:
        try:
            learn(str(rec.get("mint") or "").lower())      # after the timed decide: R1 learn
        except Exception:
            logger.exception("rh fast: learn offer failed for %s", rec.get("mint"))
    try:
        rank = tape.rank(rec["mint"], params["now"])
    except Exception:
        rank = None
    _insert_decision(db_path, rec, out, params, rank)
    return out


def record_pending_expired(rec, enqueued_perf, blocker, *, db_path, now_perf=None):
    """One rh_fast_decisions row for an exact-source re-ask that expired: reason
    source_pending_expired:<blocker>. Called by the decide loop after ready(), outside any decide
    call and outside the tape lock, through the same insert a decision uses."""
    import time
    now_perf = time.perf_counter() if now_perf is None else now_perf
    out = _fallback("source_pending_expired:%s" % blocker)
    out.update(decide_ms=0.0, handoff_ms=(now_perf - enqueued_perf) * 1000.0)
    _insert_decision(db_path, rec, out, {}, None)
    return out


def _insert_decision(db_path, rec, out, params, rank):
    import sqlite3
    from sniper.util.clock import real_utc_now_iso
    cap = out.get("cap_price")
    db = sqlite3.connect(db_path, timeout=5)
    try:
        db.executescript(_DECISIONS)
        from sniper.services.memecoin_rh_fast_lane import add_missing_columns
        add_missing_columns(db, "rh_fast_decisions",
                            _ROUTE_COLUMNS + _DEPOSIT_COLUMNS + _SOURCE_POOL_COLUMNS + _TRACE_COLUMNS
                            + _FRESH_READ_COLUMNS)
        db.execute(
            "INSERT INTO rh_fast_decisions (tx_hash, seq, wallet, handle, mint, arrival_utc, "
            "decided_at, reason, pool, hooked, state_kind, state_block, state_age_blocks, "
            "tape_lag_blocks, payment_token, paid, src_in_wei, src_out, est_post_sqrt, cap_price, chase_pct, premium, "
            "fee_pct, clip_usd, clip_wei, min_out, eth_usd, float_wei, gas_price_wei, tx_bytes, "
            "handoff_ms, decide_ms, activity_rank, lp_fee_pips, state_liquidity, route_kind, bridge, "
            "bridge_pool, x_mid, a_state_kind, a_state_age_blocks, order_id, deposit_status, "
            "deposit_margin_ms, deposit_origin_tx, pool_choice, est_out, state_sqrt, source_post_reason, "
            "a_fresh_read, a_fresh_read_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?,?)",
            (rec.get("tx_hash"), rec.get("seq"), rec.get("wallet"), rec.get("handle"), rec.get("mint"),
             rec.get("arrival_utc"), real_utc_now_iso(), out["reason"], out.get("pool"),
             None if out.get("hooked") is None else int(out["hooked"]), out.get("state_kind"),
             out.get("state_block"), out.get("state_age_blocks"), out.get("tape_lag_blocks"),
             rec.get("payment_token"),
             _s(rec.get("paid")), _s(out.get("src_in_wei")), _s(out.get("src_out")),
             _s(out.get("est_post_sqrt")), None if cap is None else float(cap), params.get("chase_pct"),
             params.get("premium"), params.get("fee_pct"), params.get("clip_usd"), _s(out.get("clip_wei")),
             _s(out.get("min_out")), params.get("eth_usd"), _s(params.get("float_wei")),
             _s(params.get("gas_price_wei")),
             None if not out.get("tx") else (len(out["tx"]["data"]) - 2) // 2,
             out["handoff_ms"], out["decide_ms"], rank, out.get("lp_fee_pips"),
             _s(out.get("state_liquidity")), out.get("route_kind"), out.get("bridge"),
             out.get("bridge_pool"), _s(out.get("x_mid")), out.get("a_state_kind"),
             out.get("a_state_age_blocks"), rec.get("order_id"), rec.get("deposit_status"),
             rec.get("deposit_margin_ms"), (rec.get("deposit_proof") or {}).get("origin_tx"),
             out.get("pool_choice"), _s(out.get("est_out")), _s(out.get("state_sqrt")),
             out.get("source_post_reason"), out.get("a_fresh_read"), out.get("a_fresh_read_ms")))
        db.commit()
    finally:
        db.close()


def enrich_true(*, db_path, tape_path, give_up=200):
    """The source's real swap (pool, post sqrt price, pool input, output) from the tape, written onto
    its decision: the reference the estimate and the fee haircut are graded against. The tape keeps
    about an hour, so this runs live; a row is given up (true_checked=2) after `give_up` passes."""
    import sqlite3
    db = sqlite3.connect(db_path, timeout=5)
    filled = 0
    try:
        db.executescript(_DECISIONS)
        rows = db.execute("SELECT id, tx_hash, pool, true_checked FROM rh_fast_decisions "
                          "WHERE true_checked <= 0").fetchall()
        if not rows:
            return 0
        tape = open_tape(tape_path)
        try:
            for rid, tx, pool, checked in rows:
                hits = tape.execute("SELECT pool, block_no, sqrt_price_x96, amount0, amount1, log_index, "
                                    "liquidity "
                                    "FROM pool_ticks WHERE chain_id = '4663' AND tx_sig = ? "
                                    "AND kind = 'v3_swap'", (tx,)).fetchall()
                if hits:
                    # Our chosen pool when the source swapped in it, else its last swap.
                    hit = next((h for h in hits if h[0] == pool), max(hits, key=lambda h: h[5]))
                    a0, a1 = int(hit[3]), int(hit[4])
                    db.execute("UPDATE rh_fast_decisions SET true_checked=1, true_pool=?, "
                               "true_block=?, true_post_sqrt=?, true_in_wei=?, true_out=?, "
                               "true_liquidity=? WHERE id=?",
                               (hit[0], hit[1], hit[2], str(a0), str(-a1), hit[6], rid))
                    filled += 1
                else:
                    db.execute("UPDATE rh_fast_decisions SET true_checked=? WHERE id=?",
                               (2 if checked <= -give_up else checked - 1, rid))
        finally:
            tape.close()
        db.commit()
    finally:
        db.close()
    return filled


def _research_rpc(url, counters, spacing_s=0.5, rotation=None):
    """JSON-RPC on the 4663 RESEARCH host only (it 429s at about 10 calls in 1.5 s), spaced. `url`
    is the base research key; `rotation` (a research_rotation_4663.Rotation, built from the
    environment when None) sends each read to its active slot and moves on a quota answer."""
    import json
    import threading
    import time
    import urllib.error
    import urllib.request
    if rotation is None:
        from sniper.execution.research_rotation_4663 import Rotation
        rotation = Rotation(drpc_fallback=True)
    last, lock = [0.0], threading.Lock()

    def post(method, target, headers=None, body=None, timeout=5):
        req = urllib.request.Request(target, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:                                  # a JSON-RPC body says quota or rate limit
                answer = json.loads(exc.read() or b"{}")
            except Exception:
                answer = None
            if isinstance(answer, (dict, list)):
                for row in answer if isinstance(answer, list) else [answer]:
                    if isinstance(row, dict) and isinstance(row.get("error"), dict):
                        row["error"]["http"] = exc.code
                return exc.code, answer
            return exc.code, {"error": {"http": exc.code}}
        except Exception as exc:
            return None, {"error": {"transport": type(exc).__name__}}

    def request(payload):
        with lock:
            wait = spacing_s - (time.monotonic() - last[0])
            if wait > 0:
                time.sleep(wait)
            body = json.dumps(payload).encode()
            counters["rpc_calls"] += 1
            status = None
            try:
                status, out = rotation.send(url, lambda target: post("POST", target, body=body),
                    allow_batch=isinstance(payload, list),
                    identity=lambda target: post("POST", target, body=json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode()))
            except OSError as exc:
                out = {"error": {"transport": type(exc).__name__}}
            finally:
                last[0] = time.monotonic()
        return status, out

    def call(method, params):
        _status, out = request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(out, dict) or (out.get("error") and not isinstance(out["error"], dict)):
            out = {"error": {"malformed": True}}
        if out.get("error"):
            counters["rpc_errors"] += 1
            counters["rpc_429"] += out["error"].get("http") == 429
        return out.get("result"), out.get("error")

    def batch(calls):
        payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                   for i, (m, p) in enumerate(calls, 1)]
        status, out = request(payload)
        error = out.get("error") if isinstance(out, dict) else None
        if status != 200:
            error = dict(error) if isinstance(error, dict) else {}
            error["http"] = status
        rows = {}
        if status == 200 and isinstance(out, list):
            for row in out:
                if (not isinstance(row, dict) or type(row.get("id")) is not int
                        or not 1 <= row["id"] <= len(calls) or row["id"] in rows):
                    break
                rows[row["id"]] = row
            else:
                if len(rows) == len(calls):
                    replies = [(rows[i].get("result"), rows[i].get("error"))
                               for i in range(1, len(calls) + 1)]
                    counters["rpc_errors"] += any(e is not None for _r, e in replies)
                    counters["rpc_429"] += any(isinstance(e, dict) and e.get("http") == 429
                                               for _r, e in replies)
                    return replies
        error = error if isinstance(error, dict) and error else {"malformed": True}
        counters["rpc_errors"] += 1
        counters["rpc_429"] += error.get("http") == 429
        return [(None, error)] * len(calls)

    call.batch = batch  # private background client; no process-wide RPC mutation
    return call


def bridge_state_reader(url, *, timeout_s=0.2, cooldown_s=60.0, post=None, clock=None, log=None):
    """read(pool_id, block) -> (row or None, outcome, ms): ONE extsload(bytes32[]) eth_call to the 4663
    PoolManager for a V4 pool's slot0 and liquidity words at the explicit `block` (the bridge fresh
    read, MEMECOIN_RH_FAST_BRIDGE_FRESH_READ). `url` is the tx_read host; a node without that block
    errors rather than answering older state. Decide thread only; the cooldown is this reader's own.

    Bounded: the call runs on a daemon thread joined for `timeout_s`; a later answer is dropped
    (its socket times out at 1 s). All or nothing, as _onchain_liquidities: an answer whose offset or
    length word disagrees, an rpc error, a non-200, a zero price or zero liquidity is no row. A 403
    or 429 cools the reader for `cooldown_s`: publicnode is the watcher's detection host."""
    import json
    import threading
    import time
    import urllib.error
    import urllib.request
    from sniper.execution.evm_v4_multihop import _EXTSLOAD_BATCH, _LIQUIDITY_OFFSET, _liquidity_slot
    clock = clock or time.monotonic
    manager, cool_until = v4.V4["4663"]["pool_manager"], [float("-inf")]

    def http_post(body):
        req = urllib.request.Request(url, json.dumps(body).encode(), {
            "Content-Type": "application/json", "User-Agent": "curl/8.5.0"})   # publicnode 403s urllib's
        try:
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, None
    post = post or http_post

    def ask(pool_id, block):
        if clock() < cool_until[0]:
            return None, "cooldown"
        liq = _liquidity_slot(pool_id)
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [
            {"to": manager, "data": "0x" + _EXTSLOAD_BATCH + "%064x" % 32 + "%064x" % 2
             + "%064x" % (int(liq, 16) - _LIQUIDITY_OFFSET) + liq}, hex(block)]}
        box = {}

        def run():
            try:
                box["answer"] = post(body)
            except Exception as exc:
                box["error"] = type(exc).__name__
        worker = threading.Thread(target=run, name="rh-fast-bridge-read", daemon=True)
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            return None, "timeout"
        if "error" in box:
            return None, "transport:" + box["error"]
        status, answer = box["answer"]
        if status in (403, 429):
            cool_until[0] = clock() + cooldown_s
        if status != 200 or not isinstance(answer, dict):
            return None, "http_%s" % status
        raw = answer.get("result")
        if answer.get("error") is not None or not isinstance(raw, str):
            return None, "rpc_error"
        try:
            words = bytes.fromhex(raw[2:]) if raw.startswith("0x") else b""
        except ValueError:
            words = b""
        if (len(words) != 128 or int.from_bytes(words[:32], "big") != 32
                or int.from_bytes(words[32:64], "big") != 2):
            return None, "malformed"
        sqrt = int.from_bytes(words[64:96], "big") & ((1 << 160) - 1)
        liquidity = int.from_bytes(words[96:128], "big") & ((1 << 128) - 1)
        if not sqrt or not liquidity:
            return None, "empty_state"
        return dict(pool=pool_id, block_no=block, log_index=-1, tx_sig=None, sqrt=sqrt,
                    liquidity=liquidity, learned=True), "ok"

    def read(pool_id, block):
        t0 = time.perf_counter()
        try:
            row, outcome = ask(pool_id, int(block))
        except Exception as exc:                 # a malformed pool id: no row, today's refusal
            row, outcome = None, "error:" + type(exc).__name__
        ms = (time.perf_counter() - t0) * 1000.0
        if log is not None:
            log("[rh-fast] bridge fresh read %s @%s %s %.0f ms" % (str(pool_id)[:12], block, outcome, ms))
        return row, outcome, ms
    return read


def make_bridge_fresh_read(log):
    """The decide's `fresh_read` (bridge_state_reader on MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663), or
    None: knob off, or no tx_read URL (logged)."""
    import os
    from sniper.settings import memecoin as cfg
    if not cfg.MEMECOIN_RH_FAST_BRIDGE_FRESH_READ:
        return None
    url = os.environ.get("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663", "").strip()
    if not url:
        log("[rh-fast] bridge fresh read is on but MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663 is empty: no read")
        return None
    return bridge_state_reader(url, timeout_s=cfg.MEMECOIN_RH_FAST_BRIDGE_FRESH_READ_TIMEOUT_S,
                               cooldown_s=cfg.MEMECOIN_ONCHAIN_EVM_TX_READ_COOLDOWN_S, log=log)


def refresh_warm_set(warm, ledger_path, now_s, chain="4663"):
    """Tier 0 (decided on in the last hour) and tier 1 (decided on or sold by an armed wallet in the
    chain's warm window) into `warm`, the shared warm tiers (lambda + Beta, Alpha GO 2026-10-14). An
    unreadable ledger keeps the last good sets. Returns True when both were read."""
    from datetime import datetime, timezone
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg

    def iso(at):
        return datetime.fromtimestamp(at, timezone.utc).isoformat()
    try:
        since = iso(now_s - cfg.safety_warm_window_s(chain))
        hour = m.armed_bought_mints(ledger_path, chain, iso(now_s - 3600))
        recent = m.armed_bought_mints(ledger_path, chain, since) | m.armed_sold_mints(ledger_path, chain, since)
    except Exception:
        logger.warning("rh fast: armed warm set unreadable; keeping the last one", exc_info=True)
        return False
    warm.update(hour=hour, recent=recent)
    return True


def feed_clip_usd(mode=None):
    """The clip the feed's float check uses: the funded max clip, except while the lane is live,
    when it is the lane's pilot clip if smaller (Alpha 2026-10-02, B1). In live the feed forwards
    only would_send, and the lane never buys above its pilot clip, so a max-clip check would stop
    every lane signal as float_short and demand a float the lane cannot spend."""
    from sniper.settings import memecoin as cfg
    mode = cfg.memecoin_rh_fast_lane_mode() if mode is None else mode
    clip = cfg.evm_max_clip_usd_for("4663", slots=99)
    return min(clip, cfg.MEMECOIN_RH_FAST_LANE_PILOT_CLIP_USD) if mode == "live" else clip


def start_decide(*, db_path, tape_path, log=print, ledger_path=None, watched_fn=None, head_fn=None):
    """Start M1's off-path workers in the M0 shadow process. Returns (offer, threads): the feed thread
    calls offer(signal record) and never waits. Reads: the tape (read-only) and the research host."""
    import collections
    import os
    import queue
    import threading
    import time
    from datetime import datetime, timezone
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now, real_utc_now_iso

    if cfg.MEMECOIN_REAL_WALLET_CAPS_ENABLED and ledger_path is not None:
        from sniper.persistence.db_core import get_readonly_connection
        from sniper.services.memecoin_wallet_caps import CACHE
        CACHE.start(lambda: get_readonly_connection(str(ledger_path)), real_utc_now_iso)
    url = os.environ.get("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "").strip()
    own = next((a.strip().lower() for a in cfg.MEMECOIN_OWN_ADDRESSES.split(",")
                if a.strip().startswith("0x")), None)
    weth = CHAIN_QUOTE["4663"]["token"].lower()
    stables = [t.lower() for t in cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES.get("4663", ())]
    conn = open_tape(tape_path)
    key_fn = None
    if ledger_path is not None:
        try:
            key_fn = ledger_key_reader(ledger_path)
        except Exception:                      # today's tape-only keys; logged, never fatal
            logger.exception("rh fast: ledger PoolKey reader unavailable (tape keys only)")
    tape = TapeState(meta_reader(conn, key_fn=key_fn))
    live = dict(eth_usd=None, clip_usd=feed_clip_usd(),
                chase_pct=cfg.MEMECOIN_REAL_CHASE_MAX_PCT, premium=cfg.MEMECOIN_REAL_MAX_EXEC_PREMIUM,
                fee_pct=cfg.MEMECOIN_RH_FAST_SOURCE_FEE_PCT,
                max_lag_blocks=cfg.MEMECOIN_RH_FAST_TAPE_MAX_LAG_BLOCKS, float_wei=None,
                gas_price_wei=None, low_wei=cfg.MEMECOIN_EVM_GAS_TOPUP_LOW_WEI,
                decimals={weth: 18}, taker=own or v4._ZERO_ADDRESS, snapshot=None, eth_usd_at=None,
                fresh_read=make_bridge_fresh_read(log))
    counters = collections.Counter()
    work = queue.Queue(maxsize=100)
    from sniper.services.memecoin_rh_fast_lane import (
        default_address, make_feed_hold_sender, make_lane_sender, send_signal)
    lane_address = default_address()
    on_decided = make_lane_sender(cfg.memecoin_rh_fast_lane_mode(), lane_address)
    # D-b: hold the signal for the runner's live lane at the feed's arrival (MEMECOIN_RH_FAST_FEED_HOLD).
    feed_holds = make_feed_hold_sender(cfg.memecoin_rh_fast_lane_mode(), lane_address)
    deposits = confirmer = deposit_counters = None
    deposit_threads = []
    signal_seen = {}                                   # mint -> host time of its last signal
    warm = {"hour": set(), "recent": set()}          # the shared warm tiers 0 and 1 (last good read)
    seen = {"observed": 0.0}
    learn_q, learned_at = queue.Queue(maxsize=256), {}  # routes R1: mints to load, and when

    def learn_offer(mint):
        if not mint or time.monotonic() - learned_at.get(mint, -1e9) < 600:
            return
        try:
            learn_q.put_nowait(mint)
        except queue.Full:
            counters["learn_drops"] += 1

    relay_offer = None
    if ((cfg.MEMECOIN_RH_RELAY_PREWARM or cfg.MEMECOIN_RH_RELAY_PREBUY or cfg.MEMECOIN_BSC_RELAY_PREBUY)
            and ledger_path is not None
            and watched_fn is not None and head_fn is not None):
        # P2/P3 (spec 2026-10-14-rh-relay-prebuy-design.md): its own thread, fed by the deposit socket.
        from sniper.services.memecoin_rh_relay_prebuy import start_relay_poller
        relay_offer, relay_thread = start_relay_poller(
            tape=tape, live=live, learn=learn_offer, watched_fn=watched_fn, head_fn=head_fn,
            tape_path=tape_path, db_path=db_path, address=lane_address, log=log, ledger_path=ledger_path)
        deposit_threads.append(relay_thread)
    if ledger_path is not None:
        from sniper.services.memecoin_rh_deposit_proof import start_deposit_proofs
        deposits, confirmer, deposit_counters, threads_started = start_deposit_proofs(
            ledger_path=ledger_path, db_path=db_path, log=log, on_proof=relay_offer)
        deposit_threads += threads_started
        if relay_offer is not None and not threads_started:
            log("[rh-relay] no deposit socket (deposit proofs off): the poller hears no deposit")
    confirm = None if confirmer is None else (lambda proof, rec: confirmer.add(proof, rec))

    def learn_loop():
        """Routes R1, its OWN thread (Gamma LOW1): never the tape thread the decide's tape_post
        depends on. Its own read-only tape handle and its own read-only ledger key reader."""
        lconn = open_tape(tape_path)
        lkey = None
        if ledger_path is not None:
            try:
                lkey = ledger_key_reader(ledger_path)
            except Exception:
                logger.exception("rh fast learn: ledger PoolKey reader unavailable (tape keys only)")
        meta = meta_reader(lconn, key_fn=lkey)
        while True:
            mint = learn_q.get()
            if time.monotonic() - learned_at.get(mint, -1e9) < 600:
                continue
            t0 = time.perf_counter()
            try:
                pools = learn_pools(lconn, meta, mint)
                tape.learn(pools)
                learned_at[mint] = time.monotonic()
                counters["learned_mints"] += 1
                counters["learned_pools"] += len(pools)
                log("[rh-fast] learned %s: %d pools in %.1f ms" % (mint[:10], len(pools),
                                                                   (time.perf_counter() - t0) * 1000.0))
            except Exception:
                # Backs off like a success: the candidates loop would otherwise re-offer it every
                # cycle (Gamma LOW on 4c2fd530b).
                learned_at[mint] = time.monotonic()
                logger.exception("rh fast: learn failed for %s", mint)
                time.sleep(1.0)

    def offer(rec):
        feed_holds.hold(rec)                    # before the queue and the receipt wait (D-b)
        try:
            work.put_nowait((rec, time.perf_counter()))
        except queue.Full:
            counters["decide_drops"] += 1
            feed_holds.release(rec)

    def tape_loop():
        cursor = None
        while True:
            try:
                if cursor is None:            # inside the try: a locked tape at start retries (Gamma LOW)
                    since = datetime.fromtimestamp(real_utc_now() - _ACTIVITY_S,
                                                   timezone.utc).isoformat()
                    first = conn.execute("SELECT min(id) FROM pool_ticks WHERE chain_id = '4663' "
                                         "AND observed_at >= ?", (since,)).fetchone()[0]
                    cursor = (first - 1) if first else (
                        conn.execute("SELECT max(id) FROM pool_ticks").fetchone()[0] or 0)
                cursor = tail_once(conn, tape, cursor)
            except Exception:
                logger.exception("rh fast: tape tail failed (retrying)")
                time.sleep(1.0)
            time.sleep(0.1)

    def source_fill_loop():
        if ledger_path is None:
            return
        from sniper.persistence.db_core import get_readonly_connection
        proof_conn, cursor = None, None
        while True:
            try:
                if proof_conn is None:
                    proof_conn = get_readonly_connection(str(ledger_path))
                    proof_conn.execute("PRAGMA busy_timeout=50")
                if cursor is None:
                    # Old observations lack the new proof. Bound startup instead of scanning history.
                    latest = proof_conn.execute("SELECT max(id) FROM onchain_wallet_detections "
                                                "WHERE network_id='4663'").fetchone()[0] or 0
                    cursor = max(0, latest - 4096)
                cursor = source_fills_once(proof_conn, tape, cursor)
            except Exception:
                logger.exception("rh fast: receipt proof cache unavailable; ordinary lane continues")
                if proof_conn is not None:
                    proof_conn.close()
                proof_conn = None
                time.sleep(1.0)
            time.sleep(0.05)

    from sniper.execution.research_rotation_4663 import Rotation
    from sniper.services.memecoin_rh_deposit_proof import heartbeat_counters
    rotation = Rotation(log=log, drpc_fallback=True)

    def float_loop():
        rpc = _research_rpc(url, counters, rotation=rotation) if url else None
        if rpc is None:
            log("[rh-fast] MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663 unset: float stays unknown")
        period, ok_at, beat = 10.0, 0.0, time.monotonic()
        while True:
            started, healthy = time.monotonic(), False
            try:
                result = poll_snapshot(rpc, own, weth) if rpc is not None else None
                t = open_tape(tape_path)
                try:
                    usd = t.execute("SELECT usd, observed FROM volume_quote_prices WHERE "
                                    "chain_id = '4663' AND token = ?", (v4._NATIVE,)).fetchone()
                    decimals = {weth: 18}
                    for token in stables:
                        d = t.execute("SELECT decimals FROM volume_token_decimals WHERE "
                                      "chain_id = '4663' AND lower(token) = ?", (token,)).fetchone()
                        if d and d[0] is not None:
                            decimals[token] = int(d[0])
                    newest = t.execute("SELECT max(observed_at) FROM pool_ticks WHERE "
                                       "chain_id = '4663'").fetchone()[0]
                finally:
                    t.close()
                tick = float_tick(result, usd, now=real_utc_now(), ok_at=ok_at, period=period)
                ok_at, period = tick.pop("ok_at"), tick.pop("period")
                live.update(tick, decimals=decimals)
                seen["observed"] = _epoch(newest)
                if on_decided is not None:
                    # M2 step 2b: the mints the runner's safety cache should hold.
                    host_now = time.time()
                    for mint in [m for m, at in signal_seen.items() if host_now - at > 3600]:
                        signal_seen.pop(mint, None)
                    if ledger_path is not None:
                        refresh_warm_set(warm, ledger_path, real_utc_now())
                    armed = warm["hour"]
                    entries = make_candidates(tape, real_utc_now(), signal_mints=set(signal_seen),
                                              armed_mints=armed, recent_mints=warm["recent"],
                                              k=cfg.MEMECOIN_RH_FAST_SAFETY_CANDIDATES_K)
                    routed = {e["mint"] for e in entries}
                    for mint in (set(signal_seen) | set(armed)) - routed:
                        learn_offer(str(mint).lower())          # routes R1: a candidate with no route
                    payload = {"kind": "candidates", "sent_epoch": host_now, "entries": entries}
                    if cfg.MEMECOIN_RH_PRESIGNAL_DISCOVERY_ENABLED:
                        payload["discovery_mints"] = make_discovery_mints(
                            tape, real_utc_now(), signal_mints=set(signal_seen), armed_mints=armed,
                            k=cfg.MEMECOIN_RH_FAST_SAFETY_CANDIDATES_K)
                    send_signal(lane_address, payload)
                if time.monotonic() - beat >= 60:
                    beat = time.monotonic()
                    pipeline = getattr(relay_offer, "pipeline", None)
                    _beat(db_path, counters, real_utc_now() - seen["observed"], tape,
                          real_utc_now_iso, sum(t.is_alive() for t in threads), rotation,
                          deposit=None if deposits is None else heartbeat_counters(
                              deposit_counters, deposits, confirmer),
                          relay=None if pipeline is None else dict(
                              pipeline.beat(signal_seen), queue_drops=relay_offer.counters["queue_drops"]))
                healthy = result is not None and result[1] is None
            except Exception:
                logger.exception("rh fast: float/price poll failed")
            # Healthy work counts toward cadence; failures retain their full cooldown.
            time.sleep(max(0.0, period - (time.monotonic() - started)) if healthy else period)

    receipt_hint = None
    if cfg.MEMECOIN_RH_FAST_RECEIPT_FETCH:
        from sniper.services.memecoin_receipt_hint import Sender, socket_path
        hint_sender = Sender(socket_path("4663"))

        def receipt_hint(rec):
            sent = hint_sender.send(rec)
            log("[rh-fast] receipt hint %s %s" % (str(rec.get("tx_hash"))[:12], "sent" if sent else
                "not_sent:%s" % (hint_sender.stats["last_error"] or "invalid")))

    def decide_loop():
        last_enrich = time.monotonic()
        pending_sources = PendingSourceSwaps(hint_fn=receipt_hint, hint_after_s=cfg.MEMECOIN_RH_FAST_RECEIPT_FETCH_AFTER_S)
        while True:
            try:
                rec, enqueued = work.get(timeout=0.025 if pending_sources else 5.0)
            except queue.Empty:
                rec = None
            if rec is not None:
                signal_seen[str(rec.get("mint") or "").lower()] = time.time()
                out = None
                try:
                    out = handle_signal(rec, enqueued, tape=tape, live=live, db_path=db_path,
                                        now_fn=real_utc_now, on_decided=on_decided, learn=learn_offer,
                                        deposits=deposits, confirm=confirm)
                    if (out.get("reason") in _SOURCE_REASK_REASONS
                            and pending_sources.offer(rec, enqueued=enqueued, now=time.monotonic())):
                        out = {"reason": "reask_pending"}          # the re-ask settles its hold
                    counters["decisions"] += 1
                except Exception:
                    logger.exception("rh fast: decision write failed for %s", rec.get("tx_hash"))
                if (out or {}).get("reason") != "reask_pending":
                    feed_holds.settle(rec, out)
            counters["decisions"] += service_pending_sources(
                pending_sources, tape=tape, live=live, db_path=db_path, now_fn=real_utc_now,
                on_decided=on_decided, now=time.monotonic(), deposits=deposits, confirm=confirm,
                settle=feed_holds.settle)
            if time.monotonic() - last_enrich >= 5:
                last_enrich = time.monotonic()
                try:
                    enrich_true(db_path=db_path, tape_path=tape_path)
                except Exception:
                    logger.exception("rh fast: true-swap enrichment failed")

    threads = [threading.Thread(target=fn, name=name, daemon=True) for fn, name in (
        (tape_loop, "rh-fast-tape"), (float_loop, "rh-fast-float"), (decide_loop, "rh-fast-decide"),
        (learn_loop, "rh-fast-learn"), (source_fill_loop, "rh-fast-source-fill"))]
    for thread in threads:
        thread.start()
    log("[rh-fast] M1 decide shadow started: sign-less; rows -> %s" % db_path)
    offer.relay = relay_offer          # the feed writer notes Relay fills by order id (source outcome)
    stream_status = deposit_counters.get('stream') if deposit_counters is not None else None
    if isinstance(stream_status, dict) and stream_status.get('provider') == 'shyft':
        offer.deposit_stream_status = stream_status  # actual live map; diagnostic only
    return offer, threads + deposit_threads


def _beat(db_path, counters, lag_s, tape, now_iso, alive, rotation=None, deposit=None, relay=None):
    import json
    import sqlite3
    from sniper.services.memecoin_rh_fast_lane import add_missing_columns, rotation_columns
    db = sqlite3.connect(db_path, timeout=5)
    try:
        db.executescript(_DECISIONS)
        add_missing_columns(db, "rh_fast_heartbeat",
                            (("research_slot", "INTEGER"), ("research_slots", "TEXT"),
                             ("deposit_counters", "TEXT"), ("relay_counters", "TEXT")))
        slot, slots = rotation_columns(rotation)
        db.execute("INSERT INTO rh_fast_heartbeat (at, rpc_calls, rpc_errors, rpc_429, tape_rows, "
                   "tape_lag_s, pools, decisions, threads_alive, research_slot, research_slots, "
                   "deposit_counters, relay_counters) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (now_iso(), counters["rpc_calls"], counters["rpc_errors"], counters["rpc_429"],
                    tape.ingested, round(lag_s, 3), len(tape._meta), counters["decisions"], alive,
                    slot, slots, None if deposit is None else json.dumps(dict(deposit), sort_keys=True),
                    None if relay is None else json.dumps(relay, sort_keys=True)))
        db.commit()
    finally:
        db.close()


def float_tick(result, usd_row, *, now, ok_at, period):
    """One snapshot poll's effect on the live inputs, pure (Gamma LOW: the loop's rules were
    untested). `result` is poll_snapshot's (snapshot, error) or None without a research host;
    `usd_row` the tape's (usd, observed epoch) for native ETH. A good read sets the float, the gas
    price and the snapshot and resets the period to 10 s; a 429 doubles it up to 60 s; no good read
    for 60 s makes all three absent; an ETH price older than 600 s is absent."""
    out = {}
    snap, error = result if result else (None, "no_rpc")
    if error is None:
        out.update(float_wei=snap["native_wei"], gas_price_wei=snap["gas_price_wei"], snapshot=snap)
        ok_at, period = now, 10.0
    elif error == "rate_limit":
        period = min(period * 2, 60.0)
    if now - ok_at > 60:
        out.update(float_wei=None, gas_price_wei=None, snapshot=None)
    fresh = usd_row and usd_row[0] and now - float(usd_row[1] or 0) <= 600
    out.update(eth_usd=float(usd_row[0]) if fresh else None,
               eth_usd_at=float(usd_row[1]) if fresh else None)
    return dict(out, ok_at=ok_at, period=period)
