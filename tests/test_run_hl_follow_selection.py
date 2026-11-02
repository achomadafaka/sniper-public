import importlib


def test_select_followable_uses_archetype_pass():
    mod = importlib.import_module("scripts.run_hl_follow")
    tracked = [
        {"wallet": "0xa", "verdict": "watch", "archetype_pass": 1},      # renamed, still followable
        {"wallet": "0xb", "verdict": "market_maker", "archetype_pass": 0},
        {"wallet": "0xc", "verdict": "copyable", "archetype_pass": 1},    # pre-rename legacy
    ]
    got = {t["wallet"] for t in mod.select_followable(tracked)}
    assert got == {"0xa", "0xc"}


def test_select_followable_legacy_copyable_without_column():
    mod = importlib.import_module("scripts.run_hl_follow")
    # a row written before the migration has no archetype_pass key at all
    tracked = [{"wallet": "0xd", "verdict": "copyable"}]
    assert [t["wallet"] for t in mod.select_followable(tracked)] == ["0xd"]
