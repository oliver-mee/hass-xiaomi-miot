from custom_components.xiaomi_miot.core.converters import MiotEventConv


def event_converters(device):
    return {
        converter.attr: converter
        for converter in device.converters
        if isinstance(converter, MiotEventConv)
    }


def make(make_device, load_miot_spec, **customizes):
    return make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback",
        customizes=customizes or {},
    )


def test_spec_parses_events(make_device, load_miot_spec):
    """Events were previously discarded at parse time — nothing consumed them."""
    device = make(make_device, load_miot_spec)
    service = device.spec.services[2]

    assert sorted(e.name for e in service.events.values()) == ["paper_jammed", "print_finished"]

    jammed = service.get_event("paper_jammed")
    assert jammed.unique_prop == "event.2.2"
    assert jammed.full_name == "printer.paper_jammed"
    assert [p.name for p in jammed.argument_properties()] == ["brightness", "mode"]


def test_no_event_entities_without_opt_in(make_device, load_miot_spec):
    device = make(make_device, load_miot_spec)

    assert event_converters(device) == {}


def test_event_entities_created_when_enabled(make_device, load_miot_spec):
    device = make(make_device, load_miot_spec, event_entities=True)
    converters = event_converters(device)

    assert set(converters) == {"printer.print_finished", "printer.paper_jammed"}
    assert all(c.domain == "event" for c in converters.values())
    assert converters["printer.paper_jammed"].mi == "event.2.2"


def test_event_arguments_are_named_by_the_spec(make_device, load_miot_spec):
    """A device sends arguments positionally; the spec says what they mean."""
    device = make(make_device, load_miot_spec, event_entities=True)
    conv = event_converters(device)["printer.paper_jammed"]

    payload = {}
    conv.decode(device, payload, [7, 1])

    assert payload["event.printer.paper_jammed"] == {"brightness": 7, "mode": 1}


def test_event_arguments_accept_piid_value_dicts(make_device, load_miot_spec):
    device = make(make_device, load_miot_spec, event_entities=True)
    conv = event_converters(device)["printer.paper_jammed"]

    payload = {}
    conv.decode(device, payload, [{"piid": 3, "value": 7}, {"piid": 2, "value": 1}])

    assert payload["event.printer.paper_jammed"] == {"brightness": 7, "mode": 1}


def test_event_with_no_arguments_passes_value_through(make_device, load_miot_spec):
    device = make(make_device, load_miot_spec, event_entities=True)
    conv = event_converters(device)["printer.print_finished"]

    payload = {}
    conv.decode(device, payload, [])

    assert payload["event.printer.print_finished"] == []


def test_events_are_inbound_only(make_device, load_miot_spec):
    """Nothing may be written back to the device for an event."""
    device = make(make_device, load_miot_spec, event_entities=True)
    conv = event_converters(device)["printer.paper_jammed"]

    payload = {}
    conv.encode(device, payload, "anything")

    assert payload == {}


def test_device_decode_routes_eiid_to_the_event_converter(make_device, load_miot_spec):
    device = make(make_device, load_miot_spec, event_entities=True)

    payload = device.decode({"siid": 2, "eiid": 2, "arguments": [7, 1]})

    assert payload == {"event.printer.paper_jammed": {"brightness": 7, "mode": 1}}


def test_device_decode_still_routes_piid(make_device, load_miot_spec):
    """The eiid branch must not shadow ordinary property decoding."""
    device = make(make_device, load_miot_spec, generic_entities=True)

    payload = device.decode({"siid": 2, "piid": 3, "value": 5})

    assert payload == {"number.printer.brightness": 5}


def test_event_entities_respect_excluded_services(make_device, load_miot_spec):
    device = make(make_device, load_miot_spec, event_entities=True)
    device._exclude_miot_services = ["printer"]
    device.converters.clear()
    device.init_converters()

    assert event_converters(device) == {}


