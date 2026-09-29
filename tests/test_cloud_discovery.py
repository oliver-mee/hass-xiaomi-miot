"""Discovery keeps complete cached data on transient and malformed responses."""
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.xiaomi_miot.core.xiaomi_cloud import MiotCloud, MiCloudException


def cloud(hass):
    result = MiotCloud(hass, 'user', 'password', 'sg')
    result.user_id = '100'
    return result


async def test_shared_home_owner_and_unassigned_room_metadata(hass):
    client = cloud(hass)
    client.get_device_list = AsyncMock(return_value=[])
    client.async_request_api = AsyncMock(return_value={
        'code': 0, 'result': {'device_info': [{'did': 'shared', 'model': 'test'}]},
    })
    devices = await client.get_all_devices([{'id': '22', 'uid': '200', 'name': 'Shared'}])
    assert devices[0]['home_id'] == '22'
    assert devices[0]['home_name'] == 'Shared'
    assert client.async_request_api.call_args.args[1]['home_owner'] == 200


async def test_repeated_pagination_cursor_is_bounded(hass):
    client = cloud(hass)
    client.get_device_list = AsyncMock(return_value=[])
    client.async_request_api = AsyncMock(return_value={
        'result': {'device_info': [], 'has_more': True, 'max_did': 'again'},
    })
    with pytest.raises(MiCloudException, match='pagination'):
        await client.get_all_devices([{'id': 22, 'uid': 200}])
    assert client.async_request_api.await_count == 2


@pytest.mark.parametrize('response', [None, [], {'result': None}, {'code': 1, 'result': {}}, {'result': {'list': [None]}}])
async def test_malformed_device_list_is_not_empty_success(hass, response):
    client = cloud(hass)
    client.async_request_api = AsyncMock(return_value=response)
    with pytest.raises(MiCloudException):
        await client.get_device_list()


@pytest.mark.parametrize('failure', ['empty', 'error', 'cancel'])
async def test_cached_devices_and_homes_survive_failed_refresh(hass, failure):
    import asyncio
    client = cloud(hass)
    cached = {'devices': [{'did': 'old'}], 'homes': [{'id': '1'}], 'update_time': 1}
    store = AsyncMock()
    store.async_load.return_value = cached
    client.get_home_devices = AsyncMock(return_value={'homelist': [], 'devices': {}})
    client.get_all_devices = AsyncMock(return_value=[])
    if failure == 'error':
        client.get_all_devices.side_effect = MiCloudException('malformed')
    if failure == 'cancel':
        client.get_all_devices.side_effect = asyncio.CancelledError
    with patch('custom_components.xiaomi_miot.core.xiaomi_cloud.Store', return_value=store):
        if failure == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await client.async_get_devices(renew=True)
        else:
            assert await client.async_get_devices(renew=True, return_all=True) == cached
    store.async_save.assert_not_awaited()


async def test_home_filter_normalizes_id_types(hass):
    client = cloud(hass)
    client.async_get_devices = AsyncMock(return_value=[{'did': 'd1', 'home_id': '22', 'model': 'test'}])
    result = await client.async_get_devices_by_key('did', filters={'filter_home_id': 'include', 'home_id_list': [22]})
    assert list(result) == ['d1']


async def test_region_cache_keys_are_distinct(hass):
    store = AsyncMock()
    store.async_load.return_value = {'update_time': 9_999_999_999, 'devices': []}
    keys = []
    def make_store(hass, version, key):
        keys.append(key)
        return store
    with patch('custom_components.xiaomi_miot.core.xiaomi_cloud.Store', side_effect=make_store):
        for region in ('cn', 'sg'):
            client = MiotCloud(hass, 'user', 'password', region)
            client.user_id = '100'
            assert await client.async_get_devices() == []
    assert keys == ['xiaomi_miot/devices-100-cn.json', 'xiaomi_miot/devices-100-sg.json']
