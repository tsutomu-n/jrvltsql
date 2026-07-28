"""No-live coverage for the opt-in bounded JV-Link COM diagnostic trace."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from src.importer import batch
from src.importer.batch import BatchProcessor
from src.jvlink.wrapper import JVLinkError, JVLinkWrapper
from scripts import quickstart


class FakeCom:
    def __init__(self, *, open_error: BaseException | None = None, close_error: BaseException | None = None) -> None:
        self.open_error = open_error
        self.close_error = close_error
        self.calls: list[str] = []

    def JVInit(self, _: str) -> int:
        self.calls.append("JVInit")
        return 0

    def JVOpen(self, *_: object) -> tuple[int, int, int, str]:
        self.calls.append("JVOpen")
        if self.open_error is not None:
            raise self.open_error
        return 0, 2, 0, "20260727120000"

    def JVClose(self) -> int:
        self.calls.append("JVClose")
        if self.close_error is not None:
            raise self.close_error
        return 0

    def JVRead(self, *_: object) -> None:
        self.calls.append("JVRead")
        raise AssertionError("JVRead must not be called by a diagnostic trace")


def _install_com_modules(monkeypatch: pytest.MonkeyPatch, com: FakeCom) -> list[str]:
    calls: list[str] = []
    pythoncom = types.ModuleType("pythoncom")

    def co_initialize() -> None:
        calls.append("pythoncom.CoInitialize")

    pythoncom.CoInitialize = co_initialize  # type: ignore[attr-defined]
    client = types.ModuleType("win32com.client")

    def dispatch(progid: str) -> FakeCom:
        calls.append(f"Dispatch:{progid}")
        return com

    client.Dispatch = dispatch  # type: ignore[attr-defined]
    win32com = types.ModuleType("win32com")
    win32com.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    return calls


def _event_pairs(trace_path: Path) -> list[tuple[str, str]]:
    value = json.loads(trace_path.read_text(encoding="utf-8"))
    return [(event["operation"], event["phase"]) for event in value["events"]]


def test_trace_records_all_com_boundaries_in_order_without_jvread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    com = FakeCom()
    bootstrap_calls = _install_com_modules(monkeypatch, com)
    trace_path = tmp_path / "trace.json"

    wrapper = JVLinkWrapper("TEST", diagnostic_trace_path=trace_path)
    assert wrapper.jv_init() == 0
    assert wrapper.jv_open("RACE", "20260713000000", option=2)[0] == 0
    assert wrapper.jv_close() == 0

    assert bootstrap_calls == ["pythoncom.CoInitialize", "Dispatch:JVDTLab.JVLink"]
    assert com.calls == ["JVInit", "JVOpen", "JVClose"]
    assert _event_pairs(trace_path) == [
        ("process", "started"),
        ("pythoncom.CoInitialize", "started"),
        ("pythoncom.CoInitialize", "returned"),
        ("Dispatch", "started"),
        ("Dispatch", "returned"),
        ("JVInit", "started"),
        ("JVInit", "returned"),
        ("JVOpen", "started"),
        ("JVOpen", "returned"),
        ("JVClose", "started"),
        ("JVClose", "returned"),
    ]
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["runtime"]["bits"] in {32, 64}
    assert all("exception_message" not in event for event in trace["events"])


def test_trace_records_com_exceptions_and_close_failure_without_exception_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    com = FakeCom(close_error=RuntimeError("private close failure"))
    _install_com_modules(monkeypatch, com)
    trace_path = tmp_path / "close-failure.json"
    wrapper = JVLinkWrapper("TEST", diagnostic_trace_path=trace_path)

    wrapper.jv_init()
    wrapper.jv_open("RACE", "20260713000000", option=2)
    with pytest.raises(JVLinkError):
        wrapper.jv_close()

    trace_text = trace_path.read_text(encoding="utf-8")
    assert "private close failure" not in trace_text
    assert _event_pairs(trace_path)[-2:] == [("JVClose", "started"), ("JVClose", "exception")]
    assert com.calls == ["JVInit", "JVOpen", "JVClose"]


def test_bounded_preflight_starts_trace_that_the_wrapper_resumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    com = FakeCom()
    _install_com_modules(monkeypatch, com)
    trace_path = tmp_path / "preflight-and-open.json"

    runtime_check = quickstart._check_jvlink_runtime(str(trace_path))
    assert runtime_check.ok is True
    assert runtime_check.stable_error is None
    wrapper = JVLinkWrapper(
        "JLTSQL",
        diagnostic_trace_path=trace_path,
        diagnostic_trace_resume=True,
    )
    wrapper.jv_init()
    wrapper.jv_open("RACE", "20260713000000", option=2)
    wrapper.jv_close()

    assert com.calls == ["JVInit", "JVInit", "JVOpen", "JVClose"]
    assert _event_pairs(trace_path) == [
        ("process", "started"),
        ("pythoncom.CoInitialize", "started"),
        ("pythoncom.CoInitialize", "returned"),
        ("Dispatch", "started"),
        ("Dispatch", "returned"),
        ("JVInit", "started"),
        ("JVInit", "returned"),
        ("pythoncom.CoInitialize", "started"),
        ("pythoncom.CoInitialize", "returned"),
        ("Dispatch", "started"),
        ("Dispatch", "returned"),
        ("JVInit", "started"),
        ("JVInit", "returned"),
        ("JVOpen", "started"),
        ("JVOpen", "returned"),
        ("JVClose", "started"),
        ("JVClose", "returned"),
    ]


def test_trace_is_disabled_by_default_and_batch_only_passes_an_explicit_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    com = FakeCom()
    _install_com_modules(monkeypatch, com)
    wrapper = JVLinkWrapper("TEST")
    wrapper.jv_init()
    wrapper.jv_open("RACE", "20260713000000", option=2)
    wrapper.jv_close()
    assert list(tmp_path.iterdir()) == []
    assert com.calls == ["JVInit", "JVOpen", "JVClose"]

    captured: dict[str, object] = {}

    class FakeHistoricalFetcher:
        def __init__(self, *_: object, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(batch, "HistoricalFetcher", FakeHistoricalFetcher)
    BatchProcessor(
        database=object(),
        jvlink_diagnostic_trace=str(tmp_path / "explicit-trace.json"),
    )
    assert captured["jvlink_diagnostic_trace"] == str(tmp_path / "explicit-trace.json")
