"""Helius history backfill: cycle attestation, persistent credit ledger and quota classification
(docs/superpowers/specs/2026-10-27-helius-history-backfill-design.md sections 2 and 7). Offline logic only: this module makes no provider call.

Reserve-before-send: an upper-bound cost is committed to disk BEFORE every attempt. A clean response becomes `committed` at its actual cost; a timeout, lost response, crash or malformed
body stays charged in full (`reserved` / `ambiguous`), and a restart never refunds it. A method with no entry in the cost table is refused. Spend is summed by epoch_id in ONE database,
never in the daily output files. `max usage` writes a stop for the whole UTC night. The code never writes the cycle file."""
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sniper.util.clock import real_utc_now

SIZING_MARGIN = 1.25
MAX_RATE_ATTEMPTS = 4                     # the first attempt plus 3 retries per page, each reserved and charged
COSTS = {}                                # method -> upper-bound credits. EMPTY until the prototype run calibrates it against the dashboard: empty refuses everything
_NUMS = ("plan_credits", "used_at_activation", "month_cap", "funded_reserve")


class Refusal(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s %s" % (code, detail))
        self.code, self.detail = code, detail


def _credits(name, v, allow_zero=True):
    if isinstance(v, bool) or not isinstance(v, int) or v < 0 or (v == 0 and not allow_zero):
        raise Refusal("credits_invalid", "%s=%r" % (name, v))
    return v


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError):
        raise Refusal("cycle_invalid", "bad timestamp %r" % (s,))


def load_cycle(path, *, now=None):
    now = real_utc_now() if now is None else now
    try:
        raw = Path(path).read_text()
    except OSError:
        raise Refusal("no_cycle_attestation", str(path))
    try:
        c = json.loads(raw)
    except ValueError:
        raise Refusal("cycle_invalid", "not json")
    if not isinstance(c, dict) or not c.get("epoch_id"):
        raise Refusal("cycle_invalid", "shape")
    for k in _NUMS:
        v = c.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or (k != "used_at_activation" and v <= 0):
            raise Refusal("cycle_invalid", k)
    start, end, att = _ts(c.get("epoch_start")), _ts(c.get("epoch_end")), _ts(c.get("attested_at"))
    if end <= start:
        raise Refusal("cycle_invalid", "epoch_end <= epoch_start")
    if start > now:
        raise Refusal("cycle_not_started")
    if now >= end:
        raise Refusal("cycle_expired")
    if att > now:
        raise Refusal("attested_at_future")
    if att < start:
        raise Refusal("attested_at_outside_epoch")
    if c["plan_credits"] - c["used_at_activation"] - c["month_cap"] < SIZING_MARGIN * c["funded_reserve"]:
        raise Refusal("cycle_unsized", "plan - used - month_cap < %.2f x funded_reserve" % SIZING_MARGIN)
    return dict(c, _start=start, _end=end)


