"""J7Tracker wallet-tracker socket as the funded runner's signal source.

WHY. fomo refused our third account on 2026-09-09. J7Tracker relays fomo trades for up to 50
tracked fomo users per account, over a Socket.IO service separate from its tweet
feed: wss://nj.j7tracker.io/wallets/socket.io/. Measured the same day from the recorded tap
(scripts/j7_socket_tap.py): fomo -> J7 181-243 ms, fomo -> our process ~300 ms. The HTTP poll it
replaces ran on a 5 s loop with a 45 s global-feed period.

WHAT ARRIVES. One `fomo_event` per relayed action:

    {"type": "FOMO_EVENT", "kind": "trade", "received_at": <J7 server ms>,
     "data": {"id": "swap:raybot:0x..:0x..:0x..", "side": "buy"|"sell",
              "userId": <fomo user id == memecoin_wallets.source_user_id>, "userHandle": ...,
              "usdAmount": 496.7, "price": 0.00101, "marketCap": 968114.9,
              "timestamp": "2026-09-09T15:27:25.343Z",
              "token": {"address": "0x..", "networkId": 4663, "symbol": "MOO", ...}}}

`tracked_fomo` arrives once per connect with the tracked roster (fomo_user_id + handle).

WHAT THIS MODULE DOES. `fomo_event_to_feed_item` turns one relay into the fomo /feed item shape
that `sniper.feeds.fomo_feed.normalize_feed_item` already consumes, so the copy path downstream of
the fetch is byte-identical to the fomo era. `J7WalletsFeed` holds the socket on a daemon thread
and hands the runner a drained batch per cycle. Nothing here opens a position.

WHAT IT DOES NOT COVER. J7 relays only the tracked roster (50 slots). The fomo global feed's
discovery of wallets we do not follow is gone with this source; the funded runner never copied
those anyway.

Pump has a separate roster. Its trade adapter requires a Solana wallet, mint, signature,
aware source timestamp, and positive USD price and size. Callouts never become trade signals.
Pump wallet IDs use `pump:<address>`. Existing chain approval and entry controls remain authoritative.
"""
from __future__ import annotations

import os
import hashlib
import json
import math
import uuid
import collections
import queue
import re
import threading
import time
from datetime import datetime
from typing import Optional

WALLETS_URL = "https://nj.j7tracker.io"
WALLETS_PATH = "wallets/socket.io"
# J7's edge refuses handshakes that do not look like its web app.
HEADERS = {
    "Origin": "https://j7tracker.io",
    "Referer": "https://j7tracker.io/",
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/140.0.0.0 Safari/537.36"),
}
_SIDE_TO_TYPE = {"buy": "large_buy", "sell": "large_sell"}   # the fomo feed types _BUY/_SELL know
_TX_ID_KEYS = ("tx_sig", "signature", "tx_hash", "txHash", "transactionHash", "tx")
STATUS_FIELDS = ("status", "connected_at", "last_event_at", "last_error_at", "reconnect_count",
                 "events_total", "tracked_count", "queue_len", "dropped",
                 "receipt_dropped", "receipt_write_failures", "receipt_failed_rows",
                 "receipt_written", "receipt_pending", "receipt_writer_active",
                 "pump_tracked_count", "pump_events_total", "pump_trades_total", "accounts",
                 "auth_refresh")


# The id a tracked entry might carry, in order. J7 has never sent us a NON-EMPTY tracked_pump
# frame we could record, so the pump key is not known from a capture — try every plausible one and
# report loudly when none of them matched, rather than guessing a single name and silently
# dropping the list (which is exactly what happened on 2026-09-22, see `_tracked_rows`).
_FOMO_ID_KEYS = ("fomo_user_id", "user_id", "id")
_PUMP_ID_KEYS = ("pump_user_id", "user_id", "wallet", "address", "id", "fomo_user_id")


def _pump_roster_id(value):
    """The roster's id for a pump wallet: `pump:<address>`, never a bare address.

    `pump_event_to_feed_item` writes `userId = f"pump:{wallet}"`, so a tracked entry that carries
    the bare wallet matches NO roster row until it is prefixed. An entry that already carries the
    prefix is left alone.
    """
    text = str(value).strip()
    return text if text.startswith("pump:") else "pump:%s" % text


def _tracked_rows(users, *id_keys, normalise=None):
    """The tracked list reduced to what identifies a wallet.

    J7 CAPS EACH ACCOUNT AT 50 WALLETS AND ONLY THOSE CAN EVER SIGNAL, so this list is the whole
    addressable roster.

    THREE OUTCOMES, AND CONFUSING ANY TWO RETIRES LIVE WALLETS:
      None  — no list arrived, or a list arrived whose entries we could not read AT ALL. Unknown.
      []    — a real empty list. A live `tracked_pump` frame does carry `pump_users: []`.
      [...] — the parsed list.

    THE 2026-09-22 DEFECT IS THE THIRD CASE COLLAPSING INTO THE SECOND. This function was given one
    guessed pump key, matched nothing in a 9-entry frame, and returned `[]` — "J7 tracks nobody on
    pump" — while the count beside it said 9. The prune then read 7 ACTIVE pump wallets as
    untracked, one with 202 events in 7 days and two funded closes. A parse failure must be
    UNKNOWN, never zero.
    """
    if not isinstance(users, list):
        return None
    rows = []
    for user in users:
        if not isinstance(user, dict):
            continue
        uid = next((str(user[k]) for k in id_keys if user.get(k)), None)
        if not uid:
            continue
        rows.append({"source_user_id": (normalise(uid) if normalise else uid),
                     "handle": (user.get("handle") or user.get("username") or None),
                     "display_name": (user.get("display_name")
                                      or user.get("displayName") or None),
                     "added_at": (user.get("added_at") or None)})
    if users and not rows:
        return None                        # entries arrived and NONE parsed: unknown, not empty
    return rows


