from sniper.analysis.winning_wallet_thesis.intake import CandidateWallet, load_curated_candidates


def test_candidate_wallet_carries_source_and_survivorship_label():
    c = CandidateWallet(wallet="0xabc", source="curated", selection_note="operator supplied",
                        survivorship_risk="high")
    assert c.wallet == "0xabc"
    assert c.survivorship_risk == "high"


def test_load_curated_candidates_from_json(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text('[{"wallet": "0xabc", "selection_note": "SwissMiss style"}]')
    out = load_curated_candidates(str(path))
    assert out[0].source == "curated"
    assert out[0].survivorship_risk == "high"      # default for curated winners
