"""Desk entry points refuse a state root inside a pinned release (Gamma, 2026-10-21): without
SNIPER_STATE_ROOT, state_root() is the source tree, which inside .runtime/ is the release itself."""
import importlib.util
from pathlib import Path

import pytest

from sniper.util import state_paths

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ["scripts/memecoin_desk_assembler.py", "scripts/memecoin_desk_checker.py",
           "scripts/memecoin_desk_cluster_worker.py", "scripts/memecoin_desk_crawler.py",
           "scripts/memecoin_desk_trending.py", "scripts/memecoin_desk_dexscreener.py",
           "scripts/memecoin_desk_narrative.py",
           "scripts/memecoin_desk_judge_worker.py", "scripts/memecoin_desk_postmortem.py",
           "scripts/memecoin_desk_provision_cohort.py", "scripts/memecoin_desk_ranker_worker.py",
           "scripts/memecoin_desk_risk_shadow.py", "scripts/memecoin_desk_source_scout.py",
           "scripts/memecoin_desk_universe_worker.py", "scripts/memecoin_desk_x_producer.py",
           "scripts/research/desk_u_ranker_labels.py", "scripts/memecoin_desk_offchain.py",
           "scripts/memecoin_fee_reconciler.py"]


def test_the_guard_refuses_a_root_inside_a_release_and_names_it(tmp_path):
    release = tmp_path / ".runtime" / "memecoin" / "releases" / "abc"
    with pytest.raises(SystemExit, match="inside a pinned release"):
        state_paths.require_state_root_outside_release({"SNIPER_STATE_ROOT": str(release)})
    assert state_paths.require_state_root_outside_release({"SNIPER_STATE_ROOT": str(tmp_path)}) == tmp_path.resolve()


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_desk_entry_point_refuses_before_reading_anything(script, tmp_path, monkeypatch):
    release = tmp_path / ".runtime" / "memecoin" / "releases" / "abc"
    release.mkdir(parents=True)
    monkeypatch.delenv("SNIPER_STATE_ROOT", raising=False)
    monkeypatch.setattr(state_paths, "SOURCE_ROOT", release)          # as if run from the release
    spec = importlib.util.spec_from_file_location("desk_entry_%s" % Path(script).stem, ROOT / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(SystemExit, match="inside a pinned release"):
        try:
            mod.main([])
        except TypeError:
            mod.main()