def _now_iso():
    from sniper.util.clock import real_utc_now_iso
    return real_utc_now_iso()


def tracked_frames(status):
    """(account_index, kind, rows, seen_at, reported_count) for every list that has a frame.

    The stamp is the frame's ARRIVAL, read from the feed status; a list with no stamp in this
    process is a cache and is not yielded, so nothing downstream can mistake it for evidence
    (Codex review 2026-09-23, A3). `rows` may be None (a frame that parsed to nothing) and the
    persister records that as unreadable, never as empty.
    """
    for index, account in enumerate((status or {}).get("accounts") or []):
        for kind, users_key, count_key, seen_key in (
                ("fomo", "tracked_users", "tracked_count", "tracked_seen_at"),
                ("pump", "pump_tracked_users", "pump_tracked_count", "pump_tracked_seen_at")):
            seen = account.get(seen_key)
            if not seen:
                continue
            yield index, kind, account.get(users_key), seen, account.get(count_key)


def expected_lists(status):
    """(account_index, kind, has_frame) for EVERY configured account and both kinds.

    `tracked_frames` yields only lists that carry a frame, so an account that has never delivered
    one produced no row anywhere and completeness had nothing to compare: account 0 fresh and
    complete while account 1 stayed silent read as "every list agrees", and account 1's wallets
    were nominated for pruning (Codex recheck 2026-09-23, A3).

    The inventory is knowable without a socket. `J7AccountsFeed.status()` builds `accounts` from
    its own feed list, one entry per configured token, whether or not that socket has ever spoken,
    so the expected set is exactly those accounts times {fomo, pump}. `account_index` is POSITIONAL
    -- reordering the tokens in `.j7.env` renumbers the accounts.
    """
    for index, account in enumerate((status or {}).get("accounts") or []):
        for kind, seen_key in (("fomo", "tracked_seen_at"), ("pump", "pump_tracked_seen_at")):
            yield index, kind, bool((account or {}).get(seen_key))


def load_j7_env(path) -> dict:
    """KEY=value lines; quotes stripped; missing file -> {}. Values never leave this process."""
    env = {}
    try:
        with open(path) as fh:
            for line in fh:
                m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
                if m:
                    env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def configured_tokens():
    from sniper.util.state_paths import state_path
    env = load_j7_env(os.getenv("J7_ENV_FILE") or state_path(".j7.env"))
    token = env.get("J7_TOKEN") or os.getenv("J7_TOKEN")
    if not token:
        raise RuntimeError("MEMECOIN_SIGNAL_SOURCE=j7 but no J7_TOKEN in .j7.env")
    second = env.get("J7_TOKEN_2") or os.getenv("J7_TOKEN_2")
    if second == token:
        raise RuntimeError("J7_TOKEN_2 duplicates J7_TOKEN")
    return [token, second] if second else [token]


def token_exp(token):
    """The JWT `exp` claim as epoch seconds, decoded locally, or None when it cannot be read.

    ⚠️ ONLY THE NUMBER LEAVES THIS FUNCTION. It never logs, raises with, or returns any part of the
    token: every failure -- not three segments, bad base64, bad JSON, no `exp`, a non-numeric
    `exp` -- is the same None, and the caller pages "expiry unknown" rather than guessing. The
    signature is not verified; this reads a deadline, it does not authenticate anything.
    """
    try:
        import base64
        parts = str(token).split(".")
        if len(parts) != 3:
            return None
        body = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(body.encode("ascii"))).get("exp")
        exp = float(exp)
        return exp if math.isfinite(exp) and exp > 0 else None
    except Exception:
        return None


def _redacted(text, token):
    """An error string safe to keep: the token removed wherever it appears, then truncated."""
    text = str(text)
    if token:
        text = text.replace(str(token), "<token>")
    return text[:200]


def account_fingerprint(token):
    """Which J7 account a tracked list belongs to, as the first 16 hex chars of sha256(token).

    64 bits distinguishes the handful of accounts we configure; the full digest would be a stable
    fingerprint of the credential itself, readable by anyone who can read the ledger (Alpha's
    ruling, 2026-09-24). It is stored in `j7_tracked_meta` and written to no log or trace.
    """
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16]


