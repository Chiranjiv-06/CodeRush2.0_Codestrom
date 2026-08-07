@echo off
setlocal EnableDelayedExpansion

title M2X Compute ^& Tool Exchange -- Launcher
color 0B

echo.
echo  =====================================================================
echo   M2X Compute ^& Tool Exchange  --  Launcher v3.0
echo  =====================================================================
echo.

:: ── Locate project root ─────────────────────────────────────────────────────
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "BACKEND=%ROOT%\backend"
set "VENV=%ROOT%\.venv"
set "ENV_FILE=%ROOT%\.env"
set "STATIC_DIR=%BACKEND%\app\static"
set "STANDALONE_SRC=%ROOT%\standalone_app.html"
set "STANDALONE_DEST=%STATIC_DIR%\standalone.html"
set "DASHBOARD_DEST=%STATIC_DIR%\dashboard.html"
set "PORT=8000"

echo  [INFO] Project root: %ROOT%
echo.

:: ── Copy .env from example if missing ───────────────────────────────────────
if not exist "%ENV_FILE%" (
    if exist "%ROOT%\.env.example" (
        echo  [INFO] .env not found -- copying from .env.example
        copy /Y "%ROOT%\.env.example" "%ENV_FILE%" >nul
        echo  [OK]   .env created.
    ) else (
        echo  [WARN] No .env or .env.example found -- using defaults.
    )
)

:: ── Ensure static directory exists ──────────────────────────────────────────
if not exist "%STATIC_DIR%" (
    echo  [INFO] Creating static directory...
    mkdir "%STATIC_DIR%"
    echo  [OK]   Static directory created.
)

:: ── Sync standalone_app.html into static/ (always overwrite to stay current) ─
if exist "%STANDALONE_SRC%" (
    copy /Y "%STANDALONE_SRC%" "%STANDALONE_DEST%" >nul
    echo  [OK]  standalone_app.html synced to static\standalone.html
    copy /Y "%STANDALONE_SRC%" "%DASHBOARD_DEST%" >nul
    echo  [OK]  dashboard.html synced from standalone_app.html
) else (
    echo  [WARN] standalone_app.html not found at project root.
)

echo.

:: ── Detect Python / uv ──────────────────────────────────────────────────────
set "PYTHON="
set "USE_UV=0"

where uv >nul 2>&1
if !errorlevel! == 0 (
    set "USE_UV=1"
    echo  [OK]  uv found -- using uv for fast dependency management
)

:: Also find system Python (needed as fallback even when uv is present)
for %%P in (python python3 py) do (
    if "!PYTHON!"=="" (
        where %%P >nul 2>&1
        if !errorlevel! == 0 set "PYTHON=%%P"
    )
)

if "!PYTHON!"=="" (
    for %%D in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
    ) do (
        if exist %%D (
            set "PYTHON=%%~D"
            goto :found_python
        )
    )
)

