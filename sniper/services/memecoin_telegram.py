"""memecoin_telegram.py — fail-closed Telegram sender for the front-run latency instrument.

Fires a message the instant we detect a curated large buy, so the operator can compare (on the
same phone = shared clock) when our alert lands vs when fomo's own push lands. External send:
the runner gates it behind MEMECOIN_TG_ALERTS (default off). The bot token is a credential set
by the operator in env; it is never hardcoded. This module NEVER raises into the poll loop.
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
import math
import os
import queue as _queue
import re
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)
_warned = {"config": False}
_flood_waits = 0
_flood_waits_lock = threading.Lock()


class TelegramDeliveryUnknown(Exception):
    """Telegram can receive the message although its response is unavailable."""


class TelegramTransientError(Exception):
    """A network-level send failure that survived the retry: timeout, DNS, refused, reset.

    Separated from an unexpected fault so the caller can log ONE line instead of a traceback.
    2175 identical rails-blind tracebacks buried the three real Telegram failures in this log;
    a transient network blip is not a defect report and must not read like one.
    """


# urllib raises socket.timeout (an OSError alias in 3.10+) and URLError for the whole transient
# family. OSError is the common base and covers refused/reset/unreachable without listing each.
TRANSIENT_SEND_ERRORS = (TimeoutError, OSError)


def telegram_retryable_error(exc):
    """Only uncertain transport/server outcomes permit the explicitly marked resend."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, (TelegramDeliveryUnknown, TelegramTransientError, *TRANSIENT_SEND_ERRORS))


class TelegramFloodWait(Exception):
    def __init__(self, retry_after):
        try:
            retry_after = float(retry_after)
        except (TypeError, ValueError):
            retry_after = 1.0
        self.retry_after = max(0.0, retry_after)
        super().__init__(f"telegram flood wait {self.retry_after:g}s")


class _TelegramRateLimiter:
    """Serialize send starts per chat without coupling unrelated recipients."""

    def __init__(self, *, monotonic=time.monotonic, sleep_fn=time.sleep):
        self._monotonic = monotonic
        self._sleep = sleep_fn
        self._state_lock = threading.Lock()
        self._states = {}

    def wait(self, chat_id, min_gap_s):
        gap = max(0.0, float(min_gap_s))
        if gap == 0:
            return
        with self._state_lock:
            state = self._states.setdefault(str(chat_id), {
                "lock": threading.Lock(), "last_started": None,
            })
        with state["lock"]:
            now = self._monotonic()
            if state["last_started"] is not None:
                delay = gap - (now - state["last_started"])
                if delay > 0:
                    self._sleep(delay)
            state["last_started"] = self._monotonic()


_send_limiter = _TelegramRateLimiter()


def telegram_flood_wait_count():
    with _flood_waits_lock:
        return _flood_waits


def _record_flood_wait(chat_id, retry_after):
    global _flood_waits
    with _flood_waits_lock:
        _flood_waits += 1
    logger.warning(
        "memecoin telegram flood wait: chat=%s retry_after=%.1fs count=%d",
        chat_id, retry_after, telegram_flood_wait_count(),
    )


class _TelegramDispatcher:
    """One bounded daemon sender; enqueue never waits for Telegram network I/O."""

    def __init__(self, send_fn, *, maxsize, audit_fn=None, now_fn=None, lifecycle_factory=None, retry_after_s=5.0):
        self._send_fn = send_fn
        self._lifecycle_factory = lifecycle_factory
        self._lifecycle = None
        self._audit_fn = audit_fn
        self._now_fn = now_fn or _real_utc_now_iso
        self._queue = _queue.Queue(maxsize=max(1, int(maxsize)))
        self._retry_after_s = float(retry_after_s)
        if not math.isfinite(self._retry_after_s) or self._retry_after_s < 0:
            raise ValueError("Telegram retry delay must be finite and nonnegative")
        self._retries = []                 # owned by the sender thread; bounded like the primary queue
        self._thread = threading.Thread(
            target=self._run,
            name="memecoin-telegram-dispatch",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, text, *, kind="generic", source_id=None, on_result=None):
        """`on_result(ok)`, when given, runs on this dispatcher's thread after the send and its audit
        row, after the final attempt: ok is delivery success, never queue acceptance (Codex F11).
        It never runs for a refused enqueue. Generic jobs, delayed retries and callbacks are
        process-local and lost at shutdown; only lifecycle transitions recover after restart.
        A False callback cannot prove Telegram did not receive an ambiguous send."""
        raw = str(text)
        audit_payload = telegram_audit_payload(raw)
        job = {
            "text": raw,
            "message_kind": str(kind or "generic")[:80],
            "source_id": str(source_id)[:200] if source_id is not None else None,
            "queued_at": self._now_fn(),
            "attempt": 1,
            **audit_payload,
        }
        if on_result is not None:
            job["_on_result"] = on_result
        try:
            self._queue.put_nowait(job)
            return True
        except _queue.Full:
            logger.warning("memecoin telegram queue full — alert dropped; funded path unaffected")
            return False

    def enqueue_source(self, event):
        return self.enqueue_identity({"event": event})

    def enqueue_identity(self, identity):
        try:
            self._queue.put_nowait({"notification_identity": True, **identity})
            return True
        except _queue.Full:
            logger.warning("memecoin notification queue full; durable reconciliation will recover the result")
            return False

    def _run(self):
        while True:
            if self._lifecycle_factory and self._lifecycle is None:
                try:
                    self._lifecycle = self._lifecycle_factory()
                except Exception:
                    logger.exception("memecoin notification worker initialization failed")
                    time.sleep(2)
                    continue
            from_retry = False
            try:
                # 0.5 s idle cadence: a held source alert must see its decision promptly. The
                # cycle only delivers here; its scans keep their own 2 s throttle.
                timeout = 0.5 if self._lifecycle_factory else None
                due = min(self._retries, key=lambda r: r[0]) if self._retries else None
                if due and due[0] <= time.monotonic():
                    self._retries.remove(due)
                    job, from_retry = due[1], True
                else:
                    if due:
                        wait = max(0.0, due[0] - time.monotonic())
                        timeout = wait if timeout is None else min(timeout, wait)
                    job = self._queue.get(timeout=timeout)
            except _queue.Empty:
                try:
                    if self._lifecycle:
                        self._lifecycle.cycle()
                except Exception:
                    logger.exception("memecoin notification reconciliation failed")
                continue
            if job.get("notification_identity"):
                try:
                    if self._lifecycle:
                        self._lifecycle.cycle(job)
                except Exception:
                    logger.exception("memecoin notification preparation failed; durable result remains available")
                finally:
                    self._queue.task_done()
                continue
            on_result = job.pop("_on_result", None)
            started_at = self._now_fn()
            t0 = time.perf_counter()
            ok = False
            send_ms = 0.0
            error_class = None
            retry = False
            try:
                result = self._send_fn(job["text"])
                if isinstance(result, tuple):
                    ok = bool(result[0])
                    send_ms = float(result[1] or 0.0)
                else:
                    ok = bool(result)
                    send_ms = (time.perf_counter() - t0) * 1000.0
            except Exception as exc:
                send_ms = (time.perf_counter() - t0) * 1000.0
                error_class = type(exc).__name__[:80]
                retry = job.get("attempt", 1) == 1 and telegram_retryable_error(exc)
                if retry and len(self._retries) >= self._queue.maxsize:
                    logger.warning("memecoin telegram retry queue full; resend dropped")
                    retry = False
                logger.exception("memecoin telegram async send failed")
            finally:
                if self._audit_fn is not None:
                    fields = {**job, "send_started_at": started_at,
                              "completed_at": self._now_fn(), "send_ok": ok,
                              "send_ms": send_ms, "error_class": error_class}
                    fields.pop("text", None)
                    try:
                        self._audit_fn(fields)
                    except Exception:
                        logger.exception("memecoin telegram delivery audit failed")
                if retry:
                    text = "RESENT (may be a duplicate)\n" + job["text"]
                    resend = {**job, "text": text, "attempt": 2, **telegram_audit_payload(text)}
                    if on_result is not None:
                        resend["_on_result"] = on_result
                    self._retries.append((time.monotonic() + self._retry_after_s, resend))
                elif on_result is not None:
                    try:
                        on_result(ok)
                    except Exception:
                        logger.exception("memecoin telegram delivery result hook failed")
                if not from_retry:
                    self._queue.task_done()
                if self._lifecycle:
                    try:
                        self._lifecycle.cycle()
                    except Exception:
                        logger.exception("memecoin notification reconciliation failed")


