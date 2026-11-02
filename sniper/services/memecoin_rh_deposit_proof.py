"""4663 fast lane, option C: prove a Relay buy's initiation from the armed user's OWN signed Solana
deposit into Relay's depository, seen at processed commitment, matched to the 4663 fill by its order
id (operator yes 2026-10-07 via Alpha; continuous use of the paid research Helius key e1f95763 is an
operator exception to the flat-book rule). Default off: MEMECOIN_RH_FAST_DEPOSIT_PROOF.

WHAT IS PROVED, ON THE FRAME (the Solana half of memecoin_relay_initiation._verify_relay): no error;
exactly one top-level depository instruction, depositErc20 with amount and order id; sender ==
depositor, vault, USDC; the depositor signed; the depositor's USDC down by the amount and the
vault's up by it; the depositor is a verified Solana identity (conflict-free, verified before the
frame arrived) of a user armed on 4663. Relay's own API is never asked on the hot path.

THE MATCH (`DepositProofs.take`, in memory under one lock, on the decide thread): the fill's last 32
calldata bytes == the deposit's order id; the fill's recipient belongs to the same user; the fill
pays a 4663 USD stable worth 0.98..1.00 of the deposit (37/37 recent pairs: USDG, 0.9934-0.9991);
the fill arrived 0..120 s after the deposit frame, on the same host clock. A proof is single use.
Anything else is no proof, and the signal takes today's path.

AFTER THE SEND (`DepositConfirmer`): every matched deposit is asked getSignatureStatuses every 2 s
on the same key until it confirms. A failed one, one still unseen at 90 s, or one the RPC never
answered pages the operator and is recorded `initiation_unconfirmed:<why>` in rh_deposit_proofs.
Nothing is sold automatically.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import threading
import time
from fractions import Fraction
from types import SimpleNamespace

import base58

from sniper.services.memecoin_relay_initiation import (
    SOLANA_DEPOSITORY, SOLANA_TOKEN_DEPOSIT, SOLANA_VAULT)
from sniper.services.memecoin_source_initiation import (
    RELAY_DEPOSIT_PROOF_VERSION as PROOF_VERSION, USDC, source_initiation_refusal)

logger = logging.getLogger(__name__)

# The attempt row's delivery_lane for a deposit-proved buy. A refused one that never sent does not
# count as "already attempted" for the on-chain lane (Gamma LOW-1, Alpha 2026-10-07): its refusal
# rests on estimated economics, and the on-chain lane judges the same buy on its receipt.
DEPOSIT_DELIVERY_LANE = "rh_fast_lane_deposit"
MAX_FILL_LAG_S = 120.0                 # _verify_relay's origin-to-destination bound
AMOUNT_BAND = (Fraction(98, 100), Fraction(1))
TTL_S = 600.0
MAX_PROOFS = 4096
CONFIRM_POLL_S = 2.0
CONFIRM_GIVE_UP_S = 90.0
HISTORY_AFTER_S = 60.0                # past this age a status is asked with searchTransactionHistory
BACKOFF_S = 60.0
ROSTER_S = 60.0
_USDC_DECIMALS = 6


def deposit_from_frame(result, *, owners, arrived):
    """(proof, None) for a transactionSubscribe `result` that is a full deposit proof, else
    (None, reason). `owners` is deposit_owners(); `arrived` the host time the frame arrived."""
    try:
        inner, meta = result["transaction"]["transaction"], result["transaction"]["meta"]
        message = inner["message"]
        if meta.get("err") is not None:
            return None, "failed"
        deposits = [ins for ins in message["instructions"] if ins.get("programId") == SOLANA_DEPOSITORY]
        if not deposits:
            return None, "not_a_deposit"
        if len(deposits) != 1:
            return None, "ambiguous_deposit"
        ins = deposits[0]
        raw = base58.b58decode(ins["data"])
        if len(raw) != 48 or raw[:8] != SOLANA_TOKEN_DEPOSIT:
            return None, "not_a_token_deposit"
        amount = int.from_bytes(raw[8:16], "little")
        accounts = ins["accounts"]
        depositor = accounts[1]
        if accounts[2] != depositor:
            return None, "sender_not_depositor"
        if accounts[3] != SOLANA_VAULT or accounts[4] != USDC:
            return None, "not_usdc_vault"
        if amount <= 0:
            return None, "zero_amount"
        if not any(isinstance(k, dict) and k.get("pubkey") == depositor and k.get("signer") is True
                   for k in message["accountKeys"]):
            return None, "depositor_did_not_sign"

        def balance(rows, owner):
            return sum(int(r["uiTokenAmount"]["amount"]) for r in rows
                       if r["mint"] == USDC and r.get("owner") == owner)
        pre, post = meta["preTokenBalances"], meta["postTokenBalances"]
        if (balance(pre, depositor) - balance(post, depositor) != amount
                or balance(post, SOLANA_VAULT) - balance(pre, SOLANA_VAULT) != amount):
            return None, "payment_unproved"
        uids = tuple(sorted(uid for uid, at in (owners.get(depositor) or {}).items()
                            if at is not None and at <= arrived))
        if not uids:
            return None, "unregistered_depositor"
        signature = inner["signatures"][0]
        if result.get("signature") not in (None, signature):
            return None, "malformed"
        return dict(order_id="0x" + raw[16:].hex(), signature=signature, slot=int(result["slot"]),
                    depositor=depositor, amount_atomic=amount, uids=uids, arrived=arrived), None
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None, "malformed"


class DepositProofs:
    """order id -> deposit proof, bounded by age and size. The socket thread puts; the decide
    thread takes, and a take removes the entry (single use)."""

    def __init__(self, *, clock=time.time, ttl_s=TTL_S, max_proofs=MAX_PROOFS, store=None,
                 registry_ok=None, on_put=None):
        self.clock, self.ttl_s, self.max_proofs = clock, ttl_s, max_proofs
        self.store, self.registry_ok = store, registry_ok
        # The 4663 Relay poller's queue (memecoin_rh_relay_prebuy): each NEW deposit, once, no I/O.
        self.on_put = on_put
        self._lock = threading.Lock()
        self._by_order = collections.OrderedDict()
        self.counters = collections.Counter()

    def __len__(self):
        with self._lock:
            return len(self._by_order)

    def _expire(self, now):
        while self._by_order:
            first = next(iter(self._by_order.values()))
            if now - first["arrived"] <= self.ttl_s:
                break
            self._by_order.popitem(last=False)

    def put(self, proof):
        if self.store is not None:
            try:
                self.store.put(proof)
            except Exception:                            # noqa: BLE001 - the map still holds it
                self.counters["store_put_error"] += 1
                logger.exception("rh deposit: store put failed for %s", proof.get("order_id"))
        with self._lock:
            self._expire(self.clock())
            key = proof["order_id"]
            held = self._by_order.get(key)
            if held is not None and held.get("signature") == proof["signature"]:
                return                                   # the same deposit delivered again
            if held is not None:
                # Two signed deposits on one order id: neither binds the fill.
                self._by_order[key] = dict(held, duplicate=True)
                self.counters["duplicate_order"] += 1
                return
            self._by_order[key] = proof
            self.counters["proofs"] += 1
            while len(self._by_order) > self.max_proofs:
                self._by_order.popitem(last=False)
                self.counters["evicted"] += 1
        if self.on_put is not None:
            try:
                self.on_put(proof)
            except Exception:                            # noqa: BLE001 - never the socket's failure
                self.counters["on_put_error"] += 1
                logger.exception("rh deposit: relay poller offer failed for %s", proof.get("order_id"))

    def take(self, fill, *, decimals, now):
        """(lane proof, "matched", margin ms) or (None, reason, margin ms or None)."""
        got = self._take(fill, decimals=decimals, now=now)
        self.counters["take_" + got[1]] += 1
        return got

    def _take(self, fill, *, decimals, now):
        from sniper.settings import memecoin as cfg
        order = str(fill.get("order_id") or "").lower()
        with self._lock:
            self._expire(self.clock())
            proof = self._by_order.pop(order, None) if order else None
        if proof is None:
            return None, "no_deposit", None
        margin_ms = (now - proof["arrived"]) * 1000.0
        if proof.get("duplicate"):
            return None, "duplicate_order", margin_ms
        if not 0 <= now - proof["arrived"] <= MAX_FILL_LAG_S:
            return None, "stale", margin_ms
        uid = fill.get("source_user_id")
        if uid not in proof["uids"]:
            return None, "uid_mismatch", margin_ms
        pay = str(fill.get("payment_token") or "").lower()
        stables = {t.lower() for t in cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES.get("4663", ())}
        dec = decimals.get(pay)
        if pay not in stables or dec is None:
            return None, "payment_token", margin_ms
        ratio = Fraction(int(fill["paid"]), 10 ** int(dec)) / Fraction(proof["amount_atomic"],
                                                                       10 ** _USDC_DECIMALS)
        if not AMOUNT_BAND[0] <= ratio <= AMOUNT_BAND[1]:
            return None, "amount_band", margin_ms
        try:
            if self.registry_ok is None:
                return None, "registry_unavailable", margin_ms
            if self.store is not None:
                got, status = self.store.take(
                    order_id=order, fill_tx=fill.get("tx_hash"), source_user_id=uid,
                    fill_usd=Fraction(int(fill["paid"]), 10 ** int(dec)), now=now,
                    registry_ok=self.registry_ok,
                    fill_info={"chain": "4663", "wallet": fill.get("wallet"), "mint": fill.get("mint")})
                if got is None:
                    return None, status, margin_ms
            elif not self.registry_ok(proof["depositor"], uid):
                return None, "registry_revoked", margin_ms
        except Exception:                                # noqa: BLE001 - no current claim, no proof
            logger.exception("rh deposit: registry/claim failed for %s", order)
            return None, "store_error", margin_ms
        return dict(proof_version=PROOF_VERSION, order_id=proof["order_id"],
                    origin_tx=proof["signature"], origin_wallet=proof["depositor"],
                    origin_slot=proof["slot"], origin_amount_atomic=str(proof["amount_atomic"]),
                    origin_identity_user_id=uid, payer=str(fill.get("payer") or "").lower() or None,
                    origin_arrived=proof["arrived"], margin_ms=margin_ms), "matched", margin_ms


def lane_initiation(payload):
    """The funded hook's source_initiation for a lane datagram carrying a deposit proof, or None."""
    d = payload.get("deposit_proof")
    if not isinstance(d, dict):
        return None
    return dict(schema_version=1, proof_version=d.get("proof_version"), network_id="4663",
                tx_hash=str(payload.get("tx_hash") or "").lower(), block_number=payload.get("seq"),
                mint=str(payload.get("mint") or "").lower(),
                beneficiary=str(payload.get("wallet") or "").lower(), payer=d.get("payer"),
                source_user_id=payload.get("source_user_id"), origin_network_id="solana",
                origin_wallet=d.get("origin_wallet"), origin_tx=d.get("origin_tx"),
                origin_slot=d.get("origin_slot"), origin_amount_atomic=d.get("origin_amount_atomic"),
                order_id=d.get("order_id"), origin_identity_user_id=d.get("origin_identity_user_id"))


