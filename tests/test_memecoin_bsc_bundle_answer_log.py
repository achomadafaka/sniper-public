"""BlockRazor's answer to every bundle is logged (Gamma/Alpha 2026-10-07, EVM-08 go-live).

EVM-08 added maxBlockNumber to the live bundle. BscSender returns on the first member acceptance,
so the bundle's own reply was dropped: a rejection of the new param would cost the private path on
56 in silence behind the public posts. One WARNING per bundle post, after the post (from the
detached thread too), never carrying the operator's token. Logging only: the reply is unchanged.
"""
import io
import json
import logging
import threading
import time
import urllib.error

import pytest
from eth_account import Account

from sniper.services import memecoin_bsc_fast_lane as bsc

TOKEN = "op-Token+Secret/Value=1"


def _raw():
    tx = {"type": 2, "chainId": 56, "nonce": 1, "maxFeePerGas": 10, "maxPriorityFeePerGas": 0,
          "gas": 21000, "to": "0x" + "22" * 20, "value": 0, "data": b""}
    return "0x" + bytes(Account.sign_transaction(tx, b"\x03" * 32).raw_transaction).hex()


@pytest.fixture(autouse=True)
def _isolated_lock_dir(monkeypatch, tmp_path):
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_default_lock_dir", lambda: str(tmp_path))


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("bsc.bundle ")]


@pytest.mark.parametrize("answer, want", [
    ({"jsonrpc": "2.0", "id": 1, "result": {"bundleHash": "0xabc"}}, "answer=ok"),
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid maxBlockNumber"}},
     "answer=error code=-32602 message=invalid maxBlockNumber"),
    ({}, "answer=no_result"),
])
def test_every_bundle_answer_is_logged_with_its_max_block(caplog, answer, want):
    send = bsc.blockrazor_bundle(TOKEN, post=lambda url, body, headers: answer)
    with caplog.at_level(logging.WARNING):
        reply = send(_raw(), max_block=1020)
    [line] = _lines(caplog)
    assert "max_block=1020" in line and want in line and " ms=" in line, line
    assert reply[0] == "ambiguous"                         # the reply itself is unchanged


def test_an_http_refusal_logs_its_status_and_body_without_the_token(caplog):
    def post(url, body, headers):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(
            ('{"error":{"code":-32000,"message":"bad param, auth %s"}}' % TOKEN).encode()))
    send = bsc.blockrazor_bundle(TOKEN, post=post)
    with caplog.at_level(logging.WARNING):
        send(_raw(), max_block=1020)
    [line] = _lines(caplog)
    assert "answer=http_400" in line and "bad param" in line, line
    assert TOKEN not in line and "Secret" not in line


def test_a_transport_failure_is_logged(caplog):
    def post(url, body, headers):
        raise TimeoutError("timed out")
    with caplog.at_level(logging.WARNING):
        bsc.blockrazor_bundle(TOKEN, post=post)(_raw(), max_block=None)
    [line] = _lines(caplog)
    assert "max_block=None" in line and "answer=transport:TimeoutError" in line, line


def test_an_error_echoing_the_token_never_logs_it(caplog):
    answer = {"error": {"code": 401, "message": "token %s is not authorized" % TOKEN}}
    with caplog.at_level(logging.WARNING):
        bsc.blockrazor_bundle(TOKEN, post=lambda u, b, h: answer)(_raw(), max_block=7)
    [line] = _lines(caplog)
    assert TOKEN not in line and "not authorized" in line, line


def test_the_detached_bundle_post_logs_after_the_sender_returned(caplog):
    """BscSender returns on a member's acceptance while the bundle post is still running."""
    gate, posted = threading.Event(), threading.Event()

    def post(url, body, headers):
        posted.set()
        gate.wait(5)
        return {"result": "0xbundle"}

    class Member:
        def send(self, raw):
            posted.wait(5)                                  # the bundle is on the wire first
            return "accepted", "0x" + "ab" * 32
    sender = bsc.BscSender([Member()], bundle=bsc.blockrazor_bundle(TOKEN, post=post))
    with caplog.at_level(logging.WARNING):
        assert sender.send(_raw(), max_block=1020)[0] == "accepted"
        assert _lines(caplog) == []                         # not answered yet
        gate.set()
        sender._pool.shutdown(wait=True)
    [line] = _lines(caplog)
    assert "max_block=1020" in line and "answer=ok" in line, line


def test_a_failing_answer_log_never_changes_the_reply(monkeypatch):
    monkeypatch.setattr(bsc, "_without", lambda *a: (_ for _ in ()).throw(ValueError("x")))
    send = bsc.blockrazor_bundle(TOKEN, post=lambda u, b, h: {"result": "0xb"})
    assert send(_raw(), max_block=1) == ("ambiguous", "blockrazor bundle")
