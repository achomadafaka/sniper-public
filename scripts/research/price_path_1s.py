#!/usr/bin/env python3
"""1-second price paths for Solana coins, rebuilt from their pools' own on-chain swaps, and a replay
of the CURRENT funded exit policy over each path. Research only: nothing here is on a fill path.

Order: Alpha task 85515c15 (operator 2026-10-02 ~13:4xZ: refusal grades use 1-second price paths,
not 60 s or 25 s quote readings). Edge list approved by Alpha with changes C1-C3 (brain episode
39846353). Gamma supplies the cohort; this tool never selects one.

INPUT, one JSON object per line (Gamma):
    row_id            unique id of the row (the attempt_id is fine)                       required
    cohort            free tag carried into the output                                     required
    mint              the coin                                                             required
    pool              pool address, PumpSwap pool PDA, curve PDA or the pool's coin vault  optional
    venue             pump_curve | pump_amm | raydium_cpmm | raydium_launchlab | ...       optional
    fill_slot         slot of the (modelled) fill                      one of fill_slot / fill_at
    fill_at           ISO time of the fill (resolved to a slot by getBlockTime bisection)
    fill_price_quote  fill price in the pool's quote units per whole coin  one of the two
    fill_price_usd    fill price in USD per whole coin (converted with the row's SOL/USD)
    clip_usd          the clip in USD (sizes the executable sell quote)                    required
    source_user_id    the copied wallet (selects the funded policy and the mirror)         required
    source_address    the wallet's Solana address(es), comma list; else the address registry
    band              nano | micro | small | mid | out_of_scope; null runs every band       optional
    decision_at       ISO decision time (Degen V4 cohort test, SOL/USD lookup)            required
    attempt_id        ledger attempt (its trace's SOL/USD)                                 optional
    sol_usd           USD per SOL to use for this row                                      optional
    window_s          path length after the fill, default 3900 (65 min)                    optional

PRICE. Per swap, the pool's spot after the swap where the venue's reserves are exact: the pump curve
TradeEvent's virtual reserves, PumpSwap vaults plus the Pool account's virtual quote, the CPMM
SwapEvent's curve reserves, the LaunchLab TradeEvent's virtual curve. Every other venue (DAMM v2,
DLMM, DBC, Whirlpool, CLMM, unknown programs) is priced at each swap's PAID price, |quote|/|coin|,
flagged basis=trade_avg: it carries the fee and the swap's own impact. A reserve basis that disagrees
with the paid prices it produced (bracket check below) is downgraded to trade_avg for that pool.
The 1 s price is the state after the LAST swap of the second; an empty second carries it, stale=1.

QUOTE TERMS ARE PRIMARY (Alpha C1). Returns need no USD. price_usd on a SOL pool uses ONE SOL/USD per
row from our own ledger, held constant (sol_usd_const): SOL drift over the window is not modelled.

REPLAY. The policy comes from the funded code (memecoin_risk.funded_policy_for_origin plus the Degen
V4 overlay), with the RUNNING runner's config (process env, then .env first-wins, then the cockpit
overlay; Alpha C2) and the code of the PINNED release. Rails are decided by the runner's own pure
`alpha_b_real_exit_reason` and confirmed by its own `executable_exit_allowed` on a synthetic position
carrying a canonical contract; the volume-death decision is the runner's own `armed_exit_decision`
over an in-memory marks table built from the path. The sweep's wrappers (TP window, no-loss window,
instant stop, stop window or debounce) are re-done here from the same cfg values and the runner's
pure helpers. Named models: the mirror (source sells read from the chain), the exit lag.

READS. The research Helius key only (refused when it is also a funded key), the LOWEST-priority client
of its shared pacer (Alpha C3): at most one call per MIN_GAP_S, never ahead of a reservation another
reader already holds, and a 429 waits out the pause plus 60 s. Every call first checks that the Solana
funded book is flat; a book that opens stops the run, keeps what was read, and the next run resumes.
At most --page-cap pages per row; past it the path is censored, never extrapolated.

    python3 scripts/research/price_path_1s.py --input cohort.jsonl --out data/price_path_1s/ \
        [--until-exit] [--page-cap 600] [--cadence 1] [--exit-lag-s 1.0]
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import copy
import fcntl
import json
import math
import re
import os
import sqlite3
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAPE_DIR = HERE                     # main() points this at the pinned release's copy

PRE_SLOTS = 150                     # the path starts ~60 s before the fill (source buy, anchor)
CHUNK_SLOTS = 150                   # read forward in ~60 s chunks so --until-exit can stop early
PAGE_CAP = 600                      # per row, ~11 min of the shared key at its own gap
BATCH_PAGES = 20                    # pages per read call before the rows are stored
WINDOW_S = 3900                     # 65 min after the fill (operator yes, 2026-10-01)
SLOT_S_MIN = 0.35                   # a slot is never assumed faster: the read covers the window
MIN_GAP_S = 2.0                     # this backfill's own floor between calls (Alpha C3)
PAUSE_EXTRA_S = 60.0                # after a 429: the pacer's pause plus this (Alpha C3)
# 2 (2026-10-07, S1): an LP change censors the replay ("lp_unknown_depth") instead of pricing on
# the mid. Rows of version 1 and 2 must not be mixed in one comparison.
REPLAY_VERSION = 2
EXIT_LAG_S = 1.0                    # funded Solana exits, executor entry to close: p50 1.03 s,
                                    # p90 1.83 s, n=356 since 09-18 (excludes the confirming quote)
BRACKET_FEE = 0.03                  # a paid price may sit up to 3% beyond the spot range (fees)
BRACKET_MAX_VIOLATIONS = 0.2        # more than this share outside the range: the reserves are wrong
SOL_USD_MAX_AGE_S = 6 * 3600
SLOTS_PER_EPOCH = 432_000
FILL_AT_TOL_S = 2.0                 # a fill_at further than this from its nearest block is censored
ANCHOR_MAX_BLOCK_READS = 23         # getBlockTime reads one fill_at search may spend (plus getSlot)
BANDS = ("nano", "micro", "small", "mid", "out_of_scope")
RESERVE_BASES = {"curve_event", "pump_amm_vaults_v", "cpmm_event", "launchlab_event"}
CL_BASES = {"damm_v2_event", "dlmm_event"}       # exact post-swap price from the venue's own event
EXACT = RESERVE_BASES | CL_BASES
EVENT_CPI = bytes.fromhex("e445a52e51cb9a1d")    # anchor emit_cpi! prefix on a self-invoke
DAMM_V2 = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
LOSS_REASONS = {"b_stop", "catastrophe"}


class ReadStop(Exception):
    """The run stops here: the book opened, the page cap, a host error or a limit we could not wait out."""


class RowCensor(Exception):
    """This row alone is censored (its fill cannot be anchored); the run goes on to the next row."""


def _tape():
    """The signal-window tape module (reader, transaction parser, DDL), from TAPE_DIR."""
    import importlib.util
    mod = sys.modules.get("sol_signal_window_tape")
    if mod is None:
        spec = importlib.util.spec_from_file_location("sol_signal_window_tape",
                                                      TAPE_DIR / "sol_signal_window_tape.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["sol_signal_window_tape"] = mod
        spec.loader.exec_module(mod)
    return mod


def _iso(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def _epoch(iso):
    t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("naive time %r" % iso)
    return t.timestamp()


# --- the input ---------------------------------------------------------------------------------------

def parse_row(row):
    """(row, None) or (None, why). Never guesses a missing field."""
    if not isinstance(row, dict):
        return None, "not an object"
    for k in ("row_id", "cohort", "mint", "clip_usd", "source_user_id", "decision_at"):
        if row.get(k) in (None, ""):
            return None, "missing %s" % k
    if row.get("fill_slot") is None and not row.get("fill_at"):
        return None, "missing fill_slot and fill_at"
    if row.get("fill_price_quote") is None and row.get("fill_price_usd") is None:
        return None, "missing fill_price_quote and fill_price_usd"
    band = row.get("band")
    if band is not None and band not in BANDS:
        return None, "unknown band %r" % band
    try:
        _epoch(row["decision_at"])
        if row.get("fill_at"):
            _epoch(row["fill_at"])
        # R9 (Codex review 2026-10-06): float() takes True, inf and a negative slot. Numbers here
        # are finite and positive, never booleans; a slot is a non-negative integer.
        for k in ("clip_usd", "fill_price_quote", "fill_price_usd", "sol_usd"):
            v = row.get(k)
            if v is not None and (isinstance(v, bool) or not math.isfinite(float(v)) or not float(v) > 0):
                return None, "%s must be a finite positive number" % k
        slot = row.get("fill_slot")
        # N10 (Codex night review 2026-10-06): str.isdigit takes '²' and Arabic-Indic digits; a
        # slot is ASCII digits or an int, inside u64.
        if slot is not None and (
                isinstance(slot, bool)
                or not (isinstance(slot, int) or (isinstance(slot, str) and re.fullmatch(r"[0-9]+", slot)))
                or not 0 <= int(slot) < 2 ** 64):
            return None, "fill_slot must be a non-negative integer"
    except (TypeError, ValueError, OverflowError) as exc:      # float(10**1000) overflows
        return None, "bad value: %s" % exc
    return dict(row), None


# --- the research key, lowest priority ---------------------------------------------------------------

class LowPriorityRpc:
    """`call(method, params)` on the research key for this backfill. Before every call: the book
    must be flat, our own gap must have passed, and no other reader may hold a reservation ahead of
    us (the shared pacer's state file). A 429 is waited out (pause + PAUSE_EXTRA_S) up to 3 times."""

    def __init__(self, rpc, *, flat_fn, pacer_state_fn, min_gap_s=MIN_GAP_S, clock=time.time,
                 sleep=time.sleep):
        self.rpc, self.flat_fn, self.state_fn = rpc, flat_fn, pacer_state_fn
        self.min_gap_s, self.clock, self.sleep = min_gap_s, clock, sleep
        self.last, self.calls = 0.0, {}

    def _yield(self):
        while True:
            wait = self.last + self.min_gap_s - self.clock()
            state = self.state_fn() or {}
            ahead = max(float(state.get("last") or 0.0), float(state.get("paused_until") or 0.0))
            wait = max(wait, ahead - self.clock())
            if wait <= 0:
                return
            self.sleep(min(wait, 5.0))

    def __call__(self, method, params):
        import urllib.error
        for _attempt in range(4):
            self._yield()
            # After every wait, at the send (R6, Codex review 2026-10-06): a book that opened while
            # we waited stops the read. The shared pacer's own wait is covered by FlatAtSend.
            if not self.flat_fn():
                raise ReadStop("solana book open")
            self.last = self.clock()
            self.calls[method] = self.calls.get(method, 0) + 1
            try:
                return self.rpc(method, params)
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise ReadStop("http_%s on %s" % (exc.code, method)) from None
                paused = float((self.state_fn() or {}).get("paused_until") or 0.0)
                self.sleep(max(0.0, paused - self.clock()) + PAUSE_EXTRA_S)
        raise ReadStop("429 four times on %s" % method)


class FlatAtSend:
    """The shared pacer, with the book checked once more after its own wait: the research transport
    waits inside acquire(), after LowPriorityRpc's check, so a book that opened there must stop the
    read before the HTTP send (R6). Everything else is the pacer's."""

    def __init__(self, pacer, flat_fn):
        self.pacer, self.flat_fn = pacer, flat_fn

    def acquire(self):
        self.pacer.acquire()
        if not self.flat_fn():
            raise ReadStop("solana book open")

    def __getattr__(self, name):
        return getattr(self.pacer, name)


def pacer_state_reader(path):
    """The shared pacer's {last, paused_until}, read without its lock (it is replaced atomically)."""
    def read():
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}
    return read


