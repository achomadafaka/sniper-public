"""The ADD-ON REFUSAL (operator 2026-10-17, 'Fix it, it happened again' / 'At least for him').

notdecu (pump:4vw54Bm...) bought ~$305 three times today; each time our slot send failed
BuySlippageBelowMinTokensOut and the backup refused the +41..43% chase. 15-22 s later his ~$25.7
add-on was a clean fill for us, and each lost about 30% within 30 s (#73925 -30.21, #73928 -32.41,
#73929 -30.04). The premium cap judged the add-on's own price; the floor judged his net.

RULE, for a buy by a wallet in MEMECOIN_REAL_ADDON_REFUSE_WALLET_IDS ('*' = every wallet; absent =
off), with MEMECOIN_REAL_ADDON_REFUSE on: when his prior net bought into this (chain, coin) inside
MEMECOIN_REAL_ADDON_WINDOW_S exceeds the buy (the buy is under MEMECOIN_REAL_ADDON_MAX_SHARE_PCT of
it; 100 = any smaller add-on) and we hold no open position and no live reservation in the coin,
refuse 'addon_after_missed_entry prior_net=$.. addon=$.. share=..% first=..'. share is the add-on's
share of his net including it (the label's addon_share_pct).

FILL PATH (rule 2): the prior net comes from MEMORY, the AddonRing here, never a ledger read. Every
buy each admission sees is noted after it is judged (the slot lane after admission_refusal priced
it, the runner hook at its start), keyed by its transaction, so the same trade seen by the slot lane,
the on-chain lane and J7 counts once; a report without a transaction (J7's 'swap:<uuid>') is not
noted. A sell by the wallet of the coin clears his entry (his net is then unknown, so the next buy
passes). Our own holding is read from the ledger ONLY when the ring says refuse (a scoped wallet's
add-on), the source-buy floor's cold-path pattern. Restart = empty ring = every buy passes, as today.

ABSENT IS NOT ZERO: no ring entry = no known prior net = not an add-on = pass. An unknown judged size
passes too. Bounded: MAX_KEYS (wallet, chain, coin) keys, the oldest dropped first; entries older
than the window are dropped on every note.
Not handled: a first buy only J7's tx-less report carried (not noted: the add-on passes), and sells
the runner never sees for a wallet (his net stays as noted until the window passes)."""
from __future__ import annotations

import collections
import logging
import threading
import time

logger = logging.getLogger(__name__)

REASON = "addon_after_missed_entry"
MAX_KEYS = 20_000
PER_KEY = 16


def _cfg():
    from sniper.settings import memecoin as cfg
    return cfg


def scoped(source_user_id):
    """Whether the refusal applies to this wallet (knob on and the wallet listed, or '*')."""
    cfg = _cfg()
    if not getattr(cfg, "MEMECOIN_REAL_ADDON_REFUSE", False):
        return False
    ids = getattr(cfg, "MEMECOIN_REAL_ADDON_REFUSE_WALLET_IDS", ()) or ()
    return "*" in ids or (source_user_id is not None and str(source_user_id) in ids)


def _key_tx(tx):
    return None if not tx else (str(tx).lower() if str(tx).startswith("0x") else str(tx))


