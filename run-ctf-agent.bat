@echo off
setlocal

cd /d "%~dp0"

if not defined CTF_AGENT_PORT set "CTF_AGENT_PORT=9400"
if not defined CTF_AGENT_MAX_CHALLENGES set "CTF_AGENT_MAX_CHALLENGES=3"

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv was not found in PATH.
    echo Install uv, reopen this terminal, and run this file again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] Creating the project environment...
    uv sync
    if errorlevel 1 (
        echo [ERROR] uv sync failed.
        pause
        exit /b 1
    )
)

echo.
echo [CTF Agent] Starting coordinator...
echo [Dashboard] http://127.0.0.1:%CTF_AGENT_PORT%
echo [Mode] Configure CTFd in the dashboard or use a local challenge.
echo [Stop] Press Ctrl+C in this window.
echo.

uv run ctf-solve --dashboard-port %CTF_AGENT_PORT% --max-challenges %CTF_AGENT_MAX_CHALLENGES% %*
set "CTF_AGENT_EXIT_CODE=%ERRORLEVEL%"

if not "%CTF_AGENT_EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] CTF Agent exited with code %CTF_AGENT_EXIT_CODE%.
    pause
)

exit /b %CTF_AGENT_EXIT_CODE%
