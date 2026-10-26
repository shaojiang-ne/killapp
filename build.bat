@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   killapp 打包成 Windows 桌面应用
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 python，请先安装 Python 3.8+ 并加入 PATH。
    pause
    exit /b 1
)

python build.py %*
if errorlevel 1 (
    echo.
    echo [错误] 构建失败，请检查上面的输出。
    pause
    exit /b 1
)

echo.
echo 双击 dist\killapp.exe 即可使用。
pause
