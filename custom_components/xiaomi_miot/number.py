"""Support number entity for Xiaomi Miot."""
import logging
from decimal import Decimal, InvalidOperation

from homeassistant.exceptions import ServiceValidationError

from homeassistant.components.number import (
    DOMAIN as ENTITY_DOMAIN,
    RestoreNumber,
    NumberMode,
)
from homeassistant.helpers.event import async_call_later

from . import (
    DOMAIN,
    XIAOMI_CONFIG_SCHEMA as PLATFORM_SCHEMA,  # noqa: F401
    HassEntry,
    XEntity,
    async_setup_config_entry,
    bind_services_to_entries,
)

_LOGGER = logging.getLogger(__name__)
DATA_KEY = f'{ENTITY_DOMAIN}.{DOMAIN}'

SERVICE_TO_METHOD = {}


async def async_setup_entry(hass, config_entry, async_add_entities):
    HassEntry.init(hass, config_entry).new_adder(ENTITY_DOMAIN, async_add_entities)
    await async_setup_config_entry(hass, config_entry, async_setup_platform, async_add_entities, ENTITY_DOMAIN)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hass.data[DOMAIN]['add_entities'][ENTITY_DOMAIN] = async_add_entities
    bind_services_to_entries(hass, SERVICE_TO_METHOD)


class NumberEntity(XEntity, RestoreNumber):
    _attr_mode = NumberMode.AUTO

    def on_init(self):
        if self._miot_property:
            self._attr_native_step = self._miot_property.range_step()
            self._attr_native_max_value = self._miot_property.range_max()
            self._attr_native_min_value = self._miot_property.range_min()
            self._attr_native_unit_of_measurement = self._miot_property.unit_of_measurement

    def get_state(self) -> dict:
        return {self.attr: self._attr_native_value}

    def set_state(self, data: dict):
        val = self.conv.value_from_dict(data)
        if val is None:
            return
        self._attr_native_value = val

    def _validated_native_value(self, value: float) -> int | float:
        minimum, maximum, step = (
            Decimal(str(v)) for v in (self.native_min_value, self.native_max_value, self.native_step)
        )
        message = (
            f"Invalid value {value} for {self.entity_id}: expected a finite value "
            f"from {minimum} to {maximum} in steps of {step} starting at {minimum}"
        )
        if self._miot_property and self._miot_property.is_integer:
            message += "; this property requires an integer"
        try:
            number = Decimal(str(value))
            if not all(v.is_finite() for v in (number, minimum, maximum, step)) or step <= 0:
                raise ServiceValidationError(message)
            if not minimum <= number <= maximum:
                raise ServiceValidationError(message)
            tick = (number - minimum) / step
            nearest = tick.to_integral_value()
            # Accept representation noise, not a request for a different step.
            if abs(tick - nearest) > Decimal('1e-9'):
                raise ServiceValidationError(message)
            number = minimum + nearest * step
            if not minimum <= number <= maximum:
                raise ServiceValidationError(message)
            if self._miot_property and self._miot_property.is_integer:
                if number != number.to_integral_value():
                    raise ServiceValidationError(message)
                return int(number)
            return float(number)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ServiceValidationError(message) from exc

    async def async_set_native_value(self, value: float):
        value = self._validated_native_value(value)
        await self.device.async_write({self.attr: value})

        if self._miot_action:
            self._attr_native_value = None
            async_call_later(self.hass, 0.5, self.schedule_update_ha_state)


XEntity.CLS[ENTITY_DOMAIN] = NumberEntity
