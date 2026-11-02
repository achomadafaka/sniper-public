"""Relay option A (spec 2026-10-09 relay deposit proof on the on-chain lane): the store of signed Solana
deposits that crosses processes.

The deposit socket (memecoin_rh_deposit_proof, option C's frame proof, unchanged) writes one row per order
id. A lane that sees the fill takes the row: the same fill tx may take it again (another lane, a
restart), a second fill tx never does. Option C's rules hold at take time, and two more apply: the
registry is read again (a conflict set after the frame arrived revokes the row), and a depositor that
funded fills of SHARED_AT_UIDS or more source users is never a proof. At exactly two the registry owner
keeps its proof and the pair pages once (version b, Alpha 2026-10-09). The key is source_user_id, never
a handle: one user can carry two (d47559fc is NotARandomUser in the feed, randomuser123 in
memecoin_wallets).

WAL with synchronous=NORMAL: a take is one indexed statement (p90 0.094 ms measured). A power loss can
lose the last claim, but the claim cannot rebind: a restart outlasts MAX_FILL_LAG_S and the fill tx is
unique. Any SQLite failure is `store_error`, which callers treat as no proof (the Relay path runs).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from fractions import Fraction

logger = logging.getLogger(__name__)

STORE_TTL_S = 600.0
MAX_FILL_LAG_S = 120.0                 # _verify_relay's origin-to-destination bound
AMOUNT_BAND = (Fraction(98, 100), Fraction(1))
SHARED_AT_UIDS = 3                     # version (b): shared at 3 source users, a page at 2
PAGE_AT_UIDS = 2
_USDC_DECIMALS = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS deposits (
    order_id TEXT PRIMARY KEY, signature TEXT NOT NULL, depositor TEXT NOT NULL, uids_json TEXT NOT NULL,
    amount_atomic INTEGER NOT NULL, slot INTEGER NOT NULL, arrived REAL NOT NULL,
    duplicate INTEGER NOT NULL DEFAULT 0, used_fill_tx TEXT, used_at REAL, used_info TEXT,
    confirm_status TEXT);
CREATE TABLE IF NOT EXISTS funders (
    depositor TEXT NOT NULL, source_user_id TEXT NOT NULL, first_seen REAL NOT NULL,
    PRIMARY KEY (depositor, source_user_id));
CREATE TABLE IF NOT EXISTS funder_pages (
    depositor TEXT NOT NULL, pair TEXT NOT NULL, paged_at REAL NOT NULL, PRIMARY KEY (depositor, pair));
"""


