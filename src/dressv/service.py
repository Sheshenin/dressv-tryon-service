"""Session-bound job lifecycle and in-memory worker pool."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .jobs import JobStore, Payload, QueueFull, State


class IdempotencyConflict(Exception):
    pass


@dataclass(frozen=True)
class Receipt:
    session: str
    digest: bytes
    job_id: str
    deadline: float


class Service:
    def __init__(
        self,
        generate: Callable[[Payload, float], bytes],
        review: Callable[[Payload, bytes, float], bool],
        *,
        ttl: float = 600,
        workers: int = 2,
        receipt_limit: int = 256,
    ) -> None:
        self._generate = generate
        self._review = review
        self._ttl = ttl
        self.store = JobStore(ttl_seconds=ttl, running_limit=workers)
        self._receipt_limit = receipt_limit
        self._receipts: dict[tuple[str, str], Receipt] = {}
        self._owners: dict[str, Receipt] = {}
        self._failures: dict[str, str] = {}
        self._condition = threading.Condition()
        self._closed = threading.Event()
        self._threads = [
            threading.Thread(target=self._worker, name=f"dressv-worker-{index}", daemon=True)
            for index in range(workers)
        ]
        self._threads.append(threading.Thread(target=self._sweep, name="dressv-sweep", daemon=True))
        for thread in self._threads:
            thread.start()

    def submit(self, session: str, key: str, payload: Payload) -> tuple[str, bool]:
        digest = hashlib.sha256()
        fields = (
            payload.product_id.encode(), payload.prompt.encode(),
            str(len(payload.required_details)).encode(),
            *(detail.encode() for detail in payload.required_details),
            payload.portrait, *payload.references,
        )
        for field in fields:
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
        fingerprint = digest.digest()
        with self._condition:
            self._expire()
            receipt = self._receipts.get((session, key))
            if receipt:
                if fingerprint != receipt.digest:
                    raise IdempotencyConflict()
                return receipt.job_id, False
            if len(self._receipts) >= self._receipt_limit:
                raise QueueFull()
            deadline = time.monotonic() + self._ttl
            job_id = self.store.submit(payload)
            receipt = Receipt(session, fingerprint, job_id, deadline)
            self._receipts[session, key] = receipt
            self._owners[job_id] = receipt
            self._condition.notify()
            return job_id, True

    def status(self, session: str, job_id: str) -> tuple[State | None, str | None]:
        with self._condition:
            if not self._owns(session, job_id):
                return None, None
            return self.store.status(job_id), self._failures.get(job_id)

    def result(self, session: str, job_id: str) -> tuple[State | None, bytes | None]:
        with self._condition:
            if not self._owns(session, job_id):
                return None, None
            return self.store.status(job_id), self.store.result(job_id)

    def discard(self, session: str, job_id: str) -> None:
        with self._condition:
            if self._owns(session, job_id):
                self.store.discard(job_id)
                self._owners.pop(job_id, None)
                self._failures.pop(job_id, None)

    def _owns(self, session: str, job_id: str) -> bool:
        receipt = self._owners.get(job_id)
        return bool(receipt and receipt.session == session and time.monotonic() < receipt.deadline)

    def _expire(self) -> None:
        self.store.expire()
        now = time.monotonic()
        expired = [key for key, receipt in self._receipts.items() if receipt.deadline <= now]
        for key in expired:
            receipt = self._receipts.pop(key)
            self._owners.pop(receipt.job_id, None)
            self._failures.pop(receipt.job_id, None)

    def _sweep(self) -> None:
        while not self._closed.wait(min(self._ttl, 1)):
            with self._condition:
                self._expire()

    def _worker(self) -> None:
        while not self._closed.is_set():
            with self._condition:
                lease = self.store.claim()
                if lease is None:
                    self._condition.wait()
                    continue
            result = None
            failure = "provider_failed"
            try:
                candidate = self._generate(lease.payload, lease.deadline)
                if self.store.status(lease.job_id) is State.RUNNING:
                    if self._review(lease.payload, candidate, lease.deadline):
                        result = candidate
                    else:
                        failure = "quality_rejected"
            except Exception:
                # Provider exceptions may contain request details.
                pass
            with self._condition:
                if self.store.finish(lease.job_id, result) and self.store.status(lease.job_id) is State.FAILED:
                    self._failures[lease.job_id] = failure
                self._condition.notify_all()
            del lease, result
            candidate = None

    def close(self) -> None:
        with self._condition:
            self._closed.set()
            self._condition.notify_all()
        for thread in self._threads:
            thread.join(timeout=1)
