"""Entry capture P2, T1 Solana order flow (spec 2026-09-29-memecoin-entry-feature-capture-design.md):
default OFF, research key only through the shared pacer, flat-book gated, the queue held in the
capture's own DB. Synthetic transactions in the getTransaction / getTransactionsForAddress shapes."""
import json
import sqlite3

import pytest

from sniper.persistence import db_entry_features as store
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_entry_capture as cap
from sniper.services import memecoin_entry_capture_sol as sol
from sniper.services import memecoin_slot_end_shadow as shadow

MINT = "Mint1111111111111111111111111111111111111111"
VAULT = "Vau1t111111111111111111111111111111111111111"
CURVE = "Curve11111111111111111111111111111111111111"     # the vault's owner: the pump curve PDA
SRC_SIG = "5" * 88
D = 1_790_000_000.0                                    # the decision


TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
USER_COIN = "UserCoin" + "1" * 36
USER_WSOL = "UserWso1" + "1" * 36
FEE = "FeeAcct1" + "1" * 36
COIN_DEC, WSOL_DEC = 6, 9


def _ui(amt, dec):
    return {"uiAmount": amt, "amount": str(round(amt * 10 ** dec)), "decimals": dec}


def _atoms(amt, dec):
    return round(amt * 10 ** dec)


def _move(keys, src, dst, atoms, *, fee=None, mint=MINT):
    """A compiled SPL transfer: Transfer (3), or Token-2022 TransferCheckedWithFee (26/1) with `fee`."""
    import base58
    if fee is None:
        data, accounts = bytes([3]) + int(atoms).to_bytes(8, "little"), [src, dst, keys[0]]
        program = TOKEN
    else:
        data = bytes([26, 1]) + int(atoms).to_bytes(8, "little") + bytes([COIN_DEC]) + int(fee).to_bytes(8, "little")
        accounts, program = [src, mint, dst, keys[0]], TOKEN22
    for k in accounts + [program]:
        if k not in keys:
            keys.append(k)
    return {"programIdIndex": keys.index(program), "accounts": [keys.index(k) for k in accounts],
            "data": base58.b58encode(data).decode()}


def _tx(sig, bt, slot, idx, *, coin_pre, coin_post, sol_pre, sol_post, payer="Payer" + "1" * 39,
        wsol=False, mint=MINT, groups=None):
    """`groups`: [[(source, destination, atoms[, fee]), ...], ...], one list per top-level instruction's
    inner transfers; default one group moving the vault (and the wSOL vault) by exactly the deltas."""
    keys = [payer, VAULT, CURVE, shadow.PUMP]
    tb = lambda amt: [{"accountIndex": 1, "mint": mint, "owner": CURVE,
                       "uiTokenAmount": _ui(amt, COIN_DEC)}]
    meta = {"preTokenBalances": tb(coin_pre), "postTokenBalances": tb(coin_post),
            "preBalances": [10 ** 10, 0, 0, 0], "postBalances": [10 ** 10, 0, 0, 0],
            "innerInstructions": []}
    wvault = "WsolVau1t" + "1" * 35
    if wsol:
        keys.append(wvault)
        w = lambda amt: {"accountIndex": 4, "mint": sol.WSOL, "owner": CURVE,
                         "uiTokenAmount": _ui(amt, WSOL_DEC)}
        meta["preTokenBalances"].append(w(sol_pre))
        meta["postTokenBalances"].append(w(sol_post))
        meta["preBalances"].append(0)
        meta["postBalances"].append(0)
    else:
        meta["preBalances"][2], meta["postBalances"][2] = int(sol_pre * 1e9), int(sol_post * 1e9)
    if groups is None:
        coin = _atoms(coin_post, COIN_DEC) - _atoms(coin_pre, COIN_DEC)
        group = [(USER_COIN, VAULT, coin) if coin > 0 else (VAULT, USER_COIN, -coin)] if coin else []
        if wsol:
            q = _atoms(sol_post, WSOL_DEC) - _atoms(sol_pre, WSOL_DEC)
            group += [(USER_WSOL, wvault, q) if q > 0 else (wvault, USER_WSOL, -q)] if q else []
        groups = [group]
    inner = [[_move(keys, *t[:3], fee=(t[3] if len(t) > 3 else None), mint=mint) for t in g]
             for g in groups]
    meta["innerInstructions"] = [{"index": i, "instructions": ixs} for i, ixs in enumerate(inner) if ixs]
    meta["preBalances"] += [0] * (len(keys) - len(meta["preBalances"]))
    meta["postBalances"] += [0] * (len(keys) - len(meta["postBalances"]))
    msg = {"accountKeys": keys, "instructions": [{"programIdIndex": 3, "accounts": [1, 2]}
                                                 for _g in (groups or [[]])]}
    return {"slot": slot, "blockTime": int(bt), "transactionIndex": idx, "meta": meta,
            "transaction": {"signatures": [sig], "message": msg}}


def _buy(sig, bt, slot, idx, coin, sol_amt, **kw):
    return _tx(sig, bt, slot, idx, coin_pre=1_000_000.0, coin_post=1_000_000.0 - coin,
               sol_pre=100.0, sol_post=100.0 + sol_amt, **kw)


def _sell(sig, bt, slot, idx, coin, sol_amt, **kw):
    return _tx(sig, bt, slot, idx, coin_pre=1_000_000.0, coin_post=1_000_000.0 + coin,
               sol_pre=100.0, sol_post=100.0 - sol_amt, **kw)


SOURCE = _buy(SRC_SIG, D - 10, 100, 5, 10.0, 1.0)              # 0.1 SOL a coin at $15: SOL $150


