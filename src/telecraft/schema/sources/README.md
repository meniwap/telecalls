# TL schema sources

These files are pinned inputs for code generation:

- `api.tl`: Telegram Core API schema (layered)
- `mtproto.tl`: MTProto schema

Refresh/pin them with:

```bash
python tools/fetch_schema.py --source tdesktop --ref dev
```

Notes:

- `--source tdesktop` downloads the raw `.tl` inputs used by Telegram Desktop (preferred for TL-based codegen).
- `--source core-json` downloads JSON schema from `core.telegram.org` (useful reference, but doesn't include the layer number).


