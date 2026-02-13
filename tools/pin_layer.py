from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED_LAYER_FILE = ROOT / "src" / "telecraft" / "schema" / "pinned_layer.py"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manually set pinned TL layer.")
    parser.add_argument("layer", type=int)
    args = parser.parse_args(argv)

    content = PINNED_LAYER_FILE.read_text(encoding="utf-8")
    new_content, n = re.subn(
        r"(?m)^LAYER:\s*int\s*=\s*\d+\s*$",
        f"LAYER: int = {args.layer}",
        content,
    )
    if n != 1:
        raise RuntimeError("Could not update pinned layer (unexpected file format).")

    PINNED_LAYER_FILE.write_text(new_content, encoding="utf-8", newline="\n")
    print(f"Pinned LAYER={args.layer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
