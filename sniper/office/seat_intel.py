"""Intel tables a seat can read through db_read (Alpha ruling 2026-10-30; Gamma measurement conditions). Specification only: seat_db applies it.

Every table is dated by its OWN receipt time (a cut never sees a later row), reads named columns only, and carries a source note in its coverage:
what it is, where it came from (origin class), its retention, and that NO predictive value is shown. Text fields are web or model text: data, never
instructions; each is cut to TEXT_BYTES and marked `<field>_truncated`. Tables with text taint the seat's wake exactly as a web read does.

table: dict(source, src (table in that file), time (receipt column), cols (destination columns), expr ({dest: SQL on the source table}), text (columns cut and marked),
stale_after_s, web (True: web or model text), origin, note)."""

TEXT_BYTES = 1000

# source name: path under the desk db's directory. A pattern with ? or * is day-partitioned: the newest DAY_FILES files are read.
SOURCES = {
    'x': 'desk/x/????-??-??.db', 'news': 'desk/news/????-??-??.db', 'trending': 'desk/trending/????-??-??.db', 'narrative': 'desk/narrative/????-??-??.db',
    'narrative_v2': 'desk/narrative/live-v2/????-??-??.db', 'attention': 'desk/attention/tape.db', 'guess': 'desk/narrative_guess/narrative_guess.db', 'crawler': 'desk/crawler/crawls.db',
    'offchain': 'desk/offchain/offchain.db', 'boost': 'research/dex_boost/seen.db',
}
DAY_FILES = 7                     # day files opened per day-partitioned source: the newest feed the copy, all feed the per-day row counts

NO_PREDICTION = 'No predictive value is shown or claimed; a row is something that was recorded, not a signal.'
RETENTION_DAY = 'One file per UTC day. No prune rule was found in this producer source (searched for RETENTION in the desk producer scripts); whether another job prunes is unknown. This reader opens only the newest DAY_FILES day files (reader_horizon_start in coverage): a missing row before reader_horizon_start is outside what this tool reads, and min_receipt is the oldest row among the OPENED files only (outside the rows examined; whether older storage exists is unknown).'
RETENTION_X = 'One file per UTC day. The producer prunes day files older than MEMECOIN_DESK_X_RETENTION_DAYS (default 30; memecoin_desk_x_producer.py lines 53 and 285). This reader opens only the newest DAY_FILES day files (reader_horizon_start in coverage), so reader_horizon_start is NOT the oldest stored row: a missing row before reader_horizon_start is outside what this tool reads.'


def _t(source, src, time, cols, *, expr=None, text=(), stale, web=False, origin, note, retention=RETENTION_DAY, klass):
    return dict(klass=klass, source=source, src=src, time=time, cols=tuple(cols), expr=expr or {}, text=tuple(text), stale_after_s=stale, web=web, origin=origin,
                note='%s Origin: %s. %s Retention: %s' % (note, origin, NO_PREDICTION, retention))


