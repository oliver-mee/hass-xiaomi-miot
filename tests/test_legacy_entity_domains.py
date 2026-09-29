"""Legacy entity suggestions must belong to the platform adding them."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_DEVICE
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.xiaomi_miot import MiotEntity
from custom_components.xiaomi_miot.vacuum import MiotVacuumEntity
from custom_components.xiaomi_miot.water_heater import MiotWaterHeaterEntity


@pytest.mark.parametrize(('entity_cls', 'domain'), [
    (MiotVacuumEntity, 'vacuum'), (MiotWaterHeaterEntity, 'water_heater'),
])
@pytest.mark.asyncio
async def test_legacy_platform_registers_and_preserves_existing_id(
    hass, make_device, load_miot_spec, entity_cls, domain,
):
    spec = load_miot_spec('test.generic.fallback.json')
    device = make_device(spec)
    entity = entity_cls({CONF_DEVICE: device}, spec.get_service('printer'))
    assert entity.entity_id.startswith(f'{domain}.')
    entity._attr_device_info = None
    unique_id = entity.unique_id
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(domain, domain, unique_id, suggested_object_id='my_existing_device')
    component = EntityComponent(device.log, domain, hass)
    # Avoid device I/O; this test exercises HA's real registration machinery.
    with patch.object(entity, 'async_added_to_hass', AsyncMock()):
        await component.async_add_entities([entity])
    assert entity.entity_id == existing.entity_id
    assert entity.unique_id == unique_id
    await component.async_remove_entity(entity.entity_id)


def test_no_service_fallback_uses_platform_domain(make_device, load_miot_spec):
    class LegacyVacuum(MiotEntity):
        _entity_domain = 'vacuum'

    device = make_device(load_miot_spec('test.generic.fallback.json'))
    entity = LegacyVacuum(config={CONF_DEVICE: device})
    assert entity.entity_id.startswith('vacuum.')
    assert entity.unique_id == device.unique_id
