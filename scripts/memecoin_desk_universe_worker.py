#!/usr/bin/env python3
"""Desk producer U worker (spec docs/superpowers/specs/2026-10-21-desk-universe-producer-design.md).

Its own process and its own Shyft gRPC connection: Pump.fun program transactions + blocks_meta at
CONFIRMED. Decisions live in sniper/services/memecoin_desk_universe.py. Never imports the runner,
never opens the ledger, never reads the funded RPC lane. Raw frames stay in a byte-bounded RAM ring
except for eight bounded, first-only decoder witnesses and opt-in --wallet-flow
128MiB/30-day raw research tape. Wallet flow never changes program completeness.

    python3 scripts/memecoin_desk_universe_worker.py            # MEMECOIN_DESK_UNIVERSE_ENABLED=1
"""
import asyncio
import base64
import collections
import fcntl
import json
import logging
import math
import os
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import itertools
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402

load_env_files(str(state_root()))

from sniper.util import clock                                      # noqa: E402
from sniper.services import memecoin_desk_universe as U             # noqa: E402
from sniper.services.memecoin_desk_contract import WSOL_MINT          # noqa: E402
from sniper.services import memecoin_desk_crawler as C               # noqa: E402
from sniper.services import memecoin_desk_sell_fee_witness as SFW
from sniper.services.memecoin_desk_pumpswap import SELL_FEE_CAPTURE_KEYS,BUY_EXACT_CAPTURE_KEYS
from sniper.services import memecoin_desk_extend_shadow as ES             # noqa: E402
from sniper.services import memecoin_desk_ps_losses as PSL             # noqa: E402
from sniper.services import memecoin_desk_txshape as TXS             # noqa: E402
from sniper.services import memecoin_desk_keep_extend as KE            # noqa: E402

log = logging.getLogger("desk_universe")
KNOBS = {   # producer-owned knobs live in sniper/settings/memecoin.py (MEMECOIN_DESK_UNIVERSE_*);
            # the desk-wide caps and watch set are plumbing spec section 7 names, read here with its defaults
    "enabled": ("MEMECOIN_DESK_UNIVERSE_ENABLED", "0"),
    "url": ("MEMECOIN_DESK_UNIVERSE_GRPC_HOST", "grpc.fra.shyft.to"),   # fra: keeps the p99 tail off the funded ams feed (throttle run 3)
    "ring_mb": ("MEMECOIN_DESK_UNIVERSE_RING_MB", "64"),
    "cooldown_s": ("MEMECOIN_DESK_UNIVERSE_COOLDOWN_S", "300"),
    "sol_max_age_s": ("MEMECOIN_DESK_UNIVERSE_SOL_MAX_AGE_S", "120"),
    "band_lo": ("MEMECOIN_DESK_UNIVERSE_BAND_LO_USD", "30000"),
    "band_hi": ("MEMECOIN_DESK_UNIVERSE_BAND_HI_USD", "100000"),
    "watch_max": ("MEMECOIN_DESK_WATCH_MAX_POOLS", "50"),
    "soft_gb": ("MEMECOIN_DESK_DISK_SOFT_CAP_GB", "15"),
    "hard_gb": ("MEMECOIN_DESK_DISK_HARD_CAP_GB", "20"),
    "min_free_gb": ("MEMECOIN_DESK_MIN_FREE_GB", "20"),
    "ps_hard_gb": ("MEMECOIN_DESK_PUMPSWAP_DISK_HARD_CAP_GB", "1"),
    "ps_retention_h": ("MEMECOIN_DESK_PUMPSWAP_RETENTION_H", "2"),
    "ps_keep_window_s": ("MEMECOIN_DESK_PS_KEEP_WINDOW_S", "660"),
    # TX SHAPE tape (terminal attention, Alpha/operator 2026-10-23): hash-sampled share of swap transactions whose routing (non-core
    # programs, System transfers, compute budget) is recorded to data/desk/txshape/<day>.db. 0 = off, byte-identical to before.
    "txshape_pct": ("MEMECOIN_DESK_TXSHAPE_SAMPLE_PCT", "5"),
    "ps_resume_hyst_gb": ("MEMECOIN_DESK_PS_RESUME_HYST_GB", "0.25"),  # resume only when live PS bytes <= cap - this (must be < the cap: 0 <= hyst < cap, validated at start)
    "ps_resume_dwell_s": ("MEMECOIN_DESK_PS_RESUME_DWELL_S", "300"),    # and at least this long after the stop
    "ps_resume_max_per_h": ("MEMECOIN_DESK_PS_RESUME_MAX_PER_H", "3"),  # more than this many resumes in an hour = flapping, stay stopped
    "ps_wal_try_gb": ("MEMECOIN_DESK_PS_WAL_TRY_GB", "2"),             # a WAL above this gets a TRUNCATE checkpoint attempt each check
}
# Rolling PumpSwap retention (operator 2026-10-23, "make the pumpswap capture autoclear"). swaps_ps rows
# received more than ps_retention_h ago are deleted on the processor thread right after each 1 s commit,
# at most PS_PRUNE_BATCH rows per pass. Measured 2026-10-23 on a copy of the live tape: capture writes
# ~190 rows/s; a 500-row delete takes p50 36 ms / p90 40 ms, so a pass deletes 2.6x the insert rate.
# Freed pages go to SQLite's freelist and new rows reuse them, so the file plateaus at about one window
# of rows; no VACUUM runs. pools, gaps_ps, pricing_ps and ps_keep are small and are kept, and so is every
# swap inside a graduation's keep window (ps_keep), which later price-path reads need.
PS_PRUNE_BATCH = 500
PS_WAL_LIMIT_BYTES = 64 * 1024 * 1024
# Kept graduation rows never expire under retention, so they filled the tape (operator via Alpha 2026-10-26: research
# evidence, never delete). Windows that closed more than PS_ARCHIVE_AFTER_S ago move to data/archive/pumpswap/keep-<day>.db;
# a row leaves tape.db only after its archive copy is read back equal.
PS_ARCHIVE_AFTER_S = U.PS_ARCHIVE_AFTER_S  # Keep the public alias for archive tools and tests.
# TAPE SHRINK (operator 2026-10-23, '20 GB is insane'): full swaps_ps rows are written only for pools that MATTER (M = captured
# canonical create_pool pools U curve-derived graduations in ps_grads U ps_keep pools); every other swap is only counted in
# ps_pool_hours. A swap on a pool that is not in M yet waits in a short in-memory holdback so a graduation fact that arrives a
# few seconds later still gets the pool's first swaps, stored with the map_status computed when they were processed.
PS_HOLDBACK_S = 30.0
PS_WATCH_KEEP_S = 30 * 86400.0
PS_WATCH_DDL = ("CREATE TABLE IF NOT EXISTS ps_watch_events(signature TEXT, ordinal INTEGER, pool TEXT, mint TEXT, quote_mint TEXT, slot INTEGER, tx_index INTEGER, is_buy INTEGER, actor TEXT, "
                "base_atomic TEXT, quote_atomic TEXT, pre_quote_reserve_atomic TEXT, post_quote_reserve_atomic TEXT, received_at REAL, PRIMARY KEY(signature,ordinal))")
PS_HOLD_MAX_ROWS = 20000
PS_KEEP_BEFORE_S, PS_KEEP_AFTER_S = 60.0, 600.0     # around a graduation's create_pool receipt
# swaps_ps.payload_json keeps only what is not a typed column and that a reader needs: the as-of pricer
# reads ix_name and the virtual-quote proof (memecoin_desk_ps_asof). swaps_ps.payload_b64 is written NULL
# (operator via Alpha 2026-10-28, tape at its 50 GB cap): the raw event was ~65% of a row and no reader uses
# it; rows written before keep theirs. create_pool rows (pools) still keep the raw event.
PS_PAYLOAD_KEYS = ("ix_name", "virtual_quote_reserves", "virtual_quote_atomic", "virtual_quote_source")
WALLET_FLOW_RING_CAP = 8 * 1024 * 1024
WALLET_FLOW_FRAME_MAX = 128 * 1024
WALLET_FLOW_RECORD_MAX = 256 * 1024
WALLET_FLOW_DDL = (
    "CREATE TABLE IF NOT EXISTS wallet_flow_transactions(flow_id INTEGER PRIMARY KEY AUTOINCREMENT,signature TEXT UNIQUE,slot INTEGER,tx_index INTEGER,block_time INTEGER,received_at REAL,processed_at REAL,commitment TEXT,payload_json TEXT,payload_archived INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS wallet_flow_received ON wallet_flow_transactions(received_at)",
    "CREATE INDEX IF NOT EXISTS wallet_flow_pending_slot ON wallet_flow_transactions(slot) WHERE block_time IS NULL",
    "CREATE TABLE IF NOT EXISTS wallet_flow_meta(id INTEGER PRIMARY KEY CHECK(id=1),payload_bytes INTEGER NOT NULL,pending_metadata INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS wallet_flow_gaps(day TEXT,reason TEXT,n INTEGER,first_at REAL,last_at REAL,known_at REAL,PRIMARY KEY(day,reason)) WITHOUT ROWID",
)

FLOW_S = 60.0
SOL_QUOTE = "11111111111111111111111111111111"     # a SOL-quoted curve's quote mint (the default key)
INITIAL_REAL_TOKEN = 793_100_000 * 10**6      # Pump.fun curve real token reserve at launch (6 dp)


# Pump.fun events the opportunity decoder blocks as unknown. Names from the official IDL
# (github.com/pump-fun/pump-public-docs idl/pump.json, matched 2026-10-21). Fee and bookkeeping
# only: none carries a trade or a create, so they are counted, never decoded.
KNOWN_IRRELEVANT = {
    "e3bed7ceb0b4a584": "DistributeFeeToHoldersEvent",
    "742b4dbd117a482b": "SweepBondingCurveFeeEvent",
    "929fbdac925838f4": "CloseUserVolumeAccumulatorEvent",
    "86240d48e86582d8": "InitUserVolumeAccumulatorEvent",
    "a537817004b3ca28": "DistributeCreatorFeesEvent",
    "7a027f010ebf0caf": "CollectCreatorFeeEvent",
    "e2d6f62107f293e5": "ClaimCashbackEvent",
    "6161d7905d92167c": "ExtendAccountEvent",
    "9ba768dcd56cf303": "MigrateBondingCurveCreatorEvent",
}
# A buy after the curve completed (before migration): the mint is no longer curve-tradeable.
POST_COMPLETE_BUY = "6fb06d8b316cd5fb"


def classify_unknown_event(raw: bytes):
    """('irrelevant', name) | ('post_complete_buy', mint) | ('unknown', disc_hex) for a blocked event."""
    disc = raw[:8].hex()
    # Official Pump IDL: quote-control configuration, not a curve trade. Keep SOL changes unknown.
    if (disc == "a943276d6686b6e8" and len(raw) == 128
            and raw[72:104] not in (bytes(32), U.b58decode("So11111111111111111111111111111111111111112"))):
        return "irrelevant", "SetQuoteControlMintReservesEvent"
    if disc in KNOWN_IRRELEVANT:
        return "irrelevant", KNOWN_IRRELEVANT[disc]
    if disc == POST_COMPLETE_BUY and len(raw) >= 72:        # disc | user(32) | mint(32) | ...
        return "post_complete_buy", U.b58encode(raw[40:72])
    return "unknown", disc


def knobs(env=os.environ):
    """Env wins; otherwise the settings constant (sniper/settings/memecoin.py) when one exists."""
    import sniper.settings.memecoin as S
    out = {}
    for k, (name, default) in KNOBS.items():
        const = getattr(S, name, None)
        if const is not None and not isinstance(const, bool):
            default = str(const)
        out[k] = env.get(name, default)
    return out


def _quantile(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1) if xs else None


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


class WalletFlowConnection(sqlite3.Connection):
    """Observe outer transactions on this connection only; savepoints are not commits."""
    def _flow_outcome(self,state):
        if state=='rollback':self.wallet_flow_rollback_notifications=getattr(self,'wallet_flow_rollback_notifications',0)+1
        callback=getattr(self,'wallet_flow_outcome',None)
        if callback is not None:
            try:callback(state)
            except Exception:
                self.wallet_flow_callback_errors=getattr(self,'wallet_flow_callback_errors',0)+1

    def execute(self,*args,**kwargs):
        active=self.in_transaction
        try:return super().execute(*args,**kwargs)
        except sqlite3.Error:
            if active and not self.in_transaction:self._flow_outcome('rollback')
            raise

    def executemany(self,*args,**kwargs):
        active=self.in_transaction
        try:return super().executemany(*args,**kwargs)
        except sqlite3.Error:
            if active and not self.in_transaction:self._flow_outcome('rollback')
            raise

    def commit(self):
        active=self.in_transaction
        try:super().commit()
        except BaseException:
            self._flow_outcome('commit_failed' if self.in_transaction else 'rollback')
            raise
        self._flow_outcome('committed' if active else 'idle')

    def rollback(self):
        try:super().rollback()
        finally:self._flow_outcome('rollback')

    def __exit__(self,kind,value,trace):
        active=self.in_transaction
        try:result=super().__exit__(kind,value,trace)
        except BaseException:
            self._flow_outcome('commit_failed' if self.in_transaction else 'rollback')
            raise
        self._flow_outcome('rollback' if kind is not None else 'committed' if active else 'idle')
        return result


class DayFiles:
    """Curve daily files; one lifetime PS database gives global event identity and indexed maps."""
    PS_STREAMS = ("pools", "swaps_ps", "gaps_ps", "pricing_ps", "ps_keep", "ps_grads", "ps_decode_losses", "ps_pool_hours",
                  "ps_keep_done", "wallet_setup_events", *PSL.SCHEMAS, *ES.SCHEMAS, *SFW.SCHEMAS)
    DDL = {
        "wallet_setup_events": "CREATE TABLE IF NOT EXISTS wallet_setup_events(setup_id INTEGER PRIMARY KEY AUTOINCREMENT,signature TEXT,outer_ix INTEGER,program TEXT,payer TEXT,wallet TEXT,slot INTEGER,tx_index INTEGER,block_time INTEGER,received_at REAL,processed_at REAL,UNIQUE(signature,outer_ix))",
        "launches": "CREATE TABLE IF NOT EXISTS launches(mint TEXT PRIMARY KEY, curve TEXT, creator TEXT, "
                    "slot INTEGER, signature TEXT, block_time INTEGER, received_at REAL)",
        "swaps": "CREATE TABLE IF NOT EXISTS swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, "
                 "curve TEXT, is_buy INTEGER, quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, "
                 "virtual_quote INTEGER, virtual_token INTEGER, block_time INTEGER, received_at REAL, "
                 "real_token INTEGER, fee_bps INTEGER, creator_fee_bps INTEGER, "
                 "PRIMARY KEY(signature, ordinal)) WITHOUT ROWID",
        "candles_1s": "CREATE TABLE IF NOT EXISTS candles_1s(pool TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, "
                      "buy_usd REAL, sell_usd REAL, n_swaps INTEGER, n_makers INTEGER, slot_lo INTEGER, "
                      "slot_hi INTEGER, PRIMARY KEY(pool, t)) WITHOUT ROWID",
        "gaps": "CREATE TABLE IF NOT EXISTS gaps(at REAL, cause TEXT, last_slot INTEGER)",
        # every confirmed slot's block time, as block_meta delivers it: Yellowstone sends a slot's
        # block_meta AFTER its transactions, so swaps/launches written at receipt had NULL (Gamma)
        "blocks": "CREATE TABLE IF NOT EXISTS blocks(slot INTEGER PRIMARY KEY, block_time INTEGER, "
                  "received_at REAL) WITHOUT ROWID",
        "pools": "CREATE TABLE IF NOT EXISTS pools(pool TEXT, mint TEXT, quote_mint TEXT, pool_creator TEXT, "
                 "canonical INTEGER, slot INTEGER, signature TEXT, ordinal INTEGER, tx_index INTEGER, "
                 "event_time INTEGER, block_time INTEGER, received_at REAL, processed_at REAL, known_at REAL, "
                 "payload_json TEXT, payload_b64 TEXT, network_id TEXT, venue TEXT, program TEXT, "
                 "PRIMARY KEY(signature,ordinal)) WITHOUT ROWID",
        "swaps_ps": "CREATE TABLE IF NOT EXISTS swaps_ps(pool TEXT, mint TEXT, quote_mint TEXT, slot INTEGER, "
                    "signature TEXT, ordinal INTEGER, tx_index INTEGER, is_buy INTEGER, actor TEXT, coin_creator TEXT, "
                    "base_atomic TEXT, quote_atomic TEXT, pre_base_reserve_atomic TEXT, pre_quote_reserve_atomic TEXT, "
                    "post_base_reserve_atomic TEXT, post_quote_reserve_atomic TEXT, lp_fee_bps TEXT, protocol_fee_bps TEXT, "
                    "creator_fee_bps TEXT, cashback_fee_bps TEXT, buyback_fee_bps TEXT, event_time INTEGER, block_time INTEGER, "
                    "received_at REAL, processed_at REAL, map_known_at REAL, map_status TEXT, payload_json TEXT, "
                    "payload_b64 TEXT, network_id TEXT, venue TEXT, program TEXT)",
        "gaps_ps": "CREATE TABLE IF NOT EXISTS gaps_ps(at REAL, cause TEXT, last_slot INTEGER)",
        # one row per PS capture STOP: [start_at, end_at]; end_at NULL = still stopped; ended_by 'resume' | 'restart'.
        # A reader whose window lies INSIDE a stop sees no gaps_ps row and no swaps: it must test span overlap.
        "ps_gap_spans": "CREATE TABLE IF NOT EXISTS ps_gap_spans(start_at REAL, end_at REAL, cause TEXT, ended_by TEXT, PRIMARY KEY(start_at, cause))",
        # EVERY PumpSwap swap seen (stored or not) counted per pool and UTC hour: venue totals survive the write filter
        "ps_pool_hours": "CREATE TABLE IF NOT EXISTS ps_pool_hours(pool TEXT, hour INTEGER, swaps INTEGER, buys INTEGER, "
                         "first_slot INTEGER, last_slot INTEGER, first_received REAL, last_received REAL, "
                         "PRIMARY KEY(pool, hour)) WITHOUT ROWID",
        # graduation keep windows (Gamma 2026-10-23): retention never deletes a swap on `pool` whose
        # received_at is in [keep_from, keep_until]; written when a canonical create_pool lands
        # windows already moved to the archive (Delta 10-26): the open-time backfill below must not resurrect them
        "ps_keep_done": "CREATE TABLE IF NOT EXISTS ps_keep_done(pool TEXT PRIMARY KEY, archived_at REAL) WITHOUT ROWID",
        "ps_keep": "CREATE TABLE IF NOT EXISTS ps_keep(pool TEXT PRIMARY KEY, keep_from REAL, keep_until REAL, "
                   "signature TEXT, slot INTEGER) WITHOUT ROWID",
        # CURVE-SIDE graduations (Gamma A1 (e), 2026-10-23): the Pump curve stream's complete / migration event for a
        # mint, with the canonical PumpSwap pool DERIVED from the mint (pumpswap_pool_address: a pure function of
        # mint + WSOL + index 0). A source separate from `pools` (captured create_pool events): the coverage of
        # create_pool capture is |ps_grads pools also in pools| / |ps_grads|, and swaps on a derived-only pool map
        # with map_status 'mapped_curve_derived', never 'mapped'.
        "ps_grads": "CREATE TABLE IF NOT EXISTS ps_grads(pool TEXT, mint TEXT, quote_mint TEXT, kind TEXT, slot INTEGER, "
                    "signature TEXT, ordinal INTEGER, tx_index INTEGER, block_time INTEGER, received_at REAL, "
                    "known_at REAL, curve_quote_mint TEXT, curve_real_quote INTEGER, grad_class TEXT, "
                    "PRIMARY KEY(signature,ordinal)) WITHOUT ROWID",
        # one row per transaction whose PumpSwap events were discarded (DecodeLoss is all-or-nothing per tx): the
        # gaps_ps row only keeps cause + last_slot, so a miss could not be named
        "ps_decode_losses": "CREATE TABLE IF NOT EXISTS ps_decode_losses(signature TEXT, cause TEXT, slot INTEGER, "
                            "received_at REAL, PRIMARY KEY(signature,cause)) WITHOUT ROWID",
        "pricing_ps": "CREATE TABLE IF NOT EXISTS pricing_ps(signature TEXT, ordinal INTEGER, day TEXT, "
                      "received_at REAL, processed_at REAL, reason TEXT, PRIMARY KEY(signature,ordinal)) WITHOUT ROWID",
    }
    # columns added to an existing day file in place (the tape probe's inputs, Gamma 3a7d89c2d)
    ADDED = {"ps_grads": (("curve_quote_mint", "TEXT"), ("curve_real_quote", "INTEGER"), ("grad_class", "TEXT")),
             "swaps": (("real_token", "INTEGER"), ("fee_bps", "INTEGER"), ("creator_fee_bps", "INTEGER"),
                       # the tx's position in its block (Yellowstone TransactionInfo.index): (slot, ordinal) does
                       # not order two txs of one slot. NULL = old row or unproved proto3 default zero
                       ("tx_index", "INTEGER"),
                       # the trade event's real_sol_reserves AFTER the swap: the as-of liq reads it directly (the curve's
                       # initial virtual quote is 30 SOL on classic curves and ~180-340 SOL on others, so virtual - 30 SOL
                       # is not the real quote). NULL = a row from before this column existed
                       ("real_quote", "INTEGER"),
                       # THE QUOTE MINT, EXPLICIT (Beta/Alpha 2026-10-23): 17-19% of curve trades are on curves
                       # quoted in another token, whose legacy SOL fields are all 0. quote_mint labels every row
                       # (the default key = SOL); q_amount / q_virtual / q_real carry the event's own quote-unit
                       # amounts for a non-SOL curve (NULL on SOL rows and on rows from before these columns).
                       # quote_mint NULL = a pre-migration row: neither SOL nor non-SOL; readers must say so.
                       ("quote_mint", "TEXT"), ("q_amount", "INTEGER"), ("q_virtual", "INTEGER"),
                       ("q_real", "INTEGER"),
                       # the buyer-paid cashback leg (Alpha 2026-10-23: all-in cost was unknowable without it): the event's
                       # cashback_fee_basis_points and cashback lamports. NULL = an unparsed event or a row before this column
                       ("cashback_fee_bps", "INTEGER"), ("cashback", "INTEGER")),
             # the create event's metadata: the source scout resolves a $TICKER against it
             # the signing dev beside the curve creator (Alpha 2026-10-28): `creator` is the on-chain curve creator (fee recipient, CreateEvent.creator); `dev` is the
             # CreateEvent `user` (the signing account of the create instruction) with its own source. NULL = a row from before this column; never backfilled from a guess
             "launches": (("symbol", "TEXT"), ("name", "TEXT"), ("dev", "TEXT"), ("dev_source", "TEXT"))}
    # the assembler's tape reads: a mint's newest trade and the tape head, without a full scan
    INDEXES = {"swaps": ("CREATE INDEX IF NOT EXISTS swaps_mint_slot ON swaps(mint, slot)",
                         "CREATE INDEX IF NOT EXISTS swaps_slot ON swaps(slot)"),
               "launches": ("CREATE INDEX IF NOT EXISTS launches_slot ON launches(slot)",),
               "pools": ("CREATE INDEX IF NOT EXISTS pools_pool ON pools(pool)",),
               "ps_grads": ("CREATE INDEX IF NOT EXISTS ps_grads_pool ON ps_grads(pool)",
                            "CREATE INDEX IF NOT EXISTS ps_grads_received ON ps_grads(received_at)"),
               "swaps_ps": ("CREATE INDEX IF NOT EXISTS swaps_ps_pool_slot ON swaps_ps(pool,slot)",
                            "CREATE INDEX IF NOT EXISTS swaps_ps_slot ON swaps_ps(slot)",
                            "CREATE INDEX IF NOT EXISTS swaps_ps_received ON swaps_ps(received_at)",
                            "CREATE INDEX IF NOT EXISTS swaps_ps_pool_received ON swaps_ps(pool,received_at)"),
               "gaps_ps": ("CREATE INDEX IF NOT EXISTS gaps_ps_at ON gaps_ps(at)",),
               "ps_decode_losses": ("CREATE INDEX IF NOT EXISTS ps_decode_losses_received ON ps_decode_losses(received_at)",),
               "pricing_ps": ("CREATE INDEX IF NOT EXISTS pricing_ps_day_reason ON pricing_ps(day,reason)",)}

    DDL.update(SFW.SCHEMAS)
    INDEXES.update(SFW.INDEXES)
    DDL.update(ES.SCHEMAS)
    INDEXES.update(ES.INDEXES)
    DDL.update(PSL.SCHEMAS)
    INDEXES.update(PSL.INDEXES)

    def __init__(self, base, *, wallet_flow_outcome=None):
        self.wallet_flow_outcome=wallet_flow_outcome
        self.base, self._open = Path(base), {}
        self.ps_wal_checkpoint = None   # (busy, wal_frames, checkpointed) from the open-time PASSIVE checkpoint
        self.rowid_layout = None        # set when the PumpSwap file is opened: True = lean layout, False = old WITHOUT ROWID

    def conn(self, stream, ts):
        day = utc_day(ts)
        ps = stream in self.PS_STREAMS or stream == "ps_gap_spans"
        key = ("pumpswap", "all") if ps else (stream, day)
        if key not in self._open:
            for old in [k for k in self._open if k[0] == stream]:
                c = self._open.pop(old)
                c.commit()                          # close() would roll back the uncommitted tail
                c.close()
            path = self.base / "pumpswap" / "tape.db" if ps else self.base / stream / f"{day}.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            if ps and self.wallet_flow_outcome is not None:
                try:before=path.stat();before=(before.st_dev,before.st_ino)
                except FileNotFoundError:before=None  # Fresh state: connect creates the empty tape.
                c=sqlite3.connect(str(path),timeout=0,factory=WalletFlowConnection)
                try:
                    after=path.stat();after=(after.st_dev,after.st_ino)
                    if before is not None and before!=after:raise sqlite3.OperationalError('capture_database_identity_changed')
                except BaseException:
                    c.close();raise
                c.wallet_flow_outcome=self.wallet_flow_outcome
                c.wallet_flow_source_identity=after
                # Identity handshake detects ordinary replacement; adversarial ABA renames need an fd-level SQLite API.
            else:c = sqlite3.connect(str(path), timeout=0 if ps else 30)
            c.execute("PRAGMA journal_mode=WAL")
            if ps:
                # The 10-26 stop: an 11.4 GB WAL that never shrank. Auto-checkpoints recycle the WAL but leave the file at
                # its peak unless this limit truncates it after each reset. A reader pinning the WAL still grows it.
                c.execute(f"PRAGMA journal_size_limit={PS_WAL_LIMIT_BYTES}")
                # The limit only applies after a checkpoint, and the cap check can stop capture before the first write
                # (10-26 12:41Z restart re-stopped on a leftover 11.4 GB WAL). Flush it once at open, PASSIVE only (AGENTS routine-maintenance rule: never wait,
                # never hold the checkpoint lock): the frames go into the database, the file becomes reusable space and ps_wal_relief counts it as such.
                self.ps_wal_checkpoint = c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            for table in self.PS_STREAMS if ps else (stream,):
                existing_loss_table = table == "ps_decode_losses" and c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ps_decode_losses'").fetchone() is not None
                c.execute(self.DDL[table])
                for ddl in self.INDEXES.get(table, ()):
                    # Existing source files need explicit index provisioning; never block capture on a retained-history build.
                    if not existing_loss_table:
                        c.execute(ddl)
            if ps:
                # An OLD file keeps its WITHOUT ROWID swaps_ps (the PRIMARY KEY dedupes); a new file gets the lean layout
                # (rowid table, ~3x smaller per row: the 88-char signature is no longer the clustered key) and its UNIQUE
                # (signature, ordinal) index. Never add the unique index to an old file: it would duplicate the key.
                sql = (c.execute("SELECT sql FROM sqlite_master WHERE name='swaps_ps'").fetchone() or ("",))[0] or ""
                self.rowid_layout = "WITHOUT ROWID" not in sql.upper()
                if self.rowid_layout:
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS swaps_ps_sig_ord ON swaps_ps(signature, ordinal)")
            if ps:                                  # graduations captured before ps_keep existed
                c.execute("CREATE TABLE IF NOT EXISTS ps_keep_done(pool TEXT PRIMARY KEY, archived_at REAL) WITHOUT ROWID")
                c.execute("INSERT OR IGNORE INTO ps_keep SELECT pool, received_at - ?, received_at + ?, signature, slot "
                          "FROM pools WHERE canonical = 1 AND pool NOT IN (SELECT pool FROM ps_keep_done)",
                          (PS_KEEP_BEFORE_S, PS_KEEP_AFTER_S))
            for table in self.PS_STREAMS if ps else (stream,):      # every table of the file, not just the one asked for
                have = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
                for name, kind in self.ADDED.get(table, ()):
                    if name not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
            self._open[key] = c
        if stream == "ps_gap_spans":
            self._ensure_span_table(self._open[key])
        return self._open[key]

    def _ensure_span_table(self, c):
        """Only a span WRITE asks for conn("ps_gap_spans"): the table is created here, never at open (a real tape carries no span table until a stop is written, and
        capture_proof reads one only if it exists)."""
        c.execute(self.DDL["ps_gap_spans"])
        self.upgrade_span_table(c)

    @staticmethod
    def upgrade_span_table(c):
        """The older (start_at, end_at) shape gets what restore and the writes need: cause and ended_by columns, a unique stop index, and cause 'legacy' on rows
        that had none (an OPEN legacy span is a stop of unknown cause: restored as stopped, never failed open). No row is dropped or has its times changed."""
        have = {r[1] for r in c.execute("PRAGMA table_info(ps_gap_spans)")}
        for col in ("cause", "ended_by"):
            if col not in have:
                c.execute("ALTER TABLE ps_gap_spans ADD COLUMN %s TEXT" % col)
        c.execute("UPDATE ps_gap_spans SET cause='legacy' WHERE cause IS NULL")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ps_gap_spans_stop ON ps_gap_spans(start_at, cause)")
        c.commit()

    def validate_ps_layout(self):
        if any(next((self.base / stream).glob("*.db"), None) for stream in self.PS_STREAMS):
            raise ValueError("legacy PumpSwap daily files require explicit migration; never ignored")

    def commit(self, *, ps_error=None):
        for key, c in self._open.items():
            try:
                c.commit()
            except (sqlite3.Error, OSError) as exc:
                if key[0] != "pumpswap" or ps_error is None:
                    raise
                ps_error(exc)


