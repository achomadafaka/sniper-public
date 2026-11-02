"""Entry feature capture, phase P2: T1 Solana order flow (spec 2026-09-29-memecoin-entry-feature-capture-design.md).

Default OFF (MEMECOIN_ENTRY_CAPTURE_SOL_FLOW). When on, the T0 row of a Solana T1/T2 attempt carries
its flow features as `pending:t1`, and this module's worker fills them later. The worker:
- reads only on the research key, refused when it is also a funded key (rpc_from_env), and only
  while the funded Solana book is flat (0 open real positions, 0 reserved: the research-read rule);
- paces through the key's shared cross-process pacer;
- takes its queue from the capture's own database (pending rows), so a deferred attempt cannot fall
  out of the cursor's re-read.

THE READ, per attempt (about 2-4 calls):
1. getTransaction on the source signature: the slot, the block time, the coin vault (the coin
   account that paid the source out, memecoin_slot_end_shadow.pool_of) and the pool owner. A split
   route, an unknown pool, or no signature (J7 uuid origins) is `unavailable:<why>`.
2. getTransactionsForAddress on the vault, newest first from the decision backward, filtered by
   block time, at most MAX_PAGES pages of 100. A window is covered only when the list is exhausted
   or the oldest transaction fetched is older than the window's start; otherwise
   `unavailable:truncated`, never a partial count.

PER TRANSACTION: the vault's coin delta gives the side (the vault paid coin out: a buy; took coin in:
a sell). The SOL leg is the pool owner's wSOL change, else the owner account's lamport change (the
pump curve holds SOL as lamports). USD = SOL leg x SOL/USD, where SOL/USD comes from the source trade
itself: the attempt's source_price (USD per coin) over the source's own SOL per coin. A pool whose
quote is not SOL falls back to coin x source_price, labelled. The maker is the fee payer, a LOWER
bound on traders behind routers. Our own signatures are excluded.

Windows match the EVM flow: (decision-N, decision], [src_t-N, src_t) and the copier queue (src_t,
decision], with src_t the source's block time; same_slot_buyers_after_source counts buys in the
source's slot after it (transactionIndex)."""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger("memecoin")

WSOL = "So11111111111111111111111111111111111111112"
MAX_PAGES = 3
PAGE_LIMIT = 100
SETTLE_S = 30.0               # the index lags the chain: read a decision at least this long after
# A row still pending this long is written unavailable:t1_expired. Above the longest busy
# stretch of the funded Solana book (Gamma, 7 days to 2026-10-02: 23.4 h; at 6 h, 17.3% of decisions
# would have expired, clustered in busy stretches). gtfa serves history that far back; confirm on
# the first live pass.
MAX_AGE_S = 48 * 3600.0
PENDING = "pending:t1"


HOLDER_NAMES = ("top1_share", "top10_share")
BURN_OWNERS = frozenset({"1nc1nerator11111111111111111111111111111111",
                         "11111111111111111111111111111111"})


def _switch(name):
    import os
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes"}


def enabled():
    """P2a, the Solana T1 order flow."""
    return _switch("MEMECOIN_ENTRY_CAPTURE_SOL_FLOW")


def holders_enabled():
    """P2b, the Solana T1 top-holder shares (its own switch, spec Budgets)."""
    return _switch("MEMECOIN_ENTRY_CAPTURE_SOL_HOLDERS")


def _keys(tx):
    from sniper.services.memecoin_slot_end_shadow import _keys as keys
    return keys(tx)


def _sig(tx):
    return ((tx.get("transaction") or {}).get("signatures") or [None])[0]


