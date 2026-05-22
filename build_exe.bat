@echo off
setlocal
cd /d "%~dp0"

py -m pip install --upgrade pip
py -m pip install -r requirements.txt
py -m PyInstaller --onefile --clean --name epikrisis_control_2023_2025 src\epikrisis_finder.py

echo.
echo Built: dist\epikrisis_control_2023_2025.exe
pause
