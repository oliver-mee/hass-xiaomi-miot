"""Tests for Xiaomi MIoT device registry compatibility."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_miot import DOMAIN, _handle_device_registry_event
from custom_components.xiaomi_miot.core.device import Device, DeviceInfo
from custom_components.xiaomi_miot.core.hass_entry import HassEntry


def _make_device(hass, *, unique_id, entry_id="test-entry"):
    entry = SimpleNamespace(
        hass=hass,
        cloud=None,
        id=entry_id,
        get_config=lambda key=None, default=None: default,
    )
    info = DeviceInfo({
        "did": unique_id,
        "mac": unique_id,
        "name": "Test Device",
        "model": "test.device.model",
    })
    return Device(info, entry)


def test_device_info_omits_empty_via_device(hass):
    """Do not pass the deprecated via_device key for standalone devices."""
    device = _make_device(hass, unique_id="aa:bb:cc:dd:ee:01")

    assert "via_device" not in device.hass_device_info
    assert "via_device_id" not in device.hass_device_info


def test_device_info_uses_parent_device_id(hass):
    """Link proxy devices using the parent's registry ID on newer HA versions."""
    parent = _make_device(hass, unique_id="aa:bb:cc:dd:ee:01")
    child = _make_device(hass, unique_id="aa:bb:cc:dd:ee:02")
    child._proxy_device = parent
    parent_entry = SimpleNamespace(id="parent-device-id")
    registry = Mock()
    registry.async_get_device_by_identifier.return_value = parent_entry

    with patch(
        "custom_components.xiaomi_miot.core.device.dr.async_get",
        return_value=registry,
    ):
        device_info = child.hass_device_info

    registry.async_get_device_by_identifier.assert_called_once_with(
        next(iter(parent.identifiers)), parent.entry.id
    )
    assert device_info["via_device_id"] == parent_entry.id
    assert "via_device" not in device_info


def test_hass_device_uses_unambiguous_identifier_lookup(hass):
    """Look up devices within their owning config entry on newer HA versions."""
    device = _make_device(hass, unique_id="aa:bb:cc:dd:ee:01")
    registry = Mock()
    registry.async_get_device_by_identifier.return_value = expected = object()

    with patch(
        "custom_components.xiaomi_miot.core.device.dr.async_get",
        return_value=registry,
    ):
        assert device.hass_device is expected

    registry.async_get_device_by_identifier.assert_called_once_with(
        next(iter(device.identifiers)), device.entry.id
    )


def test_device_info_falls_back_to_parent_identifier(hass):
    """Keep proxy device links working on HA versions before 2026.8."""
    parent = _make_device(hass, unique_id="aa:bb:cc:dd:ee:01")
    child = _make_device(hass, unique_id="aa:bb:cc:dd:ee:02")
    child._proxy_device = parent
    registry = SimpleNamespace(async_get_device=Mock())

    with patch(
        "custom_components.xiaomi_miot.core.device.dr.async_get",
        return_value=registry,
    ):
        device_info = child.hass_device_info

    assert device_info["via_device"] == next(iter(parent.identifiers))
    assert "via_device_id" not in device_info


@pytest.mark.parametrize('action', ['disable', 'enable', 'delete'])
async def test_registry_updates_use_supported_ownership_api(
    hass, device_registry, monkeypatch, caplog, action,
):
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, 'test-mac')},
    )
    if action == 'disable':
        device_registry.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    elif action == 'delete':
        device_registry.async_update_device(device.id, name_by_user='delete')
    await hass.async_block_till_done()
    miot_device = SimpleNamespace(
        identifiers=device.identifiers,
        coordinators=[] if action == 'enable' else [object()],
        async_unload=AsyncMock(),
        init_coordinators=AsyncMock(),
        log=Mock(),
    )
    monkeypatch.setattr(HassEntry, 'ALL', {
        config_entry.entry_id: SimpleNamespace(devices={'test': miot_device}),
    })
    await _handle_device_registry_event(hass)
    caplog.clear()

    hass.bus.async_fire(dr.EVENT_DEVICE_REGISTRY_UPDATED, {
        'action': 'update', 'device_id': device.id, 'changes': {},
    })
    await hass.async_block_till_done()

    if action == 'enable':
        miot_device.init_coordinators.assert_awaited_once_with()
        miot_device.async_unload.assert_not_awaited()
    else:
        miot_device.async_unload.assert_awaited_once_with()
        miot_device.init_coordinators.assert_not_awaited()
    assert (device_registry.async_get(device.id) is None) is (action == 'delete')
    assert not [record for record in caplog.records if 'deprecated' in record.message.lower()]
    assert not [record for record in caplog.records if record.levelno >= 40]


async def test_registry_update_ignores_removed_device(hass, device_registry):
    await _handle_device_registry_event(hass)
    hass.bus.async_fire(dr.EVENT_DEVICE_REGISTRY_UPDATED, {
        'action': 'remove', 'device_id': 'missing',
    })
    await hass.async_block_till_done()
