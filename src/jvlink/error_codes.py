"""API-specific JV-Link return-code classification.

JV-Link reuses numeric return codes across APIs.  Callers must therefore
classify a code together with the API that returned it instead of relying on a
single global message table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JVLinkErrorDescriptor:
    """Stable classification for one API return code."""

    api: str
    code: int
    category: str
    stable_error: str
    message: str
    retryable: bool = False
    terminal: bool = False


def _error(
    api: str,
    code: int,
    category: str,
    stable_error: str,
    message: str,
    *,
    retryable: bool = False,
    terminal: bool = False,
) -> JVLinkErrorDescriptor:
    return JVLinkErrorDescriptor(
        api=api,
        code=code,
        category=category,
        stable_error=stable_error,
        message=message,
        retryable=retryable,
        terminal=terminal,
    )


_OPEN_COMMON = {
    -1: ("no_data", "no_data", "該当データ無し", False, False),
    -2: (
        "setup_cancelled",
        "setup_cancelled",
        "セットアップダイアログでキャンセルされました",
        False,
        True,
    ),
    -202: (
        "open_not_closed",
        "open_not_closed",
        "前回のJVOpen/JVRTOpen/JVMVOpenがJVCloseされていません",
        False,
        True,
    ),
    -301: (
        "auth_invalid",
        "auth_invalid",
        "利用キーの認証に失敗しました",
        False,
        True,
    ),
    -302: (
        "auth_expired",
        "auth_expired",
        "利用キーの有効期限が切れています",
        False,
        True,
    ),
    -303: (
        "auth_not_set",
        "auth_not_set",
        "利用キーが設定されていません",
        False,
        True,
    ),
}


def _open_errors(api: str, prefix: str) -> dict[int, JVLinkErrorDescriptor]:
    return {
        code: _error(
            api,
            code,
            category,
            f"{prefix}_{suffix}",
            message,
            retryable=retryable,
            terminal=terminal,
        )
        for code, (category, suffix, message, retryable, terminal) in _OPEN_COMMON.items()
    }


_API_ERRORS: dict[str, dict[int, JVLinkErrorDescriptor]] = {
    "JVInit": {
        -101: _error(
            "JVInit",
            -101,
            "sid_missing",
            "jvinit_sid_missing",
            "sidが設定されていません",
            terminal=True,
        ),
        -102: _error(
            "JVInit",
            -102,
            "sid_too_long",
            "jvinit_sid_too_long",
            "sidが64byteを超えています",
            terminal=True,
        ),
        -103: _error(
            "JVInit",
            -103,
            "sid_invalid",
            "jvinit_sid_invalid",
            "sidが不正です",
            terminal=True,
        ),
    },
    "JVOpen": _open_errors("JVOpen", "jvopen"),
    "JVRTOpen": _open_errors("JVRTOpen", "jvrtopen"),
    "JVStatus": {
        -201: _error(
            "JVStatus",
            -201,
            "not_initialized",
            "jvstatus_not_initialized",
            "JVInitが行われていません",
            terminal=True,
        ),
        -203: _error(
            "JVStatus",
            -203,
            "open_not_called",
            "jvstatus_open_not_called",
            "JVOpenが行われていません",
            terminal=True,
        ),
        -502: _error(
            "JVStatus",
            -502,
            "download_failed",
            "jvstatus_download_failed",
            "ダウンロードに失敗しました",
            retryable=True,
        ),
    },
    "JVRead": {
        -1: _error(
            "JVRead",
            -1,
            "file_switch",
            "jvread_file_switch",
            "物理ファイルの終端です",
        ),
        -3: _error(
            "JVRead",
            -3,
            "downloading",
            "jvread_downloading",
            "ファイルをダウンロード中です",
            retryable=True,
        ),
        -201: _error(
            "JVRead",
            -201,
            "not_initialized",
            "jvread_not_initialized",
            "JVInitが行われていません",
            terminal=True,
        ),
        -202: _error(
            "JVRead",
            -202,
            "open_not_closed",
            "jvread_open_not_closed",
            "前回のopenがJVCloseされていません",
            terminal=True,
        ),
        -203: _error(
            "JVRead",
            -203,
            "open_not_called",
            "jvread_open_not_called",
            "JVOpenが行われていません",
            terminal=True,
        ),
        -402: _error(
            "JVRead",
            -402,
            "download_file_invalid",
            "jvread_download_file_empty",
            "ダウンロードしたファイルのサイズが0です",
            retryable=True,
        ),
        -403: _error(
            "JVRead",
            -403,
            "download_file_invalid",
            "jvread_download_file_invalid",
            "ダウンロードしたファイルの内容が不正です",
            retryable=True,
        ),
        -502: _error(
            "JVRead",
            -502,
            "download_failed",
            "jvread_download_failed",
            "ダウンロードに失敗しました",
            retryable=True,
        ),
        -503: _error(
            "JVRead",
            -503,
            "file_missing",
            "jvread_file_missing",
            "読み出すファイルが見つかりません",
            retryable=True,
        ),
    },
}
_API_ERRORS["JVGets"] = {
    code: _error(
        "JVGets",
        code,
        descriptor.category,
        descriptor.stable_error.replace("jvread_", "jvgets_"),
        descriptor.message,
        retryable=descriptor.retryable,
        terminal=descriptor.terminal,
    )
    for code, descriptor in _API_ERRORS["JVRead"].items()
}


def describe_jvlink_error(api: str, code: int) -> JVLinkErrorDescriptor:
    """Return an API-specific descriptor, including a stable fallback."""

    descriptor = _API_ERRORS.get(api, {}).get(code)
    if descriptor is not None:
        return descriptor
    prefix = "".join(character.lower() for character in api if character.isalnum())
    return _error(
        api,
        code,
        "unknown",
        f"{prefix}_error_{abs(code)}",
        f"{api}の不明なエラーコードです",
    )
