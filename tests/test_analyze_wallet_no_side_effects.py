"""Core analyzer purity guard: no filesystem writes from analyze_wallet."""

from __future__ import annotations

import os

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.types import WalletData


def test_analyze_wallet_no_filesystem_writes_when_hook_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(os.listdir(tmp_path))
    data = WalletData(
        wallet="0x" + "a" * 40,
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1_700_000_000,
    )

    report = analyze_wallet(data.wallet, pre_fetched_data=data)

    assert report.wallet == data.wallet
    assert set(os.listdir(tmp_path)) == before
