"""Fixture tests for API-specific JV-Link error classification."""

from unittest.mock import MagicMock, patch

import pytest

from src.fetcher.base import FetcherError
from src.fetcher.historical import HistoricalFetcher
from src.jvlink.error_codes import describe_jvlink_error
from src.jvlink.wrapper import JVLinkError, JVLinkWrapper


def test_jvopen_302_is_terminal_auth_expired() -> None:
    descriptor = describe_jvlink_error("JVOpen", -302)

    assert descriptor.category == "auth_expired"
    assert descriptor.stable_error == "jvopen_auth_expired"
    assert descriptor.retryable is False
    assert descriptor.terminal is True
    assert "有効期限" in descriptor.message


@pytest.mark.parametrize(
    ("code", "category", "stable_error"),
    [
        (-301, "auth_invalid", "jvopen_auth_invalid"),
        (-302, "auth_expired", "jvopen_auth_expired"),
        (-303, "auth_not_set", "jvopen_auth_not_set"),
    ],
)
def test_all_jvopen_auth_errors_are_terminal(
    code: int,
    category: str,
    stable_error: str,
) -> None:
    descriptor = describe_jvlink_error("JVOpen", code)

    assert descriptor.category == category
    assert descriptor.stable_error == stable_error
    assert descriptor.retryable is False
    assert descriptor.terminal is True


def test_jvrtopen_302_has_api_specific_stable_error() -> None:
    descriptor = describe_jvlink_error("JVRTOpen", -302)

    assert descriptor.category == "auth_expired"
    assert descriptor.stable_error == "jvrtopen_auth_expired"
    assert descriptor.retryable is False


def test_open_202_is_not_retryable() -> None:
    descriptor = describe_jvlink_error("JVOpen", -202)

    assert descriptor.category == "open_not_closed"
    assert descriptor.stable_error == "jvopen_open_not_closed"
    assert descriptor.retryable is False
    assert descriptor.terminal is True


def test_502_is_download_failure_only_for_download_apis() -> None:
    status = describe_jvlink_error("JVStatus", -502)
    read = describe_jvlink_error("JVRead", -502)
    opened = describe_jvlink_error("JVOpen", -502)

    assert status.category == "download_failed"
    assert status.retryable is True
    assert read.category == "download_failed"
    assert read.retryable is True
    assert opened.category == "unknown"
    assert opened.retryable is False


def test_minus_one_meaning_depends_on_api() -> None:
    assert describe_jvlink_error("JVOpen", -1).category == "no_data"
    assert describe_jvlink_error("JVRead", -1).category == "file_switch"


@patch("win32com.client.Dispatch")
def test_wrapper_preserves_jvopen_auth_expired(mock_dispatch: MagicMock) -> None:
    mock_com = MagicMock()
    mock_com.JVOpen.return_value = (-302, 0, 0, "")
    mock_dispatch.return_value = mock_com
    wrapper = JVLinkWrapper(sid="TEST")

    with pytest.raises(JVLinkError) as exc_info:
        wrapper.jv_open("RACE", "20260710000000", option=2)

    error = exc_info.value
    assert error.api == "JVOpen"
    assert error.error_code == -302
    assert error.category == "auth_expired"
    assert error.stable_error == "jvopen_auth_expired"
    assert error.retryable is False
    assert error.terminal is True
    assert "ダウンロード待ち" not in str(error)


@patch("win32com.client.Dispatch")
def test_jvopen_minus_two_is_setup_cancelled_not_no_data(
    mock_dispatch: MagicMock,
) -> None:
    mock_com = MagicMock()
    mock_com.JVOpen.return_value = (-2, 0, 0, "")
    mock_dispatch.return_value = mock_com
    wrapper = JVLinkWrapper(sid="TEST")

    with pytest.raises(JVLinkError) as exc_info:
        wrapper.jv_open("RACE", "20260710000000", option=2)

    assert exc_info.value.category == "setup_cancelled"
    assert exc_info.value.stable_error == "jvopen_setup_cancelled"


def test_jvstatus_download_failure_retries_then_preserves_error(monkeypatch) -> None:
    with patch("win32com.client.Dispatch", return_value=MagicMock()):
        fetcher = HistoricalFetcher(show_progress=False)
    fetcher.jvlink = MagicMock()
    fetcher.jvlink.jv_status.side_effect = [-502, -502, -502]
    monkeypatch.setattr("src.fetcher.historical.time.sleep", lambda _: None)

    with pytest.raises(FetcherError) as exc_info:
        fetcher._wait_for_download(download_count=1, timeout=1, interval=0)

    error = exc_info.value
    assert fetcher.jvlink.jv_status.call_count == 3
    assert error.category == "download_failed"
    assert error.stable_error == "jvstatus_download_failed"
    assert error.retryable is True


def test_jvstatus_open_not_called_does_not_retry(monkeypatch) -> None:
    with patch("win32com.client.Dispatch", return_value=MagicMock()):
        fetcher = HistoricalFetcher(show_progress=False)
    fetcher.jvlink = MagicMock()
    fetcher.jvlink.jv_status.return_value = -203
    monkeypatch.setattr("src.fetcher.historical.time.sleep", lambda _: None)

    with pytest.raises(FetcherError) as exc_info:
        fetcher._wait_for_download(download_count=1, timeout=1, interval=0)

    assert fetcher.jvlink.jv_status.call_count == 1
    assert exc_info.value.category == "open_not_called"
    assert exc_info.value.retryable is False
