"""Validate number commands before conversion or action execution."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.setup import async_setup_component
from homeassistant.exceptions import ServiceValidationError
from custom_components.xiaomi_miot.number import NumberEntity


def number_entity(hass, bounds, integer=False, action=False):
    entity = NumberEntity.__new__(NumberEntity)
    entity.hass = hass
    entity.entity_id = 'number.audit'
    entity.attr = 'number.audit'
    entity.device = SimpleNamespace(async_write=AsyncMock())
    entity._attr_native_min_value, entity._attr_native_max_value, entity._attr_native_step = bounds
    entity._attr_native_unit_of_measurement = None
    entity._attr_native_value = None
    entity._miot_property = SimpleNamespace(is_integer=integer)
    entity._miot_action = object() if action else None
    return entity


@pytest.mark.parametrize('action', [False, True])
@pytest.mark.parametrize('value', [4, 4.7, 0, 10, float('nan'), float('inf'), -float('inf')])
async def test_invalid_numbers_never_write(hass, value, action):
    entity = number_entity(hass, (1, 9, 2), integer=True, action=action)
    with pytest.raises(ServiceValidationError, match='steps of 2'):
        await entity.async_set_native_value(value)
    entity.device.async_write.assert_not_awaited()


@pytest.mark.parametrize('value', [4, 4.7, float('nan')])
async def test_ha_service_rejects_off_step_values(hass, value):
    entity = number_entity(hass, (1, 9, 2), integer=True)
    entity._attr_available = True
    entity.async_added_to_hass = AsyncMock()
    entity.async_will_remove_from_hass = AsyncMock()
    assert await async_setup_component(hass, 'number', {})
    await hass.data['number'].async_add_entities([entity])
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call('number', 'set_value', {
            'entity_id': entity.entity_id, 'value': value,
        }, blocking=True)
    entity.device.async_write.assert_not_awaited()


@pytest.mark.parametrize('bounds,value,expected,integer', [
    ((1, 9, 2), 1, 1, True), ((1, 9, 2), 9, 9, True),
    ((-5, 5, 2), -3, -3, True), ((0, 1, .1), .1 + .2, .3, False),
    ((.05, .95, .1), .35, .35, False), ((-1, 1, .25), -.75, -.75, False),
    ((0, 10, 1), 3.0000000000000004, 3, True),
])
@pytest.mark.parametrize('action', [False, True])
async def test_valid_values_preserve_device_value(hass, bounds, value, expected, integer, action):
    entity = number_entity(hass, bounds, integer, action)
    with patch('custom_components.xiaomi_miot.number.async_call_later'):
        await entity.async_set_native_value(value)
    entity.device.async_write.assert_awaited_once_with({'number.audit': expected})
    if integer:
        assert type(entity.device.async_write.call_args.args[0]['number.audit']) is int


async def test_integer_property_cannot_accept_fractional_step(hass):
    entity = number_entity(hass, (0, 10, .5), integer=True)
    with pytest.raises(ServiceValidationError, match='requires an integer'):
        await entity.async_set_native_value(2.5)
    entity.device.async_write.assert_not_awaited()


@pytest.mark.parametrize('value', [.3501, -.751, .34999999])
async def test_float_tolerance_does_not_round_unsupported_values(hass, value):
    entity = number_entity(hass, (-1, 1, .1))
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(value)
    entity.device.async_write.assert_not_awaited()
