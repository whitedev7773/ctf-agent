@echo off
setlocal EnableExtensions EnableDelayedExpansion

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

call :find_dashboard_listener
if defined CTF_AGENT_EXISTING_PID (
    set "CTF_AGENT_EXISTING_NAME=unknown"
    for /f "usebackq delims=" %%N in (`powershell.exe -NoProfile -Command "try { (Get-Process -Id %CTF_AGENT_EXISTING_PID% -ErrorAction Stop).ProcessName } catch { 'unknown' }"`) do set "CTF_AGENT_EXISTING_NAME=%%N"

    echo.
    echo [NOTICE] Port %CTF_AGENT_PORT% is already in use.
    echo [Process] !CTF_AGENT_EXISTING_NAME! ^(PID %CTF_AGENT_EXISTING_PID%^)
    choice /C YN /N /M "Stop the existing server and start CTF Agent? [Y/N] "
    if errorlevel 2 (
        echo [CANCELLED] The existing server was left running.
        exit /b 0
    )

    echo [STOP] Stopping PID %CTF_AGENT_EXISTING_PID% and its child processes...
    call :stop_dashboard_listener
    if errorlevel 1 (
        echo [ERROR] Port %CTF_AGENT_PORT% is still in use. The new server was not started.
        pause
        exit /b 1
    )
    echo [STOP] Existing server stopped.
)

echo.
echo [CTF Agent] Starting coordinator...
echo [Dashboard] http://127.0.0.1:%CTF_AGENT_PORT%
echo [Mode] Configure CTFd in the dashboard or use a local challenge.
echo [Stop] Press Ctrl+C in this window.
echo.

".venv\Scripts\python.exe" -m backend.cli --dashboard-port %CTF_AGENT_PORT% --max-challenges %CTF_AGENT_MAX_CHALLENGES% %*
set "CTF_AGENT_EXIT_CODE=%ERRORLEVEL%"

if not "%CTF_AGENT_EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] CTF Agent exited with code %CTF_AGENT_EXIT_CODE%.
    pause
)

exit /b %CTF_AGENT_EXIT_CODE%

:find_dashboard_listener
set "CTF_AGENT_EXISTING_PID="
for /f "usebackq delims=" %%P in (`powershell.exe -NoProfile -Command "$listeners = @(Get-NetTCPConnection -LocalPort %CTF_AGENT_PORT% -State Listen -ErrorAction SilentlyContinue); if ($listeners.Count -gt 0) { $listeners[0].OwningProcess }"`) do set "CTF_AGENT_EXISTING_PID=%%P"
exit /b 0

:stop_dashboard_listener
powershell.exe -NoProfile -Command "$deadline = [DateTime]::UtcNow.AddSeconds(15); while ([DateTime]::UtcNow -lt $deadline) { $listeners = @(Get-NetTCPConnection -LocalPort %CTF_AGENT_PORT% -State Listen -ErrorAction SilentlyContinue); if ($listeners.Count -eq 0) { Start-Sleep -Milliseconds 400; $listeners = @(Get-NetTCPConnection -LocalPort %CTF_AGENT_PORT% -State Listen -ErrorAction SilentlyContinue); if ($listeners.Count -eq 0) { exit 0 } }; foreach ($listener in $listeners) { $ownerId = [int]$listener.OwningProcess; Start-Process -FilePath taskkill.exe -ArgumentList '/PID', $ownerId, '/T', '/F' -WindowStyle Hidden -Wait }; Start-Sleep -Milliseconds 250 }; exit 1"
exit /b %ERRORLEVEL%
