"""Owner-private host-user admission shared by independent local processes.

Slot zero deliberately retains the legacy execution.lock inode. Lock files are
never removed: unlinking one while a process holds it would split the budget.
These are control-plane locks, never mounted into an authoring container.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from functools import lru_cache
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time


AUTHOR_LIMIT = 2
MAX_WAIT_SECONDS = 1800
_LIMITS = {"pipeline": AUTHOR_LIMIT, "codeagent": AUTHOR_LIMIT,
           "compiler": 1, "docker-admission": 1,
           "pipeline-legacy": 1, "codeagent-legacy": 1}


class BudgetError(RuntimeError):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


@lru_cache(maxsize=1)
def _runtime_directory():
    """Same namespace for a cleared desktop environment and an ordinary CLI.

    Darwin's trusted per-user directory preserves existing default-temp lock
    inodes. Never consult TMPDIR/TEMP/TMP. Older binaries with a custom temp
    directory must be stopped and restarted before mixed-version operation.
    """
    if sys.platform != "darwin":
        return Path("/tmp")
    try:
        result = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], env={},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2, check=False)
        if result.returncode or not 1 <= len(result.stdout) <= 4096:
            raise ValueError()
        path = Path(result.stdout.decode("utf8").strip())
        if not path.is_absolute() or path == Path("/"):
            raise ValueError()
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not _private(os.fstat(descriptor), directory=True):
                raise ValueError()
        finally:
            os.close(descriptor)
        return path
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise BudgetError("execution-lock-invalid", "canonical host runtime directory is unavailable") from error


def _root(kind):
    return _runtime_directory() / f"vibapp-{kind}-budget-{os.getuid()}"


def _private(metadata, *, directory=False):
    return (stat.S_ISDIR(metadata.st_mode) if directory else
            stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1) and (
        metadata.st_uid == os.getuid() and not metadata.st_mode & 0o077)


@contextmanager
def private_directory(kind):
    """Open a checked stable budget directory; callers must not replace it."""
    if not isinstance(kind, str) or kind not in _LIMITS:
        raise BudgetError("execution-lock-invalid", "unknown host execution budget")
    descriptor = None
    try:
        path = _root(kind)
        path.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        current = path.lstat()
        if (not _private(metadata, directory=True)
                or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)):
            raise BudgetError("execution-lock-invalid", "host budget directory is unsafe")
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise BudgetError("execution-lock-invalid", "host budget directory is unavailable") from error
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _check_slot(descriptor, name, root_descriptor):
    metadata = os.fstat(descriptor)
    current = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
    if (not _private(metadata)
            or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)):
        raise BudgetError("execution-lock-invalid", "host budget lock is unsafe")


def _open_slot(name, root_descriptor):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    for _ in range(3):
        try:
            return os.open(name, flags, dir_fd=root_descriptor)
        except FileNotFoundError:
            try:
                return os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_descriptor)
            except FileExistsError:
                # Another caller created the stable inode between the two opens.
                continue
    raise BudgetError("execution-lock-invalid", "host budget lock changed during open")


@contextmanager
def lease(kind, *, cancellation=None, wait_seconds=0):
    """Hold one fixed slot, failing busy or waiting a bounded cancellable time.

The yielded zero-based slot is informational only. A process crash closes the
descriptor and releases its slot without deleting or trusting a PID file.
"""
    if (not isinstance(kind, str) or kind not in _LIMITS or isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= MAX_WAIT_SECONDS
            or not math.isfinite(wait_seconds)):
        raise BudgetError("execution-lock-invalid", "invalid host budget or wait bound")
    deadline = time.monotonic() + wait_seconds
    descriptors = []
    acquired = None
    with private_directory(kind) as root_descriptor:
        try:
            # Validate every slot first; an unsafe occupied slot cannot silently
            # turn into a second namespace when another slot happens to be free.
            for slot in range(_LIMITS[kind]):
                name = "execution.lock" if slot == 0 else f"execution-{slot}.lock"
                descriptor = _open_slot(name, root_descriptor)
                descriptors.append((descriptor, name))
                _check_slot(descriptor, name, root_descriptor)
            while acquired is None:
                if cancellation is not None and cancellation.is_set():
                    raise BudgetError("provider-cancelled")
                for slot, (descriptor, name) in enumerate(descriptors):
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    acquired = descriptor
                    _check_slot(descriptor, name, root_descriptor)
                    if cancellation is not None and cancellation.is_set():
                        raise BudgetError("provider-cancelled")
                    break
                if acquired is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BudgetError("local-capacity-busy", "host execution capacity is occupied")
                if cancellation is None:
                    time.sleep(min(.05, remaining))
                else:
                    cancellation.wait(min(.05, remaining))
        except BaseException as error:
            for descriptor, _ in descriptors:
                os.close(descriptor)
            if isinstance(error, OSError):
                raise BudgetError("execution-lock-invalid", "host budget lock is unavailable") from error
            raise
        try:
            yield slot
        finally:
            try:
                fcntl.flock(acquired, fcntl.LOCK_UN)
            finally:
                for descriptor, _ in descriptors:
                    os.close(descriptor)
