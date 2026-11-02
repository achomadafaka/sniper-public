"""Tests for replay engine API endpoints."""
import importlib
import json
import os
import sys
import tempfile
import unittest


class TestReplayRunEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(cls._tmp.name, "test_replay.db")
        # Reimport only the server, which owns the DB connection. Route blueprints
        # receive the new connection through ctx. Removing route modules leaves stale
        # function objects in other test modules and breaks later monkeypatches.
        sys.modules.pop("sniper.web.server", None)
        cls.web = importlib.import_module("sniper.web.server")
        cls.client = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _seed_signal_and_outcome(self):
        """Insert minimal signal_history + market_outcomes rows for replay."""
        conn = self.web.conn
        conn.execute(
            """INSERT OR IGNORE INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES ('0xA', 'c1', 'Yes', 'Test', 0, 0.40, 100, 0.40,
                       70, 20, 'insider_candidate', 'holding', 10, 1000, 1000)""",
        )
        conn.execute(
            """INSERT OR IGNORE INTO market_outcomes
               (condition_id, outcome, resolved_price, resolved_at)
               VALUES ('c1', 'yes', 1.0, 2000)""",
        )
        conn.commit()

    def setUp(self):
        self._seed_signal_and_outcome()

    def test_basic_replay_run(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": 1000, "min_insider_score": 55,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("summary", data)
        self.assertIn("trades", data)
        self.assertIn("equity_curve", data)
        self.assertIn("run_id", data)
        self.assertGreaterEqual(data["summary"]["total_trades"], 1)

    def test_replay_run_with_save(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": 1000,
            "save_as": "test-run-config",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("run_id", data)

    def test_replay_run_invalid_bankroll(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": -100,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_replay_run_non_numeric_bankroll(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": "not_a_number",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_replay_run_invalid_config_type(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": 1000,
            "max_concurrent_positions": "not_a_number",
        })
        self.assertEqual(resp.status_code, 400)

    def test_replay_run_invalid_start_ts(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": 1000,
            "start_ts": "abc",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_replay_run_invalid_end_ts(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": 1000,
            "end_ts": "xyz",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_replay_run_rejects_inverted_date_range(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": 1000,
            "start_ts": 2000,
            "end_ts": 1000,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_replay_run_duration_ms_present(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": 1000,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("duration_ms", data)
        self.assertIsInstance(data["duration_ms"], int)

    def test_replay_run_rejects_unknown_strategy_type(self):
        resp = self.client.post("/api/replay/run", json={
            "strategy_type": "does_not_exist",
            "initial_bankroll": 1000,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Replay not supported", resp.get_json()["error"])

    def test_replay_run_rejects_weather_without_adapter(self):
        resp = self.client.post("/api/replay/run", json={
            "strategy_type": "weather_v1",
            "initial_bankroll": 1000,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Replay not supported", resp.get_json()["error"])


class TestReplayListEndpoints(TestReplayRunEndpoint):
    def test_list_configs(self):
        resp = self.client.get("/api/replay/configs")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), list)

    def test_list_configs_self_heals_missing_replay_tables(self):
        self.web.conn.execute("DROP TABLE IF EXISTS replay_runs")
        self.web.conn.execute("DROP TABLE IF EXISTS replay_configs")
        self.web.conn.commit()
        resp = self.client.get("/api/replay/configs")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_list_runs(self):
        resp = self.client.get("/api/replay/runs")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), list)

    def test_list_runs_self_heals_missing_replay_tables(self):
        self.web.conn.execute("DROP TABLE IF EXISTS replay_runs")
        self.web.conn.execute("DROP TABLE IF EXISTS replay_configs")
        self.web.conn.commit()
        resp = self.client.get("/api/replay/runs")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_list_runs_with_limit(self):
        resp = self.client.get("/api/replay/runs?limit=5")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), list)

    def test_get_run_not_found(self):
        resp = self.client.get("/api/replay/runs/99999")
        self.assertEqual(resp.status_code, 404)

    def test_get_run_exists(self):
        # First create a run
        run_resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": 1000,
        })
        self.assertEqual(run_resp.status_code, 200)
        run_id = run_resp.get_json()["run_id"]

        # Now fetch it
        resp = self.client.get(f"/api/replay/runs/{run_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["id"], run_id)


class TestReplayCompare(TestReplayRunEndpoint):
    def test_compare_two_runs(self):
        r1 = self.client.post("/api/replay/run", json={"initial_bankroll": 1000})
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post("/api/replay/run", json={"initial_bankroll": 2000})
        self.assertEqual(r2.status_code, 200)
        id1 = r1.get_json()["run_id"]
        id2 = r2.get_json()["run_id"]
        self.assertIsNotNone(id1)
        self.assertIsNotNone(id2)

        resp = self.client.get(f"/api/replay/compare?run_ids={id1},{id2}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("runs", data)
        self.assertEqual(len(data["runs"]), 2)
        self.assertIn("config_diff", data)
        # initial_bankroll differs (1000 vs 2000)
        self.assertIn("initial_bankroll", data["config_diff"])

    def test_compare_missing_runs(self):
        resp = self.client.get("/api/replay/compare?run_ids=99999,99998")
        self.assertEqual(resp.status_code, 404)

    def test_compare_too_few_ids(self):
        resp = self.client.get("/api/replay/compare?run_ids=1")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("at least 2", resp.get_json()["error"])

    def test_compare_invalid_ids(self):
        resp = self.client.get("/api/replay/compare?run_ids=abc,def")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("comma-separated integers", resp.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
