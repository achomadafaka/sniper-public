"""Wallet Map slice 3: flow change around a source buy (spec
docs/superpowers/specs/2026-09-30-memecoin-wallet-map-slice3-impact-design.md, Gamma CLEAN 45c9385ae).
Pure: page fixtures, no network."""
import sqlite3

from scripts.research import wallet_link_graph as g

MINT = "Mint111"
T0 = 1_790_000_000


def _tx(sig, slot, index, t, trader=None, side="buy", sol=1.0, unit="SOL"):
    return {"sig": sig, "slot": slot, "transactionIndex": index, "blockTime": t,
            "trade": ({"trader": trader, "side": side, "amount": sol, "unit": unit} if trader else None)}


def _trade(tx, mint):
    return tx["trade"]


def _sig(tx):
    return tx["sig"]


SRC = _tx("src", 100, 7, T0, "A")


def _read(txs, excluded=(), ours=()):
    return g.impact_read(txs, "src", T0, MINT, excluded=set(excluded), ours=set(ours),
                         trade_fn=_trade, sig_fn=_sig)


def test_before_and_after_split_by_chain_order_inside_the_source_second():
    txs = [SRC, _tx("b1", 90, 1, T0 - 30, "B", sol=2.0), _tx("same_early", 100, 3, T0, "C", sol=1.0),
           _tx("same_late", 100, 9, T0, "D", sol=5.0), _tx("a_sell", 120, 2, T0 + 20, "E", side="sell", sol=1.5),
           _tx("too_early", 50, 1, T0 - 61, "F", sol=9.0), _tx("too_late", 200, 1, T0 + 61, "G", sol=9.0),
           _tx("undec", 110, 1, T0 + 5)]
    r = _read(txs)
    assert r["status"] == "ok"
    assert (r["net_before"], r["buyers_before"], r["tx_before"], r["undecoded_before"]) == (3.0, 2, 2, 0)
    assert (r["net_after"], r["buyers_after"], r["tx_after"], r["undecoded_after"]) == (3.5, 1, 3, 1)
    assert r["delta_sol"] == 0.5


def test_our_fills_the_sources_other_address_and_its_own_trader_are_not_market_flow():
    txs = [SRC, _tx("our_exit", 110, 1, T0 + 3, "US", side="sell", sol=3.0),
           _tx("alt", 111, 1, T0 + 4, "A2", sol=4.0), _tx("again", 112, 1, T0 + 5, "A", sol=2.0)]
    r = _read(txs, excluded={"A2"}, ours={"our_exit"})
    assert (r["status"], r["net_after"], r["buyers_after"], r["tx_after"]) == ("ok", 0.0, 0, 2)


def test_a_non_sol_trade_in_either_window_censors_the_read():
    assert _read([SRC, _tx("u", 90, 1, T0 - 5, "B", unit="USDC")])["status"] == "non_sol"
    assert _read([SRC, _tx("u", 110, 1, T0 + 5, "B", unit=None)])["status"] == "non_sol"


def test_a_missing_source_or_a_clock_mismatch_is_censored():
    assert _read([_tx("x", 100, 1, T0, "B")]) == {"status": "not_found"}
    assert _read([_tx("src", 100, 7, T0 + 1, "A")]) == {"status": "clock_mismatch"}


def test_empty_windows_are_a_known_zero_and_a_missing_amount_is_undecoded():
    r = _read([SRC])
    assert (r["status"], r["net_before"], r["net_after"], r["delta_sol"]) == ("ok", 0.0, 0.0, 0.0)
    r = _read([SRC, _tx("x", 110, 1, T0 + 2, "B", sol=None)])
    assert (r["net_after"], r["undecoded_after"], r["tx_after"]) == (0.0, 1, 1)


def _page(n, token, t=T0):
    return {"data": [{"slot": 5, "transactionIndex": i, "blockTime": t} for i in range(n)], "paginationToken": token}


