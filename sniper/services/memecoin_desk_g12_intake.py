"""Stage 1b: inline G2 coin age + G1 creator history for a desk candidate that reached the pre-PASS evidence window (Alpha 2026-10-28; Gamma's measurement notes).

Runs inside the intake assembler's collect() (research paper desk, never a funded or entry path). Default OFF (MEMECOIN_DESK_G12_INTAKE != 1: nothing opened, nothing read, nothing written).
Bounded: DEADLINE_S overall, TRANSPORT_TIMEOUT_S per request, no inline retry, creator scan capped at INLINE_CREATOR_PAGES (a heavier wallet is a lower bound 'capped'; the nightly worker finishes
it). Credits come from the SAME month ledger as the nightly worker (the month cap binds), plus a DAILY inline cap. Every ticket writes one g12_inline_log row (host time, corrected offset, wall ms,
credits, typed result): a typed skip or a timeout is an ABSENT row/unknown, never a launch count of 0.

DEADLINE CONTRACT (Alpha 2026-10-28, amends the earlier hard 4 s): the remaining budget is recomputed AFTER every durable ledger reserve and bounds the next request (a request starts only if >= its
2 s timeout is left, and is told min(2 s, real remainder)); an unsendable reserved attempt settles at 0 credits. The 4 s is a SOFT wall bound: urllib's timeout is a socket-idle timeout, so a slow
body read can still overshoot it (no thread, no timer). The overshoot is not hidden: g12_inline_log.wall_ms records the real wall time of every ticket."""
import time
from pathlib import Path

from sniper.services import memecoin_desk_backfill_history as H
from sniper.services import memecoin_desk_backfill_ledger as L
from sniper.services import memecoin_desk_backfill_transport as T

KNOB = "MEMECOIN_DESK_G12_INTAKE"
DEADLINE_S = 4.0
TRANSPORT_TIMEOUT_S = 2.0
INLINE_CREATOR_PAGES = 2
DAILY_CAP = 30_000
PER_TICKET_CAP = 30                                   # one coin call + two creator pages at 10 credits each
BUSY_TIMEOUT_S = 0.5                                  # an inline ticket never waits longer than this on a locked SQLite file (the nightly default is 30 s)
_LOG = """create table if not exists g12_inline_budget(id integer primary key autoincrement, day text not null, mint text not null, reserved integer not null, settled integer, ts real not null);
create index if not exists g12_inline_budget_day on g12_inline_budget(day);
create table if not exists g12_inline_log(ts real not null, day text not null, mint text not null, status text not null, reason text, coin text, creator_state text,
  credits integer not null, wall_ms real, clock_offset_s real);
create index if not exists g12_inline_log_day on g12_inline_log(day);
create index if not exists g12_inline_log_mint on g12_inline_log(mint, ts);"""
_COLLECTED = ("holders", "cluster", "dump_group", "chart")
_CHEAP = ("market_data", "token_safety", "sell_probe")


def cheap_pass(evidence_checks, collected, required=_COLLECTED):
    """True only when every gate that can refuse a coin already passes: market_data, token_safety (authorities closed), sell_probe and the four collected soft facts. A superset of the
    real PASS (later gates may still refuse); a coin any of these refuses or leaves unknown is never fetched."""
    for name in _CHEAP:
        row = (evidence_checks or {}).get(name)
        if not isinstance(row, dict) or row.get("status") != "pass":
            return False
    safety = evidence_checks["token_safety"]
    if safety.get("authority_mint") == "open" or safety.get("authority_freeze") == "open":
        return False
    # the cohort's frozen require_collected list must pass; any other collected fact may be unknown or absent (a JUDGE fact) but never a kill
    for n in _COLLECTED:
        row = collected.get(n)
        status = row.get("status") if isinstance(row, dict) else None
        if status != "pass" if n in required else status == "kill":
            return False
    return True


