"""Process memory helpers for the hosted VM (8 GiB, no swap).

The Python workers run thread pools doing pandas work.  glibc keeps freed
memory inside the process, so a worker that needed 1.4 GiB for two minutes
stayed that large for the rest of the day: on 2026-11-11 the workers' resting
memory crept from about 0.35 GiB at boot to 1.6-1.9 GiB, and the VM ran out of
memory three times.  ``trim_memory`` hands freed heap back to the kernel after
long tasks; ``resident_mb`` is what the readiness timing reports.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import gc
import os
import threading
import time
from typing import Optional

_trim_lock = threading.Lock()
_last_trim = 0.0
_libc: Optional[ctypes.CDLL] = None
_libc_checked = False


def resident_mb() -> Optional[float]:
    """This process's resident memory in MiB (Linux), or None where /proc is unavailable."""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError):
        return None


def _malloc_trim() -> Optional[ctypes.CDLL]:
    global _libc, _libc_checked
    if not _libc_checked:
        _libc_checked = True
        try:
            name = ctypes.util.find_library("c")
            candidate = ctypes.CDLL(name) if name else None
            if candidate is not None and hasattr(candidate, "malloc_trim"):
                _libc = candidate
        except OSError:
            _libc = None
    return _libc


def trim_memory(min_interval_seconds: float = 30.0) -> bool:
    """Collect garbage and return freed heap to the kernel; at most once per interval.

    True when a trim ran.  A no-op (False) off glibc, when another thread is
    already trimming, or inside the interval.  Never raises.
    """
    global _last_trim
    now = time.monotonic()
    if now - _last_trim < min_interval_seconds or not _trim_lock.acquire(blocking=False):
        return False
    try:
        if now - _last_trim < min_interval_seconds:
            return False
        _last_trim = now
        libc = _malloc_trim()
        if libc is None:
            return False
        gc.collect()
        libc.malloc_trim(0)
        return True
    except Exception:
        return False
    finally:
        _trim_lock.release()