def test_the_window_read_filters_plus_minus_60_s_and_caps_at_10_pages():
    seen = []

    def rpc(method, params):
        seen.append((params[0], params[1]["filters"], params[1]["sortOrder"], params[1]["limit"]))
        return _page(100, "more")
    txs, used, truncated = g.helius_impact_window(MINT, T0, rpc=rpc)
    assert (len(txs), used, truncated) == (1000, 10, True)
    assert seen[0] == (MINT, {"status": "succeeded", "blockTime": {"gte": T0 - 60, "lte": T0 + 60}}, "asc", 100)


def test_the_slot_read_keeps_its_three_page_cap():
    txs, used, truncated = g.helius_slot_fetch({"mint": MINT, "block_time": 1}, rpc=lambda m, p: _page(100, "more"))
    assert (used, truncated) == (g.SLOT_PAGES, True)


def test_the_age_probe_reads_the_mints_first_transaction():
    seen = []

    def rpc(method, params):
        seen.append(params[1])
        return {"data": [{"blockTime": T0 - 500}]}
    assert g.helius_first_tx_time(MINT, rpc=rpc) == (T0 - 500, 1)
    assert seen[0]["sortOrder"] == "asc" and seen[0]["limit"] == 1 and "filters" not in seen[0]
    assert g.helius_first_tx_time(MINT, rpc=lambda m, p: {"data": []}) == (None, 1)


def test_the_age_probe_stops_on_a_failed_call_or_an_open_book():
    def boom(method, params):
        raise OSError("down")
    for kw in ({"rpc": boom}, {"rpc": lambda m, p: {"data": []}, "flat_fn": lambda: False}):
        try:
            g.helius_first_tx_time(MINT, **kw)
        except g.ReadStop:
            continue
        raise AssertionError("no ReadStop for %r" % kw)


def _unit(sig="src", t0=T0, origin="detection", empties=0):
    return {"signature": sig, "source_user_id": "uA", "mint": MINT, "origin": origin, "t0": t0,
            "empty_probes": empties}


def _run(units, *, first=T0 - 600, window=None, truncated=False, budget=100, flat=lambda: True, stop=None):
    windows = []

    def fetch_age(u):
        return first, 1

    def fetch_window(u):
        windows.append(u["signature"])
        if stop:
            raise g.ReadStop(stop)
        return (window if window is not None else [SRC, _tx("b", 110, 1, T0 + 2, "B", sol=2.0)]), 1, truncated
    reads, calls, why = g.read_impacts(units, fetch_age=fetch_age, fetch_window=fetch_window, budget=budget,
                                       flat_fn=flat, excluded_for=lambda uid: set(), ours=set(),
                                       trade_fn=_trade, sig_fn=_sig)
    return reads, calls, why, windows


def test_a_mint_at_most_60_s_old_is_a_launch_read_with_no_window_call():
    reads, calls, why, windows = _run([_unit()], first=T0 - 60)
    assert (reads[0]["status"], reads[0]["age_s"], calls, windows) == ("launch", 60, 1, [])
    reads, calls, why, windows = _run([_unit()], first=T0 - 61)
    assert (reads[0]["status"], reads[0]["delta_sol"], calls, windows) == ("ok", 2.0, 2, ["src"])


def test_an_empty_age_probe_is_unread_then_probe_empty_after_three_runs():
    reads, *_ = _run([_unit(empties=0)], first=None)
    assert (reads[0]["status"], reads[0]["empty_probes"]) == ("unread", 1)
    reads, *_ = _run([_unit(empties=2)], first=None)
    assert (reads[0]["status"], reads[0]["empty_probes"]) == ("probe_empty", 3)


def test_truncation_wins_over_a_missing_source():
    reads, *_ = _run([_unit()], window=[_tx("other", 90, 1, T0 - 50, "B")], truncated=True)
    assert reads[0]["status"] == "truncated"


