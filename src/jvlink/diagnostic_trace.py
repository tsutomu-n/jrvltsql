"""Opt-in, non-secret trace for bounded JV-Link COM diagnosis."""

from __future__ import annotations

import json
import os
import platform
import struct
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "jrvltsql_jvlink_com_diagnostic_trace_v1"


def _now_jst() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat()


def _safe_return_value(value: Any) -> Any:
    """Keep result values useful without serializing COM objects or secrets."""

    if value is None or type(value) in {bool, int, float}:
        return value
    if isinstance(value, str):
        return value[:64]
    if isinstance(value, tuple):
        return [_safe_return_value(item) for item in value[:8]]
    return {"python_type": type(value).__name__}


class JVLinkDiagnosticTrace:
    """Create-once trace snapshots for explicitly authorized diagnosis only."""

    def __init__(self, path: str | Path, *, resume: bool = False) -> None:
        self.path = Path(path).resolve()
        if self.path.exists():
            if not resume:
                raise FileExistsError(f"JV-Link diagnostic trace already exists: {self.path}")
            value = json.loads(self.path.read_text(encoding="utf-8"))
            events = value.get("events") if isinstance(value, dict) else None
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != SCHEMA_VERSION
                or not isinstance(events, list)
                or any(not isinstance(event, dict) for event in events)
            ):
                raise ValueError(f"JV-Link diagnostic trace is invalid: {self.path}")
            if any(event.get("pid") != os.getpid() for event in events):
                raise ValueError(f"JV-Link diagnostic trace PID does not match: {self.path}")
            self.events = events
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self.record("process", "started")

    def record(
        self,
        operation: str,
        phase: str,
        *,
        return_value: Any = None,
        exception: BaseException | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "sequence": len(self.events) + 1,
            "at_jst": _now_jst(),
            "operation": operation,
            "phase": phase,
            "pid": os.getpid(),
            "runtime_bits": struct.calcsize("P") * 8,
        }
        if phase == "returned":
            event["return_value"] = _safe_return_value(return_value)
        if phase == "exception" and exception is not None:
            event["exception_type"] = type(exception).__name__
        self.events.append(event)
        self._write_snapshot()

    def _write_snapshot(self) -> None:
        value = {
            "schema_version": SCHEMA_VERSION,
            "runtime": {
                "executable": str(Path(sys.executable).resolve()),
                "python_version": platform.python_version(),
                "bits": struct.calcsize("P") * 8,
            },
            "events": self.events,
        }
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
