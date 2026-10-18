from __future__ import annotations

from collections.abc import Callable
from threading import Event, Lock, Thread
from types import TracebackType

from ..integrations.apim_control_plane_contract import RetryablePublicationError


class WorkerLease:
    def __init__(self, renew: Callable[[], bool], lease_seconds: int) -> None:
        self._renew = renew
        self._interval = max(0.1, lease_seconds / 3)
        self._stopped = Event()
        self._lost = Event()
        self._lock = Lock()
        self._thread = Thread(target=self._maintain, daemon=True)

    def heartbeat(self) -> None:
        with self._lock:
            if self._lost.is_set():
                raise RetryablePublicationError("Worker lease is no longer owned")
            try:
                renewed = self._renew()
            except Exception:
                self._lost.set()
                raise
            if not renewed:
                self._lost.set()
                raise RetryablePublicationError("Worker lease is no longer owned")

    def _maintain(self) -> None:
        while not self._stopped.wait(self._interval):
            try:
                self.heartbeat()
            except Exception:
                return

    def __enter__(self) -> WorkerLease:
        self.heartbeat()
        self._thread.start()
        return self

    def __exit__(
        self, error_type: type[BaseException] | None,
        error: BaseException | None, traceback: TracebackType | None,
    ) -> None:
        self._stopped.set()
        self._thread.join(timeout=1)