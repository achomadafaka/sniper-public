"""WO-6 — ExitIntent binds the decision to the claim and is re-verified BEFORE signing.

A superseded decision (the WO-5A controller revision moved), a lost claim, or a position closed while
the executor walked the slippage ladder is now refused pre-sign — the executor never signs a stale
on-chain sale. The existing one-shot claim / durable signed identity / submitted-unknown machinery is
extended, not rewritten (the happy-path close/derisk tests are untouched and still pass).
"""
import sqlite3

from sniper.execution import solana_exit_executor as ex
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_exit_intent import (
    ExitIntent, bind_quote, build_exit_intent, quote_binding_complete, verify_exit_intent)


def _c(entry_notional=10.0):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="(real)", mint="MINT", ticker="",
                          entry_price=0.02, entry_notional=entry_notional, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                          execution_mode="real")
    return c, m.get_position(c, pid)


# --- value + verifier --------------------------------------------------------------------------

def test_exit_intent_json_roundtrip_and_with_quote():
    i = ExitIntent(position_id=1, claim_id="cid", action="close", fraction=1.0,
                   controller_revision=3, balance_snapshot=500.0, decision_state_id=42)
    assert ExitIntent.from_json(i.to_json()) == i
    q = i.with_quote(quote_digest="d", min_output=9.0, quote_time="t", blockhash="bh",
                     last_valid_height=100, quote_asset_usd=2345.67)
    assert q.quote_digest == "d" and q.last_valid_height == 100 and q.claim_id == "cid"
    assert q.quote_asset_usd == 2345.67
    assert ExitIntent.from_json("not json") is None
    assert ExitIntent.from_json("[]") is None
    assert ExitIntent.from_json('{"position_id": 1}') is None


def test_build_captures_controller_revision_and_balance():
    pos = {"id": 7, "controller_revision": 4}
    i = build_exit_intent(pos, claim_id="c", action="close", fraction=1.0, balance=250.0)
    assert i.controller_revision == 4 and i.balance_snapshot == 250.0 and i.position_id == 7


def test_quote_binding_requires_positive_exact_input_and_minimum_output():
    intent = build_exit_intent({"id": 7}, claim_id="c", action="close", fraction=1.0)
    assert not quote_binding_complete(bind_quote(intent, {"outAmount": "10"}))
    assert not quote_binding_complete(bind_quote(
        intent, {"inAmount": "5", "otherAmountThreshold": "0"}))
    assert quote_binding_complete(bind_quote(
        intent, {"inAmount": "5", "otherAmountThreshold": "9"}))


def test_verify_typed_refusals():
    intent = ExitIntent(position_id=1, claim_id="C", action="close", fraction=1.0,
                        controller_revision=2, balance_snapshot=500.0)
    ok = {"is_open": 1, "close_claim_id": "C", "controller_revision": 2}
    assert verify_exit_intent(intent, ok, balance=500.0) is None
    assert verify_exit_intent(intent, {"is_open": 0}) == "position_gone"
    assert verify_exit_intent(intent, {**ok, "close_claim_id": "OTHER"}) == "claim_lost"
    assert verify_exit_intent(intent, {**ok, "controller_revision": 3}) == "decision_superseded"
    assert verify_exit_intent(intent, ok, balance=100.0) == "balance_changed"  # concurrent sale
    assert verify_exit_intent(intent, ok, balance=700.0) == "balance_changed"  # new tokens arrived
    state_bound = ExitIntent(position_id=1, claim_id="C", action="close", fraction=1.0,
                             controller_revision=2, decision_state_id=9)
    assert verify_exit_intent(state_bound, ok) == "decision_state_unverifiable"
    assert verify_exit_intent(state_bound, ok, current_decision_state_id=10) == \
        "decision_state_superseded"
    assert verify_exit_intent(state_bound, ok, current_decision_state_id=9) is None