def test_a_read_starts_only_when_its_worst_case_fits_and_the_book_is_flat():
    assert _run([_unit()], budget=10)[:3] == ([], 0, "budget")
    assert len(_run([_unit()], budget=11)[0]) == 1
    assert _run([_unit()], flat=lambda: False)[:3] == ([], 0, "solana book open")


def test_a_failed_call_records_the_read_unread_and_ends_the_pass():
    reads, calls, why, _ = _run([_unit("src"), _unit("second")], stop="HTTP 429 max usage")
    assert [r["status"] for r in reads] == ["unread"] and why.startswith("stopped: HTTP 429")


def _slot(sig, uid, mint, detected_at, status="ok", slot=5, idx=1):
    return {"signature": sig, "source_user_id": uid, "network_id": "solana", "mint": mint, "status": status,
            "detected_at": detected_at, "slot": slot, "index": idx}


def test_units_take_the_first_buy_by_chain_time_with_origin_and_t0():
    reads = [_slot("d1", "uA", "M1", "2026-09-30T00:00:03+00:00"),        # detection: detected_at is HOST time
             _slot("d0", "uA", "M1", "2026-09-30T00:00:05+00:00"),
             _slot("f1", "uA", "M2", "2026-09-30T00:00:10+00:00"),        # fill-fed: detected_at is chain time
             _slot("bad", "uB", "M3", "2026-09-30T00:00:10+00:00", status="not_found")]
    base = int(g._ts("2026-09-30T00:00:00+00:00"))
    units = {u["signature"]: u for u in g.impact_units(reads, {"d1": base + 2, "d0": base + 1})}
    assert set(units) == {"d0", "f1"}                                     # d0 is earlier by chain time
    assert (units["d0"]["origin"], units["d0"]["t0"]) == ("detection", base + 1)
    assert (units["f1"]["origin"], units["f1"]["t0"]) == ("fill", base + 10)


def test_detection_block_times_skip_null_and_our_fill_sigs_include_exits():
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, mint, block_time, "
              "detected_at, created_at) VALUES ('s1','uA','a','M',100,'t','t'), ('s2','uA','a','M',NULL,'t','t')")
    assert g.detection_block_times(c, ["s1", "s2", "s3"]) == {"s1": 100}
    c.execute("INSERT INTO memecoin_positions (id, source_user_id, mint, network_id, execution_mode, is_open) "
              "VALUES (1,'uA','M','solana','real',0), (2,'uA','E','4663','real',0)")
    c.execute("INSERT INTO memecoin_fills (position_id, kind, tx_sig) VALUES (1,'entry','in'), (1,'exit','out'), "
              "(2,'entry','evm')")
    assert g.our_fill_sigs(c) == {"in", "out"}


def test_excluded_traders_hold_every_source_address_the_pump_id_and_ours():
    ex = g.excluded_traders({"A1": "uA", "A2": "uA", "B1": "uB"}, {"OURS"})
    assert ex("uA") == {"A1", "A2", "OURS"}
    assert ex("pump:PumpAddr") == {"PumpAddr", "OURS"}


def test_impact_reads_persist_and_only_unread_ones_are_pending(tmp_path):
    graph = tmp_path / "graph.db"
    base = {"source_user_id": "uA", "network_id": "solana", "mint": "M", "t0": 1, "calls": 1}
    g.save_impact_reads(graph, [dict(base, signature="ok1", origin="detection", status="ok", delta_sol=1.5),
                                dict(base, signature="un1", origin="fill", status="unread", empty_probes=2)],
                        read_at="t")
    done = g.load_impact_reads(graph)
    assert done["ok1"]["delta_sol"] == 1.5 and done["un1"]["empty_probes"] == 2
    units = [{"signature": s, "origin": o, "t0": t} for s, o, t in
             (("ok1", "detection", 5), ("un1", "fill", 9), ("new_old", "detection", 1), ("new_new", "detection", 7))]
    pend = g.pending_impacts(units, done)
    assert [u["signature"] for u in pend] == ["new_new", "new_old", "un1"]
    assert pend[2]["empty_probes"] == 2


