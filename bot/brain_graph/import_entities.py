"""Exact identifier extraction. Free prose never creates a guessed identifier."""
import re
import subprocess

HEX = re.compile(r'(?<![0-9A-Za-z_/.:-])([0-9a-f]{7,40})(?![0-9A-Za-z_])')
KNOB = re.compile(r'\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,}\b')
PATH = re.compile(r'(?<![\w./-])((?:scripts|sniper|bot|tests|docs|static|templates|index|knowledge|data|logs|archive|\.claude|\.github)/[\w./@+-]*\.[A-Za-z0-9]{1,6})\b')
CHAINS = [('4663', ('robin', 'robinhood', 'Robinhood')), ('8453', ('Base',)),
          ('56', ('BSC', 'bnb')), ('5042', ('Arc',)), ('solana', ('Solana', 'solana', 'sol'))]
EXTERNAL_TABLES = {'pool_ticks','evm_position_pools','volume_feed','volume_feed_lag','block_times','pool_meta',
                  'brain_facts','brain_entities','brain_episodes','brain_links','brain_sections','brain_views','brain_health','brain_judgements'}
# Explicit corpus names only. Unknown names remain prose until curated.
CURATED = {
    'venue': [('Pump', ('Pump.fun','PumpSwap'), 'memecoin/solana'), ('Pons', ('Pons V1',), 'memecoin/4663'),
              ('Uniswap', ('Uniswap V2','Uniswap V3','Uniswap V4'), 'memecoin'),
              ('PancakeSwap', (), 'memecoin/56'), ('Bags', (), 'memecoin'), ('Raydium', (), 'memecoin/solana')],
    'provider': [(n, (), 'infra/rpc') for n in ('publicnode','DexScreener','Jupiter','Helius','Binance','GMGN','Alchemy','Fomo','Ankr','QuickNode')],
    'lane': [(n, (), 'memecoin') for n in ('slot-lane','onchain-detections','j7','fastlane','Radar','V6')],
    'process': [(n, (), 'infra/runtime') for n in ('memecoin_runner_loop','brain_nightly','memecoin_watchdog')],
    'gate': [(n, (), 'memecoin/gates') for n in ('post_peak','chase cap','age gate','route safety','kill switch')],
    'exit_rail': [(n, (), 'memecoin/exits') for n in ('trailing stop','derisk','catastrophe','source sell mirror','stop loss')],
}


def resolve_commits(tokens, repo):
    tokens = sorted(t for t in tokens if re.fullmatch('[0-9a-f]{7,40}', t))
    if not tokens:
        return {}
    result = subprocess.run(['git','-C',str(repo),'cat-file','--batch-check=%(objectname) %(objecttype)'],
                            input='\n'.join(tokens) + '\n', text=True, capture_output=True, timeout=60, check=True)
    return {t: line.split()[0] for t, line in zip(tokens, result.stdout.splitlines())
            if len(line.split()) == 2 and line.split()[1] == 'commit'}


def fact_section(front, mtype):
    if mtype == 'feedback':
        return 'crew/methods'
    return {'memecoin':'memecoin','crypto5m':'crypto5m','hyperliquid':'hyperliquid',
            'wallet-analyzer':'wallet-analyzer','sports/markets':'wallet-analyzer',
            'brain/infra':'infra/runtime'}.get(front, 'research/studies')


def path_section(path):
    if path.startswith(('sniper/services/memecoin_', 'sniper/execution/', 'sniper/feeds/')):
        return 'memecoin'
    if path.startswith('scripts/deploy') or 'watchdog' in path:
        return 'infra/deploy'
    if path.startswith(('scripts/research/', 'docs/audits/')):
        return 'research/studies'
    if path.startswith(('bot/brain_graph/', 'scripts/brain')):
        return 'brain'
    return 'infra/runtime'


def knob_section(name):
    suffix = name.rsplit('_', 1)[-1]
    if suffix in {'4663','8453','56','5042'}:
        return 'memecoin/' + suffix
    for pattern, section in [('STOP|TP|TRAIL|EXIT|VOLDEATH|MIRROR','memecoin/exits'),
                             ('CHASE|MCAP|VOLUME|TURNOVER|FLOOR|CEILING|AGE','memecoin/gates'),
                             ('CLIP|SIZE|EXPOSURE|CONCURRENT|BANKROLL','memecoin/sizing'),
                             ('^MEMECOIN_','memecoin'), ('RPC|HELIUS|ALCHEMY|_URL','infra/rpc'),
                             ('^HL_','hyperliquid'), ('CRYPTO5M','crypto5m')]:
        if re.search(pattern, name):
            return section
    return 'infra/runtime'


def _word(name, text):
    return name in text and re.search(r'(?<![\w])' + re.escape(name) + r'(?![\w])', text)


def extract(text, *, commits, tables, handles, knob_ok):
    out = {}
    def add(kind, name, section):
        out.setdefault((kind, name), dict(type=kind, name=name, section=section, aliases=[]))
    for token in HEX.findall(text):
        if token in commits:
            add('commit', commits[token], fact_section('other', None))
    for path in PATH.findall(text):
        add('path', path.rstrip('.'), path_section(path))
    for knob in sorted(set(KNOB.findall(text))):
        if knob_ok(knob):
            add('knob', knob, knob_section(knob))
    for table in sorted(set(tables) | EXTERNAL_TABLES):
        if table not in text:
            continue
        section = 'memecoin' if table.startswith('memecoin_') else 'brain' if table.startswith('brain_') else 'hyperliquid' if table.startswith('hl_') else 'infra/db'
        if _word(table, text):
            add('table', table, section)
        for col in re.findall(r'(?<![\w/.-])' + re.escape(table) + r'\.([a-zA-Z_][a-zA-Z0-9_]*)\b', text):
            if col not in {'py','sh','md','sql','json','jsonl','db','sqlite','csv','yaml','yml','toml','js','html'}:
                add('column', table + '.' + col, section)
    for chain, aliases in CHAINS:
        if (chain.isdigit() and re.search(r'(?<![\w$])' + chain + r'(?!\w)', text)) or any(_word(a,text) for a in aliases):
            add('chain', chain, 'memecoin/' + chain)
    for pos in re.findall(r'\b(?:position|pos|positions)\s+#?(\d+)\b', text, re.I):
        add('position', pos, 'memecoin')
    for handle in sorted(handles):
        if len(handle) >= 4 and _word(handle,text):
            add('wallet', handle, 'memecoin')
    for kind, entries in CURATED.items():
        for name, aliases, section in entries:
            if any(_word(a, text) for a in (name, *aliases)):
                add(kind, name, section)
    return list(out.values())


def evidence_items(entities):
    mapping = {'commit':'sha','path':'path','knob':'knob','column':'column'}
    return [{'type':mapping[e['type']], 'ref':e['name']} for e in entities if e['type'] in mapping][:8]
