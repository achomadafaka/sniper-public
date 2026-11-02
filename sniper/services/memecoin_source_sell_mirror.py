"""Mirror a registry owner's own SELL of a coin we hold (operator order 2026-09-27).

2026-10-20 exception: frozen source-mirror-tp100-v1 positions queue each confirmed, exactly
sized source sale. Processed hints alone cannot prove that ratio. See the named-profile spec.

Full-only (min_fraction >= 1) requires a signed sale and a fresh complete owner/mint snapshot
showing zero tokens, resolved on the existing background worker. Partial/unknown sales hold;
account drops, decoded holdings and frozen first-sell policies cannot grant a full-only exit.
The fractional policy documented below remains available when min_fraction < 1.

Spec: docs/superpowers/specs/2026-09-27-memecoin-pump-id-sell-mirror-design.md. The operator
ordered it built "for safety" knowing the measured copy-sell was break-even at best, and then:
"We need to sell without waiting for any confirmation, that would be too late."

Detection. The slot lane's processed-logs socket decodes the owner's own sells
(memecoin_slot_signal.decode_owner_sells) and `offer`s each one from the socket thread, which
only enqueues. It also taps every own BUY it decodes (`offer_buy`), because the curve sell event
carries no balance: the source's holding of a coin is the sum of its decoded buys less its decoded
sells. The confirmed watcher (scripts/research/onchain_wallet_watch.py) writes sells with an EXACT
share to `onchain_wallet_sells`; that is a backup for sells the socket missed, never a gate.
The slot socket hands one frame's legs together, so split-pool legs of one coin are summed before
deduplication by (source signature, owner, coin). Opposite paths reconcile their cumulative
depletion within a slot; a later observation may prove additional selling. A
transaction that sells two coins is two sales (review S27-03, 2026-09-27).

Decision, on this module's OWN thread so a mirror never waits behind a fastlane pass: match the
open funded Solana position of that source on that coin; size the sell without any read
(PumpSwap: the event's own pre-sell balance; curve: the decoded holding; the watcher: the
transaction's balances); keep the existing size-aware rule, the cumulative share sold since our
position opened, 1 - prod(1 - f), at or above MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION means a FULL
exit. A curve holding that is unknown, or smaller than the sale, counts as a full dump and FIRES
(operator order: the error goes toward selling). The exit is the position's ordinary exit_fn
(claim, exact whole-balance route, sign, send) under the funded exit lock, with none of the price
rails' waits. No network read runs on this thread (review S27-04, 2026-09-27: an audit-only
balance read with a 1.5 s timeout sat between one protective exit and the next); the exact share
of the same sale is in the watcher's onchain_wallet_sells_v2 row, joined on the signature.

ACCOUNT PATH (2026-09-27, routed sellers). jpegkite, Tekkerrss, songz and 397397 trade by
USDC-paid routes (DFlow, OKX, Jupiter into Raydium, Meteora, Whirlpool), and their logs carry no
owner or mint the socket can decode. `OwnerAccountWatch` subscribes, at processed, to each HELD
position's SOURCE owner token account for that coin, with a baseline read when the position is
first seen: every drop is a sale with an exact share (drop / previous balance), whatever the router
or venue. Each subscribe ack re-reads the account once, so a drop between the baseline read and the
subscription going live is not lost (66021, 2026-09-27). The baseline also sizes a curve sell seen
in logs exactly, so a restart no longer turns a small trim into a full exit. One sale seen by
several paths is decided once: the account path is matched to the signed paths by (owner, coin,
slot), since an account notification carries no signature, and signed sales are told apart by
signature (review DR-01). ANY drop counts, a transfer out included: a >=30% drop is a full exit.
That is deliberate, operator decision 2026-09-27 (review S27-01 flag F, option i), not an oversight
to fix. The account is watched from our RESERVATION, not our fill (operator order 2026-09-27, "a,
sell at once"): a drop between the two waits for our position row and then fires.

OBSERVED ORDER (review S27-02, 2026-09-27). A sale's share is fixed when it is SEEN, in the order
events arrive, and the decoded holding shrinks then, once per sale whichever path saw it first. A
buy that arrives later, while the mirror was busy or the sale waited for our position row, never
changes an earlier sale's denominator (hold 100, sell 40, buy 1000 is 40%, not 40/1100). Order
inside one slot is the slot-lane socket's arrival order: the owner's buy and sell of one coin
both write its token account, so they execute serially; that notifications keep that order is
assumed, not verified.
"""
from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

