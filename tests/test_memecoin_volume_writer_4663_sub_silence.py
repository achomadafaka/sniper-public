"""4663 re-opens a silent swap subscription after 30 s, not 120 s (2026-10-17, operator order via Alpha).

publicnode's free 4663 socket drops single swap subscriptions while heads and the other topic keep
flowing. The volume writer log held 1,339 per-subscription re-opens at the 120 s threshold
(10-13..10-17), each a hole of at least 120 s in the tape the rh fast lane reads, which is what the
tape_behind / bridge_tape_behind refusals measure. Healthy per-subscription silences top out at
2.1-4.0 s (Gamma, 2026-09-29), so 30 s keeps a 7x margin and cuts each hole by 90 s.

Kill knob: MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_4663=120 restores the old threshold on 4663 alone;
MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S=0 still disables the watch on every chain."""
import pytest

from sniper.services import memecoin_volume_writer as w
from sniper.settings import memecoin as cfg
from test_memecoin_volume_writer_v2 import _confirms, _harness, _head


def test_4663_defaults_to_30_s_and_other_chains_keep_the_global(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S", 120.0)
    monkeypatch.delenv("MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_4663", raising=False)
    monkeypatch.delenv("MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_8453", raising=False)
    assert cfg.onchain_sub_silence_s_for("4663") == 30.0
    assert cfg.onchain_sub_silence_s_for(4663) == 30.0, "an int chain id is the same chain"
    assert cfg.onchain_sub_silence_s_for("8453") == 120.0


def test_the_per_chain_knob_overrides_and_the_global_zero_still_disables(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S", 120.0)
    monkeypatch.setenv("MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_4663", "120")
    assert cfg.onchain_sub_silence_s_for("4663") == 120.0, "kill knob restores the old threshold"
    monkeypatch.setenv("MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_8453", "45")
    assert cfg.onchain_sub_silence_s_for("8453") == 45.0
    monkeypatch.delenv("MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_4663")
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S", 0.0)
    assert cfg.onchain_sub_silence_s_for("4663") == 0.0, "global 0 disables the built-in 4663 default"
    monkeypatch.setenv("MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_4663", "30")
    assert cfg.onchain_sub_silence_s_for("4663") == 30.0, "an explicit chain value beats global 0"


def test_a_malformed_per_chain_value_fails_loudly(monkeypatch):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_4663", "thirty")
    with pytest.raises(ValueError):
        cfg.onchain_sub_silence_s_for("4663")


@pytest.mark.parametrize("chain,expected", [("4663", 30.0), ("8453", 120.0)])
def test_the_writer_builds_its_watch_with_the_chain_threshold(tmp_path, monkeypatch, chain, expected):
    monkeypatch.delenv(f"MEMECOIN_ONCHAIN_M5_SUB_SILENCE_S_{chain}", raising=False)
    monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", "")
    built = []

    class Recording(w.SubscriptionWatch):
        def __init__(self, silence_s):
            built.append(silence_s)
            super().__init__(silence_s)

    monkeypatch.setattr(w, "SubscriptionWatch", Recording)
    ids = [rid for rid, _params in w.subscriptions(chain)]
    connect_fn, _sockets = _harness(
        monkeypatch, [[{"id": 1, "result": hex(int(chain))}] + _confirms(ids) + [_head(1, 1000)]])
    with pytest.raises(KeyboardInterrupt):
        w.run(chain, "wss://unit-test", path=tmp_path / "v.db", connect_fn=connect_fn)
    assert built == [expected]
