"""Wallet Map coverage (spec docs/superpowers/specs/2026-09-30-memecoin-wallet-map-overhaul-design.md
section 0; pre-registration docs/audits/2026-09-30-wallet-map-reads-preregistration.md A, Gamma
17:40Z). Slot reads were picked from our funded fills and the watcher's detections of ARMED wallets:
597 of 3,352 J7-delivered Solana wallet-coin buys were read, prevaile 1 of 377.

- Unit: one (wallet, coin), read at the wallet's FIRST buy of the coin across all three intakes (J7
  reports, watcher detections, our fills), by chain time where a record has one, else its own stamp.
- No substitution: when that first buy has no transaction (a 'swap:<uuid>' report with no signed
  twin), the unit is 'no_signature' and nothing is read in its place.
- Backfill order: buy time, newest first, all wallets interleaved.
- A signature with no status is a final 'no_slot' row ('slot_unknown' in the notes).
- Off unless the run is started with --coverage; after slices 3 and 3b; skipped after a 429.
- Reads are tagged intake 'j7'; slices 3 and 3b never take one as a unit; slot edges whose sample
  holds one carry their own method string.
"""
import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

from scripts.research import wallet_link_graph as g

MINT, MINT2 = "Mint111", "Mint222"
T0 = datetime(2026, 9, 30, tzinfo=timezone.utc)
T = T0.timestamp()


def _sig(n):
    return "%d%s" % (n, "B" * 87)


def _rec(sig, t, *, uid="U", mint=MINT, usd=200.0, chain=False):
    return {"source_user_id": uid, "mint": mint, "signature": sig, "t": T + t, "usd": usd, "chain": chain}


def _unit(n, t, uid="U", mint=MINT):
    return {"source_user_id": uid, "mint": mint, "signature": _sig(n), "t": T + t}


def _tx(sig, slot, index, trader=None, side="buy"):
    return {"sig": sig, "slot": slot, "transactionIndex": index,
            "trade": {"trader": trader, "side": side, "sol": 1.0} if trader else None}


def _trade(tx, mint):
    return tx["trade"]


def _txsig(tx):
    return tx["sig"]


# ---------------------------------------------------------------------------------------- the intakes
def test_the_relay_intake_is_every_solana_buy_with_its_transaction_or_none(tmp_path):
    path = tmp_path / "ledger.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE memecoin_events (id INTEGER PRIMARY KEY, fomo_event_id TEXT, source_user_id TEXT, "
              "mint TEXT, side TEXT, network_id TEXT, received_at TEXT, their_size_usd REAL)")
    at, old = (T0 + timedelta(hours=1)).isoformat(), (T0 - timedelta(days=20)).isoformat()
    c.executemany("INSERT INTO memecoin_events VALUES (?,?,?,?,?,?,?,?)", [
        (1, "swap:raybot:%s:owner:%s" % (_sig(1), MINT), "U", MINT, "buy", "solana", at, 200.0),
        (2, "pump:%s" % _sig(2), "P", MINT2, "buy", "solana", at, 50.0),
        (3, "swap:9f1c2e2a-uuid", "U", MINT2, "buy", "solana", at, 199.6),
        (4, "swap:raybot:%s:owner:%s" % (_sig(4), MINT), "U", MINT, "sell", "solana", at, 10.0),
        (5, "swap:raybot:0x%s:0xowner:0xc465" % ("ab" * 32), "E", "0xc465", "buy", "4663", at, 1.0),
        (6, "swap:raybot:%s:owner:%s" % (_sig(6), MINT), "U", MINT, "buy", "solana", old, 1.0),
    ])
    c.commit()
    c.close()
    conn = g.ledger_connection(path)
    got = g.j7_solana_buys(conn, T0.isoformat())
    assert [(b["signature"], b["source_user_id"], b["usd"], b["chain"]) for b in got] == [
        (_sig(1), "U", 200.0, False), (_sig(2), "P", 50.0, False), (None, "U", 199.6, False)]
    plan = str(conn.execute("EXPLAIN QUERY PLAN " + g.J7_SOLANA_BUYS_SQL, (1, T0.isoformat())).fetchall())
    assert "SCAN" not in plan, plan                                     # an id range, never a ledger scan