def test_close_and_derisk_claims_are_mutually_exclusive():
    c, pos = _c()
    close = m.claim_position_close(c, pos["id"], "t", "real_trail")
    assert close
    assert m.claim_position_derisk(c, pos["id"], "t") is None
    assert m.release_position_close_claim(c, pos["id"], close)
    derisk = m.claim_position_derisk(c, pos["id"], "t")
    assert derisk
    assert m.claim_position_close(c, pos["id"], "t", "real_trail") is None


def test_exit_intent_binding_is_write_once_for_claim_owner():
    c, pos = _c()
    claim = m.claim_position_close(c, pos["id"], "t", "real_trail")
    first = build_exit_intent(pos, claim_id=claim, action="close", fraction=1.0)
    other = build_exit_intent(pos, claim_id=claim, action="close", fraction=0.5)
    assert m.bind_exit_intent(c, pos["id"], claim, first.to_json()) is True
    assert m.bind_exit_intent(c, pos["id"], claim, other.to_json()) is False
    assert ExitIntent.from_json(m.get_position(c, pos["id"])["exit_intent_json"]) == first


def test_exit_intent_binding_rejects_identity_or_action_mismatch():
    c, pos = _c()
    claim = m.claim_position_close(c, pos["id"], "t", "real_trail")
    wrong_claim = build_exit_intent(pos, claim_id="other", action="close", fraction=1.0)
    wrong_action = build_exit_intent(pos, claim_id=claim, action="derisk", fraction=0.5)
    wrong_position = ExitIntent(position_id=999, claim_id=claim, action="close", fraction=1.0,
                                controller_revision=0)
    assert m.bind_exit_intent(c, pos["id"], claim, wrong_claim.to_json()) is False
    assert m.bind_exit_intent(c, pos["id"], claim, wrong_action.to_json()) is False
    assert m.bind_exit_intent(c, pos["id"], claim, wrong_position.to_json()) is False


# --- executor: refuse to sign a superseded decision --------------------------------------------

class _Deps:
    def __init__(self, conn=None, pid=None, bump_on_quote=False):
        self.conn, self.pid, self.bump_on_quote = conn, pid, bump_on_quote
        self.signs = 0
        self.confirmed = False
    def quote_fn(self, mint, tokens, slip):
        if self.bump_on_quote:      # simulate a concurrent reducer advancing the decision revision
            m.persist_controller_state_cas(self.conn, self.pid, 0, consecutive_break_events=1)
            self.bump_on_quote = False
        return {"inAmount": "500", "outAmount": "100", "otherAmountThreshold": "90"}
    def usdc_fn(self):
        return 15.0 if self.confirmed else 0.0
    def token_fn(self, mint):
        return 500.0
    def sign_fn(self, quote):
        self.signs += 1
        return (b"tx", f"SIG{self.signs}")
    def send_fn(self, signed):
        pass
    def confirm_fn(self, sig):
        self.confirmed = True
        return "confirmed"


def _run_close(c, pos, d):
    return ex.execute_exit(c, pos, "b_stop", "2026-07-09T01:00:00Z",
                           quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
                           confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_balance_fn=d.token_fn,
                           slippage_ladder=[3.0, 10.0, 25.0])


def test_close_still_binds_intent_on_the_happy_path():
    c, pos = _c()
    d = _Deps()
    assert _run_close(c, pos, d)["status"] == "closed"
    # the intent was bound (audit trail) even though it settled clean
    p = m.get_position(c, pos["id"])
    intent = ExitIntent.from_json(p["exit_intent_json"])
    assert intent is not None and d.signs == 1
    assert intent.quote_digest and intent.signed_payload_digest
    assert intent.tx_identity == "SIG1"
    status = c.execute(
        "SELECT status FROM memecoin_exit_intents WHERE position_id=?", (pos["id"],)).fetchone()[0]
    assert status == "confirmed"


def test_close_refuses_to_sign_a_superseded_decision():
    c, pos = _c()
    d = _Deps(conn=c, pid=pos["id"], bump_on_quote=True)
    r = _run_close(c, pos, d)
    assert r["status"] == "superseded" and r["reason"] == "decision_superseded"
    assert d.signs == 0                                     # NEVER signed the stale sale
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 1 and p["close_sig"] is None
    assert p["close_claim_id"] is None                     # unsigned claim released on refusal


