"""base_fast_landing.py corrections from the 2026-10-08 engineering review (EVM03, EVM04, EVM05).

EVM03: an eth_getLogs answer that is not a list (null, missing, false, "", {}) became `[]`, a clean
row of zero competing swaps, and `censored IS NULL` then marked it done forever. Every answer is
now shape-checked; a malformed one censors the row (retried next pass), and a valid `[]` stays an
honest zero.
"""
import io
import json
from datetime import datetime, timezone

import pytest

from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
from test_memecoin_base_fast_decide import L, _Gate, _would_send, base


def _flat(rec):
    s, post, liq = rec["block"], int(rec["post_sqrt"]), int(rec["liquidity"])
    return {b: [(post, 0, 0, liq)] for b in (s, s + 1, s + 2)}


def _log(rec, block, index, **over):
    log = {"address": V4_POOL_MANAGERS["8453"], "topics": [V4_SWAP, rec["pool"]],
           "blockNumber": hex(block), "logIndex": hex(index), "removed": False}
    log.update(over)
    return log


def _no_measures(row):
    return not any(k.startswith(("clears_", "other_", "a_mark")) for k in row)


# --- EVM03: logs ------------------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [None, False, "", {}, "0x"])
def test_a_logs_answer_that_is_not_a_list_censors_the_row(answer):
    rec = _would_send()
    gate = _Gate(_flat(rec))
    gate.logs = answer
    row = L.grade(rec, gate)
    assert row["censored"] == "logs_shape" and _no_measures(row), row


def test_a_valid_empty_log_list_is_an_honest_zero():
    rec = _would_send()
    row = L.grade(rec, _Gate(_flat(rec), logs=[]))
    assert row.get("censored") is None
    assert (row["other_swaps_s"], row["other_swaps_s1"], row["other_swaps_s2"]) == (0, 0, 0)
    assert json.loads(row["detail_json"])["logs_checked"] is True


@pytest.mark.parametrize("bad", [
    {"logIndex": None}, {"blockNumber": "zz"}, {"removed": True},
    {"address": "0x" + "99" * 20}, {"topics": [V4_SWAP, "0x" + "77" * 32]}, {"topics": ["0x" + "00" * 32]},
])
def test_a_malformed_or_foreign_log_censors_the_row(bad):
    rec = _would_send()
    s = rec["block"]
    row = L.grade(rec, _Gate(_flat(rec), logs=[_log(rec, s + 1, 1), _log(rec, s + 1, 2, **bad)]))
    assert row["censored"] == "logs_shape" and _no_measures(row), row


def test_a_log_outside_s_to_s2_censors_the_row():
    rec = _would_send()
    row = L.grade(rec, _Gate(_flat(rec), logs=[_log(rec, rec["block"] + 3, 1)]))
    assert row["censored"] == "logs_shape"


def test_valid_logs_still_count_by_block():
    rec = _would_send()
    s = rec["block"]
    logs = [_log(rec, s, rec["swap_log_index"]), _log(rec, s + 1, 1), _log(rec, s + 1, 9), _log(rec, s + 2, 3)]
    row = L.grade(rec, _Gate(_flat(rec), logs=logs))
    assert (row["other_swaps_s"], row["other_swaps_s1"], row["other_swaps_s2"]) == (0, 2, 1)


@pytest.mark.parametrize("raw", [{"x": 1}, "0xnothex" + "0" * 120, 7])
def test_a_malformed_state_answer_censors_instead_of_crashing(raw):
    rec = _would_send()

    def call(method, params):
        return raw if method == "eth_call" else []
    row = L.grade(rec, call)
    assert row["censored"] == "extsload_shape" and _no_measures(row), row


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("body,why", [
    ({"jsonrpc": "2.0", "id": 1}, "rpc_shape"),          # no result field at all
    ([{"result": []}], "rpc_shape"),                     # not an object
    ("null", "rpc_shape"),
])
def test_the_gateway_censors_a_response_without_a_result(body, why):
    raw = body if isinstance(body, str) else json.dumps(body)
    gw = L.Gateway(gap_s=0, sleep=lambda s: None, opener=lambda req, timeout: _Resp(raw.encode()))
    with pytest.raises(L.Censored) as got:
        gw("eth_getLogs", [{}])
    assert str(got.value) == why


def test_the_gateway_keeps_a_null_result_for_the_caller_to_judge():
    gw = L.Gateway(gap_s=0, sleep=lambda s: None,
                   opener=lambda req, timeout: _Resp(b'{"jsonrpc":"2.0","id":1,"result":null}'))
    assert gw("eth_getLogs", [{}]) is None


def test_a_censored_row_is_retried_and_replaced_once_the_logs_answer(tmp_path):
    w = base.DecisionWriter(tmp_path / "d.db")
    rec = _would_send()
    w.put(rec)
    w.flush()
    w.close()
    now = datetime(2026, 10, 2, 0, 1, 0, tzinfo=timezone.utc)
    bad = _Gate(_flat(rec))
    bad.logs = None
    first = L.run(tmp_path / "d.db", tmp_path / "l.db", call=bad, now=now)
    assert (first["graded"], first["censored"]) == (1, 1)
    good = _Gate(_flat(rec), logs=[])
    assert L.run(tmp_path / "d.db", tmp_path / "l.db", call=good, now=now)["censored"] == 0
    assert L.run(tmp_path / "d.db", tmp_path / "l.db", call=good, now=now)["graded"] == 0


