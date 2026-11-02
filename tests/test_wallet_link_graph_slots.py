"""Wallet graph slot edges (spec docs/superpowers/specs/2026-09-29-memecoin-wallet-link-graph-design.md
section 2; Alpha GO 2026-09-29): co_buy_slot and slot_follower, read around the armed wallets' own
Solana buys (onchain_wallet_detections) on the research Helius key.

- One read per source buy: every succeeded transaction on the mint in the source's blockTime second,
  kept to the source's slot. A failed read is 'unread' (censored), never "no companions".
- Reads stop at the per-run call budget, and while a real Solana position or reservation is open.
- An edge needs >= 3 coins AND must beat a binomial null against the companion's own base rate
  (its share of all read source slots), so a crowd executor in every slot is not an edge.
"""
from scripts.research import wallet_link_graph as g

MINT = "Mint111"


def _tx(sig, slot, index, trader=None, side="buy", sol=1.0):
    return {"sig": sig, "slot": slot, "transactionIndex": index, "trade": (
        {"trader": trader, "side": side, "sol": sol} if trader else None)}


def _trade(tx, mint):
    return tx["trade"]


def _sig(tx):
    return tx["sig"]


def test_a_slot_read_keeps_other_buyers_in_the_source_slot():
    txs = [_tx("before", 99, 5, "X"), _tx("src", 100, 7, "A"), _tx("early", 100, 3, "B"),
           _tx("late", 100, 9, "C"), _tx("sell", 100, 10, "D", side="sell"), _tx("ours", 100, 11, "US"),
           _tx("undecoded", 100, 12), _tx("next", 101, 1, "E"), _tx("self", 100, 13, "A")]
    read = g.slot_read(txs, "src", MINT, ours={"ours"}, trade_fn=_trade, sig_fn=_sig)
    assert (read["status"], read["slot"], read["index"]) == ("ok", 100, 7)
    assert read["companions"] == [{"trader": "B", "index": 3, "amount": 1.0, "unit": None},
                                  {"trader": "C", "index": 9, "amount": 1.0, "unit": None}]
    assert read["undecoded"] == 1


def test_a_read_without_the_source_transaction_is_not_found():
    read = g.slot_read([_tx("other", 100, 1, "B")], "src", MINT, ours=set(), trade_fn=_trade, sig_fn=_sig)
    assert read == {"status": "not_found"}


def _read(uid, mint, slot, index, companions, status="ok"):
    return {"source_user_id": uid, "network_id": "solana", "mint": mint, "status": status, "slot": slot,
            "index": index, "companions": [{"trader": t, "index": i, "sol": 1.0} for t, i in companions],
            "detected_at": "2026-09-2%dT12:00:00+00:00" % (slot % 9)}


def _population(extra=()):
    """30 source buys by 10 other wallets with a crowd executor CROWD in 25 of them."""
    out = []
    for i in range(30):
        out.append(_read("U%d" % (i % 10), "p%d" % i, 1000 + i, 50, [("CROWD", 60)] if i < 25 else []))
    return out + list(extra)


def test_a_repeat_slot_companion_is_an_edge_and_the_crowd_is_not():
    reads = _population([_read("A", "a%d" % i, 2000 + i, 40, [("B", 45), ("CROWD", 30)]) for i in range(3)])
    edges = {(e["edge_type"], e["src"], e["dst"]): e for e in g.slot_edges(reads, owners={})}
    co = edges[("co_buy_slot", "A", "B")]
    assert co["n_coins"] == 3 and co["evidence"]["null"]["p"] <= 0.01
    assert ("slot_follower", "A", "B") in edges                       # B landed after A every time
    assert not any(k[2] == "CROWD" for k in edges)                    # in 28 of 33 slots: base rate


def test_two_coins_are_not_a_slot_edge():
    reads = _population([_read("A", "a%d" % i, 2000 + i, 40, [("B", 45)]) for i in range(2)])
    assert g.slot_edges(reads, owners={}) == []


def test_a_companion_ahead_of_the_source_is_not_a_slot_follower():
    reads = _population([_read("A", "a%d" % i, 2000 + i, 40, [("B", 10)]) for i in range(3)])
    kinds = {e["edge_type"] for e in g.slot_edges(reads, owners={}) if e["dst"] == "B"}
    assert kinds == {"co_buy_slot"}