def quote_binding(tx, pool, mint):
    """The pool's own quote account, bound from its source BUY (R5, Codex night review 2026-10-06):
    ("token", address, quote mint) for the one account the pool owner holds, other than the coin,
    whose balance rose; ("lamports", owner, None) when none rose and the owner's own SOL did (the
    pump curve). None when nothing rose or several did: the quote cannot be bound, no trade proved."""
    from sniper.services.memecoin_slot_end_shadow import _balances
    pre, post = _balances(tx, "pre"), _balances(tx, "post")
    owner, rose = pool.get("owner"), []
    for account in set(pre) | set(post):
        holder, held, _amount = post.get(account) or pre[account]
        if holder == owner and held != mint:
            before = pre.get(account, (holder, held, 0.0))[2]
            if post.get(account, (holder, held, 0.0))[2] > before:
                rose.append((account, held))
    if len(rose) == 1:
        return ("token",) + rose[0]
    if rose:
        return None
    meta, keys = tx.get("meta") or {}, _keys(tx)
    if owner in keys:
        i = keys.index(owner)
        try:
            if int(meta["postBalances"][i]) > int(meta["preBalances"][i]):
                return ("lamports", owner, None)
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return None


def _quote_delta(tx, pool):
    """The bound quote account's change in `tx` (ui units; SOL for lamports), or None when absent."""
    bound = pool.get("quote")
    if not bound:
        return None
    kind, account, _mint = bound
    if kind == "lamports":
        meta, keys = tx.get("meta") or {}, _keys(tx)
        if account not in keys:
            return None
        i = keys.index(account)
        try:
            return (int(meta["postBalances"][i]) - int(meta["preBalances"][i])) / 1e9
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    from sniper.services.memecoin_slot_end_shadow import _balances
    pre, post = _balances(tx, "pre"), _balances(tx, "post")
    if account not in pre and account not in post:
        return None
    return (post.get(account, (None, None, 0.0))[2] - pre.get(account, (None, None, 0.0))[2])


def _sol_leg(tx, pool):
    """SOL the pool's bound quote moved in `tx`: a wSOL quote account or the owner's own lamports;
    None for a quote in another token or an unbound one."""
    bound = pool.get("quote")
    if not bound or (bound[0] == "token" and bound[2] != WSOL):
        return None
    delta = _quote_delta(tx, pool)
    return delta if delta else None


def _coin_delta(tx, vault, mint):
    """The vault's coin change in `tx` (ui units), or None when the vault is not in it."""
    from sniper.services.memecoin_slot_end_shadow import _balances
    pre, post = _balances(tx, "pre"), _balances(tx, "post")
    if vault not in pre and vault not in post:
        return None
    before = pre.get(vault, (None, mint, 0.0))
    after = post.get(vault, (None, mint, 0.0))
    if mint not in (before[1], after[1]):
        return None
    return after[2] - before[2]


UNPROVED = "unproved"
TOKEN_PROGRAMS = frozenset({"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"})


def _token_moves(tx):
    """{account: [(group, signed atoms), ...]} for every SPL transfer in `tx`: Transfer (3),
    TransferChecked (12) and Token-2022 TransferCheckedWithFee (26/1, the destination credited
    amount minus the withheld fee). `group` is the top-level instruction the transfer runs under.
    Other instructions are skipped; anything they move shows up as a sum that does not match.

    None when a token-program instruction cannot be read whole (review SF03, 2026-10-08): a
    recognized transfer naming fewer accounts than its opcode needs (Transfer: source, destination,
    owner; the checked forms: source, mint, destination, owner), an account index past the key list,
    or undecodable data. Its transaction is then unproved (the caller censors), never a crash and
    never a quiet zero."""
    import base58
    keys = _keys(tx)
    message = (tx.get("transaction") or {}).get("message") or {}
    groups = [(i, [ix]) for i, ix in enumerate(message.get("instructions") or [])]
    groups += [(g.get("index"), g.get("instructions") or [])
               for g in ((tx.get("meta") or {}).get("innerInstructions") or [])]
    moves = {}
    for group, ixs in groups:
        for ix in ixs:
            try:
                if keys[ix["programIdIndex"]] not in TOKEN_PROGRAMS:
                    continue
            except (KeyError, IndexError, TypeError):
                continue                         # no readable program: the sum check covers it
            try:
                accounts = [keys[a] for a in ix["accounts"]]
                data = base58.b58decode(ix["data"])
            except (KeyError, IndexError, TypeError, ValueError):
                return None
            if len(data) == 9 and data[0] == 3:
                need, src_i, dst_i, sent = 3, 0, 1, int.from_bytes(data[1:9], "little")
                got = sent
            elif len(data) == 10 and data[0] == 12:
                need, src_i, dst_i, sent = 4, 0, 2, int.from_bytes(data[1:9], "little")
                got = sent
            elif len(data) == 19 and data[0] == 26 and data[1] == 1:
                need, src_i, dst_i, sent = 4, 0, 2, int.from_bytes(data[2:10], "little")
                got = sent - int.from_bytes(data[11:19], "little")
            else:
                continue
            if len(accounts) < need:
                return None
            src, dst = accounts[src_i], accounts[dst_i]
            moves.setdefault(src, []).append((group, -sent))
            moves.setdefault(dst, []).append((group, got))
    return moves


