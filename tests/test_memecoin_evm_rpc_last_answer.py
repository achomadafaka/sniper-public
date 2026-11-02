"""_rpc remembers which member answered, so a phantom balance can be attributed (2026-09-05)."""
from sniper.execution import evm_wallet as W
from sniper.execution import evm_rpc_pool as P


def test_single_member_path_records_target_and_raw_result(monkeypatch):
    monkeypatch.setattr(P, "pool_for", lambda url: [url])
    def http(_m, url, body=None, **_k):
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x1550f7dca70000"}, None
    j = W._rpc("https://only.example/rpc", "eth_getBalance", ["0x" + "11" * 20, "latest"], http=http)
    assert j["result"] == "0x1550f7dca70000"
    assert W.last_answer() == ("https://only.example/rpc", "0x1550f7dca70000")


def test_last_answer_is_per_thread_and_empty_when_nothing_answered():
    import threading
    seen = {}
    def run():
        seen["other"] = W.last_answer()
    t = threading.Thread(target=run); t.start(); t.join()
    assert seen["other"] == (None, None)