class Intake:
    def __init__(self, desk_dir, *, environ=None, clock=time.time, transport=None, daily_cap=DAILY_CAP, env_path=T.ENV_PATH):
        import os
        self.desk, self.environ, self.clock, self.daily_cap, self.env_path = Path(desk_dir), os.environ if environ is None else environ, clock, daily_cap, env_path
        self._transport, self._ledger, self._con = transport, None, None

    def enabled(self):
        return self.environ.get(KNOB) == "1"

    def _open(self):
        """-> None | reason. Lazily opens the ledger and history once; any refusal is a typed skip."""
        if self._ledger is not None:
            return None
        bdir = self.desk / "backfill"
        costs = T.load_costs(bdir)
        if costs is None:
            return "costs_not_calibrated"
        try:
            self._ledger = L.Ledger(bdir / "credits.db", L.load_cycle(bdir / "cycle.json"), clock=self.clock, costs=costs, busy_timeout=BUSY_TIMEOUT_S)
        except (L.Refusal, OSError, ValueError, KeyError):
            return "no_cycle"
        self._con = H.connect(bdir / "history.db", busy_timeout=BUSY_TIMEOUT_S)
        self._con.executescript(_LOG)
        if self._transport is None:
            try:
                self._transport = T.make_transport(T.helius_url(self.env_path, self.environ), timeout=TRANSPORT_TIMEOUT_S)
            except T.Refused as exc:
                self._ledger = None
                return str(exc)
        return None

    def _day(self, ts):
        import datetime
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()

    def spent_today(self):
        """Credits the day's inline budget has committed to: a reservation counts in full until it is settled, so a crash between the reserve and the settle never reopens budget."""
        if self._con is None:
            return 0
        return self._con.execute("select coalesce(sum(coalesce(settled, reserved)),0) from g12_inline_budget where day=?", (self._day(self.clock()),)).fetchone()[0]

    def _reserve_day(self, mint, now):
        """Durable, atomic daily authority BEFORE any provider call (one write transaction across the assembler and any second instance). -> row id | None when the cap is reached."""
        self._con.commit()
        self._con.execute("begin immediate")
        try:
            day = self._day(now)
            spent = self._con.execute("select coalesce(sum(coalesce(settled, reserved)),0) from g12_inline_budget where day=?", (day,)).fetchone()[0]
            if self.daily_cap - spent < PER_TICKET_CAP:
                self._con.execute("rollback")
                return None
            rid = self._con.execute("insert into g12_inline_budget(day, mint, reserved, ts) values(?,?,?,?)", (day, mint, PER_TICKET_CAP, now)).lastrowid
            self._con.execute("commit")
            return rid
        except BaseException:
            try:
                self._con.execute("rollback")
            except Exception:
                pass
            raise

    def _settle_day(self, rid, credits):
        try:
            self._con.execute("update g12_inline_budget set settled=? where id=?", (min(int(credits), PER_TICKET_CAP), rid))
            self._con.commit()
        except Exception:
            pass                                          # an unsettled reservation keeps counting in full: conservative, never reopened
    def fetch(self, mint, *, deadline_s=DEADLINE_S):
        """-> {status: skipped|fetched|error, reason, coin, creator_state, credits, wall_ms}. Never raises."""
        if not self.enabled():
            return {"status": "skipped", "reason": "knob_off", "credits": 0}
        started = self.clock()
        rid = None
        try:
            why = self._open()
            if why is not None:
                return self._log(started, mint, {"status": "skipped", "reason": why, "credits": 0})
            rid = self._reserve_day(mint, started)
            if rid is None:
                return self._log(started, mint, {"status": "skipped", "reason": "daily_cap", "credits": 0})
            self._ledger.start_run(PER_TICKET_CAP)
            f = H.Fetcher(self._con, self._ledger, self._transport, clock=self.clock, creator_page_cap=INLINE_CREATOR_PAGES, rate_retry=False, call_timeout=TRANSPORT_TIMEOUT_S, via="inline")
            f.deadline = started + deadline_s
            coin = f.fetch_coin(mint)
            creator_state, reason = None, None if coin == "known" else coin
            if coin == "known":
                creator = self._con.execute("select creator from coin_creation where mint=?", (mint,)).fetchone()[0]
                if creator:
                    creator_state = f.fetch_creator(creator)
                else:
                    creator_state = "creator_unknown"
                if creator_state not in ("complete", "pagination_incomplete"):
                    reason = creator_state
            self._settle_day(rid, self._ledger.run_spent)
            return self._log(started, mint, {"status": "fetched", "reason": reason, "coin": coin, "creator_state": creator_state, "credits": self._ledger.run_spent})
        except Exception as exc:                                                    # a failed fetch is a typed row, never an assembler crash; the day reservation stays unsettled = counted in full
            return self._log(started, mint, {"status": "error", "reason": type(exc).__name__, "credits": getattr(self._ledger, "run_spent", 0) if self._ledger else 0})

    def _log(self, started, mint, out):
        now = self.clock()
        out["wall_ms"] = round((now - started) * 1000, 1)
        if self._con is not None:
            try:
                self._con.execute("insert into g12_inline_log values(?,?,?,?,?,?,?,?,?,?)", (now, self._day(now), mint, out["status"], out.get("reason"), out.get("coin"),
                                  out.get("creator_state"), int(out.get("credits") or 0), out["wall_ms"], H._host_offset()))
                self._con.commit()
            except Exception:
                pass
        return out