def fomo_event_to_feed_item(evt: dict) -> Optional[dict]:
    """One J7 `fomo_event` -> one fomo /feed-shaped item, or None when it is not a trade.

    Every key here is read by `normalize_feed_item` / `normalize_feed_observation`. `id` is J7's
    swap id, stable across re-delivery, so the events table's UNIQUE index dedups a replay.
    `isFirstBuy` is not relayed; it stays absent (False downstream) rather than guessed.
    """
    if not isinstance(evt, dict) or evt.get("kind") != "trade":
        return None
    d = evt.get("data")
    if not isinstance(d, dict):
        return None
    side = str(d.get("side") or "").lower()
    ftype = _SIDE_TO_TYPE.get(side)
    tok = d.get("token") if isinstance(d.get("token"), dict) else {}
    mint = tok.get("address")
    if ftype is None or not mint or not d.get("userId"):
        return None
    network = tok.get("networkId")
    network = str(network) if network not in (None, "") else ""
    body = {
        "userId": str(d.get("userId")),
        "userHandle": d.get("userHandle") or "",
        "displayName": d.get("displayName") or "",
        "ticker": tok.get("symbol") or "",
        "price": d.get("price"),
        "marketCap": d.get("marketCap"),
        # NOT "currentSizeUsd" (2026-09-15, Beta/Alpha): J7 relays only the TRADE size, never a
        # true remaining-position figure. `currentSizeUsd` is also the key fomo's own /swaps-poll
        # responses use for the trader's genuine remaining holding
        # (memecoin_holder_evidence.reconcile_follow_poll), and db_memecoin._explicit_remaining_usd
        # reads that key with no way to tell the two sources apart -- every J7 sell was misread as
        # "source still holds $<this trade's own size>", which reaches the live mirror-on-sell gate
        # (source_remaining_usd_for_event / active_armed_holders_for_coin). "tradeSizeUsd" is read
        # by normalize_feed_item/normalize_feed_observation in fomo_feed.py for their_size_usd
        # (unaffected) and by nothing that reads it as a holding.
        "tradeSizeUsd": d.get("usdAmount"),
        "networkId": network,
        "j7_received_at": evt.get("received_at"),
        "j7_source": d.get("source"),
    }
    return {
        "id": str(d.get("id") or ""),
        "type": ftype,
        "userId": str(d.get("userId")),
        "networkId": network,
        "tokenAddress": str(mint),
        "createdAt": str(d.get("timestamp") or ""),
        "body": body,
        # THE ARMED-ROSTER LANE, NOT THE TELESCOPE. The funded gate refuses signal_source
        # 'global_feed' (MEMECOIN_REAL_GLOBAL_FEED_ENTRIES=0: -$79.9 over 16 fills vs +$942.8 from
        # the armed lane, 2026-09-06) and the first two J7 relays were refused exactly so at
        # 15:45Z on 2026-09-09. J7 relays only wallets we track, which is what follow_activity
        # meant when fomo's WS delivered it, so it carries that source and the 60 s freshness cap,
        # cohort-start check, and follow-real gate that come with it. delivery_lane keeps the
        # transport distinguishable ('websocket' / 'rest' were fomo's) for per-lane measurement.
        "signal_source": "follow_activity",
        "delivery_lane": "j7_wallets",
    }


def pump_event_to_feed_item(evt: dict) -> Optional[dict]:
    """Convert a Pump trade to the shared event format. Callouts cannot produce trades."""
    if not isinstance(evt, dict) or evt.get("type") != "PUMP_EVENT" or evt.get("kind") != "trade":
        return None
    data = evt.get("data")
    if not isinstance(data, dict):
        return None
    author, token = data.get("author"), data.get("token")
    if not isinstance(author, dict) or not isinstance(token, dict):
        return None
    side = str(data.get("side") or "").lower()
    wallet, mint, signature = author.get("wallet"), token.get("address"), data.get("tx")
    if side not in _SIDE_TO_TYPE or str(token.get("networkId")) not in {"solana", "1399811149"}:
        return None
    from sniper.services.memecoin_links import _public_id
    if not all(isinstance(value, str) and _public_id(value, "solana", size)
               for value, size in ((wallet, 32), (mint, 32), (signature, 64))):
        return None
    try:
        timestamp = data["timestamp"]
        if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).utcoffset() is None:
            return None
        for key in ("priceUsd", "usdAmount"):
            value = data[key]
            if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
                return None
        market_cap = data.get("marketCapUsd")
        if market_cap is not None and (isinstance(market_cap, bool)
                or not math.isfinite(float(market_cap)) or float(market_cap) < 0):
            return None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None
    # One transaction can contain multiple wallet, token, or side observations.
    event_id = f"pump:{signature}:{wallet}:{side}:{mint}"
    item = fomo_event_to_feed_item({
        "kind": "trade", "received_at": evt.get("received_at"),
        "data": {"id": event_id, "side": side, "userId": f"pump:{wallet}",
                 "userHandle": author.get("username") or author.get("displayName") or wallet,
                 "displayName": author.get("displayName"), "usdAmount": data["usdAmount"],
                 "price": data["priceUsd"], "marketCap": data.get("marketCapUsd"),
                 "timestamp": timestamp, "source": "pump.fun", "token": token}})
    item["delivery_lane"] = "j7_pump"
    item["body"].update(source_wallet=wallet, source_transaction=signature, source_platform="pump.fun")
    return item


