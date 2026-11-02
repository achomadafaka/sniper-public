from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

# Whole-file skip when node isn't on PATH — these tests evaluate a real
# JS source via `node -e` and have no Python-only fallback. Without the
# skip, missing node → subprocess.run blocks forever → xdist worker hangs
# at 99% completion. Codex 2026-05-27 hit this on a runner without node.
pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node CLI not on PATH; world-map JS evaluation tests skipped",
)


ROOT = Path(__file__).resolve().parent.parent
WORLD_MAP_JS = ROOT / "static" / "world-map.js"
APP_JS = ROOT / "static" / "app.js"


def _run_world_map_js(expression: str):
    script = f"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync({json.dumps(str(WORLD_MAP_JS))}, "utf8") + "\\n;globalThis.__wm = WorldMap;";
const ctx = {{
  console,
  setTimeout,
  clearTimeout,
  Date,
  window: {{ innerWidth: 1440 }},
  document: {{}},
  globalThis: null,
}};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
const result = ({expression});
console.log(JSON.stringify(result));
"""
    # 30s ceiling — defensive against a runaway JS expression. node itself
    # is fast; each test takes well under 1s. Without this, a hung node
    # invocation would block its xdist worker indefinitely.
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(proc.stdout)


def test_compute_label_layout_prioritizes_open_idea_and_caps_dense_cluster():
    result = _run_world_map_js(
        """
(() => {
  const helper = ctx.__wm.__test__.computeLabelLayout;
  const openIdeaSlugs = new Set(["open-idea"]);
  const pinData = [
    { slug: "plain-a", title: "Plain A", px: 100, py: 100, _ns: "active", _nt: "stable", connected_events: [] },
    { slug: "plain-b", title: "Plain B", px: 101, py: 100, _ns: "active", _nt: "stable", connected_events: [] },
    { slug: "plain-c", title: "Plain C", px: 99, py: 101, _ns: "active", _nt: "stable", connected_events: [] },
    { slug: "plain-d", title: "Plain D", px: 102, py: 98, _ns: "active", _nt: "stable", connected_events: [] },
    { slug: "open-idea", title: "Open Idea Event", px: 100, py: 99, _ns: "active", _nt: "escalating", connected_events: ["x"] },
  ];
  return helper(pinData, { width: 800, isMobile: false, zoomK: 1, openIdeaSlugs, nowMs: Date.UTC(2026, 2, 27) });
})()
"""
    )
    assert len(result) == 1
    assert result[0]["slug"] == "open-idea"
    assert result[0]["_labelText"].endswith("+4")


def test_compute_clusters_collapses_dense_hotspot():
    result = _run_world_map_js(
        """
(() => {
  const helper = ctx.__wm.__test__.computeClusters;
  const pinData = [
    { slug: "a", title: "Alpha", px: 100, py: 100, _ns: "active", _nt: "stable", connected_events: [] },
    { slug: "b", title: "Bravo", px: 106, py: 101, _ns: "active", _nt: "stable", connected_events: [] },
    { slug: "c", title: "Charlie", px: 111, py: 103, _ns: "active", _nt: "escalating", connected_events: [] },
    { slug: "d", title: "Delta", px: 300, py: 120, _ns: "active", _nt: "stable", connected_events: [] },
  ];
  return helper(pinData, { zoomK: 1, isMobile: false, openIdeaSlugs: new Set(), nowMs: Date.UTC(2026, 2, 27) });
})()
"""
    )
    assert len(result) == 2
    hotspot = next(item for item in result if item["_clusterCount"] == 3)
    assert hotspot["title"] == "Charlie"
    assert hotspot["_clusterEvents"][0]["slug"] == "c"


def test_compute_clusters_breaks_apart_with_zoom():
    result = _run_world_map_js(
        """
(() => {
  const helper = ctx.__wm.__test__.computeClusters;
  const pinData = [
    { slug: "a", title: "Alpha", px: 100, py: 100, _ns: "active", _nt: "stable", connected_events: [] },
    { slug: "b", title: "Bravo", px: 112, py: 100, _ns: "active", _nt: "stable", connected_events: [] },
    { slug: "c", title: "Charlie", px: 124, py: 100, _ns: "active", _nt: "stable", connected_events: [] },
  ];
  const lowZoom = helper(pinData, { zoomK: 1, isMobile: false, openIdeaSlugs: new Set(), nowMs: Date.UTC(2026, 2, 27) });
  const highZoom = helper(pinData, { zoomK: 3.8, isMobile: false, openIdeaSlugs: new Set(), nowMs: Date.UTC(2026, 2, 27) });
  return { lowZoom: lowZoom.length, highZoom: highZoom.length };
})()
"""
    )
    assert result["lowZoom"] == 1
    assert result["highZoom"] == 3


def test_compute_label_layout_is_sparser_on_mobile():
    result = _run_world_map_js(
        """
(() => {
  const helper = ctx.__wm.__test__.computeLabelLayout;
  const pinData = Array.from({ length: 12 }, (_, i) => ({
    slug: `event-${i}`,
    title: `Event ${i}`,
    px: 100 + (i % 4) * 18,
    py: 100 + Math.floor(i / 4) * 14,
    _ns: "active",
    _nt: i % 3 === 0 ? "escalating" : "stable",
    connected_events: [],
  }));
  const desktop = helper(pinData, { width: 900, isMobile: false, zoomK: 1, openIdeaSlugs: new Set(), nowMs: Date.UTC(2026, 2, 27) });
  const mobile = helper(pinData, { width: 390, isMobile: true, zoomK: 1, openIdeaSlugs: new Set(), nowMs: Date.UTC(2026, 2, 27) });
  return { desktop: desktop.length, mobile: mobile.length };
})()
"""
    )
    assert result["mobile"] < result["desktop"]


def test_build_focus_transform_zooms_to_cluster():
    result = _run_world_map_js(
        """
(() => {
  const helper = ctx.__wm.__test__.buildFocusTransform;
  return helper([
    { px: 300, py: 120 },
    { px: 320, py: 130 },
    { px: 310, py: 145 },
  ], 900, 420, { padding: 30, maxScale: 5, minScale: 1, singlePointScale: 2.8 });
})()
"""
    )
    assert result["k"] > 1
    assert isinstance(result["x"], (int, float))
    assert isinstance(result["y"], (int, float))


def test_app_passes_filtered_events_to_world_map():
    src = APP_JS.read_text()
    assert "WorldMap.setEvents(filtered" in src
    assert "view: (this._eventStatusFilter !== 'all' || this._eventRegionFilter !== 'all') ? 'fit' : 'reset'" in src