def _market(extra=()):
    return [_buy("a" * 88, D - 5, 112, 0, 20.0, 2.0),             # a copier, inside every window
            _buy("b" * 88, D - 10, 100, 7, 4.0, 0.4),             # same slot, after the source
            SOURCE,
            _sell("c" * 88, D - 15, 88, 1, 30.0, 3.0),            # before the source
            _buy("d" * 88, D - 400, 1, 1, 50.0, 5.0),             # outside every window
            *extra]


class Rpc:
    def __init__(self, txs, *, page=100, source=SOURCE, pages_cap=None):
        self.txs, self.page, self.source, self.calls = txs, page, source, []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "getTransaction":
            return self.source if params[0] == SRC_SIG else None
        assert method == "getTransactionsForAddress" and params[0] == VAULT
        opts = params[1]
        lo, hi = opts["filters"]["blockTime"]["gte"], opts["filters"]["blockTime"]["lte"]
        rows = sorted((t for t in self.txs if lo <= t["blockTime"] <= hi),
                      key=lambda t: (t["blockTime"], t["slot"], t["transactionIndex"]), reverse=True)
        start = int(opts.get("paginationToken") or 0)
        chunk = rows[start:start + self.page]
        more = start + self.page < len(rows)
        return {"data": chunk, "paginationToken": str(start + self.page) if more else None}


def _attempt(**over):
    a = {"attempt_id": "att1", "origin_event_id": "onchain:" + SRC_SIG, "mint": MINT,
         "decision_started_at": cap._iso(D), "source_price": 15.0}
    a.update(over)
    return a


def test_every_window_counts_the_market_around_the_decision_in_usd_at_the_sources_sol_price():
    out = sol.flow_features(_attempt(), Rpc(_market()), own_sigs=set())
    v, cov, src = out["flow_30s"]
    assert cov == "ok" and src == "helius_gtfa:sol_x_source_sol_usd:swap_bound"
    assert (v["buy_n"], v["sell_n"]) == (3, 1)                     # source, copier, same-slot; a sell
    assert v["buy_usd"] == pytest.approx((1.0 + 2.0 + 0.4) * 150) and v["sell_usd"] == pytest.approx(450)
    assert v["source_buy_usd"] == pytest.approx(150) and v["unique_makers"] == 1
    assert out["flow_ex_source_30s"][0]["buy_n"] == 2
    pre = out["pre_source_30s"][0]
    assert (pre["buy_n"], pre["sell_n"]) == (0, 1)                 # [src-30, src): the sell only
    q = out["copier_queue"][0]
    assert (q["buy_n"], q["sell_n"]) == (1, 0)                     # (src, decision]: the copier
    assert out["same_block_buyers_after_source"] == (1, "ok", "helius_gtfa")
    assert out["flow_300s"][0]["buy_n"] == 3                       # the -400 s buy is outside


def test_our_own_transaction_is_never_market_flow():
    own = _buy("OwnSig" + "9" * 82, D - 3, 115, 0, 5.0, 0.5)
    out = sol.flow_features(_attempt(), Rpc(_market([own])), own_sigs={("OwnSig" + "9" * 82).lower()})
    assert out["flow_30s"][0]["buy_n"] == 3


def test_a_capped_read_marks_only_the_windows_it_could_not_see():
    busy = [_buy("e%087d" % i, D - 1, 120 + i, 0, 1.0, 0.1) for i in range(300)]   # 300 at -1 s
    rpc = Rpc(_market(busy))
    out = sol.flow_features(_attempt(), rpc, own_sigs=set())
    assert rpc.calls.count("getTransactionsForAddress") == sol.MAX_PAGES
    assert out["flow_30s"][1] == "unavailable:truncated"           # never a partial count
    assert out["pre_source_30s"][1] == "unavailable:truncated"
    assert out["same_block_buyers_after_source"][1] == "unavailable:truncated"


def test_a_short_window_is_complete_when_the_read_reaches_past_its_start():
    busy = [_buy("e%087d" % i, D - 40, 60, i, 1.0, 0.1) for i in range(200)]       # 200 at -40 s
    out = sol.flow_features(_attempt(), Rpc(_market(busy), page=60), own_sigs=set())
    assert out["flow_30s"][1] == "ok" and out["flow_30s"][0]["buy_n"] == 3   # read reached -40 s
    assert out["flow_300s"][1] == "unavailable:truncated"                    # but not -300 s
    out = sol.flow_features(_attempt(), Rpc(_market(busy), page=100), own_sigs=set())
    assert out["flow_300s"][1] == "ok" and out["flow_300s"][0]["buy_n"] == 203   # 3 pages: all


def test_a_pool_quoted_in_wsol_takes_the_sol_leg_from_the_owners_wsol_account():
    src = _buy(SRC_SIG, D - 10, 100, 5, 10.0, 1.0, wsol=True)
    rpc = Rpc([src, _buy("a" * 88, D - 5, 112, 0, 20.0, 2.0, wsol=True)], source=src)
    v = sol.flow_features(_attempt(), rpc, own_sigs=set())["flow_30s"][0]
    assert v["buy_usd"] == pytest.approx(3.0 * 150)


def test_without_a_sol_leg_the_usd_is_coin_at_the_source_price_and_says_so(monkeypatch):
    monkeypatch.setattr(sol, "_sol_leg", lambda tx, owner: None)
    out = sol.flow_features(_attempt(), Rpc(_market()), own_sigs=set())
    assert out["flow_30s"][2] == "helius_gtfa:coin_x_source_price:swap_bound"
    assert out["flow_30s"][0]["buy_usd"] == pytest.approx((10 + 20 + 4) * 15.0)


@pytest.mark.parametrize("attempt, why", [
    (dict(origin_event_id="swap:6f9619ff-8b86-d011-b42d-00c04fc964ff"), "unavailable:no_source_signature"),
    (dict(origin_event_id="onchain:" + "7" * 88), "unavailable:source_tx_unreadable")])
