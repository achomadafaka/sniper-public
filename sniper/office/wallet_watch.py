"""Watch list and drain history for the Office scammer network work (operator order 2026-10-31). Paper research only: no funded path reads any of this, it cannot arm a wallet, and a listed wallet is a seat's or crew's claim, not a finding.

The list lives in wallet_network.db. The capture worker reads a JSON copy every 30 s and records every PumpSwap swap by a listed wallet into ps_watch_events, stored pool or not.
Episodes are read from stored swap rows only: a pool outside the stored set, or a lost swap, is not seen, so a missing episode is not proof of none."""
import json
import math
import sqlite3
from pathlib import Path

from sniper.office.wallet_network import ADDRESS, MAX_TEXT

MAX_LISTED = -1  # SQLite LIMIT -1: no operator-imposed registry ceiling.
FLAT_FRACTION = 0.01     # an episode ends when the wallet holds under 1 percent of its peak
SCHEMA = '''
CREATE TABLE IF NOT EXISTS watch_list(address TEXT PRIMARY KEY,operator_id TEXT,evidence TEXT,source TEXT NOT NULL,actor_id TEXT,added_at REAL NOT NULL,removed_at REAL);
CREATE TABLE IF NOT EXISTS watch_campaign_members(campaign TEXT NOT NULL,address TEXT NOT NULL,added_at REAL NOT NULL,PRIMARY KEY(campaign,address));
CREATE TABLE IF NOT EXISTS drain_episodes(address TEXT NOT NULL,pool TEXT NOT NULL,mint TEXT,first_buy_at REAL,last_buy_at REAL,buys INTEGER,bought_quote_sol REAL,bought_share_of_pool_pct REAL,first_sell_at REAL,last_sell_at REAL,sells INTEGER,
  sold_quote_sol REAL,state TEXT NOT NULL,price_at_entry REAL,price_at_first_sell REAL,peak_price_between REAL,computed_at REAL NOT NULL,PRIMARY KEY(address,pool,first_buy_at),CHECK(state IN ('closed','open','entry_unseen')));
'''


def watch_file(desk_dir):
    return Path(desk_dir) / 'pumpswap' / 'watch_wallets.json'


