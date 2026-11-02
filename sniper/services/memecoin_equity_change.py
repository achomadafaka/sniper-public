"""Equity change breakdown: total equity once a minute, and a window's change split into Trading,
native price drift and Other.

Operator order 2026-10-08 ~17:40Z (via Alpha). Spec
docs/superpowers/specs/2026-10-08-memecoin-equity-change-breakdown-design.md.

DISPLAY ONLY. Nothing here is read by an entry, an exit or a sizing decision. It runs on the native
float sampler's thread after each sample and composes equity with the hero's formula (cash + native
USD + deployed - reserved + unrealized, per network) from reads that already happen: the sampler's
own chain units and Binance prices, the Solana USDC the slot-lane prefetch recorded, one public Arc
USDC read every 5 min, and the open book from `real_accounting` on a read-only ledger handle. It
takes no lock the fill path takes and calls no funded seam.

Other = change - Trading - drift is always published, with its sign; a missing network, price or
book makes the sample's equity unknown (None), never $0.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

from sniper.services import memecoin_native_float as nf

logger = logging.getLogger(__name__)

ARC = "5042"
ARC_PERIOD_S = 300.0               # the Arc USDC read, on this thread, to the chain-owner endpoint
START_SLACK_S = 150.0              # a window's start sample is the first one this soon after it
MARK_MAX_AGE_S = 900.0             # a stored position mark older than this is no mark
STALE_S = 180.0
RETENTION_S = 30 * 86400
ASSETS = ("ETH", "BNB", "SOL")


def networks():
    """The hero's networks: Solana and every funded EVM chain."""
    from sniper.settings import memecoin as cfg
    return ["solana"] + sorted(str(c) for c in cfg.MEMECOIN_EVM_ALLOWLIST)


def window_starts(now):
    """{window: start epoch}: Today is the UTC day (the hero's `now[:10]`), 24h is the last day."""
    day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return {"today": day.timestamp(), "24h": now - 86400.0}


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


# ------------------------------------------------------------------ the Arc read

def arc_usdc(post, address):
    """Arc USDC (its ERC-20 face, 6 decimals) from one batched call with an eth_chainId proof, or None
    (unknown, never zero). Arc's native gas asset IS this balance, so its native USD is 0."""
    try:
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        quote = CHAIN_QUOTE[ARC]
        data = "0x70a08231" + str(address).lower().replace("0x", "").rjust(64, "0")
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
                 {"jsonrpc": "2.0", "id": 2, "method": "eth_call",
                  "params": [{"to": quote["token"], "data": data}, "latest"]}]
        answers = {a.get("id"): a.get("result") for a in post(batch) if isinstance(a, dict)}
        if str(answers.get(1) or "").lower() != hex(int(ARC)):
            logger.warning("equity change: Arc endpoint answered chain %r", answers.get(1))
            return None
        return int(answers[2], 16) / 10 ** int(quote["decimals"])
    except Exception as exc:
        logger.warning("equity change: Arc USDC read failed: %s: %s", type(exc).__name__, exc)
        return None


# ------------------------------------------------------------------ the sample

def _chain_row(snapshot, chain):
    asset = (snapshot.get("assets") or {}).get(nf.ASSET_OF_CHAIN.get(chain)) or {}
    row = next((c for c in asset.get("chains") or () if c.get("chain") == chain), None)
    return row, asset.get("price_usd")


def _parts(net, now, snapshot, solana_cash, arc_cash):
    """({cash_usd, native_usd, observed_at}, None) or (None, what is missing)."""
    if net == ARC:
        if arc_cash is None or now - arc_cash[1] > nf.UNITS_MAX_AGE_S:
            return None, "cash"
        return {"cash_usd": arc_cash[0], "native_usd": 0.0, "observed_at": arc_cash[1]}, None
    if snapshot is None or net not in nf.ASSET_OF_CHAIN:
        return None, "no_source" if snapshot is not None else "units"
    row, price = _chain_row(snapshot, net)
    if row is None or row.get("units") is None or row.get("age_s") is None:
        return None, "units"
    if price is None:
        return None, "price"
    seen = snapshot["ts"] - row["age_s"]
    if net == "solana":
        if solana_cash is None:
            return None, "cash"
        return {"cash_usd": solana_cash[0], "native_usd": row["native"] * price,
                "observed_at": min(seen, solana_cash[1])}, None
    if row.get("wrapped") is None:
        return None, "units"
    return {"cash_usd": row["wrapped"] * price, "native_usd": row["native"] * price, "observed_at": seen}, None


