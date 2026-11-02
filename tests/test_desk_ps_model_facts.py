"""Frozen-cut PumpSwap model facts from real SQLite fixtures, without provider calls."""
import copy
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_desk_ps_tape as PT
from sniper.services import memecoin_desk_ps_losses as L
from sniper.services import memecoin_desk_structure as S
from test_memecoin_desk_pumpswap_contract import ps_candidate
from test_memecoin_desk_contract import rehash

try:
    from sniper.services import memecoin_desk_ps_facts as F
except ImportError:
    F = None

NOW, SOL, CUT = 1_800_000_000.0, 100.0, 1000
COLS = ("pool,mint,quote_mint,slot,signature,ordinal,is_buy,actor,coin_creator,base_atomic,quote_atomic,"
        "post_base_reserve_atomic,post_quote_reserve_atomic,block_time,received_at,payload_json,"
        "pre_base_reserve_atomic,pre_quote_reserve_atomic,tx_index")
ROWS = [(-100, 1.0, 1, 10, "old"), (-55, 2.0, 1, 50, "buyer"), (-40, 1.8, 0, 20, "seller"),
        (-25, 3.0, 1, 500, next(iter(S.AGENTS))), (-10, 4.0, 1, 600, "ours"),
        (-5, 2.2, 1, 30, "buyer2")]


def api():
    assert F is not None, "PumpSwap model facts reader is missing"
    return F


def candidate(decimals=6):
    c = ps_candidate()
    c["emitted_at"] = "2027-01-15T08:00:00+00:00"  # NOW; tape and knowledge use one host clock
    for r in c["snapshot"]["fields"].values():
        r["known_at"] = NOW
    c["snapshot"]["emission_at"] = NOW
    state = c["snapshot"]["fields"]["pool_state"]
    state["value"].update(base_decimals=decimals, cut_slot=CUT, sol_usd=SOL,
                          reserve_known_at=NOW - 5, sol_age_s=1)
    if decimals is None:
        state["completeness"] = "partial"
        c["snapshot"]["missing"] = ["pool_state"]
    return rehash(c)


def tape(tmp, rows=ROWS, *, coverage=3600):
    c = candidate()
    path = tmp / "pumpswap" / "tape.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE swaps_ps(" + COLS + ",PRIMARY KEY(signature,ordinal))")
        db.execute("CREATE INDEX IF NOT EXISTS swaps_ps_received ON swaps_ps(received_at)")
        db.execute("CREATE INDEX IF NOT EXISTS swaps_ps_pool_slot ON swaps_ps(pool,slot)")
        db.execute("CREATE INDEX swaps_ps_pool_received ON swaps_ps(pool,received_at)")
        db.execute("CREATE INDEX IF NOT EXISTS swaps_ps_pool_slot ON swaps_ps(pool,slot)")
        db.execute("CREATE TABLE gaps_ps(at REAL,cause TEXT,last_slot INTEGER)")
        db.execute("CREATE INDEX gaps_ps_at ON gaps_ps(at)")
        db.execute("CREATE TABLE ps_decode_losses(signature TEXT,cause TEXT,received_at REAL,slot INTEGER,"
                   "PRIMARY KEY(signature,cause)) WITHOUT ROWID")
        db.execute("CREATE INDEX ps_decode_losses_received ON ps_decode_losses(received_at)")
        for ddl in L.SCHEMAS.values():
            db.execute(ddl)
        for statements in L.INDEXES.values():
            for ddl in statements:
                db.execute(ddl)
        if coverage is not None:
            db.execute("INSERT INTO ps_pool_selection VALUES(?,?,?,?,1)",
                       (c["pool"], "fixture", NOW - coverage, None))
            db.execute("INSERT INTO ps_capture_intervals VALUES(?,?,?,1)", ("fixture", NOW - coverage, NOW))
        previous = None
        for n, (rel, price, buy, usd, actor) in enumerate(rows):
            base, quote = 10**12, int(price / SOL * 10**6 * 10**9)
            payload = json.dumps({"ix_name": "buy", "virtual_quote_source": PT.VQ_SOURCE,
                                  "virtual_quote_atomic": "0", "virtual_quote_reserves": 0})
            db.execute("INSERT INTO swaps_ps VALUES(" + ",".join("?" * 19) + ")", (
                c["pool"], c["mint"], PT.WSOL_MINT, n + 10, "fixture%d" % n, 0, buy, actor, "creator",
                "1000", str(int(usd / SOL * 1e9)), str(base) if buy else None, str(quote) if buy else None,
                int(NOW + rel), NOW + rel, payload if buy else "{}", str(base),
                str(previous if previous is not None else quote), n + 1))
            previous = quote
    return c, path


