"""Re-exec the current script under the project ``.venv`` (stdlib only)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def reexec_venv_if_needed(project_root: Path | None = None) -> None:
    """If not already running in ``project_root/.venv``, ``os.execv`` into it."""
    if os.environ.get("STOCK_SCREENER_VENV_OK") == "1":
        return
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    # When imported from scripts/*.py, pass ROOT explicitly (parents[1] of this
    # file is scripts/). Callers should pass the repo root.
    venv_py = root / ".venv" / "bin" / "python"
    if not venv_py.exists():
        return
    # Compare prefixes, not resolved binaries (macOS venv python → system path).
    if Path(sys.prefix).resolve() == (root / ".venv").resolve():
        return
    os.environ["STOCK_SCREENER_VENV_OK"] = "1"
    os.execv(str(venv_py), [str(venv_py), *sys.argv])
