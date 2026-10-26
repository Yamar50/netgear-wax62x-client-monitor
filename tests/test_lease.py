import asyncio
from copy import deepcopy
import pytest
from custom_components.netgear_wax.lease import LeaseManager, BusyError, LeaseError


@pytest.fixture
def memory():
    state = {"now": 100, "disk": {}, "writes": 0}

    async def save(data):
        await asyncio.sleep(0)
        state["disk"] = deepcopy(data)
        state["writes"] += 1

    state["save"] = save
    state["manager"] = LeaseManager(save, now=lambda: state["now"])
    return state


async def test_simultaneous_ha_external_exactly_one_wins(memory):
    manager = memory["manager"]
    results = await asyncio.gather(
        manager.acquire("HA", 3600, "collector"), manager.acquire("consumer"), return_exceptions=True
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, BusyError) for r in results) == 1
    assert memory["writes"] == 1


async def test_restart_keeps_external_reservation(memory):
    ticket = await memory["manager"].acquire("consumer")
    restarted = LeaseManager(memory["save"], memory["disk"], now=lambda: memory["now"])
    with pytest.raises(BusyError):
        await restarted.acquire("HA", 3600, "collector")
    await restarted.release("consumer", ticket["token"])
    assert not restarted.status()["busy"]
    assert memory["disk"] == {}


async def test_stale_token_cannot_release_new_owner(memory):
    manager = memory["manager"]
    old = await manager.acquire("consumer")
    memory["now"] += 601
    new = await manager.acquire("HA", 3600, "collector")
    with pytest.raises(LeaseError):
        await manager.release("consumer", old["token"])
    assert manager.status()["busy"]
    await manager.release("HA", new["token"])


async def test_token_not_written_or_exported_and_wrong_user_rejected(memory):
    manager = memory["manager"]
    ticket = await manager.acquire("one")
    assert ticket["token"] not in str(memory["disk"])
    assert ticket["token"] not in str(manager.status())
    with pytest.raises(LeaseError):
        await manager.release("two", ticket["token"])
    with pytest.raises(LeaseError):
        await manager.release("one", "wrong-token")


async def test_renew_and_expiry(memory):
    manager = memory["manager"]
    ticket = await manager.acquire("consumer", 120)
    memory["now"] += 90
    renewal = await manager.renew("consumer", ticket["token"], 120)
    assert renewal["expires_at"] == 310
    memory["now"] = 311
    assert not manager.status()["busy"]
    with pytest.raises(LeaseError):
        await manager.renew("consumer", ticket["token"])


async def test_failed_persistence_never_grants_reservation():
    async def fail(data):
        raise OSError("disk unavailable")

    manager = LeaseManager(fail)
    with pytest.raises(OSError):
        await manager.acquire("consumer")
    assert not manager.status()["busy"]


@pytest.mark.parametrize("ttl", [0, 119, 901, -1])
async def test_invalid_duration(memory, ttl):
    with pytest.raises(LeaseError):
        await memory["manager"].acquire("consumer", ttl)
