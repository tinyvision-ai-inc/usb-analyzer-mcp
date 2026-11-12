"""Dedicated COM apartment thread.

The USB Protocol Suite automation server is an out-of-process COM server
(UsbSuite.exe). COM objects must be used from the apartment that created
them, and the analyzer's connection-point events (_IAnalyzerEvents) are only
delivered while that apartment pumps Windows messages. All COM work is
therefore funnelled through one long-lived STA thread that pumps messages
while idle.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from typing import Any, Callable, TypeVar

import pythoncom

T = TypeVar("T")


class ComThread:
    def __init__(self, name: str = "usb-analyzer-com") -> None:
        self._q: queue.Queue[tuple[Callable[..., Any], tuple, dict, concurrent.futures.Future] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._main, name=name, daemon=True)
        self._started = threading.Event()
        self._thread.start()
        self._started.wait()

    def _main(self) -> None:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        self._started.set()
        try:
            while True:
                pythoncom.PumpWaitingMessages()
                try:
                    item = self._q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    break
                fn, args, kwargs, fut = item
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    fut.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001 - forwarded to caller
                    fut.set_exception(exc)
        finally:
            pythoncom.CoUninitialize()

    @property
    def is_com_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> concurrent.futures.Future[T]:
        fut: concurrent.futures.Future[T] = concurrent.futures.Future()
        self._q.put((fn, args, kwargs, fut))
        return fut

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run synchronously on the COM thread (blocking the caller)."""
        if self.is_com_thread:
            return fn(*args, **kwargs)
        return self.submit(fn, *args, **kwargs).result()

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run on the COM thread and await the result from asyncio."""
        return await asyncio.wrap_future(self.submit(fn, *args, **kwargs))

    def stop(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=10)


def pump_for(seconds: float) -> None:
    """Sleep while pumping COM messages (only valid on the COM thread)."""
    import time

    end = time.monotonic() + seconds
    while True:
        pythoncom.PumpWaitingMessages()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.02, remaining))