def test_the_three_intakes_are_read_with_the_chain_time_where_a_detection_has_one(monkeypatch):
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(conn)
    conn.executemany("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, mint, "
                     "block_time, detected_at, created_at, source_size_usd) VALUES (?,?,?,?,?,?,?,?,?)", [
                         (_sig(1), "U", "a", "solana", MINT, int(T) + 5, "2026-09-30T00:00:07+00:00", "t", 200.0),
                         (_sig(2), "U", "a", "solana", MINT2, 0, "2026-09-30T00:00:09+00:00", "t", None)])
    from sniper.services import memecoin_wallet_coverage as cov   # the one definition the job imports
    monkeypatch.setattr(cov, "j7_solana_buys", lambda ledger, since: [])
    monkeypatch.setattr(cov, "funded_solana_fills", lambda ledger, since: [
        {"id": 1, "source_user_id": "U", "mint": MINT, "opened_at": "2026-09-30T00:00:08+00:00", "source_sig": _sig(1)},
        {"id": 2, "source_user_id": "U", "mint": MINT2, "opened_at": "2026-09-30T00:00:09+00:00", "source_sig": None}])
    got = {(r["signature"], r["chain"]): r["t"] - T for r in g.coverage_records(conn, T0.isoformat())}
    assert got == {(_sig(1), True): 5.0, (_sig(2), False): 9.0, (_sig(1), False): 8.0}   # block time 0 is none


# ---------------------------------------------------------------------------------------- the unit
def test_the_unit_is_the_first_buy_across_intakes_by_chain_time_where_known():
    recs = [_rec(_sig(2), 3), _rec(_sig(1), 10), _rec(_sig(1), 1, chain=True)]   # sig 1's chain time is 1
    units, nosig = g.coverage_units(recs)
    assert [u["signature"] for u in units] == [_sig(1)] and nosig == []


def test_a_first_buy_with_no_transaction_is_no_signature_and_nothing_stands_in():
    recs = [_rec(None, 0, usd=500.0), _rec(_sig(2), 30)]                  # $500 against $200: another trade
    units, nosig = g.coverage_units(recs)
    assert units == [] and [(u["source_user_id"], u["mint"], u["signature"]) for u in nosig] == [("U", MINT, None)]


def test_a_uuid_report_of_a_signed_trade_is_that_trade():
    """J7 sends one trade twice; the uuid report is often first. Paired within 120 s and 5%."""
    units, nosig = g.coverage_units([_rec(None, 0, usd=199.6), _rec(_sig(1), 18, usd=200.0)])
    assert [u["signature"] for u in units] == [_sig(1)] and nosig == []
    units, nosig = g.coverage_units([_rec(None, 0, usd=199.6), _rec(_sig(1), 121, usd=200.0)])
    assert units == [] and len(nosig) == 1                              # 121 s: another trade
    units, nosig = g.coverage_units([_rec(None, 0, usd=None), _rec(_sig(1), 18, usd=200.0)])
    assert units == [] and len(nosig) == 1                              # an unknown size pairs with nothing


def test_each_signed_trade_takes_one_uuid_report():
    recs = [_rec(None, 0), _rec(None, 5), _rec(_sig(1), 10)]              # two reports, one trade
    units, nosig = g.coverage_units(recs)
    assert units == [] and len(nosig) == 1                              # the unpaired report is the first buy


def test_units_are_per_wallet_and_coin():
    recs = [_rec(_sig(1), 0), _rec(_sig(2), 0, mint=MINT2), _rec(_sig(3), 0, uid="V")]
    units, _ = g.coverage_units(recs)
    assert sorted(u["signature"] for u in units) == sorted([_sig(1), _sig(2), _sig(3)])


