from sniper.settings.memecoin import _evm_archive_rpc_pool


def test_arc_archive_urls_are_read_per_chain():
    pool = _evm_archive_rpc_pool({
        "MEMECOIN_EVM_ARCHIVE_RPC_URLS_5042": "https://a.example, https://b.example,https://a.example",
        "MEMECOIN_EVM_ARCHIVE_RPC_URLS_4663": "https://c.example",
    })
    assert pool == {"5042": ("https://a.example", "https://b.example"), "4663": ("https://c.example",)}


def test_unset_chain_has_no_archive():
    assert _evm_archive_rpc_pool({}) == {}
