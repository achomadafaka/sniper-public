from pathlib import Path


APP = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()


def test_shell_alert_chip_uses_browser_safe_system_stats_payload():
    shell = APP.split("const shellCluster =", 1)[1].split(
        "// WEBSOCKET", 1
    )[0]

    assert "sv.pending_alert_count" in shell
    assert "/api/agent/alerts" not in shell


def test_shell_telegram_chip_uses_memecoin_status_not_legacy_alerter():
    shell = APP.split("const shellCluster =", 1)[1].split(
        "// WEBSOCKET", 1
    )[0]

    assert "sv.memecoin_telegram" in shell
    assert "hd.alerter.status === 'ok'" not in shell
    assert "const healthFailing = hardFail" in shell
