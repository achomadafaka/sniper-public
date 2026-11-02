"""One cohort = one band everywhere (operator 2026-10-23): producer and ranker read the active U cohort's frozen band."""
import json

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services.memecoin_desk_universe import cohort_band

ENV = {"MEMECOIN_DESK_UNIVERSE_BAND_LO_USD": "30000", "MEMECOIN_DESK_UNIVERSE_BAND_HI_USD": "100000"}


def db(tmp_path, hard_filter, producer="U"):
    path = tmp_path / "memecoin_desk.db"
    c = store.connect(path)
    store.register_cohort(c, cohort_id="cU", producer=producer, prompt_version="desk-judge-v23",
                          policy={"clip_usd": 25, "hard_filter": hard_filter}, created_at="2026-10-23T19:00:00+00:00")
    c.close()
    return path


def test_cohort_band_wins_and_flags_the_env_mismatch(tmp_path):
    got = cohort_band(db(tmp_path, {"mcap_band": [3960.0, 100000.0]}), "cU", env=ENV)
    assert got == {"band": (3960.0, 100000.0), "source": "cohort", "mismatch": True}


def test_cohort_without_frozen_band_is_the_default(tmp_path):
    assert cohort_band(db(tmp_path, {}), "cU", env=ENV) == {"band": (30000.0, 100000.0), "source": "cohort", "mismatch": False}


@pytest.mark.parametrize("cid", [None, ""])
def test_no_cohort_falls_back_to_env(tmp_path, cid):
    got = cohort_band(db(tmp_path, {"mcap_band": [3960.0, 100000.0]}), cid, env={**ENV, "MEMECOIN_DESK_UNIVERSE_BAND_LO_USD": "5000"})
    assert got == {"band": (5000.0, 100000.0), "source": "env", "mismatch": False}


def test_unreadable_or_missing_cohort_is_named_never_silent_env(tmp_path):
    got = cohort_band(tmp_path / "nope.db", "cU", env=ENV)
    assert got["source"] == "cohort_unreadable" and got["mismatch"] is None and got["band"] == (30000.0, 100000.0)
    assert cohort_band(db(tmp_path, {}), "missing", env=ENV)["source"] == "cohort_missing"


def test_malformed_frozen_band_raises(tmp_path):
    with pytest.raises(ValueError):
        cohort_band(db(tmp_path, {"mcap_band": [100000.0, 3960.0]}), "cU", env=ENV)


def test_ranker_worker_ranks_the_producers_band(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("ranker_worker_band", root / "scripts" / "memecoin_desk_ranker_worker.py")
    W = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(W)
    path = db(tmp_path, {"mcap_band": [3960.0, 100000.0]})
    monkeypatch.setattr("sniper.util.state_paths.state_path", lambda *parts: path)
    monkeypatch.setenv("MEMECOIN_DESK_U_COHORT_ID", "cU")
    W._BAND.clear()
    assert W.producer_band() == (3960.0, 100000.0)
