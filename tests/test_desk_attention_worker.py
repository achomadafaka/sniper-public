"""Attention worker (tape side): blind is not calm, ids are idempotent, dry vs fired, Beta's table contract. Offline."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memecoin_desk_attention_worker as W  # noqa: E402
from sniper.services import memecoin_desk_attention_signals as A  # noqa: E402
from sniper.services import memecoin_desk_attention_store as S  # noqa: E402
from sniper.services import memecoin_desk_attention_tape as T  # noqa: E402

NOW = 1_800_000_000.0
DAY = T.utc_day(NOW)
MINT = "MintAAAA"
CREATOR = "CreatorWallet"
P = A.Params()


def build(tmp, *, swaps=(), gaps=(), heartbeat=None, creator=CREATOR, holders=None):
    desk = tmp / "desk"
    for folder in ("swaps", "launches", "gaps"):
        (desk / folder).mkdir(parents=True)
    hb = {"ts": NOW - 0.5, "connected": True, "sol_usd": 100.0}
    hb.update(heartbeat or {})
    (desk / "universe.heartbeat").write_text(json.dumps(hb))
    c = sqlite3.connect(str(desk / "swaps" / (DAY + ".db")))
    c.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, is_buy INTEGER, "
              "quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, "
              "block_time INTEGER, received_at REAL, real_token INTEGER, fee_bps INTEGER, creator_fee_bps INTEGER, "
              "PRIMARY KEY(signature, ordinal))")
    for i, (t, actor, buy, usd, token) in enumerate(swaps):
        c.execute("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (1000 + i, "sig%d" % i, 0, MINT, "curve", int(buy), int(usd / 100.0 * 1e9), token, actor,
                   30_000_000_000 + i, 1_000_000_000_000, 0, NOW + t, 1, 100, 30))
    c.commit()
    c.close()
    c = sqlite3.connect(str(desk / "launches" / (DAY + ".db")))
    c.execute("CREATE TABLE launches(mint TEXT PRIMARY KEY, curve TEXT, creator TEXT, slot INTEGER, signature TEXT, "
              "block_time INTEGER, received_at REAL, symbol TEXT, name TEXT)")
    if creator:
        c.execute("INSERT INTO launches VALUES(?,?,?,?,?,?,?,?,?)", (MINT, "curve", creator, 1, "l", 0, NOW - 999, "", ""))
    c.commit()
    c.close()
    c = sqlite3.connect(str(desk / "gaps" / (DAY + ".db")))
    c.execute("CREATE TABLE gaps(at REAL, cause TEXT, last_slot INTEGER)")
    c.executemany("INSERT INTO gaps VALUES(?,?,?)", [(g, "x", 0) for g in gaps])
    c.commit()
    c.close()
    cl = None
    if holders is not None:
        cl = desk / "cluster.db"
        c = sqlite3.connect(str(cl))
        c.execute("CREATE TABLE cluster_snapshots(snapshot_id TEXT PRIMARY KEY, mint TEXT NOT NULL, snapshot_at TEXT NOT NULL)")
        c.execute("CREATE TABLE cluster_snapshot_holders(snapshot_id TEXT, ata TEXT, owner TEXT, balance_atomic TEXT, exclusion TEXT)")
        c.execute("INSERT INTO cluster_snapshots VALUES('s1',?,?)", (MINT, T.datetime.fromtimestamp(NOW - 500, T.timezone.utc).isoformat()))
        for i, (owner, bal) in enumerate(holders.items()):
            c.execute("INSERT INTO cluster_snapshot_holders VALUES('s1',?,?,?,NULL)", ("ata%d" % i, owner, str(bal)))
        c.commit()
        c.close()
    return desk, cl


POS = {"id": 7, "mint": MINT, "opened_at": NOW - 400}


def rows(desk, where=""):
    c = sqlite3.connect(str(desk / "attention" / "tape.db"))
    try:
        return c.execute("SELECT signal, fired, thresholds_version, value_json, id FROM attention %s ORDER BY id" % where).fetchall()
    finally:
        c.close()


def run(desk, cl, now=NOW, version="unset", positions=(POS,), params=P):
    return W.tick(desk_dir=desk, out_db=desk / "attention" / "tape.db", positions=list(positions), cluster_db=cl,
                  params=params, now=now, version=version)


CREATOR_SELL = [(-100, CREATOR, False, 60, 10)]


def test_creator_sell_is_written_dry_when_thresholds_unset_and_fired_when_frozen(tmp_path):
    desk, cl = build(tmp_path, swaps=CREATOR_SELL, holders={"H": 100})
    (desk / "attention").mkdir()
    assert run(desk, cl)["fired"] == 1
    sig, fired, version, body, _ = rows(desk, "WHERE signal='creator_sell'")[0]
    assert (fired, version) == (None, "unset") and json.loads(body)["usd"] == 60                    # measured, dry
    run(desk, cl, version="attn-v1")
    assert [(r[1], r[2]) for r in rows(desk, "WHERE signal='creator_sell'")] == [(None, "unset"), (1, "attn-v1")]


def test_restart_replays_nothing(tmp_path):
    desk, cl = build(tmp_path, swaps=CREATOR_SELL, holders={"H": 100})
    (desk / "attention").mkdir()
    run(desk, cl)
    before = rows(desk)
    run(desk, cl, now=NOW + 3)                                                                     # later tick, same trigger
    assert rows(desk) == before                                                                    # first write stands, never replaced


def test_stale_heartbeat_writes_unknown_and_no_detector_runs(tmp_path):
    desk, cl = build(tmp_path, swaps=CREATOR_SELL, holders={"H": 100}, heartbeat={"ts": NOW - 30})
    (desk / "attention").mkdir()
    s = run(desk, cl)
    assert s["fired"] == 0 and s["unknown"] == 1 and s["tape_ok"] is False
    (sig, fired, _, body, _), = rows(desk)
    assert (sig, fired) == ("tape", None) and json.loads(body)["reason"] == "tape_stale"           # blind, never calm
    for bad in ({"connected": False}, {"sol_usd": 0}, {"sol_usd": None}):
        d2, c2 = build(tmp_path / ("x%d" % len(str(bad))), swaps=CREATOR_SELL, heartbeat=bad)
        (d2 / "attention").mkdir()
        assert run(d2, c2)["tape_ok"] is False


def test_gap_in_the_lookback_suppresses_detectors_and_says_so(tmp_path):
    desk, cl = build(tmp_path, swaps=CREATOR_SELL, holders={"H": 100}, gaps=[NOW - 20])
    (desk / "attention").mkdir()
    s = run(desk, cl)
    assert s["fired"] == 0 and s["unknown"] == 1
    assert json.loads(rows(desk)[0][3])["reason"] == "tape_gap"
    d2, c2 = build(tmp_path / "old", swaps=CREATOR_SELL, holders={"H": 100}, gaps=[NOW - 5000])      # outside the look-back
    (d2 / "attention").mkdir()
    assert run(d2, c2)["fired"] == 1


def test_unknown_creator_and_missing_entry_snapshot_are_explicit_not_silent(tmp_path):
    desk, cl = build(tmp_path, swaps=[], creator=None, holders=None)
    (desk / "attention").mkdir()
    run(desk, cl)
    got = {(r[0], json.loads(r[3])["reason"]) for r in rows(desk)}
    assert got == {("creator_sell", "creator_unknown"), ("holder_dump", "no_entry_snapshot")}


def test_snapshot_after_entry_is_not_an_entry_snapshot(tmp_path):
    desk, cl = build(tmp_path, swaps=[], holders={"H": 100})
    late = {**POS, "opened_at": NOW - 900}                                                          # snapshot taken at NOW-500: after entry
    (desk / "attention").mkdir()
    run(desk, cl, positions=[late])
    assert ("holder_dump", "no_entry_snapshot") in {(r[0], json.loads(r[3])["reason"]) for r in rows(desk)}


def test_holder_dump_from_entry_snapshot(tmp_path):
    desk, cl = build(tmp_path, swaps=[(-50, "H", False, 60, 80)], holders={"H": 100})
    (desk / "attention").mkdir()
    run(desk, cl)
    dump = [json.loads(r[3]) for r in rows(desk, "WHERE signal='holder_dump' AND fired IS NULL AND value_json LIKE '%sold_pct%'")]
    assert dump and dump[0]["sold_pct"] == 80.0


def test_excluded_holders_are_not_in_the_dump_baseline(tmp_path):
    desk, cl = build(tmp_path, swaps=[(-50, "POOLACC", False, 60, 80)], holders={"H": 100})
    c = sqlite3.connect(str(cl))
    c.execute("INSERT INTO cluster_snapshot_holders VALUES('s1','ataX','POOLACC','100','program_account:Foo')")
    c.commit()
    c.close()
    (desk / "attention").mkdir()
    run(desk, cl)
    assert not [r for r in rows(desk, "WHERE signal='holder_dump'") if "sold_pct" in r[3]]


def test_our_wallet_swaps_are_ignored_via_ours_fn(tmp_path):
    desk, cl = build(tmp_path, swaps=[(-100, "OUR", False, 500, 1)] * 1 + CREATOR_SELL, holders={"H": 100}, creator="OUR")
    (desk / "attention").mkdir()
    W.tick(desk_dir=desk, out_db=desk / "attention" / "tape.db", positions=[POS], cluster_db=cl, params=P, now=NOW,
           ours_fn=lambda pos, swaps: {"OUR"})
    assert [r for r in rows(desk) if r[0] == "creator_sell" and "usd" in r[3]] == []


def test_table_is_betas_exact_contract_and_tape_price_is_published(tmp_path):
    desk, cl = build(tmp_path, swaps=CREATOR_SELL, holders={"H": 100})
    (desk / "attention").mkdir()
    run(desk, cl)
    c = sqlite3.connect(str(desk / "attention" / "tape.db"))
    cols = [r[1] for r in c.execute("PRAGMA table_info(attention)")]
    assert cols == ["id", "position_id", "mint", "signal", "value_json", "observed_at", "source", "fired", "thresholds_version"]
    price = c.execute("SELECT mint, price_sol, price_usd, receipt_cut FROM tape_price").fetchone()
    assert price[0] == MINT and price[1] == pytest.approx((30_000_000_000 / 1e9) / (1e12 / 1e6), rel=1e-6)
    assert price[2] == pytest.approx(price[1] * 100.0) and price[3] == NOW - W.COMMIT_MARGIN_S
    c.close()


def test_no_positions_writes_health_only(tmp_path):
    desk, cl = build(tmp_path)
    (desk / "attention").mkdir()
    s = run(desk, cl, positions=[])
    assert s["positions"] == 0 and rows(desk) == []
    c = sqlite3.connect(str(desk / "attention" / "tape.db"))
    assert c.execute("SELECT positions, tape_ok FROM health").fetchone() == (0, 1)


def test_one_bad_position_never_stops_the_others(tmp_path, monkeypatch):
    desk, cl = build(tmp_path, swaps=CREATOR_SELL, holders={"H": 100})
    (desk / "attention").mkdir()
    real = T.creator_of

    def flaky(d, mint, now, **kw):
        if mint == "BOOM":
            raise RuntimeError("x")
        return real(d, mint, now, **kw)
    monkeypatch.setattr(T, "creator_of", flaky)
    s = run(desk, cl, positions=[{"id": 8, "mint": "BOOM", "opened_at": NOW - 400}, POS])
    assert s["dropped"] == 1 and s["fired"] >= 1


def test_params_from_env_rejects_typos_and_nonpositive():
    assert A.params_from_env({"MEMECOIN_DESK_ATTN_SELL_BURST_RATIO": "3"}).sell_burst_ratio == 3.0
    assert A.params_from_env({"MEMECOIN_DESK_ATTN_EXODUS_MIN_BUYERS": "7"}).exodus_min_buyers == 7
    for bad in ("abc", "0", "-1", "nan", "inf"):
        with pytest.raises(ValueError):
            A.params_from_env({"MEMECOIN_DESK_ATTN_SELL_BURST_RATIO": bad})


def test_dry_count_counts_distinct_events_and_marks_gap_steps(tmp_path):
    desk, _ = build(tmp_path, swaps=CREATOR_SELL, holders={"H": 100}, gaps=[NOW + 100])
    (desk / "candidate_emissions").mkdir()
    emission = {"mint": MINT, "snapshot": {"emission_at": NOW - 200.0}}
    (desk / "candidate_emissions" / (DAY + ".jsonl")).write_text(json.dumps(emission) + "\n")
    out = tmp_path / "events.jsonl"
    r = W.dry_count(desk, [DAY], P, 100.0, life_s=300.0, step_s=10.0, events_out=str(out))
    assert r["positions"] == 1 and r["events"]["creator_sell"] == 1                               # once, not once per step
    assert r["unknown_steps_tape_gap"] > 0 and r["sol_usd"] == 100.0 and r["params_version"] == A.PARAMS_VERSION
    assert json.loads(out.read_text().splitlines()[0])["signal"] == "creator_sell"


def test_our_fill_signatures_remove_our_wallet_from_every_signal(tmp_path):
    # three distinct sellers would be a burst; one of them is OUR wallet, learned only from our fill's tx signature
    swaps = [(-9, "ours", False, 40, 5), (-8, "a", False, 40, 5), (-7, "b", False, 40, 5)]
    desk, cl = build(tmp_path, swaps=swaps, holders={"H": 100})
    (desk / "attention").mkdir()
    run(desk, cl)
    assert any(r[0] == "sell_burst" for r in rows(desk))                                          # without the wiring: a false burst
    desk2, cl2 = build(tmp_path / "wired", swaps=swaps, holders={"H": 100})
    (desk2 / "attention").mkdir()
    run(desk2, cl2, positions=[{**POS, "fill_sigs": ("sig0",)}])                                  # sig0 = the first swap's transaction
    assert not any(r[0] == "sell_burst" for r in rows(desk2))


def test_read_fill_sigs_from_a_real_shaped_fills_table(tmp_path):
    db = tmp_path / "ledger.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE memecoin_fills(id INTEGER PRIMARY KEY, position_id INTEGER, kind TEXT, price REAL, qty REAL, "
              "notional REAL, fill_source TEXT, fraction REAL, created_at TEXT, tx_sig TEXT, attempt_id TEXT)")
    c.executemany("INSERT INTO memecoin_fills(position_id, kind, tx_sig) VALUES(?,?,?)",
                  [(7, "entry", "ENTRYSIG"), (7, "exit", "EXITSIG"), (7, "derisk", None), (7, "partial_exit", ""), (8, "entry", "OTHER")])
    c.commit()
    c.close()
    assert sorted(W.read_fill_sigs(db, 7)) == ["ENTRYSIG", "EXITSIG"]
    assert W.read_fill_sigs(db, 99) == ()


def test_actors_of_signatures_finds_our_wallet_beyond_the_lookback(tmp_path):
    desk, _ = build(tmp_path, swaps=[(-5000, "ours", True, 20, 5), (-10, "x", False, 5, 1)])
    assert T.actors_of_signatures(desk, ["sig0"], NOW - 10000, NOW) == {"ours"}
    assert T.actors_of_signatures(desk, [], NOW - 10000, NOW) == set()


def test_wallet_learned_from_fills_drops_its_other_transactions_too(tmp_path):
    # ours also sells in a transaction that is NOT one of our recorded fills (sig2); learning the actor must drop it as well
    swaps = [(-9, "ours", True, 40, 5), (-8, "a", False, 40, 5), (-7, "ours", False, 40, 5), (-6, "b", False, 40, 5)]
    desk, cl = build(tmp_path, swaps=swaps, holders={"H": 100})
    (desk / "attention").mkdir()
    run(desk, cl)
    assert any(r[0] == "sell_burst" for r in rows(desk))                       # unwired: a, ours, b = 3 sellers
    desk2, cl2 = build(tmp_path / "w", swaps=swaps, holders={"H": 100})
    (desk2 / "attention").mkdir()
    run(desk2, cl2, positions=[{**POS, "fill_sigs": ("sig0",)}])
    assert not any(r[0] == "sell_burst" for r in rows(desk2))


def test_a_worker_error_on_a_position_writes_a_blind_row_not_silence(tmp_path, monkeypatch):
    desk, cl = build(tmp_path, swaps=CREATOR_SELL, holders={"H": 100})
    (desk / "attention").mkdir()
    real = T.creator_of

    def flaky(d, mint, now, **kw):
        if mint == "BOOM":
            raise RuntimeError("x")
        return real(d, mint, now, **kw)
    monkeypatch.setattr(T, "creator_of", flaky)
    s = run(desk, cl, positions=[{"id": 8, "mint": "BOOM", "opened_at": NOW - 400}])
    assert s["dropped"] == 1 and s["unknown"] == 1
    (sig, fired, _, body, _), = rows(desk, "WHERE position_id=8")
    assert (sig, fired) == ("tape", None) and json.loads(body)["reason"] == "worker_error:RuntimeError"


def test_a_gap_older_than_the_lookback_but_after_open_withholds_the_whole_life_signals(tmp_path):
    pos = {"id": 7, "mint": MINT, "opened_at": NOW - 400}
    desk, cl = build(tmp_path, swaps=CREATOR_SELL, holders={"H": 100}, gaps=[NOW - 300])         # after open, before the look-back
    (desk / "attention").mkdir()
    s = run(desk, cl, positions=[pos])
    assert s["fired"] == 0
    got = {(r[0], json.loads(r[3])["reason"]) for r in rows(desk)}
    assert got == {("creator_sell", "tape_gap_since_open"), ("holder_dump", "tape_gap_since_open")}
    d2, c2 = build(tmp_path / "before", swaps=CREATOR_SELL, holders={"H": 100}, gaps=[NOW - 450])   # before open: no effect
    (d2 / "attention").mkdir()
    assert run(d2, c2, positions=[pos])["unknown"] == 0


def test_dry_count_measures_holder_dump_from_the_entry_snapshot_and_counts_the_unmeasured(tmp_path):
    desk, cl = build(tmp_path, swaps=[(-100, "H", False, 60, 80)], holders={"H": 100})
    (desk / "candidate_emissions").mkdir()
    emission = {"mint": MINT, "snapshot": {"emission_at": NOW - 200.0}}                  # the snapshot (NOW-500) precedes it
    (desk / "candidate_emissions" / (DAY + ".jsonl")).write_text(json.dumps(emission) + "\n")
    r = W.dry_count(desk, [DAY], P, 100.0, life_s=300.0, step_s=10.0, cluster_db=cl)
    assert r["events"]["holder_dump"] == 1 and r["positions_without_entry_snapshot"] == 0
    r = W.dry_count(desk, [DAY], P, 100.0, life_s=300.0, step_s=10.0)                    # no cluster db: UNMEASURED, said so
    assert r["events"]["holder_dump"] == 0 and r["positions_without_entry_snapshot"] == 1


def test_only_armed_signals_fire_under_a_version(tmp_path):
    desk, cl = build(tmp_path, swaps=CREATOR_SELL, holders={"H": 100})
    (desk / "attention").mkdir()
    W.tick(desk_dir=desk, out_db=desk / "attention" / "tape.db", positions=[POS], cluster_db=cl, params=P, now=NOW,
           version="attn-v1", armed=frozenset({"sell_burst"}))
    assert [(r[1], r[2]) for r in rows(desk, "WHERE signal='creator_sell'")] == [(None, "attn-v1")]   # measured, not armed


def test_arming_knob_defaults_to_nothing_and_refuses_unknown_names():
    assert W.armed_signals({}) == frozenset()
    assert W.armed_signals({"MEMECOIN_DESK_ATTN_ARMED": " creator_sell, sell_burst "}) == {"creator_sell", "sell_burst"}
    with pytest.raises(ValueError):
        W.armed_signals({"MEMECOIN_DESK_ATTN_ARMED": "creator_sel"})
