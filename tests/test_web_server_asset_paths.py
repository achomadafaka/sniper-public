import importlib
import sys
from pathlib import Path


def test_web_server_serves_index_and_static_assets(tmp_path, monkeypatch):
    # Hermetic DB path: get_db_path() defaults to the RELATIVE "sniper_tracker.db", so under
    # parallel xdist workers (different cwds) importing the server intermittently failed with
    # "unable to open database file". Pin an absolute, unique DB so this never races. (2026-06-17)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test_web.db"))
    sys.modules.pop("sniper.web.server", None)
    web = importlib.import_module("sniper.web.server")
    client = web.app.test_client()

    home = client.get("/")
    assert home.status_code == 200
    assert b"SNIPER" in home.data

    asset = client.get("/static/app.js")
    assert asset.status_code == 200
    assert b"VALID_TABS" in asset.data


def test_analyzer_position_status_counts_conversion_flow():
    src = Path("static/app.js").read_text()
    block = src[src.index("    _posStatus(p) {"):]
    block = block[:block.index("_posPnl(p)")]
    assert "convert_in_shares" in block
    assert "convert_out_shares" in block


def test_mobile_nav_no_longer_exposes_btc_algo_tab():
    # crypto5m maker fleet ARCHIVED 2026-07-08 (no realizable edge) — the "5m" mobile nav item is removed.
    html = Path("templates/index.html").read_text()
    mobile = html[html.index('id="mobile-bottom-nav"'):]
    mobile = mobile[:mobile.index("</nav>")]
    assert 'data-tab="algo"' not in mobile


def test_mobile_shell_declares_dark_standalone_browser_chrome():
    html = Path("templates/index.html").read_text()

    expected_meta = (
        '<meta name="color-scheme" content="dark">',
        '<meta name="theme-color" content="#000000">',
        '<meta name="mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">',
        '<meta name="apple-mobile-web-app-title" content="Sniper">',
    )
    for tag in expected_meta:
        assert html.count(tag) == 1


def test_asset_versions_are_mtime_not_hardcoded(tmp_path, monkeypatch):
    # Regression (2026-07-18): index.html hardcoded ?v=176 while app.js changed 4+ times, so
    # browsers served stale JS and UI edits "didn't take effect". Versions must derive from mtime.
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test_web.db"))
    sys.modules.pop("sniper.web.server", None)
    web = importlib.import_module("sniper.web.server")
    vers = web._asset_versions()
    assert set(vers) >= {"app_js", "styles", "mc_tokens", "world_map", "ticker_bar"}
    # a real static file resolves to its (large) mtime int, never the 1 fallback or a stale constant
    assert vers["app_js"] > 1_000_000_000
    html = web.app.test_client().get("/").get_data(as_text=True)
    assert f"app.js?v={vers['app_js']}" in html
    assert "app.js?v=176" not in html            # the stale hardcode is gone


def test_tp_display_shows_mcap_target_not_off(tmp_path):
    # Regression (2026-07-18): the /exit endpoint stores tp_pct=0 alongside tp_target_mcap
    # ("mcap IS the take-profit"), and _exitLegTxt checked pct first -> rendered every mcap TP
    # as "off", so operator mcap-TP changes looked like they never took effect. mcap must win.
    from pathlib import Path
    src = Path("static/app.js").read_text()
    block = src[src.index("    _exitLegTxt(eff, leg, dflt, floor, capBasis) {"):]
    block = block[:block.index("_tpDisplay(p) {")]
    i_mcap = block.index("txt = '@$' + this._mcapShort(mcap)")
    i_pct = block.index("(Number(pct) === 0) ? 'off'")
    assert i_mcap < i_pct, "mcap branch must be checked BEFORE the pct/off branch"
