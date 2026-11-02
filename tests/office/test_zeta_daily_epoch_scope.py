import json
from office.test_persisted_reporting_ui import database,daily
from sniper.office import store
from sniper.office.report import read_state
from sniper.office.reporting_view import read_daily


def test_null_member_never_grants_unscoped_publication():
 c=database()
 c.execute('ALTER TABLE agent_daily ADD COLUMN epoch_id TEXT')
 daily(c)
 assert not read_daily(c,now=200.,public_scope=False,public_epochs={None,'d'})['items']
 assert read_daily(c,now=200.,public_scope=True,public_epochs={None,'d'})['items']
 c.execute("UPDATE agent_daily SET epoch_id='d'")
 assert read_daily(c,now=200.,public_scope=False,public_epochs={None,'d'})['items']

def test_null_epoch_identity_cannot_publish_unscoped_rows_in_mixed_universe(tmp_path):
 schema=database()
 c=store.open_store(tmp_path/'office.db')
 c.execute('INSERT INTO epochs VALUES(?,?)',('d',json.dumps({'epoch_id':None,'kind':'discovery','opened_at':1.})))
 c.execute('INSERT INTO epochs VALUES(?,?)',('private',json.dumps({'epoch_id':'private','kind':'evaluated','opened_at':1.})))
 sql=schema.execute("SELECT sql FROM sqlite_master WHERE name='agent_daily'").fetchone()[0]
 c.execute(sql);c.execute('ALTER TABLE agent_daily ADD COLUMN epoch_id TEXT')
 daily(c,actor_id='PRIVATE_UNSCOPED',calls=77)
 c.commit();c.close()
 result=read_state(tmp_path,now=200.,host_now=200.)
 assert not result['persisted_daily']['items']
