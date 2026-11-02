#!/usr/bin/env python3
"""Read-only: how wrong is a carried virtual quote? At every proven buy, value the pool on its own reserves with the previous proven buy's quote, against its own: carried/proven spot, by carry age and by sells since the previous proven buy. Dump stratum, fixed before looking: at least 10 intervening sells."""
import argparse,sqlite3,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sniper.services import memecoin_desk_ps_tape as T


def ratios(rows):
    """rows: (pool,received_at,is_buy,base,real,payload) ordered by pool and chain order; yields (age_s,sells_between,ratio)."""
    last={}
    for pool,at,is_buy,base,real,payload in rows:
        state=last.get(pool)
        if not is_buy:
            if state:state[2]+=1
            continue
        v=T._proof(payload)
        if v is None or not base or not real:continue
        if state:yield at-state[0],state[2],(int(real)+state[1])/(int(real)+v)
        last[pool]=[at,v,0]


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--tape',required=True);parser.add_argument('--window-s',type=float,default=1200.)
    args=parser.parse_args()
    conn=sqlite3.connect('file:%s?mode=ro'%args.tape,uri=True,timeout=2)
    got=list(ratios(conn.execute('SELECT pool,received_at,is_buy,post_base_reserve_atomic,post_quote_reserve_atomic,payload_json FROM swaps_ps WHERE received_at>? ORDER BY pool,slot,tx_index,ordinal',(time.time()-args.window_s,))))
    def show(name,g):
        g=sorted(abs(r-1) for r in g)
        print(name,'n=%d'%len(g),'insufficient' if len(g)<100 else 'p50=%.4f p90=%.4f p99=%.4f max=%.4f'%(g[len(g)//2],g[int(len(g)*.9)],g[int(len(g)*.99)],g[-1]))
    show('all',[r for _,_,r in got])
    show('dump sells>=10',[r for _,s,r in got if s>=10])
    show('sells>=30',[r for _,s,r in got if s>=30])
    for lo,hi in ((0,30),(30,120),(120,300)):show('age %s-%s'%(lo,hi),[r for a,_,r in got if lo<=a<hi])


if __name__=='__main__':main()