def flat_reader(ledger):
    """The research-read rule (the window tape's own check); an error reads as NOT flat."""
    def flat():
        return not _tape().solana_book_open(ledger)
    return flat


# --- the store ---------------------------------------------------------------------------------------

EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS swap_state(signature TEXT NOT NULL, coin_vault TEXT NOT NULL, basis TEXT,
  coin_res TEXT, quote_res TEXT, fee_bps REAL, extra TEXT, PRIMARY KEY(signature, coin_vault));
CREATE TABLE IF NOT EXISTS lp_events(signature TEXT NOT NULL, mint TEXT NOT NULL, coin_vault TEXT NOT NULL,
  quote_vault TEXT, coin_res TEXT, quote_res TEXT, slot INTEGER, tx_index INTEGER, block_time INTEGER,
  UNIQUE(signature, coin_vault));
CREATE TABLE IF NOT EXISTS undecoded(signature TEXT NOT NULL, mint TEXT NOT NULL, vault TEXT NOT NULL,
  programs TEXT, slot INTEGER, block_time INTEGER, UNIQUE(signature, vault));
CREATE TABLE IF NOT EXISTS pool_map(mint TEXT NOT NULL, pool TEXT NOT NULL, coin_vault TEXT NOT NULL,
  PRIMARY KEY(mint, pool));
CREATE TABLE IF NOT EXISTS facts(k TEXT PRIMARY KEY, v TEXT, read_at TEXT);
"""


def connect(path):
    db = sqlite3.connect(str(path), timeout=30)
    db.executescript(_tape().DDL + EXTRA_DDL)
    return db


def _fee_bps(part, whole):
    return 1e4 * part / whole if whole else None


def venue_states(tx, mint, swaps):
    """{coin_vault: (basis, coin_res, quote_res, fee_bps)} for the swaps in `tx` whose venue event
    gives the pool's exact curve reserves. A swap without one keeps the parser's own row.

    An event binds to a swap row only when its pool and the row's coin vault sit in ONE instruction
    of the venue's own program, and exactly one event does (R3, Codex review 2026-10-06): an equal
    coin amount through another pool in the same transaction took the first event's reserves, 10x
    off on the second pool. The shared CPMM or LaunchLab authority owns every pool's vaults, so the
    vault owner cannot bind; the instruction's own accounts can."""
    from sniper.execution import pumpswap_buy, raydium_cpmm_buy, raydium_launchlab_buy
    logs = (tx.get("meta") or {}).get("logMessages") or []
    t = _tape()
    try:
        ixs = [(program, set(accounts)) for program, accounts, _d, _i in t._instructions(tx, t._keys(tx))]
    except (KeyError, TypeError, AttributeError):
        ixs = []

    def bound(program, vault, pool):
        return any(p == program and vault in a and pool in a for p, a in ixs)

    out = {}
    for s in swaps:
        if s["venue"] == "pump_curve":
            out[s["coin_vault"]] = ("curve_event", s["coin_reserve_after"], s["quote_reserve_after"],
                                    s["fee_bps"])
    cpmm = _safe(raydium_cpmm_buy.swap_events, logs)
    ll = _safe(raydium_launchlab_buy.trade_events, logs)
    for s in swaps:
        amount = abs(int(s["coin_delta"]))
        if s["venue"] == "raydium_cpmm":
            hits = [e for e in cpmm if mint in (e.output_mint, e.input_mint)
                    and (e.output_amount if e.output_mint == mint else e.input_amount) == amount
                    and bound(raydium_cpmm_buy.CPMM_PROGRAM, s["coin_vault"], e.pool)]
            for e in hits if len(hits) == 1 else ():
                coin_out = e.output_mint == mint
                zero = type("Rates", (), {"protocol_fee_rate": 0, "fund_fee_rate": 0})
                r_in, r_out = raydium_cpmm_buy.post_trade_reserves(e, zero)
                coin_res, quote_res = (r_out, r_in) if coin_out else (r_in, r_out)
                out[s["coin_vault"]] = ("cpmm_event", coin_res, quote_res,
                                        _fee_bps(e.trade_fee + e.creator_fee, e.input_amount))
                break
        elif s["venue"] == "raydium_launchlab":
            hits = [e for e in ll if (e.amount_out if e.is_buy else e.amount_in) == amount
                    and bound(raydium_launchlab_buy.LAUNCHLAB_PROGRAM, s["coin_vault"], e.pool)]
            for e in hits if len(hits) == 1 else ():
                fees = e.protocol_fee + e.platform_fee + e.creator_fee + e.share_fee
                out[s["coin_vault"]] = ("launchlab_event", e.virtual_base - e.real_base_after,
                                        e.virtual_quote + e.real_quote_after,
                                        _fee_bps(fees, e.amount_in if e.is_buy else e.amount_out))
                break
    cl = cl_events(tx)
    for s in swaps:
        amount, coin_out = abs(int(s["coin_delta"])), int(s["coin_delta"]) < 0
        if s["venue"] == "meteora_damm_v2":
            hits = [e for e in cl["damm_v2"]
                    if (e["output_amount"] if coin_out else e["included_fee_input_amount"]) == amount
                    and bound(DAMM_V2, s["coin_vault"], e["pool"])]
            for e in hits if len(hits) == 1 else ():
                # trade_direction 0 is A->B, 1 is B->A: the coin is A when it is the side that left
                # (B->A) or the side that came in (A->B).
                coin_is_a = (e["trade_direction"] == 1) == coin_out
                out[s["coin_vault"]] = ("damm_v2_event", None, None,
                                        _fee_bps(e["fees"], e["included_fee_input_amount"]),
                                        {"sqrt_price_x64": str(e["next_sqrt_price"]), "coin_is_a": coin_is_a,
                                         "pool": e["pool"]})
                break
        elif s["venue"] == "meteora_dlmm":
            hits = [e for e in cl["dlmm"] if (e["amount_out"] if coin_out else e["amount_in"]) == amount
                    and bound(DLMM, s["coin_vault"], e["lb_pair"])]
            for e in hits if len(hits) == 1 else ():
                # swap_for_y: X goes in. The coin is X when it went in with swap_for_y, or came out
                # without it.
                coin_is_x = e["swap_for_y"] != coin_out
                out[s["coin_vault"]] = ("dlmm_event", None, None, _fee_bps(e["fee"], e["amount_in"]),
                                        {"lb_pair": e["lb_pair"], "start_bin_id": e["start_bin_id"],
                                         "end_bin_id": e["end_bin_id"], "coin_is_x": coin_is_x})
                break
    owners = {s["coin_vault"]: s["pool_owner"] for s in swaps if s["venue"] == "pump_amm"}
    if owners:
        for line in logs:
            if not line.startswith("Program data: "):
                continue
            try:
                ev = pumpswap_buy.parse_buy_event(line)
            except (ValueError, IndexError, KeyError):
                continue
            for vault, owner in owners.items():
                if ev.get("pool") == owner:
                    out[vault] = ("pump_amm_vaults_v", None, None, float(pumpswap_buy.total_fee_bps(ev)))
    return out


def cl_events(tx):
    """{'damm_v2': [...], 'dlmm': [...]}: the swap events Meteora DAMM v2 (EvtSwap2) and DLMM (Swap)
    emit by self-CPI into innerInstructions. Layouts checked on a real routed buy (fixture
    damm_v2_swap2_routed.json): every amount equals its vault delta, the timestamp equals blockTime,
    and the paid price lies between the event's pre and post prices. DAMM v2's older EvtSwap is not
    decoded (no recorded sample): such a swap stays trade_avg."""
    import base58
    import hashlib
    from solders.pubkey import Pubkey
    t = _tape()
    keys = t._keys(tx)
    want = {DAMM_V2: hashlib.sha256(b"event:EvtSwap2").digest()[:8],
            DLMM: hashlib.sha256(b"event:Swap").digest()[:8]}
    out = {"damm_v2": [], "dlmm": []}
    for program, _accounts, data, inner in t._instructions(tx, keys):
        if not inner or program not in want or not data:
            continue
        try:
            raw = base58.b58decode(data)
        except ValueError:
            continue
        if raw[:8] != EVENT_CPI or raw[8:16] != want[program]:
            continue
        b = raw[16:]
        try:
            if program == DAMM_V2 and len(b) == 180:
                inc, exc, left, outa = struct.unpack_from("<QQQQ", b, 52)
                out["damm_v2"].append({
                    "pool": str(Pubkey.from_bytes(b[:32])), "trade_direction": b[32],
                    "included_fee_input_amount": inc, "excluded_fee_input_amount": exc,
                    "output_amount": outa, "next_sqrt_price": int.from_bytes(b[84:100], "little"),
                    "fees": sum(struct.unpack_from("<QQQQ", b, 100))})
            elif program == DLMM and len(b) == 129:
                sb, eb = struct.unpack_from("<ii", b, 64)
                ain, aout = struct.unpack_from("<QQ", b, 72)
                out["dlmm"].append({"lb_pair": str(Pubkey.from_bytes(b[:32])), "start_bin_id": sb,
                                    "end_bin_id": eb, "amount_in": ain, "amount_out": aout,
                                    "swap_for_y": bool(b[88]), "fee": struct.unpack_from("<Q", b, 89)[0]})
        except (ValueError, struct.error):
            continue
    return out