def payload_refusal(payload):
    """The hook's own initiation verdict for this datagram, before any attempt row is written (a
    refused attempt would make the on-chain lane skip the same buy as duplicate_of_feed)."""
    tx, mint = str(payload.get("tx_hash") or "").lower(), str(payload.get("mint") or "").lower()
    ev = SimpleNamespace(signal_source="rh_fast_lane", fomo_event_id="rhfast:4663:%s:%s" % (tx, mint),
                         mint=mint, source_user_id=payload.get("source_user_id"),
                         source_wallet=payload.get("wallet"), source_block_number=payload.get("seq"),
                         source_initiation=lane_initiation(payload))
    return source_initiation_refusal(ev, "4663")


def cap_bound(payload):
    """True when the datagram's cap is positive and no looser than the post-trade price x
    (1 + chase): the only cap a deposit-proved (receipt-less) buy may use (operator option 2).

    IN THE CAP'S OWN UNITS. A bridged cap is X per coin atomic while post_wei is wei per coin, so
    the post price is taken back to X at hop A's mid (a_sqrt^2 / 2^192 X per wei, native being
    A's currency0); comparing X with wei refused every bridged deposit, or passed any, by X's price."""
    from sniper.settings import memecoin as cfg
    try:
        chase = Fraction(str(cfg.MEMECOIN_REAL_CHASE_MAX_PCT))
        post = Fraction(int(payload["post_wei_num"]), int(payload["post_wei_den"]))
        if (payload.get("route_kind") or payload.get("kind")) == "v4_bridge":
            post *= Fraction(int(payload["a_sqrt"]) ** 2, 1 << 192)
        cap = Fraction(int(payload["cap_num"]), int(payload["cap_den"]))
        return chase > 0 and post > 0 and 0 < cap <= post * (1 + chase / 100)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def deposit_networks():
    """The funded chains whose armed users' deposits the socket watches: 4663 while option C is on,
    plus MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS (Relay option A). Empty: no socket."""
    from sniper.settings import memecoin as cfg
    return (({"4663"} if cfg.MEMECOIN_RH_FAST_DEPOSIT_PROOF else set())
            | set(cfg.MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS))


