from __future__ import annotations

import importlib


def _module():
    return importlib.import_module("scripts.memecoin_reclaim_ata_rent")


def test_submit_ambiguity_confirms_the_local_signature():
    module = _module()
    calls = []

    def send(_url, _method, _params):
        raise TimeoutError("send timed out")

    def confirm(signature, rpc_url):
        calls.append((signature, rpc_url))
        return "confirmed"

    state = module.submit_and_confirm(
        "https://rpc",
        "signed-base64",
        "local-signature",
        send_fn=send,
        confirm_fn=confirm,
    )

    assert state == "confirmed"
    assert calls == [("local-signature", "https://rpc")]


def test_submit_rejects_an_rpc_signature_mismatch():
    module = _module()
    calls = []

    def send(_url, _method, _params):
        return {"result": "different-signature"}

    def confirm(signature, rpc_url):
        calls.append((signature, rpc_url))
        return "confirmed"

    state = module.submit_and_confirm(
        "https://rpc",
        "signed-base64",
        "local-signature",
        send_fn=send,
        confirm_fn=confirm,
    )

    assert state == "identity_mismatch"
    assert calls == []


def test_submit_does_not_count_a_pending_transaction_as_confirmed():
    module = _module()

    def send(_url, _method, _params):
        return {"result": "local-signature"}

    state = module.submit_and_confirm(
        "https://rpc",
        "signed-base64",
        "local-signature",
        send_fn=send,
        confirm_fn=lambda _signature, _url: "pending",
    )

    assert state == "pending"


def test_empty_accounts_returns_only_zero_atomic_balances(monkeypatch):
    module = _module()
    response = {
        "result": {
            "value": [
                {
                    "pubkey": "empty",
                    "account": {
                        "lamports": 2_039_280,
                        "data": {"parsed": {"info": {"tokenAmount": {"amount": "0"}}}},
                    },
                },
                {
                    "pubkey": "funded",
                    "account": {
                        "lamports": 2_039_280,
                        "data": {"parsed": {"info": {"tokenAmount": {"amount": "1"}}}},
                    },
                },
            ]
        }
    }
    monkeypatch.setattr(module, "_rpc", lambda _url, _method, _params: response)

    assert module.empty_accounts("https://rpc", "owner") == [("empty", 2_039_280)]
