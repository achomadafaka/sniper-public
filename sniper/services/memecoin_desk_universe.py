"""Desk producer U: Pump.fun universe decisions (spec 2026-10-21-desk-universe-producer-design.md).

Pure code: curve state from each trade event's own virtual reserves (one consistent pair per event,
never a cross-account balance join), band-entry detection, 1 s bars keyed on the slot clock, and the
candidate snapshot with per-field provenance. The worker (scripts/memecoin_desk_universe_worker.py)
owns the gRPC connection, the clock and the files; nothing here does I/O.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime

PS_ARCHIVE_AFTER_S = 6 * 3600  # Shared live/archive seed boundary; 2 d -> 6 h (operator 2026-10-28 ~14:55Z, "archive it already").
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CPI_EVENT_PREFIX = bytes.fromhex("e445a52e51cb9a1d")
PUMP_SUPPLY_TOKENS = 1e9          # Pump.fun mints 1e9 tokens; used only when the create was not seen
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# absent: no source record existed by emission (a crawl never ran), vs missing: a record existed but failed or
# went stale (Gamma, CRAWLER design)
COMPLETENESS = ("complete", "partial", "missing", "window_not_elapsed", "absent")


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + raw


def pump_event_lines(*, account_keys, inner, logs):
    """[(ordinal, 'Program data: <b64>')] for Pump.fun anchor events in one transaction.

    `inner` is [(outer_index, [(program_id_index, data_bytes), ...])]. Ordinal follows the opportunity
    engine: (outer << 16) | inner for CPI events, else the log-line index."""
    pump = b58decode(PUMP_PROGRAM)
    found = []
    for outer, instructions in inner:
        for j, (pidx, data) in enumerate(instructions):
            if pidx < len(account_keys) and bytes(account_keys[pidx]) == pump and bytes(data[:8]) == CPI_EVENT_PREFIX:
                found.append(((outer << 16) | j, "Program data: " + base64.b64encode(bytes(data[8:])).decode()))
    if found:
        return found
    from sniper.services.memecoin_opportunity_live import program_data_logs
    return list(program_data_logs(list(logs), PUMP_PROGRAM))


def bonding_curve(mint: str) -> str:
    """Pump.fun bonding-curve PDA ["bonding-curve", mint]. TradeEvent carries no curve account."""
    from solders.pubkey import Pubkey
    return str(Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                           Pubkey.from_string(PUMP_PROGRAM))[0])


def curve_mcap_usd(virtual_quote, virtual_token, *, supply_tokens, sol_usd):
    """Curve mcap in USD from one reserve pair: lamports (1e9) over 6-decimal tokens. None if unknown."""
    if not virtual_quote or not virtual_token or not sol_usd or not supply_tokens:
        return None
    return (virtual_quote / 1e9) / (virtual_token / 1e6) * supply_tokens * sol_usd


def ticket_id(network_id, mint, band_entry_slot, signature, ordinal):
    key = f"{network_id}|{mint}|{band_entry_slot}|{signature}|{ordinal}"
    return "desk:U:" + hashlib.sha256(key.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Emission:
    mint: str
    curve: str
    band_entry_slot: int
    signature: str
    ordinal: int
    mcap_usd: float
    at: float
    ticket_id: str


class CurveBook:
    """Per-mint curve state. A crossing is emitted only between two observed states; after a gap the
    state is unknown, and the next observation re-seeds it without emitting."""

    def __init__(self, *, band=(30_000.0, 100_000.0), cooldown_s=60.0, sol_max_age_s=120.0):
        self.lo, self.hi = band
        self.cooldown_s, self.sol_max_age_s = cooldown_s, sol_max_age_s
        self._inside = {}            # mint -> True/False; absent = never seen or unknown
        self._mcap = {}
        self._supply = {}
        self._last_emit = {}
        self._graduated = set()
        self._unknown = set()

    def on_launch(self, mint, supply_atomic=None):
        if supply_atomic:
            self._supply[mint] = supply_atomic / 1e6

    def on_graduated(self, mint):
        self._graduated.add(mint)
        self._inside.pop(mint, None)
        self._mcap[mint] = None

    def mark_unknown(self, _reason):
        self._unknown.update(self._inside)
        self._inside.clear()
        self._mcap = dict.fromkeys(self._mcap)

    def state(self, mint):
        if mint in self._graduated:
            return "graduated"
        if mint in self._inside:
            return "inside" if self._inside[mint] else "outside"
        return "unknown" if mint in self._unknown else "unseen"

    def last_mcap(self, mint):
        return self._mcap.get(mint)

    def on_trade(self, *, mint, curve, virtual_quote, virtual_token, slot, signature, ordinal, now,
                 sol_usd, sol_usd_at, skip_cooldown=False):
        if mint in self._graduated:
            return None
        fresh = sol_usd if (sol_usd_at is not None and now - sol_usd_at <= self.sol_max_age_s) else None
        mcap = curve_mcap_usd(virtual_quote, virtual_token,
                              supply_tokens=self._supply.get(mint, PUMP_SUPPLY_TOKENS), sol_usd=fresh)
        self._mcap[mint] = mcap
        if mcap is None:
            return None                          # unknown price: neither a crossing nor a seed
        inside = self.lo <= mcap <= self.hi
        was = self._inside.get(mint)
        self._inside[mint] = inside
        self._unknown.discard(mint)
        if not inside or was is not False:
            return None                          # need an observed outside -> inside step
        last = self._last_emit.get(mint)
        if last is not None and now - last < self.cooldown_s and not skip_cooldown:
            return None                          # skip_cooldown: a verified winner re-entry recross
        self._last_emit[mint] = now
        return Emission(mint, curve, slot, signature, ordinal, mcap, now,
                        ticket_id("solana", mint, slot, signature, ordinal))


SOFT_LOSS_EXITS = ("desk_risk_exit", "real_trail")      # mirrors db_memecoin_desk.SOFT_LOSS_EXITS


def cohort_intake_feeds(desk_db, cohort_id):
    """Resolve intake sources once at worker startup; a configured cohort never falls back to legacy."""
    if not cohort_id:
        return ("pumpfun",)
    import sqlite3
    from sniper.services.memecoin_desk_contract import intake_feeds
    try:
        conn = sqlite3.connect(Path(desk_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        try:
            row = conn.execute("SELECT producer, policy_json FROM desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        raise ValueError("intake_cohort_unreadable") from exc
    if row is None:
        raise ValueError("intake_cohort_missing")
    if row[0] != "U":
        raise ValueError("intake_cohort_producer")
    try:
        policy = json.loads(row[1])
        if not isinstance(policy, dict):
            raise ValueError("policy_object")
        return intake_feeds(policy.get("hard_filter"))
    except (TypeError, ValueError) as exc:
        raise ValueError("intake_cohort_policy") from exc


def cohort_band(desk_db, cohort_id, *, env=None):
    """ONE cohort = ONE band everywhere (operator 2026-10-23): (lo, hi, source). The active cohort's frozen
    hard_filter.mcap_band (contract.mcap_band: absent = the 30k-100k default) when MEMECOIN_DESK_U_COHORT_ID names a
    readable producer-U cohort; else the MEMECOIN_DESK_UNIVERSE_BAND_{LO,HI}_USD env (source "env"). A malformed frozen band
    raises (never a guessed band). `mismatch` = the env band disagrees with the cohort's (the cohort wins; logged)."""
    import os
    import sqlite3
    from sniper.services.memecoin_desk_contract import mcap_band
    env = os.environ if env is None else env
    env_band = (float(env.get("MEMECOIN_DESK_UNIVERSE_BAND_LO_USD", "30000")),
                float(env.get("MEMECOIN_DESK_UNIVERSE_BAND_HI_USD", "100000")))
    row = None
    if cohort_id:
        try:
            conn = sqlite3.connect(Path(desk_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                row = conn.execute("SELECT producer, policy_json FROM desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            # a configured cohort we cannot read is NOT "no cohort": its own source, so the caller pages / logs it (eCat)
            return {"band": env_band, "source": "cohort_unreadable", "mismatch": None, "error": type(exc).__name__}
    if cohort_id and row is None:
        return {"band": env_band, "source": "cohort_missing", "mismatch": None}
    if row is None or row[0] != "U":
        return {"band": env_band, "source": "env", "mismatch": False}
    band = mcap_band((json.loads(row[1]) or {}).get("hard_filter"))
    return {"band": band, "source": "cohort", "mismatch": band != env_band}


class WinnerReentry:
    """Operator 2026-10-23: re-arm a mint whose latest desk ticket closed with a recorded realized_pnl > 0.
    The arm list is read from the desk DB close stages (never the ledger); the store re-verifies every
    claim and owns the per-mint hourly cap. One emission per closed position per process. A failed read
    keeps the last list for `stale_s`, then empties it (fail closed, never an implied empty result)."""

    SQL = ("SELECT h.mint, t.output_json FROM desk_ticket_heads h JOIN desk_tickets t "
           "ON t.ticket_id=h.ticket_id AND t.stage='close' WHERE h.network_id='solana' AND h.state='closed' "
           "AND t.ts >= ? AND h.created_at=(SELECT MAX(created_at) FROM desk_ticket_heads h2 "
           "WHERE h2.network_id=h.network_id AND h2.mint=h.mint)")

    def __init__(self, *, window_s, refresh_s=5.0, stale_s=30.0, watch=False):
        self.window_s, self.refresh_s, self.stale_s = window_s, refresh_s, stale_s
        self.watch = watch           # v23: every claim is trigger "watch", on the first post-close trade (no price/flow rule)
        self.armed = {}              # mint -> {"prev_position_id", "exit_price_usd"}
        self.closed = {}             # mint -> close time (s); a trade must be AFTER it on the chain clock
        self.soft = set()            # mints armed by a SOFT loss (v2): the reclaim also needs net buy flow > 0
        self.fired = set()           # prev_position_id already emitted by this process
        self.loaded_at = self.tried_at = None

    def due(self, now):
        return self.tried_at is None or now - self.tried_at >= self.refresh_s

    def load(self, rows, now):
        armed, closed_at, soft = {}, {}, set()
        for mint, output_json in rows:
            close = json.loads(output_json)
            pnl, price, pid = close.get("realized_pnl"), close.get("exit_price_usd"), close.get("position_id")
            try:
                closed = datetime.fromisoformat(close["closed_at"]).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            if type(pnl) not in (int, float) or type(pid) is not int or now - closed > self.window_s:
                continue
            if pnl <= 0:                 # soft loss (v2): reclaim OUR entry fill; the store refuses it in a v1 cohort
                if close.get("exit_reason") not in SOFT_LOSS_EXITS or type(price) not in (int, float):
                    continue
                price = close.get("entry_fill_price_usd")
                soft.add(mint)
            if type(price) in (int, float) and price > 0:
                armed[mint], closed_at[mint] = {"prev_position_id": pid, "exit_price_usd": price}, closed
            else:
                soft.discard(mint)
        self.armed, self.closed, self.soft, self.loaded_at, self.tried_at = armed, closed_at, soft, now, now

    def failed(self, now):
        self.tried_at = now
        if self.loaded_at is None or now - self.loaded_at > self.stale_s:
            self.armed, self.closed = {}, {}

    def pending(self, mint, trade_time=None):
        """The unspent arm for this mint, else None. With trade_time (the trade's block time), only a
        trade strictly after the close counts: a backlog/after-gap trade from before it never fires."""
        arm = self.armed.get(mint)
        if arm is None or arm["prev_position_id"] in self.fired:
            return None
        if trade_time is not None and not trade_time > self.closed.get(mint, float("inf")):
            return None
        return arm

    def take(self, mint, trade_time, trigger="recross"):
        """Spend the arm: (a) an ordinary band recross carries it; `trigger` is frozen in the claim."""
        arm = self.pending(mint, trade_time)
        if arm is None:
            return None
        self.fired.add(arm["prev_position_id"])
        return {**arm, "trigger": "watch" if self.watch else trigger}

    def check(self, mint, price_usd, trade_time, net_flow_usd=None):
        """(b) The claim when this trade prints above the armed price, else None. A soft-loss arm also needs a known,
        positive net regular buy flow over the coin's flow window (unknown or <= 0 never fires)."""
        arm = self.pending(mint, trade_time)
        if arm is not None and self.watch:
            return self.take(mint, trade_time)       # JUDGE reads the post-exit tape and sets the trigger, or skips
        if arm is None or price_usd is None or not price_usd > arm["exit_price_usd"]:
            return None
        if mint in self.soft and not (type(net_flow_usd) in (int, float) and net_flow_usd > 0):
            return None
        return self.take(mint, trade_time, "above_exit")


WATCH_TRIGGER_SCORE = {"new_high": 4, "volume_burst": 3, "pullback_reclaim": 2, "fresh_buyers": 1}


class WatchBook:
    """watch-list-v1 change triggers (operator 10-26): the waiting WATCH looks of the desk DB, each with its last-look
    reference (as-of price in SOL, the sheet's 5 s noise and 60 s regular buy USD), checked against every live trade.
    A change since the look emits ONE re-look request (the caller journals it, then commit()); unknown inputs never
    trigger (cadence still runs). Regular flow only: the Mayhem agent's trades move price, never volume or buyers."""

    SQL = ("SELECT h.ticket_id, h.mint, t.inputs_json, json_extract(t.inputs_json,'$.response_at') "
           "FROM desk_ticket_heads h JOIN desk_tickets t ON t.ticket_id=h.ticket_id AND t.stage='vet' "
           "WHERE h.state='waiting' AND t.reason='watch' AND json_extract(t.inputs_json,'$.watch.watch_arm')=1")

    def __init__(self, *, refresh_s=5.0, buyers_min=5, burst_x=2.0, high_k=2.0, dip_k=1.0):
        self.refresh_s, self.buyers_min, self.burst_x, self.high_k, self.dip_k = refresh_s, buyers_min, burst_x, high_k, dip_k
        self.by_mint, self.state, self.sent, self.pending = {}, {}, set(), []
        self.tried_at = self.loaded_at = None

    def due(self, now):
        return self.tried_at is None or now - self.tried_at >= self.refresh_s

    def load(self, rows, now, reset=False):
        if reset:
            self.state = {}
        by_mint = {}
        for ticket_id, mint, inputs_json, response_at in rows:
            try:
                inputs = json.loads(inputs_json)
                sheet = inputs.get("sheet") or {}
                st = sheet.get("structure") or {}
                vol = (st.get("volatility") or {}).get("median_5s_swing_pct")
                base = ((st.get("windows") or {}).get("60s") or {}).get("buy_usd")
                price = (((inputs.get("asof") or {}).get("meta") or {}).get("rank") or {}).get("price_now")
                w = inputs.get("watch") or {}
                hi, px = w.get("look_high"), w.get("look_price_usd")
                ok = lambda v: type(v) in (int, float) and math.isfinite(v)                       # noqa: E731
                by_mint.setdefault(mint, []).append({
                    "ticket_id": ticket_id, "decided": datetime.fromisoformat(response_at).timestamp(),
                    "p0": float(price) if ok(price) and price > 0 else None,
                    "noise": float(vol) if ok(vol) and vol > 0 else None,
                    "base60": float(base) if ok(base) and base > 0 else None,
                    # the last-look HIGH in SOL (prereg: new high above the last-look high): p0 x the look's USD high/price
                    "h0": float(price) * hi / px if ok(price) and price > 0 and ok(hi) and ok(px) and hi > 0 and px > 0 else None})
            except (TypeError, ValueError, KeyError):
                continue
        self.by_mint, self.loaded_at, self.tried_at = by_mint, now, now

    def failed(self, now):
        self.tried_at = now

    def on_trade(self, *, mint, price_sol, received_at, is_buy, actor, agents, gap_since_load, recent):
        """Change triggers since each look; `recent` = [(t, is_buy, usd, actor, price)] of this mint (the worker's ring)."""
        orders = self.by_mint.get(mint)
        if not orders:
            return
        if gap_since_load:
            for o in orders:
                self.state.pop(o["ticket_id"], None)
            return
        for o in orders:
            tid = o["ticket_id"]
            if tid in self.sent or any(t == tid for t, _ in self.pending) or received_at < o["decided"]:
                continue
            st = self.state.setdefault(tid, {"high": None, "low": None, "buyers": set()})
            if price_sol is not None:
                st["high"] = price_sol if st["high"] is None else max(st["high"], price_sol)
                st["low"] = price_sol if st["low"] is None else min(st["low"], price_sol)
            if is_buy and actor and actor not in agents:
                st["buyers"].add(actor)
            kind = None
            p0, noise = o["p0"], o["noise"]
            if p0 is not None and noise is not None and price_sol is not None:
                if o["h0"] is not None and price_sol >= o["h0"] * (1 + self.high_k * noise / 100):
                    kind = "new_high"
                elif st["low"] is not None and st["low"] <= p0 * (1 - self.dip_k * noise / 100) and price_sol >= p0:
                    kind = "pullback_reclaim"
            if kind is None and o["base60"] is not None and received_at - o["decided"] >= 60:
                win = [x for x in recent if received_at - x[0] <= 60 and x[1] and x[3] not in agents]
                if win and all(x[2] is not None for x in win) and sum(x[2] for x in win) >= self.burst_x * o["base60"]:
                    kind = "volume_burst"
            if kind is None and len(st["buyers"]) >= self.buyers_min:
                kind = "fresh_buyers"
            if kind is not None:
                self.pending.append((tid, {"trigger": kind, "change_score": WATCH_TRIGGER_SCORE[kind],
                                           "received_at": received_at, "price_sol": price_sol}))

    def take(self):
        return list(self.pending)

    def commit(self, ticket_id):
        self.sent.add(ticket_id)
        self.pending = [(t, r) for t, r in self.pending if t != ticket_id]
        self.state.pop(ticket_id, None)


class StandingBook:
    """Watch v22 executor (operator 2026-10-23): the open price triggers of waiting desk tickets, read from the desk DB
    vet stage (never the ledger), checked against each live trade. px_up fires on `confirm` consecutive prints at or
    above the frozen SOL level; px_hold fires once price has printed at or below the level and then `confirm`
    consecutive prints above it. One fire per ticket per process; the desk writer re-checks every fire."""

    SQL = ("SELECT h.ticket_id, h.mint, json_extract(t.inputs_json,'$.trigger'), json_extract(t.inputs_json,'$.response_at') "
           "FROM desk_ticket_heads h JOIN desk_tickets t ON t.ticket_id=h.ticket_id AND t.stage='vet' "
           "WHERE h.state='waiting' AND json_extract(t.inputs_json,'$.trigger') IS NOT NULL")

    def __init__(self, *, refresh_s=5.0, stale_s=30.0, max_lag_s=5.0):
        self.refresh_s, self.stale_s, self.max_lag_s = refresh_s, stale_s, max_lag_s
        self.by_mint = {}            # mint -> [order]; order = dict(ticket_id, kind, level, confirm, expires, decided)
        self.state = {}              # ticket_id -> {"dipped": bool, "run": int}
        self.fired = set()
        self.cancels = []            # [(ticket_id, cancel)] for the caller to write (take_cancels), standing-v2 band exits
        self.loaded_at = self.tried_at = None

    def due(self, now):
        return self.tried_at is None or now - self.tried_at >= self.refresh_s

    def load(self, rows, now, reset=False):
        """reset=True (a tape gap since the previous load) drops every partial confirmation: a hold or a confirm run
        never spans a gap (Codex on 1073128bb). `fired` is kept: a ticket fires at most once per process."""
        if reset:
            self.state = {}
        by_mint = {}
        for ticket_id, mint, trig_json, response_at in rows:
            try:
                trig = json.loads(trig_json)
                order = {"ticket_id": ticket_id, "kind": trig["kind"], "level": float(trig["level_price_sol"]),
                         "level_usd": float(trig["level_price_usd"]) if trig.get("level_price_usd") is not None else None,
                         "confirm": int(trig["confirm"]), "hold_s": trig.get("hold_s"),
                         "usd_only": trig.get("order") == "standing-v2",
                         "band": (tuple(float(v) for v in trig["cancel_band_usd_mcap"])
                                  if trig.get("order") == "standing-v2" and trig.get("cancel_band_usd_mcap") else None),
                         "ceiling_usd": (float(trig["ceiling_price_usd"]) if trig.get("ceiling_price_usd") is not None
                                         else None),
                         "expires": datetime.fromisoformat(trig["expires_at"]).timestamp(),
                         "decided": datetime.fromisoformat(response_at).timestamp()}
            except (TypeError, ValueError, KeyError):
                continue
            if order["kind"] in ("px_up", "px_hold", "px_limit") and order["level"] > 0 and ticket_id not in self.fired:
                by_mint.setdefault(mint, []).append(order)
        self.by_mint, self.loaded_at, self.tried_at = by_mint, now, now
        self.state = {k: v for k, v in self.state.items() if any(o["ticket_id"] == k for os_ in by_mint.values() for o in os_)}

    def failed(self, now):
        self.tried_at = now
        if self.loaded_at is None or now - self.loaded_at > self.stale_s:
            self.by_mint = {}        # fail closed: an unreadable work list never fires
            self.state = {}          # and no partial confirmation survives the outage (Codex on 28994d4f1)

    def on_trade(self, *, mint, price_sol, block_time, received_at, gap_since_load, net_flow_usd=None, sol_usd=None):
        """[(order, state)] that fire on this trade. A stale (lagging) print, a print before the decision or after
        expiry, or any tape gap since the work list loaded never fires and never advances a trigger."""
        orders = self.by_mint.get(mint)
        if not orders or price_sol is None or not price_sol > 0 or block_time is None:
            return []
        if gap_since_load or received_at - block_time > self.max_lag_s:
            for o in orders:             # unusable evidence breaks any partial confirmation; it never carries over
                self.state.pop(o["ticket_id"], None)
            return []
        out = []
        for o in orders:
            if o["ticket_id"] in self.fired or not o["decided"] - 1 < block_time <= o["expires"]:
                continue
            if o.get("band") and type(sol_usd) in (int, float) and sol_usd > 0:
                from sniper.services.memecoin_desk_universe import PUMP_SUPPLY_TOKENS as _supply
                mcap = price_sol * sol_usd * _supply
                if not o["band"][0] <= mcap <= o["band"][1]:      # the coin left the frozen band: the order dies
                    # NOT marked fired here (Zeta): only commit() after the caller's durable write; a failed write retries
                    # on the next out-of-band print
                    if all(tid != o["ticket_id"] for tid, _ in self.cancels):
                        self.cancels.append((o["ticket_id"], {"reason": "left_band", "mcap_usd": mcap, "block_time": block_time,
                                                          "received_at": received_at, "sol_usd": sol_usd}))
                    continue
            st = self.state.setdefault(o["ticket_id"], {"dipped": False, "run": 0, "above_at": None})
            if o["kind"] == "px_hold":
                if price_sol <= o["level"]:
                    st["dipped"], st["run"], st["above_at"] = True, 0, None
                    continue
                if not st["dipped"]:
                    continue
                if o.get("hold_s"):          # time hold (Gamma), on the RECEIVE clock: a chain-time lower bound could
                    st["above_at"] = received_at if st["above_at"] is None else st["above_at"]   # overstate it
                    if received_at - st["above_at"] < o["hold_s"]:
                        continue
                    st["run"] = o["confirm"]
                else:
                    st["run"] += 1
            elif o["kind"] == "px_limit":       # LIMIT arm: a print at or below the sheet price (no flow rule)
                # the frozen USD ceiling is checked HERE with the fresh FX, before the fire is committed (eCat: a fire the
                # writer refuses on FX would burn the one watch). Unknown FX never fires and never consumes it.
                usd_ok = (o["level_usd"] is not None and type(sol_usd) in (int, float) and sol_usd > 0
                          and price_sol * sol_usd <= o["level_usd"])
                # standing-v2: the frozen USD level is the order; its SOL is derived per print with the fresh FX
                st["run"] = st["run"] + 1 if (o.get("usd_only") or price_sol <= o["level"]) and usd_ok else 0
            else:
                st["run"] = st["run"] + 1 if price_sol >= o["level"] else 0
            if st["run"] >= o["confirm"]:
                if o.get("ceiling_usd") is not None and not (type(sol_usd) in (int, float) and sol_usd > 0
                                                             and price_sol * sol_usd <= o["ceiling_usd"]):
                    continue             # gate ceiling checked HERE with the fresh FX: an above-ceiling print never burns the watch
                if o["kind"] == "px_hold" and not (type(net_flow_usd) in (int, float) and net_flow_usd >= 0):
                    continue             # a hold needs known net buy flow (operator 2026-10-23); unknown never fires
                out.append(o)            # the caller commits it (commit()) only after the fire request is durable
        return out

    def take_cancels(self):
        """The pending cancels, NOT cleared: the caller calls commit(ticket_id) for each after its line is durable."""
        return list(self.cancels)

    def commit(self, ticket_id):
        """Mark a fire as sent: called after its request line was appended (Codex: a fire that could not be priced
        or written must not burn the watch)."""
        self.fired.add(ticket_id)
        self.state.pop(ticket_id, None)
        self.cancels = [(tid, c) for tid, c in self.cancels if tid != ticket_id]


class BarBuilder:
    """1 s bars on the slot clock for a bounded watch set. A slot with no block time is never placed
    in a second (DATA_INSUFFICIENT)."""

    def __init__(self, *, max_pools):
        self.max_pools = max_pools
        self._pools = {}             # pool -> {slot: [swaps]}
        self._times = {}             # slot -> block_time (s)

    def watch(self, pool):
        if pool in self._pools:
            return True
        if len(self._pools) >= self.max_pools:
            return False
        self._pools[pool] = {}
        return True

    def unwatch(self, pool):
        self._pools.pop(pool, None)

    def watched(self):
        return list(self._pools)

    def set_block_time(self, slot, block_time):
        self._times[slot] = block_time

    def add_swap(self, pool, *, slot, price, quote_usd, is_buy, maker):
        slots = self._pools.get(pool)
        if slots is None:
            return False
        slots.setdefault(slot, []).append((price, quote_usd, is_buy, maker))
        return True

    def insufficient(self, pool):
        return sorted(s for s in self._pools.get(pool, {}) if s not in self._times)

    def closed_bars(self, pool, *, upto_slot):
        by_second = {}
        for slot in sorted(self._pools.get(pool, {})):
            if slot > upto_slot or slot not in self._times:
                continue
            by_second.setdefault(self._times[slot], []).append(slot)
        out = []
        for t in sorted(by_second):
            swaps = [(s, x) for s in by_second[t] for x in self._pools[pool][s]]
            prices = [x[0] for _, x in swaps]
            out.append({"t": t, "o": prices[0], "h": max(prices), "l": min(prices), "c": prices[-1],
                        "buy_usd": sum(x[1] for _, x in swaps if x[2]),
                        "sell_usd": sum(x[1] for _, x in swaps if not x[2]),
                        "n_swaps": len(swaps), "n_makers": len({x[3] for _, x in swaps}),
                        "slot_lo": min(by_second[t]), "slot_hi": max(by_second[t])})
        return out

    def drop_through(self, pool, slot):
        """Forget swaps at or below `slot` once their bars are persisted."""
        slots = self._pools.get(pool)
        if slots:
            for s in [s for s in slots if s <= slot]:
                del slots[s]


@dataclass(frozen=True)
class Field:
    value: object
    source: str
    known_at: float
    slot: int | None
    completeness: str


def build_snapshot(*, emission_at, fields):
    """Immutable candidate snapshot. A field known after the snapshot is withheld, never back-filled."""
    out, missing = {}, []
    for name in sorted(fields):
        f = fields[name]
        if f.completeness not in COMPLETENESS:
            raise ValueError(f"bad completeness {f.completeness!r} for {name}")
        if f.known_at > emission_at:
            row = {"value": None, "source": f.source, "known_at": None, "slot": None,
                   "completeness": "window_not_elapsed"}
        else:
            row = {"value": f.value, "source": f.source, "known_at": f.known_at, "slot": f.slot,
                   "completeness": f.completeness}
        if row["value"] is None or row["completeness"] != "complete":
            missing.append(name)
        out[name] = row
    body = {"emission_at": emission_at, "fields": out, "missing": missing}
    body["hash"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body