def _atomic_delta(tx, account):
    """The account's token balance change in atoms, or None when a balance row carries no amount."""
    keys, out = _keys(tx), {}
    for side in ("pre", "post"):
        for b in (tx.get("meta") or {}).get("%sTokenBalances" % side) or []:
            try:
                if keys[b["accountIndex"]] == account:
                    out[side] = int(b["uiTokenAmount"]["amount"])
            except (KeyError, IndexError, TypeError, ValueError):
                return None
    return out.get("post", 0) - out.get("pre", 0) if out else None


def _bound_to_one_instruction(tx, pool):
    """True when every transfer of the coin vault (and, for a token quote, of the bound quote
    account) runs under ONE top-level instruction, each account's transfers there all go one way,
    and they sum exactly to its balance change (Codex R5, 2026-10-07). Vault identity is not swap
    identity: a deposit plus a same-vault fee withdrawal in another instruction nets like a sell.
    Known limits (Gamma on 85830c1d7): a Token-2022 coin with a transfer fee moved by plain
    TransferChecked credits the vault less than the instruction names, so its sells read UNPROVED
    (censored, counted in the coverage report); and the pump curve's lamport side is the curve's
    balance delta, so another instruction moving the curve's lamports skews `sol`, not the side."""
    moves = _token_moves(tx)
    if moves is None:
        return False                             # a transfer we cannot read whole proves nothing
    accounts = [pool["vault"]]
    bound = pool.get("quote")
    if bound and bound[0] == "token":
        accounts.append(bound[1])
    groups = set()
    for account in accounts:
        rows, delta = moves.get(account) or [], _atomic_delta(tx, account)
        rows = [(g, a) for g, a in rows if a]          # a 0-atom transfer has no direction (Gamma)
        if not rows or delta is None or sum(a for _g, a in rows) != delta:
            return False
        if len({a > 0 for _g, a in rows}) != 1:
            return False
        groups.update(g for g, _a in rows)
    return len(groups) == 1


def trade_of(tx, pool, mint):
    """(side +1 buy / -1 sell, coin ui, sol or None, maker) of one transaction on the pool, None, or
    UNPROVED. A trade moves the pool's BOUND quote account (pool["quote"], from its source buy)
    opposite to its coin vault, inside ONE swap instruction whose transfers explain both balance
    changes exactly. A liquidity add or withdrawal moves both the same way, and another token's
    movement under the same authority proves nothing (R5, Codex reviews 2026-10-06), so neither is a
    trade; an unbound quote proves no trade. A coin move no single instruction explains (a
    composition, two operations, a movement no transfer names) is UNPROVED: the caller censors."""
    coin = _coin_delta(tx, pool["vault"], mint)
    quote = _quote_delta(tx, pool)
    if not coin or quote is None:
        return None
    if not _bound_to_one_instruction(tx, pool):
        return UNPROVED
    if quote * coin >= 0:
        return None
    sol = _sol_leg(tx, pool)
    maker = (_keys(tx) or [None])[0]
    return (1 if coin < 0 else -1), abs(coin), (abs(sol) if sol else None), maker


def sol_usd_of_source(tx, pool, mint, source_price_usd):
    """SOL/USD implied by the source trade: its USD price per coin over its own SOL per coin."""
    t = trade_of(tx, pool, mint)
    if not t or t is UNPROVED or not t[2] or not source_price_usd or source_price_usd <= 0:
        return None
    return float(source_price_usd) / (t[2] / t[1])


