"""Offline acceptance witnesses against immutable lessons review pin 52fe5ed97."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_desk_lessons import L, DG, W, T0, COST, swap, unit, facts, row, _fixture


def test_a_single_later_swap_does_not_prove_the_120_second_horizon():
    rows = [swap(T0 - 1, 3e-8), swap(T0, 3e-8), swap(T0 + 1, 3e-8)]
    f, _ = facts(rows)
    result = f["alternatives"]["fixed_120s"]
    assert result["R"] is None and result["censored"], result


def test_a_late_pool_print_is_not_a_30_second_value():
    rows = [swap(T0 - 1, 3e-8), swap(T0, 3e-8),
            swap(T0 + 5, 3e-8, real_quote=85_000_000_000)]
    pool = [{"t": T0 + 800, "q": 100_000_000_000, "b": 1_000_000_000_000, "fee": 0.0}]
    tape = L.Tape(rows, pool)
    qty = tape.buy_tokens(1, COST)
    assert tape.value_at(T0 + 30, qty) is None


def test_fixed_exit_does_not_sell_at_migration_twenty_five_seconds_early():
    rows = [swap(T0 - 1, 3e-8), swap(T0, 3e-8),
            swap(T0 + 5, 3e-8, real_quote=85_000_000_000)]
    pool = [{"t": T0 + t, "q": int(q * 1e9), "b": 1_000_000_000_000, "fee": 0.0}
            for t, q in ((6, 30), (31, 60), (61, 90), (901, 120))]
    f, _ = facts(rows, pool=pool)
    result = f["alternatives"]["fixed_30s"]
    assert result["reason"] == "time" and result["after_s"] >= 30, result


def test_missing_fees_do_not_become_a_zero_fee_quote():
    r = swap(T0, 3e-8)
    r.update(fee_bps=None, creator_fee_bps=None)
    tape = L.Tape([r])
    assert tape.v(0, 1_000_000) is None


MINT = "7WF3wosXD5FtRN65NvsG2AvAkmhAG773ddYiY9cspump"


def _pool_fixture(root, *, buyback="0", virtual_quote="17500000000"):
    """One legacy buy with a proven virtual quote on the coin's canonical pool, in the PS tape's own schema."""
    from sniper.services import memecoin_desk_ps_tape as PT
    p = PT.tape_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE swaps_ps(pool TEXT, mint TEXT, quote_mint TEXT, received_at REAL, slot INTEGER, ordinal INTEGER, tx_index INTEGER, "
              "is_buy INTEGER, pre_base_reserve_atomic TEXT, pre_quote_reserve_atomic TEXT, post_base_reserve_atomic TEXT, "
              "post_quote_reserve_atomic TEXT, lp_fee_bps TEXT, protocol_fee_bps TEXT, creator_fee_bps TEXT, cashback_fee_bps TEXT, "
              "buyback_fee_bps TEXT, payload_json TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS swaps_ps_received ON swaps_ps(received_at)")
    c.execute("CREATE INDEX IF NOT EXISTS swaps_ps_pool_slot ON swaps_ps(pool,slot)")
    c.execute("CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER)")
    proof = {"ix_name": "buy", "virtual_quote_atomic": virtual_quote, "virtual_quote_reserves": virtual_quote,
             "virtual_quote_source": "pumpswap_grpc:buy_event_virtual_quote"}
    c.execute("INSERT INTO swaps_ps VALUES(?,?,NULL,?,1,0,0,1,'1000000000000','10000000000','1000000000000','10000000000','95','5','25','0',?,?)",
              (PT.canonical_pool(MINT), MINT, T0, buyback, json.dumps(proof)))
    c.commit()
    c.close()
    return L.load_pool(root, MINT, T0 - 1, T0 + 1)[0]


def test_buyback_protocol_share_is_not_an_additional_fee(tmp_path):
    state = _pool_fixture(tmp_path, buyback="5000")
    assert state["fee"] == pytest.approx((95 + 5 + 25) / 10000)


def test_proven_virtual_quote_is_included_in_the_pool_state(tmp_path):
    state = _pool_fixture(tmp_path)
    assert state["q"] == 10_000_000_000 + 17_500_000_000


def test_censored_selection_outcomes_do_not_become_failures():
    rows = [row("skip_counterfactual", "known", [], best=0.3)]
    rows += [row("skip_counterfactual", "missing%d" % i, [], best=None) for i in range(19)]
    result = DG.digest(rows)["selection"]["sampled_skips"]
    assert result["share_best_ge_20pct"] == 1.0, result


def test_twenty_tags_with_zero_evaluable_pairs_are_not_candidate_evidence():
    rows = [row("realized", "m%d" % i, ["migration_step"], pnl=-1, R=-0.04,
                alts={"sell_before_migration": None}) for i in range(20)]
    proposal = DG.digest(rows)["mistakes"][0]
    assert proposal["evidence"]["n"] == 0
    assert proposal["status"] != "candidate", proposal


def test_the_current_version_is_not_selected_by_lexical_order(tmp_path):
    p = tmp_path / "lessons.db"
    db = L.connect(p)
    for ver in ("lessons-v2", "lessons-v10"):
        db.execute("INSERT INTO lessons VALUES('pos:1',?,'realized','M',1,'t',1,1,?,?)",
                   (ver, json.dumps({"source_version": ver}), "[]"))
    db.commit()
    db.close()
    assert DG.load(p)[0]["feature_version"] == "lessons-v10"


def test_the_documented_cron_uses_the_shared_state_root(tmp_path, monkeypatch):
    state, release = tmp_path / "state", tmp_path / "release"
    state.mkdir()
    release.mkdir()
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(state))
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("MEMECOIN_DESK_LESSONS_ENABLED", raising=False)
    monkeypatch.chdir(release)
    assert W.main(["--max", "0"]) == 0
    assert (state / "data/desk/lessons.db").is_file()
    assert not (release / "data/desk/lessons.db").exists()


def _run_fixture(root, desk, ledger, deskdb, monkeypatch):
    monkeypatch.setattr(W, "time", SimpleNamespace(time=lambda: T0 + 6000))
    monkeypatch.setenv("MEMECOIN_DESK_LESSONS_SKIP_SAMPLE_PCT", "0")
    monkeypatch.delenv("MEMECOIN_DESK_LESSONS_ENABLED", raising=False)
    out = root / "lessons.db"
    W.main(["--ledger", ledger, "--desk-dir", str(desk), "--desk-db", deskdb, "--out", str(out)])
    return out


def test_a_temporary_tape_outage_does_not_freeze_missing_evidence(tmp_path, monkeypatch):
    desk, ledger, deskdb = _fixture(tmp_path)
    tape_path = desk / "swaps" / (L.utc_day(T0) + ".db")
    unavailable = tape_path.with_suffix(".temporarily-unavailable")
    tape_path.rename(unavailable)
    out = _run_fixture(tmp_path, desk, ledger, deskdb, monkeypatch)
    unavailable.rename(tape_path)
    _run_fixture(tmp_path, desk, ledger, deskdb, monkeypatch)
    c = sqlite3.connect(out)
    try:
        value = json.loads(c.execute("SELECT facts_json FROM lessons WHERE key='pos:1'").fetchone()[0])
    finally:
        c.close()
    assert "entry_swap_missing" not in value["censored"], value


def test_an_actual_open_position_is_not_a_never_attempted_counterfactual(tmp_path, monkeypatch):
    desk, ledger, deskdb = _fixture(tmp_path)
    c = sqlite3.connect(deskdb)
    c.execute("DELETE FROM desk_tickets WHERE ticket_id='desk:U:b1' AND stage='fill'")
    c.commit()
    c.close()
    c = sqlite3.connect(ledger)
    c.execute("INSERT INTO memecoin_positions VALUES(2,'MB',?,NULL,NULL,NULL,25,'desk',1,'desk:U:b1')",
              (W.datetime.fromtimestamp(T0 + 2002, __import__('datetime').timezone.utc).isoformat(),))
    c.commit()
    c.close()
    out = _run_fixture(tmp_path, desk, ledger, deskdb, monkeypatch)
    c = sqlite3.connect(out)
    try:
        mistaken = c.execute("SELECT kind FROM lessons WHERE key='tkt:desk:U:b1'").fetchone()
    finally:
        c.close()
    assert mistaken is None, mistaken
