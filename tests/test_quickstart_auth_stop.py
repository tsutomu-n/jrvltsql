"""No-live tests for bounded update error propagation."""

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
