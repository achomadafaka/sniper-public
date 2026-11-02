"""Wallet Map bot trail and best herd member (pre-registration
docs/audits/2026-09-30-wallet-map-reads-preregistration.md C and D). Pure functions only; the reads
and their budgets are wired in wallet_link_graph.py.

- Regular: k >= 3 of the herder's n read coins and k/n >= 10%; none named under n = 10; truncated
  reads are out of k and n.
- Probe: a regular of 2+ FROZEN herders; our roster and our addresses never.
- Leader of a sampled probe buy: bought the coin in the probe's slot with a lower index, or in the
  10 s before; proven by its own signature and payment; creator, launch bundle, probes and our
  addresses excluded. Candidate: leads 3+ distinct probes on 3+ distinct coins.
- Round trips: SOL out and in from the probe's own balance changes, fees included, failed
  transactions charged to the one coin in their keys; closed / open / partial / unpriced.
"""
from scripts.research import wallet_probes as p
from scripts.research.wallet_upstream import USDC

P, C, C2 = "ProbeBot", "CoinMint1", "CoinMint2"
SOL = 10 ** 9


def _tx(sig, slot, idx, bt, signers, *, native=None, tok=None, fee=5000, err=None, keys=()):
    addrs = list(signers) + [a for a in (native or {}) if a not in signers]
    addrs += [k for k in keys if k not in addrs]
    pre = [10 ** 12 for _ in addrs]
    post = [10 ** 12 + (native or {}).get(a, 0) for a in addrs]
    ptb, qtb = [], []
    for (owner, mint), (a, b) in (tok or {}).items():
        ptb.append({"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(a), "decimals": 6}})
        qtb.append({"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(b), "decimals": 6}})
    return {"slot": slot, "transactionIndex": idx, "blockTime": bt,
            "transaction": {"signatures": [sig], "message": {"accountKeys": addrs,
                                                             "header": {"numRequiredSignatures": len(signers)}}},
            "meta": {"fee": fee, "err": err, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": ptb, "postTokenBalances": qtb}}


def _buy(sig, who, mint, slot, idx, bt, *, sol=1.0, held=0, got=1000, fee=5000, err=None):
    return _tx(sig, slot, idx, bt, [who], native={who: -int(sol * SOL) - fee}, tok={(who, mint): (held, held + got)},
               fee=fee, err=err, keys=(mint,))


def _sell(sig, who, mint, slot, idx, bt, *, sol=1.5, held=1000, gone=1000, fee=5000):
    return _tx(sig, slot, idx, bt, [who], native={who: int(sol * SOL) - fee}, tok={(who, mint): (held, held - gone)},
               fee=fee, keys=(mint,))


# ---------------------------------------------------------------- regulars and probes
def _read(h, mint, companions, *, index=10, truncated=False, status="ok"):
    return {"source_user_id": h, "mint": mint, "index": index, "status": status, "truncated": truncated,
            "network_id": "solana", "companions": [{"trader": t, "index": i} for t, i in companions]}


def _reads():
    out = []
    for i in range(10):                                        # H1: 10 read coins
        out.append(_read("H1", "a%d" % i, [("X", 20)] if i < 4 else [("Y", 20)] if i < 6 else [("Z", 5)]))
    for i in range(10):                                        # H2: 10 read coins, X behind on 3
        out.append(_read("H2", "b%d" % i, [("X", 30)] if i < 3 else [("R", 30)] * (i < 7)))
    for i in range(5):                                         # H3: 5 read coins, too few to name a regular
        out.append(_read("H3", "c%d" % i, [("Y", 20)]))
    out.append(_read("H1", "t0", [("X", 20)], truncated=True))  # out of k and n
    for i in range(40):                                        # H4: 40 read coins, W behind on 3 (7.5%)
        out.append(_read("H4", "d%d" % i, [("W", 20)] if i < 3 else []))
    return out


def test_a_regular_is_three_coins_and_ten_percent_of_ten_or_more():
    reg = p.regulars(_reads())
    assert reg["H1"] == {"X": 4} and "Z" not in reg["H1"]          # Z is ahead of H1 in its slot
    assert reg["H2"] == {"X": 3, "R": 4} and "H3" not in reg       # Y has 2 of 10 behind H1; H3 has n = 5
    assert reg["H4"] == {}                                         # 3 coins, but 3 of 40 is under 10%
    assert p.herds(_reads())["H1"][0] == 10                        # the truncated read is not a read coin


def test_one_read_stands_for_a_pair_the_first_buy_read():
    """Gamma MEDIUM 1: a bot behind only on a LATER read of the coin is not a member."""
    at = lambda h: "2026-09-30T%02d:00:00+00:00" % h                    # noqa: E731
    reads = [dict(_read("H", "m1", [("LATE", 20)]), signature="later", intake="detection", detected_at=at(9)),
             dict(_read("H", "m1", [("EARLY", 20)]), signature="first", intake="detection", detected_at=at(5)),
             dict(_read("H", "m2", [("B", 20)]), signature="cov", intake="j7", detected_at=at(5)),
             dict(_read("H", "m3", [("B", 20)]), signature="only-later", intake="detection", detected_at=at(9)),
             dict(_read("H", "m4", [("B", 20)], truncated=True), signature="trunc", intake="j7", detected_at=at(5)),
             dict(_read("H", "m5", [], status="no_signature"), signature="nosig:H:m5", intake="j7", detected_at=at(5))]
    t5 = 1790744400.0                                                   # 2026-09-30T05:00Z
    first = {("H", "m1"): ("first", t5), ("H", "m2"): ("sig2", t5), ("H", "m3"): ("first3", t5),
             ("H", "m4"): ("trunc", t5), ("H", "m5"): (None, t5), ("H", "m6"): ("never-read", t5)}
    chosen, details = p.unit_reads(reads, first)
    assert sorted(r["signature"] for r in chosen) == ["cov", "first"]
    assert p.herds(chosen)["H"] == (2, {"EARLY": 1, "B": 1})             # LATE is not a member
    assert details == {"H": {"read": 2, "not_first": 1, "truncated": 1, "no_signature": 1, "unread": 1}}


def test_a_pair_whose_read_first_buy_left_the_window_keeps_that_read():
    """Gamma (d4f2e636e): read at its true first buy by the old intake, which left the 14 days, then
    rebought inside them. coverage_pending holds the pair as done; the unit is the old read."""
    old = dict(_read("H", "m1", [("BOT", 20)]), signature="true-first", intake="detection",
               detected_at="2026-09-10T00:00:00+00:00")
    later = dict(_read("H", "m1", [("LATE", 20)]), signature="rebuy-other", intake="detection",
                 detected_at="2026-09-29T00:00:00+00:00")
    rebuy = 1790640000.0                                                # 2026-09-29T00:00Z, the window's first buy
    chosen, details = p.unit_reads([later, old], {("H", "m1"): ("rebuy-sig", rebuy)})
    assert [r["signature"] for r in chosen] == ["true-first"] and details == {"H": {"read": 1}}
    chosen, details = p.unit_reads([later], {("H", "m1"): ("rebuy-sig", rebuy - 1)})
    assert chosen == [] and details == {"H": {"not_first": 1}}          # a read after the first buy never stands in
    rebuy_read = dict(_read("H", "m1", [("LATE", 20)]), signature="rebuy-sig", intake="detection",
                      detected_at="2026-09-29T00:00:02+00:00")          # the rebuy was read too (host time)
    for first_read in (old, dict(old, intake="j7")):
        chosen, _d = p.unit_reads([rebuy_read, first_read], {("H", "m1"): ("rebuy-sig", rebuy)})
        assert [r["signature"] for r in chosen] == ["true-first"]       # never the rebuy's read
    chosen, _d = p.unit_reads([rebuy_read], {("H", "m1"): ("rebuy-sig", rebuy)})
    assert [r["signature"] for r in chosen] == ["rebuy-sig"]            # its own first buy, seen 2 s after chain time
    stale_cov = dict(rebuy_read, signature="cov-of-later", intake="j7", detected_at="2026-09-29T00:00:30+00:00")
    chosen, details = p.unit_reads([stale_cov], {("H", "m1"): ("earlier-sig", rebuy)})
    assert chosen == [] and details == {"H": {"not_first": 1}}          # an earlier buy became known: wait for its read


def test_a_probe_is_a_regular_of_two_frozen_herders_and_never_ours():
    assert p.probes(_reads(), frozen={"H1", "H2"}) == {"X": {"H1": 4, "H2": 3}}
    assert p.probes(_reads(), frozen={"H1"}) == {}                 # only frozen herders count
    assert p.probes(_reads(), frozen={"H1", "H2"}, excluded={"X"}) == {}


# ---------------------------------------------------------------- sampling
def test_buys_are_sampled_uniformly_with_a_seed_from_succeeded_buys_only():
    txs = [_buy("b%d" % i, P, "m%d" % i, 100 + i, 1, 1000 + i) for i in range(30)]
    txs += [_buy("failed", P, "mf", 90, 1, 990, err={"InstructionError": [0, "x"]}),
            _sell("s", P, "m0", 200, 1, 2000),
            _tx("airdrop", 91, 1, 991, [P], native={P: -5000}, tok={(P, "mdrop"): (0, 1000)}, keys=("mdrop",))]
    one, two = p.sample_buys(txs, P, seed=7), p.sample_buys(txs, P, seed=7)
    assert one == two and len(one) == 10
    assert p.sample_buys(txs, P, seed=8) != one
    assert {b[2] for b in p.probe_buys(txs, P)} == {"m%d" % i for i in range(30)}   # no failed buy, sell, airdrop
    assert len(p.sample_buys(txs[:3], P, seed=7)) == 3


# ---------------------------------------------------------------- leaders
def test_a_leader_bought_in_the_probe_slot_before_it_or_in_the_ten_seconds_before():
    buy = ((500, 20), 1000, C, "probe-buy")
    window = [_buy("same-slot-before", "L1", C, 500, 5, 1000), _buy("same-slot-after", "L2", C, 500, 25, 1000),
              _buy("9s-before", "L3", C, 499, 50, 991), _buy("11s-before", "L4", C, 497, 3, 989),
              _buy("creator-buy", "DEV", C, 498, 1, 995), _buy("probe-itself", P, C, 499, 1, 995),
              _tx("airdrop", 499, 60, 995, ["L5"], native={"L5": -5000}, tok={("L5", C): (0, 1000)}, keys=(C,))]
    assert p.leaders(window, buy, excluded={"DEV", P}) == {"L1", "L3"}   # L5 got coins without paying


def test_the_creator_and_the_launch_bundle_are_excluded():
    first = _buy("create", "DEV", C, 400, 0, 900)
    window = [_buy("bundle", "SNIPER", C, 400, 3, 900), _buy("later", "EARLY", C, 401, 0, 901)]
    assert p.launch_excluded(first, window, C) == {"DEV", "SNIPER"}
    assert p.launch_excluded(None, window, C) == set()


def test_a_candidate_leads_three_probes_on_three_coins():
    leads = [("P1", "m1", "W"), ("P2", "m2", "W"), ("P3", "m3", "W"),
             ("P1", "m1", "V"), ("P2", "m1", "V"), ("P3", "m1", "V"),        # one coin only
             ("P1", "m1", "U"), ("P1", "m2", "U"), ("P1", "m3", "U")]        # one probe only
    assert p.candidates(leads) == {"W": {"probes": 3, "coins": 3}}


# ---------------------------------------------------------------- round trips
def test_a_coin_bought_and_sold_in_the_stretch_is_closed_with_fees_in_its_net():
    rows, unattributed, failed = p.round_trips(
        [_buy("b", P, C, 10, 1, 1000), _sell("s", P, C, 20, 1, 1060)], P)
    r = rows[C]
    assert r["status"] == "closed" and abs(r["net_sol"] - (0.5 - 2 * 5000 / SOL)) < 1e-12
    assert (r["first_buy"], r["last_sell"], unattributed, failed) == (1000, 1060, 0.0, 0)


def test_open_partial_and_unpriced_coins_are_never_a_win_or_a_loss():
    txs = [_buy("b1", P, C, 10, 1, 1000), _sell("s1", P, C, 11, 1, 1010, gone=900),          # 10% left: open
           _buy("b2", P, C2, 12, 1, 1020, held=500),                                          # held before: partial
           _sell("s3", P, "Sold1st", 13, 1, 1030),                                            # sold first: partial
           _tx("usdc", 14, 1, 1040, [P], native={P: -5000},
               tok={(P, "UsdcCoin"): (0, 1000), (P, USDC): (5_000_000, 0)}, keys=("UsdcCoin",))]
    rows, _u, _f = p.round_trips(txs, P)
    assert {m: r["status"] for m, r in rows.items()} == {C: "open", C2: "partial", "Sold1st": "partial",
                                                         "UsdcCoin": "unpriced"}


def test_a_coin_left_with_one_percent_is_closed():
    rows, _u, _f = p.round_trips([_buy("b", P, C, 10, 1, 1000), _sell("s", P, C, 11, 1, 1001, gone=990)], P)
    assert rows[C]["status"] == "closed"


def test_a_failed_transaction_charges_its_fee_to_its_one_coin():
    failed = _tx("f1", 15, 1, 1005, [P], native={P: -7000}, fee=7000, err={"x": 1}, keys=(C,))
    stray = _tx("f2", 16, 1, 1006, [P], native={P: -3000}, fee=3000, err={"x": 1}, keys=("Elsewhere",))
    theirs = _tx("f3", 17, 1, 1007, ["Other"], native={"Other": -9000}, fee=9000, err={"x": 1}, keys=(C,))
    both = _tx("f4", 18, 1, 1008, [P], native={P: -2000}, fee=2000, err={"x": 1}, keys=(C, C2))
    rows, unattributed, n_failed = p.round_trips(
        [_buy("b", P, C, 10, 1, 1000), _buy("b2", P, C2, 11, 1, 1001), failed, stray, theirs, both,
         _sell("s", P, C, 20, 1, 1060)], P)
    assert abs(rows[C]["fees_failed_sol"] - 7000 / SOL) < 1e-15 and rows[C2]["fees_failed_sol"] == 0
    assert abs(unattributed - 5000 / SOL) < 1e-15 and n_failed == 4   # two coins in the keys: unattributed


def test_profitable_needs_ten_closed_coins_a_positive_sum_and_median():
    trips = []
    for i in range(10):
        trips += [_buy("b%d" % i, P, "m%d" % i, 10 * i, 1, 1000 + 10 * i),
                  _sell("s%d" % i, P, "m%d" % i, 10 * i + 5, 1, 1005 + 10 * i, sol=1.2)]
    rows, unattributed, _f = p.round_trips(trips, P)
    v = p.profitable(rows, unattributed)
    assert v["profitable"] and v["closed"] == 10 and v["gain_share"] == 1.0 and v["median_hold_s"] == 5
    assert not p.profitable(dict(list(rows.items())[:9]))["profitable"]
    assert not p.profitable(rows, unattributed_fee_sol=3.0)["profitable"]   # fees can sink the total


def test_a_probe_sell_inside_60_s_after_a_herder_buy_is_a_sell_into_the_herd():
    txs = [_sell("s1", P, C, 10, 1, 1030), _sell("s2", P, C2, 11, 1, 1100), _sell("s3", P, "Other", 12, 1, 1010)]
    assert p.sells_into(txs, P, {C: [1000], C2: [1000]}) == 1          # 30 s after: yes; 100 s after: no


# ---------------------------------------------------------------- the pass (wallet_link_graph.trail_pass)
import json                                                            # noqa: E402
import sqlite3                                                         # noqa: E402

from scripts.research import wallet_link_graph as g                    # noqa: E402

FROZEN = {"frozen_at": "2026-10-02T00:00:00Z", "herders": ["H1"], "probes": [P, "P2"]}
NOW = 1_790_000_000


def _probe_txs(probe, n=12):
    return [_buy("%s-b%d" % (probe, i), probe, "m%d" % i, 1000 + i, 20, NOW - 3600 + i) for i in range(n)]


def _fakes(calls, *, window_stop=False):
    def pages(address, since_t):
        calls["pages"].append((address, since_t))
        return _probe_txs(address), 2, False

    def first(mint):
        calls["first"].append(mint)
        return _buy("create-" + mint, "DEV", mint, 900, 0, NOW - 7200), 1

    def window(mint, bt):
        calls["window"].append(mint)
        if window_stop:
            raise g.ReadStop("HTTP 429 on getTransactionsForAddress")
        slot = 1000 + int(mint[1:])
        return [_buy("lead-" + mint, "L1", mint, slot, 5, bt), _buy("dev-" + mint, "DEV", mint, slot, 6, bt),
                _buy("other-probe-" + mint, "P2", mint, slot, 7, bt), _buy("own-" + mint, "OURS", mint, slot, 8, bt)], 1, False
    return pages, first, window


def _trail(tmp_path, *, budget=500, frozen=FROZEN, flat=lambda: True, window_stop=False):
    calls = {"pages": [], "first": [], "window": []}
    pages, first, window = _fakes(calls, window_stop=window_stop)
    out = g.trail_pass(tmp_path / "graph.db", frozen, budget=budget, flat_fn=flat, own_addresses={"OURS"},
                       now_t=NOW, herder_buys={}, fetch_pages=pages, fetch_first=first, fetch_window=window)
    return out, calls, sqlite3.connect(tmp_path / "graph.db")


def test_each_frozen_probe_is_read_once_a_day_and_its_leaders_stored(tmp_path):
    out, calls, db = _trail(tmp_path)
    assert (out["probe_days"], out["stop"], out["pending"]) == (2, "done", 2)
    assert [a for a, _s in calls["pages"]] == ["P2", P] and all(s == NOW - 86400 for _a, s in calls["pages"])
    days = {r[0]: r[1:] for r in db.execute("SELECT probe, status, sampled, seed, list_pages, calls FROM memecoin_wallet_probe_days")}
    assert days[P][:3] == ("ok", 10, g._seed(P, g.datetime.fromtimestamp(NOW, g.timezone.utc).date().isoformat())) and days[P][3] == 2
    leaders = {r[0] for r in db.execute("SELECT leader FROM memecoin_wallet_probe_leads")}
    assert leaders == {"L1"}                                        # never the creator, a probe or our own wallet
    assert len(calls["first"]) == len(set(calls["first"]))          # the creator is read once per mint per run
    assert out["calls"] == 2 * 2 + len(set(calls["first"])) + len(calls["window"])
    out2, calls2, _db = _trail(tmp_path)
    assert (out2["pending"], calls2["pages"]) == (0, [])            # read today: not again


def test_the_pass_needs_the_frozen_list_the_budget_and_a_flat_book(tmp_path):
    out, calls, _db = _trail(tmp_path, frozen={"probes": [P]})
    assert out["stop"] == "no frozen list" and calls["pages"] == []
    out, calls, _db = _trail(tmp_path, budget=g.TRAIL_WORST - 1)
    assert out["stop"] == "budget" and calls["pages"] == []
    out, calls, _db = _trail(tmp_path, flat=lambda: False)
    assert out["stop"] == "solana book open" and calls["pages"] == []


def test_a_failed_read_leaves_the_probe_day_unread_and_stops(tmp_path):
    out, calls, db = _trail(tmp_path, window_stop=True)
    assert out["stop"].startswith("stopped: HTTP 429") and out["probe_days"] == 0
    assert db.execute("SELECT probe, status FROM memecoin_wallet_probe_days").fetchall() == [("P2", "unread")]
    assert db.execute("SELECT COUNT(*) FROM memecoin_wallet_probe_leads").fetchone()[0] == 0
    out, calls, _db = _trail(tmp_path)                               # retried
    assert out["probe_days"] == 2


def test_the_stretch_is_the_newest_24_h_every_status_and_says_when_it_was_cut():
    seen = []

    def rpc(method, params):
        seen.append(params[1].copy())
        n = 100 if len(seen) < 3 else 40
        return {"data": [{"blockTime": 5} for _ in range(n)], "paginationToken": "t%d" % len(seen)}
    txs, used, cut = g.helius_address_pages("A", since_t=123, pages=10, rpc=rpc)
    assert (len(txs), used, cut) == (240, 3, False)
    assert seen[0]["sortOrder"] == "desc" and seen[0]["filters"] == {"blockTime": {"gte": 123}}   # no status filter
    seen.clear()
    _txs, used, cut = g.helius_address_pages("A", since_t=123, pages=2, rpc=rpc)
    assert (used, cut) == (2, True)


def test_the_sample_frame_is_the_newest_200_transactions_and_the_seed_is_per_probe_and_day(tmp_path):
    newest = [_sell("s%d" % i, P, "m0", 5000 - i, 1, NOW - i) for i in range(197)]
    newest += [_buy("b%d" % i, P, "new%d" % i, 4000 - i, 1, NOW - 300 - i) for i in range(3)]
    older = [_buy("o%d" % i, P, "old%d" % i, 3000 - i, 1, NOW - 600 - i) for i in range(50)]
    calls = {"window": []}
    frozen = dict(FROZEN, probes=[P])
    g.trail_pass(tmp_path / "graph.db", frozen, budget=500, flat_fn=lambda: True, own_addresses=set(), now_t=NOW,
                 fetch_pages=lambda a, since_t: (newest + older, 3, False), fetch_first=lambda m: (None, 1),
                 fetch_window=lambda m, bt: calls["window"].append(m) or ([], 1, False))
    sampled = sqlite3.connect(tmp_path / "graph.db").execute("SELECT sampled FROM memecoin_wallet_probe_days").fetchone()[0]
    assert sampled == 3 and not any(m.startswith("old") for m in calls["window"])
    assert g._seed(P, "2026-10-01") != g._seed("P2", "2026-10-01") != g._seed("P2", "2026-10-02")


# ---------------------------------------------------------------- the one-off Axiom kol-list read (F)
def test_first_buys_are_each_coins_earliest_listed_buy_for_the_newest_coins():
    txs = [_buy("a-late", "A", "ma", 30, 1, 3000), _buy("a-early", "A", "ma", 10, 1, 1000),
           _buy("b", "A", "mb", 20, 1, 2000), _buy("c", "A", "mc", 5, 1, 500)]
    buys, prior = p.first_buys(txs, "A", 2)
    assert [b[3] for b in buys] == ["a-early", "b"] and prior == []      # mc's first buy is the oldest


def test_a_coin_that_left_the_wallet_before_its_first_listed_buy_is_a_prior_position():
    """Gamma F guard 1: the wallet held it before the listing, so its first listed buy is not its first."""
    txs = [_sell("s-old", "A", "mheld", 5, 1, 500), _buy("b-held", "A", "mheld", 10, 1, 1000),
           _tx("send", 6, 1, 600, ["A"], native={"A": -5000}, tok={("A", "msent"): (1000, 0)}, keys=("msent",)),
           _buy("b-sent", "A", "msent", 11, 1, 1100), _buy("b-new", "A", "mnew", 12, 1, 1200),
           _sell("s-after", "A", "mnew", 13, 1, 1300)]                  # a sale AFTER the buy is fine
    buys, prior = p.first_buys(txs, "A", 2)
    assert [b[2] for b in buys] == ["mnew"] and prior == ["mheld", "msent"]


def test_the_listing_stops_when_enough_coins_are_found():
    seen = []

    def rpc(method, params):
        seen.append(1)
        return {"data": [{"blockTime": 5}] * 100, "paginationToken": "t"}
    _txs, used, cut = g.helius_address_pages("A", since_t=0, pages=3, rpc=rpc, enough=lambda txs: len(txs) >= 200)
    assert (used, cut) == (2, True)                                     # stopped on a full page: older history


SEED = {"wallets": [{"address": "A", "handle": "kolA", "rank_7d": 1, "lists": ["kol"]},
                    {"address": "T", "handle": "tracked", "rank_7d": 2, "lists": ["kol", "pump"]}]}


def _axiom(tmp_path, *, budget=500, flat=lambda: True, stop_at=None, cut=False):
    calls = {"pages": [], "slots": []}

    def pages(address):
        calls["pages"].append(address)
        return [_buy("%s-%d" % (address, i), address, "m%d" % i, 100 + i, 3, 1000 + i) for i in range(25)], 1, cut

    def slot(det):
        calls["slots"].append(det)
        if stop_at is not None and len(calls["slots"]) > stop_at:
            raise g.ReadStop("HTTP 429 on getTransactionsForAddress")
        return [_buy(det["signature"], "SRC", det["mint"], det["slot"], 3, 1000),
                _buy("bot-" + det["signature"], "BOT", det["mint"], det["slot"], 9, 1000)], 1, False
    out = g.axiom_pass(tmp_path / "graph.db", SEED, budget=budget, flat_fn=flat, ours=set(), tracked={"T"},
                       fetch_pages=pages, fetch_slot=slot,
                       trade_fn=lambda tx, mint: {"trader": tx["transaction"]["signatures"][0].split("-")[0]
                                                  if tx["transaction"]["signatures"][0].startswith(("A-", "T-")) else
                                                  ("BOT" if tx["transaction"]["signatures"][0].startswith("bot") else "SRC"),
                                                  "side": "buy", "sol": 1.0},
                       sig_fn=lambda tx: tx["transaction"]["signatures"][0])
    return out, calls, sqlite3.connect(tmp_path / "graph.db")


def test_each_kol_wallet_is_read_once_into_its_own_table(tmp_path):
    out, calls, db = _axiom(tmp_path)
    assert (out["wallets"], out["reads"], out["stop"]) == (2, 40, "done")
    assert len(calls["slots"]) == 40 and {d["slot"] for d in calls["slots"]} >= {105, 124}   # its 20 newest coins
    reads = g.load_reads(tmp_path / "graph.db", "", table=g.AXIOM_READS)
    assert len(reads) == 40 and {r["intake"] for r in reads} == {"axiom"} and {r["status"] for r in reads} == {"ok"}
    assert g.load_reads(tmp_path / "graph.db", "") == []                # no slot edge, flow or herd count sees them
    rows = dict(db.execute("SELECT address, tracked FROM memecoin_wallet_axiom_wallets").fetchall())
    assert rows == {"A": 0, "T": 1}                                     # tracked is labelled, not dropped
    assert db.execute("SELECT DISTINCT list_cut, prior_positions FROM memecoin_wallet_axiom_wallets").fetchall() == [(0, 0)]
    herds = p.herds(reads)
    assert herds["A"] == (20, {"BOT": 20})
    out2, calls2, _db = _axiom(tmp_path)
    assert (out2["pending"], calls2["pages"]) == (0, [])                # a one-off read


def test_the_axiom_pass_holds_its_worst_case_and_leaves_a_failed_wallet_unread(tmp_path):
    out, calls, _db = _axiom(tmp_path, budget=g.AXIOM_WORST - 1)
    assert out["stop"] == "budget" and calls["pages"] == []
    out, calls, _db = _axiom(tmp_path, flat=lambda: False)
    assert out["stop"] == "solana book open" and calls["pages"] == []
    out, calls, db = _axiom(tmp_path, stop_at=5)
    assert out["stop"].startswith("stopped: HTTP 429") and out["wallets"] == 0
    assert db.execute("SELECT address, status, coins FROM memecoin_wallet_axiom_wallets").fetchall() == [("A", "unread", None)]
    assert g.load_reads(tmp_path / "graph.db", "", table=g.AXIOM_READS) == []
    out, _calls, _db = _axiom(tmp_path)
    assert out["wallets"] == 2                                          # retried


def test_the_kol_seed_takes_labels_and_ranks_from_each_lists_newest_payload():
    from scripts.research import axiom_kol_seed as ak

    def row(addr, rank, src):
        return {"source": src, "handle": addr.lower(), "wallets": {"solana": {"address": addr}},
                "windows": {"7d": {"rank": rank}}}
    rows = [(1, {"source": "pump", "rows": [row("K2", 1, "pump")]}),                 # K2 on pump only earlier
            (2, {"source": "kol", "rows": [row("K1", 2, "kol"), row("K2", 1, "kol")]}),
            (3, {"source": "pump", "rows": [row("K1", 5, "pump"), row("P", 1, "pump")]}),
            (4, {"source": "kol", "rows": [row("K1", 1, "kol"), row("K2", 2, "kol")]})]
    got = ak.seed(rows)
    assert [(w["address"], w["rank_7d"], w["lists"], w["earlier_only"]) for w in got] == [
        ("K1", 1, ["kol", "pump"], []), ("K2", 2, ["kol"], ["pump"])]


def test_a_wallet_whose_listing_stopped_on_a_full_page_is_list_cut(tmp_path):
    """Gamma F guard 2: older history exists, so its first buys may not be its first."""
    _out, _calls, db = _axiom(tmp_path, cut=True)
    assert db.execute("SELECT DISTINCT list_cut FROM memecoin_wallet_axiom_wallets").fetchall() == [(1,)]


USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


def test_a_quote_token_is_never_a_coin_row_and_a_stable_paid_coin_is_unpriced():
    """Gamma LOW 2 (1e114be02): USDT and the other quote mints became 'unpriced coins'."""
    txs = [_tx("usdt", 14, 1, 1040, [P], native={P: -5000},
               tok={(P, "UsdtCoin"): (0, 1000), (P, USDT): (5_000_000, 0)}, keys=("UsdtCoin",))]
    rows, _u, _f = p.round_trips(txs, P)
    assert {m: r["status"] for m, r in rows.items()} == {"UsdtCoin": "unpriced"}


def test_closing_the_token_account_is_flagged():
    """Gamma LOW 3: a first buy's native SOL carries the token-account rent, refunded only on a close."""
    sell = _sell("s", P, C, 20, 1, 1060)
    sell["meta"]["postTokenBalances"] = [b for b in sell["meta"]["postTokenBalances"]
                                         if (b["owner"], b["mint"]) != (P, C)]
    rows, _u, _f = p.round_trips([_buy("b", P, C, 10, 1, 1000), sell, _buy("b2", P, C2, 11, 1, 1001)], P)
    assert rows[C]["closes_ata"] is True and rows[C2]["closes_ata"] is False


def test_a_probe_day_never_exceeds_the_pre_registered_42_calls():
    assert g.TRAIL_WORST <= 42
