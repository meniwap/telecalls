from __future__ import annotations

import os
import platform
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

    configure_cmd = ["cmake", "-S", str(native_root), "-B", str(native_build)]
    cmake_prefix_path = os.environ.get("TELECALLS_CMAKE_PREFIX_PATH")
    if not cmake_prefix_path and platform.system() == "Darwin":
        if Path("/opt/homebrew").exists():
            cmake_prefix_path = "/opt/homebrew/opt/jpeg;/opt/homebrew"
    if cmake_prefix_path:
        configure_cmd.append(f"-DCMAKE_PREFIX_PATH={cmake_prefix_path}")
    _run(configure_cmd, cwd=repo_root)

    build_cmd = ["cmake", "--build", str(native_build), "--config", "Release"]
    build_jobs = os.environ.get("TELECALLS_BUILD_JOBS")
    if build_jobs:
        build_cmd.extend(["--parallel", str(build_jobs)])
    _run(build_cmd, cwd=repo_root)

    env = dict(os.environ)
    env.setdefault("TELECALLS_NATIVE_LIB_DIR", str(native_build))
    subprocess.run(
        [sys.executable, "-m", "telecraft.client.calls.native_build"],
        cwd=str(repo_root),
        env=env,
        check=True,
    )
    if env.get("TELECALLS_BUILD_TGCALLS_CFFI") == "1":
        subprocess.run(
            [sys.executable, "-m", "telecraft.client.calls.tgcalls_native_build"],
            cwd=str(repo_root),
            env=env,
            check=True,
        )

    print({"native_build": "ok", "output_dir": str(native_build)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