def make_entry_with(device):
    """Minimal HassEntry wired to one device, for the dispatch path."""
    from custom_components.xiaomi_miot.core.hass_entry import HassEntry

    entry = HassEntry.__new__(HassEntry)
    entry.devices = {"unique": device}
    entry.did_to_unique = {"test-device": "unique"}
    return entry


def test_dispatch_routes_a_cloud_message_to_the_event_entity(make_device, load_miot_spec):
    device = make(make_device, load_miot_spec, event_entities=True)
    entry = make_entry_with(device)
    seen = []
    device.add_listener(lambda data, only_info=False: seen.append(data))

    handled = entry.dispatch_device_event({
        "did": "test-device",
        "params": {"body": {"event": "paper_jammed", "arguments": [7, 1]}},
    })

    assert handled is True
    assert {"event.printer.paper_jammed": {"brightness": 7, "mode": 1}} in seen


def test_dispatch_prefers_eiid_over_the_event_name(make_device, load_miot_spec):
    """Names can collide across services; the eiid cannot."""
    device = make(make_device, load_miot_spec, event_entities=True)
    entry = make_entry_with(device)
    seen = []
    device.add_listener(lambda data, only_info=False: seen.append(data))

    entry.dispatch_device_event({
        "did": "test-device",
        "params": {"body": {"event": "wrong_name", "eiid": 1, "siid": 2, "arguments": []}},
    })

    assert {"event.printer.print_finished": []} in seen


def test_dispatch_ignores_messages_with_no_event(make_device, load_miot_spec):
    device = make(make_device, load_miot_spec, event_entities=True)
    entry = make_entry_with(device)

    assert entry.dispatch_device_event({"did": "test-device", "params": {"body": {}}}) is False


def test_dispatch_ignores_unknown_devices(make_device, load_miot_spec):
    device = make(make_device, load_miot_spec, event_entities=True)
    entry = make_entry_with(device)

    assert entry.dispatch_device_event({
        "did": "someone-elses-device",
        "params": {"body": {"event": "paper_jammed"}},
    }) is False


def test_dispatch_ignores_events_with_no_entity(make_device, load_miot_spec):
    """Opted out of event entities — a matching message must stay inert."""
    device = make(make_device, load_miot_spec)
    entry = make_entry_with(device)

    assert entry.dispatch_device_event({
        "did": "test-device",
        "params": {"body": {"event": "paper_jammed"}},
    }) is False


REAL_CAMERA_MESSAGE = {
    "did": "test-device",
    "params": {
        "body": {
            "event": "smart_camera_motion",
            "extra": {
                "extraInfo": '{"ver":"1.0.0","alarmStart":true,"eventType":"PeopleMotion","channel":"0"}',
                "isAlarm": True,
            },
        },
    },
}


def test_message_event_names_reads_the_nested_extra_info():
    from custom_components.xiaomi_miot.core.utils import message_event_names

    names = message_event_names(REAL_CAMERA_MESSAGE["params"]["body"])

    # The specific type must outrank the generic notification name.
    assert names.index("PeopleMotion") < names.index("smart_camera_motion")
    assert "people_motion" in names


def test_message_event_names_applies_aliases_first():
    from custom_components.xiaomi_miot.core.utils import message_event_names

    names = message_event_names(
        REAL_CAMERA_MESSAGE["params"]["body"],
        {"PeopleMotion": "someone_appeared"},
    )

    assert names[0] == "someone_appeared"


def test_message_event_names_survives_unparsable_extra_info():
    from custom_components.xiaomi_miot.core.utils import message_event_names

    names = message_event_names({"event": "smart_camera_motion", "extra": {"extraInfo": "not json"}})

    assert names == ["smart_camera_motion"]


def test_message_event_names_handles_an_empty_body():
    from custom_components.xiaomi_miot.core.utils import message_event_names

    assert message_event_names({}) == []


