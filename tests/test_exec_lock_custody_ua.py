import io
import json

from sniper.execution import exec_lock


def test_custody_post_sends_an_explicit_user_agent(monkeypatch):
    # chain 4663's RPC sits behind Cloudflare, which answers Python-urllib's default agent 403 1010;
    # the EVM-08 fence then reads "custody read failed: HTTPError" and cannot see a mined tx.
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return io.BytesIO(json.dumps({"result": "0x1"}).encode())
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert exec_lock._custody_post("https://rpc.example", "eth_getTransactionCount", ["0x0", "latest"]) == {"result": "0x1"}
    assert seen["ua"] and "Python-urllib" not in seen["ua"]
