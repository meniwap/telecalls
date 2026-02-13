from __future__ import annotations

from pathlib import Path

from telecraft.tl import parse_tl_with_errors

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "src" / "telecraft" / "schema" / "sources"


def _summarize(name: str, text: str) -> int:
    schema, errors = parse_tl_with_errors(text)
    constructors = len(schema.constructors)
    methods = len(schema.methods)
    print(f"{name}: constructors={constructors} methods={methods} errors={len(errors)}")
    for e in errors[:10]:
        print(f"  line {e.line_no}: {e.error} :: {e.line[:160]}")
    return len(errors)


def main() -> int:
    api_path = SOURCES / "api.tl"
    mtproto_path = SOURCES / "mtproto.tl"
    if not api_path.exists() or not mtproto_path.exists():
        raise SystemExit("Schema not present. Run: python tools/fetch_schema.py --source tdesktop")

    api_text = api_path.read_text(encoding="utf-8")
    mtproto_text = mtproto_path.read_text(encoding="utf-8")

    errors = 0
    errors += _summarize("api.tl", api_text)
    errors += _summarize("mtproto.tl", mtproto_text)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
