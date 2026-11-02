"""The V6 quant engine was retired on 2026-08-30 and its dashboard section removed on 2026-09-05
at the operator's instruction (916d9cc0, 027e93e2). This file used to pin that the V6 panel did
not describe the radar as its input; with the panel gone the truthful pin is its absence, so a
future change cannot quietly bring the section back."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 2026-08-30: part of the V6 set moved to archive/legacy/memecoin-v6-2026-08-30. Resolve each
# subject wherever it now lives rather than hardcoding one root.
_ARCHIVE = ROOT / "archive/legacy/memecoin-v6-2026-08-30"


def _subject(rel):
    live = ROOT / rel
    if live.exists():
        return live
    archived = _ARCHIVE / rel
    if archived.exists():
        return archived
    raise AssertionError(f"guard subject vanished from both roots: {rel}")


def test_v6_runtime_has_no_radar_receipt_consumer():
    files = tuple(_subject(r) for r in (
        "scripts/memecoin_v6_paper_worker.py",
        "sniper/services/memecoin_v6_paper_runner.py",
        "sniper/settings/memecoin_v6_paper.py",
        "sniper/settings/memecoin.py",
        "sniper/routes/api_memecoin.py",
    ))
    text = "\n".join(path.read_text() for path in files)
    for rejected in (
        "consume_radar_receipts",
        "memecoin_v6_radar_signals",
        "MEMECOIN_V6_RADAR_",
        "radar_conn",
    ):
        assert rejected not in text
    assert not (ROOT / "sniper/services/memecoin_v6_radar_signals.py").exists()


def test_the_dashboard_has_no_v6_panel():
    src = (ROOT / "static" / "app.js").read_text()
    assert "V6 PAPER SECTION" not in src
    assert "mc-v6-paper" not in src
    assert "_loadV6Paper" not in src


def test_the_stylesheet_has_no_v6_cockpit_rules():
    css = (ROOT / "static" / "styles.css").read_text()
    assert ".v6-" not in css and "#mc-v6" not in css