def classify(status, body):
    """quota (`max usage`, HTTP 402/403/429 body OR JSON-RPC error in a 200), rate (429 without it), rpc_error, http_error, malformed, ok."""
    text = (body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")).lower()
    if status != 200:
        if status in (402, 403, 429) and "max usage" in text:
            return "quota"
        return "rate" if status == 429 else "http_error"
    try:
        obj = json.loads(text)
    except ValueError:
        return "malformed"
    if not isinstance(obj, dict):
        return "malformed"
    if "error" in obj:
        err = obj["error"]
        msg = str(err.get("message", "") if isinstance(err, dict) else err).lower()
        return "quota" if "max usage" in msg else "rpc_error"
    return "ok" if "result" in obj else "malformed"


class Ledger:
    def __init__(self, path, cycle, *, clock=real_utc_now, costs=None, busy_timeout=30):
        self.cycle, self.clock = cycle, clock
        self.costs = COSTS if costs is None else costs
        self.epoch = cycle["epoch_id"]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path, timeout=busy_timeout, isolation_level=None)
        self.con.execute("pragma journal_mode=wal")
        self.con.executescript("""
create table if not exists epochs(epoch_id text primary key, identity text not null);
create table if not exists ledger(attempt_id integer primary key autoincrement, epoch_id text not null, method text not null, page text, reserved integer not null,
  committed integer, state text not null check(state in ('reserved','committed','ambiguous')), ts real not null);
create index if not exists ledger_epoch on ledger(epoch_id);
create table if not exists stops(epoch_id text not null, utc_night text not null, reason text not null, ts real not null, primary key(epoch_id, utc_night));
""")
        ident = json.dumps({k: cycle[k] for k in ("epoch_start", "epoch_end", "plan_credits", "used_at_activation")}, sort_keys=True)
        self.con.execute("begin immediate")
        row = self.con.execute("select identity from epochs where epoch_id=?", (self.epoch,)).fetchone()
        if row is None:
            for other_id, other in self.con.execute("select epoch_id, identity from epochs").fetchall():        # a renamed or overlapping cycle must not open a fresh zero-spend bucket
                o = json.loads(other)
                if _ts(o["epoch_start"]) < cycle["_end"] and cycle["_start"] < _ts(o["epoch_end"]):
                    self.con.execute("rollback")
                    raise Refusal("epoch_overlap", "%s overlaps %s" % (self.epoch, other_id))
            self.con.execute("insert into epochs values(?,?)", (self.epoch, ident))
        elif row[0] != ident:
            self.con.execute("rollback")
            raise Refusal("epoch_identity_changed", self.epoch)
        self.con.execute("commit")
        self.run_cap, self.run_spent, self.pages, self.bound_breach = None, 0, {}, False

    def _night(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()

    def total(self):
        return self.con.execute("select coalesce(sum(case when state='committed' then committed else reserved end),0) from ledger where epoch_id=?", (self.epoch,)).fetchone()[0]

    def counts(self):
        return dict(self.con.execute("select state, count(*) from ledger where epoch_id=? and state!='reserved' group by state", (self.epoch,)).fetchall())

    def budget(self, run_cap):
        return max(0, min(run_cap, int(self.cycle["month_cap"]) - self.total()))

    def start_run(self, run_cap):
        self.run_cap, self.run_spent, self.pages = int(run_cap), 0, {}

    def record_stop(self, reason):
        self.con.execute("insert or ignore into stops values(?,?,?,?)", (self.epoch, self._night(), reason, self.clock()))

    def stopped(self):
        return self.con.execute("select reason from stops where epoch_id=? and utc_night=?", (self.epoch, self._night())).fetchone() is not None

    def rate_retry_allowed(self, page):
        return self.pages.get(page, 0) < MAX_RATE_ATTEMPTS

    def reserve(self, method, page=None):
        cost = self.costs.get(method)
        if cost is None:
            raise Refusal("unknown_method_cost", method)
        _credits("cost[%s]" % method, cost, allow_zero=False)
        self.con.execute("begin immediate")                        # check and insert atomically even if a second cron instance overlaps
        try:
            now = self.clock()
            if now >= self.cycle["_end"]:
                raise Refusal("cycle_expired")
            if self.stopped():
                raise Refusal("quota_stop")
            if self.bound_breach:
                raise Refusal("cost_bound_exceeded")
            if self.total() + cost > self.cycle["month_cap"]:
                raise Refusal("month_cap")
            if self.run_cap is not None and self.run_spent + cost > self.run_cap:
                raise Refusal("run_cap")
            cur = self.con.execute("insert into ledger(epoch_id, method, page, reserved, state, ts) values(?,?,?,?,'reserved',?)", (self.epoch, method, page, cost, now))
        except BaseException:
            self.con.execute("rollback")
            raise
        self.con.execute("commit")                                  # on disk BEFORE the request is sent
        self.run_spent += cost
        if page is not None:
            self.pages[page] = self.pages.get(page, 0) + 1
        return cur.lastrowid

    def commit(self, attempt_id, actual=None):
        """Only the attempt this ledger reserved, in this epoch, still open (`reserved`). The charge is the actual cost the response reported (integer >= 0), else the documented reservation.
        A committed or ambiguous row is terminal: a second commit, or a late commit after the send was marked ambiguous, is refused and changes nothing."""
        if actual is not None:
            _credits("actual", actual)
        self.con.execute("begin immediate")                         # settlement and the over-bound stop are ONE transaction: a crash cannot keep the paid amount and lose the stop
        try:
            row = self.con.execute("select reserved from ledger where attempt_id=? and epoch_id=? and state='reserved'", (attempt_id, self.epoch)).fetchone()
            cur = self.con.execute("update ledger set state='committed', committed=coalesce(?, reserved) where attempt_id=? and epoch_id=? and state='reserved'",
                                   (actual, attempt_id, self.epoch))
            if cur.rowcount != 1 or row is None:
                raise Refusal("attempt_not_open", str(attempt_id))
            if actual is not None and actual > row[0]:
                self.record_stop("cost_bound_exceeded")             # the reported cost broke the documented upper bound: the cost table is wrong, stop for the UTC night
        except BaseException:
            self.con.execute("rollback")
            raise
        self.con.execute("commit")
        if actual is not None:
            self.run_spent += actual - row[0]                       # the run's spend follows what was actually paid (up OR down); ambiguous rows are never refunded
            if actual > row[0]:
                self.bound_breach = True

    def mark_ambiguous(self, attempt_id):
        cur = self.con.execute("update ledger set state='ambiguous' where attempt_id=? and epoch_id=? and state='reserved'", (attempt_id, self.epoch))
        if cur.rowcount != 1:
            raise Refusal("attempt_not_open", str(attempt_id))
