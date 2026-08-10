"""Support notify entity for Xiaomi Miot."""
import logging

from homeassistant.components.notify import (
    DOMAIN as ENTITY_DOMAIN,
    NotifyEntity as BaseEntity,
)

from . import (
    DOMAIN,
    XIAOMI_CONFIG_SCHEMA as PLATFORM_SCHEMA,  # noqa: F401
    HassEntry,
    XEntity,
    async_setup_config_entry,
)

_LOGGER = logging.getLogger(__name__)
DATA_KEY = f'{ENTITY_DOMAIN}.{DOMAIN}'


async def async_setup_entry(hass, config_entry, async_add_entities):
    HassEntry.init(hass, config_entry).new_adder(ENTITY_DOMAIN, async_add_entities)
    await async_setup_config_entry(hass, config_entry, async_setup_platform, async_add_entities, ENTITY_DOMAIN)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hass.data.setdefault(DATA_KEY, {})


class NotifyEntity(XEntity, BaseEntity):
    """A MIoT action that takes arguments no single-value control can express.

    Actions with one input become a select or a text entity; those with several
    have no natural control, and were previously dropped. Sending them as a
    notify message keeps them reachable from automations — the message carries
    the parameters.
    """

    def on_init(self):
        self._attr_available = True

    def set_state(self, data: dict):
        pass

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        params = self.parse_message(message)
        await self.device.async_write({self.attr: params})

    @staticmethod
    def parse_message(message: str):
        """Turn a message into action parameters.

        A MIoT action takes a positional list. Comma-separated input is the
        natural way to express that from an automation, so split on commas and
        coerce anything numeric — `MiotActionConv.encode` handles the rest.
        A message with no comma is passed through as a single value.
        """
        if message is None:
            return []
        parts = [p.strip() for p in str(message).split(',')]
        out = []
        for part in parts:
            try:
                out.append(int(part))
                continue
            except ValueError:
                pass
            try:
                out.append(float(part))
            except ValueError:
                out.append(part)
        return out


XEntity.CLS[ENTITY_DOMAIN] = NotifyEntity
