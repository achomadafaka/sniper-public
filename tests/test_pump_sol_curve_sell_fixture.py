"""The frozen confirmed Pump curve sells: exact pre-state account bytes paired with the next real sell."""
import base64
import json
import struct
from pathlib import Path

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/pump_sol_curve_sells.json").read_text())
DEFAULT_KEY = "11111111111111111111111111111111"


def _ceil(x, bps):
    return -(-x * bps // 10_000)


def _pre(row):
    acct = base64.b64decode(row["pre_state"]["account_b64"])
    return acct, struct.unpack_from("<4Q", acct, 8)


def test_every_sol_row_is_sol_quoted_and_reproduces_its_own_event_from_the_pre_state_bytes():
    assert len(FX["rows"]) >= 15
    for row in FX["rows"]:
        acct, (vt, vs, rt, rs) = _pre(row)
        ev = row["trade_event"]
        assert acct[83:115] == bytes(32) and ev["quote_mint"] == DEFAULT_KEY and row["quote_is_sol"] is True
        q = ev["token_amount"]
        gross = vs * q // (vt + q)
        assert ev["sol_amount"] == gross > 0, row["sell"]["signature"]       # the SOL fields, never quote_amount
        assert ev["virtual_sol_reserves"] == vs - gross and ev["real_sol_reserves"] == rs - gross
        assert ev["virtual_token_reserves"] == vt + q
        assert ev["fee"] == _ceil(gross, ev["fee_basis_points"])
        assert ev["creator_fee"] == _ceil(gross, ev["creator_fee_basis_points"])
        assert gross <= rs


def test_the_quote_token_rows_are_kept_apart_and_never_read_as_lamports():
    for row in FX["quote_token_rows"]:
        acct, _ = _pre(row)
        ev = row["trade_event"]
        assert acct[83:115] != bytes(32) and ev["quote_mint"] != DEFAULT_KEY and ev["sol_amount"] == 0


def test_the_sol_rows_cover_mayhem_and_cashback():
    modes = {(_pre(r)[0][81], _pre(r)[0][82]) for r in FX["rows"]}
    assert (1, 0) in modes and (0, 1) in modes and (0, 0) in modes
