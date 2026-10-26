"""DFS reuses the collector login; connected clients are explicitly allowed."""
import asyncio
from copy import deepcopy

import pytest

from custom_components.netgear_wax.api import WaxError
from custom_components.netgear_wax.radio_recovery import validate_dfs_policies
from test_radio_recovery import Session, TABLE, CONFIG, collect
from test_radio_recovery import monitor as _monitor

monitor = _monitor

POLICY = {'allowed': [52, 56, 60, 64], 'rotation': [64, 60]}


class DfsSession(Session):
    def __init__(self):
        super().__init__()
        self.result.update(channel_5ghz=36, clients_5ghz=3, reported_clients_5ghz=3,
                           clients=[{'band': '5GHz'}] * 3)
        self.actual_channel = 36
        self.channel_supported = True
        self.apply_error = False
        self.apply_started = asyncio.Event()
        self.apply_release = None

    async def dashboard(self):
        system, counts = await super().dashboard()
        system['monitor']['currentChannel'] = {'wlan1': self.actual_channel}
        counts['wlan1'] = 3
        return system, counts

    async def socket(self, payload, referer='/dashboard', timeout=None):
        table = payload['system']['wlanSettings']['wlanSettingTable']
        if 'getRadioDetails' in table:
            self.calls.append('settings')
            value = deepcopy(self.table)
            value['wlan1']['channelList'] = {str(c): str(c) for c in POLICY['allowed']} if self.channel_supported else {}
            return {'status': 0, 'system': {'wlanSettings': {'wlanSettingTable': value}}}
        if 'wlan1' in table and table['wlan1'].get('radioStatus') in ('0', '1'):
            assert timeout == 180
            assert table['wlan0'] == TABLE['wlan0']
            assert {k:v for k,v in table['wlan1'].items() if k != 'channel'} == {
                k:v for k,v in TABLE['wlan1'].items() if k != 'channel'}
            self.calls.append('channel-' + table['wlan1']['channel'])
            self.apply_started.set()
            if self.apply_release:
                await self.apply_release.wait()
            if self.apply_error:
                raise WaxError('apply_unconfirmed')
            self.table = deepcopy(table)
            return {'status': 0}
        return await super().socket(payload, referer)


def enable(monitor):
    monitor.state['config'].update(dfs_enabled=True, dfs_policies={'a': deepcopy(POLICY)})


async def test_active_clients_do_not_block_dfs_and_same_login_is_used(monitor):
    enable(monitor)
    client = DfsSession()
    result = await collect(client, monitor)
    assert result['dfs_restoration'] == 'channel_configured'
    assert 'off' not in client.calls and 'on' not in client.calls
    assert client.calls == ['/AP_login', 'login', 'clients', 'settings', 'final-count', 'channel-64', 'settings', '/logout']
    assert monitor.state['dfs_indexes']['a'] == 1
    assert monitor.state['missing'] == {}
    assert result['channel_5ghz'] == 36  # Sample remains the original observation; CAC reception is not invented.
    assert (await monitor.store.async_load())['dfs_indexes']['a'] == 1


@pytest.mark.parametrize('current', [52, 56, 60, 64])
async def test_allowed_channel_skips_all_radio_requests(monitor, current):
    enable(monitor)
    client = DfsSession()
    client.result['channel_5ghz'] = current
    result = await collect(client, monitor)
    assert result['dfs_restoration'] == 'channel_allowed'
    assert client.calls == ['/AP_login', 'login', 'clients', '/logout']


@pytest.mark.parametrize('bad', [None, '', 'CAC', 0, False])
async def test_unknown_channel_never_becomes_dfs_request(monitor, bad):
    enable(monitor)
    client = DfsSession()
    client.result['channel_5ghz'] = bad
    await collect(client, monitor)
    assert 'settings' not in client.calls


async def test_newer_channel_already_allowed_stops_before_apply(monitor):
    enable(monitor)
    client = DfsSession()
    client.actual_channel = 64
    result = await collect(client, monitor)
    assert result['dfs_restoration'] == 'channel_allowed'
    assert not any(x.startswith('channel-') for x in client.calls)
    assert monitor.state.get('dfs_indexes', {}) == {}


async def test_failed_collection_does_not_do_dfs(monitor):
    enable(monitor)
    client = DfsSession()
    client.result = WaxError('incomplete_client_list')
    with pytest.raises(WaxError):
        await collect(client, monitor)
    assert client.calls == ['/AP_login', 'login', 'clients', '/logout']


async def test_unavailable_channel_no_apply_or_rotation_advance(monitor):
    enable(monitor)
    client = DfsSession()
    client.channel_supported = False
    result = await collect(client, monitor)
    assert result['dfs_restoration'] == 'target_channel_not_available'
    assert monitor.state.get('dfs_indexes', {}) == {}
    assert not any(x.startswith('channel-') for x in client.calls)


async def test_ambiguous_apply_advances_rotation_once_and_waits_next_poll(monitor):
    enable(monitor)
    client = DfsSession()
    client.apply_error = True
    result = await collect(client, monitor)
    assert result['dfs_restoration'] == 'apply_unconfirmed'
    assert [x for x in client.calls if x.startswith('channel-')] == ['channel-64']
    assert monitor.state['dfs_indexes']['a'] == 1
    later = DfsSession()
    await collect(later, monitor, cycle=2)
    assert 'channel-60' in later.calls
    assert monitor.state['dfs_indexes']['a'] == 0


async def test_standby_and_cancellation_wait_for_channel_apply_and_logout(monitor):
    enable(monitor)
    client = DfsSession()
    client.apply_release = asyncio.Event()
    poll = asyncio.create_task(collect(client, monitor))
    await client.apply_started.wait()
    stop = asyncio.create_task(monitor.set_mode('standby'))
    poll.cancel()
    await asyncio.sleep(0)
    assert not stop.done() and not client.logout_ok
    client.apply_release.set()
    with pytest.raises(asyncio.CancelledError):
        await poll
    await stop
    assert client.logout_ok


async def test_rotation_import_requires_standby_complete_valid_mapping(monitor):
    enable(monitor)
    with pytest.raises(ValueError):
        await monitor.import_dfs_indexes({'a': 1})
    await monitor.set_mode('standby')
    for invalid in ({}, {'a': -1}, {'a': 2}, {'a': True}, {'other': 1}):
        with pytest.raises(ValueError):
            await monitor.import_dfs_indexes(invalid)
    assert (await monitor.import_dfs_indexes({'a': 1}))['dfs_indexes'] == {'a': 1}


def test_policy_validation_restricts_known_aps_and_supported_5ghz_numbers():
    assert validate_dfs_policies({'a': POLICY}, [CONFIG]) == {'a': POLICY}
    for value in ({'other': POLICY}, {'a': {'allowed': [64], 'rotation': [100]}},
                  {'a': {'allowed': [True], 'rotation': [True]}}, {'a': {'allowed': [], 'rotation': []}}):
        with pytest.raises(ValueError):
            validate_dfs_policies(value, [CONFIG])