def test_an_attempt_the_chain_cannot_anchor_is_unavailable_everywhere(attempt, why):
    out = sol.flow_features(_attempt(**attempt), Rpc(_market()), own_sigs=set())
    assert {c for _v, c, _s in out.values()} == {why}
    assert set(out) == set(cap._flow_names())


def test_a_split_route_source_is_unavailable_never_one_pools_share():
    split = _buy(SRC_SIG, D - 10, 100, 5, 10.0, 1.0)
    split["transaction"]["message"]["accountKeys"].append("Vau2t" + "1" * 39)
    at = len(split["transaction"]["message"]["accountKeys"]) - 1
    split["meta"]["preTokenBalances"].append({"accountIndex": at, "mint": MINT, "owner": "X",
                                              "uiTokenAmount": {"uiAmount": 50.0}})
    split["meta"]["postTokenBalances"].append({"accountIndex": at, "mint": MINT, "owner": "X",
                                               "uiTokenAmount": {"uiAmount": 45.0}})
    split["meta"]["preBalances"].append(0)
    split["meta"]["postBalances"].append(0)
    out = sol.flow_features(_attempt(), Rpc(_market(), source=split), own_sigs=set())
    assert out["flow_30s"][1] == "unavailable:split_route"


# ---- the worker: the queue lives in the capture DB; flat-book gated; expiry; settle ----

def _db(tmp_path, tier=1, decision=D, pending=True):
    db = store.connect(tmp_path / "f.db")
    cov = {n: (sol.PENDING if pending else "not_attempted:t1") for n in cap._flow_names()}
    cov["src_prior_n"] = "ok"
    store.insert(db, {"attempt_id": "att1", "network_id": "solana", "mint": MINT, "tier": tier,
                      "decision_at": cap._iso(decision), "feature_version": 1,
                      "features_json": json.dumps({"src_prior_n": 3}), "coverage_json": json.dumps(cov),
                      "sources_json": "{}", "read_lag_json": "{}", "computed_at": "x"})
    return db


class Ledger:
    def __init__(self, attempt):
        self.attempt = attempt


@pytest.fixture
def ledger_fns(monkeypatch):
    monkeypatch.setattr(m, "entry_attempt_by_id", lambda conn, aid: conn.attempt)
    monkeypatch.setattr(m, "own_tx_hashes", lambda conn, chain, mint: set())


def test_the_worker_fills_a_pending_row_and_keeps_its_t0_features(tmp_path, ledger_fns):
    db = _db(tmp_path)
    stats = {}
    n = sol.run_t1_once(db, Ledger(_attempt()), Rpc(_market()), flat_fn=lambda: True,
                        now_ts=D + 60, stats=stats)
    row = db.execute("SELECT features_json, coverage_json FROM entry_features").fetchone()
    features, coverage = json.loads(row[0]), json.loads(row[1])
    assert n == 1 and coverage["flow_30s"] == "ok" and features["flow_30s"]["buy_n"] == 3
    assert features["src_prior_n"] == 3 and coverage["src_prior_n"] == "ok"
    assert sol.pending_rows(db) == []


def test_nothing_is_read_while_the_book_is_not_flat(tmp_path, ledger_fns):
    db, rpc = _db(tmp_path), Rpc(_market())
    stats = {}
    assert sol.run_t1_once(db, Ledger(_attempt()), rpc, flat_fn=lambda: False, now_ts=D + 60,
                           stats=stats) == 0
    assert rpc.calls == [] and stats["not_flat"] == 1 and len(sol.pending_rows(db)) == 1


def test_a_fresh_decision_waits_for_the_index_and_an_old_one_expires(tmp_path, ledger_fns):
    db, rpc = _db(tmp_path), Rpc(_market())
    sol.run_t1_once(db, Ledger(_attempt()), rpc, flat_fn=lambda: True, now_ts=D + 5)
    assert rpc.calls == [] and len(sol.pending_rows(db)) == 1
    sol.run_t1_once(db, Ledger(_attempt()), rpc, flat_fn=lambda: True, now_ts=D + sol.MAX_AGE_S + 1)
    cov = json.loads(db.execute("SELECT coverage_json FROM entry_features").fetchone()[0])
    assert cov["flow_30s"] == "unavailable:t1_expired" and rpc.calls == []


def test_a_transport_failure_leaves_the_row_pending(tmp_path, ledger_fns):
    db = _db(tmp_path)

    def boom(method, params):
        raise TimeoutError("research key")
    stats = {}
    sol.run_t1_once(db, Ledger(_attempt()), boom, flat_fn=lambda: True, now_ts=D + 60, stats=stats)
    assert stats["errors"] == 1 and len(sol.pending_rows(db)) == 1


def test_tier_zero_and_other_chains_are_never_queued(tmp_path):
    db = _db(tmp_path, tier=0)
    assert sol.pending_rows(db) == []


# ---- T0 marks the row pending only when the switch is on and the attempt is T1/T2 ----

@pytest.mark.parametrize("switch, reason, status, expect", [
    ("1", "chase_cap", "refused", sol.PENDING),
    ("1", None, "filled", sol.PENDING),
    ("1", "not_approved", "refused", "not_attempted:t1"),
    ("0", "chase_cap", "refused", "not_attempted:t1")])
def test_t0_marks_solana_flow_pending_only_for_t1_with_the_switch_on(monkeypatch, switch, reason,
                                                                      status, expect):
    monkeypatch.setenv("MEMECOIN_ENTRY_CAPTURE_SOL_FLOW", switch)
    rec = cap._Row()
    cap._order_flow(None, {}, {"network_id": "solana", "terminal_reason": reason,
                               "terminal_status": status}, rec, D)
    assert set(rec.coverage.values()) == {expect}