def test_dispatch_translates_a_real_camera_message(make_device, load_miot_spec):
    """The shape actually observed on a C701: notification name, nested type."""
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback",
        customizes={
            "event_entities": True,
            "cloud_events": {"PeopleMotion": "paper_jammed"},
        },
    )
    entry = make_entry_with(device)
    seen = []
    device.add_listener(lambda data, only_info=False: seen.append(data))

    assert entry.dispatch_device_event(REAL_CAMERA_MESSAGE) is True
    assert any("event.printer.paper_jammed" in d for d in seen)


def test_camera_alias_map_ships_for_every_camera():
    """A user should not have to hand-write the mapping for a common device."""
    from custom_components.xiaomi_miot.core.device_customizes import DEVICE_CUSTOMIZES

    aliases = DEVICE_CUSTOMIZES["*.camera.*"]["cloud_events"]

    # Both confirmed against a live chuangmi.camera.079ae2. Note the naming is
    # not systematic — 'PeopleMotion' but plain 'Pet' — so these cannot be
    # derived, only observed.
    assert aliases["PeopleMotion"] == "someone_appeared"
    assert aliases["Pet"] == "pet_appeared"


def test_dispatch_routes_without_a_cloud_session(make_device, load_miot_spec):
    """Regression: routing must not depend on which entry owns the cloud.

    `HassEntry.get_cloud()` did not always pass `hass_entry=self` to
    `MiotCloud.from_token`, so `cloud.hass_entry` could be None and every event
    was silently dropped. Ownership of the *device* is what decides routing.
    """
    from custom_components.xiaomi_miot.core.hass_entry import HassEntry

    device = make(make_device, load_miot_spec, event_entities=True)
    entry = make_entry_with(device)
    seen = []
    device.add_listener(lambda data, only_info=False: seen.append(data))

    original = dict(HassEntry.ALL)
    HassEntry.ALL.clear()
    HassEntry.ALL['test-entry'] = entry
    try:
        handled = HassEntry.dispatch_event_to_devices(REAL_CAMERA_MESSAGE)
    finally:
        HassEntry.ALL.clear()
        HassEntry.ALL.update(original)

    assert handled is False, 'no alias configured, so nothing should match'
    assert seen == []


def test_dispatch_to_devices_finds_the_owning_entry(make_device, load_miot_spec):
    from custom_components.xiaomi_miot.core.hass_entry import HassEntry

    other = make_device(
        load_miot_spec("cnhdm.airrtc.wkq01.json"), model="cnhdm.airrtc.wkq01.other",
    )
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback",
        customizes={"event_entities": True, "cloud_events": {"PeopleMotion": "paper_jammed"}},
    )
    not_owning = make_entry_with(other)
    not_owning.did_to_unique = {"someone-else": "unique"}
    owning = make_entry_with(device)
    seen = []
    device.add_listener(lambda data, only_info=False: seen.append(data))

    original = dict(HassEntry.ALL)
    HassEntry.ALL.clear()
    HassEntry.ALL['a'] = not_owning
    HassEntry.ALL['b'] = owning
    try:
        handled = HassEntry.dispatch_event_to_devices(REAL_CAMERA_MESSAGE)
    finally:
        HassEntry.ALL.clear()
        HassEntry.ALL.update(original)

    assert handled is True
    assert any("event.printer.paper_jammed" in d for d in seen)


def test_pet_message_shape_from_a_live_camera():
    """Regression: the pet alias was guessed as 'PetMotion' and never fired.

    Captured payload from the Living Room C701, 2026-08-06 08:11.
    """
    from custom_components.xiaomi_miot.core.utils import message_event_names
    from custom_components.xiaomi_miot.core.device_customizes import DEVICE_CUSTOMIZES

    body = {
        "event": "smart_camera_motion",
        "extra": {
            "extraInfo": '{"ver":"1.0.0","alarmStart":true,"eventType":"Pet","channel":"0"}',
            "isAlarm": True,
        },
    }
    names = message_event_names(body, DEVICE_CUSTOMIZES["*.camera.*"]["cloud_events"])

    assert names[0] == "pet_appeared"