class AddonRing:
    """(network, wallet, coin) -> ((tx, usd, price, epoch), ...). Writers share one lock; readers
    take none (each key's tuple and the mapping are replaced whole)."""

    def __init__(self, *, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._keys = collections.OrderedDict()
        self.stats = dict(noted=0, cleared=0, refused=0, passed_held=0, held_read_errors=0, evicted=0)

    @staticmethod
    def key(network_id, source_user_id, mint):
        mint = str(mint or "")
        return (str(network_id or ""), str(source_user_id or ""), mint.lower() if mint.startswith("0x") else mint)

    def note_buy(self, network_id, source_user_id, mint, tx, usd, price):
        tx = _key_tx(tx)
        if not tx or not source_user_id or not mint:
            return
        try:
            usd = float(usd)
        except (TypeError, ValueError):
            return
        if not usd > 0:
            return
        now, window = self.clock(), float(getattr(_cfg(), "MEMECOIN_REAL_ADDON_WINDOW_S", 600.0) or 0)
        key = self.key(network_id, source_user_id, mint)
        with self._lock:
            old = tuple(e for e in self._keys.get(key, ()) if now - e[3] <= window and e[0] != tx)
            self._keys.pop(key, None)
            self._keys[key] = (old + ((tx, usd, price, now),))[-PER_KEY:]
            while len(self._keys) > MAX_KEYS:
                self._keys.popitem(last=False)
                self.stats["evicted"] += 1
            self.stats["noted"] += 1

    def note_sell(self, network_id, source_user_id, mint):
        key = self.key(network_id, source_user_id, mint)
        with self._lock:
            if self._keys.pop(key, None) is not None:
                self.stats["cleared"] += 1

    def note_tx_failed(self, tx):
        """Drop a transaction that failed or never landed (LandingWatch's source_failed /
        source_not_landed): it was never his buy, so his re-send is not an add-on to it. A scan over
        the bounded ring; verdicts are rare."""
        tx = _key_tx(tx)
        if not tx:
            return
        with self._lock:
            for key, entries in tuple(self._keys.items()):
                kept = tuple(e for e in entries if e[0] != tx)
                if len(kept) != len(entries):
                    if kept:
                        self._keys[key] = kept
                    else:
                        del self._keys[key]
                    self.stats["tx_failed_dropped"] = self.stats.get("tx_failed_dropped", 0) + 1

    def prior(self, network_id, source_user_id, mint, tx):
        """(prior_net_usd, first entry) of the wallet's earlier buys in the window, the judged
        transaction excluded, or None when none is known. Memory only, no lock."""
        now, window = self.clock(), float(getattr(_cfg(), "MEMECOIN_REAL_ADDON_WINDOW_S", 600.0) or 0)
        tx = _key_tx(tx)
        entries = [e for e in self._keys.get(self.key(network_id, source_user_id, mint), ())
                   if e[0] != tx and now - e[3] <= window]
        if not entries:
            return None
        first = min(entries, key=lambda e: e[3])
        return sum(e[1] for e in entries), first


RING = AddonRing()


def judge(network_id, source_user_id, mint, tx, judged_usd, *, held_fn=None, ring=None):
    """None (pass) or (reason, detail) for one buy. `held_fn()` -> True when we hold an open position
    or a live reservation in the coin; read only when the ring says refuse, and a failed read passes
    (fail open, counted). Never raises."""
    ring = ring or RING
    try:
        if not scoped(source_user_id):
            return None
        judged = float(judged_usd)
        if not judged > 0:
            return None
        known = ring.prior(network_id, source_user_id, mint, tx)
        if known is None:
            return None
        prior, first = known
        limit = float(getattr(_cfg(), "MEMECOIN_REAL_ADDON_MAX_SHARE_PCT", 100.0) or 0)
        if not (prior > 0 and 100.0 * judged / prior < limit):
            return None
        if held_fn is not None:
            try:
                if held_fn():
                    ring.stats["passed_held"] += 1
                    return None
            except Exception:
                ring.stats["held_read_errors"] += 1
                logger.exception("add-on guard holding read failed (the buy passes)")
                return None
        share = 100.0 * judged / (prior + judged)
        detail = {"prior_net_usd": round(prior, 2), "addon_usd": round(judged, 2),
                  "addon_share_pct": round(share, 1), "first_buy_usd": round(first[1], 2),
                  "first_buy_price": first[2], "first_buy_at": first[3], "first_buy_tx": first[0]}
        price = ("%.2e" % float(first[2])) if first[2] else "unknown"
        reason = "%s prior_net=$%.1f addon=$%.1f share=%.1f%% first=%s" % (REASON, prior, judged, share, price)
        ring.stats["refused"] += 1
        return reason, detail
    except Exception:
        logger.exception("add-on guard failed (the buy passes)")
        return None


def held_in_ledger(conn, network_id, mint):
    """True when we hold an open real position or a live reservation in (network, coin)."""
    from sniper.persistence.db_memecoin import COIN_NETWORK_SQL, normalized_coin_network
    net = normalized_coin_network(network_id, mint)
    if conn.execute(f"SELECT 1 FROM memecoin_positions WHERE is_open=1 AND execution_mode='real' AND mint=? "
                    f"AND {COIN_NETWORK_SQL}=? LIMIT 1", (mint, net)).fetchone():
        return True
    return conn.execute("SELECT 1 FROM memecoin_pending_entries WHERE mint=? AND status='reserved' LIMIT 1",
                        (mint,)).fetchone() is not None
