from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(slots=True)
class TLObject:
    """
    Base class for generated TL constructors.

    Generated code will override `TL_ID` and `TL_NAME`.
    """

    TL_ID: ClassVar[int]
    TL_NAME: ClassVar[str]


@dataclass(slots=True)
class TLRequest:
    """
    Base class for generated TL methods (requests).

    Generated code will override `TL_ID`, `TL_NAME`, `TL_RESULT`.
    """

    TL_ID: ClassVar[int]
    TL_NAME: ClassVar[str]
    TL_RESULT: ClassVar[str]

    def to_payload(self) -> dict[str, Any]:
        # Placeholder hook for future codec/serialization layer.
        return self.__dict__.copy()
