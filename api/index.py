"""Vercel serverless entrypoint — exposes the Amusement FastAPI app."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from arena_api import app  # noqa: F401  (Vercel detects the `app` instance)
