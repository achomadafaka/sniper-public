"""BSC 56 Relay pre-buy, the runner side: spec docs/superpowers/specs/2026-10-14-bsc-relay-prebuy-design.md.

The 4663 feed's Relay poller hands a 56 match to the BSC lane unpriced (memecoin_rh_relay_prebuy
.bsc_prebuy_payload). Here, on the lane's own host and in the runner, it is priced at one block (§3.3),
routed over the coin's PancakeSwap V2 pairs (§3.4), and executed through the BSC lane's own executor.
Default off: MEMECOIN_BSC_RELAY_PREBUY.
"""
from __future__ import annotations

import logging
from fractions import Fraction

from sniper.services import memecoin_bsc_fast_lane as bsc

logger = logging.getLogger(__name__)

GET_RESERVES = "0x0902f1ac"           # getReserves() -> (uint112 reserve0, uint112 reserve1, uint32 ts)
DECIMALS = "0x313ce567"               # decimals()


def _candidate_pairs(mint, hop_quotes):
    """The coin/WBNB pair, and for each hop quote q the coin/q and WBNB/q pairs (CREATE2)."""
    mint = str(mint).lower()
    pairs = [bsc.our_pair(mint, bsc.WBNB)]
    for quote in hop_quotes:
        pairs += [bsc.our_pair(mint, quote), bsc.our_pair(bsc.WBNB, quote)]
    return list(dict.fromkeys(pairs))


def bsc_spot_batch(host, mint, hop_quotes):
    """({"block", "reserves": {pair: (reserve0, reserve1)}, "decimals"}, None) from ONE batch on the
    lane's own host (LaneHost.batch: ids paired, any error item raises), or (None, reason). The coin's
    decimals ride in the same batch (a cold coin has no cached facts yet; the lane checks them against its
    own facts read). A pair whose call returns "0x" has no contract: absent, never zero. A host that does not
    batch, a raised or malformed answer, or a coin without decimals: no buy."""
    pairs = _candidate_pairs(mint, hop_quotes)
    calls = ([("eth_blockNumber", []), ("eth_call", [{"to": str(mint).lower(), "data": DECIMALS}, "latest"])]
             + [("eth_call", [{"to": pair, "data": GET_RESERVES}, "latest"]) for pair in pairs])
    try:
        got = host.batch(calls)
    except Exception as exc:                       # transport, 429, an error item, ids that do not pair
        logger.warning("bsc relay prebuy: spot batch failed: %s", type(exc).__name__)
        return None, "spot_read_failed"
    if got is None:
        return None, "batch_unsupported"
    try:
        if not isinstance(got, list) or len(got) != len(calls):
            raise ValueError("reply count")
        block = int(got[0], 16)
        if not isinstance(got[1], str) or len(got[1]) <= 2:
            return None, "no_decimals"
        decimals = int(got[1], 16)
        if not 0 <= decimals <= 36:
            return None, "no_decimals"
        reserves = {}
        for pair, word in zip(pairs, got[2:]):
            if not isinstance(word, str) or not word.startswith("0x"):
                raise ValueError("not hex")
            body = word[2:]
            if not body:
                continue                           # no pair deployed at this address
            if len(body) != 192:
                raise ValueError("getReserves is three words")
            reserves[pair] = (int(body[0:64], 16), int(body[64:128], 16))
    except (TypeError, ValueError):
        return None, "spot_read_failed"
    return {"block": block, "reserves": reserves, "decimals": decimals}, None


def _leg(reserves, mint, quote):
    """(pair, coin1, r_coin, r_quote) for the coin's pair against `quote`, or None (absent or a zero side)."""
    pair = bsc.our_pair(mint, quote)
    state = reserves.get(pair)
    if not state or min(state) <= 0:
        return None
    coin1 = int(mint, 16) > int(quote, 16)
    r_coin, r_quote = (state[1], state[0]) if coin1 else state
    return pair, coin1, int(r_coin), int(r_quote)


