@echo off
chcp 65001 >nul
title Easy Windows Tools

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "easy_windows_tools.py"
) else (
    python "easy_windows_tools.py"
)
pause
