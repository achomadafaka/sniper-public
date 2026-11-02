"""Backfill swap storage: decimal TEXT amounts, global dedupe across day files (spec section 3 and 7)."""
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_desk_backfill_store as S


def row(**k):
    base = dict(network_id="solana", venue="pumpswap", pool="P", mint="M", signature="sig1", instruction_index=2, inner_index=0, event_ordinal=0, slot=10, tx_index=3,
                block_time=int(datetime(2026, 10, 26, 12, tzinfo=timezone.utc).timestamp()), is_buy=1, actor="a", quote_mint="So11", quote_atomic=5, token_atomic=7,
                pre_base=1, pre_quote=2, post_base=3, post_quote=4, fee_lp=0, fee_protocol=0, fee_creator=0, fee_bps=25, fetched_at=1.0)
    base.update(k)
    return base


def test_amounts_above_2_53_round_trip_exactly(tmp_path):
    big = 2 ** 53 + 1
    st = S.Store(tmp_path)
    assert st.insert_swap(row(quote_atomic=big, post_base=str(2 ** 70)))
    c = sqlite3.connect(tmp_path / "2026-10-26.db")
    q, pb, src = c.execute("select quote_atomic, post_base, source from swaps_hist").fetchone()
    assert (q, pb, src) == (str(big), str(2 ** 70), "helius_history") and int(q) == big


@pytest.mark.parametrize("bad", [1.5, True, "1e9", "12.5", "0x10", "abc"])
def test_a_float_or_non_decimal_amount_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        S.Store(tmp_path).insert_swap(row(quote_atomic=bad))


def test_absent_stays_null_not_zero(tmp_path):
    S.Store(tmp_path).insert_swap(row(fee_creator=None))
    assert sqlite3.connect(tmp_path / "2026-10-26.db").execute("select fee_creator from swaps_hist").fetchone()[0] is None


def test_the_same_event_on_another_day_is_a_typed_conflict_not_a_second_row(tmp_path):
    st = S.Store(tmp_path)
    assert st.insert_swap(row()) is True
    assert st.insert_swap(row(fetched_at=2.0)) is False                             # same payload (fetched_at is not identity): already stored
    other_day = int(datetime(2026, 10, 27, 1, tzinfo=timezone.utc).timestamp())
    with pytest.raises(S.StoreConflict):
        st.insert_swap(row(block_time=other_day))                                   # same identity, another day: conflict, nothing written
    with pytest.raises(S.StoreConflict):
        S.Store(tmp_path).insert_swap(row(quote_atomic=6))                          # same identity, another payload, fresh process
    assert not (tmp_path / "2026-10-27.db").exists()


def test_two_events_of_one_instruction_are_both_stored(tmp_path):
    st = S.Store(tmp_path)
    assert st.insert_swap(row(event_ordinal=0)) and st.insert_swap(row(event_ordinal=1))
    assert sqlite3.connect(tmp_path / "2026-10-26.db").execute("select count(*) from swaps_hist").fetchone()[0] == 2


def test_a_missing_key_field_is_refused(tmp_path):
    r = row()
    del r["inner_index"]
    with pytest.raises(ValueError):
        S.Store(tmp_path).insert_swap(r)


@pytest.mark.parametrize("field,bad", [("instruction_index", 2.9), ("instruction_index", -1), ("instruction_index", True), ("inner_index", True), ("inner_index", -2), ("inner_index", 1.5),
                                       ("event_ordinal", -1), ("event_ordinal", 0.5), ("slot", 1.0), ("tx_index", -1), ("block_time", 1.7e9)])
def test_locations_are_exact_non_bool_integers(tmp_path, field, bad):
    with pytest.raises(ValueError):
        S.Store(tmp_path).insert_swap(row(**{field: bad}))
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("2026-") and p.suffix == ".db"]


def test_a_top_level_instruction_uses_the_documented_sentinel(tmp_path):
    assert S.TOP_LEVEL == -1
    assert S.Store(tmp_path).insert_swap(row(inner_index=S.TOP_LEVEL))


@pytest.mark.parametrize("field", ["quote_atomic", "token_atomic", "pre_base", "pre_quote", "post_base", "post_quote", "fee_lp", "fee_protocol", "fee_creator"])
@pytest.mark.parametrize("bad", [-1, "-5", "007", "+3"])
def test_negative_or_non_canonical_amounts_are_refused(tmp_path, field, bad):
    with pytest.raises(ValueError):
        S.Store(tmp_path).insert_swap(row(**{field: bad}))


def test_a_crash_before_the_day_commit_is_recovered_by_a_replay(tmp_path, monkeypatch):
    st = S.Store(tmp_path)

    def boom(day, r):
        raise OSError("disk gone")
    monkeypatch.setattr(st, "_write_day", boom)
    with pytest.raises(OSError):
        st.insert_swap(row())
    assert len(st.pending()) == 1 and not [p for p in tmp_path.iterdir() if p.name.startswith("2026-") and p.suffix == ".db"]          # claim durable, day row absent: NOT complete
    st2 = S.Store(tmp_path)                                                         # restart
    assert len(st2.pending()) == 1
    assert st2.insert_swap(row()) is True and st2.pending() == []
    assert sqlite3.connect(tmp_path / "2026-10-26.db").execute("select count(*) from swaps_hist").fetchone()[0] == 1


def test_a_crash_after_the_day_commit_before_done_is_recovered_without_a_second_row(tmp_path, monkeypatch):
    st = S.Store(tmp_path)
    monkeypatch.setattr(st, "_mark_done", lambda key: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(OSError):
        st.insert_swap(row())
    assert len(st.pending()) == 1 and (tmp_path / "2026-10-26.db").exists()
    st2 = S.Store(tmp_path)
    assert st2.insert_swap(row()) is False and st2.pending() == []                  # the row was already there: no duplicate, claim finalized
    assert sqlite3.connect(tmp_path / "2026-10-26.db").execute("select count(*) from swaps_hist").fetchone()[0] == 1


def test_replaying_a_pending_claim_on_another_day_after_a_crash_is_a_conflict(tmp_path, monkeypatch):
    st = S.Store(tmp_path)
    monkeypatch.setattr(st, "_mark_done", lambda key: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(OSError):
        st.insert_swap(row())
    with pytest.raises(S.StoreConflict):
        S.Store(tmp_path).insert_swap(row(block_time=int(datetime(2026, 10, 27, 1, tzinfo=timezone.utc).timestamp())))
    assert not (tmp_path / "2026-10-27.db").exists()
