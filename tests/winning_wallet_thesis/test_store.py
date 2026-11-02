from sniper.analysis.winning_wallet_thesis.store import load_profiles, persist_profile
from sniper.analysis.winning_wallet_thesis.types import ThesisProfile


def test_persist_and_load_roundtrip(tmp_path):
    p = ThesisProfile(wallet="0xABC", run_id="r1", headline="h")
    path = persist_profile(p, store_dir=str(tmp_path))
    assert path.endswith("0xabc__r1.json")          # wallet lowercased + run_id
    loaded = load_profiles(store_dir=str(tmp_path))
    assert len(loaded) == 1 and loaded[0]["wallet"] == "0xABC"


def test_distinct_run_ids_accumulate(tmp_path):
    persist_profile(ThesisProfile("0xw", "r1", "h"), store_dir=str(tmp_path))
    persist_profile(ThesisProfile("0xw", "r2", "h"), store_dir=str(tmp_path))
    persist_profile(ThesisProfile("0xw", "r1", "h"), store_dir=str(tmp_path))  # same run_id overwrites
    assert len(load_profiles(store_dir=str(tmp_path))) == 2