def dlmm_bin_step(raw):
    """The LbPair account's bin_step, or None. bin_step_seed (u16 at 73) is the same number written
    at creation, so the two must agree: a wrong layout reads two unrelated words and is refused."""
    if raw is None or len(raw) < 82:
        return None
    seed, step = struct.unpack_from("<H", raw, 73)[0], struct.unpack_from("<H", raw, 80)[0]
    return step if step and seed == step else None


def _safe(fn, logs):
    try:
        return fn(logs)
    except (ValueError, IndexError, KeyError):
        return []


def side_rows(tx, mint, swaps, pool=None):
    """(lp_rows, undecoded_rows, pool_vault) for one transaction. A coin vault (off-curve owner)
    whose balance moved without a swap row is a liquidity event when the same owner's quote vault
    moved the SAME way in that instruction, else undecoded (named with its programs). `pool_vault`
    is the coin vault that sits in one instruction with `pool`, when `pool` is given."""
    t = _tape()
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return [], [], None
    keys = t._keys(tx)
    moved = t._moved(tx, keys)
    priced = {s["coin_vault"] for s in swaps}
    if any(s["venue"] == "pump_curve" for s in swaps):
        priced |= {a for a, r in moved.items() if r[1] == mint and not t.on_curve(r[0])
                   and any(s["pool_owner"] == r[0] for s in swaps)}
    base = {"signature": tx["transaction"]["signatures"][0], "slot": tx.get("slot"),
            "tx_index": tx.get("transactionIndex"), "block_time": tx.get("blockTime")}
    lp, undecoded, pool_vault, programs = [], [], None, {}
    for program, accounts, _data, _inner in t._instructions(tx, keys):
        for a in accounts:
            programs.setdefault(a, set()).add(program)
        if pool and pool in accounts:
            hit = [a for a in accounts if a in moved and moved[a][1] == mint and not t.on_curve(moved[a][0])]
            pool_vault = pool_vault or (hit[0] if len(hit) == 1 else None)
    for vault, (owner, m_, pre, post, _dec) in moved.items():
        if m_ != mint or t.on_curve(owner) or vault in priced:
            continue
        same = [a for a, r in moved.items() if r[0] == owner and r[1] != mint
                and (r[3] - r[2]) * (post - pre) > 0]
        if len(same) == 1:
            q = moved[same[0]]
            lp.append(dict(base, mint=mint, coin_vault=vault, quote_vault=same[0], coin_res=str(post),
                           quote_res=str(q[3])))
        else:
            undecoded.append(dict(base, mint=mint, vault=vault,
                                  programs=",".join(sorted(programs.get(vault, ())))))
    return lp, undecoded, pool_vault


def store(db, row_id, mint, read, pool=None):
    """Write one read: the tape's own rows (ticks, pool_swaps, pages, coverage) plus the exact venue
    reserves, liquidity events, undecoded transactions and the pool's coin vault."""
    t = _tape()
    for tx in read.txs:
        _ticks, swaps = t.parse_tx(tx, mint)
        sig = (tx.get("transaction") or {}).get("signatures", [None])[0]
        if sig is None:
            continue
        for vault, (basis, c, q, fee, *extra) in venue_states(tx, mint, swaps).items():
            db.execute("INSERT OR REPLACE INTO swap_state VALUES (?,?,?,?,?,?,?)",
                       (sig, vault, basis, None if c is None else str(c), None if q is None else str(q), fee,
                        json.dumps(extra[0]) if extra else None))
        lp, und, pool_vault = side_rows(tx, mint, swaps, pool)
        for r in lp:
            db.execute("INSERT OR IGNORE INTO lp_events VALUES (?,?,?,?,?,?,?,?,?)",
                       (r["signature"], mint, r["coin_vault"], r["quote_vault"], r["coin_res"],
                        r["quote_res"], r["slot"], r["tx_index"], r["block_time"]))
        for r in und:
            db.execute("INSERT OR IGNORE INTO undecoded VALUES (?,?,?,?,?,?)",
                       (r["signature"], mint, r["vault"], r["programs"], r["slot"], r["block_time"]))
        if pool and pool_vault:
            db.execute("INSERT OR IGNORE INTO pool_map VALUES (?,?,?)", (mint, pool, pool_vault))
    t._store(db, row_id, mint, "path", read)          # commits


def fact(db, key, fetch):
    """A cached one-time read (a pool account, a mint): fetched once, then from the store."""
    row = db.execute("SELECT v FROM facts WHERE k=?", (key,)).fetchone()
    if row:
        return json.loads(row[0])
    value = fetch()
    db.execute("INSERT OR REPLACE INTO facts VALUES (?,?,?)",
               (key, json.dumps(value), datetime.now(timezone.utc).isoformat()))
    db.commit()
    return value


# --- reading a row's window --------------------------------------------------------------------------

def slot_at(call, t_epoch, *, max_reads=ANCHOR_MAX_BLOCK_READS):
    """(slot, blockTime) of a block whose blockTime is within 1 s before t_epoch (blockTime has 1 s
    resolution, so the search stops inside that second: up to ~2-5 slots before the very latest
    such block; harmless for 1 s paths). An exact search was measured and refused: past a run of
    skipped slots it spent the 23-read budget on 119-169 of 300 simulated rows (Gamma on 7b1fb10a2).

    A search inside a bracket [lo, hi] (blockTime(lo) <= t < blockTime(hi)), each probe interpolated
    on the bracket's own measured times with every third probe a bisection, so it ends in O(log n)
    whatever the slot time is. The old fixed 0.4 s/slot steps never converged when the real slot
    time differed and returned anchors up to 393 s late (Gamma, 2026-10-06). RowCensor when the
    nearest block at or before t_epoch is more than FILL_AT_TOL_S away (a halt, a gap), the read
    budget runs out, or a probe lands in more than 8 skipped slots: that is this row's unknown, so
    the row is censored and the run goes on (Gamma on 7b1fb10a2). Only the finalized head's read
    stops the run. A skipped slot is stepped over."""
    left = [int(max_reads)]

    def block(slot, head=False):
        for probe in range(int(slot), int(slot) - 8, -1):
            if left[0] <= 0:
                raise RowCensor("fill_at_unresolved: %d block reads spent" % max_reads)
            left[0] -= 1
            try:
                bt = call("getBlockTime", [probe])
            except RuntimeError:
                continue                         # skipped slot
            if bt is not None:
                return probe, int(bt)
        if head:
            raise ReadStop("no blockTime near slot %d" % slot)
        raise RowCensor("fill_at_unresolved: no block in the 8 slots at or below %d" % slot)

    head = int(call("getSlot", [{"commitment": "finalized"}]))
    hi, hi_t = block(head, head=True)
    if hi_t <= t_epoch:
        if t_epoch - hi_t > FILL_AT_TOL_S:
            raise RowCensor("fill_at_unresolved: %.0f s after the finalized head" % (t_epoch - hi_t))
        return hi, hi_t
    span = int((hi_t - t_epoch) / SLOT_S_MIN) + 64      # the fastest plausible slot overshoots back
    while True:
        lo_b, lo_t = block(max(0, head - span))
        if lo_t <= t_epoch:
            break
        hi, hi_t, span = lo_b, lo_t, span * 2
    lo, probes = lo_b, 0
    while hi - lo > 1 and t_epoch - lo_t >= 1.0:
        probes += 1
        if probes % 3 == 0:
            mid = (lo + hi) // 2
        else:
            frac = (t_epoch - lo_t) / max(1.0, hi_t - lo_t)
            mid = min(hi - 1, max(lo + 1, lo + int(frac * (hi - lo))))
        got, got_t = block(mid)
        if got <= lo:                            # every slot in (lo, mid] was skipped
            lo = mid
        elif got_t <= t_epoch:
            lo, lo_b, lo_t = mid, got, got_t     # (got, mid] skipped: the bound moves to mid
        else:
            hi, hi_t = got, got_t
    if t_epoch - lo_t > FILL_AT_TOL_S:
        raise RowCensor("fill_at_unresolved: nearest block %.0f s before it" % (t_epoch - lo_t))
    return lo_b, lo_t


def fill_slot_from_trace(ledger, attempt_id):
    """The decision's own quote slot (its `buy_context_slot`, the latest in the trace), or None.
    A fill modelled at the decision is anchored exactly here, with no getBlockTime search."""
    try:
        row = ledger.execute("SELECT trace_json FROM memecoin_entry_attempts WHERE attempt_id=?",
                             (attempt_id,)).fetchone()
        events = (json.loads(row[0] or "{}").get("substage_events") or []) if row else []
        slots = [int(e["buy_context_slot"]) for e in events
                 if isinstance(e, dict) and e.get("buy_context_slot") is not None]
    except (sqlite3.Error, TypeError, ValueError):
        return None
    return max(slots) if slots else None


def read_window(db, call, row, *, lo, hi, page_cap, pool=None, done_fn=None):
    """Read [lo, hi] of the row's mint, dense, in CHUNK_SLOTS chunks, skipping what the store already
    covers. Returns (last fully covered slot, pages used, censor reason or None). `done_fn(slot)`
    True stops early (--until-exit)."""
    t = _tape()
    mint, pages = row["mint"], 0
    cursor = lo
    while cursor <= hi:
        end = min(hi, cursor + CHUNK_SLOTS - 1)
        while True:
            cov = [tuple(r) for r in db.execute("SELECT lo, hi, bucket FROM coverage WHERE mint=?", (mint,))]
            gaps = t.uncovered(cov, cursor, end, 0)
            if not gaps:
                break
            a, b = gaps[0]
            left = page_cap - pages
            if left <= 0:
                return a - 1, pages, "page_cap"
            # Stored every BATCH_PAGES pages, so a book that opens mid-read loses at most that many.
            r = t.read_range(call, mint, a, b, max_pages=min(left, BATCH_PAGES))
            store(db, row["row_id"], mint, r, pool)
            pages += len(r.pages)
            if r.truncated and not r.covered:
                return a - 1, pages, "slot_too_dense"          # one slot holds more than a batch
        cursor = end + 1
        if done_fn is not None and done_fn(end):
            return end, pages, None
    return hi, pages, None


def read_anchor(db, call, row, lo, pool=None):
    """The newest transaction before `lo` (one desc page), so the path's first second has a price."""
    t = _tape()
    r = t.read_range(call, row["mint"], max(0, lo - 9000), lo - 1, max_pages=1, order="desc")
    r.covered = []                               # a desc page proves nothing about the slots it skipped
    store(db, row["row_id"], row["mint"], r, pool)
    return len(r.pages)