def deposit_owners(conn, networks=("4663",)):
    """{solana address: {source_user_id: status_at epoch}}: the conflict-free verified Solana
    addresses of users armed on any of `networks` (the deposit socket's roster)."""
    from datetime import datetime
    from sniper.persistence import db_memecoin as m
    ids = sorted({uid for network in networks for uid in m.real_approved_user_ids(conn, network_id=network)})
    if not ids:
        return {}
    owners = {}
    for uid, address, status_at in conn.execute(
            "SELECT source_user_id, address, status_at FROM memecoin_address_registry "
            "WHERE network_id='solana' AND status='verified' AND conflict_at IS NULL "
            "AND source_user_id IN (%s)" % ",".join("?" * len(ids)), ids):
        try:
            at = datetime.fromisoformat(status_at).timestamp()
        except (TypeError, ValueError):
            at = None
        owners.setdefault(address, {})[uid] = at
    return owners


def key_refusal(url, env):
    """Why `url` may not carry the deposit socket, or None. Never the watcher's key (funded
    detection) and never a funded key."""
    from sniper.execution.solana_rpc_pool import key_id
    from sniper.services.memecoin_research_rpc import funded_overlap
    if not str(url or "").strip():
        return "unset"
    watch = (env.get("ONCHAIN_WATCH_HELIUS_RPC_URL") or "").strip()
    if watch and key_id(watch) == key_id(url):
        return "watcher_key"
    if funded_overlap([url], env):
        return "funded_key"
    return None


