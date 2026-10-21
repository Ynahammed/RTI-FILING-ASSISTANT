"""Per-thread Playwright browser reuse.

Every filing used to launch Chromium, and so did the payment and the verifier:
three process launches per run. Under concurrency that was the dominant cost in
the operations benchmark (submit p95 of 31 s, with Chromium failures falling back
to HTTP). A browser is expensive to start and cheap to reuse, so each worker
thread keeps one and opens a fresh context per call.

Playwright's sync objects are thread-affine, which is why this is
thread-local rather than a single shared browser. Each new context is isolated:
no cookies or storage are carried between a filing and the verifier's check.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator

_lock = threading.Lock()
_launches = 0
_reuses = 0


def _new_state() -> SimpleNamespace:
    global _launches
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=True)
    except Exception:
        playwright.stop()
        raise
    with _lock:
        _launches += 1
    return SimpleNamespace(playwright=playwright, browser=browser)


def _state() -> SimpleNamespace:
    global _reuses
    current = getattr(threading.current_thread(), "_rti_browser", None)
    if current is not None and current.browser.is_connected():
        with _lock:
            _reuses += 1
        return current
    if current is not None:
        close()
    state = _new_state()
    setattr(threading.current_thread(), "_rti_browser", state)
    return state


@contextmanager
def page() -> Iterator[object]:
    """Yield a fresh page in an isolated context, reusing the thread's browser."""
    state = _state()
    context = state.browser.new_context()
    try:
        yield context.new_page()
    finally:
        try:
            context.close()
        except Exception:
            pass


def close() -> None:
    state = getattr(threading.current_thread(), "_rti_browser", None)
    if state is None:
        return
    setattr(threading.current_thread(), "_rti_browser", None)
    try:
        state.browser.close()
    finally:
        try:
            state.playwright.stop()
        except Exception:
            pass


def stats() -> dict[str, int]:
    """Launch and reuse counts, so a measurement can prove reuse actually happened."""
    with _lock:
        return {"launches": _launches, "reuses": _reuses}


def reset_stats() -> None:
    global _launches, _reuses
    with _lock:
        _launches = 0
        _reuses = 0
