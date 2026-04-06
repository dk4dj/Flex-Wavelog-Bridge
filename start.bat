@echo off
REM ============================================================
REM  FlexRadio - Wavelog Bridge  -  Starter
REM  Nutzt die ueber den Windows Store installierte Python-Runtime
REM  (python3.exe aus %LOCALAPPDATA%\Microsoft\WindowsApps)
REM ============================================================
setlocal EnableDelayedExpansion

SET SCRIPT_DIR=%~dp0
SET VENV_DIR=%SCRIPT_DIR%venv

REM ---- 1. Python aus dem Windows Store suchen ----------------
REM  Der Store installiert python3.exe nach:
REM    %LOCALAPPDATA%\Microsoft\WindowsApps\
REM  Auch python.exe ist dort verfuegbar.

SET PY=

FOR %%V IN (python3.12 python3.11 python3.10 python3) DO (
    IF NOT DEFINED PY (
        WHERE %%V >nul 2>&1
        IF !ERRORLEVEL! EQU 0 (
            FOR /F "delims=" %%P IN ('WHERE %%V 2^>nul') DO (
                IF NOT DEFINED PY SET "PY=%%P"
            )
        )
    )
)

IF NOT DEFINED PY (
    WHERE python >nul 2>&1
    IF !ERRORLEVEL! EQU 0 (
        FOR /F "delims=" %%P IN ('WHERE python 2^>nul') DO (
            IF NOT DEFINED PY SET "PY=%%P"
        )
    )
)

IF NOT DEFINED PY (
    echo.
    echo  FEHLER: Python wurde nicht gefunden.
    echo.
    echo  Bitte Python ueber den Microsoft Store installieren:
    echo    Start - Microsoft Store - "Python 3.12" suchen - Installieren
    echo.
    echo  Alternativ direkt: https://apps.microsoft.com/detail/9NCVDN91XZQP
    echo.
    pause
    exit /b 1
)

echo Verwende Python: %PY%

REM ---- 2. Virtuelle Umgebung anlegen falls noetig -----------
IF NOT EXIST "%VENV_DIR%\Scripts\activate.bat" (
    echo Erstelle virtuelle Umgebung in %VENV_DIR% ...
    "%PY%" -m venv "%VENV_DIR%"
    IF !ERRORLEVEL! NEQ 0 (
        echo FEHLER: venv konnte nicht erstellt werden.
        pause
        exit /b 1
    )
)

REM ---- 3. Abhaengigkeiten installieren ----------------------
echo Pruefe und installiere Abhaengigkeiten ...
"%VENV_DIR%\Scripts\python.exe" -m pip install --quiet --upgrade pip
"%VENV_DIR%\Scripts\pip.exe" install --quiet -r "%SCRIPT_DIR%requirements.txt"

IF !ERRORLEVEL! NEQ 0 (
    echo.
    echo  FEHLER: pip install fehlgeschlagen.
    echo  Bitte Internetverbindung pruefen.
    echo.
    pause
    exit /b 1
)

REM ---- 4. App starten (kein Konsolenfenster) ----------------
echo Starte FlexRadio Wavelog Bridge ...

SET VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe
IF EXIST "%VENV_PYW%" (
    "%VENV_PYW%" "%SCRIPT_DIR%flex_wavelog_bridge.py"
) ELSE (
    START "" /B "%VENV_DIR%\Scripts\python.exe" "%SCRIPT_DIR%flex_wavelog_bridge.py"
)
