@echo off
title Synology - Immich Migration
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo  Python wurde nicht gefunden.
    echo  Bitte installiere Python von https://www.python.org/downloads/
    echo  Wichtig: Beim Installieren "Add python.exe to PATH" ankreuzen.
    echo.
    pause
    exit /b 1
)

echo.
echo  Starte Synology - Immich Migration Tool ...
echo  Der Browser oeffnet sich gleich automatisch.
echo  Dieses Fenster bitte offen lassen, solange das Tool laeuft.
echo.

python synology_to_immich.py
if errorlevel 1 (
    echo.
    echo  ============================================================
    echo   Ein Fehler ist aufgetreten. Fehlermeldung siehe oben.
    echo  ============================================================
    echo.
    pause
)