def _fetch(rpc, vault, lo_ts, hi_ts):
    """(transactions newest first, exhausted) on `vault` with lo_ts <= blockTime <= hi_ts."""
    out, token = [], None
    for _page in range(MAX_PAGES):
        opts = {"transactionDetails": "full", "sortOrder": "desc", "limit": PAGE_LIMIT,
                "encoding": "json", "maxSupportedTransactionVersion": 1,
                "filters": {"status": "succeeded",
                            "blockTime": {"gte": int(lo_ts), "lte": int(hi_ts) + 1}}}
        if token:
            opts["paginationToken"] = token
        page = rpc("getTransactionsForAddress", [vault, opts])
        # A NULL OR SHAPELESS PAGE IS A FAILED READ (Gamma on 307f10cee): `or {}` made it an empty,
        # exhausted list and every window an ok zero. Raised as the read failing, so the row stays
        # pending and the batch retries next pass; `{"data": []}` is a real empty page.
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise RuntimeError("getTransactionsForAddress answered no page: %s" % type(page).__name__)
        out.extend(page["data"])
        token = page.get("paginationToken")
        if not token:
            return out, True
    return out, False


class _Flow:
    def __init__(self):
        self.buy_n = self.sell_n = 0
        self.buy_usd = self.sell_usd = 0.0
        self.largest_buy_usd = 0.0
        self.makers = set()
        self.source_buy_usd = 0.0

    def add(self, side, usd, maker, is_source):
        if side > 0:
            self.buy_n += 1
            self.buy_usd += usd
            self.largest_buy_usd = max(self.largest_buy_usd, usd)
            if is_source:
                self.source_buy_usd += usd
        else:
            self.sell_n += 1
            self.sell_usd += usd
        if maker:
            self.makers.add(maker)

    def as_dict(self, with_source):
        return {"buy_n": self.buy_n, "sell_n": self.sell_n,
                "buy_usd": round(self.buy_usd, 6), "sell_usd": round(self.sell_usd, 6),
                "net_flow_usd": round(self.buy_usd - self.sell_usd, 6),
                "unique_makers": len(self.makers),
                "largest_buy_usd": round(self.largest_buy_usd, 6),
                "source_buy_usd": round(self.source_buy_usd, 6) if with_source else None,
                "source_share_of_buys": (round(self.source_buy_usd / self.buy_usd, 6)
                                         if with_source and self.buy_usd > 0 else None)}


def source_of(attempt, rpc):
    """The source trade, read once per attempt for both the flow and the holders: {sig, tx, pool,
    why}, where `why` names why there is no single pool (None when there is one)."""
    from sniper.services import memecoin_entry_capture as cap
    from sniper.services.memecoin_slot_end_shadow import pool_of, source_sig
    sig = source_sig(attempt.get("origin_event_id"))
    if not sig:
        return {"sig": None, "tx": None, "pool": {}, "why": "no_source_signature"}
    tx = rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                      "commitment": "confirmed"}])
    if not tx or cap._ts(attempt.get("decision_started_at")) is None:
        return {"sig": sig, "tx": None, "pool": {}, "why": "source_tx_unreadable"}
    pool, why = pool_of(tx, attempt.get("mint"))
    if "vault" not in pool or why in ("pool_not_found", "split_route"):
        return {"sig": sig, "tx": tx, "pool": pool, "why": why or "pool_not_found"}
    pool["quote"] = quote_binding(tx, pool, attempt.get("mint"))
    if pool["quote"] is None:
        return {"sig": sig, "tx": tx, "pool": pool, "why": "quote_unbound"}
    return {"sig": sig, "tx": tx, "pool": pool, "why": None}