# --- the path ----------------------------------------------------------------------------------------

def _spot(basis, coin_res, quote_res, cdec, qdec, v_quote=0):
    try:
        c, q = int(coin_res), int(quote_res) + int(v_quote or 0)
    except (TypeError, ValueError):
        return None
    if c <= 0 or q <= 0 or cdec is None or qdec is None:
        return None
    return (q / 10 ** qdec) / (c / 10 ** cdec)


def cl_spot(basis, extra, cdec, qdec, bin_step=None):
    """Quote per whole coin after a DAMM v2 or DLMM swap, from its event, or None.
    DAMM v2: next_sqrt_price is Q64.64 sqrt(B atomic per A atomic). DLMM: the active bin after the
    swap prices Y atomic per X atomic at (1 + bin_step / 10^4) ^ bin_id."""
    if not extra or cdec is None or qdec is None:
        return None
    try:
        if basis == "damm_v2_event":
            b_per_a = (int(extra["sqrt_price_x64"]) / 2 ** 64) ** 2
            atomic = b_per_a if extra["coin_is_a"] else (1.0 / b_per_a if b_per_a else None)
        elif basis == "dlmm_event" and bin_step:
            y_per_x = (1.0 + bin_step / 1e4) ** int(extra["end_bin_id"])
            atomic = y_per_x if extra["coin_is_x"] else 1.0 / y_per_x
        else:
            return None
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    return atomic * 10 ** cdec / 10 ** qdec if atomic else None


def load_swaps(db, mint, v_quote_fn=lambda owner: None, bin_step_fn=lambda lb_pair: None):
    """Every stored swap of `mint` in chain order, each with its pool key, basis, spot and paid price.
    Pool key: 'curve' for the pump curve (one per mint), else the coin vault."""
    cols = ("signature", "venue", "coin_vault", "pool_owner", "quote_mint", "quote_decimals",
            "coin_decimals", "coin_delta", "quote_delta", "coin_reserve_after", "quote_reserve_after",
            "price", "cp", "fee_bps", "slot", "tx_index", "block_time")
    rows = db.execute(
        "SELECT %s, s.basis, s.coin_res, s.quote_res, s.fee_bps, s.extra FROM pool_swaps p "
        "LEFT JOIN swap_state s ON s.signature=p.signature AND s.coin_vault=p.coin_vault "
        "WHERE p.mint=? ORDER BY p.slot, p.tx_index" % ",".join("p." + c for c in cols), (mint,)).fetchall()
    out = []
    for r in rows:
        d = dict(zip(cols, r[:len(cols)]))
        basis, c_res, q_res, fee, extra = r[len(cols):]
        extra = json.loads(extra) if extra else None
        d["pool"] = "curve" if d["venue"] == "pump_curve" else d["coin_vault"]
        d["fee_bps_event"] = fee
        v, spot = 0, None
        if d["venue"] == "pump_amm":
            v = v_quote_fn(d["pool_owner"])
            basis, c_res, q_res = (("pump_amm_vaults_v", d["coin_reserve_after"], d["quote_reserve_after"])
                                   if v is not None else ("trade_avg", None, None))
        if basis in CL_BASES:
            step = bin_step_fn(extra.get("lb_pair")) if basis == "dlmm_event" else None
            spot = cl_spot(basis, extra, d["coin_decimals"], d["quote_decimals"], step)
            if spot is None:
                basis = "trade_avg"                  # an unread or refused bin step: unknown
        elif basis is None or c_res is None:
            basis = "trade_avg"
        else:
            spot = _spot(basis, c_res, q_res, d["coin_decimals"], d["quote_decimals"], v)
        d.update(basis=basis, coin_res=c_res, quote_res=q_res, v_quote=v or 0, extra=extra,
                 spot=spot if basis in EXACT else None, paid=d["price"])
        out.append(d)
    return out


def bracket_violations(swaps):
    """{pool: (violations, checked)}. On a constant-product pool a swap's paid price lies between
    the spot before and after it, up to the fee. A reserve basis that breaks this on more than
    BRACKET_MAX_VIOLATIONS of its swaps is not the pool's price (a wrong virtual reserve, say)."""
    last, out = {}, {}
    for s in swaps:
        if s["basis"] not in EXACT or not s["spot"] or not s["paid"]:
            continue
        pre = last.get(s["pool"])
        last[s["pool"]] = s["spot"]
        if pre is None:
            continue
        lo, hi = min(pre, s["spot"]), max(pre, s["spot"])
        ok = lo * (1 - BRACKET_FEE) <= s["paid"] <= hi * (1 + BRACKET_FEE)
        bad, n = out.get(s["pool"], (0, 0))
        out[s["pool"]] = (bad + (not ok), n + 1)
    return out


