import hashlib
import json

import pytest

from custom_components.xiaomi_miot.core.miot_lan import (
    HELLO,
    LanDevice,
    MiotLanListener,
)

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
