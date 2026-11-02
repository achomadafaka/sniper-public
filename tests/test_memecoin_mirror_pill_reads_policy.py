"""The mirror pill must report the POLICY, not the operator override alone.

Live 2026-08-19, funded position 40611 (VPN, `small` band, `follow-30-50-v1`): the card printed
`mirror ON` while the position's frozen contract carried `mirror_on_sell = False`. The pill read
`p.mirror_disabled` — an operator override that can only switch a mirror the policy actually has —
so it asserted the position would follow the trader out when its contract says it never will.
`follow-30-50-v1` having no mirror is the whole reason it holds past the trader's exit.

Same class as the mark-age defect fixed earlier that day: the UI asserting behaviour the engine
does not have.

Precedence:
  policy mirror_on_sell false  -> "no mirror · policy", not toggleable
  policy has a mirror + override off -> "mirror OFF · holding" (toggleable)
  policy has a mirror + override on  -> "mirror ON" (toggleable)
  legacy row, no contract            -> override-only reading, unchanged
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path(__file__).resolve().parents[1] / "static" / "app.js"
SRC = APP.read_text()


def _pill(position):
    """Evaluate the shipped mirrorPill expression under Node for one position row."""
    node = shutil.which("node")
    assert node is not None, (
        "node is required: it evaluates the shipped mirrorPill expression, the only behavioural "
        "test that the pill follows the frozen policy"
    )
    start = SRC.index("      const effPolicy = p.effective_exit_policy;")
    end = SRC.index("      const chg1h = p.change_1h_pct;")
    snippet = SRC[start:end]
    script = (
        "const esc = s => String(s);\n"
        f"const p = {json.dumps(position)};\n"
        "const tick = 'T';\n"
        + snippet
        + "\nconsole.log(JSON.stringify(mirrorPill));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=60, check=True)
    return json.loads(out.stdout.strip())


def test_a_policy_without_a_mirror_never_claims_mirror_on():
    """The VPN regression: follow-30-50-v1 carries mirror_on_sell false."""
    html = _pill({"id": 40611, "mirror_disabled": 0,
                  "effective_exit_policy": {"policy_id": "follow-30-50-v1",
                                            "mirror_on_sell": False}})
    assert "Mirror on" not in html   # label is "Mirror on" since 0436984a
    assert "no mirror" in html
    assert "follow-30-50-v1" in html          # names the policy responsible


def test_a_policy_without_a_mirror_is_not_offered_as_a_toggle():
    """Clicking would write `mirror_disabled` and change nothing — do not invite it."""
    html = _pill({"id": 1, "mirror_disabled": 0,
                  "effective_exit_policy": {"policy_id": "follow-30-50-v1",
                                            "mirror_on_sell": False}})
    assert "toggle-mirror" not in html
    assert "cursor:pointer" not in html


@pytest.mark.parametrize("disabled,expected", [(0, "Mirror on"), (1, "Mirror off")])
def test_a_policy_with_a_mirror_still_reports_the_operator_override(disabled, expected):
    """combo_f / funded-*-v2 DO mirror, so the override is meaningful and stays toggleable."""
    html = _pill({"id": 2, "mirror_disabled": disabled,
                  "effective_exit_policy": {"policy_id": "funded-micro-v2",
                                            "mirror_on_sell": True}})
    assert expected in html
    assert "toggle-mirror" in html


@pytest.mark.parametrize("disabled,expected", [(0, "Mirror on"), (1, "Mirror off")])
def test_a_legacy_row_without_a_contract_is_unchanged(disabled, expected):
    """Pre-contract positions have no effective_exit_policy; their old rails did mirror, so the
    override-only reading is right for them and must not regress to 'no mirror'."""
    html = _pill({"id": 3, "mirror_disabled": disabled, "effective_exit_policy": None})
    assert expected in html
    assert "no mirror" not in html


def test_the_payload_actually_carries_the_field_the_pill_now_reads():
    """`effective_exit_policy` comes from effective_funded_policy(), whose dict includes
    mirror_on_sell — guard against the route dropping it and silently reverting the pill to the
    legacy branch for every funded row."""
    from sniper.services.memecoin_funded_policy import _follow3050_policy, resolve_funded_policy
    assert _follow3050_policy(coin_band="small").to_dict()["mirror_on_sell"] is False
    api = (Path(__file__).resolve().parents[1] / "sniper" / "routes" / "api_memecoin.py").read_text()
    assert '"effective_exit_policy": effective' in api
