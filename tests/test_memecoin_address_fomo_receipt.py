"""A Solana address verified by the profile's own Fomo buy receipts (2026-09-28, Alpha GO (a)).

Ew4rdo and smol_intern were armed on Solana yet dark on every lane: their buys reach us as Fomo
activity ids with no signature, so no J7 receipt could name their wallet. The Axiom directory's
x_handle match carried each profile's 8 most recent distinct-mint buys (the address's own balance
of that mint rose within the window) and none of the other profile's.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_address_registry as reg

FIXTURE = json.loads((Path(__file__).parent / "fixtures/memecoin/fomo_buy_receipt_ew4rdo.json").read_text())
EW4RDO = "V8c7i9prMkHT7fdMScUdZcZUjPmVJKFpTiVbzbaE2Bt"


def test_the_real_receipt_window_has_exactly_the_owners_buy():
    rises = [(tx["signature"], reg.owner_token_rise(tx, FIXTURE["owner"], FIXTURE["mint"]))
             for tx in FIXTURE["transactions"]]
    bought = [sig for sig, d in rises if d > 0]
    assert len(bought) == 1 and bought[0].startswith("3cnBA9AnVDHY"), rises
    other = "mDR91ufq4S2uSHa9E2PYL7xg4LwcQXaQFP7YK864a1z"
    assert all(reg.owner_token_rise(tx, other, FIXTURE["mint"]) == 0 for tx in FIXTURE["transactions"])


def test_a_failed_transaction_moves_nothing():
    tx = next(t for t in FIXTURE["transactions"]
              if reg.owner_token_rise(t, FIXTURE["owner"], FIXTURE["mint"]) > 0)
    assert reg.owner_token_rise(dict(tx, err={"InstructionError": [0, "x"]}),
                                FIXTURE["owner"], FIXTURE["mint"]) == 0


def _checks(n, matched=None, prefix="m"):
    matched = n if matched is None else matched
    return [("%s%d" % (prefix, i), "sig%d" % i if i < matched else None) for i in range(n)]


@pytest.mark.parametrize("checks, control, verified", [
    (_checks(8), _checks(8, 0, "c"), True),
    (_checks(8), _checks(8, 1, "c"), False),        # the control matched once: not this wallet
    (_checks(8, 7), _checks(8, 0, "c"), False),     # one own buy unmatched
    (_checks(4), _checks(4, 0, "c"), False),        # too few
    (_checks(8), _checks(3, 0, "c"), False),        # control too small to mean anything
    ([("same", "a")] * 8, _checks(8, 0, "c"), False),   # one mint eight times is one buy
])
def test_the_verdict(checks, control, verified):
    assert (reg.fomo_buy_receipt_status(checks, control) == "verified") is verified


def _ledger(tmp_path):
    c = sqlite3.connect(str(tmp_path / "ledger.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "uid-ew", "Ew4rdo")
    m.set_wallet_real_approved(c, "uid-ew", True, chains=["solana"], force=True)
    c.commit()
    return c


def test_a_verified_match_lands_in_the_registry_and_the_watchers_set(tmp_path):
    c = _ledger(tmp_path)
    assert EW4RDO not in (reg.armed_verified_owners(c) or {})
    got = reg.record_fomo_buy_receipt_match(
        c, source_user_id="uid-ew", handle="Ew4rdo", address=EW4RDO, checks=_checks(8),
        control_checks=_checks(8, 0, "c"), control_user_id="uid-control",
        now_iso="2026-09-28T13:00:00Z", actor="test")
    c.commit()
    assert got == "verified"
    row = c.execute("SELECT status, evidence_kind, evidence_json FROM memecoin_address_registry "
                    "WHERE source_user_id='uid-ew'").fetchone()
    assert (row[0], row[1]) == ("verified", "fomo_buy_receipt_match")
    proof = json.loads(row[2])
    assert len(proof["checks"]) == 8 and proof["control_user_id"] == "uid-control"
    assert reg.armed_verified_owners(c)[EW4RDO][0] == "uid-ew"
    assert reg.record_fomo_buy_receipt_match(
        c, source_user_id="uid-ew", handle="Ew4rdo", address=EW4RDO, checks=_checks(8),
        control_checks=_checks(8, 0, "c"), control_user_id="uid-control",
        now_iso="2026-09-28T13:01:00Z", actor="test") is None, "a re-run changes nothing"


def test_the_swapped_control_never_writes(tmp_path):
    c = _ledger(tmp_path)
    assert reg.record_fomo_buy_receipt_match(
        c, source_user_id="uid-ew", handle="Ew4rdo", address=EW4RDO, checks=_checks(8, 0),
        control_checks=_checks(8, 0, "c"), control_user_id="uid-control",
        now_iso="2026-09-28T13:00:00Z", actor="test") is None
    assert c.execute("SELECT COUNT(*) FROM memecoin_address_registry").fetchone()[0] == 0
