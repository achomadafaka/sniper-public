"""theme-rules-v1: one case per theme, precedence, boundaries, normalization; no model, no I/O."""
import pytest

from sniper.services import memecoin_desk_theme_rules as R
from sniper.services.memecoin_desk_narrative import THEMES


@pytest.mark.parametrize("symbol,name,theme", [
    ("ABD", "AMERICA IS BACK", "political"), ("VITALIK", "Vitalik Coin", "celebrity"),
    ("VibesAI", "Vibes AI", "ai_agent"), ("GBI", "General Biological Intelligence", "ai_agent"),
    ("GOAL", "World Cup Goal", "sports"), ("CITY", "CITY GTA", "anime_gaming"),
    ("SPOOK", "Halloween Pumpkin", "holiday_event"), ("PETIT", "Petit Chat", "animal_cat"),
    ("Inu", "イヌ", "animal_dog"), ("SKYDOGE", "SKYDOGE", "animal_dog"),
    ("GPEPE", "Godfather PEPE", "animal_other"), ("MEG", "Mega Goose Coin", "animal_other"),
    ("TOAST", "toastcoin", "food"), ("Banana", "The Banana Cult", "food"),
    ("TSLA", "Tesla Robotaxi", "tech_brand"), ("KOL", "key opinion larp", "crypto_meta"),
    ("PumpDrop", "PumpDrop", "other"), ("SIMP", "SIMP", "other"), ("6", "\U0001f300 600", "unknown"),
])
def test_each_theme_has_a_case(symbol, name, theme):
    assert R.tag(symbol, name)[0] == theme


def test_every_taxonomy_id_is_reachable_and_only_those():
    themes = {t for t, _, _ in R.RULES} | {"other", "unknown"}
    assert themes == set(THEMES) and R.RULES_VERSION == "theme-rules-v1"


def test_precedence_specific_subject_beats_generic_crypto_and_animals():
    assert R.tag("PMCAT", "PUMPMINE CAT")[0] == "animal_cat"          # cat beats pump
    assert R.tag("TRUMPCAT", "Trump Cat")[0] == "political"           # subject beats animal
    assert R.tag("swordelon", "sword elon")[0] == "celebrity"


def test_word_boundaries_and_normalization():
    assert R.tag("CATCH", "catch me")[0] == "other"                   # 'cat' inside a word
    assert R.tag("X", "ＣＡＴ coin")[0] == "animal_cat"    # full-width CAT
    assert R.tag("SOLAR", "solar flare")[0] == "other"                # 'sol' inside a word
    assert R.tag("X", "the solana kid")[0] == "crypto_meta"