def _sync(conn, desk_dir, now):
    path = watch_file(desk_dir)
    tmp = path.with_name(path.name + '.tmp')
    # Serialize the snapshot and replace with watch writers; an older export cannot win a race.
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        owners=[r[0] for r in conn.execute('SELECT address FROM watch_list WHERE removed_at IS NULL ORDER BY added_at,address LIMIT ?', (MAX_LISTED,))]
        aliases=[];coverage='owners_only_token_accounts_unavailable'
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='watch_token_accounts'").fetchone():
            # Receipt arrival can be out of chain order; same-slot conflicts stay unknown.
            conn.execute('CREATE INDEX IF NOT EXISTS watch_token_account_latest ON watch_token_accounts(account)')
            try:
                # Check conflicts per identity; validate every eligible balance snapshot below.
                rows=conn.execute("""WITH latest AS MATERIALIZED (
                    SELECT a.account,MAX(t.slot) AS slot FROM watch_token_accounts a
                    LEFT JOIN watch_flow_timing t ON t.source=a.source AND t.flow_id=a.flow_id AND t.signature=a.signature
                    WHERE a.known_at<=? GROUP BY a.account),
                    candidates AS MATERIALIZED (
                    SELECT DISTINCT a.account,a.owner,a.mint,a.program,a.decimals,t.slot
                    FROM latest JOIN watch_token_accounts a ON a.account=latest.account
                    CROSS JOIN watch_list w ON w.address=a.owner AND w.removed_at IS NULL
                    CROSS JOIN watch_flow_timing t ON t.source=a.source AND t.flow_id=a.flow_id AND t.signature=a.signature AND t.slot=latest.slot
                    WHERE a.known_at>0 AND a.known_at<=? AND t.flow_state='known' AND t.graph_commit_after>=a.known_at AND t.graph_commit_after<=? AND typeof(t.slot)='integer' AND t.slot>0
                    ), safe AS MATERIALIZED (
                    SELECT a.* FROM candidates a WHERE NOT EXISTS(SELECT 1 FROM watch_token_accounts newer LEFT JOIN watch_flow_timing n ON n.source=newer.source AND n.flow_id=newer.flow_id AND n.signature=newer.signature
                        WHERE newer.account=a.account AND newer.known_at<=?
                        AND (n.slot>a.slot OR (n.slot=a.slot OR n.slot IS NULL OR n.graph_commit_after IS NULL OR n.flow_state!='known') AND (newer.owner IS NOT a.owner OR newer.mint IS NOT a.mint OR newer.program IS NOT a.program OR newer.decimals IS NOT a.decimals OR newer.post_atomic IS NULL)))
                    AND NOT EXISTS(SELECT 1 FROM watch_list removed WHERE removed.address=a.account AND removed.removed_at IS NOT NULL)
                    ) SELECT a.account,a.owner,a.mint,a.program,a.decimals,a.post_atomic,a.state,a.reason,a.known_at,t.graph_commit_after
                    FROM safe s JOIN watch_token_accounts a INDEXED BY watch_token_account_latest ON a.account=s.account AND a.owner IS s.owner AND a.mint IS s.mint AND a.program IS s.program AND a.decimals IS s.decimals
                    JOIN watch_flow_timing t ON t.source=a.source AND t.flow_id=a.flow_id AND t.signature=a.signature AND t.slot=s.slot
                    WHERE a.known_at>0 AND a.known_at<=? AND t.flow_state='known' AND t.graph_commit_after>=a.known_at AND t.graph_commit_after<=? AND typeof(t.slot)='integer' AND t.slot>0 ORDER BY a.account""",(now,now,now,now,now,now))
                from .wallet_token_flow import TOKEN,TOKEN22,_address,_amount
                for account,owner,mint,program,decimals,post,state,reason,known,after in rows:
                    try:
                        if program not in (TOKEN,TOKEN22) or type(decimals) is not int or not 0<=decimals<=255:continue
                        if not (state=='known' and reason is None or state=='unknown' and reason=='token_balance_side_missing'):continue
                        if post is None:continue  # A closed account is not a current alias.
                        _amount(post);_address(owner);_address(mint);_address(account)
                        aliases.append(account)
                    except (ValueError,TypeError):continue
                aliases=sorted(set(aliases));coverage='observed_owner_and_token_accounts_only'
            except sqlite3.Error:
                aliases=[]
        tmp.write_text(json.dumps(dict(version=1,written_at=now,wallets=list(dict.fromkeys(owners+aliases)),owner_wallets=owners,token_accounts=aliases,coverage=coverage)))
        tmp.replace(path)


def _add_arguments(address,operator_id,evidence,campaign_limit):
    if not (isinstance(address, str) and ADDRESS.match(address)) or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > MAX_TEXT \
            or (operator_id is not None and (not isinstance(operator_id, str) or len(operator_id) > 80)):
        return dict(state='refused', reason='bad_arguments')
    if campaign_limit is not None and (type(campaign_limit) is not int or campaign_limit!=-1 and campaign_limit<1 or not operator_id):
        return dict(state='refused',reason='bad_arguments')

