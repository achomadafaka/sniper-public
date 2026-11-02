"""Native float drift: the idle native float and its USD price, sampled for the dashboard.

Operator order 2026-10-08 ~16:35Z (via Alpha): "make it so I can see the live price moves of our
non-USDC tokens". Spec docs/superpowers/specs/2026-10-08-memecoin-native-float-drift-panel-design.md.

DISPLAY ONLY. Nothing here is read by an entry, an exit or a sizing decision, and nothing here reads
a funded seam. Balances come from two places:
- `remember` / `remember_raw`: a value the gas top-up pass (4663, 56) or the slot-lane prefetch
  (Solana) has already read, copied into this module's memory. A dict assignment; never raises.
- `public_balances`: one batched JSON-RPC POST to a chain's independent public read endpoint
  (memecoin_evm_holdings._public_post), every 5 min, for 8453 and 1, and for 4663/56 when the
  recorder has nothing recent.
Prices: Binance public ticker once a minute, and 7 days of 1h klines at startup.

The sampler thread writes data/native_float.db (its own SQLite) and data/native_float.json
(tmp + rename). The web reads those files only.
"""
from __future__ import annotations

import itertools
import json
import logging
import math
import os
import sqlite3
import tempfile
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

# chain -> the native asset of its idle float (wrapped twin read from CHAIN_QUOTE's token)
ASSET_OF_CHAIN = {"solana": "SOL", "4663": "ETH", "8453": "ETH", "1": "ETH", "56": "BNB"}
CHAIN_ID_HEX = {"4663": hex(4663), "8453": "0x2105", "1": "0x1", "56": "0x38"}
SYMBOLS = {"ETH": "ETHUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT"}
BINANCE = "https://api.binance.com/api/v3/"
HTTP_TIMEOUT_S = 6.0
PERIOD_S = 60.0
PUBLIC_PERIOD_S = 300.0
PRICE_STALE_S = 180.0              # older than this: "price unavailable"
RECORDER_MAX_AGE_S = 600.0         # a recorded balance older than this: ask the public endpoint
UNITS_MAX_AGE_S = 900.0            # units OBSERVED longer ago than this: unknown
RECORDER_WAIT_S = 30.0             # the first sample waits this long at most for the SOL recorder
RETENTION_S = 30 * 86400
WINDOWS = {"1h": 3600.0, "24h": 86400.0, "7d": 7 * 86400.0}
SPARK_POINTS = 96

_LAST = {}                         # (chain, kind) -> (units, epoch seconds); one tuple per key


# ------------------------------------------------------------------ the recorder

def remember(chain, kind, units, now=None):
    """Keep the last good balance a funded reader already holds. Never raises, never does I/O."""
    try:
        if isinstance(units, bool) or not isinstance(units, (int, float)):
            return
        value = float(units)
        if not math.isfinite(value) or value < 0 or kind not in ("native", "wrapped", "quote"):
            return
        _LAST[(str(chain), kind)] = (value, float(time.time() if now is None else now))
    except Exception:
        pass


def remember_raw(chain, kind, raw, decimals, now=None):
    """`remember` for an atomic balance; returns `raw` unchanged, so a seam can pass it through."""
    try:
        if raw is not None and not isinstance(raw, bool):
            remember(chain, kind, int(raw) / (10 ** int(decimals)), now=now)
    except Exception:
        pass
    return raw


def recorded_value(chain, kind, now):
    """(value, epoch) of one recorded kind while younger than RECORDER_MAX_AGE_S, else None. The
    "quote" kind is a chain's cash in USD (Solana: the USDC the slot-lane prefetch already reads),
    for the equity change recorder."""
    got = _LAST.get((str(chain), kind))
    return None if got is None or now - got[1] > RECORDER_MAX_AGE_S else got