def _receipt_envelope_key(evt, platform):
    """Cheap duplicate identity only; the full adapters still own trade admission."""
    if not isinstance(evt, dict) or evt.get("kind") != "trade":
        return None
    data = evt.get("data")
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    if not isinstance(token, dict):
        return None
    side, mint = str(data.get("side") or "").lower(), token.get("address")
    network = token.get("networkId")
    network = str(network) if network not in (None, "") else ""
    tx = next((data[key] for key in _TX_ID_KEYS
               if isinstance(data.get(key), str) and data[key].strip()), None)
    user, event_id = data.get("userId"), str(data.get("id") or "")
    if platform == "pump":
        author = data.get("author")
        wallet = author.get("wallet") if isinstance(author, dict) else None
        user = f"pump:{wallet}" if wallet else None
        event_id = f"pump:{data.get('tx')}:{wallet}:{side}:{mint}"
    if side not in _SIDE_TO_TYPE or not user or not mint:
        return None
    exact = (network, str(user), str(mint), side, tx)
    return exact if tx else ("j7_event", *exact[:-1], event_id) if event_id else None


def _earlier_receipt_unresolved(snapshot, key, anchor):
    pending, overflow = snapshot
    return bool(overflow or any(mono < anchor["monotonic_ns"] and (source is None or source == key)
                                for mono, source in pending))


