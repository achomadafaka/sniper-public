"""4663 Relay prewarm (P2) and pre-buy (P3): spec docs/superpowers/specs/2026-10-14-rh-relay-prebuy-design.md.

A verified Relay deposit (memecoin_rh_deposit_proof: an armed user's own signed Solana deposit) names an
order id. Relay's request for that order names the destination mint while it is still unsettled, about
260 ms before the fill reaches our feed (Banksy / koy, 2026-10-14). This module asks for it, on its own
thread in the feed process, and accepts it only on an exact match. Both uses default off:
MEMECOIN_RH_RELAY_PREWARM, MEMECOIN_RH_RELAY_PREBUY.
"""
from __future__ import annotations

import collections
import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

SOLANA_CHAIN_ID = 792703809
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
UNSETTLED = frozenset({"pending", "depositing", "waiting", "submitted"})
REQUESTS_URL = "https://api.relay.link/requests/v2"


class RelayHttpError(Exception):
    """A non-200 answer from Relay: 429 and 5xx stop the poller at once."""

    def __init__(self, status):
        super().__init__("relay http %s" % status)
        self.status = int(status)


def _addr(value):
    text = str(value or "").strip().lower()
    return text if text.startswith("0x") and len(text) == 42 else None


def request_match(payload, deposit, *, watched):
    """(found, None) when Relay's answer for the deposit's order is ONE unsettled request bound for an
    enabled chain whose recipient is the depositor's own user's verified wallet ON THAT CHAIN (`watched`:
    {chain id: {address: (source_user_id, handle)}}, a roster per chain whose pre-buy is on), paying exactly
    the deposit's USDC; else (None, reason). Anything missing or unknown is a refusal, never a match."""
    requests = payload.get("requests") if isinstance(payload, dict) else None
    if not isinstance(requests, list):
        return None, "malformed_request"
    if not requests:
        return None, "not_indexed"
    if len(requests) != 1:
        return None, "ambiguous_request"
    request = requests[0]
    try:
        data = request["data"]
        meta = data["metadata"]
        out, inn = meta["currencyOut"], meta["currencyIn"]
        out_chain, mint = out["currency"]["chainId"], _addr(out["currency"]["address"])
        in_chain, in_token = inn["currency"]["chainId"], inn["currency"]["address"]
        in_amount = int(inn["amount"])
        recipient = _addr(request["recipient"])
        status = str(request["status"])
        out_txs = data.get("outTxs") or []
        request_id = str(request["id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, "malformed_request"
    if status == "success" or out_txs:
        return None, "indexed_after_fill"
    if status not in UNSETTLED:
        return None, "request_not_unsettled"
    roster = watched.get(out_chain) if isinstance(watched, dict) and type(out_chain) is int else None
    if roster is None:
        return None, "destination_not_enabled"
    owner = roster.get(recipient) if recipient else None
    if owner is None or owner[0] not in set(deposit["uids"]):
        return None, "recipient_mismatch"
    if mint is None:
        return None, "malformed_request"
    if in_chain != SOLANA_CHAIN_ID or in_token != USDC:
        return None, "currency_in_not_usdc"
    if in_amount != int(deposit["amount_atomic"]):
        return None, "amount_mismatch"
    return dict(mint=mint, wallet=recipient, uid=owner[0], handle=owner[1], request_id=request_id,
                status=status, created_at=request.get("createdAt"), out_chain=out_chain,
                in_amount=in_amount, deposit=deposit), None


def prebuy_initiation(found):
    """The pre-buy's source_initiation (relay-request-prebuy-v1): the armed user's own signed Solana
    deposit (the socket proved it: signer, payment, verified depositor) and the Relay request bound to
    it (recipient, destination chain, the deposit's exact USDC). Checked by source_initiation_refusal."""
    from sniper.services.memecoin_source_initiation import RELAY_PREBUY_PROOF_VERSION
    deposit = found["deposit"]
    return dict(schema_version=1, proof_version=RELAY_PREBUY_PROOF_VERSION, network_id=str(found["out_chain"]),
                mint=found["mint"], beneficiary=found["wallet"], source_user_id=found["uid"],
                origin_network_id="solana", origin_wallet=deposit["depositor"],
                origin_tx=deposit["signature"], origin_slot=deposit["slot"],
                origin_amount_atomic=deposit["amount_atomic"], order_id=deposit["order_id"],
                origin_identity_user_id=found["uid"], request_id=found["request_id"],
                request_recipient=found["wallet"], request_out_chain=found["out_chain"],
                request_in_amount=found["in_amount"])


class RequestPoller:
    """At most `max_polls` asks per deposit, `1/per_s` apart, on THIS poller's own burst rate (Alpha:
    6/s, headroom under Relay's 10/s per key beside the watchers' verify calls). The first 429 or 5xx
    stops the deposit at once; nothing is retried past the polls. Counters feed the heartbeat."""

    def __init__(self, *, fetch, watched_fn, clock=time.monotonic, sleep=time.sleep, max_polls=8,
                 per_s=6.0, url=REQUESTS_URL):
        self.fetch, self.watched_fn, self.clock, self.sleep = fetch, watched_fn, clock, sleep
        self.max_polls, self.gap, self.url = int(max_polls), 1.0 / float(per_s), url
        self.counters = collections.Counter()
        self.found_ms = collections.deque(maxlen=256)
        self._request_lock, self._last_start = threading.Lock(), None

    def request(self, url):
        """One request-start budget for first polls, retries and delayed outcomes on this worker."""
        with self._request_lock:
            if self._last_start is not None:
                wait = self.gap - (self.clock() - self._last_start)
                if wait > 0:
                    self.sleep(wait)
            self._last_start = self.clock()
            return self.fetch(url)

    def poll(self, deposit):
        self.counters["deposits_seen"] += 1
        started = self.clock()
        url = "%s?orderId=%s" % (self.url, deposit["order_id"])
        for attempt in range(self.max_polls):
            self.counters["polls"] += 1
            try:
                payload = self.request(url)
            except RelayHttpError as exc:
                counter = "http_429" if exc.status == 429 else "http_5xx" if exc.status >= 500 else "http_other"
                self.counters[counter] += 1
                return None, counter
            except Exception as exc:                    # transport: unknown, stop (never a match)
                self.counters["transport"] += 1
                logger.warning("relay prebuy poll transport failure: %s", type(exc).__name__)
                return None, "transport"
            found, why = request_match(payload, deposit, watched=self.watched_fn())
            if found is not None:
                found["found_ms"] = (self.clock() - started) * 1000.0
                self.found_ms.append(found["found_ms"])
                self.counters["found"] += 1
                return found, None
            if why != "not_indexed":
                self.counters["rejected_mismatch" if why.endswith("_mismatch") or why in {
                    "destination_not_enabled", "currency_in_not_usdc"} else why] += 1
                return None, why
        self.counters["not_indexed_after_%d" % self.max_polls] += 1
        return None, "not_indexed_after_%d" % self.max_polls


def default_fetch(*, api_key=None, client=None, timeout_s=0.8):
    """GET with keep-alive; a non-200 raises RelayHttpError, a body that is not JSON raises ValueError."""
    from sniper.execution.fast_http import PooledHttpClient
    client = client or PooledHttpClient(max_connections=2, max_keepalive=2)
    headers = {"accept": "application/json", **({"x-api-key": api_key} if api_key else {})}

    def fetch(url):
        status, payload, _date = client.request("GET", url, headers=headers, timeout=timeout_s)
        if status != 200:
            raise RelayHttpError(status)
        if isinstance(payload, str):
            payload = json.loads(payload)
        return payload
    return fetch


# --- P3: the pre-buy decision on the detector's tape (its own thread; no network) -------------------

def _key_of(fields):
    """The coin pool's V4 key from a decision's fields (the B pool on a bridge)."""
    return fields["key"]


def _hook_known(key):
    """No hook, or a hook whose output take is known (MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS)."""
    from sniper.execution import evm_v4_quote as v4
    from sniper.settings import memecoin as cfg
    return (not v4._pool_is_hooked(key)
            or str(key.get("hooks") or "").lower() in cfg.MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS)


def prebuy_decide(found, *, tape, eth_usd, clip_usd, max_impact_pct, max_lag_blocks, chain_head, gap_fn, now):
    """(fields, None) for a pre-buy datagram, or (None, reason). The coin's route as the decide picks it
    (native, then WETH, then a bridge), priced at the pool's CURRENT tape state: our clip's estimated
    output (net of LP fees and a known hook's take) against the pool's spot. The cap is spot x (1 +
    max_impact_pct); min_out is the clip at that cap, so we never pay more than that, or the swap
    reverts. The tape must be current (chain_head - tape.head <= max_lag_blocks) and gap-free over
    (the pool's last row, chain_head]; there is no pool-age bound (Alpha option 1, 2026-10-14)."""
    from fractions import Fraction
    from sniper.services import memecoin_rh_fast_decide as fd
    if not eth_usd or eth_usd <= 0:
        return None, "no_eth_usd"
    if tape.head is None or chain_head is None:
        return None, "tape_empty"
    if int(chain_head) - int(tape.head) > int(max_lag_blocks):
        return None, "tape_behind"
    mint = str(found["mint"]).lower()
    clip_wei = int(Fraction(str(clip_usd)) / Fraction(str(eth_usd)) * 10 ** 18)
    if clip_wei <= 0:
        return None, "no_clip"
    fields = dict(clip_wei=clip_wei, state_kind="prebuy_tape",
                  tape_lag_blocks=int(chain_head) - int(tape.head))
    routed = tape.route(mint, now)
    if routed[0] is not None:
        fields.update(route_kind="v4_native", pool=routed[0], key=routed[1], in_is_c0=True)
    else:
        weth = tape.weth_route(mint, now)
        bridge = None if weth is not None else tape.bridge_route(mint, now)
        if weth is not None:
            fields.update(route_kind="v4_weth", pool=weth["pool"], key=weth["key"], in_is_c0=weth["in_is_c0"])
        elif bridge is not None:
            fields.update(route_kind="v4_bridge", pool=bridge["b_pool"], key=bridge["b_key"],
                          bridge=bridge["bridge"], a_pool=bridge["a_pool"], a_key=bridge["a_key"],
                          x_is_c0=bridge["x_is_c0"], in_is_c0=bridge["x_is_c0"])
        else:
            return None, routed[1]
    keys = [fields["key"]] + ([fields["a_key"]] if fields["route_kind"] == "v4_bridge" else [])
    if not all(isinstance(k, dict) and k.get("tick_spacing") is not None for k in keys):
        return None, "unknown_v4_key"
    if not all(_hook_known(k) for k in keys):
        return None, "unknown_hook"
    state_kind, row = tape.state(fields["pool"], int(chain_head) + 1, None)
    if state_kind is None:
        return None, row
    lows = [int(row["block_no"])]
    a_row = None
    if fields["route_kind"] == "v4_bridge":
        a_kind, a_row = tape.state(fields["a_pool"], int(chain_head) + 1, None)
        if a_kind is None:
            return None, "bridge_" + a_row
        lows.append(int(a_row["block_no"]))
    fields["state_block"] = int(row["block_no"])
    fields["state_age_blocks"] = int(chain_head) + 1 - int(row["block_no"])
    if gap_fn(min(lows), int(chain_head)):
        return None, "tape_gap"
    in_is_c0 = fields["in_is_c0"]
    spot = fd.coin_price(row["sqrt"], in_is_c0)               # paying-side atomic per coin atomic
    if fields["route_kind"] == "v4_bridge":
        a_sq = int(a_row["sqrt"]) ** 2
        fields["a_sqrt"] = int(a_row["sqrt"])
        paid = clip_wei * a_sq >> 192                         # X for our clip, at hop A's mid
        est_out = fd.source_pool_est_out(clip_wei, row, fields["key"], in_is_c0, a_row, fields["a_key"])
        spot_wei = spot * Fraction(1 << 192, a_sq)
    else:
        paid = clip_wei
        est_out = fd.source_pool_est_out(clip_wei, row, fields["key"], in_is_c0)
        spot_wei = spot
    if not est_out or est_out <= 0 or spot <= 0:
        return None, "no_liquidity"
    impact = (Fraction(paid, est_out) / spot - 1) * 100
    fields.update(est_out=int(est_out), impact_pct=float(impact), spot_wei_num=spot_wei.numerator,
                  spot_wei_den=spot_wei.denominator)
    if impact > Fraction(str(max_impact_pct)):
        return None, "impact_over_cap"
    cap = spot * (1 + Fraction(str(max_impact_pct)) / 100)
    fields.update(cap_num=cap.numerator, cap_den=cap.denominator,
                  min_out=paid * cap.denominator // cap.numerator)
    return fields, None


def tape_gap_fn(conn):
    """gap_fn for prebuy_decide: True when an unresolved 4663 tick gap overlaps [lo, hi], or the gap
    evidence cannot be read (unknown is a gap)."""
    import sqlite3

    def gap(lo, hi):
        try:
            return conn.execute("SELECT 1 FROM tick_gaps WHERE chain_id='4663' AND resolved_at IS NULL "
                                "AND to_block>=? AND from_block<=? LIMIT 1", (int(lo), int(hi))).fetchone() is not None
        except sqlite3.Error:
            return True
    return gap



# --- the feed side's wiring: one thread, rh-relay-poller, fed by the deposit socket (spec §2.1-§2.3) -

_REQUESTS = """
CREATE TABLE IF NOT EXISTS rh_relay_requests (
    order_id TEXT PRIMARY KEY, uids TEXT, depositor TEXT, deposit_sig TEXT, deposit_slot INTEGER,
    amount_atomic INTEGER, deposit_seen_at TEXT, polled_at TEXT, poll_reason TEXT, found_ms REAL,
    mint TEXT, wallet TEXT, uid TEXT, request_id TEXT, request_status TEXT, warmed INTEGER,
    prebuy_reason TEXT, prebuy_sent_epoch REAL, chain_head INTEGER, state_age_blocks INTEGER,
    impact_pct REAL, route_kind TEXT, route_pool TEXT, source_fill_tx TEXT, source_fill_block INTEGER,
    source_outcome TEXT, outcome_detail TEXT, outcome_at TEXT)
"""
OUTCOME_AFTER_S = 30.0          # the source outcome is settled this long after the request was found
# Relay's own word for a request that never filled, when our feed saw no fill by then.
_RELAY_OUTCOMES = {"success": "landed", "refund": "refunded", "refunded": "refunded", "failure": "failed"}


def _pct(values, p):
    """Nearest-rank percentile of a sorted list, or None for none."""
    return values[min(len(values) - 1, int(round((len(values) - 1) * p / 100.0)))] if values else None
_PAYLOAD_FIELDS = ("route_kind", "pool", "key", "in_is_c0", "bridge", "a_pool", "a_key", "a_sqrt", "x_is_c0",
                   "cap_num", "cap_den", "clip_wei", "min_out", "state_kind", "tape_lag_blocks",
                   "spot_wei_num", "spot_wei_den")


def prebuy_payload(found, fields, *, chain_head, live, arrival_utc, sent_epoch):
    """The relay_prebuy_v1 datagram (memecoin_rh_fast_lane.PREBUY_KIND) for one decision: the lane's
    route fields, the detector's snapshot and ETH price, the deposit's USD and the proof. `tx_hash` is
    the Relay order id, never a source transaction; `seq` is the chain head the decision priced at."""
    deposit = found["deposit"]
    order = str(deposit["order_id"]).lower()
    payload = {
        "kind": lane.PREBUY_KIND, "tx_hash": order, "seq": int(chain_head), "wallet": found["wallet"],
        "mint": found["mint"], "handle": found.get("handle"), "source_user_id": found["uid"],
        "permit_nonce": "relay:" + order, "arrival_utc": arrival_utc, "receipt_clock": None,
        "sent_epoch": float(sent_epoch), "paid_usd": int(deposit["amount_atomic"]) / 10 ** 6,
        "snapshot": live.get("snapshot"), "eth_usd": live.get("eth_usd"), "eth_usd_at": live.get("eth_usd_at"),
        "source_initiation": prebuy_initiation(found),
        "prebuy": {"order_id": order, "deposit_sig": deposit["signature"], "deposit_slot": deposit["slot"],
                   "request_id": found["request_id"], "request_status": found.get("status"),
                   "deposit_seen_at": arrival_utc, "request_found_at": found.get("found_at"),
                   "found_ms": found.get("found_ms"), "state_block": fields.get("state_block"),
                   "state_age_blocks": fields.get("state_age_blocks"), "impact_pct": fields.get("impact_pct"),
                   "est_out": fields.get("est_out")}}
    payload.update({k: fields.get(k) for k in _PAYLOAD_FIELDS})
    return payload


def relay_rosters(owners_fn):
    """{chain id: {address: (uid, handle)}} for each chain whose pre-buy (or 4663's prewarm) is on: the
    poller matches a request only against its destination chain's roster. `owners_fn(chain)` is
    evm_watched_owners on the caller's read-only ledger handle."""
    from sniper.settings import memecoin as cfg
    rosters = {}
    if cfg.MEMECOIN_RH_RELAY_PREWARM or cfg.MEMECOIN_RH_RELAY_PREBUY:
        rosters[4663] = owners_fn("4663")
    if cfg.MEMECOIN_BSC_RELAY_PREBUY:
        rosters[56] = owners_fn("56")
    return rosters


def bsc_prebuy_payload(found, *, arrival_utc, sent_epoch):
    """The relay_prebuy_v1 datagram for a 56 match: identity, the deposit's USD, the proof and the Relay
    meta. No route or price: the BSC lane prices it on its own host (spec §3.3). `tx_hash` is the order."""
    deposit = found["deposit"]
    order = str(deposit["order_id"]).lower()
    return {"kind": lane.PREBUY_KIND, "chain": "56", "tx_hash": order, "wallet": found["wallet"],
            "mint": found["mint"], "handle": found.get("handle"), "source_user_id": found["uid"],
            "permit_nonce": "relay:" + order, "arrival_utc": arrival_utc, "sent_epoch": float(sent_epoch),
            "paid_usd": int(deposit["amount_atomic"]) / 10 ** 6, "source_initiation": prebuy_initiation(found),
            "prebuy": {"order_id": order, "deposit_sig": deposit["signature"], "deposit_slot": deposit["slot"],
                       "request_id": found["request_id"], "request_status": found.get("status"),
                       "deposit_seen_at": arrival_utc, "request_found_at": found.get("found_at"),
                       "found_ms": found.get("found_ms")}}


class RelayPipeline:
    """One verified deposit, on the poller thread: Relay's request (RequestPoller), then the warm (P2:
    the decide learns the coin's pools; the lane's safety cache hears it at priority -1) and the
    pre-buy (P3: prebuy_decide, then one datagram to the lane), each behind its own knob. One row per
    deposit in the feed's own SQLite. Nothing here signs, reserves or touches the ledger."""

    def __init__(self, *, poller, tape, live, learn, send, head_fn, gap_fn, watched_fn, db_path, address,
                 prewarm, prebuy, clip_usd, max_impact_pct, now_fn, now_iso, registry_ok, clock=time.time,
                 bsc_address=None, bsc_send=None):
        # BSC 56 (spec 2026-10-14-bsc-relay-prebuy-design.md §3.1): a 56 match is handed to the BSC lane's
        # socket, unpriced; the runner prices it on the lane's own host.
        self.bsc_address, self.bsc_send = bsc_address, bsc_send
        self.poller, self.tape, self.live, self.learn, self.send = poller, tape, live, learn, send
        # registry_ok(depositor, uid): the depositor is STILL the uid's verified, conflict-free Solana
        # identity (memecoin_relay_deposit_store.registry_reader; the rule DepositProofs takes under).
        self.registry_ok = registry_ok
        self.head_fn, self.gap_fn, self.watched_fn = head_fn, gap_fn, watched_fn
        self.db_path, self.address = db_path, address
        self.prewarm, self.prebuy = bool(prewarm), bool(prebuy)
        self.clip_usd, self.max_impact_pct = clip_usd, max_impact_pct
        self.now_fn, self.now_iso, self.clock = now_fn, now_iso, clock
        self.counters = collections.Counter()
        # A request buying a quote asset (native, WETH/WBNB, a stable) is no coin to warm or pre-buy (Gamma
        # LOW 2): {chain id: addresses}.
        self.quote_assets = {}
        self.warmed = collections.OrderedDict()        # mint -> host time warmed (warmed_then_signal)
        # THE SOURCE OUTCOME (spec §2.5): the feed's Relay fills by order id (the writer thread notes
        # them), and each found request's settle time. Both bounded; the lock guards them.
        self._lock = threading.Lock()
        self.fills = collections.OrderedDict()         # order id -> {tx_hash, block, mint}
        self.due = collections.deque()                 # (host epoch, order id)

    def handle(self, deposit, seen_at):
        row = dict(order_id=str(deposit["order_id"]).lower(), uids=",".join(deposit["uids"]),
                   depositor=deposit["depositor"], deposit_sig=deposit["signature"], deposit_slot=deposit["slot"],
                   amount_atomic=int(deposit["amount_atomic"]), deposit_seen_at=seen_at)
        armed = {owner[0] for roster in (self.watched_fn() or {}).values() for owner in roster.values()}
        if not set(deposit["uids"]) & armed:
            # A deposit of a user armed only on a chain without a pre-buy (Relay option A's roster): no
            # Relay call.
            self.counters["not_armed_prebuy_chain"] += 1
            return None
        row["polled_at"] = self.now_iso()
        found, why = self.poller.poll(deposit)
        if found is not None and found["mint"] in self.quote_assets.get(found["out_chain"], ()):
            found, why = None, "quote_asset_out"
            self.counters[why] += 1
        row["poll_reason"] = why or "found"
        if found is not None:
            found["found_at"] = self.now_iso()
            row.update(found_ms=found.get("found_ms"), mint=found["mint"], wallet=found["wallet"],
                       uid=found["uid"], request_id=found["request_id"], request_status=found.get("status"))
            self.due.append((self.clock() + OUTCOME_AFTER_S, row["order_id"]))
            if found["out_chain"] == 56:
                self._handoff_56(found, row, seen_at)
            else:
                self.learn(found["mint"])              # the decide's tape holds the coin's pools
                if self.prewarm:
                    row["warmed"] = int(self._warm(found["mint"]))
                if self.prebuy:
                    self._prebuy(found, row, seen_at)
        try:
            self._record(row)
        except Exception:
            logger.exception("relay poller: request row not written for order %s", row["order_id"])
        return row

    def _warm(self, mint):
        from sniper.services.memecoin_rh_fast_decide import lane_route
        route = lane_route(self.tape, mint, self.now_fn())
        if route is None:
            self.counters["warm_no_route"] += 1
            return False
        sent = self.send(self.address, {"kind": "candidates", "sent_epoch": self.clock(),
                                        "entries": [dict(route, mint=mint, priority=-1)]})
        if not sent:
            self.counters["warm_send_failed"] += 1
            return False
        self.counters["warmed"] += 1
        with self._lock:
            self.warmed[mint] = self.clock()
            while len(self.warmed) > 256:
                self.warmed.popitem(last=False)
        return True

    def _prebuy(self, found, row, seen_at):
        why = self._registry_refusal(found)
        if why is not None:
            row["prebuy_reason"] = why
            self.counters["prebuy_refused:" + why] += 1
            return
        head = self.head_fn()
        row["chain_head"] = head
        fields, why = prebuy_decide(found, tape=self.tape, eth_usd=self.live.get("eth_usd"),
                                    clip_usd=self.clip_usd, max_impact_pct=self.max_impact_pct,
                                    max_lag_blocks=self.live["max_lag_blocks"], chain_head=head,
                                    gap_fn=self.gap_fn, now=self.now_fn())
        if fields is not None and self.live.get("snapshot") is None:
            fields, why = None, "no_snapshot"             # the lane re-checks the wallet from it
        if fields is None:
            row["prebuy_reason"] = why
            self.counters["prebuy_refused:" + why] += 1
            return
        payload = prebuy_payload(found, fields, chain_head=head, live=self.live, arrival_utc=seen_at,
                                 sent_epoch=self.clock())
        sent = self.send(self.address, payload)
        row.update(prebuy_reason="sent" if sent else "send_failed", prebuy_sent_epoch=payload["sent_epoch"],
                   state_age_blocks=fields.get("state_age_blocks"), impact_pct=fields.get("impact_pct"),
                   route_kind=fields.get("route_kind"), route_pool=fields.get("pool"))
        self.counters["prebuy_sent" if sent else "prebuy_send_failed"] += 1

    def _handoff_56(self, found, row, seen_at):
        """A 56 match goes to the BSC lane unpriced (the feed holds no 56 state), after the same registry
        re-read as 4663's; the lane's worker prices, warms and decides."""
        why = self._registry_refusal(found)
        if why is None and self.bsc_send is None:
            why = "no_bsc_lane"
        if why is not None:
            row["prebuy_reason"] = why
            self.counters["prebuy_refused_56:" + why] += 1
            return
        payload = bsc_prebuy_payload(found, arrival_utc=seen_at, sent_epoch=self.clock())
        sent = self.bsc_send(self.bsc_address, payload)
        row.update(prebuy_reason="sent_56" if sent else "send_failed", prebuy_sent_epoch=payload["sent_epoch"])
        self.counters["prebuy_sent_56" if sent else "prebuy_send_failed_56"] += 1

    def _registry_refusal(self, found):
        """None while the depositor is still the uid's verified identity, read now (a conflict set after
        the deposit frame revokes it); unknown is a refusal."""
        if self.registry_ok is None:
            return "registry_unavailable"
        try:
            return None if self.registry_ok(found["deposit"]["depositor"], found["uid"]) else "registry_revoked"
        except Exception:
            logger.exception("relay poller: registry read failed for order %s", found["deposit"].get("order_id"))
            return "registry_error"

    def note_fills(self, records):
        """The feed writer's batch: each Relay fill's order id (decode_relay_fill), off the feed thread."""
        with self._lock:
            for rec in records:
                order = str(rec.get("order_id") or "").lower()
                if order and rec.get("side", "buy") == "buy":
                    self.fills[order] = {"tx_hash": rec.get("tx_hash"), "block": rec.get("seq"),
                                         "mint": rec.get("mint")}
                    self.fills.move_to_end(order)
            while len(self.fills) > 1024:
                self.fills.popitem(last=False)

    def settle_due(self, now):
        """At most one due request's source outcome: landed when the feed saw its fill, else ONE Relay
        call on the poller's rate (refunded / failed / landed per Relay, none_30s while unsettled); an
        unanswered call is unknown, never none. Returns the outcome, or None when nothing was due."""
        if not self.due or self.due[0][0] > now:
            return None
        _at, order = self.due.popleft()
        with self._lock:
            fill = self.fills.get(order)
        tx = block = None
        if fill is not None:
            outcome, detail, tx, block = "landed", "feed", fill["tx_hash"], fill["block"]
        else:
            self.counters["outcome_polls"] += 1
            try:
                requests = self.poller.request("%s?orderId=%s" % (self.poller.url, order)).get("requests")
                request = requests[0] if isinstance(requests, list) and len(requests) == 1 else None
                status = str(request["status"]) if isinstance(request, dict) else None
            except Exception as exc:                       # 429, 5xx, transport, malformed: unknown
                status, outcome, detail = None, "unknown", "relay_error:%s" % getattr(exc, "status", type(exc).__name__)
            else:
                outcome, detail = _RELAY_OUTCOMES.get(status, "none_30s"), "relay:%s" % status
                if outcome == "landed":
                    out_txs = (request.get("data") or {}).get("outTxs") or []
                    tx = (out_txs[0] or {}).get("hash") if out_txs and isinstance(out_txs[0], dict) else None
        self.counters["source_outcome:" + outcome] += 1
        import sqlite3
        try:
            db = sqlite3.connect(self.db_path, timeout=2)
            try:
                db.execute(_REQUESTS)
                db.execute("UPDATE rh_relay_requests SET source_outcome=?, outcome_detail=?, source_fill_tx=?, "
                           "source_fill_block=?, outcome_at=? WHERE order_id=?",
                           (outcome, detail, tx, block, self.now_iso(), order))
                db.commit()
            finally:
                db.close()
        except Exception:
            logger.exception("relay poller: outcome not written for order %s", order)
        return outcome

    def beat(self, signal_seen=None):
        """The heartbeat's relay counters (spec §2.6): the poller's, this pipeline's, the found-latency
        p50/p90 and, given the decide's signal_seen (mint -> host time), warmed_then_signal."""
        out = dict(self.poller.counters)
        out.update(self.counters)
        found = sorted(self.poller.found_ms)
        out.update(mint_found_ms_p50=_pct(found, 50), mint_found_ms_p90=_pct(found, 90))
        if signal_seen is not None:
            with self._lock:
                warmed = list(self.warmed.items())
            out["warmed_then_signal"] = sum(1 for mint, at in warmed if (signal_seen.get(mint) or 0) >= at)
        return out

    def _record(self, row):
        import sqlite3
        db = sqlite3.connect(self.db_path, timeout=2)
        try:
            db.execute(_REQUESTS)
            cols = sorted(row)
            db.execute("INSERT OR IGNORE INTO rh_relay_requests (%s) VALUES (%s)"
                       % (", ".join(cols), ", ".join("?" * len(cols))), [row[c] for c in cols])
            db.commit()
        finally:
            db.close()


def start_relay_poller(*, tape, live, learn, watched_fn, head_fn, tape_path, db_path, address, log, ledger_path,
                       send=None):
    """The poller thread in the feed process, when MEMECOIN_RH_RELAY_PREWARM or _PREBUY is on. Returns
    `offer(proof)`, which the deposit socket calls for each new verified deposit (no I/O: a bounded
    queue; a full queue drops and counts). offer.pipeline and offer.counters feed the heartbeat."""
    import queue
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now, real_utc_now_iso
    work = queue.Queue(maxsize=32)
    counters = collections.Counter()
    poller = RequestPoller(fetch=default_fetch(), watched_fn=watched_fn, max_polls=cfg.MEMECOIN_RH_RELAY_POLL_MAX,
                           per_s=cfg.MEMECOIN_RH_RELAY_POLL_PER_S)

    def offer(proof):
        try:
            work.put_nowait((proof, real_utc_now_iso()))
        except queue.Full:
            counters["queue_drops"] += 1

    def loop():
        from sniper.services.memecoin_relay_deposit_store import registry_reader
        from sniper.services.memecoin_rh_fast_decide import open_tape
        tconn = open_tape(tape_path)
        pipeline = offer.pipeline = RelayPipeline(
            poller=poller, tape=tape, live=live, learn=learn, send=send or lane.send_signal, head_fn=head_fn,
            gap_fn=tape_gap_fn(tconn), watched_fn=watched_fn, db_path=db_path, address=address,
            prewarm=cfg.MEMECOIN_RH_RELAY_PREWARM, prebuy=cfg.MEMECOIN_RH_RELAY_PREBUY,
            clip_usd=cfg.MEMECOIN_RH_RELAY_PREBUY_CLIP_USD, max_impact_pct=cfg.MEMECOIN_RH_RELAY_PREBUY_MAX_IMPACT_PCT,
            now_fn=real_utc_now, now_iso=real_utc_now_iso, registry_ok=registry_reader(ledger_path))
        from sniper.services.memecoin_rh_feed_shadow import quote_assets_4663
        from sniper.services import memecoin_bsc_fast_lane as bsc
        pipeline.quote_assets = {4663: frozenset({"0x" + "00" * 20, *quote_assets_4663()}),
                                 56: frozenset({"0x" + "00" * 20, bsc.WBNB, *cfg.MEMECOIN_BSC_FAST_LANE_HOP_QUOTES})}
        if cfg.MEMECOIN_BSC_RELAY_PREBUY:
            pipeline.bsc_address, pipeline.bsc_send = bsc.default_address(), bsc.send_signal
        while True:
            try:
                proof, seen_at = work.get(timeout=0.5)
            except queue.Empty:
                try:                    # idle only: a deposit never waits behind an outcome call
                    pipeline.settle_due(time.time())
                except Exception:
                    logger.exception("relay poller: outcome settle failed")
                continue
            try:
                pipeline.handle(proof, seen_at)
            except Exception:
                logger.exception("relay poller: deposit %s failed", proof.get("order_id"))

    offer.pipeline, offer.counters, offer.poller = None, counters, poller
    thread = threading.Thread(target=loop, name="rh-relay-poller", daemon=True)
    thread.start()
    log("[rh-relay] poller started: prewarm=%s prebuy=%s, %d polls at %.0f/s per deposit" % (
        int(cfg.MEMECOIN_RH_RELAY_PREWARM), int(cfg.MEMECOIN_RH_RELAY_PREBUY), cfg.MEMECOIN_RH_RELAY_POLL_MAX,
        cfg.MEMECOIN_RH_RELAY_POLL_PER_S))
    return offer, thread


# --- P3: the runner side (memecoin_rh_fast_lane's process; spec §2.3.4-§2.5) -----------------------

from fractions import Fraction                                        # noqa: E402

from sniper.services import memecoin_rh_fast_lane as lane             # noqa: E402

PREBUY_SOURCE = "relay_prebuy"
PREBUY_LANE = "rh_relay_prebuy"


def prebuy_origin(payload):
    return "rhprebuy:4663:%s:%s" % (str(payload["tx_hash"]).lower(), str(payload["mint"]).lower())


def prebuy_cap_bound(payload, max_impact_pct):
    """True when the datagram's cap is positive and no looser than the tape spot x (1 + max impact),
    in the cap's own units: a bridged cap is X per coin atomic, so the spot (wei per coin) goes back to
    X at hop A's mid, as cap_bound does for option C."""
    try:
        spot = Fraction(int(payload["spot_wei_num"]), int(payload["spot_wei_den"]))
        if lane.route_kind(payload) == "v4_bridge":
            spot *= Fraction(int(payload["a_sqrt"]) ** 2, 1 << 192)
        cap = Fraction(int(payload["cap_num"]), int(payload["cap_den"]))
        return spot > 0 and 0 < cap <= spot * (1 + Fraction(str(max_impact_pct)) / 100)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


class PrebuyEvent(lane.LaneEvent):
    """The funded hook's event for one pre-buy (spec §2.3.5). No source trade exists yet: their size is
    the deposit's signed USDC (never Relay's amountUsd), their price and the coin's mcap are the tape's
    spot, and the chase reference names its absence, so the chase gates compare against spot (the
    only exemption). Its origin is the order id, never a source transaction."""
    signal_source = PREBUY_SOURCE
    delivery_lane = PREBUY_LANE

    def __init__(self, payload, *, decimals, supply, chain="4663", delivery_lane=PREBUY_LANE):
        from sniper.services.memecoin_evm_post_trade import EvmChaseReference
        self.mint = str(payload["mint"]).lower()
        self.receipt_clock = None
        self.network_id = str(chain)
        self.delivery_lane = delivery_lane
        self.source_user_id = payload.get("source_user_id")
        self.source_handle = payload.get("handle")
        self.source_wallet = payload.get("wallet")
        self.source_block_number = payload.get("seq")
        self.shared_evm_route = False
        self.source_initiation = payload["source_initiation"]
        spot = Fraction(int(payload["spot_wei_num"]), int(payload["spot_wei_den"]))  # wei per coin atomic
        eth_usd = Fraction(str(payload["eth_usd"]))
        self.their_size_usd = float(payload["paid_usd"])
        self.their_price = float(spot * 10 ** int(decimals) / 10 ** 18 * eth_usd)
        self.their_market_cap = float(int(supply) * spot / 10 ** 18 * eth_usd)
        self.evm_chase_reference = EvmChaseReference(self.network_id, post={
            "reason": "relay_prebuy_no_source_fill"})
        self.implied_quote_reserve_usd = self.implied_quote_leg = None
        self.fomo_event_id = prebuy_origin(payload)
        self.created_at = self.received_at = self.received_at_normalized = payload.get("arrival_utc")
        order = str(payload["tx_hash"]).lower()
        self.source_evidence = {
            "their_size_usd": self.their_size_usd, "source_user_id": self.source_user_id,
            "network_id": self.network_id, "mint": self.mint, "signature": order,
            "source_timestamp_trust": "observed_detection", "valuation_basis": "relay_deposit_usdc",
            "source_fill": None, "source_initiation": self.source_initiation,
            "relay_prebuy": dict(payload["prebuy"])}


class PrebuyLane(lane.LiveLane):
    """The fast lane's own checks, hook and executor (wallet exec lock, nonce floor, write-ahead
    identity: no new submission path) for a pre-buy. Only what differs: its event, its proof, its
    state kind (the pool's current tape row) and its own origin and release names."""
    DELIVERY_LANE = PREBUY_LANE
    ORIGIN_PREFIX = "rhprebuy"
    RELEASE_PREFIX = "rh_prebuy_"
    EXACT_STATE_KIND = "prebuy_tape"

    def _lane_event(self, payload, **kw):
        return PrebuyEvent(payload, chain=str(self.CHAIN_ID), **kw)

    def _initiation(self, payload, kind, rec):
        """The hook's own initiation verdict before any attempt row (as option C's payload_refusal),
        and the cap no looser than spot x (1 + max impact)."""
        from types import SimpleNamespace
        from sniper.services.memecoin_source_initiation import source_initiation_refusal
        from sniper.settings import memecoin as cfg
        if kind not in lane.ROUTE_KINDS:
            return "prebuy_route_unsupported"
        ev = SimpleNamespace(signal_source=PREBUY_SOURCE, fomo_event_id=prebuy_origin(payload),
                             mint=str(payload["mint"]).lower(), source_user_id=payload.get("source_user_id"),
                             source_wallet=payload.get("wallet"), source_block_number=payload.get("seq"),
                             source_initiation=payload.get("source_initiation"))
        why = source_initiation_refusal(ev, str(self.CHAIN_ID))
        if why is not None:
            return "prebuy_proof_refused:%s" % why
        if payload.get("eth_usd") is None:
            return "no_eth_usd"
        if not prebuy_cap_bound(payload, cfg.MEMECOIN_RH_RELAY_PREBUY_MAX_IMPACT_PCT):
            return "prebuy_cap_unbound"
        rec["initiation"] = "relay_request:%s" % str(payload["tx_hash"]).lower()
        return None

    def _source_trade_reason(self, payload, rec):
        try:
            if int(payload["spot_wei_num"]) > 0 and int(payload["spot_wei_den"]) > 0:
                return None
        except (KeyError, TypeError, ValueError):
            pass
        return "no_spot"


_PREBUYS = """
CREATE TABLE IF NOT EXISTS rh_relay_prebuys (
    order_id TEXT PRIMARY KEY, uid TEXT, mint TEXT, deposit_sig TEXT, request_id TEXT,
    deposit_seen_at TEXT, request_found_at TEXT, decided_at TEXT, decide_head_block INTEGER,
    state_age_blocks INTEGER, route_kind TEXT, route_pool TEXT, impact_pct REAL, reason TEXT,
    pending_id INTEGER, position_id INTEGER, our_tx TEXT, our_landed_block INTEGER, reverted INTEGER,
    rec_json TEXT, source_fill_tx TEXT, source_fill_lane TEXT, source_fill_reason TEXT,
    source_fill_disposition TEXT)
"""
# What a finished pre-buy holds (Alpha 2026-10-14, on Gamma's MEDIUM 2): only one that acquired tokens or
# may have (ambiguous) keeps refusing the source fill, through one position per coin; one that acquired
# nothing leaves the source fill to be judged as today.
_ACQUIRED = frozenset({"filled", "filled_from_receipt_net"})
_AMBIGUOUS = frozenset({"submitted_unknown", "receipt_unknown", "booking_held", "binding_conflict",
                        "signed_identity_invalid", "error"})
# The runner's pre-buy workers by chain ("4663": build_prebuy_worker, "56": the BSC lane's): a lane's
# verdict on a source buy is noted to its own chain's worker only (counters per chain).
ACTIVE = {}


def note_source_fill(chain, mint, uid, source_tx, lane_name, reason):
    """A lane judged the source's own buy of `mint` on `chain` (both lanes call this). No-op without that
    chain's pre-buy worker, or when no finished pre-buy of that user's coin is recent."""
    worker = ACTIVE.get(str(chain))
    if worker is not None:
        return worker.note_source_fill(mint, uid, source_tx, lane_name, reason)
    return None
# The rows that end a run of reverts: a fill, or the operator's reset row after a revert stop.
_STREAK_ENDS = ("filled", "filled_from_receipt_net", "operator_reset")


def write_prebuy_record(db_path, payload, rec):
    """Our side of one pre-buy, keyed on its order id, in the lane's own SQLite (never the ledger)."""
    import sqlite3
    from sniper.util.clock import real_utc_now_iso
    meta = payload.get("prebuy") or {}
    db = sqlite3.connect(db_path, timeout=2)
    try:
        db.execute(_PREBUYS)
        db.execute(
            "INSERT OR IGNORE INTO rh_relay_prebuys (order_id, uid, mint, deposit_sig, request_id, "
            "deposit_seen_at, request_found_at, decided_at, decide_head_block, state_age_blocks, route_kind, "
            "route_pool, impact_pct, reason, pending_id, position_id, our_tx, our_landed_block, reverted, "
            "rec_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(payload["tx_hash"]).lower(), payload.get("source_user_id"), str(payload["mint"]).lower(),
             meta.get("deposit_sig"), meta.get("request_id"), meta.get("deposit_seen_at"),
             meta.get("request_found_at"), real_utc_now_iso(), payload.get("seq"),
             meta.get("state_age_blocks"), lane.route_kind(payload), payload.get("pool"),
             meta.get("impact_pct"), rec.get("reason"), rec.get("pending_id"), rec.get("position_id"),
             rec.get("tx_hash"), rec.get("our_block"), int(rec.get("reason") == "reverted"),
             json.dumps({k: v for k, v in rec.items() if not k.startswith("_")}, sort_keys=True, default=str)))
        db.commit()
    finally:
        db.close()


def consecutive_reverts(db_path, limit):
    """The run of reverts since the last fill (or operator_reset row), newest first, at most `limit`:
    the revert stop survives a restart. No file yet is no history (0); a file that cannot be read is
    unknown history, which is a stop (`limit`), never a fresh start."""
    import os
    import sqlite3
    if not os.path.exists(db_path):
        return 0
    try:
        db = sqlite3.connect(db_path, timeout=2)
        try:
            db.execute(_PREBUYS)
            rows = db.execute("SELECT reason FROM rh_relay_prebuys WHERE reason IN (?,?,?,?) "
                              "ORDER BY rowid DESC LIMIT ?", ("reverted",) + _STREAK_ENDS + (int(limit),)).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        logger.exception("relay prebuy: revert streak unreadable: stopped until it reads")
        return int(limit)
    run = 0
    for (reason,) in rows:
        if reason != "reverted":
            break
        run += 1
    return run


class RelayPrebuyWorker:
    """One pre-buy at a time on its own thread with its own ledger connection (the SharedRouteWorker
    pattern), never the listener's. Its own pilot: MEMECOIN_RH_RELAY_PREBUY_PILOT_FILLS fills and
    _MAX_PER_HOUR sends (the lane's own counters, seeded from the ledger by signal source), and it
    stops itself with an alert at MEMECOIN_RH_RELAY_PREBUY_MAX_REVERTS consecutive reverts, a stop
    the records table carries across a restart. The fast lane's stop file stops it too."""

    def __init__(self, prebuy_lane, *, live, connect, db_path, registry, cache, max_reverts, alert_fn,
                 clock=time.time):
        import queue
        self.lane, self.live, self.connect, self.db_path = prebuy_lane, live, connect, db_path
        self.lane._stop_owner = live
        self.registry, self.cache, self.max_reverts = registry, cache, int(max_reverts)
        self.alert_fn, self.clock = alert_fn, clock
        self.jobs = queue.SimpleQueue()
        self.thread = threading.Thread(target=self._run, name="rh-relay-prebuy", daemon=True)
        self.closed, self.busy, self.conn = False, False, None
        self._inflight = None                          # the mint of the pre-buy offered and not finished
        self.idle = threading.Event()
        self.idle.set()
        self.counts = collections.Counter()
        self._lock = threading.Lock()                  # the listener, the on-chain lane and this thread
        self.ended = collections.OrderedDict()         # mint -> the last finished pre-buy that reserved it
        self.notes = collections.deque(maxlen=256)     # (order, source tx, lane, reason, disposition)
        self.reverts = consecutive_reverts(db_path, self.max_reverts)
        self.stop_reason = "revert_stop" if self.reverts >= self.max_reverts else None
        self._pilot_alerted = self.lane.fills >= self.lane.pilot_fills

    def start(self):
        self.thread.start()

    def offer(self, payload, recv_epoch):
        """Queue one pre-buy (listener thread: no read, no lock). Busy means refused, never queued."""
        if self.closed or not self.thread.is_alive():
            reason = "prebuy_worker_unavailable"
        elif self.busy:
            reason = "prebuy_busy"
        else:
            self.busy = True
            self._inflight = str(payload["mint"]).lower()
            self.idle.clear()
            self.jobs.put((dict(payload), recv_epoch))
            return "prebuy_queued"
        self.counts["prebuy_refused:" + reason] += 1
        return reason

    def close(self):
        if not self.closed:
            self.closed = True
            self.jobs.put(None)
        if self.thread.ident is not None:
            self.thread.join(timeout=3)

    def _run(self):
        try:
            while (job := self.jobs.get()) is not None:
                try:
                    self.run_one(*job)
                except Exception:
                    # Never release a claim here: the funded engine owns signed/unknown truth, and the
                    # source fill keeps being refused while it may hold the coin (ambiguous).
                    logger.exception("relay prebuy: attempt failed for order %s", job[0].get("tx_hash"))
                    self._finish(job[0], {"reason": "error", "pending_id": -1})
                finally:
                    self._inflight = None
                    self.busy = False
                    self.idle.set()
        finally:
            if self.conn is not None:
                self.conn.close()

    def run_one(self, payload, recv_epoch):
        mint = str(payload["mint"]).lower()
        if self.stop_reason is not None:
            rec = {"reason": "prebuy_" + self.stop_reason, "source_tx": payload["tx_hash"], "mint": mint}
        else:
            if self.conn is None:
                self.conn = self.connect()
                self.conn.execute("PRAGMA busy_timeout=200")
            self.lane.conn = self.conn
            self.lane.stopped = bool(getattr(self.live, "stopped", False))   # the operator's stop file
            now = self.clock()
            # As the listener does for a signal: the route the safety quote needs, then the cache's
            # current answer (an inline check on a miss runs inside the lane's own deadline).
            self.registry[mint] = lane.registry_entry({
                "kind": lane.route_kind(payload), "key": payload["key"], "pool": payload.get("pool"),
                "bridge": payload.get("bridge"), "a_key": payload.get("a_key"), "a_pool": payload.get("a_pool")})
            self.cache.note_candidates([{"mint": mint, "priority": -1}], now=now)
            safety = self.cache.reason(mint, now=self.clock())
            if safety in ("no_safety_verdict", "safety_stale"):
                # WARM ONLY (Alpha, on Gamma's MEDIUM 1): a cold check loses the race to the source's
                # own fill, so no inline read on this path; the warm above serves the normal copy.
                rec = {"reason": "prebuy_skipped:cold_safety", "source_tx": payload["tx_hash"], "mint": mint}
                self.counts["prebuy_skipped:cold_safety"] += 1
                self._record(payload, rec)
                return rec
            rec = self.lane.handle(payload, recv_epoch=recv_epoch, safety_reason=safety,
                                   stats=self.cache.stats(mint, now=self.clock()))
        self._after(rec)
        self._record(payload, rec)
        self._finish(payload, rec)
        return rec

    def _record(self, payload, rec):
        try:
            write_prebuy_record(self.db_path, payload, rec)
        except Exception:
            logger.exception("relay prebuy: record not written for order %s", payload.get("tx_hash"))

    def inflight(self, mint):
        """True while a pre-buy of `mint` is offered and not finished (the lane's listener asks)."""
        return self._inflight is not None and self._inflight == str(mint).lower()

    def _finish(self, payload, rec):
        """Register a finished pre-buy that reserved the coin, for the source fill's disposition."""
        if rec.get("pending_id") is None and not rec.get("tx_hash"):
            return                                       # never reserved: it holds nothing
        outcome = str(rec.get("reason") or "unknown").split(":")[0]
        held = ("acquired" if outcome in _ACQUIRED else "ambiguous" if outcome in _AMBIGUOUS else "nothing")
        with self._lock:
            mint = str(payload["mint"]).lower()
            self.ended.pop(mint, None)
            self.ended[mint] = dict(order_id=str(payload["tx_hash"]).lower(), uid=payload.get("source_user_id"),
                                    outcome=outcome, held=held, at=self.clock(), noted=False)
            while len(self.ended) > 256:
                self.ended.popitem(last=False)

    def note_source_fill(self, mint, uid, source_tx, lane_name, reason, window_s=300.0):
        """The source's own buy of a coin we pre-bought, once judged by a lane: refused_by_prebuy when our
        pre-buy holds or may hold the coin, else rejudged_after_prebuy. Counted once per pre-buy."""
        with self._lock:
            ended = self.ended.get(str(mint).lower())
            if (ended is None or ended["noted"] or ended["uid"] != uid
                    or self.clock() - ended["at"] > window_s):
                return None
            ended["noted"] = True
            disposition = "rejudged_after_prebuy" if ended["held"] == "nothing" else "refused_by_prebuy"
            self.counts["source_fill_%s:%s" % (disposition, ended["outcome"])] += 1
            self.notes.append((ended["order_id"], source_tx, lane_name, reason, disposition))
        return disposition

    def flush_notes(self, db_path):
        """The source fill's disposition onto each pre-buy's row (the lane's heartbeat thread)."""
        import sqlite3
        with self._lock:
            notes, self.notes = list(self.notes), collections.deque(maxlen=256)
        if not notes:
            return
        db = sqlite3.connect(db_path, timeout=2)
        try:
            db.execute(_PREBUYS)
            db.executemany("UPDATE rh_relay_prebuys SET source_fill_tx=?, source_fill_lane=?, source_fill_reason=?, "
                           "source_fill_disposition=? WHERE order_id=? AND source_fill_tx IS NULL",
                           [(tx, lane_name, reason, disposition, order)
                            for order, tx, lane_name, reason, disposition in notes])
            db.commit()
        finally:
            db.close()

    def beat(self):
        """The lane heartbeat's pre-buy counters (spec §2.6)."""
        now = self.clock()
        return dict(self.counts, fills=self.lane.fills, reverts=self.reverts, stop_reason=self.stop_reason,
                    sends_last_hour=sum(1 for at in list(self.lane.send_times) if now - at <= 3600))

    def _after(self, rec):
        reason = str(rec.get("reason") or "")
        if rec.get("send_reply") is not None:
            self.counts["prebuy_sent"] += 1
        else:
            self.counts["prebuy_refused:" + reason.split(":")[0]] += 1
        if reason == "reverted":
            self.reverts += 1
            self.counts["prebuy_reverts"] += 1
            if self.reverts >= self.max_reverts and self.stop_reason is None:
                self.stop_reason = "revert_stop"
                self.alert_fn("relay pre-buy STOPPED: %d consecutive reverts (last tx %s). It stays stopped "
                              "across restarts until an operator_reset row is written to rh_relay_prebuys."
                              % (self.reverts, rec.get("tx_hash")))
        elif reason in _STREAK_ENDS:
            self.reverts = 0
            if self.lane.fills >= self.lane.pilot_fills and not self._pilot_alerted:
                self._pilot_alerted = True
                self.alert_fn("relay pre-buy pilot complete: %d fills. Every later pre-buy is refused "
                              "(pilot_complete)." % self.lane.fills)


def build_prebuy_worker(live, live_kwargs, *, connect, db_path, registry, cache, log):
    """The pre-buy lane (the fast lane's own arguments, with the pre-buy's clip, caps and seeded counts)
    and its worker. MEMECOIN_RH_RELAY_PREBUY on, live mode only: build_lane calls it."""
    from sniper.services.memecoin_alerts import send_alert
    from sniper.settings import memecoin as cfg

    def alert(text):
        send_alert(PREBUY_LANE, text)
    fills, sends = lane._seed_pilot(live_kwargs["conn"], signal_source=PREBUY_SOURCE, network_id="4663")
    # No inline safety or facts on this path (warm only): a cold coin is skipped before the lane.
    prebuy_lane = PrebuyLane(**dict(
        live_kwargs, conn=None, inline_safety_fn=None, inline_facts_fn=None, inline_host=None,
        clip_usd=cfg.MEMECOIN_RH_RELAY_PREBUY_CLIP_USD,
        pilot_fills=cfg.MEMECOIN_RH_RELAY_PREBUY_PILOT_FILLS,
        max_sends_per_hour=cfg.MEMECOIN_RH_RELAY_PREBUY_MAX_PER_HOUR,
        fills=fills, send_times=sends, alert_fn=alert))
    worker = RelayPrebuyWorker(prebuy_lane, live=live, connect=connect, db_path=db_path, registry=registry,
                               cache=cache, max_reverts=cfg.MEMECOIN_RH_RELAY_PREBUY_MAX_REVERTS,
                               alert_fn=alert)
    ACTIVE["4663"] = worker
    live.prebuy_inflight_fn = worker.inflight         # the lane leaves the source fill to the on-chain lane
    log("[rh-relay-prebuy] LIVE: $%.0f clip, %d fills of %d, %d sends in the last hour of %d, %d "
        "consecutive reverts of %d%s" % (
            prebuy_lane.clip_usd, fills, prebuy_lane.pilot_fills, len(sends), prebuy_lane.max_sends_per_hour,
            worker.reverts, worker.max_reverts, ": STOPPED (revert stop)" if worker.stop_reason else ""))
    return worker
