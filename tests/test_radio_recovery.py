"""Synthetic AP sessions only: no real clients, radios, or network access."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant
import pytest

from custom_components.netgear_wax.api import WaxClient, WaxError
from custom_components.netgear_wax.radio_monitor import RadioMonitor
from custom_components.netgear_wax.radio_recovery import radio_payload, uptime_seconds

CONFIG = {'id': 'a', 'name': 'Synthetic', 'host': 'ap.example.test', 'username': 'sample', 'password': 'synthetic'}
TABLE = {band: {'radioStatus': '1', 'operateMode': '10', 'guardInterval': '1', 'txPower': '2',
                'channel': channel, 'channelWidth': '2', 'beamForming': '1'}
         for band, channel in [('wlan0', '1'), ('wlan1', '48')]}


class Session(WaxClient):
    def __init__(self):
        super().__init__(CONFIG)
        self.calls = []
        self.table = deepcopy(TABLE)
        self.result = {'status': 'ok', 'clients_5ghz': 0, 'reported_clients_5ghz': 0,
                       'clients': [], 'uptime': '2 days 3 hours 4 minutes 5 seconds'}
        self.latest_count = 0
        self.off_error = False
        self.on_error = False
        self.incomplete = False
        self.on_started = asyncio.Event()
        self.on_release = None

    async def request(self, path, payload=None, referer='/dashboard', login=False):
        self.calls.append('login' if login else path)
        return {'status': 0, 'system': {'security_token': 'synthetic'}}

    async def read_clients(self):
        self.calls.append('clients')
        if isinstance(self.result, Exception):
            raise self.result
        return deepcopy(self.result)

    async def dashboard(self):
        self.calls.append('final-count')
        return {'monitor': {'productId': 'WAX625', 'DeviceInfo': {'UpTime': self.result['uptime']}}}, {'wlan1': self.latest_count}

    async def socket(self, payload, referer='/dashboard'):
        table = payload['system']['wlanSettings']['wlanSettingTable']
        if 'getRadioDetails' in table:
            self.calls.append('settings')
            result = deepcopy(self.table)
            if self.incomplete:
                result['wlan0'].pop('beamForming')
            return {'status': 0, 'system': {'wlanSettings': {'wlanSettingTable': result}}}
        if table.get('wlan1', {}).get('radioStatus') in ('0', '1'):
            value = table['wlan1']['radioStatus']
            self.calls.append('off' if value == '0' else 'on')
            assert table['wlan0'] == TABLE['wlan0']
            assert {k: v for k, v in table['wlan1'].items() if k != 'radioStatus'} == {
                k: v for k, v in TABLE['wlan1'].items() if k != 'radioStatus'}
            self.table = deepcopy(table)
            if value == '0' and self.off_error:
                raise WaxError('ambiguous_off_response')
            if value == '1':
                self.on_started.set()
                if self.on_release:
                    await self.on_release.wait()
                if self.on_error:
                    raise WaxError('on_response_failed')
            return {'status': 0}
        self.calls.append('missing-fields')
        return {'status': 0, 'system': {'wlanSettings': {'wlanSettingTable': {}}}}


@pytest.fixture
async def monitor(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    co = SimpleNamespace(hass=hass, entry=SimpleNamespace(entry_id='synthetic'), settings={'aps': [CONFIG]},
                         async_update_listeners=Mock())
    radio = RadioMonitor(co)
    now = time.time()
    radio.state.update(mode='observe', config={'recovery_enabled': True, 'missing_minutes': 30},
                       missing={'a': now - 1801}, checked_timestamp=now,
                       observations={'a': {'visible': False}, 'b': {'visible': True}})
    yield radio
    await hass.async_stop(force=True)


async def collect(client, monitor, cycle=1):
    async with monitor.lock:
        return await client.collect(after_collect=lambda c, r: monitor.after_collection(c, r, cycle))


async def test_complete_collection_recovery_and_logout_share_one_login(monitor):
    client = Session()
    result = await collect(client, monitor)
    assert result['radio_recovery'] == 'recovered'
    assert client.calls == ['/AP_login', 'login', 'clients', 'settings', 'final-count', 'off', 'on', 'settings', '/logout']
    assert monitor.state['cooldowns']['a'] > time.time() + 3500
    saved = await monitor.store.async_load()
    assert saved['last_recovery']['a']['status'] == 'recovered'
    assert client.logout_ok


@pytest.mark.parametrize('changes', [
    {'status': 'unavailable'}, {'clients_5ghz': 1}, {'reported_clients_5ghz': 1},
    {'clients_5ghz': None}, {'clients_5ghz': False}, {'reported_clients_5ghz': None},
    {'clients': [{'band': '5GHz'}]}, {'uptime': '3 minutes'}, {'uptime': 'unknown'},
])
async def test_bad_data_active_clients_and_boot_skip_without_radio_writes(monitor, changes):
    client = Session()
    client.result.update(changes)
    await collect(client, monitor)
    assert not any(x in client.calls for x in ('off', 'on', 'settings'))
    assert client.calls.count('login') == 1


async def test_failure_does_not_enter_hook_or_retry(monitor):
    client = Session()
    client.result = WaxError('unknown_client_status')
    with pytest.raises(WaxError):
        await collect(client, monitor)
    assert client.calls == ['/AP_login', 'login', 'clients', '/logout']
    # A later ordinary cycle, not an immediate retry, is allowed to recover.
    client = Session()
    await collect(client, monitor, 2)
    assert 'off' in client.calls


async def test_new_client_in_same_session_final_count_prevents_off(monitor):
    client = Session()
    client.latest_count = 1
    result = await collect(client, monitor)
    assert result['radio_recovery'] == 'clients_active'
    assert 'off' not in client.calls and 'on' not in client.calls
    assert monitor.state['cooldowns'] == {}


async def test_incomplete_settings_never_guess_defaults(monitor):
    client = Session()
    client.incomplete = True
    result = await collect(client, monitor)
    assert result['radio_recovery'] == 'incomplete_radio_settings:wlan0:beamForming'
    assert 'off' not in client.calls and 'on' not in client.calls


async def test_storage_failure_prevents_first_write(monitor):
    client = Session()
    monitor.save = AsyncMock(side_effect=OSError('synthetic'))
    with pytest.raises(OSError):
        await collect(client, monitor)
    assert 'off' not in client.calls and 'on' not in client.calls
    assert client.logout_ok


async def test_ambiguous_off_still_attempts_on_and_keeps_cooldown(monitor):
    client = Session()
    client.off_error = True
    result = await collect(client, monitor)
    assert result['radio_recovery'] == 'ambiguous_off_response'
    assert client.calls[-3:] == ['off', 'on', '/logout']
    assert monitor.state['cooldowns']['a'] > time.time() + 3500
    another = Session()
    await collect(another, monitor, 2)
    assert 'settings' not in another.calls


@pytest.mark.parametrize('on_error', [False, True])
async def test_cancellation_and_standby_wait_for_on_then_logout(monitor, on_error):
    client = Session()
    client.on_error = on_error
    client.on_release = asyncio.Event()
    poll = asyncio.create_task(collect(client, monitor))
    await client.on_started.wait()
    standby = asyncio.create_task(monitor.set_mode('standby'))
    poll.cancel()
    await asyncio.sleep(0)
    assert not standby.done() and not client.logout_ok
    client.on_release.set()
    with pytest.raises(asyncio.CancelledError):
        await poll
    await standby
    assert client.calls[-1] == '/logout'
    assert client.logout_ok and monitor.state['mode'] == 'standby'


async def test_preflight_runs_only_with_ordinary_collection_and_never_writes(monitor):
    monitor.state['config']['recovery_enabled'] = False
    await monitor.queue_preflight()
    client = Session()
    await collect(client, monitor)
    assert client.calls == ['/AP_login', 'login', 'clients', 'settings', '/logout']
    assert monitor.state['preflight_pending'] == []
    assert monitor.state['preflight']['a']['status'] == 'settings_validated'
    later = Session()
    await collect(later, monitor, 2)
    assert 'settings' not in later.calls


def test_payload_retains_non_radio_fields_and_rejects_incomplete_values():
    assert radio_payload(TABLE, '0')['system']['wlanSettings']['wlanSettingTable']['wlan0'] == TABLE['wlan0']
    bad = deepcopy(TABLE)
    bad['wlan1']['channel'] = None
    with pytest.raises(WaxError):
        radio_payload(bad, '1')


@pytest.mark.parametrize('value', [None, '', '1.5 hours', '5 minutes 1 hour', '1 hour 60 minutes'])
def test_unknown_uptime_rejected(value):
    with pytest.raises(WaxError):
        uptime_seconds(value)


async def test_integration_unload_drains_on_and_logout(monitor):
    client = Session()
    client.on_release = asyncio.Event()
    poll = asyncio.create_task(collect(client, monitor))
    await client.on_started.wait()
    closing = asyncio.create_task(monitor.close())
    await asyncio.sleep(0)
    assert not closing.done() and monitor.closing
    client.on_release.set()
    await poll
    await closing
    assert client.logout_ok
    assert not monitor.recovery_due('a', time.time(), 2)