def _redact(text):
    """A Helius URL is the credential: never let one reach a log line."""
    import re
    return re.sub(r"api-key=[^&\s'\"]+", "api-key=***", text)


def ws_url(url):
    return "wss://" + url.split("://", 1)[1] if "://" in url else url


class DepositConfirmer:
    """Post-hoc confirmation of matched deposits (its own thread). `rpc(method, params)` returns
    the JSON-RPC result or raises; `sink(row)` records an outcome; `alert_fn(text)` pages."""

    def __init__(self, rpc, *, alert_fn, sink, give_up_s=CONFIRM_GIVE_UP_S, store=None):
        self.rpc, self.alert_fn, self.sink, self.give_up_s = rpc, alert_fn, sink, give_up_s
        # Relay option A: deposits another process claimed (the on-chain watchers) are confirmed
        # here too, read from the shared store; a finished one is marked there.
        self.store = store
        self._lock = threading.Lock()
        self._pending = {}
        self.counters = collections.Counter()

    def add(self, proof, fill):
        with self._lock:
            self._pending[proof["origin_tx"]] = dict(proof=proof, fill=fill, answered=False)

    def _pull_store(self):
        try:
            claimed = self.store.unconfirmed()
        except Exception:                                # noqa: BLE001 - retried next tick
            self.counters["store_read_error"] += 1
            logger.exception("rh deposit: store read for confirmation failed")
            return
        with self._lock:
            for row in claimed:
                if row["origin_tx"] in self._pending:
                    continue
                info = row["fill_info"]
                uid = info.get("source_user_id") or (row["uids"][0] if row["uids"] else None)
                self._pending[row["origin_tx"]] = dict(
                    proof=dict(origin_tx=row["origin_tx"], order_id=row["order_id"],
                               origin_identity_user_id=uid, origin_amount_atomic=row["origin_amount_atomic"],
                               origin_arrived=row["origin_arrived"], margin_ms=row["margin_ms"],
                               # A claim may come 120 s after the deposit: its give-up counts from
                               # the claim (Gamma L3), never from the frame.
                               confirm_from=row["origin_arrived"] + row["margin_ms"] / 1000.0),
                    fill=dict(tx_hash=row["fill_tx"], wallet=info.get("wallet"), mint=info.get("mint"),
                              chain=info.get("chain")),
                    answered=False)

    def tick(self, now):
        if self.store is not None:
            self._pull_store()
        with self._lock:
            pending = dict(self._pending)
        if not pending:
            return
        sigs = sorted(pending)[:256]
        # The status cache holds about 300 slots (~2 min). A deposit older than HISTORY_AFTER_S is
        # asked with searchTransactionHistory, so a claim late in the 120 s window is not "dropped".
        old = {sig for sig in sigs if now - pending[sig]["proof"]["origin_arrived"] > HISTORY_AFTER_S}
        try:
            self.counters["rpc_calls"] += 1
            if old:
                statuses = (self.rpc("getSignatureStatuses", [sigs, {"searchTransactionHistory": True}])
                            or {}).get("value") or [None] * len(sigs)
            else:
                statuses = (self.rpc("getSignatureStatuses", [sigs]) or {}).get("value") or [None] * len(sigs)
        except Exception as exc:                         # noqa: BLE001 - recorded, retried next tick
            self.counters["rpc_errors"] += 1
            self.counters["rpc_429"] += "429" in str(exc)
            statuses = None
        for i, sig in enumerate(sigs):
            item = pending[sig]
            got = statuses[i] if statuses is not None and i < len(statuses) else None
            if statuses is not None:
                item["answered"] = True
            if got and got.get("err") is not None:
                self._finish(sig, item, "initiation_unconfirmed:failed", now, err=got.get("err"))
            elif got and got.get("confirmationStatus") in ("confirmed", "finalized"):
                self._finish(sig, item, "confirmed", now)
            elif now - item["proof"].get("confirm_from", item["proof"]["origin_arrived"]) >= self.give_up_s:
                self._finish(sig, item, "initiation_unconfirmed:%s"
                             % ("dropped" if item["answered"] else "unchecked"), now)

    def _finish(self, sig, item, status, now, err=None):
        with self._lock:
            self._pending.pop(sig, None)
        proof, fill = item["proof"], item["fill"]
        self.counters[status.split(":")[-1]] += 1
        row = dict(status=status, origin_tx=sig, order_id=proof["order_id"], fill_tx=fill.get("tx_hash"),
                   wallet=fill.get("wallet"), mint=fill.get("mint"),
                   source_user_id=proof["origin_identity_user_id"],
                   origin_amount_atomic=proof["origin_amount_atomic"], margin_ms=proof.get("margin_ms"),
                   err=None if err is None else json.dumps(err)[:300], decided_at=now)
        try:
            self.sink(row)
        except Exception:
            logger.exception("rh deposit: confirmation row failed for %s", sig)
        if self.store is not None:
            try:
                self.store.mark_confirmed(sig, status)
            except Exception:
                logger.exception("rh deposit: store confirmation mark failed for %s", sig)
        if status != "confirmed":
            self.alert_fn("%s deposit-proved fill %s (%s): its Solana deposit %s was %s. If the "
                          "lane bought, the position's initiation is UNCONFIRMED; nothing was sold."
                          % (fill.get("chain") or "4663", fill.get("tx_hash"), fill.get("mint"), sig,
                             status.split(":")[-1]))