def bsc_prebuy_route(mint, block, reserves, *, clip_wei, max_impact_pct, hop_quotes):
    """(fields, None) for the BSC lane's native payload, or (None, reason). The cheapest of the coin's V2
    routes (coin/WBNB direct, or coin/q through WBNB/q), each priced through the router's own path
    (v2_amount_out with its 0.25% fee, the hop first). Spot is the coin leg's reserve ratio in quote atomic
    per coin atomic; on a hop the clip is taken to q at the hop's FEE-INCLUSIVE spot, as min_out_for does,
    so the hop's own impact counts against the cap. Impact = (paid / est_out / spot - 1) x 100; above
    `max_impact_pct` no buy. cap = spot x (1 + max_impact_pct): we never pay more, or the swap reverts.
    Tax is unknown before the source buys: 0, so a taxed coin reverts on min_out (spec §3.4)."""
    mint = str(mint).lower()
    clip = Fraction(int(clip_wei))
    routes = []
    direct = _leg(reserves, mint, bsc.WBNB)
    if direct is not None:
        pair, coin1, r_coin, r_quote = direct
        routes.append(dict(pool=pair, quote=bsc.WBNB, coin1=coin1, r_coin=r_coin, r_quote=r_quote, hop=None,
                           paid=clip, est_out=bsc.v2_amount_out(int(clip_wei), r_quote, r_coin)))
    for quote in hop_quotes:
        quote = str(quote).lower()
        coin_leg = _leg(reserves, mint, quote)
        hop_pair = bsc.our_pair(bsc.WBNB, quote)
        hop_state = reserves.get(hop_pair)
        if coin_leg is None or not hop_state or min(hop_state) <= 0:
            continue
        pair, coin1, r_coin, r_quote = coin_leg
        wbnb1 = int(bsc.WBNB, 16) > int(quote, 16)
        r_wbnb, r_hop_quote = (hop_state[1], hop_state[0]) if wbnb1 else hop_state
        got_quote = bsc.v2_amount_out(int(clip_wei), r_wbnb, r_hop_quote)
        routes.append(dict(pool=pair, quote=quote, coin1=coin1, r_coin=r_coin, r_quote=r_quote,
                           hop={"pair": hop_pair, "r_wbnb": int(r_wbnb), "r_quote": int(r_hop_quote),
                                "source": "lane_host", "height": int(block)},
                           paid=clip * 9975 / 10000 * Fraction(int(r_hop_quote), int(r_wbnb)),
                           est_out=bsc.v2_amount_out(got_quote, r_quote, r_coin)))
    priced = []
    for route in routes:
        if route["est_out"] <= 0:
            continue
        spot = Fraction(route["r_quote"], route["r_coin"])
        priced.append(((route["paid"] / route["est_out"] / spot - 1) * 100, spot, route))
    if not priced:
        return None, "no_v2_route"
    impact, spot, route = min(priced, key=lambda item: item[0])
    if impact > Fraction(str(max_impact_pct)):
        return None, "impact_over_cap"
    cap = spot * (1 + Fraction(str(max_impact_pct)) / 100)
    return dict(pool=route["pool"], quote=route["quote"], coin1=route["coin1"], r_coin=route["r_coin"],
                r_quote=route["r_quote"], hop=route["hop"], src_in=int(route["paid"]), src_out=int(route["est_out"]),
                tax_num=0, tax_den=1, cap_num=cap.numerator, cap_den=cap.denominator, impact_pct=float(impact),
                est_out=int(route["est_out"]), state_kind="prebuy_lane_host", seq=int(block)), None


