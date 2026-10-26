"""Windows 快速进程信息采集。

psutil 在 Windows 上逐个进程调用 ``GetExitCodeProcess`` / ``GetProcessMemoryInfo``
时，每次都要重新打开进程句柄，实测单个进程约 4 毫秒，几百个进程一轮下来要 2 秒以上，
做不了「任务管理器」那种流畅的自动刷新。

这里改用 Win32 API 自己实现：每个进程只 ``OpenProcess`` 一次，然后复用同一个句柄
取内存、CPU 时间、状态和可执行文件路径，单进程耗时降到微秒级。

打不开句柄的系统进程（未提权时）由调用方回退到 psutil。
"""

import ctypes
import os
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
STILL_ACTIVE = 259

# FILETIME 是自 1601-01-01 起的 100 纳秒计数，转 Unix 时间戳要减掉这个偏移
_FILETIME_EPOCH_OFFSET = 11644473600.0

if IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    _kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    _kernel32.OpenProcess.restype = wintypes.HANDLE

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL

    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


class ProcFast:
    """单个进程的快速快照。"""

    __slots__ = ("rss", "vms", "cpu_seconds", "create_time", "status", "exe")

    def __init__(self, rss, vms, cpu_seconds, create_time, status, exe):
        self.rss = rss
        self.vms = vms
        self.cpu_seconds = cpu_seconds
        self.create_time = create_time
        self.status = status
        self.exe = exe


def _filetime_ticks(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _query_image_path(handle):
    """取进程的可执行文件路径，取不到返回空串。"""
    size = wintypes.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(size.value)
    if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return buffer.value
    return ""


def _read_memory(handle):
    """取 (rss, vms)；拿不到时返回 (0, 0)。"""
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
    if _psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return counters.WorkingSetSize, counters.PagefileUsage
    return 0, 0


def read_process(pid):
    """快速读取单个进程信息，失败时返回 ``None``（通常是权限不足或进程已退出）。"""
    if not IS_WINDOWS:
        return None

    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = _FILETIME()
        exited = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not _kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None

        cpu_seconds = (
            _filetime_ticks(kernel) + _filetime_ticks(user)
        ) / 1e7
        create_time = _filetime_ticks(created) / 1e7 - _FILETIME_EPOCH_OFFSET

        code = wintypes.DWORD()
        status = "running"
        if _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            if code.value != STILL_ACTIVE:
                status = "dead"

        rss, vms = _read_memory(handle)
        exe = _query_image_path(handle)
        return ProcFast(rss, vms, cpu_seconds, create_time, status, exe)
    finally:
        _kernel32.CloseHandle(handle)


def is_supported():
    """当前平台是否支持快速采集。"""
    return IS_WINDOWS
