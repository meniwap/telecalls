from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from telecraft.tl.generated.types import InputPhoneCall, PhoneCallProtocol


@dataclass(frozen=True, slots=True)
class PhoneCallRef:
    call_id: int
    access_hash: int

    @classmethod
    def from_parts(cls, call_id: int, access_hash: int) -> PhoneCallRef:
        return cls(call_id=int(call_id), access_hash=int(access_hash))


@dataclass(frozen=True, slots=True)
class CallProtocolSettings:
    udp_p2p: bool = False
    udp_reflector: bool = True
    # Voice-call protocol layers are independent from the MTProto API schema layer.
    min_layer: int = 65
    max_layer: int = 92
    library_versions: tuple[str, ...] = ("2.4.4", "2.7.7", "2.8.8")


@dataclass(frozen=True, slots=True)
class CallConfig:
    raw: dict[str, Any]
    protocol: CallProtocolSettings
    connect_timeout_seconds: float | None
    packet_timeout_seconds: float | None


def build_input_phone_call(ref: PhoneCallRef | Any) -> Any:
    if isinstance(ref, PhoneCallRef):
        return InputPhoneCall(id=int(ref.call_id), access_hash=int(ref.access_hash))
    return ref


def default_protocol(settings: CallProtocolSettings | None = None) -> PhoneCallProtocol:
    selected = settings if settings is not None else CallProtocolSettings()
    flags = 0
    if selected.udp_p2p:
        flags |= 1
    if selected.udp_reflector:
        flags |= 2

    return PhoneCallProtocol(
        flags=flags,
        udp_p2p=True if selected.udp_p2p else None,
        udp_reflector=True if selected.udp_reflector else None,
        min_layer=int(selected.min_layer),
        max_layer=int(selected.max_layer),
        library_versions=list(selected.library_versions),
    )


def parse_call_config(data_json: Any) -> CallConfig:
    parsed = _coerce_data_json(data_json)
    protocol = protocol_from_call_config(parsed)
    connect_timeout_seconds = _read_timeout_seconds(
        parsed,
        keys=("call_connect_timeout_ms", "connect_timeout_ms", "connect_timeout"),
    )
    packet_timeout_seconds = _read_timeout_seconds(
        parsed,
        keys=("call_packet_timeout_ms", "packet_timeout_ms", "packet_timeout"),
    )
    return CallConfig(
        raw=parsed,
        protocol=protocol,
        connect_timeout_seconds=connect_timeout_seconds,
        packet_timeout_seconds=packet_timeout_seconds,
    )


def protocol_from_call_config(config: Mapping[str, Any] | None) -> CallProtocolSettings:
    data = config or {}
    protocol_obj = _pick_protocol_object(data)

    udp_p2p = _read_bool(protocol_obj, "udp_p2p", default=False)
    udp_reflector = _read_bool(protocol_obj, "udp_reflector", default=True)

    min_layer = _read_int(protocol_obj, "min_layer", default=65)
    max_layer = _read_int(protocol_obj, "max_layer", default=92)
    if min_layer < 0:
        min_layer = 0
    if max_layer < min_layer:
        max_layer = min_layer

    versions = _read_library_versions(protocol_obj)
    if not versions:
        versions = ("2.4.4", "2.7.7", "2.8.8")

    return CallProtocolSettings(
        udp_p2p=udp_p2p,
        udp_reflector=udp_reflector,
        min_layer=min_layer,
        max_layer=max_layer,
        library_versions=versions,
    )


def _coerce_data_json(data_json: Any) -> dict[str, Any]:
    if isinstance(data_json, dict):
        return dict(data_json)
    if isinstance(data_json, Mapping):
        return dict(cast(Mapping[str, Any], data_json))

    payload = getattr(data_json, "data", None)
    if isinstance(payload, (bytes, bytearray)):
        try:
            return _parse_json_object(bytes(payload).decode("utf-8"))
        except UnicodeDecodeError:
            return {}
    if isinstance(payload, str):
        return _parse_json_object(payload)
    if isinstance(data_json, (bytes, bytearray)):
        try:
            return _parse_json_object(bytes(data_json).decode("utf-8"))
        except UnicodeDecodeError:
            return {}
    if isinstance(data_json, str):
        return _parse_json_object(data_json)
    return {}


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    if isinstance(obj, dict):
        return cast(dict[str, Any], obj)
    return {}


def _pick_protocol_object(config: Mapping[str, Any]) -> Mapping[str, Any]:
    if "protocol" in config and isinstance(config["protocol"], Mapping):
        return cast(Mapping[str, Any], config["protocol"])
    if "phone_call_protocol" in config and isinstance(config["phone_call_protocol"], Mapping):
        return cast(Mapping[str, Any], config["phone_call_protocol"])
    return config


def _read_bool(config: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = config.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _read_int(config: Mapping[str, Any], key: str, *, default: int) -> int:
    value = config.get(key)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def _read_library_versions(config: Mapping[str, Any]) -> tuple[str, ...]:
    value = config.get("library_versions")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    versions: list[str] = []
    for item in value:
        if isinstance(item, str):
            normalized = item.strip()
            if normalized:
                versions.append(normalized)
    return tuple(versions)


def _read_timeout_seconds(config: Mapping[str, Any], *, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = config.get(key)
        if isinstance(value, (int, float)):
            if "ms" in key:
                return max(0.1, float(value) / 1000.0)
            return max(0.1, float(value))
        if isinstance(value, str):
            try:
                parsed = float(value.strip())
            except ValueError:
                continue
            if "ms" in key:
                return max(0.1, parsed / 1000.0)
            return max(0.1, parsed)
    return None