def _r(uid, delta, status="ok", origin="detection"):
    return {"source_user_id": uid, "origin": origin, "status": status, "delta_sol": delta if status == "ok" else None,
            "buyers_before": 1, "buyers_after": 2, "tx_before": 2, "tx_after": 2,
            "undecoded_before": 0, "undecoded_after": 1}


def _rows(reads):
    return {r["source_user_id"]: r for r in g.wallet_impacts(reads, window_from="w", computed_at="c")}


def test_verdict_bands_by_n():
    reads = [_r("n9", 1.0)] * 9 + [_r("n19", 1.0)] * 19 + [_r("n20", 1.0)] * 20 + [_r("pool", 0.0)] * 30
    rows = _rows(reads)
    assert (rows["n9"]["verdict"], rows["n9"]["verdict_reason"], rows["n9"]["median_delta_sol"]) == ("unknown", "n", None)
    assert (rows["n19"]["verdict"], rows["n19"]["median_delta_sol"], rows["n19"]["p_value"]) == ("untested", 1.0, None)
    assert rows["n20"]["verdict"] in ("mover", "not_distinguished") and rows["n20"]["k"] == 20


def test_censored_share_over_a_quarter_is_unknown():
    reads = ([_r("hi", 1.0)] * 20 + [_r("hi", None, "truncated")] * 7
             + [_r("lo", 1.0)] * 20 + [_r("lo", None, "non_sol")] * 6
             + [_r("lo", None, "launch")] * 9 + [_r("lo", None, "unread")] * 9 + [_r("pool", 0.0)] * 30)
    rows = _rows(reads)
    assert (rows["hi"]["verdict"], rows["hi"]["verdict_reason"], rows["hi"]["censored_share"]) == ("unknown", "censored", 0.2593)
    assert rows["lo"]["censored_share"] == 0.2308 and rows["lo"]["verdict"] in ("mover", "not_distinguished")
    assert rows["lo"]["censored"] == {"non_sol": 6, "launch": 9}


def test_fill_fed_reads_are_reported_apart_and_never_move_the_median():
    reads = [_r("x", 1.0)] * 20 + [_r("y", 0.0)] * 20 + [_r("z", 100.0, origin="fill")] * 20
    rows = _rows(reads)
    assert rows["x"]["k"] == 20                          # M = y's 0.0; z's fills would make it 50
    assert (rows["z"]["n"], rows["z"]["verdict"], rows["z"]["n_fill"], rows["z"]["median_delta_sol_fill"]) == (0, "unknown", 20, 100.0)


def test_the_roster_median_leaves_the_wallet_out():
    rows = _rows([_r("x", 5.0)] * 20 + [_r("y", 2.0)] * 10)
    assert (rows["x"]["k"], rows["x"]["verdict"]) == (20, "mover")    # pooled with itself M = 5.0 and k = 0


def test_bh_and_k_needed_by_hand():
    q = g._bh([0.01, 0.04, 0.03])
    assert [round(x, 6) for x in q] == [0.03, 0.04, 0.04]
    assert (g._k_needed(20, 23), g._k_needed(20, 8), g._k_needed(10, 23)) == (17, 16, 10)


def test_impact_rows_are_rewritten_per_run(tmp_path):
    graph = tmp_path / "graph.db"
    rows = g.wallet_impacts([_r("x", 1.0)] * 3, window_from="w", computed_at="c1")
    assert g.write_impacts(graph, rows) == 1 and g.write_impacts(graph, rows) == 1
    conn = sqlite3.connect(graph)
    assert conn.execute("SELECT COUNT(*), MAX(verdict) FROM memecoin_wallet_impact").fetchone() == (1, "unknown")


