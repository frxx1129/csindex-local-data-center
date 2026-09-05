"""Package entry point for `python -m csindex_local` and Nuitka package mode."""

from __future__ import annotations

from .main import main


if __name__ == "__main__":  # pragma: no cover - exercised by package execution
    raise SystemExit(main())
