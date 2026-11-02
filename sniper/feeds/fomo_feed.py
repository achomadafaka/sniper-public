"""fomo_feed.py — fomo.family copy-mirror feed adapter.

Layers:
  - CopyEvent + normalize_feed_item/filter_curated : pure normalization
  - FomoAuth + fomo_get                            : Privy auth + WAF-header client
  - FomoFeedAdapter                                : FeedAdapter poll integration

The global feed still polls GET /feed. Armed follow activity also uses Fomo's
authenticated trading-activity WebSocket, with REST polling as recovery. See
docs/superpowers/specs/2026-07-05-memecoin-copy-mirror-design.md.
"""
from __future__ import annotations

import json as _json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from sniper.util.receipt_clock import freeze_receipt_clock
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalization (pure)
# ---------------------------------------------------------------------------

FEED_TYPES = [
    "large_buy", "large_sell", "large_transfer_in", "large_transfer_out",
    "single_user_transfer_out", "single_user_sell", "thesis_created",
    "multi_user_buy", "multi_user_sell", "new_token_listing",
    "price_since_listing", "user_trade_profit_milestone",
    "user_with_smart_following", "manual",
]

_BUY = {"large_buy", "multi_user_buy"}
_SELL = {"large_sell", "single_user_sell", "multi_user_sell"}


@dataclass
class CopyEvent:
    source_user_id: str
    source_handle: str
    mint: str
    ticker: str
    side: str
    their_price: float
    their_market_cap: float
    their_size_usd: float
    is_first_buy: bool
    fomo_event_id: str
    created_at: str
    received_at: str
    lag_s: float
    signal_source: str = "global_feed"   # 'global_feed' (real-time /feed) | 'follow_activity' (per-follow poll)
    network_id: str = ""                  # fomo chain id: 'solana' | '8453' (Base) | '56' (BSC) | ... — routes the real leg
    received_at_normalized: Optional[str] = None
    clock_domain: str = "observed_real_utc"
    delivery_lane: Optional[str] = None
    receipt_clock: Optional[dict] = None
    receipt_identity_status: str = "legacy_unknown"
    receipt_stages: tuple = ()


