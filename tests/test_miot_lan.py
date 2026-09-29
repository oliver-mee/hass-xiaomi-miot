import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.xiaomi_miot.core.miot_lan import (
    HELLO,
    LanDevice,
    MiotLanListener,
)
from custom_components.xiaomi_miot.core.const import DOMAIN
from custom_components.xiaomi_miot.core.device import MiotDevice
from custom_components.xiaomi_miot.core.hass_entry import HassEntry

TOKEN = '00112233445566778899aabbccddeeff'


def hello_reply(device_id=1212381824, ts=1000):
    """The 32-byte handshake reply: magic, len, zeros, did, device clock."""
    return (
        b'\x21\x31' + (32).to_bytes(2, 'big') + b'\x00\x00\x00\x00'
        + device_id.to_bytes(4, 'big') + ts.to_bytes(4, 'big') + b'\x00' * 16
    )


@pytest.fixture
def lan(make_device, load_miot_spec):
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback",
        customizes={"event_entities": True, "generic_entities": True},
    )
    device.info.data['localip'] = '192.168.2.4'
    device.info.data['token'] = TOKEN
    d = LanDevice(device, '192.168.2.4', TOKEN)
    d.note_hello(hello_reply())
    return d


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def get_extra_info(self, _):
        return ('0.0.0.0', 12345)

    def close(self):
        pass


@pytest.fixture
def listener(hass, lan):
    li = MiotLanListener(hass)
    li._transport = FakeTransport()
    li.devices[lan.did] = lan
    li._by_host[lan.host] = lan
    return li


def test_key_derivation_matches_the_miio_scheme(lan):
    token = bytes.fromhex(TOKEN)
    assert lan.key == hashlib.md5(token).digest()
    assert lan.iv == hashlib.md5(lan.key + token).digest()


def test_frame_round_trips(lan):
    payload = {'id': 1, 'method': 'miIO.sub', 'params': {'sub_method': '.'}}
    raw = lan.frame(payload)

    assert raw[:2] == b'\x21\x31'
    assert int.from_bytes(raw[2:4], 'big') == len(raw)
    assert int.from_bytes(raw[8:12], 'big') == lan.device_id
    # checksum covers header + token + ciphertext
    assert raw[16:32] == hashlib.md5(raw[:16] + lan.token + raw[32:]).digest()
    assert lan.parse(raw) == payload


def test_parse_rejects_foreign_packets(lan):
    assert lan.parse(b'not a miio packet') is None
    assert lan.parse(hello_reply()) is None  # header only, no payload


def test_handshake_then_subscribe(listener, lan):
    """A hello reply is what tells us the device clock, so subscribe follows it."""
    lan.device_id = None
    listener.on_datagram(hello_reply(), (lan.host, 54321))

    assert lan.device_id == 1212381824
    sent = [lan.parse(d) for d, _ in listener._transport.sent]
    subs = [m for m in sent if m and m.get('method') == 'miIO.sub']
    assert len(subs) == 1
    assert subs[0]['params']['sub_method'] == '.'


def test_subscribe_reply_marks_subscribed(listener, lan):
    lan.sub_id = 42
    listener.on_datagram(lan.frame({'id': 42, 'result': {'code': 0}}), (lan.host, 54321))

    assert lan.subscribed is True


def test_subscribe_failure_is_not_treated_as_success(listener, lan):
    lan.sub_id = 42
    listener.on_datagram(lan.frame({'id': 42, 'result': {'code': -1}}), (lan.host, 54321))

    assert lan.subscribed is False


def test_event_occured_reaches_the_event_converter(listener, lan):
    seen = []
    lan.device.add_listener(lambda data, only_info=False: seen.append(data))

    listener.on_datagram(lan.frame({
        'id': 7,
        'method': 'event_occured',
        'params': {'did': lan.did, 'siid': 2, 'eiid': 2, 'arguments': [7, 1]},
    }), (lan.host, 54321))

    assert {'event.printer.paper_jammed': {'brightness': 7, 'mode': 1}} in seen


def test_properties_changed_reaches_the_property_converter(listener, lan):
    seen = []
    lan.device.add_listener(lambda data, only_info=False: seen.append(data))

    listener.on_datagram(lan.frame({
        'id': 8,
        'method': 'properties_changed',
        'params': [{'did': lan.did, 'siid': 2, 'piid': 3, 'value': 5}],
    }), (lan.host, 54321))

    assert {'number.printer.brightness': 5} in seen


def test_every_uplink_is_acked(listener, lan):
    """Unacked, the device retries for ~4s and then reports user ack timeout."""
    listener.on_datagram(lan.frame({
        'id': 99, 'method': 'event_occured',
        'params': {'did': lan.did, 'siid': 2, 'eiid': 1, 'arguments': []},
    }), (lan.host, 54321))

    acks = [lan.parse(d) for d, _ in listener._transport.sent]
    assert {'id': 99, 'result': {'code': 0}} in acks