def test_the_loop_starts_no_t1_worker_while_the_switch_is_off(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import memecoin_entry_capture_loop as loop
    monkeypatch.setenv("MEMECOIN_ENTRY_CAPTURE_SOL_FLOW", "0")
    assert loop.start_t1_worker() is None
    monkeypatch.setenv("MEMECOIN_ENTRY_CAPTURE_SOL_FLOW", "1")
    monkeypatch.delenv("MEMECOIN_RESEARCH_HELIUS_RPC_URL", raising=False)
    monkeypatch.delenv("MEMECOIN_RESEARCH_HELIUS_RPC_URLS", raising=False)
    assert loop.start_t1_worker() is None                     # no research key: refused


def test_the_flat_check_is_the_research_read_rule_and_an_error_is_not_flat():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    assert m.solana_book_flat(c) is True
    m.reserve_pending(c, mint=MINT, source_user_id="u", strategy="s", notional=50.0,
                      reserved_at="2026-10-02T00:00:00Z", network_id="solana")
    assert m.solana_book_flat(c) is False

    class Broken:
        def execute(self, *a):
            raise sqlite3.OperationalError("locked")
    assert sol.flat_check(Broken())() is False


def test_the_worker_overwrites_only_what_is_still_pending(tmp_path, ledger_fns):
    db = _db(tmp_path)
    cov = json.loads(db.execute("SELECT coverage_json FROM entry_features").fetchone()[0])
    cov["source_anchor"] = "unavailable:settled_elsewhere"            # already decided
    db.execute("UPDATE entry_features SET coverage_json=?", (json.dumps(cov),))
    db.commit()
    sol.run_t1_once(db, Ledger(_attempt()), Rpc(_market()), flat_fn=lambda: True, now_ts=D + 60)
    cov = json.loads(db.execute("SELECT coverage_json FROM entry_features").fetchone()[0])
    assert cov["source_anchor"] == "unavailable:settled_elsewhere" and cov["flow_30s"] == "ok"


# ---- P2b: top-holder shares (own switch) ----

def _largest(entries):
    return {"value": [{"address": a, "uiAmount": amt} for a, amt in entries]}


def _infos(owners):
    return {"value": [None if o is None else {"data": {"parsed": {"info": {"owner": o}}}}
                      for o in owners]}


class HolderRpc(Rpc):
    def __init__(self, largest, owners, supply=1000.0, **kw):
        super().__init__(_market(), **kw)
        self.largest, self.owners, self.supply = largest, owners, supply

    def __call__(self, method, params):
        if method == "getTokenSupply":
            self.calls.append(method)
            return {"value": {"uiAmount": self.supply}}
        if method == "getTokenLargestAccounts":
            self.calls.append(method)
            return _largest(self.largest)
        if method == "getMultipleAccounts":
            self.calls.append(method)
            assert params[1] == {"encoding": "jsonParsed"}
            return _infos(self.owners)
        return super().__call__(method, params)


def _holders(rpc, tier=1, now=D + 120):
    src = sol.source_of(_attempt(), rpc)
    return sol.holder_features(_attempt(), rpc, source=src, now_ts=now, tier=tier)


def test_the_pools_own_coin_and_burns_are_not_holders():
    rpc = HolderRpc([(VAULT, 600.0), ("Acct2", 100.0), ("Acct3", 50.0), ("Acct4", 40.0)],
                    [CURVE, "Burnt", "1nc1nerator11111111111111111111111111111111", "Holder4"])
    out = _holders(rpc)
    assert out["top1_share"][:2] == (0.1, "ok")                    # Acct2 owned by "Burnt": a holder
    assert out["top10_share"][0] == pytest.approx((100 + 40) / 1000.0)
    assert out["top1_share"][2] == "solana_rpc:top20_token_accounts:source_pool_excluded_only:after_decision"
    assert out["top1_share"][3] == pytest.approx(120.0)            # the read's lag from the decision


def test_an_account_the_pool_owner_holds_is_the_pool():
    rpc = HolderRpc([("PoolSide", 700.0), ("Acct2", 10.0)], [CURVE, "Holder2"])
    assert _holders(rpc)["top1_share"][0] == pytest.approx(0.01)


def test_a_full_top20_list_that_leaves_under_ten_holders_cannot_prove_the_top10():
    rows = [("P%d" % i, 30.0) for i in range(12)] + [("H%d" % i, 10.0) for i in range(8)]
    rpc = HolderRpc(rows, [CURVE] * 12 + ["h%d" % i for i in range(8)])
    out = _holders(rpc)
    assert out["top10_share"][1] == "unavailable:top20_exhausted"
    assert out["top1_share"][:2] == (0.01, "ok")
    rpc = HolderRpc(rows[12:], ["h%d" % i for i in range(8)])     # every account listed: complete
    assert _holders(rpc)["top10_share"][:2] == (pytest.approx(0.08), "ok")


def test_a_fill_is_labelled_post_exit_and_no_supply_is_unavailable():
    rpc = HolderRpc([("Acct2", 10.0)], ["Holder2"])
    assert _holders(rpc, tier=2)["top1_share"][2] == "solana_rpc:top20_token_accounts:source_pool_excluded_only:post_exit"
    rpc = HolderRpc([("Acct2", 10.0)], ["Holder2"], supply=0.0)
    assert _holders(rpc)["top10_share"][1] == "unavailable:no_supply"


def _db_with(tmp_path, names):
    db = store.connect(tmp_path / "f.db")
    cov = {n: sol.PENDING for n in names}
    store.insert(db, {"attempt_id": "att1", "network_id": "solana", "mint": MINT, "tier": 1,
                      "decision_at": cap._iso(D), "feature_version": 1, "features_json": "{}",
                      "coverage_json": json.dumps(cov), "sources_json": "{}",
                      "read_lag_json": "{}", "computed_at": "x"})
    return db


def test_holders_alone_read_the_source_once_and_never_page_the_vault(tmp_path, ledger_fns):
    db = _db_with(tmp_path, sol.HOLDER_NAMES)
    rpc = HolderRpc([("Acct2", 10.0)], ["Holder2"])
    sol.run_t1_once(db, Ledger(_attempt()), rpc, flat_fn=lambda: True, now_ts=D + 60)
    assert rpc.calls == ["getTransaction", "getTokenSupply", "getTokenLargestAccounts",
                         "getMultipleAccounts"]
    lags = json.loads(db.execute("SELECT read_lag_json FROM entry_features").fetchone()[0])
    assert lags["top1_share"] == 60.0


def test_flow_and_holders_share_one_source_read(tmp_path, ledger_fns):
    db = _db_with(tmp_path, list(cap._flow_names()) + list(sol.HOLDER_NAMES))
    rpc = HolderRpc([("Acct2", 10.0)], ["Holder2"])
    sol.run_t1_once(db, Ledger(_attempt()), rpc, flat_fn=lambda: True, now_ts=D + 60)
    assert rpc.calls.count("getTransaction") == 1 and sol.pending_rows(db) == []


@pytest.mark.parametrize("chain, switch, tier_reason, expect", [
    ("solana", "1", "chase_cap", sol.PENDING),
    ("solana", "1", "not_approved", "not_attempted:t1"),
    ("solana", "0", "chase_cap", "not_attempted:t1"),
    ("4663", "1", "chase_cap", "unavailable:no_transfer_collector")])
def test_t0_marks_holders_by_chain_switch_and_tier(monkeypatch, chain, switch, tier_reason, expect):
    monkeypatch.setenv("MEMECOIN_ENTRY_CAPTURE_SOL_HOLDERS", switch)
    rec = cap._Row()
    cap._holders({"network_id": chain, "terminal_reason": tier_reason, "terminal_status": "refused"},
                 rec)
    assert {rec.coverage[n] for n in sol.HOLDER_NAMES} == {expect}


def test_the_holders_switch_alone_starts_the_worker_only_with_a_research_key(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import memecoin_entry_capture_loop as loop
    monkeypatch.setenv("MEMECOIN_ENTRY_CAPTURE_SOL_FLOW", "0")
    monkeypatch.setenv("MEMECOIN_ENTRY_CAPTURE_SOL_HOLDERS", "1")
    monkeypatch.delenv("MEMECOIN_RESEARCH_HELIUS_RPC_URL", raising=False)
    monkeypatch.delenv("MEMECOIN_RESEARCH_HELIUS_RPC_URLS", raising=False)
    assert loop.start_t1_worker() is None                     # switch on, but no research key
    started = []
    import threading
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    monkeypatch.setenv("MEMECOIN_RESEARCH_HELIUS_RPC_URL", "https://r.test/?api-key=K")
    assert loop.start_t1_worker() is not None and started == ["entry-capture-t1"]


def test_the_pool_vault_is_excluded_by_address_even_when_its_owner_is_unreadable():
    rpc = HolderRpc([(VAULT, 600.0), ("Acct2", 100.0)], [None, "Holder2"])
    assert _holders(rpc)["top1_share"][0] == pytest.approx(0.1)


# ---- Gamma on 752569c54: the sample, the per-row flat check, a transport error stops the pass ----

def _two_rows(tmp_path):
    db = store.connect(tmp_path / "f.db")
    cov = {n: sol.PENDING for n in cap._flow_names()}
    for aid, at in (("att1", D), ("att2", D + 1)):
        store.insert(db, {"attempt_id": aid, "network_id": "solana", "mint": MINT, "tier": 1,
                          "decision_at": cap._iso(at), "feature_version": 1, "features_json": "{}",
                          "coverage_json": json.dumps(cov), "sources_json": "{}",
                          "read_lag_json": "{}", "computed_at": "x"})
    return db


def test_the_book_is_checked_before_every_row_not_once_a_pass(tmp_path, ledger_fns):
    db, rpc = _two_rows(tmp_path), Rpc(_market())
    answers = iter([True, False])
    sol.run_t1_once(db, Ledger(_attempt()), rpc, flat_fn=lambda: next(answers), now_ts=D + 60)
    assert [r["attempt_id"] for r in sol.pending_rows(db)] == ["att2"]   # row 2 read nothing
    assert rpc.calls.count("getTransaction") == 1


def test_a_transport_failure_stops_the_pass_instead_of_moving_on(tmp_path, ledger_fns):
    db = _two_rows(tmp_path)
    calls = []

    def boom(method, params):
        calls.append(method)
        raise TimeoutError("429 or timeout")
    sol.run_t1_once(db, Ledger(_attempt()), boom, flat_fn=lambda: True, now_ts=D + 60)
    assert calls == ["getTransaction"] and len(sol.pending_rows(db)) == 2


def test_a_row_waits_two_days_for_a_flat_book_before_it_expires(tmp_path, ledger_fns):
    db, rpc = _db(tmp_path), Rpc(_market())
    sol.run_t1_once(db, Ledger(_attempt()), rpc, flat_fn=lambda: False, now_ts=D + 24 * 3600)
    assert len(sol.pending_rows(db)) == 1                    # the 23.4 h busy stretch is not enough
    sol.run_t1_once(db, Ledger(_attempt()), rpc, flat_fn=lambda: False, now_ts=D + 49 * 3600)
    assert sol.pending_rows(db) == []


def test_t0_records_the_solana_book_at_the_decision():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_positions)")}
    base = {"mint": MINT, "network_id": "solana", "execution_mode": "real", "source_user_id": "u",
            "strategy": "s"}
    rows = [dict(base, opened_at="2026-10-02T00:00:00+00:00", is_open=1, closed_at=None),
            dict(base, mint=MINT + "2", opened_at="2026-10-02T00:00:00+00:00", is_open=0,
                 closed_at="2026-10-02T00:30:00+00:00"),
            dict(base, mint=MINT + "3", opened_at="2026-10-02T00:00:00+00:00", is_open=0,
                 closed_at="2026-10-02T00:05:00+00:00"),                       # closed before
            dict(base, mint=MINT + "5", opened_at="2026-10-02T00:01:00+00:00", is_open=0,
                 closed_at="2026-10-02T00:06:00+00:00"),                       # closed before
            dict(base, mint=MINT + "6", opened_at="2026-10-02T00:20:00+00:00", is_open=1,
                 closed_at=None),                                               # opened after
            dict(base, mint=MINT + "7", opened_at="2026-10-02T00:15:00+00:00", is_open=0,
                 closed_at="2026-10-02T00:40:00+00:00"),                       # opened after
            dict(base, mint=MINT + "8", execution_mode="paper", opened_at="2026-10-02T00:00:00+00:00",
                 is_open=1, closed_at=None),                                    # paper
            dict(base, mint=MINT + "9", opened_at="2026-10-02T00:00:00+00:00", is_open=1,
                 closed_at=None, tracking_paused_at="2026-10-02T00:02:00+00:00"),  # paused, as the gate
            # the fill stamps decide, as is_open does (Gamma LOW 5 on 7cf935c72)
            dict(base, mint=MINT + "A", opened_at="2026-10-02T00:09:00+00:00",
                 opened_recorded_at="2026-10-02T00:11:00+00:00", is_open=1, closed_at=None),
            dict(base, mint=MINT + "B", opened_at="2026-10-02T00:00:00+00:00", is_open=0,
                 closed_at="2026-10-02T00:09:00+00:00", closed_recorded_at="2026-10-02T00:11:00+00:00"),
            dict(base, mint=MINT + "C", opened_at="2026-10-02T00:00:00+00:00", is_open=0,
                 closed_at="2026-10-02T00:08:00+00:00", closed_recorded_at="2026-10-02T00:12:00+00:00"),
            dict(base, mint=MINT + "4", network_id="4663", opened_at="2026-10-02T00:00:00+00:00",
                 is_open=1, closed_at=None)]                                    # another chain
    for r in rows:
        keys = [k for k in r if k in cols]
        c.execute("INSERT INTO memecoin_positions (%s) VALUES (%s)" % (",".join(keys), ",".join("?" * len(keys))),
                  [r[k] for k in keys])
    assert m.real_positions_open_at(c, "solana", "2026-10-02T00:10:00+00:00") == 4   # 1, 2, B, C
    rec = cap._Row()
    cap._book_at_decision(c, {"network_id": "solana", "decision_started_at": "2026-10-02T00:10:00+00:00"}, rec)
    assert rec.features["sol_real_open_at_decision"] == 4
    rec = cap._Row()
    cap._book_at_decision(c, {"network_id": "4663", "decision_started_at": "2026-10-02T00:10:00+00:00"}, rec)
    assert rec.coverage["sol_real_open_at_decision"] == "not_applicable:chain"


# ---- Gamma on 7cf935c72 + 1847dc536 ----

def test_a_full_top20_list_with_no_holder_left_cannot_prove_the_top1():
    rpc = HolderRpc([("P%d" % i, 30.0) for i in range(20)], [CURVE] * 20)
    out = _holders(rpc)
    assert out["top1_share"][:2] == (None, "unavailable:top20_exhausted")
    rpc = HolderRpc([("P%d" % i, 30.0) for i in range(3)], [CURVE] * 3)   # all listed: none exist
    assert _holders(rpc)["top1_share"][:2] == (0.0, "ok")


def test_our_own_wallet_is_not_a_holder_on_the_production_worker_path(tmp_path, ledger_fns,
                                                                      monkeypatch):
    import sniper.settings.memecoin as mc_settings
    monkeypatch.setattr(mc_settings, "MEMECOIN_OWN_ADDRESSES", "0xabc, OurSolWallet111")
    db = _db_with(tmp_path, sol.HOLDER_NAMES)
    rpc = HolderRpc([("OurAta", 500.0), ("Acct2", 10.0)], ["OurSolWallet111", "Holder2"])
    sol.run_t1_once(db, Ledger(_attempt()), rpc, flat_fn=lambda: True, now_ts=D + 60)
    f = json.loads(db.execute("SELECT features_json FROM entry_features").fetchone()[0])
    assert f["top1_share"] == pytest.approx(0.01)


def test_t0_names_the_holder_source_as_the_worker_does(monkeypatch):
    monkeypatch.setenv("MEMECOIN_ENTRY_CAPTURE_SOL_HOLDERS", "1")
    rec = cap._Row()
    cap._holders({"network_id": "solana", "terminal_reason": "chase_cap",
                  "terminal_status": "refused"}, rec)
    assert rec.sources["top10_share"] == "solana_rpc:top20_token_accounts"


def test_the_read_splits_holders_by_lag_and_tier(tmp_path, capsys):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))
    import entry_capture_coverage as read
    db = store.connect(tmp_path / "f.db")
    for aid, tier, lag in (("a", 1, 30.0), ("b", 1, 61.0), ("c", 2, 3601.0), ("d", 2, None)):
        cov = {"top10_share": "ok" if lag is not None else sol.PENDING, "flow_30s": "ok"}
        store.insert(db, {"attempt_id": aid, "network_id": "solana", "mint": MINT, "tier": tier,
                          "decision_at": cap._iso(D), "feature_version": 1,
                          "features_json": json.dumps({"sol_real_open_at_decision": 0}),
                          "coverage_json": json.dumps(cov), "sources_json": "{}",
                          "read_lag_json": json.dumps({"top10_share": lag} if lag else {}),
                          "computed_at": "x"})
    db.close()
    read.main(["--since", "2000-01-01", "--db", str(tmp_path / "f.db")])
    out = " ".join(capsys.readouterr().out.split())
    for line in ("tier 1 lag <=60s ok 1", "tier 1 lag <=1h ok 1", "tier 2 lag >1h ok 1",
                 "tier 2 lag none pending 1"):
        assert "top10_share " + line in out


