from __future__ import annotations

import gzip
import os
import struct
from dataclasses import dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VECTOR_CONSTRUCTOR_ID = 0x1CB5C415

# MTProto core "special" constructors which are not present in the generated schema.
# These are still sent by the server and must be parsed manually.
_RPC_RESULT_CONSTRUCTOR_ID = -212046591  # 0xF35C6D01
_MSG_CONTAINER_CONSTRUCTOR_ID = 1945237724  # 0x73F1F8DC
_GZIP_PACKED_CONSTRUCTOR_ID = 812830625  # 0x3072CFA1
_POLL_CONSTRUCTOR_ID = 1484026161  # 0x58747131
_POLL_RESULTS_CONSTRUCTOR_ID = 2061444128  # 0x7ADF2420
_ACCOUNT_THEMES_CONSTRUCTOR_ID = -1707242387  # 0x9A3D8C8D
_THEME_CONSTRUCTOR_ID = -1609668650  # 0xA00E67D6


@dataclass(slots=True)
class RpcResult:
    req_msg_id: int
    result: Any


@dataclass(slots=True)
class ContainerMessage:
    msg_id: int
    seqno: int
    obj: Any


@dataclass(slots=True)
class MsgContainer:
    messages: list[ContainerMessage]


@dataclass(slots=True)
class UnknownTLObject:
    cid: int | None
    expected_type: str | None
    path: str
    raw: bytes


class TLCodecError(Exception):
    pass


def _pad4(n: int) -> int:
    return (4 - (n % 4)) % 4


def _debug_dump_bad_tl_payload(data: bytes, *, prefix: str = "bad_payload") -> None:
    if os.getenv("TELECRAFT_DEBUG_DUMP_TL") != "1":
        return
    root = Path(os.getenv("TELECRAFT_DEBUG_TL_DIR", "reports/debug_tl"))
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = os.urandom(2).hex()
    path = root / f"{prefix}_{ts}_{suffix}.bin"
    path.write_bytes(data)


class TLWriter:
    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def to_bytes(self) -> bytes:
        return bytes(self._buf)

    def write_int(self, value: int) -> None:
        self._buf += struct.pack("<i", value)

    def write_uint(self, value: int) -> None:
        self._buf += struct.pack("<I", value)

    def write_long(self, value: int) -> None:
        self._buf += struct.pack("<q", value)

    def write_double(self, value: float) -> None:
        self._buf += struct.pack("<d", value)

    def write_bytes(self, data: bytes) -> None:
        ln = len(data)
        if ln < 254:
            self._buf.append(ln)
            self._buf += data
            self._buf += b"\x00" * _pad4(1 + ln)
            return

        self._buf.append(254)
        self._buf += struct.pack("<I", ln)[:3]
        self._buf += data
        self._buf += b"\x00" * _pad4(4 + ln)

    def write_string(self, value: str | bytes | bytearray) -> None:
        if isinstance(value, (bytes, bytearray)):
            self.write_bytes(bytes(value))
            return
        if isinstance(value, str):
            self.write_bytes(value.encode("utf-8"))
            return
        raise TLCodecError("string value must be str/bytes/bytearray")

    def write_object(self, obj: Any) -> None:
        # TLObject/TLRequest both have TL_ID/TL_PARAMS as ClassVar.
        tl_id = getattr(obj, "TL_ID", None)
        if not isinstance(tl_id, int) or tl_id == 0:
            raise TLCodecError(f"Object has invalid TL_ID: {obj!r}")
        self.write_int(tl_id)
        self._write_params(obj)

    def _write_params(self, obj: Any) -> None:
        tl_params = getattr(obj, "TL_PARAMS", None)
        if tl_params is None:
            return
        if not isinstance(tl_params, tuple):
            raise TLCodecError("Invalid TL_PARAMS")

        # Compute flags (for any param declared as '#').
        flags_values: dict[str, int] = {}
        for field, type_expr in tl_params:
            if type_expr == "#":
                flags_values[field] = 0

        if flags_values:
            for field, type_expr in tl_params:
                # e.g. flags.1?string
                if "?" not in type_expr or "." not in type_expr:
                    continue
                before_q, inner = type_expr.split("?", 1)
                if "." not in before_q:
                    continue
                flags_name, bit_s = before_q.split(".", 1)
                if flags_name not in flags_values:
                    continue
                try:
                    bit = int(bit_s)
                except ValueError:
                    continue
                value = getattr(obj, field, None)
                if inner == "true":
                    present = bool(value)
                else:
                    present = value is not None
                if present:
                    flags_values[flags_name] |= 1 << bit

        for field, type_expr in tl_params:
            if type_expr == "#":
                self.write_int(flags_values.get(field, int(getattr(obj, field, 0))))
                continue

            # Optional flags field: flags.N?T
            if "?" in type_expr and "." in type_expr.split("?", 1)[0]:
                before_q, inner = type_expr.split("?", 1)
                flags_name, bit_s = before_q.split(".", 1)
                bit_opt: int | None
                try:
                    bit_opt = int(bit_s)
                except ValueError:
                    bit_opt = None
                if bit_opt is not None and (flags_values.get(flags_name, 0) & (1 << bit_opt)) == 0:
                    continue
                if inner == "true":
                    continue
                value = getattr(obj, field)
                self.write_value(inner, value)
                continue

            value = getattr(obj, field)
            self.write_value(type_expr, value)

    def write_value(self, type_expr: str, value: Any) -> None:
        type_expr = type_expr.strip()
        if type_expr == "int":
            self.write_int(int(value))
            return
        if type_expr == "long":
            self.write_long(int(value))
            return
        if type_expr == "int128":
            if isinstance(value, int):
                value = int(value).to_bytes(16, "little", signed=False)
            if not isinstance(value, (bytes, bytearray)) or len(value) != 16:
                raise TLCodecError("int128 must be 16 bytes (or int convertible to 16 bytes)")
            self._buf += bytes(value)
            return
        if type_expr == "int256":
            if isinstance(value, int):
                value = int(value).to_bytes(32, "little", signed=False)
            if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
                raise TLCodecError("int256 must be 32 bytes (or int convertible to 32 bytes)")
            self._buf += bytes(value)
            return
        if type_expr == "double":
            self.write_double(float(value))
            return
        if type_expr == "string":
            self.write_string(value)
            return
        if type_expr == "bytes":
            if not isinstance(value, (bytes, bytearray)):
                raise TLCodecError("bytes value must be bytes/bytearray")
            self.write_bytes(bytes(value))
            return
        if type_expr == "Bool":
            # Encoded as constructor id
            self.write_int(-1720552011 if bool(value) else -1132882121)
            return
        if type_expr.startswith("Vector<") and type_expr.endswith(">"):
            inner = type_expr[len("Vector<") : -1].strip()
            if not isinstance(value, list):
                raise TLCodecError("Vector value must be a list")
            self.write_int(VECTOR_CONSTRUCTOR_ID)
            self.write_int(len(value))
            for item in value:
                self.write_value(inner, item)
            return

        # Assume TLObject/TLRequest
        if is_dataclass(value) or hasattr(value, "TL_ID"):
            self.write_object(value)
            return
        raise TLCodecError(f"Unsupported type expression: {type_expr!r}")


