@echo off
echo Music Finder - starting up...

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Download it from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Check ffmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo ERROR: ffmpeg not found. Install it with:
    echo   winget install ffmpeg
    echo Then open a new terminal and try again.
    pause
    exit /b 1
)

:: Install dependencies if needed
pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies.
        pause
        exit /b 1
    )
)

echo.
echo Starting server at http://localhost:8000
echo Press Ctrl+C to stop.
echo.
start "" http://localhost:8000
uvicorn main:app --port 8000
pause
