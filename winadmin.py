"""Windows 提权相关工具：以管理员身份重新启动当前程序。"""

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

SW_SHOWNORMAL = 1
SE_ERR_ACCESSDENIED = 5


def is_admin():
    """当前进程是否以管理员身份运行。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _launch_target():
    """返回 ``(可执行文件, 参数列表)``。

    打包成 exe 后 ``sys.executable`` 就是 exe 本身；源码运行时用 ``pythonw.exe``
    重新拉起，避免多出一个黑色控制台窗口。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller onefile：sys.executable 即 killapp.exe
        return sys.executable, list(sys.argv[1:])

    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    args = [script] if script else []
    return exe, args + list(sys.argv[1:])


def relaunch_as_admin():
    """通过 UAC 以管理员身份重新启动本程序。

    返回 ``(是否已启动, 错误信息)``。调用方应在成功后再退出当前实例。
    """
    if is_admin():
        return False, "当前已经是管理员权限"

    exe, args = _launch_target()
    # 用 list2cmdline 做 Windows 命令行转义，手拼字符串遇到带空格的路径会出错
    arg_str = subprocess.list2cmdline(args)

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    ret = shell32.ShellExecuteW(None, "runas", exe, arg_str, None, SW_SHOWNORMAL)

    # ShellExecuteW 返回值 > 32 表示成功，<= 32 是错误码
    if ret <= 32:
        if ret == SE_ERR_ACCESSDENIED:
            return False, "已取消提权，或系统拒绝了管理员启动请求"
        return False, f"以管理员身份启动失败（错误码 {int(ret)}）"
    return True, ""
