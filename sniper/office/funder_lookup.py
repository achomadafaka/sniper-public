"""Who first funded a wallet, without paging its history (Forensics' need for busy wallets, where a signature walk reads unknown after 3,000 signatures).
Order: what we already hold (the desk cluster cache, the wallet-graph funders, the scanner funders), then ONE provider funded-by call for a wallet nobody resolved.
Read-only. The caller owns the key, the pacing, the quota stop and the budget: `fetch(wallet) -> (http_status, body)` is injected, so this module never sees a key or a URL.
A funder is evidence with a transaction id, not proof of control: an exchange or a program can fund a wallet. Absent, unknown and conflict are three different answers."""
import json
import sqlite3

from sniper.services.memecoin_cluster_rpc import is_address

LOCAL = ('cluster_funder_cache', 'wallet_graph_funders', 'runner_wallet_funders')


def _detail(raw):
    """The decoded source_detail object; empty/NULL is {}, anything that is not a JSON object (a list, a string) raises so the source lands in unreadable_sources."""
    out = json.loads(raw) if raw else {}
    if not isinstance(out, dict): raise ValueError('source_detail_not_object')
    return out


def _ro(path):
    try:
        return sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=2)
    except sqlite3.Error:
        return None


def local(wallet, *, cluster_db=None, graph_db=None, runner_db=None):
    """-> (records, unreadable). One record per local source that names a funder: {funder, source, signature, slot, date, read_at}. A source that cannot be read is listed, never read as empty."""
    out, bad = [], []
    for name, path, sql, cook in (
        ('cluster_funder_cache', cluster_db, "SELECT funder,funder_source,source_detail,resolved_at FROM cluster_funder_cache WHERE owner=? AND status='resolved' AND funder IS NOT NULL",
         lambda r: dict(funder=r[0], source='cluster_funder_cache:%s' % r[1], read_at=r[3], **{k: _detail(r[2]).get(k) for k in ('signature', 'slot', 'date')})),
        ('wallet_graph_funders', graph_db, "SELECT funder,funding_sig,funded_at,read_at FROM memecoin_wallet_funders WHERE wallet=? AND status='resolved' AND funder IS NOT NULL",
         lambda r: dict(funder=r[0], source='wallet_graph_funders', signature=r[1], slot=None, date=r[2], read_at=r[3])),
        ('runner_wallet_funders', runner_db, "SELECT funder,resolved_at FROM wallet_funders WHERE address=? AND status='resolved' AND funder IS NOT NULL",
         lambda r: dict(funder=r[0], source='runner_wallet_funders', signature=None, slot=None, date=None, read_at=r[1]))):
        if path is None: continue
        conn = _ro(path)
        if conn is None: bad.append(name); continue
        try: out += [cook(r) for r in conn.execute(sql, (wallet,))]
        except (sqlite3.Error, ValueError, TypeError): bad.append(name)
        finally: conn.close()
    return [r for r in out if is_address(r['funder']) and r['funder'] != wallet], bad


def funded_by(wallet, fetch):
    """One provider call. -> {state: resolved|absent|unknown, ...}. absent only when the provider says it knows no funder (404, or none/self); every failure is unknown with its reason, and a 429 sets quota=True so the caller can stop for the day."""
    try:
        status, body = fetch(wallet)
    except OSError:
        return dict(state='unknown', reason='provider_unreachable')
    if status == 404: return dict(state='absent', reason='provider_404')
    if status in (401, 403): return dict(state='unknown', reason='provider_auth')
    if status == 429: return dict(state='unknown', reason='provider_quota', quota=True)
    if status != 200 or not isinstance(body, dict): return dict(state='unknown', reason='provider_%s' % status)
    if 'funder' not in body: return dict(state='unknown', reason='provider_malformed')     # a missing field is a bad answer; only an explicit null (or self) is "no funder"
    funder = body['funder']
    if funder is None or funder == wallet: return dict(state='absent', reason='provider_no_funder')
    if not is_address(funder): return dict(state='unknown', reason='provider_malformed')
    return dict(state='resolved', funder=funder, source='helius_funded_by', signature=body.get('signature'), slot=body.get('slot'), date=body.get('date'),
                funder_type=body.get('funderType'))


def funder_of(wallet, *, cluster_db=None, graph_db=None, runner_db=None, fetch=None):
    """The first funder of `wallet`. state: resolved (one funder), conflict (sources name different funders: all listed, none chosen), absent (the provider knows none),
    unknown (not found locally and no provider answer). Local answers cost no credit; the provider is asked only when nothing local resolves it."""
    if not is_address(wallet): return dict(state='unknown', reason='wallet_invalid')
    found, bad = local(wallet, cluster_db=cluster_db, graph_db=graph_db, runner_db=runner_db)
    funders = {r['funder'] for r in found}
    if len(funders) == 1: return dict(state='resolved', funder=next(iter(funders)), evidence=found, unreadable_sources=bad, provider_called=False)
    if len(funders) > 1: return dict(state='conflict', funders=sorted(funders), evidence=found, unreadable_sources=bad, provider_called=False)
    if fetch is None: return dict(state='unknown', reason='not_held_locally_no_provider', unreadable_sources=bad, provider_called=False)
    got = funded_by(wallet, fetch)
    return dict(got, unreadable_sources=bad, provider_called=True, **({'evidence': [got]} if got['state'] == 'resolved' else {}))
