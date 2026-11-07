"""Live URL contracts using a published MIoT specification, no camera access.

Fixture source: https://miot-spec.org/miot-spec-v2/instance?type=urn:miot-spec-v2:device:camera:0000A01C:chuangmi-079ae2:1:0000D070
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.xiaomi_miot.camera import CameraEntity


@pytest.fixture
def cameras(hass, load_miot_spec):
    def make(alexa=False):
        active_spec = load_miot_spec('chuangmi.camera.079ae2.json')
        if alexa:
            active_spec.services.pop(28)
        entity = CameraEntity.__new__(CameraEntity)
        entity.hass = hass
        entity.device = SimpleNamespace(spec=active_spec, cloud=object())
        entity._attr_device_info = {'model': 'chuangmi.camera.079ae2'}
        entity._attr_supported_features = 0
        entity._attr_extra_state_attributes = {}
        entity.custom_config = lambda key: None
        entity.on_init()
        assert entity._live_srv.iid == (29 if alexa else 28)
        assert entity._live_start.iid == (2 if alexa else 3)
        def response(action, *args, **kwargs):
            return {'code': 0, 'out': (['https://example.com/live.m3u8', '', 300]
                                        if alexa else ['https://example.com/live.m3u8'])}
        entity.async_call_action = AsyncMock(side_effect=response)
        return entity
    return make


@pytest.mark.parametrize('expiration,expected', [
    (300, 1290), (1300, 2290), (1700000300, 1700000290),
    (1700000300000, 1700000290), (None, 1270), ('bad', 1270),
    (True, 1270), (float('inf'), 1270),
])
async def test_expiry_units(cameras, expiration, expected):
    entity = cameras(alexa=True)
    with patch('custom_components.xiaomi_miot.camera.time.time', return_value=1000):
        entity.async_call_action.return_value = None
        entity.async_call_action.side_effect = lambda *a, **k: {'code': 0, 'out': ['https://example.com/live.m3u8', '', expiration]}
        assert await entity.async_get_live_url() == 'https://example.com/live.m3u8'
    assert entity._live_expire == expected


@pytest.mark.parametrize('url', ['garbage', 'file:///etc/passwd', 'xiaomi://example.com',
                                 'https:///missing', 'https://[invalid',
                                 'https://example.com/" -i injected', 123, {}])
async def test_invalid_source_falls_back_without_caching(cameras, url):
    entity = cameras()
    entity._attr_stream_source = 'motion-clip'
    entity.async_call_action.side_effect = lambda *a, **k: {'code': 0, 'out': [url]}
    assert await entity.stream_source() == 'motion-clip'
    assert not entity._live_url


async def test_concurrent_requests_share_only_their_camera(cameras):
    first, second = cameras(), cameras()
    entered, release = asyncio.Event(), asyncio.Event()
    original = first.async_call_action.side_effect
    async def action(*args, **kwargs):
        if args[0] is first._live_start:
            entered.set()
            await release.wait()
        return original(*args, **kwargs)
    first.async_call_action.side_effect = action
    tasks = [asyncio.create_task(first.async_get_live_url()) for _ in range(4)]
    try:
        await entered.wait()
        assert await second.async_get_live_url()
        release.set()
        assert len(set(await asyncio.gather(*tasks))) == 1
        assert sum(call.args[0] is first._live_start for call in first.async_call_action.call_args_list) == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancelled_request_releases_lock(cameras):
    entity = cameras()
    original = entity.async_call_action.side_effect
    entered = asyncio.Event()
    async def action(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    entity.async_call_action.side_effect = action
    task = asyncio.create_task(entity.async_get_live_url())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    entity.async_call_action.side_effect = original
    assert await entity.async_get_live_url()


async def test_no_cloud_or_service_preserves_motion_source(cameras):
    entity = cameras()
    entity._attr_stream_source = 'motion-clip'
    entity.device.cloud = None
    assert await entity.stream_source() == 'motion-clip'
    entity.device.cloud = object()
    entity._live_start = None
    assert await entity.stream_source() == 'motion-clip'
    entity.async_call_action.assert_not_awaited()


@pytest.mark.parametrize('url', ['http://example.com/live', 'https://example.com/live',
                                 'rtsp://example.com/live', 'rtsps://example.com:443/live'])
async def test_supported_sources_are_cached_privately_then_refresh(cameras, url):
    entity = cameras()
    entity.async_call_action.side_effect = lambda *a, **k: {'code': 0, 'out': [url]}
    with patch('custom_components.xiaomi_miot.camera.time.time', return_value=1000):
        assert await entity.async_get_live_url() == url
        assert await entity.async_get_live_url() == url
        assert entity._live_expire == 1270  # HLS action returns only the address.
    assert entity.async_call_action.await_count == 2  # One stop/start pair.
    assert url not in str(entity.extra_state_attributes)
    with patch('custom_components.xiaomi_miot.camera.time.time', return_value=1271):
        assert await entity.async_get_live_url() == url
    assert entity.async_call_action.await_count == 4


@pytest.mark.parametrize('response', [None, [], {'code': -1, 'out': ['https://example.com/live']},
                                      {'code': 0, 'out': 'not-a-list'},
                                      {'code': False, 'out': ['https://example.com/live']}])
async def test_failed_or_malformed_reply_does_not_supply_a_source(cameras, response):
    entity = cameras()
    entity.async_call_action.side_effect = None
    entity.async_call_action.return_value = response
    assert await entity.async_get_live_url() is None
    assert entity._live_url is None


async def test_expired_absolute_reply_is_not_cached(cameras):
    entity = cameras(alexa=True)
    entity.async_call_action.side_effect = lambda *a, **k: {
        'code': 0, 'out': ['https://example.com/live', '', 1700000300],
    }
    with patch('custom_components.xiaomi_miot.camera.time.time', return_value=1700000400):
        assert await entity.async_get_live_url() is None
    assert entity._live_url is None