_PROOF_TABLE = """
CREATE TABLE IF NOT EXISTS rh_deposit_proofs (
    id INTEGER PRIMARY KEY, status TEXT NOT NULL, origin_tx TEXT, order_id TEXT, fill_tx TEXT,
    wallet TEXT, source_user_id TEXT, mint TEXT, origin_amount_atomic TEXT, margin_ms REAL,
    err TEXT, decided_at REAL);
"""


def proof_sink(db_path):
    def sink(row):
        import sqlite3
        db = sqlite3.connect(db_path, timeout=5)
        try:
            db.executescript(_PROOF_TABLE)
            db.execute("INSERT INTO rh_deposit_proofs (status, origin_tx, order_id, fill_tx, wallet, "
                       "source_user_id, mint, origin_amount_atomic, margin_ms, err, decided_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (row["status"], row["origin_tx"], row["order_id"], row["fill_tx"], row["wallet"],
                        row["source_user_id"], row["mint"], row["origin_amount_atomic"],
                        row["margin_ms"], row["err"], row["decided_at"]))
            db.commit()
        finally:
            db.close()
    return sink


def _rpc(url):
    import urllib.request

    def call(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(url, data=body, headers={"content-type": "application/json",
                                                              "user-agent": "Mozilla/5.0 Sniper/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            out = json.loads(resp.read())
        if out.get("error"):
            raise RuntimeError(str(out["error"])[:200])
        return out.get("result")
    return call


def subscribe_params(addresses):
    """transactionSubscribe params: deposits only (the depository REQUIRED), by roster addresses only,
    at processed. Requiring the depository keeps the roster's other transactions off the socket."""
    return [{"accountInclude": list(addresses), "accountRequired": [SOLANA_DEPOSITORY], "vote": False,
             "failed": False},
            {"commitment": "processed", "encoding": "jsonParsed", "transactionDetails": "full",
             "showRewards": False, "maxSupportedTransactionVersion": 0}]


async def _socket(url, roster, proofs, counters, log):
    """One transactionSubscribe at processed over the roster's addresses; resubscribes when the
    roster changes. Any failure (429, close, timeout) waits BACKOFF_S on the same key: no fallback
    key, and no proofs meanwhile, so signals take today's path."""
    import websockets
    while True:
        sub, addrs = None, None
        try:
            async with websockets.connect(ws_url(url), max_size=16_000_000, ping_interval=20,
                                          open_timeout=15) as ws:
                counters["ws_connects"] += 1
                while True:
                    want = roster()
                    if want != addrs:
                        if sub is not None:
                            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method":
                                                      "transactionUnsubscribe", "params": [sub]}))
                        sub, addrs = None, want
                        if want:
                            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe",
                                                      "params": subscribe_params(want)}))
                        counters["ws_subscribed_addresses"] = len(want)
                    try:
                        text = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    arrived = time.time()
                    counters["ws_frames"] += 1
                    counters["ws_bytes"] += len(text)
                    msg = json.loads(text)
                    if msg.get("id") == 1:                 # the subscribe answer
                        if "error" in msg:
                            raise RuntimeError("transactionSubscribe refused: %s" % str(msg["error"])[:200])
                        sub = msg.get("result")
                        continue
                    result = (msg.get("params") or {}).get("result") or {}
                    if SOLANA_DEPOSITORY not in text:
                        continue
                    proof, why = deposit_from_frame(result, owners=roster.owners, arrived=arrived)
                    counters["deposit_" + (why or "proof")] += 1
                    if proof is not None:
                        proofs.put(proof)
                    log("[rh-deposit] %s order %s: %s" % (
                        str(result.get("signature"))[:12],
                        proof["order_id"][:12] if proof else "-", why or "proof"))
        except Exception as exc:                         # noqa: BLE001 - every failure backs off
            counters["ws_errors"] += 1
            counters["ws_429"] += "429" in str(exc)
            log("[rh-deposit] socket down (%s: %s); retry in %.0fs, no proofs meanwhile"
                % (type(exc).__name__, _redact(str(exc))[:160], BACKOFF_S))
            await asyncio.sleep(BACKOFF_S)


