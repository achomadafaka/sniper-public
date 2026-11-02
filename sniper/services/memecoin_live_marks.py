"""Live display marks for held Solana coins, published off every trading path (S27-05).

Codex review 2026-09-27 (docs/audits/2026-09-27-solana-and-system-review.md S27-05). The stream
cache holds a new pool or curve price in memory within milliseconds. The UI read it only through
the mark tape, and the tape is written after:
- the fastlane batch snapshot;
- the exact previews;
- the exit sweep under the exit lock;
and even then with a one-second floor per coin. A changed price at +400 ms never reached the
screen.

This module is DISPLAY ONLY:
- One daemon thread copies the cache's swap and curve marks (`display_marks`) every 100 ms.
- It writes a snapshot to tmpfs only when the content changed, plus a heartbeat every second. The
  write is a temp file and a rename in the same directory, with no fsync: the file is ephemeral,
  and a reboot erasing it is correct.
- The web ticker shows a fresh snapshot's mark, else the route price or unknown (never the tape,
  whose on-chain rows carry DexScreener's quote USD: DR-04, 2026-09-27).
- Nothing here feeds an exit decision, the tape, the rail wake or an order.

Guarantees:
- One writer: an flock on a sidecar lock file. A second runner (a deploy overlap) stays silent.
- Honest clocks: `observed_at` is the chain update's local receive time and `slot` its slot;
  `published_at` is only "checked".
- A USD conversion older than MAX_QUOTE_USD_AGE_S makes the mark unhealthy, and the web does not
  show an unhealthy mark.
- A lower slot arriving after a higher one never replaces the published price. The cache being
  invalidated (disconnect or epoch rollover) withdraws the mark and resets that guard.

Rollback: MEMECOIN_LIVE_MARKS_PUBLISH=0 (default on). The file then goes stale within
MAX_PUBLISH_AGE_S and the web shows the route price or unknown for Solana.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

SCHEMA = 1
MAX_PUBLISH_AGE_S = 2.0          # web: an older snapshot is not current
MAX_QUOTE_USD_AGE_S = 60.0       # a USD conversion older than this is not a live price
SWAP_MAX_AGE_S = 5.0             # the ticker's own swap-mark TTL


def _flag(name, env=None, default="1"):
    env = os.environ if env is None else env
    return str(env.get(name, default)).strip().lower() not in {"0", "false", "no", "off"}


def enabled(env=None):
    return _flag("MEMECOIN_LIVE_MARKS_PUBLISH", env)


def carry_enabled(env=None):
    """Quiet-pool carry (step 2). MEMECOIN_LIVE_MARKS_CARRY=0 shows only swap marks <= 5 s old."""
    return _flag("MEMECOIN_LIVE_MARKS_CARRY", env)


def liquidity_enabled(env=None):
    """On-chain liquidity on the live marks. MEMECOIN_LIVE_MARKS_LIQUIDITY=0: the web shows the
    stored provider liquidity, labelled with its source, exactly as before."""
    return _flag("MEMECOIN_LIVE_MARKS_LIQUIDITY", env)


# ON-CHAIN LIQUIDITY (operator 2026-09-27 via Alpha: replace DexScreener liquidity when something
# better exists). The displayed depth is the REAL quote-side deposit x its USD x 2, the scale the
# operator and modeled_fill (MEMECOIN_SLIPPAGE_K) already read. DexScreener values the base reserve at
# the pool's own price, which a wash pool fakes for free (finding-memecoin-self-valued-liquidity);
# a quote deposit cannot be faked. Only pools whose quote vault IS the depth are listed, by the pool
# account's owner program:
CONSTANT_PRODUCT_PROGRAMS = {
    # Raydium AMM v4. Its vaults also hold pending PnL and open-orders amounts; fine for display.
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium_cpmm",
    # PumpSwap: the real quote vault only. The Pool's virtual quote (byte 245) prices trades but
    # nobody deposited it and no seller can withdraw it, so it is left out (conservative).
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
}
# NOT listed (they keep the provider's number, labelled): concentrated pools (Raydium CLMM, Meteora
# DLMM, Orca Whirlpool), whose vaults hold every range, not the active depth; Meteora dynamic pools
# (Eo7WjKq6...), whose vaults hold LP shares of Meteora's vault program, not the underlying (Alpha,
# 2026-09-27: out until a real frame proves the depth); and any program not read yet.


class PoolOwnerBook:
    """pool -> owner program, read ONCE per pool on this object's own thread (never the stream
    thread, never the publisher's), each read held to a wall-clock deadline: urllib's socket
    timeout does not bound DNS. A failed or late read is counted and retried after `retry_s`; a
    late answer is discarded."""

    def __init__(self, owner_fn, *, deadline_s=3.0, retry_s=30.0, mono_fn=time.monotonic):
        self.owner_fn = owner_fn
        self.deadline_s = float(deadline_s)
        self.retry_s = float(retry_s)
        self.mono_fn = mono_fn
        self._owners = {}                 # pool -> program; replaced whole (one atomic rebind)
        self._wanted = frozenset()
        self._due = {}
        self.reads = 0
        self.failures = 0

    def want(self, pools):
        self._wanted = frozenset(pools)   # one atomic rebind from the publisher thread

    def owner(self, pool):
        return self._owners.get(pool)

    def counts(self):
        return {"known": len(self._owners), "reads": self.reads, "failures": self.failures}

    def _read(self, pool):
        box = {}

        def work():
            try:
                box["owner"] = self.owner_fn(pool)
            except Exception as exc:          # counted below; the reason goes to the log
                box["error"] = exc

        worker = threading.Thread(target=work, name="memecoin-pool-owner-read", daemon=True)
        worker.start()
        worker.join(self.deadline_s)
        if worker.is_alive():
            return None, "deadline"
        if "error" in box:
            return None, repr(box["error"])[:120]
        return box.get("owner"), None if box.get("owner") else "no owner"

    def refresh_once(self):
        for pool in sorted(self._wanted):
            if pool in self._owners or self.mono_fn() < self._due.get(pool, 0.0):
                continue
            self.reads += 1
            program, why = self._read(pool)
            if program is None:
                self.failures += 1
                self._due[pool] = self.mono_fn() + self.retry_s
                logger.warning("live marks: pool owner read failed pool=%s reason=%s failures=%d",
                               str(pool)[:8], why, self.failures)
                continue
            self._owners = {**self._owners, pool: program}

    def run(self, stop):
        while not stop.is_set():
            try:
                self.refresh_once()
            except Exception:
                logger.exception("live marks pool owner cycle failed")
            stop.wait(1.0)


def default_path(env=None):
    """tmpfs, named after the state root, so the runner and the web of one deployment share a
    file and a test run (its own state root) never touches the live one."""
    env = os.environ if env is None else env
    explicit = (env.get("MEMECOIN_LIVE_MARKS_PATH") or "").strip()
    if explicit:
        return explicit
    from sniper.util.state_paths import state_root
    tag = hashlib.sha256(str(state_root(env)).encode()).hexdigest()[:12]
    base = "/dev/shm" if os.path.isdir("/dev/shm") else (env.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    return os.path.join(base, "sniper_live_marks_%s.json" % tag)


def write_ephemeral(path, obj):
    """Temp file plus rename in the same directory: a reader sees the whole old file or the whole
    new one. No fsync: this file is display-only and does not survive a reboot by design."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-live-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _age_s(now_iso, then_iso):
    now, then = _parse(now_iso), _parse(then_iso)
    if now is None or then is None or now.tzinfo is None or then.tzinfo is None:
        return None
    return (now - then).total_seconds()


