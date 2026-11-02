"""Pump.fun curve VOLUME TAPE: every TradeEvent on the chain, per-mint rolling windows in memory.

The slot lane has no DexScreener read on its path, so its dead-market guard reads this instead:
`volume_facts(mint)` answers from memory, no I/O. The socket (start_tape_thread) is one program-
wide `processed` logsSubscribe on pump.fun. Measured 2026-09-26: the PUBLIC
wss://api.mainnet-beta.solana.com accepts it and delivered 814/814 chain signatures over 4 slots;
about 220 frames/s and 353 KB/s, 89% of pump transactions FAIL (bot spam) and only ~20% of frames
carry a TradeEvent, so a substring check on the raw text skips the rest before json.loads.

UNKNOWN IS NOT ZERO. A window's exact value is reported only when the socket watched all of it;
an outage (the public endpoint closed the socket once in 90 s, 2026-09-26) makes the windows that
overlap it None, while `*_sol_min` keeps the observed lower bound. A tape that has seen no trade on
ANY mint for DEAD_TAPE_S answers None for everything (pump never goes that quiet; the socket is
lying). A mint the tape never saw, inside a fully watched window, is a real 0.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Optional

from sniper.research.memecoin_opportunity.decoders import BorshReader, anchor_event_bytes
from sniper.services.memecoin_opportunity_live import parse_solana_log_notification, program_data_logs

logger = logging.getLogger("memecoin")

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"
_PREFIX = "Program data: vdt/"          # base64 of the TradeEvent discriminator's first bytes
_TRADE = bytes([189, 219, 127, 211, 78, 230, 97, 238])
# SOL-quoted curves report the system program (32 zero bytes) or wrapped SOL as quote mint.
_SOL_QUOTES = {bytes(32), bytes.fromhex("069b8857feab8184fb687f634618c035dac439dc1aeb3b5598a0f00000000001")}
M5_S, H1_S, DEAD_TAPE_S = 300.0, 3600.0, 30.0


def decode_trade(raw):
    """(mint32, lamports, is_buy, user32) for a SOL-quoted curve TradeEvent, else None. Raises
    ValueError on a truncated body. quote_mint sits after a variable-length tail, so it is walked."""
    r = BorshReader(raw[8:])
    mint = r.take(32)
    lamports, _tokens, is_buy = r.u64(), r.u64(), r.bool()
    user = r.take(32)
    r.take(8 + 32 + 32 + 16 + 32 + 16)   # timestamp, 4 reserves, fee recipient/bps/fee, creator/bps/fee
    r.bool(); r.take(32); r.string(); r.bool(); r.take(32)
    r.take(r.u32() * 34)                  # shareholders: Vec<{pubkey, u16}>
    return (mint, lamports, is_buy, user) if r.take(32) in _SOL_QUOTES else None


class _Mint:
    __slots__ = ("recent", "minutes", "last")

    def __init__(self):
        self.recent = deque()     # (mono, lamports, is_buy, user32, signature), last M5_S only
        self.minutes = deque()    # [minute_index, lamports, trades], last 60 minutes
        self.last = None


class PumpTape:
    def __init__(self, *, max_mints=20_000, idle_evict_s=H1_S, clock=time.monotonic):
        self.clock = clock
        self.max_mints, self.idle_evict_s = int(max_mints), float(idle_evict_s)
        self._lock = threading.Lock()
        self._mints = {}
        self.coverage_since = None        # monotonic start of the current continuous connection
        self.started = None               # first subscription ack; nothing before it is covered
        self._gaps = deque()              # [start, end|None] socket outages since `started`
        self.last_trade_mono = None
        self.stats = {"frames": 0, "prefiltered": 0, "trades": 0, "non_sol": 0, "decode_errors": 0}
        self._next_sweep = 0.0

    def mark_connected(self, mono=None):
        """A subscription ack: closes the open outage, if any."""
        with self._lock:
            mono = self.clock() if mono is None else mono
            self.coverage_since = mono
            if self.started is None:
                self.started = mono
            elif self._gaps and self._gaps[-1][1] is None:
                self._gaps[-1][1] = mono

    def mark_disconnected(self, mono=None):
        with self._lock:
            mono = self.clock() if mono is None else mono
            self.coverage_since = None
            if self.started is not None and not (self._gaps and self._gaps[-1][1] is None):
                self._gaps.append([mono, None])
            while self._gaps and self._gaps[0][1] is not None and mono - self._gaps[0][1] > H1_S + 60:
                self._gaps.popleft()

    def _uncovered(self, window, now):
        """Seconds of [now - window, now] the socket did not watch (before start, or an outage)."""
        lo = now - window
        out = max(0.0, min(now, self.started) - lo)
        for start, end in self._gaps:
            out += max(0.0, min(now, end if end is not None else now) - max(lo, start))
        return min(window, out)

    def ingest(self, text, mono):
        """One raw WS frame. Returns the number of trades recorded. Safe to call on any frame."""
        self.stats["frames"] += 1
        if _PREFIX not in text:
            self.stats["prefiltered"] += 1
            return 0
        parsed = parse_solana_log_notification(json.loads(text))
        if not parsed or parsed["err"]:
            return 0
        trades = []
        for _, line in program_data_logs(parsed["logs"], PUMP_PROGRAM):
            try:
                raw = anchor_event_bytes(line)
                if raw[:8] != _TRADE:
                    continue
                trade = decode_trade(raw)
            except ValueError:
                self.stats["decode_errors"] += 1
                continue
            if trade is None:
                self.stats["non_sol"] += 1
            else:
                trades.append(trade)
        if not trades:
            return 0
        minute = int(mono // 60)
        with self._lock:
            for mint, lamports, is_buy, user in trades:
                m = self._mints.get(mint)
                if m is None:
                    m = self._mints[mint] = _Mint()
                m.recent.append((mono, lamports, is_buy, user, parsed["signature"]))
                if m.minutes and m.minutes[-1][0] == minute:
                    m.minutes[-1][1] += lamports
                    m.minutes[-1][2] += 1
                else:
                    m.minutes.append([minute, lamports, 1])
                m.last = mono
                self._trim(m, mono)
            self.last_trade_mono = mono
            self.stats["trades"] += len(trades)
            if mono >= self._next_sweep:
                self._sweep(mono)
        return len(trades)

    def _trim(self, m, now):
        while m.recent and now - m.recent[0][0] > M5_S:
            m.recent.popleft()
        while m.minutes and int(now // 60) - m.minutes[0][0] >= 60:
            m.minutes.popleft()

    def _sweep(self, now):
        self._next_sweep = now + 10.0
        for mint in [k for k, m in self._mints.items() if now - m.last > self.idle_evict_s]:
            del self._mints[mint]
        if len(self._mints) > self.max_mints:   # ponytail: O(n log n) sort every 10 s past the cap
            for mint, _ in sorted(self._mints.items(), key=lambda kv: kv[1].last)[:len(self._mints) - self.max_mints]:
                del self._mints[mint]

    def volume_facts(self, mint, *, exclude_signatures=(), now=None):
        """Rolling SOL volume for one mint, or None when the tape itself cannot answer.

        `m1_sol` / `m5_sol` / `h1_sol` (and the m5 counts) are EXACT or None: None whenever any part
        of that window was not watched (before the tape started, or a socket outage). `*_sol_min`
        is what was observed either way, a LOWER BOUND: it can prove a floor is met, never that it
        is missed. `*_uncovered_s` says how much of each window was blind. h1 is summed from whole
        minutes, so it may include up to 60 s beyond the hour. `exclude_signatures` removes those
        trades (the copied source buy) from every window.
        """
        from solders.pubkey import Pubkey
        key = bytes(Pubkey.from_string(str(mint)))
        with self._lock:
            now = self.clock() if now is None else now
            if (self.started is None or self.last_trade_mono is None
                    or now - self.last_trade_mono > DEAD_TAPE_S):
                return None
            blind = {w: self._uncovered(w, now) for w in (60.0, M5_S, H1_S)}
            m = self._mints.get(key)
            recent = list(m.recent) if m else []
            h1_lamports = sum(b[1] for b in m.minutes if int(now // 60) - b[0] < 60) if m else 0
            last, since = (m.last if m else None), self.coverage_since
        skip = set(exclude_signatures)
        kept = [t for t in recent if t[4] not in skip and now - t[0] <= M5_S]
        h1_lamports = max(0, h1_lamports - sum(t[1] for t in recent if t[4] in skip))
        m1 = sum(t[1] for t in kept if now - t[0] <= 60.0) / 1e9
        m5 = sum(t[1] for t in kept) / 1e9
        exact5 = blind[M5_S] == 0.0
        return {
            "m1_sol": m1 if blind[60.0] == 0.0 else None,
            "m5_sol": m5 if exact5 else None,
            "h1_sol": h1_lamports / 1e9 if blind[H1_S] == 0.0 else None,
            "m5_buy_sol": sum(t[1] for t in kept if t[2]) / 1e9 if exact5 else None,
            "trades_m5": len(kept) if exact5 else None,
            "traders_m5": len({t[3] for t in kept}) if exact5 else None,
            "m1_sol_min": m1, "m5_sol_min": m5, "h1_sol_min": h1_lamports / 1e9,
            "m1_uncovered_s": blind[60.0], "m5_uncovered_s": blind[M5_S], "h1_uncovered_s": blind[H1_S],
            "last_trade_age_s": (now - last) if last is not None else None,
            "coverage_since": since,
        }


def start_tape_thread(tape, *, status, ws_url=PUBLIC_WS, stop=None, connect=None):
    """Keep one program-wide pump.fun logsSubscribe feeding `tape` on a daemon thread."""
    status.update(connected=False, reconnects=0, last_error=None, last_frame_mono=None)
    stop = stop or threading.Event()

    async def run():
        import websockets
        dial = connect or (lambda u: websockets.connect(u, max_size=None, ping_interval=20))
        backoff = 1.0
        while not stop.is_set():
            try:
                async with dial(ws_url) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                              "params": [{"mentions": [PUMP_PROGRAM]},
                                                         {"commitment": "processed"}]}))
                    while not stop.is_set():
                        try:
                            text = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        mono = time.monotonic()
                        backoff = 1.0
                        status["last_frame_mono"] = mono
                        if tape.coverage_since is None:   # until this socket's ack arrives
                            ack = json.loads(text)
                            if ack.get("id") == 1:
                                if not isinstance(ack.get("result"), int):
                                    raise RuntimeError("subscribe refused: %s" % ack.get("error"))
                                tape.mark_connected(mono)   # coverage starts at the subscription ack
                                status["connected"] = True
                            continue
                        try:
                            tape.ingest(text, mono)
                        except Exception:
                            tape.stats["decode_errors"] += 1
                            logger.exception("pump tape frame not ingested")
            except Exception as exc:
                logger.warning("pump tape socket down: %r; retry in %.0fs", exc, backoff)
                status.update(last_error=repr(exc)[:200], reconnects=status["reconnects"] + 1)
            tape.mark_disconnected()
            status["connected"] = False
            if not stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    thread = threading.Thread(target=lambda: asyncio.run(run()), name="pump-tape", daemon=True)
    thread.start()
    return thread
