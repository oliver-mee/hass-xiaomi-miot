"""Request failures must not poison later requests or swallow task cancellation."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.xiaomi_miot.core.xiaomi_cloud import MiotCloud


@pytest.fixture
def cloud(hass):
    cloud = MiotCloud(hass, 'test', 'test', 'sg')
    cloud.service_token = 'test'
    cloud.async_request_rc4_api = AsyncMock()
    return cloud


async def test_timeout_then_success_resets_failure_count(cloud):
    cloud.async_request_rc4_api.side_effect = [TimeoutError(), '{"code":0,"result":[1]}']
    assert await cloud.async_request_api('test', {}) is None
    assert cloud.attrs['timeouts'] == 1
    assert await cloud.async_request_api('test', {}) == {'code': 0, 'result': [1]}
    assert cloud.attrs['timeouts'] == 0
    assert cloud.async_request_rc4_api.await_count == 2


async def test_explicit_timeout_propagates_without_replaying(cloud):
    cloud.async_request_rc4_api.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await cloud.async_request_api('test', {}, raise_timeout=True)
    assert cloud.async_request_rc4_api.await_count == 1


async def test_real_task_cancellation_propagates_and_next_request_works(cloud):
    started = asyncio.Event()
    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    cloud.async_request_rc4_api.side_effect = blocked
    task = asyncio.create_task(cloud.async_request_api('test', {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert cloud.attrs.get('timeouts', 0) == 0
    cloud.async_request_rc4_api.side_effect = None
    cloud.async_request_rc4_api.return_value = '{"code":0}'
    assert await cloud.async_request_api('test', {}) == {'code': 0}