# ---------------------------------------------------------------------------------------- pending
def test_pending_is_newest_first_all_wallets_interleaved_and_skips_final_reads(tmp_path):
    graph = tmp_path / "graph.db"
    g.save_reads(graph, [
        {"signature": _sig(1), "status": "ok", "detected_at": T0.isoformat(), "companions": []},
        {"signature": _sig(2), "status": "unread", "detected_at": T0.isoformat(), "companions": []},
    ], read_at="t")
    units = [_unit(1, 50), _unit(2, 10, uid="V"), _unit(3, 30, uid="W"), _unit(4, 20)]
    assert [u["signature"] for u in g.coverage_pending(graph, units)] == [_sig(3), _sig(4), _sig(2)]


def test_a_pair_read_at_its_first_buy_stays_done_when_that_buy_leaves_the_window(tmp_path):
    """Gamma MEDIUM: the 14-day window slides past a read first buy; the next buy is then the window's
    first buy and must not stand in. Whatever intake read the earlier buy."""
    graph = tmp_path / "graph.db"
    for n, intake in ((1, "j7"), (2, "detection")):
        g.save_reads(graph, [{"signature": _sig(n), "source_user_id": "U%d" % n, "mint": MINT, "status": "ok",
                              "detected_at": (T0 - timedelta(days=15)).isoformat(), "companions": [],
                              "intake": intake}], read_at="t")
    units = [_unit(11, 0, uid="U1"), _unit(12, 0, uid="U2")]           # their second buys, inside the window
    assert g.coverage_pending(graph, units) == []


def test_a_pair_whose_only_read_is_a_later_buy_still_reads_its_first_buy(tmp_path):
    graph = tmp_path / "graph.db"
    g.save_reads(graph, [{"signature": "later", "source_user_id": "U", "mint": MINT, "status": "ok",
                          "detected_at": (T0 + timedelta(minutes=5)).isoformat(), "companions": [],
                          "intake": "detection"}], read_at="t")
    assert [u["signature"] for u in g.coverage_pending(graph, [_unit(1, 0)])] == [_sig(1)]


def test_a_uuid_only_first_buy_holds_its_pair_after_it_leaves_the_window(tmp_path):
    """Gamma MEDIUM residual (09e0bd3ff): a no_signature unit wrote no row, so when its uuid first buy
    left the window the next signed buy was read in its place, about 5 a day."""
    graph = tmp_path / "graph.db"
    nosig = [{"source_user_id": "U", "mint": MINT, "signature": None, "t": T - 15 * 86400}]
    out = g.coverage_pass([], graph, budget=0, flat_fn=lambda: False, ours=set(), sig_slots=None, fetch_slot=None,
                          no_signature=nosig)
    assert out["pending"] == 0
    (row,) = [r for r in g.load_reads(graph, "")]
    assert (row["signature"], row["status"], row["intake"]) == ("nosig:U:%s" % MINT, "no_signature", "j7")
    assert g.coverage_pending(graph, [_unit(1, 0)]) == []              # the later signed buy stands in for nothing
    later = [dict(nosig[0], t=T - 86400)]                               # a later uuid unit keeps the first time
    g.save_no_signature(graph, later)
    assert [r["detected_at"][:10] for r in g.load_reads(graph, "")] == ["2026-09-15"]
    assert [e for e in g.slot_edges(g.load_reads(graph, ""), owners={})] == []


def test_at_an_equal_time_a_signed_trade_beats_an_unpaired_uuid_report():
    units, nosig = g.coverage_units([_rec(None, 0, usd=500.0), _rec(_sig(1), 0)])
    assert [u["signature"] for u in units] == [_sig(1)] and nosig == []


def test_a_transaction_reported_twice_takes_its_earliest_stamp():
    recs = [_rec(_sig(1), 10), _rec(_sig(1), 2), _rec(_sig(2), 5)]      # a fill at 10, the relay at 2
    units, _ = g.coverage_units(recs)
    assert [u["signature"] for u in units] == [_sig(1)] and units[0]["t"] == T + 2


