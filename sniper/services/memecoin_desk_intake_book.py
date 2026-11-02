"""Desk intake emitter, inside the universe worker (spec 2026-10-27-desk-intake-candidate-path-design.md section 2 + 11;
design change GO by Alpha/Delta 2026-10-27): a coin an intake feed listed in the last MAX_AGE_S becomes a PumpSwap desk
candidate on its NEXT proven BUY print, built by the same state_from_print the band book uses (no new price math).

Clocks: the sighting age is corrected UTC with the row's own clock_offset_s (never diffed with host time); the candidate
is the print's own host receipt. Durability: every claim stores its canonical candidate bytes BEFORE the caller appends
them (pending -> emitted); a restart re-appends stored bytes, never a rebuilt candidate. Cooldown is a sliding window on
(feed, network_id, mint). Every sighting ends in one outcome row (claimed -> emitted after the durable append /
cooldown / no_print). Only a print RECEIVED after the sighting (host clocks) can trigger it.

Nothing here may stop or slow the PumpSwap tape: the worker calls only on_print / reload / take_pending / committed,
each wrapped by the worker in its own try/except.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_pumpswap_candidates as P

CUT_BASIS = "batch_end"   # cohort D Amendment 36 (Gamma): emission_at = the end of the trigger print's receipt batch


def batch_end(received_at):
    """Am37: the first microsecond boundary strictly after received_at. Every reader's received_at < cut bound then
    holds the trigger receipt's whole microsecond bucket (rows in that bucket included, even received_at + 1 ULP) and
    no receipt at or after the boundary, even one that arrived before the worker processed this print. ISO
    round-trips exactly (emitted_at == emission_at)."""
    if type(received_at) not in (int, float) or not math.isfinite(received_at):
        raise ValueError("received_at")
    cut = (math.floor(received_at * 1_000_000) + 1) / 1_000_000
    while datetime.fromtimestamp(cut, timezone.utc).timestamp() <= received_at:
        cut = math.nextafter(cut, math.inf)
    return datetime.fromtimestamp(cut, timezone.utc).timestamp()

DDL = """
CREATE TABLE IF NOT EXISTS claims (opportunity_id TEXT PRIMARY KEY, feed TEXT NOT NULL, filter TEXT NOT NULL,
    network_id TEXT NOT NULL, mint TEXT NOT NULL, intake_row_ref TEXT NOT NULL, claimed_at_host REAL NOT NULL,
    candidate_json TEXT NOT NULL, candidate_sha256 TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','emitted')));
CREATE INDEX IF NOT EXISTS claims_key ON claims(feed, network_id, mint, claimed_at_host);
CREATE TABLE IF NOT EXISTS fx_deferrals (network_id TEXT NOT NULL, mint TEXT NOT NULL,
    first_fx_deferred_at REAL NOT NULL, deferral_prints INTEGER NOT NULL, first_ticket_at REAL,
    PRIMARY KEY (network_id, mint));
CREATE TABLE IF NOT EXISTS outcomes (intake_row_ref TEXT PRIMARY KEY, feed TEXT NOT NULL, filter TEXT NOT NULL,
    network_id TEXT NOT NULL, mint TEXT NOT NULL, reason TEXT NOT NULL, opportunity_id TEXT, at_host REAL NOT NULL);
"""
FEEDS = ("pumpfun",)              # legacy default; extra sources require the active cohort's frozen allowlist
MAX_AGE_S = 120.0
COOLDOWN_S = 3600.0
RELOAD_S = 5.0
RELOAD_ROWS = 5000                # bounded read per feed day file per reload
MAX_LISTED_POOLS = 2000           # Alpha F1: listed pools join M while listed; a cap bounds the extra tape rows


def _utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def opportunity_id(network_id, mint, feed, filt, row_ref):
    key = "|".join((network_id, mint, feed, filt, row_ref))
    return "desk:U:intake:" + hashlib.sha256(key.encode()).hexdigest()[:32]


class IntakeBook:
    def __init__(self, *, intake_dir, db_path, feeds=FEEDS, max_age_s=MAX_AGE_S, cooldown_s=COOLDOWN_S,
                 reload_s=RELOAD_S, reserve_max_age_s=60, sol_max_age_s=120):
        if (not isinstance(feeds, (tuple, list)) or not feeds
                or any(not isinstance(f, str) or f not in K.INTAKE_FEEDS for f in feeds)
                or len(set(feeds)) != len(feeds)):
            raise ValueError("intake_feeds")
        self.intake_dir, self.feeds = Path(intake_dir), tuple(feeds)
        self.max_age_s, self.cooldown_s, self.reload_s = max_age_s, cooldown_s, reload_s
        self.reserve_max_age_s, self.sol_max_age_s = reserve_max_age_s, sol_max_age_s
        self.db = sqlite3.connect(db_path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(DDL)
        self.listed = {}              # mint -> {(feed, filter): sighting}, freshest per (feed, filter)
        self.pools = {}               # canonical PumpSwap pool -> listed mint (worker: M while listed, F2 binding)
        self.loaded_at = None
        self.stats = {"reload_errors": 0, "sightings": 0, "claimed": 0, "emitted": 0, "cooldown": 0, "no_print": 0,
                      "build_failed": 0,
                      "pools": 0, "pools_capped": 0}

    # ---- sightings
    def due(self, now):
        return self.loaded_at is None or now - self.loaded_at >= self.reload_s

    def reload(self, now):
        self.apply_listed(self.read_listed(now), now)

    def read_listed(self, now):
        """The listed set from the intake day files (today + yesterday, read-only, bounded). Own connections only, so the
        worker runs it in a thread off its write path; apply_listed then runs on the worker's thread."""
        fresh, errors = {}, 0
        for feed in self.feeds:
            for day in sorted({_utc_day(now - 86400), _utc_day(now)}):
                path = self.intake_dir / feed / ("%s.db" % day)
                if not path.is_file():
                    continue
                try:
                    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=0.5)
                    try:
                        rows = conn.execute(
                            "SELECT feed, filter, network_id, mint, provider_rank, rank_kind, observed_at, host_received_at, "
                            "clock_offset_s, source_ref FROM intake_rows WHERE host_received_at > ? ORDER BY id DESC LIMIT ?",
                            (now - 4 * self.max_age_s, RELOAD_ROWS)).fetchall()
                    finally:
                        conn.close()
                except sqlite3.Error:
                    errors += 1
                    continue
                for feed_, filt, network_id, mint, rank, kind, observed, host_rx, offset, ref in rows:
                    if feed_ != feed:
                        errors += 1
                        continue
                    if network_id != "solana" or not self._fresh(observed, offset, now):
                        continue
                    s = {"feed": feed_, "filter": filt, "provider_rank": rank, "rank_kind": kind,
                         "observed_at": observed, "host_received_at": host_rx, "clock_offset_s": offset,
                         "intake_row_ref": "%s:%s" % (day, ref)}   # source_ref restarts per day file: the day makes it unique
                    best = fresh.setdefault(mint, {}).get((feed_, filt))
                    if best is None or observed > best["observed_at"]:
                        fresh[mint][(feed_, filt)] = s
        from sniper.services import memecoin_desk_safety as S
        pools = {}
        for mint in sorted(fresh, key=lambda m: -max(s["observed_at"] for s in fresh[m].values())):   # freshest first
            if len(pools) >= MAX_LISTED_POOLS:
                break
            try:
                pools[S.pumpswap_pool_address(mint)] = mint   # canonical PDA of (mint, WSOL)
            except Exception:
                errors += 1                                   # not a valid base58 mint: never a pool
        return fresh, errors, pools, len(fresh) - len(pools)

    def apply_listed(self, read, now):
        """Swap in the new listed set; sightings that aged out without a candidate get outcome 'no_print'."""
        fresh, errors, pools, capped = read
        self.stats["reload_errors"] += errors
        if capped > 0:
            import logging
            logging.getLogger(__name__).warning("desk intake listed pools capped at %d; %d listed mints get no M slot",
                                                MAX_LISTED_POOLS, capped)
        self.pools = pools
        self.stats.update(pools=len(pools), pools_capped=capped)
        for mint, by in self.listed.items():                    # aged out, never printed
            for key, s in by.items():
                if (fresh.get(mint) or {}).get(key, {}).get("intake_row_ref") != s["intake_row_ref"]:
                    self._outcome(s, mint, "no_print", None, now)
        self.listed, self.loaded_at = fresh, now
        self.stats["sightings"] = sum(len(v) for v in fresh.values())

    def _fresh(self, observed, offset, now):
        """Corrected-UTC age of a sighting in [0, max_age_s]; an unknown offset or observed_at never qualifies."""
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (observed, offset)):
            return False
        age = (now - offset) - observed
        return 0 <= age <= self.max_age_s

    # ---- prints
    def fx_deferred(self, mint, now):
        """Cohort D Amendment 39: one row per coin (network_id, mint), insert-only on its first FX deferral; later
        deferrals only count prints (diagnostic); first_ticket_at is set once when the coin first tickets."""
        self.db.execute("INSERT OR IGNORE INTO fx_deferrals VALUES ('solana', ?, ?, 0, NULL)", (mint, now))
        self.db.execute("UPDATE fx_deferrals SET deferral_prints=deferral_prints+1 WHERE network_id='solana' AND mint=?",
                        (mint,))
        self.db.commit()

    def on_print(self, *, event, mapping, metadata, slot, signature, ordinal, tx_index, now, received_at, sol_usd,
                 sol_usd_at):
        """Candidates (pending, stored) for every fresh sighting of this print's mint; the caller appends each one's
        candidate_json and then calls committed(opportunity_id)."""
        mint = (mapping or {}).get("mint") if isinstance(mapping, dict) else None
        by = self.listed.get(mint)
        if not by:
            return []
        try:
            cut = batch_end(received_at)        # Am36: the cut is the end of the trigger's receipt batch, not processing time
            pool, mint, state, mcap, liq, at = P.state_from_print(
                event=event, mapping=mapping, metadata=metadata, slot=slot, signature=signature, ordinal=ordinal,
                tx_index=tx_index, now=cut, received_at=received_at, sol_usd=sol_usd, sol_usd_at=sol_usd_at,
                reserve_max_age_s=self.reserve_max_age_s, sol_max_age_s=self.sol_max_age_s, metadata_optional=True)
        except (KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError):
            self.stats["build_failed"] += 1
            return []                 # not every print proves a state (sells, unproven virtual quote): wait for the next
        out = []
        for key, s in list(by.items()):
            if not self._fresh(s["observed_at"], s["clock_offset_s"], now):
                continue
            # causal (Zeta P1): the print must be RECEIVED after the sighting was received; both host clocks. A queued
            # pre-listing print never supplies the signal, and the sighting stays for a genuinely later print.
            if not (type(s["host_received_at"]) in (int, float) and received_at > s["host_received_at"]):
                continue
            del by[key]
            if self._cooling(s["feed"], mint, now):
                self._outcome(s, mint, "cooldown", None, now)
                continue
            oid = opportunity_id("solana", mint, s["feed"], s["filter"], s["intake_row_ref"])
            candidate = P.candidate_from_state(pool=pool, mint=mint, state=state, mcap=mcap, liq=liq, at=at, slot=slot,
                                               signature=signature, ordinal=ordinal, ticket_id=oid,
                                               extra={"intake": dict(s)})
            K.validate_candidate(candidate)
            body = json.dumps(candidate, sort_keys=True)
            # no schema change (Zeta HOLD 418dbcabd: a rollback writer inserts 10 values): processed_at is claimed_at_host,
            # and cut_basis is carried by the stored candidate itself (emission_at == batch_end(reserve_known_at))
            # The durable claim and its deferral completion must survive a crash together.
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO claims VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (oid, s["feed"], s["filter"], "solana", mint, s["intake_row_ref"], now, body,
                                 hashlib.sha256(body.encode()).hexdigest(), "pending"))
                self.db.execute("UPDATE fx_deferrals SET first_ticket_at=? WHERE network_id='solana' AND mint=? "
                                "AND first_ticket_at IS NULL", (now, mint))
            self._outcome(s, mint, "claimed", oid, now)     # 'emitted' only after the durable append (Zeta P2)
            out.append((oid, body))
        return out

    def _cooling(self, feed, mint, now):
        return self.db.execute("SELECT 1 FROM claims WHERE feed=? AND network_id='solana' AND mint=? AND claimed_at_host>? "
                               "LIMIT 1", (feed, mint, now - self.cooldown_s)).fetchone() is not None

    def _outcome(self, s, mint, reason, oid, now):
        self.db.execute("INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?,?,?,?)",
                        (s["intake_row_ref"], s["feed"], s["filter"], "solana", mint, reason, oid, now))
        self.db.commit()
        self.stats[reason] += 1

    # ---- durability
    def take_pending(self):
        """[(opportunity_id, stored candidate bytes)] not yet appended: re-appended as-is after a restart."""
        return self.db.execute("SELECT opportunity_id, candidate_json FROM claims WHERE status='pending' AND feed IN ("
                               + ",".join("?" for _ in self.feeds) + ") ORDER BY claimed_at_host", self.feeds).fetchall()

    def committed(self, oid):
        self.db.execute("UPDATE claims SET status='emitted' WHERE opportunity_id=?", (oid,))
        self.db.execute("UPDATE outcomes SET reason='emitted' WHERE opportunity_id=?", (oid,))
        self.db.commit()
        self.stats["emitted"] += 1