def shyft_subscription(addresses, label):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    transactions = {label: pb.SubscribeRequestFilterTransactions(vote=False, failed=False,
        account_include=tuple(addresses), account_required=(SOLANA_DEPOSITORY,))} if addresses else {}
    return pb.SubscribeRequest(transactions=transactions,
        slots={'head': pb.SubscribeRequestFilterSlots(filter_by_commitment=True)}, commitment=pb.PROCESSED)


def shyft_deposit(update, *, owners, arrived, recv_monotonic, head_slot, head_monotonic):
    """Actual processed metadata preserves Option C proof; never confirmed source authority."""
    from sniper.services.memecoin_shyft_confirmed import transaction_metadata, receipt_signers
    receipt = transaction_metadata(update, owners, head_slot=head_slot,
        recv_monotonic=recv_monotonic, head_monotonic=head_monotonic)
    if receipt is None:
        return None, 'metadata_unknown'
    signers = receipt_signers(receipt)
    if signers is None:
        return None, 'signer_unknown'
    message = receipt['transaction']['message']
    message['accountKeys'] = [dict(pubkey=key, signer=key in signers) for key in message['accountKeys']]
    result = dict(signature=receipt['transaction']['signatures'][0], slot=receipt['slot'],
                  transaction={'transaction': receipt['transaction'], 'meta': receipt['meta']})
    return deposit_from_frame(result, owners=owners, arrived=arrived)


def start_shyft_deposit_stream(config, roster, proofs, counters, log, *, stop=None, env=None):
    """One EU processed producer plus bounded immediate off-path proof consumer."""
    import queue
    from urllib.parse import urlsplit
    token, urls = config
    url = next((u for u in urls if urlsplit(u).hostname == 'grpc.fra.shyft.to'), urls[0])
    stop = stop if stop is not None else threading.Event()
    jobs = queue.Queue(maxsize=64)  # each admitted protobuf <=64KiB: at most4MiB pending
    status = counters['stream'] = dict(provider='shyft', hostname=urlsplit(url).hostname,
        commitment='processed', connected=False, generation=0, acknowledged_generation=None,
        requested_count=0, acknowledged_count=0, watch_generation=0, requested_watch_generation=None,
        acknowledged_watch_generation=None, requested_filter_fingerprint=None,
        acknowledged_filter_fingerprint=None, head_slot=None, head_monotonic=None,
        queue_overflows=0, coverage_gaps=0, last_gap=None, consumer_stale=0)
    def consume():
        while not stop.is_set():
            try:
                update, owners, mono, wall, head, head_mono, generation, fingerprint, epoch = jobs.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if (not status['connected'] or generation != status['generation']
                        or fingerprint != status['acknowledged_filter_fingerprint']
                        or epoch != status.get('acknowledged_watch_generation')
                        or tuple(sorted(owners)) != roster()
                        or time.monotonic() - mono > 2):
                    status['consumer_stale'] += 1
                    continue
                proof, why = shyft_deposit(update, owners=owners, arrived=wall,
                    recv_monotonic=mono, head_slot=head, head_monotonic=head_mono)
                counters['deposit_' + (why or 'proof')] += 1
                if proof is not None:
                    proofs.put(proof)  # includes optional store IO, entirely on this consumer
            except Exception:
                counters['deposit_consumer_error'] += 1
                status['coverage_gaps'] += 1
                status['last_gap'] = 'consumer_failure'
            finally:
                jobs.task_done()
    from sniper.services.memecoin_shyft_startup import ShyftStartupGate
    startup = ShyftStartupGate(env=env, stop=stop)
    status['startup_gate'] = startup.status
    consumer = threading.Thread(target=consume, name='rh-deposit-consumer', daemon=True)
    producer = threading.Thread(target=lambda: startup.wait() and asyncio.run(_shyft_deposit_socket(
        url, token, roster, jobs, status, stop)), name='rh-deposit-yellowstone', daemon=True)
    consumer.start()
    producer.start()
    return [consumer, producer]


