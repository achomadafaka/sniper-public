"""A blind exit rung says WHAT failed (2026-09-29, pos 66990 on 56).

Ten manual-close ladders logged only `code=provider_rpc_transport`. The real answer was Kyber's
HTTP 400 {"code":4011,"message":"token not found"} (its BSC index had dropped WBNB): the rung's
`_failure_type` maps every non-429 HTTPError to rpc_transport and the body was thrown away, the
ladder's `_run` kept no text of a raised exception, and the exit line printed no detail. The CLASS
stays RPC_TRANSPORT (infrastructure; it never advances unsellable counters). Only the words change.
"""
import io
import json
import logging
import urllib.error

import pytest

from sniper.execution import evm_kyber_quote as K
from sniper.execution import evm_real_seams as S
from sniper.settings import memecoin as cfg


def _http_400(body):
    return urllib.error.HTTPError("https://aggregator-api.kyberswap.com/bsc/api/v1/routes", 400,
                                  "Bad Request", {}, io.BytesIO(json.dumps(body).encode()))


def test_a_kyber_400_keeps_its_code_and_message_and_stays_blind(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", ("56",), raising=False)

    def reject(req, timeout=None):
        raise _http_400({"code": 4011, "message": "token not found", "details": None})
    monkeypatch.setattr(K.urllib.request, "urlopen", reject)
    with pytest.raises(K.KyberQuoteUnavailable) as caught:
        K.dex_kyber_quote("56", "0x" + "7c" * 20, "0x" + "bb" * 20, 10 ** 18, "0x" + "89" * 20)
    assert caught.value.failure_type == "rpc_transport"          # class unchanged: infrastructure
    assert "4011" in str(caught.value) and "token not found" in str(caught.value)


def test_a_raised_rung_carries_its_error_text_into_the_miss_detail():
    rung = S.rung_record("kyber", False, run_id="r1", blind_reason="rpc_transport",
                         reason="KyberQuoteUnavailable: rpc_transport: HTTP 400 4011 token not found")
    code, detail = S.classify_quote_miss([rung])
    assert code == "provider_rpc_transport"
    assert "kyber could not read: rpc_transport" in detail and "token not found" in detail, detail


def test_the_ladder_records_a_raised_rungs_error_text_without_urls():
    err = S.rung_error_text(RuntimeError(
        "boom at https://bnb-mainnet.g.alchemy.com/v2/SECRETKEY123/x and more"))
    assert err.startswith("RuntimeError: boom at bnb-mainnet.g.alchemy.com")
    assert "SECRETKEY123" not in err and len(err) <= 200
    err = S.rung_error_text(RuntimeError("ws down wss://bsc-rpc.example/ws/KEYABC and price?apikey=K2&x=1"))
    assert "KEYABC" not in err and "K2" not in err and "apikey=***" in err, err


def test_the_exit_miss_line_prints_the_detail(caplog):
    from sniper.execution.evm_exit_executor import execute_exit_evm
    import sqlite3
    from sniper.persistence import db_memecoin as db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    pid = db.open_position(
        conn, source_user_id="s", source_handle="s", mint="0x" + "7c" * 20, ticker="DOTS",
        entry_price=1.0, entry_notional=18.94, entry_fill_source="chain", entry_lag_s=1.0,
        their_size_usd=10.0, opened_at="2026-09-29T17:13:27Z", strategy="alpha_b",
        execution_mode="real", network_id="56")
    db.insert_fill(conn, position_id=pid, kind="entry", price=1.0, qty=5.0, notional=5.0,
                   fill_source="chain", fraction=1.0, created_at="2026-09-29T17:13:27Z")
    conn.commit()
    position = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    miss = {"code": "provider_rpc_transport",
            "detail": "kyber could not read: rpc_transport: HTTP 400 4011 token not found",
            "rungs": [{"rung": "kyber", "hit": False, "blind_reason": "rpc_transport"}]}
    with caplog.at_level(logging.WARNING):
        execute_exit_evm(conn, position, "manual", "2026-09-29T19:26:00Z",
                         quote_fn=lambda *_a: None, quote_miss_fn=lambda _slip: miss,
                         approve_fn=lambda *_a: True, sign_fn=lambda _tx: None,
                         send_fn=lambda _raw: False, confirm_fn=lambda _h: None,
                         usdc_fn=lambda: 0.0, token_balance_fn=lambda _m: 5.0,
                         slippage_ladder=[10])
    lines = [r.getMessage() for r in caplog.records if "exit quote miss" in r.getMessage()]
    assert lines and "token not found" in lines[0], lines


def test_the_ladder_keeps_a_raised_rungs_text_as_its_reason(monkeypatch):
    """Through the real ladder: the Kyber rung raises its typed 400 and its record says so."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from test_memecoin_evm_measured_ladder import CHAIN, _ladder
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", (CHAIN,), raising=False)

    def reject(*_a, **_k):
        raise K.KyberQuoteUnavailable("rpc_transport", "HTTP 400 4011 token not found")
    monkeypatch.setattr(K, "_routes", reject)
    rungs = []
    assert _ladder(monkeypatch, rungs=rungs) is None
    kyber = next(r for r in rungs if r["rung"] == "kyber")
    assert kyber["blind_reason"] == "rpc_transport"
    assert "token not found" in kyber["reason"] and kyber["reason"].startswith("KyberQuoteUnavailable")