class QuoteUsdHolder:
    """The fastlane pass's quote-asset USD, handed to the publisher thread by one reference swap.
    An empty batch (a failed fetch) keeps the last good value with its OLD time. Its age then
    grows and the marks turn unhealthy after MAX_QUOTE_USD_AGE_S; they are never re-stamped by a
    pool trade."""

    def __init__(self):
        self._value = ({}, None)

    def update(self, quote_usd, fetched_at):
        if quote_usd:
            self._value = (dict(quote_usd), fetched_at)

    def get(self):
        return self._value


class SingleWriterHolder(QuoteUsdHolder):
    """A QuoteUsdHolder with ONE writer role. The first role to publish owns it; any other role is
    refused, so no second thread can overwrite the value. The same role may publish again, so a
    restarted slot lane keeps publishing. Readers take the (value, time) tuple in one reference
    read."""

    def __init__(self):
        import threading
        super().__init__()
        self._writer = None
        self._claim = threading.Lock()

    def publish(self, writer, quote_usd, fetched_at):
        with self._claim:
            if self._writer is None:
                self._writer = writer
            if writer != self._writer:
                return False
        self.update(quote_usd, fetched_at)
        return True


WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_USD_WRITER = "slot_lane_prefetch"
# SOL/USD for display, NEVER DexScreener (operator, 2026-09-27). The slot lane's prefetch refreshes a
# Jupiter SOL->USDC quote (the funded executors' own quote path) every 10 s and publishes it here.
# With the lane down, the value ages. Marks go unhealthy after MAX_QUOTE_USD_AGE_S, and the ticker
# shows an on-chain tape mark, the route price or unknown; it never goes back to DexScreener.
SOL_USD = SingleWriterHolder()