class DepositStore:
    """One SQLite file shared by the socket (writer of deposits) and every lane (takers)."""

    def __init__(self, path, *, clock=time.time, busy_ms=50, page_fn=None):
        self.path, self.clock, self.busy_ms, self.page_fn = str(path), clock, int(busy_ms), page_fn
        self._local = threading.local()
        conn = self._conn()
        conn.executescript(SCHEMA)

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=self.busy_ms / 1000.0, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=%d" % self.busy_ms)
            self._local.conn = conn
        return conn

    def put(self, proof):
        """Store one frame proof (deposit_from_frame's dict). The same signature again is one deposit;
        a second signed deposit on the order id marks it duplicate, and neither then binds a fill."""
        conn = self._conn()
        row = conn.execute("SELECT signature FROM deposits WHERE order_id=?", (proof["order_id"],)).fetchone()
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO deposits (order_id, signature, depositor, uids_json, amount_atomic, "
                "slot, arrived) VALUES (?,?,?,?,?,?,?)",
                (proof["order_id"], proof["signature"], proof["depositor"], json.dumps(list(proof["uids"])),
                 int(proof["amount_atomic"]), int(proof["slot"]), float(proof["arrived"])))
        elif row[0] != proof["signature"]:
            conn.execute("UPDATE deposits SET duplicate=1 WHERE order_id=?", (proof["order_id"],))

    def note_funder(self, depositor, source_user_id, now=None):
        """One observed (depositor, source user) pair: from a Relay answer or a taken deposit."""
        if not depositor or not source_user_id:
            return
        self._conn().execute("INSERT OR IGNORE INTO funders VALUES (?,?,?)",
                             (str(depositor), str(source_user_id), self.clock() if now is None else now))

    def funder_count(self, depositor, *, with_uid=None):
        """Distinct source users the depositor is seen funding, counting `with_uid` too."""
        uids = {r[0] for r in self._conn().execute(
            "SELECT source_user_id FROM funders WHERE depositor=?", (str(depositor),))}
        if with_uid:
            uids.add(str(with_uid))
        return len(uids)

    def funders(self, depositor):
        return sorted(r[0] for r in self._conn().execute(
            "SELECT source_user_id FROM funders WHERE depositor=?", (str(depositor),)))

    def shared(self, depositor, source_user_id=None):
        """True when the depositor funds SHARED_AT_UIDS or more source users (with this one). At
        PAGE_AT_UIDS it pages once per pair and is not shared."""
        n = self.funder_count(depositor, with_uid=source_user_id)
        if n >= SHARED_AT_UIDS:
            return True
        if n >= PAGE_AT_UIDS:
            self._page_pair(depositor, sorted(set(self.funders(depositor)) | {str(source_user_id)}))
        return False

    def _page_pair(self, depositor, uids):
        pair = ",".join(uids)
        cur = self._conn().execute("INSERT OR IGNORE INTO funder_pages VALUES (?,?,?)",
                                   (str(depositor), pair, self.clock()))
        if cur.rowcount and self.page_fn is not None:
            try:
                self.page_fn("Relay depositor %s funds two source users (%s): registry review. Neither is "
                             "refused by the shared-depositor rule; a third user makes it shared."
                             % (depositor, pair))
            except Exception:
                logger.exception("relay deposit store: page failed for %s", depositor)

    def take(self, *, order_id, fill_tx, source_user_id, fill_usd, now, registry_ok, fill_info=None):
        """(proof, "matched") or (None, reason). `registry_ok(depositor, uid)` re-reads the registry;
        `fill_info` (chain, wallet, mint) rides the claim to the confirmer."""
        try:
            return self._take(str(order_id or "").lower(), str(fill_tx or "").lower(), source_user_id,
                              fill_usd, now, registry_ok, fill_info)
        except sqlite3.Error:
            logger.warning("relay deposit store: take failed for %s", order_id, exc_info=True)
            return None, "store_error"

    def claim(self, order_id, fill_tx, now, *, source_user_id=None, depositor=None, fill_info=None):
        """True when this fill tx now holds the order id (or already did). The fast lane's own checks
        ran on its frame; this is only the single use that crosses processes."""
        order_id, fill_tx = str(order_id or "").lower(), str(fill_tx or "").lower()
        claimed = self._conn().execute(
            "UPDATE deposits SET used_fill_tx=?, used_at=coalesce(used_at, ?), used_info=coalesce(used_info, ?) "
            "WHERE order_id=? AND duplicate=0 AND (used_fill_tx IS NULL OR used_fill_tx=?)",
            (fill_tx, now, json.dumps(fill_info or {}), order_id, fill_tx)).rowcount
        if claimed:
            self.note_funder(depositor, source_user_id, now)
        return bool(claimed)

    def unconfirmed(self, limit=256):
        """Claimed deposits the confirmer has not finished: (order_id, signature, depositor, uid,
        amount, arrived, used_fill_tx, used_info, margin_ms)."""
        out = []
        for order_id, sig, depositor, uids_json, amount, arrived, fill_tx, info, used_at in self._conn().execute(
                "SELECT order_id, signature, depositor, uids_json, amount_atomic, arrived, used_fill_tx, "
                "used_info, used_at FROM deposits WHERE used_fill_tx IS NOT NULL AND confirm_status IS NULL "
                "ORDER BY arrived LIMIT ?", (int(limit),)):
            out.append(dict(order_id=order_id, origin_tx=sig, origin_wallet=depositor, uids=json.loads(uids_json),
                            origin_amount_atomic=str(amount), origin_arrived=arrived, fill_tx=fill_tx,
                            fill_info=json.loads(info or "{}"), margin_ms=(used_at - arrived) * 1000.0))
        return out

    def mark_confirmed(self, origin_tx, status):
        self._conn().execute("UPDATE deposits SET confirm_status=? WHERE signature=? AND confirm_status IS NULL",
                             (str(status), str(origin_tx)))

    def _take(self, order_id, fill_tx, uid, fill_usd, now, registry_ok, fill_info=None):
        conn = self._conn()
        row = conn.execute(
            "SELECT signature, depositor, uids_json, amount_atomic, slot, arrived, duplicate, used_fill_tx "
            "FROM deposits WHERE order_id=?", (order_id,)).fetchone() if order_id else None
        if row is None:
            return None, "no_deposit"
        signature, depositor, uids_json, amount, slot, arrived, duplicate, used = row
        if duplicate:
            return None, "duplicate_order"
        if used is not None and used != fill_tx:
            return None, "order_reused"
        if not 0 <= now - arrived <= MAX_FILL_LAG_S:
            return None, "stale"
        if uid not in json.loads(uids_json):
            return None, "uid_mismatch"
        try:
            ratio = Fraction(str(fill_usd)) / Fraction(int(amount), 10 ** _USDC_DECIMALS)
        except (TypeError, ValueError, ZeroDivisionError):
            return None, "amount_band"
        if not AMOUNT_BAND[0] <= ratio <= AMOUNT_BAND[1]:
            return None, "amount_band"
        if not registry_ok(depositor, uid):
            return None, "registry_revoked"
        if self.shared(depositor, uid):
            self.note_funder(depositor, uid, now)        # an observation either way; only ever stricter
            return None, "shared_depositor"
        if not self.claim(order_id, fill_tx, now, source_user_id=uid, depositor=depositor,
                          fill_info=dict(fill_info or {}, source_user_id=uid)):
            return None, "order_reused"
        return dict(order_id=order_id, origin_tx=signature, origin_wallet=depositor, origin_slot=int(slot),
                    origin_amount_atomic=str(int(amount)), origin_identity_user_id=uid,
                    origin_arrived=arrived, margin_ms=(now - arrived) * 1000.0), "matched"

    def prune(self, now=None):
        now = self.clock() if now is None else now
        # A claimed deposit is kept until its confirmation finished (give-up 90 s < TTL 600 s).
        self._conn().execute("DELETE FROM deposits WHERE arrived < ? AND (used_fill_tx IS NULL "
                             "OR confirm_status IS NOT NULL)", (now - STORE_TTL_S,))