def recorded(chain, now):
    """{native, wrapped, at} from the recorder when every part is younger than RECORDER_MAX_AGE_S,
    else None. `at` is the older part. Solana records native SOL only: wSOL is not read anywhere,
    so its wrapped part is None (unknown, never a recorded 0; Gamma 2026-10-08)."""
    native, wrapped = _LAST.get((str(chain), "native")), _LAST.get((str(chain), "wrapped"))
    if str(chain) == "solana" and native is not None:
        return None if now - native[1] > RECORDER_MAX_AGE_S else {
            "native": native[0], "wrapped": None, "at": native[1]}
    if native is None or wrapped is None:
        return None
    at = min(native[1], wrapped[1])
    if now - at > RECORDER_MAX_AGE_S:
        return None
    return {"native": native[0], "wrapped": wrapped[0], "at": at}


# ------------------------------------------------------------------ public sources

def http_get_json(url, timeout=HTTP_TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Sniper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _positive(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


def fetch_prices(get=http_get_json):
    """{asset: USD price} from one Binance ticker call; a failed or bad price is absent."""
    symbols = json.dumps(sorted(SYMBOLS.values()), separators=(",", ":"))
    try:
        rows = get(BINANCE + "ticker/price?symbols=" + urllib.request.quote(symbols), timeout=HTTP_TIMEOUT_S)
    except Exception as exc:
        logger.warning("native float: Binance ticker failed: %s: %s", type(exc).__name__, exc)
        return {}
    by_symbol = {}
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict):
            by_symbol[row.get("symbol")] = _positive(row.get("price"))
    return {asset: by_symbol[sym] for asset, sym in SYMBOLS.items() if by_symbol.get(sym)}


def fetch_klines(asset, get=http_get_json):
    """[(epoch, price)] for the last 7 days of 1h candles, or [] on failure: each candle's OPEN price
    at its open time. Binance's row is [open time, open, high, low, close, volume, close time, ...]
    (verified live 2026-10-08); the close belongs to the close time, an hour later, and the running
    candle's close is the current price."""
    try:
        rows = get(BINANCE + "klines?symbol=%s&interval=1h&limit=168" % SYMBOLS[asset], timeout=HTTP_TIMEOUT_S)
        out = []
        for row in rows:
            price = _positive(row[1])
            if price:
                out.append((int(row[0]) / 1000.0, price))
        return out
    except Exception as exc:
        logger.warning("native float: Binance klines %s failed: %s: %s", asset, type(exc).__name__, exc)
        return []


def public_balances(chain, address, post=None):
    """(native_units, wrapped_units) from one batched call to the chain's public read endpoint,
    with an eth_chainId proof in the same batch, or None (unknown, never zero)."""
    try:
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        token = CHAIN_QUOTE[str(chain)]["token"]
        decimals = int(CHAIN_QUOTE[str(chain)].get("decimals", 18))
        if post is None:
            return None
        data = "0x70a08231" + str(address).lower().replace("0x", "").rjust(64, "0")
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
                 {"jsonrpc": "2.0", "id": 2, "method": "eth_getBalance", "params": [address, "latest"]},
                 {"jsonrpc": "2.0", "id": 3, "method": "eth_call",
                  "params": [{"to": token, "data": data}, "latest"]}]
        answers = {a.get("id"): a.get("result") for a in post(batch) if isinstance(a, dict)}
        if str(answers.get(1) or "").lower() != CHAIN_ID_HEX[str(chain)].lower():
            logger.warning("native float: chain %s public endpoint answered chain %r", chain, answers.get(1))
            return None
        native, wrapped = int(answers[2], 16), int(answers[3], 16)
        return native / 1e18, wrapped / (10 ** decimals)
    except Exception as exc:
        logger.warning("native float: public balance read chain=%s failed: %s: %s",
                       chain, type(exc).__name__, exc)
        return None


# ------------------------------------------------------------------ storage