def test_a_read_equal_to_the_roster_median_is_not_above_it():
    rows = _rows([_r("x", 0.0)] * 20 + [_r("y", 0.0)] * 20)
    assert (rows["x"]["k"], rows["x"]["verdict"]) == (0, "not_distinguished")


def test_impact_pass_reads_pending_units_then_rewrites_the_wallet_rows(tmp_path, monkeypatch):
    from sniper.persistence import db_memecoin as m
    graph = tmp_path / "graph.db"
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {})
    ledger = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(ledger)
    ledger.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, mint, block_time, "
                   "detected_at, created_at) VALUES ('src', 'uA', 'a', ?, ?, 't', 't')", (MINT, T0))
    g.save_reads(graph, [dict(_slot("src", "uA", MINT, "2026-09-30T00:00:01+00:00"), companions=[]),
                         dict(_slot("fsrc", "uB", MINT, g._iso(T0)), companions=[])], read_at="t")
    ages, windows = [], []

    def fetch_age(u):
        ages.append(u["signature"])
        return T0 - 600, 1

    def fetch_window(u):
        windows.append(u["signature"])
        src = dict(SRC, sig=u["signature"])
        return [src, _tx("b", 110, 1, T0 + 2, "B", sol=2.0)], 1, False
    kw = dict(budget=100, flat_fn=lambda: True, own_addresses=set(), fetch_age=fetch_age, fetch_window=fetch_window,
              now_iso="2026-09-30T12:00:00+00:00")
    out = g.impact_pass(ledger, graph, "2026-09-16T00:00:00+00:00", trade_fn=_trade, sig_fn=_sig, **kw)
    assert (out["reads_new"], out["calls"], out["units"], out["wallets"]) == (2, 4, 2, 2)
    assert ages == ["src", "fsrc"]                                        # detection-fed first
    again = g.impact_pass(ledger, graph, "2026-09-16T00:00:00+00:00", trade_fn=_trade, sig_fn=_sig,
                          **dict(kw, budget=0))
    assert (again["reads_new"], again["stop"], again["wallets"]) == (0, "skipped (budget 0)", 2)
    reads = g.load_impact_reads(graph)
    assert (reads["src"]["origin"], reads["fsrc"]["origin"], reads["src"]["delta_sol"]) == ("detection", "fill", 2.0)


# ---------------------------------------------------------------- Gamma's review of 15f31bb4c
# 16 of her 30 mutants survived the first test set. Each test below names the mutant it kills.

def test_the_verdict_is_decided_by_the_bh_q_not_by_the_raw_p():
    """Three tested wallets. x has 15 of 20 above the others' median: p = 0.0207, under 0.05. Its BH
    q is 0.0207 x 3 / 1 = 0.062, over 0.05. So x is not a mover, and the bar it missed is the bar
    for THREE tested wallets (16), not the bar for one (15)."""
    reads = ([_r("x", 1.0)] * 15 + [_r("x", -1.0)] * 5 + [_r("y", 0.0)] * 20 + [_r("z", 0.0)] * 20)
    rows = _rows(reads)
    assert (rows["x"]["k"], round(rows["x"]["p_value"], 4)) == (15, 0.0207)
    assert (rows["x"]["verdict"], round(rows["x"]["q_value"], 3)) == ("not_distinguished", 0.062)
    assert rows["x"]["k_needed"] == 16 and g._k_needed(20, 1) == 15
    assert (rows["y"]["verdict"], rows["y"]["k_needed"]) == ("not_distinguished", 16)


def test_probe_empty_counts_as_censored_and_is_final():
    reads = [_r("w", 1.0)] * 20 + [_r("w", None, "probe_empty")] * 7 + [_r("pool", 0.0)] * 30
    row = _rows(reads)["w"]
    assert (row["verdict"], row["verdict_reason"], row["censored_share"]) == ("unknown", "censored", 0.2593)
    done = {"p": {"status": "probe_empty", "empty_probes": 3}, "u": {"status": "unread", "empty_probes": 1}}
    units = [{"signature": "p", "origin": "detection", "t0": 2}, {"signature": "u", "origin": "detection", "t0": 1}]
    assert [u["signature"] for u in g.pending_impacts(units, done)] == ["u"]      # probe_empty is never retried