# ---- Codex review 2026-10-06: R5 liquidity is not a trade; R7 an unknown owner is not a holder ----

@pytest.mark.parametrize("wsol", [False, True])
def test_a_liquidity_add_or_withdrawal_is_not_a_trade(wsol):
    """The review's repro: 100 coins and 1 SOL into the pool read as a 100-coin SELL, out of it as
    a BUY. A trade moves the pool's quote side opposite to its coin vault."""
    pool = {"vault": VAULT, "owner": CURVE}
    pool["quote"] = sol.quote_binding(_buy(SRC_SIG, D - 10, 100, 5, 10.0, 1.0, wsol=wsol), pool, MINT)
    add = _tx("f" * 88, D - 2, 113, 0, coin_pre=1_000_000.0, coin_post=1_000_100.0,
              sol_pre=100.0, sol_post=101.0, wsol=wsol)
    remove = _tx("g" * 88, D - 2, 113, 1, coin_pre=1_000_000.0, coin_post=999_900.0,
                 sol_pre=100.0, sol_post=99.0, wsol=wsol)
    assert sol.trade_of(add, pool, MINT) is None and sol.trade_of(remove, pool, MINT) is None
    assert sol.trade_of(_buy("h" * 88, D - 2, 113, 2, 10.0, 1.0, wsol=wsol), pool, MINT)[0] == 1
    assert sol.trade_of(_sell("i" * 88, D - 2, 113, 3, 10.0, 1.0, wsol=wsol), pool, MINT)[0] == -1
    out = sol.flow_features(_attempt(), Rpc(_market([add, remove])), own_sigs=set())
    assert (out["flow_30s"][0]["buy_n"], out["flow_30s"][0]["sell_n"]) == (3, 1)