def prove_from_deposit(row, transaction, *, chain, store, registry_ok, now):
    """(the row with a relay-deposit-processed-v1 source_initiation, "matched") or (None, reason) for a
    watcher Relay row. The order id is the fill calldata's last 32 bytes (the hint WatcherVerifier
    asks Relay with); the fill's USD is the watcher's own valuation of it. The receipt's facts
    (tx, block, mint, beneficiary, payer, acquired) are kept as read; new dictionaries only."""
    from sniper.services.memecoin_source_initiation import RELAY_DEPOSIT_PROOF_VERSION
    buy = (row or {}).get("buy") or {}
    proof = buy.get("source_initiation") or {}
    data = str((transaction or {}).get("input") or "")
    if len(data) < 66:
        return None, "no_order_id"
    got, status = store.take(
        order_id="0x" + data[-64:].lower(), fill_tx=proof.get("tx_hash") or row.get("tx_hash"),
        source_user_id=row.get("source_user_id"), fill_usd=buy.get("source_size_usd"), now=now,
        registry_ok=registry_ok,
        fill_info={"chain": str(chain), "wallet": proof.get("beneficiary"), "mint": proof.get("mint")})
    if got is None:
        return None, status
    proved = dict(proof, proof_version=RELAY_DEPOSIT_PROOF_VERSION, origin_network_id="solana",
                  origin_wallet=got["origin_wallet"], origin_tx=got["origin_tx"], origin_slot=got["origin_slot"],
                  origin_amount_atomic=got["origin_amount_atomic"], order_id=got["order_id"],
                  origin_identity_user_id=got["origin_identity_user_id"])
    return dict(row, buy=dict(buy, source_initiation=proved)), "matched"


def registry_reader(ledger_path):
    """registry_ok(depositor, uid): the depositor is still a verified, conflict-free Solana identity of
    the uid, read now on a short read-only connection (a conflict set after the frame revokes it)."""
    def ok(depositor, uid):
        from sniper.persistence.db_core import get_readonly_connection
        conn = get_readonly_connection(str(ledger_path))
        try:
            conn.execute("PRAGMA busy_timeout=50")
            return conn.execute(
                "SELECT 1 FROM memecoin_address_registry WHERE source_user_id=? AND network_id='solana' "
                "AND address=? AND status='verified' AND conflict_at IS NULL", (uid, depositor)).fetchone() is not None
        finally:
            conn.close()
    return ok


def deposit_check(store, *, chain, registry_ok, now_fn=time.time):
    """WatcherVerifier's deposit_fn: the item with its row proved from a stored deposit, or None."""
    def check(item):
        proved, _status = prove_from_deposit(item["row"], item.get("transaction"), chain=chain, store=store,
                                             registry_ok=registry_ok, now=now_fn())
        return None if proved is None else dict(item, row=proved)
    return check