def test_retransmissions_are_dropped(listener, lan):
    """The device resends until acked, so the same id can arrive twice."""
    seen = []
    lan.device.add_listener(lambda data, only_info=False: seen.append(data))
    frame = lan.frame({
        'id': 123, 'method': 'event_occured',
        'params': {'did': lan.did, 'siid': 2, 'eiid': 1, 'arguments': []},
    })

    listener.on_datagram(frame, (lan.host, 54321))
    listener.on_datagram(frame, (lan.host, 54321))

    fired = [d for d in seen if 'event.printer.print_finished' in d]
    assert len(fired) == 1


def test_unknown_methods_are_ignored(listener, lan):
    seen = []
    lan.device.add_listener(lambda data, only_info=False: seen.append(data))

    listener.on_datagram(lan.frame({
        'id': 5, 'method': 'something_else', 'params': {},
    }), (lan.host, 54321))

    assert seen == []


def test_packets_from_unknown_hosts_are_ignored(listener, lan):
    seen = []
    lan.device.add_listener(lambda data, only_info=False: seen.append(data))

    listener.on_datagram(lan.frame({
        'id': 6, 'method': 'event_occured',
        'params': {'did': lan.did, 'siid': 2, 'eiid': 1, 'arguments': []},
    }), ('10.0.0.1', 54321))

    assert seen == []


def test_hello_constant_is_the_documented_probe():
    assert len(HELLO) == 32
    assert HELLO[:4] == bytes.fromhex('21310020')
    assert HELLO[4:] == b'\xff' * 28


def test_cloud_dispatch_defers_to_lan(make_device, load_miot_spec, hass, lan):
    """Both transports carry the same event; only one may fire it."""
    from custom_components.xiaomi_miot.core.const import DOMAIN
    from custom_components.xiaomi_miot.core.hass_entry import HassEntry

    device = lan.device
    entry = HassEntry.__new__(HassEntry)
    entry.devices = {'unique': device}
    entry.did_to_unique = {device.info.did: 'unique'}

    li = MiotLanListener(hass)
    li.devices[lan.did] = lan
    lan.subscribed = True
    hass.data.setdefault(DOMAIN, {})['lan_listener'] = li

    handled = entry.dispatch_device_event({
        'did': device.info.did,
        'params': {'body': {'event': 'paper_jammed', 'arguments': [7, 1]}},
    })

    assert handled is False


@pytest.fixture
async def refreshable_device(hass, lan, listener):
    device = lan.device
    device.cloud = SimpleNamespace(user_id='test-user')
    device.local = MiotDevice.from_device(device)
    device.entry.get_cloud_device = AsyncMock()
    lan.subscribed = True
    hass.data[DOMAIN]['lan_listener'] = listener
    yield device
    await listener.async_stop()


@pytest.mark.parametrize('updates', [
    {'localip': '192.168.2.5'},
    {'token': '11' * 16},
    {'localip': '192.168.2.5', 'token': '11' * 16},
])
async def test_local_refresh_resubscribes_lan_and_allows_cloud_until_ready(
    refreshable_device, listener, lan, updates,
):
    device = refreshable_device
    device.entry.get_cloud_device.return_value = updates
    seen = []
    device.add_listener(lambda data, only_info=False: seen.append(data))
    entry = HassEntry.__new__(HassEntry)
    entry.devices = {'unique': device}
    entry.did_to_unique = {device.info.did: 'unique'}
    message = {
        'did': device.info.did,
        'params': {'body': {'event': 'paper_jammed', 'arguments': [7, 1]}},
    }

    assert await device.async_refresh_local_device() is True
    replacement = listener.devices[lan.did]
    assert replacement is not lan
    assert replacement.host == device.info.host
    assert replacement.token == bytes.fromhex(device.info.token)
    assert listener._by_host == {device.info.host: replacement}
    assert listener._transport.sent == [(HELLO, (device.info.host, 54321))]
    assert replacement.subscribed is False
    assert entry.dispatch_device_event(message) is True

    # Complete the new handshake and subscription with the refreshed token.
    listener.on_datagram(hello_reply(), (replacement.host, 54321))
    listener.on_datagram(replacement.frame({
        'id': replacement.sub_id, 'result': {'code': 0},
    }), (replacement.host, 54321))
    assert replacement.subscribed is True
    assert entry.dispatch_device_event(message) is False
    listener.on_datagram(replacement.frame({
        'id': 77, 'method': 'event_occured',
        'params': {'did': lan.did, 'siid': 2, 'eiid': 2, 'arguments': [7, 1]},
    }), (replacement.host, 54321))
    events = [data for data in seen if 'event.printer.paper_jammed' in data]
    assert len(events) == 2  # One cloud occurrence, then one LAN occurrence.


@pytest.mark.parametrize('cloud_info', [
    {},
    {'localip': '192.168.2.4', 'token': TOKEN},
    {'localip': '0.0.0.0'},
])
async def test_unsuccessful_local_refresh_keeps_lan_registration(
    refreshable_device, listener, lan, cloud_info,
):
    device = refreshable_device
    old_local = device.local
    device.entry.get_cloud_device.return_value = cloud_info

    assert await device.async_refresh_local_device() is False
    assert device.local is old_local
    assert listener.devices[lan.did] is lan
    assert listener._by_host == {lan.host: lan}
    assert lan.subscribed is True
    assert listener._transport.sent == []