def publish_sol_usd(price, observed_at, writer=SOL_USD_WRITER):
    """True when accepted: positive, finite, and from the holder's one writer."""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return False
    if not (price > 0 and price != float("inf")):
        return False
    return SOL_USD.publish(writer, {WSOL: price}, observed_at)


class QuoteBook:
    """{quote_mint: (usd, fetched_at)} for quote assets other than SOL and USDC, from Jupiter,
    written by ONE thread (the ExoticQuoteRefresher) with a whole-dict swap. A failed quote keeps
    the mint's old value and time, so it ages into unhealthy; it never goes to DexScreener."""

    def __init__(self):
        self._value = {}
        self._writer = None
        import threading
        self._claim = threading.Lock()

    def publish(self, writer, mint, usd, fetched_at):
        with self._claim:
            if self._writer is None:
                self._writer = writer
            if writer != self._writer:
                return False
            nxt = dict(self._value)
            nxt[mint] = (float(usd), fetched_at)
            self._value = nxt
        return True

    def get(self):
        return self._value


EXOTIC_USD = QuoteBook()
EXOTIC_WRITER = "live_marks_quote_refresher"


def onchain_quote_usd():
    """{quote_mint: (usd, fetched_at)}, NEVER DexScreener:
    - SOL from the slot lane's Jupiter quote;
    - USDC = 1.0 (fetched_at None: a constant never ages);
    - any other quote asset from EXOTIC_USD (Jupiter on the capture budget, every 10 s).
    Before the first SOL price there is no SOL entry, so SOL-quoted marks are absent."""
    out = dict(EXOTIC_USD.get())
    sol, at = SOL_USD.get()
    if sol and sol.get(WSOL):
        out[WSOL] = (float(sol[WSOL]), at)
    out[USDC] = (1.0, None)
    return out


# FOR THE FUNDED EXIT MAPS (DexScreener out, phase A, X1/X2; Alpha 2026-09-29). The same source, read
# by the exit sweep and the fast lane when their DexScreener consumer is switched off on Solana.
SOL_BAND_USD = (20.0, 2000.0)      # a SOL rate outside this is a wrong answer, not a price
OTHER_MAX_USD = 10_000_000.0       # any other quote asset: finite, positive and under this


def fresh_quote_usd(quote_mints, *, now_iso=None):
    """{quote_mint: usd} for the asked quote mints, from `onchain_quote_usd()`, fit to price a funded
    exit map. CACHE ONLY: two reference reads, no I/O, no lock that a writer holds across a network
    call (the writers compute first and swap a reference), so it is safe on the exit and fast-lane
    threads. FAIL CLOSED per asset: older than MAX_QUOTE_USD_AGE_S, undated (USDC excepted: a
    constant), non-finite, not positive, or outside its sanity band means ABSENT, and
    `fresh_price_map` then drops that asset's marks. Callers pass their own `now_iso`; without one
    this reads the corrected clock itself (`real_utc_now_iso`, a small file read)."""
    wanted = {str(m) for m in (quote_mints or ())}
    if not wanted:
        return {}
    if now_iso is None:
        from sniper.util.clock import real_utc_now_iso
        now_iso = real_utc_now_iso()
    out = {}
    for mint, entry in onchain_quote_usd().items():
        if mint not in wanted:
            continue
        try:
            usd, at = float(entry[0]), entry[1]
        except (TypeError, ValueError, IndexError):
            continue
        if not math.isfinite(usd) or usd <= 0:
            continue
        if mint == WSOL and not SOL_BAND_USD[0] <= usd <= SOL_BAND_USD[1]:
            continue
        if mint not in (WSOL, USDC) and usd >= OTHER_MAX_USD:
            continue
        if at is None:
            if mint != USDC:
                continue
        else:
            age = _age_s(now_iso, at)
            if age is None or not 0 <= age <= MAX_QUOTE_USD_AGE_S:
                continue
        out[mint] = usd
    return out


