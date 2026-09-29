"""Regression tests for regional MiHome message and scene sensors."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_miot import DOMAIN
from custom_components.xiaomi_miot import sensor as sensor_platform
from custom_components.xiaomi_miot.sensor import (
    MihomeMessageSensor,
    MihomeSceneHistorySensor,
)


def _cloud(*, uid="123", account="123-sg-xiaomiio"):
    return SimpleNamespace(
        user_id=uid,
        unique_id=account,
        async_get_homerooms=AsyncMock(return_value=[]),
    )


def test_regional_cloud_ids_do_not_share_sensor_account_slots(hass):
    sg = MihomeMessageSensor(hass, _cloud(account="123-sg-xiaomiio"))
    cn = MihomeMessageSensor(hass, _cloud(account="123-cn-micoapi"))

    assert sg.unique_id != cn.unique_id
    assert sg.entity_id != cn.entity_id

    hass.data[DOMAIN]["accounts"][sg.cloud.unique_id] = {"messenger": sg}
    hass.data[DOMAIN]["accounts"][cn.cloud.unique_id] = {"messenger": cn}
    assert set(hass.data[DOMAIN]["accounts"]) >= {
        "123-sg-xiaomiio",
        "123-cn-micoapi",
    }


def test_legacy_message_registry_entry_migrates_only_for_owner(hass):
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old_unique_id = f"{DOMAIN}-mihome-message-123"
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        old_unique_id,
        config_entry=config_entry,
        suggested_object_id="legacy_message",
    )
    old_entity_id = old.entity_id
    registry.async_update_entity(old_entity_id, name="User label")

    entity = MihomeMessageSensor(hass, _cloud(), config_entry.entry_id)

    migrated = registry.async_get(old_entity_id)
    assert entity.unique_id == f"{DOMAIN}-mihome-message-123-sg-xiaomiio"
    assert entity.entity_id == old_entity_id
    assert migrated.unique_id == entity.unique_id
    assert migrated.name == "User label"

    retried = MihomeMessageSensor(hass, _cloud(), config_entry.entry_id)
    assert retried.entity_id == old_entity_id

    other_entry = MockConfigEntry(domain=DOMAIN)
    other_entry.add_to_hass(hass)
    foreign = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}-mihome-message-456",
        config_entry=other_entry,
        suggested_object_id="foreign_message",
    )
    MihomeMessageSensor(
        hass,
        _cloud(uid="456", account="456-cn-xiaomiio"),
        config_entry.entry_id,
    )
    assert registry.async_get(foreign.entity_id).unique_id == f"{DOMAIN}-mihome-message-456"


async def test_scene_history_reserves_regional_account_when_messages_disabled(hass):
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={"disable_message": True},
    )
    config_entry.add_to_hass(hass)
    cloud = _cloud()
    cloud.async_get_homerooms.return_value = [{"id": 7, "uid": 123}]
    added = []
    seen_reservation = []

    def add_entities(entities, **kwargs):
        added.extend(entities)

    class FakeEntity:
        def __init__(self, hass_arg, cloud_arg, home_id, owner_uid, entry_id):
            self.coordinator = SimpleNamespace(
                async_config_entry_first_refresh=self._refresh,
            )

        async def _refresh(self):
            seen_reservation.append(
                hass.data[DOMAIN]["accounts"][cloud.unique_id]["scene_history_7"] is self
            )

    hass_entry = SimpleNamespace(
        get_cloud=AsyncMock(return_value=cloud),
        get_config=lambda key=None, default=None: {
            "disable_message": True,
        }.get(key, default),
        new_adder=lambda domain, callback: hass_entry,
    )
    with patch.object(sensor_platform.HassEntry, "init", return_value=hass_entry), \
         patch.object(sensor_platform, "MihomeSceneHistorySensor", FakeEntity), \
         patch.object(sensor_platform, "async_setup_config_entry", AsyncMock()):
        await sensor_platform.async_setup_entry(hass, config_entry, add_entities)

    assert seen_reservation == [True]
    assert len(added) == 1
    assert "messenger" not in hass.data[DOMAIN]["accounts"][cloud.unique_id]


async def test_same_account_concurrent_setup_uses_one_reservation(hass):
    cloud = _cloud()
    cloud.async_get_homerooms.return_value = []
    refresh_started = asyncio.Event()
    allow_refresh = asyncio.Event()
    created = []

    class BlockingEntity:
        def __init__(self, *args):
            self.coordinator = SimpleNamespace(async_config_entry_first_refresh=self._refresh)
            created.append(self)

        async def _refresh(self):
            refresh_started.set()
            await allow_refresh.wait()

    entries = {}
    config_entries = {}

    def get_entry(hass_arg, config_entry):
        return entries[config_entry.entry_id]

    for entry_id in ("one", "two"):
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id=entry_id,
            data={"disable_scene_history": True},
        )
        config_entry.add_to_hass(hass)
        config_entries[entry_id] = config_entry
        fake_entry = SimpleNamespace(
            get_cloud=AsyncMock(return_value=cloud),
            get_config=lambda key=None, default=None: {
                "disable_scene_history": True,
            }.get(key, default),
        )
        fake_entry.new_adder = lambda domain, callback, owner=fake_entry: owner
        entries[entry_id] = fake_entry

    def add_entities(*args, **kwargs):
        return None

    with patch.object(sensor_platform.HassEntry, "init", side_effect=get_entry), \
         patch.object(sensor_platform, "MihomeMessageSensor", BlockingEntity), \
         patch.object(sensor_platform, "async_setup_config_entry", AsyncMock()):
        first = asyncio.create_task(
            sensor_platform.async_setup_entry(hass, config_entries["one"], add_entities)
        )
        await refresh_started.wait()
        second = asyncio.create_task(
            sensor_platform.async_setup_entry(hass, config_entries["two"], add_entities)
        )
        await asyncio.sleep(0)
        assert len(created) == 1
        allow_refresh.set()
        await asyncio.gather(first, second)


async def test_regional_setups_keep_sg_and_cn_reservations_separate(hass):
    clouds = {
        "sg": _cloud(account="123-sg-xiaomiio"),
        "cn": _cloud(account="123-cn-xiaomiio"),
    }
    config_entries = {}
    hass_entries = {}
    for name, cloud in clouds.items():
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={"disable_scene_history": True},
        )
        config_entry.add_to_hass(hass)
        config_entries[name] = config_entry
        fake_entry = SimpleNamespace(
            get_cloud=AsyncMock(return_value=cloud),
            get_config=lambda key=None, default=None: {
                "disable_scene_history": True,
            }.get(key, default),
        )
        fake_entry.new_adder = lambda domain, callback, owner=fake_entry: owner
        hass_entries[config_entry.entry_id] = fake_entry

    class Entity:
        def __init__(self, *args):
            self.coordinator = SimpleNamespace(
                async_config_entry_first_refresh=AsyncMock(),
            )

    with patch.object(
        sensor_platform.HassEntry,
        "init",
        side_effect=lambda hass_arg, config_entry: hass_entries[config_entry.entry_id],
    ), patch.object(sensor_platform, "MihomeMessageSensor", Entity), \
         patch.object(sensor_platform, "async_setup_config_entry", AsyncMock()):
        await asyncio.gather(*(
            sensor_platform.async_setup_entry(
                hass, config_entry, lambda *args, **kwargs: None,
            )
            for config_entry in config_entries.values()
        ))

    assert hass.data[DOMAIN]["accounts"]["123-sg-xiaomiio"]["messenger"] is not \
        hass.data[DOMAIN]["accounts"]["123-cn-xiaomiio"]["messenger"]


async def test_cancellation_releases_message_reservation(hass):
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    cloud = _cloud()
    started = asyncio.Event()

    class BlockingEntity:
        def __init__(self, *args):
            self.coordinator = SimpleNamespace(async_config_entry_first_refresh=self._refresh)

        async def _refresh(self):
            started.set()
            await asyncio.Future()

    hass_entry = SimpleNamespace(
        get_cloud=AsyncMock(return_value=cloud),
        get_config=lambda key=None, default=None: default,
        new_adder=lambda domain, callback: hass_entry,
    )
    with patch.object(sensor_platform.HassEntry, "init", return_value=hass_entry), \
         patch.object(sensor_platform, "MihomeMessageSensor", BlockingEntity), \
         patch.object(sensor_platform, "async_setup_config_entry", AsyncMock()):
        task = asyncio.create_task(
            sensor_platform.async_setup_entry(hass, config_entry, lambda *args, **kwargs: None)
        )
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert "messenger" not in hass.data[DOMAIN]["accounts"][cloud.unique_id]


async def test_unload_cleanup_does_not_remove_replacement_reservation(hass):
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    callbacks = []
    config_entry.async_on_unload = callbacks.append
    cloud = _cloud()
    created = []

    class Entity:
        def __init__(self, *args):
            self.coordinator = SimpleNamespace(async_config_entry_first_refresh=AsyncMock())
            created.append(self)

    hass_entry = SimpleNamespace(
        get_cloud=AsyncMock(return_value=cloud),
        get_config=lambda key=None, default=None: default,
        new_adder=lambda domain, callback: hass_entry,
    )
    with patch.object(sensor_platform.HassEntry, "init", return_value=hass_entry), \
         patch.object(sensor_platform, "MihomeMessageSensor", Entity), \
         patch.object(sensor_platform, "async_setup_config_entry", AsyncMock()):
        await sensor_platform.async_setup_entry(hass, config_entry, lambda *args, **kwargs: None)

    replacement = object()
    account = hass.data[DOMAIN]["accounts"][cloud.unique_id]
    for callback in callbacks:
        callback()
    assert "messenger" not in account

    account["messenger"] = replacement
    for callback in callbacks:
        callback()
    assert account["messenger"] is replacement


def test_legacy_scene_history_registry_entry_migrates_with_metadata(hass):
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old_unique_id = f"{DOMAIN}-mihome-scene-history-123_7"
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        old_unique_id,
        config_entry=config_entry,
        suggested_object_id="legacy_scene_history",
    )
    registry.async_update_entity(old.entity_id, name="Scene label")

    entity = MihomeSceneHistorySensor(
        hass, _cloud(), 7, 123, config_entry.entry_id,
    )
    migrated = registry.async_get(old.entity_id)
    assert entity.entity_id == old.entity_id
    assert migrated.unique_id == f"{DOMAIN}-mihome-scene-history-123-sg-xiaomiio_7"
    assert migrated.name == "Scene label"


def test_registry_target_owned_by_another_entry_is_not_claimed(hass):
    owner = MockConfigEntry(domain=DOMAIN)
    owner.add_to_hass(hass)
    other = MockConfigEntry(domain=DOMAIN)
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}-mihome-message-123",
        config_entry=owner,
        suggested_object_id="legacy_message",
    )
    target = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}-mihome-message-123-sg-xiaomiio",
        config_entry=other,
        suggested_object_id="other_message",
    )

    with pytest.raises(HomeAssistantError):
        MihomeMessageSensor(hass, _cloud(), owner.entry_id)

    assert registry.async_get(old.entity_id).unique_id == f"{DOMAIN}-mihome-message-123"
    assert registry.async_get(target.entity_id).config_entry_id == other.entry_id


async def test_message_reservation_is_released_when_first_refresh_fails(hass):
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    cloud = _cloud()

    class FailingEntity:
        def __init__(self, *args):
            self.coordinator = SimpleNamespace(
                async_config_entry_first_refresh=AsyncMock(side_effect=RuntimeError("boom")),
            )

    hass_entry = SimpleNamespace(
        get_cloud=AsyncMock(return_value=cloud),
        get_config=lambda key=None, default=None: default,
        new_adder=lambda domain, callback: hass_entry,
    )
    with patch.object(sensor_platform.HassEntry, "init", return_value=hass_entry), \
         patch.object(sensor_platform, "MihomeMessageSensor", FailingEntity), \
         patch.object(sensor_platform, "async_setup_config_entry", AsyncMock()):
        try:
            await sensor_platform.async_setup_entry(hass, config_entry, lambda entities, **kwargs: None)
        except RuntimeError:
            pass

    assert "messenger" not in hass.data[DOMAIN]["accounts"][cloud.unique_id]


async def test_add_entities_failure_releases_message_reservation(hass):
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    cloud = _cloud()

    class Entity:
        def __init__(self, *args):
            self.coordinator = SimpleNamespace(
                async_config_entry_first_refresh=AsyncMock(),
            )

    hass_entry = SimpleNamespace(
        get_cloud=AsyncMock(return_value=cloud),
        get_config=lambda key=None, default=None: default,
        new_adder=lambda domain, callback: hass_entry,
    )

    def fail_add(*args, **kwargs):
        raise RuntimeError("add failed")

    with patch.object(sensor_platform.HassEntry, "init", return_value=hass_entry), \
         patch.object(sensor_platform, "MihomeMessageSensor", Entity), \
         patch.object(sensor_platform, "async_setup_config_entry", AsyncMock()):
        with pytest.raises(RuntimeError, match="add failed"):
            await sensor_platform.async_setup_entry(hass, config_entry, fail_add)

    assert "messenger" not in hass.data[DOMAIN]["accounts"][cloud.unique_id]