class J7WalletsFeed:
    """Background Socket.IO client on the J7 wallet-tracker service. `drain()` per runner cycle."""

    def __init__(self, token, *, maxsize=2048, log_fn=None, receipt_buffer=4096, session=None):
        if not token:
            raise ValueError("J7 token required")
        self._token = token
        self._session = session
        self._pending_token = None
        self._auth_refresh_error = None
        self._auth_wake = threading.Event()
        self._auth_thread = None
        self._q = queue.Queue(maxsize=maxsize)
        # THE LOCAL ARRIVAL CLOCK. J7's `received_at` is ITS server's clock; comparing two feeds
        # with it measures their clocks, not their speed. monotonic_ns cannot go backwards and is
        # the only stamp an arrival comparison may use.
        self._mono_fn = time.monotonic_ns
        if int(receipt_buffer) < 1:
            raise ValueError("receipt_buffer must be positive")
        self._receipts = collections.deque(maxlen=int(receipt_buffer))
        from sniper.util.receipt_clock import clock_domain
        self._clock_domain = clock_domain()
        self._capture_run_id = uuid.uuid4().hex
        from sniper.util.receipt_clock import ReceiptClock
        self._receipt_clock = ReceiptClock("j7")
        self._receipt_anchors = self._receipt_clock
        self._receipt_unresolved = {}
        self._receipt_unresolved_cap = int(receipt_buffer)
        self._receipt_unresolved_overflow = 0
        self._receipt_entering = collections.deque(maxlen=int(receipt_buffer))
        self._receipt_prelock_overflow = False
        # Immutable snapshots let the account drain read before looking up completed anchors.
        self._receipt_unresolved_snapshot = ((), 0)
        self._receipt_thread = None
        self._receipt_writer = None
        self._wake = threading.Event()      # set per queued relay; see wait()
        self._log = log_fn or (lambda s: None)
        self._lock = threading.Lock()
        self._state = {"status": "starting", "connected_at": None, "last_event_at": None,
                       "last_error_at": None, "last_error": None,
                       "reconnect_count": 0, "events_total": 0,
                       "tracked_count": None, "queue_len": 0, "dropped": 0,
                       "receipt_dropped": 0, "receipt_write_failures": 0,
                       "receipt_failed_rows": 0, "receipt_written": 0,
                       "receipt_writer_active": False, "pump_tracked_count": None,
                       "receipt_ingress_pending": 0, "receipt_ingress_overflow": 0,
                       "pump_events_total": 0, "pump_trades_total": 0,
                       # The tracked lists themselves, not just their length. None until a
                       # tracked_* frame arrives on this account; [] is a real empty list.
                       "tracked_users": None, "pump_tracked_users": None,
                       # When each list FRAME arrived on this account. The heartbeat persists
                       # the lists with this stamp, never with its own clock, so a cached list
                       # on a reconnecting account cannot read as fresh (Codex review
                       # 2026-09-23, A3). None until a frame arrives in this process.
                       "tracked_seen_at": None, "pump_tracked_seen_at": None}
        self._last_server_epoch = None
        self._thread = None

    # -- status ------------------------------------------------------------------------------
    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    def status(self) -> dict:
        with self._lock:
            s = dict(self._state)
            s["account_fingerprint"] = account_fingerprint(self._token)
            s["receipt_pending"] = len(self._receipts)
            s["receipt_ingress_pending"] += len(self._receipt_entering)
            refresh_error = self._auth_refresh_error
        s["queue_len"] = self._q.qsize()
        s["auth_refresh"] = (self._session.status() if self._session is not None
                             else {"enabled": False, "status": "manual"})
        if refresh_error:
            s["auth_refresh"] = dict(s["auth_refresh"], status="worker_error", last_error=refresh_error)
        return s

    def account_fingerprints(self):
        return [account_fingerprint(self._token)]

    def token_expiries(self):
        """[(account_index, exp_epoch_or_None)] -- the deadline only, never the token."""
        return [(1, token_exp(self._token))]

    def _refresh_auth(self, sio):
        """Only the auth daemon does I/O; the socket owner activates a durable token."""
        if self._session is None or not self._session.enabled:
            return
        try:
            with self._lock:
                force = self._state["status"] == "auth_error"
            token = self._session.refresh_if_due(force=force)
            with self._lock:
                self._auth_refresh_error = None
                if token and token != self._token:
                    self._pending_token = token
                pending = self._pending_token is not None
                if pending:
                    self._auth_wake.set()
            if pending:
                # End an old-token reconnect owner even when its transport is disconnected.
                sio.shutdown()
        except Exception as exc:  # credential or HTTP exception text may contain secrets
            with self._lock:
                self._auth_refresh_error = type(exc).__name__
            self._log("[j7] session renewal failed (%s)" % type(exc).__name__)

    # -- socket ------------------------------------------------------------------------------
    def start(self):
        import socketio
        feed = self

        def _socket(token):
            # Socket.IO dispatches callbacks on unjoined threads. Each credential needs its
            # own client so late callbacks can still identify their immutable owner.
            sio = socketio.Client(reconnection=True, reconnection_delay=5, reconnection_delay_max=120,
                                  logger=False, engineio_logger=False)
            auth_state = {"rejected": False}

            def _set_current(**kw):
                with feed._lock:
                    if token != feed._token:
                        return False
                    feed._state.update(kw)
                    return True

            @sio.event
            def connect():
                auth_state["rejected"] = False
                connected_at = _now_iso()
                with feed._lock:
                    if token != feed._token:
                        return
                    feed._state.update(status="connected", connected_at=connected_at,
                                       last_error=None, last_error_at=None)
                    pending = feed._pending_token is not None
                feed._log("[j7] wallets socket connected sid=%s" % sio.sid)
                if pending:
                    # A transport retry can race the daemon's disconnect request.
                    sio.disconnect()

            @sio.event
            def connect_error(data):
                message = data.get("message") if isinstance(data, dict) else data
                auth_state["rejected"] = str(message).strip().casefold() == "invalid token"
                if _set_current(status="auth_error" if auth_state["rejected"] else "connect_error",
                                last_error_at=_now_iso(), last_error=_redacted(data, token)):
                    feed._log("[j7] wallets connect_error %s" % _redacted(data, token))

            @sio.event
            def disconnect():
                with feed._lock:
                    if token != feed._token:
                        return
                    feed._state["status"] = "reconnecting"
                    feed._state["reconnect_count"] += 1
                feed._log("[j7] wallets socket disconnected")

            @sio.on("auth_error")
            def on_auth_error(data=None):
                auth_state["rejected"] = True
                if _set_current(status="auth_error", last_error_at=_now_iso(),
                                last_error=_redacted(data, token)):
                    feed._log("[j7] AUTH_ERROR %s -- re-export J7_TOKEN" % _redacted(data, token))

            @sio.on("tracked_fomo")
            def on_tracked(data=None):
                users = (data or {}).get("fomo_users") if isinstance(data, dict) else None
                _set_current(tracked_count=len(users) if isinstance(users, list) else None,
                             tracked_users=_tracked_rows(users, *_FOMO_ID_KEYS),
                             tracked_seen_at=_now_iso())

            @sio.on("fomo_event")
            def on_fomo_event(data=None):
                if token == feed._token:
                    feed.observe(data)

            @sio.on("tracked_pump")
            def on_tracked_pump(data=None):
                users = (data or {}).get("pump_users") if isinstance(data, dict) else None
                rows = _tracked_rows(users, *_PUMP_ID_KEYS, normalise=_pump_roster_id)
                current = _set_current(pump_tracked_count=len(users) if isinstance(users, list) else None,
                                       pump_tracked_users=rows, pump_tracked_seen_at=_now_iso())
                if current and isinstance(users, list) and users and rows is None:
                    # A real list with no readable IDs is unknown, never an empty roster.
                    feed._log("[j7] tracked_pump: %d entries, NO id key matched %s; keys seen: %s"
                              % (len(users), list(_PUMP_ID_KEYS),
                                 sorted({k for u in users if isinstance(u, dict) for k in u})))

            @sio.on("pump_event")
            def on_pump_event(data=None):
                if token == feed._token:
                    feed.observe(data, platform="pump")

            return sio, auth_state

        sio, auth_state = _socket(feed._token)
        self._sio = sio

        def _run():
            nonlocal sio, auth_state
            while True:
                rotated = False
                with feed._lock:
                    if feed._pending_token is not None:
                        # Old transport has stopped; clear its roster with its identity.
                        feed._token = feed._pending_token
                        feed._pending_token = None
                        feed._auth_wake.clear()
                        feed._state.update(status="reconnecting", connected_at=None,
                                           last_event_at=None, last_error=None, last_error_at=None,
                                           tracked_users=None, tracked_count=None, tracked_seen_at=None,
                                           pump_tracked_users=None, pump_tracked_count=None,
                                           pump_tracked_seen_at=None)
                        rotated = True
                if rotated:
                    sio, auth_state = _socket(feed._token)
                    feed._sio = sio
                auth_state["rejected"] = False
                try:
                    sio.connect(WALLETS_URL, socketio_path=WALLETS_PATH, transports=["websocket"],
                                headers=HEADERS, auth=lambda: {"token": feed._token}, wait_timeout=20)
                    sio.wait()          # returns when the client gives up reconnecting
                except Exception as exc:  # noqa: BLE001 - a feed thread must never die silently
                    # Socket.IO's namespace exception must not erase this attempt's auth rejection.
                    if not auth_state["rejected"]:
                        feed._set(status="connect_failed", last_error_at=_now_iso(),
                                  last_error=_redacted(exc, feed._token))
                    feed._log("[j7] wallets connect failed: %s" % _redacted(exc, feed._token))
                if feed._session is not None and feed._session.enabled:
                    feed._auth_wake.wait(15)
                else:
                    time.sleep(15)

        self._thread = threading.Thread(target=_run, name="memecoin-j7-wallets", daemon=True)
        self._thread.start()
        if self._session is not None and self._session.enabled:
            def _renew():
                while True:
                    feed._refresh_auth(feed._sio)
                    time.sleep(60)

            self._auth_thread = threading.Thread(target=_renew, name="memecoin-j7-auth", daemon=True)
            self._auth_thread.start()
        return self

    # -- receipt ------------------------------------------------------------------------------
    def observe(self, data, *, platform="fomo"):
        """One relay: stamp its LOCAL arrival first, then do everything the handler used to do."""
        pending = object()
        # One atomic, bounded, feed-owned append BEFORE capture makes a callback waiting
        # for the existing first lock visible. No captured T0 can precede its marker.
        self._receipt_entering.append(pending)
        mono = self._mono_fn()
        # Reuse the first callback lock BEFORE correction/parsing. Keep the pre-lock stamp;
        # waiting for this existing lock must never redefine the original receipt.
        with self._lock:
            self._mark_prelock_saturation()
            self._state["events_total"] += 1
            if platform == "pump":
                self._state["pump_events_total"] += 1
            retained = len(self._receipt_unresolved) < self._receipt_unresolved_cap
            if retained:
                self._receipt_unresolved[pending] = (mono, _receipt_envelope_key(data, platform))
            else:
                self._receipt_unresolved_overflow += 1
                self._state["receipt_ingress_overflow"] += 1
            self._publish_unresolved()
            try:
                self._receipt_entering.remove(pending)
            except ValueError:
                # A bounded-ring eviction loses duplicate identity, so remain unknown for
                # this process rather than later invent a trustworthy first receipt.
                self._mark_prelock_overflow()
        item, received_at, identity = None, None, None
        ra = (data or {}).get("received_at") if isinstance(data, dict) else None
        # LEARN THE CLOCK HERE, AT RECEIPT (2026-09-09). `received_at` is J7's server clock in
        # real UTC, stamped ~300 ms before this handler runs. The first wiring handed it to the
        # runner at drain time on EVERY cycle, so between relays the "trusted real UTC" was the
        # LAST relay's stamp and the corrected clock aged with the lull: 21 s behind at 20:54Z,
        # and every ledger stamp with it. fomo's Date header never had this problem because it
        # arrived with each poll.
        try:
            received_at = _now_iso()
            receipt_clock = self._receipt_clock.capture(monotonic_ns=mono, corrected_utc=received_at)
            item = pump_event_to_feed_item(data) if platform == "pump" else fomo_event_to_feed_item(data)
            if isinstance(ra, (int, float)) and ra > 0:
                try:
                    from sniper.util.clock import record_real_utc
                    record_real_utc(float(ra) / 1000.0)
                except Exception:  # noqa: BLE001 - a clock hint must never cost a signal
                    pass
            if item is not None:
                identity = {"source_user_id": item["userId"], "chain": item["networkId"],
                            "mint": item["tokenAddress"], "side": data["data"]["side"].lower()}
                if data["data"].get("usdAmount") is not None:
                    identity["amount_usd"] = data["data"]["usdAmount"]
                for key in _TX_ID_KEYS:
                    if isinstance(data["data"].get(key), str) and data["data"][key].strip():
                        identity["tx_sig"] = data["data"][key]
                        break
        finally:
            # Existing second callback lock owns completed anchors, queue publication and
            # pending cleanup together. A cleared snapshot therefore cannot hide an anchor
            # that has not yet been made available to the drain.
            with self._lock:
                if received_at is not None:
                    self._state["last_event_at"] = received_at
                if isinstance(ra, (int, float)) and ra > 0:
                    self._last_server_epoch = float(ra) / 1000.0
                if platform == "pump":
                    self._state["pump_trades_total"] += int(item is not None)
                try:
                    if item is not None and identity is not None:
                        tx = identity.get("tx_sig")
                        exact = (item["networkId"], item["userId"], item["tokenAddress"], identity["side"], tx)
                        key = exact if tx else ("j7_event", *exact[:-1], item["id"])
                        item["received_mono_ns"] = mono
                        item["_receipt_key"] = key if tx or item["id"] else None
                        item["receipt_clock"] = (self._receipt_anchors.remember(key, receipt_clock)
                                                 if tx or item["id"] else receipt_clock)
                        item["_receipt_anchor"] = item["receipt_clock"]
                        item["_receipt_identity_status"] = ("exact_source_transaction" if tx
                                                            else "source_identity_unknown")
                        item["receipt_identity_status"] = item["_receipt_identity_status"]
                        self._state["receipt_anchor_evicted"] = self._receipt_anchors.evicted
                        if len(self._receipts) == self._receipts.maxlen:
                            self._state["receipt_dropped"] += 1
                        self._receipts.append({"fomo_event_id": item["id"], "received_mono_ns": mono,
                                               "received_at": received_at, "clock_domain": self._clock_domain,
                                               "capture_run_id": self._capture_run_id,
                                               "identity_json": json.dumps(identity, sort_keys=True)})
                        try:
                            self._q.put_nowait(item)
                        except queue.Full:
                            self._state["dropped"] += 1
                finally:
                    if retained:
                        self._receipt_unresolved.pop(pending, None)
                    else:
                        self._receipt_unresolved_overflow -= 1
                    self._publish_unresolved()
        if item is not None:
            self._wake.set()

    def _publish_unresolved(self):
        """Called only under the existing callback lock; no I/O or extra acquisition."""
        self._receipt_unresolved_snapshot = (tuple(self._receipt_unresolved.values()),
                                             self._receipt_unresolved_overflow)
        self._state["receipt_ingress_pending"] = (len(self._receipt_unresolved)
                                                   + self._receipt_unresolved_overflow)

    def _mark_prelock_overflow(self):
        if not self._receipt_prelock_overflow:
            self._receipt_prelock_overflow = True
            self._state["receipt_ingress_overflow"] += 1

    def _mark_prelock_saturation(self):
        # Conservative at capacity, before any removal can hide a discarded marker.
        if len(self._receipt_entering) >= self._receipt_entering.maxlen:
            self._mark_prelock_overflow()

    def _unresolved_snapshot(self):
        # A first-lock registration publishes pending BEFORE removing its entering marker.
        # Reading in this order cannot miss a callback transitioning between those phases.
        entering = tuple(self._receipt_entering)
        pending, overflow = self._receipt_unresolved_snapshot
        return pending, bool(overflow or entering or self._receipt_prelock_overflow)

    def take_receipts(self):
        """Every buffered arrival, removed from the buffer. For a consumer that owns a handle."""
        with self._lock:
            rows = list(self._receipts)
            self._receipts.clear()
        return rows

    def flush_receipts(self):
        """Start one bounded background write. Return without a database wait."""
        with self._lock:
            if self._state["receipt_writer_active"] or not self._receipts:
                return 0
            self._state["receipt_writer_active"] = True
        def write():
            rows = self.take_receipts()
            try:
                count = (self._receipt_writer or _default_receipt_writer)(rows)
                with self._lock:
                    self._state["receipt_written"] += count
            except Exception as exc:
                with self._lock:
                    self._state["receipt_write_failures"] += 1
                    self._state["receipt_failed_rows"] += len(rows)
                self._log("[j7] receipt flush failed: %s" % str(exc)[:200])
            finally:
                self._set(receipt_writer_active=False)
        self._receipt_thread = threading.Thread(target=write, name="j7-receipts", daemon=True)
        try:
            self._receipt_thread.start()
        except Exception:
            self._set(receipt_writer_active=False)
            raise
        return 0

    # -- consumer ----------------------------------------------------------------------------
    def wait(self, timeout):
        """Sleep up to `timeout` seconds, or until a relay is queued. True when a relay woke it.

        WHY (2026-09-14, operator: "sometimes fast sometimes slow"). The runner drained this queue
        once per main-loop cycle, and the watchdog paces that loop at 5 s for fomo's REST rate
        limit, which the J7 source never calls. J7 delivers in about 0.3 s, but a relay then waited
        0-5 s for the next drain: 3.0 s median, 5.1 s p90, 12.7 s p99 on 1,657 Solana signals
        (09-11..09-14). Waking on arrival keeps the idle cadence and removes that wait. A relay
        queued between a drain and this call leaves the event set, so it is never missed."""
        woke = self._wake.wait(max(0.0, float(timeout)))
        self._wake.clear()
        return woke

    def drain(self):
        """All queued feed items, and J7's latest server epoch (real UTC) or None."""
        items = []
        while True:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            self._mark_prelock_saturation()
            epoch = self._last_server_epoch
            snapshot = self._unresolved_snapshot()
            for item in items:
                key = item.get("_receipt_key")
                anchor = item["_receipt_anchor"]
                if key is not None:
                    anchor = self._receipt_anchors.remember(key, anchor)
                unresolved = _earlier_receipt_unresolved(snapshot, key, anchor)
                history_unknown = anchor.get("history_unknown", False)
                item["receipt_clock"] = None if unresolved or history_unknown else anchor
                item["receipt_identity_status"] = ("earlier_receipt_unresolved" if unresolved
                                                   else "receipt_history_unknown" if history_unknown
                                                   else item["_receipt_identity_status"])
        return items, epoch


