from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES_DIR = ROOT / "src" / "telecraft" / "schema" / "sources"
PINNED_LAYER_FILE = ROOT / "src" / "telecraft" / "schema" / "pinned_layer.py"


def _download_text(url: str) -> str:
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        data = resp.read()
    return data.decode("utf-8", errors="strict")


def _extract_layer(api_tl_text: str) -> int | None:
    # Telegram Desktop schema usually has: "// LAYER 195"
    m = re.search(r"(?m)^//\s*LAYER\s+(\d+)\s*$", api_tl_text)
    if not m:
        return None
    return int(m.group(1))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _update_pinned_layer(layer: int) -> None:
    content = PINNED_LAYER_FILE.read_text(encoding="utf-8")
    new_content, n = re.subn(
        r"(?m)^LAYER:\s*int\s*=\s*\d+\s*$",
        f"LAYER: int = {layer}",
        content,
    )
    if n != 1:
        raise RuntimeError("Could not update pinned layer (unexpected file format).")
    _write_text(PINNED_LAYER_FILE, new_content)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Fetch and pin Telegram TL schemas.")
    parser.add_argument(
        "--source",
        choices=["tdesktop", "core-json"],
        default="tdesktop",
        help=(
            "Schema source: 'tdesktop' downloads .tl from telegramdesktop/tdesktop "
            "(recommended for TL-based codegen); 'core-json' downloads JSON from core.telegram.org."
        ),
    )
    parser.add_argument(
        "--ref",
        default="dev",
        help=(
            "Git ref (branch/tag/commit) for telegramdesktop/tdesktop schema files "
            "(when --source tdesktop)."
        ),
    )
    parser.add_argument(
        "--layer",
        type=int,
        default=None,
        help="Override pinned layer (useful with --source core-json, which doesn't expose layer).",
    )
    args = parser.parse_args(argv)

    if args.source == "core-json":
        api_url = "https://core.telegram.org/schema/json"
        mtproto_url = "https://core.telegram.org/schema/mtproto-json"
    else:
        # TL sources used by Telegram Desktop (paths can change over time).
        api_url = (
            f"https://raw.githubusercontent.com/telegramdesktop/tdesktop/{args.ref}"
            "/Telegram/SourceFiles/mtproto/scheme/api.tl"
        )
        mtproto_url = (
            f"https://raw.githubusercontent.com/telegramdesktop/tdesktop/{args.ref}"
            "/Telegram/SourceFiles/mtproto/scheme/mtproto.tl"
        )

    api_text = _download_text(api_url)
    mtproto_text = _download_text(mtproto_url)

    if args.source == "core-json":
        _write_text(SOURCES_DIR / "api.json", api_text)
        _write_text(SOURCES_DIR / "mtproto.json", mtproto_text)
    else:
        _write_text(SOURCES_DIR / "api.tl", api_text)
        _write_text(SOURCES_DIR / "mtproto.tl", mtproto_text)

    layer: int | None = args.layer
    if layer is None and args.source != "core-json":
        layer = _extract_layer(api_text)
    if layer is None:
        raise RuntimeError(
            "Could not determine LAYER. Use --layer N (required for --source core-json)."
        )

    _update_pinned_layer(layer)
    extra = f"ref={args.ref}" if args.source == "tdesktop" else "core.telegram.org"
    print(f"Fetched schema source={args.source} ({extra}); pinned LAYER={layer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