def test_close_revision_change_after_verify_never_broadcasts_signed_sale(monkeypatch):
    # The SERIAL order (MEMECOIN_EXIT_OVERLAP_READS=0): the hook below is the USDC pre-read, which
    # ran between the verify and the signature. With the reads overlapped that read happens during
    # the quote, and the same window is the swap build -- covered by
    # tests/test_memecoin_exit_overlap_reads.py::test_a_revision_change_during_the_build_is_refused_unsigned.
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_OVERLAP_READS", False, raising=False)
    c, pos = _c()
    d = _Deps()
    sends = {"n": 0}

    def usdc():
        if m.get_position(c, pos["id"])["controller_revision"] == 0:
            m.persist_controller_state_cas(c, pos["id"], 0, consecutive_break_events=1)
        return 15.0

    d.usdc_fn = usdc
    d.send_fn = lambda _raw: sends.__setitem__("n", sends["n"] + 1)
    result = _run_close(c, pos, d)

    assert result["status"] == "superseded"
    assert d.signs == 0 and sends["n"] == 0
    row = m.get_position(c, pos["id"])
    assert row["close_sig"] is None and row["close_claim_id"] is None


def test_close_revision_change_inside_sign_is_caught_by_submission_cas():
    c, pos = _c()
    d = _Deps()
    sends = {"n": 0}

    def sign(_quote):
        m.persist_controller_state_cas(c, pos["id"], 0, consecutive_break_events=1)
        d.signs += 1
        return b"signed", "SIG"

    d.sign_fn = sign
    d.send_fn = lambda _raw: sends.__setitem__("n", sends["n"] + 1)
    result = _run_close(c, pos, d)

    assert result == {"status": "superseded", "reason": "decision_superseded"}
    assert d.signs == 1 and sends["n"] == 0
    row = m.get_position(c, pos["id"])
    assert row["close_sig"] is None and row["close_claim_id"] is None


def test_close_rereads_balance_immediately_before_signing():
    c, pos = _c()
    d = _Deps()
    balances = iter([500.0, 100.0])
    d.token_fn = lambda _mint: next(balances)

    result = _run_close(c, pos, d)

    assert result == {"status": "superseded", "reason": "balance_changed"}
    assert d.signs == 0


def test_incomplete_exact_quote_never_reaches_signing():
    c, pos = _c()
    d = _Deps()
    d.quote_fn = lambda *_args: {"outAmount": "100"}

    result = _run_close(c, pos, d)

    assert result["status"] == "stuck"
    assert d.signs == 0


def test_newer_materialized_state_supersedes_before_signing():
    c, pos = _c()
    d = _Deps()
    state_ids = iter([9, 10])
    result = ex.execute_exit(
        c, pos, "b_stop", "2026-07-09T01:00:00Z",
        quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_balance_fn=d.token_fn,
        slippage_ladder=[3.0], decision_state_id=9,
        decision_state_id_fn=lambda: next(state_ids))

    assert result == {"status": "superseded", "reason": "decision_state_superseded"}
    assert d.signs == 0


def test_partial_exit_refuses_superseded_decision():
    c, pos = _c()
    d = _Deps(conn=c, pid=pos["id"], bump_on_quote=True)
    r = ex.execute_partial_exit(c, pos, 0.5, "2026-07-09T01:00:00Z",
                                quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
                                confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn,
                                token_balance_fn=d.token_fn, slippage_ladder=[3.0, 10.0])
    assert r["status"] == "superseded" and d.signs == 0
    p = m.get_position(c, pos["id"])
    assert p["derisk_claim_id"] is None and p["derisked_at"] is None


