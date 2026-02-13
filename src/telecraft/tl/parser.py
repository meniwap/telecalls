from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .ast import TLConstructor, TLMethod, TLParam, TLSchema, TLTypeRef

_SECTION_TYPES = "types"
_SECTION_FUNCTIONS = "functions"


_RE_SECTION = re.compile(r"^---(types|functions)---\s*$")


def _strip_inline_comment(line: str) -> str:
    # TL comments are usually full-line `// ...`, but we also defensively strip inline.
    if "//" not in line:
        return line
    return line.split("//", 1)[0]


def _parse_constructor_id(token: str) -> tuple[str, int | None]:
    if "#" not in token:
        return token, None
    name, hex_id = token.split("#", 1)
    hex_id = hex_id.strip()
    if not hex_id:
        return name, None
    # Telegram uses 32-bit signed ints in schema but encoded as hex.
    value = int(hex_id, 16)
    if value >= 2**31:
        value -= 2**32
    return name, value


def _parse_param(token: str) -> TLParam:
    token = token.strip()
    is_generic = False
    if token.startswith("{") and token.endswith("}"):
        token = token[1:-1].strip()
        is_generic = True
    if ":" not in token:
        raise ValueError(f"Invalid param token: {token!r}")
    name, type_expr = token.split(":", 1)
    return TLParam(name=name, type_ref=TLTypeRef(type_expr), is_generic=is_generic)


def _parse_combinator_line(line: str, section: str) -> TLConstructor | TLMethod:
    # Example:
    #   user#2e13f4c3 id:long first_name:string flags:# last_name:flags.1?string = User;
    if "=" not in line or not line.endswith(";"):
        raise ValueError("Not a combinator line")

    left, right = line.split("=", 1)
    left = left.strip()
    right = right.strip()
    if not right.endswith(";"):
        raise ValueError("Missing ';'")
    result_expr = right[:-1].strip()

    parts = [p for p in left.split(" ") if p]
    if not parts:
        raise ValueError("Empty combinator line")

    name_token = parts[0]
    name, cid = _parse_constructor_id(name_token)
    params_list: list[TLParam] = []
    in_brackets = False
    for p in parts[1:]:
        # Some special schema lines (notably `vector`) include bare tokens like `#` or `[ t ]`.
        if in_brackets:
            if "]" in p:
                in_brackets = False
            continue
        if "[" in p:
            in_brackets = True
            continue
        if p == "#" or p == "?":
            continue
        params_list.append(_parse_param(p))
    params = tuple(params_list)
    result = TLTypeRef(result_expr)

    if section == _SECTION_FUNCTIONS:
        return TLMethod(name=name, constructor_id=cid, params=params, result=result)
    return TLConstructor(name=name, constructor_id=cid, params=params, result=result)


@dataclass(frozen=True, slots=True)
class TLParseError:
    line_no: int
    line: str
    error: str


def parse_tl(text: str, *, strict: bool = True) -> TLSchema:
    schema, errors = parse_tl_with_errors(text)
    if strict and errors:
        first = errors[0]
        raise ValueError(f"TL parse error at line {first.line_no}: {first.error}: {first.line!r}")
    return schema


def parse_tl_with_errors(text: str) -> tuple[TLSchema, tuple[TLParseError, ...]]:
    constructors: list[TLConstructor] = []
    methods: list[TLMethod] = []
    errors: list[TLParseError] = []

    section = _SECTION_TYPES
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_inline_comment(raw_line).strip()
        if not line:
            continue

        m = _RE_SECTION.match(line)
        if m:
            section = m.group(1)
            continue

        if not line.endswith(";") or "=" not in line:
            # Ignore non-combinator lines (headers, etc.).
            continue

        try:
            combinator = _parse_combinator_line(line, section)
        except Exception as e:  # noqa: BLE001
            errors.append(TLParseError(line_no=idx, line=line, error=str(e)))
            continue

        if isinstance(combinator, TLConstructor):
            constructors.append(combinator)
        else:
            methods.append(combinator)

    return TLSchema(constructors=tuple(constructors), methods=tuple(methods)), tuple(errors)


def parse_tl_file(path: str | Path) -> TLSchema:
    p = Path(path)
    return parse_tl(p.read_text(encoding="utf-8"))
