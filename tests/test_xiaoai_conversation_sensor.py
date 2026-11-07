from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.xiaomi_miot import sensor as sensor_module
from custom_components.xiaomi_miot.sensor import XiaoaiConversationSensor


@pytest.mark.parametrize('response_data', [None, []])
async def test_invalid_response_data_keeps_last_conversation(monkeypatch, response_data):
    class FakeMiotCloud:
        async_request_api = AsyncMock(return_value={'data': response_data})

    monkeypatch.setattr(sensor_module, 'MiotCloud', FakeMiotCloud)
    previous = {
        'content': 'turn off the computer',
        'answers': [],
        'history': [],
        'timestamp': None,
    }
    sensor = XiaoaiConversationSensor.__new__(XiaoaiConversationSensor)
    sensor._parent = SimpleNamespace(
        xiaoai_cloud=FakeMiotCloud(),
        xiaoai_device={'deviceID': 'test-device', 'hardware': 'test-hardware'},
        device_name='Test Speaker',
    )
    sensor._model = 'test.speaker'
    sensor._available = True
    sensor._attr_native_value = previous['content']
    sensor._state_attrs = previous.copy()
    sensor.conversation = {'query': previous['content']}

    result = await sensor.fetch_latest_message()

    assert result == {}
    assert sensor._attr_native_value == previous['content']
    assert sensor._state_attrs == previous
    assert sensor.conversation == {'query': previous['content']}


@pytest.mark.parametrize("response", [
    None, [], [1], "invalid", {"data": None}, {"data": []}, {"data": "null"}, {"data": "[]"}, {"data": "{"}, {"data": b"\xff"},
    {"data": {"records": "invalid"}}, {"data": {"records": None}}, {"data": {"records": {}}},
    {"data": {"records": [None]}},
    {"data": {"records": [{"query": "new", "answers": None}]}},
    {"data": {"records": [{"query": "new", "answers": [{"type": None}]}]}},
    {"data": {"records": [{"query": "new", "answers": [{"type": "tts", "tts": None}]}]}},
    {"data": {"records": [{"query": "new", "time": "invalid"}]}},
    {"data": {"records": [{"query": "new", "time": float("nan")}]}},
    {"data": {"records": [{"query": 123}]}},
    {"data": {"records": [{"query": "new", "time": float("inf")}]}},
    {"data": {"records": [{"query": "new", "time": 10**100}]}},
    {"data": {"records": [{"query": "new"}, None]}},
])
async def test_malformed_poll_preserves_state_and_recovers(monkeypatch, response):
    from copy import deepcopy
    class FakeMiotCloud:
        async_request_api = AsyncMock()
    monkeypatch.setattr(sensor_module, "MiotCloud", FakeMiotCloud)
    sensor = XiaoaiConversationSensor.__new__(XiaoaiConversationSensor)
    cloud = FakeMiotCloud()
    sensor._parent = SimpleNamespace(xiaoai_cloud=cloud,
        xiaoai_device={"deviceID": "test-device"}, device_name="Test Speaker")
    sensor._model = "test.speaker"
    sensor._available = True
    sensor._state = sensor._attr_native_value = "previous"
    sensor._state_attrs = {"content": "previous", "answers": [], "history": [], "timestamp": None}
    sensor.conversation = {"query": "previous"}
    previous = deepcopy(sensor._state_attrs)
    cloud.async_request_api.return_value = response
    assert await sensor.fetch_latest_message() == {}
    assert sensor._attr_native_value == sensor._state == "previous"
    assert sensor._state_attrs == previous
    assert sensor.conversation == {"query": "previous"}
    cloud.async_request_api.return_value = {"data": {"records": [
        {"query": "recovered", "time": 1700000000000, "answers": [{"type": "tts", "tts": {"text": "yes"}}]},
        {"query": "older"}]}}
    result = await sensor.fetch_latest_message()
    assert result["query"] == sensor._attr_native_value == "recovered"
    assert sensor._state_attrs["history"] == ["older"]
    assert sensor._state_attrs["timestamp"] is not None


async def test_poll_cancellation_is_owned_by_caller(monkeypatch):
    import asyncio
    class FakeMiotCloud:
        async_request_api = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(sensor_module, "MiotCloud", FakeMiotCloud)
    sensor = XiaoaiConversationSensor.__new__(XiaoaiConversationSensor)
    sensor._parent = SimpleNamespace(xiaoai_cloud=FakeMiotCloud(),
        xiaoai_device={"deviceID": "test-device"})
    sensor.conversation = {"query": "previous"}
    with pytest.raises(asyncio.CancelledError):
        await sensor.fetch_latest_message()
    assert sensor.conversation == {"query": "previous"}
