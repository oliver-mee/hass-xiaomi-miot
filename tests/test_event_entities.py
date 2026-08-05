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
