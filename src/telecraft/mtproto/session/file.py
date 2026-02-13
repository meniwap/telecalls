from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_SESSION_VERSION = 1


class SessionError(Exception):
    pass


@dataclass(slots=True)
class MtprotoSession:
    """
    Minimal MTProto session persistence.

    This intentionally stores only what we need to avoid re-doing the auth key exchange:
    - dc endpoint + framing (so we don't accidentally reuse a key for the wrong DC)
    - auth_key
    - server_salt
    - session_id (optional; if absent, a new one will be generated)
    """

    dc_id: int
    host: str
    port: int
    framing: str  # "intermediate" | "abridged"
    auth_key: bytes
    server_salt: bytes  # 8 bytes little-endian
    session_id: bytes | None = None  # 8 bytes
    version: int = _SESSION_VERSION

    def validate(self) -> None:
        if self.version != _SESSION_VERSION:
            raise SessionError(f"Unsupported session version: {self.version}")
        if not isinstance(self.dc_id, int) or self.dc_id <= 0:
            raise SessionError("Invalid dc_id")
        if not self.host:
            raise SessionError("Invalid host")
        if not isinstance(self.port, int) or not (0 < self.port < 65536):
            raise SessionError("Invalid port")
        if self.framing not in {"intermediate", "abridged"}:
            raise SessionError("Invalid framing")
        if not isinstance(self.auth_key, (bytes, bytearray)) or len(self.auth_key) < 32:
            raise SessionError("Invalid auth_key")
        if not isinstance(self.server_salt, (bytes, bytearray)) or len(self.server_salt) != 8:
            raise SessionError("Invalid server_salt (must be 8 bytes)")
        if self.session_id is not None and len(self.session_id) != 8:
            raise SessionError("Invalid session_id (must be 8 bytes)")

    def to_json_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "version": self.version,
            "dc_id": self.dc_id,
            "host": self.host,
            "port": self.port,
            "framing": self.framing,
            "auth_key_b64": base64.b64encode(self.auth_key).decode("ascii"),
            "server_salt_hex": self.server_salt.hex(),
            "session_id_hex": self.session_id.hex() if self.session_id is not None else None,
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, object]) -> MtprotoSession:
        try:
            version_obj = data.get("version", _SESSION_VERSION)
            if not isinstance(version_obj, (int, str)):
                raise SessionError("Invalid version")
            version = int(version_obj)

            dc_id_obj = data["dc_id"]
            if not isinstance(dc_id_obj, (int, str)):
                raise SessionError("Invalid dc_id")
            dc_id = int(dc_id_obj)

            host_obj = data["host"]
            if not isinstance(host_obj, str):
                raise SessionError("Invalid host")
            host = host_obj

            port_obj = data["port"]
            if not isinstance(port_obj, (int, str)):
                raise SessionError("Invalid port")
            port = int(port_obj)

            framing_obj = data["framing"]
            if not isinstance(framing_obj, str):
                raise SessionError("Invalid framing")
            framing = framing_obj

            auth_key_b64_obj = data["auth_key_b64"]
            if not isinstance(auth_key_b64_obj, str):
                raise SessionError("Invalid auth_key_b64")
            auth_key_b64 = auth_key_b64_obj

            server_salt_hex_obj = data["server_salt_hex"]
            if not isinstance(server_salt_hex_obj, str):
                raise SessionError("Invalid server_salt_hex")
            server_salt_hex = server_salt_hex_obj

            session_id_hex = data.get("session_id_hex")
        except Exception as e:  # noqa: BLE001
            raise SessionError("Invalid session JSON shape") from e

        try:
            auth_key = base64.b64decode(auth_key_b64.encode("ascii"))
        except Exception as e:  # noqa: BLE001
            raise SessionError("Invalid auth_key_b64") from e

        try:
            server_salt = bytes.fromhex(server_salt_hex)
        except Exception as e:  # noqa: BLE001
            raise SessionError("Invalid server_salt_hex") from e

        session_id: bytes | None
        if session_id_hex is None:
            session_id = None
        else:
            try:
                if not isinstance(session_id_hex, str):
                    raise SessionError("Invalid session_id_hex")
                session_id = bytes.fromhex(session_id_hex)
            except Exception as e:  # noqa: BLE001
                raise SessionError("Invalid session_id_hex") from e

        sess = cls(
            version=version,
            dc_id=dc_id,
            host=host,
            port=port,
            framing=framing,
            auth_key=auth_key,
            server_salt=server_salt,
            session_id=session_id,
        )
        sess.validate()
        return sess


def load_session_file(path: str | Path) -> MtprotoSession:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise SessionError(f"Failed to parse session JSON: {p}") from e
    if not isinstance(data, dict):
        raise SessionError("Session JSON must be an object")
    return MtprotoSession.from_json_dict(data)


def save_session_file(path: str | Path, session: MtprotoSession) -> None:
    session.validate()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(session.to_json_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Best-effort on non-POSIX filesystems.
        pass

    tmp.replace(p)
