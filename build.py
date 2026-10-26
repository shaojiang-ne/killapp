"""一键构建 killapp.exe。

用法：
    python build.py            # 构建
    python build.py --run      # 构建后顺便启动
    python build.py --no-venv  # 用当前解释器打包（快，但 exe 会比较大）

会在项目下建一个干净的 .venv 再打包 —— 直接用 Anaconda 打包会把 numpy/pandas
这些依赖一起拖进 exe，体积能到几百 MB。
"""

import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PY = VENV / "Scripts" / "python.exe"

# 这些包用不到，排除掉可以显著减小体积
EXCLUDES = [
    "numpy", "pandas", "matplotlib", "PIL", "IPython", "pytest",
    "tkinter.test", "unittest", "pydoc_data", "setuptools", "pip",
    "notebook", "IPython.core", "sqlalchemy", "boto3",
]


def log(msg):
    print(f"[build] {msg}", flush=True)


def run(cmd, **kwargs):
    printable = " ".join(str(c) for c in cmd)
    log(f"$ {printable}")
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def ensure_venv():
    """创建（或复用）干净的虚拟环境并装好依赖。"""
    if not VENV_PY.exists():
        log("创建虚拟环境 .venv ...")
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
    run([VENV_PY, "-m", "pip", "install", "--upgrade", "--quiet", "pip"])
    run([
        VENV_PY, "-m", "pip", "install", "--quiet",
        "-r", ROOT / "requirements.txt",
        "-r", ROOT / "requirements-build.txt",
    ])


def close_running():
    """关掉正在运行的 killapp.exe，否则 dist/killapp.exe 被占用无法覆盖。"""
    if os.name != "nt":
        return
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq killapp.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="ignore", timeout=15,
        ).stdout
    except Exception:
        return
    if "killapp.exe" not in out:
        return
    log("检测到正在运行的 killapp.exe，先结束它 ...")
    subprocess.run(["taskkill", "/F", "/IM", "killapp.exe"],
                   capture_output=True, errors="ignore")
    time.sleep(1.0)


def clean():
    for name in ("__pycache__", "build", "dist"):
        path = ROOT / name
        if path.exists():
            log(f"清理 {name}")
            shutil.rmtree(path, ignore_errors=True)
    for spec in ROOT.glob("*.spec"):
        spec.unlink()


def build(python):
    icon = ROOT / "assets" / "killapp.ico"
    icon_png = ROOT / "assets" / "icon.png"
    if not icon.exists():
        log("警告：缺少 assets/killapp.ico，将使用默认图标")

    cmd = [
        python, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",          # 单文件 exe
        "--windowed",         # 不弹控制台窗口
        "--name", "killapp",
        "--hidden-import", "psutil",
    ]
    if icon.exists():
        cmd += ["--icon", str(icon)]
    if icon_png.exists():
        cmd += ["--add-data", f"{icon_png};assets"]
    for module in EXCLUDES:
        cmd += ["--exclude-module", module]
    cmd.append(str(ROOT / "killapp_gui.py"))

    run(cmd, cwd=ROOT)


def main():
    args = sys.argv[1:]
    use_venv = "--no-venv" not in args

    if use_venv:
        ensure_venv()
        python = VENV_PY
    else:
        python = sys.executable

    close_running()
    clean()
    build(python)

    exe = ROOT / "dist" / "killapp.exe"
    if not exe.exists():
        log("构建失败：没有生成 dist/killapp.exe")
        return 1

    size_mb = exe.stat().st_size / 1024 / 1024
    log(f"构建完成: {exe}  ({size_mb:.1f} MB)")
    log("双击该文件即可启动；遇到权限不足可在界面里点「管理员重启」。")

    if "--run" in args:
        subprocess.Popen([str(exe)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