def desk_bytes(base, extra=(), exclude=()):
    def size(path):
        try:
            return path.stat().st_size
        except FileNotFoundError:  # SQLite can unlink sidecars after is_file().
            return 0

    total = 0
    excluded = tuple(map(Path, exclude))
    for p in [Path(base), *map(Path, extra)]:
        if p.is_file():
            total += size(p)
        elif p.is_dir():
            total += sum(size(f) for f in p.rglob("*")
                         if f.is_file() and not any(f.is_relative_to(e) for e in excluded))
    return total


PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_QUOTE = "So11111111111111111111111111111111111111112"
SOL_CURVE_FULL_LAMPORTS = 84_000_000_000     # a SOL curve graduates at ~85.005 SOL real quote; Gamma's population (1) is >= 84


def frame_filters(raw):
    """Read top-level repeated field 1 without decoding transaction bodies; protobuf order is arbitrary."""
    out, i = set(), 0
    def varint():
        nonlocal i
        n = 0
        for shift in range(0, 70, 7):
            b = raw[i]
            i += 1
            n |= (b & 127) << shift
            if b < 128:
                return n
        raise ValueError("varint")
    try:
        while i < len(raw):
            tag = varint()
            if tag < 8:
                return set()
            wire = tag & 7
            if wire == 2:
                n = varint()
                if i + n > len(raw):
                    return set()
                if tag == 10:
                    out.add(raw[i:i + n])
                i += n
            elif wire == 0:
                varint()
            elif wire in (1, 5):
                i += 8 if wire == 1 else 4
                if i > len(raw):
                    return set()
            else:
                return set()
    except (IndexError, ValueError):
        return set()
    return out


FIRE_FX_MAX_AGE_S = 120.0       # the desk writer's hard SOL/USD age cap for a fire (record_fire): never looser here


def causal_fx(sol_usd, sol_at, now, max_age_s):
    """The SOL/USD a px_limit fire may be decided on: finite and positive, stamped no later than now, and no older than
    min(max_age_s, FIRE_FX_MAX_AGE_S); else None (no fire, the watch is kept). eCat on 7d8dd4998."""
    import math
    if not (type(sol_usd) in (int, float) and math.isfinite(sol_usd) and sol_usd > 0
            and type(sol_at) in (int, float) and math.isfinite(sol_at) and math.isfinite(now)):
        return None
    age = now - sol_at
    return sol_usd if 0 <= age <= min(max_age_s, FIRE_FX_MAX_AGE_S) else None


@contextmanager
def wallet_flow_write(connection):
    # Never commit or roll back the existing program capture transaction.
    if not connection.in_transaction:connection.execute('BEGIN')
    rollback_generation=getattr(connection,'wallet_flow_rollback_notifications',0)
    connection.execute('SAVEPOINT wallet_flow_write')
    try:
        yield
        connection.execute('RELEASE wallet_flow_write')
    except BaseException:
        for statement in ('ROLLBACK TO wallet_flow_write','RELEASE wallet_flow_write'):
            try:connection.execute(statement)
            except sqlite3.Error:pass
        if not connection.in_transaction and getattr(connection,'wallet_flow_rollback_notifications',0)==rollback_generation:
            notify=getattr(connection,'_flow_outcome',None)
            if notify is not None:notify('rollback')
        raise


