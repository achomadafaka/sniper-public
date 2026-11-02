#!/usr/bin/env python3
"""PumpSwap jump anatomy, exploratory (Gamma 2026-10-31). Read-only on closed plain archive days. sign() is the frozen shadow trigger.
    nice -n 19 python3 scripts/research/ps_jump_anatomy.py OUT.pkl WALLETS.json keep-DAY.db ..."""
import sqlite3,sys,bisect,statistics as st,random,json,pickle
from collections import Counter as C,defaultdict as DD
W=set(json.load(open(sys.argv[2]))) if len(sys.argv)>2 else set()
MIN_Q=40e9
EV=[];SG=[];ST=C()
LAT=(1,2,5,10,20,30,60,120,300)
def analyse(day,pool,rows):
    rows.sort(key=lambda r:(r[0],r[1]));v=None;p=[]
    for r in rows:
        if r[6] is not None:v=int(r[6])
        try:q,b=int(r[4]),int(r[5])
        except:continue
        if b>0:p.append((r[0],(q+(v or 0))/b,q,r[2],r[3],int(r[7] or 0)))
    if len(p)<20: ST['pool_lt20']+=1;return
    ST['pools']+=1;ST['pool_seconds']+=p[-1][0]-p[0][0]
    t=[x[0] for x in p];P=[x[1] for x in p]
    used=-1
    # jump: earliest print, liquid, with forward max>=2x within 300 s and no -30% before reaching it
    i=0;n=len(p)
    while i<n:
        if p[i][2]<MIN_Q or t[i]-t[0]<60: i+=1;continue
        j=i+1;mx=P[i];hit=None;bad=False
        while j<n and t[j]-t[i]<=300:
            if P[j]<=P[i]*.7: bad=True;break
            if P[j]>=2*P[i]: hit=j;break
            j+=1
        if hit is None: i+=1;continue
        # back to lowest print in prior 60 s without breaking: take trough start from i forward up to hit
        k=min(range(i,hit+1),key=lambda a:P[a])
        # peak: run forward until -30% from running peak or 900 s
        pk=hit;m=hit
        while m<n and t[m]-t[k]<=900:
            if P[m]>P[pk]:pk=m
            if P[m]<=P[pk]*.7:break
            m+=1
        ev=dict(day=day,pool=pool,start=t[k],p0=P[k],peak=P[pk],rise=P[pk]/P[k]-1,t_double=t[hit]-t[k],t_peak=t[pk]-t[k],end_cov=t[-1]-t[k],open_after=t[min(m,n-1)]-t[pk])
        half=P[k]+.5*(P[pk]-P[k])
        ev['t_half']=next(t[a]-t[k] for a in range(k,pk+1) if P[a]>=half)
        ev['cap']={L:(lambda a:(P[pk]/P[a]-1) if a<n else None)(bisect.bisect_left(t,t[k]+L)) for L in LAT}
        # features in prior 60 s
        a=bisect.bisect_left(t,t[k]-60);pre=p[a:k+1]
        ev['pre']=feat(pre,p[k][2],p,a);
        # first sign
        ev['sign']=None
        for b in range(k+1,hit+1):
            if sign(p,t,P,b): ev['sign']=t[b]-t[k];ev['sign_idx']=b;break
        if ev['sign'] is not None:
            b=ev['sign_idx'];ev['after_sign']=P[pk]/P[b]-1;ev['pre_sign']=feat(p[bisect.bisect_left(t,t[b]-30):b+1],p[b][2],p,0)
        EV.append(ev);used=m;i=max(m,hit+1)
    # sign fires, every setup, once per 300 s
    nxt=-1
    for b in range(1,n):
        if t[b]<nxt or t[b]-t[0]<60 or p[b][2]<MIN_Q: continue
        if sign(p,t,P,b):
            nxt=t[b]+300
            e=bisect.bisect_right(t,t[b]+300);fw=P[b:e]
            cov=t[-1]-t[b]>=300
            mxr=max(fw)/P[b]-1;mnr=min(fw)/P[b]-1
            # rails from sign at L=10 s, tp100 sl30 trail20
            res={}
            for L in (2,10,30,60):
                s=bisect.bisect_left(t,t[b]+L)
                if s>=n or t[s]>t[b]+L+30: res[L]=None;continue
                p0=P[s];pkk=p0;o=None
                for x in range(s+1,n):
                    r=P[x]/p0-1;pkk=max(pkk,P[x])
                    if r<=-.3 or r>=1 or P[x]<=pkk*.8: o=r;break
                    if t[x]-t[s]>3600: o=r;break
                res[L]=o
            SG.append(dict(day=day,pool=pool,t=t[b],cov=cov,mx=mxr,mn=mnr,jump=mxr>=1,rail=res,f=feat(p[bisect.bisect_left(t,t[b]-30):b+1],p[b][2],p,0)))
def sign(p,t,P,b):
    a=bisect.bisect_left(t,t[b]-30)
    if b-a<5 or P[b]<P[a]*1.10: return False
    w=p[a:b+1];buy=[x for x in w if x[3]];sell=[x for x in w if not x[3]]
    return len({x[4] for x in buy})>=5 and sum(x[5] for x in buy)>sum(x[5] for x in sell)
def feat(w,q,p,a0):
    buy=[x for x in w if x[3]];sell=[x for x in w if not x[3]];bu=sum(x[5] for x in buy)/1e9;se=sum(x[5] for x in sell)/1e9
    seen={x[4] for x in p[:max(0,len(p)-1)] if 0} 
    lb=max((x[5] for x in buy),default=0)/1e9
    return dict(n=len(w),buyers=len({x[4] for x in buy}),sellers=len({x[4] for x in sell}),buy=bu,sell=se,largest=lb,lb_share=lb/(q/1e9) if q else None,q=q/1e9,follow=len({x[4] for x in w}&W))
for f in sys.argv[3:]:
    c=sqlite3.connect(f'file:{f}?mode=ro',uri=True,timeout=5);cur=None;rows=[]
    day=f[-13:-3]
    pools=DD(list)
    for r in c.execute("select pool,received_at,slot,is_buy,actor,post_quote_reserve_atomic,post_base_reserve_atomic,json_extract(payload_json,'$.virtual_quote_atomic'),quote_atomic from swaps_ps"):
        pools[r[0]].append((r[1],r[2],r[3],r[4],r[5],r[6],r[7],r[8]))
    c.close();print(day,len(pools),sum(map(len,pools.values())),flush=True)
    for pool,rs in pools.items():
        analyse(day,pool,rs)
    del pools
pickle.dump(dict(EV=EV,SG=SG,ST=ST),open(sys.argv[1],'wb'));print('done',dict(ST),len(EV),len(SG),flush=True)