_RECEIPT_SCHEMA_PATHS = set()


def _default_receipt_writer(rows):
    """Its OWN short connection, off the signal path: never the runner's handle, never a poll."""
    import sqlite3
    from sniper.persistence.db_axiom import ensure_axiom_tables, record_j7_receipts
    from sniper.persistence.db_core import get_db_path
    path = get_db_path()
    conn = sqlite3.connect(path, timeout=0.05)
    try:
        if path not in _RECEIPT_SCHEMA_PATHS:
            ensure_axiom_tables(conn)
            _RECEIPT_SCHEMA_PATHS.add(path)
        return record_j7_receipts(conn, rows)
    finally:
        conn.close()


class J7AccountsFeed:
    """Two J7 sockets use one wake and one signal batch."""

    def __init__(self, tokens, *, log_fn=None, sessions=None):
        self._wake = threading.Event()
        write_lock = threading.Lock()

        def write(rows):
            with write_lock:
                return _default_receipt_writer(rows)

        self._feeds = []
        from sniper.util.receipt_clock import ReceiptClock
        self._receipt_anchors = ReceiptClock("j7-accounts")
        for index, token in enumerate(tokens, 1):
            logger = (lambda message, n=index: log_fn(f"[j7 account {n}] {message}")) if log_fn else None
            feed = J7WalletsFeed(token, log_fn=logger,
                                 session=sessions[index - 1] if sessions is not None else None)
            feed._wake = self._wake
            feed._receipt_writer = write
            self._feeds.append(feed)

    def start(self):
        for feed in self._feeds:
            feed.start()
        return self

    def drain(self):
        items, epochs = [], []
        for feed in self._feeds:
            batch, epoch = feed.drain()
            if epoch is not None:
                epochs.append(epoch)
            items.extend(batch)
        # This map belongs to the existing single drain consumer, never the socket callbacks.
        # Read pending snapshots BEFORE completed-anchor lookups: a concurrent completion
        # either remains conservatively unknown or has already published its earlier anchor.
        unresolved = [feed._unresolved_snapshot() for feed in self._feeds]
        for item in items:
            key = item.get("_receipt_key")
            anchor = item["_receipt_anchor"]
            if key is not None:
                for feed in self._feeds:
                    anchor = feed._receipt_anchors.known(key, anchor)
                self._receipt_anchors.remember(key, anchor)
        for item in items:
            key = item.get("_receipt_key")
            anchor = item["_receipt_anchor"]
            if key is not None:
                anchor = self._receipt_anchors.known(key, anchor)
            pending = any(_earlier_receipt_unresolved(snapshot, key, anchor) for snapshot in unresolved)
            history_unknown = anchor.get("history_unknown", False)
            item["receipt_clock"] = None if pending or history_unknown else anchor
            item["receipt_identity_status"] = ("earlier_receipt_unresolved" if pending
                                               else "receipt_history_unknown" if history_unknown
                                               else item["_receipt_identity_status"])
        return items, max(epochs) if epochs else None

    def wait(self, timeout):
        woke = self._wake.wait(max(0.0, float(timeout)))
        self._wake.clear()
        return woke

    def flush_receipts(self):
        for feed in self._feeds:
            feed.flush_receipts()
        return 0

    def status(self):
        accounts = [feed.status() for feed in self._feeds]
        connected = sum(account["status"] == "connected" for account in accounts)
        status = "connected" if connected == len(accounts) else "degraded" if connected else accounts[0]["status"]
        out = {"status": status, "accounts": [
            {key: account[key] for key in ("status", "tracked_count", "pump_tracked_count",
                                          "last_error_at", "last_error", "reconnect_count",
                                          "tracked_users", "pump_tracked_users",
                                          "tracked_seen_at", "pump_tracked_seen_at",
                                          "account_fingerprint", "auth_refresh")}
            for account in accounts]}
        for key in ("connected_at", "last_event_at", "last_error_at"):
            out[key] = max((account[key] for account in accounts if account[key]), default=None)
        for key in ("tracked_count", "pump_tracked_count"):
            out[key] = (sum(account[key] for account in accounts)
                        if all(account[key] is not None for account in accounts) else None)
        for key in ("reconnect_count", "events_total", "queue_len", "dropped", "receipt_dropped",
                    "receipt_write_failures", "receipt_failed_rows", "receipt_written", "receipt_pending",
                    "receipt_ingress_pending", "receipt_ingress_overflow",
                    "pump_events_total", "pump_trades_total"):
            out[key] = sum(account[key] for account in accounts)
        out["receipt_writer_active"] = any(account["receipt_writer_active"] for account in accounts)
        out["receipt_anchor_evicted"] = (self._receipt_anchors.evicted
                                        + sum(feed._receipt_anchors.evicted for feed in self._feeds))
        return out

    def account_fingerprints(self):
        return [account_fingerprint(feed._token) for feed in self._feeds]

    def token_expiries(self):
        """[(account_index, exp_epoch_or_None)], positional like `account_index` elsewhere."""
        return [(index, token_exp(feed._token)) for index, feed in enumerate(self._feeds, 1)]


