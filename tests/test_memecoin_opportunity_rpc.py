import asyncio
from threading import Lock
import time
from urllib.error import HTTPError

from sniper.services.memecoin_opportunity_rpc import OpportunityRpcGovernor, RpcPriority


def test_rpc_governor_limits_concurrency_and_prioritizes_confirmation():
    asyncio.run(_priority_case())


async def _priority_case():
    governor = OpportunityRpcGovernor(concurrency=1, initial_rps=1000, min_rps=1, max_rps=1000)
    order = []
    gate = Lock()

    def work(name, delay=0):
        with gate:
            order.append(name)
        time.sleep(delay)
        return name

    first = asyncio.create_task(governor.call(RpcPriority.CURRENT, work, "current", 0.03))
    await asyncio.sleep(0.005)
    recovery = asyncio.create_task(governor.call(RpcPriority.RECOVERY, work, "recovery"))
    confirmation = asyncio.create_task(governor.call(RpcPriority.CONFIRMATION, work, "confirmation"))
    assert await asyncio.gather(first, recovery, confirmation) == ["current", "recovery", "confirmation"]
    assert order == ["current", "confirmation", "recovery"]
    assert governor.metrics()["max_observed_concurrency"] == 1
    await governor.close()


def test_rpc_governor_halves_rate_on_429_and_retries():
    asyncio.run(_throttle_case())


async def _throttle_case():
    governor = OpportunityRpcGovernor(concurrency=2, initial_rps=8, min_rps=1, max_rps=8)
    attempts = 0

    def throttled():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError("https://rpc.invalid", 429, "limited", {"Retry-After": "0"}, None)
        return "ok"

    assert await governor.call(RpcPriority.RECOVERY, throttled) == "ok"
    metrics = governor.metrics()
    assert metrics["rate_limit_events"] == 1
    assert metrics["current_rps"] == 4.0
    assert attempts == 2
    await governor.close()
