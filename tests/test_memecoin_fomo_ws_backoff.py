"""A dropped socket and a REFUSED socket are different answers, so they get different backoffs.

Measured 2026-09-06: the fomo activity WebSocket was closed with `1008 policy violation` 13 times
in ten minutes while the REST lane stayed healthy and the JWT stayed valid. That is the same
app-layer NO that produced the account ban on 2026-08-03. The worker reset its ladder to 1 s on
every payload, so a socket that connected, delivered one message and was then kicked retried a
second later and the 30 s cap never held.
"""
import ast
import pathlib

import pytest
import websockets.exceptions as WE
import websockets.frames as WF

from sniper.feeds import fomo_feed as FF

REPO = pathlib.Path(__file__).resolve().parents[1]


def _closed(code, reason="x"):
    return WE.ConnectionClosedError(WF.Close(code, reason), None)


def test_a_policy_close_is_told_apart_from_a_dropped_socket():
    assert FF.is_ws_policy_refusal(_closed(1008, "forbidden")) is True
    assert FF.is_ws_policy_refusal(_closed(1006, "abnormal")) is False
    assert FF.is_ws_policy_refusal(ConnectionError("ended")) is False


def test_the_close_code_is_read_from_the_exception_not_from_its_text():
    """A string match on "policy violation" stops working the day the reason text changes, and it
    fails OPEN — the refusal would be paced like a dropped socket again."""
    assert FF.is_ws_policy_refusal(_closed(1008, "")) is True
    assert FF.is_ws_policy_refusal(_closed(1006, "received 1008 policy violation")) is False


def test_a_refusal_never_retries_faster_than_the_refusal_floor():
    """THE BUG. A long healthy session followed by a policy kick must NOT reset to 1 s."""
    assert FF.fomo_ws_backoff(1.0, 3600.0, _closed(1008)) >= FF.FOMO_WS_REFUSAL_FLOOR_S


def test_a_refusal_ladder_climbs_to_its_own_ceiling():
    r, seen = 1.0, []
    for _ in range(12):
        r = FF.fomo_ws_backoff(r, 300.0, _closed(1008))
        seen.append(r)
    assert seen[0] == FF.FOMO_WS_REFUSAL_FLOOR_S
    assert seen == sorted(seen), seen
    assert max(seen) == FF.FOMO_WS_REFUSAL_CAP_S
    assert FF.FOMO_WS_REFUSAL_CAP_S > FF.FOMO_WS_TRANSPORT_CAP_S


def test_a_dropped_socket_still_retries_quickly_and_caps_where_it_did():
    r, seen = 1.0, []
    for _ in range(10):
        r = FF.fomo_ws_backoff(r, 2.0, _closed(1006))
        seen.append(r)
    assert seen[0] <= 2.0, "a transport blip must not be paced like a refusal"
    assert max(seen) == FF.FOMO_WS_TRANSPORT_CAP_S


def test_only_a_long_session_earns_the_reset():
    healthy = FF.FOMO_WS_HEALTHY_SESSION_S
    assert FF.fomo_ws_backoff(16.0, healthy, _closed(1006)) == 1.0
    assert FF.fomo_ws_backoff(16.0, healthy - 0.1, _closed(1006)) > 1.0


@pytest.mark.parametrize("bad", [None, "", "nope", float("nan")])
def test_a_malformed_previous_value_never_raises_inside_the_worker(bad):
    """This runs in the reconnect path of a funded lane; raising here kills the fast lane."""
    out = FF.fomo_ws_backoff(bad, 1.0, _closed(1006))
    assert out == out and out > 0, out          # not NaN, and a real wait


def test_the_worker_no_longer_resets_the_ladder_on_a_payload():
    """The reset must be earned by session length inside fomo_ws_backoff, not granted per message."""
    src = (REPO / "scripts" / "memecoin_runner_loop.py").read_text()
    tree = ast.parse(src)
    worker = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_fomo_activity_ws_worker")
    assigns = [n for n in ast.walk(worker)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "retry_s" for t in n.targets)]
    # exactly two: the initial 1.0, and the one fed by fomo_ws_backoff
    consts = [a for a in assigns if isinstance(a.value, ast.Constant)]
    assert len(consts) == 1, "retry_s is still assigned a literal inside the loop: " + str(
        [ast.unparse(a) for a in consts])
    assert any("fomo_ws_backoff" in ast.unparse(a.value) for a in assigns)


def test_the_reconnect_log_names_the_wait_it_is_about_to_take():
    """It printed the PREVIOUS wait before, so the log understated every escalation."""
    src = (REPO / "scripts" / "memecoin_runner_loop.py").read_text()
    i_calc = src.index("retry_s = fomo_ws_backoff")
    i_log = src.index("Fomo activity WebSocket reconnect in")
    assert i_calc < i_log, "the backoff is logged before it is computed"
