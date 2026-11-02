"""A malformed compiled token instruction is unproved flow, never a crash (2026-10-08 review SF03).

_token_moves guarded the key and data decoding, then indexed accounts[0..2] outside the guard: a
9-byte Transfer with accounts=[] raised IndexError out of flow_features, run_t1_once read it as a
transport failure and broke the batch, and every later row waited behind it until the 48 h expiry.
Now a token-program instruction that cannot be read whole (short account list for its opcode, an
account or program index out of range, undecodable data) makes its transaction UNPROVED, so the
windows holding it are censored (unavailable:unproved_operation), never counted as zero. And the
worker splits data errors (that row is written unavailable:parse_error:<type>, the batch goes on)
from transport errors (the row stays pending and the batch stops, as before).
"""
import copy
import json

import base58
import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_entry_capture as cap
from sniper.services import memecoin_entry_capture_sol as sol
from sniper.persistence import db_entry_features as store
from test_memecoin_entry_capture_t1_sol import (D, SOURCE, Rpc, _attempt, _buy, _db, _market,
                                                 ledger_fns)  # noqa: F401

COPIER = "a" * 88                         # inside every window around the decision


def _ix(tx):
    return tx["meta"]["innerInstructions"][0]["instructions"][0]


def _transfer_checked(atoms, dec=6, op=12):
    return base58.b58encode(bytes([op]) + int(atoms).to_bytes(8, "little") + bytes([dec])).decode()


def _fee(atoms, fee):
    return base58.b58encode(bytes([26, 1]) + int(atoms).to_bytes(8, "little") + bytes([6])
                            + int(fee).to_bytes(8, "little")).decode()


def _broken(kind):
    tx = copy.deepcopy(_buy(COPIER, D - 5, 112, 0, 20.0, 2.0))
    ix = _ix(tx)
    if kind == "transfer_no_accounts":
        ix["accounts"] = []
    elif kind == "transfer_short":
        ix["accounts"] = ix["accounts"][:1]
    elif kind == "checked_short":
        ix["data"], ix["accounts"] = _transfer_checked(20_000_000), ix["accounts"][:3]
    elif kind == "fee_short":
        ix["data"], ix["accounts"] = _fee(20_000_000, 0), ix["accounts"][:3]
    elif kind == "account_out_of_range":
        ix["accounts"] = [99, 100, 101]
    elif kind == "bad_base58":
        ix["data"] = "0OIl"
    elif kind == "program_out_of_range":
        ix["programIdIndex"] = 99
    return tx


def _copier_market(tx):
    return [t for t in _market() if t["transaction"]["signatures"][0] != COPIER] + [tx]


@pytest.mark.parametrize("kind", ["transfer_no_accounts", "transfer_short", "checked_short", "fee_short",
                                  "account_out_of_range", "bad_base58", "program_out_of_range"])
def test_a_malformed_transfer_censors_its_windows_and_never_raises(kind):
    out = sol.flow_features(_attempt(), Rpc(_copier_market(_broken(kind))), own_sigs=set())
    assert out["flow_30s"] == (None, "unavailable:unproved_operation", out["flow_30s"][2]), out["flow_30s"]
    assert out["copier_queue"][1] == "unavailable:unproved_operation"


def test_a_malformed_transfer_outside_a_window_leaves_that_window_measured():
    tx = _broken("transfer_no_accounts")
    tx["blockTime"], tx["slot"] = int(D - 400), 1             # before every window
    out = sol.flow_features(_attempt(), Rpc(_copier_market(tx)), own_sigs=set())
    assert out["flow_30s"][1] == "ok"


def test_a_valid_non_transfer_token_instruction_is_still_skipped():
    tx = copy.deepcopy(_buy(COPIER, D - 5, 112, 0, 20.0, 2.0))
    extra = dict(_ix(tx), data=base58.b58encode(bytes([9])).decode())     # CloseAccount: not a move
    tx["meta"]["innerInstructions"][0]["instructions"].append(extra)
    out = sol.flow_features(_attempt(), Rpc(_copier_market(tx)), own_sigs=set())
    assert out["flow_30s"][1] == "ok" and out["flow_30s"][0]["buy_n"] == 3