:found_python
if "!PYTHON!" neq "" (
    echo  [OK]  Python found: !PYTHON!
) else if "!USE_UV!"=="0" (
    echo  [ERROR] Python 3.10+ not found.
    echo          Install from https://www.python.org/downloads/
    echo          Tick "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

:: ── Validate or create virtual environment ──────────────────────────────────
:: The key fix: check that the venv Python actually RUNS, not just that the
:: file exists. Venvs copied from another machine or user will have a broken
:: python.exe trampoline pointing to a non-existent interpreter.
set "PY=%VENV%\Scripts\python.exe"
set "NEED_VENV=0"

if not exist "%VENV%" (
    set "NEED_VENV=1"
    echo  [INFO] No virtual environment found.
) else if not exist "%PY%" (
    set "NEED_VENV=1"
    echo  [WARN] venv exists but python.exe is missing.
) else (
    :: Smoke-test: can the venv python actually execute?
    "%PY%" -c "import sys; sys.exit(0)" >nul 2>&1
    if !errorlevel! neq 0 (
        set "NEED_VENV=1"
        echo  [WARN] venv python is broken (copied from another machine?) -- recreating...
    ) else (
        echo  [OK]  Existing virtual environment is healthy.
    )
)

if "!NEED_VENV!"=="1" (
    :: Remove broken venv directory if it exists
    if exist "%VENV%" (
        echo  [INFO] Removing broken virtual environment...
        rmdir /S /Q "%VENV%" >nul 2>&1
    )

    if "!USE_UV!"=="1" (
        echo  [INFO] Creating virtual environment with uv...
        uv venv "%VENV%" --python 3.11 >nul 2>&1
        if !errorlevel! neq 0 (
            echo  [WARN] uv venv --python 3.11 failed, trying default...
            uv venv "%VENV%" >nul 2>&1
            if !errorlevel! neq 0 (
                echo  [WARN] uv venv failed, falling back to system Python...
                if "!PYTHON!"=="" (
                    echo  [ERROR] No Python found to create venv. Install Python 3.10+.
                    pause
                    exit /b 1
                )
                "!PYTHON!" -m venv "%VENV%"
                if !errorlevel! neq 0 (
                    echo  [ERROR] Failed to create virtual environment.
                    pause
                    exit /b 1
                )
            )
        )
    ) else (
        echo  [INFO] Creating virtual environment with !PYTHON!...
        "!PYTHON!" -m venv "%VENV%"
        if !errorlevel! neq 0 (
            echo  [ERROR] Failed to create virtual environment.
            pause
            exit /b 1
        )
    )
    echo  [OK]  Virtual environment created.
)

set "PY=%VENV%\Scripts\python.exe"
echo  [OK]  Virtual environment: %PY%
echo.

:: ── Install dependencies (staged: core → algorand → optional → test) ────────
:: Install in stages so a failure in optional packages (psycopg, redis, minio,
:: langgraph) does not block the core platform from running.
echo  [INFO] Installing/checking dependencies...
echo         (May take up to 90s on first run)
echo.

:: STAGE 1: Core API packages (must succeed)
echo  [1/4] Core API packages...
if "!USE_UV!"=="1" (
    uv pip install --quiet --python "%PY%" fastapi "uvicorn[standard]" pydantic pydantic-settings python-multipart httpx SQLAlchemy PyJWT prometheus-client >nul 2>&1
) else (
    "%PY%" -m pip install --quiet --upgrade pip >nul 2>&1
    "%PY%" -m pip install --quiet fastapi "uvicorn[standard]" pydantic pydantic-settings python-multipart httpx SQLAlchemy PyJWT prometheus-client >nul 2>&1
)
if !errorlevel! neq 0 (
    echo  [ERROR] Core packages failed to install. Check your internet connection.
    pause
    exit /b 1
)
echo  [OK]  Core API packages installed.

:: STAGE 2: Algorand / AlgoKit (should succeed, but non-fatal)
echo  [2/4] Algorand SDK packages...
if "!USE_UV!"=="1" (
    uv pip install --quiet --python "%PY%" py-algorand-sdk algokit-utils >nul 2>&1
) else (
    "%PY%" -m pip install --quiet py-algorand-sdk algokit-utils >nul 2>&1
)
if !errorlevel! neq 0 (
    echo  [WARN] Algorand SDK install failed (exchange will use stub data).
) else (
    echo  [OK]  Algorand SDK installed.
)

:: STAGE 3: Optional infrastructure packages (non-fatal on Windows)
echo  [3/4] Optional packages (redis, minio)...
if "!USE_UV!"=="1" (
    uv pip install --quiet --python "%PY%" redis minio >nul 2>&1
) else (
    "%PY%" -m pip install --quiet redis minio >nul 2>&1
)
if !errorlevel! neq 0 (
    echo  [WARN] Some optional packages skipped (local fallbacks will be used).
) else (
    echo  [OK]  Optional packages installed.
)

:: STAGE 4: Test packages (non-fatal)
echo  [4/4] Test packages...
if "!USE_UV!"=="1" (
    uv pip install --quiet --python "%PY%" pytest pytest-asyncio >nul 2>&1
) else (
    "%PY%" -m pip install --quiet pytest pytest-asyncio >nul 2>&1
)
if !errorlevel! neq 0 (
    echo  [WARN] Test packages skipped (not needed for running the app).
) else (
    echo  [OK]  Test packages installed.
)

echo.
echo  [OK]  All dependencies ready.
echo.

:: ── Verify app can import ───────────────────────────────────────────────────
echo  [INFO] Verifying application imports...
"%PY%" -c "from app.main import app; print('  [OK]  Application imports verified.')" 2>nul
if !errorlevel! neq 0 (
    echo  [WARN] Application import check failed. Server may still start with warnings.
)
echo.

:: ── Kill any existing process on port 8000 ──────────────────────────────────
netstat -ano 2>nul | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if !errorlevel! == 0 (
    echo  [INFO] Port %PORT% already in use -- freeing it...
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
        echo  [INFO]   Killing PID %%P
        taskkill /F /PID %%P >nul 2>&1
    )
    timeout /t 2 /nobreak >nul
    echo  [OK]  Port %PORT% freed.
    echo.
)

:: ── Start FastAPI server in a new console window ─────────────────────────────
echo  [INFO] Starting M2X API server on http://localhost:%PORT% ...

start "M2X API Server" /D "%BACKEND%" cmd /k ""%PY%" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT% --reload --log-level info"

echo  [INFO] Waiting for server to become healthy (up to 60s)...
echo.

:: ── Health check using PowerShell (reliable on all Windows versions) ─────────
set "READY=0"
set "TRIES=0"
set "MAX=30"

:wait_loop
timeout /t 2 /nobreak >nul
set /a TRIES+=1
echo  [....] Health check attempt !TRIES!/%MAX%...

powershell -NoProfile -NonInteractive -Command ^
  "try{$r=(Invoke-WebRequest -Uri 'http://localhost:%PORT%/health' -UseBasicParsing -TimeoutSec 2).StatusCode;if($r -ge 200 -and $r -lt 400){exit 0}else{exit 1}}catch{exit 1}" >nul 2>&1
if !errorlevel! == 0 (
    set "READY=1"
    goto :server_ready
)

if !TRIES! lss !MAX! goto :wait_loop

echo.
echo  [WARN] Server did not respond within 60 seconds.
echo         Check the server window for startup errors.

:server_ready
echo.
if "!READY!"=="1" (
    echo  [OK]  Server is live and healthy!
) else (
    echo  [INFO] Opening browser anyway (server may still be starting)...
)

timeout /t 1 /nobreak >nul

:: ── Open browser ─────────────────────────────────────────────────────────────
echo.
echo  =====================================================================
echo   Opening M2X Dashboard in your browser
echo  =====================================================================
echo.

start "" "http://localhost:%PORT%/dashboard"

echo  [OK]  Browser launched!
echo.
echo  Available URLs:
echo    Dashboard  :  http://localhost:%PORT%/dashboard
echo    Standalone :  http://localhost:%PORT%/standalone
echo    API Docs   :  http://localhost:%PORT%/docs
echo    ReDoc      :  http://localhost:%PORT%/redoc
echo    Health     :  http://localhost:%PORT%/health
echo    AlgoKit    :  http://localhost:%PORT%/v1/algokit/status
echo    VibeKit    :  http://localhost:%PORT%/v1/vibekit/info
echo.
echo  Keep the "M2X API Server" console window open while using the app.
echo  Press Ctrl+C in that window to stop the server.
echo.
pause
endlocal
