"""F22: native-float partial valuation and measured-zero regressions."""
import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("eth_units,eth_value,eth_price,shown", [
    (1, None, None, "—"), (1, 200, 200, "$800.00"),
    (0, 0, 200, "$600.00"), (0, None, None, "$600.00"),
])
def test_float_total_never_presents_a_partial_asset_valuation_as_complete(
        eth_units, eth_value, eth_price, shown):
    root = Path(__file__).resolve().parents[1]
    snapshot = {"available": True, "age_s": 1, "assets": {}, "headline": {}}
    for asset, units, value, price in [("ETH", eth_units, eth_value, eth_price),
                                      ("BNB", 1, 500, 500), ("SOL", 1, 100, 100)]:
        snapshot["assets"][asset] = dict(units=units, value_usd=value, price_usd=price,
            chains=[dict(chain="56", units=units, native=units, wrapped=0)], change={}, sparkline={})
    script = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('    _maybeLoadDrift() {');
const end = source.indexOf('    // ------------------------------------------------------------------ manual entry', start);
const esc = value => String(value);
const panel = eval('({' + source.slice(start, end) + '})');
panel._usd2 = value => '$' + Number(value).toFixed(2);
panel._driftRecvAt = Date.now();
panel._drift = JSON.parse(process.argv[2]);
const html = panel._driftPanel();
const cell = html.match(/class="[^"]*nf-float[^"]*"[^>]*>([^<]*)</)[1];
process.stdout.write(JSON.stringify({cell, html}));
"""
    result = subprocess.run(["node", "-e", script, str(root / "static/app.js"), json.dumps(snapshot)],
                            check=True, capture_output=True, text=True, timeout=10)
    out = json.loads(result.stdout)
    assert out["cell"] == shown
    if shown == "—":
        assert "ETH valuation unknown" in out["html"]
    assert "nf-banner" not in out["html"]
