import sys

from sniper.analysis.winning_wallet_thesis import cli as C
from sniper.analysis.winning_wallet_thesis.types import ThesisProfile


WALLET = "0x1111111111111111111111111111111111111111"


def test_cli_save_persists_queryable_analyzer_store(monkeypatch, tmp_path):
    calls = {}

    def fake_run_extractor(wallet, **kwargs):
        calls["wallet"] = wallet
        calls["kwargs"] = kwargs
        return ThesisProfile(wallet=wallet, run_id="run1", headline="h")

    from sniper.analysis.winning_wallet_thesis import store

    monkeypatch.setattr(sys, "argv", ["run_winning_wallet_thesis.py", WALLET])
    monkeypatch.setattr(C, "run_extractor", fake_run_extractor)
    monkeypatch.setattr(store, "persist_profile", lambda profile: str(tmp_path / "profile.json"))

    C.main()

    assert calls["wallet"] == WALLET
    assert calls["kwargs"]["persist_store"] is True


def test_cli_no_save_skips_queryable_analyzer_store(monkeypatch):
    calls = {}

    def fake_run_extractor(wallet, **kwargs):
        calls["kwargs"] = kwargs
        return ThesisProfile(wallet=wallet, run_id="run1", headline="h")

    monkeypatch.setattr(sys, "argv", ["run_winning_wallet_thesis.py", "--no-save", WALLET])
    monkeypatch.setattr(C, "run_extractor", fake_run_extractor)

    C.main()

    assert calls["kwargs"]["persist_store"] is False