def choose_pool(swaps, *, pool_key=None, venue=None, after_slot=None, first_s=60):
    """(pool key, how, candidates). The given pool, else the venue's (or any) pool with the most
    swaps in the first `first_s` seconds after `after_slot`; a runner-up with half the leader's
    count or more is ambiguous and refuses (None)."""
    if pool_key:
        return pool_key, "given", {}
    early = [s for s in swaps if (after_slot is None or s["slot"] >= after_slot)
             and (venue is None or s["venue"] == venue)]
    if early and early[0]["block_time"] is not None:
        t0 = early[0]["block_time"]
        early = [s for s in early if s["block_time"] is not None and s["block_time"] <= t0 + first_s]
    counts = {}
    for s in early:
        counts[s["pool"]] = counts.get(s["pool"], 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if not ranked:
        return None, "no_swaps", counts
    if len(ranked) > 1 and ranked[1][1] * 2 >= ranked[0][1]:
        return None, "ambiguous", counts
    return ranked[0][0], "most_swaps_first_minute", counts


def migrated_pool(swaps, primary):
    """(pump_amm pool, first slot) a curve migrated into: the PumpSwap pool of this mint whose first
    swap comes after the curve's last one. None when the primary is not the curve or no such pool."""
    if primary != "curve":
        return None
    curve = [s["slot"] for s in swaps if s["pool"] == "curve"]
    if not curve:
        return None
    last = max(curve)
    after = {}
    for s in swaps:
        if s["venue"] == "pump_amm" and s["slot"] > last:
            after.setdefault(s["pool"], s["slot"])
    if not after:
        return None
    before = {s["pool"] for s in swaps if s["venue"] == "pump_amm" and s["slot"] <= last}
    fresh = [(slot, pool) for pool, slot in after.items() if pool not in before]
    return min(fresh)[::-1] if fresh else None


def executable_unit(state, qty, *, tf=(0, 0)):
    """Quote per whole coin our whole bag `qty` would fetch against `state` (a swap's reserves), or
    None. The coin's Token-2022 transfer fee (bps, maximum atomic) comes off what reaches the pool;
    the venue fee comes off the input on CPMM and off the output elsewhere. After a de-risk the
    replay still prices the full bag's impact (slightly low for the smaller remainder)."""
    if (state is None or state["basis"] not in EXACT or not qty or qty <= 0
            or state.get("fee_bps") is None or state["coin_decimals"] is None):
        return None
    if state["basis"] in CL_BASES:
        # ponytail: no impact on concentrated pools (their depth is not in the event); the venue fee
        # only. Optimistic by our clip's impact; derive L from consecutive sqrt prices if it matters.
        bps, maximum = tf or (0, 0)
        return state["spot"] * (1 - float(state["fee_bps"]) / 1e4) * (1 - (bps or 0) / 1e4)
    try:
        b = int(state["coin_res"])
        q = int(state["quote_res"]) + int(state.get("v_quote") or 0)
    except (TypeError, ValueError):
        return None
    if b <= 0 or q <= 0:
        return None
    f = float(state["fee_bps"]) / 1e4
    x = qty * 10 ** state["coin_decimals"]
    bps, maximum = tf or (0, 0)
    if bps:
        x -= min(math.ceil(x * bps / 1e4), maximum) if maximum else math.ceil(x * bps / 1e4)
    if state["venue"] == "raydium_cpmm":
        x *= (1 - f)
    out = q * x / (b + x)
    if state["venue"] != "raydium_cpmm":
        out *= (1 - f)
    return out / 10 ** state["quote_decimals"] / qty


def build_path(swaps, lp_rows, undecoded, *, primary, t_start, t_end, qty=None, tf=(0, 0),
               own_sigs=(), sol_usd=None, quote_is_usd=False):
    """One row per second t_start..t_end (inclusive): the primary pool's state after the last swap
    at or before that second. Returns (seconds, flags, ticks): `ticks` is every primary-pool swap
    from t_start on, in chain order, with the state right after it, so the replay can decide after
    each swap and never on a later swap of the same second. A downgraded pool (bracket check) is
    priced on paid prices; a curve that migrated continues on its PumpSwap pool."""
    flags = {}
    viol = bracket_violations(swaps)
    bad = {p for p, (b, n) in viol.items() if n >= 5 and b / n > BRACKET_MAX_VIOLATIONS}
    if bad:
        flags["reserve_mismatch"] = {p: viol[p] for p in bad}
    mig = migrated_pool(swaps, primary)
    if mig:
        flags["migrated"] = {"pool": mig[0], "first_slot": mig[1]}
    fee = {}
    timed = []
    for s in swaps:
        if s["fee_bps_event"] is not None:
            fee[s["pool"]] = s["fee_bps_event"]
        on = s["pool"] == primary or (mig and s["pool"] == mig[0] and s["slot"] >= mig[1])
        if s["block_time"] is None:
            flags["no_block_time"] = flags.get("no_block_time", 0) + 1
            continue
        basis = "trade_avg" if s["pool"] in bad else s["basis"]
        price = s["spot"] if basis in EXACT else s["paid"]
        state = dict(s, basis=basis, fee_bps=fee.get(s["pool"]))
        timed.append((int(s["block_time"]), on, price, state, s["signature"] in own_sigs,
                      (s["slot"], s["tx_index"] or 0)))
    # R4 (Codex review 2026-10-06): a liquidity add or withdrawal on the priced pool changes its
    # depth even when the spot stays; the executable quote is unknown from it until the next swap
    # of that pool, never carried across it. Placed in chain order among the swaps.
    for r in lp_rows:
        if r.get("block_time") is None or r.get("slot") is None:
            continue
        if r["coin_vault"] == primary or (mig and r["coin_vault"] == mig[0] and r["slot"] >= mig[1]):
            timed.append((int(r["block_time"]), True, None, "lp", False, (r["slot"], r.get("tx_index") or 0)))
    timed.sort(key=lambda x: x[5])
    lp_by_t, und_by_t = {}, {}
    for r in lp_rows:
        if r["coin_vault"] == primary and r["block_time"] is not None:
            lp_by_t[int(r["block_time"])] = lp_by_t.get(int(r["block_time"]), 0) + 1
    for r in undecoded:
        if r["block_time"] is not None:
            und_by_t[int(r["block_time"])] = und_by_t.get(int(r["block_time"]), 0) + 1
    out, ticks, i, last, state, lp_stale = [], [], 0, None, None, False
    for sec in range(int(t_start), int(t_end) + 1):
        n = other = own = 0
        while i < len(timed) and timed[i][0] <= sec:
            _t, on, price, st, mine, _pos = timed[i]
            if st == "lp":
                lp_stale = True
                if _t >= t_start:            # the replay's own moment: depth unknown from here (S1)
                    ticks.append({"t": _t, "lp": True})
                i += 1
                continue
            if on:
                if price:
                    last, state, lp_stale = price, st, False
                    if _t >= t_start:
                        ticks.append({"t": _t, "price": price, "basis": st["basis"],
                                      "exec": executable_unit(st, qty, tf=tf) if qty else None})
                n += _t == sec
                own += mine and _t == sec
            else:
                other += _t == sec
            i += 1
        ex = executable_unit(state, qty, tf=tf) if qty and not lp_stale else None
        out.append({"t_s": sec, "slot": state["slot"] if state else None,
                    "price_quote": last, "price_usd": (last * (1.0 if quote_is_usd else sol_usd)
                                                       if last is not None and (quote_is_usd or sol_usd) else None),
                    "exec_quote": ex, "n_swaps": n, "stale": int(n == 0), "n_other_pool": other,
                    "n_own": own, "n_undecoded": und_by_t.get(sec, 0), "lp_event": lp_by_t.get(sec, 0),
                    "exec_lp_stale": int(lp_stale),
                    "basis": state["basis"] if state else None})
    return out, flags, ticks


# --- the replay --------------------------------------------------------------------------------------

def synthetic_position(policy, copy_arm, *, mint, fill_iso, entry_price, qty):
    """A position dict the runner's exit code parses: a canonical funded contract carrying `policy`,
    and the volume-death threshold build_funded_contract freezes when the timecap lift is on."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_funded_policy import FUNDED_CONTRACT_VERSION
    from sniper.settings import memecoin as cfg
    source = {"copy_arm": copy_arm} if copy_arm else {}
    if getattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", False) and getattr(cfg, "MEMECOIN_VOLDEATH_EXIT_S", 0):
        source["voldeath_exit_s"] = float(cfg.MEMECOIN_VOLDEATH_EXIT_S)
    contract = {"contract_version": FUNDED_CONTRACT_VERSION,
                "identity": {"network_id": "solana", "mint": mint},
                "policy": policy.to_dict(), "source": source}
    payload, digest = m.canonical_funded_json(contract)
    return {"id": 1, "mint": mint, "network_id": "solana", "entry_price": entry_price,
            "entry_notional": entry_price * qty, "opened_at": fill_iso, "opened_recorded_at": fill_iso,
            "funded_contract_version": FUNDED_CONTRACT_VERSION, "funded_contract_hash": digest,
            "funded_contract_json": payload, "executable_peak_price": None, "executable_peak_at": None,
            "derisked_at": None, "derisk_sig": None, "last_quote_ts": fill_iso, "stuck_bag": 0}


def _marks_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_position_marks (chain_id TEXT, mint TEXT, sweep_ts TEXT, "
              "source TEXT, mid REAL)")
    c.execute("CREATE TABLE memecoin_fills (position_id INTEGER, kind TEXT, notional REAL, price REAL)")
    return c


def mirror_seconds(ticks, owners):
    """[(t_s, side, tokens, price)] of the source's own trades of the coin from the chain: an owner in
    `owners` whose balance of the coin moved in a swap transaction."""
    out = []
    for t in ticks:
        if t["owner"] in owners and t["kind"] == "swap" and t["block_time"] is not None:
            d = int(t["delta"])
            out.append((int(t["block_time"]), "buy" if d > 0 else "sell", abs(d) / 10 ** (t["decimals"] or 0)))
    return sorted(out)


_REPLAY_IDS = iter(range(1, 10 ** 9))


def _events(seconds, ticks, fill_t, cadence_s):
    """The moments the replay decides at: (time, mid, exec, basis, is_swap, lp_invalid). At cadence 1, after
    EVERY primary-pool swap from the fill on, at the swap's own place in the second (time = its
    second plus k/1000 for its index k there), and at the end of each second with no swap. That is
    no lookahead inside a second (Alpha, after Gamma LOW 3), and it sees every touch the pool made,
    which the live ~1 s sweep can miss. At a slower cadence: the end-of-second state every
    `cadence_s` seconds, the slow lane's view."""
    rows = sorted((r for r in seconds if r["t_s"] >= fill_t), key=lambda r: r["t_s"])
    if cadence_s != 1 or ticks is None:
        return [(r["t_s"], r["price_quote"], r["exec_quote"], r.get("basis"), bool(r.get("n_swaps")),
                 bool(r.get("exec_lp_stale"))) for r in rows if (r["t_s"] - fill_t) % cadence_s == 0]
    by_sec = {}
    for tk in ticks:
        if tk["t"] >= fill_t:
            by_sec.setdefault(tk["t"], []).append(tk)
    out, mid = [], None
    for r in rows:
        swaps = by_sec.get(r["t_s"])
        if not swaps:
            mid = r["price_quote"]
            out.append((r["t_s"], mid, r["exec_quote"], r.get("basis"), False, bool(r.get("exec_lp_stale"))))
            continue
        for k, tk in enumerate(swaps[:999]):
            if tk.get("lp"):
                # An LP change on the priced pool (S1): the executable depth is unknown from this
                # moment until the next swap. The display mid carries; nothing executable does.
                out.append((r["t_s"] + k / 1000.0, mid, None, None, False, True))
                continue
            mid = tk["price"]
            out.append((r["t_s"] + k / 1000.0, tk["price"], tk["exec"], tk["basis"], True, False))
    return out


