#!/usr/bin/env python3
"""
seed_country_profiles.py — Seed country profile data into the Supabase brain.

Stores each country profile as a knowledge/country:{ISO} memory entry,
accessible to all agents via:
    from bot.supabase_helper import get_memory
    get_memory("country:IR", type_filter="knowledge")

Usage:
    python3 scripts/seed_country_profiles.py           # upsert all profiles
    python3 scripts/seed_country_profiles.py --check   # list existing profiles
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()

from bot.supabase_helper import upsert_memory, search_memories  # noqa: E402

PROFILES = [
    {
        "country_code": "IR",
        "country_name": "Iran",
        "region": "middle_east",
        "capital": "Tehran",
        "population_millions": 88.5,
        "gdp_billions_usd": 388,
        "government_type": "Islamic Republic (theocratic)",
        "head_of_state": "Ali Khamenei (Supreme Leader)",
        "head_of_government": "Masoud Pezeshkian (President)",
        "major_religion": "Shia Islam (90-95%)",
        "key_exports": ["oil", "petrochemicals", "metals", "agriculture"],
        "key_imports": ["machinery", "food", "technology"],
        "major_allies": ["Russia", "China", "Syria", "Hezbollah", "Houthis"],
        "major_adversaries": ["United States", "Israel", "Saudi Arabia"],
        "economic_dependencies": ["oil exports (60% of revenue)", "SWIFT access (sanctioned)", "Strait of Hormuz shipping"],
        "sanctions_status": "Heavy US/EU sanctions since 2018, oil export restrictions",
        "currency": "Iranian Rial (IRR)",
        "military_overview": "Large conventional forces, ballistic missiles, proxy network (Hezbollah, Houthis, Iraqi militias)",
        "strategic_importance": "Controls Strait of Hormuz (20% of global oil), nuclear program, regional proxy influence",
        "current_situation_notes": "Active confrontation with US. Strait of Hormuz partially disrupted — contributing to WTI crude at ~$100/bbl. Multiple ceasefire/de-escalation market signals active (April 15, April 30, May 31 deadlines). Regime stability questions intensifying — prediction markets pricing 65% probability of regime collapse before 2027. Supreme Leader Khamenei succession (Mojtaba) a live market question. Proxy network (Houthis, Hezbollah) under pressure.",
        "updated_at": "2026-03-28",
    },
    {
        "country_code": "IL",
        "country_name": "Israel",
        "region": "middle_east",
        "capital": "Jerusalem (disputed)",
        "population_millions": 9.8,
        "gdp_billions_usd": 525,
        "government_type": "Parliamentary democracy",
        "head_of_state": "Isaac Herzog (President, ceremonial)",
        "head_of_government": "Benjamin Netanyahu (Prime Minister)",
        "major_religion": "Judaism (74%), Islam (18%)",
        "key_exports": ["technology", "diamonds", "pharmaceuticals", "defense systems"],
        "key_imports": ["oil", "grains", "military equipment"],
        "major_allies": ["United States", "UK", "Germany", "UAE", "Bahrain"],
        "major_adversaries": ["Iran", "Hezbollah", "Hamas", "Syria"],
        "economic_dependencies": ["US military aid ($3.8B/yr)", "tech sector (50% of exports)", "tourism"],
        "sanctions_status": "None (some BDS movement pressure)",
        "currency": "Israeli New Shekel (ILS)",
        "military_overview": "Qualitative military edge in region, nuclear capability (undeclared), Iron Dome, advanced air force",
        "strategic_importance": "Only nuclear power in Middle East, US strategic ally, tech innovation hub",
        "current_situation_notes": "Coordinating with US on Iran pressure campaign. IDF operations ongoing in Lebanon and Gaza. Elevated military readiness given Hormuz/Iran escalation. Netanyahu government navigating US relationship under Trump 2nd term.",
        "updated_at": "2026-03-28",
    },
    {
        "country_code": "US",
        "country_name": "United States",
        "region": "north_america",
        "capital": "Washington, D.C.",
        "population_millions": 335,
        "gdp_billions_usd": 27360,
        "government_type": "Federal presidential republic",
        "head_of_state": "Donald Trump (President)",
        "head_of_government": "Donald Trump (President)",
        "major_religion": "Christianity (65%), unaffiliated (28%)",
        "key_exports": ["technology", "petroleum", "agriculture", "aerospace", "financial services"],
        "key_imports": ["consumer goods", "vehicles", "electronics", "oil"],
        "major_allies": ["NATO members", "Japan", "South Korea", "Australia", "Israel"],
        "major_adversaries": ["Russia (adversarial)", "China (competitive)", "Iran", "North Korea"],
        "economic_dependencies": ["USD reserve currency status", "tech sector", "consumer spending (70% of GDP)"],
        "sanctions_status": "Major sanctions enforcer (OFAC)",
        "currency": "US Dollar (USD)",
        "military_overview": "Largest military globally, carrier groups, global base network, nuclear triad",
        "strategic_importance": "Global reserve currency, NATO leader, largest economy, tech dominance",
        "current_situation_notes": "Trump 2nd term (Jan 2025–). Active Iran pressure campaign with possible military component (market pricing 41% by April 30). Aggressive tariff policy driving equity selloff — S&P -1.67%, NASDAQ -2.15%, VIX at 31 (+13%). Fed hawkish. 'No Kings' domestic protest movement active. Cabinet stability questioned (prediction market open: will no one leave Trump Cabinet before 2027?).",
        "updated_at": "2026-03-28",
    },
    {
        "country_code": "CN",
        "country_name": "China",
        "region": "east_asia",
        "capital": "Beijing",
        "population_millions": 1412,
        "gdp_billions_usd": 18530,
        "government_type": "One-party socialist republic (CPC)",
        "head_of_state": "Xi Jinping (President)",
        "head_of_government": "Li Qiang (Premier)",
        "major_religion": "Buddhism, Taoism, folk religion (official atheism)",
        "key_exports": ["electronics", "machinery", "textiles", "rare earths"],
        "key_imports": ["semiconductors", "oil", "soybeans", "iron ore"],
        "major_allies": ["Russia", "Pakistan", "North Korea", "Iran (strategic partner)"],
        "major_adversaries": ["United States (competitive)", "Japan", "India", "Taiwan"],
        "economic_dependencies": ["manufacturing exports", "Belt & Road investments", "rare earth monopoly", "US Treasury holdings"],
        "sanctions_status": "Targeted US sanctions on tech (chips, Huawei), trade tariffs",
        "currency": "Chinese Yuan (CNY)",
        "military_overview": "Largest military by personnel, rapidly modernizing navy, nuclear arsenal, space capabilities",
        "strategic_importance": "Second largest economy, Taiwan strait control, global supply chain hub, rare earth dominance",
        "current_situation_notes": "Trade war with US intensifying under Trump tariff regime. Supporting Iran diplomatically. Taiwan strait tensions in background. Watching US equity selloff as potential leverage point. Belt & Road continues but facing debt-trap pushback.",
        "updated_at": "2026-03-28",
    },
    {
        "country_code": "RU",
        "country_name": "Russia",
        "region": "russia_cis",
        "capital": "Moscow",
        "population_millions": 144,
        "gdp_billions_usd": 2240,
        "government_type": "Federal semi-presidential republic (authoritarian)",
        "head_of_state": "Vladimir Putin (President)",
        "head_of_government": "Mikhail Mishustin (Prime Minister)",
        "major_religion": "Russian Orthodox Christianity (71%)",
        "key_exports": ["oil", "natural gas", "wheat", "arms", "metals"],
        "key_imports": ["machinery", "vehicles", "pharmaceuticals"],
        "major_allies": ["China", "Iran", "North Korea", "Belarus", "Syria"],
        "major_adversaries": ["NATO", "United States", "Ukraine", "EU"],
        "economic_dependencies": ["Energy exports (45% of federal revenue)", "China trade relationship", "sanctions evasion"],
        "sanctions_status": "Comprehensive Western sanctions since 2022 (SWIFT partial ban, asset freezes, oil price cap)",
        "currency": "Russian Ruble (RUB)",
        "military_overview": "Largest nuclear arsenal, modernizing but degraded by Ukraine war losses, Wagner Group",
        "strategic_importance": "Largest country by area, major energy supplier to Europe/Asia, nuclear superpower, UN veto",
        "current_situation_notes": "Ukraine war ongoing (Year 3+). Trump administration pushing for ceasefire — prediction market pricing 67% by end of 2026. Russia's military degraded but still advancing slowly. Peace talks framework being discussed. US aid posture uncertain under Trump.",
        "updated_at": "2026-03-28",
    },
    {
        "country_code": "UA",
        "country_name": "Ukraine",
        "region": "europe_east",
        "capital": "Kyiv",
        "population_millions": 37,
        "gdp_billions_usd": 175,
        "government_type": "Unitary semi-presidential republic",
        "head_of_state": "Volodymyr Zelenskyy (President)",
        "head_of_government": "Denys Shmyhal (Prime Minister)",
        "major_religion": "Orthodox Christianity (67%)",
        "key_exports": ["grains", "sunflower oil", "iron", "steel"],
        "key_imports": ["energy", "machinery", "defense equipment"],
        "major_allies": ["United States", "EU", "UK", "NATO (aspiring)"],
        "major_adversaries": ["Russia"],
        "economic_dependencies": ["Western military/economic aid", "grain exports", "EU integration path"],
        "sanctions_status": "None (recipient of Western support)",
        "currency": "Ukrainian Hryvnia (UAH)",
        "military_overview": "Battle-hardened from 3+ years of war, Western-equipped, drone warfare pioneer",
        "strategic_importance": "Major grain exporter, buffer between NATO and Russia, energy transit corridor",
        "current_situation_notes": "Zelensky government under pressure to accept ceasefire terms. Resisting any deal that cedes occupied territory. Economy in war footing — heavily dependent on Western aid. Drone warfare capability the primary asymmetric advantage.",
        "updated_at": "2026-03-28",
    },
    {
        "country_code": "SA",
        "country_name": "Saudi Arabia",
        "region": "middle_east",
        "capital": "Riyadh",
        "population_millions": 36,
        "gdp_billions_usd": 1069,
        "government_type": "Absolute monarchy",
        "head_of_state": "Salman bin Abdulaziz (King)",
        "head_of_government": "Mohammed bin Salman (Crown Prince, de facto ruler)",
        "major_religion": "Sunni Islam (85-90%)",
        "key_exports": ["oil (60% of revenue)", "petrochemicals", "plastics"],
        "key_imports": ["machinery", "vehicles", "food", "military equipment"],
        "major_allies": ["United States", "UAE", "Egypt", "UK", "France"],
        "major_adversaries": ["Iran", "Houthis (Yemen)"],
        "economic_dependencies": ["Oil exports (dominant)", "Vision 2030 diversification", "sovereign wealth fund (PIF)"],
        "sanctions_status": "None (arms sales controversies)",
        "currency": "Saudi Riyal (SAR)",
        "military_overview": "Large US-equipped military, ongoing Yemen intervention, growing defense industry",
        "strategic_importance": "Largest oil exporter, OPEC leader, custodian of Mecca/Medina, regional power broker",
        "current_situation_notes": "Positioned as swing mediator in Iran-US standoff. Oil revenue benefiting from $100 crude but regional stability threatened. Vision 2030 projects dependent on peaceful environment. OPEC+ balancing act between US pressure and producer interests.",
        "updated_at": "2026-03-28",
    },
    {
        "country_code": "GB",
        "country_name": "United Kingdom",
        "region": "europe_west",
        "capital": "London",
        "population_millions": 68,
        "gdp_billions_usd": 3340,
        "government_type": "Parliamentary constitutional monarchy",
        "head_of_state": "King Charles III",
        "head_of_government": "Keir Starmer (Prime Minister)",
        "major_religion": "Christianity (46%), no religion (37%)",
        "key_exports": ["financial services", "pharmaceuticals", "aerospace", "oil"],
        "key_imports": ["machinery", "vehicles", "food"],
        "major_allies": ["United States", "NATO", "Five Eyes", "France", "EU (complex post-Brexit)"],
        "major_adversaries": ["Russia"],
        "economic_dependencies": ["Financial services (City of London)", "North Sea energy", "trade deals post-Brexit"],
        "sanctions_status": "Sanctions enforcer (aligned with US/EU)",
        "currency": "British Pound (GBP)",
        "military_overview": "Nuclear power (Trident), professional forces, NATO framework nation, intelligence capabilities",
        "strategic_importance": "UN Security Council veto, Five Eyes intelligence, financial center, Atlantic bridge",
        "current_situation_notes": "Starmer government navigating post-Brexit trade deals. Supporting Ukraine. Watching US-Iran escalation and impact on oil/shipping routes. Domestic economic headwinds from global tariff uncertainty.",
        "updated_at": "2026-03-28",
    },
    {
        "country_code": "TW",
        "country_name": "Taiwan",
        "region": "east_asia",
        "capital": "Taipei",
        "population_millions": 23.5,
        "gdp_billions_usd": 790,
        "government_type": "Semi-presidential republic",
        "head_of_state": "Lai Ching-te (President)",
        "head_of_government": "Cho Jung-tai (Premier)",
        "major_religion": "Buddhism, Taoism, folk religion",
        "key_exports": ["semiconductors (TSMC)", "electronics", "machinery"],
        "key_imports": ["oil", "natural gas", "machinery"],
        "major_allies": ["United States (informal)", "Japan (informal)"],
        "major_adversaries": ["China (claims sovereignty)"],
        "economic_dependencies": ["Semiconductor manufacturing (60% of global advanced chips)", "US security guarantee"],
        "sanctions_status": "None (but China pressures diplomatic isolation)",
        "currency": "New Taiwan Dollar (TWD)",
        "military_overview": "Defensive posture, US arms sales, conscription, asymmetric warfare focus",
        "strategic_importance": "TSMC produces 60%+ of global advanced semiconductors, Taiwan Strait is key shipping lane",
        "current_situation_notes": "Taiwan Strait risk elevated as secondary concern behind Iran. TSMC remains globally critical chokepoint. Lai government maintaining status quo amid US-China trade war tensions.",
        "updated_at": "2026-03-28",
    },
    {
        "country_code": "LB",
        "country_name": "Lebanon",
        "region": "middle_east",
        "capital": "Beirut",
        "population_millions": 5.5,
        "gdp_billions_usd": 23,
        "government_type": "Parliamentary republic (confessional system)",
        "head_of_state": "Joseph Aoun (President)",
        "head_of_government": "Nawaf Salam (Prime Minister)",
        "major_religion": "Islam (54%), Christianity (40.5%)",
        "key_exports": ["jewelry", "chemicals", "food products"],
        "key_imports": ["petroleum", "vehicles", "machinery", "food"],
        "major_allies": ["France (historical)", "Saudi Arabia (Sunni alignment)", "United States"],
        "major_adversaries": ["Israel (border conflict)", "Syria (complex relationship)"],
        "economic_dependencies": ["Remittances from diaspora", "banking sector (collapsed 2019)", "foreign aid"],
        "sanctions_status": "Hezbollah-targeted sanctions (US/EU), banking crisis",
        "currency": "Lebanese Pound (LBP, collapsed)",
        "military_overview": "Small military, Hezbollah operates as parallel force with larger capability",
        "strategic_importance": "Hezbollah presence (Iran proxy), Mediterranean port, Syrian refugee host (1.5M)",
        "current_situation_notes": "Post-ceasefire stabilization with Israel ongoing. New government (Aoun/Salam) attempting reconstruction. Hezbollah weakened but still present. Dependent on international aid. Regional Iran escalation creates renewed instability risk.",
        "updated_at": "2026-03-28",
    },
]


def seed_profiles():
    """Upsert all country profiles into the Supabase brain."""
    for profile in PROFILES:
        code = profile["country_code"]
        name = f"country:{code}"
        description = f"{profile['country_name']} country profile"
        content = json.dumps(profile, indent=2)

        result = upsert_memory("knowledge", name, description, content, source="vps")
        status = "updated" if result else "failed"
        print(f"  {name} ({profile['country_name']}): {status}")

    print(f"\nSeeded {len(PROFILES)} country profiles into brain (knowledge/country:XX)")


def check_profiles():
    """List existing country profiles in the brain."""
    results = search_memories("country:")
    profiles = [m for m in results if m.get("name", "").startswith("country:")]
    if not profiles:
        print("No country profiles found in brain.")
        return
    print(f"Found {len(profiles)} country profiles:")
    for p in profiles:
        name = p.get("name", "?")
        updated = p.get("updated_at", "?")
        content = p.get("content", "")
        try:
            data = json.loads(content)
            country = data.get("country_name", "?")
        except (json.JSONDecodeError, TypeError):
            country = "?"
        print(f"  {name}: {country} (updated: {updated})")


def main():
    parser = argparse.ArgumentParser(description="Seed country profiles into Supabase brain")
    parser.add_argument("--check", action="store_true", help="List existing profiles instead of seeding")
    args = parser.parse_args()

    if args.check:
        check_profiles()
    else:
        seed_profiles()


if __name__ == "__main__":
    main()
