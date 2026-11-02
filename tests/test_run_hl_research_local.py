import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.run_hl_research_local import build_step_commands, load_dotenv, parse_steps, reset_db_files


class TestRunHLResearchLocal(unittest.TestCase):
    def test_parse_steps_all(self):
        self.assertEqual(
            parse_steps("all"),
            ["funding", "price", "oi", "dataset", "v2", "combo", "regression"],
        )

    def test_parse_steps_subset(self):
        self.assertEqual(parse_steps("oi,dataset,v2"), ["oi", "dataset", "v2"])

    def test_parse_steps_rejects_unknown(self):
        with self.assertRaises(ValueError):
            parse_steps("funding,nope")

    def test_build_step_commands_uses_dedicated_db_and_require_coinalyze(self):
        args = SimpleNamespace(
            db="data/hl_research.db",
            output_dir="output/hl_research_local",
            months=6,
            assets="BTC,ETH,SOL",
            steps="oi,v2,combo",
            require_coinalyze=True,
        )
        steps = build_step_commands(args)

        self.assertEqual([step.name for step in steps], ["oi", "v2", "combo"])
        self.assertIn("--require-coinalyze", steps[0].cmd)
        self.assertIn("--db", steps[1].cmd)
        self.assertIn("signal_research_results.json", steps[2].cmd[-1])

    def test_reset_db_files_removes_sidecars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = os.path.join(tmpdir, "hl_research.db")
            for suffix in ("", "-wal", "-shm"):
                with open(base + suffix, "w", encoding="utf-8") as handle:
                    handle.write("x")

            reset_db_files(os.path.abspath(base))

            for suffix in ("", "-wal", "-shm"):
                self.assertFalse(os.path.exists(base + suffix))

    def test_load_dotenv_sets_missing_keys_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env")
            with open(env_path, "w", encoding="utf-8") as handle:
                handle.write("FOO=bar\nBAZ=qux\n")

            old_foo = os.environ.get("FOO")
            old_baz = os.environ.get("BAZ")
            try:
                os.environ["FOO"] = "keep"
                os.environ.pop("BAZ", None)
                load_dotenv(Path(env_path))
                self.assertEqual(os.environ["FOO"], "keep")
                self.assertEqual(os.environ["BAZ"], "qux")
            finally:
                if old_foo is None:
                    os.environ.pop("FOO", None)
                else:
                    os.environ["FOO"] = old_foo
                if old_baz is None:
                    os.environ.pop("BAZ", None)
                else:
                    os.environ["BAZ"] = old_baz


if __name__ == "__main__":
    unittest.main()
