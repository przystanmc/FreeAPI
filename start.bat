@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Nie znaleziono Pythona. Zainstaluj Python 3.9+ ze strony https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist ".venv\Scripts\activate.bat" (
    echo Tworze srodowisko wirtualne .venv ...
    python -m venv .venv
)
call ".venv\Scripts\activate.bat"

python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo Instalacja zaleznosci nie powiodla sie.
    pause
    exit /b 1
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Utworzono plik .env. Wpisz w nim swoje klucze API, zapisz i uruchom start.bat ponownie.
    notepad ".env"
    pause
    exit /b 0
)

python run_all.py %*
echo.
pause
