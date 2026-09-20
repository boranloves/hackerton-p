@echo off
rem =====================================================
rem  냉장고 털이 AI — 운영 실행 스크립트 (더블클릭 실행)
rem  종료: 이 창을 닫거나 Ctrl+C
rem  LAN 공유가 필요하면 아래 SET HOST 줄의 주석을 해제
rem =====================================================
cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python
rem set HOST=0.0.0.0
rem set PORT=5000
title 냉장고 털이 AI
"%PY%" app.py
pause