# DISPLAY QUOTES ARE ISOLATED FROM THE FUNDED JUPITER STATE (Alpha review of 1d7c8873, 2026-09-27).
# The first version called solana_real_seams._quote_with_fallback, the FUNDED path. A 429 or 401 on
# a display quote then:
# - wrote _jup_host_cooldown[api.jup.ag] for 60 s, so every funded entry and exit quote led with the
#   keyless 30-per-minute tier (the 09-24 "119 lost exit decisions" class, from a display thread);
# - counted in JUPITER_LOAD;
# - registered as funded in-flight;
# - persisted a lost funded decision to the load sink.
# This is a plain GET on the CAPTURE key's budget, with its own deadline and its own backoff. It
# touches none of that module state.
#
# WHY THE CAPTURE KEY AND NEVER KEYLESS. The keyless lite-api tier (30 per minute per IP) is what the
# funded quotes fall back to while api.jup.ag cools down, so it is the scarcest budget on this host,
# and a display consumer must not spend it. The capture key is a separate measurement budget on the
# keyed tier (api.jup.ag, the only host that honours a key). Without it, odd quote tokens get no USD,
# and those coins show the route price.
JUP_DISPLAY_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
DISPLAY_JUP_KEY_VAR = "MEMECOIN_CAPTURE_JUPITER_API_KEY"
QUOTE_DEADLINE_S = 3.0
RATE_LIMIT_BACKOFF_S = 60.0
NO_KEY_RECHECK_S = 300.0


def jupiter_display_quote(mint, decimals, *, env=None, urlopen=None, deadline_s=QUOTE_DEADLINE_S):
    """(USD for one whole `mint` token, "ok"), or (None, status) where status is one of no_key,
    rate_limited, auth_failed, timeout or error. The deadline is wall-clock: the request runs on a
    daemon thread joined for `deadline_s`, because a urllib timeout does not bound getaddrinfo. The
    key goes in a header only, and never into a URL, log line or exception."""
    import json as _json
    import threading
    import urllib.error
    import urllib.request
    key = str(((os.environ if env is None else env).get(DISPLAY_JUP_KEY_VAR)) or "").strip()
    if not key:
        return None, "no_key"
    url = "%s?inputMint=%s&outputMint=%s&amount=%d&slippageBps=50&swapMode=ExactIn" % (
        JUP_DISPLAY_QUOTE_URL, mint, USDC, 10 ** int(decimals))
    req = urllib.request.Request(url, headers={"x-api-key": key, "Accept": "application/json",
                                               "User-Agent": "sniper-live-marks/1"})
    opener = urlopen or urllib.request.urlopen
    box = {}

    def call():
        try:
            with opener(req, timeout=deadline_s) as resp:
                box["json"] = _json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            box["status"] = exc.code
        except Exception as exc:                  # transport, DNS, TLS, bad JSON
            box["error"] = type(exc).__name__

    worker = threading.Thread(target=call, name="live-marks-jupiter", daemon=True)
    worker.start()
    worker.join(deadline_s)
    if worker.is_alive():
        return None, "timeout"
    if box.get("status") == 429:
        return None, "rate_limited"
    if box.get("status") in (401, 403):
        return None, "auth_failed"
    j = box.get("json")
    try:
        usd = float((j or {}).get("outAmount") or 0) / 1e6
    except (TypeError, ValueError, AttributeError):
        return None, "error"
    return (usd, "ok") if usd > 0 and math.isfinite(usd) else (None, "error")


