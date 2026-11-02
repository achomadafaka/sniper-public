"""Volume proof uses program events, never the legacy net-delta tape."""
import base64
import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sniper.execution import pump_curve_buy as pump
from sniper.execution import pumpswap_buy as amm
from sniper.persistence import db_sol_volume as db
from sniper.services import memecoin_solana_tape as tape
from sniper.services import memecoin_solana_volume_evidence as evidence


FIXTURE = json.loads((Path(__file__).parent / "fixtures/pump_curve_buy_frames.json").read_text())
LIVE = FIXTURE["direct_buy_exact_sol_in"]
EVENT = pump.parse_trade_event(LIVE["trade_event_log"])
MINT = EVENT["mint"]
CURVE = LIVE["accounts"][3][0]
VAULT = LIVE["accounts"][4][0]
BASE = 1_800_000_000.0


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    tape.ensure_sol_cursor(c)
    c.execute("INSERT INTO sol_pool_vaults (pool, mint, coin_vault, quote_vault, quote_mint, resolved_at, program) VALUES(?,?,?,?,?,?,?)",
              (VAULT, MINT, VAULT, CURVE, "native-sol", iso(BASE), CURVE))
    c.commit()
    db.ensure_tables(c)
    return c


def tx(*, block_ts=BASE + 1, logs=None, signature="CaseSensitiveSig"):
    keys = [LIVE["accounts"][6][0], VAULT, CURVE]
    balance = {"accountIndex": 1, "mint": MINT, "owner": CURVE,
               "uiTokenAmount": {"amount": "100000"}}
    return {"blockTime": block_ts,
            "transaction": {"signatures": [signature], "message": {"accountKeys": keys}},
            "meta": {"err": None, "logMessages": copy.deepcopy(logs or LIVE["logs"]),
                     "preTokenBalances": [balance], "postTokenBalances": [balance]}}


def run(c, now, transactions=(), *, complete=True, reset=False):
    token = evidence.begin_pass(c, MINT, VAULT, iso(now), reset=reset)
    evidence.finish_pass(c, token, transactions=transactions, complete=complete)
    return db.coverage_rows(c, now=now)[0]


def warm(c, start=BASE):
    run(c, start)
    for delta in range(5, 301, 5):
        row = run(c, start + delta)
    return row


def test_first_clean_head_starts_new_300_second_window_and_legacy_is_not_proof():
    c = conn()
    assert db.coverage_rows(c, now=BASE) == []
    assert run(c, BASE)["reason"] == "warming_up"
    for delta in range(5, 296, 5):
        run(c, BASE + delta)
    assert not run(c, BASE + 299)["complete"]
    row = run(c, BASE + 300)
    assert row["complete"] == 1
    assert row["verified_since"] == BASE
    assert row["through"] == BASE + 300
    assert row["venue"] == "pump_curve" and row["market_pool"] == MINT


def test_receipts_use_real_block_time_case_sensitive_signature_and_each_gross_event():
    c = conn()
    run(c, BASE)
    # Two events in one transaction must not collapse into a net reserve change or legacy log 0.
    logs = [f"Program {pump.PUMP_PROGRAM} invoke [1]", "Program log: Instruction: BuyExactSolIn",
            LIVE["trade_event_log"], LIVE["trade_event_log"], f"Program {pump.PUMP_PROGRAM} success"]
    run(c, BASE + 2, [("CaseSensitiveSig", tx(logs=logs))])
    rows = db.swap_rows(c, VAULT, since=BASE, through=BASE + 2)
    assert len(rows) == 2 and len({r["event_index"] for r in rows}) == 2
    assert {r["signature"] for r in rows} == {"CaseSensitiveSig"}
    assert {r["block_ts"] for r in rows} == {BASE + 1}
    assert all(r["quote_atomic"] == str(EVENT["sol_amount"]) for r in rows)
    assert all(r["maker"] == EVENT["user"] for r in rows)
    assert all(r["payer"] == LIVE["accounts"][6][0] for r in rows)
    run(c, BASE + 3, [("casesensitivesig", tx(signature="casesensitivesig", logs=logs))])
    assert len(db.swap_rows(c, VAULT, since=BASE, through=BASE + 3)) == 4
    assert c.execute("SELECT typeof(quote_atomic) FROM sol_volume_swaps_v1").fetchone()[0] == "text"


@pytest.mark.parametrize("damage", ["no_time", "no_logs", "truncated", "wrong_emitter",
                                   "unclosed_stack", "wrong_mint", "wrong_signature",
                                   "unsupported_data", "unknown_instruction"])
