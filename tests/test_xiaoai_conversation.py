"""Regression coverage for malformed XiaoAI conversation responses."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import custom_components.xiaomi_miot.sensor as sensor_module
from custom_components.xiaomi_miot.sensor import XiaoaiConversationSensor


class FakeCloud:
    def __init__(self, response=None, side_effect=None):
        self.async_request_api = AsyncMock(
            return_value=response,
            side_effect=side_effect,
        )


class StubConversationSensor:
    fetch_latest_message = XiaoaiConversationSensor.fetch_latest_message

    @property
    def name_model(self):
        return "Test XiaoAI (test.model)"


def make_sensor(monkeypatch, response=None, side_effect=None):
    monkeypatch.setattr(sensor_module, "MiotCloud", FakeCloud)
    cloud = FakeCloud(response=response, side_effect=side_effect)
    entity = object.__new__(StubConversationSensor)
    entity._parent = SimpleNamespace(
        xiaoai_cloud=cloud,
        xiaoai_device={"deviceID": "device-id", "hardware": "test"},
    )
    entity._available = True
    entity.conversation = {"query": "last valid"}
    entity._state = "last valid"
    entity._attr_native_value = "last valid"
    entity._state_attrs = {
        "content": "last valid",
        "answers": [],
        "history": [],
        "timestamp": None,
    }
    return entity, cloud


def state_snapshot(entity):
    return {
        "conversation": copy.deepcopy(entity.conversation),
        "state": entity._state,
        "native_value": entity._attr_native_value,
        "state_attrs": copy.deepcopy(entity._state_attrs),
    }


@pytest.mark.asyncio
async def test_valid_response_updates_from_copies_and_strips_nested_bitsets(monkeypatch):
    response = {
        "data": {
            "records": [
                {
                    "query": "turn on the light",
                    "time": 1700000000000,
                    "answers": [
                        {
                            "type": "text",
                            "bitSet": 1,
                            "text": {"content": "Done", "bitSet": 2},
                        }
                    ],
                },
                {"query": "earlier"},
            ]
        }
    }
    original = copy.deepcopy(response)
    entity, _ = make_sensor(monkeypatch, response=response)

    result = await entity.fetch_latest_message()

    assert result["query"] == "turn on the light"
    assert entity._attr_native_value == "turn on the light"
    assert entity._state_attrs["history"] == ["earlier"]
    assert entity._state_attrs["answers"] == [
        {"type": "text", "text": {"content": "Done"}}
    ]
    assert response == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        42,
        {"data": None},
        {"data": []},
        {"data": 42},
        {"data": "not json"},
        {"data": "null"},
        {"data": "[]"},
        {"data": "42"},
        {"data": {"records": None}},
        {"data": {"records": 42}},
        {"data": {"records": [{"query": "new"}, "malformed"]}},
        {"data": {"records": [{"query": {}}]}},
        {"data": {"records": [{"query": []}]}},
        {"data": {"records": [{"query": 42}]}},
    ],
)
async def test_malformed_response_preserves_last_valid_state(monkeypatch, response):
    entity, _ = make_sensor(monkeypatch, response=response)
    before = state_snapshot(entity)

    assert await entity.fetch_latest_message() == {}
    assert state_snapshot(entity) == before


@pytest.mark.asyncio
async def test_empty_records_preserve_last_valid_state(monkeypatch):
    entity, _ = make_sensor(monkeypatch, response={"data": {"records": []}})
    before = state_snapshot(entity)

    assert await entity.fetch_latest_message() == {}
    assert state_snapshot(entity) == before


@pytest.mark.asyncio
async def test_valid_response_recovers_after_malformed_response(monkeypatch):
    valid = {"data": {"records": [{"query": "recovered", "answers": []}]}}
    entity, cloud = make_sensor(
        monkeypatch,
        side_effect=[{"data": {"records": ["bad"]}}, valid],
    )

    assert await entity.fetch_latest_message() == {}
    assert entity._attr_native_value == "last valid"
    result = await entity.fetch_latest_message()

    assert result["query"] == "recovered"
    assert entity._attr_native_value == "recovered"
    assert cloud.async_request_api.await_count == 2


@pytest.mark.asyncio
async def test_valid_response_recovers_after_malformed_query(monkeypatch):
    valid = {"data": {"records": [{"query": "recovered", "answers": []}]}}
    entity, cloud = make_sensor(
        monkeypatch,
        side_effect=[{"data": {"records": [{"query": {}}]}}, valid],
    )

    assert await entity.fetch_latest_message() == {}
    assert entity._attr_native_value == "last valid"
    result = await entity.fetch_latest_message()

    assert result["query"] == "recovered"
    assert entity._attr_native_value == "recovered"
    assert cloud.async_request_api.await_count == 2


@pytest.mark.asyncio
async def test_cancellation_propagates(monkeypatch):
    entity, _ = make_sensor(monkeypatch, side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await entity.fetch_latest_message()
