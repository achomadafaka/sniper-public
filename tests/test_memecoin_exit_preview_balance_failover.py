"""The exact-exit preview's balance read fails over for the DECISION, never for signing.

Position 55147 (BEER, Solana, 2026-09-09): the exit sweep quoted the bag ~8 times a second, then
produced nothing for 57 s, then confirmed the -35% stop at -52%. Zero Jupiter requests in the
window: the preview's FIRST step, a custody-endpoint balance read, returned nothing and the seam
returned before Jupiter was asked -- no route row, no miss counter (the nominal mid was blind too),
no log line. A single endpoint, fail-closed to zero, invisibly.

Same fixture style as test_memecoin_real_seams.py. `token_balance_raw` is patched per URL so the
custody read and the read-endpoint read can answer differently.
"""
from solders.keypair import Keypair

from sniper.analysis.memecoin_quote_failures import QuoteFailure
from sniper.execution import solana_real_seams as S
from sniper.settings import memecoin as cfg

CUSTODY = "https://custody"
READ = "https://read"


def _executor(http=lambda *_a, **_k: (200, {}, None)):
    return S.build_real_solana_executor(keypair=Keypair(), rpc_url=CUSTODY, http=http,
                                        usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)


def test_an_infra_failure_on_custody_prices_the_bag_off_the_read_endpoint(monkeypatch):
    """THE REGRESSION. Custody says nothing (empty observation -> infra class); the read endpoint
    holds the balance; Jupiter is asked and the preview succeeds, labelled as failed over."""
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_READ_RPC_URL", READ)
    urls = []

    def balance(pubkey, mint, url, http=None, ata_fallback=False):
        urls.append(url)
        return 0 if url == CUSTODY else 777
    monkeypatch.setattr(S, "token_balance_raw", balance)
    monkeypatch.setattr(S, "_sleep", lambda _s: None)
    asked = {}
    monkeypatch.setattr(S, "jupiter_exit_quote",
                        lambda mint, atomic, slip, http=None: asked.update(atomic=atomic) or {"outAmount": "5000000"})
    res = _executor()["preview_exit_fn"]({"id": 55147, "mint": "MintX"})
    assert res.preview is not None and res.failure is None
    assert res.quote_evidence["venue"] == "jupiter"
    assert res.quote_evidence["input_atomic"] == "777"
    assert res.quote_evidence["slippage_bps"] == int(round(cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER[-1] * 100))
    assert asked["atomic"] == 777, "the read endpoint's balance sized the quote"
    assert res.provider_id == "solana:exact_quote(read_balance)"
    # An empty observation classes as MALFORMED_RESPONSE, which is not in the custody retry set
    # (RPC_TRANSPORT / PROVIDER_TIMEOUT), so custody is read once and the read endpoint next.
    assert urls == [CUSTODY, READ]


def test_an_account_that_truly_holds_nothing_never_fails_over(monkeypatch):
    """TOKEN_REJECTED is a fact about the bag, not the provider. The read endpoint is not asked."""
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_READ_RPC_URL", READ)
    urls = []

    def http(method, url, headers=None, body=None):
        import json
        urls.append(url)
        return 200, {"result": {"value": [{"pubkey": "account", "account": {
            "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {
                "type": "account", "info": {"owner": json.loads(body)["params"][0],
                "mint": "MintX", "tokenAmount": {"amount": "0", "decimals": 6}}}}}}]}}, None
    res = _executor(http=http)["preview_exit_fn"]({"id": 1, "mint": "MintX"})
    assert res.failure is QuoteFailure.TOKEN_REJECTED
    assert READ not in urls


def test_an_empty_owner_index_is_infra_and_never_token_rejection(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_READ_RPC_URL", READ)
    urls = []
    def http(method, url, headers=None, body=None):
        urls.append(url)
        return 200, {"result": {"value": []}}, None
    res = _executor(http=http)["preview_exit_fn"]({"id": 1, "mint": "MintX"})
    assert res.failure is QuoteFailure.MALFORMED_RESPONSE
    assert urls == [CUSTODY, READ]


def test_with_no_read_endpoint_configured_behaviour_is_unchanged(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_READ_RPC_URL", "")
    monkeypatch.setattr(S, "token_balance_raw", lambda *_a, **_k: 0)
    monkeypatch.setattr(S, "_sleep", lambda _s: None)
    res = _executor()["preview_exit_fn"]({"id": 2, "mint": "MintX"})
    assert res.failure is not None and res.provider_id == "solana:balance_rpc"