# ---------------------------------------------------------------------------------------- the pass
def _pass(tmp_path, units, *, budget=100, slots=None, flat=lambda: True, fetch=None, sig_slots=None):
    graph = tmp_path / "graph.db"
    seen = {"status_calls": [], "fetched": []}

    def default_sig_slots(sigs):
        seen["status_calls"].append(list(sigs))
        return {s: 7 for s in sigs if s in (slots if slots is not None else set(sigs))}, (len(sigs) + 255) // 256

    def default_fetch(det):
        seen["fetched"].append(det)
        return [_tx(det["signature"], det["slot"], 3, "SRC"), _tx("bot", det["slot"], 9, "BOT")], 1, False
    out = g.coverage_pass(units, graph, budget=budget, flat_fn=flat, ours=set(),
                          sig_slots=sig_slots or default_sig_slots, fetch_slot=fetch or default_fetch,
                          trade_fn=_trade, sig_fn=_txsig)
    return out, seen, {r["signature"]: r for r in g.load_reads(graph)}


def test_a_unit_is_read_by_its_slot_and_tagged_j7(tmp_path):
    out, seen, reads = _pass(tmp_path, [_unit(1, 120)])
    assert seen["fetched"][0]["slot"] == 7 and seen["fetched"][0]["signature"] == _sig(1)
    r = reads[_sig(1)]
    assert (r["status"], r["intake"], r["index"]) == ("ok", "j7", 3)
    assert [c["trader"] for c in r["companions"]] == ["BOT"]
    assert r["detected_at"] == "2026-09-30T00:02:00+00:00"              # the unit's buy time
    assert (out["reads_new"], out["calls"], out["stop"], out["pending"]) == (1, 2, "done", 1)


def test_a_signature_with_no_status_is_a_final_no_slot_row(tmp_path):
    out, seen, reads = _pass(tmp_path, [_unit(1, 0), _unit(2, 0, mint=MINT2)], slots={_sig(1)})
    assert reads[_sig(2)]["status"] == "no_slot" and reads[_sig(2)]["intake"] == "j7"
    assert out["slot_unknown"] == 1 and len(seen["fetched"]) == 1
    assert g.coverage_pending(tmp_path / "graph.db", [_unit(2, 0, mint=MINT2)]) == []


def test_status_lookups_are_asked_only_for_the_units_the_budget_can_read(tmp_path):
    """Worst case: one status call per 256 signatures plus SLOT_PAGES calls per read, a hard cap."""
    units = [_unit(i, i, mint="m%d" % i) for i in range(1, 11)]
    out, seen, _reads = _pass(tmp_path, units, budget=1 + 3 * g.SLOT_PAGES)
    assert [len(s) for s in seen["status_calls"]] == [3] and out["reads_new"] == 3
    assert out["stop"] == "budget" and out["calls"] <= 1 + 3 * g.SLOT_PAGES
    out, seen, _ = _pass(tmp_path, units, budget=g.SLOT_PAGES)          # no room for a status call AND a read
    assert seen["status_calls"] == [] and (out["reads_new"], out["stop"]) == (0, "budget")


def test_the_pass_waits_for_a_flat_book_and_stops_on_a_failed_lookup(tmp_path):
    out, seen, reads = _pass(tmp_path, [_unit(1, 0)], flat=lambda: False)
    assert (out["stop"], seen["status_calls"], reads) == ("solana book open", [], {})

    def failing(sigs):
        raise g.ReadStop("HTTP 429 on getSignatureStatuses: max usage")
    out, _seen, reads = _pass(tmp_path, [_unit(1, 0)], sig_slots=failing)
    assert out["stop"].startswith("stopped: HTTP 429") and reads == {}   # nothing final: retried next run