_SINGLETON = None


def j7_feed_or_none(log_fn=None):
    """The process-wide feed when MEMECOIN_SIGNAL_SOURCE=j7, else None. Started on first call.

    Reads `.j7.env` from the STATE root. J7_TOKEN_2 starts a second socket.
    """
    global _SINGLETON
    from sniper.settings.memecoin import MEMECOIN_SIGNAL_SOURCE
    if MEMECOIN_SIGNAL_SOURCE != "j7":
        return None
    if _SINGLETON is None:
        tokens = configured_tokens()
        from sniper.util.state_paths import state_path
        path = os.getenv("J7_ENV_FILE") or state_path(".j7.env")
        env = load_j7_env(path)
        sessions = []
        for index, token in enumerate(tokens, 1):
            suffix = "" if index == 1 else "_%d" % index
            session = None
            if env.get("J7_REFRESH_TOKEN" + suffix):
                from sniper.feeds.j7_auth import J7Session
                session = J7Session(path, "J7_TOKEN" + suffix, token)
            sessions.append(session)
        _SINGLETON = (J7AccountsFeed(tokens, log_fn=log_fn, sessions=sessions)
                      if len(tokens) > 1 else J7WalletsFeed(tokens[0], log_fn=log_fn,
                                                           session=sessions[0])).start()
    return _SINGLETON
