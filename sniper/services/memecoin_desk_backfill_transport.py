"""Research Helius transport for the G1/G2 history fetchers: key pin, JSON-RPC over urllib, calibrated cost table. Research key only (a56bd061); never a funded provider."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from sniper.services import memecoin_desk_backfill_history as H

ENV_PATH = "/home/bigpapi/Desktop/Sniper/.env"
KEY_ENV = "MEMECOIN_RESEARCH_HELIUS_RPC_URL"
KEY_PREFIX = "a56bd061"


class Refused(Exception):
    pass


def helius_url(env_path=ENV_PATH, environ=None):
    environ = os.environ if environ is None else environ
    url = environ.get(KEY_ENV)
    if not url:
        try:
            for line in Path(env_path).read_text().splitlines():
                if line.startswith(KEY_ENV + "="):
                    url = line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            url = None
    if not url:
        raise Refused("no_key")
    key = (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("api-key") or [""])[0]
    if not key.startswith(KEY_PREFIX):
        raise Refused("key_pin_mismatch")                      # fail closed: nothing is fetched with any other key
    return url


def make_transport(url, timeout=30):
    def transport(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": [params["address"], params["config"]]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=params.get("timeout", timeout)) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
    return transport


def load_costs(bdir):
    """The calibrated cost table (costs.json written by attest-costs), or None: nothing is fetched until it exists."""
    try:
        c = json.loads((Path(bdir) / "costs.json").read_text())
        if c.get("calibrated") is True and c.get(H.METHOD_FULL) == 10:
            return {H.METHOD_FULL: int(c[H.METHOD_FULL])}
    except (OSError, ValueError):
        pass
    return None
