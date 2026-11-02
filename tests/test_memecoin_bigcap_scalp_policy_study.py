import sqlite3

import numpy as np

from scripts.research import memecoin_bigcap_scalp_policy_study as study


def test_mcap_bands_use_signal_time_boundaries():
    assert study.band_of(999_999) == "<1M"
    assert study.band_of(1_000_000) == "1-5M"
    assert study.band_of(5_000_000) == "5-30M"
    assert study.band_of(30_000_000) == ">=30M"


def test_grid_fills_at_next_mark_after_trigger():
    returns, open_end = study.simulate_grid(
        1.0,
        np.array([0.0, 60.0, 120.0]),
        np.array([1.0, 1.20, 1.10]),
        haircut=0.0,
    )
    index = study.POLICY_KEYS.index((15, -50, None, "off"))
    assert abs(returns.reshape(-1)[index] - 0.10) < 1e-12
    assert not open_end.reshape(-1)[index]


def test_position_cohort_freezes_band_from_earliest_signal_time_mcap():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE memecoin_positions("
        "id INTEGER, mint TEXT, network_id TEXT, entry_price REAL, opened_at TEXT, closed_at TEXT, "
        "is_open INTEGER, strategy TEXT, source_user_id TEXT, origin_event_id TEXT, execution_mode TEXT)",
    )
    conn.execute(
        "CREATE TABLE memecoin_events("
        "fomo_event_id TEXT, mint TEXT, created_at TEXT, their_market_cap REAL)",
    )
    conn.executemany(
        "INSERT INTO memecoin_events VALUES(?,?,?,?)",
        [
            ("e1", "M", "2026-07-14T10:00:00+00:00", 40_000_000.0),
            ("e2", "M", "2026-07-14T11:00:00+00:00", 900_000.0),
        ],
    )
    conn.executemany(
        "INSERT INTO memecoin_positions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "M", "solana", 1.0, "2026-07-14T10:00:01+00:00", None, 1,
             "control", "u1", "e1", "paper"),
            (2, "M", "solana", 1.0, "2026-07-14T11:00:01+00:00", None, 1,
             "control", "u2", "e2", "paper"),
        ],
    )
    frame = study.load_positions_with_band(conn)
    assert set(frame["mcap_band"]) == {">=30M"}


def test_path_builder_rejects_quote_unit_glitch():
    import pandas as pd
    positions = pd.DataFrame([{
        "pos_id": 1, "mint": "M", "entry_price": 1.0, "opened_epoch": 1000.0,
        "closed_at": None, "mcap_band": ">=30M",
    }])
    marks = {"M": (
        np.arange(1001.0, 1007.0),
        np.array([1.0, 1.1, 1.2, 1.3, 1.4, 25.0]),
    )}
    paths, diagnostics = study.build_position_paths(positions, marks)
    assert paths.empty
    assert diagnostics["sanity_cap_dropped"] == 1


def test_mint_entry_selection_is_structural_before_policy_simulation():
    import pandas as pd
    positions = pd.DataFrame([
        {"mint": "M", "opened_epoch": 2000.0, "pos_id": 2, "mcap_band": "<1M"},
        {"mint": "M", "opened_epoch": 1000.0, "pos_id": 1, "mcap_band": ">=30M"},
        {"mint": "N", "opened_epoch": 1500.0, "pos_id": 3, "mcap_band": "1-5M"},
    ])
    entries = study.select_mint_entries(positions)
    assert list(entries["pos_id"]) == [1, 3]
    assert list(entries["mcap_band"]) == [">=30M", "1-5M"]


def test_path_builder_uses_fixed_horizon_not_the_existing_arms_close_time():
    import pandas as pd
    opened = study._iso_to_epoch("2026-07-14T10:00:00+00:00")
    positions = pd.DataFrame([{
        "pos_id": 1, "mint": "M", "entry_price": 1.0, "opened_epoch": opened,
        "closed_at": "2026-07-14T10:00:02+00:00", "mcap_band": ">=30M",
    }])
    marks = {"M": (
        opened + np.arange(1.0, 7.0),
        np.array([1.0, 1.01, 1.02, 1.03, 1.04, 1.05]),
    )}
    paths, diagnostics = study.build_position_paths(positions, marks, horizon_s=10.0)
    assert diagnostics["kept"] == 1
    assert paths.iloc[0]["n_marks"] == 6