def _add_on_connection(net,conn,*,address, operator_id, evidence, source, actor, now, campaign_limit=None,_listed=False):
    """Caller owns transaction and must export the committed batch before advancing its cursor."""
    why=_add_arguments(address,operator_id,evidence,campaign_limit)
    if why:return why
    rows_added=2 if campaign_limit is not None else 1
    why = net._refusal(conn, now, rows=rows_added)    # campaign claims share the same transaction and row cap
    if why:
        return dict(state='refused', reason=why)
    if campaign_limit is not None:
        if conn.execute('SELECT 1 FROM watch_list WHERE address=?',(address,)).fetchone():
            return dict(state='refused',reason='campaign_watch_existing')
        # Membership survives removals and legacy relabelling of the mutable watch row.
        if campaign_limit>=0 and conn.execute('SELECT count(*) FROM watch_campaign_members WHERE campaign=?',(operator_id,)).fetchone()[0]>=campaign_limit:
            return dict(state='refused',reason='campaign_watch_full')
    n = conn.execute('SELECT count(*) FROM watch_list WHERE removed_at IS NULL AND address!=?', (address,)).fetchone()[0] if _listed or MAX_LISTED>=0 else None
    if MAX_LISTED>=0 and n >= MAX_LISTED:
        return dict(state='refused', reason='watch_list_full', max=MAX_LISTED)
    conn.execute('INSERT INTO watch_list VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(address) DO UPDATE SET operator_id=excluded.operator_id,evidence=excluded.evidence,removed_at=NULL', (address, operator_id, evidence, source, actor, now))
    if campaign_limit is not None:
        conn.execute('INSERT INTO watch_campaign_members VALUES(?,?,?)',(operator_id,address,now))
    net._count(conn, now, rows=rows_added)
    return dict(state='added',**({'listed':n+1} if _listed else {}))


def add(net, desk_dir, *, address, operator_id, evidence, source, actor, now, campaign_limit=None):
    """evidence is required: an unexplained entry helps nobody. Re-adding updates operator and evidence and reinstates."""
    why=_add_arguments(address,operator_id,evidence,campaign_limit)
    if why:return why
    conn=net._conn()
    try:
        conn.executescript(SCHEMA)
        conn.execute('BEGIN IMMEDIATE')
        result=_add_on_connection(net,conn,address=address,operator_id=operator_id,evidence=evidence,
                                  source=source,actor=actor,now=now,campaign_limit=campaign_limit,_listed=True)
        if result['state']!='added':return result
        conn.commit()
        _sync(conn,desk_dir,now)
        return result
    finally:conn.close()


def remove(net, desk_dir, *, address, now):
    conn = net._conn()
    try:
        conn.executescript(SCHEMA)
        got = conn.execute('UPDATE watch_list SET removed_at=? WHERE address=? AND removed_at IS NULL', (now, address)).rowcount
        conn.commit()
        _sync(conn, desk_dir, now)
        return dict(state='removed' if got else 'not_listed')
    finally:
        conn.close()


def read_list(net):
    conn = net._conn()
    try:
        conn.executescript(SCHEMA)
        return [dict(r) for r in conn.execute('SELECT * FROM watch_list WHERE removed_at IS NULL ORDER BY added_at')]
    finally:
        conn.close()