async def test_cloud_refresh_error_keeps_lan_registration(refreshable_device, listener, lan):
    device = refreshable_device
    device.entry.get_cloud_device.side_effect = OSError('cloud unavailable')

    with pytest.raises(OSError, match='cloud unavailable'):
        await device.async_refresh_local_device()
    assert listener.devices[lan.did] is lan
    assert lan.subscribed is True
    assert listener._transport.sent == []


async def test_lan_restart_failure_does_not_undo_local_refresh(
    refreshable_device, listener, lan, monkeypatch,
):
    device = refreshable_device
    device.entry.get_cloud_device.return_value = {'localip': '192.168.2.5'}
    monkeypatch.setattr(listener, 'async_add_device', AsyncMock(side_effect=OSError('socket failed')))

    assert await device.async_refresh_local_device() is True
    assert device.local.host == '192.168.2.5'
    assert lan.did not in listener.devices
    assert lan.host not in listener._by_host
    assert HassEntry.lan_subscribed(device) is False


@pytest.mark.parametrize('has_listener', [False, True])
async def test_local_refresh_does_not_enable_lan_events(
    refreshable_device, listener, lan, hass, has_listener,
):
    device = refreshable_device
    listener.async_remove_device(lan.did)
    if not has_listener:
        hass.data[DOMAIN].pop('lan_listener')
    device.entry.get_cloud_device.return_value = {'localip': '192.168.2.5'}

    assert await device.async_refresh_local_device() is True
    assert listener.devices == {}
    assert listener._transport.sent == []
    assert ('lan_listener' in hass.data[DOMAIN]) is has_listener


@pytest.mark.parametrize('damage', ['checksum', 'length', 'identity', 'padding', 'json_shape'])
def test_invalid_packet_cannot_dispatch_or_poison_dedup(listener, lan, damage):
    seen = []
    lan.device.add_listener(lambda data, only_info=False: seen.append(data))
    good = lan.frame({'id': 123, 'method': 'event_occured',
                      'params': {'siid': 2, 'eiid': 1, 'arguments': []}})
    bad = bytearray(good)
    if damage == 'checksum':
        bad[16] ^= 1
    elif damage == 'length':
        bad[3] ^= 1
    elif damage == 'identity':
        bad[8] ^= 1
        bad[16:32] = hashlib.md5(bytes(bad[:16]) + lan.token + bytes(bad[32:])).digest()
    elif damage == 'padding':
        bad = bad[:-1]
        bad[2:4] = len(bad).to_bytes(2, 'big')
        bad[16:32] = hashlib.md5(bytes(bad[:16]) + lan.token + bytes(bad[32:])).digest()
    else:
        bad = lan.frame([1])
    from custom_components.xiaomi_miot.core.miot_lan import _LanProtocol
    protocol = _LanProtocol(listener)
    protocol.datagram_received(bytes(bad), (lan.host, 54321))
    assert seen == []
    assert listener._transport.sent == []
    assert listener._recent == {}
    protocol.datagram_received(good, (lan.host, 54321))
    assert len(seen) == 1


@pytest.mark.parametrize('packet', [b'Z' * 32, hello_reply(device_id=0), hello_reply(device_id=0xffffffff)])
def test_invalid_hello_cannot_change_handshake(listener, lan, packet):
    lan.device_id = None
    lan.delta_ts = None
    listener.on_datagram(packet, (lan.host, 54321))
    assert lan.device_id is None
    assert lan.delta_ts is None
    assert listener._transport.sent == []


@pytest.mark.parametrize('token', ['00' * 12, '00' * 17, 'not hex'])
def test_reject_invalid_token_length(lan, token):
    with pytest.raises(ValueError):
        LanDevice(lan.device, lan.host, token)


@pytest.mark.parametrize('result', [None, [], True, {'code': False}, {'code': '0'}])
def test_malformed_subscription_response_does_not_change_state(listener, lan, result):
    lan.sub_id = 42
    listener.on_datagram(lan.frame({'id': 42, 'result': result}), (lan.host, 54321))
    assert not lan.subscribed
    assert listener._transport.sent == []


def test_subscription_response_is_not_acknowledged(listener, lan):
    lan.sub_id = 42
    listener.on_datagram(lan.frame({'id': 42, 'result': {'code': 0}}), (lan.host, 54321))
    assert lan.subscribed
    assert listener._transport.sent == []


@pytest.mark.parametrize('params', [None, [], [None], {'siid': True, 'eiid': 1}, {'siid': 2, 'eiid': '1'}])
def test_invalid_uplink_does_not_affect_dedup(listener, lan, params):
    listener.on_datagram(lan.frame({'id': 7, 'method': 'event_occured', 'params': params}), (lan.host, 54321))
    assert listener._recent == {}
    assert listener._transport.sent == []
