from sniper.analysis.winning_wallet_thesis import ledger as L
from sniper.analysis.winning_wallet_thesis import position_builder as PB, slicer as S
from sniper.analysis.winning_wallet_thesis import clusters as C, assembler as A
from sniper.analysis.winning_wallet_thesis.types import WalletEventLedger, EnrichedLedger


def _trade(i):
    # winner bought at 0.4 -> +edge; one event per condition
    return {"type": "TRADE", "conditionId": f"ufc-{i}", "outcomeIndex": 0, "outcome": "Y",
            "title": "t", "eventSlug": f"ufc-{i}", "timestamp": i, "side": "BUY",
            "size": 100, "usdcSize": 40}


def test_full_pipeline_produces_edge_cluster():
    acts = [_trade(i) for i in range(20)]
    led = WalletEventLedger(wallet="0xw", rows=L.normalize(acts), raw_activity=acts)
    res = {f"ufc-{i}": {"resolved": True, "winner": 0, "push": False} for i in range(20)}
    tags = {f"ufc-{i}": ["Sports", "UFC"] for i in range(20)}
    tmpl = {f"ufc-{i}": "ufc" for i in range(20)}
    enriched = EnrichedLedger(ledger=led, resolutions=res, tags=tags, templates=tmpl)

    book = PB.build_position_book(enriched)
    tables = S.slice_edges(book)
    baseline = S.shuffle_baseline(book, n_shuffles=10, seed=1)
    clusters = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    profile = A.assemble_profile("0xw", book, tables, baseline, clusters)

    assert any(c.label == "UFC" for c in profile.edge_clusters)
    assert profile.run_id
    assert "UNVALIDATED" in profile.headline


def test_run_extractor_importable():
    from sniper.analysis.winning_wallet_thesis import run_extractor  # noqa: F401
    assert callable(run_extractor)