class Worker:
    def __init__(self, cfg, *, base, sol_price_fn, alert_fn, pool_metadata_fn=None):
        self.cfg, self.base = cfg, Path(base)
        self.wallet_flow_enabled=cfg.get('wallet_flow','0')=='1'
        self.release_sha = (ROOT.name if len(ROOT.name) == 40 and
                            all(ch in '0123456789abcdef' for ch in ROOT.name) else None)
        self.book = U.CurveBook(band=(float(cfg["band_lo"]), float(cfg["band_hi"])),
                                cooldown_s=float(cfg["cooldown_s"]), sol_max_age_s=float(cfg["sol_max_age_s"]))
        self.bars = U.BarBuilder(max_pools=int(cfg["watch_max"]))
        from sniper.settings import memecoin as mc
        # watch v22 executor: open price triggers of waiting desk tickets (read-only desk DB, never the ledger)
        self.standing = U.StandingBook() if mc.MEMECOIN_DESK_STANDING_ORDERS else None
        self.watch = U.WatchBook() if mc.MEMECOIN_DESK_WATCH_LIST else None     # watch-list-v1 change triggers
        self.reentry = (U.WinnerReentry(window_s=mc.MEMECOIN_DESK_WINNER_REENTRY_WINDOW_S, watch=mc.MEMECOIN_DESK_REENTRY_WATCH)
                        if mc.MEMECOIN_DESK_WINNER_REENTRY else None)
        try:
            self.ps_keep_window_s = float(cfg.get("ps_keep_window_s", "660"))
            if not math.isfinite(self.ps_keep_window_s) or self.ps_keep_window_s < 660:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("PumpSwap keep window must be finite and at least 660 seconds") from None
        self.ps_keep_widen_end = None
        self.files = DayFiles(self.base,wallet_flow_outcome=self.wallet_flow_transaction) if self.wallet_flow_enabled else DayFiles(self.base)  # legacy backfill keeps 60+600; only new anchors use the knob
        self.last_wal_alert = 0.0
        self.sol_price_fn, self.alert_fn = sol_price_fn, alert_fn
        self.sol_usd, self.sol_at = None, None
        self.sol_prev = (None, None)       # the sample before the current one (Am36: FX at or before an earlier cut)
        self.block_time = {}                       # slot -> block_time
        self.meta_hi_slot = self.meta_hi_bt = None   # highest slot whose block time is known (a proven lower bound)
        self.recent = collections.defaultdict(collections.deque)   # mint -> (t, is_buy, usd, actor, price)
        self.launch_seen = {}                      # mint -> create slot (its block time is joined later)
        self.creator_launches = collections.Counter()
        self.creator_of = {}
        self.watch_order = collections.deque()
        self.last_gap_at, self.started_at = None, time.time()
        self.stats = collections.Counter()
        self.blocked_samples = {}
        self.wallet_flow_ring,self.wallet_flow_ring_bytes=collections.deque(),0
        self.wallet_flow_desired=()
        self.wallet_flow_local_watch=None
        self.wallet_flow_local_keys=frozenset()
        self.wallet_flow_sent=()
        self.wallet_flow_submitted_at=None
        self.wallet_flow_submitted_digest=None
        self.wallet_flow_ready=False
        self.wallet_flow_gaps={}
        self.wallet_flow_staged={}
        self.wallet_flow_last_commit=None
        self.wallet_archive_future=None
        self.wallet_archive_batch=None
        self.wallet_archive_staged=None
        self.wallet_archive_status=dict(state='unknown',reason='not_checked',queued=None,queued_lower_bound=0,staged=0,last_committed_at=None)
        self.wallet_flow_us=collections.deque(maxlen=2000)
        self.ring, self.ring_bytes = collections.deque(), 0
        self.ps_ring, self.ps_ring_bytes = collections.deque(), 0
        self.ps_bindings = collections.OrderedDict()
        self.ring_cap = int(float(cfg["ring_mb"]) * 1024 * 1024)
        self.stop = False
        self.slot_tx = {}                          # slot -> {tx_index: signature}, newest slots only
        self.txshape = None
        self.txshape_us = collections.deque(maxlen=2000)           # per sampled tx cost (hash gate excluded), microseconds
        try:
            pct = int(float(cfg.get("txshape_pct", "0")))
        except (TypeError, ValueError):
            pct = 0
        if 0 < pct <= 100:
            self.txshape = TXS.Writer(self.base, pct)
        # PumpSwap count-only census (Alpha 2026-10-22): a second program filter on this same subscription,
        # frames counted at the reader and dropped before the ring, so the curve tape never sees them
        # Curve tape retirement (operator 2026-10-27, brain 48d0986c): MEMECOIN_DESK_CURVE_CAPTURE=0 stops recording
        # curve swaps/launches and every curve-driven action (band emissions, standing fires, watch triggers,
        # re-entry); graduation events still decode so PumpSwap binding continues. The heartbeat says
        # curve_capture=false and curve_committed_through stays frozen at the last committed curve cut: readers
        # report 'retired', never 0. Read at start (a restart applies a change). Default OFF (the operator's order);
        # MEMECOIN_DESK_CURVE_CAPTURE=1 restores recording from that moment, the retired interval stays a hole.
        self.curve_capture = os.getenv("MEMECOIN_DESK_CURVE_CAPTURE", "0").strip() == "1"
        self.curve_retired_since = None if self.curve_capture else time.time()
        self.census = os.getenv("MEMECOIN_DESK_PUMPSWAP_CENSUS", "0").strip() == "1"
        self.ps_capture = os.getenv("MEMECOIN_DESK_PUMPSWAP_CAPTURE", "0").strip() == "1"
        if self.ps_capture:
            self.files.validate_ps_layout()
            try:
                cap = float(cfg["ps_hard_gb"])
                if not math.isfinite(cap) or cap <= 0:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise ValueError("pumpswap disk cap must be finite and positive") from None
            try:
                hyst = float(cfg["ps_resume_hyst_gb"])
                if not math.isfinite(hyst) or not 0 <= hyst < cap:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise ValueError("pumpswap resume hysteresis must be finite with 0 <= hysteresis < the disk cap (%.3f GB)" % cap) from None
            try:
                self.ps_retention_s = float(cfg.get("ps_retention_h", "2")) * 3600
                if not math.isfinite(self.ps_retention_s) or self.ps_retention_s <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("pumpswap retention hours must be finite and positive") from None
        self.ps_pending_gaps, self.ps_gap_flushed_at = {}, None
        self.ps_loss_pending, self.ps_loss_counts = {}, collections.Counter()
        self.ps_witness_attempted = set()
        self.ps_routed_error_causes = set()
        self.ps_state_witness_enabled = (self.ps_capture and
            os.getenv('MEMECOIN_DESK_PS_STATE_WITNESSES', '1').strip() == '1')
        self.ps_state_witness = None
        self.ps_state_witness_init_attempted = False
        self.ps_state_witness_errors = set()
        self.ps_sell_fee_witness_errors = set()
        self.ps_coverage_session = uuid.uuid4().hex
        self.ps_selected = set()
        self.ps_watch_ready, self.ps_watch_pruned = False, 0.0
        self.ps_listed_watch = frozenset()
        self.ps_setup_watch = {}                   # short capture-only leads until the existing watch collector catches up
        self.ps_watch = frozenset()                # wallets on the Office watch list; every swap of theirs is recorded, stored pool or not
        self.ps_selection_repair = False
        self.ps_archive_conn = self.ps_archive_day = self.ps_archive_lock = None
        self.ps_archive_stuck = set()             # windows that mismatched or failed this run: skipped, still counted
        self.ps_prune_from = float("-inf")         # received_at already examined by retention (kept or deleted)
        self.ps_committed_through = self.ps_capture_from = None
        self._ps_source = self._ps_start = None
        self._ps_source_pending = False
        self.curve_committed_through = self.curve_capture_from = None
        self._curve_source = self._curve_start = None
        self._curve_source_pending = False
        self.curve_pending_gaps = {}               # retained until the discontinuities commit; an unwritten gap never grants coverage
        self.ps_book = None
        self.make_ps_book = None                   # set when emissions are on: rebuilds the PoolBook on an auto-resume
        self.ps_stopped = None                     # {"at", "cause"} while PS capture is stopped at its disk cap
        self.ps_restore_unread = None              # boot time while the ps_gap_spans read failed: an unread stop is not "no stop", capture stays OFF until it is read
        self.ps_resumes = collections.deque()      # times of recent auto-resumes (flap guard)
        self.ps_spans_closed = False
        self.ps_span_pending = []                  # span writes that have not been VERIFIED yet: retried until they are
        self.ps_wal_clean = False                  # the last PASSIVE checkpoint flushed every frame (the WAL resets at the next write)
        self.pool_metadata_fn = pool_metadata_fn
        self.ps_metadata_reason = None
        if os.getenv("MEMECOIN_DESK_PUMPSWAP_EMISSIONS", "0").strip() == "1":
            if not self.ps_capture:
                raise ValueError("pumpswap emissions require capture")
            from sniper.services.memecoin_desk_pumpswap_candidates import PoolBook
            from sniper.settings.memecoin import MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S, MEMECOIN_DESK_PS_SOL_MAX_AGE_S
            try:
                band = tuple(float(os.environ[k]) for k in ("MEMECOIN_DESK_PUMPSWAP_BAND_LO_USD",
                                                            "MEMECOIN_DESK_PUMPSWAP_BAND_HI_USD"))
            except (KeyError, ValueError):
                raise ValueError("explicit pumpswap emission band required") from None
            self.make_ps_book = lambda: PoolBook(band=band, cooldown_s=float(cfg["cooldown_s"]),
                                                 sol_max_age_s=MEMECOIN_DESK_PS_SOL_MAX_AGE_S,
                                                 reserve_max_age_s=MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S)
            self.ps_book = self.make_ps_book()
        # Desk intake emitter (spec 2026-10-27-desk-intake-candidate-path-design.md; GO Alpha/Delta 2026-10-27): a coin an
        # intake feed listed in the last 120 s becomes a PumpSwap candidate on its next proven BUY print. Never stops or
        # slows the tape: every call is guarded in intake_hook / intake_reload. Kill knob MEMECOIN_DESK_INTAKE_EMIT=0.
        self.intake_book = None
        if self.ps_capture and os.getenv("MEMECOIN_DESK_INTAKE_EMIT", "1").strip() != "0":
            try:
                from sniper.services.memecoin_desk_intake_book import IntakeBook
                from sniper.settings.memecoin import MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S, MEMECOIN_DESK_PS_SOL_MAX_AGE_S
                feeds = U.cohort_intake_feeds(self.base.parent / "memecoin_desk.db",
                                              os.environ.get("MEMECOIN_DESK_U_COHORT_ID"))
                self.intake_book = IntakeBook(intake_dir=self.base / "intake", db_path=self.base / "intake_emitter.db",
                                              feeds=feeds,
                                              reserve_max_age_s=MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S,
                                              sol_max_age_s=MEMECOIN_DESK_PS_SOL_MAX_AGE_S)
                for oid, body in self.intake_book.take_pending():     # a crash between claim and append: same bytes
                    self.intake_append(oid, body, time.time())
            except Exception:
                self.stats["intake_start_errors"] += 1
                log.exception("desk intake book failed to start; tape continues without intake emissions")
                self.intake_book = None
        self.ps_hist, self.ps_sec, self.ps_sec_n = collections.Counter(), 0, 0
        # write filter (see PS_HOLDBACK_S): MEMECOIN_DESK_PUMPSWAP_FILTER=0 stores every swap as before
        self.ps_filter = os.getenv("MEMECOIN_DESK_PUMPSWAP_FILTER", "1").strip().lower() not in ("0", "false", "off", "no")
        self.ps_matter = None                      # the set M, loaded from the tape on first use
        self.ps_extended = {}                      # pool -> until (host receipt clock): outside M, stored while listed in keep_extend.json
        self.ps_ext_shed, self.ps_ext_warn = False, KE.Warn()
        self.ps_ext_rows = {}                      # pool -> start_at of the selection row THIS feature opened (None: a feed row already covers it)
        self.ps_ext_path = Path(base) / "pumpswap" / KE.FILE_NAME
        self.ps_hold = collections.OrderedDict()   # pool -> [(received, row tuple)], not in M yet
        self.ps_hold_rows = 0
        self.ps_agg = {}                           # (pool, utc hour) -> [swaps, buys, first_slot, last_slot, first_recv, last_recv]
        if self.ps_capture:
            self.restore_ps_stop(time.time())

    def census_frame(self, raw):
        """True when the frame matched only the census filter: counted here and not queued."""
        names = frame_filters(raw)
        if b"pump" in names or b"pumpswap" not in names:
            if b"pump" in names:
                self.stats["cs_pump_frames"] += 1
                self.stats["cs_pump_bytes"] += len(raw)
                if b"pumpswap" in names:
                    self.stats["ps_dual_frames"] += 1
            return False
        sec = int(time.time())
        if sec != self.ps_sec:
            if self.ps_sec_n:
                self.ps_hist[str(min(self.ps_sec_n, 200))] += 1
            self.ps_sec, self.ps_sec_n = sec, 0
        self.ps_sec_n += 1
        self.stats["ps_tx"] += 1
        self.stats["ps_bytes"] += len(raw)
        return True

    # --- ring: reader thread pushes raw bytes, processor pops -------------------------------
    def push(self, raw, *, received_at=None, pumpswap=False):
        received_at = time.time() if received_at is None else received_at
        queue = self.ps_ring if pumpswap else self.ring
        size = "ps_ring_bytes" if pumpswap else "ring_bytes"
        queue.append((raw, received_at))
        setattr(self, size, getattr(self, size) + len(raw))
        while getattr(self, size) > self.ring_cap and queue:
            dropped, _ = queue.popleft()
            setattr(self, size, getattr(self, size) - len(dropped))
            self.stats["ps_ring_dropped" if pumpswap else "ring_dropped"] += 1
            if pumpswap:
                self.ps_gap("ring_overflow", received_at)
            else:
                self.gap("ring_overflow")

    def wallet_flow_selectors(self):
        if not self.wallet_flow_enabled:return ()
        try:
            path=Path(self.ps_ext_path).with_name('watch_wallets.json');fingerprint=None
            if not path.exists():wallets=[]
            else:
                st=path.stat();fingerprint=(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size)
                if fingerprint==getattr(self,'wallet_flow_selector_fingerprint',None):return self.wallet_flow_desired
                with path.open('rb') as src:wallets=json.load(src)['wallets']
            selected=wallet_flow_addresses(wallets)
            if selected is None:raise ValueError('watch_addresses')
            self.wallet_flow_selector_fingerprint=fingerprint if path.exists() else None
        except Exception:
            self.stats['wallet_flow_selector_errors']+=1
            self.wallet_flow_gap('selector_invalid',time.time());selected=()
        if selected!=self.wallet_flow_desired:
            self.wallet_flow_desired=selected
            self.stats['wallet_flow_selector_updates']+=1
            self.wallet_flow_gap('selectors_changed',time.time())
        self.stats['wallet_flow_selector_size']=len(selected)
        return selected

    def wallet_flow_gap(self,reason,at):
        row=self.wallet_flow_gaps.setdefault(reason,[0,at,at])
        row[0]+=1;row[2]=at
        self.stats['wallet_flow_gaps']+=1

    def push_wallet_flow(self,raw,*,received_at):
        if not self.wallet_flow_enabled:return
        if len(raw)>WALLET_FLOW_FRAME_MAX:
            self.stats['wallet_flow_dropped']+=1;self.wallet_flow_gap('frame_oversize',received_at);return
        self.wallet_flow_ring.append((raw,received_at));self.wallet_flow_ring_bytes+=len(raw)
        while self.wallet_flow_ring_bytes>WALLET_FLOW_RING_CAP:
            old,_=self.wallet_flow_ring.popleft();self.wallet_flow_ring_bytes-=len(old)
            self.stats['wallet_flow_dropped']+=1;self.wallet_flow_gap('ring_overflow',received_at)

    def wallet_flow_local_copy(self,raw,keys,received_at):
        local=self.ps_watch|frozenset(self.wallet_flow_desired)
        if self.wallet_flow_local_watch!=local:
            self.wallet_flow_local_watch=local
            selected=wallet_flow_addresses(local)
            self.wallet_flow_local_keys=frozenset(U.b58decode(w) for w in selected or ())
            if selected is None:self.wallet_flow_gap('local_watch_invalid',received_at)
        if any(k in self.wallet_flow_local_keys for k in keys):
            self.push_wallet_flow(raw,received_at=received_at)
            self.stats['wallet_flow_local_frames']+=1

    def queue_frame(self,raw,*,received_at,has_ps):
        if self.wallet_flow_enabled:
            names=frame_filters(raw)
            if b'wallet_flow' in names:
                self.stats['wallet_flow_frames']+=1
                try:self.push_wallet_flow(raw,received_at=received_at)
                except Exception:
                    self.stats['wallet_flow_errors']+=1;self.wallet_flow_gap('queue_error',received_at)
                if not names.intersection((b'pump',b'pumpswap')):return
        only_ps=self.census_frame(raw) if has_ps else False
        if only_ps and not self.ps_capture:return
        self.push(raw,received_at=received_at,pumpswap=only_ps)

    def next_frame(self):
        if self.ring:
            raw,at=self.ring.popleft();self.ring_bytes-=len(raw);return raw,at,'curve'
        if self.ps_ring:
            raw,at=self.ps_ring.popleft();self.ps_ring_bytes-=len(raw);return raw,at,'pumpswap'
        if self.wallet_flow_ring:
            raw,at=self.wallet_flow_ring.popleft();self.wallet_flow_ring_bytes-=len(raw);return raw,at,'wallet_flow'
        return None

    def wallet_flow_conn(self,now):
        c=self.files.conn('swaps_ps',now)
        if not self.wallet_flow_ready:
            for ddl in WALLET_FLOW_DDL:c.execute(ddl)
            if 'payload_archived' not in {r[1] for r in c.execute('PRAGMA table_info(wallet_flow_transactions)')}:
                c.execute('ALTER TABLE wallet_flow_transactions ADD COLUMN payload_archived INTEGER NOT NULL DEFAULT 0')
            if c.execute('SELECT 1 FROM wallet_flow_transactions LIMIT 1').fetchone() is None:
                c.execute('CREATE INDEX IF NOT EXISTS wallet_flow_archive_pending ON wallet_flow_transactions(received_at,flow_id) WHERE payload_archived=0')
            # Existing nonempty tapes require an operator-provisioned index, never a startup history scan.
            meta=c.execute('SELECT payload_bytes,pending_metadata FROM wallet_flow_meta WHERE id=1').fetchone()
            if meta is None:
                if c.execute('SELECT 1 FROM wallet_flow_transactions LIMIT 1').fetchone():raise ValueError('flow_budget_unknown')
                c.execute('INSERT INTO wallet_flow_meta VALUES(1,0,0)');meta=(0,0)
            self.stats['wallet_flow_payload_bytes'],self.stats['wallet_flow_metadata_pending']=meta
            self.wallet_flow_ready=True
        return c

    def wallet_flow_process(self,raw,*,received_at):
        if not self.wallet_flow_enabled:return
        started=time.perf_counter();now=time.time()
        try:
            if len(raw)>WALLET_FLOW_FRAME_MAX:raise ValueError('frame_oversize')
            if self.ps_stopped is not None or self.ps_restore_unread is not None or self.stop:raise ValueError('storage_stopped')
            from sniper.feeds.yellowstone import geyser_pb2 as pb
            u=pb.SubscribeUpdate.FromString(raw)
            if u.WhichOneof('update_oneof')!='transaction':raise ValueError('not_transaction')
            info=u.transaction.transaction;slot=u.transaction.slot
            if not 0<slot<2**63 or len(info.signature)!=64 or not any(info.signature):raise ValueError('identity_invalid')
            if not math.isfinite(received_at) or not 0<received_at<=now:raise ValueError('receipt_clock')
            sig=U.b58encode(info.signature);index=info.index if 0<info.index<2**63 else None
            bt=self.block_time.get(slot);bt=bt if type(bt) is int and bt>0 else None
            from sniper.office.wallet_stream_proof import encode_payload
            payload=encode_payload(raw)
            size=len(payload.encode())
            if size>WALLET_FLOW_RECORD_MAX:raise ValueError('record_oversize')
            c=self.wallet_flow_conn(now)
            with wallet_flow_write(c):
                if c.execute('SELECT 1 FROM wallet_flow_transactions WHERE signature=?',(sig,)).fetchone():
                    self.stats['wallet_flow_duplicates']+=1;return
                used=c.execute('SELECT payload_bytes FROM wallet_flow_meta WHERE id=1').fetchone()[0]
                n=c.execute('INSERT OR IGNORE INTO wallet_flow_transactions(signature,slot,tx_index,block_time,received_at,processed_at,commitment,payload_json) VALUES(?,?,?,?,?,?,?,?)',(sig,slot,index,bt,received_at,now,'confirmed',payload)).rowcount
                if n:
                    c.execute('UPDATE wallet_flow_meta SET payload_bytes=payload_bytes+?,pending_metadata=pending_metadata+? WHERE id=1',(size,int(bt is None)))
                    self.stats['wallet_flow_payload_bytes']=used+size
                    self.stats['wallet_flow_metadata_pending']+=int(bt is None)
                    self.stats['wallet_flow_transactions']+=1
                    self.stats['wallet_flow_failed']+=int(info.meta.HasField('err'))
        except Exception as exc:
            self.wallet_flow_error('process',exc,time.time())
            self.wallet_flow_reconcile()
            self.stats['wallet_flow_errors']+=1
            self.stats['wallet_flow_dropped']+=1
            self.wallet_flow_gap('process_error',received_at)
        finally:self.wallet_flow_us.append((time.perf_counter()-started)*1e6)

    def wallet_flow_metadata(self,slot,bt,now):
        if not self.wallet_flow_enabled or not self.wallet_flow_ready:return
        if type(bt) is not int or bt<=0:return
        try:
            c=self.wallet_flow_conn(now)
            with wallet_flow_write(c):
                n=c.execute('UPDATE wallet_flow_transactions SET block_time=?,processed_at=? WHERE slot=? AND block_time IS NULL',(bt,now,slot)).rowcount
                if n:
                    c.execute('UPDATE wallet_flow_meta SET pending_metadata=MAX(0,pending_metadata-?) WHERE id=1',(n,))
                    self.stats['wallet_flow_metadata_pending']=max(0,self.stats['wallet_flow_metadata_pending']-n)
                    self.stats['wallet_flow_metadata_updates']+=n
        except Exception as exc:
            self.wallet_flow_error('metadata',exc,time.time())
            self.wallet_flow_reconcile()
            self.stats['wallet_flow_errors']+=1;self.wallet_flow_gap('metadata_error',now)

    def wallet_flow_error(self,stage,exc,now):
        trace=exc.__traceback__;site=None
        wrappers=(WalletFlowConnection.execute.__code__,WalletFlowConnection.executemany.__code__)
        while trace is not None:
            if trace.tb_frame.f_code not in wrappers:site=trace.tb_frame.f_code.co_name+':'+str(trace.tb_lineno)
            trace=trace.tb_next
        self.wallet_flow_last_error=dict(stage=stage,at=now,error_type=type(exc).__name__,
            sqlite_errorcode=getattr(exc,'sqlite_errorcode',None),sqlite_errorname=getattr(exc,'sqlite_errorname',None),site=site)

    def wallet_flow_reconcile(self):
        if not self.wallet_flow_ready:return
        c=self.files._open.get(('pumpswap','all'))
        try:
            row=c.execute('SELECT payload_bytes,pending_metadata FROM wallet_flow_meta WHERE id=1').fetchone()
            if row is None:raise ValueError('flow_budget_unknown')
            self.stats['wallet_flow_payload_bytes'],self.stats['wallet_flow_metadata_pending']=row
        except (sqlite3.Error,OSError,ValueError,AttributeError):
            self.wallet_flow_ready=False
            self.stats['wallet_flow_payload_bytes']=self.stats['wallet_flow_metadata_pending']=None

    def wallet_flow_archive_poll(self):
        """At most one finite local-I/O job; no producer SQLite connection crosses threads."""
        if not self.wallet_flow_enabled or not self.wallet_flow_ready:return
        if self.wallet_archive_future is not None and self.wallet_archive_future.done():
            try:result=self.wallet_archive_future.result()
            except Exception as exc:result=dict(state='unknown',reason='archive_prepare_failed',error_type=type(exc).__name__,items=[])
            self.wallet_archive_future=None
            self.wallet_archive_status.update(state=result['state'],reason=result['reason'])
            if result['state']!='known':self.wallet_archive_status.update(queued=None,queued_lower_bound=0)
            if result['state']=='known':
                if result['items']:
                    self.wallet_archive_batch=result
                    self.wallet_archive_status.update(queued=None,queued_lower_bound=len(result['items']))
                else:self.wallet_archive_status.update(queued=0,queued_lower_bound=0)
        if self.wallet_archive_future is None and self.wallet_archive_batch is None and self.wallet_archive_staged is None:
            from sniper.office.wallet_flow_archive import next_batch
            path=self.base/'pumpswap/tape.db'
            self.wallet_archive_future=asyncio.create_task(asyncio.to_thread(next_batch,path,path.parent/'wallet-flow-archive'))

    def wallet_flow_archive_apply(self,conn,now):
        if self.wallet_archive_batch is None or self.wallet_archive_staged is not None:return
        from sniper.office.wallet_flow_archive import apply_batch
        result=apply_batch(conn,self.base/'pumpswap/tape.db',self.wallet_archive_batch,
                           opened_identity=getattr(conn,'wallet_flow_source_identity',None))
        self.wallet_archive_status.update(state=result['state'],reason=result['reason'],staged=result['staged'])
        if result['state']=='partial':self.wallet_archive_staged=result

    def wallet_flow_transaction(self,state):
        if not hasattr(self,'wallet_flow_staged'):return
        if self.wallet_archive_staged is not None:
            if state=='committed':
                self.wallet_archive_status.update(state='known',reason='archive_commit_observed',staged=0,queued=None,queued_lower_bound=0,last_committed_rows=self.wallet_archive_staged['staged'],last_committed_at=time.time())
                self.wallet_archive_batch=None;self.wallet_archive_staged=None
            elif state=='rollback':
                self.wallet_archive_status.update(state='unknown',reason='archive_outer_rollback',staged=0)
                self.wallet_archive_staged=None  # Keep immutable batch for an idempotent retry.
            elif state=='commit_failed':
                self.wallet_archive_status.update(state='unknown',reason='archive_commit_unknown')
        if state=='committed':
            remaining={reason:list(row) for reason,row in self.wallet_flow_gaps.items()}
            for reason,snapshot in self.wallet_flow_staged.items():
                pending=remaining.get(reason)
                if pending is not None:
                    pending[0]-=snapshot[0]
                    if pending[0]<=0:del remaining[reason]
            self.wallet_flow_gaps=remaining
            self.wallet_flow_staged.clear()
            self.wallet_flow_last_commit='known'
        elif state=='rollback':
            self.wallet_metadata_cursor=(-1,-1)
            self.wallet_flow_staged.clear()   # SQL is gone; all original/new losses stay pending in RAM.
            self.wallet_flow_last_commit='unknown'
            self.wallet_flow_reconcile()
            self.wallet_flow_gap('transaction_rollback',time.time())  # Unknown lost rows, never an exact loss count.
        elif state=='commit_failed':
            # An open transaction can be retried: its staged SQL must not be added twice.
            self.wallet_flow_last_commit='unknown'

    def wallet_flow_metadata_repair(self,c,now):
        """Retry twenty old NULL rows from exact retained slot receipts, never RPC."""
        cursor=getattr(self,'wallet_metadata_cursor',(-1,-1))
        rows=c.execute('SELECT flow_id,slot,received_at FROM wallet_flow_transactions INDEXED BY wallet_flow_pending_slot WHERE block_time IS NULL AND (slot,flow_id)>(?,?) ORDER BY slot,flow_id LIMIT 20',cursor).fetchall()
        if not rows:
            self.wallet_metadata_cursor=(-1,-1)
            return
        sources={}
        from sniper.office.wallet_metadata_jobs import read_times
        recovery=read_times(self.base.parent/'office/v2/metadata_jobs.db',self.base.parent/'office/v2/chain.db',self.base/'pumpswap/tape.db',slots=[r[1] for r in rows],now=now)
        self.wallet_metadata_recovery={k:v for k,v in recovery.items() if k!='items'}
        try:
            for flow_id,slot,received in rows:
                if received>now-60:continue
                # Transaction and metadata arrival can straddle either side of UTC midnight.
                for at in (received,received-86400,received+86400):
                    day=utc_day(at)
                    if day not in sources:
                        path=self.base/'blocks'/f'{day}.db'
                        try:sources[day]=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0)
                        except (sqlite3.Error,OSError):sources[day]=None
                    source=sources[day]
                    if source is None:continue
                    try:row=source.execute('SELECT block_time FROM blocks WHERE slot=?',(slot,)).fetchone()
                    except (sqlite3.Error,OSError):continue
                    if row and type(row[0]) is int and 0<row[0]<=received and row[0]<=now:
                        n=c.execute('UPDATE wallet_flow_transactions SET block_time=?,processed_at=? WHERE flow_id=? AND slot=? AND block_time IS NULL',(row[0],now,flow_id,slot)).rowcount
                        if n:c.execute('UPDATE wallet_flow_meta SET pending_metadata=MAX(0,pending_metadata-?) WHERE id=1',(n,))
                        break
                else:
                    bt=recovery['items'].get(slot)
                    if type(bt) is int and 0<bt<=received and bt<=now:
                        n=c.execute('UPDATE wallet_flow_transactions SET block_time=?,processed_at=? WHERE flow_id=? AND slot=? AND block_time IS NULL',(bt,now,flow_id,slot)).rowcount
                        if n:c.execute('UPDATE wallet_flow_meta SET pending_metadata=MAX(0,pending_metadata-?) WHERE id=1',(n,))
        finally:
            for source in sources.values():
                if source is not None:source.close()
        self.wallet_metadata_cursor=(rows[-1][1],rows[-1][0])

    def wallet_flow_maintenance(self,now):
        if not self.wallet_flow_enabled or self.ps_stopped is not None or self.ps_restore_unread is not None:return
        archive_was_staged=self.wallet_archive_staged
        try:
            c=self.wallet_flow_conn(now)
            snapshot={reason:list(row) for reason,row in self.wallet_flow_gaps.items()}
            with wallet_flow_write(c):
                for reason,(n,first,last) in snapshot.items():
                    delta=n-self.wallet_flow_staged.get(reason,[0])[0]
                    if delta:
                        c.execute('INSERT INTO wallet_flow_gaps VALUES(?,?,?,?,?,?) ON CONFLICT(day,reason) DO UPDATE SET n=n+excluded.n,last_at=excluded.last_at,known_at=excluded.known_at',(utc_day(first),reason,delta,first,last,now))
                self.wallet_flow_archive_apply(c,now)
                self.wallet_flow_metadata_repair(c,now)
                meta=c.execute('SELECT payload_bytes,pending_metadata FROM wallet_flow_meta WHERE id=1').fetchone()
                self.stats['wallet_flow_payload_bytes'],self.stats['wallet_flow_metadata_pending']=meta
            self.wallet_flow_staged.update(snapshot)  # RELEASE is not durable; only the connection commit hook acks.
        except Exception as exc:
            if archive_was_staged is None and self.wallet_archive_staged is not None:
                self.wallet_archive_staged=None
                self.wallet_archive_status.update(state='unknown',reason='archive_maintenance_rollback',staged=0)
            self.wallet_metadata_cursor=(-1,-1)
            self.wallet_flow_error('maintenance',exc,time.time())
            self.wallet_flow_reconcile()
            self.stats['wallet_flow_errors']+=1;self.wallet_flow_gap('maintenance_error',now)

    def _blind_curve(self):
        self.curve_committed_through = self.curve_capture_from = None
        self._curve_source = self._curve_start = None
        self._curve_source_pending = False

    def _write_curve_gaps(self):
        try:
            for cause, (at, slot) in self.curve_pending_gaps.items():
                self.files.conn("gaps", at).execute(
                    "INSERT INTO gaps SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM gaps WHERE at=? AND cause=?)",
                    (at, cause, slot, at, cause))
        except (sqlite3.Error, OSError):
            self.stats["curve_gap_write_errors"] += 1
            log.warning("desk curve gap write pending; coverage stays unknown")
            return False
        return True

    def gap(self, cause):
        self._blind_curve()
        self.book.mark_unknown(cause)
        self.last_gap_at = time.time()
        self.stats["gaps"] += 1
        self.curve_pending_gaps.setdefault(cause, (self.last_gap_at, max(self.block_time, default=None)))
        self._write_curve_gaps()
        if self.ps_capture:
            self.ps_gap(cause, time.time())
        log.warning("desk universe gap cause=%s", cause)

    def ps_gap(self, cause, at):
        if cause not in ("decode_loss", "virtual_quote_unproven"):
            self._blind_ps()
        if cause in PSL.DECODE_CAUSES:
            self.ps_loss_counts[cause] += 1
        self.stats["ps_gaps"] += 1
        if self.ps_book is not None:
            self.ps_book.mark_unknown(cause)
        self.ps_pending_gaps[cause] = min(at, self.ps_pending_gaps.get(cause, at))

    def _blind_ps(self):
        self.ps_committed_through = self.ps_capture_from = None
        self._ps_source = self._ps_start = None
        self._ps_source_pending = False

    def ps_storage_error(self, exc):
        if self.wallet_flow_enabled:self.wallet_flow_error('shared_ps_storage',exc,time.time())
        self.stats["ps_write_errors"] += 1
        self.ps_selected.clear()
        self.ps_selection_repair = True
        self.ps_loss_pending.clear()  # rolled-back receipts cannot attest any pending legacy gap
        self.ps_bindings.clear()              # a rolled-back map must not survive in memory
        conn = self.files._open.get(("pumpswap", "all"))
        if conn is not None:
            try:
                conn.rollback()
            except (sqlite3.Error, OSError):
                pass
        self.ps_gap("write_error", time.time())
        log.warning("desk PumpSwap storage failed: %s", type(exc).__name__)

    def flush_ps_gaps(self, now):
        """Reader overflow does no SQLite I/O; processor records at most one overflow gap per second."""
        if self.ps_pending_gaps and (self.ps_gap_flushed_at is None or now - self.ps_gap_flushed_at >= 1):
            self.ps_gap_flushed_at = now
            try:
                conn = self.files.conn("gaps_ps", now)
                for cause, at in self.ps_pending_gaps.items():
                    gid = conn.execute("INSERT INTO gaps_ps VALUES(?,?,?)",
                                       (at, cause, max(self.block_time, default=None))).lastrowid
                    members = self.ps_loss_pending.get(cause, [])
                    if members and len(members) == self.ps_loss_counts[cause]:
                        conn.execute("INSERT INTO ps_loss_batches VALUES(?,?,?,?,?,?)",
                                     (gid, at, cause, len(set(members)), PSL.PROTOCOL, max(at, now)))
                        conn.executemany("INSERT INTO ps_loss_batch_members VALUES(?,?,?)",
                                         [(gid, sig, cause) for sig in set(members)])
                conn.commit()
            except (sqlite3.Error, OSError) as exc:
                self.ps_storage_error(exc)
            else:
                self.stats["ps_gap_rows"] += len(self.ps_pending_gaps)
                self.ps_pending_gaps.clear()
                self.ps_loss_pending.clear()
                self.ps_loss_counts.clear()

    def commit_files(self):
        before = time.time()
        errors = self.stats["ps_write_errors"]
        if not self.ps_capture:
            self._blind_ps()
        curve_gaps_written = self._write_curve_gaps()
        self.flush_ps_gaps(before)
        if self.ps_capture:
            try:
                self.expire_hold(before)
                self.flush_agg(before)
            except (sqlite3.Error, OSError) as exc:
                self.ps_storage_error(exc)
        try:
            self.files.commit(ps_error=self.ps_storage_error)
        except (sqlite3.Error, OSError):
            self.gap("write_error")
            raise
        if curve_gaps_written:
            self.curve_pending_gaps.clear()
        offset = math.nan
        if self._curve_source_pending or self._ps_source_pending:
            try:
                offset = float(clock.offset_s())
            except (TypeError, ValueError, OSError):
                pass
        if self._curve_source_pending and not self.ring and not self.curve_pending_gaps:
            if math.isfinite(offset):
                received, block_time = self._curve_source
                self.curve_committed_through = min(received, block_time + offset)
                self.curve_capture_from = self._curve_start
                self._curve_source_pending = False
            else:
                self._blind_curve()
        # Both queues share the event loop; no receive callback runs between this cut and commit.
        if self.stats["ps_write_errors"] != errors:
            self._blind_ps()
        if (self.ps_capture and not self.ring and not self.ps_ring and not self.ps_pending_gaps
                and self.stats["ps_write_errors"] == errors):
            if self._ps_source_pending:
                if math.isfinite(offset):
                    received, block_time = self._ps_source
                    self.ps_committed_through = min(received, block_time + offset)
                    self.ps_capture_from = self._ps_start
                    try:
                        # Rows committed above precede this persisted frontier; failed proof writes grant no coverage.
                        if self.ps_capture_from <= self.ps_committed_through:
                            c = self.files.conn("ps_capture_intervals", before)
                            c.execute("INSERT INTO ps_capture_intervals VALUES(?,?,?,?) "
                                      "ON CONFLICT(session,start_at) DO UPDATE SET through_at=excluded.through_at",
                                      (self.ps_coverage_session, self.ps_capture_from,
                                       self.ps_committed_through, PSL.PROTOCOL))
                            c.commit()
                    except (sqlite3.Error, OSError) as exc:
                        self.ps_storage_error(exc)
                    self._ps_source_pending = False
                else:
                    self._blind_ps()
            self.prune_ps(before)
            self.archive_keep_ps(before)

    def prune_ps(self, now):
        """Delete one batch of swaps_ps rows older than the retention window, on the capture connection
        (no second writer, so no lock against the timeout=0 capture). A failure is counted and retried next
        pass; it never becomes a tape gap, because the rows it fails to delete are still correct rows."""
        conn = self.files._open.get(("pumpswap", "all"))
        if conn is None:
            return
        started = time.monotonic()
        try:
            keep = ("NOT EXISTS (SELECT 1 FROM ps_keep k WHERE k.pool = s.pool AND s.received_at BETWEEN k.keep_from AND k.keep_until)")
            # Kept rows sit at the old end of received_at and never leave; without a cursor every batch re-walked them
            # (ps_prune_ms_max 158614 on 10-26). Keep windows only widen around new graduations, so a row once kept stays kept.
            frm, cut = self.ps_prune_from, now - self.ps_retention_s
            hi = conn.execute("SELECT MAX(received_at) FROM (SELECT received_at FROM swaps_ps WHERE received_at > ? AND received_at < ? "
                              "ORDER BY received_at LIMIT ?)", (frm, cut, PS_PRUNE_BATCH)).fetchone()[0]
            if hi is None:
                conn.commit()
                return
            if self.files.rowid_layout:
                n = conn.execute("DELETE FROM swaps_ps WHERE rowid IN (SELECT s.rowid FROM swaps_ps s WHERE s.received_at > ? AND s.received_at <= ? AND " + keep
                                 + ")", (frm, hi)).rowcount
            else:                                   # an old WITHOUT ROWID file: no rowid, the primary key is the handle
                n = conn.execute("DELETE FROM swaps_ps WHERE (signature, ordinal) IN (SELECT signature, ordinal FROM swaps_ps s "
                                 "WHERE received_at > ? AND received_at <= ? AND " + keep + ")",
                                 (frm, hi)).rowcount
            conn.commit()
        except (sqlite3.Error, OSError):
            self.stats["ps_prune_errors"] += 1
            log.exception("desk PumpSwap retention delete failed")
            try:
                conn.rollback()
            except (sqlite3.Error, OSError):
                pass
            return
        self.ps_prune_from = hi
        self.stats["ps_pruned"] += n
        self.stats["ps_prune_from"] = int(hi)            # heartbeat: how far retention has walked (epoch s)
        self.stats["ps_prune_ms_max"] = max(self.stats["ps_prune_ms_max"], int((time.monotonic() - started) * 1000))

    def _ps_archive(self, day):
        """The archive file for one keep_until day. Moving past a day closes its file read-only (expired windows are taken
        oldest keep_until first, so a passed day gets no more rows)."""
        if self.ps_archive_day != day:
            if self.ps_archive_conn is not None:
                self.ps_archive_conn.close()
                os.chmod(self.ps_archive_path(self.ps_archive_day), 0o444)
                self.ps_archive_conn = None
            if self.ps_archive_lock is not None:
                os.close(self.ps_archive_lock)          # releases the day's seal lock
                self.ps_archive_lock = None
            path = self.ps_archive_path(day)
            path.parent.mkdir(parents=True, exist_ok=True)
            # One per-day seal lock with the compressor (scripts/ps_archive_maintenance.py): taken NON-blocking here (the
            # capture loop never waits; busy = this pass is skipped and the window stays in the tape), held while the day
            # is open, so the compressor cannot publish keep-<day>.db.zst between this check and the open below.
            fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise sqlite3.OperationalError("archive day locked (busy): %s" % path.name) from None
            if Path(str(path) + ".zst").exists():   # a sealed (compressed) day: never a second plain file beside it
                os.close(fd)
                raise OSError("archive_day_sealed: %s" % path.name)
            self.ps_archive_lock = fd
            a = sqlite3.connect(str(path), timeout=0)   # on the capture loop: a locked archive is a skipped pass, never a wait
            tape = self.files._open[("pumpswap", "all")]
            for name in ("swaps_ps", "ps_keep"):
                a.execute(tape.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0]
                          .replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1).replace("IF NOT EXISTS IF NOT EXISTS", "IF NOT EXISTS"))
            a.execute("CREATE UNIQUE INDEX IF NOT EXISTS swaps_ps_sig_ord ON swaps_ps(signature, ordinal)")
            a.commit()
            self.ps_archive_conn, self.ps_archive_day = a, day
        return self.ps_archive_conn

    def ps_archive_path(self, day):
        return self.base.parent / "archive" / "pumpswap" / f"keep-{day}.db"

    def archive_keep_ps(self, now):
        """Move one batch of an expired keep window (oldest keep_until first) to its daily archive. Copy, commit, read the
        copy back; only rows read back equal are deleted from tape.db. A mismatch is counted and the window is left alone."""
        conn = self.files._open.get(("pumpswap", "all"))
        if conn is None:
            return
        started, pool = time.monotonic(), None
        try:
            keep = next((k for k in conn.execute("SELECT * FROM ps_keep WHERE keep_until < ? ORDER BY keep_until LIMIT ?",
                                                 (now - PS_ARCHIVE_AFTER_S, len(self.ps_archive_stuck) + 1))
                         if k[0] not in self.ps_archive_stuck), None)
            if keep is None:
                return
            pool, keep_from, keep_until = keep[0], keep[1], keep[2]
            a = self._ps_archive(utc_day(keep_until))
            cur = conn.execute("SELECT * FROM swaps_ps WHERE pool = ? AND received_at BETWEEN ? AND ? ORDER BY signature, ordinal "
                               "LIMIT ?", (pool, keep_from, keep_until, PS_PRUNE_BATCH))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            if rows:
                sig, ordi = cols.index("signature"), cols.index("ordinal")
                names, marks = ",".join(cols), ",".join("?" * len(cols))
                a.executemany(f"INSERT OR IGNORE INTO swaps_ps({names}) VALUES({marks})", rows)
                a.commit()
                back = [a.execute(f"SELECT {names} FROM swaps_ps WHERE signature = ? AND ordinal = ?", (r[sig], r[ordi])).fetchone()
                        for r in rows]
                if back != rows:
                    self.stats["ps_archive_mismatch"] += 1
                    self.ps_archive_stuck.add(pool)
                    log.error("desk PumpSwap archive copy differs: pool=%s day=%s rows=%d", pool, self.ps_archive_day, len(rows))
                    return
                conn.executemany("DELETE FROM swaps_ps WHERE signature = ? AND ordinal = ?", [(r[sig], r[ordi]) for r in rows])
                conn.commit()
                self.stats["ps_archived"] += len(rows)
            else:                                   # window empty in tape.db: the keep row itself moves last
                a.execute("INSERT OR REPLACE INTO ps_keep VALUES(" + ",".join("?" * len(keep)) + ")", keep)
                a.commit()
                if a.execute("SELECT * FROM ps_keep WHERE pool = ?", (pool,)).fetchone() != keep:
                    self.stats["ps_archive_mismatch"] += 1
                    self.ps_archive_stuck.add(pool)
                    log.error("desk PumpSwap archive keep row differs: pool=%s", pool)
                    return
                conn.execute("DELETE FROM ps_keep WHERE pool = ?", (pool,))
                conn.execute("INSERT OR REPLACE INTO ps_keep_done VALUES(?, ?)", (pool, now))
                conn.commit()
                self.stats["ps_archived_windows"] += 1
        except (sqlite3.Error, OSError) as exc:
            busy = isinstance(exc, sqlite3.OperationalError) and ("locked" in str(exc) or "busy" in str(exc))
            if busy:
                self.stats["ps_archive_busy"] += 1      # a reader holds the archive: skip this pass, retry next one
            else:
                self.stats["ps_archive_errors"] += 1
                self.ps_archive_stuck.add(pool)         # a failing window never pins the queue for the windows behind it
                log.exception("desk PumpSwap keep archive failed")
            try:
                conn.rollback()
                if self.ps_archive_conn is not None:
                    self.ps_archive_conn.rollback()
            except (sqlite3.Error, OSError):
                pass
            return
        finally:
            self.stats["ps_archive_ms_max"] = max(self.stats["ps_archive_ms_max"], int((time.monotonic() - started) * 1000))

    # --- frame processing --------------------------------------------------------------------
    def process(self, raw, *, received_at=None):
        from sniper.feeds.yellowstone import geyser_pb2 as pb
        from sniper.research.memecoin_opportunity.pumpfun import decode_pumpfun_event
        u = pb.SubscribeUpdate.FromString(raw)
        kind = u.WhichOneof("update_oneof")
        now = time.time()
        received_at = now if received_at is None else received_at
        if kind == "block_meta":
            ps_errors = self.stats["ps_write_errors"]
            if u.block_meta.HasField("block_time"):
                slot, bt = u.block_meta.slot, u.block_meta.block_time.timestamp
                self.block_time[slot] = bt
                self.wallet_flow_metadata(slot,bt,now)
                if self.meta_hi_slot is None or slot > self.meta_hi_slot:
                    self.meta_hi_slot, self.meta_hi_bt = slot, bt
                self.bars.set_block_time(slot, bt)
                self.files.conn("blocks", now).execute("INSERT OR IGNORE INTO blocks VALUES(?,?,?)", (slot, bt, now))
                # the slot's rows already written today get their time now; a slot split by UTC
                # midnight keeps NULL in yesterday's file and is joined through blocks
                for stream in (("swaps", "launches") if self.curve_capture else ()):
                    self.files.conn(stream, now).execute(
                        f"UPDATE {stream} SET block_time=? WHERE slot=? AND block_time IS NULL", (bt, slot))
                if self.ps_capture:
                    try:
                        for stream in ("swaps_ps", "pools"):
                            self.files.conn(stream, now).execute(
                                f"UPDATE {stream} SET block_time=? WHERE slot=? AND block_time IS NULL", (bt, slot))
                    except (sqlite3.Error, OSError) as exc:
                        self.ps_storage_error(exc)
            if len(self.block_time) > 20000:
                for s in sorted(self.block_time)[:5000]:
                    del self.block_time[s]
            self.flush_bars()
            if u.block_meta.HasField("block_time") and self.curve_capture:   # retired: the curve cut never advances
                if self._curve_start is None:
                    self._curve_start = received_at
                self._curve_source = (received_at, bt)
                self._curve_source_pending = True
            if u.block_meta.HasField("block_time"):
                if self.ps_capture and self.stats["ps_write_errors"] == ps_errors:
                    if self._ps_start is None:
                        self._ps_start = received_at
                    self._ps_source = (received_at, bt)
                    self._ps_source_pending = True
            return
        if kind != "transaction":
            return
        self.stats["tx"] += 1
        info, slot = u.transaction.transaction, u.transaction.slot
        if self.ps_capture and info.meta.HasField("err"):
            self.stats["ps_failed_tx"] += 1
            return
        msg = info.transaction.message
        keys = list(msg.account_keys) + list(info.meta.loaded_writable_addresses) + list(info.meta.loaded_readonly_addresses)
        inner = [(g.index, [(ix.program_id_index, ix.data) for ix in g.instructions]) for g in info.meta.inner_instructions]
        sig = U.b58encode(info.signature)
        # Proto3 omits default zero, so even a genuine first tx cannot prove presence here.
        tx_index = int(info.index) if 0 < info.index < 2**63 else None
        if tx_index is None:
            self.stats["tx_index_unknown"] += 1
        else:
            seen = self.slot_tx.setdefault(slot, {})
            if len(self.slot_tx) > 4:
                del self.slot_tx[min(self.slot_tx)]
            if seen.setdefault(tx_index, sig) != sig:
                self.stats["tx_index_collision"] += 1
        bt = self.block_time.get(slot)
        if self.ps_capture and self.ps_watch and self.ps_stopped is None and self.ps_restore_unread is None:
            # Local research facts only; failures must not discard the transaction's swap events.
            try:
                from sniper.services.memecoin_desk_wallet_setup import decode_setups
                setups = decode_setups(keys, msg.header.num_required_signatures,
                                       msg.header.num_readonly_signed_accounts, msg.instructions, self.ps_watch)
                if setups:
                    c = self.files.conn("wallet_setup_events", now)
                    for setup in setups:
                        self.stats["wallet_setup_events"] += c.execute(
                            "INSERT OR IGNORE INTO wallet_setup_events(signature,outer_ix,program,payer,wallet,slot,tx_index,block_time,received_at,processed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (sig, setup['outer_ix'], setup['program'], setup['payer'], setup['wallet'],
                             slot, tx_index, bt, received_at, now)).rowcount
                        if setup['payer'] in self.ps_watch:
                            self.ps_setup_watch[setup['wallet']] = now + 120.0
                            self.ps_watch = self.ps_watch | {setup['wallet']}
            except Exception:
                self.stats["wallet_setup_errors"] += 1
        if self.wallet_flow_enabled and self.ps_watch and 'wallet_flow' not in u.filters:
            try:self.wallet_flow_local_copy(raw,keys,received_at)
            except Exception:
                self.stats['wallet_flow_errors']+=1;self.wallet_flow_gap('local_copy_error',received_at)
        if self.ps_capture and any(PUMPSWAP_PROGRAM in line for line in info.meta.log_messages):
            from sniper.services import memecoin_desk_pumpswap as PS
            try:
                events = PS.events(info.meta.log_messages)
                self.stats["ps_no_decoded_events"] += int(not events)
                for ordinal, event in events:
                    self.on_pumpswap(event, slot, sig, ordinal, tx_index, bt, received_at, now)
            except PS.DecodeLoss as exc:
                self.stats["ps_decode_loss"] += 1
                cause = "virtual_quote_unproven" if str(exc) == "pumpswap_virtual_quote_unproven" else "decode_loss"
                self.ps_gap(cause, received_at)
                try:                           # name the lost transaction: a gaps_ps row only keeps cause + last_slot
                    c = self.files.conn("ps_decode_losses", now)
                    new_loss = c.execute("INSERT OR IGNORE INTO ps_decode_losses VALUES(?,?,?,?)",
                                         (sig, cause, slot, received_at)).rowcount
                    # Keep every account index and every CPI; legacy curve normalization above deliberately omits them.
                    all_ix = itertools.chain(msg.instructions,
                                             (ix for group in info.meta.inner_instructions for ix in group.instructions))
                    instructions = [{'program_id_index': ix.program_id_index, 'accounts': list(ix.accounts),
                                     'data': bytes(ix.data)} for ix in itertools.islice(all_ix, 129)]
                    loaded_complete = (sum(len(a.writable_indexes) for a in msg.address_table_lookups)
                                       == len(info.meta.loaded_writable_addresses)
                                       and sum(len(a.readonly_indexes) for a in msg.address_table_lookups)
                                       == len(info.meta.loaded_readonly_addresses))
                    detail = PS.loss_details(info.meta.log_messages, reason=getattr(exc, 'detail_reason', str(exc)), account_keys=keys,
                                             instructions=instructions,
                                             inner_complete=(loaded_complete and not info.meta.log_messages_none
                                                             and not info.meta.inner_instructions_none))
                    c.execute("INSERT OR IGNORE INTO ps_loss_attribution VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                              (sig, cause, detail['reason'], detail['scope'], json.dumps(detail['pools']),
                               detail['invocations'], detail['discriminator'], detail['event_length'],
                               PS.PUMP_AMM_IDL_SHA256, received_at, slot, PSL.PROTOCOL))
                    try:                               # additive record of every sweep event in this tx (the loss rows above stay; nothing stops censoring); a failure here never loses the loss
                        for sw in PS.sweep_events(info.meta.log_messages):
                            self.stats["ps_sweep_events"] += c.execute("INSERT OR IGNORE INTO ps_sweep_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (sig, sw["ordinal"], sw["pool"], sw["base_mint"], sw["quote_mint"], sw["recipient"], sw["payer"], sw["amount"], sw["bucket"],
                                       sw["event_time"], slot, tx_index, received_at, PS.SWEEP_RECORDER_VERSION)).rowcount
                    except Exception:
                        self.stats["ps_sweep_record_failed"] += 1
                        log.exception("sweep event record failed; loss rows retained")
                    if (new_loss and os.getenv('MEMECOIN_DESK_PS_ROUTED_SCOPE', '1').strip() == '1'
                            and getattr(exc, 'detail_reason', '') ==
                            'pumpswap_event_malformed:unsupported_or_inconsistent_buy:unsupported_instruction'):
                        try:
                            from sniper.services import memecoin_desk_pumpswap_routed as routed
                            groups = {g.index: g for g in info.meta.inner_instructions}
                            routed_instructions = []
                            for outer, top in enumerate(msg.instructions):
                                routed_instructions.append(dict(program_id_index=top.program_id_index,
                                    accounts=list(top.accounts), data=bytes(top.data), outer_ix=outer, stack_height=1))
                                if outer in groups:
                                    for child in groups[outer].instructions:
                                        routed_instructions.append(dict(program_id_index=child.program_id_index,
                                            accounts=list(child.accounts), data=bytes(child.data), outer_ix=outer,
                                            stack_height=child.stack_height if child.HasField('stack_height') else None))
                            pools = routed.prove_pools(info.meta.log_messages, account_keys=keys,
                                instructions=routed_instructions, inner_complete=(loaded_complete
                                and not info.meta.log_messages_none and not info.meta.inner_instructions_none))
                            proof = PSL.pool_key_record(pools, signature=sig, cause=cause, received_at=received_at,
                                                        known_at=time.time(), slot=slot)
                            if not math.isfinite(proof['known_at']) or proof['known_at'] < received_at:
                                raise ValueError('scope clock')
                        except (PS.DecodeLoss, ValueError, TypeError):
                            self.stats['ps_routed_scope_refused'] += 1
                        except Exception as scope_exc:
                            self.stats['ps_routed_scope_refused'] += 1
                            cause_name = type(scope_exc).__name__
                            if cause_name not in self.ps_routed_error_causes:
                                self.ps_routed_error_causes.add(cause_name)
                                log.exception("routed scope decoder failed; original loss retained")
                        else:
                            c.execute('INSERT OR IGNORE INTO ps_loss_scope VALUES(?,?,?,?,?,?,?)',
                                (sig,cause,json.dumps(proof,sort_keys=True),1,proof['proof'],
                                 proof['decoder_version'],proof['known_at']))
                            self.stats['ps_routed_scope_complete'] += 1
                    envelope_keys, envelope_profile = ((detail['event_account_keys'], PSL.EVENT_ENVELOPE_PROFILE)
                                                       if 'event_account_keys' in detail else
                                                       (detail.get('swap_tx_account_keys'), PSL.SWAP_TX_ENVELOPE_PROFILE))
                    if new_loss and envelope_keys is not None:
                        try:
                            known = time.time()
                            if not (math.isfinite(known) and 0 <= received_at <= known):
                                raise ValueError('event proof clock')
                            account_bits = PSL.event_account_filter(envelope_keys)
                            proof = (sig, cause, PSL.EVENT_ENVELOPE_KIND, envelope_profile, account_bits,
                                     len(envelope_keys), PS.PUMP_AMM_IDL_SHA256,
                                     received_at, known, slot, PSL.PROTOCOL)
                            checksum = PSL.event_envelope_sha256(proof)
                        except (ValueError, TypeError, OverflowError):
                            self.stats['ps_event_envelope_refused'] += 1
                        else:
                            c.execute('INSERT OR IGNORE INTO ps_loss_event_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                      (*proof, checksum))
                    if new_loss:
                        try:
                            shadow_keys=PS.extend_account_shadow_keys(info.meta.log_messages,account_keys=keys,
                                instructions=instructions,inner_complete=(loaded_complete and not info.meta.log_messages_none
                                and not info.meta.inner_instructions_none),idl_sha256=PS.PUMP_AMM_IDL_SHA256)
                            if shadow_keys is not None:
                                shadow_status=ES.record(c,raw,signature=sig,cause=cause,received_at=received_at,
                                    known_at=time.time(),slot=slot,keys=shadow_keys,reason=detail['reason'],
                                    original_class='unknown_loss_receipts' if detail['scope']=='unknown' else 'own_pool_losses')
                                self.stats['ps_extend_shadow:'+shadow_status]+=1
                        except Exception:
                            self.stats['ps_extend_shadow:error']+=1
                            try:ES.note_error(c,time.time(),'record')
                            except Exception:self.stats['ps_extend_shadow:error_receipt_failed']+=1
                            log.exception("ExtendAccount shadow failed; original loss retained")
                    self.ps_loss_pending.setdefault(cause, []).append(sig)
                    self.sample_ps_decode_witness(raw, info, keys, sig, received_at, detail['reason'])
                    if new_loss:
                        self.sample_ps_state_witness(raw, slot, sig, tx_index, received_at, detail)
                except (sqlite3.Error, OSError) as write_exc:
                    self.ps_storage_error(write_exc)
            except (sqlite3.Error, OSError) as exc:
                self.ps_storage_error(exc)
            except Exception:
                self.stats["ps_process_errors"] += 1
                self.ps_gap("process_error", now)
                log.exception("desk PumpSwap frame failed; curve processing continues")
        events = [(ordinal, line, decode_pumpfun_event(U.PUMP_PROGRAM, line))
                  for ordinal, line in U.pump_event_lines(account_keys=keys, inner=inner, logs=info.meta.log_messages)]
        has_trade = any(e.blocker is None and getattr(e.event_kind, "value", None) in ("buy", "sell")
                        for _, _, e in events)
        for ordinal, line, e in events:
            if not self.curve_capture:
                self._retired_curve_event(e, slot, sig, ordinal, tx_index, bt, received_at, now, line=line)
                continue
            if e.blocker is not None:
                try:     # which event shape the decoder does not know (13% of events in the 11:37Z capture)
                    import base64 as _b64
                    raw_ev = _b64.b64decode(line.split("Program data: ", 1)[1])
                    # V1 transactions can emit a prefix-only wrapper alongside their real TradeEvents.
                    if not raw_ev and has_trade:
                        self.stats["known_irrelevant:empty_event_wrapper"] += 1
                        continue
                    disc = raw_ev[:8].hex()
                    kind, what = classify_unknown_event(raw_ev)
                    if kind == "irrelevant":
                        self.stats["known_irrelevant:" + what] += 1
                        continue
                    if kind == "post_complete_buy":
                        self.stats["post_complete_buy"] += 1
                        self.book.on_graduated(what)          # curve complete: no more curve trades
                        continue
                    self.stats["blocked_disc:" + disc] += 1
                    samples = self.blocked_samples.setdefault(disc, [])
                    if len(samples) < 3:          # length + signature let us name the event offline
                        samples.append({"len": len(raw_ev), "sig": sig, "slot": slot})
                except (ValueError, IndexError):
                    pass
                self.stats["decode_blocked:" + str(getattr(e.blocker, "value", e.blocker))] += 1
                self.gap("decode_blocked")
                continue
            k = getattr(e.event_kind, "value", str(e.event_kind))
            self.stats["ev:" + k] += 1
            if k == "launch":
                self.book.on_launch(e.token_id, (e.facts or {}).get("token_total_supply"))
                self.launch_seen[e.token_id] = slot
                self.creator_of[e.token_id] = e.creator_id
                self.creator_launches[e.creator_id] += 1
                facts = e.facts or {}
                self.files.conn("launches", now).execute(
                    "INSERT OR IGNORE INTO launches(mint, curve, creator, slot, signature, block_time, received_at, "
                    "symbol, name, dev, dev_source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (e.token_id, e.curve_id, e.creator_id, slot, sig, bt, now, facts.get("symbol"), facts.get("name"),
                     e.actor_id, "create_event_user" if e.actor_id else None))
            elif k in ("buy", "sell"):
                self.on_swap(e, k == "buy", slot, sig, ordinal, bt, now, tx_index, line=line)
            elif k in ("complete", "migrated", "graduated", "migration"):
                self.book.on_graduated(e.token_id)
                if self.ps_capture:
                    self.note_graduation(e.token_id, k, slot, sig, ordinal, tx_index, bt, received_at, now)
            else:
                self.stats["ev_other"] += 1
                if e.lifecycle is not None and getattr(e.lifecycle, "value", "") == "migrated":
                    self.book.on_graduated(e.token_id)
        if self.txshape is not None:                  # LAST: after every band emission and funded hook of this transaction
            self.sample_txshape(info, msg, keys, sig, slot, tx_index, received_at, now)

    def _retired_curve_event(self, e, slot, sig, ordinal, tx_index, bt, received_at, now, line=None):
        """Curve retired: only graduation is kept (PumpSwap binding); no curve row, no gap, no band/standing/watch action.
        grad_class reads the mint's last curve row, so it is 'unknown' for coins whose curve was never recorded."""
        if e.blocker is not None:                       # the live path's post_complete_buy graduation, mirrored (Lambda)
            if line is not None:
                try:
                    import base64 as _b64
                    kind, what = classify_unknown_event(_b64.b64decode(line.split("Program data: ", 1)[1]))
                    if kind == "post_complete_buy":
                        self.book.on_graduated(what)
                        return
                except (ValueError, IndexError):
                    pass
            self.stats["curve_retired_skipped"] += 1
            return
        k = getattr(e.event_kind, "value", str(e.event_kind))
        if k in ("complete", "migrated", "graduated", "migration"):
            self.book.on_graduated(e.token_id)
            if self.ps_capture:
                self.note_graduation(e.token_id, k, slot, sig, ordinal, tx_index, bt, received_at, now)
        elif k not in ("launch", "buy", "sell") and e.lifecycle is not None and getattr(e.lifecycle, "value", "") == "migrated":
            self.book.on_graduated(e.token_id)          # the live path's lifecycle 'migrated' branch, mirrored
        else:
            self.stats["curve_retired_skipped"] += 1

    def sample_ps_state_witness(self, raw, slot, sig, tx_index, received_at, detail):
        """Optional diagnostics after the original loss bookkeeping; never tape authority."""
        if not self.ps_state_witness_enabled:
            return None
        try:
            if self.ps_state_witness is None:
                if self.ps_state_witness_init_attempted:
                    return None
                self.ps_state_witness_init_attempted = True
                from sniper.services.memecoin_desk_state_witness import Writer as StateWitnessWriter
                self.ps_state_witness = StateWitnessWriter(self.base)
            from sniper.services.memecoin_desk_pumpswap import PUMP_AMM_IDL_SHA256
            return self.ps_state_witness.capture(raw, signature=sig, slot=slot, tx_index=tx_index,
                received_at=received_at, known_at=time.time(), discriminator=detail['discriminator'],
                reason=detail['reason'], idl_sha256=PUMP_AMM_IDL_SHA256, decoder_root=str(ROOT))
        except Exception as exc:
            self.stats['ps_state_witness_error'] += 1
            cause = type(exc).__name__
            if cause not in self.ps_state_witness_errors:
                self.ps_state_witness_errors.add(cause)
                log.exception("state witness capture failed; original loss retained")
            return None

    def sample_ps_decode_witness(self, raw, info, keys, sig, received_at, reason):
        """Eight first-only public receipts for missing multi-hop layouts; never scope or trade proof."""
        try:
            top, groups = info.transaction.message.instructions, info.meta.inner_instructions
            amm, pump = U.b58decode(PUMPSWAP_PROGRAM), U.b58decode(U.PUMP_PROGRAM)
            count = next((len(ix.accounts) for ix in itertools.chain(top, (ix for g in groups for ix in g.instructions))
                          if ix.program_id_index < len(keys) and keys[ix.program_id_index] == amm
                          and len(ix.data) == 24 and ix.data[:8] == bytes.fromhex('2b644913e9f66f94')
                          and len(ix.accounts) in (26, 31, 36, 41)), None)
            if count is None:
                return
            has_pump = any(ix.program_id_index < len(keys) and keys[ix.program_id_index] == pump
                           for ix in itertools.chain(top, (ix for g in groups for ix in g.instructions)))
            name = 'multi_hop_%d_%s.json' % (count, 'pump' if has_pump else 'amm')
            if name in self.ps_witness_attempted:
                return
            self.ps_witness_attempted.add(name)  # Failed writes and oversized frames get no retry this process.
            if len(raw) > 256 * 1024:
                self.stats['ps_decode_witness_oversize'] += 1
                return
            from sniper.services.memecoin_desk_pumpswap import PUMP_AMM_IDL_SHA256
            encoded = json.dumps({'raw_b64': base64.b64encode(raw).decode(), 'received_at': received_at,
                                  'signature': sig, 'decoder_root': str(ROOT), 'idl_sha256': PUMP_AMM_IDL_SHA256,
                                  'reason': reason[:160]}, sort_keys=True).encode()
            if len(encoded) > 256 * 1024:
                self.stats['ps_decode_witness_oversize'] += 1
                return
            directory = self.base / 'pumpswap' / 'decode_witnesses'
            directory.mkdir(parents=True, exist_ok=True)
            try:
                target = (directory / name).open('xb')
            except FileExistsError:
                self.stats['ps_decode_witness_existing'] += 1
                return
            with target:
                target.write(encoded)
            self.stats['ps_decode_witness_stored'] += 1
        except Exception:
            self.stats['ps_decode_witness_error'] += 1

    def sample_txshape(self, info, msg, keys, sig, slot, tx_index, received_at, now):
        """Hash-sampled routing record of this swap transaction (TXS). Own sqlite handle, own try/except: it can never delay or break the
        tape or a band emission, and a failure is a counter in the heartbeat."""
        w = self.txshape
        if not TXS.hash_sampled(sig, w.pct):
            return
        started = time.perf_counter()
        try:
            kb = [U.b58encode(k) if isinstance(k, (bytes, bytearray)) else k for k in keys]
            top = [(ix.program_id_index, bytes(ix.accounts), bytes(ix.data)) for ix in msg.instructions]
            inner = [(g.index, [(ix.program_id_index, bytes(ix.accounts), bytes(ix.data)) for ix in g.instructions])
                     for g in info.meta.inner_instructions]
            ps, curve = PUMPSWAP_PROGRAM in kb, U.PUMP_PROGRAM in kb
            row = TXS.shape(kb, top, inner, venue="both" if ps and curve else "ps" if ps else "curve")
            w.add(sig, slot, tx_index, received_at, row, now)
        except Exception:                             # a malformed frame is a skipped sample, never a worker failure
            w.stats["errors"] += 1
            self.stats["txshape_errors"] += 1
        finally:
            self.txshape_us.append((time.perf_counter() - started) * 1e6)

    def pool_binding(self, pool, now, *, slot, signature, ordinal, tx_index, received_at):
        """Read only captured creation facts; never enrich or rewrite a prior unmapped trade.
        Two sources: `pools` (a captured create_pool event, status 'mapped') and `ps_grads` (a curve-side
        graduation whose canonical pool is derived from the mint, status 'mapped_curve_derived'). A derived row
        is causal only for a swap in a LATER slot, or the same slot with both tx_index proven and the swap's
        later: the curve event and the pool swap of one transaction are never ordered against each other."""
        if pool not in self.ps_bindings:
            query = ("SELECT mint,quote_mint,canonical,known_at,slot,signature,ordinal,tx_index,received_at,0 "
                     "FROM pools WHERE pool=?")
            rows = list(self.files.conn("pools", now).execute(query, (pool,)))
            derived = ("SELECT mint,quote_mint,1,known_at,slot,signature,ordinal,tx_index,received_at,1 "
                       "FROM ps_grads WHERE pool=?")
            rows += list(self.files.conn("ps_grads", now).execute(derived, (pool,)))
            self.ps_bindings[pool] = rows
            if len(self.ps_bindings) > 4096:
                self.ps_bindings.popitem(last=False)
        self.ps_bindings.move_to_end(pool)
        rows = [r for r in self.ps_bindings[pool] if r[3] <= now]
        identities = {(r[0], r[1]) for r in rows}
        if len(identities) > 1:
            return None, "conflict"
        def before(r):
            if r[4] > slot or r[8] > received_at:
                return False
            if r[9]:                                       # curve-derived: strict, see the docstring
                return r[4] < slot or (r[4] == slot and r[5] != signature
                                       and r[7] is not None and tx_index is not None and r[7] < tx_index)
            if r[5] == signature:
                return r[6] < ordinal
            return r[4] < slot or (r[7] is not None and tx_index is not None and r[7] < tx_index)
        canonical = [r for r in rows if r[2] and before(r)]
        if not canonical:
            return None, "unmapped"
        r = min(canonical, key=lambda r: (r[3], r[9]))     # the earlier fact wins; a captured create_pool wins a tie
        return ({"pool": pool, "mint": r[0], "quote_mint": r[1], "known_at": r[3], "slot": r[4],
                 "canonical": True, "map_source": "curve_derived" if r[9] else "create_pool"},
                "mapped_curve_derived" if r[9] else "mapped")

    def keep_window(self, c, pool, received, sig, slot):
        """Retention window around a graduation. A pool can get two anchors (the curve-side event and the later
        create_pool): the window only ever WIDENS, so a migration that lags `complete` cannot be pruned."""
        c.execute("INSERT INTO ps_keep VALUES(?,?,?,?,?) ON CONFLICT(pool) DO UPDATE SET "
                  "keep_from = MIN(keep_from, excluded.keep_from), keep_until = MAX(keep_until, excluded.keep_until)",
                  (pool, received - PS_KEEP_BEFORE_S,
                   received + self.ps_keep_window_s - PS_KEEP_BEFORE_S, sig, slot))
        c.execute("CREATE TABLE IF NOT EXISTS ps_keep_ext(pool TEXT PRIMARY KEY, base_until REAL, extended_at REAL) WITHOUT ROWID")
        c.execute("UPDATE ps_keep_ext SET base_until = MAX(base_until, ?) WHERE pool = ?",     # a later ordinary anchor is baseline, not extension: a shed must not undo it
                  (received + self.ps_keep_window_s - PS_KEEP_BEFORE_S, pool))

    @staticmethod
    def grad_class(quote_mint, real_quote):
        """What the migration event is, from the mint's LAST curve row: 'quote_token' (a curve quoted in another token),
        'sol85' (a SOL curve that ran to its threshold: the desk's population), 'sub_threshold' (a SOL curve far below
        it, e.g. coins that reach PumpSwap after 1-3 curve swaps), 'unknown' (no curve row / NULL real_quote)."""
        if quote_mint is None or real_quote is None:
            return "unknown"
        if quote_mint != SOL_QUOTE:
            return "quote_token"
        return "sol85" if real_quote >= SOL_CURVE_FULL_LAMPORTS else "sub_threshold"

    def note_graduation(self, mint, kind, slot, sig, ordinal, tx_index, bt, received, now):
        """A curve-side graduation event (complete / migration) for `mint`: record it with the canonical pool
        derived from the mint. Off the fill path (this worker feeds the desk, never the entry). A mint that
        cannot be derived is a stat, never a row."""
        try:
            from sniper.services.memecoin_desk_safety import pumpswap_pool_address
            pool = pumpswap_pool_address(mint)
        except Exception:                                  # noqa: BLE001 - recorded, never fatal to the curve path
            self.stats["ps_grad_underivable"] += 1
            return
        try:
            c = self.files.conn("ps_grads", now)
            try:                                           # the mint's last curve row (indexed swaps(mint, slot)), this day's file
                last = self.files.conn("swaps", now).execute(
                    "SELECT quote_mint, real_quote FROM swaps WHERE mint=? ORDER BY slot DESC, ordinal DESC LIMIT 1",
                    (mint,)).fetchone()
            except (sqlite3.Error, OSError):
                last = None
            cq, rq = (last or (None, None))
            if last is None:
                self.stats["ps_grad_no_curve_row"] += 1       # the day file lacks the mint (midnight roll, restart): class unknown
            inserted = c.execute("INSERT OR IGNORE INTO ps_grads(pool,mint,quote_mint,kind,slot,signature,ordinal,tx_index,"
                                 "block_time,received_at,known_at,curve_quote_mint,curve_real_quote,grad_class) "
                                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (pool, mint, WSOL_QUOTE, kind, slot, sig, ordinal, tx_index, bt, received, now,
                                  cq, rq, self.grad_class(cq, rq))).rowcount
            if inserted:
                self.ps_bindings.pop(pool, None)           # a cached 'unmapped' must see the new fact
                self.stats["ps_grads"] += 1
                self.stats["ps_grads:" + self.grad_class(cq, rq)] += 1
                self.keep_window(c, pool, received, sig, slot)
                self.add_matter(pool, now)
        except (sqlite3.Error, OSError) as exc:
            self.ps_storage_error(exc)

    def pool_metadata(self, pool, binding, received):
        self.ps_metadata_reason = None
        if self.pool_metadata_fn is not None:
            return self.pool_metadata_fn(pool)
        path = self.base / "pumpswap" / "metadata.db"
        if binding is None or not path.is_file():
            return None
        from sniper.services.memecoin_desk_ps_metadata import read_metadata
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
            try:
                row = read_metadata(conn, binding["mint"], as_of=received)
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError, TypeError):
            self.stats["ps_metadata_errors"] += 1
            self.ps_metadata_reason = "metadata_unreadable"
            return None
        if row is None:
            return None
        return {"pool": pool, "mint": binding["mint"], "quote_mint": binding["quote_mint"],
                "base_decimals": row["decimals"], "supply_atomic": row["supply_atomic"],
                "known_at": row["metadata_known_at"], "slot": row["metadata_slot"],
                "source": row["metadata_source"]}

    def on_pumpswap(self, e, slot, sig, ordinal, tx_index, bt, received, now):
        if e["kind"] == "create_pool":
            c = self.files.conn("pools", now)
            inserted = c.execute("INSERT OR IGNORE INTO pools VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (e["pool"], e["base_mint"], e["quote_mint"], e["pool_creator"], int(e["canonical"]),
                                  slot, sig, ordinal, tx_index, e["event_time"], bt, received, now, now,
                                  json.dumps(e["payload"], sort_keys=True), e["payload_b64"],
                                  "solana", "pumpswap", PUMPSWAP_PROGRAM)).rowcount
            self.stats["ps_pools" if inserted else "ps_duplicate"] += 1
            if inserted and e["canonical"]:
                self.keep_window(c, e["pool"], received, sig, slot)
                self.add_matter(e["pool"], now)
            if inserted:
                self.ps_bindings.pop(e["pool"], None)
            return
        binding, status = self.pool_binding(e["pool"], now, slot=slot, signature=sig, ordinal=ordinal,
                                            tx_index=tx_index, received_at=received)
        mint, quote, known = ((binding["mint"], binding["quote_mint"], binding["known_at"])
                              if binding else (None, None, None))
        c = self.files.conn("swaps_ps", now)
        nullable_text = lambda v: None if v is None else str(v)
        capture_keys=(PS_PAYLOAD_KEYS+SELL_FEE_CAPTURE_KEYS if e['kind']=='sell' else
                      PS_PAYLOAD_KEYS+BUY_EXACT_CAPTURE_KEYS if e['payload'].get('ix_name')=='buy_exact_quote_in' else PS_PAYLOAD_KEYS)
        row = (e["pool"], mint, quote, slot, sig, ordinal, tx_index, int(e["kind"] == "buy"),
               e["actor"], e["coin_creator"], e["base_atomic"], e["quote_atomic"],
               e["pre_base_reserve_atomic"], e["pre_quote_reserve_atomic"],
               e["post_base_reserve_atomic"], e["post_quote_reserve_atomic"],
               *[nullable_text(e[k]) for k in ("lp_fee_bps", "protocol_fee_bps", "creator_fee_bps",
                                             "cashback_fee_bps", "buyback_fee_bps")],
               e["event_time"], bt, received, now, known, status,
               json.dumps({k: e["payload"][k] for k in capture_keys if k in e["payload"]},
                          sort_keys=True), None,
               "solana", "pumpswap", PUMPSWAP_PROGRAM)
        self.count_swap(e["pool"], slot, received, int(e["kind"] == "buy"))
        if e["actor"] in self.ps_watch:
            self.watch_record(c, e, row, mint, quote, slot, sig, ordinal, tx_index, received)
        # PS candidates are M pools only: the book prices a swap only through a mapping, and a mapping exists only for a canonical
        # create_pool pool or a curve-derived graduation, i.e. a pool in M. Held rows flushed by add_matter were 'unmapped' when processed,
        # so the book would not have priced them either; they skip the book and pricing_ps.
        if e['kind']=='sell' or e['payload'].get('ix_name')=='buy_exact_quote_in':
            try:
                witness_status=SFW.record(c,e,signature=sig,ordinal=ordinal,slot=slot,tx_index=tx_index,
                                          received_at=received,known_at=now)
                self.stats['ps_sell_fee_witness:'+witness_status]+=1
            except Exception as exc:
                self.stats['ps_sell_fee_witness:error']+=1
                cause=type(exc).__name__
                if cause not in self.ps_sell_fee_witness_errors:
                    self.ps_sell_fee_witness_errors.add(cause)
                    log.exception("sell fee witness failed; captured swap retained")
        listed = (self.intake_book is not None and e["pool"] in self.intake_book.pools) or e["pool"] in self.ps_extended     # Alpha F1: M while listed
        if self.ps_filter and e["pool"] not in self.matter_set(now) and not listed:
            self.hold_row(e["pool"], received, row)        # not stored unless the pool enters M within PS_HOLDBACK_S
            self.stats["ps_swaps_seen"] += 1
            return
        inserted = c.execute("INSERT OR IGNORE INTO swaps_ps VALUES(" + ",".join("?" for _ in range(32)) + ")", row).rowcount
        if not inserted:
            self.stats["ps_duplicate"] += 1
            return
        self.stats["ps_swaps"] += 1
        self.stats["ps_swaps_seen"] += 1
        if status != "mapped":
            self.stats["ps_" + status] += 1
        if self.intake_book is not None and not self.ps_pending_gaps:
            intake_binding = binding
            # `listed` also covers keep-extension pools, which are not in the intake book: only the book proves this identity.
            if binding is None and status == "unmapped" and e["pool"] in self.intake_book.pools:
                # Alpha F2: no captured binding, the print IS on the canonical PDA of a listed mint (pools is keyed by
                # S.pumpswap_pool_address(mint)): the address itself proves the identity. Intake hook only; the tape row
                # keeps its own 'unmapped' status and the band book never sees this binding.
                intake_binding = {"pool": e["pool"], "mint": self.intake_book.pools[e["pool"]], "quote_mint": WSOL_MINT,
                                  "known_at": received, "slot": slot, "canonical": True,
                                  "map_source": "intake_canonical_pda"}
            self.intake_hook(e, intake_binding, slot, sig, ordinal, tx_index, now, received)
        if self.ps_book is not None:
            candidate, reason = None, "tape_gap"
            if not self.ps_pending_gaps:
                metadata = self.pool_metadata(e["pool"], binding, received)
                self.stats["ps_pricing_unknown"] += int(metadata is None)
                candidate = self.ps_book.on_swap(event=e, mapping=binding, metadata=metadata, slot=slot,
                                                 signature=sig, ordinal=ordinal, tx_index=tx_index, now=now,
                                                 received_at=received,
                                                 sol_usd=self.sol_usd, sol_usd_at=self.sol_at)
                reason = self.ps_metadata_reason or self.ps_book.last_reason
            self.files.conn("pricing_ps", now).execute("INSERT OR IGNORE INTO pricing_ps VALUES(?,?,?,?,?,?)",
                (sig, ordinal, utc_day(received), received, now, reason))
            if candidate is not None:
                path = self.base / "candidate_emissions_ps" / f"{utc_day(now)}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a") as output:
                    output.write(json.dumps(candidate, sort_keys=True) + "\n")
                self.stats["ps_emissions"] += 1

    def fx_at_or_before(self, cut):
        """(sol_usd, sol_usd_at) of the newest cached sample taken at or before `cut`, else (None, None). Am36 cuts at
        the trigger's receipt batch end, so a sample fetched after the print must not price it; the previous cached
        sample (with its own known age) does. No fetch here."""
        for usd, at in ((self.sol_usd, self.sol_at), self.sol_prev):
            if usd is not None and at is not None and at <= cut:
                return usd, at
        return None, None

    def intake_hook(self, e, binding, slot, sig, ordinal, tx_index, now, received):
        """One stored-tape PS print -> desk intake candidates. Any failure is counted and logged here, never raised."""
        try:
            if e.get("kind") != "buy" or not isinstance(binding, dict) or binding.get("mint") not in self.intake_book.listed:
                return
            metadata = self.pool_metadata(e["pool"], binding, received)
            from sniper.services.memecoin_desk_intake_book import batch_end
            sol_usd, sol_at = self.fx_at_or_before(batch_end(received))
            if sol_usd is None and self.sol_usd is not None:
                # fail-closed, sightings kept for the next print. A PRINT diagnostic only (Am38: the reported fx_after_cut
                # share is computed from persisted tickets), and only while this coin still has pending sightings.
                if self.intake_book.listed.get(binding.get("mint")):
                    self.stats["prints_fx_after_cut"] += 1
                    self.intake_book.fx_deferred(binding["mint"], now)     # Am39: the persisted per-coin record
                return
            for oid, body in self.intake_book.on_print(event=e, mapping=binding, metadata=metadata, slot=slot,
                                                       signature=sig, ordinal=ordinal, tx_index=tx_index, now=now,
                                                       received_at=received, sol_usd=sol_usd, sol_usd_at=sol_at):
                self.intake_append(oid, body, now)
        except Exception:
            self.stats["intake_errors"] += 1
            log.exception("desk intake hook failed; PumpSwap tape unaffected")

    def intake_append(self, oid, body, now):
        """Append the claim's stored bytes, then mark it emitted (a crash between the two re-appends the same bytes)."""
        try:
            path = self.base / "candidate_emissions_intake" / f"{utc_day(now)}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+b") as output:
                output.seek(0, os.SEEK_END)
                if output.tell():                       # a crash mid-append left a partial line: terminate it, so the
                    output.seek(-1, os.SEEK_END)        # re-appended claim is its own line (never glued to garbage)
                    if output.read(1) != b"\n":
                        output.write(b"\n")
                output.write((body + "\n").encode())
                output.flush()
                os.fsync(output.fileno())
            self.intake_book.committed(oid)
            self.stats["intake_emissions"] += 1
        except Exception:
            self.stats["intake_errors"] += 1
            log.exception("desk intake append failed ticket=%s; stays pending for the next start", oid)

    def intake_apply(self, read, now):
        try:
            before = set(self.intake_book.pools)
            self.intake_book.apply_listed(read, now)
            selected = set(self.intake_book.pools)
            # The union of active feeds is selected NOW; held historical rows do not backdate inclusion.
            added, removed = selected - self.ps_selected, self.ps_selected - selected
            if added or removed or self.ps_selection_repair:
                c = self.files.conn("ps_pool_selection", now)
                if self.ps_selection_repair:
                    c.execute("UPDATE ps_pool_selection SET end_at=? WHERE session=? AND end_at IS NULL",
                              (now, self.ps_coverage_session))
                    self.ps_selection_repair = False
                c.executemany("INSERT INTO ps_pool_selection VALUES(?,?,?,?,?)",
                              [(pool, self.ps_coverage_session, now, None, PSL.PROTOCOL) for pool in added])
                c.executemany("UPDATE ps_pool_selection SET end_at=? WHERE pool=? AND session=? AND end_at IS NULL",
                              [(now, pool, self.ps_coverage_session) for pool in removed])
                self.ps_selected = selected
                for pool in removed:                # the feed's close ended every open row of the pool: an extended pool is still being stored
                    if pool in self.ps_extended:
                        c.execute("INSERT OR IGNORE INTO ps_pool_selection VALUES(?,?,?,?,?)", (pool, self.ps_coverage_session, now, None, PSL.PROTOCOL))
                        self.ps_ext_rows[pool] = now
            for pool in selected - before:      # newly listed: store its held rows (like add_matter,
                held = self.ps_hold.pop(pool, None)                # but M itself is not changed: F1, never persisted)
                if held:
                    self.ps_hold_rows -= len(held)
                    c = self.files.conn("swaps_ps", now)
                    for _received, row in held:
                        self.stats["ps_swaps"] += int(c.execute(
                            "INSERT OR IGNORE INTO swaps_ps VALUES(" + ",".join("?" for _ in range(32)) + ")", row).rowcount)
                    self.stats["ps_held_flushed_intake"] += len(held)
        except Exception:
            self.stats["intake_errors"] += 1
            log.exception("desk intake reload failed")

    # --- keep extension (keep_extend.json): longer tape for PASS and open-order pools -----------------------------------
    def keep_extend_tick(self, now):
        """Read the one-way list and EXTEND. A pool already in ps_keep gets keep_until = MAX(keep_until, until) (keep_from is never touched, and the persisted
        value outlives the file: stopping the writer stops future extensions, windows already written age out). A pool outside M is added to the stored set
        while listed and gets its selection row from its actual inclusion, never earlier. Extensions never stop capture: shed first, ignore errors."""
        try:
            self.keep_extend_drop(now, "expired")             # expiry never depends on the file's health
            if self.ps_ext_shed:
                self.keep_extend_drop(now, "shed")
                return
            got = KE.read(self.ps_ext_path, now)
            if got["state"] != "ok":
                self.stats["ps_ext_unavailable"] += 1
                if got["reason"] != "missing" and self.ps_ext_warn.due("read:" + str(got["reason"]), now):
                    log.warning("desk PS keep extension list unavailable (%s); no change", got["reason"])
                return
            if got["dropped"]:
                self.stats["ps_ext_dropped"] += got["dropped"]
            c = self.files.conn("ps_keep", now)
            c.execute("CREATE TABLE IF NOT EXISTS ps_keep_ext(pool TEXT PRIMARY KEY, base_until REAL, extended_at REAL) WITHOUT ROWID")
            c.execute("DELETE FROM ps_keep_ext WHERE pool NOT IN (SELECT pool FROM ps_keep)")      # archived windows leave the ledger
            for pool, until in got["pools"].items():
                row = c.execute("SELECT keep_until FROM ps_keep WHERE pool = ?", (pool,)).fetchone()
                if row is not None:
                    if until > row[0]:           # the pre-extension end is recorded once, so a shed can undo exactly this and nothing else
                        c.execute("INSERT OR IGNORE INTO ps_keep_ext VALUES(?,?,?)", (pool, row[0], now))
                        c.execute("UPDATE ps_keep SET keep_until = ? WHERE pool = ?", (until, pool))
                        self.stats["ps_ext_extended"] += 1
                    continue
                if self.ps_extended.get(pool) is None:
                    if pool in self.ps_selected:                    # a feed row already covers it; it is reopened for us if the feed lets go first
                        self.ps_ext_rows[pool] = None
                    else:
                        sel = self.files.conn("ps_pool_selection", now)
                        sel.execute("INSERT OR IGNORE INTO ps_pool_selection VALUES(?,?,?,?,?)", (pool, self.ps_coverage_session, now, None, PSL.PROTOCOL))
                        self.ps_ext_rows[pool] = now
                    self.stats["ps_ext_added"] += 1
                self.ps_extended[pool] = until
            self.keep_extend_drop(now, "expired", keep=got["pools"])
        except Exception:
            self.stats["ps_ext_errors"] += 1
            if self.ps_ext_warn.due("tick", now):
                log.exception("desk PS keep extension failed; capture unaffected")

    def keep_extend_drop(self, now, why, keep=None):
        """Pools leave the extended set when their until passes, they leave the list, or the soft shed is on; their selection row ends at that moment."""
        gone = [p for p, u in self.ps_extended.items() if u <= now or (keep is not None and p not in keep) or why == "shed"]
        if not gone:
            return
        sel = self.files.conn("ps_pool_selection", now)
        for pool in gone:
            start = self.ps_ext_rows.pop(pool, None)
            if start is not None:             # only the row we opened (same capture session as the feed, so the coverage join still works)
                sel.execute("UPDATE ps_pool_selection SET end_at=? WHERE pool=? AND session=? AND start_at=? AND end_at IS NULL", (now, pool, self.ps_coverage_session, start))
            del self.ps_extended[pool]
        self.stats["ps_ext_removed"] += len(gone)

    def keep_extend_shed(self, ps_used_gb, free_gb, now):
        """Soft shed BELOW the hard stop: extensions go first so they can never be what stops capture. On above 80 percent of the PS cap or under 100 GB free
        on the host; off again below 70 percent and above 110 GB (hysteresis). Windows already extended in ps_keep stay and age out."""
        cap = float(self.cfg["ps_hard_gb"])
        if not self.ps_ext_shed and (ps_used_gb >= 0.8 * cap or free_gb < KE.SHED_FREE_GB):
            self.ps_ext_shed = True
            self.stats["ps_ext_shed"] += 1
            log.warning("desk PS keep extensions shed: PS %.2f GB of %.2f GB cap, free %.1f GB (extensions dropped before capture would stop)", ps_used_gb, cap, free_gb)
        elif self.ps_ext_shed and ps_used_gb < 0.7 * cap and free_gb >= KE.SHED_FREE_GB + 10:
            self.ps_ext_shed = False
            log.warning("desk PS keep extensions resumed: PS %.2f GB, free %.1f GB", ps_used_gb, free_gb)
        if self.ps_ext_shed:
            self.keep_extend_drop(now, "shed")
            self.keep_extend_rollback(now)

    def keep_extend_rollback(self, now):
        """Shed also undoes the DURABLE widening, explicitly and counted: each extended window goes back to its recorded pre-extension end (never below now, so no row inside
        a window that is still open is exposed). Rows past that end are then pruned like any other tape. Without this an existing-M pool keeps its widened window under
        pressure, and extensions could still be what fills the cap."""
        try:
            c = self.files.conn("ps_keep", now)
            c.execute("CREATE TABLE IF NOT EXISTS ps_keep_ext(pool TEXT PRIMARY KEY, base_until REAL, extended_at REAL) WITHOUT ROWID")
            rows = c.execute("SELECT e.pool, e.base_until FROM ps_keep_ext e JOIN ps_keep k ON k.pool = e.pool WHERE k.keep_until > e.base_until").fetchall()
            for pool, base in rows:
                c.execute("UPDATE ps_keep SET keep_until = ? WHERE pool = ?", (max(base, now), pool))
            c.execute("DELETE FROM ps_keep_ext")
            if rows:
                self.stats["ps_ext_rolled_back"] += len(rows)
                log.warning("desk PS keep extensions rolled back on shed: %d windows returned to their original end", len(rows))
        except Exception:
            self.stats["ps_ext_errors"] += 1
            log.exception("desk PS keep extension rollback failed; capture unaffected")

    # --- write filter (PS_HOLDBACK_S): which pools get full rows ------------------------------------------
    def matter_set(self, now):
        """M = captured canonical create_pool pools U every curve-side graduation pool (ps_grads) U ps_keep pools, loaded from the
        tape once and kept current by add_matter. Safe on an empty tape."""
        if self.ps_matter is None:
            m = set()
            c = self.files.conn("pools", now)
            for sql in ("SELECT pool FROM pools WHERE canonical=1", "SELECT pool FROM ps_grads", "SELECT pool FROM ps_keep"):
                m.update(r[0] for r in c.execute(sql))
            self.ps_matter = m
            self.stats["ps_matter_loaded"] = len(m)
        return self.ps_matter

    def add_matter(self, pool, now):
        """A pool entered M: its held swaps (processed up to PS_HOLDBACK_S ago) are stored now, with the map_status computed
        when they were processed (causality unchanged)."""
        if not self.ps_filter:
            return
        self.matter_set(now).add(pool)
        held = self.ps_hold.pop(pool, None)
        if not held:
            return
        self.ps_hold_rows -= len(held)
        c = self.files.conn("swaps_ps", now)
        for _received, row in held:
            self.stats["ps_swaps"] += int(c.execute("INSERT OR IGNORE INTO swaps_ps VALUES(" + ",".join("?" for _ in range(32)) + ")",
                                                    row).rowcount)
        self.stats["ps_held_flushed"] += len(held)

    def watch_tick(self, now):
        """Reads the one-way list. Missing is empty; unreadable keeps the old list and warns: a bad file never stops capture."""
        self.ps_setup_watch = {w: expiry for w, expiry in self.ps_setup_watch.items() if expiry > now}
        leads = sorted(set(self.ps_setup_watch) - self.ps_listed_watch)
        self.ps_watch = self.ps_listed_watch | frozenset(leads)
        try:
            path = Path(self.ps_ext_path).with_name("watch_wallets.json")
            wallets = json.loads(path.read_text())["wallets"] if path.exists() else []
            if not isinstance(wallets, list) or not all(isinstance(w, str) for w in wallets):
                raise ValueError("watch_list_malformed")
            self.ps_listed_watch = frozenset(wallets)
            listed = list(dict.fromkeys(wallets))
            leads = sorted(set(self.ps_setup_watch) - set(listed))
            self.ps_watch = frozenset(listed + leads)
            if not self.ps_watch:
                return
            c = self.files.conn("swaps_ps", now)
            if not self.ps_watch_ready:
                c.execute(PS_WATCH_DDL)
                c.execute("CREATE INDEX IF NOT EXISTS ps_watch_events_at ON ps_watch_events(received_at)")
                c.execute("CREATE INDEX IF NOT EXISTS wallet_setup_events_at ON wallet_setup_events(received_at)")
                self.ps_watch_ready = True
            if now - self.ps_watch_pruned >= 3600.0:        # indexed and hourly: this runs on the capture loop
                self.ps_watch_pruned = now
                c.execute("DELETE FROM ps_watch_events WHERE received_at<?", (now - PS_WATCH_KEEP_S,))
                c.execute("DELETE FROM wallet_setup_events WHERE received_at<?", (now - PS_WATCH_KEEP_S,))
        except Exception:
            self.stats["ps_watch_list_errors"] += 1
            log.exception("watch list read failed; keeping %d wallets", len(self.ps_watch))

    def watch_record(self, c, e, row, mint, quote, slot, sig, ordinal, tx_index, received):
        """Never costs a captured swap: its own failure is counted and logged."""
        try:
            self.stats["ps_watch_events"] += c.execute("INSERT OR IGNORE INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sig, ordinal, e["pool"], mint, quote, slot, tx_index, int(e["kind"] == "buy"), e["actor"], e["base_atomic"], e["quote_atomic"],
                 e["pre_quote_reserve_atomic"], e["post_quote_reserve_atomic"], received)).rowcount
        except Exception:
            self.stats["ps_watch_errors"] += 1
            log.exception("watch record failed sig=%s", sig)

    def hold_row(self, pool, received, row):
        self.ps_hold.setdefault(pool, []).append((received, row))
        self.ps_hold_rows += 1
        while self.ps_hold_rows > PS_HOLD_MAX_ROWS:       # bounded memory: the oldest held row of the oldest pool goes
            first = next(iter(self.ps_hold))
            self.ps_hold[first].pop(0)
            self.ps_hold_rows -= 1
            self.stats["ps_hold_overflow"] += 1
            if not self.ps_hold[first]:
                del self.ps_hold[first]

    def expire_hold(self, now):
        cut = now - PS_HOLDBACK_S
        for pool in list(self.ps_hold):
            rows = self.ps_hold[pool]
            keep = [r for r in rows if r[0] >= cut]
            dropped = len(rows) - len(keep)
            if dropped:
                self.ps_hold_rows -= dropped
                self.stats["ps_filtered"] += dropped      # seen, counted in ps_pool_hours, never stored
            if keep:
                self.ps_hold[pool] = keep
            else:
                del self.ps_hold[pool]

    def count_swap(self, pool, slot, received, buy):
        key = (pool, int(received // 3600) * 3600)
        a = self.ps_agg.get(key)
        if a is None:
            self.ps_agg[key] = [1, buy, slot, slot, received, received]
        else:
            a[0] += 1
            a[1] += buy
            a[2], a[3] = min(a[2], slot), max(a[3], slot)
            a[4], a[5] = min(a[4], received), max(a[5], received)

    def flush_agg(self, now):
        if not self.ps_agg:
            return
        c = self.files.conn("ps_pool_hours", now)
        c.executemany(
            "INSERT INTO ps_pool_hours VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(pool, hour) DO UPDATE SET swaps=swaps+excluded.swaps, "
            "buys=buys+excluded.buys, first_slot=MIN(first_slot, excluded.first_slot), last_slot=MAX(last_slot, excluded.last_slot), "
            "first_received=MIN(first_received, excluded.first_received), last_received=MAX(last_received, excluded.last_received)",
            [(pool, hour, *a) for (pool, hour), a in self.ps_agg.items()])
        self.ps_agg.clear()

    def ps_live_bytes(self):
        """What the PumpSwap cap compares: LIVE data in the tape ((pages - freelist) x page size, deleted rows' reusable pages are not
        'used') + its WAL + the sidecar dirs. A file that retention keeps at a plateau is not 'growing'."""
        conn = self.files._open.get(("pumpswap", "all"))
        base = self.base / "pumpswap"
        side = sum(desk_bytes(self.base / n) for n in ("candidate_emissions_ps", "ranker_ps") if (self.base / n).exists())
        if conn is None:
            return desk_bytes(base)
        try:
            page = conn.execute("PRAGMA page_size").fetchone()[0]
            live = (conn.execute("PRAGMA page_count").fetchone()[0] - conn.execute("PRAGMA freelist_count").fetchone()[0]) * page
        except sqlite3.Error:
            return desk_bytes(base)                       # unreadable: the file size, the conservative number
        wal = sum(f.stat().st_size for f in base.glob("tape.db-wal") if f.is_file())
        if self.ps_wal_clean:                         # every frame is checkpointed: the WAL file is REUSABLE space like freelist pages (the next write resets it,
            wal = 0                                   # journal_size_limit bounds the physical file), so only live data counts toward the cap
        small = sum(f.stat().st_size for f in base.iterdir() if f.is_file() and not f.name.startswith("tape.db"))   # pool_mints, census
        return live + wal + side + small

    def _apply_span_op(self, op):
        """One VERIFIED span write on the capture connection; True only when the row is there in the wanted state (an UPDATE that matched no row, or a
        failed INSERT, is not success)."""
        conn = self.files.conn("ps_gap_spans", op[1])
        if op[0] == "open":
            _kind, start, cause = op
            conn.execute("INSERT OR IGNORE INTO ps_gap_spans(start_at, end_at, cause, ended_by) VALUES(?,NULL,?,NULL)", (start, cause))
            conn.commit()
            return conn.execute("SELECT 1 FROM ps_gap_spans WHERE start_at=? AND cause=?", (start, cause)).fetchone() is not None
        _kind, start, cause, end, ended_by = op
        cur = conn.execute("UPDATE ps_gap_spans SET end_at=?, ended_by=? WHERE start_at=? AND cause=? AND end_at IS NULL", (end, ended_by, start, cause))
        if cur.rowcount == 0:                           # the open row was never written (an earlier failed insert): record the CLOSED span itself
            conn.execute("INSERT OR IGNORE INTO ps_gap_spans(start_at, end_at, cause, ended_by) VALUES(?,?,?,?)", (start, end, cause, ended_by))
        conn.commit()
        row = conn.execute("SELECT end_at FROM ps_gap_spans WHERE start_at=? AND cause=?", (start, cause)).fetchone()
        return row is not None and row[0] is not None

    def flush_ps_spans(self):
        """Apply the pending span writes in order; an op that fails (or does not verify) stays queued and blocks the ones behind it. True when none is pending."""
        while self.ps_span_pending:
            try:
                ok = self._apply_span_op(self.ps_span_pending[0])
            except (sqlite3.Error, OSError):
                ok = False
            if not ok:
                self.stats["ps_span_errors"] += 1
                log.warning("desk PS gap span write pending (will retry): %r", self.ps_span_pending[0])
                return False
            self.ps_span_pending.pop(0)
        return True

    def write_ps_span(self, start_at):
        """The stop as a span [start_at, NULL): one row per stop (PRIMARY KEY). Queued, verified, retried until it is durable."""
        self.ps_span_pending.append(("open", start_at, "disk_cap"))
        self.flush_ps_spans()

    def restore_ps_stop(self, now):
        """At start, an OPEN disk_cap span means capture was STOPPED when the last process ended: restore that stop (and the hourly resume history) so
        the resume goes through the SAME dwell, flap, free-disk and hysteresis guards. A restart is not a resume. Other open spans are closed by
        close_open_ps_spans(). Never creates the tape. A failed read keeps capture OFF (ps_restore_unread) and is retried at the next disk check: guessing "no stop"
        would resume capture straight past a disk cap that was in force."""
        if not (self.base / "pumpswap" / "tape.db").exists():
            return
        try:
            conn = self.files.conn("gaps_ps", now)                  # a READ never creates the span table
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ps_gap_spans'").fetchone() is None:
                row, resumes = None, []                              # no table: no stop was ever written (a clean read, not an unreadable one)
            else:
                self.files.upgrade_span_table(conn)                  # an older two-column table is upgraded BEFORE the query names cause / ended_by
                row = conn.execute("SELECT start_at, cause FROM ps_gap_spans WHERE end_at IS NULL AND cause IN ('disk_cap', 'legacy') ORDER BY start_at DESC LIMIT 1").fetchone()
                resumes = [r[0] for r in conn.execute("SELECT end_at FROM ps_gap_spans WHERE ended_by='resume' AND end_at>? ORDER BY end_at", (now - 3600,))]
        except (sqlite3.Error, OSError):
            self.stats["ps_span_errors"] += 1
            log.exception("desk PS stop restore failed; capture stays OFF until the stop state is read")
            if self.ps_restore_unread is None:
                self.ps_restore_unread = now
            self.ps_capture, self.ps_book = False, None
            return
        self.ps_resumes.extend(resumes)
        unread_since, self.ps_restore_unread = self.ps_restore_unread, None
        if row is not None:
            self.ps_stopped = {"at": row[0], "cause": row[1]}
            self.ps_capture, self.ps_book = False, None
            self.stats["ps_stop_restored"] += 1
            log.warning("desk PumpSwap capture restored STOPPED (disk cap since %.0f); resumes only through the guards", row[0])
        elif unread_since is not None:                       # the read finally succeeded and no stop is open: capture was off only because the state was unreadable
            self.ps_span_pending.append(("close", unread_since, "restore_unread", now, "restore_retry"))
            if not self.flush_ps_spans():                    # the blackout must be durable BEFORE capture resumes
                self.ps_restore_unread = unread_since
                return
            self.ps_bindings.clear()
            if self.make_ps_book is not None:
                self.ps_book = self.make_ps_book()
            self.ps_capture = True
            self.stats["ps_restore_retry_resumed"] += 1
            log.warning("desk PumpSwap capture resumed: stop state read after %.0f s (no stop was open)", now - unread_since)

    def close_open_ps_spans(self, now, ended_by="restart"):
        """A worker start with PS capture on ends every open span EXCEPT a disk_cap stop that is still in force (restore_ps_stop kept it). Done only
        once the UPDATE is verified; a failure is retried on the next disk check."""
        if self.ps_spans_closed or not (self.ps_capture or self.ps_stopped is not None):
            return
        if not (self.base / "pumpswap" / "tape.db").exists():       # nothing was ever captured: no span to close, no file to create
            self.ps_spans_closed = True
            return
        try:
            conn = self.files.conn("gaps_ps", now)
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ps_gap_spans'").fetchone() is None:
                self.ps_spans_closed = True                          # no span table: nothing to close, and none is created for it
                return
            conn.execute("UPDATE ps_gap_spans SET end_at=?, ended_by=? WHERE end_at IS NULL AND cause NOT IN ('disk_cap', 'legacy')", (now, ended_by))
            if self.ps_stopped is None:
                conn.execute("UPDATE ps_gap_spans SET end_at=?, ended_by=? WHERE end_at IS NULL", (now, ended_by))
            conn.commit()
            left = conn.execute("SELECT COUNT(*) FROM ps_gap_spans WHERE end_at IS NULL AND (cause NOT IN ('disk_cap', 'legacy') OR ? = 0)",
                                (1 if self.ps_stopped is not None else 0,)).fetchone()[0]
        except (sqlite3.Error, OSError):
            self.stats["ps_span_errors"] += 1
            log.exception("desk PS gap span close failed; retried at the next disk check")
            return
        self.ps_spans_closed = left == 0

    def maybe_resume_ps(self, free):
        """Auto-resume PS capture after a disk-cap stop. ALL must hold: the dwell has passed; fewer than ps_resume_max_per_h
        resumes in the last hour; the free-disk floor (min_free + cap) holds; the WAL is truncatable NOW (a pinned WAL is the
        usual cause and it must not resume into the same wall); live PS bytes (data + WAL after that truncate) are at or below
        cap - hysteresis. The PS tape's own data never shrinks while stopped (no prune runs), so a stop caused by DATA, not
        WAL, stays stopped until an operator acts. Only PS fields change: the curve ring, book and `stop` are untouched."""
        now = time.time()
        cap, hyst = float(self.cfg["ps_hard_gb"]), float(self.cfg["ps_resume_hyst_gb"])
        if now - self.ps_stopped["at"] < float(self.cfg["ps_resume_dwell_s"]):
            return False
        while self.ps_resumes and now - self.ps_resumes[0] > 3600:
            self.ps_resumes.popleft()
        if len(self.ps_resumes) >= int(self.cfg["ps_resume_max_per_h"]):
            self.stats["ps_resume_flap_blocked"] += 1
            return False
        if free < float(self.cfg["min_free_gb"]) + cap:
            return False
        wal, pinners = self.ps_wal_relief(now, force=True)
        if pinners or self.stats.get("ps_wal_pinned_last", 0) or self.stats.get("ps_wal_unknown_last", 0) or (not self.ps_wal_clean and wal >= float(self.cfg["ps_wal_try_gb"]) * 1e9):
            return False                                     # still pinned (or frames remain un-checkpointed): never resume
        used = self.ps_live_bytes() / 1e9
        self.stats["ps_disk_gb_x100"] = int(used * 100)
        if used > cap - hyst:
            return False
        self.resume_ps(now, used)
        return True

    def resume_ps(self, now, used_gb):
        start = self.ps_stopped["at"]
        self.ps_span_pending.append(("close", start, self.ps_stopped["cause"], now, "resume"))
        if not self.flush_ps_spans():                        # the span end must be durable BEFORE capture resumes: otherwise the stop vanishes
            self.ps_span_pending = [op for op in self.ps_span_pending if op[0] != "close"]      # a queued close would end the stop at the next flush while capture is still off
            log.warning("desk PS gap span end not verified; resume deferred")
            return
        self.ps_bindings.clear()                             # a pool bound before the gap may have changed during it
        if self.make_ps_book is not None:
            self.ps_book = self.make_ps_book()               # fresh book: no pre-gap reserve is ever reused
        self.ps_capture, self.ps_stopped = True, None
        self.ps_resumes.append(now)
        self.stats["ps_capture_resumed"] += 1
        try:
            self.alert_fn("desk-universe-ps-resume", "PumpSwap capture resumed after %.0f min (PS %.2f GB, cap %.2f GB)" % (
                (now - start) / 60.0, used_gb, float(self.cfg["ps_hard_gb"])))
        except Exception:
            self.stats["ps_alert_errors"] += 1
            log.exception("desk PS resume alert failed")
        log.warning("desk PumpSwap capture resumed after %.0f s", now - start)

    def wal_pinners(self):
        """[(pid, cmdline)] of OTHER processes holding the PS tape (or its -shm) open: the readers that can pin its WAL."""
        names = {str(self.base / "pumpswap" / n) for n in ("tape.db", "tape.db-shm", "tape.db-wal")}
        out = []
        for d in Path("/proc").glob("[0-9]*"):
            if d.name == str(os.getpid()):
                continue
            try:
                if any(os.readlink(f) in names for f in (d / "fd").iterdir()):
                    out.append((int(d.name), (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")[:90]))
            except OSError:
                continue
        return out

    def ps_wal_relief(self, now, force=False):
        """Before the cap check: when the PS WAL is over min(ps_wal_try_gb, cap / 2) (or `force`, a stopped capture's resume check), try a PASSIVE checkpoint on the writer
        connection (our own commit just ran; the connection has timeout 0 and PASSIVE never waits, so a pinned WAL answers at once and the ingest thread never waits;
        RESTART/TRUNCATE stay an operator action). A pinned WAL is NAMED (the holder pids), never reported as 'restart required'. A checkpoint that did not run (busy,
        raised, or no connection) is UNKNOWN: ps_wal_clean stays False and ps_wal_unknown_last blocks a resume. Returns (wal_bytes, pinners)."""
        base = self.base / "pumpswap"
        wal = sum(f.stat().st_size for f in base.glob("tape.db-wal") if f.is_file()) if base.exists() else 0
        self.stats["ps_wal_gb_x100"] = int(wal / 1e9 * 100)
        conn = self.files._open.get(("pumpswap", "all"))
        # The size gate must sit below the cap: a WAL under ps_wal_try_gb (default 2) but over the cap (default 1) counted toward the cap forever. A STOPPED capture
        # (force) always tries: nothing else writes, so nothing else would ever reset the WAL it is waiting on.
        gate = min(float(self.cfg["ps_wal_try_gb"]), float(self.cfg["ps_hard_gb"]) / 2.0) * 1e9
        if conn is None:                                      # nothing to checkpoint with: unknown, never clean (a resume waits for a connection that ran a checkpoint)
            self.stats["ps_wal_unknown_last"] = 1
            self.ps_wal_clean = False
            return wal, []
        if wal < gate and not force:
            self.stats["ps_wal_pinned_last"] = 0                  # a small WAL is not a pin worth blocking a resume
            self.stats["ps_wal_unknown_last"] = 0
            self.ps_wal_clean = False
            return wal, []
        try:
            self.commit_files()
            # PASSIVE only (operator rule): it never waits for a reader and never blocks the ingest thread; reclaiming the file (RESTART/TRUNCATE) stays an
            # operator action. log = frames in the WAL, ckpt = frames checkpointed: ckpt < log means a reader pins the rest.
            began = time.perf_counter()
            _busy, log_frames, ckpt = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            ms = int((time.perf_counter() - began) * 1000)          # published: the post-deploy read of PASSIVE time on the real WAL
            self.stats["ps_wal_checkpoint_ms_last"] = ms
            self.stats["ps_wal_checkpoint_ms_max"] = max(ms, self.stats.get("ps_wal_checkpoint_ms_max", 0))
        except sqlite3.Error:
            log.exception("desk PS WAL checkpoint failed")
            self.stats["ps_wal_checkpoint_errors"] += 1
            self.stats["ps_wal_unknown_last"] = 1             # a failed attempt is as unknown as a busy one: no resume on it
            self.ps_wal_clean = False
            return wal, []
        if _busy or log_frames < 0 or ckpt < 0:             # (1, -1, -1): the checkpoint did not run at all. UNKNOWN, never clean: the WAL keeps counting toward the cap
            self.stats["ps_wal_checkpoint_busy"] += 1
            self.stats["ps_wal_unknown_last"] = 1             # a resume waits for a checkpoint that ran
            self.ps_wal_clean = False
            return wal, []
        pinned = ckpt < log_frames
        self.stats["ps_wal_unknown_last"] = 0
        self.ps_wal_clean = not pinned
        if not pinned:
            self.stats["ps_wal_checkpointed"] += 1
            self.stats["ps_wal_pinned_last"] = 0
            return 0, []                                   # nothing live left in the WAL: the file is reusable space, bounded by journal_size_limit
        self.stats["ps_wal_pinned"] += 1
        self.stats["ps_wal_pinned_last"] = 1
        pinners = self.wal_pinners()
        if now - self.last_wal_alert >= 1800:
            self.last_wal_alert = now
            try:
                self.alert_fn("desk-universe-ps-wal", "PumpSwap WAL %.1f GB not checkpointing; held open by %s" % (
                    wal / 1e9, "; ".join("pid %d %s" % p for p in pinners) or "no other process found (check readers of tape.db)"))
            except Exception:
                self.stats["ps_alert_errors"] += 1
                log.exception("desk PS WAL alert failed")
        return wal, pinners

    def full_event(self, line):
        """The full TradeEvent (pump_curve_buy.parse_trade_event) or None when the line does not parse (counted)."""
        try:
            from sniper.execution.pump_curve_buy import parse_trade_event
            return parse_trade_event(line or "")
        except Exception:
            return None

    def quote_facts(self, e, line, ev=None):
        """(quote_mint, q_amount, q_virtual, q_real). A row with non-zero legacy SOL reserves is a SOL curve
        (the default key). Otherwise the full event is read for its own quote mint and quote-unit amounts; an
        unparseable one is labelled 'unknown', never SOL."""
        if e.virtual_quote_reserves:
            return SOL_QUOTE, None, None, None
        ev = ev if ev is not None else self.full_event(line)
        if ev is None:
            self.stats["quote_mint_unparsed"] += 1
            return "unknown", None, None, None
        return ev["quote_mint"], ev["quote_amount"], ev["virtual_quote_reserves"], ev["real_quote_reserves"]

    def on_swap(self, e, is_buy, slot, sig, ordinal, bt, now, tx_index=None, line=None):
        mint = e.token_id
        curve = e.curve_id or U.bonding_curve(mint)      # TradeEvent has no curve; derive the PDA
        q, t = abs(e.quote_delta_atomic or 0), abs(e.token_delta_atomic or 0)
        ev = self.full_event(line) if line else None          # one parse: the cashback leg and a non-SOL quote
        if line and ev is None:
            self.stats["trade_event_unparsed"] += 1
        quote_mint, q_amount, q_virtual, q_real = self.quote_facts(e, line, ev)
        cb_bps, cb = ((ev.get("cashback_fee_basis_points"), ev.get("cashback")) if ev is not None else (None, None))
        self.files.conn("swaps", now).execute(
            "INSERT OR IGNORE INTO swaps(slot, signature, ordinal, mint, curve, is_buy, quote_atomic, "
            "token_atomic, actor, virtual_quote, virtual_token, block_time, received_at, real_token, fee_bps, "
            "creator_fee_bps, tx_index, real_quote, quote_mint, q_amount, q_virtual, q_real, cashback_fee_bps, cashback) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (slot, sig, ordinal, mint, curve, int(is_buy), q, t, e.actor_id,
             e.virtual_quote_reserves, e.virtual_token_reserves, bt, now, e.real_token_reserves,
             (e.facts or {}).get("fee_basis_points"), (e.facts or {}).get("creator_fee_basis_points"), tx_index,
             e.real_quote_reserves, quote_mint, q_amount, q_virtual, q_real, cb_bps, cb))
        if quote_mint != SOL_QUOTE:
            # THE DESK IS SOL-CURVE ONLY, BY RULE (not by a price of 0): a non-SOL curve is recorded above for
            # the sizing read and never priced, charted or offered to the book.
            self.stats["non_sol_quote_skipped"] += 1
            return
        price = (e.virtual_quote_reserves / 1e9) / (e.virtual_token_reserves / 1e6) \
            if e.virtual_quote_reserves and e.virtual_token_reserves else None
        usd = q / 1e9 * self.sol_usd if self.sol_usd else None
        dq = self.recent[mint]
        dq.append((now, is_buy, usd, e.actor_id, price))
        while dq and now - dq[0][0] > 3600:
            dq.popleft()
        if price is not None and self.bars.add_swap(curve or mint, slot=slot, price=price, quote_usd=usd or 0.0,
                                                    is_buy=is_buy, maker=e.actor_id):
            pass
        # blocks_meta for a slot usually lands AFTER its transactions, so bt is mostly unknown here (re-entry never armed
        # live 15:08-17:30Z 10-23). t_chain is a PROVEN LOWER BOUND on the trade's chain time: its own block time, else the
        # block time of a known earlier slot (block times never decrease with slot); else None and this print decides
        # nothing (Codex: never a guessed clock). A lower bound can only make a print look earlier: never fires early.
        t_chain = self.chain_time_floor(slot, bt)
        if self.standing is not None and price is not None and t_chain is not None:
            self.standing_fire(e, mint, price, slot, sig, ordinal, tx_index, t_chain, now)
        if self.watch is not None:
            self.watch_relook(mint, price, is_buy, e.actor_id, now)
        armed = (self.reentry is not None and t_chain is not None and self.refresh_reentry(now)
                 and self.reentry.pending(mint, t_chain) is not None)
        em = self.book.on_trade(mint=mint, curve=curve, virtual_quote=e.virtual_quote_reserves,
                                virtual_token=e.virtual_token_reserves, slot=slot, signature=sig,
                                ordinal=ordinal, now=now, sol_usd=self.sol_usd, sol_usd_at=self.sol_at,
                                skip_cooldown=armed)
        if em is not None:
            claim = self.reentry.take(mint, t_chain) if armed else None
            if claim is not None:
                self.stats["reentry_recross_emissions"] += 1
            self.emit(em, e, bt, now, reentry=claim)
        elif armed and price is not None:
            self.winner_reentry(mint, curve, price, e, t_chain, slot, sig, ordinal, now)

    def chain_time_floor(self, slot, bt):
        """The trade's block time, else the block time of the highest known slot below it (a proven lower bound),
        else None."""
        if bt is not None:
            return bt
        if self.meta_hi_slot is not None and self.meta_hi_slot < slot:
            return self.meta_hi_bt
        return None

    def watch_relook(self, mint, price, is_buy, actor, now):
        """watch-list-v1: a change since a WATCH look appends ONE re-look request line (journal, flush, then commit);
        the JUDGE worker's writer re-checks it (record_relook). This process never writes the desk DB."""
        from sniper.services.memecoin_desk_structure import AGENTS
        if self.watch.due(now):
            try:
                path = self.base.parent / "memecoin_desk.db"
                conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
                try:
                    prior = self.watch.loaded_at
                    gap = self.last_gap_at is not None and prior is not None and self.last_gap_at >= prior
                    self.watch.load(conn.execute(U.WatchBook.SQL).fetchall(), now, reset=gap)
                finally:
                    conn.close()
            except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                self.watch.failed(now)
                self.stats["watch_read_errors"] += 1
                log.warning("desk watch-list work list read failed: %s", exc)
        gap = self.last_gap_at is not None and self.watch.loaded_at is not None and self.last_gap_at >= self.watch.loaded_at
        self.watch.on_trade(mint=mint, price_sol=price, received_at=now, is_buy=is_buy, actor=actor, agents=AGENTS,
                            gap_since_load=gap, recent=list(self.recent[mint]))
        pending = self.watch.take()
        if not pending:
            return
        path = self.base / "standing_fires" / f"{utc_day(now)}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            for ticket_id, req in pending:
                fh.write(json.dumps({"ticket_id": ticket_id, "relook": req}, sort_keys=True) + "\n")
                fh.flush()
                self.watch.commit(ticket_id)          # only after the durable line: a failed write retries next swap
        self.stats["watch_relooks"] += len(pending)

    def standing_fire(self, e, mint, price, slot, sig, ordinal, tx_index, bt, now):
        """Append one fire request per matching waiting trigger to standing_fires/<day>.jsonl. The JUDGE worker (the
        desk writer) re-checks it in record_fire: this process never writes the desk DB."""
        if self.standing.due(now):
            path = self.base.parent / "memecoin_desk.db"
            try:
                conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
                try:
                    prior = self.standing.loaded_at
                    gap = self.last_gap_at is not None and prior is not None and self.last_gap_at >= prior
                    self.standing.load(conn.execute(U.StandingBook.SQL).fetchall(), now, reset=gap)
                finally:
                    conn.close()
            except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                self.standing.failed(now)
                self.stats["standing_read_errors"] += 1
                log.warning("desk standing-order work list read failed: %s", exc)
        gap = self.last_gap_at is not None and self.standing.loaded_at is not None and self.last_gap_at >= self.standing.loaded_at
        win = [x for x in self.recent[mint] if now - x[0] <= FLOW_S]
        flow = (sum(x[2] for x in win if x[1]) - sum(x[2] for x in win if not x[1])
                if win and all(x[2] is not None for x in win) and now - max(self.started_at, self.last_gap_at or 0) >= FLOW_S
                else None)
        fresh_fx = causal_fx(self.sol_usd, self.sol_at, now, float(self.cfg["sol_max_age_s"]))   # px_limit: causal FX, else none
        fires = self.standing.on_trade(mint=mint, price_sol=price, block_time=bt, received_at=now, gap_since_load=gap,
                                       net_flow_usd=flow, sol_usd=fresh_fx)
        cancels = self.standing.take_cancels()
        if cancels:                                  # standing-v2 band exits: the JUDGE worker's writer re-checks each
            path = self.base / "standing_fires" / f"{utc_day(now)}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as fh:
                for ticket_id, cancel in cancels:
                    fh.write(json.dumps({"ticket_id": ticket_id, "cancel": cancel}, sort_keys=True) + "\n")
                    fh.flush()
                    self.standing.commit(ticket_id)          # only now: a failed write leaves the cancel pending
            self.stats["standing_cancels"] += len(cancels)
        if not fires:
            return
        progress = (max(0.0, min(100.0, (1 - e.real_token_reserves / INITIAL_REAL_TOKEN) * 100))
                    if e.real_token_reserves is not None else None)
        if progress is None or not e.real_quote_reserves or self.sol_usd is None or self.sol_at is None:
            self.stats["standing_fire_unpriced"] += len(fires)
            return                                   # the writer would refuse it anyway: never a guessed fact
        path = self.base / "standing_fires" / f"{utc_day(now)}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            for o in fires:
                fire = {"slot": slot, "signature": sig, "ordinal": ordinal, "tx_index": tx_index if tx_index is not None else -1,
                        "block_time": bt, "received_at": now, "price_sol": price,
                        "real_quote_sol": e.real_quote_reserves / 1e9, "sol_usd": self.sol_usd, "sol_at": self.sol_at,
                        "curve_pct": round(progress, 4)}
                fh.write(json.dumps({"ticket_id": o["ticket_id"], "fire": fire}, sort_keys=True) + "\n")
                fh.flush()
                self.standing.commit(o["ticket_id"])
        self.stats["standing_fires"] += len(fires)

    def refresh_reentry(self, now):
        """Reload the winner arm list from the desk DB (read-only, never the ledger) every refresh_s."""
        if self.reentry.due(now):
            path = self.base.parent / "memecoin_desk.db"
            cutoff = datetime.fromtimestamp(now - self.reentry.window_s, timezone.utc).isoformat()
            try:
                conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
                try:
                    self.reentry.load(conn.execute(U.WinnerReentry.SQL, (cutoff,)).fetchall(), now)
                finally:
                    conn.close()
            except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                self.reentry.failed(now)
                self.stats["reentry_read_errors"] += 1
                log.warning("desk winner re-entry arm read failed: %s", exc)
        return True

    def winner_reentry(self, mint, curve, price, e, bt, slot, sig, ordinal, now):
        """(b) A trade above the winning exit fill price re-emits the mint (store re-verifies; cap and all
        gates unchanged). Only while the mint is inside the band; unknown tape or a stale SOL price never re-arms."""
        fresh = self.sol_at is not None and now - self.sol_at <= float(self.cfg["sol_max_age_s"])
        if self.book.state(mint) != "inside" or not fresh or not self.sol_usd:     # in-band reclaim only
            return
        win = [x for x in self.recent[mint] if now - x[0] <= FLOW_S]
        flow = (sum(x[2] for x in win if x[1]) - sum(x[2] for x in win if not x[1])
                if win and all(x[2] is not None for x in win) and now - max(self.started_at, self.last_gap_at or 0) >= FLOW_S
                else None)                       # unknown (gap, unpriced trade, short history) is never a positive flow
        claim = self.reentry.check(mint, price * self.sol_usd, bt, flow)
        if claim is None:
            return
        em = U.Emission(mint, curve, slot, sig, ordinal, self.book.last_mcap(mint), now,
                        U.ticket_id("solana", mint, slot, sig, ordinal))
        self.stats["reentry_emissions"] += 1
        self.emit(em, e, bt, now, reentry=claim)

    # --- emission ----------------------------------------------------------------------------
    def emit(self, em, e, bt, now, reentry=None):
        # One clock for both stamps: Codex's validate_candidate requires
        # fromisoformat(emitted_at).timestamp() == snapshot.emission_at exactly.
        at = datetime.fromtimestamp(now, timezone.utc)
        now = at.timestamp()
        pool = em.curve or em.mint
        if not self.bars.watch(pool):
            self.evict_oldest()
            self.bars.watch(pool)
        self.watch_order.append(pool)
        snap = U.build_snapshot(emission_at=now, fields=self.fields(em, e, now))
        row = {"schema": "desk-candidate/v1", "ticket_id": em.ticket_id, "producer": "U", "network_id": "solana",
               "mint": em.mint, "pool": pool, "band_entry_slot": em.band_entry_slot,
               "trigger_signature": em.signature, "trigger_ordinal": em.ordinal,
               "emitted_at": at.isoformat(), "gate_state": None,
               "snapshot": snap}
        if reentry is not None:
            row["reentry"] = reentry
        path = self.base / "candidate_emissions" / f"{utc_day(now)}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        self.stats["emissions"] += 1

    def fields(self, em, e, now):
        src = "pumpfun_grpc"
        F = U.Field
        launch_slot = self.launch_seen.get(em.mint)
        # blocks_meta for a slot can arrive AFTER its transactions, so the create's block time is
        # joined at emission, not at the create (Gamma: age_s was missing on all 60 emissions)
        launch_bt = self.block_time.get(launch_slot) if launch_slot is not None else None
        healthy_since = max(self.started_at, self.last_gap_at or 0)
        flow_complete = now - healthy_since >= FLOW_S
        win = [x for x in self.recent[em.mint] if now - x[0] <= FLOW_S]
        usd_known = all(x[2] is not None for x in win)
        flow = {"buy_n": sum(1 for x in win if x[1]), "sell_n": sum(1 for x in win if not x[1]),
                "buy_usd": sum(x[2] or 0 for x in win if x[1]) if usd_known else None,
                "sell_usd": sum(x[2] or 0 for x in win if not x[1]) if usd_known else None,
                "makers": len({x[3] for x in win}),
                "largest_buy": max((x[2] or 0 for x in win if x[1]), default=0) if usd_known else None}
        prices = [x[4] for x in win if x[4] is not None]
        cur = prices[-1] if prices else None
        creator = self.creator_of.get(em.mint)
        progress = None
        if e.real_token_reserves is not None:
            progress = max(0.0, min(100.0, (1 - e.real_token_reserves / INITIAL_REAL_TOKEN) * 100))
        return {
            "mcap_usd": F(round(em.mcap_usd, 2), src + ":curve_reserves", now, em.band_entry_slot, "complete"),
            "curve_pct": F(round(progress, 2) if progress is not None else None, src + ":real_token_reserves",
                           now, em.band_entry_slot, "complete" if progress is not None else "missing"),
            # block_time ahead of the host clock by < 1 s reads as 0, never negative
            "age_s": F(round(max(0.0, now - launch_bt), 1) if launch_bt else None, src + ":create_block_time",
                       now, launch_slot, "complete" if launch_bt else "missing"),
            "creator_prior_launches": F(self.creator_launches[creator] - 1 if creator else None,
                                        src + ":creates_since_worker_start", now, None,
                                        "partial" if creator else "missing"),
            "flow_60s": F(flow, src + ":swaps", now, em.band_entry_slot,
                          "complete" if flow_complete and usd_known else "partial"),
            "price_vs_60s_low_pct": F(round((cur / min(prices) - 1) * 100, 2) if prices else None, src + ":swaps",
                                      now, em.band_entry_slot, "complete" if flow_complete and prices else "partial"),
            "price_vs_60s_high_pct": F(round((cur / max(prices) - 1) * 100, 2) if prices else None, src + ":swaps",
                                       now, em.band_entry_slot, "complete" if flow_complete and prices else "partial"),
            "liq_usd": F(round(e.real_quote_reserves / 1e9 * self.sol_usd, 2)
                         if e.real_quote_reserves is not None and self.sol_usd else None,
                         src + ":real_quote_reserves", now, em.band_entry_slot,
                         "complete" if e.real_quote_reserves is not None and self.sol_usd else "missing"),
            # the crawl cache, newest crawl known at or before emission and <= 6 h old (CRAWLER design (a))
            **C.stamp_fields(self.base / "crawler" / "crawls.db", em.mint, now),
        }

    # --- bars / caps -------------------------------------------------------------------------
    def flush_bars(self):
        if not self.block_time:
            return
        upto = max(self.block_time) - 2                 # leave the last slots open for late swaps
        for pool in self.bars.watched():
            rows = self.bars.closed_bars(pool, upto_slot=upto)
            if rows:
                c = self.files.conn("candles_1s", time.time())
                c.executemany("INSERT OR REPLACE INTO candles_1s VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                              [(pool, b["t"], b["o"], b["h"], b["l"], b["c"], b["buy_usd"], b["sell_usd"],
                                b["n_swaps"], b["n_makers"], b["slot_lo"], b["slot_hi"]) for b in rows])
                self.bars.drop_through(pool, upto)

    def evict_oldest(self):
        while self.watch_order:
            old = self.watch_order.popleft()
            if old in self.bars.watched():
                self.bars.unwatch(old)
                self.stats["watch_evicted"] += 1
                return

    def check_disk(self):
        if self.ps_span_pending:
            self.flush_ps_spans()
        if not self.ps_spans_closed:
            self.close_open_ps_spans(time.time())
        ps_paths = tuple(self.base / name for name in ("pumpswap", "candidate_emissions_ps", "ranker_ps"))
        used = desk_bytes(self.base, [self.base.parent / "memecoin_desk.db"], ps_paths) / 1e9
        free = shutil.disk_usage(self.base.parent).free / 1e9
        self.stats["disk_gb_x100"] = int(used * 100)
        hard_stop = used >= float(self.cfg["hard_gb"]) or free < float(self.cfg["min_free_gb"])
        if hard_stop:
            self.stop = True
        if self.ps_capture:
            _wal, pinners = self.ps_wal_relief(time.time())      # free a WAL we can free BEFORE it counts toward the cap
            ps_used = self.ps_live_bytes() / 1e9        # LIVE data, not the file (a pruned file plateaus, its freelist is reused)
            self.stats["ps_disk_gb_x100"] = int(ps_used * 100)
            self.stats["ps_file_gb_x100"] = int(desk_bytes(ps_paths[0], ps_paths[1:]) / 1e9 * 100)
            ps_stop = ps_used >= float(self.cfg["ps_hard_gb"]) or free < float(self.cfg["min_free_gb"]) + float(self.cfg["ps_hard_gb"])
            self.keep_extend_shed(ps_used, free, time.time())
            if (hard_stop or ps_stop) and self.ps_keep_window_s > 660 and self.ps_keep_widen_end is None:
                from sniper.util.clock import real_utc_now
                self.ps_keep_widen_end = real_utc_now()
                log.warning("desk PS keep widening ended at corrected UTC %.6f (disk cap)", self.ps_keep_widen_end)
            if ps_stop:
                stop_at = time.time()
                self.ps_gap("disk_cap", stop_at)
                self.ps_capture, self.ps_book = False, None
                self.ps_stopped = {"at": stop_at, "cause": "disk_cap"}
                self.stats["ps_capture_stopped"] += 1
                self.ps_ring.clear()
                self.ps_ring_bytes = 0
                try:
                    wal_gb = self.stats.get("ps_wal_gb_x100", 0) / 100.0
                    why = (f"; WAL {wal_gb:.1f} GB not checkpointing, held open by "
                           + ("; ".join("pid %d %s" % p for p in pinners) or "an unknown reader") + " (stop that reader; the worker checkpoints PASSIVE itself, reclaiming the file is an operator action)"
                           if pinners or wal_gb >= float(self.cfg["ps_wal_try_gb"]) else "")
                    self.alert_fn("desk-universe-ps-disk", f"PumpSwap capture stopped: PS {ps_used:.2f} GB, cap {float(self.cfg['ps_hard_gb']):.2f} GB, free {free:.1f} GB{why}; auto-resumes below {float(self.cfg['ps_hard_gb']) - float(self.cfg['ps_resume_hyst_gb']):.2f} GB (or restart the worker)")
                except Exception:
                    self.stats["ps_alert_errors"] += 1
                    log.exception("desk PumpSwap disk-stop alert failed")
                self.commit_files()
                self.write_ps_span(stop_at)
                log.warning("desk PumpSwap capture disabled at its disk cap; auto-resume armed")
        elif self.ps_restore_unread is not None and not self.stop:
            self.restore_ps_stop(time.time())
        elif self.ps_stopped is not None and not self.ps_capture and not self.stop:
            self.maybe_resume_ps(free)
        if hard_stop:
            if self.ps_capture:
                self.ps_gap("disk_cap", time.time())
                self.commit_files()
            self.alert_fn("desk-universe-disk", f"producer U stopped: desk {used:.1f} GB, free {free:.1f} GB")
            log.warning("desk universe hard cap: used_gb=%.2f free_gb=%.2f -> stop", used, free)
        elif used >= float(self.cfg["soft_gb"]):
            self.evict_oldest()
            log.warning("desk universe soft cap: used_gb=%.2f -> evicted oldest watched pool", used)

    def heartbeat(self, connected):
        beat = {"ts": time.time(), "pid": os.getpid(), "connected": connected,
                "sol_usd": self.sol_usd, "sol_age_s": time.time() - self.sol_at if self.sol_at else None,
                "watched": len(self.bars.watched()), "ring_bytes": self.ring_bytes,
                "pumpswap_capture": self.ps_capture, "ps_ring_bytes": self.ps_ring_bytes,
                "ps_keep_window_s": self.ps_keep_window_s, "ps_keep_widen_end": self.ps_keep_widen_end,
                "release_sha": self.release_sha,
                "curve_capture": self.curve_capture, "curve_retired_since": self.curve_retired_since,
                "intake": None if self.intake_book is None else dict(self.intake_book.stats),
                "pumpswap_emissions": self.ps_book is not None,
                "ps_committed_through": self.ps_committed_through, "curve_committed_through": self.curve_committed_through,
                "curve_capture_from": self.curve_capture_from, "ps_capture_from": self.ps_capture_from,
                "ps_loss_protocol": PSL.PROTOCOL, "ps_pool_coverage_protocol": PSL.PROTOCOL,
                "ps_gap_pending": bool(self.ps_pending_gaps), "ps_pending_gaps": dict(self.ps_pending_gaps),
                "wallet_flow": dict(enabled=self.wallet_flow_enabled,storage_initialized=self.wallet_flow_ready,archive=dict(self.wallet_archive_status,preparing=self.wallet_archive_future is not None),last_error=getattr(self,'wallet_flow_last_error',None),last_commit_state='unknown' if getattr(self.files._open.get(('pumpswap','all')),'wallet_flow_callback_errors',0) else self.wallet_flow_last_commit,transaction_callback_errors=getattr(self.files._open.get(('pumpswap','all')),'wallet_flow_callback_errors',0),desired_selectors=len(self.wallet_flow_desired),submitted_selectors=len(self.wallet_flow_sent),submitted_at=self.wallet_flow_submitted_at,submitted_selector_digest=self.wallet_flow_submitted_digest,selector_acknowledged=None,ring_bytes=self.wallet_flow_ring_bytes,ring_cap_bytes=WALLET_FLOW_RING_CAP,metadata_pending=self.stats['wallet_flow_metadata_pending'],metadata_recovery=getattr(self,'wallet_metadata_recovery',None),payload_bytes=self.stats['wallet_flow_payload_bytes'],pending_gaps=dict(self.wallet_flow_gaps),processor_p50_us=_quantile(self.wallet_flow_us,.5),processor_p90_us=_quantile(self.wallet_flow_us,.9)),
                "blocked_samples": self.blocked_samples, "ps_hist": dict(self.ps_hist), **self.stats}
        try:
            beat["ps_state_witness"] = (dict(initialized=True, **self.ps_state_witness.status())
                if self.ps_state_witness is not None else
                dict(enabled=self.ps_state_witness_enabled, initialized=False,
                     reason="init_error" if self.ps_state_witness_init_attempted else "not_initialized"))
        except Exception as exc:
            beat["ps_state_witness"] = dict(status="unknown", reason="status_error")
            self.stats["ps_state_witness_error"] += 1
            cause = "status:" + type(exc).__name__
            if cause not in self.ps_state_witness_errors:
                self.ps_state_witness_errors.add(cause)
                log.exception("state witness status failed; tape continues")
        connection=self.files._open.get(('pumpswap','all'))
        day=utc_day(time.time())
        # RELEASE commits idle witness maintenance, but never an existing capture transaction.
        if connection is not None:connection.execute('SAVEPOINT ps_heartbeat_witness')
        try:
            if connection is not None:
                try:ES.expire(connection,day)
                except Exception:
                    self.stats['ps_extend_shadow:expiry_error']+=1
                    try:ES.note_error(connection,time.time(),'expiry')
                    except Exception:self.stats['ps_extend_shadow:error_receipt_failed']+=1
                try:SFW.expire(connection,day)
                except Exception:self.stats['ps_sell_fee_witness:expiry_error']+=1
            try:
                beat['ps_extend_shadow']=(ES.status(connection,day) if connection is not None
                                          else {'status':'not_initialized','rule':ES.RULE,'audit_errors':None})
            except Exception:
                beat['ps_extend_shadow']={'status':'unknown','reason':'status_error','rule':ES.RULE,'audit_errors':None}
                try:ES.note_error(connection,time.time(),'status')
                except Exception:self.stats['ps_extend_shadow:error_receipt_failed']+=1
        finally:
            if connection is not None:connection.execute('RELEASE SAVEPOINT ps_heartbeat_witness')
        beat['ps_extend_shadow']['session_record_errors']=self.stats['ps_extend_shadow:error']
        beat['ps_extend_shadow']['session_error_receipt_failures']=self.stats['ps_extend_shadow:error_receipt_failed']
        beat['ps_extend_shadow']['denominator_complete']=False
        beat['ps_extend_shadow']['denominator_basis']='retained_rows_only'
        if self.txshape is not None:
            self.txshape.commit(time.time())
            beat["txshape"] = {**self.txshape.stats, "pct": self.txshape.pct, "us_p50": _quantile(self.txshape_us, 0.5),
                               "us_p90": _quantile(self.txshape_us, 0.9)}
        tmp = self.base / "universe.heartbeat.tmp"
        tmp.write_text(json.dumps(beat, sort_keys=True))
        tmp.replace(self.base / "universe.heartbeat")


def ps_filter_wanted(worker):
    """The PS program filter stays on the subscription while capture is on OR armed-stopped (a restored disk-cap stop): a later resume must receive frames."""
    return bool(worker.census or worker.ps_capture or worker.ps_stopped is not None or worker.ps_restore_unread is not None)


WALLET_FLOW_EXECUTABLES = frozenset({
    '11111111111111111111111111111111',
    'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
    'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
    U.PUMP_PROGRAM,PUMPSWAP_PROGRAM,
    'ComputeBudget111111111111111111111111111111',
    'Vote111111111111111111111111111111111111111',
    'AddressLookupTab1e1111111111111111111111111',
})


def wallet_flow_addresses(wallets):
    if not isinstance(wallets,(list,tuple,set,frozenset)):return None
    try:
        if not all(isinstance(w,str) and w==w.strip() and w not in WALLET_FLOW_EXECUTABLES and len(U.b58decode(w))==32 for w in wallets):return None
        return tuple(sorted(set(wallets)))
    except Exception:return None


def build_request(pb, census, wallet_flow=()):
    txs = {"pump": pb.SubscribeRequestFilterTransactions(vote=False, failed=False,
                                                         account_include=[U.PUMP_PROGRAM])}
    if census:
        txs["pumpswap"] = pb.SubscribeRequestFilterTransactions(vote=False, failed=False,
                                                                account_include=[PUMPSWAP_PROGRAM])
    selected=wallet_flow_addresses(wallet_flow)
    if selected:txs['wallet_flow']=pb.SubscribeRequestFilterTransactions(vote=False,account_include=selected)
    return pb.SubscribeRequest(transactions=txs, blocks_meta={"meta": pb.SubscribeRequestFilterBlocksMeta()},
                               commitment=pb.CommitmentLevel.CONFIRMED)


def serialize_request(worker,pb,request):
    raw=pb.SubscribeRequest.SerializeToString(request)
    if worker.wallet_flow_enabled and not request.HasField('ping'):
        from sniper.office.wallet_selector_identity import selector_digest
        selected=tuple(request.transactions['wallet_flow'].account_include) if 'wallet_flow' in request.transactions else ()
        digest=selector_digest(selected)
        worker.wallet_flow_sent=selected
        worker.wallet_flow_submitted_digest=digest
        worker.wallet_flow_submitted_at=time.time()
        worker.stats['wallet_flow_submitted_updates']+=1
    return raw


async def request_updates(worker,pb,has_ps):
    selected=worker.wallet_flow_selectors() if worker.wallet_flow_enabled else ()
    next_ping=time.monotonic()+10;n=0
    yield build_request(pb,has_ps,wallet_flow=selected)
    while not worker.stop:
        await asyncio.sleep(.25)
        changed=worker.wallet_flow_selectors() if worker.wallet_flow_enabled else ()
        if changed!=selected:
            selected=changed;yield build_request(pb,has_ps,wallet_flow=selected)
        now=time.monotonic()
        if now>=next_ping:
            n+=1;next_ping=now+10
            yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=n))