class TLReader:
    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def _read(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise TLCodecError("Unexpected EOF")
        out = self._data[self._pos : self._pos + n]
        self._pos += n
        return out

    def _remaining(self) -> int:
        return len(self._data) - self._pos

    def _peek_int(self) -> int:
        if self._remaining() < 4:
            raise TLCodecError("Unexpected EOF")
        return int(struct.unpack_from("<i", self._data, self._pos)[0])

    def read_int(self) -> int:
        return int(struct.unpack("<i", self._read(4))[0])

    def read_long(self) -> int:
        return int(struct.unpack("<q", self._read(8))[0])

    def read_double(self) -> float:
        return float(struct.unpack("<d", self._read(8))[0])

    def read_bytes(self) -> bytes:
        first = self._read(1)[0]
        if first < 254:
            ln = first
            data = self._read(ln)
            self._read(_pad4(1 + ln))
            return data
        ln = int.from_bytes(self._read(3), "little")
        data = self._read(ln)
        self._read(_pad4(4 + ln))
        return data

    def read_string(self) -> bytes:
        # TL "string" is stored with the same encoding as TL "bytes" (length + padding).
        # Decoding to text is caller responsibility.
        return self.read_bytes()

    def read_value(self, type_expr: str, *, path: str = "root") -> Any:
        type_expr = type_expr.strip()
        if type_expr == "int":
            return self.read_int()
        if type_expr == "long":
            return self.read_long()
        if type_expr == "int128":
            return self._read(16)
        if type_expr == "int256":
            return self._read(32)
        if type_expr == "double":
            return self.read_double()
        if type_expr == "string":
            return self.read_string()
        if type_expr == "bytes":
            return self.read_bytes()
        if type_expr == "Bool":
            cid = self.read_int()
            if cid == -1720552011:
                return True
            if cid == -1132882121:
                return False
            raise TLCodecError(f"Invalid Bool constructor id: {cid}")
        if type_expr.startswith("Vector<") and type_expr.endswith(">"):
            inner = type_expr[len("Vector<") : -1].strip()
            cid = self.read_int()
            if cid != VECTOR_CONSTRUCTOR_ID:
                raise TLCodecError(f"Invalid vector constructor id: {cid}")
            count = self.read_int()
            if count < 0:
                raise TLCodecError("Negative vector item count")
            return [self.read_value(inner, path=f"{path}[{idx}]") for idx in range(count)]
        # TL object (sum types): read by constructor id.
        return self.read_object(path=path, expected_type=type_expr)

    def _read_poll_for_message_media_poll(self, *, path: str) -> Any:
        from telecraft.tl.generated.types import Poll

        start = self._pos
        cid = self.read_int()
        if cid != _POLL_CONSTRUCTOR_ID:
            self._pos = start
            return self.read_object(path=path, expected_type="Poll")

        poll_id = self.read_long()
        flags = self.read_int()
        closed = bool(flags & (1 << 0))
        public_voters = bool(flags & (1 << 1))
        multiple_choice = bool(flags & (1 << 2))
        quiz = bool(flags & (1 << 3))
        question = self.read_value("TextWithEntities", path=f"{path}.question")
        answers = self.read_value("Vector<PollAnswer>", path=f"{path}.answers")

        close_period: int | None = None
        if flags & (1 << 4):
            close_period = self.read_int()

        close_date: int | None = None
        if flags & (1 << 5):
            if self._remaining() >= 4 and self._peek_int() == _POLL_RESULTS_CONSTRUCTOR_ID:
                # Compatibility: some payloads advertise close_date but jump
                # directly to pollResults.
                flags &= ~(1 << 5)
            else:
                close_date = self.read_int()

        return Poll(
            id=poll_id,
            flags=flags,
            closed=closed,
            public_voters=public_voters,
            multiple_choice=multiple_choice,
            quiz=quiz,
            question=question,
            answers=answers,
            close_period=close_period,
            close_date=close_date,
        )

    def _read_poll_results_bare(self, *, path: str) -> Any:
        from telecraft.tl.generated.types import PollResults

        flags = self.read_int()
        if flags < 0:
            raise TLCodecError(f"Invalid pollResults flags: {flags}")
        known_flags_mask = (1 << 5) - 1
        if flags & ~known_flags_mask:
            raise TLCodecError(f"Unsupported pollResults flags: {flags}")

        min_value = bool(flags & (1 << 0))
        results = (
            self.read_value("Vector<PollAnswerVoters>", path=f"{path}.results")
            if flags & (1 << 1)
            else None
        )
        total_voters = self.read_int() if flags & (1 << 2) else None
        recent_voters = (
            self.read_value("Vector<Peer>", path=f"{path}.recent_voters")
            if flags & (1 << 3)
            else None
        )
        solution = self.read_value("string", path=f"{path}.solution") if flags & (1 << 4) else None
        solution_entities = (
            self.read_value("Vector<MessageEntity>", path=f"{path}.solution_entities")
            if flags & (1 << 4)
            else None
        )

        return PollResults(
            flags=flags,
            min=min_value,
            results=results,
            total_voters=total_voters,
            recent_voters=recent_voters,
            solution=solution,
            solution_entities=solution_entities,
        )

    def _read_poll_results_for_message_media_poll(self, *, path: str) -> Any:
        start = self._pos

        try:
            cid = self.read_int()
            if cid != _POLL_RESULTS_CONSTRUCTOR_ID:
                raise TLCodecError(f"Unexpected pollResults constructor id: {cid}")
            return self._read_poll_results_bare(path=path)
        except TLCodecError:
            self._pos = start
            return self._read_poll_results_bare(path=path)

    def _read_message_media_poll(self, *, path: str) -> Any:
        from telecraft.tl.generated.types import MessageMediaPoll

        poll = self._read_poll_for_message_media_poll(path=f"{path}.poll")
        results = self._read_poll_results_for_message_media_poll(path=f"{path}.results")
        return MessageMediaPoll(poll=poll, results=results)

    def _find_next_theme_resync_pos(self, *, start: int) -> int | None:
        marker = struct.pack("<i", _THEME_CONSTRUCTOR_ID)
        search_from = start + 1
        while True:
            candidate = self._data.find(marker, search_from)
            if candidate < 0:
                return None

            saved_pos = self._pos
            try:
                self._pos = candidate
                obj = self.read_object(path="root.account.themes.resync", expected_type="Theme")
                if getattr(obj, "TL_NAME", None) == "theme":
                    return candidate
            except TLCodecError:
                pass
            finally:
                self._pos = saved_pos
            search_from = candidate + 1

    def _read_account_themes_resilient(self, *, path: str) -> Any:
        from telecraft.tl.generated.types import AccountThemes

        hash_value = self.read_long()
        vector_cid = self.read_int()
        if vector_cid != VECTOR_CONSTRUCTOR_ID:
            raise TLCodecError(f"Invalid vector constructor id: {vector_cid}")
        count = self.read_int()
        if count < 0:
            raise TLCodecError("Negative account.themes count")

        themes: list[Any] = []
        for idx in range(count):
            element_path = f"{path}.themes[{idx}]"
            element_start = self._pos
            try:
                theme_obj = self.read_object(path=element_path, expected_type="Theme")
                themes.append(theme_obj)
                continue
            except TLCodecError:
                self._pos = element_start

            next_pos = self._find_next_theme_resync_pos(start=element_start)
            if next_pos is None:
                if idx < count - 1:
                    raise
                next_pos = len(self._data)

            raw = bytes(self._data[element_start:next_pos])
            cid: int | None = None
            if len(raw) >= 4:
                cid = int(struct.unpack("<i", raw[:4])[0])
            themes.append(
                UnknownTLObject(
                    cid=cid,
                    expected_type="Theme",
                    path=element_path,
                    raw=raw,
                )
            )
            self._pos = next_pos

        return AccountThemes(hash=hash_value, themes=themes)

    def read_object(self, *, path: str = "root", expected_type: str | None = None) -> Any:
        from telecraft.tl.generated.registry import CONSTRUCTORS_BY_ID, METHODS_BY_ID

        cid_pos = self._pos
        cid = self.read_int()

        # Manual parsing for core MTProto containers/results.
        if cid == _RPC_RESULT_CONSTRUCTOR_ID:
            req_msg_id = self.read_long()
            result = self.read_object(path=f"{path}.rpc_result")
            return RpcResult(req_msg_id=req_msg_id, result=result)

        if cid == _MSG_CONTAINER_CONSTRUCTOR_ID:
            count = self.read_int()
            if count < 0:
                raise TLCodecError("Negative msg_container message count")
            messages: list[ContainerMessage] = []
            for idx in range(count):
                msg_id = self.read_long()
                seqno = self.read_int()
                ln = self.read_int()
                if ln < 0:
                    raise TLCodecError("Negative msg_container message length")
                payload = self._read(ln)
                inner = TLReader(payload)
                obj = inner.read_object(path=f"{path}.msg_container[{idx}]")
                messages.append(ContainerMessage(msg_id=msg_id, seqno=seqno, obj=obj))
            return MsgContainer(messages=messages)

        if cid == _GZIP_PACKED_CONSTRUCTOR_ID:
            packed = self.read_bytes()
            try:
                unpacked = gzip.decompress(packed)
            except Exception as e:  # noqa: BLE001
                raise TLCodecError("Failed to decompress gzip_packed payload") from e
            return TLReader(unpacked).read_object(path=f"{path}.gzip_packed")

        cls = CONSTRUCTORS_BY_ID.get(cid) or METHODS_BY_ID.get(cid)
        if cls is None:
            type_note = f" for {expected_type}" if expected_type else ""
            raise TLCodecError(
                f"Unknown constructor id: {cid}{type_note} at {path} (pos={cid_pos})"
            )

        if cid == _ACCOUNT_THEMES_CONSTRUCTOR_ID:
            return self._read_account_themes_resilient(path=path)
        if getattr(cls, "TL_NAME", None) == "messageMediaPoll":
            return self._read_message_media_poll(path=path)

        tl_params = getattr(cls, "TL_PARAMS", ())
        kwargs: dict[str, Any] = {}

        # Pre-read any flags int(s) so optional params can check bits.
        flags_values: dict[str, int] = {}
        for field, type_expr in tl_params:
            if type_expr == "#":
                flags_values[field] = self.read_int()
                kwargs[field] = flags_values[field]

        for field, type_expr in tl_params:
            if type_expr == "#":
                continue
            field_path = f"{path}.{getattr(cls, 'TL_NAME', cls.__name__)}.{field}"
            if "?" in type_expr and "." in type_expr.split("?", 1)[0]:
                before_q, inner = type_expr.split("?", 1)
                flags_name, bit_s = before_q.split(".", 1)
                try:
                    bit = int(bit_s)
                except ValueError:
                    bit = None
                if bit is None or (flags_values.get(flags_name, 0) & (1 << bit)) == 0:
                    # flags.N?true: bit not set => field is False (not True)
                    kwargs[field] = False if inner == "true" else None
                    continue
                if inner == "true":
                    kwargs[field] = True
                    continue
                kwargs[field] = self.read_value(inner, path=field_path)
                continue

            kwargs[field] = self.read_value(type_expr, path=field_path)

        return cls(**kwargs)


def dumps(obj: Any) -> bytes:
    w = TLWriter()
    w.write_object(obj)
    return w.to_bytes()


def loads(data: bytes) -> Any:
    r = TLReader(data)
    try:
        obj = r.read_object(path="root")
    except TLCodecError:
        _debug_dump_bad_tl_payload(data)
        raise
    return obj