def test_a_fill_median_needs_ten_reads_and_a_read_with_no_transactions_has_no_undecoded_share():
    rows = _rows([_r("nine", 3.0, origin="fill")] * 9 + [_r("ten", 3.0, origin="fill")] * 10)
    assert (rows["nine"]["n_fill"], rows["nine"]["median_delta_sol_fill"]) == (9, None)
    assert (rows["ten"]["n_fill"], rows["ten"]["median_delta_sol_fill"]) == (10, 3.0)
    quiet = dict(_r("q", 0.0), tx_before=0, tx_after=0, undecoded_before=0, undecoded_after=0)
    row = _rows([quiet] * 10)["q"]
    assert (row["n"], row["median_delta_sol"], row["median_undecoded_share"]) == (10, 0.0, None)


def test_an_excluded_traders_usdc_trade_still_censors_the_read():
    r = _read([SRC, _tx("u", 110, 1, T0 + 5, "A2", unit="USDC")], excluded={"A2"})
    assert r["status"] == "non_sol"


def test_a_stop_in_the_window_read_still_counts_the_probe_call():
    reads, calls, why, _ = _run([_unit("src")], stop="HTTP 429 max usage")
    assert (calls, reads[0]["calls"], reads[0]["status"]) == (1, 1, "unread")


def test_units_come_from_solana_slot_reads_only():
    reads = [dict(_slot("e1", "uA", "M1", "2026-09-30T00:00:03+00:00"), network_id="4663")]
    assert g.impact_units(reads, {}) == []


def test_impact_reads_of_another_method_version_are_not_loaded(tmp_path):
    graph = tmp_path / "graph.db"
    g.save_impact_reads(graph, [{"signature": "now", "source_user_id": "uA", "network_id": "solana", "mint": "M",
                                 "origin": "detection", "t0": 1, "status": "ok", "delta_sol": 1.0, "calls": 2}],
                        read_at="t")
    conn = sqlite3.connect(graph)
    conn.execute("INSERT INTO memecoin_wallet_impact_reads (signature, origin, status, method_version, read_at) "
                 "VALUES ('old', 'detection', 'ok', 'impact-v0', 't')")
    conn.commit()
    conn.close()
    assert set(g.load_impact_reads(graph)) == {"now"}


def test_a_first_transaction_after_the_source_buy_is_an_index_fault_not_a_launch():
    """Gamma LOW A: age < 0 means the probe did not return the mint's first transaction. It is the
    same fault as an empty page: unread, then probe_empty after three runs, never `launch`."""
    reads, calls, why, windows = _run([_unit(empties=0)], first=T0 + 5)
    assert (reads[0]["status"], reads[0]["empty_probes"], calls, windows) == ("unread", 1, 1, [])
    reads, *_ = _run([_unit(empties=2)], first=T0 + 5)
    assert (reads[0]["status"], reads[0]["empty_probes"]) == ("probe_empty", 3)
    reads, *_ = _run([_unit()], first=T0)                    # age 0 is a launch
    assert reads[0]["status"] == "launch"


