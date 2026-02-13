from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], *, cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True)


def main() -> int:
    if os.environ.get("TELECALLS_BUILD_NATIVE") != "1":
        print({"native_build": "skipped", "reason": "TELECALLS_BUILD_NATIVE!=1"})
        return 0

    repo_root = Path(__file__).resolve().parents[1]
    native_root = repo_root / "native"
    native_build = native_root / "build"

    _run(["cmake", "-S", str(native_root), "-B", str(native_build)], cwd=repo_root)
    _run(["cmake", "--build", str(native_build), "--config", "Release"], cwd=repo_root)

    env = dict(os.environ)
    env.setdefault("TELECALLS_NATIVE_LIB_DIR", str(native_build))
    subprocess.run(
        [sys.executable, "-m", "telecraft.client.calls.native_build"],
        cwd=str(repo_root),
        env=env,
        check=True,
    )

    print({"native_build": "ok", "output_dir": str(native_build)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
