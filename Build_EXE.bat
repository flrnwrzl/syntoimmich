@echo off
title Baue Synology-Immich-Migration.exe
cd /d "%~dp0"

echo.
echo  ============================================================
echo   Baue Synology-Immich-Migration.exe
echo   Dauert 1-2 Minuten ...
echo  ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo  Python nicht gefunden!
    echo  https://www.python.org/downloads  ^(Haken bei "Add to PATH"^)
    pause & exit /b 1
)

echo  [1/4] Installiere Abhaengigkeiten ...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet --upgrade pyinstaller requests
if errorlevel 1 ( echo Fehler! & pause & exit /b 1 )

echo  [2/4] Ermittle requests-Pfad ...
for /f "delims=" %%i in ('python -c "import requests, os; print(os.path.dirname(requests.__file__))"') do set REQUESTS_DIR=%%i
echo  requests gefunden in: %REQUESTS_DIR%

echo  [3/4] Baue exe ...
python -m PyInstaller ^
    --onefile ^
    --console ^
    --name "Synology-Immich-Migration" ^
    --paths "%REQUESTS_DIR%" ^
    --hidden-import=requests ^
    --hidden-import=requests.adapters ^
    --hidden-import=requests.auth ^
    --hidden-import=requests.cookies ^
    --hidden-import=requests.exceptions ^
    --hidden-import=requests.hooks ^
    --hidden-import=requests.models ^
    --hidden-import=requests.sessions ^
    --hidden-import=requests.structures ^
    --hidden-import=requests.utils ^
    --hidden-import=urllib3 ^
    --hidden-import=urllib3.util ^
    --hidden-import=urllib3.util.retry ^
    --hidden-import=urllib3.util.ssl_ ^
    --hidden-import=urllib3.poolmanager ^
    --hidden-import=urllib3.connectionpool ^
    --hidden-import=certifi ^
    --hidden-import=charset_normalizer ^
    --hidden-import=idna ^
    --collect-all requests ^
    --collect-all urllib3 ^
    --collect-all certifi ^
    synology_to_immich.py

echo  [4/4] Pruefe Ergebnis ...
echo.
if exist "dist\Synology-Immich-Migration.exe" (
    echo  ============================================================
    echo   Erfolgreich erstellt:
    echo   %cd%\dist\Synology-Immich-Migration.exe
    echo.
    echo   Einfach doppelklicken - kein Python noetig.
    echo  ============================================================
) else (
    echo  ============================================================
    echo   FEHLER: exe nicht erstellt.
    echo   Bitte Meldungen oben nach "ERROR" durchsuchen.
    echo  ============================================================
)
echo.
pause
