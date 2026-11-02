"""Configured direct V3 deployments on Robinhood chain 4663."""
import importlib


def test_extra_v3_deployment_keeps_the_ramses_abi_kind(monkeypatch):
    from sniper.settings import memecoin as cfg

    entry = "0xquoter:0xrouter:ramses_v3"
    monkeypatch.setenv("MEMECOIN_EVM_V3_EXTRA_DEPLOYMENTS_4663", entry)
    try:
        importlib.reload(cfg)
        assert cfg.MEMECOIN_EVM_V3_EXTRA_DEPLOYMENTS["4663"] == (
            {"quoter": "0xquoter", "router": "0xrouter", "protocol": "ramses_v3"},)
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)