# ------------------------------------------------------------------------------ slices 3, 3b, edges
def test_slices_3_and_3b_never_take_a_j7_read_as_a_unit():
    reads = [
        {"signature": "legacy", "source_user_id": "U", "network_id": "solana", "mint": MINT, "status": "ok",
         "detected_at": "2026-09-30T00:00:10+00:00", "intake": None},
        {"signature": "cov", "source_user_id": "V", "network_id": "solana", "mint": MINT, "status": "ok",
         "detected_at": "2026-09-30T00:00:01+00:00", "intake": "j7"},       # earlier, and must not win 3b
    ]
    assert [u["signature"] for u in g.impact_units(reads, {})] == ["legacy"]


def test_an_older_reads_table_gains_the_intake_column(tmp_path):
    graph = tmp_path / "graph.db"
    c = sqlite3.connect(graph)
    c.executescript(g.SLOT_SCHEMA)                                      # the table as releases made it
    c.execute("INSERT INTO memecoin_wallet_slot_reads (signature, status, detected_at, read_at) "
              "VALUES ('s', 'ok', '2026-09-30T00:00:00+00:00', 't')")
    c.commit()
    assert "intake" not in {r[1] for r in c.execute("PRAGMA table_info(memecoin_wallet_slot_reads)")}
    c.close()
    g.save_reads(graph, [{"signature": "n", "status": "ok", "detected_at": "2026-09-30T00:00:00+00:00",
                          "companions": [], "intake": "detection"}], read_at="t")
    assert {r["signature"]: r["intake"] for r in g.load_reads(graph)} == {"s": None, "n": "detection"}


def _edge_reads(last_intake):
    reads = []
    for i in range(30):
        reads.append({"source_user_id": "U%d" % (i % 10), "network_id": "solana", "mint": "p%d" % i, "status": "ok",
                      "slot": 1000 + i, "index": 50, "companions": [], "detected_at": "2026-09-29T00:00:00+00:00",
                      "intake": "detection"})
    for i in range(3):
        reads.append({"source_user_id": "A", "network_id": "solana", "mint": "a%d" % i, "status": "ok",
                      "slot": 2000 + i, "index": 40, "companions": [{"trader": "B", "index": 45}],
                      "detected_at": "2026-09-29T00:00:00+00:00", "intake": last_intake})
    return reads


def test_slot_edges_from_a_sample_with_coverage_reads_carry_their_own_method(tmp_path):
    (co,) = [e for e in g.slot_edges(_edge_reads("j7"), owners={}) if e["edge_type"] == "co_buy_slot"]
    assert co["coverage"]["slot_intakes"] == {"detection": 30, "j7": 3}
    assert co["method_version"] == g.SLOT_COVERAGE_METHOD != g.METHOD_VERSION
    (old,) = [e for e in g.slot_edges(_edge_reads("fill"), owners={}) if e["edge_type"] == "co_buy_slot"]
    assert old["method_version"] == g.METHOD_VERSION
    graph = tmp_path / "graph.db"
    g.write_edges(graph, [co], computed_at="t1")
    g.write_edges(graph, [old], computed_at="t2")                       # a rerun replaces both strings
    rows = sqlite3.connect(graph).execute("SELECT method_version FROM memecoin_wallet_edges").fetchall()
    assert rows == [(g.METHOD_VERSION,)]
    assert "relay-reported" in g.statement_for(co, {}) and g.SLOT_COVERAGE_METHOD in g.statement_for(co, {})


