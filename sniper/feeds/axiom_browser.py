"""Axiom capture worker (spec docs/superpowers/specs/2026-09-16-axiom-observation-lanes-design.md).

A headless Chromium on a profile the operator logged into by hand. The worker opens the pages a
slice needs and records what the app itself receives: every WebSocket frame and every JSON
response from *.axiom.trade. It types nothing, clicks nothing that changes state, and never
imports the funded entry hook. `CaptureCore` holds every decision and is tested without a
browser; `run_capture` is the thin Playwright glue.

Fail closed: an HTTP 403, a Cloudflare challenge, a login page, or three empty reloads in a row
writes the halt file, pages the operator once, and stops. The worker never retries into a block.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PAGES = {
    # ink and arc added 2026-09-16 (operator: Ink feed via Axiom; Arc is our network_id 5042).
    # The app's own chain selector decides which *-api hosts answer, so this URL IS the feed switch.
    "trackers": "https://axiom.trade/trackers?chains=sol,robinhood,bnb,base,ink,arc",
    "discover": "https://axiom.trade/discover?chains=sol,robinhood,bnb,base,ink,arc",
    "pulse": "https://axiom.trade/pulse",
}
HALT_MARKERS = ("challenges.cloudflare.com", "/login", "turnstile")
EMPTY_RELOADS_TO_HALT = 3


def default_profile_dir(env=None):
    """OUTSIDE the repo, like the X fixture profile: the session cookies never sit in a checkout."""
    src = os.environ if env is None else env
    raw = (src.get("AXIOM_PROFILE_DIR") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".local" / "share" / "sniper" / "axiom_profile"


def _sensitive_response(url):
    return urlsplit(url).path.rsplit('/', 1)[-1].startswith('bundle-key-and-wallets')


def _strip_query(url):
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}{u.path}"


class CaptureCore:
    """Every decision of the worker, with the browser abstracted to plain calls."""

    def __init__(self, conn, *, now_iso_fn, mono_ns_fn=time.monotonic_ns, alert_fn=None,
                 heartbeat_path=None, halt_path=None, parsers=None):
        from sniper.persistence import db_axiom as dba
        self._db = dba
        self.conn = conn
        self.now_iso = now_iso_fn
        self.mono_ns = mono_ns_fn
        self.alert = alert_fn
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else None
        self.halt_path = Path(halt_path) if halt_path else None
        self.parsers = parsers                      # sniper.feeds.axiom_parse or None
        self.halted = None
        if self.halt_path and self.halt_path.exists():
            try:
                self.halted = json.loads(self.halt_path.read_text()).get('reason') or 'saved_halt'
            except (OSError, ValueError, AttributeError):
                self.halted = 'unreadable_halt_file'
        from sniper.util.receipt_clock import clock_domain
        self.clock_domain = clock_domain()
        self.capture_run_id = str(uuid.uuid4())
        self.parser_version = getattr(parsers, 'PARSER_VERSION', 'axiom-capture-v2')
        self.stop_requested = False
        self.frames_total = 0
        self.rest_total = 0
        self.parse_refused = 0
        self.http_429 = 0
        self.http_errors = 0
        self.page_403 = 0
        self.ws_open = 0
        self.ws_closed = 0
        self.tabs_reopened = 0                       # run_capture sets -len(pages): the first open is not a reopen
        self.ws_events = []                          # (at, kind, socket, page, error) recent, bounded
        self.page_backoff_ns = 300_000_000_000        # first page-403 pause: 5 min, then doubling
        self.backoff_until_mono = 0
        self.last_frame_at = None
        self._empty_reloads = 0
        self._window = []                            # (mono_ns) of frames, for ws_frames_60s
        self.commit_every_ns = 1_000_000_000         # Batch raw-only writes; release parsed writes before idle.
        self._last_commit = self.mono_ns()

    def _commit_if_due(self, force=False):
        if force or self.mono_ns() - self._last_commit >= self.commit_every_ns:
            self.conn.commit()
            self._last_commit = self.mono_ns()

    # ---- sockets: open/close are S4 disconnect-gap evidence and the diagnosis of a quiet feed
    def on_ws_open(self, socket_url, *, page):
        self.ws_open += 1
        self.ws_events.append((self.now_iso(), "open", _strip_query(socket_url), page, None))
        logger.info("ws open %s (%s)", _strip_query(socket_url), page)

    def on_ws_close(self, socket_url, *, page, error=None):
        self.ws_closed += 1
        self.ws_events.append((self.now_iso(), "error" if error else "close", _strip_query(socket_url), page, error))
        logger.warning("ws %s %s (%s)%s", "error" if error else "close", _strip_query(socket_url), page,
                       (" " + error) if error else "")

    # ---- recording -------------------------------------------------------------------------
    def on_ws_frame(self, socket_url, payload, *, page):
        mono, at = self.mono_ns(), self.now_iso()
        raw_id = self._db.insert_raw(self.conn, received_mono_ns=mono, received_at=at, transport="ws",
                                     source=_strip_query(socket_url), page=page, payload=payload)
        self.frames_total += 1
        self.last_frame_at = at
        self._window.append(mono)
        if self.parsers is not None:
            try:
                ev = self.parsers.parse_wallet_event(payload)
            except Exception:
                logger.exception("parse_wallet_event raised; raw row %s kept", raw_id)
                ev = None
            if ev:
                try:
                    self._db.insert_wallet_event(self.conn, ev, received_mono_ns=mono, received_at=at,
                                                 raw_id=raw_id, transport='ws', observation_kind=ev.get('observation_kind', 'unknown'),
                                                 clock_domain=self.clock_domain, capture_run_id=self.capture_run_id,
                                                 parser_version=self.parser_version)
                finally:
                    self._commit_if_due(force=True)
        if self.parsers is not None:
            try:
                try:
                    frame = json.loads(payload)
                except (ValueError, TypeError):
                    frame = None
                room = frame.get('room', '') if isinstance(frame, dict) else ''
                if isinstance(room, str) and room.split('|')[-1] == 'gmgn-callouts':
                    rows = self.parsers.parse_kol_callouts(payload)
                    self._db.insert_kol_callouts(self.conn, rows, at, raw_id=raw_id)
                    self._commit_if_due(force=True)
                elif isinstance(room, str) and ':trending:' in room:
                    rows = self.parsers.parse_discovery(payload)
                    self._db.insert_discovery(self.conn, rows, at)
                    self._commit_if_due(force=True)
            except Exception:
                self.parse_refused += 1
                logger.exception('WS research parser failed; raw row %s kept', raw_id)
        self._commit_if_due()
        return raw_id

    def on_rest_response(self, url, status, body_text, *, page, received_mono_ns=None, received_at=None, resource_type=None):
        """`received_mono_ns` is taken by the caller at callback entry, BEFORE the body read; a slow
        body must not age the receipt stamp (Codex review, 2026-09-16)."""
        if _sensitive_response(url):
            return None
        if status == 403:
            host = (urlsplit(url).hostname or "")
            if host == "axiom.trade" and resource_type == "document":
                # A 403 on the PAGE DOCUMENT is Cloudflare rate-limiting reloads (live 2026-09-16
                # 08:05Z at a 30 s cadence; the session itself stayed logged in). Back off the
                # reload interval instead of halting; push data keeps flowing on the open tabs.
                self.page_403 += 1
                self.backoff_until_mono = self.mono_ns() + self.page_backoff_ns
                self.page_backoff_ns = min(self.page_backoff_ns * 2, 3_600_000_000_000)
                logger.warning("axiom page 403 on %s; reloads paused %.0fs (total %d)",
                               _strip_query(url), self.page_backoff_ns / 2e9, self.page_403)
                return None
            self.halt("http_403 " + _strip_query(url))   # an API 403 is an account-level block
            return None
        if status == 429:
            # Axiom pushing back at this cadence. Counted and shown in the heartbeat; the operator
            # owns AXIOM_PAGE_INTERVAL_S and asked for full speed, so this is their signal to slow.
            self.http_429 += 1
            logger.warning("axiom 429 on %s (total %d)", _strip_query(url), self.http_429)
            return None
        if status >= 400:
            self.http_errors += 1
            return None
        if not body_text:
            return None
        mono = self.mono_ns() if received_mono_ns is None else int(received_mono_ns)
        at = received_at or self.now_iso()
        sha = hashlib.sha256(body_text.encode("utf-8", "replace")).hexdigest()
        source = _strip_query(url)
        raw_id = self._db.insert_raw(self.conn, received_mono_ns=mono, received_at=at, transport="rest",
                                     source=source, page=page, payload=body_text, body_sha=sha)
        if raw_id is not None:
            self.rest_total += 1
        parse_raw_id = raw_id if raw_id is not None else self._db.raw_id_for_body(self.conn, source, sha)
        self._parse_rest(source, body_text, at, parse_raw_id, mono)
        self._commit_if_due()
        return raw_id

    # REST endpoint basename -> (parser name, writer). A parser that refuses returns [] and the raw
    # row stays; nothing here reads a positional field the parser has not named.
    REST_PARSERS = {
        "tracked-wallets-v4": ("parse_tracked_wallets", "directory"),
        "smart-money-traders": ("parse_trader_directory", "directory"),
        "discover-trending": ("parse_discovery", "discovery"),
        "sol-new-trending": ("parse_discovery", "discovery"),
        "gmgn-callouts-feed": ("parse_kol_callouts", "kol"),
        "callouts-feed": ("parse_kol_callouts", "kol"),
        "tracked-wallet-transactions-v4": ("parse_wallet_transactions", "events"),
    }

    def _parse_rest(self, source, body_text, at, raw_id, mono):
        if self.parsers is None:
            return
        name = source.rsplit("/", 1)[-1]
        spec = self.REST_PARSERS.get(name)
        if not spec:
            return
        fn = getattr(self.parsers, spec[0], None)
        if fn is None:
            return
        try:
            # Discovery and wallet parsers obtain the chain from the source host.
            rows = (fn(body_text, source_url=source)
                    if spec[0] in ("parse_discovery", "parse_wallet_transactions") else fn(body_text)) or []
            if spec[1] == "directory":
                n = self._db.upsert_directory(self.conn, rows, at)
            elif spec[1] == "discovery":
                n = self._db.insert_discovery(self.conn, rows, at)
            elif spec[1] == "kol":
                n = self._db.insert_kol_callouts(self.conn, rows, at, raw_id=raw_id)
            else:
                n = 0
                for ev in rows:
                    if not (ev.get("address") and ev.get("chain") and ev.get("mint") and ev.get("side")):
                        continue               # an unresolved identity is no event (mint from the image stem)
                    self._db.insert_wallet_event(self.conn, ev, received_mono_ns=mono,
                                                 received_at=at, raw_id=raw_id, transport='rest',
                                                 observation_kind='history', clock_domain=self.clock_domain,
                                                 capture_run_id=self.capture_run_id, parser_version=self.parser_version)
                    n += 1
            if n == 0:
                self.parse_refused += 1
        except Exception:
            self.parse_refused += 1
            logger.exception("REST parser %s failed on %s", spec[0], name)
        finally:
            self._commit_if_due(force=True)

    def on_page_loaded(self, page, final_url, html, *, frames_since_reload, responses_since_reload):
        """After each reload: halt checks, then the page parsers on the rendered HTML."""
        low = (final_url or "").lower()
        if any(m in low for m in HALT_MARKERS) or (html and "challenges.cloudflare.com" in html):
            self.halt("challenge_or_login " + _strip_query(final_url or ""))
            return
        if frames_since_reload == 0 and responses_since_reload == 0:
            self._empty_reloads += 1
            if self._empty_reloads >= EMPTY_RELOADS_TO_HALT:
                self.halt("empty_reloads %d" % self._empty_reloads)
                return
        else:
            self._empty_reloads = 0
        if self.parsers is None or not html:
            return
        at = self.now_iso()
        try:
            if page == "trackers":
                n = self._db.upsert_directory(self.conn, self.parsers.parse_trader_directory(html), at)
                n += self._db.insert_kol_callouts(self.conn, self.parsers.parse_kol_callouts(html), at)
            elif page in ("discover", "pulse"):
                n = self._db.insert_discovery(self.conn, self.parsers.parse_discovery(html), at)
            else:
                n = 0
            if n == 0:
                self.parse_refused += 1
        except Exception:
            self.parse_refused += 1
            logger.exception("page parser failed on %s", page)
        finally:
            self._commit_if_due(force=True)

    # ---- state -----------------------------------------------------------------------------
    def halt(self, reason):
        if self.halted:
            return
        self.halted = reason
        logger.error("axiom capture HALTED: %s", reason)
        self._commit_if_due(force=True)
        if self.halt_path:
            self.halt_path.parent.mkdir(parents=True, exist_ok=True)
            self.halt_path.write_text(json.dumps({"reason": reason, "at": self.now_iso()}))
        if self.alert:
            try:
                self.alert("axiom capture halted: %s (no retry; check the session)" % reason)
            except Exception:
                logger.exception("halt alert failed")

    def write_heartbeat(self, *, pages_open):
        if not self.heartbeat_path:
            return
        cut = self.mono_ns() - 60_000_000_000
        self._window = [m for m in self._window if m >= cut]
        del self.ws_events[:-64]
        body = {"ts": time.time(), "at": self.now_iso(), "pages_open": pages_open,
                "ws_frames_60s": len(self._window), "frames_total": self.frames_total,
                "rest_total": self.rest_total, "last_frame_at": self.last_frame_at,
                "parse_refused": self.parse_refused, "http_429": self.http_429,
                "http_errors": self.http_errors, "page_403": self.page_403,
                "ws_open": self.ws_open, "ws_closed": self.ws_closed,
                "tabs_reopened": max(0, self.tabs_reopened),
                "ws_recent": self.ws_events[-8:],
                "reloads_paused_s": max(0, (self.backoff_until_mono - self.mono_ns()) // 1_000_000_000),
                "halted": self.halted, "clock_domain": self.clock_domain,
                "capture_run_id": self.capture_run_id, "parser_version": self.parser_version,
                "subscription_coverage": "unknown"}
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.heartbeat_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body))
        os.replace(tmp, self.heartbeat_path)

    def prune(self, retention_days):
        n = self._db.prune_raw(self.conn, now_iso=self.now_iso(), retention_days=retention_days)
        self._commit_if_due(force=True)
        return n


# ---- Playwright glue ------------------------------------------------------------------------
def open_browser(p, *, profile_dir=None, cdp_url=None, headless=True):
    """Two transports. cdp_url: attach to the operator's own running Chrome (started with
    --remote-debugging-port), where they are already logged in and Cloudflare already admitted
    them; nothing is launched and no profile is copied. profile_dir: a separate persistent
    Chromium profile the operator logged into by hand. Returns (context, closer)."""
    if cdp_url:
        browser = p.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        return ctx, browser.close          # closes only OUR connection, not the operator's Chrome
    ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=headless)
    return ctx, ctx.close


def run_capture(core, *, profile_dir=None, cdp_url=None, pages, page_interval_s, retention_days,
                headless=True, max_cycles=None, sleep_fn=time.sleep):
    """Open `pages`, record until halted. Returns the halt reason or None (max_cycles reached)."""
    if core.halted:
        return core.halted
    from playwright.sync_api import sync_playwright
    if not cdp_url:
        profile_dir = Path(profile_dir or "")
        if not profile_dir.is_dir():
            core.halt("login_required: profile dir missing %s" % profile_dir)
            return core.halted
    counters = {}

    VISIBLE_JS = """
        // A background tab reports hidden and the app pauses its socket rooms: live 2026-09-16,
        // frames stopped ~60 s after every load. Report visible, and swallow visibilitychange.
        Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
        Object.defineProperty(document, 'hidden', {get: () => false});
        document.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
        window.addEventListener('blur', e => e.stopImmediatePropagation(), true);
    """

    def attach(page, name):
        counters[name] = {"frames": 0, "responses": 0}
        page.add_init_script(VISIBLE_JS)

        def on_ws(ws):
            core.on_ws_open(ws.url, page=name)

            def on_frame(payload):
                text = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
                counters[name]["frames"] += 1
                core.on_ws_frame(ws.url, text, page=name)
            ws.on("framereceived", on_frame)
            ws.on("close", lambda _ws: core.on_ws_close(ws.url, page=name))
            ws.on("socketerror", lambda err: core.on_ws_close(ws.url, page=name, error=str(err)[:120]))

        def on_response(resp):
            entry_mono, entry_at = core.mono_ns(), core.now_iso()
            url = resp.url
            host = urlsplit(url).hostname or ""
            if not (host == "axiom.trade" or host.endswith(".axiom.trade")):
                return
            if _sensitive_response(url):
                return
            ctype = (resp.headers.get("content-type") or "").lower()
            if resp.status < 400 and "json" not in ctype:
                return
            try:
                body = resp.text() if resp.status < 400 else ""
            except Exception:
                body = ""
            if resp.status < 400 and body:
                counters[name]["responses"] += 1
            core.on_rest_response(url, resp.status, body, page=name, received_mono_ns=entry_mono,
                                  received_at=entry_at, resource_type=resp.request.resource_type)

        page.on("websocket", on_ws)
        page.on("response", on_response)

    from playwright.sync_api import Error as PlaywrightError

    with sync_playwright() as p:
        ctx, closer = open_browser(p, profile_dir=profile_dir, cdp_url=cdp_url, headless=headless)
        tabs = {}

        def ensure_tabs():
            """The capture Chrome is also a window the operator can use; a closed capture tab is
            reopened (live 2026-09-16 08:20Z: both tabs closed by hand, the worker died). A gone
            browser is a halt: the operator must relaunch it."""
            for name in pages:
                page = tabs.get(name)
                if page is not None and not page.is_closed():
                    continue
                try:
                    page = ctx.new_page()
                except PlaywrightError as exc:
                    core.halt("browser_closed: " + str(exc)[:100])
                    return False
                attach(page, name)
                tabs[name] = page
                core.tabs_reopened += 1
            return True

        core.tabs_reopened = -len(pages)
        if not ensure_tabs():
            return core.halted
        cycles = 0
        try:
            while not core.halted and not core.stop_requested and (max_cycles is None or cycles < max_cycles):
                if not ensure_tabs():
                    break
                for name, page in list(tabs.items()):
                    c = counters[name]
                    c["frames"], c["responses"] = 0, 0
                    if core.backoff_until_mono > core.mono_ns():
                        continue                   # page-403 backoff: no reload, tabs stay open
                    try:
                        page.goto(PAGES[name], wait_until="domcontentloaded", timeout=60_000)
                        page.wait_for_timeout(5_000)
                        html = page.content()
                    except Exception as exc:
                        logger.warning("page %s load failed: %s", name, str(exc)[:160])
                        html = ""
                        if page.is_closed():
                            continue               # reopened at the top of the next cycle
                    core.on_page_loaded(name, page.url, html, frames_since_reload=c["frames"],
                                        responses_since_reload=c["responses"])
                    if core.halted:
                        break
                core.write_heartbeat(pages_open=len(tabs))
                core.prune(retention_days)
                cycles += 1
                if core.halted:
                    break
                # Wait INSIDE a Playwright call: the sync API dispatches page events (socket frames,
                # responses) only while one is in progress. A plain time.sleep here left 17 open
                # sockets silent for the whole interval (live 2026-09-16 08:16Z; Codex's review had
                # warned of exactly this). Heartbeat every 30 s.
                waited = 0.0
                while waited < page_interval_s and not core.halted and not core.stop_requested:
                    if any(t.is_closed() for t in tabs.values()):
                        break
                    step = min(30.0, page_interval_s - waited)
                    if sleep_fn is time.sleep:
                        live = [t for t in tabs.values() if not t.is_closed()]
                        if not live:
                            break                  # all tabs closed by hand: reopen at the top
                        try:
                            live[0].wait_for_timeout(step * 1000)
                        except PlaywrightError:
                            break                  # the tab went away mid-wait; reopen at the top
                    else:
                        sleep_fn(step)             # tests inject their own clock
                    waited += step
                    core.prune(retention_days)
                    core.write_heartbeat(pages_open=sum(1 for t in tabs.values() if not t.is_closed()))
        finally:
            for page in tabs.values():
                try:
                    page.close()               # our tabs only; the operator's tabs stay
                except Exception:
                    pass
            closer()
    return core.halted
