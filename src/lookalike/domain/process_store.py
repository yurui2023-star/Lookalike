"""Durable Process / Version store with async scoring support (Design v2.1 P1).

Persists process metadata, versions, candidate CSVs, and score tables under
``data/store/`` so restarts do not wipe process_version history.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from lookalike.config import STORE_DIR

ProcessStatus = Literal["draft", "running", "completed", "failed"]
VersionStatus = Literal["pending", "running", "completed", "failed"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


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
    candidate_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.isoformat()
        payload["updated_at"] = self.updated_at.isoformat()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessRecord:
        return cls(
            id=data["id"],
            name=data["name"],
            product=data.get("product", "bank_marketing_term_deposit"),
            candidate_source=data.get("candidate_source", "file"),
            status=data.get("status", "draft"),
            created_at=_parse_dt(data["created_at"]) or _utcnow(),
            updated_at=_parse_dt(data["updated_at"]) or _utcnow(),
            error_message=data.get("error_message"),
            latest_version_id=data.get("latest_version_id"),
            candidate_path=data.get("candidate_path"),
        )


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
    # Large score tables live on disk; optional in-memory cache for recent reads.
    scores_path: str | None = None
    histogram: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    scores: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "process_id": self.process_id,
            "status": self.status,
            "progress": self.progress,
            "triggered_by": self.triggered_by,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_message": self.error_message,
            "total_candidates": self.total_candidates,
            "valid_candidates": self.valid_candidates,
            "scored_candidates": self.scored_candidates,
            "cold_start_excluded": self.cold_start_excluded,
            "similarity_threshold": self.similarity_threshold,
            "scores_path": self.scores_path,
            "histogram": self.histogram,
            "snapshot": self.snapshot,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VersionRecord:
        return cls(
            id=data["id"],
            process_id=data["process_id"],
            status=data.get("status", "pending"),
            progress=float(data.get("progress", 0.0)),
            triggered_by=data.get("triggered_by", "manual"),
            created_at=_parse_dt(data["created_at"]) or _utcnow(),
            completed_at=_parse_dt(data.get("completed_at")),
            error_message=data.get("error_message"),
            total_candidates=int(data.get("total_candidates", 0)),
            valid_candidates=int(data.get("valid_candidates", 0)),
            scored_candidates=int(data.get("scored_candidates", 0)),
            cold_start_excluded=int(data.get("cold_start_excluded", 0)),
            similarity_threshold=data.get("similarity_threshold"),
            scores_path=data.get("scores_path"),
            histogram=data.get("histogram"),
            snapshot=data.get("snapshot"),
        )


class ProcessStore:
    """Thread-safe durable store for process / process_version / candidates / scores."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else STORE_DIR
        self.processes_dir = self.root / "processes"
        self.versions_dir = self.root / "versions"
        self.candidates_dir = self.root / "candidates"
        self.scores_dir = self.root / "scores"
        for path in (
            self.root,
            self.processes_dir,
            self.versions_dir,
            self.candidates_dir,
            self.scores_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._processes: dict[str, ProcessRecord] = {}
        self._versions: dict[str, VersionRecord] = {}
        self._candidate_frames: dict[str, pd.DataFrame] = {}
        self._load()

    def _process_file(self, process_id: str) -> Path:
        return self.processes_dir / f"{process_id}.json"

    def _version_file(self, version_id: str) -> Path:
        return self.versions_dir / f"{version_id}.json"

    def _candidate_file(self, process_id: str) -> Path:
        return self.candidates_dir / f"{process_id}.csv"

    def _scores_file(self, version_id: str) -> Path:
        return self.scores_dir / f"{version_id}.json"

    def _load(self) -> None:
        for path in self.processes_dir.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            record = ProcessRecord.from_dict(data)
            self._processes[record.id] = record
        for path in self.versions_dir.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            record = VersionRecord.from_dict(data)
            self._versions[record.id] = record

    def _persist_process(self, record: ProcessRecord) -> None:
        self._process_file(record.id).write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _persist_version(self, record: VersionRecord) -> None:
        self._version_file(record.id).write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def create_process(
        self,
        name: str,
        product: str = "bank_marketing_term_deposit",
    ) -> ProcessRecord:
        with self._lock:
            process_id = str(uuid.uuid4())
            record = ProcessRecord(id=process_id, name=name, product=product)
            self._processes[process_id] = record
            self._persist_process(record)
            return record

    def list_processes(self) -> list[ProcessRecord]:
        with self._lock:
            return sorted(self._processes.values(), key=lambda p: p.created_at, reverse=True)

    def get_process(self, process_id: str) -> ProcessRecord | None:
        with self._lock:
            return self._processes.get(process_id)

    def set_candidates(
        self,
        process_id: str,
        frame: pd.DataFrame,
        source: str = "file",
    ) -> ProcessRecord:
        with self._lock:
            process = self._require_process(process_id)
            candidate_path = self._candidate_file(process_id)
            frame.to_csv(candidate_path, index=False)
            self._candidate_frames[process_id] = frame.copy()
            process.candidate_source = source
            process.candidate_path = str(candidate_path)
            process.updated_at = _utcnow()
            self._persist_process(process)
            return process

    def get_candidates(self, process_id: str) -> pd.DataFrame | None:
        with self._lock:
            if process_id in self._candidate_frames:
                return self._candidate_frames[process_id]
            process = self._processes.get(process_id)
            if process is None or not process.candidate_path:
                return None
            path = Path(process.candidate_path)
            if not path.exists():
                return None
            frame = pd.read_csv(path)
            self._candidate_frames[process_id] = frame
            return frame

    def create_version(self, process_id: str, triggered_by: str = "manual") -> VersionRecord:
        with self._lock:
            process = self._require_process(process_id)
            version_id = str(uuid.uuid4())
            version = VersionRecord(
                id=version_id,
                process_id=process_id,
                triggered_by=triggered_by,
            )
            self._versions[version_id] = version
            process.latest_version_id = version_id
            process.status = "running"
            process.error_message = None
            process.updated_at = _utcnow()
            self._persist_version(version)
            self._persist_process(process)
            return version

    def get_version(self, version_id: str) -> VersionRecord | None:
        with self._lock:
            version = self._versions.get(version_id)
            if version is None:
                return None
            if not version.scores and version.scores_path:
                scores_path = Path(version.scores_path)
                if scores_path.exists():
                    version.scores = json.loads(scores_path.read_text(encoding="utf-8"))
            return version

    def list_versions(self, process_id: str) -> list[VersionRecord]:
        with self._lock:
            versions = [v for v in self._versions.values() if v.process_id == process_id]
            return sorted(versions, key=lambda v: v.created_at, reverse=True)

    def update_version(self, version_id: str, **kwargs: Any) -> VersionRecord:
        with self._lock:
            version = self._require_version(version_id)
            scores = kwargs.pop("scores", None)
            for key, value in kwargs.items():
                setattr(version, key, value)
            if scores is not None:
                scores_path = self._scores_file(version_id)
                scores_path.write_text(
                    json.dumps(scores, ensure_ascii=False),
                    encoding="utf-8",
                )
                version.scores_path = str(scores_path)
                version.scores = scores

            process = self._require_process(version.process_id)
            if version.status == "completed":
                process.status = "completed"
                process.error_message = None
            elif version.status == "failed":
                process.status = "failed"
                process.error_message = version.error_message
            process.updated_at = _utcnow()
            self._persist_version(version)
            self._persist_process(process)
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


_store: ProcessStore | None = None
_store_lock = threading.Lock()


def get_process_store(root: Path | None = None) -> ProcessStore:
    """Return process store singleton (optional root override for tests)."""
    global _store
    if root is not None:
        return ProcessStore(root=root)
    with _store_lock:
        if _store is None:
            _store = ProcessStore()
        return _store


def reset_process_store_for_tests(root: Path) -> ProcessStore:
    """Replace the global singleton (tests only)."""
    global _store
    with _store_lock:
        _store = ProcessStore(root=root)
        return _store
