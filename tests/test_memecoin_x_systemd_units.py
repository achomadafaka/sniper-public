from __future__ import annotations

import configparser
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"
EXPECTED = {
    "sniper-memecoin-x-receiver.service",
    "sniper-memecoin-x-correlator.service",
    "sniper-memecoin-x-correlator.timer",
    "sniper-memecoin-x-signals.service",
    "sniper-memecoin-x-signals.timer",
    "sniper-x-intel-capture.service",
    "sniper-x-intel-notifier.service",
    "sniper-x-intel-notifier.timer",
}


def _parse(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    assert parser.read(SYSTEMD / name), f"missing unit: {name}"
    return parser


def test_all_core_units_exist_and_exclude_execution_commands():
    for name in EXPECTED:
        body = (SYSTEMD / name).read_text().lower()
        forbidden = ["real_trader", "memecoin_runner", "copy_trader", "funded", "--real",
                     "provider-factory"]
        if name not in {"sniper-x-intel-notifier.service", "sniper-x-intel-notifier.timer"}:
            forbidden.append("telegram")
        assert not any(value in body for value in forbidden), name
        if name not in {"sniper-x-intel-capture.service", "sniper-x-intel-notifier.service"}:
            assert "environmentfile=" not in body, name


def test_receiver_is_loopback_restartable_and_clock_explicit():
    unit = _parse("sniper-memecoin-x-receiver.service")
    service = unit["Service"]
    command = service["ExecStart"]
    assert service["Type"] == "simple"
    assert service["Restart"] == "on-failure"
    assert "scripts/run_memecoin_x_receiver.py" in command
    assert "--bind 127.0.0.1" in command
    assert "--db /home/bigpapi/Desktop/Sniper/data/memecoin_x.db" in command
    assert "--clock-offset-file /home/bigpapi/Desktop/Sniper/data/clock_offset.json" in command
    assert "chrome" not in command.lower()
    assert unit["Install"]["WantedBy"] == "default.target"


def test_receiver_cli_only_accepts_loopback_bind():
    help_result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_memecoin_x_receiver.py"), "--help"],
        text=True, capture_output=True, check=False,
    )
    assert help_result.returncode == 0
    assert "--bind {127.0.0.1}" in help_result.stdout


def test_correlator_and_signal_units_are_bounded_oneshots():
    correlator = _parse("sniper-memecoin-x-correlator.service")["Service"]
    signal = _parse("sniper-memecoin-x-signals.service")["Service"]
    assert correlator["Type"] == signal["Type"] == "oneshot"
    assert "scripts/run_memecoin_x_correlator.py" in correlator["ExecStart"]
    assert "--x-db /home/bigpapi/Desktop/Sniper/data/memecoin_x.db" in correlator["ExecStart"]
    assert "--fomo-db /home/bigpapi/Desktop/Sniper/sniper_tracker.db" in correlator["ExecStart"]
    assert "--once" in correlator["ExecStart"]
    assert "scripts/run_memecoin_x_signal_builder.py" in signal["ExecStart"]
    assert "--db /home/bigpapi/Desktop/Sniper/data/memecoin_x.db" in signal["ExecStart"]
    assert "--once" in signal["ExecStart"]
    assert "--clock-offset-file /home/bigpapi/Desktop/Sniper/data/clock_offset.json" in correlator["ExecStart"]
    assert "--clock-offset-file /home/bigpapi/Desktop/Sniper/data/clock_offset.json" in signal["ExecStart"]


def test_one_minute_timers_are_persistent_and_installable():
    for name, unit_name in (
        ("sniper-memecoin-x-correlator.timer", "sniper-memecoin-x-correlator.service"),
        ("sniper-memecoin-x-signals.timer", "sniper-memecoin-x-signals.service"),
    ):
        unit = _parse(name)
        timer = unit["Timer"]
        assert timer["OnUnitActiveSec"] == "60s"
        assert timer["Persistent"].lower() == "true"
        assert timer["Unit"] == unit_name
        assert unit["Install"]["WantedBy"] == "timers.target"


def test_installer_rewrites_root_and_requires_explicit_enable():
    body = (ROOT / "scripts/install_memecoin_x_services.sh").read_text()
    assert "systemctl --user daemon-reload" in body
    assert 's|/home/bigpapi/Desktop/Sniper|$ROOT_DIR|g' in body
    assert "--enable" in body
    assert "sniper-memecoin-x-receiver.service" in body
    assert "sniper-memecoin-x-correlator.timer" in body
    assert "sniper-memecoin-x-signals.timer" in body
    assert "sniper-memecoin-x-notification" not in body
    assert "sniper-memecoin-x-llm" not in body
    assert "if (( ENABLE == 1 ))" in body


def test_api_capture_unit_is_separate_default_off_and_secret_file_bound():
    unit = _parse("sniper-x-intel-capture.service")
    service = unit["Service"]
    assert "scripts/run_memecoin_x_api_capture.py" in service["ExecStart"]
    assert service["EnvironmentFile"] == "/home/bigpapi/Desktop/Sniper/.runtime/x-intel.env"
    assert service["Restart"] == "on-failure"
    body = (ROOT / "scripts/install_memecoin_x_services.sh").read_text()
    assert "--enable-api" in body
    assert "sniper-x-intel-capture.service" in body
    assert "sniper-x-intel-notifier.timer" in body
    assert "stat -c %a" in body


def test_installer_preflights_and_injects_a_working_runtime_python():
    body = (ROOT / "scripts/install_memecoin_x_services.sh").read_text()
    assert "MEMECOIN_X_PYTHON" in body
    assert "import flask, bs4" in body
    assert ".venv/bin/python|$PYTHON_BIN" in body

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install_memecoin_x_services.sh"), "--check"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Runtime Python OK:" in result.stdout