def bnb_usd_now(now, *, path=None):
    """(BNB/USD, observed) from the BSC tape's quote row inside its TTL (QUOTE_TTL_S), else (None, None).
    `now` is on the tape's own clock (real_utc_now). A missing or unreadable file is unknown."""
    import sqlite3
    from sniper.services import memecoin_bsc_v2_live as tape
    try:
        conn = tape.connect(path, readonly=True)
        try:
            row = conn.execute("SELECT usd, observed FROM quote WHERE id=1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None, None
    if not row or not row[0] or row[1] is None or not 0 <= float(now) - float(row[1]) <= tape.QUOTE_TTL_S:
        return None, None
    return float(row[0]), float(row[1])


# --- the lane, its event and its worker (spec §3.2, §3.5, §3.6, §4) ----------------------------------------

import math                                                                    # noqa: E402
import time                                                                    # noqa: E402
from types import SimpleNamespace                                              # noqa: E402

from sniper.services import memecoin_rh_relay_prebuy as rp                     # noqa: E402

PREBUY_LANE_56 = "bsc_relay_prebuy"
SOURCE_SEEN_LOOKBACK_S = 60.0         # a source signal for (uid, mint) this recent: the normal copy owns it
_HEX = {"tx_hash": 32, "mint": 20, "wallet": 20}


def bsc_prebuy_malformed(payload):
    """None when a relay_prebuy_v1 datagram for 56 is well formed (the feed's bsc_prebuy_payload), else
    "malformed". Unpriced by design: no route or price field is read from it."""
    try:
        if payload.get("chain") != bsc.CHAIN:
            return "malformed"
        for key, size in _HEX.items():
            value = payload[key]
            if not isinstance(value, str) or not value.startswith("0x") or len(value) != 2 + 2 * size:
                return "malformed"
            bytes.fromhex(value[2:])
        paid = payload["paid_usd"]
        if (isinstance(paid, bool) or not isinstance(paid, (int, float)) or not math.isfinite(paid) or paid <= 0
                or not isinstance(payload["source_user_id"], str) or not payload["source_user_id"]
                or not isinstance(payload["source_initiation"], dict) or not isinstance(payload["prebuy"], dict)
                or str(payload["prebuy"].get("order_id") or "").lower() != payload["tx_hash"].lower()
                or not isinstance(payload["arrival_utc"], str)
                or not math.isfinite(float(payload["sent_epoch"]))):
            return "malformed"
    except (KeyError, TypeError, ValueError, AttributeError):
        return "malformed"
    return None


def prebuy_origin_56(payload):
    return "rhprebuy:56:%s:%s" % (str(payload["tx_hash"]).lower(), str(payload["mint"]).lower())


class PrebuyBscEvent(bsc.BscLaneEvent):
    """The funded hook's event for one 56 pre-buy (spec §3.5): their size is the deposit's signed USDC,
    their price and the coin's mcap the lane host's spot, and the chase reference names the missing
    source fill, so the chase gates compare against spot. Its origin is the order id."""
    signal_source = rp.PREBUY_SOURCE
    delivery_lane = PREBUY_LANE_56

    def __init__(self, payload, *, market_cap):
        from sniper.services.memecoin_evm_post_trade import EvmChaseReference
        self.mint = str(payload["mint"]).lower()
        self.network_id = bsc.CHAIN
        self.source_user_id = payload.get("source_user_id")
        self.source_handle = payload.get("handle")
        self.source_wallet = payload.get("wallet")
        self.source_block_number = payload.get("seq")
        self.receipt_clock, self.receipt_stages = None, ()
        self.source_initiation = payload["source_initiation"]
        self.their_size_usd = float(payload["paid_usd"])
        self.their_price = float(payload["source_price"])
        self.their_market_cap = market_cap
        self.evm_chase_reference = EvmChaseReference(bsc.CHAIN, post={"reason": "relay_prebuy_no_source_fill"})
        self.implied_quote_reserve_usd = self.implied_quote_leg = None
        self.fomo_event_id = prebuy_origin_56(payload)
        self.created_at = self.received_at = self.received_at_normalized = payload.get("arrival_utc")
        self.source_evidence = {
            "their_size_usd": self.their_size_usd, "source_user_id": self.source_user_id,
            "network_id": bsc.CHAIN, "mint": self.mint, "signature": str(payload["tx_hash"]).lower(),
            "source_initiation": self.source_initiation, "source_wallet": self.source_wallet,
            "source_block_number": self.source_block_number, "source_fill": None,
            "source_timestamp_trust": "observed_detection", "valuation_basis": "relay_deposit_usdc",
            "relay_prebuy": dict(payload["prebuy"])}


class PrebuyBscLive(bsc.BscLive):
    """The BSC lane's checks, funded hook and executor (lock, nonce floor, write-ahead, builder bundle: no
    new submission path) for a pre-buy. Only what differs: its event, its proof, its origin and release
    names, no watcher observer, and a yield to the source's own fill found during its inline check."""
    DELIVERY_LANE = PREBUY_LANE_56
    ORIGIN_PREFIX = "rhprebuy"
    RELEASE_PREFIX = "bsc_prebuy_"
    collision_fn = None                # per pre-buy: the source's own fill reached the lane meanwhile

    def handle(self, payload, *, recv_epoch):
        self.entry_active = True
        try:
            return self._handle(payload, recv_epoch=recv_epoch)
        finally:
            self.entry_active = False

    def _initiation_refusal(self, payload):
        from sniper.services.memecoin_source_initiation import source_initiation_refusal
        ev = SimpleNamespace(signal_source=rp.PREBUY_SOURCE, fomo_event_id=prebuy_origin_56(payload),
                             mint=str(payload["mint"]).lower(), source_user_id=payload.get("source_user_id"),
                             source_wallet=payload.get("wallet"), source_block_number=payload.get("seq"),
                             source_initiation=payload.get("source_initiation"))
        why = source_initiation_refusal(ev, bsc.CHAIN)
        return None if why is None else "prebuy_proof_refused:%s" % why

    def _lane_event(self, payload, *, market_cap):
        return PrebuyBscEvent(payload, market_cap=market_cap)

    def _inline(self, mint, rec):
        why = super()._inline(mint, rec)
        if why is None and self.collision_fn is not None and self.collision_fn():
            return "source_fill_seen"          # inside the hook: the reservation is released, unsigned
        return why


class BscRelayPrebuyWorker(rp.RelayPrebuyWorker):
    """One 56 pre-buy at a time on its own thread and ledger connection (the 4663 worker's caps, revert
    stop, records, outcome registry and counters; per chain). Per pre-buy, in order: the collision check
    (§3.6), BNB/USD, ONE batch on the lane host, the cheapest V2 route, the collision check again, then
    PrebuyBscLive: a cached safety verdict (prebuy_cached) or the lane's own bounded inline check
    (prebuy_inline; Alpha 2026-10-14, 56 only)."""

    def __init__(self, prebuy_lane, *, live, dispatch, connect, db_path, max_reverts, alert_fn, hop_quotes,
                 clip_usd, max_impact_pct, inline_deadline_s, clock=time.time):
        super().__init__(prebuy_lane, live=live, connect=connect, db_path=db_path, registry={},
                         cache=live.cache, max_reverts=max_reverts, alert_fn=alert_fn, clock=clock)
        self.dispatch, self.hop_quotes = dispatch, tuple(hop_quotes)
        self.clip_usd, self.max_impact_pct, self.inline_deadline_s = clip_usd, max_impact_pct, inline_deadline_s
        self.lane_host = live.lane_host

    def beat(self):
        """The 4663 worker's counters plus its own inline pacer's (calls, 429s, dead now)."""
        out = super().beat()
        host = getattr(self.lane, "inline_host", None)
        if host is not None:
            out.update(inline_calls=host.counts.get("calls", 0), inline_429=host.counts.get("http_429", 0),
                       inline_dead=int(host.dead()))
        return out

    def run_one(self, payload, recv_epoch):
        mint = str(payload["mint"]).lower()
        if self.stop_reason is not None:
            rec = {"reason": "prebuy_" + self.stop_reason, "source_tx": payload["tx_hash"], "mint": mint}
        else:
            if self.conn is None:
                self.conn = self.connect()
                self.conn.execute("PRAGMA busy_timeout=200")
            self.lane.conn = self.conn
            self.lane.stopped = bool(getattr(self.live, "stopped", False))
            rec = self._decide(payload, recv_epoch)
        self._after(rec)
        # The row names the priced route, block and impact when pricing ran; the datagram is unpriced.
        self._record(rec.pop("_priced", None) or payload, rec)
        self._finish(payload, rec)
        return rec

    def _decide(self, payload, recv_epoch):
        from sniper.services.memecoin_rh_fast_lane import clip_wei_for, inline_deadline
        from sniper.util.clock import real_utc_now
        mint, uid = str(payload["mint"]).lower(), payload.get("source_user_id")

        def refused(why):                      # counted once, by _after, as every other outcome
            return {"reason": why, "source_tx": payload["tx_hash"], "mint": mint}

        def seen():
            return self.dispatch.source_fill_seen(uid, mint, since=self.clock() - SOURCE_SEEN_LOOKBACK_S)
        if self.lane.stopped:
            return refused("operator_stop")
        if seen():
            return refused("source_fill_seen")
        live_host = getattr(self.live, "inline_host", None)
        if live_host is not None and live_host.paused():
            # A 429 is the shared key's (Gamma, bdde13eb9): the live lane is waiting it out, so the pre-buy
            # reads nothing on that key meanwhile. It never pauses the live lane itself.
            return refused("live_host_paused")
        bnb_usd, observed = bnb_usd_now(real_utc_now())
        if bnb_usd is None:
            return refused("no_bnb_usd")
        with inline_deadline(time.monotonic() + float(self.inline_deadline_s)):
            state, why = bsc_spot_batch(self.lane_host, mint, self.hop_quotes)
        if why is not None:
            return refused(why)
        clip_wei = clip_wei_for(self.clip_usd, bnb_usd)
        if clip_wei is None:
            return refused("no_bnb_usd")
        fields, why = bsc_prebuy_route(mint, state["block"], state["reserves"], clip_wei=clip_wei,
                                       max_impact_pct=self.max_impact_pct, hop_quotes=self.hop_quotes)
        if why is not None:
            return refused(why)
        price = Fraction(fields["r_quote"], fields["r_coin"])          # quote atomic per coin atomic
        if fields["hop"]:
            price *= Fraction(fields["hop"]["r_wbnb"], fields["hop"]["r_quote"])   # wei per coin atomic
        source_price = float(price * 10 ** state["decimals"] / 10 ** 18 * Fraction(str(bnb_usd)))
        merged = dict(payload, **fields, route_kind="v2_native", decimals=state["decimals"], bnb_usd=bnb_usd,
                      bnb_usd_at=time.time() - max(0.0, real_utc_now() - observed), source_price=source_price,
                      receipt_clock=None,
                      prebuy=dict(payload["prebuy"], state_block=state["block"], state_age_blocks=0,
                                  impact_pct=fields["impact_pct"], est_out=fields["est_out"]))
        if seen():
            return refused("source_fill_seen")
        self.lane.stopped = bool(getattr(self.live, "stopped", False))
        if self.lane.stopped:
            return refused("operator_stop")
        self.lane.collision_fn = seen
        try:
            rec = self.lane.handle(merged, recv_epoch=recv_epoch)
        finally:
            self.lane.collision_fn = None
        path = rec.get("safety_path")
        if path in ("cache", "inline"):
            self.counts["prebuy_cached" if path == "cache" else "prebuy_inline"] += 1
        rec.setdefault("execution_evidence", {})["relay_prebuy"] = {
            "block": state["block"], "impact_pct": fields["impact_pct"], "route_pool": fields["pool"],
            "route_quote": fields["quote"], "hop": bool(fields["hop"])}
        rec["_priced"] = merged
        return rec


def build_bsc_prebuy_worker(live, dispatch, *, connect, db_path, log):
    """The 56 pre-buy lane (the live BSC lane's own arguments with the pre-buy's clip, caps and seeded
    counts) and its worker; registers it for the source-fill counters. build_bsc_lane calls it."""
    from sniper.services.memecoin_alerts import send_alert
    from sniper.services.memecoin_rh_fast_lane import _seed_pilot
    from sniper.settings import memecoin as cfg

    def alert(text):
        send_alert(PREBUY_LANE_56, text)
    if connect is None:
        from sniper.persistence.db_core import get_connection as connect
    fills, sends = _seed_pilot(live.live_kwargs["conn"], signal_source=rp.PREBUY_SOURCE, network_id=bsc.CHAIN)
    # ITS OWN PACER on the same host (Gamma LOW 4, Alpha 2026-10-14): the pre-buy's spot batch and inline
    # check pause and dead-mark only themselves, never the live lane's inline path.
    from sniper.services.memecoin_rh_fast_lane import PacedHttp
    seam, prebuy_host = live.inline_seam(PacedHttp(min_gap_s=0.0, pause_s=120.0))
    prebuy_lane = PrebuyBscLive(**dict(
        live.live_kwargs, **seam, conn=None, clip_usd=cfg.MEMECOIN_RH_RELAY_PREBUY_CLIP_USD,
        pilot_fills=cfg.MEMECOIN_RH_RELAY_PREBUY_PILOT_FILLS,
        max_sends_per_hour=cfg.MEMECOIN_RH_RELAY_PREBUY_MAX_PER_HOUR,
        fills=fills, send_times=sends, alert_fn=alert))
    worker = BscRelayPrebuyWorker(
        prebuy_lane, live=live, dispatch=dispatch, connect=connect, db_path=db_path,
        max_reverts=cfg.MEMECOIN_RH_RELAY_PREBUY_MAX_REVERTS, alert_fn=alert,
        hop_quotes=cfg.MEMECOIN_BSC_FAST_LANE_HOP_QUOTES, clip_usd=cfg.MEMECOIN_RH_RELAY_PREBUY_CLIP_USD,
        max_impact_pct=cfg.MEMECOIN_RH_RELAY_PREBUY_MAX_IMPACT_PCT,
        inline_deadline_s=cfg.MEMECOIN_BSC_FAST_INLINE_SAFETY_DEADLINE_S)
    prebuy_lane.prebuy_lane_host = worker.lane_host = prebuy_host
    rp.ACTIVE[bsc.CHAIN] = worker
    live.prebuy_inflight_fn = worker.inflight
    dispatch.prebuy = worker
    log("[bsc-relay-prebuy] LIVE: $%.0f clip, %d fills of %d, %d sends in the last hour of %d, %d "
        "consecutive reverts of %d%s; inline allowed on a cold coin" % (
            prebuy_lane.clip_usd, fills, prebuy_lane.pilot_fills, len(sends), prebuy_lane.max_sends_per_hour,
            worker.reverts, worker.max_reverts, ": STOPPED (revert stop)" if worker.stop_reason else ""))
    return worker
