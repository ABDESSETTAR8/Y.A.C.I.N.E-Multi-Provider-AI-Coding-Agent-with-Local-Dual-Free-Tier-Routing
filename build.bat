@echo off
REM ---------------------------------------------------------------------
REM Build Y.A.C.I.N.E into a standalone Windows .exe.
REM Run from the project folder:  build.bat
REM Output: dist\yacine_server.exe
REM ---------------------------------------------------------------------
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] .venv not found. Create it first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

set PY=.venv\Scripts\python.exe

echo [1/4] Installing/updating build dependencies...
%PY% -m pip install -q --upgrade pip pyinstaller || exit /b 1
%PY% -m pip install -q -r requirements.txt || exit /b 1

echo [2/4] Running the test suite (build stops if anything fails)...
%PY% -m pytest -q || (echo [!] Tests failed - not building. & exit /b 1)

echo [3/4] Clearing previous build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [4/4] Building the executable...
%PY% -m PyInstaller --noconfirm --clean yacine.spec || exit /b 1

echo.
echo ==============================================================
echo  Build complete:  dist\yacine_server.exe
echo.
echo  Run it:          dist\yacine_server.exe serve
echo  Then open:       http://127.0.0.1:8765/ui
echo.
echo  The API key is created on first run and printed in the
echo  console. The UI authenticates itself automatically.
echo ==============================================================
endlocal
