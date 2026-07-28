"""No-live tests for bounded update error propagation."""

import json
from types import SimpleNamespace

import pytest

from scripts import quickstart
from scripts.quickstart import QuickstartRunner
from src.jvlink import wrapper as jvlink_wrapper


def _runtime_ready() -> quickstart.JVLinkRuntimeCheck:
    return quickstart.JVLinkRuntimeCheck(
        ok=True,
        stage="jvinit",
        stable_error=None,
        operator_message="fixture runtime ready",
        runtime_bits=32,
    )


def _runtime_unavailable() -> quickstart.JVLinkRuntimeCheck:
    return quickstart.JVLinkRuntimeCheck(
        ok=False,
        stage="com_bootstrap",
        stable_error="jvlink_com_unavailable",
        operator_message="JV-Link COMアクセス失敗",
        runtime_bits=32,
    )


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


def test_create_tables_targets_exact_sqlite_path(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "bounded.db"
    runner = QuickstartRunner(
        {
            "mode": "update",
            "db_type": "sqlite",
            "db_path": str(db_path),
        }
    )
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(quickstart.subprocess, "run", fake_run)

    assert runner._run_create_tables() is True
    assert seen["command"] == [
        quickstart.sys.executable,
        "-m",
        "src.cli.main",
        "create-tables",
        "--db",
        "sqlite",
        "--db-path",
        str(db_path),
    ]


def test_quickstart_sqlite_database_does_not_require_postgresql_driver(
    monkeypatch,
    tmp_path,
) -> None:
    import builtins

    db_path = tmp_path / "bounded.db"
    runner = QuickstartRunner(
        {
            "mode": "update",
            "db_type": "sqlite",
            "db_path": str(db_path),
        }
    )
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.endswith("postgresql_handler"):
            raise AssertionError("SQLite runner imported PostgreSQL backend")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    database = runner._create_database()

    assert database.get_db_type() == "sqlite"
    assert database.db_path == db_path


def test_create_tables_failure_preserves_stdout_and_stable_error(
    monkeypatch,
) -> None:
    runner = _runner()

    monkeypatch.setattr(
        quickstart.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="driver unavailable",
            stderr="",
        ),
    )

    assert runner._run_create_tables() is False
    assert runner.errors == ["テーブル作成失敗: driver unavailable"]
    assert set(runner.spec_results) == set(quickstart.BOUNDED_UPDATE_SPEC_NAMES)
    assert all(
        result == {
            "status": "not_run",
            "stable_error": "table_creation_failed",
        }
        for result in runner.spec_results.values()
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


def test_runtime_check_success_does_not_claim_service_auth(monkeypatch) -> None:
    cleaned_up = False

    class FakeWrapper:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def jv_init(self) -> int:
            return 0

        def cleanup(self) -> None:
            nonlocal cleaned_up
            cleaned_up = True

    monkeypatch.setattr(jvlink_wrapper, "JVLinkWrapper", FakeWrapper)

    result = quickstart._check_jvlink_runtime()

    assert result.ok is True
    assert result.stage == "jvinit"
    assert result.stable_error is None
    assert "COM初期化OK" in result.operator_message
    assert "認証OK" not in result.operator_message
    assert cleaned_up is True


def test_runtime_check_classifies_missing_pywin32_without_exception_text(
    monkeypatch,
) -> None:
    private_text = "private dependency path"

    def missing_wrapper(*_: object, **__: object) -> None:
        raise ModuleNotFoundError(
            f"No module named 'win32com': {private_text}",
            name="win32com",
        )

    monkeypatch.setattr(jvlink_wrapper, "JVLinkWrapper", missing_wrapper)

    result = quickstart._check_jvlink_runtime()

    assert result.ok is False
    assert result.stage == "com_bootstrap"
    assert result.stable_error == "jvlink_pywin32_missing"
    assert private_text not in result.operator_message


def test_runtime_check_sanitizes_dispatch_failure(monkeypatch) -> None:
    private_text = "private COM registration detail"

    def failing_wrapper(*_: object, **__: object) -> None:
        raise RuntimeError(private_text)

    monkeypatch.setattr(jvlink_wrapper, "JVLinkWrapper", failing_wrapper)

    result = quickstart._check_jvlink_runtime()

    assert result.ok is False
    assert result.stage == "com_bootstrap"
    assert result.stable_error == "jvlink_com_unavailable"
    assert result.operator_message == "JV-Link COMアクセス失敗"
    assert private_text not in result.operator_message


def test_runtime_check_classifies_jvinit_failure(monkeypatch) -> None:
    cleaned_up = False

    class JVInitFailure(RuntimeError):
        error_code = -103

    class FakeWrapper:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def jv_init(self) -> int:
            raise JVInitFailure("private JVInit detail")

        def cleanup(self) -> None:
            nonlocal cleaned_up
            cleaned_up = True

    monkeypatch.setattr(jvlink_wrapper, "JVLinkWrapper", FakeWrapper)

    result = quickstart._check_jvlink_runtime()

    assert result.ok is False
    assert result.stage == "jvinit"
    assert result.stable_error == "jvlink_initialization_failed"
    assert result.operator_message == "JV-Link COM初期化失敗 (JVInit code: -103)"
    assert "private JVInit detail" not in result.operator_message
    assert cleaned_up is True


def test_runner_rich_prerequisite_reuses_injected_runtime_check(
    monkeypatch,
) -> None:
    runtime_check = _runtime_ready()
    runner = QuickstartRunner({}, jvlink_runtime_check=runtime_check)
    monkeypatch.setattr(quickstart.sys, "platform", "win32")
    monkeypatch.setattr(
        quickstart,
        "_check_jvlink_runtime",
        lambda *_: pytest.fail("runtime check was executed twice"),
    )

    assert runner._check_prerequisites_rich() is True
    assert runner._jvlink_runtime_check is runtime_check


def test_runner_simple_prerequisite_reuses_injected_runtime_check(
    monkeypatch,
) -> None:
    runtime_check = _runtime_ready()
    runner = QuickstartRunner({}, jvlink_runtime_check=runtime_check)
    monkeypatch.setattr(quickstart.sys, "platform", "win32")
    monkeypatch.setattr(
        quickstart,
        "_check_jvlink_runtime",
        lambda *_: pytest.fail("runtime check was executed twice"),
    )

    assert runner._check_prerequisites_simple() is True
    assert runner._jvlink_runtime_check is runtime_check


def test_main_preserves_service_precheck_failure_for_all_specs(
    tmp_path,
    monkeypatch,
) -> None:
    result_path = tmp_path / "result.json"
    runner_started = False

    class FakeRunner:
        def __init__(
            self,
            _: dict,
            *,
            jvlink_runtime_check: quickstart.JVLinkRuntimeCheck | None = None,
        ) -> None:
            nonlocal runner_started
            runner_started = True

    monkeypatch.setattr(quickstart, "QuickstartRunner", FakeRunner)
    monkeypatch.setattr(
        quickstart,
        "_check_jvlink_runtime",
        lambda *_: _runtime_unavailable(),
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
            "20260712",
            "--to-date",
            "20260726",
            "--result-json",
            str(result_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        quickstart.main()

    assert exc_info.value.code == 1
    assert runner_started is False
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert all(
        value == {
            "status": "not_run",
            "stable_error": "jvlink_com_unavailable",
        }
        for value in result["spec_results"].values()
    )


def test_main_writes_bounded_result_for_runner_outcome(
    tmp_path,
    monkeypatch,
) -> None:
    result_path = tmp_path / "result.json"
    runtime_check = _runtime_ready()
    captured_runtime_check = None

    class FakeLock:
        def __init__(self, _: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class FakeRunner:
        def __init__(
            self,
            settings: dict,
            *,
            jvlink_runtime_check: quickstart.JVLinkRuntimeCheck | None = None,
        ) -> None:
            nonlocal captured_runtime_check
            assert settings["mode"] == "update"
            captured_runtime_check = jvlink_runtime_check
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
        "_check_jvlink_runtime",
        lambda *_: runtime_check,
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
    assert captured_runtime_check is runtime_check
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
        def __init__(
            self,
            settings: dict,
            *,
            jvlink_runtime_check: quickstart.JVLinkRuntimeCheck | None = None,
        ) -> None:
            captured_settings.update(settings)
            self.spec_results = {
                name: {"status": "nodata", "stable_error": None}
                for name in quickstart.BOUNDED_UPDATE_SPEC_NAMES
            }

        def run(self) -> int:
            return 0

    monkeypatch.setattr(quickstart, "ProcessLock", FakeLock)
    monkeypatch.setattr(quickstart, "QuickstartRunner", FakeRunner)
    monkeypatch.setattr(
        quickstart,
        "_check_jvlink_runtime",
        lambda *_: _runtime_ready(),
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
            "20260711",
            "--to-date",
            "20260725",
            "--download-timeout",
            "601",
            "--stall-timeout",
            "299",
            "--jvstatus-max-retries",
            "1",
            "--result-json",
            str(result_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        quickstart.main()

    assert exc_info.value.code == 0
    assert captured_settings["download_timeout"] == 601.0
    assert captured_settings["stall_timeout"] == 299.0
    assert captured_settings["jvstatus_max_retries"] == 1


def test_bounded_race_selects_only_race_option_two() -> None:
    runner = QuickstartRunner(
        {
            "mode": "update",
            "bounded_spec": "RACE",
            "from_date": "20260713",
            "to_date": "20260727",
        }
    )

    assert runner._get_specs_for_mode() == [("RACE", "レース情報", 2)]


def test_main_propagates_bounded_race_selection(
    tmp_path,
    monkeypatch,
) -> None:
    result_path = tmp_path / "result.json"
    trace_path = tmp_path / "diagnostic-trace.json"
    captured_settings: dict = {}

    class FakeLock:
        def __init__(self, _: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class FakeRunner:
        def __init__(
            self,
            settings: dict,
            *,
            jvlink_runtime_check: quickstart.JVLinkRuntimeCheck | None = None,
        ) -> None:
            captured_settings.update(settings)
            self.spec_results = {
                "RACE": {"status": "nodata", "stable_error": None}
            }

        def run(self) -> int:
            return 0

    monkeypatch.setattr(quickstart, "ProcessLock", FakeLock)
    monkeypatch.setattr(quickstart, "QuickstartRunner", FakeRunner)
    monkeypatch.setattr(
        quickstart,
        "_check_jvlink_runtime",
        lambda *_: _runtime_ready(),
    )
    monkeypatch.setattr(
        quickstart.sys,
        "argv",
        [
            "quickstart.py",
            "--yes",
            "--mode",
            "update",
            "--bounded-spec",
            "RACE",
            "--jvstatus-max-retries",
            "1",
            "--from-date",
            "20260713",
            "--to-date",
            "20260727",
            "--result-json",
            str(result_path),
            "--jvlink-diagnostic-trace",
            str(trace_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        quickstart.main()

    assert exc_info.value.code == 0
    assert captured_settings["bounded_spec"] == "RACE"
    assert captured_settings["jvlink_diagnostic_trace"] == str(trace_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["spec_results"]["RACE"]["status"] == "nodata"
    assert all(
        result["spec_results"][name]["status"] == "not_run"
        for name in quickstart.BOUNDED_UPDATE_SPEC_NAMES
        if name != "RACE"
    )


def test_main_rejects_diagnostic_trace_outside_bounded_race(
    tmp_path,
    monkeypatch,
) -> None:
    runner_started = False

    class FakeRunner:
        def __init__(
            self,
            _: dict,
            *,
            jvlink_runtime_check: quickstart.JVLinkRuntimeCheck | None = None,
        ) -> None:
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
            "--jvlink-diagnostic-trace",
            str(tmp_path / "diagnostic-trace.json"),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        quickstart.main()

    assert exc_info.value.code == 2
    assert runner_started is False


def test_main_rejects_bounded_race_without_exact_jvstatus_retry_limit(
    tmp_path,
    monkeypatch,
) -> None:
    result_path = tmp_path / "result.json"
    runner_started = False

    class FakeRunner:
        def __init__(
            self,
            _: dict,
            *,
            jvlink_runtime_check: quickstart.JVLinkRuntimeCheck | None = None,
        ) -> None:
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
            "--bounded-spec",
            "RACE",
            "--from-date",
            "20260713",
            "--to-date",
            "20260727",
            "--result-json",
            str(result_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        quickstart.main()

    assert exc_info.value.code == 2
    assert runner_started is False


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
        def __init__(
            self,
            _: dict,
            *,
            jvlink_runtime_check: quickstart.JVLinkRuntimeCheck | None = None,
        ) -> None:
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
        def __init__(
            self,
            _: dict,
            *,
            jvlink_runtime_check: quickstart.JVLinkRuntimeCheck | None = None,
        ) -> None:
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
        "_check_jvlink_runtime",
        lambda *_: _runtime_ready(),
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
        def __init__(
            self,
            _: dict,
            *,
            jvlink_runtime_check: quickstart.JVLinkRuntimeCheck | None = None,
        ) -> None:
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
