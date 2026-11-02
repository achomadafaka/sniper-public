"""Chain-1 Protect same-nonce replacement of a held exit (operator via Alpha, 2026-10-08).

BUNKER 72163: Flashbots Protect simulated our exit swap as a revert 3 s after the send and held it.
The executor waited 64 s for a receipt, then the deadline plus 24 s, before the exit was re-armed.
The final close of the same position showed simError at one poll and landed two polls later, so a
simError is not terminal and never releases anything.

The rule:
- the status watcher (its own thread, never signs) flags a PENDING exit after 2 consecutive polls
  with simError and isRevert;
- the full-exit receipt wait returns `unknown` early on a flagged hash (knob on);
- the recovery pass (the exit lane) signs a replacement at the SAME nonce with a fresh quote at the
  next wider rung and fees bumped past Protect's replacement rule, keeps both hashes on the claim,
  and settles on whichever lands;
- no path releases the claim or re-arms the exit on a simError. Knob MEMECOIN_CHAIN1_PROTECT_REPLACE,
  default off.
"""
import json
import sqlite3

import pytest
from eth_account import Account

from sniper.execution import chain1_protect as c1
from sniper.execution.evm_exit_executor import execute_exit_evm, replace_exit_evm
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R

OLD, NEW = "0x" + "aa" * 32, "0x" + "bb" * 32
NOW = "2026-10-08T19:54:30+00:00"
GWEI = 10 ** 9


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(c1, "_watching", set())
    monkeypatch.setattr(c1, "_reverting", {})
    monkeypatch.setattr(c1, "_attempts", {})
    monkeypatch.setenv("MEMECOIN_CHAIN1_PROTECT_REPLACE", "1")


# ---- the watcher flag ------------------------------------------------------------------------------

REVERT = {"status": "PENDING", "simError": "ExecutionReverted", "isRevert": True}


def _flags_during(answers):
    """Run the watch and record `reverting(OLD)` after every poll."""
    seen = []

    def fetch(_h):
        return answers.pop(0)

    def sleep(_s):
        seen.append(c1.reverting(OLD))
    t = c1.watch_status(OLD, chain="1", action="exit", fetch=fetch, sink=lambda r: None,
                        alert_fn=lambda t: None, sleep=sleep, clock=lambda: 1.0, start=False)
    t.run()
    return seen


def test_two_consecutive_revert_polls_flag_the_hash():
    assert _flags_during([dict(REVERT), dict(REVERT), {"status": "INCLUDED"}]) == [False, True]


def test_a_clean_poll_between_reverts_resets_the_count():
    """0xa0710b7647 (BUNKER final close): simError, clean, INCLUDED. It must never be flagged."""
    seen = _flags_during([dict(REVERT), {"status": "PENDING"}, dict(REVERT), {"status": "INCLUDED"}])
    assert seen == [False, False, False]


def test_a_fee_simerror_without_isrevert_never_flags():
    fee = {"status": "PENDING", "simError": "MaxFeePerGasTooLow", "isRevert": False}
    assert _flags_during([dict(fee), dict(fee), dict(fee), {"status": "FAILED"}]) == [False] * 3


def test_a_terminal_status_clears_the_flag():
    _flags_during([dict(REVERT), dict(REVERT), {"status": "FAILED"}])
    assert c1.reverting(OLD) is False


def test_replace_requested_needs_the_knob_and_chain_1(monkeypatch):
    c1._reverting[OLD] = c1.REVERT_POLLS
    assert c1.replace_requested("1", OLD) is True
    assert c1.replace_requested("8453", OLD) is False
    monkeypatch.setenv("MEMECOIN_CHAIN1_PROTECT_REPLACE", "0")
    assert c1.replace_requested("1", OLD) is False


