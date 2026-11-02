from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "static" / "app.js"
TEMPLATE = ROOT / "templates" / "index.html"


def _renderer():
    source = APP.read_text()
    start = source.index("async _loadXSources(")
    end = source.index("\n    },\n  };", start + 20)
    return source[start:end]


def test_x_is_an_intelligence_subtab_not_a_memecoin_workspace():
    app = APP.read_text()
    template = TEMPLATE.read_text()
    assert 'data-intel-view="x"' in template
    assert 'id="intel-x-view"' in template
    assert 'data-subtab="x-sources"' not in app
    assert 'data-section="x-sources"' not in app
    assert "'x-sources': '_loadXSources'" not in app


def test_x_intel_uses_only_canonical_read_routes_and_exact_post_links():
    renderer = _renderer()
    for endpoint in (
        "/api/intel/x/status", "/api/intel/x/sources", "/api/intel/x/events",
        "/api/intel/x/suggestions", "/api/intel/x/context",
    ):
        assert endpoint in renderer
    assert "/api/memecoin/x/" not in renderer
    assert "post_url" in renderer and "Open Post" in renderer
    for forbidden in ("/positions", "/real", "data-action=", ">Buy<", ">Sell<"):
        assert forbidden not in renderer


def test_x_intel_keeps_display_only_health_and_evidence_states():
    source = APP.read_text() + TEMPLATE.read_text()
    for label in (
        "display-only", "X capture unavailable", "Recent cashtag activity",
        "Identity", "Relationship", "X source ledger", "LLM annotations",
    ):
        assert label in source