async def _shyft_deposit_socket(url, token, roster, jobs, status, stop):
    import hashlib
    import queue
    import grpc
    from urllib.parse import urlsplit
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    backoff, generation = 1., 0
    while not stop.is_set():
        stream, ready, label, requested, fingerprint = None, False, None, (), None
        pending = {}
        deadline = time.monotonic() + 10
        observed_roster, roster_epoch, requested_epoch = None, 0, None
        try:
            generation += 1
            status.update(generation=generation, connected=False, acknowledged_generation=None,
                          acknowledged_count=0, head_slot=None, head_monotonic=None,
                          acknowledged_filter_fingerprint=None, acknowledged_watch_generation=None)
            def observe_roster(current):
                nonlocal ready, deadline, observed_roster, roster_epoch
                current = tuple(sorted(current))
                if len(current) > 512:
                    raise ValueError('deposit_roster_limit')
                if current != observed_roster:
                    observed_roster = current
                    roster_epoch += 1
                    ready = False
                    deadline = time.monotonic() + 10
                    status.update(connected=False, watch_generation=roster_epoch,
                        acknowledged_generation=None, acknowledged_watch_generation=None,
                        acknowledged_count=0, acknowledged_filter_fingerprint=None)
                return current
            async def requests():
                nonlocal label, requested, fingerprint, requested_epoch
                last, ping = None, 0
                while not stop.is_set():
                    current = observe_roster(roster())
                    if current != last or requested_epoch != roster_epoch:
                        requested, requested_epoch = current, roster_epoch
                        label = f'deposits:c{generation}:r{requested_epoch}'
                        request = shyft_subscription(current, label)
                        fingerprint = hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()
                        status.update(connected=False, requested_count=len(current),
                            requested_watch_generation=requested_epoch, requested_filter_fingerprint=fingerprint, acknowledged_filter_fingerprint=None,
                            acknowledged_generation=None, acknowledged_count=0)
                        yield request
                        last = current
                    ping += 1
                    pending[ping] = (requested, fingerprint, requested_epoch)
                    if len(pending) > 4:
                        del pending[min(pending)]
                    yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=ping))
                    await asyncio.sleep(2)
            async with grpc.aio.secure_channel(urlsplit(url).hostname + ':443',
                    grpc.ssl_channel_credentials(), options=[('grpc.max_receive_message_length', 65536),
                    ('grpc.keepalive_time_ms', 10000)]) as channel:
                call = channel.stream_stream('/geyser.Geyser/Subscribe',
                    request_serializer=pb.SubscribeRequest.SerializeToString,
                    response_deserializer=pb.SubscribeUpdate.FromString)
                stream = call(requests(), metadata=(('x-token', token),))
                iterator = stream.__aiter__()
                highest, dead = 0, set()
                while not stop.is_set():
                    observe_roster(roster())
                    before = time.monotonic()
                    if not ready and before >= deadline:
                        raise TimeoutError
                    update = await asyncio.wait_for(iterator.__anext__(),
                        10 if ready else deadline - before)
                    mono = time.monotonic()
                    current = observe_roster(roster())
                    if not ready and time.monotonic() >= deadline:
                        raise TimeoutError
                    status['last_frame_monotonic'] = mono
                    if update.HasField('pong'):
                        if (pending.pop(update.pong.id, None) == (current, fingerprint, roster_epoch)
                                and requested_epoch == roster_epoch and requested == current):
                            if not ready and time.monotonic() >= deadline:
                                raise TimeoutError
                            ready, backoff = True, 1.
                            status.update(connected=True, acknowledged_generation=generation,
                                acknowledged_watch_generation=requested_epoch, acknowledged_count=len(requested), acknowledged_filter_fingerprint=fingerprint,
                                last_error=None)
                    elif update.HasField('slot'):
                        slot = update.slot.slot
                        if update.slot.status == pb.SLOT_DEAD:
                            dead.add(slot)
                            if len(dead) > 32:
                                dead.remove(min(dead))
                            if slot == status['head_slot']:
                                status.update(head_slot=None, head_monotonic=None)
                        elif slot and slot not in dead and slot >= highest:
                            highest = slot
                            status.update(head_slot=slot, head_monotonic=mono)
                    elif update.HasField('transaction'):
                        if (not ready or label not in update.filters or current != requested
                                or update.transaction.slot in dead or update.ByteSize() > 65536):
                            continue
                        try:
                            jobs.put_nowait((update, roster.owners, mono, time.time(), status['head_slot'],
                                status['head_monotonic'], generation, fingerprint, requested_epoch))
                        except queue.Full:
                            status['queue_overflows'] += 1
                            status['coverage_gaps'] += 1
                            status['last_gap'] = 'queue_overflow'
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status['last_error'] = exc.code().name if isinstance(exc, grpc.RpcError) else type(exc).__name__
        finally:
            if stream is not None:
                stream.cancel()
            status['connected'] = False
            status['coverage_gaps'] += 1
            status['last_gap'] = 'connection_lost'
        until = time.monotonic() + backoff
        while not stop.is_set() and time.monotonic() < until:
            await asyncio.sleep(min(.1, max(0., until - time.monotonic())))
        backoff = min(30., backoff * 2)


class Roster:
    """The socket's address list, refreshed by the confirm thread every ROSTER_S."""

    def __init__(self):
        self._lock = threading.Lock()
        self.owners, self._addrs = {}, ()

    def set(self, owners):
        with self._lock:
            self.owners, self._addrs = owners, tuple(sorted(owners))

    def __call__(self):
        with self._lock:
            return self._addrs


def deposit_store():
    """Relay option A's shared store, or None while MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS is empty. It
    carries its page: the 2-user page fires from whichever process sees the pair first and the
    funder_pages row is written once, so a store without one would swallow it for every process
    (Gamma L2). send_alert enqueues to the outbox; nothing waits on Telegram."""
    from sniper.settings import memecoin as cfg
    if not cfg.MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS:
        return None
    from sniper.services.memecoin_alerts import send_alert
    from sniper.services.memecoin_relay_deposit_store import DepositStore
    return DepositStore(cfg.MEMECOIN_RELAY_DEPOSIT_STORE_PATH,
                        page_fn=lambda text: send_alert("relay_origin", text))


