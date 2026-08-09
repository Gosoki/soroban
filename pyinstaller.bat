@echo off
echo ========================================
echo   soroban one-click build (PyInstaller)
echo ========================================

rem ===== Version (edit this on each release) =====
rem VERSION is a LABEL only -- it is printed and stamped, it does NOT pick a directory.
set VERSION=v1.0.0

set ROOT=%~dp0
set BACKEND=%ROOT%backend
set FRONTEND=%ROOT%frontend
rem The release dir is FIXED, deliberately WITHOUT the version in its name.
rem It doubles as the RUNTIME DATA dir: soroban.db, .env (holding SECRET_KEY) and the
rem plugins\ venvs + login sessions all live next to the exe (run.py chdir's to itself).
rem While this was Releases\%VERSION%, bumping VERSION produced a brand-new EMPTY folder:
rem the new exe created a fresh empty ledger and a NEW SECRET_KEY on first run, and the
rem user read that as "the upgrade ate my data". The data was never lost -- it was sitting
rem in the previous version's folder -- but nothing in the flow said so.
rem A fixed dir removes the failure instead of warning about it: rebuilding only replaces
rem soroban.exe and leaves the ledger exactly where it was.
set RELEASE=%ROOT%Releases\soroban
set VENV_PY=%BACKEND%\.venv\Scripts\python.exe

rem ===== Pick Python: prefer backend venv (start.bat creates it), else system =====
rem NOTE: always call PyInstaller via "python -m PyInstaller" (NOT bare "pyinstaller").
rem This script is named pyinstaller.bat; cmd would resolve "pyinstaller" to THIS file
rem (current dir before PATH) and recurse into an infinite loop.
echo.
echo [0/3] Locating Python ...
rem NOTE: %errorlevel% inside a parenthesized block is expanded when the block is PARSED,
rem not when the commands inside it run -- so `where python & if %errorlevel% neq 0` in one
rem block always tests the errorlevel from BEFORE the block. Keep these checks flat (no
rem parentheses around them) so each `if errorlevel` sees the real, current value.
if exist "%VENV_PY%" goto have_venv_py
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python not found and backend\.venv missing.
    echo        Run start.bat once to create the venv, or install Python 3.11+.
    pause
    exit /b 1
)
set "PY=python"
echo Using system python ^(backend\.venv not found; run start.bat first for a clean env^)
goto py_ready
:have_venv_py
set "PY=%VENV_PY%"
echo Using backend venv: %VENV_PY%
:py_ready

rem ===== Ensure backend deps + pyinstaller are available in the chosen interpreter =====
rem All the checks below are FLAT (no parentheses around the `if errorlevel`), for the reason
rem spelled out at the top: inside a block, %errorlevel% is expanded at parse time and would
rem always read the value from before the block -- i.e. these install failures would go unnoticed.
rem The sentinel must cover the OCR deps too. collect_data_files/collect_submodules only
rem WARN when a package is missing and return an empty list, so PyInstaller happily ships an
rem exe whose OCR is dead -- a failure that only shows up on the user's machine.
rem (soroban.spec now hard-fails on this as well; this check just fixes it earlier.)
"%PY%" -c "import fastapi, uvicorn, sqlmodel, alembic, rapidocr_onnxruntime, PIL" >nul 2>&1
if not errorlevel 1 goto backend_deps_ok
echo Backend deps missing, installing from requirements.txt ...
"%PY%" -m pip install -r "%BACKEND%\requirements.txt"
if errorlevel 1 goto deps_fail
:backend_deps_ok

"%PY%" -c "import PyInstaller" >nul 2>&1
if not errorlevel 1 goto pyinstaller_ok
echo pyinstaller not found, installing...
"%PY%" -m pip install pyinstaller
if errorlevel 1 goto pyinstaller_fail
:pyinstaller_ok

