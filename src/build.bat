@echo off
setlocal

REM --- paths ---
set "VENV_PY=%~dp0venv\Scripts\python.exe"

echo =====================================
echo   Checking venv Python...
echo =====================================

if not exist "%VENV_PY%" (
  echo Fehler: "%VENV_PY%" nicht gefunden.
  echo Stelle sicher, dass der Ordner "venv" existiert und ein venv ist.
  pause
  exit /b 1
)

echo =====================================
echo   Ensuring PyInstaller is installed...
echo =====================================

"%VENV_PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
  echo Fehler: pip ist im venv nicht verfuegbar.
  pause
  exit /b 1
)

"%VENV_PY%" -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
  echo PyInstaller nicht gefunden - installiere...
  "%VENV_PY%" -m pip install -U pyinstaller
  if errorlevel 1 (
    echo Fehler: PyInstaller-Installation fehlgeschlagen.
    pause
    exit /b 1
  )
)

echo.
echo =====================================
echo   Building WaveScope.exe...
echo =====================================

"%VENV_PY%" -m PyInstaller --clean --noconsole --onefile --name WaveScope scope_gui.py

if errorlevel 1 (
  echo.
  echo Build fehlgeschlagen.
  pause
  exit /b 1
)

echo.
echo =====================================
echo   Build erfolgreich!
echo   Die EXE liegt im Ordner: dist
echo =====================================
pause
endlocal