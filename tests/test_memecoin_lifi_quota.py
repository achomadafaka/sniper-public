import threading
import time

try:
    from sniper.execution import lifi_route as route
except ImportError:
    route = None


def test_canary_quota_boundary_subtracts_in_flight():
    assert not route.QuotaGuard(remaining=6).admit_canary(in_flight=0).allowed
    assert route.QuotaGuard(remaining=7).admit_canary(in_flight=0).allowed
    assert not route.QuotaGuard(remaining=7).admit_canary(in_flight=1).allowed
    assert route.QuotaGuard(remaining=8).admit_canary(in_flight=1).allowed


def test_one_position_admission_boundary_subtracts_in_flight():
    assert not route.QuotaGuard(remaining=7_209).admit_entry(0, 0).allowed
    assert route.QuotaGuard(remaining=7_210).admit_entry(0, 0).allowed
    assert not route.QuotaGuard(remaining=7_210).admit_entry(0, 1).allowed
    assert route.QuotaGuard(remaining=7_211).admit_entry(0, 1).allowed


def test_two_position_admission_boundary_subtracts_in_flight():
    assert not route.QuotaGuard(remaining=14_415).admit_entry(1, 0).allowed
    assert route.QuotaGuard(remaining=14_416).admit_entry(1, 0).allowed
    assert not route.QuotaGuard(remaining=14_416).admit_entry(1, 1).allowed
    assert route.QuotaGuard(remaining=14_417).admit_entry(1, 1).allowed


def test_reserve_math_uses_fastlane_and_exit_actions():
    guard = route.QuotaGuard(remaining=20_000)

    assert guard.exit_reserve(1) == 7_206
    assert guard.exit_reserve(2) == 14_412
    assert guard.entry_request_cost == 4
    assert guard.canary_request_cost == 7


def test_exit_request_gets_priority_over_waiting_entry():
    guard = route.QuotaGuard(remaining=20_000, max_in_flight=1)
    order = []
    first_entered = threading.Event()
    release_first = threading.Event()

    def first():
        with guard.reserve_request("exit"):
            first_entered.set()
            release_first.wait(2)

    def request(role):
        with guard.reserve_request(role):
            order.append(role)

    first_thread = threading.Thread(target=first)
    entry_thread = threading.Thread(target=request, args=("entry",))
    exit_thread = threading.Thread(target=request, args=("exit",))
    first_thread.start()
    assert first_entered.wait(1)
    entry_thread.start()
    time.sleep(0.02)
    exit_thread.start()
    time.sleep(0.02)
    release_first.set()
    first_thread.join(2)
    entry_thread.join(2)
    exit_thread.join(2)

    assert order == ["exit", "entry"]
