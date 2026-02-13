from __future__ import annotations

from pathlib import Path

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    _ = config
    for item in items:
        p = Path(str(item.fspath))
        parts = p.parts
        if "tests" in parts and "unit" in parts:
            item.add_marker(pytest.mark.unit)