def test_wallet_rows_use_the_current_units_not_every_stored_read(tmp_path, monkeypatch):
    from sniper.persistence import db_memecoin as m
    graph = tmp_path / "graph.db"
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {})
    ledger = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(ledger)
    ledger.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, mint, block_time, "
                   "detected_at, created_at) VALUES ('src', 'uA', 'a', ?, ?, 't', 't')", (MINT, T0))
    g.save_reads(graph, [dict(_slot("src", "uA", MINT, "2026-09-30T00:00:01+00:00"), companions=[])], read_at="t")
    stale = {"source_user_id": "uA", "network_id": "solana", "mint": "Gone", "origin": "detection", "t0": 1,
             "status": "ok", "delta_sol": 9.0, "buyers_before": 0, "buyers_after": 0, "tx_before": 1, "tx_after": 1,
             "undecoded_before": 0, "undecoded_after": 0, "calls": 2}
    g.save_impact_reads(graph, [dict(stale, signature="left_the_window")], read_at="t")
    out = g.impact_pass(ledger, graph, "2026-09-16T00:00:00+00:00", budget=100, flat_fn=lambda: True,
                        own_addresses=set(), fetch_age=lambda u: (T0 - 600, 1),
                        fetch_window=lambda u: ([dict(SRC, sig=u["signature"])], 1, False),
                        now_iso="2026-09-30T12:00:00+00:00", trade_fn=_trade, sig_fn=_sig)
    assert out["units"] == 1
    n = sqlite3.connect(graph).execute("SELECT n FROM memecoin_wallet_impact WHERE source_user_id='uA'").fetchone()[0]
    assert n == 1                                            # the stored read of a unit that is gone does not count


def _run_job(tmp_path, monkeypatch, impact_fn, *, impact_budget=300, funder_budget=50):
    """run() end to end on an empty ledger, with the impact pass and the funder read stubbed."""
    import argparse
    from sniper.persistence import db_memecoin as m
    ledger_path, graph = tmp_path / "ledger.db", tmp_path / "graph.db"
    c = sqlite3.connect(ledger_path)
    m.ensure_memecoin_tables(c)
    c.commit()
    c.close()
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_OWN_ADDRESSES", "OwnSolAddr1,0xABCDEF0000000000000000000000000000000001")
    seen = {}

    def funders(pending, *, budget, flat_fn, fetch):
        seen["funder_budget"] = budget
        return [], 0, "done"
    monkeypatch.setattr(g, "impact_pass", impact_fn)
    monkeypatch.setattr(g, "read_funders", funders)
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {})       # its tables are not the ledger's own
    g.run(argparse.Namespace(ledger=str(ledger_path), graph=str(graph), days=14, helius_budget=0,
                             fill_read_budget=None, funder_budget=funder_budget, impact_budget=impact_budget,
                             upstream_budget=0, upstream_verdicts=False))
    row = sqlite3.connect(graph).execute(
        "SELECT helius_calls, note FROM memecoin_wallet_graph_runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return seen, row


def test_run_passes_our_own_addresses_and_a_capped_budget_and_counts_the_impact_calls(tmp_path, monkeypatch, capsys):
    got = {}

    def impact(ledger, graph_path, since, **kw):
        got.update(kw)
        return {"reads_new": 3, "calls": 1990, "stop": "budget", "units": 3, "wallets": 1}
    seen, (helius_calls, note) = _run_job(tmp_path, monkeypatch, impact, impact_budget=5000)
    assert got["own_addresses"] == {"OwnSolAddr1", "0xabcdef0000000000000000000000000000000001"}
    assert got["budget"] == g.SLOT_BUDGET_MAX                  # 5000 asked, capped at the run's ceiling
    assert seen["funder_budget"] == g.SLOT_BUDGET_MAX - 1990   # the impact calls come off the funder budget
    assert helius_calls == 1990 and "impact_reads_new=3 impact_stop=budget impact_calls=1990" in note


def test_a_failing_impact_pass_does_not_end_the_hourly_run(tmp_path, monkeypatch, capsys):
    """Gamma LOW B: slices 1 and 2 (edges, funder reads, the run row) finish; the note names the error."""
    def impact(ledger, graph_path, since, **kw):
        raise RuntimeError("database is locked")
    seen, (helius_calls, note) = _run_job(tmp_path, monkeypatch, impact)
    assert seen["funder_budget"] == 50 and helius_calls == 0
    assert "impact_reads_new=0 impact_stop=error:RuntimeError impact_calls=0" in note
    assert "impact_stop\": \"error:RuntimeError" in capsys.readouterr().out