# --- EVM05: end of S+1 is not a bound ---------------------------------------------------------------
# The review's exact counterexample in the script's own one-range model. X is currency1, the coin is
# currency0, fee 0. A later swap can move the end-of-block state either way from the state our
# insertion meets, so the end-of-S+1 clear bit errs in both directions. These pin the property; the
# correction itself is the relabel in the script and the spec.

Q = 2 ** 96
POOL = dict(x_is_c0=False, clip_x=10 ** 12, min_out=5 * 10 ** 11, lp_fee_pips=0)


def _clear(sqrt):
    return L.clears((sqrt, 0, 0, 10 ** 20), **POOL)


def test_a_later_sell_makes_the_end_of_block_clear_where_insertion_fails():
    insertion, end_of_s1 = 2 * Q, Q        # buys before our slot, a reversing sell after it
    assert (_clear(insertion), _clear(end_of_s1)) == (0, 1)


def test_a_later_buy_makes_the_end_of_block_fail_where_insertion_clears():
    insertion, end_of_s1 = Q, 2 * Q
    assert (_clear(insertion), _clear(end_of_s1)) == (1, 0)


def test_every_row_names_its_s1_estimand():
    rec = _would_send()
    row = L.grade(rec, _Gate(_flat(rec), logs=[]))
    assert json.loads(row["detail_json"])["s1_estimand"] == "end_of_block_modeled_min_out_survival"


# --- EVM04: a landing DB made by the previous version takes the new columns ------------------------

OLD_DDL = L.DDL.replace("path TEXT, since_arrival_ms REAL, ", "")


def _decisions(tmp_path, *recs):
    w = base.DecisionWriter(tmp_path / "d.db")
    for rec in recs:
        w.put(rec)
    w.flush()
    w.close()
    return tmp_path / "d.db"


def _old_db(tmp_path, ddl=OLD_DDL):
    import sqlite3
    db = sqlite3.connect(tmp_path / "l.db")
    db.executescript(ddl)
    db.execute("INSERT INTO base_fast_landing (tx_hash, mint, clears_s1, graded_at) VALUES "
               "('0xold', '0xcoin', 1, '2026-10-07T21:00:00+00:00')")
    db.commit()
    db.close()
    return tmp_path / "l.db"


def _cols(path):
    import sqlite3
    return [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(base_fast_landing)")]


def _rows(path):
    import sqlite3
    return sqlite3.connect(path).execute(
        "SELECT tx_hash, clears_s1, path, since_arrival_ms FROM base_fast_landing ORDER BY tx_hash").fetchall()


NOW = datetime(2026, 10, 2, 0, 1, 0, tzinfo=timezone.utc)


def test_a_previous_version_db_is_migrated_and_keeps_its_rows(tmp_path):
    rec = dict(_would_send(), path="relay_verified", since_arrival_ms=2500.0)
    db = _old_db(tmp_path)
    out = L.run(_decisions(tmp_path, rec), db, call=_Gate(_flat(rec), logs=[]), now=NOW)
    assert out["graded"] == 1
    assert _cols(db)[-2:] == ["path", "since_arrival_ms"]
    rows = dict((r[0], r[1:]) for r in _rows(db))
    assert rows["0xold"] == (1, None, None)            # the old row's path stays unknown
    assert rows[rec["tx_hash"]][1:] == ("relay_verified", 2500.0)


def test_a_partly_migrated_db_gets_the_missing_column_once(tmp_path):
    rec = _would_send()
    db = _old_db(tmp_path, ddl=L.DDL.replace("since_arrival_ms REAL, ", ""))
    decisions = _decisions(tmp_path, rec)
    L.run(decisions, db, call=_Gate(_flat(rec), logs=[]), now=NOW)
    L.run(decisions, db, call=_Gate(_flat(rec), logs=[]), now=NOW)     # a second pass: no duplicate
    assert _cols(db).count("since_arrival_ms") == 1 and _cols(db).count("path") == 1


def test_a_fresh_db_and_a_migrated_db_end_with_the_same_columns(tmp_path):
    fresh, old = tmp_path / "fresh", tmp_path / "old"
    fresh.mkdir()
    old.mkdir()
    rec = _would_send()
    L.run(_decisions(fresh, rec), fresh / "l.db", call=_Gate(_flat(rec), logs=[]), now=NOW)
    L.run(_decisions(old, rec), _old_db(old), call=_Gate(_flat(rec), logs=[]), now=NOW)
    assert _cols(fresh / "l.db") == _cols(old / "l.db")


def test_repeated_passes_never_rewrite_old_evidence(tmp_path):
    rec = _would_send()
    db = _old_db(tmp_path)
    decisions = _decisions(tmp_path, rec)
    for _ in range(3):                                   # the --loop pass, three times
        L.run(decisions, db, call=_Gate(_flat(rec), logs=[]), now=NOW)
    assert dict((r[0], r[1:]) for r in _rows(db))["0xold"] == (1, None, None)
