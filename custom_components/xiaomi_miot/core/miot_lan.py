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

from miio.protocol import Message as MiioMessage

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
SUBSCRIBE_TIMEOUT_SECONDS = KEEPALIVE_SECONDS
STALE_SECONDS = KEEPALIVE_SECONDS * 3


class LanDevice:
    """One subscribed device: its crypto, address and subscription state."""

    def __init__(self, device: 'Device', host: str, token: str):
        self.device = device
        self.did = device.info.did
        self.host = host
        self.token = bytes.fromhex(token)
        if len(self.token) != 16:
            raise ValueError("LAN tokens must contain 16 bytes")
        self.key = hashlib.md5(self.token).digest()
        self.iv = hashlib.md5(self.key + self.token).digest()
        self.device_id: Optional[int] = None
        self.delta_ts: Optional[float] = None
        self.subscribed = False
        self.sub_id: Optional[int] = None
        self.sub_ts = 0.0
        self.pending_since: Optional[float] = None
        self.last_attempt: Optional[float] = None
        self.last_confirmed: Optional[float] = None
        self.last_seen: Optional[float] = None
        self.request_id = random.randint(100000000, 999999999)

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

    def _parse_frame(self, raw: bytes):
        if len(raw) < 32 or raw[:2] != b'\x21\x31':
            return None
        if int.from_bytes(raw[2:4], 'big') != len(raw):
            return None
        if self.device_id is not None and int.from_bytes(raw[8:12], 'big') != self.device_id:
            return None
        try:
            return MiioMessage.parse(raw, token=self.token)
        except Exception:  # noqa: BLE001 - malformed network input
            _LOGGER.debug('%s: invalid lan packet', self.did)
            return None

    def parse(self, raw: bytes) -> Optional[dict]:
        if len(raw) <= 32:
            return None
        frame = self._parse_frame(raw)
        if frame is None or not isinstance(frame.data.value, dict):
            return None
        return frame.data.value

    def note_hello(self, raw: bytes) -> bool:
        # Hello responses have no authenticated checksum in the miIO protocol.
        if len(raw) != 32 or self._parse_frame(raw) is None:
            return False
        device_id = int.from_bytes(raw[8:12], 'big')
        if device_id in (0, 0xffffffff):
            return False
        self.device_id = device_id
        self.delta_ts = time.time() - int.from_bytes(raw[12:16], 'big')
        self.last_seen = time.monotonic()
        return True


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
        try:
            self._sendto(lan.host, HELLO)
        except OSError as exc:
            _LOGGER.debug('%s: lan hello failed: %s', lan.did, exc)

    def _subscribe(self, lan: LanDevice):
        lan.request_id = (lan.request_id + 1) % 1000000000
        lan.sub_id = lan.request_id
        lan.pending_since = lan.last_attempt = time.monotonic()
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

    def _expire_subscription(self, lan: LanDevice, now: float):
        if lan.pending_since is not None and now - lan.pending_since >= SUBSCRIBE_TIMEOUT_SECONDS:
            lan.pending_since = None
            lan.sub_id = None
            lan.subscribed = False
        if lan.last_seen is not None and now - lan.last_seen >= STALE_SECONDS:
            lan.subscribed = False

    def _maybe_subscribe(self, lan: LanDevice, now: float):
        self._expire_subscription(lan, now)
        if lan.pending_since is not None or lan.device_id is None:
            return
        if lan.last_seen is None or now - lan.last_seen >= STALE_SECONDS:
            return
        if lan.last_attempt is not None and now - lan.last_attempt < SUBSCRIBE_TIMEOUT_SECONDS:
            return
        if not lan.subscribed or lan.last_confirmed is None or now - lan.last_confirmed >= RESUBSCRIBE_SECONDS:
            self._subscribe(lan)

    async def _keepalive(self, _now=None):
        now = time.monotonic()
        for lan in list(self.devices.values()):
            self._expire_subscription(lan, now)
            self._handshake(lan)
            self._maybe_subscribe(lan, now)

    # -- receive ----------------------------------------------------------

    def on_datagram(self, data: bytes, addr):
        lan = self._by_host.get(addr[0])
        if not lan:
            return

        if len(data) == 32:
            if not lan.note_hello(data):
                return
            self._maybe_subscribe(lan, time.monotonic())
            return

        msg = lan.parse(data)
        if not msg:
            return

        now = time.monotonic()
        if type(msg.get('id')) is int and lan.pending_since is not None and msg['id'] == lan.sub_id and 'result' in msg:
            result = msg['result']
            if not isinstance(result, dict) or type(result.get('code')) is not int:
                return
            self._expire_subscription(lan, now)
            if lan.pending_since is None:
                return
            lan.last_seen = now
            lan.subscribed = result['code'] == 0
            lan.pending_since = None
            lan.sub_id = None
            if lan.subscribed:
                lan.last_confirmed = now
            _LOGGER.info(
                'Miot lan subscribe %s: %s',
                'ok' if lan.subscribed else 'failed', lan.device.name_model)
            return

        method = msg.get('method')
        if method not in ('properties_changed', 'event_occured'):
            return
        if type(msg.get('id')) is not int:
            return
        params = msg.get('params')
        values = params if isinstance(params, list) else [params]
        iid = 'piid' if method == 'properties_changed' else 'eiid'
        if not values or any(
            not isinstance(value, dict)
            or type(value.get('siid')) is not int or value['siid'] <= 0
            or type(value.get(iid)) is not int or value[iid] <= 0
            for value in values
        ):
            return
        lan.last_seen = now
        # Acknowledge valid uplinks, including duplicates, but never responses.
        self._send(lan, {'id': msg['id'], 'result': {'code': 0}})
        if self._is_duplicate(lan.did, msg.get('id')):
            return

        payload = lan.device.decode(values)
        if payload:
            lan.device.dispatch(payload)

    def _is_duplicate(self, did, msg_id) -> bool:
        """Devices resend until acked, so the same message arrives more than once."""
        if msg_id is None:
            return False
        key = f'{did}.{msg_id}'
        now = time.monotonic()
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