def test_unread_slots_are_censored_not_empty():
    """An unread slot counts in neither the source's coins nor the base rates, and is on coverage."""
    reads = _population([_read("A", "a%d" % i, 2000 + i, 40, [("B", 45)]) for i in range(3)])
    reads += [_read("A", "u%d" % i, 3000 + i, None, [], status="unread") for i in range(5)]
    (co,) = [e for e in g.slot_edges(reads, owners={}) if e["edge_type"] == "co_buy_slot"]
    assert co["evidence"]["null"]["n"] == 3 and co["coverage"]["slot_reads"] == {"ok": 33, "unread": 5}


def test_a_proven_roster_address_is_named_by_its_source_user_id():
    reads = _population([_read("A", "a%d" % i, 2000 + i, 40, [("AddrOfK", 45)]) for i in range(3)])
    (co,) = [e for e in g.slot_edges(reads, owners={"AddrOfK": "K-uid"}) if e["edge_type"] == "co_buy_slot"]
    assert (co["dst"], co["dst_user_id"]) == ("K-uid", "K-uid")


def test_reads_stop_at_the_budget_and_while_the_book_is_open():
    dets = [{"signature": "s%d" % i, "mint": MINT, "block_time": 1000 + i, "source_user_id": "A",
             "detected_at": "2026-09-29T00:00:00+00:00"} for i in range(10)]

    def fetch(det):
        return [_tx(det["signature"], 5, 1, "A")], 2, False            # two calls per read
    reads, calls, why = g.read_slots(dets, fetch=fetch, budget=5, flat_fn=lambda: True, ours=set(),
                                     trade_fn=_trade, sig_fn=_sig, max_calls_per_read=2)
    assert (len(reads), calls, why) == (2, 4, "budget")                 # a hard cap: never over 5
    flat = iter([True, True, False])
    reads, calls, why = g.read_slots(dets, fetch=fetch, budget=100, flat_fn=lambda: next(flat), ours=set(),
                                     trade_fn=_trade, sig_fn=_sig)
    assert (len(reads), why) == (2, "solana book open")


def test_a_failed_read_is_recorded_unread_and_stops_the_run():
    dets = [{"signature": "s%d" % i, "mint": MINT, "block_time": 1000 + i, "source_user_id": "A",
             "detected_at": "2026-09-29T00:00:00+00:00"} for i in range(3)]

    def fetch(det):
        raise g.ReadStop("HTTP 429 max usage")
    reads, calls, why = g.read_slots(dets, fetch=fetch, budget=100, flat_fn=lambda: True, ours=set(),
                                     trade_fn=_trade, sig_fn=_sig)
    assert [r["status"] for r in reads] == ["unread"] and why.startswith("stopped: HTTP 429")


def test_slot_reads_persist_and_only_unread_ones_are_retried(tmp_path):
    graph = tmp_path / "graph.db"
    g.save_reads(graph, [dict(_read("A", "a0", 1, 1, [("B", 2)]), signature="s0"),
                         dict(_read("A", "a1", 2, None, [], status="unread"), signature="s1")], read_at="t1")
    dets = [{"signature": s} for s in ("s0", "s1", "s2")]
    assert [d["signature"] for d in g.pending_detections(graph, dets)] == ["s1", "s2"]
    assert {r["signature"]: r["status"] for r in g.load_reads(graph)} == {"s0": "ok", "s1": "unread"}


def _page(n, token):
    return {"data": [{"slot": 5, "transactionIndex": i} for i in range(n)], "paginationToken": token}


def test_a_short_page_ends_the_read():
    """Gamma LOW 2: Helius returns a paginationToken even on a short page; following it doubled the
    spend (2.1 calls per read)."""
    calls = []
    def rpc(method, params):
        calls.append(params[1].get("paginationToken"))
        return _page(40, "tok")
    txs, used, truncated = g.helius_slot_fetch({"mint": MINT, "block_time": 1}, rpc=rpc)
    assert (len(txs), used, truncated, calls) == (40, 1, False, [None])


def test_a_read_still_full_after_the_page_cap_is_truncated():
    txs, used, truncated = g.helius_slot_fetch({"mint": MINT, "block_time": 1},
                                               rpc=lambda m, p: _page(100, "more"))
    assert (len(txs), used, truncated) == (100 * g.SLOT_PAGES, g.SLOT_PAGES, True)


