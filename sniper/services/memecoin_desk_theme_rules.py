"""Code theme tagger (theme-rules-v1): deterministic keyword rules over a launch's symbol + name.

The no-model fallback for theme-taxonomy-v1 while the local tagger is on hold (its cold load stalled the
funded loop, 2026-10-21). Zero model calls, zero host load: one regex pass per launch. Every rule sits in
RULES below; a change to any rule is a new RULES_VERSION. Text is creator-controlled and is only matched,
never interpreted.

Precedence: specific subjects first (political, celebrity, ai_agent, sports, anime_gaming, holiday_event),
then animals, food and tech brands, and generic crypto words last, because nearly every launch says "coin" or
"pump". No match = "other"; no letters at all (digits, emoji, punctuation) = "unknown".

FROZEN 2026-10-21 (Gamma, Alpha) as measured: 24.7 % of that day's 5,319 launches get a specific theme. Known
misses, fixed only in a v2 at a cohort boundary: a keyword inside a longer word ("Grokpad", "Kimchinu"). A
day's meme word lands in whatever bucket its word belongs to ("kimchi" counted as food on 2026-10-21); the
emergent-token table, not this taxonomy, is the channel for that.
"""
from __future__ import annotations

import re
import unicodedata

from sniper.services.memecoin_desk_narrative import THEMES

RULES_VERSION = "theme-rules-v1"

# (theme, word tokens matched on letter boundaries, substrings matched anywhere). Substrings are only long,
# distinctive stems ("vitalik" also catches "vitalikcoin"); short words need boundaries ("cat" is not "catch").
RULES = (
    ("political", ("trump", "maga", "biden", "kamala", "obama", "president", "america", "american", "usa",
                   "election", "congress", "senate", "democrat", "republican", "putin", "zelensky", "potus",
                   "white house", "government", "vance", "rfk", "milei"), ("trump", "maga")),
    ("celebrity", ("elon", "musk", "vitalik", "zuck", "kanye", "drake", "taylor swift", "snoop", "cz", "saylor",
                   "mrbeast", "tate", "kardashian", "bezos", "beyonce", "rihanna", "ronaldo", "messi", "lebron",
                   "ansem", "toly"), ("vitalik", "zuckerberg", "kardashian", "mrbeast", "saylor")),
    ("ai_agent", ("ai", "agent", "agents", "gpt", "llm", "bot", "robot", "agi", "grok", "claude", "openai",
                  "chatgpt", "deepseek", "neural", "intelligence", "android", "cyborg"), ("chatgpt", "deepseek", "openai")),
    ("sports", ("football", "soccer", "nba", "nfl", "basketball", "tennis", "golf", "boxing", "ufc", "f1",
                "olympic", "olympics", "world cup", "goal", "striker", "baseball", "hockey"), ()),
    ("anime_gaming", ("anime", "naruto", "goku", "pokemon", "poke", "gta", "mario", "zelda", "minecraft", "roblox",
                      "fortnite", "sonic", "pikachu", "waifu", "manga", "gamer", "nintendo", "playstation", "xbox",
                      "steam", "luigi", "kirby"), ("pokemon", "pikachu", "minecraft", "fortnite")),
    ("holiday_event", ("halloween", "christmas", "xmas", "thanksgiving", "easter", "new year", "valentine",
                       "diwali", "ramadan", "hanukkah", "spooky", "pumpkin", "santa"), ("halloween", "christmas")),
    ("animal_cat", ("cat", "cats", "kitty", "kitten", "meow", "neko", "popcat", "mew", "chat", "gato"), ("kitty", "kitten")),
    ("animal_dog", ("dog", "dogs", "doge", "inu", "shib", "shiba", "puppy", "pup", "wif", "perro", "corgi",
                    "doggo", "pug", "husky", "イヌ", "犬"), ("doge", "shiba", "doggo")),
    ("animal_other", ("pepe", "frog", "monkey", "ape", "apes", "goose", "duck", "crab", "gator", "owl", "tiger",
                      "bear", "bull", "whale", "penguin", "hamster", "rat", "mouse", "fish", "shark", "bird",
                      "chicken", "horse", "pig", "cow", "lion", "wolf", "fox", "panda", "koala", "snake", "turtle",
                      "rabbit", "bunny", "bee", "gorilla", "sloth", "otter", "seal", "capybara", "hippo", "giraffe",
                      "zebra", "elephant", "deer", "moose", "goat", "sheep", "donkey", "eagle", "parrot", "crow",
                      "pigeon", "dino", "dinosaur", "lizard", "octopus", "squirrel", "raccoon", "bat", "worm"),
     ("pepe", "monkey", "penguin", "hamster", "capybara", "gorilla", "octopus", "squirrel", "raccoon")),
    ("food", ("banana", "pizza", "burger", "toast", "cheese", "taco", "sushi", "kimchi", "coffee", "bread",
              "donut", "cookie", "cake", "fries", "rice", "noodle", "ramen", "beer", "wine", "milk", "egg",
              "bacon", "sandwich", "candy", "chocolate", "apple pie", "potato", "tomato", "mango", "lemon"),
     ("banana", "pizza", "burger", "noodle", "chocolate")),
    ("tech_brand", ("apple", "google", "microsoft", "tesla", "nvidia", "amazon", "twitter", "tiktok", "spacex",
                    "starlink", "iphone", "netflix", "youtube", "discord", "telegram", "robinhood", "meta",
                    "samsung", "openai"), ("microsoft", "nvidia", "starlink", "netflix", "youtube")),
    ("crypto_meta", ("btc", "bitcoin", "sol", "solana", "eth", "ethereum", "pump", "pumpfun", "crypto", "satoshi",
                     "binance", "bnb", "defi", "nft", "memecoin", "degen", "trench", "trencher", "trenches", "kol",
                     "cto", "rug", "hodl", "wagmi", "ngmi", "airdrop", "cashback", "dex", "jupiter", "raydium",
                     "phantom", "100x", "1000x", "moon", "ponzi", "larp", "jeet", "jeets"),
     ("bitcoin", "solana", "ethereum", "pumpfun", "memecoin", "satoshi")),
)

_LETTER = r"[^\W\d_]"
_COMPILED = tuple(
    (theme, re.compile("|".join(["(?<!%s)%s(?!%s)" % (_LETTER, re.escape(w), _LETTER) for w in words]
                                + [re.escape(s) for s in subs])))
    for theme, words, subs in RULES)


def normalize(symbol, name):
    return unicodedata.normalize("NFKC", "%s %s" % (symbol or "", name or "")).lower()


def tag(symbol, name):
    """(theme, matched text) — 'unknown' when there is no letter, 'other' when no rule matches."""
    text = normalize(symbol, name)
    if not re.search(_LETTER, text):
        return "unknown", None
    for theme, rx in _COMPILED:
        m = rx.search(text)
        if m:
            return theme, m.group(0)
    return "other", None


assert {t for t, _, _ in RULES} | {"other", "unknown"} == set(THEMES), "rules must cover theme-taxonomy-v1"
