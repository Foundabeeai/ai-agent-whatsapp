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


# Lease settings for the distributed (multi-instance) queue. TTL must comfortably
# exceed a single prediction; a heartbeat renews it, and a crash reclaims it after TTL.
_DIST_TTL = 180.0       # seconds
_DIST_HEARTBEAT = 60.0  # renew the lease this often


@contextmanager
def _local_slot(label: str):
    """Per-process bounded-semaphore slot (single-instance mode)."""
    global _active, _waiting
    got = _SEMA.acquire(blocking=False)
    if not got:
        with _lock:
            _waiting += 1
        logger.info("replicate_queue: '%s' queued (%d waiting, cap=%d)", label, stats()["waiting"], _MAX)
        t0 = time.time()
        _SEMA.acquire()
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


@contextmanager
def _distributed_slot(label: str):
    """MongoDB-backed GLOBAL slot shared across all instances, with lease heartbeat."""
    import db
    limit = max(1, int(getattr(config, "REPLICATE_GLOBAL_CONCURRENCY", 6)))
    wait  = float(getattr(config, "REPLICATE_QUEUE_WAIT", 900))
    t0 = time.time()
    sid = db.acquire_replicate_slot(limit, _DIST_TTL, wait, label)
    if not sid:
        logger.warning("replicate_queue: '%s' failed open after %.0fs (queue saturated)",
                       label, time.time() - t0)
        yield                       # fail open — proceed rather than hang forever
        return
    waited = time.time() - t0
    if waited > 1.0:
        logger.info("replicate_queue: '%s' got global slot %s after %.1fs", label, sid, waited)

    # Heartbeat renews the lease so a long prediction keeps its slot
    stop = threading.Event()
    def _hb():
        while not stop.wait(_DIST_HEARTBEAT):
            db.renew_replicate_slot(sid, _DIST_TTL)
    hb = threading.Thread(target=_hb, daemon=True, name=f"replq-hb-{sid}")
    hb.start()
    try:
        yield
    finally:
        stop.set()
        db.release_replicate_slot(sid)


@contextmanager
def slot(label: str = "replicate"):
    """Acquire a Replicate slot for the duration of the block; queue if none free.
    Distributed (global) in SHARED_STATE mode, otherwise a fast per-process semaphore."""
    if getattr(config, "SHARED_STATE", False):
        with _distributed_slot(label):
            yield
    else:
        with _local_slot(label):
            yield


def gated(label: str = "replicate"):
    """Decorator: run the whole function while holding one Replicate slot."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with slot(label):
                return fn(*args, **kwargs)
        return wrapper
    return deco