REASON = "mirror_source_sell"
LANE = "source_sell_mirror"
PATH_SLOT = "slot_processed"
PATH_ACCOUNT = "account_processed"
PATH_WATCHER = "watcher_confirmed"
PATHS = (PATH_SLOT, PATH_ACCOUNT, PATH_WATCHER)
# The mirror thread re-reads which source accounts to watch. 0.25 s because reservations are
# watched too (reservation -> fill p50 1.97 s); one pass costs about 0.6 ms with a coin held.
ACCOUNT_REFRESH_S = 0.25
# A FILLED position's owner with no token account for the coin is looked up again after this. A
# reservation's miss is asked again at the next scan: at reservation a node that has not seen the
# source's buy yet finds no account, and a 30 s wait would outlast the fill.
ACCOUNT_RETRY_S = 30.0
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Not a confirmation wait: a sell that lands before our own position row exists (our buy still
# confirming, or the row not yet written) is matched as soon as the row appears, for this long.
MATCH_WINDOW_S = 10.0
WATCHER_MAX_AGE_S = 60.0    # a watcher row older than this is history, never an exit
POLL_S = 0.5                # idle wake to poll the watcher table and retry unmatched sells
READ_TIMEOUT_S = 1.5
# A mirror exit that failed with NOTHING sent is tried again (Alpha, 2026-09-27: the operator's
# intent is "get out"). These statuses all release the claim before any signature exists; an
# exception is included because a raise after sending leaves the claim held, so the retry meets
# `already_closing` and stops. `unknown` (sent, not confirmed) is NEVER retried: recovery owns it.
RETRY_DELAYS_S = (0.5, 1.5, 3.0)
# A rabbit-led fire whose source did not land (memecoin_rabbit_signal): its own entry may still be
# confirming when the verdict arrives, so the position is looked for this long, every pass.
NOT_LANDED_WINDOW_S = 30.0
NOT_LANDED_LANE = "rabbit_landing"
RETRY_STATUSES = frozenset({"stuck", "deferred", "superseded", "balance_unreadable", "error"})
# An executor's own atomic claim lost to a competing exit that claimed after our precheck, while we
# waited for the exit lock (Codex F2 verification, 8cab385f2). For a source-failure exit this is the
# same fact as a visible claim: the exit is held, never discarded (Solana and EVM executors).
# "unknown" (Gamma, on 56415b45b): our own sell was signed and its fate is unknown; its close_sig keeps
# the hold visible until it resolves, and a sell that resolves failed still gets this exit.
# "dust_route_hold" (Alpha's call): the reason does not go away because the route was dust at that
# moment; re-checked at DUST_RECHECK_S (each re-check is an exact exit quote, so slower), the
# ordinary rails keep running meanwhile, a closed position ends it.
CLAIM_COLLISION_STATUSES = frozenset({"already_closing", "unknown", "dust_route_hold"})
DUST_RECHECK_S = 5.0          # the first dust re-check; doubles per dust answer to DUST_RECHECK_MAX_S
DUST_RECHECK_MAX_S = 60.0
_DONE_CAP = 4096
_HELD_CAP = 4096
_COUNTERS = ("seen", "matched", "mirrored", "held_below", "no_position", "duplicate", "skipped",
             "errors")


@dataclass(frozen=True)
class SourceSell:
    path: str
    signature: str
    slot: Optional[int]
    source_user_id: str
    handle: Optional[str]
    owner: str
    venue: str
    mint: Optional[str]
    pool: Optional[str]
    sold_atomic: int
    pre_atomic: Optional[int]     # the owner's balance before the sell, when the evidence proves it
    seen_at: str                  # real-UTC receipt time (the ledger's clock)
    recv_mono: Optional[float] = None
    block_time: Optional[int] = None
    share_basis: Optional[tuple] = None   # (share, basis) fixed when seen; None = decide later
    post_atomic: Optional[int] = None
    exact_receipt: bool = False


def _slot_class(sell):
    """Account notifications have no signature; signed sales retain transaction identity.

    Compare per-class cumulative depletion in a slot without counting overlapping observations
    twice. If the two streams miss disjoint transactions this is a lower bound, not an exact
    union: recovering that union needs additional identity evidence, not a guessed match.
    """
    return "account" if sell.path == PATH_ACCOUNT else "signed"


def _other_class(sell):
    return "signed" if sell.path == PATH_ACCOUNT else "account"


def _epoch(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def parse_account_notification(msg):
    """(slot, atomic balance) from one accountNotification of a token account, or None.

    A closed account (the source sold everything and closed it) arrives with no token data and
    reads as 0. Anything else unreadable is None, never a guessed balance."""
    try:
        result = msg["params"]["result"]
        slot = int(result["context"]["slot"])
        value = result["value"]
        if value is None or not int(value.get("lamports") or 0):
            return slot, 0
        return slot, int(value["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except (KeyError, TypeError, ValueError):
        return None


def held_owner_mints(conn):
    """[(position_id, uid, handle, owner, mint, since)]: each open, unpaused funded Solana position,
    and each funded Solana RESERVATION not yet filled (position_id None), once per verified address
    of its source. Disarmed sources count: we still hold, or are buying, their coin. `since` is the
    confirmed fill (opened_recorded_at, else the decision start), or the reservation time.

    WATCHED FROM THE RESERVATION (operator order 2026-09-27, "a, sell at once"). Reservation ->
    fill is p50 1.97 s on funded Solana, so the subscription is live before most fills, and a
    source drop in between fires the full mirror exit as soon as our position row exists."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_address_registry import armed_verified_owners
    from sniper.settings import memecoin as cfg
    # RESERVATIONS FIRST. Finalization confirms the reservation and opens the position in one
    # commit; read in this order, a commit landing between the two reads shows the pair in both
    # (never in neither), so the account is not unsubscribed in the middle of our fill.
    pending = m.reserved_pending_sources(conn, "solana")
    positions = [p for p in m.open_positions_by_strategy(conn, cfg.MEMECOIN_REAL_POLICY, mode="real")
                 if str(p.get("network_id") or "") == "solana"]
    held = {(str(p["source_user_id"]), p["mint"]) for p in positions}
    reserved = [(str(uid), mint, at) for _pid, uid, mint, at in pending
                if uid and (str(uid), mint) not in held]
    if not positions and not reserved:
        return []
    owners = armed_verified_owners(
        conn, armed={uid for uid, _mint in held} | {uid for uid, _mint, _at in reserved}) or {}
    by_uid = {}
    for address, (uid, handle) in owners.items():
        by_uid.setdefault(str(uid), []).append((address, handle))
    return ([(p["id"], str(p["source_user_id"]), p.get("source_handle"), address, p["mint"],
              p.get("opened_recorded_at") or p.get("opened_at"))
             for p in positions for address, _handle in by_uid.get(str(p["source_user_id"]), [])]
            + [(None, uid, handle, address, mint, at)
               for uid, mint, at in reserved for address, handle in by_uid.get(uid, [])])


class OwnerAccountWatch:
    """accountSubscribe, at processed, on each held position's source-owner token account.

    Its own socket and thread on the keyless read RPC. The mirror thread decides WHAT to watch
    (`resolve`, `set_want`); the socket thread only subscribes and hands each balance to `sink`.
    """

    def __init__(self, *, ws_url, rpc_url, sink, post=None, connect=None, on_ack=None,
                 shyft_config=None, on_gap=None, on_baseline=None, stop=None):
        self._ws_url = ws_url
        self._rpc_url = rpc_url
        self._sink = sink
        self._post = post
        self._connect = connect
        self._on_ack = on_ack
        self._shyft_config = shyft_config
        self._on_gap, self._on_baseline = on_gap, on_baseline
        self._stop = stop if stop is not None else threading.Event()
        self._expected = {}
        self._stream_baselines = set()
        self._lock = threading.Lock()
        self._want = frozenset()
        self._loop = None             # the socket thread's event loop and wake event, once running
        self._wake = None
        self.status = {"connected": False, "subscriptions": 0, "notifications": 0, "acks": 0,
                       "reconnects": 0, "last_error": None}

    def _accounts_result(self, accounts):
        """The `result` of one processed jsonParsed getMultipleAccounts. Raises on any failure,
        a JSON-RPC error body included, so a failed read can never look like an empty account."""
        return self._rpc_result("getMultipleAccounts", [
            accounts, {"encoding": "base64" if self._shyft_config else "jsonParsed",
                       "commitment": "processed"}])

    def _rpc_result(self, method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        if self._post is not None:
            response = self._post(body)
        else:
            import urllib.request
            req = urllib.request.Request(self._rpc_url, body, {
                "content-type": "application/json", "user-agent": "Mozilla/5.0 Sniper/1.0"})
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT_S) as resp:
                response = json.loads(resp.read())
        if not isinstance(response, dict) or response.get("error") is not None:
            raise ValueError("owner account RPC failed")
        return response["result"]

    def full_owner_balance(self, owner, mint, min_slot, *, known_account=None):
        """Complete owner/mint balance at or after a signed sale. Unknown never means zero.

        Called only by the existing resolver thread; the mirror and entry threads never read RPC.
        """
        try:
            result = self._rpc_result("getTokenAccountsByOwner", [owner, {"mint": mint}, {
                "encoding": "jsonParsed", "commitment": "processed", "minContextSlot": min_slot}])
            slot, rows = result["context"]["slot"], result["value"]
            if type(slot) is not int or slot < min_slot or not isinstance(rows, list):
                return None
            total, seen = 0, set()
            for row in rows:
                address, account = row["pubkey"], row["account"]
                if (not isinstance(address, str) or not address or address in seen
                        or account["owner"] not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)):
                    return None
                seen.add(address)
                data = account["data"]
                parsed = data["parsed"]
                info = parsed["info"]
                amount = info["tokenAmount"]["amount"]
                if (data["program"] not in ("spl-token", "spl-token-2022")
                        or parsed["type"] != "account" or info["owner"] != owner or info["mint"] != mint
                        or isinstance(amount, bool) or not isinstance(amount, (str, int))
                        or len(str(amount)) > 20 or not str(amount).isascii()
                        or not str(amount).isdigit() or int(amount) >= 2**64):
                    return None
                total += int(amount)
            if total:
                return slot, total
            # An empty owner index can lag an existing ATA. Check the known account directly.
            if not isinstance(known_account, str) or not known_account:
                return None
            direct = self._rpc_result("getAccountInfo", [known_account, {
                "encoding": "base64", "commitment": "processed", "minContextSlot": slot}])
            direct_slot, value = direct["context"]["slot"], direct["value"]
            if type(direct_slot) is not int or direct_slot < slot:
                return None
            amount = self._raw_balance(value, mint, owner) if value is not None else 0
            return (direct_slot, amount) if amount is not None else None
        except (KeyError, TypeError, ValueError, AttributeError, OSError):
            return None

    def resolve(self, owner, mint):
        """(token account, atomic balance, context slot) of the owner's ATA for `mint`, trying the
        classic and Token-2022 programs in one read; None when neither exists or the read fails."""
        from sniper.execution.pump_native_exit import ata
        accounts = [str(ata(owner, mint, program)) for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)]
        try:
            result = self._accounts_result(accounts)
            slot = int(result["context"]["slot"])
            for account, value, program in zip(accounts, result["value"], (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)):
                if value:
                    if self._shyft_config:
                        amount = self._raw_balance(value, mint, owner, program)
                        if amount is None:
                            continue
                        return account, amount, slot
                    return account, int(value["data"]["parsed"]["info"]["tokenAmount"]["amount"]), slot
        except Exception:
            logger.warning("source-sell mirror: owner account read failed mint=%s", mint,
                           exc_info=True)
        return None

    def read_account(self, account):
        """(context slot, atomic balance) of one watched token account, None when the read fails.

        A null account reads as 0, a closed account, exactly as `parse_account_notification` does:
        the account existed at the baseline, and the mirror drops any answer older than that."""
        try:
            result = self._accounts_result([account])
            slot = int(result["context"]["slot"])
            value = result["value"][0]
            if value is None or not int(value.get("lamports") or 0):
                return None if self._shyft_config else (slot, 0)
            if self._shyft_config:
                expected = self._expected.get(account)
                amount = self._raw_balance(value, *expected) if expected and expected[2] else None
                return (slot, amount) if amount is not None else None
            return slot, int(value["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        except Exception:
            logger.warning("source-sell mirror: account re-read failed account=%s", account[:8],
                           exc_info=True)
        return None

    def set_want(self, accounts):
        """What to watch. A changed set wakes the socket thread at once instead of at its 1 s
        refresh: the one cross-thread call here, and it only schedules `Event.set` on that loop."""
        want = frozenset(accounts)
        if self._shyft_config:
            # Mirror thread publishes immutable identities; no receiver SQLite/read/lock.
            from sniper.execution.pump_native_exit import ata
            expected = {}
            for address, state in accounts.items():
                mint, owner = state['mint'], state['owner']
                prior = self._expected.get(address)
                if prior is not None and prior[:2] == (mint, owner):
                    expected[address] = prior
                    continue
                program = None
                try:
                    program = next((p for p in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)
                        if str(ata(owner, mint, p)) == address), None)
                except (ValueError, TypeError):
                    pass
                expected[address] = (mint, owner, program)
            self._expected = expected
        with self._lock:
            changed, self._want = want != self._want, want
            loop, wake = self._loop, self._wake
        if changed and self._shyft_config:
            self.status.update(connected=False, subscription_acknowledged=False, acknowledged_filter_fingerprint=None)
        if changed and loop is not None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass              # the loop is shutting down; its next refresh reads the want

    def want(self):
        with self._lock:
            return self._want

    def start(self):
        if self._shyft_config:
            return self._start_yellowstone()
        threading.Thread(target=lambda: __import__("asyncio").run(self._run()),
                         name="source-owner-accounts", daemon=True).start()

    @staticmethod
    def _raw_balance(value, mint, owner, expected_program=None):
        import base64
        import base58
        try:
            data = value['data']
            raw = base64.b64decode(data[0], validate=True) if isinstance(data, (list, tuple)) else data
            program = value['owner']
            if (program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) or not mint or not owner
                    or (expected_program is not None and program != expected_program)
                    or (len(raw) != 165 if program == TOKEN_PROGRAM else len(raw) < 165)
                    or raw[:32] != base58.b58decode(mint) or raw[32:64] != base58.b58decode(owner)
                    or raw[108] not in (1, 2)
                    or any(int.from_bytes(raw[o:o + 4], 'little') not in (0, 1) for o in (72, 109, 129))
                    or (program == TOKEN_2022_PROGRAM and len(raw) > 165 and raw[165] != 2)):
                return None
            if program == TOKEN_2022_PROGRAM and len(raw) > 165:
                offset = 166
                while offset < len(raw):
                    if not any(raw[offset:]):
                        break
                    if offset + 4 > len(raw):
                        return None
                    length = int.from_bytes(raw[offset + 2:offset + 4], 'little')
                    offset += 4 + length
                    if offset > len(raw):
                        return None
            return int.from_bytes(raw[64:72], 'little')
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    def _gap_accounts(self, reason, mono):
        self._stream_baselines.clear()
        self.status['coverage_gaps'] = self.status.get('coverage_gaps', 0) + 1
        self.status['last_gap'] = reason
        if self._on_gap is not None:
            self._on_gap(tuple(self._expected), mono)
        if self._on_ack is not None and self.status.get("subscription_acknowledged"):
            for address in self._expected:
                self._on_ack(address, mono)

    def _consume_yellowstone(self, row):
        """Consumer thread only: validate exact token identity, then hand off a balance."""
        mono = row.get('recv_monotonic', time.monotonic())
        generation = (row.get('connection_generation'), row.get('watch_generation'))
        current = (self.status.get('connection_generation'), self.status.get('watch_generation'))
        if row.get('kind') == 'account_coverage':
            self._gap_accounts(row.get('reason', 'account_unknown'), mono)
            return
        if generation != current or not self.status.get('subscription_acknowledged'):
            return
        if time.monotonic() - mono > 2:
            self.status['consumer_stale'] = self.status.get('consumer_stale', 0) + 1
            self._gap_accounts('consumer_stale', time.monotonic())
            return
        address = row.get('pubkey')
        expected = self._expected.get(address)
        if expected is None:
            return
        amount = None if row.get('deleted') or not expected[2] else self._raw_balance(row, *expected)
        if amount is None:
            self._gap_accounts('account_deleted' if row.get('deleted') else 'account_identity', mono)
            return
        if row.get('baseline_only') or address not in self._stream_baselines:
            self._stream_baselines.add(address)
            if self._on_baseline is not None:
                self._on_baseline(address, row['slot'], amount, mono)
            return
        self.status['notifications'] += 1
        self._sink(address, row['slot'], amount, mono)

    def _start_yellowstone(self):
        import asyncio
        import hashlib
        from urllib.parse import urlsplit
        from sniper.services.memecoin_shyft_signal import live_config
        from sniper.services.memecoin_shyft_accounts import AccountQueue, run_accounts, subscription
        from sniper.feeds.yellowstone import geyser_pb2 as pb
        token, urls = self._shyft_config
        live_config({'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft', 'MEMECOIN_SHYFT_LIVE_ENABLED': '1',
            'MEMECOIN_SHYFT_GRPC_TOKEN': token, 'MEMECOIN_SHYFT_GRPC_URLS': ','.join(urls)})
        self.status.update(provider='shyft', hostname=urlsplit(urls[0]).hostname, commitment='processed',
                           coverage_gaps=0, last_gap=None)
        handoff = AccountQueue(maxsize=256)
        # This prepared snapshot needs no producer-side lock; only the mirror replaces it.
        def watch():
            return tuple(self._expected)
        def consume():
            acknowledged = None
            while not self._stop.is_set():
                try:
                    self._consume_yellowstone(handoff.get(timeout=.1))
                except queue.Empty:
                    pass
                except Exception:
                    self._stream_baselines.clear()
                    self.status.update(last_error='ConsumerError', last_gap='consumer_failure')
                    self.status['coverage_gaps'] = self.status.get('coverage_gaps', 0) + 1
                    if self._on_gap is not None:
                        try:
                            self._on_gap(tuple(self._expected), time.monotonic())
                        except Exception:
                            pass
                current = (self.status.get('connection_generation'), self.status.get('watch_generation'))
                request = subscription(self.status.get('requested_accounts', ()),
                    f'accounts:c{current[0]}:w{current[1]}', commitment=pb.PROCESSED)
                self.status['requested_filter_fingerprint'] = hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()
                if (self.status.get('subscription_acknowledged') and current != acknowledged
                        and tuple(sorted(watch())) == self.status.get('requested_accounts')):
                    self.status['acknowledged_filter_fingerprint'] = self.status['requested_filter_fingerprint']
                    acknowledged = current
                    self.status['acks'] += 1
                    self.status['subscriptions'] = len(watch())
                    if self._on_ack is not None:
                        for address in watch():
                            try:
                                self._on_ack(address, time.monotonic())
                            except Exception:
                                self._stream_baselines.clear()
                                self.status.update(last_error='AckConsumerError', last_gap='consumer_failure')
                                self.status['coverage_gaps'] = self.status.get('coverage_gaps', 0) + 1
                                if self._on_gap is not None:
                                    try:
                                        self._on_gap(tuple(self._expected), time.monotonic())
                                    except Exception:
                                        pass
        async def produce():
            from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
            install_grpc_wakeup_handler()
            await run_accounts(urls[0], token, watch, handoff.offer, self.status,
                               self._stop, commitment=pb.PROCESSED)
        consumer = threading.Thread(target=consume, name='source-owner-account-consumer', daemon=True)
        producer = threading.Thread(target=lambda: asyncio.run(produce()),
            name='source-owner-yellowstone', daemon=True)
        consumer.start()
        producer.start()
        return producer

    async def _run(self):
        import asyncio
        import websockets
        connect = self._connect or (lambda u: websockets.connect(u, max_size=2 ** 22,
                                                                 ping_interval=20))
        wake = asyncio.Event()
        with self._lock:
            self._loop, self._wake = asyncio.get_running_loop(), wake
        backoff = 1.0
        while True:
            try:
                async with connect(self._ws_url) as ws:
                    self.status.update(connected=True, last_error=None)
                    backoff = 1.0
                    subs, pending, next_id, refresh_at = {}, {}, 1, 0.0
                    while True:
                        if time.monotonic() >= refresh_at:
                            refresh_at = time.monotonic() + 1.0
                            want = self.want()
                            for sid, account in list(subs.items()):
                                if account not in want:
                                    del subs[sid]
                                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 0,
                                                              "method": "accountUnsubscribe",
                                                              "params": [sid]}))
                            for account in want - set(subs.values()) - set(pending.values()):
                                pending[next_id] = account
                                await ws.send(json.dumps({
                                    "jsonrpc": "2.0", "id": next_id, "method": "accountSubscribe",
                                    "params": [account, {"encoding": "jsonParsed",
                                                         "commitment": "processed"}]}))
                                next_id += 1
                            self.status["subscriptions"] = len(subs)
                        recv = asyncio.ensure_future(ws.recv())
                        woke = asyncio.ensure_future(wake.wait())
                        done, _ = await asyncio.wait(
                            {recv, woke}, timeout=max(0.05, refresh_at - time.monotonic()),
                            return_when=asyncio.FIRST_COMPLETED)
                        recv_mono = time.monotonic()
                        if woke in done:
                            wake.clear()
                            refresh_at = 0.0      # the want changed: subscribe now
                        for task in (recv, woke):
                            if task not in done:
                                task.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await task
                        if recv not in done:
                            continue
                        raw = recv.result()
                        msg = json.loads(raw)
                        if msg.get("id") in pending:
                            account = pending.pop(msg["id"])
                            if isinstance(msg.get("result"), int):
                                subs[msg["result"]] = account
                                self.status["acks"] += 1
                                if self._on_ack is not None:
                                    self._on_ack(account, recv_mono)
                            else:
                                self.status["last_error"] = "subscribe refused: %s" % msg.get("error")
                            continue
                        if msg.get("method") != "accountNotification":
                            continue
                        account = subs.get((msg.get("params") or {}).get("subscription"))
                        got = parse_account_notification(msg) if account else None
                        if got is None:
                            continue
                        self.status["notifications"] += 1
                        self._sink(account, got[0], got[1], recv_mono)
            except Exception as exc:
                self.status.update(connected=False, subscriptions=0, last_error=repr(exc)[:200],
                                   reconnects=self.status["reconnects"] + 1)
                logger.warning("source-sell mirror: owner account socket down: %r", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


class SourceSellMirror:
    def __init__(self, *, enabled, shadow, min_fraction,
                 clock=time.monotonic, wall=time.time, real_now_fn=None,
                 match_window_s=MATCH_WINDOW_S, watcher_max_age_s=WATCHER_MAX_AGE_S):
        self.enabled = bool(enabled)
        self.shadow = bool(shadow)
        self.min_fraction = float(min_fraction)
        self.pools = None             # the slot lane's PumpSwap pool cache, for pool -> mint
        self._clock = clock
        self._wall = wall
        if real_now_fn is None:
            from sniper.util.clock import real_utc_now_iso as real_now_fn
        self._real_now = real_now_fn
        self._window = float(match_window_s)
        self._watcher_max_age = float(watcher_max_age_s)
        self._queue = queue.SimpleQueue()
        # The mirror thread only, from here down.
        self._pending = []            # [(SourceSell, first_seen_mono)]
        self._done = OrderedDict()    # decided (signature, owner, coin) sales (FIFO)
        self._held = OrderedDict()    # (owner, mint) -> atomic the decoded buys and sells leave
        self._tapped = OrderedDict()  # (buy signature, mint), last _HELD_CAP: a re-sent frame adds once
        self._applied = OrderedDict() # sale keys already taken off _held, last _DONE_CAP
        self._slot_sales = OrderedDict()  # per-path depletion; opposite paths may cover only part
        self._product = {}            # position id -> prod(1 - f) of the sells decided so far
        self._sells = {}              # position id -> [(signature, f)]
        self._cursor = None
        self._not_landed = []         # [(verdict, first_seen_mono)] waiting for the fire's position
        self.rabbit_backstop = None   # memecoin_rabbit_signal.RabbitBackstop, set by the slot lane
        self.account_watch = None     # OwnerAccountWatch, set by the runner
        self._acct_by_key = {}        # (owner, mint) -> {"account", "amount", "slot", ...}
        self._acct_by_account = {}    # token account -> the same dict
        self._acct_retry = {}         # (owner, mint) -> monotonic time of the next lookup
        self._acct_refresh_at = 0.0
        # BASELINE READS RUN OFF THIS THREAD (review DR-02, 2026-09-27). `resolve` is a network
        # read with a 1.5 s socket timeout that does not bound DNS, and it used to run here before
        # every pass, so a protective exit could wait behind it. The resolver thread only reads and
        # hands (key, result) back through `_resolved`; every piece of state is still written by
        # this thread alone.
        self._held_pairs = {}         # (owner, mint) -> (position id, uid, handle), last refresh
        self._resolving = set()       # (owner, mint) submitted and not yet answered
        self._resolved = queue.SimpleQueue()
        self._resolve_jobs = None     # the resolver thread's queue, started on first use
        self._full_balances = OrderedDict()  # sale identity -> (requested monotonic, pending, snapshot)
        self._confirmed_sales = OrderedDict()  # (signature, owner, mint) -> confirmed sale evidence
        self.resolver = {"resolved": 0, "failed": 0, "last_error": None}
        # THE SUBSCRIBE-ACK GAP (2026-09-27, 66021). accountSubscribe notifies only changes after it
        # registers, 0.5-4 s after the baseline read, so a drop in between was never seen: 66021's
        # source moved 54% of its bag 1.2 s after our fill and the mirror fired 45 s late. Every
        # ack re-reads that account once, on the resolver thread, and the answer is judged as an
        # ordinary account observation (slot guard, drop = sale). A reconnect re-acks every held
        # account, so a drop during a socket outage is caught the same way.
        self.reread = {"acks": 0, "rereads": 0, "failed": 0, "stale": 0}
        self.notes = []               # stamped lines for the runner log, written by this thread
        # (class, owner, mint, slot) -> (path, receipt monotonic). The slot key only matches the
        # ACCOUNT path against the SIGNATURE paths (review DR-01): see _slot_class.
        self._decided_keys = OrderedDict()
        self._retries = []            # [{"due", "attempt", "position_id", ...}] of failed exits
        self.counts = {path: dict.fromkeys(_COUNTERS, 0) for path in PATHS}
        self.counts.update(buys_tapped=0, exit_retries=0, handed_to_rails=0)

    # ---- the socket thread: O(1), never raises ----
    def offer(self, slot_sell):
        if self.enabled:
            self._queue.put(("sell", slot_sell, self._clock()))

    def offer_many(self, slot_sells):
        """Keep one frame's sale legs together for coin-level aggregation."""
        if self.enabled and slot_sells:
            self._queue.put(("sells", tuple(slot_sells), self._clock()))

    def offer_buy(self, slot_buy):
        if self.enabled:
            self._queue.put(("buy", slot_buy, self._clock()))

    def offer_not_landed(self, verdict):
        """From the slot-signal loop: a rabbit-led fire whose source buy did not land. Its exit is
        live whatever MEMECOIN_SOURCE_SELL_MIRROR_SHADOW says (operator 2026-10-15)."""
        if self.enabled:
            self._queue.put(("not_landed", dict(verdict), self._clock()))

    def offer_account(self, account, slot, amount, recv_mono):
        """From the owner-account socket: one processed balance of a watched source account."""
        if self.enabled:
            self._queue.put(("account", (account, int(slot), int(amount)), recv_mono))

    def offer_account_gap(self, accounts, recv_mono):
        if self.enabled:
            self._queue.put(("account_gap", accounts, recv_mono))

    def offer_account_baseline(self, account, slot, amount, recv_mono):
        if self.enabled:
            self._queue.put(("account_baseline", (account, slot, amount), recv_mono))

    def offer_ack(self, account, recv_mono):
        """From the owner-account socket: the subscription on `account` is live."""
        if self.enabled:
            self._queue.put(("ack", account, recv_mono))

    # ---- the mirror thread ----
    def serve_forever(self, *, conn_factory, contexts, exit_lock, log):
        """Block on the queue; handle each sell the moment it arrives."""
        conn = conn_factory()
        while True:
            wait = POLL_S
            if self.account_watch is not None:
                wait = max(0.01, min(wait, self._acct_refresh_at - self._clock()))
            if self._retries:
                wait = max(0.01, min(wait, min(r["due"] for r in self._retries) - self._clock()))
            try:
                items = [self._queue.get(timeout=wait)]
            except queue.Empty:
                items = []
            try:
                if (self.account_watch is not None
                        and self._clock() >= self._acct_refresh_at):
                    self._acct_refresh_at = self._clock() + ACCOUNT_REFRESH_S
                    self.refresh_accounts(conn)
                for rec in self.run(conn, contexts, exit_lock=exit_lock, items=items):
                    log(record_line(rec))
                while self.notes:
                    log(self.notes.pop(0))
            except Exception:
                logger.exception("source-sell mirror pass failed")
            finally:
                try:
                    conn.rollback()     # end any read transaction so the WAL can checkpoint
                except Exception:
                    pass

    def run(self, conn, contexts, *, exit_lock, items=(), now_iso_fn=None):
        """One pass: take queued items, watcher rows and unmatched sells; decide each sell."""
        if not self.enabled:
            return []
        now_iso_fn = now_iso_fn or self._real_now
        items = list(items)
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if self.account_watch is not None:
            self._apply_resolved()
        fresh = []
        slot_sales = []

        def flush_slot_sales():
            if slot_sales:
                fresh.extend(self._observe(s) for s in self._merge_slot_sales(slot_sales))
                slot_sales.clear()

        for kind, obj, recv_mono in items:          # arrival order: each sale sized as it lands
            if kind in ("sell", "sells"):
                slot_sales.extend(self._from_slot(s, recv_mono)
                                  for s in (obj if kind == "sells" else (obj,)))
                continue
            flush_slot_sales()
            if kind == "buy":
                self._tap_buy(obj)
            elif kind == "account_gap":
                for account in obj:
                    state = self._acct_by_account.get(account)
                    if state is not None:
                        state["amount"] = None
                        state["generation"] = state.get("generation", 0) + 1
                        if "stream_seed_slot" in state:
                            state["stream_seed_slot"] = state["slot"]
                            state["stream_seed_ready"] = False
            elif kind == "account_baseline":
                self._from_account(obj, recv_mono, baseline_only=True)
            elif kind == "ack":
                self._on_ack(obj, recv_mono)
            elif kind == "wake":
                pass                  # a baseline answer: `_apply_resolved` above took it
            elif kind == "full_balance":
                key, requested, got = obj
                current = self._full_balances.get(key)
                if current is not None and current[0] == requested:
                    self._full_balances[key] = (requested, False, got)
                    self.resolver["failed" if got is None else "resolved"] += 1
            elif kind == "not_landed":
                self._not_landed.append((obj, recv_mono))
            elif kind in ("account", "reread"):
                sale = (self._from_account(obj, recv_mono) if kind == "account"
                        else self._from_reread(obj, recv_mono))
                if sale is not None:
                    fresh.append(self._observe(sale))
        flush_slot_sales()
        fresh.extend(self._observe(sale) for sale in self._watcher_rows(conn))
        from sniper.services.memecoin_addon_guard import RING as _addon_ring
        for sell in fresh:
            self.counts[sell.path]["seen"] += 1
            # A source sell clears his add-on ring entry (memecoin_addon_guard): his net is then
            # unknown, so his next buy of the coin is not judged an add-on.
            mint = self._mint(sell.mint, sell.pool)
            if mint:
                _addon_ring.note_sell("solana", sell.source_user_id, mint)
        out = self._run_retries(conn, contexts, exit_lock=exit_lock, now_iso_fn=now_iso_fn)
        out.extend(self._run_not_landed(conn, contexts, exit_lock=exit_lock, now_iso_fn=now_iso_fn))
        now_mono = self._clock()
        work = self._pending + [(sell, now_mono) for sell in fresh]
        self._pending = []
        for sell, first_mono in work:
            if self.min_fraction < 1 and sell.path == PATH_WATCHER and not sell.exact_receipt:
                from sniper.persistence import db_memecoin as m
                from sniper.settings import memecoin as cfg
                from sniper.services.memecoin_funded_policy import is_source_mirror_tp100_position
                pos = m.get_open_position(conn, sell.source_user_id, sell.mint,
                    cfg.MEMECOIN_REAL_POLICY, mode="real", network_id="solana")
                if is_source_mirror_tp100_position(pos):
                    sell = replace(sell, exact_receipt=True, share_basis=None)
            if self._identity(sell) in self._done:
                continue
            if sell.share_basis is None:
                sell = self._observe(sell)   # its pool had no coin when seen; sized once it does
            try:
                rec = self._handle(conn, contexts, sell, exit_lock=exit_lock,
                                   now_iso_fn=now_iso_fn)
            except Exception:
                logger.exception("source-sell mirror: %s failed", sell.signature)
                self.counts[sell.path]["errors"] += 1
                self._decided(self._identity(sell))
                continue
            if rec["outcome"] == "retry":
                if now_mono - first_mono < self._window:
                    self._pending.append((sell, first_mono))
                    continue
                rec["outcome"] = rec.get("expired_as") or "no_position"
            key = rec["outcome"] if rec["outcome"] in _COUNTERS else "skipped"
            self.counts[sell.path][key] += 1
            self._decided(self._identity(sell))
            if rec.get("mint") and sell.slot is not None and rec["outcome"] != "duplicate":
                self._decided_keys.setdefault(
                    (_slot_class(sell), sell.owner, rec["mint"], int(sell.slot)),
                    (sell.path, sell.recv_mono))
                while len(self._decided_keys) > _DONE_CAP:
                    self._decided_keys.popitem(last=False)
            out.append(rec)
        return out

    def _exit(self, conn, contexts, pos, decision_at, exit_lock, rec, reason=REASON):
        """One exit attempt through the position's own exit_fn. Never raises. A source_not_landed
        exit (reason in memecoin_rabbit_signal.EXIT_REASONS) is not a mirror decision: the
        position's mirror_disabled flag does not hold it."""
        from sniper.services.memecoin_real_runner import (
            _alert_exit_result, check_wallet_demotion, context_for_position)
        ctx = context_for_position(contexts, pos)
        if ctx is None:
            return {"status": "no_context"}
        started = self._clock()
        try:
            with exit_lock:
                rec["lock_wait_ms"] = round((self._clock() - started) * 1000.0, 1)
                from sniper.persistence import db_memecoin as m
                pos = m.get_position(conn, pos["id"])
                if not pos or not pos.get("is_open"):
                    return {"status": "already_closed"}
                if pos.get("tracking_paused_at"):
                    return {"status": "tracking_paused"}
                if pos.get("mirror_disabled") and reason == REASON:
                    return {"status": "mirror_disabled"}
                res = ctx["exit_fn"](conn, pos, reason, decision_at) or {}
        except Exception as exc:
            logger.exception("source-sell mirror: exit_fn raised pos=%s", pos.get("id"))
            return {"status": "error", "reason": repr(exc)[:160]}
        _alert_exit_result(pos["mint"], pos["id"], reason, res, handle=pos.get("source_handle"),
                           ticker=pos.get("ticker"), entry_notional=pos.get("entry_notional"))
        if res.get("status") == "closed":
            check_wallet_demotion(conn, pos.get("source_user_id"), network_id="solana")
        return res

    def _note_cost(self, signature, reason, res):
        """A closed source_not_landed exit's realized PnL, to the rabbit backstop's breaker."""
        if (self.rabbit_backstop is not None and reason != REASON and signature
                and (res or {}).get("status") == "closed"):
            try:
                self.rabbit_backstop.record_cost(signature, res.get("realized"))
            except Exception:
                logger.exception("source-sell mirror: rabbit cost record failed sig=%s", signature)

    @staticmethod
    def _rabbit_owned(m, conn, pos, signature):
        """The open position is THIS rabbit-led fire's own: its origin is the source signature and
        that origin's latest attempt went out on the rabbit lane. Never an older position of the
        same coin, never the processed path's."""
        from sniper.services.memecoin_rabbit_signal import DELIVERY_LANE
        if pos.get("origin_event_id") != "slotlane:" + str(signature):
            return False
        attempt = m.entry_attempt_for_origin(conn, pos["origin_event_id"]) or {}
        return (attempt.get("delivery_lane") == DELIVERY_LANE
                and pos.get("attempt_id") in (None, attempt.get("attempt_id")))

    def _run_not_landed(self, conn, contexts, *, exit_lock, now_iso_fn):
        """Exit each rabbit-led fire whose source did not land, once its position is open."""
        from sniper.persistence import db_memecoin as m
        from sniper.settings import memecoin as cfg
        now, keep, out = self._clock(), [], []
        for verdict, first in self._not_landed:
            sig, reason = verdict["signature"], verdict["reason"]
            rec = {"path": NOT_LANDED_LANE, "sig": sig, "uid": verdict.get("source_user_id"),
                   "handle": None, "mint": verdict.get("mint"), "verdict": reason,
                   "exit_reason": reason}
            pos = m.get_open_position(conn, verdict.get("source_user_id"), verdict.get("mint"),
                                      cfg.MEMECOIN_REAL_POLICY, mode="real", network_id="solana")
            if pos is None or not self._rabbit_owned(m, conn, pos, sig):
                if verdict.get("_held_position") is not None:
                    # Held behind another exit's claim and that exit closed the position: done.
                    rec.update(outcome="closed_while_held", position_id=verdict["_held_position"])
                    out.append(rec)
                    continue
                if now - first < NOT_LANDED_WINDOW_S:
                    keep.append((verdict, first))
                    continue
                rec["outcome"] = "not_landed_no_position"       # never filled, or released unsigned
                out.append(rec)
                continue
            rec.update(position_id=pos["id"], handle=pos.get("source_handle"))
            if (pos.get("close_claim_id") or pos.get("close_sig")
                    or pos.get("derisk_claim_id") or pos.get("derisk_sig")):
                # HELD, NEVER DISCARDED (Codex F2, review through 648ada5d7): the verdict is offered
                # once, so dropping it here left a position that outlived the claim (a partial that
                # confirmed, a close that failed) without its source-failure exit. It waits, with no
                # deadline, until the claim resolves (then the ordinary exit_fn runs) or the
                # position closes; a signed claim of unknown fate stays held. Reported once.
                if verdict.get("_held_position") is None:
                    verdict["_held_position"] = pos["id"]
                    rec.update(outcome="close_in_flight", held=True)
                    out.append(rec)
                keep.append((verdict, first))
                continue
            decision_at = now_iso_fn()
            res = self._exit(conn, contexts, pos, decision_at, exit_lock, rec, reason=reason)
            rec.update(outcome="not_landed_exit", exit_status=res.get("status"))
            self._note_cost(sig, reason, res)
            detail = {"code": reason, "source_signature": sig, "basis": verdict.get("basis"),
                      "rabbit_lead_ms": verdict.get("lead_ms"), "age_ms": verdict.get("age_ms"),
                      "frame_slot": verdict.get("slot"), "owner": verdict.get("owner"),
                      "decision_at": decision_at, "lock_wait_ms": rec.get("lock_wait_ms"),
                      "exit_status": res.get("status")}
            try:
                m.record_exit_trigger_audit(
                    conn, position_id=pos["id"], reason=reason, display_price=None, preview_usdc=None,
                    allowed=res.get("status") not in ("tracking_paused", "already_closed", "no_context"),
                    detail=json.dumps(detail, sort_keys=True), checked_at=decision_at,
                    lane=NOT_LANDED_LANE, key=decision_at)
            except Exception:
                logger.exception("source-sell mirror: not-landed audit failed pos=%s", pos["id"])
            if res.get("status") in CLAIM_COLLISION_STATUSES:
                self._hold_retry(rec, pos, 0, status=res.get("status"))
            elif res.get("status") in RETRY_STATUSES:
                self._schedule_retry(rec, pos, 1)
            out.append(rec)
        self._not_landed = keep
        return out

    def _schedule_retry(self, rec, pos, attempt):
        if attempt > len(RETRY_DELAYS_S):
            self.counts["handed_to_rails"] += 1
            rec["handed_to_rails"] = True
            logger.warning("source-sell mirror: exit failed %d times pos=%s status=%s; the price "
                           "rails keep guarding it", attempt, pos["id"], rec.get("exit_status"))
            return
        self._retries.append({"due": self._clock() + RETRY_DELAYS_S[attempt - 1],
                              "attempt": attempt, "position_id": pos["id"],
                              "sig": rec.get("sig"), "path": rec.get("path"),
                              "handle": rec.get("handle"), "uid": rec.get("uid"),
                              "full_sell_key": rec.get("full_sell_key"),
                              "reason": rec.get("exit_reason", REASON)})
        rec["retry_in_s"] = RETRY_DELAYS_S[attempt - 1]

    def _hold_retry(self, rec, pos, attempt, job=None, status=None):
        """Re-check a held source-failure exit (a claim race, our own unknown sell, a dust route) at
        the first retry delay (DUST_RECHECK_S for a dust route), the attempt count unchanged (it never
        reaches the rails' hand-off), reported once."""
        base = job or {"attempt": attempt, "position_id": pos["id"], "sig": rec.get("sig"),
                       "path": rec.get("path"), "handle": rec.get("handle"), "uid": rec.get("uid"),
                       "reason": rec.get("exit_reason", REASON)}
        # A dust route backs off (Gamma LOW): 5 s doubling to 60 s, reset by any other answer.
        dust_n = base.get("dust_n", 0) + 1 if status == "dust_route_hold" else 0
        delay = (min(DUST_RECHECK_MAX_S, DUST_RECHECK_S * 2 ** (dust_n - 1)) if dust_n
                 else RETRY_DELAYS_S[0])
        held = dict(base, due=self._clock() + delay, held=True, dust_n=dust_n)
        rec["hold_delay_s"] = delay
        self._retries.append(held)
        rec["held"] = True

    def _run_retries(self, conn, contexts, *, exit_lock, now_iso_fn):
        from sniper.persistence import db_memecoin as m
        now, due, out = self._clock(), [], []
        due = [r for r in self._retries if r["due"] <= now]
        self._retries = [r for r in self._retries if r["due"] > now]
        for job in due:
            rec = {"path": job["path"], "sig": job["sig"], "uid": job["uid"],
                   "handle": job["handle"], "position_id": job["position_id"],
                   "full_sell_key": job.get("full_sell_key"),
                   "attempt": job["attempt"] + 1}
            pos = m.get_position(conn, job["position_id"])
            reason = job.get("reason", REASON)
            from sniper.services.memecoin_rabbit_signal import EXIT_REASONS
            if (reason in EXIT_REASONS and pos and pos.get("is_open") and not pos.get("tracking_paused_at")
                    and (pos.get("close_claim_id") or pos.get("close_sig")
                         or pos.get("derisk_claim_id") or pos.get("derisk_sig"))):
                # A SOURCE-FAILURE RETRY behind another exit's claim is held, not dropped (Codex F2):
                # re-checked at the first retry delay, the attempt count unchanged, reported once.
                self._retries.append(dict(job, due=now + RETRY_DELAYS_S[0], held=True))
                if not job.get("held"):
                    rec["outcome"] = "retry_held"
                    out.append(rec)
                continue
            if (not pos or not pos.get("is_open") or (pos.get("mirror_disabled") and reason == REASON)
                    or pos.get("tracking_paused_at")
                    or pos.get("close_claim_id")
                    or pos.get("close_sig") or pos.get("derisk_claim_id") or pos.get("derisk_sig")):
                rec["outcome"] = "retry_dropped"       # closed or another exit owns it now
                out.append(rec)
                continue
            proof = self._confirmed_sales.get(job.get("full_sell_key"))
            if (reason == REASON and proof is not None
                    and ((type(proof.pre_atomic) is int and proof.sold_atomic < proof.pre_atomic)
                         or (type(proof.post_atomic) is int and proof.post_atomic > 0))):
                # A retry is still unsigned; newer exact partial proof can revoke its nomination.
                rec.update(outcome="retry_vetoed", basis="full_only_partial_sale")
                out.append(rec)
                continue
            self.counts["exit_retries"] += 1
            rec["exit_reason"] = reason
            res = self._exit(conn, contexts, pos, now_iso_fn(), exit_lock, rec, reason=reason)
            self._note_cost(job.get("sig"), reason, res)
            rec.update(outcome="mirror_retry", exit_status=res.get("status"))
            if reason in EXIT_REASONS and res.get("status") in CLAIM_COLLISION_STATUSES:
                self._hold_retry(rec, pos, job["attempt"], job=job, status=res.get("status"))
                if job.get("held"):
                    continue                                          # already reported
            elif res.get("status") in RETRY_STATUSES:
                self._schedule_retry(rec, pos, job["attempt"] + 1)
            out.append(rec)
        return out

    def snapshot(self):
        return {"enabled": self.enabled, "shadow": self.shadow, "pending": len(self._pending),
                "min_fraction": self.min_fraction, "full_sell_only": self.min_fraction >= 1.0,
                "holdings_tracked": len(self._held), "accounts_watched": len(self._acct_by_account),
                "pairs_reserved": sum(1 for v in self._held_pairs.values() if v[0] is None),
                "account_socket": ({key: value for key, value in self.account_watch.status.copy().items()
                                    if key not in {"requested_accounts", "acknowledged_accounts"}}
                                   if self.account_watch is not None else None),
                "resolver_inflight": len(self._resolving),
                "resolver_failed": self.resolver["failed"],
                "resolver_last_error": self.resolver["last_error"],
                "reread": dict(self.reread),
                **{k: (dict(v) if isinstance(v, dict) else v) for k, v in self.counts.items()}}

    # ---- internals ----
    def _identity(self, sell):
        """One sale: its transaction, the owner and the coin (the pool until it maps). One
        transaction can sell several coins, so the signature alone is not a sale (S27-03)."""
        key = sell.signature, sell.owner, self._mint(sell.mint, sell.pool) or sell.pool
        # A later confirmed receipt may correct an incomplete processed observation of this sale.
        if self.min_fraction >= 1 or sell.exact_receipt:
            return key + (sell.path, sell.pre_atomic, sell.sold_atomic, sell.post_atomic)
        return key

    def _decided(self, key):
        self._done[key] = True
        while len(self._done) > _DONE_CAP:
            self._done.popitem(last=False)

    def refresh_accounts(self, conn):
        """Which source accounts to watch: one per held (owner, coin), with its baseline balance.
        Runs on the mirror thread and never waits on the network: a new pair is handed to the
        resolver thread once, and its baseline is applied when the answer comes back."""
        self._apply_resolved()
        now = self._clock()
        live, held = {}, {}
        for pid, uid, handle, owner, mint, since in held_owner_mints(conn):
            key = (owner, mint)
            held[key] = (pid, uid, handle, since)
            state = self._acct_by_key.get(key)
            if (state is None and key not in self._resolving
                    and now >= self._acct_retry.get(key, 0.0)):
                self._resolving.add(key)
                self._submit_resolve(key)
            if state is not None:
                state.update(position_id=pid, uid=uid, handle=handle, since=since)
                live[key] = state
        self._held_pairs = held
        self._acct_by_key = live
        self._acct_by_account = {state["account"]: state for state in live.values()}
        self._acct_retry = {k: v for k, v in self._acct_retry.items() if v > now}
        self.account_watch.set_want(self._acct_by_account)
        self._apply_resolved()

    def _resolver_jobs(self):
        if self._resolve_jobs is None:
            self._resolve_jobs = queue.SimpleQueue()
            threading.Thread(target=self._resolver_loop, args=(self._resolve_jobs,),
                             name="source-owner-resolver", daemon=True).start()
        return self._resolve_jobs

    def _submit_resolve(self, key):
        self._resolver_jobs().put(("resolve", key))

    def _submit_reread(self, account, ack_mono, generation):
        self._resolver_jobs().put(("reread", account, ack_mono, generation))

    def _resolver_loop(self, jobs):
        """The resolver thread: one read per job. A baseline answers through `_resolved`; an ack
        re-read answers through the mirror queue as ("reread", ...). It reads nothing of the
        mirror's state and never raises out of the loop."""
        while True:
            job = jobs.get()
            if job[0] == "full_balance":
                _kind, key, owner, mint, slot, requested, account = job
                got = None
                try:
                    got = self.account_watch.full_owner_balance(owner, mint, slot, known_account=account)
                except Exception as exc:
                    self.resolver["last_error"] = repr(exc)[:160]
                self._queue.put(("full_balance", (key, requested, got), self._clock()))
                continue
            if job[0] == "reread":
                _kind, account, ack_mono, generation = job
                started, got = self._clock(), None
                try:
                    got = self.account_watch.read_account(account)
                except Exception as exc:
                    self.resolver["last_error"] = repr(exc)[:160]
                    logger.exception("source-sell mirror: re-read failed account=%s", account[:8])
                done = self._clock()
                self._queue.put(("reread", (account, got, ack_mono, (done - started) * 1000.0,
                                            generation),
                                 done))
                continue
            key, got = job[1], None
            try:
                got = self.account_watch.resolve(*key)
            except Exception as exc:
                self.resolver["last_error"] = repr(exc)[:160]
                logger.exception("source-sell mirror: baseline resolver failed mint=%s", key[1])
            self._resolved.put((key, got))
            # Wake the mirror thread so the baseline applies (and the socket subscribes) now, not
            # at its next idle poll.
            self._queue.put(("wake", None, self._clock()))

    def _apply_resolved(self):
        """Mirror thread: take the resolver's answers. A pair no longer held is dropped; a failed
        read is looked up again after ACCOUNT_RETRY_S once filled, and at the next scan while it is
        still a reservation."""
        changed = False
        while True:
            try:
                key, got = self._resolved.get_nowait()
            except queue.Empty:
                break
            self._resolving.discard(key)
            held = self._held_pairs.get(key)
            if got is None:
                self.resolver["failed"] += 1
                filled = held is not None and held[0] is not None
                self._acct_retry[key] = self._clock() + (ACCOUNT_RETRY_S if filled else 0.0)
                continue
            self.resolver["resolved"] += 1
            if held is None or key in self._acct_by_key:
                continue
            state = {"account": got[0], "amount": got[1], "slot": got[2], "owner": key[0],
                     "mint": key[1], "position_id": held[0], "uid": held[1], "handle": held[2],
                     "since": held[3], "baseline_slot": got[2], "baseline_amount": got[1],
                     "resolved_mono": self._clock()}
            self._acct_by_key[key] = state
            self._acct_by_account[state["account"]] = state
            changed = True
        if changed:
            self.account_watch.set_want(self._acct_by_account)

    def _from_account(self, obj, recv_mono, *, notification=True, baseline_only=False):
        """A processed balance of a watched source account; a DROP is a sale with an exact share."""
        account, slot, amount = obj
        state = self._acct_by_account.get(account)
        if state is None:
            return None
        if baseline_only:
            # HTTP bank context has no write version. A stream startup/reset cannot order
            # writes inside that bank, including when its first snapshot is older.
            state["stream_seed_slot"] = state["slot"]
            state["stream_seed_ready"] = False
        if slot < state["slot"]:
            return None
        if "stream_seed_slot" in state:
            if not notification:
                state["stream_seed_slot"] = slot
                state["stream_seed_ready"] = False
                baseline_only = True  # an HTTP reread seeds; it is not a source sale
            elif not state["stream_seed_ready"]:
                if slot <= state["stream_seed_slot"]:
                    return None
                state["stream_seed_ready"] = True
                baseline_only = True  # first strictly newer stream state establishes order
        before = state["amount"]
        state["amount"], state["slot"] = amount, slot
        if amount != before:
            state["balance_version"] = state.get("balance_version", 0) + 1
        if notification:
            state["generation"] = state.get("generation", 0) + 1
            state["last_notification_mono"] = recv_mono
        if baseline_only or before is None or amount >= before:
            return None                 # a buy, or nothing: the baseline moves, no sale
        age = max(0.0, self._clock() - recv_mono)
        now = _epoch(self._real_now()) or self._wall()
        # A buy can restore a balance inside this slot before another sale reaches the same
        # amount. That second drop is distinct; an unchanged repeat already returned above.
        return SourceSell(path=PATH_ACCOUNT,
                          signature="account:%s:%d:%d:%d" % (
                              account, slot, amount, state["balance_version"]),
                          slot=slot, source_user_id=state["uid"], handle=state["handle"],
                          owner=state["owner"], venue="account", mint=state["mint"], pool=None,
                          sold_atomic=before - amount, pre_atomic=before, recv_mono=recv_mono,
                          seen_at=datetime.fromtimestamp(now - age, tz=timezone.utc).isoformat())

    def _on_ack(self, account, ack_mono):
        """The subscription on a watched account is live: re-read it once, off this thread."""
        state = self._acct_by_account.get(account)
        if state is None:
            return
        self.reread["acks"] += 1
        self._submit_reread(account, ack_mono, state.get("generation", 0))

    def _from_reread(self, obj, recv_mono):
        """The ack re-read of a watched account, judged as an ordinary account observation. An
        answer older than what this account has already seen is dropped, never a sale."""
        account, got, ack_mono, read_ms = obj[:4]
        generation = obj[4] if len(obj) > 4 else None
        state = self._acct_by_account.get(account)
        if state is None:
            return None
        now = _epoch(self._real_now()) or self._wall()
        # From our confirmed fill, or from our reservation while the coin is not filled yet (a
        # subscription live before the fill is what subscribing early is for).
        since = _epoch(state.get("since"))
        label = "fill_to_ack_ms" if state.get("position_id") is not None else "reserved_to_ack_ms"
        since_to_ack = (None if since is None
                        else "%.0f" % ((now - max(0.0, self._clock() - ack_mono) - since) * 1000.0))
        # applied_to_ack: from the baseline reaching this thread (its read ended up to one poll
        # earlier) to the subscription going live.
        head = ("[memecoin] source-sell mirror: watching %s %s pos=%s %s=%s "
                "applied_to_ack_ms=%.0f baseline slot=%s amount=%s"
                % (state.get("handle") or state.get("uid"), account[:8], state.get("position_id"),
                   label, since_to_ack, (ack_mono - state["resolved_mono"]) * 1000.0,
                   state["baseline_slot"], state["baseline_amount"]))
        if got is None:
            self.reread["failed"] += 1
            self.notes.append("%s reread failed read_ms=%.0f" % (head, read_ms))
            return None
        self.reread["rereads"] += 1
        slot, amount = got
        if (slot < state["slot"] or
                (generation is not None and generation != state.get("generation", 0)) or
                state.get("last_notification_mono", float("-inf")) > ack_mono):
            self.reread["stale"] += 1
            sale, verdict = None, "stale (newer notification or slot %s)" % state["slot"]
        else:
            sale = self._from_account((account, slot, amount), recv_mono, notification=False,
                baseline_only=bool(getattr(self.account_watch, '_shyft_config', None)))
            verdict = "drop" if sale is not None else "no drop"
        self.notes.append("%s reread slot=%s amount=%s read_ms=%.0f verdict=%s"
                          % (head, slot, amount, read_ms, verdict))
        return sale

    def _mint(self, mint, pool):
        if mint:
            return mint
        facts = self.pools.get(pool) if (self.pools is not None and pool) else None
        return (facts or {}).get("base_mint")

    def _tap_buy(self, b):
        """A decoded own buy adds to the source's known holding of that coin."""
        mint = self._mint(b.mint, b.pool)
        amount = int(getattr(b, "token_amount", 0) or 0)
        if not mint or amount <= 0:
            return
        frame = (getattr(b, "signature", None), mint)
        if frame[0] is not None:
            if frame in self._tapped:
                return
            self._tapped[frame] = True
            while len(self._tapped) > _HELD_CAP:
                self._tapped.popitem(last=False)
        key = (b.owner, mint)
        slot = getattr(b, "slot", None)
        totals = self._slot_sales.get((b.owner, mint, slot))
        if totals is not None and totals["baseline"] is not None:
            totals["baseline"] += amount
        self._held[key] = self._held.get(key, 0) + amount
        self._held.move_to_end(key)
        while len(self._held) > _HELD_CAP:
            self._held.popitem(last=False)
        self.counts["buys_tapped"] += 1

    def _from_slot(self, s, recv_mono):
        # The socket stamps host time; the ledger is real UTC. Re-date the receipt on the ledger's
        # clock by the monotonic age, so "before our entry" compares one clock with itself. The
        # socket's own receipt stamp (taken before any decode) wins over the enqueue time.
        recv_mono = getattr(s, "recv_monotonic", None) or recv_mono
        age = max(0.0, self._clock() - recv_mono)
        now = _epoch(self._real_now()) or self._wall()
        return SourceSell(path=PATH_SLOT, signature=s.signature, slot=s.slot,
                          source_user_id=s.source_user_id, handle=s.handle, owner=s.owner,
                          venue=s.venue, mint=s.mint, pool=s.pool, sold_atomic=int(s.sold_atomic),
                          pre_atomic=s.pre_atomic, recv_mono=recv_mono,
                          seen_at=datetime.fromtimestamp(now - age, tz=timezone.utc).isoformat())

    def _merge_slot_sales(self, sales):
        """One signed decision per owner, transaction and resolved mint."""
        merged = OrderedDict()
        for sale in sales:
            mint = self._mint(sale.mint, sale.pool)
            key = (sale.signature, sale.owner, mint or ("pool", sale.pool))
            prior = merged.get(key)
            if prior is None:
                merged[key] = (replace(sale, mint=mint), {sale.pool}, sale.sold_atomic,
                               sale.pre_atomic)
                continue
            first, pools, sold, pre = prior
            if sale.pool in pools:
                continue                 # duplicate delivery, not another route leg
            pools.add(sale.pool)
            # The decoder already aggregates repeated visits to a pool. Adding its entire
            # sold total to another pool's pre-balance overcounts interleaved A/B/A routes.
            pre = (max(pre, sale.pre_atomic)
                   if pre is not None and sale.pre_atomic is not None else None)
            merged[key] = (first, pools, sold + sale.sold_atomic, pre)
        return [replace(first, sold_atomic=sold, pre_atomic=pre)
                for first, _pools, sold, pre in merged.values()]

    def _watcher_rows(self, conn):
        from sniper.persistence import db_memecoin as m
        try:
            if self._cursor is None:
                self._cursor = m.onchain_sells_max_id(conn)   # never replay history on a restart
                return []
            rows = m.onchain_sells_after(conn, self._cursor)
        except Exception:
            logger.exception("source-sell mirror: watcher sells unreadable")
            return []
        now = _epoch(self._real_now())
        out = []
        for row in rows:
            self._cursor = max(self._cursor, int(row["id"]))
            detected = _epoch(row["detected_at"])
            if now is None or detected is None or now - detected > self._watcher_max_age:
                continue
            out.append(SourceSell(
                path=PATH_WATCHER, signature=row["signature"], slot=row["slot"],
                source_user_id=row["source_user_id"], handle=row["handle"], owner=row["address"],
                venue="watcher", mint=row["mint"], pool=None, sold_atomic=int(row["sold_atomic"]),
                pre_atomic=(int(row["pre_atomic"]) if row["pre_atomic"] is not None else None),
                seen_at=row["detected_at"], block_time=row["block_time"],
                post_atomic=(int(row["post_atomic"]) if row.get("post_atomic") is not None else None)))
        return out

    def _observe(self, sell):
        """Fix the sale's share against the holding as it stands when the sale is SEEN, and take
        the sale off the decoded holding once, whichever path saw it first. A later event never
        changes either (S27-02). A pool not yet mapped to its coin is sized at decision time."""
        if (self.min_fraction >= 1 and sell.path == PATH_WATCHER and sell.signature and sell.mint
                and type(sell.sold_atomic) is int and sell.sold_atomic > 0):
            partial = ((type(sell.pre_atomic) is int and sell.sold_atomic < sell.pre_atomic)
                       or (type(sell.post_atomic) is int and sell.post_atomic > 0))
            full = (type(sell.pre_atomic) is int and sell.sold_atomic == sell.pre_atomic
                    and type(sell.post_atomic) is int and sell.post_atomic == 0)
            # Either known remainder vetoes liquidation; accepting full still needs both fields.
            if partial or full:
                key = sell.signature, sell.owner, sell.mint
                self._confirmed_sales[key] = sell
                self._confirmed_sales.move_to_end(key)
                while len(self._confirmed_sales) > _DONE_CAP:
                    self._confirmed_sales.popitem(last=False)
        if self.min_fraction >= 1 or sell.exact_receipt:
            return sell               # full-only never infers liquidation from decoded inventory
        mint = self._mint(sell.mint, sell.pool)
        if mint is None:
            return sell
        identity = ("sig", sell.signature, sell.owner, mint)
        if identity in self._applied:
            return replace(sell, share_basis=(0.0, "cross_path_duplicate"))
        share_basis = self._share(sell, mint)
        credited = sell.sold_atomic
        if sell.slot is not None:
            key = (sell.owner, mint, int(sell.slot))
            totals = self._slot_sales.get(key)
            if totals is None:
                account = self._acct_by_key.get((sell.owner, mint))
                baseline = sell.pre_atomic
                if baseline is None and account is not None and account["slot"] < sell.slot:
                    baseline = account["amount"]
                if baseline is None:
                    baseline = self._held.get((sell.owner, mint))
                totals = {"sold": {"signed": 0, "account": 0},
                          "left": {"signed": 1.0, "account": 1.0}, "baseline": baseline,
                          "baseline_basis": share_basis[1]}
                self._slot_sales[key] = totals
            kind = _slot_class(sell)
            # Size unknown-pre curve events against THIS path's remaining holding. The other
            # stream may already have accounted for the same sale in the shared decoded holding.
            if sell.pre_atomic is None and totals["baseline"] is not None:
                remaining = totals["baseline"] - totals["sold"][kind]
                share_basis = ((sell.sold_atomic / remaining, totals["baseline_basis"])
                               if remaining >= sell.sold_atomic > 0
                               else (1.0, "sold_exceeds_decoded_holding_fire"))
            before_left = min(totals["left"].values())
            before_sold = max(totals["sold"].values())
            totals["sold"][kind] += sell.sold_atomic
            totals["left"][kind] *= 1.0 - share_basis[0]
            after_left = min(totals["left"].values())
            credited = max(totals["sold"].values()) - before_sold
            # Products are order independent when events carry exact pre-balances. Comparing
            # raw amounts and trimming the current denominator fails on reordered signed events.
            increment = max(0.0, 1.0 - after_left / before_left) if before_left > 0 else 0.0
            basis = share_basis[1] if totals["sold"][_other_class(sell)] == 0 else "cross_path_increment"
            share_basis = (increment, basis if increment > 0 else "cross_path_duplicate")
            while len(self._slot_sales) > _DONE_CAP:
                self._slot_sales.popitem(last=False)
        held_key = (sell.owner, mint)
        if held_key in self._held:
            self._held[held_key] = max(0, self._held[held_key] - credited)
        self._applied[identity] = True
        while len(self._applied) > _DONE_CAP:
            self._applied.popitem(last=False)
        return replace(sell, share_basis=share_basis)

    def _share(self, sell, mint):
        """(fraction of the holding this sale is, basis). Never a read; unknown fires."""
        if sell.pre_atomic is not None and sell.pre_atomic >= sell.sold_atomic > 0:
            basis = {PATH_SLOT: "event_pre_balance", PATH_ACCOUNT: "account_balance"}.get(
                sell.path, "transaction_balances")
            return sell.sold_atomic / sell.pre_atomic, basis
        account = self._acct_by_key.get((sell.owner, mint))
        if (account is not None and sell.slot is not None and account["slot"] < sell.slot
                and account["amount"] >= sell.sold_atomic > 0):
            return sell.sold_atomic / account["amount"], "account_baseline"
        held = self._held.get((sell.owner, mint))
        if held is None:
            return 1.0, "holding_unknown_fire"
        if held < sell.sold_atomic:
            return 1.0, "sold_exceeds_decoded_holding_fire"
        return sell.sold_atomic / held, "decoded_buys"

    def _full_sell(self, sell, mint):
        """(full, basis); None waits for a bounded off-thread complete owner snapshot."""
        if sell.path == PATH_ACCOUNT:
            return False, "full_only_unsigned_account_drop"
        if (type(sell.sold_atomic) is not int or sell.sold_atomic <= 0
                or type(sell.slot) is not int or sell.slot <= 0):
            return False, "full_only_unknown_sale"
        proof = self._confirmed_sales.get((sell.signature, sell.owner, mint), sell)
        if proof.pre_atomic is not None:
            if type(proof.pre_atomic) is not int or proof.pre_atomic < proof.sold_atomic:
                return False, "full_only_unknown_prebalance"
            if proof.sold_atomic < proof.pre_atomic:
                return False, "full_only_partial_sale"
        if proof.post_atomic is not None and (type(proof.post_atomic) is not int or proof.post_atomic != 0):
            return False, "full_only_nonzero_or_unknown_remainder"
        now = self._clock()
        seen = _epoch(sell.seen_at)
        receipt_now = _epoch(self._real_now())
        age = (now - sell.recv_mono if sell.recv_mono is not None else
               receipt_now - seen if receipt_now is not None and seen is not None else self._window + 1)
        if age < 0 or age >= self._window or self.account_watch is None:
            return False, "full_only_snapshot_unavailable"
        key = self._identity(sell)
        cached = self._full_balances.get(key)
        if cached is None:
            self._full_balances[key] = (now, True, None)
            while len(self._full_balances) > _DONE_CAP:
                self._full_balances.popitem(last=False)
            account = (self._acct_by_key.get((sell.owner, mint)) or {}).get("account")
            self._resolver_jobs().put(("full_balance", key, sell.owner, mint, sell.slot, now, account))
            return None, "full_only_snapshot_pending"
        requested, pending, snapshot = cached
        if now - requested >= self._window:
            return False, "full_only_snapshot_expired"
        if pending:
            return None, "full_only_snapshot_pending"
        if snapshot is None:
            return False, "full_only_snapshot_unknown"
        slot, amount = snapshot
        if type(slot) is not int or slot < sell.slot or type(amount) is not int or amount < 0:
            return False, "full_only_snapshot_unknown"
        return amount == 0, "full_owner_zero" if amount == 0 else "full_only_owner_still_holds"

    def _handle(self, conn, contexts, sell, *, exit_lock, now_iso_fn):
        from sniper.persistence import db_memecoin as m
        from sniper.settings import memecoin as cfg
        rec = {"path": sell.path, "sig": sell.signature, "slot": sell.slot,
               "uid": sell.source_user_id, "handle": sell.handle, "venue": sell.venue}
        mint = self._mint(sell.mint, sell.pool)
        if not mint:
            return {**rec, "outcome": "retry", "expired_as": "unknown_pool"}
        rec["mint"] = mint
        full_key = ("full", sell.signature, sell.owner, mint)
        if self.min_fraction >= 1 and full_key in self._done:
            return {**rec, "outcome": "duplicate"}
        first = (self._decided_keys.get((_other_class(sell), sell.owner, mint, int(sell.slot)))
                 if sell.slot is not None else None)
        pos = m.get_open_position(conn, sell.source_user_id, mint, cfg.MEMECOIN_REAL_POLICY,
                                  mode="real", network_id="solana")
        if not pos:
            return {**rec, "outcome": "retry"}
        rec["position_id"] = pos["id"]
        self.counts[sell.path]["matched"] += 1
        from sniper.services.memecoin_funded_policy import is_source_mirror_tp100_position
        exact_mirror = is_source_mirror_tp100_position(pos)
        if not exact_mirror and sell.share_basis == (0.0, "cross_path_duplicate"):
            rec.update(outcome="duplicate", first_path=first[0] if first else None,
                       lag_behind_first_ms=(round((sell.recv_mono - first[1]) * 1000.0, 1)
                                            if first and sell.recv_mono is not None and first[1] is not None
                                            else None))
            return rec
        source_slot = None
        if exact_mirror and pos.get("attempt_id"):
            evidence = m.entry_attempt_source_evidence(conn, pos["attempt_id"])
            if (evidence.get("source_user_id") == pos["source_user_id"]
                    and evidence.get("network_id") == "solana" and evidence.get("mint") == mint
                    and evidence.get("source_wallet") == sell.owner
                    and type(evidence.get("source_slot")) is int and evidence["source_slot"] >= 0):
                source_slot = evidence["source_slot"]
        opened = _epoch(pos.get("opened_at"))
        when = sell.block_time if sell.block_time is not None else _epoch(sell.seen_at)
        slack = 1.0 if sell.block_time is not None else 0.0   # block_time is whole seconds
        if source_slot is not None and type(sell.slot) is int and sell.slot < source_slot:
            return {**rec, "outcome": "before_entry"}
        after_source = source_slot is not None and type(sell.slot) is int and sell.slot > source_slot
        if not after_source and opened is not None and when is not None and when < opened - slack:
            return {**rec, "outcome": "before_entry"}
        if pos.get("tracking_paused_at"):
            return {**rec, "outcome": "tracking_paused"}
        if pos.get("mirror_disabled"):
            return {**rec, "outcome": "mirror_disabled"}
        if exact_mirror:
            # Account drops are unsigned; processed pool balances may omit another owner account.
            # The confirmed watcher carries the signed sale and its exact before/after amounts.
            known = all(type(value) is int for value in
                        (sell.sold_atomic, sell.pre_atomic, sell.post_atomic))
            if (sell.path != PATH_WATCHER or not sell.signature or not known
                    or not 0 < sell.sold_atomic <= sell.pre_atomic
                    or sell.post_atomic != sell.pre_atomic - sell.sold_atomic):
                return {**rec, "outcome": "exact_ratio_unproven"}
            decision_at = now_iso_fn()
            share = sell.sold_atomic / sell.pre_atomic
            if not self.shadow:
                from sniper.services.memecoin_manual_exit_intents import enqueue_source_partial
                rec["intent_id"] = enqueue_source_partial(
                    conn, position_id=pos["id"], network_id="solana", source_owner=sell.owner,
                    mint=mint, source_signature=sell.signature, source_event_index=0,
                    source_sold_atomic=sell.sold_atomic, source_pre_atomic=sell.pre_atomic,
                    now_iso=decision_at, source_proof={"path": sell.path, "slot": sell.slot,
                        "seen_at": sell.seen_at, "block_time": sell.block_time})
            rec.update(share=share, cumulative=None, basis="exact_receipt",
                       outcome="mirrored", shadow=self.shadow)
            self._audit(m, conn, pos, sell, rec, decision_at, share=share, cumulative=None,
                        basis="exact_receipt", allowed=not self.shadow,
                        code="source_partial_shadow" if self.shadow else "source_partial")
            return rec
        if (pos.get("close_claim_id") or pos.get("close_sig")
                or pos.get("derisk_claim_id") or pos.get("derisk_sig")):
            return {**rec, "outcome": "close_in_flight"}
        if self.min_fraction >= 1:
            rec["full_sell_key"] = sell.signature, sell.owner, mint
            mirror, basis = self._full_sell(sell, mint)
            if mirror is None:
                return {**rec, "outcome": "retry", "expired_as": "full_only_snapshot_expired"}
            cached = self._full_balances.get(self._identity(sell))
            if cached is not None and cached[2] is not None:
                rec["source_balance_slot"], rec["source_remaining_atomic"] = cached[2]
            share = cumulative = 1.0 if mirror else None
            first_sell = False
        else:
            share, basis = sell.share_basis or self._share(sell, mint)
            product = self._product.get(pos["id"], 1.0) * (1.0 - share)
            self._product[pos["id"]] = product
            self._sells.setdefault(pos["id"], []).append((sell.signature, round(share, 4)))
            cumulative = 1.0 - product
            # Frozen legacy first-sell policies cannot bypass full-only mirror configuration.
            first_sell = share > 0 and _first_sell_policy(pos)
            mirror = first_sell or cumulative >= self.min_fraction
        decision_at = now_iso_fn()
        decided_mono = self._clock()
        refused = None
        rec.update(share=None if share is None else round(share, 4),
                   cumulative=None if cumulative is None else round(cumulative, 4), basis=basis,
                   recv_to_decision_ms=(round((decided_mono - sell.recv_mono) * 1000.0, 1)
                                        if sell.recv_mono is not None else None))
        try:
            if mirror and not self.shadow:
                res = self._exit(conn, contexts, pos, decision_at, exit_lock, rec)
                rec["exit_status"] = res.get("status")
                if res.get("status") in ("tracking_paused", "mirror_disabled", "already_closed"):
                    refused = res["status"]
                if res.get("status") == "no_context":
                    rec["outcome"] = "no_context"
                    return rec
                if res.get("status") in RETRY_STATUSES:
                    self._schedule_retry(rec, pos, 1)
        finally:
            kind = "first_sell" if first_sell else "size_mirror"
            code = refused or ("size_hold" if not mirror else
                               kind + "_shadow" if self.shadow else kind)
            self._audit(m, conn, pos, sell, rec, decision_at, share=share, cumulative=cumulative,
                        basis=basis, allowed=mirror and not self.shadow and not refused, code=code)
        rec["outcome"] = refused or ("mirrored" if mirror else "held_below")
        if self.min_fraction >= 1 and mirror:
            self._decided(full_key)
        if mirror and self.shadow:
            rec["shadow"] = True
        return rec

    def _audit(self, m, conn, pos, sell, rec, decision_at, *, share, cumulative, basis, allowed,
               code):
        detail = {"code": code, "path": sell.path, "source_signature": sell.signature,
                  "source_slot": sell.slot, "venue": sell.venue, "owner": sell.owner,
                  "sold_atomic": str(sell.sold_atomic),
                  "pre_atomic": None if sell.pre_atomic is None else str(sell.pre_atomic),
                  "post_atomic": None if sell.post_atomic is None else str(sell.post_atomic),
                  "source_balance_slot": rec.get("source_balance_slot"),
                  "source_remaining_atomic": (str(rec["source_remaining_atomic"])
                                              if "source_remaining_atomic" in rec else None),
                  "share": None if share is None else round(share, 6), "share_basis": basis,
                  "cumulative": None if cumulative is None else round(cumulative, 6),
                  "min_fraction": self.min_fraction,
                  "sells": self._sells.get(pos["id"], []), "seen_at": sell.seen_at,
                  "decision_at": decision_at, "recv_to_decision_ms": rec.get("recv_to_decision_ms"),
                  "lock_wait_ms": rec.get("lock_wait_ms"), "exit_status": rec.get("exit_status"),
                  "shadow": self.shadow}
        m.record_exit_trigger_audit(
            conn, position_id=pos["id"], reason=REASON, display_price=None, preview_usdc=None,
            allowed=allowed, detail=json.dumps(detail, sort_keys=True), checked_at=decision_at,
            lane=LANE, key=decision_at)   # every decision its own row: a HOLD then a MIRROR in one
                                          # minute used to leave only the MIRROR


def _first_sell_policy(pos):
    """True when the position's frozen contract is the V4 trial (first-sell mirror). A contract that
    cannot be read keeps today's size rule."""
    try:
        from sniper.services.memecoin_funded_policy import frozen_policy_from_position, is_exit_trial_v4
        frozen = frozen_policy_from_position(pos)
    except Exception:
        return False
    return frozen is not None and is_exit_trial_v4(frozen)


def record_line(rec):
    """One runner log line per decided sell."""
    return ("[memecoin] source-sell mirror %s %s pos=%s path=%s sig=%s slot=%s share=%s cum=%s "
            "basis=%s recv_to_decision_ms=%s%s"
            % (rec.get("outcome"), rec.get("handle") or rec.get("uid"),
               rec.get("position_id"), rec.get("path"), str(rec.get("sig"))[:12], rec.get("slot"),
               rec.get("share"), rec.get("cumulative"), rec.get("basis"),
               rec.get("recv_to_decision_ms"),
               (" exit=%s" % rec["exit_status"]) if rec.get("exit_status") else "")
            + (" retry_in_s=%s" % rec["retry_in_s"] if rec.get("retry_in_s") else "")
            + (" handed_to_rails" if rec.get("handed_to_rails") else "")
            + (" first=%s lag_ms=%s" % (rec["first_path"], rec.get("lag_behind_first_ms"))
               if rec.get("first_path") else ""))
