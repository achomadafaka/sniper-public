"""Wallet link graph v2 (spec docs/superpowers/specs/2026-09-29-memecoin-wallet-link-graph-design.md,
Alpha GO 2026-09-29): follows and dumps_on from J7 events, stored in data/memecoin_wallet_graph.db.

Thresholds travel in method_version: W = 600 s, >= 3 shared coins, >= 80% same order, beat the
within-day permutation null (v2, Alpha and Gamma 2026-09-29: exact p = (1 + shuffles >= observed) /
2,001 <= 0.01 on each of 5 seeds; an edge that passes on fewer than 5 seeds is kept and flagged
'boundary'); dumps_on 120 s, >= 3 coins, seller held before the buy, and the hits must beat a
binomial null: the seller's first sell within +-600 s of the buy lands in the 120 s after it with
q = 0.1 (Gamma MED: v1 dumps_on scored the same in the adjacent windows).
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scripts.research import wallet_link_graph as g

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _ev(uid, mint, side, seconds, net="solana"):
    return {"network_id": net, "mint": mint, "source_user_id": uid, "side": side,
            "at": (T0 + timedelta(seconds=seconds)).isoformat()}


def _leader_follower(n, lag=30, *, leader="A", follower="B", spacing=3600):
    out = []
    for i in range(n):
        out.append(_ev(leader, "m%d" % i, "buy", i * spacing))
        out.append(_ev(follower, "m%d" % i, "buy", i * spacing + lag))
    return out


def test_a_repeat_leader_follower_pair_is_a_follows_edge():
    stats = {}
    edges = g.follows_edges(_leader_follower(5) + _background("B", 8), stats=stats)
    assert stats == {"pairs_tested": 1}
    (edge,) = [e for e in edges if e["edge_type"] == "follows"]
    assert (edge["src"], edge["dst"]) == ("A", "B")          # leader -> follower
    assert edge["n_coins"] == 5 and edge["n_consistent"] == 5
    assert edge["lag_p50_s"] == 30.0
    assert edge["null_p99"] < 5 and edge["flag"] is None
    null = edge["evidence"]["null"]
    assert null["shuffles"] == 2000 and null["seeds_passed"] == 5
    assert [s["seed"] for s in null["per_seed"]] == [0, 1, 2, 3, 4]
    assert all(s["p99"] < 5 and 0 <= s["exceed"] < 2000 for s in null["per_seed"])
    assert all(s["p"] == round((1 + s["exceed"]) / 2001, 6) <= 0.01 for s in null["per_seed"])
    assert edge["null_p"] == max(s["p"] for s in null["per_seed"])
    assert [c["mint"] for c in edge["evidence"]["coins"]] == ["m0", "m1", "m2", "m3", "m4"]


def test_the_seed_verdict_keeps_a_partial_pass_as_boundary():
    """Alpha 2026-09-29: beat the p99 on 5 of 5 seeds = clean; on 1-4 = kept, flagged 'boundary';
    on none = no edge."""
    seeds = lambda *ps: [{"seed": i, "p": p} for i, p in enumerate(ps)]
    assert g.null_verdict(seeds(0.001, 0.001, 0.01, 0.001, 0.001)) == (5, None)   # 0.01 passes
    assert g.null_verdict(seeds(0.001, 0.0105, 0.001, 0.001, 0.001)) == (4, "boundary")
    assert g.null_verdict(seeds(0.02, 0.02, 0.02, 0.02, 0.005)) == (1, "boundary")
    assert g.null_verdict(seeds(0.02, 0.02, 0.02, 0.02, 0.02)) == (0, None)


def test_a_pair_null_does_not_depend_on_other_pairs():
    """Each pair draws its own seeded shuffles, so adding unrelated wallets (sorted first) cannot
    move another pair's null or flag."""
    base = _leader_follower(4) + _background("B", 8)
    other = [_ev("0x%d" % (i % 2), "o%d" % (i // 2), "buy", 7 * 3600 + i * 20) for i in range(12)]
    alone = g.follows_edges(base)
    crowded = [e for e in g.follows_edges(base + other) if e["src"] == "A"]
    assert alone and [e["evidence"]["null"] for e in alone] == [e["evidence"]["null"] for e in crowded]


def test_two_shared_coins_are_not_an_edge():
    """With background buys the null is low, so only the >= 3 coin floor stops two coins."""
    edges = g.follows_edges(_leader_follower(2) + _background("B", 20))
    assert edges == []
    assert len(g.follows_edges(_leader_follower(3) + _background("B", 20))) == 1


def test_an_inconsistent_order_is_not_an_edge():
    """A first on 3 coins, B first on 2: 60% < 80%, no edge either way."""
    events = _leader_follower(4) + _background("B", 8)
    for i in range(2):
        events.append(_ev("B", "x%d" % i, "buy", (5 + i) * 3600 + 300))
        events.append(_ev("A", "x%d" % i, "buy", (5 + i) * 3600 + 330))
    assert g.follows_edges(events) == []                         # 4 of 6 = 67%
    events += [_ev("A", "y", "buy", 3 * 3600 + 900), _ev("B", "y", "buy", 3 * 3600 + 930)]
    assert len(g.follows_edges(events)) == 0                     # 5 of 7 = 71%
    events += [_ev("A", "z%d" % i, "buy", (8 + i) * 3600) for i in range(3)]
    events += [_ev("B", "z%d" % i, "buy", (8 + i) * 3600 + 30) for i in range(3)]
    (edge,) = g.follows_edges(events)                            # 8 of 10 = 80%
    assert (edge["src"], edge["n_coins"], edge["n_consistent"]) == ("A", 10, 8)


def test_a_buy_outside_the_window_does_not_count():
    assert g.follows_edges(_leader_follower(5, lag=601)) == []


def test_three_shared_coins_alone_cannot_beat_the_null():
    """Pinned: with no other buys that day the shuffle equals the real pairing 1 time in 6, so the
    p99 is 3 and three shared coins are not an edge. Real wallets carry background buys (above)."""
    assert g.follows_edges(_leader_follower(3)) == []


def test_a_burst_inside_one_window_cannot_manufacture_a_follower():
    """Ten coins, all bought by both wallets inside one 10-minute window: any pairing of B's times to
    A's coins lands inside W, so the within-day shuffle shares all ten too and the real pairing does
    not beat the null. Co-activity is not following."""
    events = []
    for i in range(10):
        events.append(_ev("A", "p%d" % i, "buy", i * 5))
        events.append(_ev("B", "p%d" % i, "buy", i * 5 + 30))
    assert g.follows_edges(events) == []


def _background(wallet, n, start=5 * 3600 + 1800):
    """Other coins the wallet bought the same day: what gives the within-day shuffle its power. With
    only the shared coins on a day, every shuffle can equal the real pairing (a real wallet buys far
    more coins a day than it shares with any one other wallet)."""
    return [_ev(wallet, "bg-%s-%d" % (wallet, i), "buy", start + i * 900) for i in range(n)]


def test_only_first_buys_count_and_networks_never_mix():
    events = _leader_follower(3) + _background("B", 8)
    events += [_ev("A", "m0", "buy", 99999)]                                   # a re-buy
    events += [_ev("A", "s%d" % i, "buy", (4 + i) * 3600) for i in range(3)]
    events += [_ev("B", "s%d" % i, "sell", (4 + i) * 3600 + 30) for i in range(3)]  # sells are not buys
    events += [_ev("B", "m%d" % i, "buy", i * 3600 + 30, net="56") for i in range(3)]
    edges = g.follows_edges(events)
    assert len(edges) == 1 and edges[0]["network_id"] == "solana" and edges[0]["n_coins"] == 3


def test_dumps_on_needs_a_prior_holding_and_the_window():
    events = []
    for i in range(3):
        events.append(_ev("S", "d%d" % i, "buy", i * 3600))              # S held before A's buy
        events.append(_ev("A", "d%d" % i, "buy", i * 3600 + 600))
        events.append(_ev("S", "d%d" % i, "sell", i * 3600 + 660))       # 60 s after A's buy
    (edge,) = g.dumps_on_edges(events)
    assert (edge["src"], edge["dst"], edge["n_coins"]) == ("S", "A", 3)
    assert g.dumps_on_edges([e for e in events if e["mint"] != "d2"]) == []   # two coins
    no_hold = [e for e in events if not (e["source_user_id"] == "S" and e["side"] == "buy")]
    assert g.dumps_on_edges(no_hold) == []
    late = [dict(e, at=(datetime.fromisoformat(e["at"]) + timedelta(seconds=100)).isoformat())
            if e["side"] == "sell" else e for e in events]
    assert g.dumps_on_edges(late) == []                                  # 160 s > 120 s
    before = [dict(e, at=(datetime.fromisoformat(e["at"]) - timedelta(seconds=120)).isoformat())
              if e["side"] == "sell" else e for e in events]
    assert g.dumps_on_edges(before) == []                                # sold before A bought


def test_the_graph_db_is_separate_and_a_rerun_replaces_the_method_rows(tmp_path):
    path = tmp_path / "memecoin_wallet_graph.db"
    edges = g.follows_edges(_leader_follower(5) + _background("B", 8))
    assert g.write_edges(path, edges, computed_at="2026-09-29T22:00:00Z") == 1
    assert g.write_edges(path, edges, computed_at="2026-09-29T23:00:00Z") == 1
    rows = sqlite3.connect(path).execute(
        "SELECT edge_type, src, dst, method_version, computed_at FROM memecoin_wallet_edges").fetchall()
    assert rows == [("follows", "A", "B", g.METHOD_VERSION, "2026-09-29T23:00:00Z")]


def test_the_ledger_is_opened_read_only(tmp_path):
    ledger = tmp_path / "ledger.db"
    c = sqlite3.connect(ledger)
    c.execute("CREATE TABLE memecoin_events (source_user_id TEXT, mint TEXT, side TEXT, "
              "network_id TEXT, received_at TEXT, delivery_lane TEXT)")
    c.commit()
    c.close()
    conn = g.ledger_connection(ledger)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO memecoin_events VALUES ('a','b','buy','solana','t','j7_wallets')")


def test_j7_events_reads_the_window_by_an_id_range_not_a_scan(tmp_path):
    """memecoin_events is ~65M rows with no received_at index (2026-09-29): the read must bound by id."""
    ledger = tmp_path / "ledger.db"
    c = sqlite3.connect(ledger)
    c.execute("CREATE TABLE memecoin_events (id INTEGER PRIMARY KEY, source_user_id TEXT, mint TEXT, "
              "side TEXT, network_id TEXT, received_at TEXT, delivery_lane TEXT)")
    c.executemany("INSERT INTO memecoin_events VALUES (?,?,?,?,?,?,?)",
                  [(i, "u%d" % (i % 7), "m%d" % i, "buy" if i % 2 else "sell", "solana",
                    (T0 + timedelta(minutes=10 * i)).isoformat(), "j7_wallets" if i % 3 else "global_feed")
                   for i in range(1, 2001)])
    c.execute("INSERT INTO memecoin_events VALUES (2001, 'late', 'mlate', 'buy', 'solana', ?, 'j7_wallets')",
              ((T0 + timedelta(minutes=10 * 1600)).isoformat(),))          # a late insert, still in window
    c.commit()
    c.close()
    conn = g.ledger_connection(ledger)
    since = (T0 + timedelta(minutes=10 * 1500)).isoformat()
    got = sorted(e["mint"] for e in g.j7_events(conn, since))
    assert got == sorted(["m%d" % i for i in range(1500, 2001) if i % 3] + ["mlate"])
    lo = g.first_id_since(conn, since)
    assert 1500 - 24 * 6 - 1 <= lo <= 1500 - 24 * 6 + 1                    # one day of margin
    plan = str(conn.execute("EXPLAIN QUERY PLAN " + g.J7_EVENTS_SQL, (lo, since)).fetchall())
    assert "SCAN" not in plan, plan


def test_hubs_rank_wallets_by_edge_count_with_their_handle():
    edges = [{"src": "H", "dst": "x"}, {"src": "y", "dst": "H"}, {"src": "H", "dst": "z"}, {"src": "y", "dst": "z"}]
    assert g.hubs(edges, {"H": "hubby"}, top=2) == [
        {"source_user_id": "H", "handle": "hubby", "edges": 3},
        {"source_user_id": "y", "handle": None, "edges": 2}]


def test_exceed_counts_shuffles_that_reach_the_real_count():
    """One shared coin, one coin each: every shuffle is the identity, so all 2,000 reach the real count."""
    import random
    assert g._null({"m0": 0.0}, {"m0": 10.0}, random.Random(0), 1) == (1, 2000)


def test_a_co_active_seller_does_not_dump_on_the_buyer():
    """Gamma MED 2026-09-29: sells spread evenly around the buyer's buys (co-activity) put 4 of 20 in
    the 120 s after the buy, which v1 called an edge. The binomial null (q = 0.1) gives p = 0.13."""
    events = []
    for i in range(20):
        events.append(_ev("S", "c%d" % i, "buy", i * 3600 - 1000))
        events.append(_ev("A", "c%d" % i, "buy", i * 3600))
        events.append(_ev("S", "c%d" % i, "sell", i * 3600 - 285 + 30 * i))
    assert g.dumps_on_edges(events) == []


def test_a_dumps_on_edge_carries_its_binomial_null():
    events = []
    for i in range(3):
        events.append(_ev("S", "d%d" % i, "buy", i * 3600))
        events.append(_ev("A", "d%d" % i, "buy", i * 3600 + 600))
        events.append(_ev("S", "d%d" % i, "sell", i * 3600 + 660))
    (edge,) = g.dumps_on_edges(events)
    assert edge["evidence"]["null"] == {"test": "binomial", "near": 3, "hits": 3, "q": 0.1, "p": 0.001,
                                        "bins_120s": [0, 0, 0, 0, 0, 3, 0, 0, 0, 0]}
    assert edge["null_p"] == 0.001
    events += [_ev("S", "e%d" % i, "sell", 40000 + i) for i in range(5)]           # far from any buy
    for i in range(2):                                                             # near, not after
        events += [_ev("S", "f%d" % i, "buy", 50000 + i * 3600), _ev("A", "f%d" % i, "buy", 50600 + i * 3600),
                   _ev("S", "f%d" % i, "sell", 50600 + i * 3600 - 300)]
    (edge,) = g.dumps_on_edges(events)
    assert (edge["evidence"]["null"]["near"], edge["evidence"]["null"]["hits"]) == (5, 3)
    assert edge["evidence"]["null"]["bins_120s"] == [0, 0, 2, 0, 0, 3, 0, 0, 0, 0]     # -300 s: [-360, -240)
    assert edge["evidence"]["null"]["p"] == round(g.binomial_tail(3, 5, 0.1), 6)      # 0.00856
    events += [_ev("S", "h", "buy", 90000), _ev("A", "h", "buy", 90600), _ev("S", "h", "sell", 90500)]
    assert g.dumps_on_edges(events) == []                               # 3 of 6: p = 0.016 > 0.01


def test_binomial_tail_is_exact():
    assert g.binomial_tail(0, 5, 0.1) == 1.0
    assert abs(g.binomial_tail(3, 3, 0.1) - 0.001) < 1e-12
    assert abs(g.binomial_tail(4, 20, 0.1) - 0.13295332) < 1e-8      # 1 - P(X <= 3)


def test_empty_j7_days_inside_the_window_are_named():
    """Gamma LOW 2: a UTC day with no J7 events inside the window goes on every edge's coverage.
    Only whole days count; the partial first and last days are not called empty."""
    events = [_ev("A", "m", "buy", h * 3600) for h in (0, 24, 72)]                   # 09-20, 09-21, 09-23
    assert g.empty_days(events, "2026-09-19T23:00:00+00:00", "2026-09-25T01:00:00+00:00") == [
        "2026-09-22", "2026-09-24"]


def _dumped(prefix, n, *extra_sells):
    """n coins S held, A bought at +600, S sells at each offset (seconds after A's buy)."""
    out = []
    for i in range(n):
        base = 200000 + i * 7200
        out += [_ev("S", "%s%d" % (prefix, i), "buy", base - 6000), _ev("A", "%s%d" % (prefix, i), "buy", base)]
        out += [_ev("S", "%s%d" % (prefix, i), "sell", base + off) for off in extra_sells]
    return out


def test_only_the_first_sell_near_the_buy_is_judged():
    """An earlier far sell (outside +-600 s) is ignored; a sell before the buy inside +-600 s is the
    one judged, so a later sell in the 120 s after it is not a hit."""
    assert len(g.dumps_on_edges(_dumped("far", 3, -5000, 60))) == 1          # far sell ignored
    assert g.dumps_on_edges(_dumped("hit", 3, 60) + _dumped("pre", 3, -300, 60)) == []   # 3 of 6


def test_the_null_p_column_is_the_worst_seed():
    edge, = g.follows_edges(_leader_follower(4) + _background("B", 4))
    ps = [s["p"] for s in edge["evidence"]["null"]["per_seed"]]
    assert len(set(ps)) > 1 and edge["null_p"] == max(ps)