INTEL = {
    'x_posts': _t('x', 'posts', 'received_at',
                  ('tweet_id', 'received_at', 'created_at_ms', 'handle', 'author_id', 'post_type', 'event', 'text', 'followers', 'views', 'likes', 'retweets'),
                  expr={'text': "json_extract(payload_json,'$.text')", 'followers': "json_extract(payload_json,'$.author.followersCount')", 'views': "json_extract(payload_json,'$.metrics.views')",
                        'likes': "json_extract(payload_json,'$.metrics.likes')", 'retweets': "json_extract(payload_json,'$.metrics.retweets')"},
                  text=('text',), stale=900, web=True, origin='X posts from the handles we follow; unverified third-party text; counts as of the receipt time only',
                  note='Posts the X collector received from the watched handles (not all of X).', retention=RETENTION_X, klass='third_party_text'),
    'x_ai_suggestions': _t('x', 'ai_suggestion', 'received_at',
                           ('tweet_id', 'received_at', 'event', 'ticker', 'ai_ticker', 'n_results', 'first_mint', 'first_symbol', 'first_name', 'first_mcap_usd', 'first_liq_usd'),
                           expr={'n_results': "json_array_length(payload_json,'$.results')", 'first_mint': "json_extract(payload_json,'$.results[0].mint')",
                                 'first_symbol': "json_extract(payload_json,'$.results[0].symbol')", 'first_name': "json_extract(payload_json,'$.results[0].name')",
                                 'first_mcap_usd': "json_extract(payload_json,'$.results[0].marketCapUsd')", 'first_liq_usd': "json_extract(payload_json,'$.results[0].liquidityUsd')"},
                           text=('first_symbol', 'first_name'), stale=900, web=True,
                           origin='MODEL OUTPUT from a third party (J7 AI suggestion of a token for a post): unvalidated, and derived from the same posts as x_posts',
                           note='For a post, the first suggested token (mint, symbol, name, market cap, liquidity) and how many were suggested.', retention=RETENTION_X, klass='model_output'),
    'news_items': _t('news', 'items', 'first_received_at', ('source', 'item_id', 'first_received_at', 'published_at', 'title', 'summary'),
                     text=('title', 'summary'), stale=7200, web=True,
                     origin='publisher text (Wikipedia top, Google Trends, Hacker News, Al Jazeera, Reddit memes); published_at is the publisher clock, the row is dated by OUR receipt',
                     note='Headlines and summaries polled every minute.', klass='publisher_text'),
    'trending_boards': _t('trending', 'boards', 'at', ('at', 'rule_version', 'coverage', 'sol_usd', 'coins', 'koth_mint', 'koth_mcap_usd', 'koth_curve_kind', 'curve_rule'),
                          stale=900, origin='computed by our trending worker from the PumpSwap tape; coverage says whether the curve side was retired or present',
                          note='A snapshot of the PumpSwap trending board per sample; coins = 0 with coverage curve_retired means no data, not no activity.', klass='derived_count'),
    'attention_events': _t('attention', 'attention', 'observed_at',
                           ('id', 'position_id', 'mint', 'signal', 'observed_at', 'source', 'fired', 'thresholds_version', 'state', 'reason'),
                           expr={'state': "json_extract(value_json,'$.state')", 'reason': "json_extract(value_json,'$.reason')"}, stale=7 * 86400,
                           origin='detector output of our attention worker for OUR OPEN DESK POSITIONS only (volume_spike, holder_dump, buyer_exodus, creator_sell); thresholds_version attn-dev-2026-10-23 is a development cohort',
                           note='NOT market-wide attention: one row per detector event on a position we held in the desk paper. fired is NULL on every row recorded so far.',
                           retention='A single file; nothing is written while no desk position is open (the newest row can be days old and the worker still alive).', klass='detector_output'),
    'attention_tape_price': _t('attention', 'tape_price', 'received_at', ('mint', 'price_sol', 'price_usd', 'slot', 'received_at', 'receipt_cut', 'updated_at'),
                               stale=7 * 86400, origin='last tape price of a mint our attention worker watched for an open desk position', note='Latest committed price per watched mint.',
                               retention='A single file keyed by mint; a row is replaced, not appended.', klass='own_receipt'),
    'narrative_guesses': _t('guess', 'guesses', 'known_at',
                            ('guess_id', 'cohort', 'source', 'model_id', 'slot_ts', 'known_at', 'expires_at', 'theme_id', 'rationale', 'confidence', 'state', 'ticker_candidates', 'name_candidates'),
                            expr={'ticker_candidates': 'ticker_candidates_json', 'name_candidates': 'name_candidates_json'},
                            text=('theme_id', 'rationale', 'ticker_candidates', 'name_candidates'), stale=4 * 3600, web=True,
                            origin='MODEL OUTPUT (the narrative guesser) from J7 suggestions, X posts and news: unvalidated, circular with x_posts / news_items / x_ai_suggestions, and the Narrative seat own lines',
                            note='A model guess of the current narrative theme with candidate tickers and names per slot.', retention='A single file; guesses are not pruned by this reader.', klass='model_output'),
    'narrative_tags': _t('narrative', 'tags', 'known_at', ('mint', 'theme', 'matched', 'rules_version', 'launch_received_at', 'known_at'), stale=3600,
                         origin='tags assigned by our narrative worker rules to curve-side launches (rules_version column); derived, unvalidated; whether any model output enters the tagging is not verified here',
                         note='Theme tag per launched mint. Reads the v1 day files desk/narrative/<UTC-day>.db written by the old narrative worker (the live-v2 generation is the narrative_v2_* tables); see source_files for the newest file and its age: a v1 table that stopped updating is stale, not quiet.', klass='derived_count'),
    'narrative_theme_agg': _t('narrative', 'theme_agg', 'at', ('at', 'theme', 'window_s', 'launches', 'band_entries', 'buy_usd', 'buyers', 'slope', 'coverage', 'rules_version'), stale=3600,
                              origin='DERIVED counts over tagged launches by our narrative worker: they inherit the tagging error, are unvalidated, and may share inputs with narrative_guesses (model output) so they are not independent of it',
                              note='Launch and buyer counts per theme and window; read coverage before the counts. Reads the v1 day files desk/narrative/<UTC-day>.db (the live-v2 generation is the narrative_v2_* tables); see source_files and stale.', klass='derived_count'),
    'narrative_token_hourly': _t('narrative', 'token_hourly', 'known_at', ('hour_start', 'kind', 'rank', 'token', 'launches', 'creators', 'prev_hour_launches', 'known_at', 'rules_version'),
                                 text=('token',), stale=3600, web=True, origin='DERIVED: creator-chosen token names ranked per hour by our narrative worker; unvalidated; shares inputs with narrative_tags and narrative_guesses',
                                 note='Most-launched token names per hour. Reads the v1 day files desk/narrative/<UTC-day>.db (the live-v2 generation is the narrative_v2_* tables); see source_files and stale.', klass='derived_count'),
    'crawler_crawls': _t('crawler', 'crawls', 'known_at', ('id', 'network_id', 'mint', 'known_at', 'input_hash', 'intent', 'model', 'call_error', 'latency_ms', 'prompt_version'),
                         text=('intent', 'call_error'), stale=3600, web=True, origin='MODEL OUTPUT of our crawler for a coin (intent label); unvalidated; inputs are not exposed',
                         note='What the crawler model labelled a coin, with its call error if the call failed.', retention='A single file, not pruned by this reader.', klass='model_output'),
    'offchain_coin_heat': _t('offchain', 'coin_heat', 'known_at',
                             ('id', 'mint', 'network_id', 'known_at', 'window_end', 'paid_boost_units', 'paid_ads', 'paid_profile', 'listing_takeover', 'dex_state', 'x_mentions_15m',
                              'x_accounts_15m', 'x_promoter_mentions_15m', 'x_state', 'crawler_intent', 'crawler_known_at', 'promoter_list_version', 'offchain_version'),
                             stale=1800, origin='DERIVED from three sources: PAID promotion counts from DexScreener (boost, ads, profile, takeover: selected by an advertiser, not organic attention; never a fill or price source), X mention counts over x_posts by a handle list (promoter_list_version), and the crawler intent label (MODEL OUTPUT, unvalidated)',
                             note='Per coin heat at a window end; read dex_state and x_state first (they say whether each source was observed).', retention='A single file, not pruned by this reader.', klass='paid_promotion+third_party_text+model_output'),
    'dex_boost_seen': _t('boost', 'seen', 'first_at', ('class', 'mint', 'first_at', 'left_censored'), stale=3600,
                         origin='PAID PROMOTION: DexScreener boost / profile / ad / takeover first seen by our forward collector (advertiser selection; never a fill or price source)',
                         note='First time a coin was seen in a DexScreener promotion class; left_censored = 1 means it was already listed when collection started.',
                         retention='A single file keyed by (class, mint).', klass='paid_promotion'),
}
INTEL.update({
    'narrative_v2_tags': _t('narrative_v2', 'tags', 'known_at', ('mint', 'theme', 'matched', 'rules_version', 'launch_received_at', 'known_at', 'name', 'symbol', 'source_rowid'),
                            text=('theme', 'name', 'symbol'), stale=3600, web=True,
                            origin='tags assigned by our narrative worker rules to observed first sightings; name and symbol are creator-chosen text; derived, unvalidated',
                            note='Theme tag per observed first sighting. ' + 'Narrative live-v2 (Epsilon, source reviewed): OBSERVED Solana first sightings of launches (not chain creations), from an initial 5,000-source-row bootstrap and then the live feed; buy flow comes from the last 5,000 retained PumpSwap receipt rows, WSOL buys only, so every flow figure is a LOWER BOUND and not market-wide or executable; creators and band_entries are NULL (unknown); a failed flow read is NULL, never 0; the theme_15m context stays unknown/curve_retired. Separate files from v1 (desk/narrative/live-v2/<UTC-day>.db); v1 is stale.', klass='derived_count'),
    'narrative_v2_theme_agg': _t('narrative_v2', 'theme_agg', 'at', ('at', 'theme', 'window_s', 'launches', 'band_entries', 'buy_usd', 'buyers', 'slope', 'coverage', 'rules_version'),
                                 text=('theme',), stale=3600, web=True,
                                 origin='DERIVED counts over tagged first sightings: they inherit the tagging error, are unvalidated, and may share inputs with narrative_guesses (model output)',
                                 note='Launch and buyer counts per theme and window; buy_usd and buyers are lower bounds; band_entries is NULL. ' + 'Narrative live-v2 (Epsilon, source reviewed): OBSERVED Solana first sightings of launches (not chain creations), from an initial 5,000-source-row bootstrap and then the live feed; buy flow comes from the last 5,000 retained PumpSwap receipt rows, WSOL buys only, so every flow figure is a LOWER BOUND and not market-wide or executable; creators and band_entries are NULL (unknown); a failed flow read is NULL, never 0; the theme_15m context stays unknown/curve_retired. Separate files from v1 (desk/narrative/live-v2/<UTC-day>.db); v1 is stale.', klass='derived_count'),
    'narrative_v2_token_hourly': _t('narrative_v2', 'token_hourly', 'known_at', ('hour_start', 'kind', 'rank', 'token', 'launches', 'creators', 'prev_hour_launches', 'known_at', 'rules_version'),
                                    text=('token',), stale=3600, web=True, origin='DERIVED: creator-chosen token names ranked per hour by our narrative worker; unvalidated; creators is NULL (unknown)',
                                    note='Most-sighted token names per hour. ' + 'Narrative live-v2 (Epsilon, source reviewed): OBSERVED Solana first sightings of launches (not chain creations), from an initial 5,000-source-row bootstrap and then the live feed; buy flow comes from the last 5,000 retained PumpSwap receipt rows, WSOL buys only, so every flow figure is a LOWER BOUND and not market-wide or executable; creators and band_entries are NULL (unknown); a failed flow read is NULL, never 0; the theme_15m context stays unknown/curve_retired. Separate files from v1 (desk/narrative/live-v2/<UTC-day>.db); v1 is stale.', klass='derived_count'),
    'narrative_v2_source_state': _t('narrative_v2', 'source_state', 'at', ('at', 'protocol', 'launch_state', 'flow_state', 'flow_rows', 'source_tip_rowid', 'cursor_rowid', 'launch_latest_at', 'bootstrap_limit', 'coverage', 'flow_fx_usd'),
                                    text=('launch_state', 'flow_state', 'coverage'), stale=3600, origin='written by our narrative worker each pass: whether the launch and flow sources were read and how far',
                                    note='Read this before the v2 counts: launch_state and flow_state say whether each source was observed, flow_rows how many filtered, deduplicated WSOL buy rows the flow covers (not the raw receipt rows scanned), coverage why a figure is NULL. ' + 'Narrative live-v2 (Epsilon, source reviewed): OBSERVED Solana first sightings of launches (not chain creations), from an initial 5,000-source-row bootstrap and then the live feed; buy flow comes from the last 5,000 retained PumpSwap receipt rows, WSOL buys only, so every flow figure is a LOWER BOUND and not market-wide or executable; creators and band_entries are NULL (unknown); a failed flow read is NULL, never 0; the theme_15m context stays unknown/curve_retired. Separate files from v1 (desk/narrative/live-v2/<UTC-day>.db); v1 is stale.', klass='own_receipt'),
})

# free text or model-derived strings reach the seat: these taint the wake like a web read. offchain_coin_heat carries the crawler intent (model output); narrative_tags and narrative_theme_agg carry themes derived from launch names.
WEB_TEXT_TABLES = frozenset(t for t, v in INTEL.items() if v['web']) | {'offchain_coin_heat', 'narrative_tags', 'narrative_theme_agg'}
WEB_TEXT_TABLES = frozenset(WEB_TEXT_TABLES | {t for t, v in INTEL.items() if v['web']})
