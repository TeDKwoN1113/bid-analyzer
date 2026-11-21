@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 입찰 분석기를 시작합니다. 이 창을 닫으면 프로그램이 꺼집니다.
start "" http://localhost:8501
venv\Scripts\streamlit run app.py
