"""In-memory Process / Version store with async scoring (Design v2.1 P1-lite)."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

ProcessStatus = Literal["draft", "running", "completed", "failed"]
VersionStatus = Literal["pending", "running", "completed", "failed"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ProcessRecord:
    id: str
    name: str
    product: str = "bank_marketing_term_deposit"
    candidate_source: str = "file"
    status: ProcessStatus = "draft"
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    error_message: str | None = None
    latest_version_id: str | None = None


@dataclass
class VersionRecord:
    id: str
    process_id: str
    status: VersionStatus = "pending"
    progress: float = 0.0
    triggered_by: str = "manual"
    created_at: datetime = field(default_factory=_utcnow)
    completed_at: datetime | None = None
    error_message: str | None = None
    total_candidates: int = 0
    valid_candidates: int = 0
    scored_candidates: int = 0
    cold_start_excluded: int = 0
    similarity_threshold: float | None = None
    # Persist only key + score (Design v2.1 §7.2) — no full feature JSON.
    scores: list[dict[str, Any]] = field(default_factory=list)
    histogram: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None


class ProcessStore:
    """Thread-safe in-memory store (swap for PostgreSQL in full P1)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[str, ProcessRecord] = {}
        self._versions: dict[str, VersionRecord] = {}
        # Candidate frames keyed by process_id (kept in memory for MVP).
        self._candidate_frames: dict[str, Any] = {}

    def create_process(
        self,
        name: str,
        product: str = "bank_marketing_term_deposit",
    ) -> ProcessRecord:
        with self._lock:
            process_id = str(uuid.uuid4())
            record = ProcessRecord(id=process_id, name=name, product=product)
            self._processes[process_id] = record
            return record

    def list_processes(self) -> list[ProcessRecord]:
        with self._lock:
            return sorted(self._processes.values(), key=lambda p: p.created_at, reverse=True)

    def get_process(self, process_id: str) -> ProcessRecord | None:
        with self._lock:
            return self._processes.get(process_id)

    def set_candidates(self, process_id: str, frame: Any, source: str = "file") -> ProcessRecord:
        with self._lock:
            process = self._require_process(process_id)
            self._candidate_frames[process_id] = frame
            process.candidate_source = source
            process.updated_at = _utcnow()
            return process

    def get_candidates(self, process_id: str) -> Any | None:
        with self._lock:
            return self._candidate_frames.get(process_id)

    def create_version(self, process_id: str, triggered_by: str = "manual") -> VersionRecord:
        with self._lock:
            process = self._require_process(process_id)
            version_id = str(uuid.uuid4())
            version = VersionRecord(id=version_id, process_id=process_id, triggered_by=triggered_by)
            self._versions[version_id] = version
            process.latest_version_id = version_id
            process.status = "running"
            process.updated_at = _utcnow()
            return version

    def get_version(self, version_id: str) -> VersionRecord | None:
        with self._lock:
            return self._versions.get(version_id)

    def list_versions(self, process_id: str) -> list[VersionRecord]:
        with self._lock:
            versions = [v for v in self._versions.values() if v.process_id == process_id]
            return sorted(versions, key=lambda v: v.created_at, reverse=True)

    def update_version(self, version_id: str, **kwargs: Any) -> VersionRecord:
        with self._lock:
            version = self._require_version(version_id)
            for key, value in kwargs.items():
                setattr(version, key, value)
            process = self._require_process(version.process_id)
            if version.status == "completed":
                process.status = "completed"
            elif version.status == "failed":
                process.status = "failed"
                process.error_message = version.error_message
            process.updated_at = _utcnow()
            return version

    def _require_process(self, process_id: str) -> ProcessRecord:
        process = self._processes.get(process_id)
        if process is None:
            raise KeyError(f"Process not found: {process_id}")
        return process

    def _require_version(self, version_id: str) -> VersionRecord:
        version = self._versions.get(version_id)
        if version is None:
            raise KeyError(f"Version not found: {version_id}")
        return version


_store = ProcessStore()


def get_process_store() -> ProcessStore:
    return _store
