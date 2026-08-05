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
    # Multi-argument actions have no single-value control.
    assert "printer.print_copies" not in converters


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