def test_uncertain_transaction_resets_coverage(damage):
    c = conn()
    assert warm(c)["complete"]
    t = tx(block_ts=BASE + 301)
    if damage == "no_time": t.pop("blockTime")
    elif damage == "no_logs": t["meta"]["logMessages"] = None
    elif damage == "truncated": t["meta"]["logMessages"] += ["Log truncated"]
    elif damage == "wrong_emitter":
        t["meta"]["logMessages"] = ["Program Attacker invoke [1]", LIVE["trade_event_log"],
                                       "Program Attacker success"]
    elif damage == "unclosed_stack": t["meta"]["logMessages"] = LIVE["logs"][:-1]
    elif damage == "wrong_mint":
        raw = bytearray(base64.b64decode(LIVE["trade_event_log"].split(": ")[1]))
        raw[8:40] = bytes(32)
        t["meta"]["logMessages"] = [l if l != LIVE["trade_event_log"] else
            "Program data: " + base64.b64encode(raw).decode() for l in LIVE["logs"]]
    elif damage == "wrong_signature": t["transaction"]["signatures"] = ["another"]
    elif damage == "unsupported_data":
        t["meta"]["logMessages"] = [f"Program {pump.PUMP_PROGRAM} invoke [1]",
            "Program data: AAAAAAAAAAA=", f"Program {pump.PUMP_PROGRAM} success"]
    elif damage == "unknown_instruction":
        t["meta"]["logMessages"] = [f"Program {pump.PUMP_PROGRAM} invoke [1]",
            "Program log: Instruction: UnknownSwap", f"Program {pump.PUMP_PROGRAM} success"]
    row = run(c, BASE + 302, [("CaseSensitiveSig", t)])
    assert not row["complete"] and row["verified_since"] is None
    assert not run(c, BASE + 303)["complete"]
    assert run(c, BASE + 303)["verified_since"] == BASE + 303


def test_invalidation_is_committed_before_network_and_partial_or_reset_requires_rewarm():
    c = conn()
    warm(c)
    token = evidence.begin_pass(c, MINT, VAULT, iso(BASE + 301))
    assert not db.coverage_rows(c, now=BASE + 301)[0]["complete"]
    evidence.finish_pass(c, token, transactions=[], complete=False)
    assert run(c, BASE + 302)["verified_since"] == BASE + 302
    assert warm(c, BASE + 302)["complete"]
    assert not run(c, BASE + 603, reset=True)["complete"]
    assert run(c, BASE + 603)["verified_since"] == BASE + 603


def test_metadata_identity_change_cannot_reuse_a_window():
    c = conn()
    warm(c)
    c.execute("UPDATE sol_pool_vaults SET quote_vault='another-curve'")
    assert not run(c, BASE + 301)["complete"]


def test_full_successful_catchup_proves_inter_poll_gap_without_new_rpc():
    c = conn()
    assert warm(c)["complete"]
    row = run(c, BASE + 330)
    assert row["verified_since"] == BASE and row["complete"]


def test_read_helpers_do_not_create_schema():
    c = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        db.coverage_rows(c, now=BASE)
    with pytest.raises(sqlite3.OperationalError):
        db.swap_rows(c, VAULT, since=BASE, through=BASE + 1)
    assert c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