def compose(now, snapshot, *, nets, solana_cash, arc_cash, book):
    """One sample: per network and in total. `book` is real_accounting's dict, or None when the
    ledger could not be read. `equity_usd` is None unless every network and the book are known.
    An open position without a fresh mark (real_accounting's unquotable_n, which it keeps at cost)
    makes its network unknown ("marks") and the sample's unrealized unknown (Gamma 2026-10-08)."""
    missing, by_network, total = [], {}, 0.0
    book_nets = (book or {}).get("by_network") or {}
    if book is None:
        missing.append("book")
    unmarked = any(int(b.get("unquotable_n") or 0) > 0 for b in book_nets.values())
    # A position on a chain outside the list is named, as the hero lists it among balances_missing.
    for net in list(nets) + [n for n in book_nets if n not in nets]:
        parts, why = _parts(net, now, snapshot, solana_cash, arc_cash)
        b = book_nets.get(net) or {"deployed": 0.0, "reserved": 0.0, "unrealized": 0.0}
        open_value = None if book is None else b["deployed"] - b.get("reserved", 0.0) + b["unrealized"]
        unrealized = None if book is None else b["unrealized"]
        if int(b.get("unquotable_n") or 0) > 0:
            open_value = unrealized = None
            why = why or "marks"
        row = {"cash_usd": None, "native_usd": None, "open_value_usd": open_value,
               "unrealized_usd": unrealized, "observed_at": None,
               "equity_usd": None, "missing": why}
        if parts is not None:
            row.update(parts)
            if open_value is not None:
                row["equity_usd"] = parts["cash_usd"] + parts["native_usd"] + open_value
        if row["equity_usd"] is None:
            if why is not None:
                missing.append(net)
        else:
            total += row["equity_usd"]
        by_network[net] = row
    return {"ts": float(now), "equity_usd": None if missing else total,
            "missing_why": {net: row["missing"] for net, row in by_network.items() if net in missing},
            "unrealized_usd": None if book is None or unmarked else book.get("unrealized_total"),
            "missing": missing, "by_network": by_network}


# ------------------------------------------------------------------ storage

class Store:
    """data/memecoin_equity.db: its own SQLite file, never the ledger; one connection, this thread."""

    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=5)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS equity_samples(ts REAL PRIMARY KEY, equity_usd REAL,"
            " unrealized_usd REAL, missing TEXT);"
            "CREATE TABLE IF NOT EXISTS equity_by_network(ts REAL NOT NULL, network TEXT NOT NULL,"
            " equity_usd REAL, cash_usd REAL, native_usd REAL, open_value_usd REAL, unrealized_usd REAL,"
            " observed_at REAL, PRIMARY KEY(network, ts));")

    def add(self, sample):
        self.db.execute("INSERT OR REPLACE INTO equity_samples VALUES (?,?,?,?)",
                        (sample["ts"], sample["equity_usd"], sample["unrealized_usd"],
                         json.dumps({"nets": sample["missing"], "why": sample.get("missing_why") or {}})))
        self.db.executemany(
            "INSERT OR REPLACE INTO equity_by_network VALUES (?,?,?,?,?,?,?,?)",
            [(sample["ts"], net, r["equity_usd"], r["cash_usd"], r["native_usd"], r["open_value_usd"],
              r["unrealized_usd"], r["observed_at"]) for net, r in sample["by_network"].items()])
        self.db.commit()

    def _sample(self, row):
        if row is None:
            return None
        missing = json.loads(row[3] or "{}")
        return {"ts": float(row[0]), "equity_usd": row[1], "unrealized_usd": row[2],
                "missing": missing.get("nets") or [], "missing_why": missing.get("why") or {}}

    def first_ts(self):
        row = self.db.execute("SELECT MIN(ts) FROM equity_samples").fetchone()
        return None if row is None or row[0] is None else float(row[0])

    def first_at_or_after(self, t0):
        return self._sample(self.db.execute(
            "SELECT ts, equity_usd, unrealized_usd, missing FROM equity_samples WHERE ts>=? "
            "ORDER BY ts LIMIT 1", (float(t0),)).fetchone())

    def first_in(self, t0, t1):
        return self._sample(self.db.execute(
            "SELECT ts, equity_usd, unrealized_usd, missing FROM equity_samples WHERE ts>=? AND ts<=? "
            "ORDER BY ts LIMIT 1", (float(t0), float(t1))).fetchone())

    def prune(self, now):
        cut = float(now) - RETENTION_S
        self.db.execute("DELETE FROM equity_samples WHERE ts<?", (cut,))
        self.db.execute("DELETE FROM equity_by_network WHERE ts<?", (cut,))
        self.db.commit()