class Store:
    """data/native_float.db: prices and units, one connection, this thread only."""

    def __init__(self, path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS native_float_prices(ts REAL NOT NULL, asset TEXT NOT NULL,"
            " price_usd REAL NOT NULL, source TEXT, PRIMARY KEY(asset, ts));"
            "CREATE TABLE IF NOT EXISTS native_float_units(ts REAL NOT NULL, chain TEXT NOT NULL,"
            " asset TEXT NOT NULL, native_units REAL, wrapped_units REAL, source TEXT,"
            " PRIMARY KEY(chain, ts));")
        # SF07 (review 2026-10-08): when the balance was READ, apart from the sample that stored it.
        # A row written before this column reads its sample time.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(native_float_units)")}
        if "observed_at" not in columns:
            self.db.execute("ALTER TABLE native_float_units ADD COLUMN observed_at REAL")
            self.db.commit()
        # Once, by its own schema version (Gamma LOW; live 2026-10-08 the column existed and the purge
        # had never run): klines stored before the open-price fix sit an hour early; dropping them lets
        # the startup backfill read them again.
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:
            self.db.execute("DELETE FROM native_float_prices WHERE source='klines'")
            self.db.execute("PRAGMA user_version = 1")
            self.db.commit()

    def add_prices(self, ts, prices, source):
        self.db.executemany("INSERT OR REPLACE INTO native_float_prices VALUES (?,?,?,?)",
                            [(float(ts), a, float(p), source) for a, p in prices.items()])
        self.db.commit()

    def add_units(self, ts, chain, asset, native, wrapped, source, observed_at=None):
        self.db.execute("INSERT OR REPLACE INTO native_float_units"
                        "(ts, chain, asset, native_units, wrapped_units, source, observed_at) VALUES (?,?,?,?,?,?,?)",
                        (float(ts), chain, asset, native, wrapped, source,
                         float(ts if observed_at is None else observed_at)))
        self.db.commit()

    def prices_since(self, asset, since):
        return [(float(t), float(p)) for t, p in self.db.execute(
            "SELECT ts, price_usd FROM native_float_prices WHERE asset=? AND ts>=? ORDER BY ts",
            (asset, float(since)))]

    def units_since(self, asset, since):
        """[(ts, total units of `asset`, oldest observation)] at each sample from `since`. Each chain's
        last read is carried forward up to UNITS_MAX_AGE_S after it was OBSERVED (SF07: never after
        the sample that copied it): 8453 and 1 are read every 5 min, 4663 every minute, and a
        per-timestamp sum dropped them on four samples of five (2026-10-08). The total is None
        (unknown, never a smaller sum) while any of the asset's chains has no read within
        UNITS_MAX_AGE_S, including before its first read."""
        expected = [c for c, a in ASSET_OF_CHAIN.items() if a == asset]
        rows = self.db.execute(
            "SELECT ts, chain, COALESCE(native_units,0)+COALESCE(wrapped_units,0), COALESCE(observed_at, ts) "
            "FROM native_float_units WHERE asset=? AND ts>=? AND native_units IS NOT NULL ORDER BY ts",
            (asset, float(since) - UNITS_MAX_AGE_S)).fetchall()
        last, out = {}, []
        for ts, group in itertools.groupby(rows, key=lambda row: float(row[0])):
            for _ts, chain, units, seen in group:
                last[chain] = (float(seen), float(units))
            if ts >= since:
                fresh = [last[c] for c in expected if c in last and ts - last[c][0] <= UNITS_MAX_AGE_S]
                out.append((ts, sum(u for _seen, u in fresh), min(seen for seen, _u in fresh))
                           if len(fresh) == len(expected) else (ts, None, None))
        return out

    def last_units(self, chain):
        row = self.db.execute("SELECT COALESCE(observed_at, ts), native_units, wrapped_units FROM native_float_units "
                              "WHERE chain=? AND native_units IS NOT NULL ORDER BY ts DESC LIMIT 1",
                              (chain,)).fetchone()
        return None if row is None else {"at": float(row[0]), "native": row[1], "wrapped": row[2]}

    def last_price(self, asset):
        row = self.db.execute("SELECT ts, price_usd FROM native_float_prices WHERE asset=? "
                              "ORDER BY ts DESC LIMIT 1", (asset,)).fetchone()
        return None if row is None else (float(row[0]), float(row[1]))

    def has_prices(self, asset, since):
        return self.db.execute("SELECT 1 FROM native_float_prices WHERE asset=? AND ts<=? LIMIT 1",
                               (asset, float(since))).fetchone() is not None

    def prune(self, now):
        cut = float(now) - RETENTION_S
        self.db.execute("DELETE FROM native_float_prices WHERE ts<?", (cut,))
        self.db.execute("DELETE FROM native_float_units WHERE ts<?", (cut,))
        self.db.commit()


# ------------------------------------------------------------------ drift math

def drift(prices, units, t0, t1):
    """USD drift from price alone over [t0, t1]: sum over consecutive price samples of the units
    held at the earlier sample x the price move. Units before the first unit sample are the earliest
    known units. `units` rows are (ts, units) or units_since's (ts, units, oldest observation). None
    when there are not two prices or no units, or when a step's units are unknown or were observed
    longer than UNITS_MAX_AGE_S before it (a whole-window figure is never built from the steps that
    happen to be known)."""
    pts = [(t, p) for t, p in prices if t0 <= t <= t1]
    if len(pts) < 2 or not units:
        return None
    units = sorted(units)
    # Price history can be sparse after an outage; check observation coverage between its points.
    for row, following in zip(units, units[1:] + [(pts[-1][0],)]):
        end = min(following[0], pts[-1][0])
        if end <= max(row[0], pts[0][0]):
            continue
        seen = row[2] if len(row) > 2 else row[0]
        if row[1] is None or end - seen > UNITS_MAX_AGE_S:
            return None
    total, j = 0.0, 0
    for (ta, pa), (_tb, pb) in zip(pts, pts[1:]):
        while j + 1 < len(units) and units[j + 1][0] <= ta:
            j += 1
        # A whole-asset stall writes no row at all, so units_since cannot mark it: past the age
        # limit the last units are unknown (Gamma 2026-10-08). Before the first row is the backfill.
        held, seen = units[j][1], (units[j][2] if len(units[j]) > 2 else units[j][0])
        if held is None or (units[j][0] <= ta and ta - seen > UNITS_MAX_AGE_S):
            return None
        total += held * (pb - pa)
    return total


def _downsample(points, limit=SPARK_POINTS):
    """At most `limit` [t, price] points, evenly spaced, always ending on the newest sample."""
    if not points:
        return []
    step = max(1, math.ceil(len(points) / limit))
    out = [[t, p] for t, p in points[::step]]
    if out[-1][0] != points[-1][0]:
        out.append(list(points[-1]))
    return out


def _price_at(prices, at):
    """The first price sample at or after `at`, or None."""
    for t, p in prices:
        if t >= at:
            return t, p
    return None


# ------------------------------------------------------------------ the sample

def write_json_atomic(path, payload):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-native-float-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_snapshot(path, now=None):
    """The web's view of the sampler snapshot: `available`, `age_s`, `stale`. Older than
    PRICE_STALE_S, or a failed price source, is stale; a stale snapshot never carries a live price
    (price_usd and value_usd become None; last_price_usd keeps the old one with its age)."""
    now = time.time() if now is None else now
    try:
        with open(path) as fh:
            snap = json.load(fh)
        ts = float(snap["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"available": False, "stale": True, "age_s": None}
    age = max(0.0, now - ts)
    old = age > PRICE_STALE_S
    for asset in (snap.get("assets") or {}).values():
        if old:
            if asset.get("price_usd") is not None:
                asset["last_price_usd"] = asset["price_usd"]
            if asset.get("last_price_age_s") is not None:
                asset["last_price_age_s"] = float(asset["last_price_age_s"]) + age
            # Every figure built on that price goes too: an old drift is not today's drift.
            asset["price_usd"] = asset["value_usd"] = None
            asset["change"] = {k: None for k in (asset.get("change") or {})}
    if old:
        snap["headline"] = {k: None for k in (snap.get("headline") or {})}
    return dict(snap, available=True, age_s=age, stale=bool(old or not snap.get("price_ok")))


def _chain_units(store, chain, now, *, post_for, evm_address, public_due):
    """(units dict or None, source). The recorder first; for an EVM chain the public endpoint when
    due or when the recorder is silent; else the last stored units while young enough."""
    got = recorded(chain, now)
    if got is not None:
        return got, "recorder"
    if chain != "solana" and evm_address and post_for is not None:
        stored = store.last_units(chain)
        if public_due or stored is None or now - stored["at"] > PUBLIC_PERIOD_S:
            post = post_for(chain)
            read = public_balances(chain, evm_address, post=post) if post is not None else None
            if read is not None:
                return {"native": read[0], "wrapped": read[1], "at": now}, "public"
    stored = store.last_units(chain)
    if stored is not None and now - stored["at"] <= UNITS_MAX_AGE_S:
        return stored, "stored"
    return None, None


def sample_once(store, *, now, get, post_for, evm_address, json_path, public_due):
    """One sample: prices, units, the store, the snapshot JSON. Returns the snapshot, or None after
    logging the fault. Never raises into the runner."""
    try:
        prices = fetch_prices(get=get)
        if prices:
            store.add_prices(now, prices, "ticker")
        chains = []
        for chain, asset in ASSET_OF_CHAIN.items():
            units, source = _chain_units(store, chain, now, post_for=post_for,
                                         evm_address=evm_address, public_due=public_due)
            if units is not None and source in ("recorder", "public"):
                store.add_units(now, chain, asset, units["native"], units["wrapped"], source,
                                observed_at=units["at"])
            chains.append({"chain": chain, "asset": asset,
                           "native": None if units is None else units["native"],
                           "wrapped": None if units is None else units["wrapped"],
                           "units": None if units is None else units["native"] + (units["wrapped"] or 0.0),
                           "age_s": None if units is None else now - units["at"], "source": source})
        store.prune(now)
        snapshot = build_snapshot(store, now, prices=prices, chains=chains)
        write_json_atomic(json_path, snapshot)
        return snapshot
    except Exception:
        logger.exception("native float: sample failed (display only; the runner is unaffected)")
        return None


def build_snapshot(store, now, *, prices, chains):
    assets, headline = {}, {k: {"usd": 0.0, "known": [], "missing": []} for k in WINDOWS}
    partial = any(c["units"] is None for c in chains)
    for asset in SYMBOLS:
        mine = [c for c in chains if c["asset"] == asset]
        known = [c for c in mine if c["units"] is not None]
        units = sum(c["units"] for c in known) if known else None
        price = prices.get(asset)
        last = store.last_price(asset)
        history = store.prices_since(asset, now - WINDOWS["7d"] - 3600)
        unit_rows = store.units_since(asset, now - WINDOWS["7d"] - 3600) or (
            [(now, units)] if units is not None else [])
        change = {}
        for name, span in WINDOWS.items():
            start = now - span
            if price is None:
                change[name] = None
                headline[name]["missing"].append(asset)
                continue
            then = _price_at(history, start)
            usd = drift(history, unit_rows, start, now)
            change[name] = None if then is None or then[0] >= now else {
                "from_at": then[0], "from_price": then[1],
                "pct": 100.0 * (price / then[1] - 1.0),
                "usd": usd}
            if usd is not None:
                headline[name]["usd"] += usd
                headline[name]["known"].append(asset)
            else:
                headline[name]["missing"].append(asset)
        spark = {}
        for name, span in WINDOWS.items():
            spark[name] = _downsample([x for x in history if x[0] >= now - span])
        assets[asset] = {
            "price_usd": price, "units": units,
            "value_usd": None if (price is None or units is None) else units * price,
            "last_price_usd": None if last is None else last[1],
            "last_price_age_s": None if last is None else now - last[0],
            "chains": mine, "change": change, "sparkline": spark,
            "units_backfilled_from": unit_rows[0][0] if unit_rows else None}
    return {"ts": now, "price_ok": all(a in prices for a in SYMBOLS), "partial": partial,
            "assets": assets,
            # An asset whose drift is unknown is named, never silently left out of the sum (Gamma LOW).
            "headline": {k: ({"usd": v["usd"], "partial": bool(v["missing"]), "missing": v["missing"]}
                             if v["known"] else None) for k, v in headline.items()},
            "price_source": "binance",
            "periods": {"sample_s": PERIOD_S, "public_s": PUBLIC_PERIOD_S}}


# ------------------------------------------------------------------ the thread

# Chain 1 has no chain-owner public endpoint in the registry and _public_post reads only the optional
# MEMECOIN_ONCHAIN_EVM_RPC_URL_1. This panel then reads chain 1 from a KEYLESS public endpoint (Gamma
# 2026-10-08: never a keyed research account outside its pacer, never custody, never the Flashbots
# submission endpoint): one batched POST every PUBLIC_PERIOD_S, eth_chainId-proved like the others.
# Display only; it is not added to the free-endpoint registry, which funded submission reads.
CHAIN1_PUBLIC_RPC = "https://ethereum-rpc.publicnode.com"


def _https_post(url):
    def post(body):
        # publicnode refuses Python's default User-Agent (memecoin_evm_post_trade.read_host notes it).
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
            "User-Agent": "curl/8.5.0", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.load(resp)
    return post


def public_post_for(chain):
    """The chain's independent public read endpoint (memecoin_evm_holdings._public_post); for chain 1
    without one, the keyless CHAIN1_PUBLIC_RPC; else None. Never the custody pool or a keyed account."""
    try:
        from sniper.services.memecoin_evm_holdings import _public_post
        return _public_post(str(chain))
    except Exception as exc:
        if str(chain) == "1":
            return _https_post(CHAIN1_PUBLIC_RPC)
        logger.warning("native float: no public read endpoint for chain %s: %s", chain, exc)
        return None


def start_sampler(*, evm_address, state_path_fn=None, get=http_get_json, post_for=public_post_for,
                  sleep=time.sleep, clock=time.time):
    """Start the daemon sampler, or None when MEMECOIN_NATIVE_FLOAT_SAMPLER is off. Display only;
    it never raises into the runner and takes no lock the fill path takes."""
    from sniper.settings import memecoin as cfg
    if not getattr(cfg, "MEMECOIN_NATIVE_FLOAT_SAMPLER", True):
        return None
    if state_path_fn is None:
        from sniper.util.state_paths import state_path as state_path_fn

    def run():
        try:
            store = Store(state_path_fn("data", "native_float.db"))
            for asset in SYMBOLS:
                last = store.last_price(asset)
                if (last is None or clock() - last[0] > PRICE_STALE_S
                        or not store.has_prices(asset, clock() - WINDOWS["7d"] + 3600)):
                    rows = fetch_klines(asset, get=get)
                    for ts, price in rows:
                        store.add_prices(ts, {asset: price}, "klines")
        except Exception:
            logger.exception("native float: sampler start failed (display only)")
            return
        # THE RESTART GAP (Gamma 2026-10-08): the slot-lane prefetch records SOL seconds after the
        # runner starts, and a first sample without it left the pre-restart row in force, so every
        # window spanning a long outage read unknown. Solana has no public fallback: wait, bounded.
        waited = 0.0
        while recorded("solana", clock()) is None and waited < RECORDER_WAIT_S:
            sleep(1.0)
            waited += 1.0
        # THE EQUITY CHANGE RECORDER (operator 2026-10-08) rides this thread: after each sample it
        # composes total equity from the same minute (memecoin_equity_change). Display only.
        equity = None
        try:
            if getattr(cfg, "MEMECOIN_EQUITY_RECORDER", True):
                from sniper.services.memecoin_equity_change import EquityRecorder
                equity = EquityRecorder(state_path_fn=state_path_fn, evm_address=evm_address,
                                        post_for=post_for)
        except Exception:
            logger.warning("equity change: recorder did not start (display only)", exc_info=True)
        last_public = 0.0
        while True:
            now = clock()
            due = now - last_public >= PUBLIC_PERIOD_S
            snapshot = sample_once(store, now=now, get=get, post_for=post_for, evm_address=evm_address,
                                   json_path=state_path_fn("data", "native_float.json"), public_due=due)
            if equity is not None:
                equity.sample(store, snapshot, now)
            if due:
                last_public = now
            sleep(PERIOD_S)

    thread = threading.Thread(target=run, name="native-float-sampler", daemon=True)
    thread.start()
    return thread
