"""Receive MIoT uplink over the LAN.

`AsyncMiIO` opens a socket per request and closes it, so an unsolicited message
has nowhere to land. A device that has been sent `miIO.sub`, though, pushes
`properties_changed` and `event_occured` of its own accord — which is the only
way to get *every* occurrence. The cloud message feed is rate limited to roughly
one notification per device per half hour, so it reports a sample, not a stream.

One long-lived endpoint per Home Assistant instance, shared by every device.

Protocol, from observed behaviour:

1. Hello — 32 bytes, `21310020` + `ff` * 28. The reply carries the device id and
   the device's own clock, whose offset every later frame needs.
2. Subscribe — an ordinary encrypted `miIO.sub`, `sub_method: '.'` for all.
3. Uplink — the device pushes to the socket the subscribe came from.
4. Ack — `{"id": <uplink id>, "result": {"code": 0}}`. A *result* frame; a
   method call is ignored and the device retries until `user ack timeout`.
5. Keepalive — re-ping well inside a minute or the subscription lapses.
"""
import asyncio
import hashlib
import json
import logging
import random
import time
from typing import TYPE_CHECKING, Optional

from homeassistant.core import HomeAssistant

from .utils import DeviceException

if TYPE_CHECKING:
    from .device import Device

_LOGGER = logging.getLogger(__name__)

OT_PORT = 54321
HELLO = bytes.fromhex('21310020' + 'ffffffff' * 7)

KEEPALIVE_SECONDS = 25
RESUBSCRIBE_SECONDS = 300
DEDUP_SECONDS = 5


class LanDevice:
    """One subscribed device: its crypto, address and subscription state."""

    def __init__(self, device: 'Device', host: str, token: str):
        self.device = device
        self.did = device.info.did
        self.host = host
        self.token = bytes.fromhex(token)
        self.key = hashlib.md5(self.token).digest()
        self.iv = hashlib.md5(self.key + self.token).digest()
        self.device_id: Optional[int] = None
        self.delta_ts: Optional[float] = None
        self.subscribed = False
        self.sub_id: Optional[int] = None
        self.sub_ts = 0.0

    # -- crypto -----------------------------------------------------------

    def _cipher(self):
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        return Cipher(
            algorithms.AES(self.key), modes.CBC(self.iv), backend=default_backend(),
        )

    def encrypt(self, plaintext: bytes) -> bytes:
        from cryptography.hazmat.primitives import padding
        p = padding.PKCS7(128).padder()
        e = self._cipher().encryptor()
        return e.update(p.update(plaintext) + p.finalize()) + e.finalize()

    def decrypt(self, ciphertext: bytes) -> bytes:
        from cryptography.hazmat.primitives import padding
        d = self._cipher().decryptor()
        u = padding.PKCS7(128).unpadder()
        return u.update(d.update(ciphertext) + d.finalize()) + u.finalize()

    # -- framing ----------------------------------------------------------

    def frame(self, payload: dict) -> bytes:
        if self.device_id is None or self.delta_ts is None:
            raise DeviceException('lan device not handshaked')
        data = self.encrypt(
            json.dumps(payload, separators=(',', ':')).encode() + b'\x00')
        raw = b'\x21\x31'
        raw += (32 + len(data)).to_bytes(2, 'big')
        raw += b'\x00\x00\x00\x00'
        raw += self.device_id.to_bytes(4, 'big')
        raw += int(time.time() - self.delta_ts).to_bytes(4, 'big')
        raw += hashlib.md5(raw + self.token + data).digest()
        return raw + data

    def parse(self, raw: bytes) -> Optional[dict]:
        if raw[:2] != b'\x21\x31' or len(raw) <= 32:
            return None
        try:
            return json.loads(self.decrypt(raw[32:]).rstrip(b'\x00'))
        except Exception as exc:  # noqa: BLE001 - a foreign packet must not kill the listener
            _LOGGER.debug('%s: undecodable lan packet: %s', self.did, exc)
            return None

    def note_hello(self, raw: bytes):
        self.device_id = int.from_bytes(raw[8:12], 'big')
        self.delta_ts = time.time() - int.from_bytes(raw[12:16], 'big')


