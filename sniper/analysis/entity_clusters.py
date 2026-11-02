"""
Multilingual entity clusters for news pre-filtering.

Each cluster has:
  - slug: machine-readable identifier (also used as driving_event in brain.py)
  - region: geographic region for map pinning
  - lat/lon: representative coordinates for world map
  - keywords: dict of language_code → list of keywords/phrases

Language codes: en, fr, de, ar, zh, ru, es, ja, pt, hi
"""

ENTITY_CLUSTERS = {
    "taiwan_strait": {
        "slug": "taiwan_strait",
        "region": "East Asia",
        "lat": 24.0, "lon": 121.0,
        "keywords": {
            "en": ["taiwan", "strait", "tsmc", "pla", "republic of china", "taipei", "cross-strait", "kinmen", "matsu", "amphibious"],
            "zh": ["台湾", "海峡", "台积电", "解放军", "两岸", "金门", "马祖", "台北", "中华民国"],
            "ja": ["台湾", "台湾海峡", "中台", "台積電", "人民解放軍"],
            "fr": ["taïwan", "détroit", "cross-strait"],
            "de": ["taiwan", "meerenge", "volksbefreiungsarmee"],
        },
    },
    "iran_nuclear": {
        "slug": "iran_nuclear",
        "region": "Middle East",
        "lat": 35.7, "lon": 51.4,
        "keywords": {
            "en": ["iran", "nuclear", "uranium", "iaea", "enrichment", "natanz", "fordow", "tehran", "khamenei", "jcpoa", "persian gulf", "strait of hormuz"],
            "ar": ["إيران", "نووي", "يورانيوم", "الوكالة الدولية", "تخصيب", "خامنئي", "طهران"],
            "fa": ["ایران", "هسته‌ای", "اورانیوم", "غنی‌سازی", "خامنه‌ای"],
            "he": ["איראן", "גרעין", "אורניום"],
            "fr": ["iran", "nucléaire", "uranium", "enrichissement"],
            "de": ["iran", "atomprogramm", "uran", "anreicherung"],
            "ru": ["иран", "ядерный", "уран", "обогащение"],
        },
    },
    "us_iran_conflict": {
        "slug": "us_iran_conflict",
        "region": "Middle East",
        "lat": 26.5, "lon": 56.0,  # Strait of Hormuz
        "keywords": {
            "en": ["irgc", "irgc commander", "us forces iran", "iran us war", "iran strike", "us iran", "hormuz", "hormuz closure", "persian gulf blockade",
                   "iran attack", "iran retaliation", "us naval iran", "centcom iran", "iran proxy",
                   "quds force", "soleimani", "iran sanctions", "iran oil embargo"],
            "ar": ["الحرس الثوري", "الحرس الثوري الإيراني", "هرمز", "الخليج الفارسي", "إيران والولايات المتحدة",
                   "هجوم إيران", "العقوبات على إيران", "قوة القدس"],
            "fa": ["سپاه", "سپاه پاسداران", "هرمز", "ایران آمریکا", "حمله ایران", "تنگه هرمز"],
            "ru": ["иргк", "хормуз", "иран сша", "персидский залив", "иранские силы"],
        },
    },
    "russia_ukraine": {
        "slug": "russia_ukraine",
        "region": "Eastern Europe",
        "lat": 49.0, "lon": 31.0,
        "keywords": {
            "en": ["ukraine", "russia", "zelensky", "putin", "kremlin", "donbas", "crimea", "kyiv", "nato", "kherson", "bakhmut", "odesa", "ceasefire", "frontline"],
            "ru": ["украина", "россия", "зеленский", "путин", "кремль", "донбасс", "крым", "киев", "нато", "перемирие"],
            "uk": ["україна", "росія", "зеленський", "путін", "кремль", "донбас", "крим", "київ"],
            "de": ["ukraine", "russland", "selenskyj", "putin", "nato", "waffenstillstand"],
            "fr": ["ukraine", "russie", "zelensky", "poutine", "otan", "cessez-le-feu"],
            "pl": ["ukraina", "rosja", "zełenski", "putin", "nato"],
        },
    },
    "israel_gaza": {
        "slug": "israel_gaza",
        "region": "Middle East",
        "lat": 31.5, "lon": 34.5,
        "keywords": {
            "en": ["israel", "gaza", "hamas", "idf", "netanyahu", "west bank", "hezbollah", "rafah", "ceasefire", "hostage", "idf strike", "houthi"],
            "ar": ["إسرائيل", "غزة", "حماس", "نتنياهو", "الضفة الغربية", "حزب الله", "رفح", "هدنة", "رهائن", "الحوثي"],
            "he": ["ישראל", "עזה", "חמאס", "צה״ל", "נתניהו", "הגדה המערבית", "חיזבאללה", "רפיח", "הפסקת אש"],
            "fr": ["israël", "gaza", "hamas", "tsahal", "netanyahou", "cisjordanie", "cessez-le-feu"],
            "de": ["israel", "gaza", "hamas", "idf", "netanjahu", "westjordanland", "waffenstillstand"],
        },
    },
    "us_china_trade": {
        "slug": "us_china_trade",
        "region": "Global",
        "lat": 39.9, "lon": 116.4,
        "keywords": {
            "en": ["tariff", "china trade", "decoupling", "export controls", "chips act", "semiconductor", "huawei", "tiktok ban", "trade war", "supply chain"],
            "zh": ["关税", "中美贸易", "出口管制", "芯片", "华为", "抖音", "贸易战", "供应链", "脱钩"],
            "ja": ["関税", "貿易戦争", "半導体", "ファーウェイ", "サプライチェーン"],
            "de": ["zölle", "handelskrieg", "halbleiter", "lieferkette"],
            "fr": ["droits de douane", "guerre commerciale", "semi-conducteurs"],
        },
    },
    "north_korea": {
        "slug": "north_korea",
        "region": "East Asia",
        "lat": 39.0, "lon": 125.8,
        "keywords": {
            "en": ["north korea", "kim jong", "pyongyang", "dprk", "icbm", "nuclear test", "missile launch", "korean peninsula"],
            "zh": ["朝鲜", "金正恩", "平壤", "核试验", "导弹", "朝鲜半岛"],
            "ja": ["北朝鮮", "金正恩", "平壌", "弾道ミサイル", "核実験"],
            "ko": ["북한", "김정은", "평양", "핵실험", "미사일"],
            "ru": ["северная корея", "ким чен ын", "пхеньян", "ядерное испытание"],
        },
    },
    "us_election": {
        "slug": "us_election",
        "region": "North America",
        "lat": 38.9, "lon": -77.0,
        "keywords": {
            # Note: removed broad terms ("house", "senate", "congress", "vote") that match
            # routine legislative news — too many false positives for non-election markets.
            "en": ["trump", "trump administration", "harris", "election", "ballot", "white house", "midterm", "primary", "maga", "executive order", "tariff", "vance", "oval office"],
            "es": ["trump", "elecciones", "voto", "congreso", "senado"],
            "fr": ["trump", "élection", "vote", "congrès", "sénat"],
            "de": ["trump", "wahl", "kongress", "senat"],
            "zh": ["特朗普", "大选", "白宫", "国会", "参议院"],
        },
    },
    "fed_rates": {
        "slug": "fed_rates",
        "region": "North America",
        "lat": 38.9, "lon": -77.0,
        "keywords": {
            "en": ["federal reserve", "fed rate", "interest rate", "powell", "inflation", "cpi", "rate cut", "rate hike", "fomc", "monetary policy", "recession", "yield curve"],
            "zh": ["美联储", "利率", "通货膨胀", "降息", "加息", "货币政策", "经济衰退"],
            "ja": ["連邦準備", "金利", "インフレ", "利下げ", "利上げ", "金融政策"],
            "de": ["federal reserve", "zinssatz", "inflation", "zinssenkung", "zinserhöhung"],
            "fr": ["réserve fédérale", "taux d'intérêt", "inflation", "récession"],
            "es": ["reserva federal", "tasa de interés", "inflación", "recesión"],
        },
    },
    "bitcoin_crypto": {
        "slug": "bitcoin_crypto",
        "region": "Global",
        "lat": None, "lon": None,
        "keywords": {
            "en": ["bitcoin", "btc", "ethereum", "eth", "crypto", "coinbase", "binance", "solana", "defi", "nft", "blockchain", "stablecoin", "sec crypto", "etf bitcoin"],
            "zh": ["比特币", "以太坊", "加密货币", "区块链", "稳定币"],
            "ja": ["ビットコイン", "仮想通貨", "ブロックチェーン", "イーサリアム"],
            "ru": ["биткоин", "криптовалюта", "блокчейн", "эфириум"],
            "ko": ["비트코인", "암호화폐", "블록체인", "이더리움"],
            "de": ["bitcoin", "kryptowährung", "blockchain"],
            "fr": ["bitcoin", "cryptomonnaie", "blockchain"],
        },
    },
    "opec_oil": {
        "slug": "opec_oil",
        "region": "Middle East",
        "lat": 24.7, "lon": 46.7,
        "keywords": {
            "en": ["opec", "crude oil", "brent", "wti", "oil price", "energy crisis", "oil embargo", "petroleum", "saudi aramco", "oil production cut"],
            "ar": ["أوبك", "النفط الخام", "أسعار النفط", "أرامكو", "الطاقة"],
            "fr": ["opep", "pétrole brut", "prix du pétrole", "énergie"],
            "de": ["opec", "rohöl", "ölpreis", "energie"],
            "ru": ["опек", "нефть", "цена нефти", "саудовская арамко"],
            "zh": ["欧佩克", "原油", "油价", "沙特阿美"],
        },
    },
    "ai_regulation": {
        "slug": "ai_regulation",
        "region": "Global",
        "lat": None, "lon": None,
        "keywords": {
            "en": ["openai", "anthropic", "gpt", "claude", "gemini", "ai regulation", "agi", "llm", "ai safety", "ai act", "deepseek", "artificial intelligence", "foundation model"],
            "zh": ["人工智能", "大模型", "AI监管", "DeepSeek", "通用人工智能"],
            "de": ["künstliche intelligenz", "ki-regulierung", "sprachmodell", "ki-sicherheit"],
            "fr": ["intelligence artificielle", "régulation de l'ia", "modèle de langage"],
            "ja": ["人工知能", "AI規制", "大規模言語モデル"],
            "eu": ["eu ai act", "foundation model", "high-risk ai"],
        },
    },
    "spacex_nasa": {
        "slug": "spacex_nasa",
        "region": "North America",
        "lat": 28.6, "lon": -80.6,
        "keywords": {
            "en": ["spacex", "starship", "falcon", "nasa", "elon musk", "mars mission", "lunar", "artemis", "launch", "rocket", "satellite"],
            "zh": ["太空探索", "星际飞船", "美国宇航局", "火星", "月球", "火箭"],
            "fr": ["spacex", "nasa", "starship", "fusée", "mission lunaire"],
            "de": ["spacex", "nasa", "rakete", "mondmission"],
        },
    },
    "india_pakistan": {
        "slug": "india_pakistan",
        "region": "South Asia",
        "lat": 28.6, "lon": 77.2,
        "keywords": {
            "en": ["india", "pakistan", "kashmir", "modi", "lahore", "islamabad", "line of control", "nuclear", "border skirmish"],
            "hi": ["भारत", "पाकिस्तान", "कश्मीर", "मोदी", "परमाणु", "सीमा"],
            "ur": ["پاکستان", "بھارت", "کشمیر", "جوہری", "سرحد"],
            "zh": ["印度", "巴基斯坦", "克什米尔", "莫迪"],
            "fr": ["inde", "pakistan", "cachemire", "nucléaire"],
        },
    },
    "venezuela_latam": {
        "slug": "venezuela_latam",
        "region": "South America",
        "lat": 6.4, "lon": -66.6,
        "keywords": {
            "en": ["venezuela", "maduro", "cuba", "nicaragua", "bolivia", "colombia", "brazil", "lula", "latam election"],
            "es": ["venezuela", "maduro", "cuba", "nicaragua", "elecciones", "colombia", "brasil", "lula", "latinoamérica"],
            "pt": ["venezuela", "maduro", "brasil", "lula", "eleições", "colômbia"],
        },
    },
    "turkey_erdogan": {
        "slug": "turkey_erdogan",
        "region": "Middle East",
        "lat": 39.9, "lon": 32.9,
        "keywords": {
            "en": ["turkey", "erdogan", "ankara", "nato", "lira", "istanbul", "kurdish", "pkk", "ottoman"],
            "tr": ["türkiye", "erdoğan", "ankara", "nato", "lira", "kürt", "pkk"],
            "ar": ["تركيا", "أردوغان", "أنقرة", "ناتو"],
            "de": ["türkei", "erdogan", "ankara", "nato", "lira"],
            "fr": ["turquie", "erdogan", "ankara", "otan"],
        },
    },
    "myanmar_coup": {
        "slug": "myanmar_coup",
        "region": "Southeast Asia",
        "lat": 16.8, "lon": 96.2,
        "keywords": {
            "en": ["myanmar", "burma", "aung san suu kyi", "junta", "tatmadaw", "coup", "resistance", "rohingya"],
            "zh": ["缅甸", "翁山素季", "军政府", "政变", "罗兴亚"],
            "ja": ["ミャンマー", "ビルマ", "軍事クーデター", "ロヒンギャ"],
            "th": ["เมียนมา", "พม่า", "รัฐประหาร"],
        },
    },
    "sahel_africa": {
        "slug": "sahel_africa",
        "region": "Africa",
        "lat": 13.5, "lon": -2.1,
        "keywords": {
            "en": ["sahel", "mali", "niger", "burkina faso", "coup", "wagner", "jihadist", "al-qaeda africa", "islamic state africa", "france africa"],
            "fr": ["sahel", "mali", "niger", "burkina faso", "coup d'état", "djihadiste", "france afrique", "wagner"],
            "ar": ["الساحل", "مالي", "النيجر", "بوركينا فاسو", "واغنر", "جهادي"],
        },
    },
    "south_china_sea": {
        "slug": "south_china_sea",
        "region": "Southeast Asia",
        "lat": 12.0, "lon": 115.0,
        "keywords": {
            "en": ["south china sea", "philippines", "spratly", "paracel", "asean", "marcos", "reef", "territorial dispute", "nine-dash line"],
            "zh": ["南海", "菲律宾", "南沙群岛", "西沙群岛", "东盟", "九段线", "礁石"],
            "tl": ["pilipinas", "timog tsina sea", "marcos"],
            "vi": ["biển đông", "philippines", "quần đảo trường sa"],
            "ja": ["南シナ海", "フィリピン", "南沙諸島"],
        },
    },
    "climate_energy": {
        "slug": "climate_energy",
        "region": "Global",
        "lat": None, "lon": None,
        "keywords": {
            "en": ["climate change", "carbon emissions", "cop summit", "renewable energy", "solar", "wind energy", "paris agreement", "net zero", "temperature record"],
            "de": ["klimawandel", "emissionen", "erneuerbare energie", "netto-null"],
            "fr": ["changement climatique", "émissions", "cop", "énergies renouvelables", "accord de paris"],
            "es": ["cambio climático", "emisiones", "energía renovable", "acuerdo de parís"],
            "zh": ["气候变化", "碳排放", "可再生能源", "净零", "巴黎协定"],
        },
    },
    "global_recession": {
        "slug": "global_recession",
        "region": "Global",
        "lat": None, "lon": None,
        "keywords": {
            "en": ["recession", "gdp contraction", "unemployment", "imf", "world bank", "debt crisis", "sovereign default", "economic slowdown", "stagflation"],
            "de": ["rezession", "wirtschaftsabschwung", "arbeitslosigkeit", "staatsverschuldung"],
            "fr": ["récession", "ralentissement économique", "chômage", "crise de la dette"],
            "zh": ["经济衰退", "失业", "国际货币基金", "世界银行", "债务危机"],
            "ja": ["景気後退", "失業率", "IMF", "世界銀行"],
            "es": ["recesión", "desempleo", "fmi", "banco mundial"],
        },
    },
}

# Reverse index: for fast lookup, map each language → list of (keyword, cluster_slug) tuples
# Built once at import time
def build_keyword_index(clusters: dict) -> dict:
    """Build language-keyed reverse index for O(1) cluster lookup per token."""
    index = {}  # lang → {token → slug}
    for slug, cluster in clusters.items():
        for lang, keywords in cluster.get("keywords", {}).items():
            if lang not in index:
                index[lang] = {}
            for kw in keywords:
                index[lang][kw.lower()] = slug
    return index

KEYWORD_INDEX = build_keyword_index(ENTITY_CLUSTERS)


def detect_entity_clusters(text: str, lang: str = "en") -> list:
    """Return list of matched cluster slugs for a piece of text.

    Checks the text against both the specified language and English (fallback).
    Returns deduplicated list of matching slugs, ordered by first match.
    """
    text_lower = text.lower()
    matched = []
    seen = set()
    # Check target language + English as fallback
    for check_lang in [lang, "en"]:
        lang_index = KEYWORD_INDEX.get(check_lang, {})
        for kw, slug in lang_index.items():
            if kw in text_lower and slug not in seen:
                matched.append(slug)
                seen.add(slug)
    return matched