@pytest.mark.parametrize("owners", [["Holder1", None, "Holder3"], ["Holder1"]],
                         ids=["null_account", "short_answer"])
def test_a_holder_whose_owner_cannot_be_read_makes_the_shares_unavailable(owners):
    """The review's repro: a 900-token burn account read as unknown gave top1 0.9 labelled ok."""
    rpc = HolderRpc([("Acct1", 50.0), ("Burn", 900.0), ("Acct3", 10.0)], owners)
    out = _holders(rpc)
    assert {out[n][1] for n in sol.HOLDER_NAMES} == {"unavailable:owner_unknown"}
    assert {out[n][0] for n in sol.HOLDER_NAMES} == {None}



@pytest.mark.parametrize("wsol", [False, True])
def test_another_mint_under_the_same_authority_is_no_quote_proof(wsol):
    """Codex night review R5 (2026-10-06): coin +100, the real quote +1 (the same way: liquidity) and
    an unrelated mint the same authority holds -1 read as a 100-coin SELL. Only the pool's own quote
    account, bound from its source buy, counts."""
    pool = {"vault": VAULT, "owner": CURVE}
    pool["quote"] = sol.quote_binding(_buy(SRC_SIG, D - 10, 100, 5, 10.0, 1.0, wsol=wsol), pool, MINT)
    assert pool["quote"][0] == ("token" if wsol else "lamports")
    mixed = _tx("j" * 88, D - 2, 113, 4, coin_pre=1_000_000.0, coin_post=1_000_100.0,
                sol_pre=100.0, sol_post=101.0, wsol=wsol)
    keys = mixed["transaction"]["message"]["accountKeys"]
    keys.append("Other" + "1" * 39)
    other = lambda amt: {"accountIndex": len(keys) - 1, "mint": "OtherMint" + "1" * 35, "owner": CURVE,
                         "uiTokenAmount": {"uiAmount": amt}}
    mixed["meta"]["preTokenBalances"].append(other(50.0))
    mixed["meta"]["postTokenBalances"].append(other(49.0))
    mixed["meta"]["preBalances"].append(0)
    mixed["meta"]["postBalances"].append(0)
    assert sol.trade_of(mixed, pool, MINT) is None


