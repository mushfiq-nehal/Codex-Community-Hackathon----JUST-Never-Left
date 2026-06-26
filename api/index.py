"""Vercel serverless entrypoint.

Vercel's Python runtime imports this module and serves the ASGI ``app`` object.
All application logic lives in the ``app`` package at the repository root.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

__all__ = ["app"]