async def run(worker, host, token):
    import grpc
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    has_ps = ps_filter_wanted(worker)
    if worker.wallet_flow_enabled:worker.wallet_flow_selectors()

    backoff = 1.0
    while not worker.stop:
        try:
            async with grpc.aio.secure_channel(host + ":443", grpc.ssl_channel_credentials(), options=[
                    ("grpc.max_receive_message_length", 16 * 1024 * 1024),
                    ("grpc.keepalive_time_ms", 10000)]) as ch:
                call = ch.stream_stream("/geyser.Geyser/Subscribe", request_serializer=lambda request:serialize_request(worker,pb,request),
                                        response_deserializer=lambda b: b)
                worker.heartbeat(True)
                async for raw in call(request_updates(worker,pb,has_ps), metadata=(("x-token", token),)):
                    received_at = time.time()
                    worker.queue_frame(raw,received_at=received_at,has_ps=has_ps)
                    backoff = 1.0
                    if worker.stop:
                        break
        except Exception as exc:                                    # every failure is a gap, logged
            worker.stats["reconnects"] += 1
            if worker.wallet_flow_enabled:worker.wallet_flow_gap("stream_reconnect",time.time())
            worker.gap("stream:" + type(exc).__name__)
            log.warning("desk universe stream error=%r backoff=%.0fs", exc, backoff)
            worker.heartbeat(False)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def processor(worker):
    last_beat = last_disk = last_sol = last_ext = 0.0
    worker.close_open_ps_spans(time.time())        # a start ends every span a previous run left open
    while not worker.stop:
        item=worker.next_frame()
        if item:
            raw,received_at,stream=item
            try:
                if stream=='wallet_flow':worker.wallet_flow_process(raw,received_at=received_at)
                else:worker.process(raw,received_at=received_at)
            except Exception:
                if stream=='wallet_flow':
                    worker.stats['wallet_flow_errors']+=1;worker.wallet_flow_gap('process_error',received_at)
                else:
                    worker.stats["process_errors"] += 1
                    if stream=='pumpswap':worker.ps_gap("process_error",received_at)
                    else:worker.gap("process_error")
                log.exception("desk universe frame failed")
        else:
            await asyncio.sleep(0.005)
        now = time.time()
        worker.flush_ps_gaps(now)
        if now - last_sol >= 60:
            last_sol = now
            price = await asyncio.to_thread(worker.sol_price_fn)
            if price:
                worker.sol_prev = (worker.sol_usd, worker.sol_at)
                worker.sol_usd, worker.sol_at = price, time.time()
            else:
                worker.stats["sol_price_failed"] += 1
        if worker.intake_book is not None and worker.intake_book.due(now):
            worker.intake_book.loaded_at = now                     # one reload in flight at a time
            try:
                read = await asyncio.to_thread(worker.intake_book.read_listed, now)   # off the write path
            except Exception:
                worker.stats["intake_errors"] += 1
                log.exception("desk intake read failed")
            else:
                worker.intake_apply(read, now)
        if now - last_ext >= 30:
            last_ext = now
            worker.keep_extend_tick(now)
            worker.watch_tick(now)
        if now - last_beat >= 1:
            last_beat = now
            if worker.wallet_flow_enabled:
                worker.wallet_flow_archive_poll()
                if not worker.ring and not worker.ps_ring:worker.wallet_flow_maintenance(now)
            worker.commit_files()
            worker.heartbeat(True)
        if now - last_disk >= 60:
            last_disk = now
            worker.check_disk()


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallet-flow',action='store_true',help='Default-off watched-wallet research capture on the existing connection')
    args=parser.parse_args(argv)
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    cfg = knobs()
    cfg["wallet_flow"]="1" if args.wallet_flow else "0"
    if cfg["enabled"] != "1":
        print("[desk-universe] MEMECOIN_DESK_UNIVERSE_ENABLED is not 1; exiting")
        return 0
    token = os.environ.get("MEMECOIN_SHYFT_GRPC_TOKEN")
    if not token:
        log.warning("desk universe: no MEMECOIN_SHYFT_GRPC_TOKEN; exiting")
        return 2
    from sniper.services.memecoin_slot_lane import sol_usd_from_jupiter
    from sniper.services.memecoin_alerts import send_alert
    base = Path(state_root()) / "data" / "desk"
    base.mkdir(parents=True, exist_ok=True)
    # one cohort = one band: the active U cohort's frozen band wins over the env band (fallback when no cohort is set)
    got = U.cohort_band(base.parent / "memecoin_desk.db", os.environ.get("MEMECOIN_DESK_U_COHORT_ID"))
    cfg = {**cfg, "band_lo": got["band"][0], "band_hi": got["band"][1]}
    if got["source"] in ("cohort_unreadable", "cohort_missing"):
        log.warning("desk universe: configured cohort %s band %s (%s); using the env band %s for this process",
                    os.environ.get("MEMECOIN_DESK_U_COHORT_ID"), got["source"], got.get("error"), got["band"])
        alert = f"desk universe: cohort band {got['source']}; env band {got['band']} in use until restart"
        try:
            from sniper.services.memecoin_alerts import send_alert as _page
            _page("desk_universe_band", alert)
        except Exception:
            log.exception("desk universe band page failed")
    if got["mismatch"]:
        log.warning("desk universe: env band differs from cohort %s band %s; using the cohort's",
                    os.environ.get("MEMECOIN_DESK_U_COHORT_ID"), got["band"])
    worker = Worker(cfg, base=base, sol_price_fn=sol_usd_from_jupiter,
                    alert_fn=lambda kind, text: send_alert(kind, text))
    # a (re)start is a hole in the tape (the stop is not caught, so nothing else names it): one gap
    # row, so readers censor across it instead of reading "no gaps"
    worker.gap("worker_start")
    worker.commit_files()

    async def both():
        await asyncio.gather(run(worker, cfg["url"], token), processor(worker))
    asyncio.run(both())
    worker.commit_files()
    return 1 if worker.stop else 0


if __name__ == "__main__":
    sys.exit(main())
