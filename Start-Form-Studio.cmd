@echo off
setlocal
cd /d "%~dp0"
if defined PHOTO2CAD_CAD_PYTHON if exist "%PHOTO2CAD_CAD_PYTHON%" goto custom_python
if exist "%USERPROFILE%\mcp-servers\overnight-ai\cadenv\Scripts\python.exe" goto cad_python
if exist "%USERPROFILE%\mcp-servers\overnight-ai\cadenv\python.exe" goto portable_python
where py >nul 2>nul
if not errorlevel 1 goto py_launcher
where python >nul 2>nul
if not errorlevel 1 goto system_python
echo Python was not found. Set PHOTO2CAD_CAD_PYTHON to the Python in your existing cadenv.
pause
exit /b 1
:custom_python
"%PHOTO2CAD_CAD_PYTHON%" -B app.py
goto done
:cad_python
"%USERPROFILE%\mcp-servers\overnight-ai\cadenv\Scripts\python.exe" -B app.py
goto done
:portable_python
"%USERPROFILE%\mcp-servers\overnight-ai\cadenv\python.exe" -B app.py
goto done
:py_launcher
py -3 -B app.py
goto done
:system_python
python -B app.py
:done
if errorlevel 1 pause