def test_tape_hook_reuses_only_existing_fetches_and_failure_keeps_legacy_capture(monkeypatch):
    c = conn()
    from sniper.persistence import db_ticks
    db_ticks.ensure_tick_tables(c)
    db_ticks.ensure_block_times(c)
    calls = []
    def sigs(_vault, _params):
        assert not db.coverage_rows(c, now=BASE)[0]["complete"]
        return [{"signature": "CaseSensitiveSig", "slot": 1, "blockTime": BASE - 1}]
    def fetched(sig):
        calls.append(sig)
        return tx(block_ts=BASE - 1)
    assert tape.tape_pass_for_vault(c, MINT, VAULT, sigs, fetched, iso(BASE), tx_budget=5) == 1
    assert calls == ["CaseSensitiveSig"]
    assert len(db.swap_rows(c, VAULT, since=BASE - 2, through=BASE)) == 1
    monkeypatch.setattr(evidence, "begin_pass", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    assert tape.tape_pass_for_vault(c, MINT, VAULT, lambda *_a: [], fetched,
                                    iso(BASE + 1), tx_budget=5) == 0


def amm_case(kind):
    if kind == "buy":
        fixture = json.loads((Path(__file__).parent / "fixtures/pumpswap_buy_frames.json").read_text())
        live = fixture["direct_buy_exact_quote_in"]
        accounts = [a[0] for a in live["accounts"]]
        logs = [f"Program {amm.AMM_PROGRAM} invoke [1]",
                "Program log: Instruction: BuyExactQuoteIn", live["buy_event_log"],
                f"Program {amm.AMM_PROGRAM} success"]
        amount = amm.parse_buy_event(live["buy_event_log"])["quote_amount_in_with_lp_fee"]
    else:
        fixture = json.loads((Path(__file__).parent / "fixtures/memecoin/pumpswap_sell_instructions.json").read_text())
        live = next(s for s in fixture["sells"] if s.get("logs")
                    and s["accounts"][4] == pump.WSOL_MINT)
        accounts = live["accounts"]
        # Fixture retained the real event only; frame it under the instruction's recorded program.
        logs = [f"Program {amm.AMM_PROGRAM} invoke [1]", "Program log: Instruction: Sell",
                *live["logs"], f"Program {amm.AMM_PROGRAM} success"]
        from sniper.execution import pumpswap_sell
        event = next(e for l in logs if l.startswith("Program data: ")
                     and (e := pumpswap_sell.parse_sell_event(l.split(": ")[1])))
        amount = event["quote_amount_out_without_lp_fee"]
    pool, mint, vault, quote_vault = accounts[0], accounts[3], accounts[7], accounts[8]
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    tape.ensure_sol_cursor(c)
    db.ensure_tables(c)
    c.execute("INSERT INTO sol_pool_vaults (pool, mint, coin_vault, quote_vault, quote_mint, resolved_at, program) VALUES(?,?,?,?,?,?,?)",
              (vault, mint, vault, quote_vault, accounts[4], iso(BASE), pool))
    keys = [accounts[1], vault, quote_vault]
    balances = [{"accountIndex": i, "mint": m, "owner": pool,
                 "uiTokenAmount": {"amount": "100000"}}
                for i, m in ((1, mint), (2, accounts[4]))]
    transaction = {"blockTime": BASE + 1,
        "transaction": {"signatures": [live["signature"]], "message": {"accountKeys": keys}},
        "meta": {"err": None, "logMessages": logs,
                 "preTokenBalances": balances, "postTokenBalances": balances}}
    return c, mint, vault, pool, live["signature"], transaction, amount


@pytest.mark.parametrize("kind", ["buy", "sell"])
def test_pumpswap_real_program_event_binds_pool_and_both_vaults(kind):
    c, mint, vault, pool, signature, transaction, amount = amm_case(kind)
    token = evidence.begin_pass(c, mint, vault, iso(BASE + 2))
    evidence.finish_pass(c, token, transactions=[(signature, transaction)], complete=True)
    row = db.coverage_rows(c, now=BASE + 2)[0]
    assert row["reason"] == "warming_up"
    assert row["venue"] == "pump_amm" and row["market_pool"] == pool
    swaps = db.swap_rows(c, vault, since=BASE, through=BASE + 2)
    assert len(swaps) == 1 and swaps[0]["quote_atomic"] == str(amount)
    assert swaps[0]["maker"] == transaction["transaction"]["message"]["accountKeys"][0]
    # A transfer with the same mint, but another owner, cannot carry this market's coverage.
    transaction["meta"]["preTokenBalances"][0]["owner"] = "another-pool"
    token = evidence.begin_pass(c, mint, vault, iso(BASE + 3))
    evidence.finish_pass(c, token, transactions=[(signature, transaction)], complete=True)
    assert not db.coverage_rows(c, now=BASE + 3)[0]["complete"]
    assert db.coverage_rows(c, now=BASE + 3)[0]["verified_since"] is None


def test_duplicate_receipt_is_idempotent_but_changed_transaction_resets_proof():
    c = conn()
    run(c, BASE)
    transaction = tx()
    run(c, BASE + 2, [("CaseSensitiveSig", transaction)])
    row = run(c, BASE + 3, [("CaseSensitiveSig", transaction)])
    assert row["verified_since"] == BASE
    assert len(db.swap_rows(c, VAULT, since=BASE, through=BASE + 3)) == 1
    raw = bytearray(base64.b64decode(LIVE["trade_event_log"].split(": ")[1]))
    raw[40:48] = (EVENT["sol_amount"] + 1).to_bytes(8, "little")
    changed = "Program data: " + base64.b64encode(raw).decode()
    transaction["meta"]["logMessages"] = [changed if l == LIVE["trade_event_log"] else l
                                            for l in LIVE["logs"]]
    row = run(c, BASE + 4, [("CaseSensitiveSig", transaction)])
    assert not row["complete"] and row["verified_since"] is None
    assert row["reason"] == "conflicting_receipt"


def test_pumpswap_empty_legacy_head_does_not_prove_program_identity():
    c, mint, vault, _pool, _sig, _transaction, _amount = amm_case("buy")
    for now in (BASE, BASE + 300):
        token = evidence.begin_pass(c, mint, vault, iso(now))
        evidence.finish_pass(c, token, transactions=[], complete=True)
    row = db.coverage_rows(c, now=BASE + 300)[0]
    assert not row["complete"] and row["reason"] == "unproven_amm_program"


def test_partial_install_volume_import_failure_cannot_abort_legacy_tape(monkeypatch):
    import builtins
    c = conn()
    original = builtins.__import__
    def unavailable(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sniper.services" and "memecoin_solana_volume_evidence" in fromlist:
            raise ImportError("partial installation")
        return original(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    assert tape.tape_pass_for_vault(c, MINT, VAULT, lambda *_: [], lambda _: None,
                                    iso(BASE), tx_budget=5) == 0
