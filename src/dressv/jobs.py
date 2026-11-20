"""Bounded, process-local storage for images waiting to be processed or delivered."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class QueueFull(Exception):
    pass


class State(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class Payload:
    product_id: str
    prompt: str
    required_details: tuple[str, ...]
    portrait: bytes
    references: tuple[bytes, ...]


@dataclass(frozen=True)
class Lease:
    job_id: str
    deadline: float
    payload: Payload


@dataclass
class _Job:
    deadline: float
    payload: Payload | None
    state: State = State.QUEUED
    result: bytes | None = None


class JobStore:
    def __init__(
        self,
        *,
        running_limit: int = 2,
        job_limit: int = 6,
        ttl_seconds: float = 600,
        input_limit: int = 16 * 1024 * 1024,
        output_limit: int = 8 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= running_limit <= job_limit:
            raise ValueError("Invalid concurrency limits")
        if min(ttl_seconds, input_limit, output_limit) <= 0:
            raise ValueError("Limits must be positive")
        self._running_limit = running_limit
        self._job_limit = job_limit
        self._ttl = ttl_seconds
        self._input_limit = input_limit
        self._output_limit = output_limit
        self._clock = clock
        self._jobs: dict[str, _Job] = {}
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, payload: Payload) -> str:
        images = (payload.portrait, *payload.references)
        if not 2 <= len(images) <= 5 or not all(images):
            raise ValueError("A portrait and one to four references are required")
        if sum(map(len, images)) > self._input_limit:
            raise ValueError("Image payload exceeds the job limit")
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            if len(self._jobs.keys() | self._running) >= self._job_limit:
                raise QueueFull()
            job_id = secrets.token_urlsafe(24)
            self._jobs[job_id] = _Job(now + self._ttl, payload)
            return job_id

    def claim(self) -> Lease | None:
        with self._lock:
            self._expire_locked(self._clock())
            if len(self._running) >= self._running_limit:
                return None
            for job_id, job in self._jobs.items():
                if job.state is State.QUEUED and job.payload is not None:
                    lease = Lease(job_id, job.deadline, job.payload)
                    job.payload = None
                    job.state = State.RUNNING
                    self._running.add(job_id)
                    return lease
            return None

    def finish(self, job_id: str, result: bytes | None) -> bool:
        """Release a worker slot. Never publish a cancelled or expired result."""
        with self._lock:
            if job_id not in self._running:
                return False
            self._running.remove(job_id)
            self._expire_locked(self._clock())
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if result and len(result) <= self._output_limit:
                job.result = result
                job.state = State.READY
            else:
                job.state = State.FAILED
            return True

    def status(self, job_id: str) -> State | None:
        with self._lock:
            self._expire_locked(self._clock())
            job = self._jobs.get(job_id)
            return job.state if job else None

    def result(self, job_id: str) -> bytes | None:
        with self._lock:
            self._expire_locked(self._clock())
            job = self._jobs.get(job_id)
            return job.result if job else None

    def discard(self, job_id: str) -> None:
        """Remove a job; a running worker still owns its lease until it exits."""
        with self._lock:
            self._jobs.pop(job_id, None)

    def expire(self) -> None:
        """Called by the service timer even when there is no incoming traffic."""
        with self._lock:
            self._expire_locked(self._clock())

    def _expire_locked(self, now: float) -> None:
        expired = [job_id for job_id, job in self._jobs.items() if job.deadline <= now]
        for job_id in expired:
            del self._jobs[job_id]