_dispatcher = None
_dispatcher_lock = threading.Lock()


def _real_utc_now_iso():
    from sniper.util.clock import real_utc_now_iso
    return real_utc_now_iso()


def redact_telegram_text(text):
    """Remove signed tap URLs while preserving every other rendered byte for incident review."""
    return re.sub(r"(?im)^(\s*(?:⚡\s*)?tap:)[^\r\n]*", r"\1 [redacted]", str(text))


def telegram_audit_payload(text):
    raw = str(text)
    return {
        "rendered_text_redacted": redact_telegram_text(raw),
        "message_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }


def _persist_delivery_audit(fields):
    """Worker-thread DB write; callers enqueue without opening or touching the funded ledger."""
    from sniper.persistence.db_core import connection_scope
    from sniper.persistence.db_memecoin import record_telegram_delivery
    from sniper.util.state_paths import state_path
    configured_path = os.getenv("DB_PATH")
    db_path = (configured_path if configured_path and os.path.isabs(configured_path)
               else str(state_path("sniper_tracker.db")))
    with connection_scope(db_path) as conn:
        record_telegram_delivery(conn, fields)


def send_telegram_audited(text, *, kind="generic", source_id=None, send_fn=None, audit_fn=None):
    """A SYNCHRONOUS send that writes its own delivery-audit row, in the dispatcher's shape (queued_at
    is the send start: there is no queue). For a one-shot process, which has no dispatcher: only the
    dispatcher wrote memecoin_telegram_delivery_audit, so the deploy's page left no row (Alpha
    2026-10-16). Returns (ok, send_ms); a send that raises is (False, ms) with its error class; an audit
    write that fails is logged and never changes the result. NO CHANNEL, NO ROW (Gamma LOW-1): as the
    dispatcher, nothing is recorded while dispatch_enabled() is false (unconfigured test runs wrote 567
    non-attempt rows on 2026-08-06)."""
    send_fn = send_fn or send_telegram
    audit_fn = audit_fn or _persist_delivery_audit
    audited = dispatch_enabled()
    started_at, t0 = _real_utc_now_iso(), time.perf_counter()
    ok, send_ms, error_class = False, 0.0, None
    try:
        result = send_fn(text)
        ok, send_ms = bool(result[0]), float(result[1] or 0.0)
    except Exception as exc:
        send_ms, error_class = (time.perf_counter() - t0) * 1000.0, type(exc).__name__[:80]
        logger.exception("memecoin telegram synchronous send failed")
    if not audited:
        return ok, send_ms
    try:
        audit_fn({"message_kind": kind, "source_id": source_id, "queued_at": started_at,
                  "send_started_at": started_at, "completed_at": _real_utc_now_iso(), "send_ok": ok,
                  "send_ms": send_ms, "error_class": error_class, **telegram_audit_payload(text)})
    except Exception:
        logger.exception("memecoin telegram delivery audit failed")
    return ok, send_ms


def _fmt_usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    if v >= 1_000_000:
        return "$%.1fM" % (v / 1_000_000)
    if v >= 1_000:
        return "$%.1fk" % (v / 1_000)
    return "$%.0f" % v


def _honest_record(context):
    """The credibility line, built from the SAME metric the arm gate uses (alpha_b_copy_stats:
    alpha_b-only, fomo-priced fantasy fills excluded) — never the inflated all-arms/all-chain total
    that made losers read as winners (smol_intern +$674 shown vs -$63 honest). `paper_pnl`/`paper_n`
    are that honest pnl / clean-close count; `gate_ok` is whether they clear the promote gate. Tells
    the operator whether an armed wallet is actually earning (or should be revoked) and whether a
    watch-only one is ready to arm."""
    if context.get("gate_applicable") is False:
        return "paper gate: not applicable"
    pnl, n = context.get("paper_pnl"), context.get("paper_n")
    gate = "gate ✓" if context.get("gate_ok") else "gate ✗"
    if pnl is None or not n:
        return "no honest αb closes yet · %s" % gate
    coins = context.get("paper_coins")
    coin_text = str(coins) if coins is not None else "Unknown"
    return "honest αb %s$%.0f · %d %s · %s distinct coins · %s" % (
        "+" if pnl >= 0 else "-", abs(pnl), n, "close" if n == 1 else "closes", coin_text, gate)



def _authority_label(context):
    status = (context or {}).get("authority_status")
    if status == "armed":
        return "ARMED ⚡"
    if status == "unfunded_chain":
        return "unfunded chain - watch-only"
    if status == "watch_only":
        return "watch-only"
    return "ARMED ⚡" if (context or {}).get("real_armed") else "watch-only"


def _entry_status(context):
    value = (context or {}).get("entry_status")
    return str(value).strip().lower() if value is not None and str(value).strip() else None


def _entry_emoji(context):
    status = _entry_status(context)
    if status == "filled":
        return "🟢"
    if status in {None, "pending", "unknown", "not_attempted", "not_attributed"}:
        return "🟡"
    return "🟠"


def _entry_stage(context):
    context = context or {}
    if context.get("position_id") is not None:
        return "position persisted"
    if context.get("tx_sig"):
        return "submitted"
    if context.get("pending_id") is not None:
        return "reserved"
    if context.get("attempt_id"):
        return "pre-reservation"
    return None


def _entry_lines(context):
    status = _entry_status(context)
    if status is None:
        return []
    context = context or {}
    label = status.replace("_", " ").upper()
    parts = [f"entry: {label}"]
    code = str(context.get("entry_code") or "").strip()
    if code and code.lower() != status:
        parts.append(code)
    stage = _entry_stage(context)
    if stage:
        parts.append(stage)
    lines = [" · ".join(parts)]
    reason = str(context.get("entry_reason") or "").strip()
    if reason and reason.lower() not in {status, code.lower()}:
        lines.append(f"reason: {reason}")
    attempt_id = str(context.get("attempt_id") or "").strip()
    if attempt_id:
        lines.append(f"attempt: {attempt_id}")
    return lines


_TICKER_CACHE = {}


def _ticker_from_feed(mint):
    """Last resort name lookup: any ticker the FEED has ever carried for this mint.

    Why the alert needs one (2026-08-24, CITRINI). `ev.ticker` is set only by
    `_set_event_market_ticker`, which runs inside the market-data read -- so any path that resolves
    mcap WITHOUT that read (from the event's own their_market_cap, or from same-mint history) fires
    an alert with no name and renders `mint[:12]`. That is most `over_ceiling` refusals: the gate
    answers from the feed's mcap and never asks a price provider anything.

    Reads memecoin_events, which is SEARCH ... USING INDEX idx_mc_events_mint -- an index seek, not
    a tape scan. Cached per mint. Deliberately NOT an HTTP lookup: `_safe_alert` is synchronous on
    the entry path, so a network call here would add a round trip to the funded loop for a
    cosmetic label. A coin the feed has never named still renders its mint prefix, which is the
    honest answer -- CITRINI itself only entered the global feed 3 minutes AFTER we refused it."""
    if not mint:
        return None
    if mint in _TICKER_CACHE:
        return _TICKER_CACHE[mint]
    name = None
    conn = None
    try:
        from sniper.persistence.db_core import get_readonly_connection
        # Autocommit + query_only, and CLOSED immediately: an idle reader that holds a snapshot is
        # what pinned the WAL in WO-10. This handle lives for one indexed seek.
        conn = get_readonly_connection()
        row = conn.execute(
            "SELECT ticker FROM memecoin_events WHERE mint=? AND ticker IS NOT NULL "
            "AND ticker!='' LIMIT 1", (mint,)).fetchone()
        name = ((row[0] or "").strip() or None) if row else None
    except Exception:
        name = None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if name:
        # POSITIVE ONLY, and deliberately so. A miss must NOT be cached: CITRINI is the case --
        # the feed named it 3 minutes AFTER we refused the coin, so a negative entry would pin the
        # mint prefix forever on exactly the coins our wallets buy early. A miss re-queries, which
        # is one indexed seek at alert frequency (minutes apart, not milliseconds).
        if len(_TICKER_CACHE) >= 4096:
            _TICKER_CACHE.clear()          # crude bound; a name never changes, so a cold rebuild costs one seek each
        _TICKER_CACHE[mint] = name
    return name


def _display_ticker(ev, context):
    mint = getattr(ev, "mint", None) or ""
    return ((context or {}).get("display_ticker")
            or getattr(ev, "ticker", None) or _ticker_from_feed(mint)
            or mint[:12] or "unknown")


def _display_mcap(ev, context):
    value = (context or {}).get("display_market_cap")
    if value is None:
        value = getattr(ev, "their_market_cap", None)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "?"
    return _fmt_usd(value) if value > 0 else "?"


# fomo.family web routes, read from its own router manifest (2026-08-25): "profile/:handle",
# "u/:handle" and "tokens/:chain/:tokenAddress". The chain segment is fomo's own slug, not our
# network_id, so it needs this map. Unmapped chain -> no coin link, never a broken one.
from sniper.services.memecoin_links import FOMO_CHAIN_SLUG as _FOMO_CHAIN_SLUG


def _coin_links(ev):
    """Use the shared chain-aware provider links."""
    from sniper.services.memecoin_links import operator_links
    links = operator_links(getattr(ev, "network_id", None), getattr(ev, "mint", None),
                           handle=getattr(ev, "source_handle", None))
    labels = {"token": "Axiom", "trader": "Fomo trader (login)",
              "chart": "chart", "explorer": "Token explorer"}
    return [f"{labels[key]}: {links[key]}" for key in labels if links.get(key)]


def build_alert_message(ev, thesis=None, sent_at=None, context=None):
    """Compose the front-run alert. Carries fomo createdAt + our detection lag + send stamp so
    the operator can compare (on the phone) against fomo's own push arrival. Includes the
    trader's thesis on this token when one exists (context, not a signal). `context`
    ({real_armed, paper_pnl, paper_n}, built by the caller — no DB access here) adds the
    watch-everything line: is this wallet real-armed, and what has its paper copy record been."""
    lag = getattr(ev, "lag_s", None)
    from sniper.settings.memecoin import MEMECOIN_TG_STALE_LAG_S
    if lag is None or lag < 0:
        lag_str = "unknown"                       # negative/missing = clock skew or no receipt stamp
    elif lag > MEMECOIN_TG_STALE_LAG_S:
        lag_str = "stale %.0fs ⚠" % lag           # minutes-old = a backfill, not a front-run
    else:
        lag_str = "%.1fs" % lag
    lines = [
        "%s %s — %s bought" % (_entry_emoji(context), _display_ticker(ev, context),
                                getattr(ev, "source_handle", "?") or "?"),
        "size ~%s | mcap ~%s | chain %s" % (
            _fmt_usd(getattr(ev, "their_size_usd", None)),
            _display_mcap(ev, context),
            getattr(ev, "network_id", "?") or "?"),
        "fomo: %s (feed lag %s)" % (getattr(ev, "created_at", "?") or "?", lag_str),
    ]
    if context:
        lines.append("wallet: %s · %s" % (_authority_label(context), _honest_record(context)))
        pb = context.get("prior_buyers_60m")
        if pb:                                     # >=1 prior distinct buyer -> a consensus cluster
            lines.append("🔥 consensus: %d prior tracked buyer%s in 60m"
                         % (pb, "s" if pb != 1 else ""))
        if _entry_status(context) is None:
            lines.append("Source buy received. This notice does not report our entry result.")
        else:
            lines.extend(_entry_lines(context))
    else:
        lines.append("Source buy received. This notice does not report our entry result.")
    if sent_at:
        lines.append("received: %s" % sent_at)
    if thesis:
        from sniper.analysis.memecoin_thesis import thesis_stance
        pnl = thesis.get("their_pnl_pct")
        pnl_str = ("%+.0f%%" % pnl) if pnl is not None else "n/a"
        lines.append("thesis (%s, %s): %s" % (thesis_stance(pnl), pnl_str,
                                              (thesis.get("comment") or "").strip()))
    lines.append("mint: %s" % (getattr(ev, "mint", "") or ""))
    lines.extend(_coin_links(ev))
    # One-tap deep link (Phase 7, dry-run until Checkpoint E): a signed single-use token frozen
    # with THIS event's parameters. Best-effort — the alert must send even if the tap layer is
    # misconfigured (no secret / no base URL -> no line, never a failed alert).
    try:
        from sniper.services import memecoin_one_tap as ot
        if ot.one_tap_enabled():
            from sniper.util.clock import real_utc_now_iso
            url = ot.tap_url(ot.mint_token(
                mint=getattr(ev, "mint", "") or "",
                network_id=getattr(ev, "network_id", "") or "",
                source_user_id=getattr(ev, "source_user_id", "") or "",
                ticker=_display_ticker(ev, context),
                now_iso=real_utc_now_iso()))
            if url:
                lines.append("⚡ tap: %s" % url)
    except Exception:
        pass
    return "\n".join(lines)


def build_follow_activity_alert(ev, context=None):
    """Delayed-catch alert for a follow-activity buy — a bet a curated/armed trader placed that the
    real-time feed missed and the 300s backfill recovered. Deliberately DISTINCT from the front-run
    alert: it says 'DELAYED CATCH', carries the backfill age (not a front-run latency), and names the
    coin by MINT because this path has no ticker. It exists so the operator is notified of every
    real-money-relevant bet, not just the ones the live feed caught first."""
    ticker = _display_ticker(ev, context)
    mint = getattr(ev, "mint", "") or ""
    age = getattr(ev, "lag_s", None)
    age_str = ("%.0fs" % age) if (age is not None and age >= 0) else "?"
    lines = [
        "%s ⏱ DELAYED CATCH (follow-activity) — %s bought %s" % (
            _entry_emoji(context), getattr(ev, "source_handle", "?") or "?",
            ticker if ticker != "?" else mint[:14]),
        "size ~%s | mcap ~%s | chain %s | %s old" % (
            _fmt_usd(getattr(ev, "their_size_usd", None)),
            _display_mcap(ev, context),
            getattr(ev, "network_id", "?") or "?", age_str),
        "mint: %s" % mint,
    ] + _coin_links(ev)
    if context:                                    # same honest αb record as the front-run alert
        lines.append("wallet: %s · %s" % (_authority_label(context), _honest_record(context)))
        lines.extend(_entry_lines(context))
    lines.append("(missed by the live feed; recovered on the backfill poll — the pump may already be moving)")
    return "\n".join(lines)


def _default_http(url, payload, timeout):
    data = _json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            raise
        retry_after = 1.0
        try:
            body = _json.loads(exc.read().decode("utf-8", errors="replace"))
            retry_after = body.get("parameters", {}).get("retry_after", retry_after)
        except (AttributeError, TypeError, ValueError, _json.JSONDecodeError):
            pass
        raise TelegramFloodWait(retry_after) from exc


def _resolve_token():
    return os.getenv("MEMECOIN_TG_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")


def _resolve_chat():
    return os.getenv("MEMECOIN_TG_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")


def _chat_list(chat_id):
    """'111, 222 , ' -> ['111', '222']. A single id behaves exactly as before.

    The bot is send-only (no /start handler, no subscriber table), so sharing signals with another
    person means listing their chat id here — obtained from getUpdates after they message the bot.
    A group/channel id (negative) works too and is the better shape for more than a couple of people:
    membership is then managed in Telegram, not in config."""
    return [c.strip() for c in str(chat_id or "").split(",") if c.strip()]


def send_telegram(text, *, token=None, chat_id=None, http=None, timeout=None,
                  limiter=None, min_gap_s=None, sleep_fn=time.sleep, parse_mode=None):
    """Send `text` to every configured chat. Returns (ok: bool, elapsed_ms: float).

    The PRIMARY chat (first in the list) owns the return contract: `ok` and `elapsed_ms` are ITS
    send's, because they feed the front-run alert LATENCY instrument. Followers are best-effort —
    a blocked or slow recipient must never fail our own alert or distort the measurement.

    Fail-closed: missing token/chat -> (False, 0.0) with NO http call (logged once). Any send
    error -> (False, elapsed) swallowed. Token/chat resolve from MEMECOIN_TG_* then TELEGRAM_*.
    """
    token = token or _resolve_token()
    chats = _chat_list(chat_id or _resolve_chat())
    if not token or not chats:
        if not _warned["config"]:
            logger.warning("memecoin telegram: no bot token / chat id configured — alerts disabled")
            _warned["config"] = True
        return (False, 0.0)
    if timeout is None:
        from sniper.settings.memecoin import MEMECOIN_TG_TIMEOUT_S
        timeout = MEMECOIN_TG_TIMEOUT_S
    http = http or _default_http
    limiter = limiter or _send_limiter
    if min_gap_s is None:
        try:
            min_gap_s = float(os.getenv("MEMECOIN_TG_MIN_GAP_S", "3"))
        except (TypeError, ValueError):
            min_gap_s = 3.0
    min_gap_s = max(0.0, float(min_gap_s))
    url = "https://api.telegram.org/bot%s/sendMessage" % token

    def _one(cid):
        payload = {"chat_id": cid, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        for attempt in range(2):
            limiter.wait(cid, min_gap_s)
            try:
                status = http(url, payload, timeout)
                return status is None or (200 <= int(status) < 300)
            except TelegramFloodWait as exc:
                _record_flood_wait(cid, exc.retry_after)
                if attempt == 1:
                    return False
                sleep_fn(exc.retry_after)
            except urllib.error.HTTPError as exc:
                if exc.code >= 500:
                    raise TelegramDeliveryUnknown("TelegramHTTP5xx") from exc
                return False
            except TRANSIENT_SEND_ERRORS as exc:
                raise TelegramDeliveryUnknown(type(exc).__name__) from exc
        return False

    primary, followers = chats[0], chats[1:]
    t0 = time.perf_counter()
    try:
        ok = _one(primary)
        ms = (time.perf_counter() - t0) * 1000.0
    except TelegramDeliveryUnknown:
        raise
    except TelegramTransientError as exc:
        # One line, no traceback: the network was slow twice and the alert did not go out. That
        # is worth knowing and is not worth 30 lines of urllib frames.
        ms = (time.perf_counter() - t0) * 1000.0
        logger.warning("memecoin telegram send failed after retry (primary chat): %s", exc)
        ok = False
    except Exception:
        ms = (time.perf_counter() - t0) * 1000.0
        logger.exception("memecoin telegram send failed (primary chat)")
        ok = False
    for cid in followers:
        try:
            _one(cid)
        except TelegramTransientError as exc:
            logger.warning("memecoin telegram: follower chat %s unreachable: %s", cid, exc)
        except Exception:
            # a shared-with user who blocked the bot must not kill the operator's own alerting
            logger.warning("memecoin telegram: send to follower chat %s failed", cid, exc_info=True)
    return (ok, ms)


def dispatch_off():
    """MEMECOIN_TG_DISPATCH_ENABLED=0 (or false/no): the operator's hard off switch, for a process that
    has a valid token but must stay silent. The deploy's synchronous page honours it too."""
    return os.getenv("MEMECOIN_TG_DISPATCH_ENABLED", "1").strip().lower() in {"0", "false", "no"}


def dispatch_enabled():
    """False when the operator hard-disabled dispatch, or when no channel is configured.

    Both are 'this process cannot deliver a Telegram message'. Returning False BEFORE the
    dispatcher exists is the point: `send_telegram` already fails closed on a missing token, but
    the worker's `finally` still wrote a delivery-audit row, so an unconfigured process logged
    thousands of 'deliveries' that were never attempted (2026-08-06: 567 of 712 rows in the funded
    ledger came from test runs, incl. the deploy's own release validation, because
    tests/conftest.py blanks the token but points DB_PATH at the real sniper_tracker.db for
    READS). A delivery audit must record deliveries, not messages a disabled channel dropped.

    MEMECOIN_TG_DISPATCH_ENABLED=0 is a hard off switch independent of configuration, for a
    process that has a valid token but must stay silent."""
    if dispatch_off():
        return False
    return bool(_resolve_token() and _chat_list(_resolve_chat()))


def _get_dispatcher():
    global _dispatcher
    if not dispatch_enabled():
        return None
    if _dispatcher is None:
        with _dispatcher_lock:
            if _dispatcher is None:
                try:
                    maxsize = int(os.getenv("MEMECOIN_TG_QUEUE_MAX", "500"))
                except (TypeError, ValueError):
                    maxsize = 500
                activation_at = _real_utc_now_iso()
                _dispatcher = _TelegramDispatcher(send_telegram, maxsize=maxsize,
                    audit_fn=_persist_delivery_audit,
                    lifecycle_factory=lambda: _NotificationWorker(activation_at=activation_at))
    return _dispatcher


def start_notification_dispatcher():
    """Start the sole source and result worker before the feed loop."""
    return _get_dispatcher()


def notification_lifecycle_active():
    return bool(_dispatcher is not None and getattr(_dispatcher, "_lifecycle_factory", None))


def enqueue_entry_notification(*, origin_event_id=None, attempt_id=None):
    """Queue only public identity. The worker reads all execution evidence."""
    dispatcher = _get_dispatcher()
    return bool(dispatcher and dispatcher.enqueue_identity(
        {"origin_event_id": origin_event_id, "attempt_id": attempt_id}))


def enqueue_telegram(text, *, kind="generic", source_id=None, on_result=None):
    """Queue one message through the shared owner. `on_result(ok)`: see _TelegramDispatcher.enqueue
    (not for identity notifications, which have their own durable reconciliation)."""
    dispatcher = _get_dispatcher()
    if not dispatcher:
        return False
    if source_id and (kind.startswith("follow_activity:") or kind == "operational:REAL-FILLED"):
        return dispatcher.enqueue_identity({"origin_event_id": source_id})
    if on_result is None:
        return dispatcher.enqueue(text, kind=kind, source_id=source_id)
    return dispatcher.enqueue(text, kind=kind, source_id=source_id, on_result=on_result)


def _public_dashboard_url(path):
    """Use the configured public HTTPS origin for internal operator links."""
    import ipaddress
    from urllib.parse import urlsplit
    try:
        base = urlsplit(os.getenv("MEMECOIN_ONE_TAP_BASE_URL", ""))
        host = base.hostname
        if base.scheme != "https" or not host or base.username or base.password:
            return None
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return f"https://{base.netloc}" + path if path.startswith("/") and not path.startswith("//") else None
    except ValueError:
        return None


def _notification_usd(value):
    return f"${float(value):,.2f}" if value is not None else "Unknown"


_STATUS_MARK = {
    "filled": "🟢", "sold": "💰", "refused": "🟠", "no_fill": "⚪", "not_evaluated": "⚪",
    "failed_before_submission": "🔴", "transaction_failed": "🔴", "retry_pending": "🔁",
    "source_received": "⏳", "evaluating": "⏳", "needs_review": "⚠️", "shadow": "⚪",
    "transaction_unresolved": "⚠️", "submitted_unknown": "⚠️",
}
# Machine reasons the operator reads every day, in words. Anything else prints verbatim.
_REASON_WORDS = {
    "not_approved": "wallet not armed on this chain",
    "missing_receipt_proof": "no proof the wallet signed and paid for this buy",
    "unsupported_or_unarmed_network": "chain not funded",
    "quiet_hours": "quiet hours (UTC window)",
    "kill_switch_tripped": "kill switch is on",
    "real_disabled": "funded entries disabled",
}


# An UNKNOWN source size is not a small one. 'source buy unknown under mcap floor 0.500% ($45)'
# reached the phone verbatim on 2026-09-26 (alert 753be289) and read as "they bought under $45"
# for a 2 SOL buy the on-chain lane could not value. The reason string stays byte-identical: it
# keys the source_buy_floor refusal family and the wallet gate exemptions.
_UNKNOWN_SOURCE_SIZE = re.compile(
    r"^source buy unknown under (?:mcap floor [\d.]+% \(\$([\d,.]+)\)|unknown-mcap floor \$([\d,.]+))")


def _unknown_source_size_words(reason):
    m = _UNKNOWN_SOURCE_SIZE.match(str(reason or ""))
    if not m:
        return None
    return f"source size unknown (missing evidence); floor ${m.group(1) or m.group(2)} not evaluated"


# THE IMPACT CAP'S HONEST CAUSE (operator 2026-10-02, option (a)). "price impact 23.37% over 12.0%
# cap" on a $20 buy (PACKZ) read as our own size moving the pool; it was the source's own push priced
# against a lagging reference. The reason string stays byte-identical: the refusal capture, the
# shadow and the family reads parse it.
_IMPACT_CAP = re.compile(r"^price impact ([\d.]+)% over ([\d.]+)% cap$")


def _impact_cap_words(view):
    m = _IMPACT_CAP.match(str(view.get("reason") or ""))
    if not m:
        return None
    row = next((r for r in reversed(view.get("gate_evidence") or [])
                if r.get("name") == "impact_cap_route"), {})
    jupiter = "Jupiter vs pre-buy price %.1f%%" % float(m.group(1))
    cap = "cap %g%%" % float(m.group(2))
    ran = row.get("ran_pct")
    if not isinstance(ran, (int, float)):
        return "%s (%s)" % (jupiter, cap)
    who = view.get("source_handle") or "the source"
    head = ("price already ran %+.1f%% above %s's fill" % (ran, who) if ran > 0
            else "our quote is %+.1f%% vs %s's fill" % (ran, who))
    return "%s (%s, %s)" % (head, jupiter, cap)


def _short_mint(mint):
    """'GFYQ…6fSx' for a coin no signal ever named, instead of 'Unknown token'."""
    mint = str(mint or "")
    return f"{mint[:4]}…{mint[-4:]}" if len(mint) > 12 else (mint or None)


def _slot_lane_words(view):
    """'slot lane refused: <its reason>' for a fast-lane-only refusal the slot lane had already
    decided (operator 2026-10-06: jpegkite RULR paged "Why: native_fast_lane_unavailable" while the
    slot lane had refused it at the source-buy floor). A hand-off or expiry note is a step, not a
    verdict, so it changes nothing."""
    reason = view.get("reason")
    if reason not in ("native_fast_lane_unavailable", "missing_receipt_proof"):
        return None
    row = next((r for r in reversed(view.get("gate_evidence") or [])
                if r.get("name") == "slot_lane_skip"), None)
    if not row or row.get("outcome") in (None, "lane", "handoff", "pair_expired", "metadata_invalid"):
        return None
    verdict = "refused" if row.get("outcome") in ("rejected", "refused") else str(row.get("outcome"))
    words = "slot lane %s: %s" % (verdict, row["reason"]) if row.get("reason") else "slot lane " + verdict
    if reason == "missing_receipt_proof":
        # The receipt refusal is still the reason; the slot lane's verdict says why no lane bought.
        return "%s; %s" % (_REASON_WORDS.get(reason, reason), words)
    return words


def _kill_deploy_words(view):
    """A refusal inside a deploy's own kill-switch trip (operator ref 1bf3a775, HOTDOG 17:16:11Z) read
    "kill switch is on". The worker sets kill_deploy_restart from the switch's audit log; the reason
    string stays kill_switch_tripped."""
    if view.get("reason") == "kill_switch_tripped" and view.get("kill_deploy_restart") is True:
        return "deploy restart (kill switch auto-trip)"
    return None


def _oversize_words(view):
    """'transaction too large (1,442 bytes, cap 1,232)' for a buy the send step refused on size.

    The bare `broadcast_rejected` said nothing on 2026-09-14 ("human" ref 7b9a3454): the signed
    swap was over the Solana packet cap, and the oversize rebuild was over it too."""
    if view.get("reason") != "broadcast_rejected":
        return None
    for row in reversed(view.get("gate_evidence") or []):
        size = row.get("post_bytes") if row.get("name") == "oversize_retry_rebuild" else (
            row.get("pre_bytes") if row.get("name") == "oversize_retry" else None)
        limit = row.get("limit")
        if size and limit and size > limit:
            return f"transaction too large ({size:,} bytes, cap {limit:,})"
    return None


def _unwrap_not_visible_words(view):
    """The 8453 PICNIC wording (2026-10-06, Gamma/Alpha). A swap the executor held back because the
    node it sends through cannot see the confirmed unwrap yet (evm.native_visible visible=False:
    native_pre_transaction_ambiguous), or that node's broadcast refusal after an unwrap whose
    visibility was never proven, bought nothing: the unwrap and the cleanup re-wrap cost gas only.
    A refusal after visible=True (every pool node saw the unwrap) is something else and keeps its
    own words (Gamma LOW on bec6db20d). The bare 'broadcast_rejected' plus a 'landing block
    resolving' line read as money in flight."""
    rows = [r for r in view.get("gate_evidence") or [] if r.get("name") == "evm.native_visible"]
    seen = rows[-1].get("visible") if rows else None
    if rows and (seen is False or (view.get("reason") == "broadcast_rejected" and seen is not True)):
        return "unwrap not yet visible to the swap node (no buy made, gas only)"
    return None


def _notification_time(value):
    """HH:MM:SS for today (UTC), a full date otherwise; None when unparseable."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
    if dt.date() == datetime.now(timezone.utc).date():
        return dt.strftime("%H:%M:%S")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _notification_price(value):
    """Four significant digits: 0.0001543, 1.234, 12,340. None -> Unknown."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "Unknown"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.4g}"


def _fmt_mcap(v):
    """$34K, $344K, $1.2M: the dashboard's market-cap form, no decimals below a million."""
    if v >= 1e9:
        return "$%.1fB" % (v / 1e9)
    if v >= 1e6:
        return "$%.1fM" % (v / 1e6)
    if v >= 1e3:
        return "$%.0fK" % (v / 1e3)
    return "$%.0f" % v


def _notification_at(value, supply):
    """A fill price as the coin's market cap ("$34K mcap") when the supply is known, else the
    price (operator, 2026-09-28: no 0.0000343 on the phone)."""
    try:
        mcap = float(value) * float(supply)
    except (TypeError, ValueError):
        return _notification_price(value)
    return f"{_fmt_mcap(mcap)} mcap" if mcap > 0 else _notification_price(value)


def _seconds_between(a, b):
    from datetime import datetime
    try:
        ta = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        tb = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        if ta.utcoffset() is None or tb.utcoffset() is None:
            return None
        return (tb - ta).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


def _earlier_suffix(view):
    """Suffix like ' (+$1.2k earlier = $2.1k)' when the source-buy floor judged this buy together
    with the wallet's earlier net into the coin, else ''. SHI 4663, 2026-09-25: "bought $877" was
    admitted on $2,109 and read as nonsense beside a refused $1.2k buy two minutes earlier."""
    try:
        row = next((r for r in reversed(view.get("gate_evidence") or [])
                    if r.get("name") == "source_buy_floor"), None)
        prior = float((row or {}).get("prior_net_usd") or 0.0)
        judged = float((row or {}).get("judged_usd") or 0.0)
        if prior > 0 and judged > prior:
            return " (+%s earlier = %s)" % (_fmt_usd(prior), _fmt_usd(judged))
    except (TypeError, ValueError):
        pass
    return ""


_SOURCE_WORDS = {"fastlane_mid": "DexScreener", "dexscreener": "DexScreener",
                 "paper_dexscreener": "DexScreener (paper)",
                 "fastlane_sol_swap": "swap", "fastlane_sol_state": "curve state"}


def _decision_seconds(later, earlier):
    """Seconds from `earlier` to `later` (ISO stamps), or None. Named apart from the entry card's
    `_seconds_between`, whose argument order is different."""
    from datetime import datetime
    try:
        return (datetime.fromisoformat(str(later).replace("Z", "+00:00"))
                - datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError):
        return None


def exit_decision_text(decision):
    """One line naming what decided an exit (2026-09-27): the exact route quote a price rail
    confirmed, or vol_death's on-chain proof; a market mid shown beside it is labelled as display
    with its ages. None when no decision row exists (the line is then left out)."""
    if not isinstance(decision, dict):
        return None
    if decision.get("decision_basis") == "onchain":
        return f"Decided on on-chain silence ({decision.get('decision_detail') or 'vol_death'})"
    exact = decision.get("decision_pnl_pct")
    if decision.get("decision_basis") != "route" or exact is None:
        return None
    text = f"Decided on exact route quote {float(exact):+.1f}%"
    shown = decision.get("display_pnl_pct")
    if shown is not None and decision.get("display_basis") == "market":
        words = _SOURCE_WORDS.get(decision.get("display_source"), decision.get("display_source"))
        ages = []
        if decision.get("display_age_s") is not None:
            ages.append(f"changed {float(decision['display_age_s']):.0f}s")
        checked = _decision_seconds(decision.get("decided_at"), decision.get("display_checked_at"))
        if checked is not None:
            ages.append(f"checked {max(checked, 0.0):.0f}s")
        when = f", {', '.join(ages)} before" if ages else ""
        text += f" · market {float(shown):+.1f}% ({words}{when}; display only)"
    elif shown is not None and decision.get("display_basis") is None:
        text += f" · shown mid {float(shown):+.1f}% (source and age not recorded; display only)"
    return text


def render_entry_notification(view):
    """One phone-sized message per result (2026-09-11 rework of the 16-line layout).

    Order is what the operator reads first: what happened, who bought, what we did, why, the
    wallet's record, how fast, then at most four links. Internal identities stay on the
    dashboard; a short `ref` keeps the message traceable to the attempt."""
    from sniper.services.memecoin_chains import chain_label, resolve_chain
    from sniper.services.memecoin_entry_view import LABELS
    from sniper.services.memecoin_links import operator_links
    exit_msg = view.get("message_kind") == "exit"
    status = "sold" if exit_msg else (view.get("status") or "needs_review")
    label = "Sold" if exit_msg else LABELS.get(status, status.replace("_", " "))
    chain = chain_label(resolve_chain(view.get("network_id")) or view.get("network_id") or "Unknown")
    token = view.get('ticker') or _short_mint(view.get('mint')) or 'Unknown token'
    lines = [f"{_STATUS_MARK.get(status, '•')} {label.upper()} · {token} · {chain}"]

    manual = view.get("signal_source") in {"manual", "manual_buy", "operator"} or not view.get("origin_event_id")
    if view.get("signal_source") == "desk":
        # The desk seat decided; there is no source wallet or source size to show.
        who = f"Desk ticket {str(view.get('origin_event_id') or '').removeprefix('desk:') or 'unknown'}"
    elif manual:
        who = "Operator request" if view.get("attempt_id") else "Source unknown"
    else:
        size = view.get("source_size_usd")
        who = (f"{view.get('source_handle') or view.get('source_user_id') or 'Unknown wallet'} bought "
               f"{_fmt_usd(size) if size is not None else 'unknown size'}{_earlier_suffix(view)}")
    now_state = (view.get("current_authority") or {}).get("state")
    then = view.get("authority") or {}
    then_state = then.get("state") or then.get("status") or "unknown"
    if exit_msg:
        who = None
    elif not manual and view.get("signal_source") != "desk":     # a desk ticket arms no wallet
        if now_state:
            phrase = {"armed": f"armed on {chain}", "not_armed": f"not armed on {chain}",
                      "unfunded_chain": f"{chain} not funded"}.get(now_state, now_state.replace("_", " "))
            if then_state not in ("unknown", now_state):
                phrase += f" (was {then_state.replace('_', ' ')} at decision)"
            who += f" · {phrase}"
        elif then_state != "unknown":
            who += f" · {then_state.replace('_', ' ')} at decision"
    if who:
        lines.append(who)

    if exit_msg:
        why = view.get("reason") or "exit"
        sold = view.get("mirror_sold_fraction")
        if why == "mirror" and isinstance(sold, (int, float)):
            why = f"trader sold {100 * sold:.0f}% of their bag"
        landing = view.get("rabbit_landing")
        if isinstance(landing, dict):
            lead, age = landing.get("lead_ms"), landing.get("age_ms")
            why = (f"{why} (rabbit lead {float(lead):.0f} ms)" if isinstance(lead, (int, float)) else
                   f"{why} (no source landing {float(age):.0f} ms after the rabbit frame)"
                   if isinstance(age, (int, float)) else f"{why} (rabbit lead unknown)")
        share = view.get("sold_share") if isinstance(view.get("sold_share"), dict) else None
        share_text = ("" if share is None else
                      f"sold {'remaining ' if share.get('remaining') else ''}{100 * float(share['share']):.0f}% · ")
        sale = (f"Our sale: {share_text}{_notification_usd(view.get('filled_notional'))} @ "
                f"{_notification_at(view.get('filled_price'), view.get('display_supply'))} · {why}")
        pnl, cost = view.get("realized_pnl"), view.get("entry_notional")
        if pnl is not None and view.get("position_closed"):
            sale += f" · P&L {'+' if pnl >= 0 else '-'}${abs(float(pnl)):,.2f}"
            if cost:
                sale += f" ({100.0 * float(pnl) / float(cost):+.1f}%)"
        lines.append(sale)
        decided = exit_decision_text(view.get("exit_decision"))
        if decided:
            lines.append(decided)
    elif status == "filled":
        fill = (f"Our fill: {_notification_usd(view.get('filled_notional'))} @ "
                f"{_notification_at(view.get('filled_price'), view.get('display_supply'))}")
        if view.get("position_id"):
            fill += f" · pos #{view['position_id']}"
        lines.append(fill)
    elif view.get("requested_usd") is not None:
        lines.append(f"Our size: {_notification_usd(view['requested_usd'])}")
    if not exit_msg and status != "filled":
        reason = view.get("reason") or label
        if reason == "float_short":
            native = {"56": "BNB", "4663": "ETH", "solana": "SOL"}.get(str(view.get("network_id")), "coin")
            reason = f"native {native} funding buffer cannot cover approved input plus gas"
        held = view.get("held_position") if status == "refused" else None
        words = _slot_lane_words(view) or _unwrap_not_visible_words(view) or _oversize_words(view) or _unknown_source_size_words(reason) or _impact_cap_words(view) or _kill_deploy_words(view) or _REASON_WORDS.get(reason, reason)
        if held:
            from datetime import datetime
            stamp = held.get("opened_recorded_at") or held.get("opened_at")
            try:
                aware = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).tzinfo is not None
            except (TypeError, ValueError):
                aware = False
            bought = _notification_time(stamp) if aware else None
            detail = (f"bought {bought}Z, " if bought else "") + f"pos {held['id']}"
            words = f"add-on refused: already holding ({detail}); {words}"
        lines.append(f"Why: {words}")

    if view.get("addon"):                                   # memecoin_addon_label: a label, never a gate
        from sniper.services.memecoin_addon_label import card_words
        words = card_words(view["addon"])
        if words:
            lines.append("Add-on: " + words)

    if view.get("historical_result_correction"):
        lines.append("Historical result correction; original execution time shown below.")

    gate_rows = view.get("gate_evidence") or []
    rt = next((r for r in reversed(gate_rows) if r.get("name") == "roundtrip_sequential" and r.get("used") is True), None)
    rt = rt or next((r for r in reversed(gate_rows) if r.get("name") == "roundtrip_cap"), None)
    reason_text = str(view.get("reason") or "").lower()
    if status in ("filled", "sold"):
        rt = None                                   # the gate passed; its numbers are dashboard detail
    if rt and rt.get("cost_pct") is not None and "round trip" not in reason_text:
        cap = rt.get("cap_pct")
        cap_off = cap is None or float(cap) >= 100      # a round trip can never cost more than 100%
        sequential = rt.get("name") == "roundtrip_sequential" and rt.get("used") is True
        basis = "sequential_simulation" if sequential else rt.get("basis") or "unknown basis"
        # cost_pct = (1 - sold back / paid) x 100: negative is a GAIN. "-4.09% · sells back $182.16"
        # on a $175 clip read as a loss (2026-09-12); say which way it went.
        cost = float(rt['cost_pct'])
        text = (f"Round trip costs {cost:.2f}%" if cost >= 0 else f"Round trip gains {-cost:.2f}%") + (
            " · cap disabled" if cap_off else f" vs cap {float(cap):.2f}%")
        back = rt.get("scaled_sell_usd")
        measurement = rt.get("evidence") or {}
        if sequential and measurement.get("sell_received_atomic") is not None:
            back = int(measurement["sell_received_atomic"]) / 1_000_000
        if back is not None:
            text += f" · sells back ${float(back):.2f}"
        # A passing check shown on a refusal card read as a second refusal (SEND 2026-09-17). Say it passed.
        passed = (not cap_off) and cost <= float(cap)
        lines.append(text + f" ({basis})" + (" · passed" if passed else ""))
    chase = next((r for r in reversed(gate_rows) if r.get("name") == "chase_quote_confirmation"), None)
    if chase and status == "refused" and "above source price" in reason_text:
        basis = {"avg_fill": "source average fill", "post_trade": "source post-trade price",
                 "post_trade_clamped": "bounded source post-trade price"}.get(chase.get("reference_basis"))
        if basis:
            lines.append(f"Price reference: {basis}")
    for row in gate_rows:
        if (status == "refused" and row.get("name") == "volume_gate"
                and row.get("allowed") is False and row.get("tested_volume_m5") is not None
                and row.get("threshold") is not None):
            lines.append(f"Volume m5: ${row['tested_volume_m5']:,.0f} vs ${row['threshold']:,.0f} "
                         f"({row.get('measurement_basis') or 'unknown basis'})")

    if status == "shadow":
        for result in (view.get("fast_lane_results") or ())[:2]:
            lines.append(f"Fast lane: {result.get('status') or 'evaluating'} · "
                         f"{result.get('reason') or 'result pending'}")

    if status in {"refused", "failed_before_submission", "transaction_failed"}:
        from collections import Counter
        earlier = Counter()
        for attempt in view.get("attempts") or ():
            elapsed = _seconds_between(attempt.get("completed_at"), view.get("decision_started_at"))
            if (attempt.get("attempt_id") != view.get("attempt_id")
                    and attempt.get("terminal_status") in {"refused", "aborted", "failed"}
                    and elapsed is not None and elapsed >= 0):
                earlier[str(attempt.get("terminal_reason") or "unknown")] += 1
        if earlier:
            detail = ", ".join(f"{reason} ({count})" for reason, count in earlier.most_common(3))
            minimum = "at least " if view.get("attempts_truncated") else ""
            lines.append(f"Earlier failures: {minimum}{sum(earlier.values())} · {detail}")

    src, got, done = view.get("source_created_at"), view.get("socket_received_at"), view.get("status_updated_at")
    processed = view.get("observed_at")
    if view.get("decision_started_at"):
        # j7_receipts belongs to a separate capture process. It is not this runner's receipt.
        got, processed = view.get("observed_at"), view["decision_started_at"]
    anchor = _notification_time(src or got or processed or done)
    chain = view.get("chain_timing") or {}
    if anchor or chain.get("text"):
        source_label = ("ticket" if view.get("signal_source") == "desk" else
                        {"claimed_untrusted": "source (unverified)",
                         "observed_detection": "detected"}.get(view.get("source_timestamp_trust"), "source"))
        timing = f"Times (UTC): {source_label if src else 'received' if got else 'processed' if processed else 'result'} {anchor}"
        # A NEGATIVE INTERVAL IS A CLOCK, NOT A DURATION (2026-09-18, POOL ref f500c417).
        # The card read "+-9.6s processed" because the J7 relay's clock has run ahead of ours since
        # 2026-09-17T21:00Z — its stamp lands after our receipt, so the subtraction goes negative.
        # Elapsed time cannot be negative, so printing one as a duration asks the operator to
        # believe we acted before the event. Say which clock disagreed instead, once per line.
        # WHICH TWO STAMPS DISAGREE (inherited review finding O3). Source-versus-ours is the
        # relay's clock running ahead. Receipt-versus-processed are BOTH our stamps, so a negative
        # interval there is two local roles out of order, not a source clock, and the order stays
        # unknown; the card names the roles and never asserts "processed after receipt".
        # ZERO AT THE CARD'S RESOLUTION (2026-09-30). The card prints tenths. A fast lane stamps
        # its decision start milliseconds before the detection row, and that read "stamps disagree
        # by 0.0s, order unknown". An interval that prints as 0.0 is zero, not a disagreement.
        def _snap(value):
            return 0.0 if value is not None and value < 0 and f"{-value:.1f}" == "0.0" else value

        def _interval(a, b, label, *, a_role):
            value = _snap(_seconds_between(a, b)) if a and b else None
            if value is None:
                return "", False
            if value < 0:
                if a_role == "source":
                    return f" · {label} unknown (source clock ahead by {abs(value):.1f}s)", True
                return (f" · {label} unknown ({a_role}/{label} stamps disagree by "
                        f"{abs(value):.1f}s, order unknown)"), True
            return f" · +{value:.1f}s {label}", False

        if chain.get("text"):
            # CHAIN TIME ANCHORS THE LINE (D-a, 2026-10-16): the source block's timestamp, else our
            # earliest detection; the J7 stamp is an offset from it, never the anchor (the entry
            # view's `_chain_times` says why). Local processing starts at this runner's receipt.
            timing = "Times (UTC): " + chain["text"]
        else:
            # Legacy source-only cards have a detection stamp but no local decision clock.
            received = got or (processed if not view.get("decision_started_at") else None)
            timing += _interval(src, received, "received", a_role="source")[0]
        if processed:
            timing += _interval(got, processed, "processed", a_role="receipt")[0] or " · processed unknown"
        step = _snap(_seconds_between(got or processed or src, done)) if done else None
        elapsed = view.get("decision_elapsed_ms") if not exit_msg else None
        if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
            timing += f" · {elapsed / 1000:.3f}s attempt duration"
        elif step is not None and step >= 0:
            baseline = "receipt" if got else "processed" if processed else "source"
            timing += f" · {step:.1f}s {baseline} to result"
        elif step is not None:
            timing += " · result time unknown (clocks disagree)"
        if step is None and not done and status not in ("source_received", "evaluating"):
            timing += " · result time unknown"
        lines.append(timing)
    if view.get("fill_timing_line") and not exit_msg and not _unwrap_not_visible_words(view):
        # Which slot we landed in and why not S+1 (fill timing, 2026-10-02).
        lines.append(view["fill_timing_line"])
    if view.get("retry_next_at"):
        lines.append(f"Next retry (UTC): {_notification_time(view['retry_next_at']) or view['retry_next_at']}")

    # TELEGRAM HTML (2026-09-11): every text line is escaped, and the links collapse to one line
    # of short names instead of three lines of raw URLs. Sent with parse_mode=HTML by the worker.
    import html as _html
    body = [_html.escape(line, quote=False) for line in lines]
    links = view.get("links") or operator_links(view.get("network_id"), view.get("mint"),
        handle=view.get("source_handle"), tx_sig=view.get("tx_sig"),
        origin_event_id=view.get("origin_event_id"), position_id=view.get("position_id"))
    named = []
    for key, title in (("transaction", "Tx"), ("chart", "Chart"), ("trader", "Trader"),
                       ("entry", "Dashboard")):
        url = links.get(key)
        if url and url.startswith("/"):
            url = _public_dashboard_url(url)
        if url and url.startswith("https://"):
            named.append(f'<a href="{_html.escape(url, quote=True)}">{title}</a>')
    if named:
        body.append(" · ".join(named))
    if view.get("attempt_id"):
        body.append(_html.escape(f"ref {str(view['attempt_id'])[:8]}", quote=False))
    return "\n".join(body)


def _send_notification_html(text):
    """Entry/exit notifications are rendered as Telegram HTML (escaped text + named links)."""
    try:
        return send_telegram(text, parse_mode="HTML")
    except TypeError:
        # a test seam that replaces send_telegram with a text-only callable
        return send_telegram(text)


class _NotificationWorker:
    """Own the notification database handles and all source/result preparation."""

    def __init__(self, *, activation_at=None):
        from sniper.persistence.db_core import get_connection, get_readonly_connection
        from sniper.persistence.db_memecoin_notifications import NotificationLifecycle, scan_notification_views
        from sniper.util.state_paths import state_path
        configured = os.getenv("DB_PATH")
        path = configured if configured and os.path.isabs(configured) else str(state_path("sniper_tracker.db"))
        self.write_conn = get_connection(path)
        try:
            self.read_conn = get_readonly_connection(path)
            self.lifecycle = NotificationLifecycle(self.write_conn, read_view=self._read,
                send_fn=_send_notification_html, render_fn=self._render, eligible_fn=self._eligible,
                now_fn=_real_utc_now_iso, audit_fn=self._audit)
            self.lifecycle.activate(activation_at)
            self.lifecycle.recover_interrupted()
            scan_notification_views(self.lifecycle, self.read_conn, self._read)
            self._last_scan = 0.0
            self._last_unresolved_scan = 0.0
        except Exception:
            self.write_conn.rollback()
            self.write_conn.close()
            if getattr(self, "read_conn", None) is not None:
                self.read_conn.close()
            raise

    def _read(self, *, origin_event_id=None, attempt_id=None, event=None, fill_id=None):
        from sniper.services.memecoin_entry_view import entry_view
        from sniper.persistence.db_memecoin_notifications import notification_exit_view, notification_fill_identity
        if fill_id is not None:
            exit_view = notification_exit_view(self.read_conn, fill_id)
            if exit_view:
                return exit_view
            identity = notification_fill_identity(self.read_conn, fill_id)
            return entry_view(self.read_conn, **identity) if identity else None
        return entry_view(self.read_conn, origin_event_id=origin_event_id, attempt_id=attempt_id, event=event)

    def _eligible(self, view):
        from types import SimpleNamespace
        from sniper.persistence import db_memecoin as m
        from sniper.persistence.db_memecoin_notifications import UNRESOLVED, PENDING
        from sniper.settings import memecoin as cfg
        from sniper.services.memecoin_runner import want_front_run_alert
        uid, net = view.get("source_user_id"), view.get("network_id")
        if view.get("status") not in PENDING:
            if view.get("message_kind") == "exit" or view.get("status") in UNRESOLVED | {"filled", "transaction_failed"}:
                return True
            if view.get("requested_usd") is not None:
                return True
        # OPERATOR 2026-09-11: "Don't send notif for disarmed wallets." A wallet that is not armed
        # on THIS chain never pages -- not its buy, not its not_approved refusal. Anything we
        # actually did (a reservation, a fill, an exit, an unresolved transaction) still pages
        # above, whoever the wallet is. Manual requests carry no wallet and are unaffected.
        if uid and uid not in m.real_approved_user_ids(self.read_conn, network_id=net):
            return False
        if view.get("status") not in PENDING:
            return view.get("side") in (None, "buy")
        if not cfg.MEMECOIN_TG_ALERTS:
            return False
        from sniper.persistence.db_memecoin_notifications import _epoch
        source_at, observed = _epoch(view.get("source_created_at")), _epoch(view.get("observed_at"))
        if source_at is not None and observed is not None and 0 < cfg.MEMECOIN_TG_MAX_LAG_S < observed - source_at:
            return False
        event = SimpleNamespace(source_user_id=uid, network_id=net, side=view.get("side"),
            their_size_usd=view.get("source_size_usd"), their_market_cap=view.get("source_market_cap"),
            mint=view.get("mint"), created_at=view.get("source_created_at"))
        return want_front_run_alert(event, m.curated_user_ids(self.read_conn), prior_fn=lambda ev:
            m.prior_buyers_60m(self.read_conn, mint=ev.mint, network_id=net,
                              source_user_id=uid, before_iso=ev.created_at))

    def _source_intent(self, view):
        from sniper.persistence import db_memecoin as m
        if not view.get("origin_event_id"):
            return
        m.record_alert(self.write_conn, {
            "fomo_event_id": view["origin_event_id"], "source_user_id": view.get("source_user_id"),
            "handle": view.get("source_handle"), "mint": view.get("mint"), "ticker": view.get("ticker"),
            "their_size_usd": view.get("source_size_usd"), "market_cap": view.get("source_market_cap"),
            "fomo_created_at": view.get("source_created_at"), "detected_at": view.get("observed_at"),
            "network_id": view.get("network_id")})

    def _audit(self, fields):
        from sniper.persistence import db_memecoin as m
        m.record_telegram_delivery(self.write_conn, fields)
        if fields.get("message_kind") == "source" and fields.get("attempt", 1) == 1:
            m.update_alert_sent(self.write_conn, fields["source_id"], fields["completed_at"],
                fields["send_ok"], fields["send_ms"],
                rendered_text_redacted=fields["rendered_text_redacted"], message_sha256=fields["message_sha256"])

    def _render(self, view):
        from sniper.persistence import db_memecoin as m
        from sniper.services.memecoin_chains import funded_chain_keys, resolve_chain
        from sniper.settings import memecoin as cfg
        view = dict(view)
        if view.get("status") == "refused":
            try:
                view["held_position"] = m.notification_held_position(self.read_conn, view.get("network_id"), view.get("mint"))
            except Exception:
                logger.warning("memecoin notification held position unavailable", exc_info=True)
        # A DEPLOY'S OWN KILL-SWITCH TRIP (Alpha 2026-10-07): the hold that covered the decision, read
        # from the switch's audit log for the card's words only. First, before the slower reads.
        if view.get("reason") == "kill_switch_tripped":
            try:
                from sniper.services import memecoin_arming as arming
                view["kill_deploy_restart"] = arming.kill_deploy_restart_at(
                    view.get("decision_started_at"), now_iso=_real_utc_now_iso())
            except Exception:
                logger.warning("memecoin notification kill-switch hold unavailable", exc_info=True)
        # THE COIN'S NAME, NOT ITS MINT (2026-09-28, operator). Slot-lane and on-chain fills carry
        # no ticker. This is the worker's own thread, so the name lookup may wait here (DexScreener's
        # 6 s deadline, names only); the trading threads never do. See memecoin_coin_names.
        # The name is then written onto the position (only where its ticker is blank), so the
        # dashboard, later alerts and the closed history keep it across restarts.
        if view.get("mint") and not str(view.get("ticker") or "").strip():
            try:
                from sniper.services import memecoin_coin_names as coin_names
                name = coin_names.name_for(self.read_conn, view.get("network_id"), view["mint"], wait=True)
                if name:
                    view["ticker"] = name
                    if view.get("position_id") is not None:
                        m.set_position_ticker(self.write_conn, view["position_id"], name)
            except Exception:
                logger.warning("memecoin notification coin name unavailable", exc_info=True)
        # The fill price reads as a market cap when the position's supply is known.
        if view.get("position_id") is not None and view.get("display_supply") is None:
            try:
                view["display_supply"] = m.position_display_supply(
                    self.read_conn, m.get_position(self.read_conn, view["position_id"]))
            except Exception:
                logger.warning("memecoin notification supply unavailable", exc_info=True)
        from sniper.persistence.db_memecoin_notifications import PENDING
        if view.get("status") in PENDING:
            self._source_intent(view)
        uid, net = view.get("source_user_id"), view.get("network_id")
        try:
            funded = resolve_chain(net) in funded_chain_keys()
            armed = uid in m.real_approved_user_ids(self.read_conn, network_id=net)
            view["current_authority"] = {"state": "armed" if armed else "not_armed" if funded else "unfunded_chain"}
        except Exception:
            logger.warning("memecoin notification wallet evidence unavailable", exc_info=True)
        return render_entry_notification(view)

    def cycle(self, job=None):
        try:
            return self._cycle(job)
        except Exception:
            self.write_conn.rollback()
            raise

    def _cycle(self, job=None):
        from sniper.persistence.db_memecoin_notifications import scan_notification_views
        from sniper.services.memecoin_entry_view import recent_entry_views
        if job:
            if job.get("event") is not None:
                current = self._read(event=job["event"])
                self.lifecycle.observe(current)
            else:
                current = self._read(origin_event_id=job.get("origin_event_id"), attempt_id=job.get("attempt_id"))
                self.lifecycle.observe(current)
        now = time.monotonic()
        if now - self._last_scan >= 2:
            scan_notification_views(self.lifecycle, self.read_conn, self._read, limit=100)
            for view in recent_entry_views(self.read_conn, limit=100):
                self.lifecycle.observe(view)
            self.lifecycle.refresh_tracked(limit=100)
            if now - self._last_unresolved_scan >= 30:
                from sniper.persistence.db_memecoin_notifications import scan_unresolved_notification_views
                scan_unresolved_notification_views(self.lifecycle, self.read_conn, self._read, limit=100)
                self._last_unresolved_scan = now
            self._last_scan = now
        self.lifecycle.deliver(limit=5)