def replay(seconds, policy, copy_arm, *, mint, fill_t, fill_price, qty, ticks=None, source_trades=(),
           cadence_s=1, exit_lag_s=EXIT_LAG_S):
    """Run the funded exit over a path from the fill. Returns a dict: reason, decided_t, exit price
    at the decision moment and after the lag (landed, primary: the end-of-second state then), legs,
    return_pct (quote terms, whole position incl. de-risk legs), or a censor reason.

    A PAID price never decides (Gamma MED on af3892f76). On a trade_avg moment the price is the
    last swap's paid price, above spot after a buy and below it after a sell, so one trade can fake
    a TP or a stop the pool never reached. A price-decided rail there, or any exit priced on one,
    censors the replay (`trade_avg_decision` / `trade_avg_exit_price`) with what it would have
    been; it never decides."""
    from sniper.services import memecoin_real_runner as rr
    from sniper.services import memecoin_volume_death as vd
    from sniper.services.memecoin_funded_policy import frozen_voldeath_exit_s, is_exit_trial_v4
    from sniper.settings import memecoin as cfg
    by_t = {r["t_s"]: r for r in seconds}
    fill_iso = _iso(fill_t)
    pos = synthetic_position(policy, copy_arm, mint=mint, fill_iso=fill_iso, entry_price=fill_price, qty=qty)
    pos["id"] = pid = next(_REPLAY_IDS)        # the exact-quote witness tape is keyed by position id
    conn = _marks_db()
    open_qty, legs, flags = qty, [], set()
    hits = 0
    # The runner's own order: armed knob, never for the V4 trial, the contract's frozen threshold
    # first and the env value only for a contract without one; the state witness as configured.
    vd_on = bool(getattr(cfg, "MEMECOIN_VOLDEATH_ARMED", False)) and not is_exit_trial_v4(policy)
    vd_frozen = frozen_voldeath_exit_s(pos)
    vd_exit_s = float(vd_frozen if vd_frozen is not None else getattr(cfg, "MEMECOIN_VOLDEATH_EXIT_S", 0) or 0)
    vd_witness = bool(getattr(cfg, "MEMECOIN_VOLDEATH_STATE_WITNESS", True))
    size_chain = "solana" in getattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_CHAINS", frozenset())
    sells = [tr for tr in source_trades if tr[1] == "sell" and tr[0] > fill_t]
    v4 = is_exit_trial_v4(policy)

    events = _events(seconds, ticks, fill_t, cadence_s)
    times = [e[0] for e in events]
    landed_basis = {}

    def price_after(t):
        """The pool's state AT time `t`: the last moment at or before it (Gamma LOW 1). A second with
        no swap holds one state throughout, so the lag is the same for a per-swap decision and an
        end-of-second one. (price, that moment), or (None, None) past the path's end or before any
        price."""
        if not events or t > events[-1][0] + 1.0:
            return None, None
        for i in range(bisect.bisect_right(times, t + 1e-9) - 1, -1, -1):
            when, mid, ex, basis, _swap, invalid = events[i]
            if invalid:
                return None, when            # depth unknown here: never scan back past an LP change (S1)
            v = ex if ex is not None else mid
            if v is not None:
                landed_basis[when] = basis
                return v, when
        return None, None

    def censor_paid(reason, t, basis_now, landed_t, by_price=True):
        """The censor record when a paid price would decide or price this exit, else None."""
        landed_paid = landed_t is not None and landed_basis.get(landed_t) == "trade_avg"
        why = ("trade_avg_decision" if by_price and basis_now == "trade_avg"
               else "trade_avg_exit_price" if landed_paid else None)
        return why and {"reason": None, "censor": why, "would_be": reason, "decided_t": t,
                        "legs": legs, "flags": sorted(flags)}

    def close(reason, t, basis_now):
        at_dec, _ = price_after(t)
        landed, landed_t = price_after(t + exit_lag_s)
        if landed is None:
            return {"reason": reason, "decided_t": t, "legs": legs, "flags": sorted(flags),
                    "censor": "lp_unknown_depth" if landed_t is not None else "no_price_after_lag"}
        paid = censor_paid(reason, t, basis_now, landed_t, by_price=not reason.startswith("mirror"))
        if paid:
            return paid
        proceeds = sum(leg["proceeds"] for leg in legs) + open_qty * landed
        legs.append({"t": landed_t, "fraction_of_open": 1.0, "price": landed, "proceeds": open_qty * landed,
                     "reason": reason})
        return {"reason": reason, "decided_t": t, "held_s": t - fill_t, "exit_price_decision": at_dec,
                "exit_price_landed": landed, "landed_t": landed_t, "legs": legs,
                "return_pct": (proceeds / (fill_price * qty) - 1.0) * 100.0, "flags": sorted(flags)}

    try:
        for t, mid, ex, basis, is_swap, invalid in events:
            now_iso = _iso(t)
            if ex is None and mid is not None and not invalid:
                ex = mid                      # a nominal-only path, never across a known LP change (S1)
                flags.add("exec_from_mid")
            if is_swap and mid is not None:
                conn.execute("INSERT INTO memecoin_position_marks VALUES ('solana',?,?,?,?)",
                             (mint, now_iso, vd.SWAP_SOURCE, mid))
                if basis in EXACT:            # the pool's own reserve state after this swap
                    conn.execute("INSERT INTO memecoin_position_marks VALUES ('solana',?,?,?,?)",
                                 (mint, now_iso, vd.STATE_SOURCE, mid))
            if ex is not None:
                conn.execute("INSERT INTO memecoin_position_marks VALUES ('solana',?,?,?,?)",
                             (mint, now_iso, vd.EXECUTABLE_SOURCE, ex))
                vd.record_exact_quote(pid, quoted_at=now_iso, price=ex)
                pos["last_quote_ts"] = now_iso
                if pos["executable_peak_price"] is None or ex > pos["executable_peak_price"]:
                    pos["executable_peak_price"], pos["executable_peak_at"] = ex, now_iso
            # MIRROR (model): the source's sells up to this moment.
            if policy.mirror_on_sell and sells and sells[0][0] <= t:
                if v4:
                    return close("mirror_first_sell", t, basis)
                if size_chain:
                    trades = [{"side": sd, "their_price": 1.0, "their_size_usd": tok, "created_at": _iso(ts)}
                              for ts, sd, tok in source_trades if ts <= t]
                    frac, _detail = rr.source_sell_share(
                        trades, opened_at=fill_iso, lookback_s=cfg.MEMECOIN_REAL_MIRROR_SIZE_LOOKBACK_S)
                    flags.add("mirror_history_from_window")
                    if frac is not None and frac >= float(cfg.MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION):
                        return close("mirror_size", t, basis)
                else:
                    flags.add("mirror_legacy_any_sell")
                    return close("mirror", t, basis)
                sells = [sl for sl in sells if sl[0] > t]
            reason = rr.alpha_b_real_exit_reason(pos, mid, now_iso)
            reason, hits = _sweep_gates(rr, cfg, pos, reason, mid=mid, ex=ex, now_iso=now_iso,
                                        held_s=t - fill_t, hits=hits)
            if reason in rr._EXECUTABLE_PRICE_REASONS and ex is None and invalid:
                # The depth is unknown after an LP change (S1): a price rail holds, as the runner's
                # executable gate does without an exact quote; it never sizes or prices on the mid.
                flags.add("lp_unknown_depth_hold")
                hits = 0 if reason in LOSS_REASONS else hits
                reason = None
            if reason in rr._EXECUTABLE_PRICE_REASONS and ex is not None:
                ok, _detail = rr.executable_exit_allowed(conn, pos, reason, {"expected_usdc": ex * open_qty},
                                                         quote_qty=open_qty)
                if not ok:
                    hits = 0 if reason in LOSS_REASONS else hits     # the runner resets them too
                    reason = None
            if reason == "derisk":
                mode, frac = rr._derisk_sizing(pos)
                if mode != "fraction":
                    frac = max(0.0, pos["entry_notional"] - sum(leg["proceeds"] for leg in legs)) / (ex * open_qty)
                landed, landed_t = price_after(t + exit_lag_s)
                paid = censor_paid("derisk", t, basis, landed_t)
                if paid:
                    return paid
                if landed is not None and 0 < frac < 0.9:
                    sold = open_qty * frac
                    legs.append({"t": landed_t, "fraction_of_open": frac, "price": landed,
                                 "proceeds": sold * landed, "reason": "derisk"})
                    conn.execute("INSERT INTO memecoin_fills VALUES (?,'derisk',?,?)", (pid, sold * landed, landed))
                    open_qty -= sold
                    pos["derisked_at"] = now_iso     # landed: no outstanding derisk_sig, so the
                                                     # trail may arm (rr.derisk_confirmed)
                reason = None
            if reason:
                return close(reason, t, basis)
            if vd_on and vd_exit_s > 0 and t - fill_t >= vd_exit_s:
                fire, _silence, _why = vd.armed_exit_decision(
                    conn, pos, now_iso=now_iso, held_s=t - fill_t, exit_s=vd_exit_s,
                    min_opened_at=fill_iso, stream_ok=True, require_state_witness=vd_witness,
                    require_onchain=False)
                if fire:
                    # A transaction this tool could not decode inside the silent window may have
                    # moved the pool unseen (Gamma LOW 1), so such a fire is censored.
                    unseen = sum((by_t.get(k) or {}).get("n_undecoded") or 0
                                 for k in range(int(t - vd_exit_s), int(t) + 1))
                    if unseen:
                        return {"reason": None, "censor": "vol_death_unseen_txs", "would_be": "vol_death",
                                "decided_t": t, "legs": legs, "flags": sorted(flags)}
                    return close("vol_death", t, basis)
        return {"reason": None, "censor": "open_at_horizon", "legs": legs, "flags": sorted(flags)}
    finally:
        with vd._EXACT_TAPE_LOCK:
            vd._EXACT_TAPE.pop(pid, None)


