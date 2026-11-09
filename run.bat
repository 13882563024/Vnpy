@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 虚拟环境不存在，请先按 README.md 的「安装」步骤操作。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py
if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出，请检查上方报错信息。
    pause
)