def facts(c, tmp, **kw):
    as_of = kw.pop("as_of", NOW)
    c = copy.deepcopy(c)
    c["snapshot"]["emission_at"] = as_of
    c["emitted_at"] = datetime.fromtimestamp(as_of, timezone.utc).isoformat()
    rehash(c)
    kw.setdefault("version",api().VERSION)
    return api().facts_for(c, desk_dir=tmp, as_of=as_of, receipt_cut=NOW,
                           cut_slot=CUT, ours=frozenset(("ours",)), **kw)


def test_full_window_flow_excludes_ours_and_agents_but_prices_keep_agent(tmp_path):
    c, _ = tape(tmp_path)
    original = copy.deepcopy(c)
    out = facts(c, tmp_path)
    assert c == original
    assert out["flow_60s"] == {"buy_n": 2, "sell_n": 1, "buy_usd": 80, "sell_usd": 20,
                                 "makers":3,"largest_buy":50,"buyers":2,"sellers":1,"n_swaps":3,
                                "covered_s":60,"partial":False,"lower_bound":True,"loss_classes_exclusive":True,
                                "unknown_loss_receipts":0,"global_loss_receipts":0,"own_pool_losses":0}
    assert out["price_vs_60s_low_pct"] == pytest.approx((2.2 / 1.8 - 1) * 100)
    assert out["price_vs_60s_high_pct"] == pytest.approx((2.2 / 3 - 1) * 100)
    assert out["coverage"]["60s"]["state"] == "complete"
    assert out["structure"]["windows"]["60s"]["n_agent_swaps"] == 1
    assert len(out["structure"]["chart"]) == 24


@pytest.mark.parametrize("decimals,multiplier", [(6, 1), (9, 1000), (None, None)])
def test_decimals_are_explicit_and_unknown_price_never_erases_flow(tmp_path, decimals, multiplier):
    _, _ = tape(tmp_path)
    out = facts(candidate(decimals), tmp_path)
    assert out["flow_60s"]["buy_usd"] == 80
    if multiplier is None:
        assert out["structure"]["price_usd"] is None
        assert out["price_vs_60s_low_pct"] is None
    else:
        assert out["structure"]["price_usd"] == pytest.approx(2.2 * multiplier)


@pytest.mark.parametrize("coverage,state,seconds", [(10, "partial", 10), (None, "unknown", 0)])
def test_partial_inclusion_never_becomes_a_full_window_from_first_swap(tmp_path, coverage, state, seconds):
    c, _ = tape(tmp_path, coverage=coverage)
    out = facts(c, tmp_path)
    assert out["flow_60s"]["partial"] is True and out["price_vs_60s_low_pct"] is None
    assert out["coverage"]["60s"]["state"] == state
    assert out["coverage"]["60s"]["covered_s"] == seconds
    assert out["structure"]["windows"]["60s"]["partial"] is True
    assert out["structure"]["windows"]["60s"]["covered_s"] == seconds
    assert out["structure"]["recent_volume_concentration_15_of_60"] is None
    # Unobserved empty bars cannot masquerade as proven zero demand.
    assert out["structure"]["chart"][0][5:] == [None, None, None]