# ---------------------------------------------------------------------------------------- the run
def _run(tmp_path, monkeypatch, *, coverage, rate_429=(0, 0), upstream_calls=0, fill_calls=0, det_calls=0,
         impact_calls=0, helius_budget=300, trail=None, axiom=None):
    from sniper.persistence import db_memecoin as m
    ledger_path, graph = tmp_path / "ledger.db", tmp_path / "graph.db"
    c = sqlite3.connect(ledger_path)
    m.ensure_memecoin_tables(c)
    c.commit()
    c.close()
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    order, seen = [], {}
    counts = iter(rate_429)
    monkeypatch.setattr(g, "_rate_429", lambda: next(counts, rate_429[-1]))
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {})
    monkeypatch.setattr(g, "impact_pass", lambda *a, **k: order.append("impact") or
                        {"reads_new": 0, "calls": impact_calls, "stop": "done", "units": 0, "wallets": 0})
    monkeypatch.setattr(g, "funded_solana_fills", lambda ledger, since: [
        {"id": 1, "source_user_id": "U", "mint": MINT, "opened_at": T0.isoformat(), "source_sig": None}])
    monkeypatch.setattr(g, "read_fills", lambda fills, **kw: ([], [], fill_calls, "done"))
    monkeypatch.setattr(g, "solana_detections", lambda ledger, since: [{"signature": "d1"}])
    monkeypatch.setattr(g, "read_slots", lambda dets, **kw: ([], det_calls, "done"))

    def upstream(*a, **k):
        order.append("upstream")
        return {"reads_new": 0, "calls": upstream_calls, "stop": "done", "status": {}, "control": "0/0",
                "control_fails": {}, "thin_losers": 0, "fx_rejected": 0, "pruned": 0, "wallets": None,
                "funder_wanted": []}
    monkeypatch.setattr(g, "upstream_pass", upstream)
    monkeypatch.setattr(g, "coverage_records", lambda ledger, since: [
        _rec(_sig(1), 0), _rec(None, 0, mint=MINT2), _rec(None, 0, uid="V")])

    def cov(units, graph_path, **kw):
        order.append("coverage")
        seen.update(kw, units=units)
        assert [(u["source_user_id"], u["mint"]) for u in kw["no_signature"]] == [("U", MINT2), ("V", MINT)]
        return {"reads_new": 1, "calls": 2, "stop": "done", "pending": 1, "slot_unknown": 0}
    monkeypatch.setattr(g, "coverage_pass", cov)
    monkeypatch.setattr(g, "read_funders", lambda pending, **kw: order.append("funders") or ([], 0, "done"))

    def trail_pass(graph_path, frozen, **kw):
        order.append("trail")
        seen["trail"] = dict(kw, frozen=frozen)
        return {"probe_days": 2, "calls": 30, "stop": "done", "leads": 5, "pending": 2}
    monkeypatch.setattr(g, "trail_pass", trail_pass)

    def axiom_pass(graph_path, seed, **kw):
        order.append("axiom")
        seen["axiom"] = dict(kw, seed=seed)
        return {"wallets": 3, "calls": 60, "stop": "budget", "reads": 55, "pending": 132}
    monkeypatch.setattr(g, "axiom_pass", axiom_pass)
    g.save_reads(graph, [{"signature": "h1", "source_user_id": "H1", "network_id": "solana", "mint": MINT,
                          "status": "ok", "detected_at": T0.isoformat(), "companions": []},
                         {"signature": "x1", "source_user_id": "X1", "network_id": "solana", "mint": MINT2,
                          "status": "ok", "detected_at": T0.isoformat(), "companions": []}], read_at="t")
    g.run(argparse.Namespace(ledger=str(ledger_path), graph=str(graph), days=3650, helius_budget=helius_budget,  # fixed T0 must stay inside the window
                             fill_read_budget=None, funder_budget=50, impact_budget=300, upstream_budget=150,
                             upstream_verdicts=False, coverage=coverage, trail=trail, axiom=axiom))
    note = sqlite3.connect(graph).execute(
        "SELECT note FROM memecoin_wallet_graph_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    return order, seen, note


def test_coverage_runs_only_when_asked_after_slices_3_and_3b(tmp_path, monkeypatch):
    order, seen, note = _run(tmp_path, monkeypatch, coverage=False)
    assert "coverage" not in order and "coverage_stop=off" in note
    order, seen, note = _run(tmp_path, monkeypatch, coverage=True, upstream_calls=40)
    assert order.index("coverage") > order.index("upstream") > order.index("impact")
    assert [u["signature"] for u in seen["units"]] == [_sig(1)]         # only signed first buys are read
    assert ("coverage_reads_new=1 coverage_stop=done coverage_calls=2 coverage_units=3 coverage_pending=1 "
            "coverage_no_signature=2 coverage_slot_unknown=0 ") in note and "rate_429=0" in note


def test_coverage_spends_what_the_slot_pass_left_and_never_passes_the_run_cap(tmp_path, monkeypatch):
    _order, seen, _note = _run(tmp_path, monkeypatch, coverage=True, fill_calls=20, det_calls=30, upstream_calls=40)
    assert seen["budget"] == 300 - 20 - 30                        # 3 and 3b have their own budgets
    _order, seen, _note = _run(tmp_path, monkeypatch, coverage=True, helius_budget=2000, fill_calls=20,
                               det_calls=30, impact_calls=1500, upstream_calls=100)
    assert seen["budget"] == g.SLOT_BUDGET_MAX - 20 - 30 - 1500 - 100


def test_a_429_earlier_in_the_run_stops_the_coverage_pass(tmp_path, monkeypatch):
    order, _seen, note = _run(tmp_path, monkeypatch, coverage=True, rate_429=(0, 2, 2))
    assert "coverage" not in order
    assert "coverage_stop=skipped (429 earlier in this run)" in note and "rate_429=2" in note


def test_the_trail_runs_after_coverage_only_with_a_frozen_list(tmp_path, monkeypatch):
    order, seen, note = _run(tmp_path, monkeypatch, coverage=True)
    assert "trail" not in order and "trail_stop=off" in note
    frozen = tmp_path / "frozen.json"
    frozen.write_text('{"frozen_at": "2026-10-02T00:00:00Z", "herders": ["H1"], "probes": ["P"]}')
    order, seen, note = _run(tmp_path, monkeypatch, coverage=True, trail=str(frozen))
    assert order.index("trail") > order.index("coverage") and order.index("funders") > order.index("trail")
    assert seen["trail"]["budget"] == g.TRAIL_BUDGET and seen["trail"]["frozen"]["probes"] == ["P"]
    assert seen["trail"]["herder_buys"] == {MINT: [T]}             # the frozen herders' read buys
    assert "trail_probe_days=2 trail_stop=done trail_calls=30 trail_leads=5 trail_pending=2" in note
    order, _seen, note = _run(tmp_path, monkeypatch, coverage=True, trail=str(frozen), rate_429=(0, 1, 1))
    assert "trail" not in order and "trail_stop=skipped (429 earlier in this run)" in note


def test_the_axiom_read_runs_after_the_trail_only_when_asked(tmp_path, monkeypatch):
    order, _seen, note = _run(tmp_path, monkeypatch, coverage=False)
    assert "axiom" not in order and "axiom_stop=off" in note
    seed = tmp_path / "seed.json"
    seed.write_text('{"wallets": [{"address": "A"}]}')
    frozen = tmp_path / "frozen.json"
    frozen.write_text('{"frozen_at": "z", "herders": [], "probes": ["P"]}')
    order, seen, note = _run(tmp_path, monkeypatch, coverage=True, trail=str(frozen), axiom=str(seed))
    assert order.index("axiom") > order.index("trail") and order.index("funders") > order.index("axiom")
    assert seen["axiom"]["budget"] == g.AXIOM_BUDGET and seen["axiom"]["seed"]["wallets"] == [{"address": "A"}]
    assert "axiom_wallets=3 axiom_stop=budget axiom_calls=60 axiom_reads=55 axiom_pending=132" in note
    order, _seen, note = _run(tmp_path, monkeypatch, coverage=False, axiom=str(seed), rate_429=(0, 1, 1))
    assert "axiom" not in order and "axiom_stop=skipped (429 earlier in this run)" in note


def test_the_cli_leaves_coverage_off(monkeypatch):
    got = {}
    monkeypatch.setattr(g, "run", lambda args: got.update(vars(args)) or 0)
    monkeypatch.setattr("sys.argv", ["wallet_link_graph.py", "run"])
    assert g.main() == 0 and got["coverage"] is False and got["trail"] is None and got["axiom"] is None
    monkeypatch.setattr("sys.argv", ["wallet_link_graph.py", "run", "--coverage"])
    g.main()
    assert got["coverage"] is True
