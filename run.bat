@echo off
REM Start Cliphound, then open http://127.0.0.1:8765 in your browser.
cd /d "%~dp0"
echo.
echo   Cliphound is starting. The first start installs everything and can take a few minutes.
echo   When you see "Application startup complete", open http://127.0.0.1:8765
echo   To stop it, close this window or press Ctrl+C.
echo.
uv run uvicorn cliphound.app:app --host 127.0.0.1 --port 8765
pause
