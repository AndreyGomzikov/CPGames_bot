#!/usr/bin/env python3
"""Synchronize a staged release into the VPS deploy directory.

Runtime secrets and state are intentionally preserved in the destination.
This replaces an rsync dependency so deployment only needs Python 3 on the VPS.
"""

from __future__ import annotations

import fnmatch
import shutil
import sys
from pathlib import Path

PRESERVE_TOP_LEVEL = {".env", "credentials.json", "data"}
PRESERVE_PATTERNS = ("*.session", "*.session-journal")


def preserved(relative: Path) -> bool:
    if not relative.parts:
        return False
    if relative.parts[0] in PRESERVE_TOP_LEVEL:
        return True
    name = relative.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in PRESERVE_PATTERNS)


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def sync(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    # Remove files that no longer exist in the release, but never runtime data.
    for target in sorted(destination.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        relative = target.relative_to(destination)
        if preserved(relative):
            continue
        if not (source / relative).exists():
            remove_path(target)

    # Copy the release over the destination, skipping protected runtime paths.
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if preserved(relative) or relative.parts[0] == ".git":
            continue
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_symlink():
            remove_path(target)
            target.symlink_to(item.readlink())
        else:
            shutil.copy2(item, target)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: sync_release.py SOURCE DESTINATION", file=sys.stderr)
        return 2
    source = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    if not source.is_dir():
        print(f"source does not exist: {source}", file=sys.stderr)
        return 1
    sync(source, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
