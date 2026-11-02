"""Crawler-only live resolution; retained activity is an observed lower bound.

No RPC/model calls. Snapshot reads close before model work. Guesser first sightings are
corrected UTC; posts and PS receipts use host time. A pass samples their clock offset.
Tickers must be unique across *all* loaded Solana sightings known by the post, even if a
second coin has no retained activity. This conservative rule avoids false uniqueness
from tape pruning. A capped metadata snapshot disables ticker resolution entirely.
"""
from collections import defaultdict
from contextlib import closing
import math
from pathlib import Path
import re
import sqlite3
import time

MINT_RE=re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')
TICKER_RE=re.compile(r'(?<![\w$])\$([A-Za-z][A-Za-z0-9_]{0,14})\b')
ACTIVE_S=86400
META_LIMIT=200000
TAPE_LIMIT=5000


def finite(x):return type(x) in (int,float) and math.isfinite(x)


def rows(path,sql,args):
    with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=.25)) as c:
        deadline=time.monotonic()+1
        c.set_progress_handler(lambda:time.monotonic()>deadline,1000)
        return c.execute(sql,args).fetchall()


class LiveResolver:
    source='guesser_ps_observed_lower_bound'

    def __init__(self,desk_dir,host_now,*,clock_offset_s):
        self.cut_host=host_now
        self.state='unavailable';self.sightings={};self.symbols=defaultdict(dict);self.activity=defaultdict(list)
        self.metadata_capped=False;self.tape_capped=False
        if not finite(clock_offset_s) or not finite(host_now):return
        d=Path(desk_dir)
        try:
            meta=rows(d/'narrative_guess/narrative_guess.db',
                "SELECT mint,symbol,first_seen_known_at FROM launches WHERE network_id='solana' ORDER BY rowid DESC LIMIT ?",(META_LIMIT+1,))
            tape=rows(d/'pumpswap/tape.db',
                'SELECT mint,received_at,network_id FROM swaps_ps INDEXED BY swaps_ps_received WHERE received_at>? AND received_at<=? ORDER BY received_at DESC LIMIT ?',
                (host_now-ACTIVE_S,host_now,TAPE_LIMIT))
        except (sqlite3.Error,OSError):return
        self.metadata_capped=len(meta)>META_LIMIT;self.tape_capped=len(tape)==TAPE_LIMIT
        for mint,symbol,known in meta[:META_LIMIT]:
            if not finite(known):continue
            known+=clock_offset_s
            if known>host_now:continue
            self.sightings[mint]=known
            if isinstance(symbol,str) and symbol.strip():self.symbols[symbol.strip().upper()][mint]=known
        for mint,received,network in tape:
            if network=='solana' and finite(received):self.activity[mint].append(received)
        self.state='observed_lower_bound'

    def active(self,mint,at):
        return (mint in self.sightings and self.sightings[mint]<=at
                and any(at-ACTIVE_S<t<=at for t in self.activity.get(mint,())))

    def resolve(self,text,at):
        if self.state=='unavailable':return None,'source_unavailable'
        if not finite(at) or at>self.cut_host:return None,'outside_snapshot'
        found={m for m in MINT_RE.findall(text or '') if self.active(m,at)}
        for ticker in {x.upper() for x in TICKER_RE.findall(text or '')}:
            if self.metadata_capped:return None,'coverage_incomplete'
            known={m for m,t in self.symbols.get(ticker,{}).items() if t<=at}
            if len(known)>1:return None,'ambiguous'
            found|={m for m in known if self.active(m,at)}
        if len(found)>1:return None,'ambiguous'
        return (next(iter(found)),None) if found else (None,None)