class MiotLanListener:
    """A single UDP endpoint receiving uplink for every subscribed device."""

    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.devices: dict[str, LanDevice] = {}
        self._by_host: dict[str, LanDevice] = {}
        self._transport = None
        self._protocol = None
        self._unsub_timer = None
        self._recent: dict[str, float] = {}

    @staticmethod
    def get(hass: HomeAssistant) -> 'MiotLanListener':
        from .const import DOMAIN
        store = hass.data.setdefault(DOMAIN, {})
        listener = store.get('lan_listener')
        if not listener:
            listener = MiotLanListener(hass)
            store['lan_listener'] = listener
        return listener

    async def async_start(self):
        if self._transport:
            return
        loop = asyncio.get_event_loop()
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: _LanProtocol(self), local_addr=('0.0.0.0', 0),
        )
        _LOGGER.info('Miot lan listener started on %s', self._transport.get_extra_info('sockname'))

    async def async_stop(self):
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        for dev in list(self.devices.values()):
            self._send(dev, {
                'id': random.randint(100000000, 999999999),
                'method': 'miIO.unsub',
                'params': {
                    'version': '2.0', 'did': str(dev.device_id or 0),
                    'update_ts': int(dev.sub_ts or 0), 'sub_method': '.',
                },
            })
        self.devices.clear()
        self._by_host.clear()
        if self._transport:
            self._transport.close()
            self._transport = None

    async def async_add_device(self, device: 'Device'):
        host = device.info.host
        token = device.info.token
        if not host or not token:
            _LOGGER.debug('%s: no host or token, cannot listen on lan', device.name_model)
            return
        if device.info.did in self.devices:
            return
        try:
            lan = LanDevice(device, host, token)
        except ValueError:
            # BLE devices carry a 24-char token that is not valid hex
            _LOGGER.debug('%s: token is not a lan token', device.name_model)
            return

        await self.async_start()
        self.devices[lan.did] = lan
        self._by_host[host] = lan
        self._handshake(lan)
        self._schedule_keepalive()

    def async_remove_device(self, did: str):
        lan = self.devices.pop(did, None)
        if lan:
            self._by_host.pop(lan.host, None)

    # -- wire -------------------------------------------------------------

    def _sendto(self, host: str, data: bytes):
        if self._transport:
            self._transport.sendto(data, (host, OT_PORT))

    def _send(self, lan: LanDevice, payload: dict):
        try:
            self._sendto(lan.host, lan.frame(payload))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug('%s: lan send failed: %s', lan.did, exc)

    def _handshake(self, lan: LanDevice):
        self._sendto(lan.host, HELLO)

    def _subscribe(self, lan: LanDevice):
        lan.sub_id = random.randint(100000000, 999999999)
        lan.sub_ts = time.time()
        self._send(lan, {
            'id': lan.sub_id,
            'method': 'miIO.sub',
            'params': {
                'version': '2.0',
                'did': str(lan.device_id or 0),
                'update_ts': int(lan.sub_ts),
                'sub_method': '.',
            },
        })

    def _schedule_keepalive(self):
        if self._unsub_timer:
            return
        from datetime import timedelta
        from homeassistant.helpers.event import async_track_time_interval
        self._unsub_timer = async_track_time_interval(
            self.hass, self._keepalive, timedelta(seconds=KEEPALIVE_SECONDS))

    async def _keepalive(self, _now=None):
        now = time.time()
        for lan in list(self.devices.values()):
            self._handshake(lan)
            # A subscription is not advertised as expiring, so renew on a timer
            # rather than waiting to notice it has gone quiet.
            if lan.subscribed and now - lan.sub_ts > RESUBSCRIBE_SECONDS:
                self._subscribe(lan)

    # -- receive ----------------------------------------------------------

    def on_datagram(self, data: bytes, addr):
        lan = self._by_host.get(addr[0])
        if not lan:
            return

        if len(data) == 32:
            first = lan.device_id is None
            lan.note_hello(data)
            if first:
                self._subscribe(lan)
            return

        msg = lan.parse(data)
        if not msg:
            return

        if 'id' in msg:
            # Ack before anything else can fail, or the device retries for ~4s
            # and then gives up with `user ack timeout`.
            self._send(lan, {'id': msg['id'], 'result': {'code': 0}})

        if msg.get('id') == lan.sub_id and 'result' in msg:
            lan.subscribed = msg.get('result', {}).get('code') == 0
            _LOGGER.info(
                'Miot lan subscribe %s: %s',
                'ok' if lan.subscribed else 'failed', lan.device.name_model)
            return

        method = msg.get('method')
        if method not in ('properties_changed', 'event_occured'):
            return
        if self._is_duplicate(lan.did, msg.get('id')):
            return

        params = msg.get('params')
        payload = lan.device.decode(params if isinstance(params, list) else [params])
        if payload:
            lan.device.dispatch(payload)

    def _is_duplicate(self, did, msg_id) -> bool:
        """Devices resend until acked, so the same message arrives more than once."""
        if msg_id is None:
            return False
        key = f'{did}.{msg_id}'
        now = time.time()
        for k, ts in list(self._recent.items()):
            if now - ts > DEDUP_SECONDS:
                del self._recent[k]
        if key in self._recent:
            return True
        self._recent[key] = now
        return False


class _LanProtocol(asyncio.DatagramProtocol):
    def __init__(self, listener: MiotLanListener):
        self.listener = listener

    def datagram_received(self, data: bytes, addr):
        try:
            self.listener.on_datagram(data, addr)
        except Exception as exc:  # noqa: BLE001 - never let one packet kill the endpoint
            _LOGGER.warning('Miot lan receive failed: %s', exc)

    def error_received(self, exc):
        _LOGGER.debug('Miot lan socket error: %s', exc)
