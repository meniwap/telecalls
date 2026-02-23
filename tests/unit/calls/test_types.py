from __future__ import annotations

from types import SimpleNamespace

from telecraft.client.calls.types import (
    CallProtocolSettings,
    default_protocol,
    parse_call_config,
    protocol_from_call_config,
)


def test_parse_call_config_extracts_protocol_and_timeouts() -> None:
    obj = SimpleNamespace(
        TL_NAME="dataJSON",
        data='{"protocol":{"udp_p2p":true,"udp_reflector":true,"min_layer":120,"max_layer":210,"library_versions":["a","b"]},"call_connect_timeout_ms":15000,"packet_timeout":8,"connection_max_layer":190}',
    )

    parsed = parse_call_config(obj)

    assert parsed.protocol.udp_p2p is True
    assert parsed.protocol.udp_reflector is True
    assert parsed.protocol.min_layer == 120
    assert parsed.protocol.max_layer == 210
    assert parsed.protocol.library_versions == ("a", "b")
    assert parsed.connect_timeout_seconds == 15.0
    assert parsed.packet_timeout_seconds == 8.0
    assert parsed.connection_max_layer == 190


def test_parse_call_config_accepts_datajson_bytes_payload() -> None:
    obj = SimpleNamespace(
        TL_NAME="dataJSON",
        data=b'{"protocol":{"udp_p2p":false,"udp_reflector":true,"min_layer":140,"max_layer":211}}',
    )

    parsed = parse_call_config(obj)

    assert parsed.protocol.udp_p2p is False
    assert parsed.protocol.udp_reflector is True
    assert parsed.protocol.min_layer == 140
    assert parsed.protocol.max_layer == 211


def test_protocol_from_call_config_defaults_when_missing() -> None:
    protocol = protocol_from_call_config({})

    assert isinstance(protocol, CallProtocolSettings)
    assert protocol.udp_p2p is False
    assert protocol.udp_reflector is True
    assert protocol.min_layer <= protocol.max_layer


def test_default_protocol_uses_runtime_settings() -> None:
    settings = CallProtocolSettings(
        udp_p2p=False,
        udp_reflector=True,
        min_layer=130,
        max_layer=211,
        library_versions=("x",),
    )
    protocol = default_protocol(settings)

    assert protocol.udp_p2p is None
    assert protocol.udp_reflector is True
    assert protocol.min_layer == 130
    assert protocol.max_layer == 211
    assert protocol.library_versions == ["x"]
