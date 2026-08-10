import logging
import asyncio
from typing import TYPE_CHECKING
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_USERNAME
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN, SUPPORTED_DOMAINS
from .utils import message_event_names
from .xiaomi_cloud import MiotCloud

if TYPE_CHECKING:
    from .device import Device

_LOGGER = logging.getLogger(__name__)

class HassEntry:
    ALL: dict[str, 'HassEntry'] = {}
    cloud: MiotCloud = None
    cloud_devices = None

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.id = entry.entry_id
        self.hass = hass
        self.entry = entry
        self.adders: dict[str, AddEntitiesCallback] = {}
        self.devices: dict[str, 'Device'] = {}
        self.mac_to_did = {}
        self.did_to_unique = {}

    @staticmethod
    def init(hass: HomeAssistant, entry: ConfigEntry):
        this = HassEntry.ALL.get(entry.entry_id)
        if not this:
            this = HassEntry(hass, entry)
            HassEntry.ALL[entry.entry_id] = this
        return this

    async def async_unload(self):
        ret = all(
            await asyncio.gather(
                *[
                    self.hass.config_entries.async_forward_entry_unload(self.entry, domain)
                    for domain in SUPPORTED_DOMAINS
                ]
            )
        )
        if ret:
            for device in self.devices.values():
                await device.async_unload()
            HassEntry.ALL.pop(self.entry.entry_id, None)
        return ret

    def __getattr__(self, item):
        return getattr(self.entry, item)

    @property
    def setup_in_progress(self):
        return self.entry.state == ConfigEntryState.SETUP_IN_PROGRESS

    def get_config(self, key=None, default=None):
        dat = {
            **self.entry.data,
            **self.entry.options,
        }
        if self.filter_models:
            dat.pop('filter_did', None)
            dat.pop('did_list', None)
        else:
            dat.pop('filter_model', None)
            dat.pop('model_list', None)
        if key:
            return dat.get(key, default)
        return dat

    @property
    def filter_models(self):
        data = {
            **self.entry.data,
            **self.entry.options,
        }
        if data.get('did_list'):
            return False
        if data.get('model_list'):
            return True
        if 'did_list' in data:
            return False
        if 'model_list' in data:
            return True
        return data.get('filter_models', False)

    async def new_device(self, device_info: dict):
        from .device import Device, DeviceInfo
        info = DeviceInfo(device_info)
        if device := self.devices.get(info.unique_id):
            return device
        device = Device(info, self)
        self.devices[info.unique_id] = device
        self.did_to_unique[info.did] = info.unique_id
        await device.async_init()
        return device

    def get_device_by_did(self, did):
        if not did:
            return None
        if unique_id := self.did_to_unique.get(f'{did}'):
            return self.devices.get(unique_id)
        return None

    @staticmethod
    def dispatch_event_to_devices(msg: dict):
        """Offer one Mi Home message to every entry until one owns the device.

        Routing by device ownership rather than by whichever entry happens to
        own the cloud session: a message names a did, and only one entry holds
        that device. This also covers an account configured as several entries
        (say one per region), where the entry that fetched the message is not
        necessarily the one holding the device it describes.
        """
        for entry in list(HassEntry.ALL.values()):
            if entry.dispatch_device_event(msg):
                return True
        return False

    @staticmethod
    def lan_subscribed(device) -> bool:
        listener = device.hass.data.get(DOMAIN, {}).get('lan_listener')
        if not listener:
            return False
        lan = listener.devices.get(device.info.did)
        return bool(lan and lan.subscribed)

    def dispatch_device_event(self, msg: dict):
        """Route one Mi Home message to its device as a MIoT event.

        The cloud message feed is the only event source available without the
        push transport, so event entities are fed from here rather than from the
        property coordinator. Returns True if it matched an event converter.
        """
        body = (msg.get('params') or {}).get('body') or {}
        did = msg.get('did') or body.get('did')
        device = self.get_device_by_did(did)
        if not device or not device.spec:
            _LOGGER.debug(
                'Event dispatch: no device for did %s (known: %s)',
                did, list(self.did_to_unique),
            )
            return False

        if self.lan_subscribed(device):
            # LAN carries every occurrence and arrives first; the cloud feed is
            # a throttled sample of the same events. Taking both would fire
            # twice for anything that appears in each.
            _LOGGER.debug(
                'Event dispatch: %s is subscribed on lan, ignoring cloud message',
                device.name_model)
            return False

        names = message_event_names(body, device.custom_config('cloud_events'))
        if not names:
            _LOGGER.debug('Event dispatch: no event names in body %s', body)
            return False

        for service in device.spec.services.values():
            event = service.events.get(body.get('eiid')) if body.get('eiid') else None
            if not event:
                for name in names:
                    if event := service.get_event(name):
                        break
            if not event:
                continue
            if not device.find_converter(f'event.{event.full_name}'):
                _LOGGER.debug(
                    'Event dispatch: %s matched but has no converter', event.full_name,
                )
                continue
            device.dispatch(device.decode({
                'siid': service.iid,
                'eiid': event.iid,
                'arguments': body.get('arguments', body.get('extra', body.get('value'))),
            }))
            _LOGGER.debug('Event dispatch: fired %s for did %s', event.full_name, did)
            return True
        # INFO, not debug: an unmatched name is almost always a `cloud_events`
        # alias that has not been written yet, and the message is the only
        # place the real eventType is visible. Logging it turns the alias map
        # into something a user can complete from their own log.
        _LOGGER.info(
            'Event dispatch: no spec event matched %s for %s (did %s). '
            'Add the right one to the `cloud_events` customize.',
            names, device.model, did,
        )
        return False

    def new_adder(self, domain, adder: AddEntitiesCallback):
        self.adders[domain] = adder
        _LOGGER.info('New adder: %s', [domain, adder])

        for device in self.devices.values():
            device.add_entities(domain)

        return self

    async def get_cloud(self, check=False, login=False):
        if not self.cloud:
            if not self.get_config(CONF_USERNAME):
                return None
            self.cloud = await MiotCloud.from_token(self.hass, self.get_config(), login=login)
        if check:
            await self.cloud.async_check_auth(notify=True)
        return self.cloud

    async def get_cloud_devices(self):
        if isinstance(self.cloud_devices, dict):
            return self.cloud_devices
        cloud = await self.get_cloud()
        if not cloud:
            return {}
        config = self.get_config()
        self.cloud_devices = await cloud.async_get_devices_by_key('did', filters=config) or {}
        for did, info in self.cloud_devices.items():
            mac = info.get('mac') or did
            self.mac_to_did[mac] = did
        return self.cloud_devices

    async def get_cloud_device(self, did=None, mac=None):
        devices = await self.get_cloud_devices()
        if mac and not did:
            did = self.mac_to_did.get(mac)
        if did:
            return devices.get(did)
        return None