rem ===== Prepare release directory =====
echo.
echo [1/3] Preparing release dir %RELEASE% ...
rem NEVER wipe %RELEASE%: it doubles as the RUNTIME data dir. The exe is portable by
rem design (run.py chdir's to its own folder), so soroban.db, .env with the SECRET_KEY,
rem and plugins\ venvs + login sessions all live right here. VERSION is a hand-edited
rem constant, so "fix a bug, rebuild" would silently delete the whole ledger -- no
rem prompt, no recycle bin, and backup.sh only covers backend\soroban.db.
rem mkdir is idempotent below; onefile emits a single soroban.exe, so there are no
rem stale artefacts that would need cleaning anyway.
if not exist "%RELEASE%" mkdir "%RELEASE%"
if exist "%ROOT%build" rmdir /s /q "%ROOT%build"

rem ===== Build frontend =====
echo.
echo [2/3] Building frontend ...
where npm >nul 2>&1
if errorlevel 1 goto no_npm
pushd "%FRONTEND%"
if exist "node_modules" goto npm_deps_ok
echo Installing frontend deps...
call npm install
if errorlevel 1 goto npm_install_fail
:npm_deps_ok
call npm run build
if errorlevel 1 goto npm_build_fail
popd
if not exist "%FRONTEND%\dist\index.html" (
    echo ERROR: frontend\dist\index.html not found, frontend build may have failed
    pause
    exit /b 1
)

rem ===== Build main program soroban.exe =====
echo.
echo [3/3] Building soroban.exe (console; frontend + Alembic migrations bundled in) ...
if not exist "%ROOT%soroban.spec" (
    echo ERROR: soroban.spec not found next to this script.
    echo        It is hand-written and MUST be committed; the stock Python .gitignore
    echo        ignores *.spec, so it can silently go missing on a fresh clone.
    echo        See .gitignore for the "!soroban.spec" un-ignore line.
    pause
    exit /b 1
)
rem Build into build\dist first, copy into the release dir only on success. Keeps the
rem whole thing near-atomic: if PyInstaller (or the npm build above) fails, the working
rem exe and the data already sitting in %RELEASE% are untouched.
"%PY%" -m PyInstaller --clean --noconfirm "%ROOT%soroban.spec" ^
    --distpath "%ROOT%build\dist" --workpath "%ROOT%build\work"
if errorlevel 1 goto build_fail

copy /y "%ROOT%build\dist\soroban.exe" "%RELEASE%\soroban.exe" >nul
if errorlevel 1 goto copy_fail

rem frontend\dist, alembic migrations AND the plugins are all bundled INTO soroban.exe
rem (see soroban.spec). On first run soroban releases the plugins next to the exe --
rem they need a writable home for their own .venv / .state / params, and the onefile
rem _MEIPASS dir is wiped on every exit.
rem soroban.spec strips .state\ .env *.log .venv\ __pycache__\ from the bundle: those
rem are the packager's OWN credentials and machine-specific paths. Once inside the exe
rem they could not be deleted afterwards, so the stripping is not optional.

rem ===== Clean build intermediates =====
if exist "%ROOT%build" rmdir /s /q "%ROOT%build"

rem ===== Tell the packager what happened to their plugin credentials =====
rem The repo's development plugins folder contains .state\*.json (the packager's own
rem Taobao LOGIN SESSION), .env and scrape.log. soroban.spec excludes those from the
rem bundle -- but say so out loud, because "my cookies are inside the exe I just built"
rem is exactly the kind of thing nobody thinks to check, and inside an exe it cannot
rem be deleted afterwards. Print it only when the artefacts actually exist.
set "PLUGSECRET="
if exist "%ROOT%plugins" for /d %%P in ("%ROOT%plugins\*") do (
    if exist "%%P\.state" set "PLUGSECRET=1"
    if exist "%%P\.env" set "PLUGSECRET=1"
)
if not defined PLUGSECRET goto plugsecret_ok
echo.
echo ========================================
echo   [i] Your plugins folder holds YOUR OWN credentials:
echo       .state\*.json   = your logged-in browser session (Taobao cookies)
echo       .env            = your own soroban account / API keys
echo   These were EXCLUDED from soroban.exe (see _PLUG_SKIP_* in soroban.spec),
echo   together with .venv\ (machine-specific) and __pycache__\.
echo   The exe carries plugin SOURCE only; users get a clean copy on first run.
echo   [!] DO NOT SHIP plugins\ AS-IS if you copy the folder by hand anyway --
echo       DELETE .state\ .env *.log .venv\ from each copy first.
echo ========================================
:plugsecret_ok

