@echo off
setlocal
cd /d "%~dp0"

echo Field Planner
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.11 or newer, then run this file again.
    echo.
    pause
    exit /b 1
)

python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed.
    echo Press any key to exit.
    pause >nul
    exit /b 1
)

start "" "http://localhost:8501"
python -m streamlit run app.py --server.port 8501 --server.headless true
if errorlevel 1 (
    echo.
    echo Streamlit exited with an error.
    echo Press any key to exit.
    pause >nul
    exit /b 1
)

pause