class ExoticQuoteRefresher:
    """One thread: every `period_s`, price each quote asset of the cache's marks that is neither
    SOL nor USDC, through jupiter_display_quote (the capture budget, isolated from every funded
    Jupiter state), into EXOTIC_USD. Its decimals come from the stream's own rows
    (cache.quote_decimals()); a mint with unknown decimals is skipped, never guessed. A 429 or 401
    backs that mint off for RATE_LIMIT_BACKOFF_S; a missing key rechecks every NO_KEY_RECHECK_S. Any
    failure keeps the mint's old value and time, so it ages into unhealthy."""

    def __init__(self, cache, *, now_iso_fn, price_fn=jupiter_display_quote, book=None,
                 period_s=10.0, mono_fn=time.monotonic):
        self.cache, self.now_iso_fn, self.price_fn = cache, now_iso_fn, price_fn
        self.book = book if book is not None else EXOTIC_USD
        self.period_s, self.mono_fn = float(period_s), mono_fn
        self._due = {}

    def refresh_once(self):
        """Quote every due exotic mint once. Returns the mints it quoted successfully."""
        done = []
        now = self.mono_fn()
        wanted = set(self.cache.quote_mints())
        decimals = self.cache.quote_decimals()
        for mint in sorted(wanted - {WSOL, USDC}):
            if now < self._due.get(mint, 0.0) or decimals.get(mint) is None:
                continue
            try:
                usd, status = self.price_fn(mint, decimals[mint])
            except Exception:
                logger.warning("live marks quote refresh failed for %s", mint[:8], exc_info=True)
                usd, status = None, "error"
            if status in ("rate_limited", "auth_failed"):
                self._due[mint] = now + max(self.period_s, RATE_LIMIT_BACKOFF_S)
            elif status == "no_key":
                self._due[mint] = now + NO_KEY_RECHECK_S
            else:
                self._due[mint] = now + self.period_s
            if status == "ok" and usd and usd > 0 and self.book.publish(
                    EXOTIC_WRITER, mint, usd, self.now_iso_fn()):
                done.append(mint)
        return done

    def run(self, stop):
        while not stop.is_set():
            try:
                self.refresh_once()
            except Exception:
                logger.exception("live marks quote refresher cycle failed")
            stop.wait(1.0)


