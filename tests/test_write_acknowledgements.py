"""Write acknowledgements must not weaken MIoT read validation."""
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.xiaomi_miot.core.miot_spec import MiotResults

PARAM = {'did': 'test-device', 'siid': 2, 'piid': 1, 'value': True}


@pytest.fixture
def device(make_device, load_miot_spec):
    return make_device(load_miot_spec('test.generic.fallback.json'))


@pytest.mark.parametrize('reply', [
    [dict(PARAM)],
    [{k: v for k, v in PARAM.items() if k != 'did'}],
    [{'siid': 2, 'piid': 1, 'code': 0}],
    [{'siid': 2, 'piid': 1, 'code': 1}],
])
@pytest.mark.asyncio
async def test_matching_ack_updates_converter_and_legacy(device, reply):
    original = deepcopy(reply)
    device.async_set_properties = AsyncMock(return_value=reply)
    device.encode = Mock(return_value={'method': 'set_properties', 'params': [PARAM]})
    device.dispatch = Mock()
    assert await device.async_write({'printer.on': True}) == reply
    device.dispatch.assert_called_once_with({'printer.on': True})
    device.dispatch.reset_mock()
    result = await device.async_set_miot_property(2, 1, True)
    assert result.is_success
    device.dispatch.assert_called_once()
    assert reply == original


@pytest.mark.parametrize('reply', [
    [], [{}], None, {'result': [PARAM]}, ['unexpected'],
    [dict(PARAM, code=None)], [dict(PARAM, code=-4005)],
    [dict(PARAM, error='device offline')],
    [dict(PARAM, code=0, error='device offline')],
    [dict(PARAM, did='another-device')], [dict(PARAM, siid=3)],
    [dict(PARAM, piid=3)], [dict(PARAM, value=False)],
    [{'siid': 2, 'piid': 1}], [{'value': True}],
])
@pytest.mark.asyncio
async def test_invalid_ack_never_dispatches_success(device, reply):
    device.async_set_properties = AsyncMock(return_value=reply)
    device.encode = Mock(return_value={'method': 'set_properties', 'params': [PARAM]})
    device.dispatch = Mock()
    await device.async_write({'printer.on': True})
    device.dispatch.assert_not_called()
    result = await device.async_set_miot_property(2, 1, True)
    assert result is None or not result.is_success
    device.dispatch.assert_not_called()


@pytest.mark.parametrize('reply', [['ok'], [None]])
@pytest.mark.asyncio
async def test_legacy_miio_converter_ack_is_preserved(device, reply):
    device.async_set_properties = AsyncMock(return_value=reply)
    device.encode = Mock(return_value={'method': 'set_properties', 'params': [PARAM]})
    device.dispatch = Mock()
    await device.async_write({'printer.on': True})
    device.dispatch.assert_called_once_with({'printer.on': True})


def test_mixed_batch_checks_every_reply_without_requiring_one_reply_per_request():
    second = dict(PARAM, piid=2, value=5)
    assert not MiotResults.from_write([PARAM], [PARAM, second]).has_error
    assert not MiotResults.from_write([PARAM, dict(second, code=0)], [PARAM, second]).has_error
    assert MiotResults.from_write([PARAM, dict(second, value=6)], [PARAM, second]).has_error
    assert MiotResults([PARAM]).has_error  # Code-less reads still fail.
