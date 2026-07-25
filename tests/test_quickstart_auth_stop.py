"""No-live tests for bounded update error propagation."""

import json

import pytest

from scripts import quickstart
from scripts.quickstart import QuickstartRunner


def _runner() -> QuickstartRunner:
    return QuickstartRunner(
        {
            "mode": "update",
            "from_date": "20260710",
            "to_date": "20260724",
            "include_timeseries": False,
            "include_realtime": False,
        }
    )


def test_update_stops_after_terminal_auth_error(monkeypatch) -> None:
    runner = _runner()
    expected_specs = [spec for spec, _, _ in runner._get_specs_for_mode()]
    calls: list[str] = []

    def fake_fetch(spec: str, option: int) -> tuple[str, dict]:
        calls.append(spec)
        return (
            "failed",
            {
                "error_type": "auth_expired",
                "stable_error": "jvopen_auth_expired",
                "error_message": "[jvopen_auth_expired] expired",
                "terminal": True,
            },
        )

    monkeypatch.setattr(runner, "_fetch_single_spec_with_progress", fake_fetch)
    monkeypatch.setattr(runner, "_print_spec_list", lambda *_: None)

    assert runner._run_fetch_all_rich() is False
    assert calls == expected_specs[:1]
    assert runner.spec_results[expected_specs[0]]["status"] == "failed"
    assert all(
        runner.spec_results[spec]["status"] == "not_run"
        for spec in expected_specs[1:]
    )


def test_update_preserves_all_configured_spec_results(monkeypatch) -> None:
    runner = _runner()
    expected_specs = [spec for spec, _, _ in runner._get_specs_for_mode()]
    calls: list[str] = []

    def fake_fetch(spec: str, option: int) -> tuple[str, dict]:
        calls.append(spec)
        return (
            "success",
            {
                "records_saved": 1,
                "error_type": None,
                "stable_error": None,
                "terminal": False,
            },
        )

    monkeypatch.setattr(runner, "_fetch_single_spec_with_progress", fake_fetch)
    monkeypatch.setattr(runner, "_print_spec_list", lambda *_: None)

    assert runner._run_fetch_all_rich() is True
    assert calls == expected_specs
    assert set(runner.spec_results) == set(expected_specs)
    assert all(
        result["status"] == "success"
        for result in runner.spec_results.values()
    )


def test_update_fails_after_nonterminal_spec_failure_but_runs_all_specs(
    monkeypatch,
) -> None:
    runner = _runner()
    expected_specs = [spec for spec, _, _ in runner._get_specs_for_mode()]
    calls: list[str] = []

    def fake_fetch(spec: str, option: int) -> tuple[str, dict]:
        calls.append(spec)
        if spec == "RACE":
            return (
                "failed",
                {
                    "error_type": "fetch",
                    "stable_error": "jvstatus_download_failed",
                    "error_message": "download failed",
                    "terminal": False,
                },
            )
        return (
            "nodata",
            {
                "error_type": None,
                "stable_error": None,
                "terminal": False,
            },
        )

    monkeypatch.setattr(runner, "_fetch_single_spec_with_progress", fake_fetch)
    monkeypatch.setattr(runner, "_print_spec_list", lambda *_: None)

    assert runner._run_fetch_all_rich() is False
    assert calls == expected_specs
    assert runner.spec_results["RACE"]["status"] == "failed"


