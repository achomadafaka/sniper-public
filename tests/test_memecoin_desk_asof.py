"""As-of-decision refresh from the local U tape: one snapshot, zero calls, never a silent emission fallback."""
import copy
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_asof as A
from sniper.services import memecoin_desk_contract as contract
from sniper.services.memecoin_desk_universe import Field, b58encode
from test_memecoin_desk_contract import MINT, NOW, SIG, candidate

VQ_E, VT_E = 40_000_000_000, 700_000_000_000_000        # 10 SOL real quote at the emission trigger swap
SOL = 100.0
DDL = ("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, is_buy INTEGER, "
       "quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, "
       "block_time INTEGER, received_at REAL, real_token INTEGER, fee_bps INTEGER, creator_fee_bps INTEGER, tx_index INTEGER, "
       "PRIMARY KEY(signature, ordinal)) WITHOUT ROWID")


class Tape:
    def __init__(self, tmp_path, now):
        self.dir, self.now = tmp_path, now
        for sub in ("swaps", "gaps"):
            (tmp_path / sub).mkdir(exist_ok=True)
        self.day = "2026-10-21"
        self.sw = sqlite3.connect(tmp_path / "swaps" / f"{self.day}.db")
        self.sw.execute(DDL)
        self.gp = sqlite3.connect(tmp_path / "gaps" / f"{self.day}.db")
        self.gp.execute("CREATE TABLE gaps(at REAL, cause TEXT, last_slot INTEGER)")
        self.beat(ok=True)

    def swap(self, sig, ordinal, slot, recv, vq, vt, buy=1, quote=1_000_000_000, actor="a", real_token=500_000_000_000_000):
        self.sw.execute("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (slot, sig, ordinal, MINT, "c", buy, quote, 1000, actor, vq, vt, None, recv, real_token, 100, 30, None))
        self.sw.commit()

    def gap(self, at, cause="stream:Error"):
        self.gp.execute("INSERT INTO gaps VALUES(?,?,?)", (at, cause, None))
        self.gp.commit()

    def beat(self, ok=True, age=1.0, sol=SOL, sol_age=10.0, connected=True):
        (self.dir / "universe.heartbeat").write_text(json.dumps(
            {"ts": self.now - age, "connected": connected, "sol_usd": sol, "sol_age_s": sol_age}))


def cand(**extra):
    base = {"liq_usd": Field(1000.0, "pumpfun_grpc:real_quote_reserves", NOW, 7, "complete"),
            "age_s": Field(100.0, "pumpfun_grpc:create_block_time", NOW, 3, "complete")}
    base.update(extra)
    return candidate(**base)


@pytest.fixture
def tape(tmp_path):
    t = Tape(tmp_path, NOW + 60)
    t.swap(SIG, 0, 7, NOW, VQ_E, VT_E)                               # the emission trigger swap
    t.swap(b58encode(bytes([9]) * 64), 0, 9, NOW + 20, 41_000_000_000, 680_000_000_000_000, quote=2_000_000_000, actor="b")
    t.swap(b58encode(bytes([8]) * 64), 0, 11, NOW + 40, 44_000_000_000, 640_000_000_000_000, buy=0, actor="c", real_token=450_000_000_000_000)
    return t


def go(tape, c=None, now=None):
    return A.refresh(c or cand(), desk_dir=tape.dir, now=tape.now if now is None else now)


def test_the_refreshed_candidate_is_one_valid_as_of_snapshot(tape):
    c = cand()
    before = copy.deepcopy(c)
    out, meta = go(tape, c)
    contract.validate_candidate(out)
    assert c == before                                                # the original is never edited
    assert out["ticket_id"] == c["ticket_id"] and out["trigger_signature"] == c["trigger_signature"]
    f, as_of = out["snapshot"]["fields"], out["snapshot"]["emission_at"]
    assert as_of == tape.now and out["emitted_at"].startswith("2026-10-21T12:01:00") and meta["as_of"] == as_of
    price_e, price_n = (40 / 7e8), (44 / 6.4e8)
    ratio = price_n / price_e
    assert f["mcap_usd"]["value"] == round(41000.0 * ratio, 2) and f["mcap_usd"]["source"].endswith(":curve_reserves")
    assert f["liq_usd"]["value"] == round(1000.0 * 14 / 10, 2)
    assert f["curve_pct"]["value"] == round((1 - 450_000_000_000_000 / A.INITIAL_REAL_TOKEN) * 100, 2)
    assert f["age_s"]["value"] == 160.0
    assert all(r["known_at"] <= as_of for r in out["snapshot"]["fields"].values() if r["known_at"] is not None)
    assert meta["emission_snapshot_hash"] == c["snapshot"]["hash"] != meta["snapshot_hash"] == out["snapshot"]["hash"]
    assert meta["lag_s"] == 60.0 and meta["last_swap"]["slot"] == 11 and meta["window_swaps"] == 2


def test_the_sheet_price_is_the_snapshot_price(tape):
    out, meta = go(tape)
    sheet = contract.render_judge_sheet(out)
    assert sheet["mcap_usd"] == out["snapshot"]["fields"]["mcap_usd"]["value"] != contract.render_judge_sheet(cand())["mcap_usd"]
    assert sheet["flow_60s"] == out["snapshot"]["fields"]["flow_60s"]["value"]


def test_flow_is_over_the_sixty_second_window_in_usd_at_the_live_sol_quote(tape):
    out, _ = go(tape)
    flow = out["snapshot"]["fields"]["flow_60s"]
    assert flow["completeness"] == "complete"
    assert flow["value"] == {"buy_n": 1, "sell_n": 1, "buy_usd": 200.0, "sell_usd": 100.0, "makers": 2, "largest_buy": 200.0}
    assert out["snapshot"]["fields"]["price_vs_60s_high_pct"]["value"] == 0.0


def test_no_swaps_in_the_window_on_a_healthy_tape_is_known_zero_flow_not_a_missing_price(tmp_path):
    t = Tape(tmp_path, NOW + 600)
    t.swap(SIG, 0, 7, NOW, VQ_E, VT_E)
    out, _ = go(t)
    f = out["snapshot"]["fields"]
    assert f["flow_60s"]["value"]["buy_n"] == 0 and f["flow_60s"]["completeness"] == "complete"
    assert f["mcap_usd"]["value"] == 41000.0                         # the last curve state is still exact
    assert f["price_vs_60s_low_pct"]["value"] is None and f["price_vs_60s_low_pct"]["completeness"] == "partial"


@pytest.mark.parametrize("kw,reason", [({"age": 6.0}, "tape_stale"), ({"connected": False}, "tape_stale"),
                                       ({"sol_age": 500.0}, "sol_usd_stale"), ({"sol": 0}, "sol_usd_stale")])
def test_a_stale_heartbeat_or_quote_refuses(tape, kw, reason):
    tape.beat(**kw)
    with pytest.raises(A.StaleSnapshot) as e:
        go(tape)
    assert e.value.reason == reason


def test_unreadable_heartbeat_refuses(tape):
    (tape.dir / "universe.heartbeat").write_text("{not json")
    with pytest.raises(A.StaleSnapshot, match="tape_heartbeat_unreadable"):
        go(tape)


def test_a_missing_emission_state_refuses_instead_of_guessing(tmp_path):
    t = Tape(tmp_path, NOW + 60)
    t.swap(b58encode(bytes([9]) * 64), 0, 9, NOW + 20, 41_000_000_000, 680_000_000_000_000)
    with pytest.raises(A.StaleSnapshot, match="no_emission_state"):
        go(t)


def test_a_gap_after_the_last_swap_refuses_but_one_before_it_only_marks_flow_partial(tape):
    tape.gap(NOW + 50)                                                # after the last swap (NOW + 40)
    with pytest.raises(A.StaleSnapshot, match="tape_gap_since_last_swap"):
        go(tape)
    tape.gp.execute("DELETE FROM gaps")
    tape.gap(NOW + 30)                                                # inside the window, before the last swap
    out, _ = go(tape)
    assert out["snapshot"]["fields"]["flow_60s"]["completeness"] == "partial"
    assert out["snapshot"]["fields"]["mcap_usd"]["completeness"] == "complete"


def test_a_completed_curve_refuses(tape):
    tape.swap(b58encode(bytes([7]) * 64), 0, 12, NOW + 45, 80_000_000_000, 300_000_000_000_000, real_token=0)
    with pytest.raises(A.StaleSnapshot, match="curve_complete"):
        go(tape)


def test_swaps_received_after_the_as_of_time_are_ignored(tape):
    tape.swap(b58encode(bytes([6]) * 64), 0, 13, tape.now + 5, 90_000_000_000, 100_000_000_000_000)
    out, meta = go(tape)
    assert meta["last_swap"]["slot"] == 11


def test_in_one_slot_the_later_received_swap_is_the_tip(tape):
    tape.swap(b58encode(bytes([5]) * 64), 0, 11, NOW + 41, 45_000_000_000, 630_000_000_000_000)
    _, meta = go(tape)
    assert meta["last_swap"]["slot"] == 11 and meta["last_swap"]["received_at"] == NOW + 41


def test_a_liq_that_disagrees_with_the_live_units_goes_missing_never_wrong(tmp_path):
    t = Tape(tmp_path, NOW + 60)
    t.swap(SIG, 0, 7, NOW, VQ_E, VT_E)
    t.swap(b58encode(bytes([9]) * 64), 0, 9, NOW + 20, 41_000_000_000, 680_000_000_000_000)
    out, meta = go(t, cand(liq_usd=Field(5000.0, "pumpfun_grpc:real_quote_reserves", NOW, 7, "complete")))
    assert out["snapshot"]["fields"]["liq_usd"]["value"] is None and out["snapshot"]["fields"]["liq_usd"]["completeness"] == "missing"
    assert "liq_usd" in out["snapshot"]["missing"] and abs(meta["liq_check"]) > A.LIQ_CHECK_TOL


def test_a_clock_before_the_emission_refuses(tape):
    with pytest.raises(A.StaleSnapshot, match="clock_before_emission"):
        go(tape, now=NOW - 5)


def test_carried_fields_keep_their_own_stamps(tape):
    c = cand(crawler_intent=Field("analysis", "crawler:cache", NOW - 30, None, "complete"))
    out, _ = go(tape, c)
    assert out["snapshot"]["fields"]["crawler_intent"]["known_at"] == NOW - 30