def flow_features(attempt, rpc, *, own_sigs=frozenset(), source=None):
    """{feature: (value, coverage, source)} for one Solana T1 attempt. Never raises for a chain
    answer; a transport error propagates (the worker leaves the row pending and retries)."""
    from sniper.services import memecoin_entry_capture as cap
    names = cap._flow_names()

    def every(coverage, source="helius_gtfa"):
        return {n: (None, coverage, source) for n in names}
    src = source or source_of(attempt, rpc)
    if src["why"] is not None:
        return every("unavailable:%s" % src["why"])
    sig, tx, pool = src["sig"], src["tx"], src["pool"]
    decision = cap._ts(attempt.get("decision_started_at"))
    mint = attempt.get("mint")
    src_t, src_slot = tx.get("blockTime"), tx.get("slot")
    if src_t is None:
        return every("unavailable:no_source_block_time")
    sol_usd = sol_usd_of_source(tx, pool, mint, attempt.get("source_price"))
    lo = min(decision, src_t) - max(cap.WINDOWS_S)
    txs, exhausted = _fetch(rpc, pool["vault"], lo, decision)
    oldest = min((t.get("blockTime") for t in txs if t.get("blockTime") is not None),
                 default=None)
    source_index = tx.get("transactionIndex")
    if source_index is None:
        source_index = next((t.get("transactionIndex") for t in txs if _sig(t) == sig), None)
    trades, unproved = [], []
    for t in txs:
        s = _sig(t)
        if (s and s.lower() in own_sigs) or t.get("blockTime") is None:  # own_tx_hashes lowercases
            continue
        got = trade_of(t, pool, mint)
        if got is None:
            continue
        if got is UNPROVED:
            unproved.append((t["blockTime"], t.get("slot"), t.get("transactionIndex")))
            continue
        side, coin, sol, maker = got
        if sol is not None and sol_usd:
            usd, basis = sol * sol_usd, "sol_x_source_sol_usd"
        else:
            usd, basis = coin * float(attempt.get("source_price") or 0.0), "coin_x_source_price"
        trades.append((t["blockTime"], t.get("slot"), t.get("transactionIndex"), side, usd, maker,
                       s == sig, basis))

    def covered(lo_excl):
        """True when every transaction after `lo_excl` (exclusive) up to the decision was read."""
        return exhausted or (oldest is not None and oldest <= lo_excl)
    out = {}
    basis = "helius_gtfa:%s:swap_bound" % ("sol_x_source_sol_usd" if sol_usd else "coin_x_source_price")

    def window(name, lo_excl, hi_incl, *, ex_name=None, lo_incl=None):
        def inside(at):
            return (lo_incl <= at < hi_incl) if lo_incl is not None else (lo_excl < at <= hi_incl)
        why = (None if covered(lo_excl if lo_incl is None else lo_incl - 1) else "unavailable:truncated")
        if why is None and any(inside(at) for at, _slot, _idx in unproved):
            why = "unavailable:unproved_operation"      # R5: censored, never a quiet undercount
        if why is not None:
            out[name] = (None, why, basis)
            if ex_name:
                out[ex_name] = (None, why, basis)
            return
        flow, ex_source = _Flow(), _Flow()
        for at, _slot, _idx, side, usd, maker, is_source, _b in trades:
            inside = (lo_incl <= at < hi_incl) if lo_incl is not None else (lo_excl < at <= hi_incl)
            if inside:
                flow.add(side, usd, maker, is_source)
                if not is_source:
                    ex_source.add(side, usd, maker, False)
        out[name] = (flow.as_dict(True) if ex_name else ex_source.as_dict(False), "ok", basis)
        if ex_name:
            out[ex_name] = (ex_source.as_dict(False), "ok", basis)

    for w in cap.WINDOWS_S:
        window(f"flow_{w}s", decision - w, decision, ex_name=f"flow_ex_source_{w}s")
    out["source_anchor"] = ("block_time", "ok", "helius_gtfa")
    for w in cap.WINDOWS_S:
        window(f"pre_source_{w}s", None, src_t, lo_incl=src_t - w)
    window("copier_queue", src_t, decision)
    if source_index is None or src_slot is None:
        out["same_block_buyers_after_source"] = (None, "unavailable:source_not_in_list",
                                                 "helius_gtfa")
    elif not covered(src_t - 1):
        out["same_block_buyers_after_source"] = (None, "unavailable:truncated", "helius_gtfa")
    elif any(slot == src_slot and (idx is None or idx > source_index) for _at, slot, idx in unproved):
        out["same_block_buyers_after_source"] = (None, "unavailable:unproved_operation", "helius_gtfa")
    else:
        n = sum(1 for _at, slot, idx, side, *_rest in trades
                if slot == src_slot and idx is not None and idx > source_index and side > 0)
        out["same_block_buyers_after_source"] = (n, "ok", "helius_gtfa")
    return out


