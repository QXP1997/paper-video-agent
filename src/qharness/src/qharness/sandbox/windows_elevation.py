# -*- coding: utf-8 -*-
"""Windows 原生确认窗口和 UAC 提权执行封装。"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SW_SHOWNORMAL = 1
_IDYES = 6
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_ERROR_CANCELLED = 1223


@dataclass(frozen=True, slots=True)
class ElevatedProcessResult:
    """保存用户确认及提权进程的执行结果。"""

    # 用户是否主动拒绝确认或取消 Windows UAC。
    cancelled: bool

    # 提权进程退出码；没有启动或等待超时时为 None。
    exit_code: int | None

    # 面向上层调用方的中文说明。
    message: str


class _ShellExecuteInfo(ctypes.Structure):
    """对应 Win32 SHELLEXECUTEINFOW 结构。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def run_srt_install_with_confirmation(
    helper_path: Path,
    *,
    timeout_seconds: float = 300.0,
) -> ElevatedProcessResult:
    """显示 QHarness 确认窗口，并由 Windows UAC 启动固定的 install 命令。"""

    if os.name != "nt":
        return ElevatedProcessResult(
            cancelled=False,
            exit_code=None,
            message="系统级 SRT 初始化窗口仅适用于 Windows。",
        )
    resolved_helper = helper_path.resolve(strict=False)
    if not resolved_helper.is_file():
        return ElevatedProcessResult(
            cancelled=False,
            exit_code=None,
            message=f"找不到 SRT Windows 初始化程序：{resolved_helper}",
        )
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0。")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    ]
    user32.MessageBoxW.restype = ctypes.c_int
    answer = user32.MessageBoxW(
        None,
        (
            "Anthropic SRT 需要创建一个 Windows 隔离账户并初始化本机权限。\n\n"
            "点击“是”后 Windows 将显示 UAC 确认窗口。这个操作只用于安装或"
            "修复 SRT，不会执行 Agent 生成的命令。"
        ),
        "QHarness 沙箱初始化",
        0x00000004 | 0x00000020 | 0x00000100,
    )
    if answer != _IDYES:
        return ElevatedProcessResult(
            cancelled=True,
            exit_code=None,
            message="用户取消了 SRT 系统初始化。",
        )

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(resolved_helper)
    # 参数固定为 install，绝不接受 Agent、模型或配置文件提供的命令文本。
    info.lpParameters = "install"
    info.lpDirectory = str(resolved_helper.parent)
    info.nShow = _SW_SHOWNORMAL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error_code = ctypes.get_last_error()
        if error_code == _ERROR_CANCELLED:
            return ElevatedProcessResult(
                cancelled=True,
                exit_code=None,
                message="用户取消了 Windows UAC 授权。",
            )
        return ElevatedProcessResult(
            cancelled=False,
            exit_code=None,
            message=f"Windows 无法启动 SRT 初始化程序，错误码：{error_code}。",
        )

    try:
        wait_result = kernel32.WaitForSingleObject(
            info.hProcess,
            min(int(timeout_seconds * 1000), 0xFFFFFFFE),
        )
        if wait_result == _WAIT_TIMEOUT:
            return ElevatedProcessResult(
                cancelled=False,
                exit_code=None,
                message="SRT 初始化仍在运行，等待已超时，请稍后重新检查状态。",
            )
        if wait_result != _WAIT_OBJECT_0:
            return ElevatedProcessResult(
                cancelled=False,
                exit_code=None,
                message=f"等待 SRT 初始化程序失败，状态码：{wait_result}。",
            )
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            error_code = ctypes.get_last_error()
            return ElevatedProcessResult(
                cancelled=False,
                exit_code=None,
                message=f"无法读取 SRT 初始化退出码，错误码：{error_code}。",
            )
        return ElevatedProcessResult(
            cancelled=False,
            exit_code=int(exit_code.value),
            message=(
                "SRT 系统初始化程序已经完成。"
                if exit_code.value == 0
                else f"SRT 系统初始化失败，退出码：{exit_code.value}。"
            ),
        )
    finally:
        if info.hProcess:
            kernel32.CloseHandle(info.hProcess)
