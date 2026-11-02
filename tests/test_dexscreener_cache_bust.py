"""DexScreener cache bust (2026-09-12). The bare tokens URL is Cloudflare-cached for 30 s."""
from sniper.execution import solana_quote as q


def test_off_by_default_the_url_is_unchanged(monkeypatch):
    monkeypatch.delenv("MEMECOIN_DEXSCREENER_CACHE_BUST", raising=False)
    assert q.ds_tokens_url("MINT") == q.DEXSCREENER + "MINT"


def test_on_every_read_gets_a_unique_query(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_CACHE_BUST", "1")
    a, b = q.ds_tokens_url("A,B"), q.ds_tokens_url("A,B")
    assert a.startswith(q.DEXSCREENER + "A,B?cb=") and a != b


def test_the_stats_read_uses_the_builder(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_CACHE_BUST", "1")
    seen = []

    def http(method, url, headers=None, body=None):
        seen.append(url)
        return 200, {"pairs": []}, None
    q.dexscreener_stats("MINT", http=http)
    assert seen and "?cb=" in seen[0]
