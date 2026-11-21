@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 가상환경 만드는 중...
python -m venv venv
if errorlevel 1 (
  echo 파이썬이 없습니다. https://www.python.org 에서 설치하고, 설치 첫 화면의 "Add python.exe to PATH" 를 꼭 체크하세요.
  pause
  exit /b
)
echo [2/2] 필요한 프로그램 설치 중... (몇 분 걸립니다)
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\pip install -r requirements.txt
echo.
echo 설치 완료! 이제 "실행.bat" 을 더블클릭하세요.
pause
