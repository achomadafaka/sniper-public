from pathlib import Path


def test_web_watchdog_has_one_systemd_owner_and_no_direct_python_fallback():
    source = Path("scripts/sniper_web_watchdog.sh").read_text(encoding="utf-8")

    assert "SERVICE=sniper-web.service" in source
    assert 'systemctl is-active "$SERVICE"' in source
    assert 'systemctl start "$SERVICE"' in source
    assert "nohup" not in source
    assert ' -m sniper.web.server' not in source
