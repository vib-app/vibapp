"""Private host storage and locks, shared by POSIX and Windows daemon paths."""
from __future__ import annotations
import os
from pathlib import Path
import time

if os.name == "nt":
    import msvcrt
    from . import windows_security

    class FileLocks:
        LOCK_EX, LOCK_UN, LOCK_NB = 2, 8, 4

        @staticmethod
        def flock(fd: int, operation: int):
            os.lseek(fd, 0, os.SEEK_SET)
            if operation & FileLocks.LOCK_UN:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                return
            deadline = time.monotonic() + (0 if operation & FileLocks.LOCK_NB else 10)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise BlockingIOError("private host state is locked") from error
                    time.sleep(0.02)
    fcntl = FileLocks
else:
    import fcntl

def owner_controlled(path: Path, info=None, *, private: bool = False, system: bool = False) -> bool:
    if os.name == "nt":
        try:
            windows_security.verify(path, allow_administrators=system)
            return True
        except (OSError, ValueError):
            return False
    info = info or path.lstat()
    return info.st_uid in ({os.getuid(), 0} if system else {os.getuid()}) and not info.st_mode & (0o077 if private else 0o022)

def protect(path: Path, *, directory: bool = False):
    if os.name == "nt":
        windows_security.protect(path, directory=directory)
    else:
        os.chmod(path, 0o700 if directory else 0o600)

def sync_directory(path: Path):
    # Windows atomic replacement is durable after fsync on the file; the CRT
    # cannot open directories. Never pass a directory to os.open on Windows.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