HOLDER_SOURCE = "solana_rpc:top20_token_accounts"


def holder_features(attempt, rpc, *, source, now_ts, tier, own=frozenset()):
    """{top1_share, top10_share: (value, coverage, source, lag)} from the top-20 token accounts.
    Shares are per TOKEN ACCOUNT, not per owner. Not holders: the SOURCE pool's own accounts (its
    vault, anything its owner holds), burn holders and our own wallets (`own`). Another venue's pool
    still counts, hence the label `source_pool_excluded_only`. A STATE read: it describes the coin
    when read, labelled with its lag from the decision (`post_exit` on a fill, review MED 3). A
    share the 20 listed accounts cannot prove complete is unavailable."""
    from sniper.services import memecoin_entry_capture as cap
    from sniper.services.memecoin_wallet_map import _addr
    decision = cap._ts(attempt.get("decision_started_at"))
    lag = (now_ts - decision) if decision is not None else None
    label = "%s:source_pool_excluded_only:%s" % (HOLDER_SOURCE,
                                                  "post_exit" if tier >= 2 else "after_decision")

    def every(coverage):
        return {n: (None, coverage, label, lag) for n in HOLDER_NAMES}
    if source["why"] is not None:
        return every("unavailable:%s" % source["why"])
    mint, pool = attempt.get("mint"), source["pool"]
    supply = ((rpc("getTokenSupply", [mint]) or {}).get("value") or {}).get("uiAmount")
    if not supply or float(supply) <= 0:
        return every("unavailable:no_supply")
    largest = (rpc("getTokenLargestAccounts", [mint]) or {}).get("value") or []
    if not largest:
        return every("unavailable:no_largest_accounts")
    infos = (rpc("getMultipleAccounts", [[a["address"] for a in largest],
                                         {"encoding": "jsonParsed"}]) or {}).get("value") or []

    def owner_of(info):
        try:
            return info["data"]["parsed"]["info"]["owner"]
        except (KeyError, TypeError):
            return None
    holders = []
    for entry, info in zip(largest, infos + [None] * (len(largest) - len(infos))):
        owner = owner_of(info)
        if entry.get("address") == pool.get("vault") or (owner and owner == pool.get("owner")):
            continue                                    # the pool's own coin
        if owner is None:
            # Unreadable: it may be a burn or the pool, so no exclusion can be proved and no share is
            # 'ok' (R7, Codex review 2026-10-06: an unknown 900-token burn read top1 0.9 as ok).
            return every("unavailable:owner_unknown")
        if owner in BURN_OWNERS or (owner and _addr(owner) in own):
            continue
        holders.append(float(entry.get("uiAmount") or 0.0))
    holders.sort(reverse=True)
    total = float(supply)

    def share(n):
        if len(largest) < 20 or len(holders) >= n:          # every account listed, or n found
            return (sum(holders[:n]) / total, "ok", label, lag)
        return (None, "unavailable:top20_exhausted", label, lag)
    return {"top1_share": share(1), "top10_share": share(10)}


# ------------------------------------------------------------------------------ the worker

def pending_rows(db, limit=20):
    return [dict(r) for r in db.execute(
        "SELECT attempt_id, decision_at, tier, coverage_json FROM entry_features WHERE network_id='solana' "
        "AND tier >= 1 AND coverage_json LIKE ? ORDER BY decision_at LIMIT ?",
        ('%"' + PENDING + '"%', int(limit)))]


def merge_t1(db, attempt_id, values):
    """Merge `values` ({feature: (value, coverage, source[, lag])}) into the row's JSON maps."""
    row = db.execute("SELECT features_json, coverage_json, sources_json, read_lag_json FROM "
                     "entry_features WHERE attempt_id=?", (attempt_id,)).fetchone()
    if row is None:
        return False
    features, coverage, sources, lags = (json.loads(row[0]), json.loads(row[1]),
                                         json.loads(row[2]), json.loads(row[3] or "{}"))
    for name, got in values.items():
        value, cov, source = got[:3]
        features[name] = value if cov == "ok" else None
        coverage[name], sources[name] = cov, source
        if len(got) > 3 and got[3] is not None:
            lags[name] = round(float(got[3]), 3)
    db.execute("UPDATE entry_features SET features_json=?, coverage_json=?, sources_json=?, "
               "read_lag_json=? WHERE attempt_id=?",
               (json.dumps(features, sort_keys=True, default=str), json.dumps(coverage, sort_keys=True),
                json.dumps(sources, sort_keys=True), json.dumps(lags, sort_keys=True), attempt_id))
    db.commit()
    return True


