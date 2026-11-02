"""Flask test clients for the memecoin web that send the CSRF token, as the cockpit does.

Since 2026-09-27 every mutating /api/memecoin (and x-analysis) route refuses a request without
X-CSRF-Token (sniper/routes/memecoin_csrf.guard_mutations). Tests of what a route DOES use this
client; tests of the guard itself (test_memecoin_csrf_all_mutations) use a plain one.
"""
from sniper.routes.memecoin_csrf import csrf_token


def csrf_client(app):
    client = app.test_client()
    with app.test_request_context():
        client.environ_base["HTTP_X_CSRF_TOKEN"] = csrf_token()
    return client
