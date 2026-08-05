"""Support event entity for Xiaomi Miot."""
import logging

from homeassistant.components.event import (
    DOMAIN as ENTITY_DOMAIN,
    EventEntity as BaseEntity,
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


class EventEntity(XEntity, BaseEntity):
    def on_init(self):
        # A MIoT event maps to exactly one HA event type. Devices distinguish
        # kinds of occurrence with separate spec events, not with a payload
        # discriminator, so a per-entity list of one keeps them separable in
        # automations.
        self._attr_event_types = [self._miot_event.name] if self._miot_event else []

    def set_state(self, data: dict):
        if self.attr not in data:
            return
        value = data[self.attr]
        attrs = value if isinstance(value, dict) else {'value': value}
        self._trigger_event(self._attr_event_types[0], attrs)


XEntity.CLS[ENTITY_DOMAIN] = EventEntity