def start_deposit_proofs(*, ledger_path, db_path, log=print, env=None, on_proof=None):
    """Start the socket and confirm threads in the feed process when deposit_networks() is not empty
    (option C's MEMECOIN_RH_FAST_DEPOSIT_PROOF, or Relay option A's MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS)
    and MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL passes the key guard. Returns (proofs, confirmer,
    counters, threads), or (None, None, None, []) when off. `proofs` (the fast lane's map) is None
    while option C is off: the socket then only feeds the shared store."""
    import os
    from sniper.execution.solana_rpc_pool import key_id
    from sniper.settings import memecoin as cfg
    env = os.environ if env is None else env
    networks = tuple(sorted(deposit_networks()))
    if not networks:
        return None, None, None, []
    from sniper.services.memecoin_solana_provider import infra_provider
    from sniper.services.memecoin_research_rpc import funded_dependency_url
    from sniper.services.memecoin_shyft_signal import live_config
    shyft = infra_provider(env) == "shyft"
    url = funded_dependency_url("rh_deposit", env) if shyft else (
        env.get("MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL") or "").strip()
    refused = None if shyft and url else key_refusal(url, env)
    stream_config = live_config(env) if shyft else None
    if refused:
        log("[rh-deposit] deposit proofs OFF: key %s (%s); every signal takes today's path"
            % (key_id(url) if url else "-", refused))
        return None, None, None, []
    counters = collections.Counter()
    from sniper.services.memecoin_alerts import send_alert
    store = deposit_store()
    from sniper.services.memecoin_relay_deposit_store import registry_reader
    proofs = DepositProofs(store=store, registry_ok=registry_reader(ledger_path), on_put=on_proof)
    roster = Roster()
    rpc = _rpc(url)
    confirmer = DepositConfirmer(rpc, alert_fn=lambda text: send_alert("rh_fast_lane", text),
                                 sink=proof_sink(db_path), store=store)

    def confirm_loop():
        from sniper.persistence.db_core import get_readonly_connection
        last_roster = last_beat = -1e9
        while True:
            now = time.monotonic()
            if now - last_roster >= ROSTER_S:
                last_roster = now
                try:
                    conn = get_readonly_connection(str(ledger_path))
                    try:
                        conn.execute("PRAGMA busy_timeout=250")
                        roster.set(deposit_owners(conn, networks=networks))
                    finally:
                        conn.close()
                except Exception:
                    logger.exception("rh deposit: roster refresh failed (keeping the last one)")
            try:
                confirmer.tick(time.time())
            except Exception:
                logger.exception("rh deposit: confirmation tick failed")
            if now - last_beat >= 60:                   # from the first minute (operator: credit)
                last_beat = now
                if store is not None:
                    try:
                        store.prune()
                    except Exception:
                        logger.exception("rh deposit: store prune failed")
                log("[rh-deposit] beat key=%s %s" % (key_id(url), json.dumps(
                    dict(heartbeat_counters(counters, proofs, confirmer)), sort_keys=True)))
            time.sleep(CONFIRM_POLL_S)

    threads = (start_shyft_deposit_stream(stream_config, roster, proofs, counters, log, env=env) if shyft else [])
    confirm_thread = threading.Thread(target=confirm_loop, name="rh-deposit-confirm", daemon=True)
    if not shyft:
        socket = threading.Thread(target=lambda: asyncio.run(_socket(url, roster, proofs, counters, log)),
                                  name="rh-deposit-socket", daemon=True)
        socket.start()
        threads.append(socket)
    confirm_thread.start()
    threads.append(confirm_thread)
    log("[rh-deposit] deposit proofs ON: key %s, %s, chains %s%s"
        % (key_id(url), "processed Yellowstone" if shyft else "processed transactionSubscribe",
           ",".join(networks), ", shared store" if store is not None else ""))
    # The fast lane's map only while option C is on: off, a proof must never change its decision.
    return (proofs if cfg.MEMECOIN_RH_FAST_DEPOSIT_PROOF else None), confirmer, counters, threads


def heartbeat_counters(counters, proofs, confirmer):
    """Socket bytes/frames/429s, the map's puts and evictions, and the confirmer's RPC calls (each a
    standard Helius call) and outcomes, for the heartbeat row and the 60 s log line."""
    out = collections.Counter(counters)
    if isinstance(out.get("stream"), dict):
        stream = out["stream"].copy()
        mono = stream.get("head_monotonic")
        stream["head_age_s"] = None if mono is None else max(0., time.monotonic() - mono)
        out["stream"] = stream
    out.update({"map_" + k: v for k, v in proofs.counters.items()})
    out.update({"confirm_" + k: v for k, v in confirmer.counters.items()})
    out["map_size"] = len(proofs)
    return out
