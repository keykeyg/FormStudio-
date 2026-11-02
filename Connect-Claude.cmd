@echo off
setlocal
cd /d "%~dp0"
set "PY="
if defined PHOTO2CAD_CAD_PYTHON if exist "%PHOTO2CAD_CAD_PYTHON%" set "PY=%PHOTO2CAD_CAD_PYTHON%"
if not defined PY if exist "%USERPROFILE%\mcp-servers\overnight-ai\cadenv\Scripts\python.exe" set "PY=%USERPROFILE%\mcp-servers\overnight-ai\cadenv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\mcp-servers\overnight-ai\cadenv\python.exe" set "PY=%USERPROFILE%\mcp-servers\overnight-ai\cadenv\python.exe"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
  echo Python was not found. Set PHOTO2CAD_CAD_PYTHON to the Python in your cadenv.
  pause
  exit /b 1
)
echo Adding Form Studio to Claude Desktop using %PY%
"%PY%" -B connect_claude.py "%PY%"
pause
