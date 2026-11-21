@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 최근 3일치 데이터 수집 (작업 스케줄러에 매일 등록해서 사용)
venv\Scripts\python collector.py --days 3 >> data\auto_collect.log 2>&1
