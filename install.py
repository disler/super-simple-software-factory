#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""Repository entry point for installing SSSF into the current directory."""

import runpy
from pathlib import Path

INSTALLER = (
    Path(__file__).resolve().parent
    / ".claude"
    / "skills"
    / "sssf"
    / "scripts"
    / "install.py"
)

runpy.run_path(str(INSTALLER), run_name="__main__")