def test_the_book_is_checked_before_every_page():
    """Gamma LOW 1: a read makes up to 3 calls with 429 backoffs between them; a position that opens
    mid-read stops the next page."""
    flat = iter([True, False])
    try:
        g.helius_slot_fetch({"mint": MINT, "block_time": 1}, rpc=lambda m, p: _page(100, "more"),
                            flat_fn=lambda: next(flat))
    except g.ReadStop as exc:
        assert "book open" in str(exc)
    else:
        raise AssertionError("the second page ran with the book open")


def test_truncated_and_not_found_reads_are_on_coverage():
    reads = _population([_read("A", "a%d" % i, 2000 + i, 40, [("B", 45)]) for i in range(3)])
    reads[0]["truncated"] = True
    reads += [_read("A", "nf", 3000, None, [], status="not_found")]
    (co,) = [e for e in g.slot_edges(reads, owners={}) if e["edge_type"] == "co_buy_slot"]
    assert co["coverage"]["slot_reads"] == {"ok": 33, "not_found": 1, "truncated": 1}



WSOL, USDC, WETH = ("So11111111111111111111111111111111111111112",
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs")


def _bal(owner, mint, amount, dec):
    return {"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(amount), "decimals": dec}}


def _pool_buy(quote, dec, paid):
    """Buyer B SIGNS and pays `paid` of `quote` from its own balance (SOL from native), as trade_of
    reads a pool trade since 2026-10-01."""
    sol = quote == WSOL
    pre = [_bal("P", MINT, 1000, 6), _bal("P", quote, 10 ** dec, dec), _bal("B", MINT, 0, 6)]
    post = [_bal("P", MINT, 900, 6), _bal("P", quote, 10 ** dec + paid, dec), _bal("B", MINT, 100, 6)]
    if not sol:
        pre.append(_bal("B", quote, 10 * 10 ** dec, dec))
        post.append(_bal("B", quote, 10 * 10 ** dec - paid, dec))
    return {"transaction": {"message": {"accountKeys": ["B", "P"], "header": {"numRequiredSignatures": 1}}},
            "meta": {"err": None, "logMessages": [], "fee": 0,
                     "preBalances": [10 ** 12, 0], "postBalances": [10 ** 12 - (paid if sol else 0), 0],
                     "preTokenBalances": pre, "postTokenBalances": post}}


def test_a_companion_buy_carries_its_quote_amount_and_unit():
    """Alpha GO 2026-09-29: the copier-crowd read had follower USD censored. The slot read now keeps
    each companion's quote amount with its unit (trade_of mixes SOL, USDC and fixed-price USD)."""
    assert g._default_trade(_pool_buy(WSOL, 9, 5 * 10 ** 9), MINT)[["amount", "unit"][0]] == 5.0
    for quote, dec, paid, want in ((WSOL, 9, 5 * 10 ** 9, (5.0, "SOL")), (USDC, 6, 20 * 10 ** 6, (20.0, "USDC")),
                                   (WETH, 8, 10 ** 8, (1.0, "WETH"))):
        t = g._default_trade(_pool_buy(quote, dec, paid), MINT)
        assert (t["trader"], t["side"], round(t["amount"], 6), t["unit"]) == ("B", "buy") + want, quote


def _owner_tx(sig, t, owner, mint, pre, post):
    return {"blockTime": t, "transaction": {"signatures": [sig]},
            "meta": {"err": None, "preTokenBalances": [_bal(owner, mint, pre, 6)],
                     "postTokenBalances": [_bal(owner, mint, post, 6)]}}


def test_the_source_buy_is_the_latest_increase_of_the_mint_before_the_fill():
    txs = [_owner_tx("other", 990, "S", "OTHER", 0, 5), _owner_tx("sell", 950, "S", MINT, 10, 0),
           _owner_tx("buy", 900, "S", MINT, 0, 10), _owner_tx("older", 800, "S", MINT, 0, 3)]
    assert g.find_source_buy(txs, "S", MINT, before=1000) == ("buy", 900)
    assert g.find_source_buy(txs, "S", MINT, before=899) == ("older", 800)
    assert g.find_source_buy(txs, "S", "NONE", before=1000) is None


def _fill(pid, uid="U", mint=MINT, t="2026-09-29T00:00:00+00:00"):
    return {"id": pid, "source_user_id": uid, "mint": mint, "opened_at": t}


def test_fill_reads_find_the_source_buy_then_read_its_slot():
    fills = [_fill(1, "pump:ADDR1"), _fill(2, "uid-k"), _fill(3, "uid-nobody"), _fill(4, "pump:ADDR4")]
    buys = {"ADDR1": ("sig1", 1000), "ADDRK": ("sigk", 1001), "ADDR4": None}

    def fetch_buy(address, mint, opened):
        return buys[address], 1

    def fetch_slot(det):
        return [_tx(det["signature"], 7, 2, "SRC"), _tx("x", 7, 5, "F")], 1, False
    reads, status, calls, why = g.read_fills(fills, addresses={"uid-k": ["ADDRK"]}, fetch_buy=fetch_buy,
                                             fetch_slot=fetch_slot, budget=100, flat_fn=lambda: True, ours=set(),
                                             trade_fn=_trade, sig_fn=_sig)
    assert {s["position_id"]: s["status"] for s in status} == {1: "ok", 2: "ok", 3: "no_address", 4: "no_buy_found"}
    assert calls == 5 and why == "done"                                # 2 + 2 + 0 + 1
    assert [(r["signature"], r["source_user_id"], r["status"]) for r in reads] == [
        ("sig1", "pump:ADDR1", "ok"), ("sigk", "uid-k", "ok")]
    assert reads[0]["detected_at"].startswith("1970-01-01T00:16:40")   # the buy's block time
    assert reads[0]["companions"] == [{"trader": "F", "index": 5, "amount": 1.0, "unit": None}]


def test_fill_reads_stop_at_the_budget_the_book_and_a_failed_read():
    fills = [_fill(i, "pump:A%d" % i) for i in range(5)]
    fb = lambda a, m, o: (("s" + a, 1000), 1)                            # noqa: E731
    fs = lambda d: ([_tx(d["signature"], 1, 1, "SRC")], 1, False)       # noqa: E731
    kw = dict(addresses={}, fetch_buy=fb, fetch_slot=fs, ours=set(), trade_fn=_trade, sig_fn=_sig)
    reads, _st, calls, why = g.read_fills(fills, budget=7, flat_fn=lambda: True, **kw)
    assert (len(reads), calls, why) == (2, 4, "budget")                 # a fill may cost 1 + SLOT_PAGES
    flat = iter([True, False])
    reads, _st, _c, why = g.read_fills(fills, budget=100, flat_fn=lambda: next(flat), **kw)
    assert (len(reads), why) == (1, "solana book open")

    def boom(a, m, o):
        raise g.ReadStop("HTTP 429 max usage")
    _r, status, _c, why = g.read_fills(fills, budget=100, flat_fn=lambda: True, **dict(kw, fetch_buy=boom))
    assert [s["status"] for s in status] == ["unread"] and why.startswith("stopped")


def test_fill_statuses_persist_and_only_unread_ones_are_retried(tmp_path):
    graph = tmp_path / "graph.db"
    g.save_fill_status(graph, [{"position_id": 1, "status": "ok", "signature": "s"},
                               {"position_id": 2, "status": "unread"},
                               {"position_id": 3, "status": "no_address"}], read_at="t")
    assert [f["id"] for f in g.pending_fills(graph, [_fill(1), _fill(2), _fill(3), _fill(4)])] == [2, 4]


def test_solana_owners_include_every_verified_registry_address(monkeypatch):
    """2026-09-29: the graph mapped Solana addresses through watcher_address_owners, which knows 2
    Solana wallets; the address registry holds 83 verified ones (311 of 349 funded fills). Verified
    registry rows count whether or not the wallet is armed now; refuted rows never do."""
    import sqlite3
    from sniper.services import axiom_address_verify as av
    monkeypatch.setattr(av, "watcher_address_owners", lambda conn: {("solana", "AxAddr"): ("uid-ax", "h")})
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE memecoin_wallets (source_user_id TEXT, handle TEXT, real_approved INTEGER, real_chains TEXT,
                                       paused INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0);
        CREATE TABLE memecoin_address_registry (source_user_id TEXT, network_id TEXT, address TEXT, status TEXT,
                                                evidence_kind TEXT, evidence_json TEXT, handle_seen TEXT,
                                                first_seen_at TEXT, status_at TEXT, conflict_at TEXT);
        INSERT INTO memecoin_wallets VALUES ('uid-k', 'kay', 0, NULL, 0, 0), ('pump:4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9', 'p', 0, NULL, 0, 0);
        INSERT INTO memecoin_address_registry VALUES
          ('uid-k', 'solana', 'KAddr', 'verified', 'fomo_buy', NULL, 'kay', 't', 't', NULL),
          ('uid-r', 'solana', 'RAddr', 'refuted', 'fomo_buy', NULL, 'r', 't', 't', NULL);
    """)
    assert g.solana_owners(c) == {"AxAddr": "uid-ax", "KAddr": "uid-k", "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9": "pump:4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"}


def test_a_fill_already_covered_by_a_watcher_read_is_not_read_again():
    reads = [{"status": "ok", "source_user_id": "U", "mint": MINT, "detected_at": "2026-09-28T23:55:00+00:00"},
             {"status": "unread", "source_user_id": "V", "mint": MINT, "detected_at": "2026-09-28T23:59:00+00:00"},
             {"status": "ok", "source_user_id": "W", "mint": MINT, "detected_at": "2026-09-28T23:40:00+00:00"}]
    fills = [_fill(1, "U"), _fill(2, "V"), _fill(3, "W")]
    assert [f["id"] for f in g.fills_without_reads(fills, reads)] == [2, 3]   # unread, and 20 min old



def test_a_fill_with_the_source_signature_reads_that_slot_directly():
    """Gamma LOW 1 / Alpha 2026-09-29: 269 of 367 fills carry the source signature in origin_event_id.
    That exact buy is read, even when the source bought the coin again later; no address search runs."""
    fills = [dict(_fill(1, "pump:ADDR1"), source_sig="copied")]

    def fetch_buy(address, mint, opened):
        raise AssertionError("no address search for a fill with its source signature")
    seen = []

    def fetch_slot(det):
        seen.append(det)
        tx = _tx("copied", 7, 2, "SRC")
        tx["blockTime"] = 800
        return [tx, _tx("x", 7, 5, "F")], 1, False
    reads, status, calls, why = g.read_fills(fills, addresses={}, fetch_buy=fetch_buy, fetch_slot=fetch_slot,
                                             budget=100, flat_fn=lambda: True, ours=set(), trade_fn=_trade,
                                             sig_fn=_sig, slots={"copied": 7})
    assert seen[0]["slot"] == 7 and seen[0]["signature"] == "copied"
    assert [(r["signature"], r["detected_at"][:19]) for r in reads] == [("copied", "1970-01-01T00:13:20")]
    assert status == [{"position_id": 1, "status": "ok", "signature": "copied"}] and calls == 1


def test_a_signature_without_a_known_slot_falls_back_to_the_address_search():
    fills = [dict(_fill(1, "pump:ADDR1"), source_sig="unknown-sig")]
    reads, status, calls, _why = g.read_fills(
        fills, addresses={}, fetch_buy=lambda a, m, o: (("later", 900), 1),
        fetch_slot=lambda d: ([_tx(d["signature"], 7, 2, "SRC")], 1, False), budget=100,
        flat_fn=lambda: True, ours=set(), trade_fn=_trade, sig_fn=_sig, slots={})
    assert status[0]["signature"] == "later" and calls == 2


def test_signature_slots_come_from_batched_status_calls():
    calls = []

    def rpc(method, params):
        calls.append((method, len(params[0]), params[1]))
        return {"value": [{"slot": 100 + i} if i % 2 == 0 else {"slot": 1, "err": {"InstructionError": [0, "x"]}}
                          if i % 4 == 1 else None for i in range(len(params[0]))]}
    sigs = ["s%d" % i for i in range(300)]
    slots, used = g.helius_sig_slots(sigs, rpc=rpc)
    assert used == 2 and [c[1] for c in calls] == [256, 44] and calls[0][2] == {"searchTransactionHistory": True}
    assert slots["s0"] == 100 and slots["s256"] == 100
    assert "s1" not in slots and "s3" not in slots                    # a failed source tx, and an unknown one


def test_a_slot_read_by_slot_filters_on_the_slot():
    seen = []

    def rpc(method, params):
        seen.append(params[1]["filters"])
        return {"data": [], "paginationToken": None}
    g.helius_slot_fetch({"mint": MINT, "slot": 7, "signature": "s"}, rpc=rpc)
    assert seen == [{"status": "succeeded", "slot": {"gte": 7, "lte": 7}}]
