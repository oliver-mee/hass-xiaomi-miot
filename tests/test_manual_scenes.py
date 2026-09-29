from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.xiaomi_miot import DOMAIN
from custom_components.xiaomi_miot import button
from custom_components.xiaomi_miot.button import (
    ManualSceneButton,
    _manual_scene_button_names,
)
from custom_components.xiaomi_miot.core.xiaomi_cloud import MiCloudException, MiotCloud


async def test_scene_discovery_is_scoped_to_each_home_and_skips_failed_home():
    cloud = MiotCloud.__new__(MiotCloud)
    cloud.user_id = '1000'
    cloud.async_get_homerooms = AsyncMock(return_value=[
        {'id': '1', 'uid': '1000', 'name': 'Unavailable home'},
        {'id': '2', 'uid': '2000', 'name': 'Shared home'},
    ])
    cloud._async_request_manual_scene_api = AsyncMock(side_effect=[
        RuntimeError('temporary network failure'),
        [{'scene_id': 11, 'scene_name': 'Sleep', 'room_id': '3'}],
    ])

    scenes = await cloud.async_get_manual_scenes()

    assert scenes == [{
        'scene_id': '11',
        'scene_name': 'Sleep',
        'room_id': '3',
        'home_id': '2',
        'home_name': 'Shared home',
        'owner_uid': '2000',
    }]
    cloud._async_request_manual_scene_api.assert_has_awaits([
        call(
            'GetManualSceneList',
            {
                'home_id': '1',
                'owner_uid': '1000',
                'source': 'zkp',
                'get_type': 2,
            },
        ),
        call(
            'GetManualSceneList',
            {
                'home_id': '2',
                'owner_uid': '2000',
                'source': 'zkp',
                'get_type': 2,
            },
        ),
    ])


async def test_manual_scene_api_requires_success_response():
    cloud = MiotCloud.__new__(MiotCloud)
    cloud.is_token_expired = lambda _: False
    cloud.async_request_api = AsyncMock(return_value={'code': 0, 'result': []})

    assert await cloud._async_request_manual_scene_api('GetManualSceneList', {}) == []
    cloud.async_request_api.assert_awaited_once_with(
        'appgateway/miot/appsceneservice/AppSceneService/GetManualSceneList',
        {},
        debug=False,
        timeout=20,
    )

    cloud.async_request_api.return_value = {'code': 1, 'result': True}
    with pytest.raises(MiCloudException):
        await cloud._async_request_manual_scene_api('NewRunScene', {})


async def test_run_manual_scene_passes_owner_home_room_scope_once():
    cloud = MiotCloud.__new__(MiotCloud)
    cloud._async_request_manual_scene_api = AsyncMock(return_value=True)

    assert await cloud.async_run_manual_scene({
        'owner_uid': '2000',
        'scene_id': '11',
        'home_id': '2',
        'room_id': '3',
    })
    cloud._async_request_manual_scene_api.assert_awaited_once_with(
        'NewRunScene',
        {
            'owner_uid': '2000',
            'scene_id': '11',
            'scene_type': 2,
            'home_id': '2',
            'room_id': '3',
        },
    )


def test_manual_scene_ids_include_account_region_sid_and_home():
    scene = {
        'scene_id': '11',
        'scene_name': 'Sleep',
        'home_id': '2',
        'home_name': 'Home',
    }
    mainland = ManualSceneButton(
        SimpleNamespace(unique_id='1000-cn-xiaomiio'), scene, 'Home Sleep'
    )
    singapore = ManualSceneButton(
        SimpleNamespace(unique_id='1000-sg-xiaomiio'), scene, 'Home Sleep'
    )
    alternate_sid = ManualSceneButton(
        SimpleNamespace(unique_id='1000-sg-micoapi'), scene, 'Home Sleep'
    )
    second_home = ManualSceneButton(
        SimpleNamespace(unique_id='1000-sg-xiaomiio'),
        {**scene, 'home_id': '3'},
        'Other Sleep',
    )

    assert mainland._attr_unique_id == '1000-cn-xiaomiio-manual-scene-2-11'
    assert len({
        mainland._attr_unique_id,
        singapore._attr_unique_id,
        alternate_sid._attr_unique_id,
        second_home._attr_unique_id,
    }) == 4
    assert mainland.device_info['identifiers'] == {
        (DOMAIN, '1000-cn-xiaomiio-manual-scenes')
    }


