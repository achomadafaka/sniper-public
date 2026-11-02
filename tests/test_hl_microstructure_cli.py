import os
from pathlib import Path
import sys
from types import SimpleNamespace

import scripts.run_hl_microstructure_paper as CLI
from sniper.algos.hl_microstructure import persistence as P


def test_fixture_mode_records_live_paper_decisions_without_network(tmp_path):
    db = os.path.join(str(tmp_path), "hlm_fixture.sqlite")

    assert CLI.main(["--fixture-mode", "--db-path", db, "--duration-seconds", "1"]) == 0

    conn = P.connect(db)
    decisions = conn.execute("SELECT COUNT(*) FROM hlm_decisions").fetchone()[0]
    snapshots = conn.execute("SELECT COUNT(*) FROM hlm_feature_snapshots").fetchone()[0]
    assert decisions > 0
    assert snapshots > 0


def test_fixture_auto_select_records_radar_and_fusion_decisions(tmp_path):
    db = os.path.join(str(tmp_path), "hlm_auto.sqlite")

    assert CLI.main([
        "--fixture-mode",
        "--auto-select-assets",
        "--max-assets", "2",
        "--db-path", db,
        "--duration-seconds", "1",
    ]) == 0

    conn = P.connect(db)
    radar_assets = [r[0] for r in conn.execute(
        "SELECT asset FROM hlm_radar_snapshots ORDER BY rank"
    ).fetchall()]
    fusion = conn.execute(
        "SELECT COUNT(*) FROM hlm_decisions WHERE policy_id='fusion_radar'"
    ).fetchone()[0]
    experts = conn.execute(
        "SELECT COUNT(*) FROM hlm_expert_predictions"
    ).fetchone()[0]

    assert radar_assets[:2] == ["HOT", "BTC"]
    assert fusion > 0
    assert experts > 0


def test_fixture_auto_select_runs_gated_ab_arms(tmp_path):
    db = os.path.join(str(tmp_path), "hlm_ab.sqlite")

    assert CLI.main([
        "--fixture-mode", "--auto-select-assets", "--max-assets", "2",
        "--db-path", db, "--duration-seconds", "1",
    ]) == 0

    conn = P.connect(db)
    arm_ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT policy_id FROM hlm_decisions WHERE policy_id LIKE 'fusion_gated_%'"
    ).fetchall()}
    control = conn.execute(
        "SELECT COUNT(*) FROM hlm_decisions WHERE policy_id='fusion_radar'"
    ).fetchone()[0]
    assert "fusion_gated_b1" in arm_ids
    assert "fusion_gated_b2" in arm_ids
    assert control > 0  # control arm still recorded, unchanged


def test_no_gated_arms_flag_disables_ab(tmp_path):
    db = os.path.join(str(tmp_path), "hlm_no_ab.sqlite")

    assert CLI.main([
        "--fixture-mode", "--auto-select-assets", "--no-gated-arms",
        "--db-path", db, "--duration-seconds", "1",
    ]) == 0

    conn = P.connect(db)
    gated = conn.execute(
        "SELECT COUNT(*) FROM hlm_decisions WHERE policy_id LIKE 'fusion_gated_%'"
    ).fetchone()[0]
    assert gated == 0


def test_live_mode_exits_when_websocket_is_closed(monkeypatch, tmp_path):
    db = os.path.join(str(tmp_path), "hlm_closed.sqlite")

    class Closed(Exception):
        pass

    class FakeWebSocket:
        def send(self, _payload):
            return None

        def settimeout(self, _timeout):
            return None

        def recv(self):
            raise Closed("socket is already closed")

        def close(self):
            return None

    fake_websocket = SimpleNamespace(
        WebSocketConnectionClosedException=Closed,
        create_connection=lambda *_args, **_kwargs: FakeWebSocket(),
    )
    monkeypatch.setitem(sys.modules, "websocket", fake_websocket)

    assert CLI.main([
        "--db-path", db,
        "--assets", "BTC",
        "--duration-seconds", "1",
        "--decision-interval-ms", "1000",
    ]) == 2


def test_live_mode_exits_when_websocket_returns_empty_frame(monkeypatch, tmp_path):
    db = os.path.join(str(tmp_path), "hlm_empty_frame.sqlite")

    class FakeWebSocket:
        def send(self, _payload):
            return None

        def settimeout(self, _timeout):
            return None

        def recv(self):
            return ""

        def close(self):
            return None

    fake_websocket = SimpleNamespace(
        WebSocketConnectionClosedException=RuntimeError,
        create_connection=lambda *_args, **_kwargs: FakeWebSocket(),
    )
    monkeypatch.setitem(sys.modules, "websocket", fake_websocket)

    assert CLI.main([
        "--db-path", db,
        "--assets", "BTC",
        "--duration-seconds", "1",
        "--decision-interval-ms", "10000000",
    ]) == 2


def test_hl_microstructure_watchdog_restarts_live_paper_runner():
    script = Path("scripts/hl_microstructure_watchdog.sh")

    assert script.exists()
    body = script.read_text()
    assert "data/hl_microstructure_paper.pid" in body
    assert "run_hl_microstructure_paper.py --db-path data/hl_microstructure_paper.sqlite" in body
    assert "--auto-select-assets" in body
    assert "hl_microstructure_paper_watchdog.log" in body
