"""Importable hard gate for submit_one / assemble / superres."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

def _pipeline_py() -> Path:
    here = Path(__file__).resolve().parent
    return here / "pipeline.py"


def require(film_dir: Path | str, action: str, shot: str | None = None) -> None:
    film_dir = Path(film_dir)
    cmd = [sys.executable, str(_pipeline_py()), "require", str(film_dir), action]
    if shot:
        cmd.extend(["--shot", shot])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "pipeline gate failed").strip()
        print(f"[replica-pipeline] {msg}", file=sys.stderr)
        sys.exit(2)
