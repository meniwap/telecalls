from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class TLTypeRef:
    """
    A reference to a TL type expression, e.g.:

    - int
    - long
    - string
    - Vector<int>
    - flags.2?string
    """

    raw: str


@dataclass(frozen=True, slots=True)
class TLParam:
    name: str
    type_ref: TLTypeRef
    # Curly-brace params in TL schema (e.g. `{X:Type}`) declare generics and are not serialized.
    is_generic: bool = False


@dataclass(frozen=True, slots=True)
class TLCombinator:
    """Common base for constructors and methods."""

    name: str
    params: tuple[TLParam, ...]
    result: TLTypeRef
    constructor_id: int | None = None  # parsed from '#abcdef01' when present


@dataclass(frozen=True, slots=True)
class TLConstructor(TLCombinator):
    kind: Literal["constructor"] = "constructor"


@dataclass(frozen=True, slots=True)
class TLMethod(TLCombinator):
    kind: Literal["method"] = "method"


@dataclass(frozen=True, slots=True)
class TLSchema:
    """
    Parsed TL schema split into `types` and `functions` sections.

    Note: MTProto schema also has only a subset, but we keep the same shape.
    """

    constructors: tuple[TLConstructor, ...]
    methods: tuple[TLMethod, ...]
