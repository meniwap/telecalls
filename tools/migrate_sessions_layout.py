from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MoveAction:
    source: Path
    target: Path
    kind: str


def _runtime_dir_for_name(name: str) -> str | None:
    if name.startswith("test_") or name == "test.current":
        return "sandbox"
    if name.startswith("prod_") or name == "prod.current":
        return "prod"
    return None


def _plan_moves(sessions_root: Path) -> list[MoveAction]:
    moves: list[MoveAction] = []
    for src in sorted(sessions_root.glob("*")):
        if src.is_dir():
            continue
        name = src.name

        runtime_dir = _runtime_dir_for_name(name)
        if runtime_dir is not None:
            target = (
                sessions_root
                / runtime_dir
                / ("current" if name in {"test.current", "prod.current"} else name)
            )
            moves.append(MoveAction(source=src, target=target, kind="move"))
            continue

        if name == "live_audit_peer.txt":
            # Default legacy audit pointer goes to prod lane.
            target = sessions_root / "prod" / "live_audit_peer.txt"
            moves.append(MoveAction(source=src, target=target, kind="move"))
            continue

    return moves


def _apply_moves(actions: list[MoveAction], *, apply: bool) -> None:
    if not actions:
        print("No session files to migrate.")
        return

    print("Session migration plan:")
    for action in actions:
        print(f"- {action.kind}: {action.source} -> {action.target}")

    if not apply:
        print("\nDry-run only. Re-run with --apply to execute.")
        return

    for action in actions:
        action.target.parent.mkdir(parents=True, exist_ok=True)
        if action.target.exists():
            print(f"! skip (target exists): {action.target}")
            continue
        shutil.move(str(action.source), str(action.target))
        print(f"  moved: {action.source.name}")

    print("\nMigration complete.")


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Migrate legacy .sessions layout into isolated runtime roots:\n"
            ".sessions/sandbox and .sessions/prod"
        )
    )
    p.add_argument(
        "--sessions-root",
        type=str,
        default=".sessions",
        help="Sessions root directory",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply move operations (default is dry-run)",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show move operations without applying them",
    )
    args = p.parse_args()

    root = Path(args.sessions_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    actions = _plan_moves(root)
    _apply_moves(actions, apply=bool(args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
