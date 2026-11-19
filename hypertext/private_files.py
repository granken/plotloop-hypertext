"""Owner-only permissions for HyperText's local data files."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def open_private_file(path: Path, flags: int) -> int:
    """Open a regular file, tightening legacy permissions on the same descriptor."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise OSError(f"Expected a regular file: {path}")
        if stat.S_IMODE(mode) != 0o600:
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def protect_existing_file(path: Path) -> None:
    """Tighten permissions without opening SQLite files and dropping process locks."""
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise OSError(f"Expected a regular file: {path}")
        if stat.S_IMODE(mode) != 0o600:
            path.chmod(0o600, follow_symlinks=False)
    except FileNotFoundError:
        pass