def test_the_knob_defaults_off(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv("MEMECOIN_CHAIN1_PROTECT_REPLACE", raising=False)
    assert cfg.memecoin_chain1_protect_replace() is False


# ---- the early receipt-wait exit -------------------------------------------------------------------

def test_the_receipt_wait_returns_unknown_once_stopped(monkeypatch):
    import sniper.execution.evm_real_seams as S
    calls = []
    monkeypatch.setattr(S, "_rpc", lambda *a, **k: calls.append(a) or {"result": None})
    stop = iter([False, False, True])
    out = S.eth_confirm(OLD, "http://x", sleep=lambda s: None, max_attempts=30,
                        stop_fn=lambda: next(stop))
    assert out == "unknown" and len(calls) == 3


# ---- the fee floor ---------------------------------------------------------------------------------

ACCT = Account.from_key("0x" + "11" * 32)


def _signed(max_fee, tip, nonce=6):
    from sniper.execution.evm_real_seams import sign_tx
    raw, h = sign_tx({"to": "0x66a9893cc07d91d95644aedd05d03f95e1dba8af", "data": "0x", "value": 0,
                      "gas": 700000, "type": 2, "maxFeePerGas": max_fee,
                      "maxPriorityFeePerGas": tip, "nonce": nonce, "chainId": 1}, ACCT)
    return raw


def test_the_replacement_fee_floor_bumps_both_fees_past_one_eighth():
    floor = c1.replacement_fee_floor(_signed(20 * GWEI, GWEI // 10))
    assert floor == (20 * GWEI * 9 // 8 + 1, (GWEI // 10) * 9 // 8 + 1)


def test_an_unreadable_old_payload_has_no_floor():
    assert c1.replacement_fee_floor("0xnot-a-tx") is None


def test_sign_tx_honours_the_fee_floor(monkeypatch):
    from eth_account.typed_transactions import TypedTransaction
    from hexbytes import HexBytes
    from sniper.execution.evm_real_seams import sign_tx
    monkeypatch.setenv("MEMECOIN_EVM_TYPE2_CHAINS", "1")
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", "0.05")
    monkeypatch.setenv("MEMECOIN_CHAIN1_FEE_HEADROOM", "0")
    raw, _h = sign_tx({"to": "0x66a9893cc07d91d95644aedd05d03f95e1dba8af", "data": "0x",
                       "value": 0, "gas": 700000, "gasPrice": 5 * GWEI, "nonce": 6, "chainId": 1},
                      ACCT, fee_floor=(30 * GWEI, GWEI))
    d = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
    assert d["maxFeePerGas"] == 30 * GWEI and d["maxPriorityFeePerGas"] == GWEI and d["nonce"] == 6


# ---- the replacement itself ------------------------------------------------------------------------

def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _held_close(c, ladder=(10, 20, 30)):
    """BUNKER's shape: a chain-1 full close signed at nonce 6, broadcast, receipt unknown."""
    pid = m.open_position(c, source_user_id="u", source_handle="(real)", mint="0xtok", ticker="",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-10-08T18:54:03Z",
                          strategy="alpha_b", execution_mode="real", network_id="1")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=50.0,
                  fill_source="chain", fraction=1.0, created_at="2026-10-08T18:54:03Z", tx_sig="E")
    quote = {"sellAmount": "10", "buyAmount": "12", "minBuyAmount": "11",
             "transaction": {"to": "0xr", "data": "0xold"}}
    res = execute_exit_evm(
        c, m.get_position(c, pid), "b_stop", "2026-10-08T19:53:48+00:00", slippage_ladder=list(ladder),
        quote_fn=lambda *a: quote, approve_fn=lambda *a: {"pipelined": True},
        sign_fn=lambda tx: (_signed(20 * GWEI, GWEI // 10), OLD, 6), send_fn=lambda raw: OLD,
        confirm_fn=lambda h: "unknown", usdc_fn=lambda: 0.0, token_balance_fn=lambda mint: 10.0,
        allowance_fn=lambda *a: 10, sleep_fn=lambda s: None)
    assert res["status"] == "unknown"
    return pid


FRESH = {"sellAmount": "10", "buyAmount": "9", "minBuyAmount": "7",
         "transaction": {"to": "0xr", "data": "0xnew"}}


def _replace(c, pid, *, quote=FRESH, tokens=10.0, send=NEW, sign=None, max_replacements=2):
    calls = {"quote": [], "sign": [], "send": []}

    def quote_fn(mint, toks, slip):
        calls["quote"].append(slip)
        return quote

    def sign_fn(tx, *, nonce, replaces):
        calls["sign"].append((tx, nonce, replaces))
        return sign if sign is not None else ("0xrawnew", NEW, nonce)

    def send_fn(raw):
        calls["send"].append(raw)
        return send
    out = replace_exit_evm(c, m.get_position(c, pid), NOW, quote_fn=quote_fn, sign_fn=sign_fn,
                           send_fn=send_fn, token_balance_fn=lambda mint: tokens,
                           slippage_ladder=[10, 20, 30], max_replacements=max_replacements)
    return out, calls


def _row(c, pid):
    pos = m.get_position(c, pid)
    return pos, json.loads(pos["exit_intent_json"])


def test_the_replacement_signs_the_same_nonce_at_the_next_rung_and_keeps_both_hashes():
    c = _c(); pid = _held_close(c)
    claim = m.get_position(c, pid)["close_claim_id"]
    out, calls = _replace(c, pid)
    assert out["status"] == "replaced", out
    assert calls["quote"] == [20]                                  # the next wider rung
    (tx, nonce, replaces), = calls["sign"]
    assert tx == FRESH["transaction"] and nonce == 6 and replaces
    assert calls["send"] == ["0xrawnew"]
    pos, intent = _row(c, pid)
    assert pos["close_sig"] == NEW and pos["close_claim_id"] == claim and pos["is_open"] == 1
    assert intent["tx_identity"] == NEW and intent["nonce"] == 6
    assert intent["min_output_atomic"] == 7 and intent["replaced_tx_identities"] == [OLD]
    (row,) = c.execute("SELECT intent_json, status FROM memecoin_exit_intents WHERE claim_id=?",
                       (claim,)).fetchall()
    assert json.loads(row[0])["tx_identity"] == NEW and row[1] == "submitted_unknown"


@pytest.mark.parametrize("case", ["quote_miss", "bag_changed", "limit", "sign_failed",
                                  "sell_amount_changed"])
def test_no_skip_path_releases_the_claim_or_touches_the_held_send(case):
    c = _c(); pid = _held_close(c)
    before = dict(m.get_position(c, pid))
    kw = {"quote_miss": dict(quote=None), "bag_changed": dict(tokens=4.0),
          "limit": dict(max_replacements=0), "sign_failed": dict(sign=False),
          "sell_amount_changed": dict(quote={**FRESH, "sellAmount": "9"})}[case]
    out, calls = _replace(c, pid, **kw)
    assert out["status"] == "skipped", out
    assert calls["send"] == []
    after = dict(m.get_position(c, pid))
    for key in ("close_claim_id", "close_sig", "exit_intent_json", "is_open"):
        assert after[key] == before[key], key


def test_a_refused_replacement_send_restores_the_held_identity():
    c = _c(); pid = _held_close(c)
    before = dict(m.get_position(c, pid))
    out, _calls = _replace(c, pid, send=False)
    assert out["status"] == "skipped" and out["reason"] == "replacement_refused"
    after = dict(m.get_position(c, pid))
    assert after["close_sig"] == OLD == before["close_sig"]
    assert json.loads(after["exit_intent_json"])["tx_identity"] == OLD


def test_the_replacement_limit_counts_earlier_replacements():
    c = _c(); pid = _held_close(c)
    out, _ = _replace(c, pid, max_replacements=1)
    assert out["status"] == "replaced"
    out, calls = _replace(c, pid, max_replacements=1)
    assert out == {"status": "skipped", "reason": "replacement_limit"} and calls["send"] == []


# ---- the recovery pass -----------------------------------------------------------------------------

def _ctx(states, *, replace=None, receipts=None):
    return {"1": {"confirm_once_fn": lambda h: states.get(h, "not_found"),
                  "receipt_proceeds_fn": receipts or (lambda h, **k: None),
                  "replace_close_fn": replace}}


def test_recovery_replaces_a_flagged_held_close_without_releasing_it():
    c = _c(); pid = _held_close(c)
    c1._reverting[OLD] = c1.REVERT_POLLS
    asked = []
    R.recover_unverified_closes(c, _ctx({}, replace=lambda conn, pos: asked.append(pos["id"])), NOW)
    assert asked == [pid]
    pos = m.get_position(c, pid)
    assert pos["close_claim_id"] and pos["close_sig"] == OLD and pos["is_open"] == 1


def test_recovery_with_the_knob_off_never_replaces(monkeypatch):
    monkeypatch.setenv("MEMECOIN_CHAIN1_PROTECT_REPLACE", "0")
    c = _c(); pid = _held_close(c)
    c1._reverting[OLD] = c1.REVERT_POLLS
    R.recover_unverified_closes(
        c, _ctx({}, replace=lambda conn, pos: pytest.fail("replaced with the knob off")), NOW)
    assert m.get_position(c, pid)["close_sig"] == OLD


def test_an_unflagged_held_close_is_not_replaced():
    c = _c(); _held_close(c)
    R.recover_unverified_closes(
        c, _ctx({}, replace=lambda conn, pos: pytest.fail("replaced without a revert flag")), NOW)


def test_the_replaced_send_landing_settles_the_close_on_its_own_hash():
    """Both bytes share nonce 6, so at most one lands. If it is the OLD one, the close books on the
    old hash and its receipt; the new hash is never mistaken for the landed sell."""
    c = _c(); pid = _held_close(c)
    _replace(c, pid)
    proceeds = {OLD: 30.0}
    R.recover_unverified_closes(
        c, _ctx({OLD: "confirmed", NEW: "not_found"},
                receipts=lambda h, **k: proceeds.get(h)), NOW)
    pos = m.get_position(c, pid)
    assert pos["is_open"] == 0 and pos["close_sig"] == OLD
    (sig,) = c.execute("SELECT tx_sig FROM memecoin_fills WHERE position_id=? AND kind!='entry'",
                       (pid,)).fetchone()
    assert sig == OLD


def test_the_new_send_landing_settles_on_the_new_hash():
    c = _c(); pid = _held_close(c)
    _replace(c, pid)
    R.recover_unverified_closes(
        c, _ctx({NEW: "confirmed"}, receipts=lambda h, **k: 25.0 if h == NEW else None), NOW)
    pos = m.get_position(c, pid)
    assert pos["is_open"] == 0 and pos["close_sig"] == NEW


# ---- the production wiring -------------------------------------------------------------------------

@pytest.mark.parametrize("chain", ["1", "8453"])
def test_the_production_context_carries_the_replacement_under_the_wallet_lock(
        monkeypatch, tmp_path, chain):
    from test_memecoin_evm_context_keys import _production_context
    ctx = _production_context(monkeypatch, tmp_path, chain)
    fn = ctx.get("replace_close_fn")
    assert callable(fn) and hasattr(fn, "__wrapped__")              # serialize_executor-wrapped
    if chain != "1":
        c = _c()
        assert fn(c, {"id": 1, "mint": "0xtok", "network_id": chain})["reason"] == "not_chain_1"


# ---- Gamma REFUSE on 657e12117 ---------------------------------------------------------------------

def test_medium1_operator_ruling_a_held_b_stop_is_resigned_without_a_stop_recheck():
    """Operator, 2026-10-08: "Our exits should be instant, not rechecked." The replacement takes no
    execution check at all; accepted risk: a coin that bounced above its stop is sold anyway."""
    import inspect
    assert "quote_check_fn" not in inspect.signature(replace_exit_evm).parameters
    c = _c(); pid = _held_close(c)
    out, calls = _replace(c, pid)
    assert out["status"] == "replaced" and calls["send"] == ["0xrawnew"]


def test_medium2_an_attempt_consumes_the_flag_so_a_refusal_does_not_retry_every_sweep():
    c = _c(); pid = _held_close(c)
    c1._reverting[OLD] = c1.REVERT_POLLS
    attempts = []

    def replace(conn, pos):
        attempts.append(pos["id"])
        return {"status": "skipped", "reason": "replacement_refused"}
    for _ in range(5):                                   # five eth-worker sweeps, 2 s apart
        R.recover_unverified_closes(c, _ctx({}, replace=replace), NOW)
    assert attempts == [pid]
    assert c1.reverting(OLD) is False                    # 2 fresh reverting polls re-arm it


def test_medium2_attempts_are_capped_per_claim_whatever_each_did(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_CHAIN1_PROTECT_REPLACE_MAX", 2)
    monkeypatch.setattr(c1, "_attempts", {})
    c = _c(); pid = _held_close(c)
    attempts = []

    def replace(conn, pos):
        attempts.append(pos["id"])
        return {"status": "skipped", "reason": "replacement_refused"}
    for _ in range(4):                                   # the watcher re-flags between sweeps
        c1._reverting[OLD] = c1.REVERT_POLLS
        R.recover_unverified_closes(c, _ctx({}, replace=replace), NOW)
    assert attempts == [pid, pid]


def test_low3_the_replacement_quotes_the_first_rung_above_the_held_signed_rung():
    c = _c(); pid = _held_close(c, ladder=[30])          # the held send was signed at 30
    assert json.loads(m.get_position(c, pid)["exit_intent_json"])["signed_slippage_pct"] == 30
    out, calls = _replace(c, pid)
    assert out["status"] == "replaced" and calls["quote"] == [30]   # no rung above: the widest
    c2 = _c(); pid2 = _held_close(c2, ladder=[10])
    _out, calls2 = _replace(c2, pid2)
    assert calls2["quote"] == [20]
    assert json.loads(m.get_position(c2, pid2)["exit_intent_json"])["signed_slippage_pct"] == 20


def test_low6_a_snapshot_hash_that_moved_is_not_replaced():
    c = _c(); pid = _held_close(c)
    snap = dict(m.get_position(c, pid), close_sig="0x" + "cc" * 32)
    out = replace_exit_evm(c, snap, NOW, quote_fn=lambda *a: pytest.fail("quoted"),
                           sign_fn=lambda tx, **k: None, send_fn=lambda raw: None,
                           token_balance_fn=lambda mint: 10.0, slippage_ladder=[10, 20, 30])
    assert out == {"status": "skipped", "reason": "held_send_moved"}


def test_low4_affordability_reads_the_signed_bytes_not_the_floor():
    """sign_tx signs maxFee = max(headroom cap, floor): the node checks gas x the SIGNED maxFee."""
    raw = _signed(40 * GWEI, GWEI)                       # signed above any 20-gwei floor
    need = 700000 * 40 * GWEI
    assert c1.replacement_affordable(raw, need) is True
    assert c1.replacement_affordable(raw, need - 1) is False
    assert c1.replacement_affordable("0xjunk", 10 ** 30) is False
