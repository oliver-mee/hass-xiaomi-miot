import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.xiaomi_miot.camera import CameraEntity, CameraEntityFeature
from custom_components.xiaomi_miot.core.converters import BaseConv
from custom_components.xiaomi_miot.core.xiaomi_cloud import MiCloudException


@pytest.fixture(autouse=True)
def freeze_camera_clock(monkeypatch):
    monkeypatch.setattr(
        'custom_components.xiaomi_miot.camera.time',
        SimpleNamespace(time=lambda: 1_700_000_000),
    )


def make_c701_camera(make_device, load_miot_spec, *, customizes=None):
    device = make_device(
        load_miot_spec('chuangmi.camera.079ae2.json'),
        model='chuangmi.camera.079ae2',
        customizes=customizes,
    )
    device.cloud = SimpleNamespace(async_request_miot_spec=AsyncMock())
    entity = CameraEntity(device, BaseConv(attr='camera_control', domain='camera'))
    return device, entity


def test_c701_uses_live_hls_action_and_respects_rtsp_setting(make_device, load_miot_spec):
    _, camera = make_c701_camera(make_device, load_miot_spec)
    assert camera._live_service.name == 'camera_stream_for_google_home'
    assert camera._live_start_action.name == 'start_hls_stream'
    assert camera.supported_features & CameraEntityFeature.STREAM

    _, rtsp_camera = make_c701_camera(
        make_device,
        load_miot_spec,
        customizes={'use_rtsp_stream': True},
    )
    assert rtsp_camera._live_service.name == 'camera_stream_for_amazon_alexa'
    assert rtsp_camera._live_start_action.name == 'start_rtsp_stream'


async def test_live_url_is_private_cached_until_expiry_then_refreshed(
    make_device, load_miot_spec, monkeypatch, caplog
):
    clock = [1_700_000_000]
    monkeypatch.setattr(
        'custom_components.xiaomi_miot.camera.time',
        SimpleNamespace(time=lambda: clock[0]),
    )
    _, camera = make_c701_camera(make_device, load_miot_spec)
    camera.device.cloud.async_request_miot_spec.side_effect = [
        {'code': 0},
        {'code': 0, 'out': ['https://stream.example/live-one.m3u8?token=secret-one', (clock[0] + 60) * 1000]},
        {'code': 0},
        {'code': 0, 'out': ['https://stream.example/live-two.m3u8?token=secret-two', (clock[0] + 111) * 1000]},
    ]
    caplog.set_level(logging.DEBUG)

    first_url = await camera.stream_source()
    assert first_url.endswith('token=secret-one')
    camera.device.cloud.async_request_miot_spec.assert_awaited_with(
        'action',
        {
            'did': 'test-device',
            'siid': 10,
            'aiid': 1,
            'in': [{'piid': 3, 'value': 1}],
        },
        debug=False,
    )
    assert await camera.stream_source() == first_url
    assert camera.device.cloud.async_request_miot_spec.await_count == 2

    clock[0] += 51
    second_url = await camera.stream_source()
    assert second_url.endswith('token=secret-two')
    assert camera.device.cloud.async_request_miot_spec.await_count == 4
    assert 'secret-one' not in caplog.text
    assert 'secret-two' not in caplog.text
    assert 'stream_address' not in camera.extra_state_attributes


async def test_concurrent_live_requests_share_one_start_action(make_device, load_miot_spec):
    _, camera = make_c701_camera(make_device, load_miot_spec)
    started = asyncio.Event()
    release = asyncio.Event()
    start_calls = 0

    async def request(api, params, **kwargs):
        nonlocal start_calls
        action_id = params['aiid']
        assert kwargs == {'debug': False}
        if action_id == 2:
            return {'code': 0}
        start_calls += 1
        started.set()
        await release.wait()
        return {'code': 0, 'out': ['https://stream.example/concurrent.m3u8', 1_800_000_000_000]}

    camera.device.cloud.async_request_miot_spec.side_effect = request
    first = asyncio.create_task(camera.stream_source())
    await started.wait()
    second = asyncio.create_task(camera.stream_source())
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second) == [
        'https://stream.example/concurrent.m3u8',
        'https://stream.example/concurrent.m3u8',
    ]
    assert start_calls == 1


async def test_cancellation_releases_live_stream_lock_for_retry(make_device, load_miot_spec):
    _, camera = make_c701_camera(make_device, load_miot_spec)
    started = asyncio.Event()
    start_calls = 0

    async def request(api, params, **kwargs):
        nonlocal start_calls
        if params['aiid'] == 2:
            return {'code': 0}
        start_calls += 1
        if start_calls == 1:
            started.set()
            await asyncio.Event().wait()
        return {'code': 0, 'out': ['https://stream.example/retried.m3u8', 1_800_000_000_000]}

    camera.device.cloud.async_request_miot_spec.side_effect = request
    pending = asyncio.create_task(camera.stream_source())
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert pending.cancelled()
    assert await camera.stream_source() == 'https://stream.example/retried.m3u8'
    assert start_calls == 2


async def test_live_failure_does_not_fall_back_to_motion_clip_and_can_retry(
    make_device, load_miot_spec, caplog
):
    _, camera = make_c701_camera(make_device, load_miot_spec)
    camera._attr_stream_source = 'https://motion.example/clip.m3u8?token=motion-secret'
    camera.device.cloud.async_request_miot_spec.side_effect = [
        {'code': 0},
        MiCloudException('request failed'),
        {'code': 0},
        {'code': 0, 'out': ['https://stream.example/recovered.m3u8?token=live-secret', 1_800_000_000_000]},
    ]
    caplog.set_level(logging.DEBUG)

    assert await camera.stream_source() is None
    assert await camera.stream_source() == 'https://stream.example/recovered.m3u8?token=live-secret'
    assert camera.device.cloud.async_request_miot_spec.await_count == 4
    assert 'live-secret' not in camera.extra_state_attributes.values()
    assert 'live-secret' not in caplog.text

@pytest.mark.parametrize('output', [None, 12, 'bad', {}, [], ['url'], [None, 1]])
async def test_malformed_live_output_is_recoverable(make_device, load_miot_spec, output):
    _, camera = make_c701_camera(make_device, load_miot_spec)
    camera.device.cloud.async_request_miot_spec.side_effect = [
        {'code': 0}, {'code': 0, 'out': output},
    ]
    assert await camera.stream_source() is None


@pytest.mark.parametrize('expiry', [0, 1_699_999_999_000, 'bad', float('inf'), float('nan')])
async def test_expired_or_invalid_expiry_is_never_cached(make_device, load_miot_spec, expiry):
    _, camera = make_c701_camera(make_device, load_miot_spec)
    camera.device.cloud.async_request_miot_spec.side_effect = [
        {'code': 0}, {'code': 0, 'out': ['https://stream.example/live', expiry]},
    ]
    assert await camera.stream_source() is None
    assert camera._live_url is None


async def test_camera_caches_are_isolated(make_device, load_miot_spec):
    cameras = [make_c701_camera(make_device, load_miot_spec)[1] for _ in range(2)]
    for index, camera in enumerate(cameras):
        camera.device.cloud.async_request_miot_spec.side_effect = [
            {'code': 0}, {'code': 0, 'out': [f'https://stream.example/{index}', None]},
        ]
    assert await asyncio.gather(*(c.stream_source() for c in cameras)) == [
        'https://stream.example/0', 'https://stream.example/1',
    ]