class LiveMarkPublisher:
    def __init__(self, cache, *, quote_usd_fn, now_iso_fn, path=None, enabled=True, carry=False,
                 now_ts_fn=time.monotonic, mono_fn=time.monotonic, interval_s=0.1, heartbeat_s=1.0,
                 swap_max_age_s=SWAP_MAX_AGE_S, owner_book=None):
        self.cache = cache
        self.path = path or default_path()
        self.enabled = bool(enabled)
        self.carry = bool(carry)
        self.quote_usd_fn = quote_usd_fn          # -> {quote_mint: (usd, fetched_at_iso | None)}
        self.now_iso_fn = now_iso_fn
        self.now_ts_fn = now_ts_fn                # the cache's own clock (observed_ts domain)
        self.mono_fn = mono_fn
        self.interval_s = float(interval_s)
        self.heartbeat_s = float(heartbeat_s)
        self.swap_max_age_s = float(swap_max_age_s)
        self.owner_book = owner_book              # None: no on-chain liquidity is published
        self.generation = uuid.uuid4().hex[:12]
        self._published = {}                      # "chain:mint" -> the mark last published
        self._last_marks = None
        self._last_write = None
        self._lock_fh = None

    # --- one writer ---------------------------------------------------------------------------
    def acquire(self):
        import fcntl
        fh = open(self.path + ".lock", "a")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._lock_fh = fh
        return True

    def release(self):
        if self._lock_fh is not None:
            self._lock_fh.close()                 # closing the descriptor releases the flock
            self._lock_fh = None

    def _liquidity(self, mark):
        """The market's on-chain depth in USD (2 x the real quote-side deposit), or {} when unknown:
        no owner book, no reserve, no USD, or a pool program whose vault is not its depth."""
        reserve, usd = mark.get("reserve_quote"), mark.get("quote_usd")
        if self.owner_book is None or reserve is None or not usd:
            return {}
        if mark.get("venue") != "curve" and \
                self.owner_book.owner(mark.get("pool")) not in CONSTANT_PRODUCT_PROGRAMS:
            return {}
        value = 2.0 * float(reserve) * float(usd)
        if not math.isfinite(value) or value < 0:
            return {}
        return {"liquidity_usd": value, "liquidity_basis": "onchain",
                "liquidity_slot": mark.get("reserve_slot"), "liquidity_at": mark.get("reserve_at")}

    # --- one cycle ----------------------------------------------------------------------------
    def cycle(self):
        """Publish when the marks changed or the heartbeat is due. True when it wrote."""
        if not self.enabled:
            return False
        quotes = self.quote_usd_fn() or {}
        now_iso = self.now_iso_fn()

        def usd_ok(mint):
            if mint not in quotes:
                return False
            at = quotes[mint][1]
            if at is None:
                return True                       # a constant (USDC = 1.0) never ages
            age = _age_s(now_iso, at)
            return age is not None and 0 <= age <= MAX_QUOTE_USD_AGE_S

        fresh = self.cache.display_marks({m: v[0] for m, v in quotes.items()},
                                         now_ts=self.now_ts_fn(), max_age_s=self.swap_max_age_s,
                                         carry=self.carry)
        if self.owner_book is not None:
            self.owner_book.want(mark["pool"] for mark in fresh.values() if mark.get("venue") == "amm")
        out = {}
        for (chain, mint), mark in fresh.items():
            key = "%s:%s" % (chain, mint)
            quote_at = quotes.get(mark["quote_mint"], (None, None))[1]
            last = self._published.get(key)
            if last is not None and mark["slot"] < last["slot"]:
                out[key] = {**last, "healthy": usd_ok(last["quote_mint"])
                            and last.get("quote_usd_at") == quote_at}
                continue                          # an older slot arrived late: keep the newer one
            out[key] = {**mark, "quote_usd_at": quote_at, "healthy": usd_ok(mark["quote_mint"]),
                        **self._liquidity(mark)}
        # A key the cache no longer holds (invalidated, aged out, closed) is dropped with its guard.
        self._published = dict(out)
        now_m = self.mono_fn()
        due = self._last_write is None or now_m - self._last_write >= self.heartbeat_s
        if out == self._last_marks and not due:
            return False
        snap = {"schema": SCHEMA, "generation": self.generation, "published_at": now_iso,
                "marks": out}
        if self.owner_book is not None:
            snap["pool_owners"] = self.owner_book.counts()
        write_ephemeral(self.path, snap)
        self._last_marks, self._last_write = out, now_m
        return True

    def run(self, stop, log=None):
        """The thread body. Never raises; a failed cycle is logged and the next one runs."""
        if not self.enabled:
            return
        if not self.acquire():
            if log:
                log("[memecoin] live marks: another publisher holds %s; this one stays silent"
                    % self.path)
            return
        try:
            while not stop.is_set():
                started = self.mono_fn()
                try:
                    self.cycle()
                except Exception:
                    logger.exception("live marks cycle failed; the web shows the route price or unknown")
                stop.wait(max(0.0, self.interval_s - (self.mono_fn() - started)))
        finally:
            self.release()


# --- web side -----------------------------------------------------------------------------------

def read_snapshot(path):
    """The parsed snapshot, or {} when missing, torn or not this schema."""
    try:
        with open(path) as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(snap, dict) or snap.get("schema") != SCHEMA or not isinstance(snap.get("marks"), dict):
        return {}
    return snap


def fresh_marks(snap, now_iso, *, max_publish_age_s=MAX_PUBLISH_AGE_S):
    """{"chain:mint": mark + checked_at} for a snapshot published at most max_publish_age_s ago,
    healthy marks only. Anything else is {}: the ticker then shows the route price or unknown."""
    if not snap:
        return {}
    age = _age_s(now_iso, snap.get("published_at"))
    if age is None or age < -1.0 or age > max_publish_age_s:
        return {}
    out = {}
    for key, mark in snap["marks"].items():
        if isinstance(mark, dict) and mark.get("healthy") and mark.get("price_usd"):
            out[key] = {**mark, "checked_at": snap.get("published_at")}
    return out
