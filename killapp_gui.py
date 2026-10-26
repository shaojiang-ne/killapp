"""killapp 图形界面入口（PyInstaller 的打包入口）。"""

import os
import sys

# --windowed 打包后 stdout/stderr 可能是 None，任何 print 都会抛 AttributeError
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")  # noqa: SIM115
if sys.stderr is None:
    sys.stderr = sys.stdout

# 高 DPI 缩放支持：必须在创建 Tk 根窗口之前设置，否则 125%/150% 缩放下界面发虚
try:
    import ctypes

    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:  # noqa: BLE001 - 非 Windows 或不支持时忽略
    pass

from gui import main  # noqa: E402 - 必须在 DPI 设置之后导入

if __name__ == "__main__":
    main()
