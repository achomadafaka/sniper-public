"""SOL buffer visibility: a stalled buffer pages once (and once on recovery), never re-pages after a restart,
and its state reaches the heartbeat file."""
import json

from sniper.services.memecoin_sol_buffer_watch import BufferWatch
from sniper.services import memecoin_runner as R


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def watch(tmp_path, clock, pages):
    return BufferWatch(tmp_path / "page.json", below_usd=25, stall_s=600, clock=clock,
                       alert=lambda kind, text: pages.append((kind, text)))


STALL = {"state": "insufficient_free_usdc", "native_usd": 3.2, "free_usdc": 211.4}


def test_a_stalled_buffer_pages_once_after_the_stall_window_and_once_on_recovery(tmp_path):
    clock, pages = Clock(), []
    w = watch(tmp_path, clock, pages)
    for dt in (0, 300, 599):
        clock.t = 1000 + dt
        assert w.observe(STALL) is None                                    # short, but not for 600 s yet
    clock.t = 1600
    w.observe(STALL)
    clock.t = 1900
    w.observe(STALL)
    assert len(pages) == 1 and pages[0][0] == "SOL-BUFFER"
    assert "insufficient_free_usdc" in pages[0][1] and "211.4" in pages[0][1]
    clock.t = 2000
    w.observe({"state": "confirmed", "native_usd": 3.2})                   # a refill landed
    assert len(pages) == 2 and "recovered" in pages[1][1]
    assert w.status()["last_refill_at"] == 2000 and w.status()["paged"] is False


def test_a_restart_never_re_pages_the_same_outage(tmp_path):
    clock, pages = Clock(), []
    w = watch(tmp_path, clock, pages)
    w.observe(STALL)
    clock.t = 1700
    w.observe(STALL)
    assert len(pages) == 1
    again = watch(tmp_path, clock, pages)                                  # runner restart
    for dt in (0, 700, 1400):
        clock.t = 1700 + dt
        again.observe(STALL)
    assert len(pages) == 1


def test_unknown_native_is_not_short_and_a_full_buffer_never_pages(tmp_path):
    clock, pages = Clock(), []
    w = watch(tmp_path, clock, pages)
    for dt in (0, 700, 1400):
        clock.t = 1000 + dt
        w.observe({"state": "balance_or_price_unavailable"})
        w.observe({"state": "buffer_cap", "native_usd": 199.0})
    assert pages == [] and w.status()["short_since"] is None


def test_the_buffer_state_reaches_the_heartbeat_file(tmp_path):
    clock, pages = Clock(), []
    w = watch(tmp_path, clock, pages)
    w.observe(STALL)
    path = tmp_path / "hb.json"
    R.write_heartbeat({}, auth_ok=True, path=str(path), sol_buffer=w.status())
    beat = json.loads(path.read_text())["sol_buffer"]
    assert beat["state"] == "insufficient_free_usdc" and beat["free_usdc"] == 211.4 and beat["native_usd"] == 3.2


def test_the_quiet_refusal_reports_free_usdc_and_native_value_for_the_watch(tmp_path):
    # 2026-10-19: the buffer stalled at the USDC floor with no trace; the tally now carries the reads
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_gas_topup import buffer_topup_once
    conn = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(conn)
    t = buffer_topup_once(conn, sol_balance=0.1, usdc_balance=213.3, sol_usd=100.0, keypair=None, pubkey="owner",
                          rpc_url="rpc", now_iso="2026-10-19T08:00:00Z", cap_usd=200.0, topup_usd=175.0,
                          daily_cap_usd=0.0, min_usdc_remaining_usd=200.0)
    assert t["state"] == "insufficient_free_usdc"
    assert t["free_usdc"] == 213.3 and t["native_usd"] is not None and t["native_usd"] < 10


def test_short_follows_the_current_clip_so_native_between_25_and_a_clip_pages(tmp_path):
    # Gamma on fa61ec974: a fixed $25 missed native SOL between $25 and a ~$63 clip, which already
    # makes prefer_sol fund in USDC
    clock, pages, clip = Clock(), [], {"usd": 63.0}
    w = BufferWatch(tmp_path / "page.json", below_usd=0, stall_s=600, clip_fn=lambda: clip["usd"], clock=clock,
                    alert=lambda kind, text: pages.append(text))
    mid = {"state": "insufficient_free_usdc", "native_usd": 40.0, "free_usdc": 210.0}
    w.observe(mid)
    clock.t = 1700
    w.observe(mid)
    assert len(pages) == 1 and "$63" in pages[0] and w.status()["threshold_usd"] == 63.0
    clip["usd"] = None                                                     # clip unknown: no decision
    clock.t = 1800
    w.observe(mid)
    assert len(pages) == 1 and w.status()["paged"] is True
    fixed = BufferWatch(tmp_path / "p2.json", below_usd=30, stall_s=600, clip_fn=lambda: 63.0, clock=clock,
                        alert=lambda kind, text: pages.append(text))
    assert fixed.threshold() == 30                                         # the knob overrides
