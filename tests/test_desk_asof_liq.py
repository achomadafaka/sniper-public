"""As-of liq from the latest swap's own real quote reserves (real data: non-classic curves have a virtual quote of ~190-340 SOL,
so 'virtual - 30 SOL' is NOT the real quote). Numbers are shaped like the live rows Gamma's probe printed."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_asof as A
from sniper.services import memecoin_desk as D
from sniper.services.memecoin_desk_universe import Field, b58encode
from test_memecoin_desk_asof import MINT, NOW, SIG, Tape, cand

SOL = 100.0
DDL_RQ = ("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, is_buy INTEGER, "
          "quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, "
          "block_time INTEGER, received_at REAL, real_token INTEGER, fee_bps INTEGER, creator_fee_bps INTEGER, tx_index INTEGER, "
          "real_quote INTEGER, PRIMARY KEY(signature, ordinal)) WITHOUT ROWID")


class RqTape(Tape):
    def __init__(self, tmp_path, now):
        super().__init__(tmp_path, now)
        self.sw.execute("DROP TABLE swaps")
        self.sw.execute(DDL_RQ)

    def swap(self, sig, ordinal, slot, recv, vq, vt, rq=None, buy=1, quote=1_000_000_000, actor="a", real_token=730_917_816_778_599):
        self.sw.execute("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (slot, sig, ordinal, MINT, "c", buy, quote, 1000, actor, vq, vt, None, recv, real_token, 100, 30, None, rq))
        self.sw.commit()


def go(tape, c=None):
    return A.refresh(c or cand(liq_usd=Field(538.52, "pumpfun_grpc:real_quote_reserves", NOW, 7, "complete")),
                     desk_dir=tape.dir, now=tape.now)


NONCLASSIC_E = (303_360_968_692, 1_010_817_816_778_599)     # vq 303 SOL, real quote 5.158 SOL: initial virtual ~298 SOL
RQ_E = 5_158_000_000


def tape_for(tmp_path, *, rq_e=RQ_E, vq_n=313_360_968_692, rq_n=15_158_000_000):
    t = RqTape(tmp_path, NOW + 60)
    t.swap(SIG, 0, 7, NOW, *NONCLASSIC_E, rq=rq_e)
    t.swap(b58encode(bytes([9]) * 64), 0, 9, NOW + 40, vq_n, 990_000_000_000_000, rq=rq_n)
    return t


def test_nonclassic_curve_liq_is_the_latest_swaps_real_quote_at_live_sol(tmp_path):
    out, meta = go(tape_for(tmp_path))
    f = out["snapshot"]["fields"]["liq_usd"]
    assert f["value"] == round(15.158 * SOL, 2) and f["completeness"] == "complete" and meta["liq_basis"] == "real_quote"
    assert f["source"].endswith(":real_quote_reserves")


def test_the_old_ratio_would_have_refused_this_coin(tmp_path):
    t = tape_for(tmp_path)
    t.sw.execute("UPDATE swaps SET real_quote=NULL")                      # the same rows without the column's data
    t.sw.commit()
    out, meta = go(t)
    assert out["snapshot"]["fields"]["liq_usd"]["value"] is None and meta["liq_basis"] == "ratio_legacy"    # guard refuses: never fabricated


def test_collapsed_coin_below_the_classic_floor_is_priced_directly(tmp_path):
    out, meta = go(tape_for(tmp_path, vq_n=299_402_968_692, rq_n=1_200_000_000))     # drained to 1.2 SOL real: the coin's initial 298.2 SOL is unchanged
    assert out["snapshot"]["fields"]["liq_usd"]["value"] == round(1.2 * SOL, 2)


def test_zero_real_quote_is_a_known_zero_not_missing(tmp_path):
    out, _ = go(tape_for(tmp_path, vq_n=298_202_968_692, rq_n=0))
    assert out["snapshot"]["fields"]["liq_usd"]["value"] == 0.0
    assert D.priced_verdict(out, clip_usd=25, basis="asof") is None


@pytest.mark.parametrize("rq_n", [-1, 400_000_000_000])                  # negative / larger than the virtual quote
def test_impossible_real_quote_is_missing(tmp_path, rq_n):
    out, _ = go(tape_for(tmp_path, rq_n=rq_n))
    assert out["snapshot"]["fields"]["liq_usd"]["value"] is None


def test_virtual_offset_changes_do_not_hide_direct_real_quote(tmp_path):
    out, meta = go(tape_for(tmp_path, vq_n=313_360_968_692, rq_n=11_000_000_000))    # 302.4 SOL initial vs 298.2 at the trigger
    assert out["snapshot"]["fields"]["liq_usd"]["value"] == 1100.0 and meta["liq_basis"] == "real_quote"
    assert meta["liq_check"] == pytest.approx((313_360_968_692 - 11_000_000_000 - (303_360_968_692 - RQ_E)) / (303_360_968_692 - RQ_E))


def test_a_trigger_row_without_real_quote_does_not_block_the_direct_liq(tmp_path):
    out, _ = go(tape_for(tmp_path, rq_e=None))                            # emitted before the column: tip alone is enough
    assert out["snapshot"]["fields"]["liq_usd"]["value"] == round(15.158 * SOL, 2)


def test_classic_curve_old_rows_still_price_through_the_guarded_legacy_ratio(tmp_path):
    t = RqTape(tmp_path, NOW + 60)
    t.swap(SIG, 0, 7, NOW, 40_000_000_000, 700_000_000_000_000, rq=None)
    t.swap(b58encode(bytes([9]) * 64), 0, 9, NOW + 40, 44_000_000_000, 640_000_000_000_000, rq=None)
    out, meta = A.refresh(cand(), desk_dir=t.dir, now=t.now)
    assert out["snapshot"]["fields"]["liq_usd"]["value"] == round(1000.0 * 14 / 10, 2) and meta["liq_basis"] == "ratio_legacy"


def test_day_file_without_the_column_reads_as_unknown_not_as_an_error(tmp_path):
    t = Tape(tmp_path, NOW + 60)                                          # the plain fixture DDL has no real_quote column
    t.swap(SIG, 0, 7, NOW, 40_000_000_000, 700_000_000_000_000)
    t.swap(b58encode(bytes([9]) * 64), 0, 9, NOW + 40, 44_000_000_000, 640_000_000_000_000)
    out, meta = A.refresh(cand(), desk_dir=t.dir, now=t.now)
    assert meta["liq_basis"] == "ratio_legacy" and out["snapshot"]["fields"]["liq_usd"]["value"] is not None


@pytest.mark.parametrize("rq_n", [-1, 400_000_000_000])
def test_impossible_real_quote_is_missing_even_without_a_trigger_real_quote(tmp_path, rq_n):
    out, _ = go(tape_for(tmp_path, rq_e=None, rq_n=rq_n))                  # no consistency partner: the range check alone must refuse
    assert out["snapshot"]["fields"]["liq_usd"]["value"] is None


def test_direct_liq_uses_the_live_sol_price_not_a_constant(tmp_path):
    t = tape_for(tmp_path)
    t.beat(sol=137.5)
    out, meta = go(t)
    assert out["snapshot"]["fields"]["liq_usd"]["value"] == round(15.158 * 137.5, 2)


FROZEN = json.loads((Path(__file__).parent / "fixtures/desk/no_verdict_price_2026-10-26.json").read_text())["records"]


@pytest.mark.parametrize("record", FROZEN, ids=lambda r: str(r["stage_id"]))
def test_frozen_mayhem_refusals_have_a_price_from_direct_reserves(tmp_path, record):
    meta = record["meta"]
    now = meta["as_of"]
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    for sub in ("swaps", "gaps"):
        (tmp_path / sub).mkdir()
    with closing(sqlite3.connect(tmp_path / "swaps" / f"{day}.db")) as conn:
        conn.execute(DDL_RQ)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(swaps)")]
        for row in (record["trigger"], record["tip"]):
            assert row["quote_mint"] == "11111111111111111111111111111111"
            assert row["mint"] == record["candidate"]["mint"] and row["curve"] == record["candidate"]["pool"]
            conn.execute("INSERT OR IGNORE INTO swaps VALUES(" + ",".join("?" * len(columns)) + ")",
                         tuple(row.get(k) for k in columns))
        conn.commit()
    with closing(sqlite3.connect(tmp_path / "gaps" / f"{day}.db")) as conn:
        conn.execute("CREATE TABLE gaps(at REAL, cause TEXT, last_slot INTEGER)")
        conn.commit()
    (tmp_path / "universe.heartbeat").write_text(json.dumps({
        "ts": now - meta["tape_heartbeat_age_s"], "connected": True,
        "sol_usd": meta["sol_usd"], "sol_age_s": meta["sol_age_s"]}))
    out, audit = A.refresh(record["candidate"], desk_dir=tmp_path, now=now, rank_inputs=True)
    assert out["snapshot"]["fields"]["liq_usd"]["value"] == record["expected_liq_usd"]
    assert audit["liq_basis"] == "real_quote"
    assert D.priced_verdict(out, clip_usd=25, basis="asof") is not None


def test_standard_non_mayhem_direct_curve_keeps_its_price(tmp_path):
    t = RqTape(tmp_path, NOW + 60)
    t.swap(SIG, 0, 7, NOW, 40_000_000_000, 700_000_000_000_000, rq=10_000_000_000)
    t.swap(b58encode(bytes([9]) * 64), 0, 9, NOW + 40, 44_000_000_000, 640_000_000_000_000, rq=14_000_000_000)
    out, meta = A.refresh(cand(), desk_dir=t.dir, now=t.now)
    price = D.priced_verdict(out, clip_usd=25, basis="asof")
    assert out["snapshot"]["fields"]["liq_usd"]["value"] == 1400.0 and meta["liq_check"] == 0
    assert price["ref_price_usd"] == 0.00004932812
    assert price["impact_pct"] == pytest.approx(1.7857142857142856)
