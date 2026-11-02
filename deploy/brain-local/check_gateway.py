"""Live check for the local brain gateway: a long `id=in.(...)` read must not 502.

PostgREST echoes the request path and query in its Content-Location response header. With
nginx's default proxy buffers, the store's 100-id brain_links chunk made that header too big, nginx answered
502 "upstream sent too big header", and brain_nightly died at its candidate sweep (2026-10-14).

    python3 deploy/brain-local/check_gateway.py    # exit 0 ok, 1 broken
"""
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def main():
    from sniper.util.envfiles import load_env_files
    load_env_files(ROOT)
    from bot.brain_graph.store import RestStore, _pg_in
    store = RestStore(timeout=10)
    # The store reads brain_links in chunks of 100 ids (store.py, all_links); use the same size.
    ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, str(i))) for i in range(100)]
    try:
        store._all('brain_links', filters=(('from_id', 'in', _pg_in(ids)),))
    except Exception as exc:
        print('FAIL: 100-id brain_links read raised %s' % type(exc).__name__)
        return 1
    print('ok: 100-id brain_links read answered')
    return 0


if __name__ == '__main__':
    sys.exit(main())