# ------------------------------------------------------------------ windows

def _drift(nf_store, t0, t1):
    """(USD, unknown assets) over [t0, t1] from the native float store; USD None when any is unknown."""
    if nf_store is None:
        return None, list(ASSETS)
    total, unknown = 0.0, []
    for asset in ASSETS:
        got = nf.drift(nf_store.prices_since(asset, t0 - 120.0), nf_store.units_since(asset, t0 - 120.0), t0, t1)
        if got is None:
            unknown.append(asset)
        else:
            total += got
    return (None if unknown else total), unknown


def window(store, nf_store, realized_fn, *, start, end):
    """One window from its start epoch to the `end` sample: always every key, Other included."""
    w = {"status": None, "start": start, "start_at": None, "end_at": end["ts"],
         "partial": False, "since_at": None, "since": None,
         "equity_start": None, "equity_now": end["equity_usd"], "change": None, "missing": [],
         "trading": {"usd": None, "realized": None, "closes": None, "unrealized_change": None},
         "drift": {"usd": None, "missing": []}, "other": {"usd": None, "why_unknown": None}}
    s = store.first_in(start, start + START_SLACK_S)
    if s is None:
        # PARTIAL (operator 2026-10-08): a window that began before the first sample, or in a gap, runs
        # from the first sample after its start and says so; never "not recorded yet" with two samples.
        s = store.first_at_or_after(start)
        if s is not None and s["ts"] < end["ts"]:
            w["partial"], w["since_at"] = True, s["ts"]
            w["since"] = "recording_began" if s["ts"] == store.first_ts() else "gap"
    if s is None or s["ts"] >= end["ts"]:
        w["status"] = "not_recorded_yet"
        w["other"]["why_unknown"] = w["status"]
        return w
    w["start_at"], w["equity_start"] = s["ts"], s["equity_usd"]
    if s["equity_usd"] is None:
        w["status"], w["missing"], w["missing_why"] = "unknown_at_start", s["missing"], s.get("missing_why") or {}
    elif end["equity_usd"] is None:
        w["status"], w["missing"], w["missing_why"] = "unknown_now", end["missing"], end.get("missing_why") or {}
    else:
        w["status"], w["change"] = "ok", end["equity_usd"] - s["equity_usd"]
    try:
        realized, closes = realized_fn(_iso(s["ts"]), _iso(end["ts"]))
        w["trading"].update(realized=realized, closes=closes)
        if s["unrealized_usd"] is not None and end["unrealized_usd"] is not None:
            change = end["unrealized_usd"] - s["unrealized_usd"]
            w["trading"]["unrealized_change"] = change
            if realized is not None:
                w["trading"]["usd"] = realized + change
    except Exception as exc:
        logger.warning("equity change: realized read failed: %s: %s", type(exc).__name__, exc)
    w["drift"]["usd"], w["drift"]["missing"] = _drift(nf_store, s["ts"], end["ts"])
    unknown = [name for name, v in (("equity", w["change"]), ("trading", w["trading"]["usd"]),
                                    ("drift", w["drift"]["usd"])) if v is None]
    if unknown:
        w["other"]["why_unknown"] = ", ".join(unknown) + " unknown"
    else:
        w["other"]["usd"] = w["change"] - w["trading"]["usd"] - w["drift"]["usd"]
    return w


