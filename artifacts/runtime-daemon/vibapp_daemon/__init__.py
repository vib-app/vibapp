"""Local product-prototype lifecycle authority for VibApp packages.

This module is intentionally a control-plane implementation.  It never loads or
executes guest package bytes.
"""

from .core import DaemonError, RuntimeDaemon

__all__ = ["DaemonError", "RuntimeDaemon"]

