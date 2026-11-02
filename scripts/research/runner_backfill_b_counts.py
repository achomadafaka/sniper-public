import sqlite3, math, collections, datetime as dt
B=sqlite3.connect('file:data/research/2026-10-21-runner-backfill-b.sqlite?mode=ro',uri=True)
L=sqlite3.connect('file:sniper_tracker.db?mode=ro',uri=True)
import json
fx={dt.datetime.fromtimestamp(c[0],dt.timezone.utc).strftime('%Y-%m-%d'):(c[1]+c[2])/2 for c in json.load(open('data/research/2026-10-21-sol-usd-daily-coinbase.json'))}  # Coinbase daily (low+high)/2
days=sorted(fx)
def solusd(ts):
    d=dt.datetime.fromtimestamp(ts,dt.timezone.utc).strftime('%Y-%m-%d')
    prior=[x for x in days if x<=d]; return fx[prior[-1]] if prior else None
S={m:(c,st) for m,c,st in B.execute("select mint,created_ts,status from sample")}
tr=collections.defaultdict(list)
for m,bt,slot,ix,o,vs,vt in B.execute("select mint,block_time,slot,tx_index,ordinal,vsol,vtok from trades where vtok>0 order by mint,slot,tx_index,ordinal"):
    tr[m].append((slot*0.4,bt,(vs/1e9)/(vt/1e6)))
n_tr=len(tr); band=0; out={15:collections.Counter(),30:collections.Counter(),60:collections.Counter()}; nofx=0
for m,(created,st) in S.items():
    rows=tr.get(m)
    if not rows: continue
    fxv=solusd(created)
    if fxv is None: nofx+=1; continue
    t0=next(((t,p) for t,bt,p in rows if p*1e9*fxv>=30000),None)
    if t0 is None: continue
    band+=1
    t0s=t0[0]; last=rows[-1][0]; end_cap=(rows[0][0]+3600-(rows[0][1]-created) if False else None)
    for N in (15,30,60):
        e=next(((t,p) for t,bt,p in rows if t>=t0s+N),None)
        if e is None or e[0]>t0s+N+60: out[N]['no_entry']+=1; continue
        win=[(t,p) for t,p in [(t,p) for t,bt,p in rows] if e[0]<t<=e[0]+600]
        # censor: window must lie inside fetched span (truncated or 60-min cap)
        if st=='truncated' and (not win or e[0]+600>last): out[N]['censored']+=1; continue
        span_end=created+3600
        bt_e=next(bt for t,bt,p in rows if t>=e[0])
        if bt_e+600>span_end: out[N]['censored']+=1; continue
        mx=max([math.log(p/e[1]) for t,p in win] or [0])
        first=None
        for t,p in win:
            r=p/e[1]-1
            if r>=0.2: first='up'; break
            if r<=-0.2: first='down'; break
        out[N]['labelled']+=1; out[N]['Y2']+=int(mx>=math.log(2)); out[N]['Y50']+=int(mx>=math.log(1.5)); out[N]['D20']+=int(first=='down')
print('sample 2000; launches with curve trades',n_tr,'no fx',nofx,'band entries (>=30k within fetched 60 min)',band)
for N,c in out.items(): print('N=%ds'%N,dict(c))