def test_a_malformed_source_transaction_is_unproved_not_a_crash():
    src = copy.deepcopy(SOURCE)
    _ix(src)["accounts"] = []
    market = [t for t in _market() if t is not SOURCE] + [src]
    out = sol.flow_features(_attempt(), Rpc(market, source=src), own_sigs=set())
    assert out["flow_30s"][1] == "unavailable:unproved_operation"


def _two_rows(tmp_path):
    db = _db(tmp_path)
    cov = {n: sol.PENDING for n in cap._flow_names()}
    store.insert(db, {"attempt_id": "att2", "network_id": "solana", "mint": "Mint2", "tier": 1,
                      "decision_at": cap._iso(D + 1), "feature_version": 1, "features_json": "{}",
                      "coverage_json": json.dumps(cov), "sources_json": "{}", "read_lag_json": "{}",
                      "computed_at": "x"})
    return db


def _coverage(db, attempt_id):
    return json.loads(db.execute("SELECT coverage_json FROM entry_features WHERE attempt_id=?",
                                 (attempt_id,)).fetchone()[0])


def test_a_data_error_on_one_row_never_blocks_the_next(tmp_path, monkeypatch, ledger_fns):
    db = _two_rows(tmp_path)
    real = sol.flow_features

    def flow(attempt, rpc, **kw):
        if attempt["attempt_id"] == "att1":
            raise KeyError("accountKeys")              # a parse defect no guard foresaw
        return real(attempt, rpc, **kw)
    monkeypatch.setattr(sol, "flow_features", flow)
    attempts = {"att1": _attempt(), "att2": _attempt(attempt_id="att2")}
    monkeypatch.setattr(m, "entry_attempt_by_id", lambda conn, aid: attempts[aid])
    stats = {}
    done = sol.run_t1_once(db, None, Rpc(_market()), flat_fn=lambda: True, now_ts=D + 60, stats=stats)
    assert done == 2 and stats.get("parse_errors") == 1, stats
    assert _coverage(db, "att1")["flow_30s"] == "unavailable:parse_error:KeyError"
    assert _coverage(db, "att2")["flow_30s"] == "ok"
    assert sol.pending_rows(db) == []


def test_a_transport_error_still_stops_the_batch_and_keeps_both_rows_pending(tmp_path, monkeypatch,
                                                                             ledger_fns):
    db = _two_rows(tmp_path)

    def boom(method, params):
        raise TimeoutError("research key")
    monkeypatch.setattr(m, "entry_attempt_by_id", lambda conn, aid: _attempt(attempt_id=aid))
    stats = {}
    sol.run_t1_once(db, None, boom, flat_fn=lambda: True, now_ts=D + 60, stats=stats)
    assert stats["errors"] == 1 and len(sol.pending_rows(db)) == 2


class _NullPages(Rpc):
    """The source reads fine; every vault page answers HTTP 200 with a null (or shapeless) result."""

    def __init__(self, page_answer):
        super().__init__(_market())
        self.page_answer = page_answer

    def __call__(self, method, params):
        if method == "getTransactionsForAddress":
            self.calls.append(method)
            return self.page_answer
        return super().__call__(method, params)


@pytest.mark.parametrize("answer", [None, {}, [], {"data": None}, {"data": "x"}])
def test_a_null_page_is_a_failed_read_never_an_empty_market(tmp_path, monkeypatch, ledger_fns, answer):
    """Gamma on 307f10cee: `rpc(...) or {}` turned a null page into ([], exhausted) and every window
    into an ok zero. A page that is not {data: [...]} now fails the read: the row stays pending."""
    db = _db(tmp_path)
    monkeypatch.setattr(m, "entry_attempt_by_id", lambda conn, aid: _attempt())
    stats = {}
    sol.run_t1_once(db, None, _NullPages(answer), flat_fn=lambda: True, now_ts=D + 60, stats=stats)
    assert stats["errors"] == 1 and len(sol.pending_rows(db)) == 1
    cov = json.loads(db.execute("SELECT coverage_json FROM entry_features").fetchone()[0])
    assert "ok" not in {cov[n] for n in cap._flow_names()}