def test_partial_exit_persists_decision_state_and_exact_quote_binding():
    c, pos = _c()
    d = _Deps()
    d.quote_fn = lambda mint, tokens, slip: {
        "inAmount": "250", "outAmount": "100", "otherAmountThreshold": "90",
        "routePlan": [{"swapInfo": {"ammKey": "pool"}}],
    }
    result = ex.execute_partial_exit(
        c, pos, 0.5, "2026-07-09T01:00:00Z", quote_fn=d.quote_fn, sign_fn=d.sign_fn,
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn,
        token_balance_fn=d.token_fn, slippage_ladder=[3.0], decision_state_id=77,
        decision_state_id_fn=lambda: 77)
    assert result["status"] == "derisked"
    intent = ExitIntent.from_json(m.get_position(c, pos["id"])["exit_intent_json"])
    assert intent.decision_state_id == 77
    assert intent.sell_amount_atomic == 250 and intent.min_output_atomic == 90
    assert intent.quote_digest and intent.signed_payload_digest and intent.tx_identity == "SIG1"
    import base64
    assert base64.b64decode(intent.signed_payload_b64) == b"tx"


def test_solana_submitted_intent_persists_blockhash_validity_and_exact_signed_bytes():
    c, pos = _c()
    d = _Deps()
    d.sign_fn = lambda _quote: (
        b"exact-signed-bytes", "SIGV",
        {"blockhash": "BH", "last_valid_height": 123})
    result = _run_close(c, pos, d)

    assert result["status"] == "closed"
    intent = ExitIntent.from_json(m.get_position(c, pos["id"])["exit_intent_json"])
    import base64
    assert intent.blockhash == "BH" and intent.last_valid_height == 123
    assert base64.b64decode(intent.signed_payload_b64) == b"exact-signed-bytes"


# --- the EVM lane gets the identical guard (patch every lane) -----------------------------------

def test_evm_close_refuses_superseded_decision():
    from sniper.execution.evm_exit_executor import execute_exit_evm
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u", source_handle="(real)", mint="0xtok", ticker="",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                          execution_mode="real", network_id="8453")
    pos = m.get_position(c, pid)
    signs = {"n": 0}

    def quote_fn(mint, tokens, slip):
        m.persist_controller_state_cas(c, pid, 0, consecutive_break_events=1)   # revision 0 -> 1
        return {"sellAmount": "10", "minBuyAmount": "9",
                "transaction": {"to": "0xr"}, "issues": {}}

    def sign_fn(tx):
        signs["n"] += 1
        return ("0xraw", "0xhash", 3)

    approvals = {"n": 0}
    def approve(*_a):
        approvals["n"] += 1
        return True

    r = execute_exit_evm(c, pos, "b_stop", "t", quote_fn=quote_fn, approve_fn=approve,
                         sign_fn=sign_fn, send_fn=lambda raw: None, confirm_fn=lambda h: "confirmed",
                         usdc_fn=lambda: 50.0, token_balance_fn=lambda mint: 10.0,
                         slippage_ladder=[3.0, 10.0])
    assert r["status"] == "superseded" and signs["n"] == 0 and approvals["n"] == 0
    assert m.get_position(c, pid)["close_sig"] is None


def test_evm_close_reverifies_revision_after_approval_before_signing():
    from sniper.execution.evm_exit_executor import execute_exit_evm
    c, pos = _c()
    signs = {"n": 0}

    def approve(*_args):
        m.persist_controller_state_cas(c, pos["id"], 0, consecutive_break_events=1)
        return True

    result = execute_exit_evm(
        c, pos, "b_stop", "t",
        quote_fn=lambda *_a: {"sellAmount": "10", "minBuyAmount": "9",
                              "transaction": {"to": "0xr"},
                              "issues": {"allowance": {"spender": "0xrouter"}}},
        approve_fn=approve,
        sign_fn=lambda _tx: (signs.__setitem__("n", signs["n"] + 1) or
                             ("0xraw", "0xhash", 3)),
        send_fn=lambda _raw: None, confirm_fn=lambda _h: "confirmed",
        usdc_fn=lambda: 50.0, token_balance_fn=lambda _mint: 10.0,
        slippage_ladder=[3.0])

    assert result == {"status": "superseded", "reason": "decision_superseded"}
    assert signs["n"] == 0
    row = m.get_position(c, pos["id"])
    assert row["close_sig"] is None and row["close_claim_id"] is None