def test_bounded_result_contains_all_six_specs_without_payload(tmp_path) -> None:
    result_path = tmp_path / "result.json"

    quickstart._write_bounded_build_result(
        str(result_path),
        exit_code=1,
        spec_results={
            "TOKU": {
                "status": "failed",
                "stable_error": "jvopen_auth_expired",
                "error_message": "must not be written",
            }
        },
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["schema_version"] == "jvdata_bounded_build_result_v1"
    assert result["exit_code"] == 1
    assert tuple(sorted(result["spec_results"])) == tuple(
        sorted(quickstart.BOUNDED_UPDATE_SPEC_NAMES)
    )
    assert result["spec_results"]["TOKU"] == {
        "status": "failed",
        "stable_error": "jvopen_auth_expired",
    }
    assert result["spec_results"]["DIFN"]["status"] == "not_run"
    assert "error_message" not in result["spec_results"]["TOKU"]


def test_bounded_result_refuses_overwrite(tmp_path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text("owner evidence", encoding="utf-8")

    with pytest.raises(FileExistsError):
        quickstart._write_bounded_build_result(
            str(result_path),
            exit_code=0,
            spec_results={},
        )

    assert result_path.read_text(encoding="utf-8") == "owner evidence"


def test_main_writes_bounded_result_for_runner_outcome(
    tmp_path,
    monkeypatch,
) -> None:
    result_path = tmp_path / "result.json"

    class FakeLock:
        def __init__(self, _: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class FakeRunner:
        def __init__(self, settings: dict) -> None:
            assert settings["mode"] == "update"
            self.spec_results = {
                name: {"status": "success", "stable_error": None}
                for name in quickstart.BOUNDED_UPDATE_SPEC_NAMES
            }

        def run(self) -> int:
            return 0

    monkeypatch.setattr(quickstart, "ProcessLock", FakeLock)
    monkeypatch.setattr(quickstart, "QuickstartRunner", FakeRunner)
    monkeypatch.setattr(
        quickstart,
        "_check_service_key",
        lambda: (True, "fixture"),
    )
    monkeypatch.setattr(
        quickstart.sys,
        "argv",
        [
            "quickstart.py",
            "--yes",
            "--mode",
            "update",
            "--from-date",
            "20260710",
            "--to-date",
            "20260724",
            "--result-json",
            str(result_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        quickstart.main()

    assert exc_info.value.code == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["exit_code"] == 0
    assert all(
        value["status"] == "success"
        for value in result["spec_results"].values()
    )


def test_main_accepts_and_propagates_download_timeouts(
    tmp_path,
    monkeypatch,
) -> None:
    result_path = tmp_path / "result.json"
    captured_settings: dict = {}

    class FakeLock:
        def __init__(self, _: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class FakeRunner:
        def __init__(self, settings: dict) -> None:
            captured_settings.update(settings)
            self.spec_results = {
                name: {"status": "nodata", "stable_error": None}
                for name in quickstart.BOUNDED_UPDATE_SPEC_NAMES
            }

        def run(self) -> int:
            return 0

    monkeypatch.setattr(quickstart, "ProcessLock", FakeLock)
    monkeypatch.setattr(quickstart, "QuickstartRunner", FakeRunner)
    monkeypatch.setattr(quickstart, "_check_service_key", lambda: (True, "fixture"))
    monkeypatch.setattr(
        quickstart.sys,
        "argv",
        [
            "quickstart.py",
            "--yes",
            "--mode",
            "update",
            "--from-date",
            "20260711",
            "--to-date",
            "20260725",
            "--download-timeout",
            "601",
            "--stall-timeout",
            "299",
            "--result-json",
            str(result_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        quickstart.main()

    assert exc_info.value.code == 0
    assert captured_settings["download_timeout"] == 601.0
    assert captured_settings["stall_timeout"] == 299.0


@pytest.mark.parametrize(
    ("download_timeout", "stall_timeout"),
    [("0", "1"), ("600", "0"), ("600", "600"), ("600", "601")],
)
def test_main_rejects_invalid_download_timeouts_before_runner(
    download_timeout,
    stall_timeout,
    monkeypatch,
) -> None:
    runner_started = False

    class FakeRunner:
        def __init__(self, _: dict) -> None:
            nonlocal runner_started
            runner_started = True

    monkeypatch.setattr(quickstart, "QuickstartRunner", FakeRunner)
    monkeypatch.setattr(
        quickstart.sys,
        "argv",
        [
            "quickstart.py",
            "--yes",
            "--mode",
            "update",
            "--download-timeout",
            download_timeout,
            "--stall-timeout",
            stall_timeout,
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        quickstart.main()

    assert exc_info.value.code == 2
    assert runner_started is False


def test_main_writes_bounded_result_when_runner_raises(
    tmp_path,
    monkeypatch,
) -> None:
    result_path = tmp_path / "result.json"

    class FakeLock:
        def __init__(self, _: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class FakeRunner:
        def __init__(self, _: dict) -> None:
            self.spec_results = {
                "TOKU": {
                    "status": "failed",
                    "stable_error": "fixture_unexpected_failure",
                }
            }

        def run(self) -> int:
            raise RuntimeError("fixture exception")

    monkeypatch.setattr(quickstart, "ProcessLock", FakeLock)
    monkeypatch.setattr(quickstart, "QuickstartRunner", FakeRunner)
    monkeypatch.setattr(
        quickstart,
        "_check_service_key",
        lambda: (True, "fixture"),
    )
    monkeypatch.setattr(
        quickstart.sys,
        "argv",
        [
            "quickstart.py",
            "--yes",
            "--mode",
            "update",
            "--from-date",
            "20260710",
            "--to-date",
            "20260724",
            "--result-json",
            str(result_path),
        ],
    )

    with pytest.raises(RuntimeError, match="fixture exception"):
        quickstart.main()

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["exit_code"] == 1
    assert result["spec_results"]["TOKU"] == {
        "status": "failed",
        "stable_error": "fixture_unexpected_failure",
    }
    assert result["spec_results"]["RACE"]["status"] == "not_run"


def test_main_refuses_existing_result_before_runner(
    tmp_path,
    monkeypatch,
) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text("owner evidence", encoding="utf-8")
    runner_started = False

    class FakeRunner:
        def __init__(self, _: dict) -> None:
            nonlocal runner_started
            runner_started = True

    monkeypatch.setattr(quickstart, "QuickstartRunner", FakeRunner)
    monkeypatch.setattr(
        quickstart.sys,
        "argv",
        [
            "quickstart.py",
            "--yes",
            "--mode",
            "update",
            "--from-date",
            "20260710",
            "--to-date",
            "20260724",
            "--result-json",
            str(result_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        quickstart.main()

    assert exc_info.value.code == 2
    assert runner_started is False
    assert result_path.read_text(encoding="utf-8") == "owner evidence"
