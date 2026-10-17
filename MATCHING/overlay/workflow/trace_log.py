"""Structured JSONL trace log for rate lookups.

Every record carries run_id, request_id, UTC time, stage, status, the adapter version and the database
snapshot. Records hold keys, counts, scores and reasons only: no patient name, member id, policy text,
free-text service descriptions or raw TINs. NPIs and ZIPs are operational data: the default log directory
(data/mrf/work/branch_a/logs) is gitignored and local; keep access restricted and delete logs older than
the retention period (default 30 days, see prune()).

A failed write never passes silently: the logger records the error, `ok` turns False, and the lookup
result reports it in `log_status`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import traceback
import uuid
from pathlib import Path

FORBIDDEN_KEYS = {"patient_name", "member_id", "service_description", "source_text", "tin", "tin_raw", "tin_value",
                  "tin_last4", "tin_business_name"}
DEFAULT_RETENTION_DAYS = 30


def _default(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, tuple):
        return list(obj)
    return str(obj)


def _scrub(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if k in FORBIDDEN_KEYS else _scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class TraceLog:
    def __init__(self, path: Path | None, run_id: str | None = None, context: dict | None = None):
        self.path = Path(path) if path else None
        self.run_id = run_id or "run_" + uuid.uuid4().hex[:12]
        self.context = dict(context or {})
        self.errors: list[str] = []
        self.records: list[dict] = []  # in-memory copy for the current request (tests, result trace)

    @property
    def ok(self) -> bool:
        return not self.errors

    def new_request(self) -> str:
        self.records = []
        return "req_" + uuid.uuid4().hex[:12]

    def event(self, request_id: str, stage: str, status: str, **data) -> None:
        rec = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"), "run_id": self.run_id,
               "request_id": request_id, "stage": stage, "status": status, **self.context, **_scrub(data)}
        self.records.append(rec)
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(rec, default=_default, sort_keys=True, allow_nan=False)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except (OSError, ValueError, TypeError) as e:
            self.errors.append(f"{stage}: {type(e).__name__}: {e}")

    def exception(self, request_id: str, stage: str, exc: BaseException) -> None:
        self.event(request_id, stage, "error", error=f"{type(exc).__name__}: {exc}",
                   stack=traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:])

    def status(self) -> str:
        if self.path is None:
            return "disabled"
        return "ok" if self.ok else "failed: " + "; ".join(self.errors[-3:])


def prune(directory: Path, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete *.jsonl logs older than the retention period. Returns the number removed."""
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for p in Path(directory).glob("*.jsonl"):
        if p.stat().st_mtime < cutoff:
            os.remove(p)
            removed += 1
    return removed
