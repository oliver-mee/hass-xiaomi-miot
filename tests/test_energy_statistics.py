"""Keep absent energy samples distinct from zero and restore native units once."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.restore_state import RestoredExtraData
from homeassistant.util import dt

from custom_components.xiaomi_miot.core.converters import BaseConv
from custom_components.xiaomi_miot.core.templates import template
from custom_components.xiaomi_miot.sensor import SensorEntity


def energy(make_device, load_miot_spec, attr='power_cost_today'):
    device = make_device(load_miot_spec('test.generic.fallback.json'),
                         customizes={'value_ratio': 0.001})
    entity = SensorEntity(device, BaseConv(attr=attr, domain='sensor'))
    return device, entity


@pytest.mark.parametrize('bad', [None, '', True, -1, float('nan'), float('inf'), 'bad'])
async def test_invalid_energy_keeps_last_value(make_device, load_miot_spec, bad):
    _, sensor = energy(make_device, load_miot_spec)
    sensor.set_state({'power_cost_today': 1000})
    assert sensor.native_value == 1
    sensor.set_state({'power_cost_today': bad})
    assert sensor.native_value == 1
    sensor.set_state({'power_cost_today': 500})
    assert sensor.native_value == 0.5  # Real decreases reach HA without a heuristic.
    sensor.set_state({'power_cost_today': 0})
    assert sensor.native_value == 0


async def test_restore_roundtrip_does_not_scale_twice(make_device, load_miot_spec):
    _, first = energy(make_device, load_miot_spec)
    first.set_state({'power_cost_today': 1234})
    _, restored = energy(make_device, load_miot_spec)
    restored.entity_id = 'sensor.energy_test'
    restored.async_get_last_extra_data = AsyncMock(return_value=RestoredExtraData(first.get_state()))
    await restored.async_added_to_hass()
    assert restored.native_value == 1.234
    restored.set_state({'power_cost_today': 2000})
    assert restored.native_value == 2
    await restored.async_will_remove_from_hass()


@pytest.mark.parametrize('values, expected', [([], None), ([None], None), ([True], None), ([0], 0), ([2.5], 2.5)])
async def test_template_missing_vs_numeric_zero(hass, values, expected):
    result = template('micloud_statistics_power_cost', hass).async_render({
        'result': [{'time': int(dt.now().timestamp()), 'value': values}],
    })
    assert result['power_cost_today'] == expected
    assert result['power_cost_month'] == expected


async def test_empty_statistics_do_not_fabricate_reset(hass):
    assert template('micloud_statistics_power_cost', hass).async_render({'result': []}) == {
        'power_cost_today': None, 'power_cost_month': None,
    }


async def test_invalid_cloud_stats_preserve_cached_property(make_device, load_miot_spec):
    device, sensor = energy(make_device, load_miot_spec)
    device.props['power_cost_today'] = 1000
    sensor.set_state(device.props)
    device.cloud = SimpleNamespace(async_request_api=AsyncMock(return_value={'result': []}))
    await device.update_cloud_statistics([{'key': 'energy', 'template': 'micloud_statistics_power_cost'}])
    assert device.props['power_cost_today'] == 1000
    assert sensor.native_value == 1


async def test_malformed_statistics_json_keeps_energy_state(make_device, load_miot_spec):
    device, sensor = energy(make_device, load_miot_spec)
    device.props['power_cost_today'] = 1000
    sensor.set_state(device.props)
    device.cloud = SimpleNamespace(async_request_api=AsyncMock(return_value={
        'result': [{'time': int(dt.now().timestamp()), 'value': 'not json'}],
    }))
    await device.update_cloud_statistics([{'key': 'energy', 'template': 'micloud_statistics_power_cost'}])
    assert device.props['power_cost_today'] == 1000
    assert sensor.native_value == 1


async def test_default_energy_policy_accepts_device_corrections(make_device, load_miot_spec):
    from custom_components.xiaomi_miot.core.utils import power_cost_guard_enabled
    device, _ = energy(make_device, load_miot_spec)
    now = dt.now()
    assert power_cost_guard_enabled(device) is False
    device.props['power_cost_today'] = 1000
    first = device._filter_power_cost_statistics({'power_cost_today': 1000}, now)
    device.props.update(first)
    assert device._filter_power_cost_statistics({'power_cost_today': 500}, now) == {'power_cost_today': 500.0}
    assert device._filter_power_cost_statistics({'power_cost_today': 0}, now) == {'power_cost_today': 0.0}


async def test_default_energy_policy_does_not_register_midnight_reset(make_device, load_miot_spec):
    from unittest.mock import Mock
    _, sensor = energy(make_device, load_miot_spec)
    sensor.entity_id = 'sensor.energy_no_reset'
    sensor.async_on_remove = Mock()
    await sensor.async_added_to_hass()
    sensor.async_on_remove.assert_not_called()


async def test_energy_guard_is_explicit_opt_in(make_device, load_miot_spec):
    from custom_components.xiaomi_miot.core.utils import power_cost_guard_enabled
    device = make_device(load_miot_spec('test.generic.fallback.json'),
                         customizes={'power_cost_guard': True})
    assert power_cost_guard_enabled(device) is True
    now = dt.now()
    device.props.update(device._filter_power_cost_statistics({'power_cost_today': 1000}, now))
    assert device._filter_power_cost_statistics({'power_cost_today': 500}, now) == {}