def test_scene_names_disambiguate_same_names_across_homes():
    scenes = [
        {'scene_id': '11', 'scene_name': 'Sleep', 'home_id': '1', 'home_name': ''},
        {'scene_id': '22', 'scene_name': 'Sleep', 'home_id': '2', 'home_name': ''},
        {'scene_id': '33', 'scene_name': 'Work', 'home_id': '3', 'home_name': 'Home'},
        {'scene_id': '44', 'scene_name': 'Work', 'home_id': '4', 'home_name': 'Home'},
    ]

    assert _manual_scene_button_names(scenes) == [
        '1 Sleep',
        '2 Sleep',
        'Home Work 3-33',
        'Home Work 4-44',
    ]


async def test_button_setup_refreshes_without_running_scenes_until_pressed(
    hass, monkeypatch
):
    scene = {
        'owner_uid': '1000',
        'scene_id': '11',
        'scene_name': 'Sleep',
        'home_id': '2',
        'home_name': 'Home',
    }
    cloud = SimpleNamespace(
        unique_id='1000-sg-xiaomiio',
        async_get_manual_scenes=AsyncMock(return_value=[scene]),
        async_run_manual_scene=AsyncMock(return_value=True),
    )
    entry = SimpleNamespace(
        cloud=cloud,
        get_config=lambda key, default=None: '1000@example.com',
        new_adder=lambda domain, add_entities: None,
    )
    monkeypatch.setattr(
        button,
        'HassEntry',
        SimpleNamespace(init=lambda _hass, _config_entry: entry),
    )
    monkeypatch.setattr(button, 'async_setup_config_entry', AsyncMock())
    added = []

    await button.async_setup_entry(hass, object(), added.extend)
    await button.async_setup_entry(hass, object(), added.extend)

    assert cloud.async_get_manual_scenes.await_count == 2
    assert len(added) == 2
    cloud.async_run_manual_scene.assert_not_awaited()

    await added[0].async_press()
    cloud.async_run_manual_scene.assert_awaited_once_with(scene)


async def test_manual_scene_button_surfaces_execution_failure():
    cloud = SimpleNamespace(
        unique_id='1000-sg-xiaomiio',
        async_run_manual_scene=AsyncMock(return_value=False),
    )
    button_entity = ManualSceneButton(
        cloud,
        {
            'owner_uid': '1000',
            'scene_id': '11',
            'scene_name': 'Sleep',
            'home_id': '2',
            'home_name': 'Home',
        },
        'Home Sleep',
    )

    with pytest.raises(HomeAssistantError):
        await button_entity.async_press()
    cloud.async_run_manual_scene.assert_awaited_once_with(button_entity.scene)

@pytest.mark.parametrize('response', [True, 'bad', [1], {}, {'code': 0}])
async def test_manual_scene_api_rejects_malformed_response(response):
    cloud = MiotCloud.__new__(MiotCloud)
    cloud.is_token_expired = lambda _: False
    cloud.async_request_api = AsyncMock(return_value=response)
    with pytest.raises(MiCloudException):
        await cloud._async_request_manual_scene_api('GetManualSceneList', {})


async def test_duplicate_scenes_are_deduplicated_per_home():
    cloud = MiotCloud.__new__(MiotCloud)
    cloud.user_id = '1000'
    cloud.async_get_homerooms = AsyncMock(return_value=[{'id': '1'}, {'id': '2'}])
    cloud._async_request_manual_scene_api = AsyncMock(return_value=[
        {'scene_id': 1, 'scene_name': 'Sleep'}, {'scene_id': 1, 'scene_name': 'Sleep'},
    ])
    scenes = await cloud.async_get_manual_scenes()
    assert [(s['home_id'], s['scene_id']) for s in scenes] == [('1', '1'), ('2', '1')]


@pytest.mark.parametrize('result', [False, None, 'error', {'code': 1}, [1]])
async def test_scene_execution_does_not_treat_error_object_as_success(result):
    cloud = MiotCloud.__new__(MiotCloud)
    cloud._async_request_manual_scene_api = AsyncMock(return_value=result)
    assert not await cloud.async_run_manual_scene({'owner_uid': '1', 'scene_id': '2'})
