@echo off
title Form Studio setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
timeout /t 15
