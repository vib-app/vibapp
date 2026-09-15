#!/usr/bin/env python3
"""Pinned inert fixture used only by SyntheticLifecycleRunner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main() -> int:
    mode = os.environ["VIBAPP_SYNTHETIC_MODE"]
    output = Path(os.environ["VIBAPP_OUTPUT_DIR"])
    if mode == "output-bomb":
        sys.stdout.write("x" * (64 * 1024))
        sys.stdout.flush()
        time.sleep(1)
        return 0
    if mode == "disk-bomb":
        with (output / "disk.bin").open("wb") as handle:
            block = b"d" * (64 * 1024)
            for _ in range(64):
                handle.write(block)
                handle.flush()
                time.sleep(0.005)
        return 0
    if mode == "fork-burst":
        for _ in range(12):
            subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        time.sleep(30)
        return 0
    if mode == "hang":
        time.sleep(30)
        return 0
    if mode == "child-holds":
        subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    if mode == "nonzero":
        return 17
    visible = {
        "environment_keys": sorted(os.environ),
        "isolate_id": os.environ["VIBAPP_ISOLATE_ID"],
        "input_entries": sorted(path.name for path in Path(os.environ["VIBAPP_INPUT_DIR"]).iterdir()),
        "mode": mode,
    }
    (output / "result.json").write_text(
        json.dumps(visible, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print("synthetic-inert-complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