def test_loss_marks_partial_flow_and_price_only_witness_never_adds_a_maker(tmp_path):
    c, path = tape(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE swaps_ps SET pre_quote_reserve_atomic='25000000000' WHERE signature='fixture5'")
        db.execute("INSERT INTO gaps_ps VALUES(?,?,0)", (NOW - 7, "worker_reconnect"))
    out = facts(c, tmp_path)
    assert out["flow_60s"]["partial"] is True
    assert out["flow_60s"]["n_swaps"] == 3
    assert out["structure"]["windows"]["60s"]["n_swaps"] == 3
    assert out["structure"]["windows"]["60s"]["buyers"] == 2


def test_missing_loss_schema_is_unknown_instead_of_healthy_empty_tape(tmp_path):
    c, path = tape(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE ps_decode_losses")
    out = facts(c, tmp_path)
    assert out["flow_60s"] is None
    assert out["structure"]["tape"] == "unknown"
    assert out["coverage"]["60s"]["state"] == "unknown"


def test_validator_closes_schema_rejects_nonfinite_and_copies_nested_data(tmp_path):
    c, _ = tape(tmp_path)
    out = facts(c, tmp_path)
    validated = api().validate(out)
    validated["coverage"]["60s"]["covered_s"] = 0
    assert out["coverage"]["60s"]["covered_s"] == 60
    for path, bad in [("foreign", 1), ("age_s", float("nan")), ("flow_60s", {"buy_n": True})]:
        poisoned = copy.deepcopy(out)
        poisoned[path] = bad
        with pytest.raises(ValueError, match="ps_facts"):
            api().validate(poisoned)


def test_an_unpriced_final_sell_never_disappears_from_full_window_range(tmp_path):
    c, _ = tape(tmp_path, ROWS + [(-1, 0.5, 0, 20, "late_seller")])
    out = facts(c, tmp_path)
    assert out["flow_60s"]["sell_n"] == 2
    assert out["price_vs_60s_low_pct"] is None
    assert out["price_vs_60s_high_pct"] is None
    assert out["price_vs_1h_high_pct"] is None


def test_quiet_window_has_no_seen_flow_even_with_selection(tmp_path):
    c, _ = tape(tmp_path, [])
    out = facts(c, tmp_path)
    assert out["flow_60s"] is None
    assert out["price_vs_60s_low_pct"] is None and out["age_s"] is None


def test_missing_coverage_index_reports_unknown_while_retaining_observed_structure(tmp_path):
    c, path = tape(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX ps_pool_selection_pool_start")
    out = facts(c, tmp_path)
    assert out["flow_60s"]["covered_s"] == 0 and out["flow_60s"]["partial"] is True
    assert out["coverage"]["60s"]["reason"] == "coverage_unreadable"
    assert out["structure"]["windows"]["60s"]["n_swaps"] == 3


@pytest.mark.parametrize("poison", ["cut", "coverage", "makers", "largest_buy"])
def test_validator_rejects_internal_cut_coverage_and_flow_contradictions(tmp_path, poison):
    c, _ = tape(tmp_path)
    out = facts(c, tmp_path)
    if poison == "cut":
        out["structure"]["as_of"] += 1
    elif poison == "coverage":
        out["structure"]["windows"]["60s"]["partial"] = True
    elif poison == "makers":
        out["flow_60s"]["makers"] = 100
    else:
        out["flow_60s"]["largest_buy"] = 1000
    with pytest.raises(ValueError, match="ps_facts"):
        api().validate(out)


@pytest.mark.parametrize("version", ["pumpswap-facts-v1", "pumpswap-facts-v2"])
def test_a_proven_quiet_window_preserves_versioned_flow_and_known_price(tmp_path, version):
    c, _ = tape(tmp_path, [(-100, 1.0, 1, 10, "old")])
    out = facts(c, tmp_path, version=version)
    if version == "pumpswap-facts-v1":
        assert out["flow_60s"]["buy_n"] == 0
    else:
        assert out["flow_60s"] is None
    assert out["price_vs_60s_low_pct"] == 0 and out["price_vs_60s_high_pct"] == 0

