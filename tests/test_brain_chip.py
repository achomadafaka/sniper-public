"""Execute the header's brain rendering against a tiny DOM, without a browser or network."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_brain_chip_clears_stale_display_and_keeps_money_warning_separate():
    text = (ROOT / "static/app.js").read_text()
    start = text.index("          const brainEl =")
    end = text.index("          // health_ok", start)
    code = text[start:end]
    script = """
const val = {textContent: ''};
const chip = {title: '', className: '', querySelector: () => val};
const document = {getElementById: () => chip};
const state = {opsHealthWarn: false};
function render(opsData) { CODE; return [val.textContent, chip.className, state.opsHealthWarn]; }
console.log(JSON.stringify([
  render({brain: {state:'ok', share_verified:.72, age_h:2, facts_live:100}}),
  render(null),
  render({brain: {state:'ok', share_verified:null}}),
  render({brain: {state:'stale', share_verified:.72}})
]));
""".replace("CODE", code)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [["72%", "shell-chip ok", False],
                                       ["unavailable", "shell-chip warn", False],
                                       ["--", "shell-chip ok", False],
                                       ["stale", "shell-chip warn", False]]