def _sweep_gates(rr, cfg, pos, reason, *, mid, ex, now_iso, held_s, hits):
    """(reason, stop hits) after the sweep's own gates between nomination and confirmation, in the
    runner's order (real_exit_sweep): the TP window, the hard no-loss window, the instant stop, then
    the stop window or the sweep debounce; a quoted non-stop sweep releases both stop clocks."""
    from sniper.services.memecoin_funded_policy import frozen_policy_from_position
    tp_window = rr._tp_confirm_window_s()
    if reason in rr._TP_CONFIRM_REASONS and tp_window > 0:
        started = pos.get("tp_breach_started_at") or now_iso
        pos["tp_breach_started_at"] = started
        ok, _el = rr.stop_breach_confirmed(started, now_iso, tp_window)
        if not ok:
            return None, hits
    elif pos.get("tp_breach_started_at") and mid:
        pos["tp_breach_started_at"] = None
    no_loss = float(getattr(cfg, "MEMECOIN_REAL_NO_LOSS_SELL_S", 0.0) or 0.0)
    if no_loss > 0 and reason in rr._LOSS_EXIT_REASONS and held_s < no_loss:
        reason = None
    instant = float(getattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_PCT", 0.0) or 0.0)
    grace = float(getattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_GRACE_S", 0.0) or 0.0)
    loss = (ex / pos["entry_price"] - 1.0) * 100.0 if ex and pos["entry_price"] else None
    in_grace = (grace > 0 and reason == "b_stop" and held_s < grace
                and not (loss is not None and loss <= -rr._effective_catastrophe_pct(pos)))
    if reason in LOSS_REASONS and instant > 0 and not in_grace and loss is not None and loss <= -instant:
        return reason, hits
    window = cfg.stop_confirm_window_s()
    need = frozen_policy_from_position(pos).stop_debounce_sweeps
    if reason in LOSS_REASONS and window > 0:
        started = pos.get("stop_breach_started_at") or now_iso
        pos["stop_breach_started_at"] = started
        ok, _el = rr.stop_breach_confirmed(started, now_iso, window)
        return (reason if ok else None), hits
    if reason in LOSS_REASONS and need > 1:
        counts, started = rr.stop_hit_counts(hits, pos.get("stop_breach_started_at"), now_iso,
                                             0.75 * float(cfg.MEMECOIN_FASTLANE_TICK_S))
        pos["stop_breach_started_at"] = started
        hits += bool(counts)
        return (reason if hits >= need else None), hits
    if reason not in LOSS_REASONS and mid:
        pos["stop_breach_started_at"] = None
        hits = 0
    return reason, hits


# --- one row: its policy, its prices, its reads -----------------------------------------------------

def policies_for(ledger, row):
    """[{band, policy, copy_arm, exit_trial} | {band, error}]: what the funded code would freeze for
    this origin (memecoin_risk.funded_policy_for_origin, then the Degen V4 overlay, as
    prepare_funded_contract does) for the row's band, or for every band when the band is unknown."""
    from sniper.services.memecoin_funded_policy import exit_trial_v4_applies, exit_trial_v4_policy
    from sniper.services.memecoin_risk import funded_policy_for_origin
    out = []
    for band in ([row["band"]] if row.get("band") else BANDS):
        try:
            policy, arm = funded_policy_for_origin(ledger, coin_band=band, network_id="solana",
                                                   source_user_id=row["source_user_id"])
            trial = exit_trial_v4_applies(source_user_id=row["source_user_id"], network_id="solana",
                                          decision_at=row["decision_at"])
            if trial is not None:
                policy, arm = exit_trial_v4_policy(coin_band=band), None
            out.append({"band": band, "policy": policy, "copy_arm": arm, "exit_trial": trial})
        except ValueError as exc:
            out.append({"band": band, "error": str(exc)})
    return out


def _trace_sol_usd(trace_json):
    try:
        v = (json.loads(trace_json or "{}").get("source_evidence") or {}).get("sol_usd")
        v = float(v)
    except (TypeError, ValueError, AttributeError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def sol_usd_for(ledger, row):
    """(USD per SOL, source, age_s) for the row (Alpha C1: one value from our own ledger, held
    constant): the row's own, its attempt's trace, else the nearest funded Solana fill or attempt
    trace within SOL_USD_MAX_AGE_S of the decision. (None, 'unknown', None) otherwise."""
    if row.get("sol_usd"):
        return float(row["sol_usd"]), "row", 0.0
    at = _epoch(row["decision_at"])
    if row.get("attempt_id"):
        r = ledger.execute("SELECT trace_json FROM memecoin_entry_attempts WHERE attempt_id=?",
                           (row["attempt_id"],)).fetchone()
        v = _trace_sol_usd(r[0]) if r else None
        if v:
            return v, "attempt_trace", 0.0
    lo, hi = _iso(at - SOL_USD_MAX_AGE_S), _iso(at + SOL_USD_MAX_AGE_S)
    best = None
    for when, v, src in [(w, v, "fill") for w, v in ledger.execute(
            "SELECT created_at, funding_sol_usd FROM memecoin_fills WHERE network_id='solana' "
            "AND funding_sol_usd > 0 AND created_at BETWEEN ? AND ?", (lo, hi))] + [
            (w, _trace_sol_usd(tj), "attempt_trace_nearest") for w, tj in ledger.execute(
                "SELECT decision_started_at, trace_json FROM memecoin_entry_attempts WHERE "
                "network_id='solana' AND decision_started_at BETWEEN ? AND ? AND trace_json LIKE "
                "'%\"sol_usd\"%' LIMIT 500", (lo, hi))]:
        try:
            age = abs(_epoch(when) - at)
        except (TypeError, ValueError):
            continue
        if v and (best is None or age < best[2]):
            best = (float(v), src, age)
    return best or (None, "unknown", None)


def owners_for(ledger, row):
    """The source wallet's Solana addresses: the row's own, else every VERIFIED registry address of
    the profile (a wallet switch keeps both), plus the address a pump.fun profile id names."""
    if row.get("source_address"):
        return {a.strip() for a in str(row["source_address"]).split(",") if a.strip()}
    from sniper.persistence import db_memecoin as m
    uid = str(row["source_user_id"])
    out = set()
    with contextlib.suppress(sqlite3.Error):
        out = {r["address"] for r in m.address_registry_rows(ledger, "solana")
               if str(r["source_user_id"]) == uid and r["status"] == "verified"}
    if uid.startswith("pump:"):
        out.add(uid[5:])
    return out


def block_time(db, call, slot):
    """(slot, blockTime) of `slot`, or of the nearest earlier slot that has a block."""
    def fetch():
        for probe in range(int(slot), int(slot) - 8, -1):
            try:
                bt = call("getBlockTime", [probe])
            except RuntimeError:
                continue
            if bt is not None:
                return [probe, int(bt)]
        return None
    got = fact(db, "block_time:%d" % int(slot), fetch)
    if got is None:
        raise ReadStop("no block near slot %s" % slot)
    return got


def mint_facts(db, call, mint, epoch):
    """(decimals, transfer fee bps, fee maximum) of the coin, or None (one jsonParsed read)."""
    from sniper.execution.raydium_cpmm_buy import _mint_facts
    value = fact(db, "mint:%s" % mint, lambda: (call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
                                               or {}).get("value"))
    got = _mint_facts(value, epoch) if value else None
    return None if got is None else got[1:]


def pumpswap_virtual_quote(db, call, pool):
    """The Pool account's virtual quote reserve (pumpswap_buy: the vaults alone price 7-15% off)."""
    from sniper.execution.pumpswap_buy import parse_pool_account

    def fetch():
        value = (call("getAccountInfo", [pool, {"encoding": "base64"}]) or {}).get("value")
        try:
            return int(parse_pool_account(value["data"][0])["virtual_quote"])
        except (TypeError, KeyError, IndexError, ValueError):
            return None
    return fact(db, "pumpswap_v:%s" % pool, fetch)


def dlmm_bin_step_for(db, call, lb_pair):
    """A DLMM pair's bin step (one cached LbPair read), or None when the layout check refuses it."""
    import base64

    def fetch():
        value = (call("getAccountInfo", [lb_pair, {"encoding": "base64"}]) or {}).get("value")
        try:
            return dlmm_bin_step(base64.b64decode(value["data"][0]))
        except (TypeError, KeyError, IndexError, ValueError):
            return None
    return fact(db, "dlmm_bin_step:%s" % lb_pair, fetch)


def resolve_primary(db, swaps, row, after_slot):
    """(pool key, how, candidates) for the row: the given pool by coin vault, pool owner or the
    instruction that named it, else choose_pool by the venue."""
    pool = row.get("pool")
    if pool:
        for s in swaps:
            if pool in (s["coin_vault"], s["pool_owner"]):
                return s["pool"], "given", {}
        hit = db.execute("SELECT coin_vault FROM pool_map WHERE mint=? AND pool=?",
                         (row["mint"], pool)).fetchone()
        return (hit[0], "given_by_instruction", {}) if hit else (None, "pool_unresolved", {})
    return choose_pool(swaps, venue=row.get("venue"), after_slot=after_slot)


def _covered_time(db, mint, slot):
    r = db.execute("SELECT MAX(block_time) FROM pool_swaps WHERE mint=? AND slot<=?", (mint, slot)).fetchone()
    return r[0]


def run_row(db, call, ledger, row, *, out_dir, page_cap=PAGE_CAP, until_exit=False, cadence_s=1,
            exit_lag_s=EXIT_LAG_S, release_sha=None, knobs=None):
    """Read, price and replay one row; write its files. Returns the replay.jsonl records."""
    mint = row["mint"]
    calls_before = dict(call.calls) if hasattr(call, "calls") else {}
    if row.get("fill_slot") is not None:
        fill_slot, fill_t = block_time(db, call, int(row["fill_slot"]))
        how_fill = "fill_slot"
    else:
        # A fill modelled AT the decision is anchored on the decision's own quote slot, exactly;
        # any other fill_at is searched (Gamma 2026-10-06), and censored if no block is near it.
        traced = (fill_slot_from_trace(ledger, row["attempt_id"])
                  if ledger is not None and row.get("attempt_id") and row.get("decision_at")
                  and abs(_epoch(row["fill_at"]) - _epoch(row["decision_at"])) < 1.0 else None)
        if traced is not None:
            fill_slot, fill_t = block_time(db, call, traced)
            how_fill = "decision_trace_quote_slot"
        else:
            fill_slot, fill_t = slot_at(call, _epoch(row["fill_at"]))
            how_fill = "fill_at_search"
    window_s = int(row.get("window_s") or WINDOW_S)
    lo, hi = fill_slot - PRE_SLOTS, fill_slot + int(math.ceil(window_s / SLOT_S_MIN))
    facts = mint_facts(db, call, mint, fill_slot // SLOTS_PER_EPOCH)
    sol_usd, sol_src, sol_age = sol_usd_for(ledger, row)
    policies = policies_for(ledger, row)
    owners = owners_for(ledger, row)
    from sniper.persistence import db_memecoin as m
    own = {s.lower() for s in m.own_tx_hashes(ledger, "solana", mint)}
    pages = 0
    if not db.execute("SELECT 1 FROM pool_swaps WHERE mint=? AND slot<? LIMIT 1", (mint, lo)).fetchone():
        pages += read_anchor(db, call, row, lo, row.get("pool"))

    def v_quote(owner):
        return pumpswap_virtual_quote(db, call, owner) if owner else None

    def bin_step(lb_pair):
        return dlmm_bin_step_for(db, call, lb_pair) if lb_pair else None

    def compute(t_end_cap):
        swaps = load_swaps(db, mint, v_quote, bin_step)
        primary, how, cands = resolve_primary(db, swaps, row, lo)
        if primary is None:
            return {"censor": how, "candidates": cands}
        prim = [s for s in swaps if s["pool"] == primary]
        quote_mint = prim[-1]["quote_mint"] if prim else None
        t = _tape()
        quote_is_usd = quote_mint in (t.USDC, t.USDT)
        if row.get("fill_price_quote") is not None:
            fill_q = float(row["fill_price_quote"])
        elif quote_is_usd:
            fill_q = float(row["fill_price_usd"])
        elif quote_mint == t.WSOL and sol_usd:
            fill_q = float(row["fill_price_usd"]) / sol_usd
        else:
            return {"censor": "fill_price_unconvertible", "quote_mint": quote_mint}
        clip_q = (float(row["clip_usd"]) if quote_is_usd
                  else float(row["clip_usd"]) / sol_usd if (quote_mint == t.WSOL and sol_usd) else None)
        qty = clip_q / fill_q if clip_q else None
        lp = [dict(zip(("coin_vault", "block_time", "slot", "tx_index"), r)) for r in db.execute(
            "SELECT coin_vault, block_time, slot, tx_index FROM lp_events WHERE mint=?", (mint,))]
        und = [dict(zip(("block_time",), r)) for r in db.execute(
            "SELECT block_time FROM undecoded WHERE mint=?", (mint,))]
        seconds, flags, ticks = build_path(
            swaps, lp, und, primary=primary, t_start=fill_t - 60, t_end=t_end_cap, qty=qty,
            tf=(facts[1], facts[2]) if facts else (0, 0), own_sigs=own,
            sol_usd=None if quote_is_usd else sol_usd, quote_is_usd=quote_is_usd)
        if not facts:
            flags["mint_facts_unknown"] = True
        # The virtual quote is read NOW and applied to every historical swap of the pool. The paid
        # prices must sit inside the spot range it gives (Gamma LOW 2); the result is in the header,
        # and a pool that fails it is already priced on paid prices (reserve_mismatch).
        viol = bracket_violations(swaps)
        flags["bracket_primary"] = viol.get(primary)
        owner_ticks = [dict(zip(("owner", "delta", "decimals", "kind", "block_time"), r)) for r in db.execute(
            "SELECT owner, delta, decimals, kind, block_time FROM ticks WHERE mint=?", (mint,))]
        trades = mirror_seconds(owner_ticks, owners)
        results, memo = [], {}
        for p in policies:
            if "error" in p:
                results.append({"band": p["band"], "censor": "policy_refused", "detail": p["error"]})
                continue
            key = json.dumps([p["policy"].to_dict(), p["copy_arm"]], sort_keys=True, default=str)
            if key not in memo:              # bands that freeze the same rails replay once
                memo[key] = replay(seconds, p["policy"], p["copy_arm"], mint=mint, fill_t=fill_t,
                                   fill_price=fill_q, qty=qty or 1.0, ticks=ticks, source_trades=trades,
                                   cadence_s=cadence_s, exit_lag_s=exit_lag_s)
            r = copy.deepcopy(memo[key])
            if not owners and p["policy"].mirror_on_sell:
                r.setdefault("flags", []).append("mirror_censored_no_address")
            results.append(dict(r, band=p["band"], policy=p["policy"].to_dict(), copy_arm=p["copy_arm"],
                                exit_trial=p["exit_trial"]))
        return {"primary": primary, "primary_how": how, "candidates": cands, "quote_mint": quote_mint,
                "fill_price_quote": fill_q, "qty": qty, "seconds": seconds, "flags": flags,
                "results": results, "swaps": swaps}

    def done(end_slot):
        if not until_exit:
            return False
        t_end = _covered_time(db, mint, end_slot)
        if t_end is None:
            return False
        got = compute(t_end)
        rs = got.get("results") or []
        return bool(rs) and all(r.get("reason") or r.get("censor") == "policy_refused"
                                for r in rs) and all(
            (r.get("landed_t") or 0) <= t_end for r in rs if r.get("reason"))

    covered, used, censor = read_window(db, call, row, lo=lo, hi=hi, page_cap=page_cap - pages,
                                        pool=row.get("pool"), done_fn=done)
    pages += used
    t_end = fill_t + window_s
    if censor or covered < hi:
        t_cov = _covered_time(db, mint, covered)
        t_end = min(t_end, t_cov if t_cov is not None else fill_t)
    got = compute(t_end)
    header = {"kind": "header", "row_id": row["row_id"], "cohort": row["cohort"], "mint": mint,
              "input": row, "release_sha": release_sha, "knobs": knobs, "fill_slot": fill_slot,
              "fill_t": fill_t, "fill_how": how_fill, "window_s": window_s, "slots": [lo, hi],
              "covered_to_slot": covered, "path_end_t": t_end, "pages": pages, "page_cap": page_cap,
              "censor": censor, "calls": {k: v - calls_before.get(k, 0) for k, v in
                                          (getattr(call, "calls", {}) or {}).items()},
              "sol_usd": sol_usd, "sol_usd_source": sol_src, "sol_usd_age_s": sol_age,
              "sol_usd_const": True, "mint_facts": facts, "owners": sorted(owners),
              "limits": ["quote terms primary; price_usd holds one SOL/USD for the whole window",
                         "a refused coin's path has no buy of ours: exits are optimistic by ~one clip impact",
                         "mirror: source sells read from the chain inside the window, no detection lag",
                         "vol_death: the runner's own decision on this tape, armed from the fill (no cohort cutoff, no route witnesses); "
                         "state witness as configured, fed by this path's reserve states; a fire with undecoded txs in its window is censored",
                         "decisions after every swap of the pool, at its place in the second (no lookahead); the live ~1 s sweep can miss sub-second touches",
                         "a paid (trade_avg) price never decides or prices an exit: such replays are censored",
                         "PumpSwap virtual quote read now and applied to history; bracket_primary checks it",
                         "SOL->USDC exit leg not modelled; exits assumed to land"]}
    for k in ("primary", "primary_how", "candidates", "quote_mint", "fill_price_quote", "qty", "flags"):
        header[k] = got.get(k)
    if got.get("censor"):
        header["censor"] = got["censor"]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / ("%s.path.jsonl" % row["row_id"]), "w") as f:
        f.write(json.dumps(header, default=str) + "\n")
        for s in got.get("seconds") or []:
            f.write(json.dumps(dict(s, kind="s")) + "\n")
    with open(out_dir / ("%s.slots.jsonl" % row["row_id"]), "w") as f:
        for s in got.get("swaps") or []:
            f.write(json.dumps({k: s[k] for k in ("slot", "tx_index", "block_time", "signature", "pool",
                                                  "venue", "basis", "coin_delta", "quote_delta", "coin_res",
                                                  "quote_res", "v_quote", "spot", "paid")}, default=str) + "\n")
    records = [dict(r, kind="replay", row_id=row["row_id"], cohort=row["cohort"], mint=mint,
                    path_censor=header["censor"], owners_known=bool(owners), replay_version=REPLAY_VERSION)
               for r in (got.get("results") or [])]
    if not records:
        records = [{"kind": "replay", "row_id": row["row_id"], "cohort": row["cohort"], "mint": mint,
                    "censor": header["censor"], "path_censor": header["censor"],
                    "replay_version": REPLAY_VERSION}]
    return records


# --- the job -----------------------------------------------------------------------------------------

def _state_root():
    env = os.getenv("SNIPER_STATE_ROOT")
    if env:
        return Path(env)
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       cwd=HERE, capture_output=True, text=True, timeout=10)
    return Path(r.stdout.strip()).resolve().parent if r.returncode == 0 else HERE.parents[1]


KNOB_NAMES = ("MEMECOIN_FOLLOW3050_ALL_WALLETS", "MEMECOIN_FOLLOW3050_TP_PCT", "MEMECOIN_FOLLOW3050_STOP_PCT",
              "MEMECOIN_REAL_BAND_POLICY_BANDS", "MEMECOIN_REAL_CATASTROPHE_PCT", "MEMECOIN_REAL_MAX_HOLD_S",
              "MEMECOIN_REAL_STOP_CONFIRM_SECONDS", "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS",
              "MEMECOIN_REAL_TP_CONFIRM_SECONDS", "MEMECOIN_REAL_NO_LOSS_SELL_S",
              "MEMECOIN_REAL_STOP_INSTANT_PCT", "MEMECOIN_REAL_STOP_INSTANT_GRACE_S",
              "MEMECOIN_VOLDEATH_ARMED", "MEMECOIN_VOLDEATH_EXIT_S", "MEMECOIN_VOLDEATH_MOVE_VETO_PCT",
              "MEMECOIN_REAL_MIRROR_SIZE_CHAINS", "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION",
              "MEMECOIN_EXIT_TRIAL_V4_WALLETS", "MEMECOIN_EXIT_TRIAL_V4_FROM", "EXIT_TRIAL_V4_TP_PCT",
              "EXIT_TRIAL_V4_FLOOR_PCT", "EXIT_TRIAL_V4_MAX_HOLD_S", "MEMECOIN_TIMECAP_LIFT_ENABLED",
              "MEMECOIN_FASTLANE_TICK_S")


def configure_from_runner(root):
    """Pin the release and load the runner's config exactly as the runner does (Alpha C2): its
    process env, then .env and .fomo.env first-wins, then the cockpit overlay on the settings module.
    Returns (release sha, ledger read-only connection, knob snapshot). Raises when the runner or the
    release is missing: a replay of a guessed config is not the current policy."""
    global TAPE_DIR
    release = (Path(root) / ".runtime" / "memecoin" / "current").resolve()
    if not (release / "sniper").is_dir():
        raise SystemExit("no pinned release at %s" % release)
    if "sniper" in sys.modules:
        raise SystemExit("sniper was imported before the release was pinned")
    sys.path.insert(0, str(release))
    TAPE_DIR = release / "scripts" / "research"
    from bot.brain_graph import live
    env = live._runner_env(Path(root))
    from sniper.util.envfiles import load_env_files
    load_env_files(str(root), env=env)
    os.environ.clear()
    os.environ.update(env)
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services.memecoin_entry_knobs import apply_overrides
    from sniper.settings import memecoin as cfg
    ledger = get_readonly_connection(str(Path(root) / "sniper_tracker.db"))
    applied = apply_overrides(ledger, cfg)
    if applied.get("deferred"):
        raise SystemExit("cockpit overlay deferred; rerun")
    knobs = {n: (sorted(v) if isinstance(v, (set, frozenset)) else v)
             for n in KNOB_NAMES for v in [getattr(cfg, n, None)]}
    knobs["_overlay_changed"] = applied.get("changed")
    return release.name, ledger, knobs


def run_rows(db, call, ledger, rows, *, out_dir, rep=None, **kw):
    """Run each input row in order. A refused row and a censored row are written and skipped; a
    ReadStop ends the run. {"records": every record written, "summary": counts}."""
    summary = {"rows": 0, "refused": 0, "censored": 0, "stopped": None, "no_owner_rows_by_wallet": {}}
    written = []

    def write(rec):
        written.append(rec)
        if rep is not None:
            rep.write(json.dumps(rec, default=str) + "\n")

    for raw in rows:
        row, bad = parse_row(raw)
        if bad:
            summary["refused"] += 1
            write({"kind": "refused", "why": bad, "line": json.dumps(raw, default=str)[:500]})
            continue
        try:
            records = run_row(db, call, ledger, row, out_dir=out_dir, **kw)
        except RowCensor as exc:
            summary["censored"] += 1
            write({"kind": "censored", "row_id": row["row_id"], "cohort": row["cohort"],
                   "why": str(exc)})
            continue
        except ReadStop as exc:
            summary["stopped"] = {"row_id": row["row_id"], "why": str(exc)}
            break
        for r in records:
            write(r)
        if records and records[0].get("owners_known") is False:
            by = summary["no_owner_rows_by_wallet"]          # Gamma LOW 4
            by[row["source_user_id"]] = by.get(row["source_user_id"], 0) + 1
        if rep is not None:
            rep.flush()
        summary["rows"] += 1
    return {"records": written, "summary": summary}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--input", required=True, help="cohort JSONL from Gamma")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--db", default=None, help="read store (default <state root>/data/research_price_path_1s.db)")
    p.add_argument("--page-cap", type=int, default=PAGE_CAP)
    p.add_argument("--until-exit", action="store_true")
    p.add_argument("--cadence", type=int, default=1)
    p.add_argument("--exit-lag-s", type=float, default=EXIT_LAG_S)
    a = p.parse_args(argv)
    root = _state_root()
    sha, ledger, knobs = configure_from_runner(root)
    from sniper.services.memecoin_research_pacer import SharedPacer
    from sniper.services.memecoin_research_rpc import research_urls
    from sniper.services.memecoin_slot_end_shadow import ResearchRpc, rpc_from_env
    rpc, why = rpc_from_env(os.environ)              # refuses a key that is also a funded key
    if rpc is None:
        raise SystemExit("research key refused: %s" % why)
    url = research_urls(os.environ)[0]
    pacer, flat = SharedPacer.for_url(url), flat_reader(ledger)
    rpc = ResearchRpc(url, pacer=FlatAtSend(pacer, flat))
    call = LowPriorityRpc(rpc, flat_fn=flat, pacer_state_fn=pacer_state_reader(pacer.path))
    db_path = Path(a.db or root / "data" / "research_price_path_1s.db")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(str(db_path) + ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("another run holds %s.lock" % db_path)
        db = connect(db_path)
        with open(a.input) as f, open(out / "replay.jsonl", "a") as rep:
            rows = (json.loads(line) for line in f if line.strip())
            summary = run_rows(db, call, ledger, rows, out_dir=out, rep=rep, page_cap=a.page_cap,
                               until_exit=a.until_exit, cadence_s=a.cadence,
                               exit_lag_s=a.exit_lag_s, release_sha=sha, knobs=knobs)["summary"]
        summary.update(release_sha=sha, knobs=knobs, calls=dict(call.calls))
        print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