rem ===== Warn when the ledger is left behind in an older versioned folder =====
rem The release dir is fixed now, so this can no longer happen on a rebuild. But the
rem builds made BEFORE that change wrote into Releases\<VERSION>\ -- so the very first
rem build after the switch lands in an empty Releases\soroban\ while the real ledger
rem still sits in the old versioned folder. Running the new exe as-is would create an
rem empty ledger and a NEW SECRET_KEY, and that reads exactly like "the upgrade ate my
rem data". One-time migration, but nothing else says it, so say it here.
if exist "%RELEASE%\soroban.db" goto data_ok
set "OLDDATA="
for /d %%D in ("%ROOT%Releases\*") do if exist "%%D\soroban.db" set "OLDDATA=%%D"
if not defined OLDDATA goto data_ok
echo.
echo ========================================
echo   [!] YOUR LEDGER IS NOT IN THIS FOLDER
echo   Found existing data in: %OLDDATA%
echo   The release dir no longer carries the version number, so %RELEASE%
echo   is a brand-new EMPTY folder. Running the new soroban.exe as-is would
echo   create an EMPTY ledger and a NEW SECRET_KEY.
echo.
echo   MOVE these from the old folder into the new one BEFORE running the new exe:
echo       soroban.db  soroban.db-wal  soroban.db-shm  .env  plugins\
echo   .env holds the SECRET_KEY: without it everyone is logged out and the saved
echo   MySQL connection string can no longer be decrypted.
echo   This is a ONE-TIME move -- from now on every rebuild reuses this same folder.
echo ========================================
:data_ok

echo.
echo ========================================
echo   Build complete! Output dir: %RELEASE%
echo ========================================
dir /b "%RELEASE%"
echo ----------------------------------------
echo   Run soroban.exe. It creates soroban.db + .env next to itself on first run,
echo   seeds an admin, then serves API + frontend on one port.
echo   This dir is also the DATA dir: soroban.db / .env / plugins\ live here and are
echo   preserved across rebuilds. Back them up before moving or deleting it.
echo   Open http://127.0.0.1:8620 in your browser (set BACKEND_PORT to change the port).
echo   Plugins ride inside the exe and are released into plugins\ on first run.
echo   Nothing to copy. Existing plugin folders are only refreshed when plugin.toml's
echo   version changed, and .venv\ / .state\ are never touched.
echo   Ship a "plugins" folder by hand only if you must -- and then FIRST delete
echo   .state\ .env *.log .venv\ from each plugin copy (they hold YOUR credentials).
echo ========================================
pause
exit /b 0

rem ===== Failure exits (kept flat so `if errorlevel` reads the real value) =====
:deps_fail
echo ERROR: backend deps install failed
pause
exit /b 1
:pyinstaller_fail
echo ERROR: pyinstaller install failed
pause
exit /b 1
:no_npm
echo ERROR: npm not found, please install Node.js: https://nodejs.org/
pause
exit /b 1
:npm_install_fail
popd
echo ERROR: npm install failed
pause
exit /b 1
:npm_build_fail
popd
echo ERROR: frontend build failed
pause
exit /b 1
:copy_fail
echo.
echo [ERROR] Could not copy soroban.exe into "%RELEASE%".
echo         It is probably still running -- close it and run this script again.
echo         The previous exe and your data in that folder are untouched.
pause
exit /b 1

:build_fail
echo ERROR: soroban.exe build failed
pause
exit /b 1