def events(desk_dir, *, now, since=None, address=None, limit=100):
    """Newest listed-wallet swaps, each with size against the pool's real quote before it. A missing pre reserve leaves that share null."""
    limit = limit if type(limit) is int and 1 <= limit <= 500 else None
    if limit is None or (since is not None and type(since) not in (int, float)):
        return dict(state='refused', reason='bad_arguments', items=[])
    try:
        conn = sqlite3.connect((Path(desk_dir) / 'pumpswap' / 'tape.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=1.0)
    except (sqlite3.Error, OSError):
        return dict(state='unknown', reason='tape_unreadable', items=[])
    conn.row_factory = sqlite3.Row
    try:
        where, args = ['received_at<=?'], [now]
        if since is not None:
            where.append('received_at>?'); args.append(since)
        if address:
            where.append('actor=?'); args.append(address)
        rows = conn.execute('SELECT * FROM ps_watch_events WHERE %s ORDER BY received_at DESC LIMIT ?' % ' AND '.join(where), (*args, limit + 1)).fetchall()
    except sqlite3.OperationalError as exc:
        return dict(state='unknown', reason='no_watch_table' if 'no such table' in str(exc) else 'tape_unreadable', items=[])
    except sqlite3.Error:
        return dict(state='unknown', reason='tape_unreadable', items=[])
    finally:
        conn.close()
    items = []
    for r in rows[:limit]:
        item = dict(r)
        try:
            item['size_pct_of_pool_quote'] = round(int(r['quote_atomic']) / int(r['pre_quote_reserve_atomic']) * 100, 3)
        except (TypeError, ValueError, ZeroDivisionError):
            item['size_pct_of_pool_quote'] = None
        items.append(item)
    return dict(state='known', as_of=now, truncated=len(rows) > limit, items=items, note='Listed wallets are claims. Records only from the worker start with this list; no row is not proof of no trade before that.')


def episodes_from_rows(rows):
    """rows: (received_at, pool, mint, is_buy, base_atomic, quote_atomic, pre_quote) oldest first for ONE wallet. -> episode dicts. A sell with no seen buy starts entry_unseen: the entry predates the tape."""
    out, live = [], {}
    def close(pool, state):
        e = live.pop(pool)
        e['state'] = state
        out.append(e)
    for at, pool, mint, buy, base, quote, pre, *_identity in rows:
        try:
            base, quote = int(base), int(quote)
        except (TypeError, ValueError):
            continue
        e = live.get(pool)
        if e is None:
            e = live[pool] = dict(pool=pool, mint=mint, first_buy_at=at if buy else None, last_buy_at=None, buys=0, bought_quote_sol=0.0, bought_share_of_pool_pct=None, first_sell_at=None, last_sell_at=None, sells=0, sold_quote_sol=0.0,
                                  held=0, peak=0, unseen=not buy, price_at_entry=None, price_at_first_sell=None, peak_price_between=None)
        price = quote / base if base else None
        if buy:
            if e['first_sell_at'] is not None and e['held'] < FLAT_FRACTION * e['peak']:
                close(pool, 'entry_unseen' if e['unseen'] else 'closed')
                e = live[pool] = dict(pool=pool, mint=mint, first_buy_at=at, last_buy_at=None, buys=0, bought_quote_sol=0.0, bought_share_of_pool_pct=None, first_sell_at=None, last_sell_at=None, sells=0, sold_quote_sol=0.0,
                                      held=0, peak=0, unseen=False, price_at_entry=None, price_at_first_sell=None, peak_price_between=None)
            e['buys'] += 1; e['last_buy_at'] = at; e['bought_quote_sol'] += quote / 1e9; e['held'] += base; e['peak'] = max(e['peak'], e['held'])
            if e['price_at_entry'] is None:
                e['price_at_entry'] = price
                try: e['bought_share_of_pool_pct'] = round(quote / int(pre) * 100, 3)
                except (TypeError, ValueError, ZeroDivisionError): pass
        else:
            if e['first_sell_at'] is None:
                e['first_sell_at'] = at; e['price_at_first_sell'] = price
            e['sells'] += 1; e['last_sell_at'] = at; e['sold_quote_sol'] += quote / 1e9; e['held'] -= base
        if price is not None and e['first_sell_at'] is None:
            e['peak_price_between'] = max(e['peak_price_between'] or 0, price)
    for pool in list(live):
        e = live[pool]
        close(pool, 'entry_unseen' if e['unseen'] else 'closed' if e['first_sell_at'] is not None and e['held'] < FLAT_FRACTION * e['peak'] else 'open')
    for e in out:
        for k in ('held', 'peak', 'unseen'):
            e.pop(k)
    return out


def scan(conn, wallets):
    """{wallet: rows} from one tape's swaps_ps and, where it exists, ps_watch_events (a listed wallet's swaps on unstored pools live only there). Each row ends with signature and ordinal, its identity for dedupe. Whole-table read: run off the tool path."""
    marks = ','.join('?' * len(wallets))
    found = {w: [] for w in wallets}
    cols = 'actor,received_at,pool,mint,is_buy,base_atomic,quote_atomic,pre_quote_reserve_atomic,signature,ordinal'
    tables = ['swaps_ps'] + (['ps_watch_events'] if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ps_watch_events'").fetchone() else [])
    for table in tables:
        for actor, *row in conn.execute('SELECT %s FROM %s WHERE actor IN (%s)' % (cols, table, marks), tuple(wallets)):
            found[actor].append(tuple(row))
    return found


def unique_swaps(rows):
    """One row per (signature, ordinal): the same swap can be in two tables or days; two swaps of equal size and time cannot collapse."""
    return list({(r[7], r[8]): r for r in rows}.values())


def store_episodes(net, found, now):
    conn = net._conn()
    try:
        conn.executescript(SCHEMA)
        n = 0
        if net._size() >= net.max_bytes:
            raise ValueError('disk_cap')                 # the batch fails loudly and the old episodes stay
        with conn:
            for wallet, rows in found.items():
                conn.execute('DELETE FROM drain_episodes WHERE address=?', (wallet,))
                for e in episodes_from_rows(sorted(rows, key=lambda r: r[0])):
                    conn.execute('INSERT OR REPLACE INTO drain_episodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (wallet, e['pool'], e['mint'], e['first_buy_at'] if e['first_buy_at'] is not None else e['first_sell_at'], e['last_buy_at'], e['buys'], e['bought_quote_sol'], e['bought_share_of_pool_pct'],
                                 e['first_sell_at'], e['last_sell_at'], e['sells'], e['sold_quote_sol'], e['state'], e['price_at_entry'], e['price_at_first_sell'], e['peak_price_between'], now))
                    n += 1
                # Checked on the database as it would commit: over the cap rolls back and the old episodes stay.
                if conn.execute('PRAGMA page_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0] >= net.max_bytes:
                    raise ValueError('disk_cap')
        return n
    finally:
        conn.close()


def read_episodes(net, *, address, limit=100):
    if not (isinstance(address, str) and ADDRESS.match(address)) or type(limit) is not int or not 1 <= limit <= 200:
        return dict(state='refused', reason='bad_arguments', items=[])
    conn = net._conn()
    try:
        conn.executescript(SCHEMA)
        rows = conn.execute('SELECT * FROM drain_episodes WHERE address=? ORDER BY first_buy_at DESC LIMIT ?', (address, limit)).fetchall()
        return dict(state='known', items=[dict(r) for r in rows], computed_at=rows[0]['computed_at'] if rows else None, note='Stored rows only. No episodes is not no history: the batch may not have run for this wallet, or its pools were not stored.')
    finally:
        conn.close()


FANOUT_HUB = 100     # funding this many wallets looks like shared infrastructure; a heuristic label, never proof of an exchange


def _known_tip_receivers():
    """Pinned submission-service accounts already used by the research collector; no RPC."""
    from sniper.services.memecoin_desk_txshape import JITO_TIP_ACCOUNTS
    from sniper.services.memecoin_fee_reconciler import tip_accounts
    return frozenset(JITO_TIP_ACCOUNTS) | tip_accounts()


def _known_infrastructure():
    """Research boundary union; the frozen cluster catalogue and funded policy stay unchanged."""
    try:
        from sniper.services.memecoin_cluster_rpc import known_funders
        return frozenset(known_funders()['addresses']) | _known_tip_receivers()
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return None


def _service_boundary(addresses,infrastructure,tips):
    if infrastructure is None:
        return dict(state='unknown',reason='infrastructure_catalogue_unknown',addresses=[],role=None,control_link_eligible=None)
    matches=sorted({a for a in addresses if isinstance(a,str)} & infrastructure)
    role='known_submission_tip_receiver' if set(matches)&tips else 'known_shared_infrastructure' if matches else None
    return dict(state='known',addresses=matches,role=role,control_link_eligible=False if matches else None,
        basis='current_pinned_catalogue_metadata_not_payment_or_ownership_proof')


HUB_CAMPAIGN_QUERY = '''SELECT src,dst,source,proof_state,observed_at,known_at,proof_kind,proof_ref,stale,method_version FROM (
 SELECT *,ROW_NUMBER() OVER (PARTITION BY src ORDER BY observed_at,known_at,edge_id) AS member_rank
 FROM edges WHERE network_id='solana' AND type='funded_by' AND proof_state='verified' AND source='host'
 AND dst=? AND observed_at>=? AND observed_at<? AND observed_at<=? AND known_at<=?
 AND observed_at>0 AND known_at>0 AND typeof(observed_at) IN ('integer','real') AND typeof(known_at) IN ('integer','real')
 AND ((proof_kind='chain_fetch' AND method_version='wallet-watch-receipt-v1')
   OR (proof_kind='research_stream' AND method_version='wallet-watch-stream-v1'))
) WHERE member_rank=1 ORDER BY src LIMIT ?'''


def funding_links(net, *, address, hops=2, limit=50, as_of=None, verified_only=False):
    """Who funded this wallet, walking funded_by edges, and who else the same funder paid. Exchange and hub funders are shown as excluded and never walked: two wallets sharing Binance are not linked. Claimed edges stay labelled. No edge is not no funder."""
    if not (isinstance(address, str) and ADDRESS.match(address)) or type(hops) is not int or not 1 <= hops <= 3 or type(limit) is not int or not 1 <= limit <= 200:
        return dict(state='refused', reason='bad_arguments')
    cutoff = net.clock() if as_of is None else as_of
    if type(cutoff) not in (int, float) or not math.isfinite(cutoff) or not 0 < cutoff <= net.clock() or type(verified_only) is not bool:
        return dict(state='refused', reason='bad_arguments')
    known = _known_infrastructure()
    tips = _known_tip_receivers() if known is not None else frozenset()
    # The same causal predicate covers direct edges, siblings and hub counts.
    predicate = "known_at<=? AND (observed_at IS NULL OR observed_at<=?)" + (" AND proof_state='verified'" if verified_only else '')
    causal = (cutoff, cutoff)
    try:
        conn = sqlite3.connect(net.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1.)
    except (sqlite3.Error, OSError):
        return dict(state='unknown', reason='store_unreadable', as_of=cutoff)
    conn.row_factory = sqlite3.Row
    budget = [5000]
    def progress():
        budget[0] -= 1
        return budget[0] < 0
    conn.set_progress_handler(progress, 1000)
    try:
        chain, excluded, siblings, seen, frontier = [], [], [], {address}, [address]
        edges_truncated = siblings_truncated = False
        hub_funding=[];hub_campaigns={}
        for hop in range(1, hops + 1):
            nxt = []
            for wallet in frontier:
                edges = conn.execute("SELECT src,dst,source,proof_state,observed_at,known_at,proof_kind,proof_ref,stale,method_version FROM edges WHERE network_id='solana' AND type='funded_by' AND src=? AND " + predicate + " ORDER BY proof_state='verified' DESC,known_at DESC LIMIT 11", (wallet, *causal)).fetchall()
                edges_truncated = edges_truncated or len(edges) > 10
                for r in edges[:10]:
                    funder = r['dst']
                    fanout = conn.execute("SELECT count(DISTINCT src) FROM edges WHERE network_id='solana' AND type='funded_by' AND dst=? AND " + predicate, (funder, *causal)).fetchone()[0]
                    edge = dict(dict(r), hop=hop, funder_wallets_paid=fanout)
                    why = 'known_infrastructure' if known and {wallet,funder}&known else 'infrastructure_catalogue_unknown' if known is None else 'high_fanout_funder' if fanout >= FANOUT_HUB else None
                    if why:
                        excluded.append(dict(edge, excluded=why,service_boundary=_service_boundary([wallet,funder],known,tips)))
                        event_at=r['observed_at'];known_at=r['known_at']
                        strict=(r['source']=='host' and r['proof_state']=='verified'
                            and (r['proof_kind'],r['method_version']) in {
                                ('chain_fetch','wallet-watch-receipt-v1'),('research_stream','wallet-watch-stream-v1')}
                            and type(known_at) in (int,float) and math.isfinite(known_at) and known_at>0
                            and (event_at is None or type(event_at) in (int,float) and math.isfinite(event_at) and event_at>0))
                        if hop==1 and fanout>=FANOUT_HUB and why in {'high_fanout_funder','infrastructure_catalogue_unknown'} and strict:
                            hub_funding.append(dict(edge,hub_relation='direct_payment_only',observed_state='unknown' if event_at is None else 'known',
                                reason='observed_time_unknown' if event_at is None else None,
                                infrastructure_state='unknown' if known is None else 'known',ownership_proven=False,control_link_eligible=False))
                            if event_at is not None and known is not None:
                                bucket=math.floor(event_at/60)*60;key=(funder,bucket)
                                if key not in hub_campaigns:
                                    member_limit=min(limit,10)
                                    members=conn.execute(HUB_CAMPAIGN_QUERY,(funder,bucket,bucket+60,cutoff,cutoff,member_limit+1)).fetchall()
                                    hub_campaigns[key]=dict(funder=funder,bucket_start=bucket,bucket_end=bucket+60,
                                        members=[dict(src=m['src'],observed_at=m['observed_at'],known_at=m['known_at'],proof_ref=m['proof_ref']) for m in members[:member_limit] if m['src'] not in known],members_truncated=len(members)>member_limit,
                                        state='partial' if len(members)>member_limit else 'known',ownership_proven=False,
                                        relation='temporal_coincidence',note='Fixed epoch-minute funding bucket; may split one campaign or mix launches. Not control or complete membership.')
                        continue
                    chain.append(edge)
                    if funder not in seen:
                        seen.add(funder)
                        nxt.append(funder)
                        found = conn.execute("SELECT DISTINCT src,proof_state FROM edges WHERE network_id='solana' AND type='funded_by' AND dst=? AND src!=? AND " + predicate + " ORDER BY src LIMIT ?", (funder, wallet, *causal, limit + 1)).fetchall()
                        siblings_truncated = siblings_truncated or len(found) > limit
                        for s in found[:limit]:
                            sibling=dict(wallet=s['src'],shared_funder=funder,proof_state=s['proof_state'])
                            if known and s['src'] in known:
                                excluded.append(dict(sibling,excluded='known_infrastructure',service_boundary=_service_boundary([s['src']],known,tips)))
                            else:siblings.append(sibling)
            frontier = nxt
        return dict(state='known' if known is not None else 'partial', reason=None if known is not None else 'known_funder_list_unreadable', funding_chain=chain, excluded_funders=excluded, siblings=siblings[:limit], siblings_truncated=siblings_truncated or len(siblings) > limit, funding_edges_truncated=edges_truncated,
                    hub_funding=hub_funding,hub_campaigns=[hub_campaigns[k] for k in sorted(hub_campaigns)],
                    as_of=cutoff, verified_only=verified_only, fanout_hub_at=FANOUT_HUB, note='Evidence, not control. A shared funder is a lead. high_fanout_funder is a heuristic label. Absent edges are not absent funders: use network_expand.')
    except sqlite3.Error:
        return dict(state='unknown', reason='store_read_budget' if budget[0] < 0 else 'store_unreadable', as_of=cutoff)
    finally:
        conn.close()


def expand_known_group_watch(path, desk_dir, addresses, *, now_host):
    """Research-only preparation; never reinstate an explicitly removed watch row."""
    from sniper.office.wallet_network import WalletNetwork
    net = WalletNetwork(path,busy_timeout=0)
    active = {row['address'] for row in read_list(net)}
    result = {}
    for address in addresses:
        if address in active:
            continue
        result[address] = add(net,desk_dir,address=address,operator_id='known-group-v1',
            evidence='prior-known group observed funding preparation',source='host',actor='known-group-tail',
            now=now_host,campaign_limit=-1)
        if result[address]['state'] == 'added':
            active.add(address)
    return result
