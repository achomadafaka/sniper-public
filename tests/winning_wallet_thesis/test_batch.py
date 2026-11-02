from sniper.analysis.winning_wallet_thesis.batch import InMemoryProfileStore, run_wallet_batch
from sniper.analysis.winning_wallet_thesis.intake import CandidateWallet


def test_batch_skips_done_wallets_on_rerun():
    calls = []

    def run(wallet):
        calls.append(wallet)
        return {"wallet": wallet, "headline": "ok"}

    store = InMemoryProfileStore()
    candidates = [CandidateWallet("0x1", "curated", ""), CandidateWallet("0x2", "curated", "")]
    run_wallet_batch(candidates, run_profile=run, store=store)
    run_wallet_batch(candidates, run_profile=run, store=store)
    assert calls == ["0x1", "0x2"]                 # second run skips both (idempotent)


def test_batch_records_failure_and_continues():
    def run(wallet):
        if wallet == "0x2":
            raise RuntimeError("boom")
        return {"wallet": wallet}

    store = InMemoryProfileStore()
    cands = [CandidateWallet("0x1", "curated", ""), CandidateWallet("0x2", "curated", ""),
             CandidateWallet("0x3", "curated", "")]
    counts = run_wallet_batch(cands, run_profile=run, store=store)
    assert counts["done"] == 2 and counts["failed"] == 1
    assert store.statuses["0x2"].startswith("failed:")


def test_batch_stamps_intake_provenance():
    store = InMemoryProfileStore()
    cands = [CandidateWallet("0x1", "curated", "SwissMiss", survivorship_risk="high")]
    run_wallet_batch(cands, run_profile=lambda w: {"wallet": w}, store=store)
    assert store.profiles["0x1"]["intake"]["survivorship_risk"] == "high"