def build_snapshot(store, nf_store, realized_fn, sample, now):
    return {"ts": float(now), "first_sample_at": store.first_ts(), "now": sample,
            "windows": {name: window(store, nf_store, realized_fn, start=start, end=sample)
                        for name, start in window_starts(now).items()}}


def read_snapshot(path, now=None):
    """The web's view: `available`, `age_s`, `stale` (older than STALE_S). Recorded values keep their
    own times; the page shows their age."""
    now = time.time() if now is None else now
    try:
        with open(path) as fh:
            snap = json.load(fh)
        ts = float(snap["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"available": False, "stale": True, "age_s": None}
    age = max(0.0, now - ts)
    return dict(snap, available=True, age_s=age, stale=age > STALE_S)


# ------------------------------------------------------------------ the recorder

class EquityRecorder:
    """Called by the native float sampler after each sample, on its thread. Never raises."""

    def __init__(self, *, state_path_fn, evm_address, post_for, connect=None, networks_fn=networks):
        self._state_path_fn, self._address, self._post_for = state_path_fn, evm_address, post_for
        self._connect, self._networks_fn = connect, networks_fn
        self._store = self._conn = None
        self._arc, self._arc_read_at = None, None

    def _ledger(self):
        """The ledger, READ-ONLY (get_readonly_connection: autocommit, query_only, no WAL pin)."""
        if self._connect is not None:
            return self._connect()
        if self._conn is None:
            from sniper.persistence import db_core
            self._conn = db_core.get_readonly_connection()
            self._conn.execute("PRAGMA busy_timeout=5000;")
        return self._conn

    def _book(self, now):
        try:
            from sniper.persistence import db_memecoin as m
            conn = self._ledger()
            # Every open position, paused ones too: each needs its own fresh mark or it is unknown.
            positions = m.list_open_positions(conn, mode="real")
            since = datetime.fromtimestamp(now - MARK_MAX_AGE_S, timezone.utc).isoformat(timespec="seconds")
            return conn, m.real_accounting(conn, price_map=m.latest_position_mids(conn, positions, since_iso=since),
                                           day=_iso(now)[:10])
        except Exception as exc:
            logger.warning("equity change: open book unknown: %s: %s", type(exc).__name__, exc)
            self._conn = None
            return None, None

    def _arc_cash(self, now):
        if self._arc_read_at is None or now - self._arc_read_at >= ARC_PERIOD_S:
            self._arc_read_at = now
            post = self._post_for(ARC) if (self._post_for and self._address) else None
            got = arc_usdc(post, self._address) if post is not None else None
            if got is not None:
                self._arc = (got, float(now))
        return self._arc

    def sample(self, nf_store, snapshot, now):
        try:
            if self._store is None:
                self._store = Store(self._state_path_fn("data", "memecoin_equity.db"))
            conn, book = self._book(now)
            sample = compose(now, snapshot, nets=self._networks_fn(),
                             solana_cash=nf.recorded_value("solana", "quote", now),
                             arc_cash=self._arc_cash(now), book=book)
            self._store.add(sample)
            self._store.prune(now)

            def realized(a, b):
                from sniper.persistence import db_memecoin as m
                if conn is None:
                    raise RuntimeError("ledger unavailable")
                return m.real_realized_between(conn, a, b)
            out = build_snapshot(self._store, nf_store, realized, sample, now)
            nf.write_json_atomic(self._state_path_fn("data", "equity_change.json"), out)
            return out
        except Exception:
            # WARNING: the funded runner's log keeps WARNING and up only.
            logger.warning("equity change: sample failed (display only; the runner is unaffected)",
                           exc_info=True)
            return None