@dataclass(frozen=True)
class FomoObservation:
    upstream_event_id: str
    raw_type: str
    normalized_side: Optional[str]
    source_user_id: str
    source_handle: str
    network_id: str
    mint: str
    ticker: str
    their_price: Optional[float]
    their_market_cap: Optional[float]
    their_size_usd: Optional[float]
    source_ts_raw: str
    source_ts: Optional[str]
    observed_at: str
    lag_s: Optional[float]
    raw_payload: dict
    event: Optional[CopyEvent]


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _numN(v):
    """float or None — preserves 'unknown' (missing key) as distinct from a known 0.0.
    Used for thesis PnL fields where 0% and 'not reported' mean different things."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_network(nid):
    """fomo emits Solana as numeric id 1399811149 on some feed items and omits/labels it
    elsewhere — the split fragmented 27% of mints across two network labels (2026-07-11
    lead-lag study), corrupting buyer-group joins. One canonical label: 'solana'."""
    s = str(nid or "")
    return "solana" if s == "1399811149" else s


def _parsed_utc_iso(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def normalize_feed_observation(raw: dict, observed_iso: str,
                               server_epoch: Optional[float] = None) -> FomoObservation:
    """Preserve one upstream item before trade-specific normalization or cohort filtering."""
    import hashlib
    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    event_id = str(raw.get("id") or "")
    if not event_id:
        canonical = _json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        event_id = "v2-synth-" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
    source_raw = str(raw.get("createdAt") or "")
    source_iso = _parsed_utc_iso(source_raw)
    observed_parsed = _parsed_utc_iso(observed_iso)
    lag_s = None
    if source_iso and observed_parsed:
        lag_s = (datetime.fromisoformat(observed_parsed)
                 - datetime.fromisoformat(source_iso)).total_seconds()
    event = normalize_feed_item(raw, observed_iso, server_epoch)
    raw_type = str(raw.get("type") or "")
    side = "buy" if raw_type in _BUY else "sell" if raw_type in _SELL else None
    return FomoObservation(
        upstream_event_id=event_id,
        raw_type=raw_type,
        normalized_side=side,
        source_user_id=str(raw.get("userId") or body.get("userId") or ""),
        source_handle=str(body.get("userHandle") or body.get("displayName") or ""),
        network_id=_norm_network(raw.get("networkId") or body.get("networkId")),
        mint=str(raw.get("tokenAddress") or body.get("tokenAddress") or ""),
        ticker=str(body.get("ticker") or ""),
        their_price=_numN(body.get("price")),
        their_market_cap=_numN(body.get("marketCap")),
        # tradeSizeUsd is J7's relayed trade size (sniper/feeds/j7_feed.py) -- kept out of
        # currentSizeUsd so db_memecoin._explicit_remaining_usd cannot misread a trade as a
        # holding; still read here, same as fomo's own currentSizeUsd, for this field only.
        their_size_usd=_numN(body.get("currentSizeUsd") or body.get("tradeSizeUsd")
                             or body.get("inHumanAmount")),
        source_ts_raw=source_raw,
        source_ts=source_iso,
        observed_at=observed_parsed or observed_iso,
        lag_s=lag_s,
        raw_payload=dict(raw),
        event=event,
    )


def normalize_feed_item(raw: dict, received_iso: str, server_epoch: Optional[float]) -> Optional[CopyEvent]:
    t = raw.get("type")
    side = "buy" if t in _BUY else "sell" if t in _SELL else None
    body = raw.get("body")
    mint = raw.get("tokenAddress")
    if side is None or not isinstance(body, dict) or not mint:
        return None
    created = raw.get("createdAt") or ""
    # Stable event id. If the feed omits `id`, synthesize one from the trade's identity
    # so distinct id-less events don't all collide on "" (UNIQUE index) and get silently
    # dropped, while a RE-delivery of the same trade still hashes to the same id (dedup holds).
    eid = str(raw.get("id") or "")
    if not eid:
        import hashlib
        key = "|".join([str(raw.get("userId") or body.get("userId") or ""),
                        str(mint), side, created])
        eid = "synth-" + hashlib.sha1(key.encode()).hexdigest()[:16]
    lag = None
    if server_epoch is not None and created:
        try:
            ce = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            lag = server_epoch - ce
        except ValueError:
            lag = None
    return CopyEvent(
        source_user_id=str(raw.get("userId") or body.get("userId") or ""),
        source_handle=str(body.get("userHandle") or body.get("displayName") or ""),
        mint=str(mint),
        ticker=str(body.get("ticker") or ""),
        side=side,
        their_price=_num(body.get("price")),
        their_market_cap=_num(body.get("marketCap")),
        # tradeSizeUsd is J7's relayed trade size (sniper/feeds/j7_feed.py) -- kept out of
        # currentSizeUsd so db_memecoin._explicit_remaining_usd cannot misread a trade as a
        # holding; still read here, same as fomo's own currentSizeUsd, for this field only.
        their_size_usd=_num(body.get("currentSizeUsd") or body.get("tradeSizeUsd")
                            or body.get("inHumanAmount")),
        is_first_buy=bool(body.get("isFirstBuy")),
        fomo_event_id=eid,
        created_at=created,
        received_at=received_iso,
        lag_s=lag if lag is not None else -1.0,
        # A relay that already knows which lane it is says so (sniper/feeds/j7_feed.py tags the
        # armed roster's trades follow_activity / j7_wallets). fomo /feed items carry neither key
        # and keep the global_feed default byte-for-byte.
        signal_source=str(raw.get("signal_source") or "global_feed"),
        delivery_lane=str(raw.get("delivery_lane") or "global_feed"),
        network_id=_norm_network(raw.get("networkId") or body.get("networkId")),
        received_at_normalized=received_iso,
        clock_domain="observed_real_utc",
        receipt_clock=freeze_receipt_clock(raw.get("receipt_clock")),
        receipt_identity_status=str(raw.get("receipt_identity_status") or "legacy_unknown"),
        receipt_stages=tuple(raw.get("receipt_stages") or ()),
    )


def filter_curated(events, curated):
    return [e for e in events if e.source_user_id in curated]


# ---------------------------------------------------------------------------
# Thesis normalization (pure) — the traders' posted reasoning
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402

_URL_RE = _re.compile(r"https?://", _re.IGNORECASE)


@dataclass
class ThesisEvent:
    """A curated trader's `thesis_created` post. Display/context ONLY — never a copy
    signal (theses skew underwater-cope; the informative unit is text + live PnL)."""
    fomo_thesis_id: str
    comment_id: str
    source_user_id: str
    handle: str
    mint: str
    ticker: str
    comment: str
    has_link: bool
    their_pnl_pct: Optional[float]            # body.percentageUnrealizedPnl — PERCENT
    their_realized_pnl_pct: Optional[float]
    their_notional_usd: Optional[float]       # THEIR position size — never our sizing input
    their_unrealized_pnl_usd: Optional[float]
    market_cap: Optional[float]
    fdv: Optional[float]
    price: Optional[float]
    tag: str
    network_id: str
    swap_id: str
    trade_id: str
    created_at: str                           # fomo clock (host runs +7h)
    received_at: str                          # host clock
    lag_s: float
    signal_source: str = "global_feed"


def normalize_thesis_item(raw: dict, received_iso: str,
                          server_epoch: Optional[float]) -> Optional[ThesisEvent]:
    if raw.get("type") != "thesis_created":
        return None
    body = raw.get("body")
    if not isinstance(body, dict):
        return None
    uid = str(raw.get("userId") or body.get("userId") or "")
    mint = str(raw.get("tokenAddress") or body.get("tokenAddress") or "")
    if not uid or not mint:
        return None
    comment = str(body.get("comment") or "")
    if not comment.strip():
        return None                            # bare position-share, no text -> skip
    created = raw.get("createdAt") or ""
    tid = str(raw.get("id") or "")
    if not tid:
        import hashlib
        key = "|".join([uid, mint, str(body.get("commentId") or ""), created])
        tid = "synth-" + hashlib.sha1(key.encode()).hexdigest()[:16]
    lag = None
    if server_epoch is not None and created:
        try:
            ce = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            lag = server_epoch - ce
        except ValueError:
            lag = None
    return ThesisEvent(
        fomo_thesis_id=tid,
        comment_id=str(body.get("commentId") or ""),
        source_user_id=uid,
        handle=str(body.get("userHandle") or body.get("displayName") or ""),
        mint=mint,
        ticker=str(body.get("ticker") or ""),
        comment=comment,
        has_link=bool(_URL_RE.search(comment)),
        their_pnl_pct=_numN(body.get("percentageUnrealizedPnl")),
        their_realized_pnl_pct=_numN(body.get("percentageRealizedPnl")),
        their_notional_usd=_numN(body.get("positionNotionalUsd")),
        their_unrealized_pnl_usd=_numN(body.get("unrealizedPnlUsd")),
        market_cap=_numN(body.get("marketCap")),
        fdv=_numN(body.get("fdv")),
        price=_numN(body.get("price")),
        tag=str(body.get("tag") or ""),
        network_id=_norm_network(raw.get("networkId") or body.get("networkId")),
        swap_id=str(raw.get("swapId") or ""),
        trade_id=str(raw.get("tradeId") or ""),
        created_at=created,
        received_at=received_iso,
        lag_s=lag if lag is not None else -1.0,
    )


# ---------------------------------------------------------------------------
# Privy auth + WAF-header client
# ---------------------------------------------------------------------------

FOMO_API = "https://prod-api.fomo.family"
# Seconds of remaining access-token life below which a lock-winner's token is NOT reused, so a
# genuinely-due refresh still happens. Above it, a concurrent caller that just waited on the lock
# takes the freshly-minted token instead of posting its own.
_HERD_SUPPRESS_MARGIN_S = 120.0

PRIVY_SESSIONS_URL = "https://auth.privy.io/api/v1/sessions"
PRIVY_APP_ID = "cm6h485o300n3zj9yl6vpedq7"
PRIVY_CLIENT = "react-auth:3.21.3"
PRIVY_CLIENT_ID = "client-WY5gFSayQjxnQhG4rP6SnwPAyPZWZpNRhJ6b9rzMnYwqH"

# fomo's edge (WAF) 403s requests that don't look like the web app.
BROWSER_HEADERS = {
    "accept": "application/json",
    "origin": "https://fomo.family",
    "referer": "https://fomo.family/",
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "x-supported-chains": "1,56,143,8453,1399811149,4663",
}


def _http_json(method, url, headers, body=None, timeout=15):
    data = _json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return r.status, (_json.loads(raw) if raw else {}), r.headers.get("Date")


# Default rotated-token store at repo root (gitignored). Privy ROTATES the
# refresh token on every refresh, so it MUST be persisted or the next process
# start uses a spent token and 403s.
from sniper.util.state_paths import state_path as _state_path  # noqa: E402

_TOKEN_STORE = os.environ.get("FOMO_TOKEN_STORE", str(_state_path(".fomo_tokens.json")))


class PrivyAppTokenUnavailable(RuntimeError):
    """Privy accepted the refresh but minted no fomo-app-scoped `token`.

    UNRECOVERABLE without an operator: `token` is only issued while the presented access token is
    still valid, so once it lapses no amount of retrying will produce one — the session has to be
    re-established from a browser. Classified `auth_dead` so the operator is paged instead of the
    loop spinning on it (2026-08-03: ~20 minutes of feed 401 -> refresh -> retry -> Cloudflare 429
    while the surface still read healthy).
    """


class FomoAuth:
    def __init__(self, access="", refresh="", ca_id="", store_path=None):
        self.access = access or os.environ.get("FOMO_ACCESS_TOKEN", "")
        self.refresh = refresh or os.environ.get("FOMO_REFRESH_TOKEN", "")
        self.ca_id = ca_id or os.environ.get("FOMO_CA_ID", "")
        self.store_path = _TOKEN_STORE if store_path is None else store_path
        # Set by refresh_token: did the last refresh actually move expiry, or did Privy just
        # re-issue the same token? See _do_refresh.
        self.extended = False
        self._load_store()

    def _load_store(self):
        # A persisted rotated token wins over env (it is newer in the chain).
        if not self.store_path or not os.path.exists(self.store_path):
            return
        try:
            d = _json.load(open(self.store_path))
            if d.get("refresh"):
                self.refresh = d["refresh"]
            if d.get("access"):
                self.access = d["access"]
            if d.get("ca_id") and not self.ca_id:
                self.ca_id = d["ca_id"]
        except Exception:
            pass

    def _persist(self):
        if not self.store_path:
            return
        try:
            # atomic: a SIGKILL mid-write must not truncate the store -> next load would fall
            # back to a spent refresh token and die silently on auth.
            from sniper.util.atomic import atomic_write_json
            atomic_write_json(self.store_path,
                              {"refresh": self.refresh, "access": self.access, "ca_id": self.ca_id},
                              mode=0o600)
        except Exception:
            pass

    def refresh_token(self, http=None):
        http = http or _http_json
        # Privy rotates the refresh token single-use. The runner and the web hold
        # separate FomoAuth instances over the same shared store; if one refreshes,
        # the other's in-memory token is spent -> 403 -> silent auth death (the board
        # goes quiet with no error). Serialize refreshes with an exclusive file lock
        # and reload the freshest persisted token before POSTing.
        lockf = None
        if self.store_path:
            try:
                import fcntl
                lockf = open(self.store_path + ".lock", "w")
                fcntl.flock(lockf, fcntl.LOCK_EX)
                self._load_store()          # pick up a token another process just rotated
            except Exception:
                lockf = None
        try:
            # DOUBLE-CHECKED: another caller may have just minted a token while we waited on the
            # lock. `fomo_request` refreshes on EVERY 401 from the fomo API, uncapped, so one
            # expired token turns N concurrent request lanes into N sequential Privy POSTs even
            # though the first one already fixed it. Measured 2026-09-02: 122 rate-limit
            # rejections in the peak hour and 1,565 auth failures across a two-hour storm, against
            # an intended cadence of one refresh per 45 minutes.
            #
            # `_load_store()` above has already reloaded whatever the winner persisted, so if that
            # token now has real life left, the work is done and posting again only spends quota.
            # The margin is deliberately small: this suppresses the herd, never a refresh that is
            # actually due — `refresh_due` in the runner still owns the proactive cadence.
            if self.access:
                try:
                    remaining = self.access_expires_in()
                except Exception:
                    remaining = None
                if remaining is not None and remaining > _HERD_SUPPRESS_MARGIN_S:
                    self.extended = False
                    return self.access
            return self._do_refresh(http)
        finally:
            if lockf is not None:
                try:
                    import fcntl
                    fcntl.flock(lockf, fcntl.LOCK_UN)
                    lockf.close()
                except Exception:
                    pass

    def _do_refresh(self, http):
        # auth.privy.io is Cloudflare-fronted: browser headers required or 403 (error 1010).
        headers = {
            "accept": "application/json", "content-type": "application/json",
            "origin": "https://fomo.family", "referer": "https://fomo.family/",
            "user-agent": BROWSER_HEADERS["user-agent"],
            "privy-app-id": PRIVY_APP_ID, "privy-client": PRIVY_CLIENT,
            "privy-client-id": PRIVY_CLIENT_ID,
        }
        if self.ca_id:
            headers["privy-ca-id"] = self.ca_id
        if self.access:
            headers["authorization"] = "Bearer " + self.access  # required by Privy
        _, j, _d = http("POST", PRIVY_SESSIONS_URL, headers, {"refresh_token": self.refresh})
        # fomo's API needs the APP-scoped access token (aud = fomo app id), which
        # Privy returns in `token`. `privy_access_token` has aud=auth.privy.io and is
        # REJECTED by fomo (401). `token` is only minted when the `authorization`
        # access token is still valid — so refresh BEFORE expiry, never after.
        minted = j.get("token")
        # Persist the ROTATED refresh token even when no app token came back: Privy rotates it
        # single-use, so dropping it here would kill the chain permanently on a partial response.
        if j.get("refresh_token"):
            self.refresh = j["refresh_token"]
        # Did expiry actually MOVE? Privy re-issues the SAME token while the current one is still
        # valid (observed 2026-08-03: `token` present, `iat` unchanged, refresh rotated) and only
        # mints a new one near expiry. "A token came back" is therefore not proof of progress —
        # banking it as success is what let the token die and deadlock auth. The caller reads
        # `.extended` to decide whether to re-arm its interval or retry with backoff.
        before = self.access_expires_in()
        if minted:
            self.access = minted
        after = self.access_expires_in()
        self.extended = (before is None or after is None or after > before + 1.0) if minted else False
        self._persist()
        if not minted:
            # 2026-08-03 outage: a 200-without-`token` used to be silently treated as success
            # (`self.access = j.get("token") or self.access`). The caller then banked it, the
            # stale token expired ~10 min later, and refresh could never mint again because the
            # presented token was expired — a deadlock only a browser re-login could break.
            # Fail LOUDLY so the caller retries while the current token is still valid.
            raise PrivyAppTokenUnavailable(
                "privy refresh returned no app-scoped `token` — refresh must happen BEFORE "
                "the current access token expires, or it can never be minted again")
        return self.access

    def access_expires_in(self, now_ts=None):
        """Seconds until the app access token expires; None when it cannot be read.

        The runner must schedule refreshes off the TOKEN's life, not the process's. `last_refresh`
        is seeded at startup, so a runner that inherits an already-old token (a restart, or a token
        pasted in from a browser) would wait its full interval and let the token die first.
        """
        import base64
        import json as _json
        import time as _time
        try:
            payload = str(self.access or "").split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = _json.loads(base64.urlsafe_b64decode(payload)).get("exp")
            if exp is None:
                return None
            return float(exp) - float(now_ts if now_ts is not None else _time.time())
        except Exception:
            return None


def build_feed_path(limit=100):   # 100 is fomo's cap (150 -> 400); more headroom = fewer burst-misses
    qs = "&".join(["limit=%d" % limit] + ["feedTypes=%s" % t for t in FEED_TYPES])
    return "/feed?" + qs


class FomoBanGuardActive(RuntimeError):
    """The ban guard is in its cooldown window — the call was refused WITHOUT touching fomo."""


class FomoThrottleBacklog(RuntimeError):
    """The inter-call gap has queued this caller further out than the wait cap — refused locally.

    The gap is GLOBAL and shared: the main poll loop and the armed follow-fast lane (one call per
    armed wallet) go through the same guard. At an escalated `gap_s` each queued caller ratchets
    `_last_call` forward by another gap, so a lane with a dozen wallets can push the main thread's
    wait minutes into the future. The main thread writes the heartbeat, so it went stale, the
    watchdog killed the runner as wedged, and the funded lane lost a minute of coverage per event.
    Caught live 2026-08-23 15:33-15:36: main thread blocked in `before_call`, heartbeat 120s+.

    Refusing is strictly better than sleeping through it. The caller treats a raised call as a
    failed poll and keeps looping (the documented contract above), so the loop stays responsive and
    keeps its heartbeat while the backlog drains.
    """


class _RateGuard:
    """Shared self-defense for every fomo call (2026-08-17, after the cadence push doubled our
    request rate against the 2026-08-03 app-ban precedent).

    - 429 doubles a global inter-call gap (cap 30 s); a clean stretch decays it back to 0.
    - 403 on the post-refresh leg — a valid JWT still refused, the exact 08-03 ban signature —
      trips a hard cooldown: every call is refused locally until it expires, and the operator is
      paged ONCE on trip and once on recovery. Hammering a ban is how a temp block goes permanent.
    Callers need no changes: lanes already treat a raised call as a failed poll and keep looping.
    """

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self.gap_s = self._base_gap()
        self.ban_until = 0.0
        self._last_call = 0.0
        self._last_escalation = 0.0
        # SPLIT FROM `_last_escalation` (2026-08-29). One field used to mean both "a 429 raised the
        # gap" and "a clean stretch lowered it", so the state could not say which had happened, and
        # the question "was the throttle ever active today" had to be answered by grepping a log
        # and inferring. It is the third defect of this shape today: the value existed and nothing
        # kept it. Decay timing is UNCHANGED — the ladder still steps off whichever event was more
        # recent, which is exactly what one shared field computed.
        self._last_decay = 0.0
        self.escalations = 0
        self.decays = 0
        self.peak_gap_s = 0.0
        self._elevated_since = 0.0
        self.elevated_s_total = 0.0
        self._paged_trip = False

    @staticmethod
    def _page(title, text):
        try:
            from sniper.services.memecoin_telegram import send_telegram
            send_telegram("🛡 %s | %s" % (title, text))
        except Exception:
            pass

    def before_call(self, max_wait_s=None):
        import time as _t
        if max_wait_s is None:
            from sniper.settings.memecoin import MEMECOIN_FOMO_MAX_THROTTLE_WAIT_S
            max_wait_s = MEMECOIN_FOMO_MAX_THROTTLE_WAIT_S
        with self._lock:
            now = _t.monotonic()
            if now < self.ban_until:
                raise FomoBanGuardActive(
                    "fomo ban guard: cooling down %.0fs more" % (self.ban_until - now))
            wait = (self._last_call + self.gap_s) - now if self.gap_s > 0 else 0.0
            if max_wait_s > 0 and wait > max_wait_s:
                # Refuse WITHOUT ratcheting `_last_call`: this call is not being made, so it must
                # not push the queue further out for everyone behind it. A legitimate single-call
                # wait never exceeds `gap_s` (30 s at full escalation); anything beyond that is
                # queueing behind other callers, which is the condition this cap exists to break.
                raise FomoThrottleBacklog(
                    "fomo throttle backlog: %.1fs wait over the %.1fs cap (gap %.1fs)"
                    % (wait, max_wait_s, self.gap_s))
            self._last_call = max(now, self._last_call + self.gap_s)
        if wait > 0:
            _t.sleep(wait)

    @staticmethod
    def _base_gap():
        """The floor the gap never decays below — steady pacing instead of 429-and-back-off.

        Read at call time so `.env` tracks without a restart of intent; 0 reproduces the previous
        behaviour exactly (start at 0, decay to 0).
        """
        from sniper.settings.memecoin import MEMECOIN_FOMO_BASE_GAP_S
        try:
            return max(0.0, float(MEMECOIN_FOMO_BASE_GAP_S))
        except (TypeError, ValueError):
            return 0.0

    def on_success(self):
        import time as _t
        with self._lock:
            now = _t.monotonic()
            # `max(...)` of the two clocks reproduces the single-field behaviour exactly: the
            # ladder waits 120 s from the LAST state change in either direction.
            # ABOVE THE FLOOR, not above zero (2026-09-06). With a base gap configured, `> 0` is
            # true at rest, so the ladder decayed on a guard that had never escalated and logged a
            # "recovered" line every 120 s with a nonsense span (233,563 s elevated, peak gap 0.0,
            # 0 escalations) — noise in the exact log used to measure throttling.
            if (self.gap_s > self._base_gap()
                    and now - max(self._last_escalation, self._last_decay) > 120.0):
                base = self._base_gap()
                self.gap_s = base if self.gap_s <= max(1.0, base) else self.gap_s / 2.0
                self._last_decay = now
                self.decays += 1
                if self.gap_s <= self._base_gap() and self._elevated_since:
                    self.elevated_s_total += max(0.0, now - self._elevated_since)
                    span, peak = now - self._elevated_since, self.peak_gap_s
                    self._elevated_since = 0.0
                    logger.warning(
                        "fomo throttle recovered: gap back to the %.2fs floor after %.0fs "
                        "elevated, peak gap %.1fs, %d escalations",
                        self._base_gap(), span, peak, self.escalations)
            if self._paged_trip:
                self._paged_trip = False
                self._page("FOMO-GUARD-RECOVERED", "requests flowing again at full cadence")

    def on_rate_limited(self):
        import time as _t
        with self._lock:
            now = _t.monotonic()
            was = self.gap_s
            self.gap_s = min(30.0, max(1.0, self.gap_s * 2.0))
            self._last_escalation = now
            self.escalations += 1
            self.peak_gap_s = max(self.peak_gap_s, self.gap_s)
            # ABOVE THE FLOOR, not above zero (2026-09-06) — the mirror of the decay guard. With
            # a base gap configured `was` is never <= 0, so an escalation never stamped
            # `_elevated_since` and the recovery it belongs to could never be reported.
            if was <= self._base_gap():
                # EDGE-TRIGGERED, and at WARNING on purpose. The runner emits warnings and drops
                # info — a `logger.info` in this process is a line that does not exist, which is
                # how the quote-failure reason was destroyed. Two lines per pressure episode, not
                # one per 429.
                self._elevated_since = now
                logger.warning("fomo throttle engaged: first 429, inter-call gap now %.1fs",
                               self.gap_s)

    def snapshot(self):
        """Current throttle state, for a caller that wants to record it without grepping a log."""
        import time as _t
        with self._lock:
            elevated = self.elevated_s_total
            if self._elevated_since:
                elevated += max(0.0, _t.monotonic() - self._elevated_since)
            return {"gap_s": self.gap_s, "peak_gap_s": self.peak_gap_s,
                    "escalations": self.escalations, "decays": self.decays,
                    "elevated_s": round(elevated, 1),
                    "currently_elevated": bool(self._elevated_since)}

    def on_ban_signature(self, cooldown_s):
        import time as _t
        with self._lock:
            self.ban_until = _t.monotonic() + float(cooldown_s)
            already = self._paged_trip
            self._paged_trip = True
        from sniper.settings import memecoin as _cfg
        if not already and _cfg.MEMECOIN_SIGNAL_SOURCE == "j7":
            # Signals arrive over J7; a fomo 403 here changes nothing the operator acts on
            # (operator 2026-09-12: "you can turn this off"). The pause above still applies.
            logger.warning("fomo ban guard tripped under J7 signal source; paused %ds, not paged",
                           int(cooldown_s))
        elif not already:
            self._page("FOMO-BAN-GUARD",
                       "403 with a fresh token (the 08-03 ban signature) — ALL fomo calls "
                       "paused %ds so a temp block cannot be hammered into a permanent one"
                       % int(cooldown_s))


_rate_guard = _RateGuard()


# --- activity-WebSocket reconnect pacing ----------------------------------------------------------
# A dropped socket and a REFUSED socket are different answers and must not share a backoff.
#
# `1008 policy violation` is fomo telling us to stop, the same app-layer NO that got the account
# banned on 2026-08-03 while the JWT stayed valid. The worker's original ladder reset to 1 s on
# EVERY payload, so a socket that connected, delivered one message and was then kicked went
# straight back to a one-second retry: on 2026-09-06 that produced 13 reconnects in ten minutes
# while the 30 s cap never held.
#
# So: only a session that stayed up long enough to be called healthy earns the reset, and a policy
# refusal starts high and climbs to its own much higher ceiling.
FOMO_WS_HEALTHY_SESSION_S = 60.0
FOMO_WS_TRANSPORT_CAP_S = 30.0
FOMO_WS_REFUSAL_FLOOR_S = 30.0
FOMO_WS_REFUSAL_CAP_S = 300.0
FOMO_WS_POLICY_CLOSE_CODES = (1008,)


def is_ws_policy_refusal(exc):
    """True when the peer closed us for POLICY, not for transport trouble.

    Reads the close code off the exception rather than matching the message: `websockets` puts it
    on `.code`, and a string match would silently stop working the day the reason text changes.
    """
    code = getattr(exc, "code", None)
    if code in FOMO_WS_POLICY_CLOSE_CODES:
        return True
    # `.code` is absent on some wrapper exceptions; fall back to the received close frame.
    rcvd = getattr(exc, "rcvd", None)
    return getattr(rcvd, "code", None) in FOMO_WS_POLICY_CLOSE_CODES


def fomo_ws_backoff(retry_s, session_s, exc):
    """Seconds to wait before the next activity-WebSocket connect attempt.

    `session_s` is how long the connection that just died stayed up. A session shorter than
    FOMO_WS_HEALTHY_SESSION_S never resets the ladder, however many payloads it delivered — a
    payload proves the socket opened, not that it is welcome.
    """
    refused = is_ws_policy_refusal(exc)
    try:
        prev = float(retry_s)
    except (TypeError, ValueError):
        prev = 1.0
    if not refused and float(session_s or 0.0) >= FOMO_WS_HEALTHY_SESSION_S:
        return 1.0
    if refused:
        return min(FOMO_WS_REFUSAL_CAP_S, max(FOMO_WS_REFUSAL_FLOOR_S, prev * 2.0))
    return min(FOMO_WS_TRANSPORT_CAP_S, max(1.0, prev) * 2.0)


def fomo_request(auth, method, path, *, body=None, http=None, retry=True, max_wait_s=None):
    """One authenticated fomo call, behind the shared rate guard.

    `max_wait_s` bounds how long THIS caller may stand inside the guard's backoff before the guard
    refuses instead of sleeping. It does NOT shorten the backoff: a refusal leaves `_last_call`
    un-ratcheted, so a caller that gives up its turn costs nobody behind it. Callers that must be
    made eventually leave it None and take the module default.
    """
    from sniper.settings.memecoin import MEMECOIN_FOMO_BAN_COOLDOWN_S
    http = http or _http_json
    _rate_guard.before_call(max_wait_s=max_wait_s)
    headers = dict(BROWSER_HEADERS)
    headers["authorization"] = "Bearer " + (auth.access or "x")
    if body is not None:
        headers["content-type"] = "application/json"
    try:
        result = http(str(method).upper(), FOMO_API + path, headers, body)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _rate_guard.on_rate_limited()
            raise
        if e.code in (401, 403) and retry:
            auth.refresh_token(http=http)
            return fomo_request(
                auth, method, path, body=body, http=http, retry=False, max_wait_s=max_wait_s,
            )
        if e.code == 403 and not retry:
            # the refresh above just minted this token and fomo STILL refuses: app-layer ban
            _rate_guard.on_ban_signature(MEMECOIN_FOMO_BAN_COOLDOWN_S)
        raise
    _rate_guard.on_success()
    return result


def fomo_get(auth, path, http=None, retry=True, max_wait_s=None):
    return fomo_request(auth, "GET", path, http=http, retry=retry, max_wait_s=max_wait_s)


# ---------------------------------------------------------------------------
# FeedAdapter integration
# ---------------------------------------------------------------------------

from sniper.feeds.base import FeedAdapter  # noqa: E402


class FomoFeedAdapter(FeedAdapter):
    feed_id = "fomo"
    display_name = "fomo.family copy feed"
    default_poll_seconds = 1
    mode = "poll"

    def __init__(self):
        self.auth = FomoAuth()

    def validate_config(self):
        return bool(self.auth.refresh or self.auth.access)

    def warmup(self, conn):
        from sniper.persistence import db_memecoin as _m
        _m.ensure_memecoin_tables(conn)

    def tick(self, conn):
        from sniper.services.memecoin_runner import run_once
        from sniper.settings import memecoin as _ws
        run_once(conn, self.auth, _ws.MEMECOIN_COPY_SIZE_USD)
