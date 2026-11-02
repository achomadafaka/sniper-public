from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
STYLES_CSS = ROOT / "static" / "styles.css"
INDEX_HTML = ROOT / "templates" / "index.html"


def test_finance_template_uses_workspace_shell():
    src = INDEX_HTML.read_text()
    assert 'class="intel-subtabs-bar"' in src
    assert 'id="tab-intelligence" class="tab-panel intel-density-compact"' in src
    assert 'data-intel-density=' not in src
    assert 'class="intel-finance-shell"' in src
    assert 'id="intel-finance-brief"' in src
    assert 'id="intel-geo-brief"' in src
    assert 'id="intel-geo-detail-toggle"' in src
    assert 'id="intel-finance-detail-toggle"' in src
    assert 'class="intel-secondary-fold intel-news-fold"' in src
    assert 'class="intel-secondary-fold intel-finance-side-fold"' in src
    assert 'class="intel-secondary-fold intel-finance-news-fold"' in src
    assert 'data-collapse-mobile="true"' in src
    assert 'id="intel-finance-report-section"' in src
    assert 'id="intel-finance-ideas"' in src
    assert 'id="intel-finance-tickers"' in src
    assert 'id="intel-finance-news"' in src


def test_finance_app_uses_structured_report_renderer_and_finance_actions():
    src = APP_JS.read_text()
    assert "_setDensityMode(" not in src
    assert "sniper.intelDensity" not in src
    assert "_renderFinanceReport(ctx)" in src
    assert "_renderGeoBrief()" in src
    assert "_renderFinanceBrief()" in src
    assert "_toggleIntelDetails(view = this._currentIntelView)" in src
    assert "_syncIntelDetailToggle(view = this._currentIntelView)" in src
    assert "_compactFinanceSectionLabel(label)" in src
    assert "_syncResponsiveIntelFolds()" in src
    assert "_parseFinanceReport(report)" in src
    assert "Desk detail" in src
    assert "lower-priority event" in src
    assert "document.getElementById('intel-finance-ideas')?.addEventListener('click', handleIdeaAction);" in src
    assert "container.innerHTML = _finGroup('Crypto desk', cryptoItems) + _finGroup('Macro desk', macroItems);" in src


def test_finance_styles_define_layout_and_report_cards():
    src = STYLES_CSS.read_text()
    assert ".intel-subtabs-bar" in src
    assert "#tab-intelligence.intel-density-compact" in src
    assert "#tab-intelligence.intel-density-comfort" not in src
    assert ".analyst-brief-grid" in src
    assert ".analyst-brief-card" in src
    assert ".intel-secondary-fold" in src
    assert ".intel-secondary-fold-summary" in src
    assert ".intel-finance-shell" in src
    assert ".finance-report-grid" in src
    assert ".finance-report-grid--secondary" in src
    assert "#intel-finance-refresh-btn" in src
    assert "#intel-refresh-btn" in src
    assert ".finance-report-card" in src
    assert ".finance-ticker-card" in src
    assert ".finance-idea-card" in src
