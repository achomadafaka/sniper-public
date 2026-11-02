"""Office observations and authenticated operator mail; no execution authority."""
from flask import Blueprint, jsonify, render_template, request, current_app
from pathlib import Path
import json
import sqlite3
from sniper.office.seat_feed import read_feed
from sniper.office.seat_events import OperatorDmRefused
from sniper.office.report import read_state
from sniper.office.operator_view import read_operator_report
from sniper.office.position_charts import OfficeCharts


def _seat_delivery(conn,office_db,actor_id):
    """What the operator should read after sending: the host state file is the seat's own record of why it isn't waking. Unknown stays unknown."""
    try:
        role=conn.execute('SELECT role FROM office_v2_roster WHERE actor_id=?',(actor_id,)).fetchone()[0]
        parked=json.loads((Path(office_db).parent/'v2'/('explore-%s.json'%role)).read_text()).get('parked_reason')
    except (OSError,ValueError,TypeError,sqlite3.Error,AttributeError):
        return dict(delivery='delivered, seat state unknown',parked_reason=None)
    return dict(delivery='delivered, seat parked: %s'%parked if parked else 'delivered, seat waking',parked_reason=parked)


def create_api_office_blueprint(ctx):
    bp=Blueprint('api_office',__name__,template_folder='../web/templates')
    position_charts=OfficeCharts(ctx['state_root'])

    @bp.get('/office')
    def office_page():
        versions={}
        for key,filename in (('office_v2_js','office-v2.js'),('office_communications_js','office-communications.js'),
                             ('office_network_js','office-network.js'),('wallet_universe_js','wallet_universe.js')):
            try:versions[key]=int((Path(current_app.static_folder)/filename).stat().st_mtime)
            except (OSError,TypeError):versions[key]=1
        return render_template('office.html',**versions)

    @bp.get('/api/office/network')
    def office_network():
        """Read-only wallet network around a seed: evidence with source, time and proof. No wallet action exists here."""
        from sniper.office.wallet_network import WalletNetwork
        types=[t for t in request.args.get('types','').split(',') if t] or None
        try:hops,limit=int(request.args.get('hops',1)),int(request.args.get('limit',200))
        except ValueError:return jsonify(dict(state='refused',reason='bad_arguments')),400
        out=WalletNetwork(Path(ctx['state_root'])/'wallet_network.db').read(seed=request.args.get('seed'),hops=hops,limit=limit,types=types)
        return jsonify(out),400 if out['state']=='refused' else 200

    @bp.get('/api/office/state')
    def office_state():
        try:
            now=ctx['clock']()
        except (OSError,ValueError,TypeError):
            now=None
        result=read_state(ctx['state_root'],now=now,stale_after_s=ctx.get('stale_after_s'),sol_usd=ctx.get('sol_usd'),sol_at=ctx.get('sol_at'))
        response=jsonify(result)
        response.headers['Cache-Control']='no-store'
        return response

    @bp.get('/api/office/feed')
    def office_feed():
        try:
            now=ctx['clock']()
        except (OSError,ValueError,TypeError):
            now=None
        try:
            result=read_feed(ctx['state_root'],now=now,after=request.args.get('after'),
                             mail_after=request.args.get('mail_after'),actor=request.args.get('actor'),
                             limit=int(request.args.get('limit','64')))
        except (ValueError,OverflowError):
            return jsonify(error='feed_query_invalid'),400
        response=jsonify(result);response.headers['Cache-Control']='no-store';return response

    @bp.get('/api/office/explore')
    def office_explore():
        authenticated=ctx.get('operator_authenticated')
        if authenticated is None or authenticated() is not True:
            return jsonify(error='unauthorized'),401
        try:
            result=read_operator_report(ctx['state_root'],now=ctx['clock']())
        except (OSError,ValueError,TypeError):
            result=dict(state='unknown',reason='report_clock_unknown')
        from sniper.office.order_price_fields import enrich
        result=enrich(ctx['state_root'],result)
        response=jsonify(result);response.headers['Cache-Control']='no-store';return response

    @bp.get('/api/office/positions/charts')
    def office_position_charts():
        authenticated=ctx.get('operator_authenticated')
        if authenticated is None or authenticated() is not True:return jsonify(error='unauthorized'),401
        try:result=position_charts.read(now=ctx['clock']())
        except (OSError,ValueError,TypeError):result=dict(state='unknown',epoch_id='office-v2-explore-1',reason='chart_clock_unknown')
        from sniper.office.order_price_fields import enrich
        result=enrich(ctx['state_root'],result)
        response=jsonify(result);response.headers['Cache-Control']='no-store';return response

    @bp.post('/api/office/dm')
    def operator_dm():
        authenticated=ctx.get('operator_authenticated')
        if authenticated is None or authenticated() is not True:
            return jsonify(error='unauthorized'),401
        origin=request.headers.get('Origin')
        # Compare with both public origins for the trusted TLS terminator; reject foreign browser sites.
        origins={request.host_url.rstrip('/')}
        if request.headers.get('X-Forwarded-Proto')=='https':
            origins.add('https://'+request.host)
        if origin not in origins or request.headers.get('Sec-Fetch-Site') not in (None,'same-origin'):
            return jsonify(error='origin_refused'),403
        if request.mimetype!='application/json':return jsonify(error='json_required'),415
        if request.content_length is None or request.content_length>16384:
            return jsonify(error='message_too_large'),413
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or set(body)!={'to_actor_id','client_id','text'}:
            return jsonify(error='dm_body_invalid'),400
        try:
            valid=(all(isinstance(body[k],str) and body[k].strip() for k in body)
                   and len(body['text'].encode('utf-8'))<=4096
                   and all(len(body[k].encode('utf-8'))<=256 for k in ('to_actor_id','client_id')))
        except UnicodeError:
            valid=False
        if not valid:return jsonify(error='dm_body_invalid'),400
        try:
            now=ctx['clock']()
            from sniper.office.report import _number
            if not _number(now) or now<0:raise ValueError('clock_unknown')
            send=ctx.get('operator_dm')
            if send is not None:
                value=send(**body,now=now)
            else:
                from sniper.office.seat_events import operator_dm as host_dm
                path=Path(ctx['state_root'])/'office.db'
                conn=sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,timeout=.1)
                conn.row_factory=sqlite3.Row
                try:
                    if body['to_actor_id']=='all':
                        # One message per seat, each idempotent, so a retry after a partial failure completes the rest.
                        seats=[r[0] for r in conn.execute('SELECT actor_id FROM office_v2_roster WHERE sealed=0 ORDER BY actor_id')]
                        if not seats:raise OperatorDmRefused('unknown_actor')
                        value=dict(state='sent',seats={a:dict(host_dm(conn,to_actor_id=a,client_id=body['client_id']+'_'+a,text=body['text'],now=now),**_seat_delivery(conn,path,a)) for a in seats})
                    else:
                        value=dict(host_dm(conn,**body,now=now),**_seat_delivery(conn,path,body['to_actor_id']))
                finally:conn.close()
        except OperatorDmRefused as exc:
            status=409 if exc.code=='client_id_conflict' else 503 if exc.code=='store_unavailable' else 400
            return jsonify(error=exc.code),status
        except (ImportError,OSError,sqlite3.Error,ValueError):
            return jsonify(error='dm_unavailable'),503
        response=jsonify(value);response.headers['Cache-Control']='no-store';return response

    return bp
