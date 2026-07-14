"""
LangSmith tracing shim.

Goals:
  - Full operation tracing to LangSmith when LANGSMITH_API_KEY is set.
  - ZERO behaviour change / no crashes when the libs aren't installed or tracing is
    off — every decorator becomes a transparent pass-through.

Usage:
    from tools.tracing import traceable

    @traceable(run_type="chain")
    def handle_something(...): ...

Call init_tracing() once at startup (main.py) to wire the env vars LangSmith reads.
"""

from __future__ import annotations

import functools
import logging
import os

import config

logger = logging.getLogger(__name__)

# Try to load the real LangSmith decorator; fall back to a no-op if unavailable.
try:
    from langsmith import traceable as _ls_traceable  # type: ignore
    _HAS_LANGSMITH = True
except Exception:
    _ls_traceable = None
    _HAS_LANGSMITH = False

_ENABLED = _HAS_LANGSMITH and getattr(config, "LANGSMITH_TRACING", False)


def init_tracing() -> None:
    """Wire the env vars LangSmith reads. Safe to call always; no-ops if disabled."""
    if not _ENABLED:
        logger.info("tracing: LangSmith disabled (no key / lib / flag) — running untraced")
        return
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")   # older env name, still honoured
    os.environ["LANGSMITH_API_KEY"] = config.LANGSMITH_API_KEY
    os.environ["LANGCHAIN_API_KEY"] = config.LANGSMITH_API_KEY
    os.environ["LANGSMITH_PROJECT"] = config.LANGSMITH_PROJECT
    os.environ["LANGCHAIN_PROJECT"] = config.LANGSMITH_PROJECT
    logger.info("tracing: LangSmith ENABLED → project '%s'", config.LANGSMITH_PROJECT)


def traceable(run_type: str = "chain", name: str | None = None, **kwargs):
    """
    Decorator that traces a function to LangSmith when enabled, else returns it
    unchanged. Works on sync functions (all of this codebase's call sites).
    """
    def decorator(fn):
        if not _ENABLED:
            return fn
        try:
            return _ls_traceable(run_type=run_type, name=name or fn.__name__, **kwargs)(fn)
        except Exception:
            return fn
    return decorator


def is_enabled() -> bool:
    return _ENABLED
