from custom_components.xiaomi_miot.core.converters import InfoConv


def converter_map(device):
    """attr -> domain for every converter except the always-present info button."""
    return {
        converter.attr: converter.domain
        for converter in device.converters
        if not isinstance(converter, InfoConv)
    }


def test_unmapped_properties_produce_nothing_without_opt_in(make_device, load_miot_spec):
    """The gap this feature closes.

    `printer.status` matches a GLOBAL_CONVERTERS rule so it is mapped, but the
    other eight properties and all three actions have no rule and no customize,
    so they are parsed from the spec and then dropped.
    """
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback",
        customizes={},
    )

    assert converter_map(device) == {"printer.status": "sensor"}


def test_generic_entities_covers_every_property_shape(make_device, load_miot_spec):
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback",
        customizes={"generic_entities": True},
    )
    converters = converter_map(device)

    # Writable properties become controls, chosen by spec shape.
    assert converters["printer.on"] == "switch"
    assert converters["printer.mode"] == "select"
    assert converters["printer.brightness"] == "number"
    assert converters["printer.custom_string"] == "text"

    # Read-only properties become sensors.
    assert converters["printer.custom_jammed"] == "binary_sensor"
    assert converters["printer.custom_count"] == "sensor"


def test_generic_entities_skips_properties_with_no_sensible_control(make_device, load_miot_spec):
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback",
        customizes={"generic_entities": True},
    )
    converters = converter_map(device)

    # Writable but unconstrained and non-textual — no control can be built.
    assert "printer.custom_blob" not in converters
    # Neither readable nor writable.
    assert "custom_notify" not in converters


def test_generic_entities_maps_actions_by_input_shape(make_device, load_miot_spec):
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback",
        customizes={"generic_entities": True},
    )
    converters = converter_map(device)

    assert converters["printer.start_print"] == "button"
    assert converters["printer.print_text"] == "text"
    assert converters["printer.set_mode"] == "select"
    # No single-value control fits two arguments, so it goes out as notify
    # rather than being dropped.
    assert converters["printer.print_copies"] == "notify"


def test_generic_entities_never_overrides_a_curated_converter(make_device, load_miot_spec):
    """`printer.status` is claimed by GLOBAL_CONVERTERS as a plain sensor.

    The fallback would classify it as a binary_sensor on shape alone, so this
    pins the precedence: curated mappings win, the fallback only fills gaps.
    """
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback",
        customizes={"generic_entities": True},
    )

    assert converter_map(device)["printer.status"] == "sensor"


def test_generic_entities_only_adds(make_device, load_miot_spec):
    without = make_device(
        load_miot_spec("cnhdm.airrtc.wkq01.json"),
        model="cnhdm.airrtc.wkq01.plain",
    )
    with_generic = make_device(
        load_miot_spec("cnhdm.airrtc.wkq01.json"),
        model="cnhdm.airrtc.wkq01.generic",
        customizes={"generic_entities": True},
    )

    curated = {converter.full_name for converter in without.converters}
    extended = {converter.full_name for converter in with_generic.converters}

    assert curated < extended, "generic pass should only add converters"


def test_generic_entities_fills_the_unmapped_service(make_device, load_miot_spec):
    """Service 4 ('function') on the thermostat matches no rule and no customize."""
    without = make_device(
        load_miot_spec("cnhdm.airrtc.wkq01.json"),
        model="cnhdm.airrtc.wkq01.plain2",
    )
    with_generic = make_device(
        load_miot_spec("cnhdm.airrtc.wkq01.json"),
        model="cnhdm.airrtc.wkq01.generic2",
        customizes={"generic_entities": True},
    )

    assert not [attr for attr in converter_map(without) if attr.startswith("function.")]
    assert [attr for attr in converter_map(with_generic) if attr.startswith("function.")]


def test_generic_entities_respects_excluded_properties(make_device, load_miot_spec):
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback",
        customizes={
            "generic_entities": True,
            "exclude_miot_properties": ["custom_string"],
        },
    )
    # `_exclude_miot_properties` is populated by async_init, which the test
    # fixture does not run; set it the same way and rebuild.
    device._exclude_miot_properties = device.custom_config_list("exclude_miot_properties", [])
    device.converters.clear()
    device.init_converters()
    converters = converter_map(device)

    assert "printer.custom_string" not in converters
    assert converters["printer.on"] == "switch"


def test_notify_message_becomes_action_parameters():
    """A MIoT action takes a positional list; commas are the natural syntax."""
    from custom_components.xiaomi_miot.notify import NotifyEntity

    assert NotifyEntity.parse_message("7, 1") == [7, 1]
    assert NotifyEntity.parse_message("draft") == ["draft"]
    assert NotifyEntity.parse_message("1.5,two,3") == [1.5, "two", 3]
    assert NotifyEntity.parse_message(None) == []


def test_notify_is_a_supported_domain():
    from custom_components.xiaomi_miot.core.const import SUPPORTED_DOMAINS

    assert "notify" in SUPPORTED_DOMAINS


def enum_ready(device, attr):
    """Build the SensorEntity metadata for one converter without a full HA setup."""
    from homeassistant.components.sensor import SensorDeviceClass
    from custom_components.xiaomi_miot.sensor import SensorEntity

    conv = next(c for c in device.converters if c.attr == attr)
    ent = SensorEntity.__new__(SensorEntity)
    ent.conv = conv
    ent._miot_property = conv.prop
    ent._attr_icon = None
    ent._attr_device_class = None
    ent._attr_state_class = None
    ent._attr_native_unit_of_measurement = None
    ent._attr_options = None
    ent.init_enum_options()
    return ent, SensorDeviceClass


def test_value_list_sensor_becomes_an_enum(make_device, load_miot_spec):
    """`printer.mode` is a value-list property exposed as a sensor."""
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback.enum",
        customizes={"generic_entities": True, "sensor_properties": "mode"},
    )
    ent, SensorDeviceClass = enum_ready(device, "printer.mode")

    assert ent._attr_device_class == SensorDeviceClass.ENUM
    # Lowercase, because MiotPropConv lowercases sensor values and HA rejects a
    # state that is not in options.
    assert ent._attr_options == ["draft", "photo"]
    # An enum sensor may carry neither of these.
    assert ent._attr_state_class is None
    assert ent._attr_native_unit_of_measurement is None


def test_non_enum_sensor_is_untouched(make_device, load_miot_spec):
    device = make_device(
        load_miot_spec("test.generic.fallback.json"),
        model="test.generic.fallback.enum2",
        customizes={"generic_entities": True},
    )
    ent, _ = enum_ready(device, "printer.custom_count")

    assert ent._attr_options is None
    assert ent._attr_device_class is None