def test_a_source_whose_quote_cannot_be_bound_is_unavailable():
    """Two of the pool authority's other accounts rose in the source buy: no single quote account."""
    src = _buy(SRC_SIG, D - 10, 100, 5, 10.0, 1.0, wsol=True)
    keys = src["transaction"]["message"]["accountKeys"]
    keys.append("Other" + "1" * 39)
    other = lambda amt: {"accountIndex": len(keys) - 1, "mint": "OtherMint" + "1" * 35, "owner": CURVE,
                         "uiTokenAmount": {"uiAmount": amt}}
    src["meta"]["preTokenBalances"].append(other(50.0))
    src["meta"]["postTokenBalances"].append(other(51.0))
    src["meta"]["preBalances"].append(0)
    src["meta"]["postBalances"].append(0)
    out = sol.flow_features(_attempt(), Rpc(_market(), source=src), own_sigs=set())
    assert out["flow_30s"][1] == "unavailable:quote_unbound"


# ---- Codex follow-up 2026-10-07, R5: a trade is bound to ONE swap instruction, or it is censored ----

def _pool(wsol):
    pool = {"vault": VAULT, "owner": CURVE}
    pool["quote"] = sol.quote_binding(_buy(SRC_SIG, D - 10, 100, 5, 10.0, 1.0, wsol=wsol), pool, MINT)
    return pool


