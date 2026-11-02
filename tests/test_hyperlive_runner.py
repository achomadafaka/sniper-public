import json
import os

import scripts.run_hyperlive_engine as CLI
from sniper.hyperlive import persistence as P


def test_fixture_mode_runs_full_cycles_and_persists(tmp_path):
    db = os.path.join(str(tmp_path), "hl.sqlite")
    assert CLI.main(["--fixture-mode", "--db-path", db]) == 0

    conn = P.connect(db)
    cycles = conn.execute("SELECT COUNT(*) FROM hyperlive_cycles").fetchone()[0]
    state = P.load_state(conn)
    assert cycles >= 1                       # at least one full trade cycle completed
    assert state is not None and state["state"] in (
        "SCANNING", "SELECTING", "DESIGNING", "ARMED", "MANAGING", "CLOSING", "EVALUATING")
    won = conn.execute("SELECT COUNT(*) FROM hyperlive_cycles WHERE won=1").fetchone()[0]
    assert won >= 1                          # the favorable fixture cascade is captured
