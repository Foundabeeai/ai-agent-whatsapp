"""
Global concurrency gate ("queue") for Replicate API calls.

Every Replicate prediction (image gen, lip-sync, captions, TTS, …) passes through a
bounded semaphore. When more requests arrive than config.REPLICATE_CONCURRENCY, the
extra ones BLOCK (queue) until a slot frees up — so a burst of user requests can't
spawn unbounded in-flight predictions and trip Replicate's rate/concurrency limits or
pile up threads. Requests just take a little longer instead of failing.

Usage:
    from tools import replicate_queue

    @replicate_queue.gated("image")
    def generate_image(...): ...

    # or as a context manager:
    with replicate_queue.slot("lipsync"):
        ...call replicate...

Queue depth (how many are waiting) is logged and readable via stats().
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from contextlib import contextmanager

import config

logger = logging.getLogger(__name__)

_MAX = max(1, int(getattr(config, "REPLICATE_CONCURRENCY", 6)))
_SEMA = threading.BoundedSemaphore(_MAX)

# Lightweight counters for observability
_lock = threading.Lock()
_active = 0
_waiting = 0


def stats() -> dict:
    with _lock:
        return {"max": _MAX, "active": _active, "waiting": _waiting}


@contextmanager
def slot(label: str = "replicate"):
    """Acquire a Replicate slot for the duration of the block; queue if none free."""
    global _active, _waiting
    got = _SEMA.acquire(blocking=False)
    if not got:
        with _lock:
            _waiting += 1
        depth = stats()["waiting"]
        logger.info("replicate_queue: '%s' queued (%d waiting, cap=%d)", label, depth, _MAX)
        t0 = time.time()
        _SEMA.acquire()          # block until a slot frees
        with _lock:
            _waiting -= 1
        logger.info("replicate_queue: '%s' started after %.1fs wait", label, time.time() - t0)
    with _lock:
        _active += 1
    try:
        yield
    finally:
        with _lock:
            _active -= 1
        _SEMA.release()


def gated(label: str = "replicate"):
    """Decorator: run the whole function while holding one Replicate slot."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with slot(label):
                return fn(*args, **kwargs)
        return wrapper
    return deco