WV = "WsolVau1t" + "1" * 35


def test_a_deposit_plus_a_fee_withdrawal_from_the_same_vault_is_unproved_and_censors_its_window():
    """The review's compound fixture: deposit +100 coin and +0.0098 SOL quote, then 0.02 SOL of
    protocol fees out of the same quote vault in another instruction. Net: coin +100, quote -0.0102,
    which the sign test read as a sell. No swap happened."""
    compound = _tx("k" * 88, D - 2, 113, 0, coin_pre=1_000_000.0, coin_post=1_000_100.0,
                   sol_pre=100.0, sol_post=100.0 - 0.0102, wsol=True,
                   groups=[[(USER_COIN, VAULT, _atoms(100, COIN_DEC)), (USER_WSOL, WV, _atoms(0.0098, WSOL_DEC))],
                           [(WV, FEE, _atoms(0.02, WSOL_DEC))]])
    assert sol.trade_of(compound, _pool(True), MINT) is sol.UNPROVED
    src = _buy(SRC_SIG, D - 10, 100, 5, 10.0, 1.0, wsol=True)
    out = sol.flow_features(_attempt(), Rpc([src, compound], source=src), own_sigs=set())
    assert out["flow_30s"][1] == "unavailable:unproved_operation"     # never a quiet undercount
    assert out["pre_source_30s"][1] == "ok"                           # a window without it stands


def test_mixed_directions_inside_one_instruction_are_unproved():
    one = _tx("l" * 88, D - 2, 113, 0, coin_pre=1_000_000.0, coin_post=1_000_100.0,
              sol_pre=100.0, sol_post=100.0 - 0.0102, wsol=True,
              groups=[[(USER_COIN, VAULT, _atoms(100, COIN_DEC)), (USER_WSOL, WV, _atoms(0.0098, WSOL_DEC)),
                       (WV, FEE, _atoms(0.02, WSOL_DEC))]])
    assert sol.trade_of(one, _pool(True), MINT) is sol.UNPROVED


def test_a_sell_that_pays_fees_out_of_the_quote_vault_inside_its_swap_is_a_sell():
    """PumpSwap-shaped: the quote vault pays the seller and the fee recipients in the same swap."""
    sell = _tx("m" * 88, D - 2, 113, 0, coin_pre=1_000_000.0, coin_post=1_000_010.0,
               sol_pre=100.0, sol_post=99.0, wsol=True,
               groups=[[(USER_COIN, VAULT, _atoms(10, COIN_DEC)), (WV, USER_WSOL, _atoms(0.9, WSOL_DEC)),
                        (WV, FEE, _atoms(0.1, WSOL_DEC))]])
    side, coin, sol_amt, _maker = sol.trade_of(sell, _pool(True), MINT)
    assert (side, coin, sol_amt) == (-1, pytest.approx(10.0), pytest.approx(1.0))


@pytest.mark.parametrize("wsol", [False, True])
def test_a_coin_move_the_transfers_do_not_explain_is_unproved(wsol):
    short = _tx("n" * 88, D - 2, 113, 0, coin_pre=1_000_000.0, coin_post=999_990.0, sol_pre=100.0,
                sol_post=101.0, wsol=wsol,
                groups=[[(VAULT, USER_COIN, _atoms(9, COIN_DEC))]
                        + ([(USER_WSOL, WV, _atoms(1.0, WSOL_DEC))] if wsol else [])])
    assert sol.trade_of(short, _pool(wsol), MINT) is sol.UNPROVED
    bare = _tx("o" * 88, D - 2, 113, 1, coin_pre=1_000_000.0, coin_post=999_990.0, sol_pre=100.0,
               sol_post=101.0, wsol=wsol, groups=[[]])
    assert sol.trade_of(bare, _pool(wsol), MINT) is sol.UNPROVED           # no transfer evidence


def test_coin_moves_split_across_two_instructions_are_unproved():
    two = _tx("p" * 88, D - 2, 113, 0, coin_pre=1_000_000.0, coin_post=999_980.0, sol_pre=100.0,
              sol_post=102.0, groups=[[(VAULT, USER_COIN, _atoms(10, COIN_DEC))],
                                      [(VAULT, USER_COIN, _atoms(10, COIN_DEC))]])
    assert sol.trade_of(two, _pool(False), MINT) is sol.UNPROVED            # two curve buys in one tx


def test_a_token_2022_transfer_fee_is_netted_from_what_the_vault_receives():
    """TransferCheckedWithFee: the vault gets amount minus the withheld fee (10.0 - 0.3)."""
    sell = _tx("q" * 88, D - 2, 113, 0, coin_pre=1_000_000.0, coin_post=1_000_009.7, sol_pre=100.0,
               sol_post=99.0, groups=[[(USER_COIN, VAULT, _atoms(10, COIN_DEC), _atoms(0.3, COIN_DEC))]])
    side, coin, _sol, _maker = sol.trade_of(sell, _pool(False), MINT)
    assert side == -1 and coin == pytest.approx(9.7)


def test_a_zero_atom_transfer_does_not_break_a_swaps_direction():
    """Gamma on 85830c1d7: a 0-atom transfer touching the vault counted as an outflow beside the
    inflows, so a real sell read as mixed direction and unproved."""
    sell = _tx("r" * 88, D - 2, 113, 0, coin_pre=1_000_000.0, coin_post=1_000_010.0, sol_pre=100.0,
               sol_post=99.0, wsol=True,
               groups=[[(USER_COIN, VAULT, _atoms(10, COIN_DEC)), (USER_COIN, VAULT, 0),
                        (WV, USER_WSOL, _atoms(1.0, WSOL_DEC))]])
    assert sol.trade_of(sell, _pool(True), MINT)[0] == -1