def flat_check(ledger):
    """The research-read gate for this worker; an error reads as NOT flat."""
    from sniper.persistence import db_memecoin as m

    def flat():
        try:
            return m.solana_book_flat(ledger)
        except Exception:
            logger.exception("entry capture T1: flat-book check failed; waiting")
            return False
    return flat


# Parse errors in one row's answers. Everything else (HTTPError, URLError, TimeoutError, the RPC's
# RuntimeError, a JSON body that does not decode) is the read failing, and stops the batch.
_DATA_ERRORS = (KeyError, IndexError, TypeError, AttributeError, ZeroDivisionError)


def run_t1_once(db, ledger, rpc, *, flat_fn, now_ts, limit=20, stats=None):
    """Fill the oldest pending Solana T1 rows. Reads nothing unless the book is flat. A row older
    than MAX_AGE_S is expired; a row younger than SETTLE_S waits. Returns how many rows were done."""
    import sniper.settings.memecoin as mc_settings
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_entry_capture as cap
    from sniper.services.memecoin_wallet_map import own_address_set
    stats = {} if stats is None else stats
    done = 0
    for row in pending_rows(db, limit):
        decision = cap._ts(row["decision_at"])
        pending = [n for n, c in json.loads(row["coverage_json"]).items() if c == PENDING]
        if decision is not None and now_ts - decision > MAX_AGE_S:
            merge_t1(db, row["attempt_id"], {n: (None, "unavailable:t1_expired", "helius_gtfa")
                                             for n in pending})
            stats["expired"] = stats.get("expired", 0) + 1
            done += 1
            continue
        if decision is None or now_ts - decision < SETTLE_S:
            continue
        if not flat_fn():
            stats["not_flat"] = 1
            break                                   # nothing reads while a real position is open
        attempt = m.entry_attempt_by_id(ledger, row["attempt_id"])
        if attempt is None:
            continue
        own = m.own_tx_hashes(ledger, "solana", attempt.get("mint"))
        own_wallets = own_address_set(mc_settings.MEMECOIN_OWN_ADDRESSES)
        flow_names = set(cap._flow_names())
        try:
            src = source_of(attempt, rpc)
            values = {}
            if flow_names & set(pending):
                values.update(flow_features(attempt, rpc, own_sigs=own, source=src))
            if set(HOLDER_NAMES) & set(pending):
                values.update(holder_features(attempt, rpc, source=src, now_ts=now_ts,
                                              tier=int(row.get("tier") or 1), own=own_wallets))
        except _DATA_ERRORS as exc:
            # A DEFECT IN THIS ROW'S DATA, NOT THE READ (review SF03): retrying it every pass would
            # hold every later row behind it until the 48 h expiry. Its pending features are written
            # unavailable, typed, and the batch goes on. A transport error is not one of these.
            logger.warning("entry capture T1: %s parsing %s (written unavailable)",
                           type(exc).__name__, row["attempt_id"], exc_info=True)
            merge_t1(db, row["attempt_id"],
                     {n: (None, "unavailable:parse_error:%s" % type(exc).__name__, "helius_gtfa")
                      for n in pending})
            stats["parse_errors"] = stats.get("parse_errors", 0) + 1
            done += 1
            continue
        except Exception as exc:                    # transport, 429, timeout: stays pending
            logger.warning("entry capture T1: %s for %s (left pending)", type(exc).__name__,
                           row["attempt_id"])
            stats["errors"] = stats.get("errors", 0) + 1
            break
        merge_t1(db, row["attempt_id"], {n: v for n, v in values.items() if n in pending})
        done += 1
    stats["done"] = done
    return done
